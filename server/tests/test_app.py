import asyncio
import datetime as dt
import io
import json
import re
import zipfile
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Booking, BookingStatus, Host, Slot, User  # noqa: E402
from app.payments import CaptureResult, FakePayments, PayPalPayments, WebhookResult  # noqa: E402
from app.vpn import TailscaleVpn, VpnGrant  # noqa: E402

NOW = dt.datetime(2026, 10, 10, 3, 0, 0)  # UTC = JST 12:00


class FakeVpn:
    def __init__(self):
        self.issued = []
        self.revoked = []

    def issue(self, tag, expiry_seconds, description):
        self.issued.append((tag, expiry_seconds))
        return VpnGrant(key_id=f"k{len(self.issued)}", auth_key=f"tskey-auth-{len(self.issued)}")

    def revoke(self, key_id, tag):
        self.revoked.append((key_id, tag))


class StubStripe(FakePayments):
    """Stripe の代わり: 決済 URL を返し、Webhook はテストから直接指定する。"""

    def __init__(self):
        self.next_event = None
        self.amounts = []

    def create_checkout(self, booking, description):
        self.amounts.append(booking.amount_yen)
        return f"https://checkout.example/{booking.id}", f"cs_{booking.id}"

    def parse_webhook(self, payload, signature):
        if signature != "good":
            raise ValueError("bad signature")
        return self.next_event


@pytest.fixture
def now(monkeypatch):
    current = {"t": NOW}
    monkeypatch.setattr(clock, "utcnow", lambda: current["t"])
    return current


class StubPayPal:
    def __init__(self):
        self.captured = []
        self.result = None

    def create_checkout(self, booking, description):
        return f"https://paypal.example/{booking.id}", f"ORDER{booking.id}"

    def capture(self, order_id):
        self.captured.append(order_id)
        return self.result


def make_app(tmp_path, payments=None, vpn=None, trial_minutes=0, screenshot_max_mb=250, site_password="", free_mode=False, invite_only=False, simple_mode=False, max_booking_hours=0, tester_db=""):
    settings = Settings(
        tester_db=tester_db,
        max_booking_hours=max_booking_hours,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        secret_key="test",
        admin_emails=frozenset({"admin@example.com"}),
        agent_start_lead_seconds=120,
        trial_minutes=trial_minutes,
        media_dir=str(tmp_path / "media"),
        screenshot_max_mb=screenshot_max_mb,
        site_password=site_password,
        free_mode=free_mode,
        invite_only=invite_only,
        simple_mode=simple_mode,
    )
    vpn = vpn or FakeVpn()
    if payments is None:
        payments = {"fake": FakePayments()}
    elif not isinstance(payments, dict):
        payments = {"stripe": payments}
    app = create_app(settings, payments=payments, vpn=vpn)
    return app, vpn


def signup(client, email, meta=""):
    meta = meta if meta is not None else None
    if meta == "":
        meta = "q_" + email.split("@")[0]
    r = client.post("/signup", data={"email": email, "password": "password123", "agree": "1"}, follow_redirects=False)
    assert r.status_code == 303
    if meta:
        client.post("/account", data={"meta_username": meta})


def admin_setup(client):
    """管理者で PC と枠を作り、Agent トークンを返す。"""
    signup(client, "admin@example.com")
    r = client.post("/admin/hosts", data={"name": "PC-1", "vpn_address": "100.64.0.1", "vpn_borrower_tag": "tag:borrower-pc1"})
    token = re.search(r"pcvr_[\w-]+", r.text).group(0)
    # JST 13:00〜14:00 = UTC 04:00〜05:00
    client.post("/admin/slots", data={"host_id": 1, "date": "2026-10-10", "start_time": "13:00", "minutes": 60, "price_yen": 500})
    client.post("/logout")
    return token


@pytest.fixture
def setup(tmp_path, now):
    app, vpn = make_app(tmp_path)
    admin = TestClient(app)
    token = admin_setup(admin)
    user = TestClient(app)
    signup(user, "user@example.com")
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    return app, vpn, user, agent


def db(app):
    return app.state.Session()


def test_landing_page_renders(tmp_path, now):
    app, _ = make_app(tmp_path)
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "PC版VRChat" in r.text


def test_slot_creation_converts_jst_to_utc(setup):
    app, *_ = setup
    slot = db(app).get(Slot, 1)
    assert slot.start_at == dt.datetime(2026, 10, 10, 4, 0)
    assert slot.end_at == dt.datetime(2026, 10, 10, 5, 0)


def test_non_admin_cannot_open_admin(setup):
    _, _, user, _ = setup
    assert user.get("/admin").status_code == 404


def test_booking_requires_meta_username(tmp_path, now):
    app, _ = make_app(tmp_path)
    admin_setup(TestClient(app))
    user = TestClient(app)
    signup(user, "nometa@example.com", meta=None)
    user.post("/slots/1/book")
    assert db(app).query(Booking).count() == 0


def test_full_session_flow(setup, now):
    app, vpn, user, agent = setup

    # 予約 (Fake 決済なので即確定し、VPN キーが発行される)
    r = user.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "/bookings/1"
    booking_page = user.get("/bookings/1").text
    assert "tskey-auth-1" in booking_page
    assert "100.64.0.1" in booking_page
    assert vpn.issued == [("tag:borrower-pc1", 7200)]

    # 埋まった枠は一覧から消える
    assert "予約する" not in user.get("/slots").text

    # 開始 2 分前より前は何もしない。次の予約だけ知らせる (Agent が「あと何分で開始」を出す)
    assert agent.get("/api/agent/poll").json() == {
        "action": "none",
        "next": {"booking_id": 1, "starts_at": "2026-10-10T04:00:00Z", "ends_at": "2026-10-10T04:55:00Z",
                 "starts_in_seconds": 3600},
    }
    assert "あと 1時間0分 で開始します" in booking_page

    # 開始 2 分前になると開始指示
    now["t"] = dt.datetime(2026, 10, 10, 3, 58, 30)
    job = agent.get("/api/agent/poll").json()
    assert job["action"] == "start"
    assert job["booking_id"] == 1
    assert job["borrower_meta_username"] == "q_user"
    assert job["remaining_seconds"] == 3390  # 13:58:30 から 13:55 (最後の 5 分は片づけと次の準備)

    assert agent.post("/api/agent/bookings/1/started").json()["status"] == "active"
    assert agent.get("/api/agent/bookings/1").json()["status"] == "active"

    now["t"] = dt.datetime(2026, 10, 10, 5, 0, 5)
    r = agent.post("/api/agent/bookings/1/finished", json={"clean": True, "end_reason": "duration_elapsed", "errors": []})
    assert r.json()["status"] == "completed"
    assert vpn.revoked == [("k1", "tag:borrower-pc1")]
    b = db(app).get(Booking, 1)
    assert b.clean is True and b.vpn_auth_key == ""


def test_agent_restart_resumes_or_cleans_up(setup, now):
    app, _, user, agent = setup
    user.post("/slots/1/book")
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")

    now["t"] = dt.datetime(2026, 10, 10, 4, 30)
    job = agent.get("/api/agent/poll").json()
    assert job["action"] == "start" and job["resume"] is True and job["remaining_seconds"] == 1500

    now["t"] = dt.datetime(2026, 10, 10, 5, 10)
    assert agent.get("/api/agent/poll").json() == {"action": "cleanup", "booking_id": 1}


def test_unstarted_booking_expires_and_revokes_vpn(setup, now):
    app, vpn, user, agent = setup
    user.post("/slots/1/book")
    now["t"] = dt.datetime(2026, 10, 10, 6, 0)  # PC がずっとオフラインだった
    assert agent.get("/api/agent/poll").json() == {"action": "none", "next": None}
    b = db(app).get(Booking, 1)
    assert b.status == BookingStatus.EXPIRED
    assert "返金" in b.note
    assert vpn.revoked


def test_admin_cancel_is_visible_to_agent(tmp_path, now):
    app, vpn = make_app(tmp_path)
    admin = TestClient(app)
    token = admin_setup(admin)
    user = TestClient(app)
    signup(user, "user@example.com")
    user.post("/slots/1/book")
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")

    admin.post("/login", data={"email": "admin@example.com", "password": "password123"})
    admin.post("/admin/bookings/1/cancel")
    assert agent.get("/api/agent/bookings/1").json()["status"] == "cancelled"
    assert "返金" in db(app).get(Booking, 1).note


def test_agent_api_rejects_bad_token_and_other_hosts_booking(setup, now):
    app, _, user, agent = setup
    assert TestClient(app).get("/api/agent/poll").status_code == 401
    bad = TestClient(app, headers={"Authorization": "Bearer pcvr_wrong"})
    assert bad.get("/api/agent/poll").status_code == 401

    user.post("/slots/1/book")
    s = db(app)
    other = Host(name="PC-2", token_hash="x" * 64)
    s.add(other)
    s.commit()
    from app.security import hash_token

    other.token_hash = hash_token("pcvr_other")
    s.commit()
    other_agent = TestClient(app, headers={"Authorization": "Bearer pcvr_other"})
    assert other_agent.get("/api/agent/bookings/1").status_code == 404


def test_stripe_flow_hold_and_webhook(tmp_path, now):
    stripe = StubStripe()
    app, vpn = make_app(tmp_path, payments=stripe)
    admin_setup(TestClient(app))
    a = TestClient(app)
    signup(a, "a@example.com")
    b = TestClient(app)
    signup(b, "b@example.com")

    r = a.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "https://checkout.example/1"
    # 支払い待ちの間は他の人は予約できない
    b.post("/slots/1/book")
    assert db(app).query(Booking).count() == 1

    # 署名が不正な Webhook は拒否
    assert TestClient(app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "bad"}).status_code == 400

    stripe.next_event = WebhookResult("paid", 1, "cs_1")
    hook = TestClient(app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "good"})
    assert hook.status_code == 200
    assert db(app).get(Booking, 1).status == BookingStatus.CONFIRMED
    # 重複して届いても VPN キーは 1 回だけ
    TestClient(app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "good"})
    assert len(vpn.issued) == 1


def test_late_payment_after_slot_taken_is_flagged_for_refund(tmp_path, now):
    stripe = StubStripe()
    app, _ = make_app(tmp_path, payments=stripe)
    admin_setup(TestClient(app))
    a = TestClient(app)
    signup(a, "a@example.com")
    b = TestClient(app)
    signup(b, "b@example.com")

    a.post("/slots/1/book")
    now["t"] = NOW + dt.timedelta(minutes=40)  # A の確保時間が切れた
    b.post("/slots/1/book")
    stripe.next_event = WebhookResult("paid", 2, "cs_2")
    TestClient(app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "good"})
    stripe.next_event = WebhookResult("paid", 1, "cs_1")
    TestClient(app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "good"})

    s = db(app)
    assert s.get(Booking, 2).status == BookingStatus.CONFIRMED
    late = s.get(Booking, 1)
    assert late.status == BookingStatus.CANCELLED and "返金" in late.note


def test_login_rejects_open_redirect(setup):
    app, *_ = setup
    c = TestClient(app)
    r = c.post("/login", data={"email": "user@example.com", "password": "password123", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/slots"


def test_overlapping_slot_is_rejected(setup):
    app, *_ = setup
    admin = TestClient(app)
    admin.post("/login", data={"email": "admin@example.com", "password": "password123"})
    admin.post("/admin/slots", data={"host_id": 1, "date": "2026-10-10", "start_time": "13:30", "minutes": 60, "price_yen": 500})
    assert db(app).query(Slot).count() == 1


def test_tailscale_provider_calls_api():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "kid", "key": "tskey-auth-xyz"})
        if request.method == "GET":
            return httpx.Response(200, json={"devices": [
                {"id": "d1", "name": "quest", "tags": ["tag:borrower-pc1"]},
                {"id": "d2", "name": "lender-pc", "tags": ["tag:host"]},
            ]})
        return httpx.Response(200)

    client = httpx.Client(base_url=TailscaleVpn.API, transport=httpx.MockTransport(handler))
    ts = TailscaleVpn("tskey-api-x", client=client)
    grant = ts.issue("tag:borrower-pc1", 3600, "booking-1")
    assert grant.auth_key == "tskey-auth-xyz"
    ts.revoke("kid", "tag:borrower-pc1")
    assert ("DELETE", "/api/v2/tailnet/-/keys/kid") in calls
    assert ("DELETE", "/api/v2/device/d1") in calls
    assert ("DELETE", "/api/v2/device/d2") not in calls


def test_trial_makes_first_30_minutes_free(tmp_path, now):
    stripe = StubStripe()
    app, _ = make_app(tmp_path, payments=stripe, trial_minutes=30)
    admin = TestClient(app)
    admin_setup(admin)  # 60分 500円
    admin.post("/login", data={"email": "admin@example.com", "password": "password123"})
    # 30分 300円の枠 → 初回は全額無料
    admin.post("/admin/slots", data={"host_id": 1, "date": "2026-10-11", "start_time": "13:00", "minutes": 30, "price_yen": 300})

    user = TestClient(app)
    signup(user, "new@example.com", meta="newbie_quest")
    page = user.get("/slots/1/confirm").text
    assert "250円" in page and "初回 30 分無料" in page

    # 60分枠: 前半30分ぶん (250円) 引いて 250円をカードで
    r = user.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "https://checkout.example/1"
    assert stripe.amounts == [250]

    # 支払い待ちの間は、2件目に無料は付かない
    assert "無料で予約する" not in user.get("/slots/2/confirm").text


def test_trial_free_slot_skips_payment_and_is_once_per_meta_username(tmp_path, now):
    stripe = StubStripe()
    app, vpn = make_app(tmp_path, payments=stripe, trial_minutes=30)
    admin = TestClient(app)
    admin_setup(admin)
    admin.post("/login", data={"email": "admin@example.com", "password": "password123"})
    admin.post("/admin/slots", data={"host_id": 1, "date": "2026-10-11", "start_time": "13:00", "minutes": 30, "price_yen": 300})

    first = TestClient(app)
    signup(first, "first@example.com", meta="Same_Quest")
    r = first.post("/slots/2/book", follow_redirects=False)
    assert r.headers["location"] == "/bookings/1"
    b = db(app).get(Booking, 1)
    assert (b.status, b.amount_yen, b.discount_yen, b.payment_provider) == ("confirmed", 0, 300, "free")
    assert vpn.issued  # 無料でも VPN キーは発行される

    # 別のメールアドレスでも、同じ Meta ユーザー名なら無料は 1 回だけ
    second = TestClient(app)
    signup(second, "second@example.com", meta="same_quest")
    second.post("/slots/1/book")
    assert stripe.amounts == [500]


def test_multiple_providers_ask_for_payment_method(tmp_path, now):
    stripe, paypal = StubStripe(), StubPayPal()
    app, _ = make_app(tmp_path, payments={"stripe": stripe, "paypal": paypal})
    admin_setup(TestClient(app))
    user = TestClient(app)
    signup(user, "u@example.com")
    r = user.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "/slots/1/confirm"
    page = user.get("/slots/1/confirm").text
    assert "クレジットカードで支払う" in page and "PayPalで支払う" in page
    assert db(app).query(Booking).count() == 0  # 選ぶまでは枠を確保しない


def test_paypal_return_captures_and_confirms(tmp_path, now):
    paypal = StubPayPal()
    app, vpn = make_app(tmp_path, payments={"paypal": paypal})
    admin_setup(TestClient(app))
    user = TestClient(app)
    signup(user, "u@example.com")
    r = user.post("/slots/1/book", data={"provider": "paypal"}, follow_redirects=False)
    assert r.headers["location"] == "https://paypal.example/1"

    # token が違えば 404
    assert user.get("/payments/paypal/return?booking_id=1&token=WRONG").status_code == 404

    paypal.result = CaptureResult(False, detail="status=APPROVED")
    user.get("/payments/paypal/return?booking_id=1&token=ORDER1")
    assert db(app).get(Booking, 1).status == BookingStatus.PENDING_PAYMENT

    paypal.result = CaptureResult(True, 500, 1)
    r = user.get("/payments/paypal/return?booking_id=1&token=ORDER1", follow_redirects=False)
    assert r.headers["location"] == "/bookings/1"
    assert db(app).get(Booking, 1).status == BookingStatus.CONFIRMED
    # リロードしても二重に capture しない
    user.get("/payments/paypal/return?booking_id=1&token=ORDER1")
    assert paypal.captured == ["ORDER1", "ORDER1"]
    assert len(vpn.issued) == 1


def test_paypal_does_not_capture_when_slot_was_taken(tmp_path, now):
    paypal = StubPayPal()
    app, _ = make_app(tmp_path, payments={"paypal": paypal, "fake": FakePayments()})
    admin_setup(TestClient(app))
    a = TestClient(app)
    signup(a, "a@example.com")
    a.post("/slots/1/book", data={"provider": "paypal"})
    now["t"] = NOW + dt.timedelta(minutes=40)  # A の確保時間が切れ、B が先に確定
    b = TestClient(app)
    signup(b, "b@example.com")
    b.post("/slots/1/book", data={"provider": "fake"})

    a.get("/payments/paypal/return?booking_id=1&token=ORDER1")
    assert paypal.captured == []  # お金は動かない
    late = db(app).get(Booking, 1)
    assert late.status == BookingStatus.CANCELLED and "未請求" in late.note


def test_paypal_gateway_against_api():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A"})
        if request.url.path == "/v2/checkout/orders":
            body = json.loads(request.content)
            assert body["purchase_units"][0]["amount"] == {"currency_code": "JPY", "value": "800"}
            return httpx.Response(201, json={"id": "O1", "links": [{"rel": "payer-action", "href": "https://paypal/approve"}]})
        if request.url.path == "/v2/checkout/orders/O1/capture":
            return httpx.Response(201, json={"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
                {"status": "COMPLETED", "custom_id": "5", "amount": {"currency_code": "JPY", "value": "800"}}]}}]})
        return httpx.Response(404)

    settings = Settings(paypal_client_id="id", paypal_client_secret="secret")
    gw = PayPalPayments(settings, client=httpx.Client(base_url="https://api", transport=httpx.MockTransport(handler)))

    class B:
        id = 5
        amount_yen = 800

    assert gw.create_checkout(B(), "test") == ("https://paypal/approve", "O1")
    assert gw.capture("O1") == CaptureResult(True, 800, 5)


def test_signup_records_ad_source(tmp_path, now):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    c.get("/?ref=himari-ryokan")
    signup(c, "from-ad@example.com")
    from app.models import User

    assert db(app).query(User).filter_by(email="from-ad@example.com").one().signup_ref == "himari-ryokan"


def test_early_end_by_lender_is_flagged_for_refund(setup, now):
    app, _, user, agent = setup
    user.post("/slots/1/book")
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    now["t"] = dt.datetime(2026, 10, 10, 4, 20)
    agent.post("/api/agent/bookings/1/finished", json={"clean": True, "end_reason": "keyboard_interrupt"})
    assert "35 分前に終了" in db(app).get(Booking, 1).note  # 使える終わり (13:55) まで


def test_borrower_can_end_now_and_the_pc_cleans_up_right_away(setup, now):
    """早く遊び終わったら「いま終わる」。PC のアプリは予約が終わったことに気づいて、すぐにログイン情報を消す。"""
    from app import invite

    app, _, user, agent = setup
    user.post("/slots/1/book")
    # 始まる前は押せない
    user.post("/bookings/1/end")
    assert db(app).get(Booking, 1).status == "confirmed"
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    page = user.get("/bookings/1").text
    assert 'action="/bookings/1/end"' in page and "いま終わる" in page

    now["t"] = dt.datetime(2026, 10, 10, 4, 20)
    user.post("/bookings/1/end")
    b = db(app).get(Booking, 1)
    assert b.status == "completed" and b.end_reason == "borrower_ended" and "残り 35 分" in b.note
    # PC のアプリは予約の状態を見て止まり、いつもの終了処理のあとに報告する
    assert agent.get("/api/agent/bookings/1").json()["status"] == "completed"
    agent.post("/api/agent/bookings/1/finished", json={"clean": True, "end_reason": "server_cancelled"})
    b = db(app).get(Booking, 1)
    assert b.end_reason == "borrower_ended" and "返金" not in b.note and b.finished_at is not None


def test_discord_bot_ends_a_booking_only_with_the_signature(setup, now):
    from app import invite

    app, _, user, agent = setup
    user.post("/slots/1/book")
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    bot = TestClient(app)  # 合言葉や Cookie なしで呼ぶ
    assert bot.post("/api/bot/bookings/1/end").status_code == 403
    assert bot.post("/api/bot/bookings/1/end", headers={"X-Geaspec-Signature": "x"}).status_code == 403
    secret = app.state.settings.secret_key
    r = bot.post("/api/bot/bookings/1/end", headers={"X-Geaspec-Signature": invite.bot_signature(secret, 1)})
    assert r.json() == {"ok": True} and db(app).get(Booking, 1).status == "completed"
    # もう終わっている
    r = bot.post("/api/bot/bookings/1/end", headers={"X-Geaspec-Signature": invite.bot_signature(secret, 1)})
    assert r.json() == {"ok": False}


def make_zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, b"\x89PNG fake " + n.encode())
    return buf.getvalue()


def finish_session(setup, now):
    app, vpn, user, agent = setup
    user.post("/slots/1/book")
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    now["t"] = dt.datetime(2026, 10, 10, 5, 0, 5)
    return app, user, agent


def test_screenshots_reach_only_the_borrower(setup, now, tmp_path):
    app, user, agent = finish_session(setup, now)
    data = make_zip(["2026-10/VRChat_1.png", "2026-10/VRChat_2.png"])
    r = agent.post("/api/agent/bookings/1/screenshots?count=2", content=data, headers={"Content-Type": "application/zip"})
    assert r.json() == {"ok": True, "count": 2}
    agent.post("/api/agent/bookings/1/finished", json={"clean": True, "end_reason": "duration_elapsed"})

    page = user.get("/bookings/1").text
    assert "スクリーンショット 2 枚をダウンロード" in page
    dl = user.get("/bookings/1/screenshots")
    assert dl.status_code == 200 and dl.content == data
    assert "attachment" in dl.headers["content-disposition"]

    # 運営 (管理者) でもダウンロードできない。管理画面には枚数だけ
    admin = TestClient(app)
    admin.post("/login", data={"email": "admin@example.com", "password": "password123"})
    assert admin.get("/bookings/1/screenshots").status_code == 404
    assert "2 枚" in admin.get("/admin/bookings/1").text
    assert "ダウンロード（zip）" not in admin.get("/bookings/1").text

    other = TestClient(app)
    signup(other, "other@example.com")
    assert other.get("/bookings/1/screenshots").status_code == 404


def test_screenshots_expire_after_seven_days(setup, now, tmp_path):
    app, user, agent = finish_session(setup, now)
    agent.post("/api/agent/bookings/1/screenshots", content=make_zip(["a.png"]))
    files = list((tmp_path / "media" / "screenshots").iterdir())
    assert len(files) == 1

    now["t"] = dt.datetime(2026, 10, 17, 5, 1)  # 7日後
    agent.get("/api/agent/poll")  # 定期的な問い合わせのついでに掃除される
    assert list((tmp_path / "media" / "screenshots").iterdir()) == []
    assert user.get("/bookings/1/screenshots").status_code == 404
    assert "ダウンロード（zip）" not in user.get("/bookings/1").text


def test_screenshot_upload_rejects_non_images_and_other_hosts(setup, now, tmp_path):
    app, user, agent = finish_session(setup, now)
    assert agent.post("/api/agent/bookings/1/screenshots", content=b"not a zip").status_code == 400
    assert agent.post("/api/agent/bookings/1/screenshots", content=make_zip(["evil.exe"])).status_code == 400
    assert list((tmp_path / "media" / "screenshots").iterdir()) == []
    assert TestClient(app).post("/api/agent/bookings/1/screenshots", content=make_zip(["a.png"])).status_code == 401


def test_screenshot_upload_size_limit(tmp_path, now):
    app, _ = make_app(tmp_path, screenshot_max_mb=0)
    token = admin_setup(TestClient(app))
    user = TestClient(app)
    signup(user, "u@example.com")
    user.post("/slots/1/book")
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert agent.post("/api/agent/bookings/1/screenshots", content=make_zip(["a.png"])).status_code == 413
    assert list((tmp_path / "media" / "screenshots").iterdir()) == []


def test_site_password_gates_every_page_but_machines(tmp_path, now, monkeypatch):
    monkeypatch.setattr("app.main.asyncio.sleep", lambda s, _orig=asyncio.sleep: _orig(0))
    app, _ = make_app(tmp_path, site_password="geaspec-secret")
    c = TestClient(app)
    for path in ("/", "/slots", "/signup", "/bookings/1"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/site-login"), path
    # 機械が呼ぶところは合言葉なしで届く (それぞれ別の認証がある)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/agent/poll").status_code == 401
    assert c.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"

    r = c.post("/site-login", data={"password": "wrong", "next": "/slots"})
    assert r.status_code == 401 and "合言葉が違います" in r.text
    r = c.post("/site-login", data={"password": "geaspec-secret", "next": "/slots"}, follow_redirects=False)
    assert r.headers["location"] == "/slots"
    assert c.get("/slots").status_code == 200

    # ログイン・ログアウトしても合言葉は聞き直さない
    signup(c, "u@example.com")
    c.post("/logout")
    assert c.get("/slots", follow_redirects=False).status_code == 200
    c.post("/login", data={"email": "u@example.com", "password": "password123"})
    assert c.get("/bookings", follow_redirects=False).status_code == 200


def test_site_login_rejects_open_redirect(tmp_path, now, monkeypatch):
    monkeypatch.setattr("app.main.asyncio.sleep", lambda s, _orig=asyncio.sleep: _orig(0))
    app, _ = make_app(tmp_path, site_password="x")
    r = TestClient(app).post("/site-login", data={"password": "x", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_pages_are_never_indexed(tmp_path, now):
    app, _ = make_app(tmp_path)
    r = TestClient(app).get("/")
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in r.text


def test_connection_code_carries_url_and_token(tmp_path, now):
    import base64

    app, _ = make_app(tmp_path)
    admin = TestClient(app)
    signup(admin, "admin@example.com")
    r = admin.post("/admin/hosts", data={"name": "PC-1"})
    code = re.search(r"geaspec1:[\w-]+", r.text).group(0)
    raw = code.removeprefix("geaspec1:")
    data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert data["server"] == "http://localhost:8000"
    assert data["token"].startswith("pcvr_")
    assert "BASE_URL" in r.text  # localhost のままだと貸し手 PC から届かない、と警告する
    agent = TestClient(app, headers={"Authorization": f"Bearer {data['token']}"})
    assert agent.get("/api/agent/hello").json()["host"] == "PC-1"


def test_agent_calendar_adds_and_removes_hourly_slots(setup, now):
    app, _, user, agent = setup
    user.post("/slots/1/book")  # UTC 04:00〜05:00 は予約あり
    start = "2026-10-09T15:00:00Z"  # JST 10/10 0:00
    r = agent.get("/api/agent/schedule", params={"start": start, "days": 7}).json()
    assert r["price_yen"] == 500
    assert [(s["start"], s["state"], s["editable"]) for s in r["slots"]] == [("2026-10-10T04:00:00Z", "booked", False)]

    body = {"start": start, "days": 7, "price_yen": 800,
            "starts": ["2026-10-10T05:00:00Z", "2026-10-10T06:00:00Z", "2026-10-10T04:00:00Z"]}
    r = agent.put("/api/agent/schedule", json=body).json()
    assert r["added"] == 2 and r["errors"] == []
    states = {s["start"]: (s["state"], s["price_yen"]) for s in r["slots"]}
    assert states == {
        "2026-10-10T04:00:00Z": ("booked", 500),  # 予約ありの枠には触らない
        "2026-10-10T05:00:00Z": ("open", 800),
        "2026-10-10T06:00:00Z": ("open", 800),
    }
    assert len(re.findall(r'href="/slots/\d+/confirm"', user.get("/slots").text)) == 2

    # 外した枠は消える。予約ありの枠は外しても残る
    body["starts"] = ["2026-10-10T06:00:00Z"]
    r = agent.put("/api/agent/schedule", json=body).json()
    assert r["removed"] == 1
    assert {s["start"] for s in r["slots"]} == {"2026-10-10T04:00:00Z", "2026-10-10T06:00:00Z"}

    # 過去・範囲外・中途半端な時刻は受け付けない
    bad = {**body, "starts": ["2026-10-10T06:30:00Z"]}
    assert agent.put("/api/agent/schedule", json=bad).status_code == 400
    assert agent.put("/api/agent/schedule", json={**body, "price_yen": 10}).status_code == 400
    r = agent.put("/api/agent/schedule", json={**body, "starts": ["2026-10-10T02:00:00Z"]}).json()
    assert r["added"] == 0  # 過去の時間には作らない


def test_removed_slot_with_history_is_closed_not_deleted(setup, now):
    app, vpn, user, agent = setup
    user.post("/slots/1/book")
    with db(app) as s:
        s.get(Booking, 1).status = BookingStatus.CANCELLED
        s.commit()
    start = "2026-10-09T15:00:00Z"
    r = agent.put("/api/agent/schedule", json={"start": start, "days": 1, "price_yen": 500, "starts": []}).json()
    assert r["removed"] == 1
    assert [(s["state"], s["editable"]) for s in r["slots"]] == [("closed", True)]
    assert "予約する" not in user.get("/slots").text
    # もう一度選ぶと受付を再開する
    r = agent.put("/api/agent/schedule", json={
        "start": start, "days": 1, "price_yen": 500, "starts": ["2026-10-10T04:00:00Z"]}).json()
    assert [s["state"] for s in r["slots"]] == ["open"]


def test_admin_calendar_saves_checked_hours(tmp_path, now):
    app, _ = make_app(tmp_path)
    admin = TestClient(app)
    signup(admin, "admin@example.com")
    admin.post("/admin/hosts", data={"name": "PC-1"})
    page = admin.get("/admin/hosts/1/schedule?week=2026-10-10").text
    assert 'value="2026-10-10T13:00"' in page
    assert 'value="2026-10-10T11:00"' not in page  # JST 12:00 現在、過ぎた時間は選べない
    r = admin.post("/admin/hosts/1/schedule", data={
        "week": "2026-10-10", "price_yen": 600, "starts": ["2026-10-10T13:00", "2026-10-11T20:00"]})
    assert r.status_code == 200
    slots = db(app).query(Slot).order_by(Slot.start_at).all()
    assert [(s.start_at, s.end_at, s.price_yen) for s in slots] == [
        (dt.datetime(2026, 10, 10, 4, 0), dt.datetime(2026, 10, 10, 5, 0), 600),
        (dt.datetime(2026, 10, 11, 11, 0), dt.datetime(2026, 10, 11, 12, 0), 600),
    ]
    assert 'value="2026-10-10T13:00" checked' in admin.get("/admin/hosts/1/schedule?week=2026-10-10").text


def test_until_text():
    from app.services import until_text

    assert until_text(30) == "1分"
    assert until_text(23 * 60) == "23分"
    assert until_text(83 * 60) == "1時間23分"
    assert until_text(2 * 86400 + 3 * 3600) == "2日3時間"


def test_old_database_gets_new_columns(tmp_path, now):
    import sqlite3

    con = sqlite3.connect(tmp_path / "test.db")
    con.execute("CREATE TABLE slots (id INTEGER PRIMARY KEY, host_id INTEGER, start_at DATETIME, end_at DATETIME,"
                " price_yen INTEGER, created_at DATETIME)")
    con.execute("CREATE TABLE hosts (id INTEGER PRIMARY KEY, name VARCHAR(100), token_hash VARCHAR(64),"
                " vpn_address VARCHAR(255), vpn_borrower_tag VARCHAR(100), active BOOLEAN, last_seen_at DATETIME,"
                " created_at DATETIME)")
    con.commit()
    con.close()
    make_app(tmp_path)
    con = sqlite3.connect(tmp_path / "test.db")
    assert "closed" in [row[1] for row in con.execute("PRAGMA table_info(slots)")]
    assert {"pending", "owner_id", "machine_id"} <= {row[1] for row in con.execute("PRAGMA table_info(hosts)")}
    con.close()


def test_lender_logs_in_from_app_and_waits_for_approval(tmp_path, now):
    app, _ = make_app(tmp_path)
    lender = TestClient(app)
    signup(lender, "lender@example.com")
    api = TestClient(app)
    body = {"email": "Lender@example.com", "password": "password123", "pc_name": "LENDER-PC", "machine_id": "abcdef0123456789"}
    assert api.post("/api/agent/login", json={**body, "password": "wrong"}).json() == {
        "error": "メールアドレスかパスワードが違います"}
    r = api.post("/api/agent/login", json=body).json()
    assert r["host"] == "LENDER-PC" and r["pending"] is True
    agent = TestClient(app, headers={"Authorization": f"Bearer {r['token']}"})
    assert agent.get("/api/agent/poll").json() == {"action": "none", "next": None, "host_state": "pending"}

    # 承認前でもカレンダーは作れるが、借り手には見えない
    agent.put("/api/agent/schedule", json={"start": "2026-10-09T15:00:00Z", "days": 1, "price_yen": 500,
                                          "starts": ["2026-10-10T10:00:00Z"]})
    user = TestClient(app)
    signup(user, "user@example.com")
    assert "/confirm" not in user.get("/slots").text

    # 同じ PC でログインし直しても PC は増えず、古い合言葉は使えなくなる
    r2 = api.post("/api/agent/login", json=body).json()
    assert db(app).query(Host).count() == 1
    assert agent.get("/api/agent/poll").status_code == 401

    admin = TestClient(app)
    signup(admin, "admin@example.com")
    page = admin.get("/admin").text
    assert "承認待ち" in page and "lender@example.com" in page
    admin.post("/admin/hosts/1/approve", data={"vpn_address": "100.64.0.9", "vpn_borrower_tag": "tag:borrower-x"})
    host = db(app).get(Host, 1)
    assert (host.pending, host.active, host.vpn_address) == (False, True, "100.64.0.9")
    agent2 = TestClient(app, headers={"Authorization": f"Bearer {r2['token']}"})
    assert agent2.get("/api/agent/poll").json() == {"action": "none", "next": None}
    assert "/confirm" in user.get("/slots").text


def test_admin_pc_from_app_needs_no_approval(tmp_path, now):
    app, _ = make_app(tmp_path)
    admin = TestClient(app)
    signup(admin, "admin@example.com")
    r = TestClient(app).post("/api/agent/login", json={"email": "admin@example.com", "password": "password123"}).json()
    assert r["pending"] is False and r["host"] == "admin@example.com の PC"


def test_home_screen_app_files_open_without_site_password(tmp_path):
    client = TestClient(make_app(tmp_path, site_password="secret")[0])
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone"
    assert {i["sizes"] for i in data["icons"]} >= {"192x192", "512x512"}
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert client.get("/static/offline.html").status_code == 200
    # 案内ページ自体は合言葉の内側
    assert client.get("/app", follow_redirects=False).status_code == 303


def test_app_page_and_head_tags(tmp_path):
    client = TestClient(make_app(tmp_path)[0])
    page = client.get("/app")
    assert page.status_code == 200
    assert "ホーム画面に追加" in page.text
    assert 'rel="manifest"' in page.text and "apple-touch-icon" in page.text


def test_notification_mails(tmp_path, now):
    app, _ = make_app(tmp_path)
    mails = app.state.mailer.sent
    assert app.state.mailer.enabled is False  # 設定がなければ送らず、記録だけ
    admin = TestClient(app)
    signup(admin, "admin@example.com")
    lender = TestClient(app)
    signup(lender, "lender@example.com")
    body = {"email": "lender@example.com", "password": "password123", "pc_name": "LG", "machine_id": "machine-0001"}
    token = TestClient(app).post("/api/agent/login", json=body).json()["token"]
    assert [(to, subj) for to, subj, _ in mails] == [("admin@example.com", "【Geaspec】新しい PC が確認待ちです")]

    admin.post("/admin/hosts/1/approve", data={})
    assert mails[-1][:2] == ("lender@example.com", "【Geaspec】PC の確認が終わりました")

    admin.post("/admin/slots", data={"host_id": 1, "date": "2026-10-10", "start_time": "13:00", "minutes": 60, "price_yen": 500})
    borrower = TestClient(app)
    signup(borrower, "user@example.com")
    del mails[:]
    borrower.post("/slots/1/book")
    assert [(to, subj) for to, subj, _ in mails] == [
        ("user@example.com", "【Geaspec】予約が確定しました 10/10（土）13:00〜14:00"),
        ("lender@example.com", "【Geaspec】新しい予約が入りました 10/10（土）13:00〜14:00"),
    ]
    assert "/bookings/1" in mails[0][2]

    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    now["t"] = dt.datetime(2026, 10, 10, 5, 0)
    agent.post("/api/agent/bookings/1/finished", json={"end_reason": "duration_elapsed", "clean": True})
    assert mails[-1][:2] == ("user@example.com", "【Geaspec】ご利用ありがとうございました")
    assert len(mails) == 3

    # テストメールのボタン: 未設定なら案内だけ
    r = admin.post("/admin/test-mail")
    assert "メールの設定がまだです" in r.text and len(mails) == 3


def test_smtp_mailer_sends_with_gmail_settings(monkeypatch):
    from app import mail

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context):
            sent.append(("tls",))
        def login(self, user, password):
            sent.append(("login", user, password))
        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"], msg["From"]))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    settings = Settings(smtp_user="geaspec@gmail.com", smtp_password="abcdefghijklmnop")
    m = mail.build_mailer(settings)
    assert m.enabled
    assert m._send("user@example.com", "件名", "本文") is True
    assert sent[0] == ("connect", "smtp.gmail.com", 587)
    assert sent[2] == ("login", "geaspec@gmail.com", "abcdefghijklmnop")
    assert sent[3][:3] == ("send", "user@example.com", "件名")
    assert "geaspec@gmail.com" in sent[3][3]


def test_last_minutes_of_a_slot_are_for_turnover(setup, now):
    """1 時間の枠は 00〜55 分が利用時間。最後の 5 分で片づけ、次の人の準備をする。"""
    app, _, user, agent = setup
    user.post("/slots/1/book")
    page = user.get("/bookings/1").text
    # 見やすいように枠の終わり (14:00) を出し、片づけのぶん早く終わることを添える
    assert "14:00" in page and "5分前に終了" in page and "13:55" not in page
    now["t"] = dt.datetime(2026, 10, 10, 4, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    # 13:55 を過ぎたら、利用中の予約は片づける
    now["t"] = dt.datetime(2026, 10, 10, 4, 55, 1)
    assert agent.get("/api/agent/poll").json() == {"action": "cleanup", "booking_id": 1}
    # 短い枠 (10 分以下) は削らない
    s = db(app).get(Slot, 1)
    s.end_at = s.start_at + dt.timedelta(minutes=10)
    assert s.play_end_at == s.end_at


def test_free_mode_never_takes_money(tmp_path, now):
    """無料の実験として動かすときは、枠に値段があっても支払いなしで確定し、料金を見せない。"""
    stripe = StubStripe()
    app, _ = make_app(tmp_path, payments=stripe, trial_minutes=30, free_mode=True)
    admin = TestClient(app)
    admin_setup(admin)  # 60分 500円の枠
    user = TestClient(app)
    signup(user, "user@example.com")

    home = user.get("/").text
    assert "実験段階" in home and "コミュニティ限定" not in home and "500円" not in home and "初回" not in home
    slots = user.get("/slots").text
    assert "無料" in slots and "500円" not in slots
    page = user.get("/slots/1/confirm").text
    assert "無料で予約する" in page and "500円" not in page

    r = user.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "/bookings/1"
    b = db(app).get(Booking, 1)
    assert (b.status, b.amount_yen, b.payment_provider) == ("confirmed", 0, "free")
    assert stripe.amounts == []
    assert "無料" in user.get("/bookings/1").text

    # 実験中の規約と、売り買いの表記を出さないこと
    terms = user.get("/legal/terms").text
    assert "実験のあいだは、お金もうけをしません" in terms and "有料のサービスにする予定" in terms and "AGPL-3.0" in terms and "ひな形" not in terms
    assert "集める情報" in user.get("/legal/privacy").text
    assert user.get("/legal/tokushoho").status_code == 404
    assert "特定商取引法" not in home


def test_free_mode_is_on_unless_turned_off(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.delenv("FREE_MODE", raising=False)
    assert Settings.from_env().free_mode is True
    monkeypatch.setenv("FREE_MODE", "0")
    assert Settings.from_env().free_mode is False


def test_invite_link_lets_testers_in_and_carries_the_meta_username(tmp_path, now):
    """合言葉の代わりに、Discord のボットが渡す本人専用リンクで入る。Meta ユーザー名は入れ直さなくてよい。"""
    from app import invite

    app, _ = make_app(tmp_path, invite_only=True, free_mode=True)
    admin = TestClient(app)
    admin.get(f"/join?t={invite.make_token('test', meta='', discord_id=9, now=clock_now(now))}")
    admin_setup(admin)

    stranger = TestClient(app)
    r = stranger.get("/slots", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/site-login")
    page = stranger.get("/site-login").text
    assert "招待制" in page and "合言葉" not in page
    assert "使えません" in stranger.get("/join?t=broken.token").text

    tester = TestClient(app)
    token = invite.make_token("test", meta="hikari_quest", discord_id=1, now=clock_now(now))
    r = tester.get(f"/join?t={token}", follow_redirects=False)
    assert r.headers["location"] == "/signup"
    assert "hikari_quest" in tester.get("/signup").text
    r = tester.post("/signup", data={"email": "hikari@example.com", "password": "password123", "agree": "1"},
                    follow_redirects=False)
    assert r.headers["location"] == "/slots"
    user = next(u for u in db(app).query(User).all() if u.email == "hikari@example.com")
    assert user.meta_username == "hikari_quest"
    assert tester.post("/slots/1/book", follow_redirects=False).headers["location"] == "/bookings/1"

    # 評価フォームへのリンクも、同じしくみで直接開ける
    other = TestClient(app)
    r = other.get(f"/join?t={token}&next=%2Ffeedback%3Ft%3D3", follow_redirects=False)
    assert r.headers["location"] == "/feedback?t=3"


def test_booking_page_sends_testers_back_to_their_discord_thread(tmp_path, now):
    """予約できたら、予約ページから Discord の自分のスレッドに戻って「予約した」を押してもらう。"""
    from app import invite

    app, _ = make_app(tmp_path, invite_only=True, free_mode=True)
    admin = TestClient(app)
    admin.get(f"/join?t={invite.make_token('test', meta='', discord_id=9, now=clock_now(now))}")
    admin_setup(admin)

    tester = TestClient(app)
    token = invite.make_token("test", meta="hikari_quest", discord_id=1, now=clock_now(now), thread="555/777")
    tester.get(f"/join?t={token}")
    tester.post("/signup", data={"email": "hikari@example.com", "password": "password123", "agree": "1"})
    user = next(u for u in db(app).query(User).all() if u.email == "hikari@example.com")
    assert user.discord_thread == "555/777"
    tester.post("/slots/1/book")
    page = tester.get("/bookings/1").text
    assert "https://discord.com/channels/555/777" in page and "「予約した」" in page

    # 別のブラウザでログインし直しても、アカウントに残っているので戻れる
    phone = TestClient(app)
    phone.get(f"/join?t={invite.make_token('test', meta='hikari_quest', discord_id=1, now=clock_now(now))}")
    phone.post("/login", data={"email": "hikari@example.com", "password": "password123"})
    assert "https://discord.com/channels/555/777" in phone.get("/bookings/1").text

    # Discord から来ていない人には出さない
    plain = TestClient(app)
    plain.get(f"/join?t={invite.make_token('test', meta='q2', discord_id=2, now=clock_now(now))}")
    plain.post("/signup", data={"email": "q2@example.com", "password": "password123", "agree": "1"})
    plain.post("/slots/2/book")
    assert "discord.com/channels" not in plain.get("/bookings/2").text


def test_public_site_opens_for_anyone_and_still_takes_discord_links(tmp_path, now):
    """公開しているサイトは、リンクがなくても開ける。Discord のリンクから来た人は、これまでどおり引き継ぐ。"""
    from app import invite

    app, _ = make_app(tmp_path, free_mode=True)
    admin = TestClient(app)
    admin_setup(admin)

    stranger = TestClient(app)
    assert stranger.get("/slots", follow_redirects=False).status_code == 200
    r = stranger.get("/site-login?next=/slots", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/slots"
    # 期限切れのリンクでも、行き止まりにしない
    r = stranger.get("/join?t=broken.token", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signup"
    assert "期限が切れていた" in stranger.get("/signup").text

    tester = TestClient(app)
    token = invite.make_token("test", meta="hikari_quest", discord_id=1, now=clock_now(now), thread="555/777")
    tester.get(f"/join?t={token}")
    tester.post("/signup", data={"email": "hikari@example.com", "password": "password123", "agree": "1"})
    user = next(u for u in db(app).query(User).all() if u.email == "hikari@example.com")
    assert user.meta_username == "hikari_quest" and user.discord_thread == "555/777"
    tester.post("/slots/1/book")
    assert "https://discord.com/channels/555/777" in tester.get("/bookings/1").text


def core_answers():
    """かならず答える質問の答え (どれも最初の選択肢)。"""
    from app import web_test

    return {q["key"]: [q["options"][0][0]] if q.get("multi") else q["options"][0][0] for q in web_test.core_questions()}


def test_testers_run_the_whole_test_on_the_booking_page(tmp_path, now):
    """Discord に戻らなくても、予約ページのチェックリストだけでテストを最後まで進められる。"""
    from app.tester_store import TesterStore

    app, _ = make_app(tmp_path, free_mode=True, tester_db=str(tmp_path / "tester_bot.db"))
    admin = TestClient(app)
    admin_setup(admin)  # JST 13:00〜14:00 の枠 (いまは 12:00)
    user = TestClient(app)
    signup(user, "hikari@example.com", meta="hikari_quest")
    user.post("/slots/1/book")
    store = TesterStore(tmp_path / "tester_bot.db")

    page = user.get("/bookings/1").text
    assert "テストのチェックリスト" in page and "12:50 から押せます" in page and "Discord に戻ってください" not in page
    user.post("/bookings/1/test/start")  # 早すぎる
    assert store.session_for_booking(1) is None

    now["t"] = NOW + dt.timedelta(minutes=52)
    user.post("/bookings/1/test/start")
    user.post("/bookings/1/test/start")  # 2 回押しても 1 回
    s = store.session_for_booking(1)
    assert s["user_id"] < 0 and s["pc"] == "PC-1" and s["status"] == "connecting"
    page = user.get("/bookings/1").text
    assert "「PC-1」を選んで" in page and "ワールドに入れた" in page

    # 困った → 運営に届く (急ぎ) ・管理者にメール
    user.post("/bookings/1/test/help", data={"what": "Virtual Desktop に PC が出てこない"})
    now["t"] += dt.timedelta(minutes=3)
    user.post("/bookings/1/test/entered")
    s = store.session_for_booking(1)
    assert s["help_count"] == 1 and s["status"] == "playing"
    assert "入るまで 3分00秒" in user.get("/bookings/1").text

    # 遊び終わった → 最後の画面で FPS と Ping (かならず)・アンケート 5問 (かならず)・写真とほかの質問 (時間があれば)
    user.post("/bookings/1/test/done")
    page = user.get("/bookings/1").text
    assert "FPS・Ping・遅れ（かならず）" in page and "時間があれば、こちらも" in page and "送ってテストを終える" in page
    answers = core_answers()
    assert len(answers) == 8
    # FPS と Ping がないと送れない
    r = user.post("/bookings/1/test/survey", data=answers)
    assert "Virtual Desktop の遅れを入れてください" in r.text and store.session_for_booking(1)["status"] == "survey"
    assert "数字を確かめて" in user.post("/bookings/1/test/survey", data={**answers, "fps_0": "999", "ping": "40", "latency": "25"}).text
    # アンケートに答えていない質問があると送れない
    r = user.post("/bookings/1/test/survey", data={**answers, "price": "", "fps_0": "72", "ping": "40", "latency": "25"})
    assert "1時間いくらなら払いますか？" in r.text and store.session_for_booking(1)["status"] == "survey"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = user.post("/bookings/1/test/survey",
                  data={**answers, "fps_0": "72", "fps_1": "", "ping": "40", "latency": "25", "mission": ["heavy", "photo"],
                        "when": ["event", "photo"], "comment": "楽しかった"},
                  files=[("photos", ("shot.png", png, "image/png")), ("photos", ("note.txt", b"x", "text/plain"))])
    assert r.status_code == 200
    s = store.session_for_booking(1)
    assert s["status"] == "done" and s["comment"] == "楽しかった"
    assert TesterStore.world_fps(s) == {"日本語話者向け集会場": 72} and s["ping_ms"] == 40 and s["latency_ms"] == 25
    assert json.loads(s["missions"]) == {"heavy": True, "crowd": False, "photo": True}
    assert json.loads(s["answers"]) == {**answers, "when": ["event", "photo"]}
    shots = json.loads(s["screenshots"])
    assert len(shots) == 1 and Path(shots[0]).read_bytes() == png
    assert Path(shots[0]).parent == tmp_path / "tester_screenshots"
    assert "テストは完了です" in user.get("/bookings/1").text
    assert "3日後" not in user.get("/bookings/1").text

    # 運営へのお知らせ (ボットが運営チャンネルに伝える)。名前やメールアドレスは書かない
    events = store.pending_web_events()
    assert [e["kind"] for e in events] == ["start", "help", "entered", "done"]
    assert events[1]["urgent"] == 1 and "PC が出てこない" in events[1]["text"] and "Meta: hikari_quest" in events[1]["text"]
    assert all("hikari@example.com" not in e["text"] for e in events)
    # ボットの集計にもそのまま入る
    assert store.summary()["done"] == 1 and store.summary()["connected_rate"] == 1.0

    # ほかの人は押せない
    other = TestClient(app)
    signup(other, "other@example.com")
    assert other.post("/bookings/1/test/start").status_code == 404
    store.close()


def test_booking_page_test_for_a_discord_tester_and_one_who_could_not_connect(tmp_path, now):
    """Discord のスレッドから来た人は、ボットと同じテスターとして記録する。つながらなかったら、理由だけ聞いて終える。"""
    from app import invite
    from app.tester_store import TesterStore

    store = TesterStore(tmp_path / "tester_bot.db")
    store.apply(42, "hikari", vrchat="v", meta="hikari_quest", gear="Quest 3", region="関東", schedule="", now=NOW)
    store.set_thread(42, 777)
    app, _ = make_app(tmp_path, free_mode=True, tester_db=str(tmp_path / "tester_bot.db"))
    admin = TestClient(app)
    admin_setup(admin)
    user = TestClient(app)
    user.get(f"/join?t={invite.make_token('test', meta='hikari_quest', discord_id=42, now=clock_now(now), thread='555/777')}")
    user.post("/signup", data={"email": "hikari@example.com", "password": "password123", "agree": "1"})
    user.post("/slots/1/book")
    page = user.get("/bookings/1").text
    assert "テストのチェックリスト" in page and "スレッドを開く" in page

    now["t"] = NOW + dt.timedelta(minutes=55)
    user.post("/bookings/1/test/start")
    s = store.session_for_booking(1)
    assert (s["user_id"], s["thread_id"]) == (42, 777)
    user.post("/bookings/1/test/giveup")
    assert "どこで止まったか" in user.get("/bookings/1").text
    user.post("/bookings/1/test/stuck", data={"stuck": "VR の画面が真っ暗"})
    s = store.session_for_booking(1)
    assert s["status"] == "done" and s["connected"] == 0 and s["stuck"] == "VR の画面が真っ暗"
    events = store.pending_web_events()
    assert [e["kind"] for e in events] == ["start", "giveup", "done"] and "<#777>" in events[0]["text"]
    store.close()


def test_test_needs_fps_and_ping_unless_they_could_not_be_shown(tmp_path, now):
    """FPS と Ping はかならず集める。どうしても表示できなかった人は、そう答えて終えられる。"""
    from app.tester_store import TesterStore

    app, _ = make_app(tmp_path, free_mode=True, tester_db=str(tmp_path / "tester_bot.db"))
    admin = TestClient(app)
    admin_setup(admin)
    user = TestClient(app)
    signup(user, "hikari@example.com")
    user.post("/slots/1/book")
    now["t"] = NOW + dt.timedelta(minutes=55)
    for step in ("start", "entered", "done"):
        user.post(f"/bookings/1/test/{step}")
    store = TesterStore(tmp_path / "tester_bot.db")
    user.post("/bookings/1/test/survey", data=core_answers())
    assert store.session_for_booking(1)["status"] == "survey"  # FPS と Ping がまだ
    # どうしても表示できなかったときは、チェックして送れる (記録に残る)
    user.post("/bookings/1/test/survey", data={**core_answers(), "no_perf": "1"})
    s = store.session_for_booking(1)
    assert s["status"] == "done" and s["comment"] == "FPS・Ping: 表示できなかった" and s["ping_ms"] is None
    assert [e["kind"] for e in store.pending_web_events()][-1] == "done"
    store.close()


def test_booking_page_has_no_test_checklist_without_the_tester_record(tmp_path, now):
    app, _ = make_app(tmp_path, free_mode=True)
    admin = TestClient(app)
    admin_setup(admin)
    user = TestClient(app)
    signup(user, "hikari@example.com")
    user.post("/slots/1/book")
    assert "テストのチェックリスト" not in user.get("/bookings/1").text
    assert user.post("/bookings/1/test/start").status_code == 404


def test_lenders_set_their_hours_on_the_website(tmp_path, now):
    """貸せる時間は、アプリを開かなくても貸し出しページのカレンダーで決められる (アプリと同じ決まり)。"""
    app, _ = make_app(tmp_path, free_mode=True)
    lender = TestClient(app)
    signup(lender, "lender@example.com")
    body = {"email": "lender@example.com", "password": "password123", "pc_name": "LENDER-PC",
            "machine_id": "abcdef0123456789"}
    host_id = 1
    assert TestClient(app).post("/api/agent/login", json=body).json()["host"] == "LENDER-PC"

    page = lender.get("/lend").text
    assert "貸し出す時間" in page and f'data-host="{host_id}"' in page and "確認待ち" in page
    start = "2026-10-09T15:00:00Z"  # JST 10/10 0:00
    r = lender.get(f"/lend/hosts/{host_id}/schedule", params={"start": start}).json()
    assert r["host"] == "LENDER-PC" and r["slots"] == []
    r = lender.put(f"/lend/hosts/{host_id}/schedule",
                   json={"start": start, "days": 7, "price_yen": r["price_yen"],
                         "starts": ["2026-10-10T05:00:00Z", "2026-10-10T06:00:00Z"]}).json()
    assert r["added"] == 2 and [s["state"] for s in r["slots"]] == ["open", "open"]
    # 外すと消える
    r = lender.put(f"/lend/hosts/{host_id}/schedule",
                   json={"start": start, "days": 7, "price_yen": 500, "starts": ["2026-10-10T06:00:00Z"]}).json()
    assert [s["start"] for s in r["slots"]] == ["2026-10-10T06:00:00Z"]
    assert lender.put(f"/lend/hosts/{host_id}/schedule", json={"start": "x"}).status_code == 400

    # ほかの人の PC は見えない・変えられない
    other = TestClient(app)
    signup(other, "other@example.com")
    assert other.get(f"/lend/hosts/{host_id}/schedule", params={"start": start}).status_code == 404
    assert other.put(f"/lend/hosts/{host_id}/schedule",
                     json={"start": start, "days": 7, "price_yen": 500, "starts": []}).status_code == 404


def test_new_lender_pc_reaches_the_ops_channel(tmp_path, now):
    """貸し手がアプリでログインして PC が確認待ちになったら、Discord の運営チャンネルにも知らせる (ボットが伝える)。"""
    from app.tester_store import TesterStore

    app, _ = make_app(tmp_path, tester_db=str(tmp_path / "tester_bot.db"))
    lender = TestClient(app)
    signup(lender, "lender@example.com", meta="lend_meta")
    body = {"email": "lender@example.com", "password": "password123", "pc_name": "MY-PC", "machine_id": "abcdef0123456789"}
    assert TestClient(app).post("/api/agent/login", json=body).json()["pending"] is True
    store = TesterStore(tmp_path / "tester_bot.db")
    events = store.pending_web_events()
    assert [e["kind"] for e in events] == ["lender_pc"] and events[0]["urgent"] == 1
    assert "MY-PC" in events[0]["text"] and "lend_meta" in events[0]["text"] and "/admin" in events[0]["text"]
    TestClient(app).post("/api/agent/login", json=body)  # 同じ PC でログインし直しても、2 回は知らせない
    assert len(store.pending_web_events()) == 1
    store.close()


def clock_now(now):
    return now["t"].replace(tzinfo=dt.timezone.utc)


def test_guide_and_terms_are_readable_before_joining(tmp_path):
    app, _ = make_app(tmp_path, invite_only=True)
    client = TestClient(app)
    page = client.get("/guide", follow_redirects=False)
    assert page.status_code == 200 and "Meta ユーザー名の見つけ方" in page.text
    for img in ("meta-1-app", "meta-2-home", "meta-3-profile", "fps-1-setting", "fps-2-shown"):
        assert f"/static/guide/{img}.webp" in page.text
        assert client.get(f"/static/guide/{img}.webp").status_code == 200
    assert client.get("/legal/terms", follow_redirects=False).status_code == 200
    rules = client.get("/legal/platforms", follow_redirects=False)
    assert rules.status_code == 200 and "Steam 利用契約" in rules.text and "自分のアカウント" in rules.text
    assert "/legal/platforms" in client.get("/legal/terms").text
    # 貸す人向けの説明に切り替えられる
    lend = client.get("/guide?for=lend")
    assert "貸せる時間を決める" in lend.text and "geaspec://open" in lend.text and "Meta ユーザー名の見つけ方" not in lend.text
    assert client.get("/slots", follow_redirects=False).status_code == 303


def test_simple_mode_hides_follow_reviews_and_the_app_page(tmp_path, now):
    """試作版では、フォロー・貸し手ページと評価・「アプリとして使う」を出さない。"""
    app, _ = make_app(tmp_path, simple_mode=True)
    admin = TestClient(app)
    admin_setup(admin)
    user = TestClient(app)
    signup(user, "user@example.com")
    for page in ("/", "/slots", "/account"):
        html = user.get(page).text
        assert "フォロー中" not in html and 'href="/app"' not in html and "/lenders/" not in html, page
    # 貸し出しのページには、Streamer のダウンロードと「時間の登録はアプリから」の案内を出す
    lend = user.get("/lend").text
    assert "https://www.vrdesktop.net/" in lend and "貸せる時間はこのページで決められます" in lend
    assert user.get("/following").status_code == 404
    assert user.get("/app").status_code == 404
    assert user.get("/lenders/1").status_code == 404
    assert user.post("/lenders/1/follow", data={"on": 1}).status_code == 404
    assert user.post("/bookings/1/review", data={"stars": 5}).status_code == 404
    # 予約そのものはこれまでどおりできる
    assert user.post("/slots/1/book", follow_redirects=False).status_code == 303


def test_lender_pc_specs_are_shown_to_borrowers(tmp_path, now):
    app, _ = make_app(tmp_path)
    admin = TestClient(app)
    token = admin_setup(admin)
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    # アプリが調べた性能を送る。長すぎる文字や変な値は捨てる
    body = {"gpu": "NVIDIA GeForce RTX 4070", "vram_gb": 12, "cpu": "AMD Ryzen 7 7700  8-Core", "ram_gb": 32,
            "extra": "x", "os": 1}
    assert agent.post("/api/agent/specs", json=body).json() == {"ok": True}
    assert TestClient(app).post("/api/agent/specs", json=body).status_code == 401
    user = TestClient(app)
    signup(user, "user@example.com")
    line = "NVIDIA GeForce RTX 4070（12GB）・AMD Ryzen 7 7700 8-Core・メモリ 32GB"
    assert line in user.get("/slots").text
    assert agent.post("/api/agent/specs", json={"gpu": "x" * 500, "ram_gb": -1}).json() == {"ok": True}
    assert "x" * 100 in user.get("/slots").text and "x" * 101 not in user.get("/slots").text


def test_pages_are_compressed_and_static_files_cached(tmp_path):
    app, _ = make_app(tmp_path)
    client = TestClient(app)
    page = client.get("/guide", headers={"Accept-Encoding": "gzip"})
    assert page.headers.get("content-encoding") == "gzip"
    assert "/static/style.css?v=" in page.text
    css = client.get("/static/style.css")
    assert "max-age=86400" in css.headers["cache-control"]
    assert "cache-control" not in client.get("/guide").headers  # ページそのものは毎回新しく


def add_hours(agent, *starts):
    # 13:00 (UTC 04:00) の枠は admin_setup で作ったもの。外すと消えるので必ず入れる
    body = {"start": "2026-10-09T15:00:00Z", "days": 7, "price_yen": 500,
            "starts": ["2026-10-10T04:00:00Z", *starts]}
    r = agent.put("/api/agent/schedule", json=body).json()
    return {s["start"]: s for s in r["slots"]}


def test_consecutive_hours_are_booked_as_one_long_session(setup, now):
    app, vpn, user, agent = setup
    slots = add_hours(agent, "2026-10-10T05:00:00Z", "2026-10-10T06:00:00Z", "2026-10-10T08:00:00Z")
    six = slots["2026-10-10T06:00:00Z"]["id"]

    # 続いて空いている 05:00・06:00 は「何時まで」でえらべる。すき間のある 08:00 はえらべない
    page = user.get("/slots/1/confirm").text
    assert "何時まで使いますか" in page
    assert "16:00 まで（3時間）" in page and "18:00" not in page
    page = user.get(f"/slots/1/confirm?until={six}").text
    assert "3時間" in page and "16:00" in page and "5分前に終了" in page

    r = user.post("/slots/1/book", data={"until": six}, follow_redirects=False)
    assert r.headers["location"] == "/bookings/1"
    slot = db(app).get(Slot, 1)
    assert (slot.start_at, slot.end_at, slot.price_yen, slot.merged) == (
        dt.datetime(2026, 10, 10, 4), dt.datetime(2026, 10, 10, 7), 1500, True)
    assert vpn.issued == [("tag:borrower-pc1", 4 * 3600)]
    # 一覧には 08:00 だけが残る。貸し手のカレンダーでは 3 時間ぶんが予約ありになる
    assert len(re.findall(r'href="/slots/\d+/confirm"', user.get("/slots").text)) == 1
    states = add_hours(agent, "2026-10-10T05:00:00Z", "2026-10-10T06:00:00Z", "2026-10-10T08:00:00Z")
    assert states["2026-10-10T04:00:00Z"]["end"] == "2026-10-10T07:00:00Z"
    assert states["2026-10-10T04:00:00Z"]["state"] == "booked"

    # Agent には 1 回の長い利用として渡る (途中でデータ消去やログアウトが入らない)
    now["t"] = dt.datetime(2026, 10, 10, 3, 58)
    job = agent.get("/api/agent/poll").json()
    assert job["action"] == "start" and job["remaining_seconds"] == (2 * 60 + 55) * 60 + 120

    # 始まる前にキャンセルされたら、1 時間ずつの枠に戻る
    now["t"] = NOW
    with db(app) as s:
        from app import services
        services.cancel_booking(s, s.get(Booking, 1), vpn, "テスト")
    with db(app) as s:
        hours = [(x.start_at.hour, x.end_at.hour, x.price_yen, x.merged) for x in s.query(Slot).order_by(Slot.start_at)]
    assert hours == [(4, 5, 500, False), (5, 6, 500, False), (6, 7, 500, False), (8, 9, 500, False)]
    assert len(re.findall(r'href="/slots/\d+/confirm"', user.get("/slots").text)) == 4


def test_long_bookings_can_be_limited(tmp_path, now):
    app, vpn = make_app(tmp_path, max_booking_hours=2)
    admin = TestClient(app)
    token = admin_setup(admin)
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    slots = add_hours(agent, "2026-10-10T05:00:00Z", "2026-10-10T06:00:00Z")
    user = TestClient(app)
    signup(user, "user@example.com")
    page = user.get("/slots/1/confirm").text
    assert "15:00 まで" in page and "16:00 まで" not in page
    r = user.post("/slots/1/book", data={"until": slots["2026-10-10T06:00:00Z"]["id"]}, follow_redirects=False)
    assert r.headers["location"] == "/slots/1/confirm"
    assert db(app).get(Slot, 1).end_at == dt.datetime(2026, 10, 10, 5)


def test_a_started_slot_can_be_booked_right_now_while_the_pc_is_on(setup, now):
    app, vpn, user, agent = setup
    now["t"] = dt.datetime(2026, 10, 10, 4, 20)  # 13:00 の枠が始まって 20 分
    assert "今すぐ入る" not in user.get("/slots").text  # PC のアプリから連絡がない間は出さない
    agent.get("/api/agent/poll")
    page = user.get("/slots").text
    assert "今すぐ入る" in page and "あと35分" in page

    r = user.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "/bookings/1"
    job = agent.get("/api/agent/poll").json()
    assert job["action"] == "start" and job["remaining_seconds"] == 35 * 60


def test_a_started_slot_needs_twenty_minutes_left(setup, now):
    app, vpn, user, agent = setup
    now["t"] = dt.datetime(2026, 10, 10, 4, 40)
    agent.get("/api/agent/poll")
    assert "今すぐ入る" not in user.get("/slots").text
    assert user.post("/slots/1/book", follow_redirects=False).headers["location"] == "/slots"


def test_started_slot_price_is_only_the_time_left(tmp_path, now):
    stripe = StubStripe()
    app, _ = make_app(tmp_path, payments=stripe)
    admin = TestClient(app)
    token = admin_setup(admin)
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    now["t"] = dt.datetime(2026, 10, 10, 4, 22)  # 残り 33 / 55 分
    agent.get("/api/agent/poll")
    user = TestClient(app)
    signup(user, "user@example.com")
    user.post("/slots/1/book", follow_redirects=False)
    assert stripe.amounts == [300]


def test_slot_list_marks_slots_that_can_run_on(setup, now):
    app, vpn, user, agent = setup
    add_hours(agent, "2026-10-10T05:00:00Z", "2026-10-10T06:00:00Z", "2026-10-10T08:00:00Z")
    page = user.get("/slots").text
    # 13:00〜16:00 は続けて空いている。続きのない 15:00 と、すき間のあとの 17:00 には出さない
    assert "連続可能 最大3時間（〜16:00）" in page
    assert "連続可能 最大2時間（〜16:00）" in page
    assert page.count("連続可能 最大") == 2


def test_own_domain_sends_http_visitors_to_https(tmp_path):
    """ログインのクッキーは https でしか送られないので、http://geaspec.com で来た人は https に送る。"""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", secret_key="test",
                        media_dir=str(tmp_path / "media"), base_url="https://geaspec.com", secure_cookies=True)
    app = create_app(settings, payments={"fake": FakePayments()}, vpn=FakeVpn())
    c = TestClient(app, base_url="http://geaspec.com")
    r = c.get("/login?next=/lend", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://geaspec.com/login?next=/lend"
    r = c.post("/login", data={"email": "a@example.com", "password": "x"}, follow_redirects=False)
    assert r.status_code == 308  # 入力した内容ごと https に送り直す
    # Cloudflare 経由で https から来た通信は、そのまま通す (X-Forwarded-Proto は uvicorn が https に直す)
    https = TestClient(app, base_url="https://geaspec.com")
    assert https.get("/healthz", follow_redirects=False).status_code == 200
    assert c.get("/healthz", headers={"cf-visitor": '{"scheme":"https"}'}, follow_redirects=False).status_code == 200
    # この PC で開く localhost は http のまま使える
    local = TestClient(app, base_url="http://localhost:8000")
    assert local.get("/healthz", follow_redirects=False).status_code == 200


class SentMailer:
    """送ったことにして、中身を残す (メールを送れるサイトのつもり)。"""

    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))


def mail_app(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", secret_key="test",
                        admin_emails=frozenset({"admin@example.com"}), media_dir=str(tmp_path / "media"),
                        base_url="http://testserver")
    mailer = SentMailer()
    app = create_app(settings, payments={"fake": FakePayments()}, vpn=FakeVpn(), mailer=mailer)
    return app, mailer


def link_in(mail, path):
    return re.search(rf"http://testserver({path}\?t=[\w.\-]+)", mail[2]).group(1)


def test_forgotten_password_can_be_reset_by_mail(tmp_path, now):
    app, mailer = mail_app(tmp_path)
    c = TestClient(app)
    signup(c, "user@example.com")
    c.post("/logout")
    assert "パスワードを忘れた方" in c.get("/login").text
    mailer.sent.clear()
    r = c.post("/forgot", data={"email": "User@Example.com"})
    assert "パスワードを再設定するメールを送りました" in r.text
    assert [m[0] for m in mailer.sent] == ["user@example.com"]
    link = link_in(mailer.sent[0], "/reset")
    # 登録していないアドレスでも同じ画面 (登録されているかどうかを知られないように)。メールは送らない
    r = c.post("/forgot", data={"email": "nobody@example.com"})
    assert "パスワードを再設定するメールを送りました" in r.text and len(mailer.sent) == 1
    # 続けて押しても、同じ人に何通も送らない
    c.post("/forgot", data={"email": "user@example.com"})
    assert len(mailer.sent) == 1

    assert "新しいパスワードを決めてください" in c.get(link).text
    t = link.split("t=")[1]
    assert "8 文字以上" in c.post("/reset", data={"t": t, "password": "short"}).text
    r = c.post("/reset", data={"t": t, "password": "newpassword1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/slots"
    assert c.get("/account").status_code == 200  # そのままログインしている
    # 1 回使ったリンクは、もう使えない
    assert "期限が切れたか、すでに使われています" in c.get(link).text
    c.post("/logout")
    assert c.post("/login", data={"email": "user@example.com", "password": "password123"}).status_code == 400
    r = c.post("/login", data={"email": "user@example.com", "password": "newpassword1"}, follow_redirects=False)
    assert r.status_code == 303


def test_reset_link_expires(tmp_path, now):
    app, mailer = mail_app(tmp_path)
    c = TestClient(app)
    signup(c, "user@example.com")
    c.post("/forgot", data={"email": "user@example.com"})
    link = link_in(mailer.sent[-1], "/reset")
    now["t"] = NOW + dt.timedelta(hours=1, minutes=1)
    assert "期限が切れたか" in c.get(link).text


def test_new_accounts_confirm_their_mail_before_booking(tmp_path, now):
    app, mailer = mail_app(tmp_path)
    admin = TestClient(app)
    admin_setup(admin)
    c = TestClient(app)
    signup(c, "fake@example.com")
    assert mailer.sent[-1][0] == "fake@example.com" and "メールアドレスの確認" in mailer.sent[-1][1]
    page = c.get("/slots").text
    assert "メールアドレスの確認がまだです" in page
    # 確認するまでは予約できない
    r = c.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"] == "/slots"
    assert c.get("/slots/1/confirm", follow_redirects=False).headers["location"] == "/slots"
    # すぐにもう一度は送らない
    before = len(mailer.sent)
    c.post("/verify/resend", data={"next": "/slots"})
    assert len(mailer.sent) == before
    # 届いたリンクを開くと予約できる
    link = link_in(mailer.sent[-1], "/verify")
    r = c.get(link, follow_redirects=False)
    assert r.headers["location"] == "/slots"
    assert "メールアドレスの確認がまだです" not in c.get("/slots").text
    r = c.post("/slots/1/book", follow_redirects=False)
    assert r.headers["location"].startswith("/bookings/")


def test_verify_link_does_not_work_for_another_account(tmp_path, now):
    app, mailer = mail_app(tmp_path)
    a, b = TestClient(app), TestClient(app)
    signup(a, "a@example.com")
    signup(b, "b@example.com")
    link = link_in(mailer.sent[0], "/verify")
    forged = link.replace("t=", "t=x")
    assert b.get(forged, follow_redirects=False).headers["location"] == "/slots"
    from app.models import User

    assert not db(app).query(User).filter_by(email="b@example.com").one().email_verified
    assert not db(app).query(User).filter_by(email="a@example.com").one().email_verified


def test_a_taken_address_points_to_password_reset(tmp_path, now):
    app, _ = mail_app(tmp_path)
    c = TestClient(app)
    signup(c, "user@example.com")
    c.post("/logout")
    r = c.post("/signup", data={"email": "user@example.com", "password": "password123", "agree": "1"})
    assert "登録済み" in r.text and "/forgot?email=user%40example.com" in r.text


def test_admin_can_make_a_reset_link_or_mark_mail_confirmed(tmp_path, now):
    app, mailer = mail_app(tmp_path)
    admin = TestClient(app)
    signup(admin, "admin@example.com")
    from app.models import User

    assert db(app).query(User).filter_by(email="admin@example.com").one().email_verified  # 運営は確認しなくてよい
    user = TestClient(app)
    signup(user, "user@example.com")
    page = admin.get("/admin").text
    assert "user@example.com" in page  # 確認がまだの人
    r = admin.post("/admin/users", data={"email": "user@example.com", "action": "reset"})
    link = re.search(r"http://testserver(/reset\?t=[\w.\-]+)", r.text).group(1)
    assert "新しいパスワードを決めてください" in TestClient(app).get(link).text
    admin.post("/admin/users", data={"email": "user@example.com", "action": "verify"})
    assert db(app).query(User).filter_by(email="user@example.com").one().email_verified
    assert "登録されていません" in admin.post("/admin/users", data={"email": "x@example.com", "action": "reset"}).text
    # 管理者でなければ使えない
    assert user.post("/admin/users", data={"email": "user@example.com", "action": "verify"}).status_code == 404


def test_without_mail_nobody_is_asked_to_confirm(tmp_path, now):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    signup(c, "user@example.com")
    assert "メールアドレスの確認がまだです" not in c.get("/slots").text
    c.post("/logout")
    assert "運営がパスワードを再設定するリンクを作ります" in c.post("/forgot", data={"email": "user@example.com"}).text


def test_people_from_before_count_as_confirmed(tmp_path):
    """列を足す前からいる人は、いきなり予約できなくならない。"""
    import sqlite3

    from app.db import add_missing_columns, make_engine

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR, password_hash VARCHAR, meta_username VARCHAR,"
                " signup_ref VARCHAR, nickname VARCHAR NOT NULL DEFAULT '', discord_thread VARCHAR NOT NULL DEFAULT '',"
                " created_at DATETIME)")
    con.execute("INSERT INTO users (email, password_hash) VALUES ('old@example.com', 'x')")
    con.commit()
    con.close()
    add_missing_columns(make_engine(f"sqlite:///{path}"))
    con = sqlite3.connect(path)
    assert con.execute("SELECT email_verified FROM users").fetchone() == (1,)
    con.close()


def test_english_top_page(tmp_path, now):
    """海外の人・会社に見せる英語のトップページ。ヘッダーとフッターも英語になる。"""
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    r = c.get("/en")
    assert r.status_code == 200
    assert '<html lang="en">' in r.text and "Play <mark" in r.text and "Sign up" in r.text
    assert "geaspec.contact@gmail.com" in r.text and 'href="/"' in r.text  # 日本語へ戻るリンク
    ja = c.get("/").text
    assert '<html lang="ja">' in ja and 'href="/en"' in ja and "新規登録" in ja


def test_agent_download_button_points_to_this_version(tmp_path, now):
    from app.version import VERSION

    app, _ = make_app(tmp_path)
    c = TestClient(app)
    r = c.get("/download/agent", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith(f"/agent-v{VERSION}/GeaspecAgent-Setup-{VERSION}.exe")
    assert 'href="/download/agent"' in c.get("/guide?for=lend").text
