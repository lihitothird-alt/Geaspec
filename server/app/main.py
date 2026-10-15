"""Web サイト・管理画面・Stripe Webhook・Agent API をまとめた FastAPI アプリ。

起動 (開発):
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import asyncio
import hmac
import re
import secrets
import zipfile
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import clock, feedback, invite, services, social
from .config import Settings
from .db import Base, add_missing_columns, make_engine, make_sessionmaker
from .models import Booking, BookingStatus, Feedback, Host, Slot, User
from .payments import PROVIDER_LABELS, PaymentGateway, build_payments
from .security import connection_code, hash_password, hash_token, new_agent_token, verify_password
from .mail import Mailer, build_mailer
from .vpn import VpnProvider, build_vpn

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
META_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")
# 合言葉なしで通すパス。Agent・決済サービスからの通知は機械が呼ぶので、それぞれ別の方法で認証している
SITE_GATE_OPEN_PREFIXES = (
    "/site-login", "/join", "/guide", "/legal/", "/healthz", "/robots.txt", "/static/", "/api/agent/", "/stripe/webhook",
    # ホーム画面アプリの設定と裏方のスクリプト。ブラウザは合言葉の Cookie を付けずに読みに来る
    "/manifest.webmanifest", "/sw.js",
)
LEGAL_PAGES = {"tokushoho": "特定商取引法に基づく表記", "terms": "利用規約", "privacy": "プライバシーポリシー"}


def create_app(
    settings: Settings | None = None,
    payments: dict[str, PaymentGateway] | None = None,
    vpn: VpnProvider | None = None,
    mailer: Mailer | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    add_missing_columns(engine)

    app = FastAPI(title=settings.service_name, docs_url=None, redoc_url=None)
    app.state.settings = settings
    Slot.turnover = dt.timedelta(minutes=max(0, settings.turnover_minutes))
    app.state.Session = make_sessionmaker(engine)
    app.state.payments = payments or build_payments(settings)
    app.state.vpn = vpn or build_vpn(settings)
    app.state.mailer = mailer or build_mailer(settings)
    media_root = Path(settings.media_dir)
    (media_root / "screenshots").mkdir(parents=True, exist_ok=True)
    @app.middleware("http")
    async def private_site(request: Request, call_next):
        """検索エンジンに載せない。合言葉か招待リンクを使う設定なら、招待した人だけに見せる。"""
        path = request.url.path
        if (settings.site_password or settings.invite_only) and not path.startswith(SITE_GATE_OPEN_PREFIXES):
            if not request.session.get("site_ok"):
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/site-login?next={target}", status_code=303)
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    # SessionMiddleware を後から追加して外側にし、上の合言葉チェックでセッションを読めるようにする
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.secure_cookies,
        max_age=14 * 24 * 3600,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["jst"] = lambda v, fmt="%Y/%m/%d %H:%M": clock.to_jst(v).strftime(fmt) if v else "-"
    templates.env.filters["yen"] = lambda v: f"{v:,}円"
    weekdays = "月火水木金土日"
    templates.env.filters["wday"] = lambda v: weekdays[clock.to_jst(v).weekday()]
    templates.env.filters["iso"] = services.iso_utc
    templates.env.globals["until_text"] = services.until_text
    templates.env.globals["provider_labels"] = PROVIDER_LABELS
    templates.env.globals["trial_minutes"] = 0 if settings.free_mode else settings.trial_minutes
    templates.env.globals["free_mode"] = settings.free_mode
    templates.env.globals["simple"] = settings.simple_mode
    templates.env.globals["mail_enabled"] = app.state.mailer.enabled

    # --- 共通 ---------------------------------------------------------------

    # --- お知らせメール -------------------------------------------------------

    def send_mail(to: str, subject: str, body: str) -> None:
        if not to:
            return
        footer = f"\n\n――\n{settings.service_name}\n{settings.base_url}\n（このメールは自動で送っています）"
        try:
            app.state.mailer.send(to, f"【{settings.service_name}】{subject}", body + footer)
        except Exception:  # noqa: BLE001 - メールが送れなくても予約などの処理は続ける
            log.exception("メールの準備に失敗しました: %s", subject)

    def booking_when(booking: Booking) -> str:
        s = booking.slot
        return f"{clock.to_jst(s.start_at):%m/%d}（{'月火水木金土日'[clock.to_jst(s.start_at).weekday()]}）{clock.to_jst(s.start_at):%H:%M}〜{clock.to_jst(s.play_end_at):%H:%M}"

    def confirm_and_notify(db: Session, booking: Booking, now: dt.datetime) -> None:
        """支払い完了 → 予約確定。はじめて確定したときだけ、借り手と貸し手にメールを送る。"""
        was = booking.status
        services.confirm_booking(db, booking, app.state.vpn, now)
        if was == BookingStatus.CONFIRMED or booking.status != BookingStatus.CONFIRMED:
            return
        when, host = booking_when(booking), booking.slot.host
        send_mail(
            booking.user.email,
            f"予約が確定しました {when}",
            f"ご予約ありがとうございます。\n\n日時: {when}\nPC: {host.name}\n\n"
            f"時間になったら Quest で Virtual Desktop を開いてください。くわしい手順は予約ページにあります。\n"
            f"{settings.base_url}/bookings/{booking.id}",
        )
        if host.owner and host.owner.email != booking.user.email:
            send_mail(
                host.owner.email,
                f"新しい予約が入りました {when}",
                f"あなたの PC「{host.name}」に予約が入りました。\n\n日時: {when}\n\n"
                "時間になると、アプリが自動で準備します。PC の電源を入れ、貸し出し専用のユーザーでサインインしたままにしてください。",
            )

    def get_db(request: Request) -> Iterator[Session]:
        db = request.app.state.Session()
        try:
            yield db
        finally:
            db.close()

    def current_user(request: Request, db: Session) -> User | None:
        uid = request.session.get("uid")
        return db.get(User, uid) if uid else None

    def is_admin(user: User | None) -> bool:
        return bool(user and user.email.lower() in settings.admin_emails)

    def flash(request: Request, message: str, kind: str = "info") -> None:
        request.session.setdefault("flash", []).append({"kind": kind, "message": message})

    def render(request: Request, db: Session, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        user = current_user(request, db)
        messages = request.session.pop("flash", [])
        return templates.TemplateResponse(
            request,
            name,
            {
                "settings": settings, "user": user, "is_admin": is_admin(user), "messages": messages,
                "is_lender": bool(user and db.scalar(select(Host.id).where(Host.owner_id == user.id).limit(1))),
                **ctx,
            },
            status_code=status_code,
        )

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def require_user(request: Request, db: Session) -> User:
        user = current_user(request, db)
        if not user:
            raise HTTPException(status_code=303, headers={"Location": f"/login?next={request.url.path}"})
        return user

    def require_admin(request: Request, db: Session) -> User:
        user = require_user(request, db)
        if not is_admin(user):
            raise HTTPException(status_code=404)
        return user

    # --- 公開ページ ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, ref: str = "", db: Session = Depends(get_db)):
        # 広告などの流入元 (例: /?ref=himari-ryokan)。登録時にユーザーへ記録する
        if ref and REF_RE.match(ref):
            request.session["ref"] = ref
        slots = services.available_slots(db, clock.utcnow(), settings.hold_minutes)
        min_price = min((s.price_yen for s in slots), default=None)
        return render(request, db, "index.html", slot_count=len(slots), min_price=min_price)

    @app.get("/guide", response_class=HTMLResponse)
    def guide(request: Request, db: Session = Depends(get_db)):
        """画像つきの「はじめての方へ」。申し込む前にも読めるよう、招待リンクなしで見られる。

        ?for=lend で PC を貸す人向けの説明に切り替える (上のスイッチでいつでも切り替えられる)。
        """
        lend = request.query_params.get("for") == "lend"
        return render(request, db, "guide.html", lend=lend)

    @app.get("/legal/{page}", response_class=HTMLResponse)
    def legal(page: str, request: Request, db: Session = Depends(get_db)):
        if page not in LEGAL_PAGES or (page == "tokushoho" and settings.free_mode):
            raise HTTPException(404)
        return render(request, db, "legal.html", page=page, title=LEGAL_PAGES[page])

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n"

    # --- ホーム画面アプリ (iPhone / Android) --------------------------------

    @app.get("/manifest.webmanifest")
    def web_manifest():
        name = settings.service_name
        manifest = {
            "name": name,
            "short_name": name,
            "description": "Quest ひとつで PC 版 VRChat を予約",
            "lang": "ja",
            "id": "/",
            "start_url": "/bookings?source=app",
            "scope": "/",
            "display": "standalone",
            "background_color": "#110e21",
            "theme_color": "#5b3cf0",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
        return JSONResponse(manifest, media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        # サイト全体を範囲にするため /static/ ではなくここから配る。中身が変わっても古いものが残らないよう毎回確認させる
        return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/app", response_class=HTMLResponse)
    def app_install(request: Request, db: Session = Depends(get_db)):
        hidden_in_simple_mode()
        return render(request, db, "app.html")

    def hidden_in_simple_mode() -> None:
        """試作版では使わない機能 (フォロー・貸し手ページと評価・アプリとして使う) は、ないものとして扱う。"""
        if settings.simple_mode:
            raise HTTPException(404)

    def safe_next(next: str, default: str = "/") -> str:
        # オープンリダイレクト防止: サイト内のパスだけ許可
        return next if next.startswith("/") and not next.startswith("//") else default

    @app.get("/site-login", response_class=HTMLResponse)
    def site_login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
        return render(request, db, "site_login.html", next=safe_next(next))

    @app.get("/join")
    def join(request: Request, t: str = "", next: str = "", db: Session = Depends(get_db)):
        """Discord のボットが渡した本人専用リンク。開いた人はサイトに入れて、Meta ユーザー名も引き継ぐ。"""
        data = invite.read_token(settings.secret_key, t, clock.utcnow().replace(tzinfo=dt.timezone.utc))
        if data is None:
            return render(request, db, "site_login.html", status_code=400, next="/", expired=True)
        request.session["site_ok"] = True
        if META_USERNAME_RE.match(data["meta"]):
            request.session["invite_meta"] = data["meta"]
        if next:
            return redirect(safe_next(next))
        return redirect("/slots" if current_user(request, db) else "/signup")

    @app.post("/site-login")
    async def site_login(request: Request, password: str = Form(""), next: str = Form("/")):
        if settings.site_password and hmac.compare_digest(password.encode(), settings.site_password.encode()):
            request.session["site_ok"] = True
            return redirect(safe_next(next))
        await asyncio.sleep(1)  # 総当たりを遅くする
        db = request.app.state.Session()
        try:
            return render(
                request, db, "site_login.html", status_code=401,
                error="合言葉が違います", next=safe_next(next),
            )
        finally:
            db.close()

    # --- アカウント ---------------------------------------------------------

    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request, db: Session = Depends(get_db)):
        return render(request, db, "signup.html")

    @app.post("/signup")
    def signup(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        agree: str = Form(""),
        db: Session = Depends(get_db),
    ):
        email = email.strip().lower()
        error = None
        if not EMAIL_RE.match(email):
            error = "メールアドレスの形式が正しくありません"
        elif len(password) < 8:
            error = "パスワードは 8 文字以上にしてください"
        elif not agree:
            error = "利用規約とプライバシーポリシーへの同意が必要です"
        if error:
            return render(request, db, "signup.html", status_code=400, error=error, email=email)
        user = User(email=email, password_hash=hash_password(password), signup_ref=request.session.get("ref", ""),
                    meta_username=request.session.get("invite_meta", ""))
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return render(
                request, db, "signup.html", status_code=400, error="このメールアドレスは登録済みです", email=email
            )
        request.session["uid"] = user.id
        if user.meta_username:
            flash(request, "登録しました。空き枠から好きな時間をえらんでください")
            return redirect("/slots")
        flash(request, "登録しました。Meta (Quest) のユーザー名を設定してください")
        return redirect("/account")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/slots", db: Session = Depends(get_db)):
        return render(request, db, "login.html", next=next)

    @app.post("/login")
    def login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        next: str = Form("/slots"),
        db: Session = Depends(get_db),
    ):
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if not user or not verify_password(password, user.password_hash):
            return render(
                request, db, "login.html", status_code=400,
                error="メールアドレスかパスワードが違います", next=next, email=email,
            )
        site_ok, invite_meta = request.session.get("site_ok"), request.session.get("invite_meta")
        request.session.clear()
        request.session["uid"] = user.id
        if site_ok:
            request.session["site_ok"] = True
        if invite_meta and not user.meta_username:
            user.meta_username = invite_meta
            db.commit()
        return redirect(safe_next(next, "/slots"))

    @app.post("/logout")
    def logout(request: Request):
        site_ok = request.session.get("site_ok")
        request.session.clear()
        if site_ok:
            request.session["site_ok"] = True
        return redirect("/")

    @app.get("/account", response_class=HTMLResponse)
    def account_form(request: Request, db: Session = Depends(get_db)):
        require_user(request, db)
        return render(request, db, "account.html")

    @app.post("/account")
    def account_save(
        request: Request, meta_username: str = Form(""), nickname: str = Form(""), db: Session = Depends(get_db)
    ):
        user = require_user(request, db)
        meta_username = meta_username.strip()
        nickname = " ".join(nickname.split())
        if not META_USERNAME_RE.match(meta_username):
            return render(
                request, db, "account.html", status_code=400,
                error="Meta のユーザー名は英数字と . _ - で 2〜64 文字です",
            )
        if len(nickname) > social.NICKNAME_MAX or "@" in nickname:
            return render(
                request, db, "account.html", status_code=400,
                error=f"ニックネームは {social.NICKNAME_MAX} 文字までで、メールアドレスは使えません",
            )
        user.meta_username = meta_username
        user.nickname = nickname
        db.commit()
        flash(request, "保存しました")
        return redirect("/slots")

    # --- 予約 ---------------------------------------------------------------

    @app.get("/slots", response_class=HTMLResponse)
    def slots(request: Request, view: str = "", db: Session = Depends(get_db)):
        now = clock.utcnow()
        user = current_user(request, db)
        items = services.available_slots(db, now, settings.hold_minutes)
        followed = {u.id for u in social.following_lenders(db, user)} if user else set()
        if view == "following" and not settings.simple_mode:
            items = [s for s in items if s.host.owner_id in followed]
        lenders = {}
        for s in items:
            owner = s.host.owner
            if owner and owner.id not in lenders:
                lenders[owner.id] = {"user": owner, "rating": social.lender_rating(db, owner)}
        # 未ログインなら「初回無料」を案内するため True 扱い
        trial = services.trial_eligible(db, user, now, settings.hold_minutes) if user else True
        prices = {
            s.id: s.price_yen - (services.trial_discount_yen(s, settings.trial_minutes) if trial else 0)
            for s in items
        }
        return render(
            request, db, "slots.html", slots=items, trial_eligible=trial, trial_prices=prices,
            lenders=lenders, followed=followed, view=view,
        )

    @app.get("/slots/{slot_id}/confirm", response_class=HTMLResponse)
    def confirm_page(slot_id: int, request: Request, db: Session = Depends(get_db)):
        user = require_user(request, db)
        if not user.meta_username:
            flash(request, "先に Meta (Quest) のユーザー名を登録してください", "error")
            return redirect("/account")
        slot = db.get(Slot, slot_id) or _404()
        now = clock.utcnow()
        if not services.is_available(db, slot, now, settings.hold_minutes):
            flash(request, "この枠はすでに埋まっています", "error")
            return redirect("/slots")
        amount, discount = services.quote(db, user, slot, now, settings.hold_minutes, settings.trial_minutes,
                                          settings.free_mode)
        return render(
            request, db, "confirm.html", slot=slot, amount=amount, discount=discount,
            providers=list(request.app.state.payments),
        )

    @app.post("/slots/{slot_id}/book")
    def book(slot_id: int, request: Request, provider: str = Form(""), db: Session = Depends(get_db)):
        user = require_user(request, db)
        slot = db.get(Slot, slot_id) or _404()
        gateways: dict[str, PaymentGateway] = request.app.state.payments
        now = clock.utcnow()
        if user.meta_username and services.is_available(db, slot, now, settings.hold_minutes):
            amount, _ = services.quote(db, user, slot, now, settings.hold_minutes, settings.trial_minutes,
                                       settings.free_mode)
            if amount > 0 and provider not in gateways:
                if len(gateways) != 1:
                    # 支払い方法を選んでもらう
                    return redirect(f"/slots/{slot_id}/confirm")
                provider = next(iter(gateways))
        try:
            booking = services.create_booking(
                db, user, slot, now, settings.hold_minutes, settings.trial_minutes, settings.free_mode
            )
        except services.BookingError as exc:
            flash(request, str(exc), "error")
            return redirect("/account" if not user.meta_username else "/slots")

        if booking.amount_yen == 0:
            booking.payment_provider = "free"
            confirm_and_notify(db, booking, now)
            flash(request, "予約が確定しました" if settings.free_mode or not booking.discount_yen
                  else f"予約が確定しました（初回 {settings.trial_minutes} 分無料）")
            return redirect(f"/bookings/{booking.id}")

        description = (
            f"{settings.service_name} {clock.to_jst(slot.start_at):%m/%d %H:%M}〜"
            f"{clock.to_jst(slot.play_end_at):%H:%M} ({slot.host.name})"
        )
        booking.payment_provider = provider
        try:
            checkout = gateways[provider].create_checkout(booking, description)
        except Exception:  # noqa: BLE001
            log.exception("決済ページの作成に失敗: booking=%s provider=%s", booking.id, provider)
            services.cancel_booking(db, booking, request.app.state.vpn, "決済ページの作成に失敗")
            flash(request, "決済ページを作成できませんでした。時間をおいて再度お試しください", "error")
            return redirect("/slots")

        if checkout is None:
            confirm_and_notify(db, booking, now)
            flash(request, "予約が確定しました")
            return redirect(f"/bookings/{booking.id}")
        url, payment_ref = checkout
        booking.payment_ref = payment_ref
        db.commit()
        return redirect(url)

    @app.get("/bookings", response_class=HTMLResponse)
    def my_bookings(request: Request, db: Session = Depends(get_db)):
        user = require_user(request, db)
        items = db.scalars(
            select(Booking).join(Slot).where(Booking.user_id == user.id).order_by(Slot.start_at.desc())
        ).all()
        return render(request, db, "bookings.html", bookings=items)

    @app.get("/bookings/{booking_id}", response_class=HTMLResponse)
    def booking_detail(booking_id: int, request: Request, paid: int = 0, db: Session = Depends(get_db)):
        user = require_user(request, db)
        booking = db.get(Booking, booking_id)
        if not booking or (booking.user_id != user.id and not is_admin(user)):
            raise HTTPException(404)
        lender = booking.slot.host.owner
        return render(
            request, db, "booking.html", booking=booking, paid=paid, now=clock.utcnow(),
            lender=lender, my_review=social.my_review(db, booking, user),
            can_review=booking.user_id == user.id and social.can_review(db, booking, user, clock.utcnow()),
            following=bool(lender and (rel := social.relation(db, user, lender)) and rel.following),
            vpn_mode=settings.vpn_mode, hold_minutes=settings.hold_minutes,
            # 写真のダウンロードは借り手本人だけ (運営にも見せない)
            screenshots_ready=booking.user_id == user.id
            and services.screenshots_available(booking, media_root, clock.utcnow()),
        )

    @app.post("/bookings/{booking_id}/review")
    def review_booking(
        booking_id: int, request: Request, stars: int = Form(0), comment: str = Form(""),
        db: Session = Depends(get_db),
    ):
        """利用が終わった予約を評価する (借り手は貸し手を、貸し手は借り手を)。"""
        hidden_in_simple_mode()
        user = require_user(request, db)
        booking = db.get(Booking, booking_id)
        if not booking or social.review_role(booking, user) is None:
            raise HTTPException(404)
        try:
            social.add_review(db, booking, user, stars, comment, clock.utcnow())
            flash(request, "評価を送りました。ありがとうございます")
        except social.SocialError as exc:
            flash(request, str(exc), "error")
        return redirect("/lend" if social.review_role(booking, user) == "lender" else f"/bookings/{booking.id}")

    # --- お得意さん関係 (フォロー・貸し手ページ) ---------------------------------

    @app.get("/lenders/{lender_id}", response_class=HTMLResponse)
    def lender_profile(lender_id: int, request: Request, db: Session = Depends(get_db)):
        hidden_in_simple_mode()
        lender = db.get(User, lender_id) or _404()
        hosts = social.lender_hosts(db, lender)
        if not hosts:
            raise HTTPException(404)
        user = current_user(request, db)
        rel = social.relation(db, user, lender) if user else None
        now = clock.utcnow()
        slots = [s for s in services.available_slots(db, now, settings.hold_minutes) if s.host in hosts]
        return render(
            request, db, "lender.html", lender=lender, hosts=hosts, slots=slots[:12],
            rating=social.lender_rating(db, lender), reviews=social.reviews_of_lender(db, lender),
            follower_count=social.follower_count(db, lender), following=bool(rel and rel.following),
        )

    @app.post("/lenders/{lender_id}/follow")
    def follow_lender(lender_id: int, request: Request, on: int = Form(1), next: str = Form(""),
                      db: Session = Depends(get_db)):
        hidden_in_simple_mode()
        user = require_user(request, db)
        lender = db.get(User, lender_id) or _404()
        try:
            social.set_following(db, user, lender, bool(on))
            flash(request, f"{lender.display_name} さんを{'フォローしました' if on else 'フォローから外しました'}")
        except social.SocialError as exc:
            flash(request, str(exc), "error")
        return redirect(safe_next(next, f"/lenders/{lender.id}"))

    @app.get("/following", response_class=HTMLResponse)
    def following_page(request: Request, db: Session = Depends(get_db)):
        hidden_in_simple_mode()
        user = require_user(request, db)
        lenders = social.following_lenders(db, user)
        return render(
            request, db, "following.html",
            lenders=[{"user": u, "rating": social.lender_rating(db, u)} for u in lenders],
        )

    @app.get("/lend", response_class=HTMLResponse)
    def lend_page(request: Request, db: Session = Depends(get_db)):
        """貸し手のページ: フォロワー・これからの予約・終わった予約の評価。"""
        user = require_user(request, db)
        hosts = social.lender_hosts(db, user)
        if not hosts:
            return render(request, db, "lend.html", hosts=[], followers=[], upcoming=[], finished=[])
        now = clock.utcnow()
        upcoming = [b for b in social.lender_bookings(db, user, (BookingStatus.CONFIRMED, BookingStatus.ACTIVE))
                    if b.slot.end_at > now]
        finished = social.lender_bookings(db, user, (BookingStatus.COMPLETED,), limit=20)
        followers = social.followers(db, user)
        people = {b.user for b in upcoming + finished} | set(followers)
        ratings = {u.id: social.borrower_rating(db, u) for u in people}
        return render(
            request, db, "lend.html", hosts=hosts, followers=followers, upcoming=upcoming, finished=finished,
            ratings=ratings, rating=social.lender_rating(db, user),
            reviewable={b.id for b in finished if social.can_review(db, b, user, now)},
        )

    @app.get("/bookings/{booking_id}/screenshots")
    def booking_screenshots(booking_id: int, request: Request, db: Session = Depends(get_db)):
        user = require_user(request, db)
        booking = db.get(Booking, booking_id)
        # 運営 (管理者) であっても、借り手本人以外にはダウンロードさせない
        if not booking or booking.user_id != user.id:
            raise HTTPException(404)
        if not services.screenshots_available(booking, media_root, clock.utcnow()):
            raise HTTPException(404)
        return FileResponse(
            media_root / booking.screenshots_file,
            media_type="application/zip",
            filename=f"geaspec-{booking.id}-screenshots.zip",
        )

    @app.post("/bookings/{booking_id}/cancel")
    def booking_cancel(booking_id: int, request: Request, db: Session = Depends(get_db)):
        user = require_user(request, db)
        booking = db.get(Booking, booking_id)
        if not booking or booking.user_id != user.id:
            raise HTTPException(404)
        if booking.status != BookingStatus.PENDING_PAYMENT:
            flash(request, "支払い後のキャンセルは運営へご連絡ください", "error")
        else:
            services.cancel_booking(db, booking, request.app.state.vpn, "借り手がキャンセル")
            flash(request, "キャンセルしました")
        return redirect("/bookings")

    # --- 決済の完了通知 -----------------------------------------------------

    @app.get("/payments/paypal/return")
    def paypal_return(request: Request, booking_id: int = 0, token: str = "", db: Session = Depends(get_db)):
        gateway = request.app.state.payments.get("paypal")
        booking = db.get(Booking, booking_id)
        if not gateway or not booking or booking.payment_provider != "paypal" or booking.payment_ref != token:
            raise HTTPException(404)
        target = f"/bookings/{booking.id}"
        if booking.status in services.TAKEN_STATUSES:
            return redirect(target)  # リロードなどで 2 回目
        if booking.status not in (BookingStatus.PENDING_PAYMENT, BookingStatus.EXPIRED):
            flash(request, "この予約はキャンセルされています", "error")
            return redirect(target)
        if services.taken_by_other(booking):
            # 売上確定 (capture) 前なので、お金は動いていない
            services.cancel_booking(db, booking, request.app.state.vpn, "支払い前に枠が埋まりました（未請求）")
            flash(request, "お支払いの前に枠が埋まってしまいました。代金は請求されていません", "error")
            return redirect("/slots")
        try:
            result = gateway.capture(token)
        except Exception:  # noqa: BLE001
            log.exception("PayPal の capture に失敗: booking=%s", booking.id)
            flash(request, "お支払いを確認できませんでした。時間をおいてこのページを再読み込みしてください", "error")
            return redirect(target)
        if not result.ok or result.amount_yen != booking.amount_yen or result.booking_id not in (None, booking.id):
            log.error("PayPal の支払い内容が一致しません: booking=%s result=%s", booking.id, result)
            booking.note = (booking.note + "\n" if booking.note else "") + f"PayPal の確認に失敗: {result}"
            db.commit()
            flash(request, "お支払いを確認できませんでした。運営までご連絡ください", "error")
            return redirect(target)
        confirm_and_notify(db, booking, clock.utcnow())
        flash(request, "お支払いが完了し、予約が確定しました")
        return redirect(target)

    @app.post("/stripe/webhook")
    async def stripe_webhook(request: Request):
        gateway = request.app.state.payments.get("stripe")
        if not gateway:
            raise HTTPException(404)
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            result = gateway.parse_webhook(payload, signature)
        except Exception:  # noqa: BLE001 - 署名不正など
            log.warning("Stripe Webhook の検証に失敗しました")
            return JSONResponse({"ok": False}, status_code=400)
        if result.kind == "ignored" or not result.booking_id:
            return {"ok": True}
        db = request.app.state.Session()
        try:
            booking = db.get(Booking, result.booking_id)
            if not booking or booking.payment_provider != "stripe" or booking.payment_ref != result.payment_ref:
                log.warning("Webhook と一致する予約がありません: %s", result)
                return {"ok": True}
            if result.kind == "paid":
                confirm_and_notify(db, booking, clock.utcnow())
            elif result.kind == "expired":
                services.expire_booking(db, booking)
            return {"ok": True}
        finally:
            db.close()

    # --- テストの評価フォーム -------------------------------------------------

    def feedback_page(request: Request, db: Session, t: str, error: str = "", values: dict | None = None,
                      status_code: int = 200) -> HTMLResponse:
        return render(
            request, db, "feedback.html", status_code=status_code, fb=feedback, t=t, error=error,
            values=values or {}, sections=feedback.SECTIONS,
        )

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_form(request: Request, t: str = "", db: Session = Depends(get_db)):
        return feedback_page(request, db, t[:20])

    @app.post("/feedback", response_class=HTMLResponse)
    async def feedback_submit(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        values = {k: [str(v) for v in form.getlist(k)] for k in form.keys()}
        t = (values.get("t") or [""])[0][:20]
        try:
            answers = feedback.parse(values)
        except feedback.FeedbackError as exc:
            return feedback_page(request, db, t, str(exc), values, status_code=400)
        user = current_user(request, db)
        ref = int(t) if t.isdigit() else None
        db.add(Feedback(session_ref=ref, user_id=user.id if user else None,
                        answers=json.dumps(answers, ensure_ascii=False), created_at=clock.utcnow()))
        db.commit()
        return render(request, db, "feedback.html", fb=feedback, done=True, sections=feedback.SECTIONS)

    def feedback_rows(db: Session) -> list[dict]:
        rows = db.scalars(select(Feedback).order_by(Feedback.id)).all()
        return [{"created_at": clock.to_jst(f.created_at).strftime("%Y/%m/%d %H:%M"), "session_ref": f.session_ref,
                 "answers": json.loads(f.answers or "{}")} for f in rows]

    @app.get("/admin/feedback", response_class=HTMLResponse)
    def admin_feedback(request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        rows = feedback_rows(db)
        return render(request, db, "admin/feedback.html", fb=feedback, rows=rows, summary=feedback.summarize(rows))

    @app.get("/admin/feedback.csv")
    def admin_feedback_csv(request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        body = feedback.to_csv(feedback_rows(db)).encode("utf-8-sig")  # Excel で文字化けしないように
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="geaspec-feedback.csv"'})

    # --- 管理画面 -----------------------------------------------------------

    @app.get("/admin", response_class=HTMLResponse)
    def admin_index(request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        now = clock.utcnow()
        hosts = db.scalars(select(Host).order_by(Host.id)).all()
        upcoming = db.scalars(select(Slot).where(Slot.end_at > now).order_by(Slot.start_at)).all()
        bookings = db.scalars(select(Booking).order_by(Booking.id.desc()).limit(50)).all()
        return render(
            request, db, "admin/index.html", hosts=hosts, slots=upcoming, bookings=bookings, now=now,
            hold_minutes=settings.hold_minutes,
        )

    @app.post("/admin/hosts", response_class=HTMLResponse)
    def admin_create_host(
        request: Request,
        name: str = Form(...),
        vpn_address: str = Form(""),
        vpn_borrower_tag: str = Form(""),
        db: Session = Depends(get_db),
    ):
        require_admin(request, db)
        token = new_agent_token()
        host = Host(
            name=name.strip(), token_hash=hash_token(token),
            vpn_address=vpn_address.strip(), vpn_borrower_tag=vpn_borrower_tag.strip(),
        )
        db.add(host)
        db.commit()
        return render_token(request, db, host, token)

    @app.post("/admin/hosts/{host_id}/token", response_class=HTMLResponse)
    def admin_rotate_token(host_id: int, request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        host = db.get(Host, host_id) or _404()
        token = new_agent_token()
        host.token_hash = hash_token(token)
        db.commit()
        return render_token(request, db, host, token)

    def render_token(request: Request, db: Session, host: Host, token: str) -> HTMLResponse:
        return render(
            request, db, "admin/host_token.html", host=host, token=token,
            code=connection_code(settings.base_url, token),
            local_only="localhost" in settings.base_url or "127.0.0.1" in settings.base_url,
        )

    @app.post("/admin/hosts/{host_id}/approve")
    def admin_approve_host(
        host_id: int,
        request: Request,
        vpn_address: str = Form(""),
        vpn_borrower_tag: str = Form(""),
        db: Session = Depends(get_db),
    ):
        require_admin(request, db)
        host = db.get(Host, host_id) or _404()
        host.pending, host.active = False, True
        host.vpn_address, host.vpn_borrower_tag = vpn_address.strip(), vpn_borrower_tag.strip()
        db.commit()
        if host.owner:
            send_mail(
                host.owner.email,
                "PC の確認が終わりました",
                f"「{host.name}」を貸し出せるようになりました。\nアプリのカレンダーで、貸し出す時間を選んで「保存」を押してください。",
            )
        flash(request, f"「{host.name}」を承認しました。貸し出せるようになりました")
        return redirect("/admin")

    @app.post("/admin/test-mail")
    def admin_test_mail(request: Request, db: Session = Depends(get_db)):
        user = require_admin(request, db)
        if not app.state.mailer.enabled:
            flash(request, "メールの設定がまだです。サイトを起動する黒い画面で Gmail を設定してください", "warn")
            return redirect("/admin")
        send_mail(user.email, "テストメール", "このメールが届いていれば、お知らせメールの設定はできています。")
        flash(request, f"{user.email} にテストメールを送りました。届かないときは迷惑メールも見てください")
        return redirect("/admin")

    @app.post("/admin/hosts/{host_id}/toggle")
    def admin_toggle_host(host_id: int, request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        host = db.get(Host, host_id) or _404()
        host.active = not host.active
        db.commit()
        return redirect("/admin")

    @app.post("/admin/slots")
    def admin_create_slot(
        request: Request,
        host_id: int = Form(...),
        date: str = Form(...),
        start_time: str = Form(...),
        minutes: int = Form(...),
        price_yen: int = Form(...),
        repeat_days: int = Form(1),
        db: Session = Depends(get_db),
    ):
        require_admin(request, db)
        host = db.get(Host, host_id) or _404()
        try:
            start_jst = dt.datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            flash(request, "日付と時刻の形式が正しくありません", "error")
            return redirect("/admin")
        created = 0
        for day in range(max(1, min(repeat_days, 31))):
            start = clock.jst_to_utc(start_jst + dt.timedelta(days=day))
            try:
                services.create_slot(db, host, start, start + dt.timedelta(minutes=minutes), price_yen)
                created += 1
            except services.BookingError as exc:
                db.rollback()
                flash(request, f"{clock.to_jst(start):%m/%d}: {exc}", "error")
        if created:
            flash(request, f"{created} 件の枠を作成しました")
        return redirect("/admin")

    @app.post("/admin/slots/{slot_id}/delete")
    def admin_delete_slot(slot_id: int, request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        slot = db.get(Slot, slot_id) or _404()
        if slot.bookings:
            flash(request, "予約履歴のある枠は削除できません", "error")
        else:
            db.delete(slot)
            db.commit()
            flash(request, "枠を削除しました")
        return redirect("/admin")

    @app.get("/admin/hosts/{host_id}/schedule", response_class=HTMLResponse)
    def admin_schedule(host_id: int, request: Request, week: str = "", db: Session = Depends(get_db)):
        require_admin(request, db)
        host = db.get(Host, host_id) or _404()
        now = clock.utcnow()
        today = clock.to_jst(now).date()
        try:
            first = dt.date.fromisoformat(week) if week else today
        except ValueError:
            first = today
        start_jst = dt.datetime.combine(first, dt.time())
        start = clock.jst_to_utc(start_jst)
        end = start + dt.timedelta(days=7)
        cells: dict[dt.datetime, dict] = {}
        for item in services.host_schedule(db, host, start, end, now, settings.hold_minutes):
            s_jst = clock.to_jst(services.parse_iso_utc(item["start"])).replace(tzinfo=None)
            e_jst = clock.to_jst(services.parse_iso_utc(item["end"])).replace(tzinfo=None)
            t = s_jst.replace(minute=0, second=0, microsecond=0)
            while t < e_jst:
                cells[t] = item
                t += dt.timedelta(hours=1)
        now_jst = clock.to_jst(now).replace(tzinfo=None)
        days = [start_jst + dt.timedelta(days=d) for d in range(7)]
        return render(
            request, db, "admin/schedule.html", host=host, days=days, cells=cells, now_jst=now_jst,
            week=first, prev_week=first - dt.timedelta(days=7), next_week=first + dt.timedelta(days=7),
            today=today, price_yen=services.suggested_price(db, host), timedelta=dt.timedelta,
        )

    @app.post("/admin/hosts/{host_id}/schedule")
    def admin_schedule_save(
        host_id: int,
        request: Request,
        week: str = Form(...),
        price_yen: int = Form(...),
        starts: list[str] = Form([]),
        db: Session = Depends(get_db),
    ):
        require_admin(request, db)
        host = db.get(Host, host_id) or _404()
        try:
            first = dt.date.fromisoformat(week)
            chosen = {clock.jst_to_utc(dt.datetime.fromisoformat(v)) for v in starts}
        except ValueError:
            flash(request, "日時の形式が正しくありません", "error")
            return redirect(f"/admin/hosts/{host_id}/schedule")
        start = clock.jst_to_utc(dt.datetime.combine(first, dt.time()))
        try:
            result = services.set_hourly_slots(
                db, host, start, start + dt.timedelta(days=7), chosen, price_yen, clock.utcnow(), settings.hold_minutes
            )
        except services.BookingError as exc:
            db.rollback()
            flash(request, str(exc), "error")
        else:
            flash(request, f"保存しました（追加 {result['added']} 件・取り消し {result['removed']} 件）")
            for message in result["errors"]:
                flash(request, message, "error")
        return redirect(f"/admin/hosts/{host_id}/schedule?week={first.isoformat()}")

    @app.get("/admin/bookings/{booking_id}", response_class=HTMLResponse)
    def admin_booking(booking_id: int, request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        booking = db.get(Booking, booking_id) or _404()
        report = json.loads(booking.report_json) if booking.report_json else None
        return render(request, db, "admin/booking.html", booking=booking, report=report)

    @app.post("/admin/bookings/{booking_id}/cancel")
    def admin_cancel_booking(booking_id: int, request: Request, db: Session = Depends(get_db)):
        require_admin(request, db)
        booking = db.get(Booking, booking_id) or _404()
        services.cancel_booking(db, booking, request.app.state.vpn, "運営がキャンセル")
        flash(request, "キャンセルしました。利用中の場合は Agent が次の確認時に終了処理を行います")
        return redirect(f"/admin/bookings/{booking_id}")

    # --- Agent API ----------------------------------------------------------

    def agent_host(request: Request, db: Session = Depends(get_db)) -> tuple[Host, Session]:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        host = db.scalar(select(Host).where(Host.token_hash == hash_token(token))) if token else None
        if not host:
            raise HTTPException(401, "invalid token")
        return host, db

    def agent_booking(booking_id: int, ctx: tuple[Host, Session]) -> Booking:
        host, db = ctx
        booking = db.get(Booking, booking_id)
        if not booking or booking.slot.host_id != host.id:
            raise HTTPException(404)
        return booking

    @app.get("/api/agent/poll")
    def agent_poll(request: Request, ctx: tuple[Host, Session] = Depends(agent_host)):
        host, db = ctx
        if not host.active:
            host.last_seen_at = clock.utcnow()
            db.commit()
            # 承認待ち・運営が止めている PC。アプリに理由を出す
            return {"action": "none", "next": None, "host_state": "pending" if host.pending else "paused"}
        services.purge_expired_screenshots(db, media_root, clock.utcnow())
        return services.agent_poll(
            db, host, clock.utcnow(), settings.agent_start_lead_seconds, request.app.state.vpn
        )

    @app.post("/api/agent/login")
    async def agent_login(request: Request, db: Session = Depends(get_db)):
        """貸し手がアプリにメールアドレスとパスワードを入れて、この PC を登録する。

        新しい PC は運営が承認するまで貸し出さない (運営自身の PC はすぐ使える)。
        同じ PC でログインし直したときは、同じ PC のまま合言葉 (トークン) だけ作り直す。
        """
        try:
            body = await request.json()
            email = str(body["email"]).strip().lower()
            password = str(body["password"])
            pc_name = str(body.get("pc_name") or "").strip()[:100]
            machine_id = str(body.get("machine_id") or "")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "invalid request")
        user = db.scalar(select(User).where(User.email == email))
        if not user or not verify_password(password, user.password_hash):
            return JSONResponse({"error": "メールアドレスかパスワードが違います"}, status_code=400)
        if not MACHINE_ID_RE.match(machine_id):
            machine_id = ""
        host = None
        if machine_id:
            host = db.scalar(select(Host).where(Host.owner_id == user.id, Host.machine_id == machine_id))
        token = new_agent_token()
        if host:
            host.token_hash = hash_token(token)
        else:
            admin = is_admin(user)
            host = Host(
                name=pc_name or f"{user.email} の PC", token_hash=hash_token(token), owner_id=user.id,
                machine_id=machine_id, pending=not admin, active=admin,
            )
            db.add(host)
            if host.pending:
                for admin_email in sorted(settings.admin_emails):
                    send_mail(
                        admin_email,
                        "新しい PC が確認待ちです",
                        f"{user.email} さんが PC「{host.name}」を登録しました。\n"
                        f"管理画面で「承認する」を押すと貸し出せるようになります。\n{settings.base_url}/admin",
                    )
        db.commit()
        return {"token": token, "host": host.name, "pending": host.pending}

    @app.get("/api/agent/hello")
    def agent_hello(ctx: tuple[Host, Session] = Depends(agent_host)):
        """接続テスト。Agent の初期設定で、どの PC として登録されたかを表示する。"""
        host, _ = ctx
        return {"host": host.name, "service": settings.service_name}

    def schedule_range(start: str, days: int) -> tuple[dt.datetime, dt.datetime]:
        try:
            begin = services.parse_iso_utc(start)
        except ValueError:
            raise HTTPException(400, "start must be ISO 8601")
        if not 1 <= days <= services.SCHEDULE_MAX_DAYS:
            raise HTTPException(400, f"days must be 1..{services.SCHEDULE_MAX_DAYS}")
        return begin, begin + dt.timedelta(days=days)

    def schedule_response(host: Host, db: Session, begin: dt.datetime, end: dt.datetime) -> dict:
        now = clock.utcnow()
        return {
            "host": host.name,
            "now": services.iso_utc(now),
            "price_yen": services.suggested_price(db, host),
            "trial_minutes": settings.trial_minutes,
            "slots": services.host_schedule(db, host, begin, end, now, settings.hold_minutes),
        }

    @app.get("/api/agent/schedule")
    def agent_schedule(start: str, days: int = 7, ctx: tuple[Host, Session] = Depends(agent_host)):
        host, db = ctx
        begin, end = schedule_range(start, days)
        return schedule_response(host, db, begin, end)

    @app.put("/api/agent/schedule")
    async def agent_schedule_save(request: Request, ctx: tuple[Host, Session] = Depends(agent_host)):
        """貸し手が Agent のカレンダーで選んだ受付時間を保存する。"""
        host, db = ctx
        try:
            body = await request.json()
            begin, end = schedule_range(str(body["start"]), int(body.get("days", 7)))
            price = int(body["price_yen"])
            raw_starts = body.get("starts", [])
            if not isinstance(raw_starts, list) or len(raw_starts) > 24 * services.SCHEDULE_MAX_DAYS:
                raise ValueError
            chosen = {services.parse_iso_utc(str(v)) for v in raw_starts}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "invalid schedule")
        try:
            result = services.set_hourly_slots(
                db, host, begin, end, chosen, price, clock.utcnow(), settings.hold_minutes
            )
        except services.BookingError as exc:
            db.rollback()
            return JSONResponse({"error": str(exc)}, status_code=400)
        if result["added"]:
            notify_followers(db, host)
        return {**result, **schedule_response(host, db, begin, end)}

    def notify_followers(db: Session, host: Host) -> None:
        """フォローしている貸し手が新しい時間を公開したら知らせる (同じ PC について 6 時間に 1 回まで)。"""
        now = clock.utcnow()
        if host.followers_notified_at and now - host.followers_notified_at < dt.timedelta(hours=6):
            return
        people = social.followers_to_notify(db, host)
        if not people:
            return
        host.followers_notified_at = now
        db.commit()
        lender = host.owner
        for person in people:
            send_mail(
                person.email,
                f"{lender.display_name} さんが新しい時間を公開しました",
                f"フォローしている {lender.display_name} さんの PC「{host.name}」に、新しく予約できる時間ができました。\n"
                f"{settings.base_url}/lenders/{lender.id}",
            )

    @app.post("/api/agent/bookings/{booking_id}/screenshots")
    async def agent_screenshots(
        booking_id: int, request: Request, ctx: tuple[Host, Session] = Depends(agent_host)
    ):
        """セッション中に撮った写真 (zip) を受け取り、借り手だけがダウンロードできるよう保管する。"""
        booking = agent_booking(booking_id, ctx)
        db = ctx[1]
        limit = settings.screenshot_max_mb * 1024 * 1024
        name = f"screenshots/booking-{booking.id}-{secrets.token_hex(8)}.zip"
        path = media_root / name
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "too large")
                    f.write(chunk)
            if not zipfile.is_zipfile(path):
                raise HTTPException(400, "not a zip file")
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
            allowed = (".png", ".jpg", ".jpeg", ".webp")
            if not names or any(not n.lower().endswith(allowed) for n in names):
                raise HTTPException(400, "zip must contain only images")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if booking.screenshots_file:
            (media_root / booking.screenshots_file).unlink(missing_ok=True)
        booking.screenshots_file = name
        booking.screenshots_count = len(names)
        booking.screenshots_expires_at = clock.utcnow() + dt.timedelta(days=settings.screenshot_days)
        db.commit()
        return {"ok": True, "count": len(names)}

    @app.get("/api/agent/bookings/{booking_id}")
    def agent_booking_status(booking_id: int, ctx: tuple[Host, Session] = Depends(agent_host)):
        booking = agent_booking(booking_id, ctx)
        return {"booking_id": booking.id, "status": booking.status}

    @app.post("/api/agent/bookings/{booking_id}/started")
    def agent_started(booking_id: int, ctx: tuple[Host, Session] = Depends(agent_host)):
        booking = agent_booking(booking_id, ctx)
        services.mark_started(ctx[1], booking, clock.utcnow())
        return {"ok": True, "status": booking.status}

    @app.post("/api/agent/bookings/{booking_id}/finished")
    async def agent_finished(
        booking_id: int, request: Request, ctx: tuple[Host, Session] = Depends(agent_host)
    ):
        booking = agent_booking(booking_id, ctx)
        try:
            report = await request.json()
            if not isinstance(report, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(400, "report must be a JSON object")
        was = booking.status
        services.mark_finished(ctx[1], booking, report, request.app.state.vpn, clock.utcnow())
        if was == BookingStatus.ACTIVE and booking.status == BookingStatus.COMPLETED:
            send_mail(
                booking.user.email,
                "ご利用ありがとうございました",
                f"{booking_when(booking)} のご利用が終わりました。\n"
                "PC に残っていたログイン情報やキャッシュは削除しました。\n"
                f"撮ったスクリーンショットは、予約ページからダウンロードできます（{settings.screenshot_days} 日間）。\n"
                "よろしければ、予約ページから貸し手の評価もお願いします。\n"
                f"{settings.base_url}/bookings/{booking.id}",
            )
        return {"ok": True, "status": booking.status}

    return app


def _404():
    raise HTTPException(404)


def _lazy_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app()


class _LazyApp:
    """`uvicorn app.main:app` で起動したときだけ環境変数から設定を読む (テストの import を軽くする)。"""

    _app: FastAPI | None = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = _lazy_app()
        await self._app(scope, receive, send)


app = _LazyApp()
