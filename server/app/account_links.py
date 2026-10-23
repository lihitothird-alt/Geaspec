"""メールで送る、アカウント用のリンク (パスワードの再設定・メールアドレスの確認)。

リンクの中身は {ユーザー番号, 用途, 期限, 確かめるための値} に、サイトの SECRET_KEY で署名したもの。データベースはいらない。
- パスワードの再設定: いまのパスワードから作った値を入れるので、パスワードを変えたら古いリンクは使えなくなる (1 回だけ)
- メールアドレスの確認: メールアドレスから作った値を入れるので、ほかのアドレスには使えない
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

RESET_HOURS = 1
# 運営が管理画面で作って、Discord などで渡すとき (メールを送れないとき)
ADMIN_RESET_HOURS = 24
VERIFY_DAYS = 7


def _key(secret: str) -> bytes:
    return hashlib.sha256(f"geaspec-account:{secret}".encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _check(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _sign(secret: str, body: str) -> str:
    return _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest()[:16])


def _make(secret: str, purpose: str, user_id: int, check: str, expires: dt.datetime) -> str:
    body = _b64(json.dumps({"u": user_id, "p": purpose, "c": check, "e": int(expires.timestamp())},
                           separators=(",", ":")).encode())
    return f"{body}.{_sign(secret, body)}"


def _read(secret: str, purpose: str, token: str, now: dt.datetime) -> dict | None:
    if not secret or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(secret, body)):
        return None
    try:
        data = json.loads(_unb64(body))
        if data["p"] != purpose or now.timestamp() > int(data["e"]):
            return None
        return {"user_id": int(data["u"]), "check": str(data["c"])}
    except (ValueError, KeyError, TypeError):
        return None


def reset_token(secret: str, user_id: int, password_hash: str, now: dt.datetime, hours: int = RESET_HOURS) -> str:
    return _make(secret, "reset", user_id, _check(password_hash), now + dt.timedelta(hours=hours))


def read_reset(secret: str, token: str, now: dt.datetime, password_hash_of) -> int | None:
    """正しく、期限内で、まだ使われていなければユーザー番号。password_hash_of(番号) はいまのパスワードの値。"""
    data = _read(secret, "reset", token, now)
    if data is None:
        return None
    current = password_hash_of(data["user_id"])
    if current is None or not hmac.compare_digest(data["check"], _check(current)):
        return None
    return data["user_id"]


def verify_token(secret: str, user_id: int, email: str, now: dt.datetime) -> str:
    return _make(secret, "verify", user_id, _check(email.lower()), now + dt.timedelta(days=VERIFY_DAYS))


def read_verify(secret: str, token: str, now: dt.datetime, email_of) -> int | None:
    data = _read(secret, "verify", token, now)
    if data is None:
        return None
    email = email_of(data["user_id"])
    if email is None or not hmac.compare_digest(data["check"], _check(email.lower())):
        return None
    return data["user_id"]
