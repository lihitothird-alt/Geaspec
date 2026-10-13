"""お知らせメール。Gmail などの SMTP で送る。

設定がなければ送らずにログへ書くだけ (サイトはそのまま動く)。
送るのに時間がかかったり失敗したりしても画面を待たせないよう、別のスレッドで送る。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr

from .config import Settings

log = logging.getLogger(__name__)


class Mailer:
    """送らない (設定がないとき)。テストでは sent を見る。"""

    enabled = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))
        log.info("メール (未設定のため送っていません) to=%s subject=%s", to, subject)


class SmtpMailer(Mailer):
    enabled = True

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.host, self.port = settings.smtp_host, settings.smtp_port
        self.user, self.password = settings.smtp_user, settings.smtp_password
        self.sender = formataddr((settings.service_name, settings.mail_from or settings.smtp_user))

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))
        threading.Thread(target=self._send, args=(to, subject, body), daemon=True).start()

    def _send(self, to: str, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
            log.info("メールを送りました to=%s subject=%s", to, subject)
            return True
        except (OSError, smtplib.SMTPException):
            log.exception("メールを送れませんでした to=%s subject=%s", to, subject)
            return False


def build_mailer(settings: Settings) -> Mailer:
    if settings.smtp_user and settings.smtp_password:
        return SmtpMailer(settings)
    return Mailer()
