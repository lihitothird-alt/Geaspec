"""データモデル。日時はすべて UTC (タイムゾーンなし) で保存し、表示時に JST へ変換する。"""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .clock import utcnow
from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Virtual Desktop の接続許可に使う Meta (Quest) のユーザー名
    meta_username: Mapped[str] = mapped_column(String(64), default="")
    # どこから来たか (広告を貼ったワールド名など。LP の ?ref= から記録)
    signup_ref: Mapped[str] = mapped_column(String(64), default="")
    # ほかの人に見せる名前 (フォロー・評価・貸し手ページ)。メールアドレスは見せない
    nickname: Mapped[str] = mapped_column(String(32), default="")
    # Discord のテスター用スレッド (「サーバー番号/スレッド番号」)。予約のあと、そこへ戻るボタンを出す
    discord_thread: Mapped[str] = mapped_column(String(64), default="")
    # メールアドレスを確認したか (届いたリンクを開いた)。前からいる人は確認済みとして扱う
    email_verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    @property
    def display_name(self) -> str:
        return self.nickname or f"ユーザー{self.id}"


class Host(Base):
    """貸し出す PC 1 台。Agent はこの PC のトークンで API を呼ぶ。"""

    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 借り手に案内する接続先 (Tailscale の IP や MagicDNS 名)
    vpn_address: Mapped[str] = mapped_column(String(255), default="")
    # この PC の借り手に付ける Tailscale タグ (例: tag:borrower-pc1)
    vpn_borrower_tag: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(default=True)
    # 貸し手がアプリからログインして登録した PC は、運営が承認するまで貸し出さない
    pending: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None, index=True)
    # 貸し手 PC のアプリが作る ID。同じ PC でログインし直したときに、別の PC として増やさない
    machine_id: Mapped[str] = mapped_column(String(64), default="")
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # 貸し手のアプリが調べた性能 (JSON: gpu・vram_gb・cpu・ram_gb)。空き枠の一覧で借りる人に見せる
    specs: Mapped[str] = mapped_column(String(500), default="")
    # フォロワーに「新しい時間を公開しました」と知らせた時刻 (送りすぎないため)
    followers_notified_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    slots: Mapped[list["Slot"]] = relationship(back_populates="host")

    @property
    def spec_text(self) -> str:
        """「RTX 4070（12GB）・Ryzen 7 7700・メモリ 32GB」のような 1 行。"""
        try:
            s = json.loads(self.specs or "{}")
        except ValueError:
            return ""
        parts = []
        if s.get("gpu"):
            parts.append(s["gpu"] + (f"（{s['vram_gb']}GB）" if s.get("vram_gb") else ""))
        if s.get("cpu"):
            parts.append(s["cpu"])
        if s.get("ram_gb"):
            parts.append(f"メモリ {s['ram_gb']}GB")
        return "・".join(parts)
    owner: Mapped["User | None"] = relationship()


class Slot(Base):
    """予約できる時間枠。"""

    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"), index=True)
    start_at: Mapped[dt.datetime] = mapped_column(index=True)
    end_at: Mapped[dt.datetime]
    price_yen: Mapped[int]
    # 貸し手が受付をやめた枠 (予約履歴があって削除できないものは、消す代わりに閉じる)
    closed: Mapped[bool] = mapped_column(default=False)
    # 続けて予約されたときに、1 時間の枠をつないで 1 つにした枠 (キャンセルされたら 1 時間ずつに戻す)
    merged: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    host: Mapped[Host] = relationship(back_populates="slots")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="slot")

    # 枠の最後の数分は、片づけ (借り手のデータを消す) と次の人の準備にあてる。起動時に設定から入れる
    turnover = dt.timedelta(minutes=5)

    @property
    def play_end_at(self) -> dt.datetime:
        """借り手が使える終わりの時刻 (例: 13:00〜14:00 の枠なら 13:55)。短すぎる枠は削らない。"""
        if self.end_at - self.start_at > 2 * self.turnover:
            return self.end_at - self.turnover
        return self.end_at

    @property
    def play_minutes(self) -> int:
        return int((self.play_end_at - self.start_at).total_seconds() // 60)

    @property
    def duration_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds() // 60)


class BookingStatus:
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    LABELS = {
        PENDING_PAYMENT: "支払い待ち",
        CONFIRMED: "予約確定",
        ACTIVE: "利用中",
        COMPLETED: "終了",
        CANCELLED: "キャンセル",
        EXPIRED: "期限切れ",
    }


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=BookingStatus.PENDING_PAYMENT, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    paid_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    started_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    finished_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # 支払い: "stripe" | "paypal" | "fake" | "free" (初回無料で 0 円)
    payment_provider: Mapped[str] = mapped_column(String(16), default="")
    # Stripe の Checkout Session ID / PayPal の Order ID
    payment_ref: Mapped[str] = mapped_column(String(255), default="", index=True)
    amount_yen: Mapped[int] = mapped_column(default=0)
    discount_yen: Mapped[int] = mapped_column(default=0)
    vpn_key_id: Mapped[str] = mapped_column(String(255), default="")
    vpn_auth_key: Mapped[str] = mapped_column(String(255), default="")
    end_reason: Mapped[str] = mapped_column(String(64), default="")
    clean: Mapped[bool | None] = mapped_column(default=None)
    report_json: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    # 借り手のスクリーンショット (zip)。借り手本人だけがダウンロードでき、期限が来たら消す
    screenshots_file: Mapped[str] = mapped_column(String(255), default="")
    screenshots_count: Mapped[int] = mapped_column(default=0)
    screenshots_expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    slot: Mapped[Slot] = relationship(back_populates="bookings")
    user: Mapped[User] = relationship()

    @property
    def status_label(self) -> str:
        return BookingStatus.LABELS.get(self.status, self.status)

    @property
    def stripe_session_id(self) -> str:
        return self.payment_ref if self.payment_provider == "stripe" else ""


class Follow(Base):
    """借り手が貸し手をフォローしている (新しい時間のお知らせ・お気に入り)。"""

    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("user_id", "lender_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    lender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    following: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    lender: Mapped[User] = relationship(foreign_keys=[lender_id])


class Review(Base):
    """利用が終わった予約への評価。借り手→貸し手、貸し手→借り手の 2 方向がある。"""

    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("booking_id", "author_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 書いた人の立場: "borrower" (借り手が貸し手を評価) / "lender" (貸し手が借り手を評価)
    role: Mapped[str] = mapped_column(String(16))
    stars: Mapped[int]
    comment: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    booking: Mapped[Booking] = relationship()
    author: Mapped[User] = relationship(foreign_keys=[author_id])
    target: Mapped[User] = relationship(foreign_keys=[target_id])


class Feedback(Base):
    """テストの最後に答えてもらう評価フォーム (/feedback) の答え。中身は feedback.py の形の JSON。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Discord ボットのテストの回 (リンクの ?t=)。ボットの記録とつなげるのに使う
    session_ref: Mapped[int | None] = mapped_column(index=True, default=None)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    answers: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
