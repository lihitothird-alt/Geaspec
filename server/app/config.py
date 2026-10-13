"""環境変数から読み込む設定。"""

from __future__ import annotations

import os
import secrets
import warnings
from dataclasses import dataclass, field


def _split(value: str | None) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    service_name: str = "Geaspec"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./pcvr.db"
    secret_key: str = ""
    admin_emails: frozenset[str] = field(default_factory=frozenset)
    # 使う決済手段 (カンマ区切りで複数可): "stripe" (クレジットカード), "paypal", "fake" (支払いなしで即確定・開発用)
    payment_providers: tuple[str, ...] = ("fake",)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_env: str = "sandbox"  # "sandbox" | "live"
    # 無料の実験として動かす (お金を受け取らない)。料金の表示と支払いをすべて止める
    free_mode: bool = True
    # 試作版としてシンプルに見せる: フォロー・貸し手ページと評価・「アプリとして使う」を隠す (データとコードは残す)
    simple_mode: bool = False
    # 初回利用時に無料にする分数 (同じ Meta ユーザー名では 1 回だけ)
    trial_minutes: int = 30
    # 借り手のスクリーンショットの保存先と保存期間
    media_dir: str = "./media"
    screenshot_days: int = 7
    screenshot_max_mb: int = 250
    # VPN: "manual" は運営が手作業で案内、"tailscale" は API で鍵を自動発行
    vpn_mode: str = "manual"
    tailscale_api_key: str = ""
    tailscale_tailnet: str = "-"
    # 支払い待ちで枠を確保しておく時間。Stripe Checkout の有効期限 (30分) より長くする
    hold_minutes: int = 35
    # 枠の最後の何分を、片づけと次の人の準備にあてるか (1 時間の枠なら 00〜55 分が利用時間)
    turnover_minutes: int = 5
    # 開始時刻の何秒前に Agent へ開始指示を出すか (前処理の時間)
    agent_start_lead_seconds: int = 300
    secure_cookies: bool = False
    # 設定するとサイト全体に合言葉をかける (招待した人だけが見られるようにする)
    site_password: str = ""
    # Discord のボットが渡す本人専用リンクを開いた人だけに見せる (合言葉の代わり)
    invite_only: bool = False
    # お知らせメール (Gmail なら smtp.gmail.com と「アプリ パスワード」)。未設定ならメールは送らない
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("SECRET_KEY", "")
        if not secret:
            warnings.warn("SECRET_KEY が未設定です。再起動するとログインが切れます (開発用のみ)")
            secret = secrets.token_hex(32)
        base_url = os.environ.get("BASE_URL", cls.base_url).rstrip("/")
        return cls(
            service_name=os.environ.get("SERVICE_NAME", cls.service_name),
            base_url=base_url,
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            secret_key=secret,
            admin_emails=_split(os.environ.get("ADMIN_EMAILS")),
            payment_providers=tuple(
                v.strip() for v in os.environ.get("PAYMENT_PROVIDERS", "fake").split(",") if v.strip()
            ),
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
            paypal_client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
            paypal_client_secret=os.environ.get("PAYPAL_CLIENT_SECRET", ""),
            paypal_env=os.environ.get("PAYPAL_ENV", cls.paypal_env),
            free_mode=os.environ.get("FREE_MODE", "1").strip().lower() not in ("0", "false", "no", "off"),
            simple_mode=os.environ.get("SIMPLE_MODE", "1").strip().lower() not in ("0", "false", "no", "off"),
            trial_minutes=int(os.environ.get("TRIAL_MINUTES", cls.trial_minutes)),
            media_dir=os.environ.get("MEDIA_DIR", cls.media_dir),
            screenshot_days=int(os.environ.get("SCREENSHOT_DAYS", cls.screenshot_days)),
            screenshot_max_mb=int(os.environ.get("SCREENSHOT_MAX_MB", cls.screenshot_max_mb)),
            vpn_mode=os.environ.get("VPN_MODE", cls.vpn_mode),
            tailscale_api_key=os.environ.get("TAILSCALE_API_KEY", ""),
            tailscale_tailnet=os.environ.get("TAILSCALE_TAILNET", cls.tailscale_tailnet),
            hold_minutes=int(os.environ.get("HOLD_MINUTES", cls.hold_minutes)),
            turnover_minutes=int(os.environ.get("TURNOVER_MINUTES", cls.turnover_minutes)),
            agent_start_lead_seconds=int(
                os.environ.get("AGENT_START_LEAD_SECONDS", cls.agent_start_lead_seconds)
            ),
            secure_cookies=base_url.startswith("https://"),
            site_password=os.environ.get("SITE_PASSWORD", ""),
            invite_only=os.environ.get("INVITE_ONLY", "0").strip().lower() in ("1", "true", "yes", "on"),
            smtp_host=os.environ.get("SMTP_HOST", cls.smtp_host),
            smtp_port=int(os.environ.get("SMTP_PORT", cls.smtp_port)),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", "").replace(" ", ""),
            mail_from=os.environ.get("MAIL_FROM", ""),
        )
