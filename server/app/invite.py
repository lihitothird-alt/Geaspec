"""Discord から渡す、本人専用の招待リンク。

合言葉の代わりに、ボットがテスターごとに期限付きのリンクを作る。リンクを開いた人だけがサイトに入れて、
新規登録では申し込みのときの Meta ユーザー名がそのまま使われる (二度手間と書き間違いをなくす)。

リンクの中身は {Meta ユーザー名, Discord のユーザー番号, その人のスレッド, 期限} に、サイトの SECRET_KEY で署名したもの。
ボットとサイトは同じ黒い画面の中で動くので、同じ鍵で作って確かめられる。データベースはいらない。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re

LINK_DAYS = 14
THREAD_RE = re.compile(r"^[0-9]{1,20}/[0-9]{1,20}$")


def _key(secret: str) -> bytes:
    return hashlib.sha256(f"geaspec-invite:{secret}".encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(secret: str, *, meta: str, discord_id: int, now: dt.datetime, days: int = LINK_DAYS,
               thread: str = "") -> str:
    """thread は「サーバー番号/スレッド番号」。サイトから、その人のスレッドに戻るボタンを出すのに使う。"""
    expires = int((now + dt.timedelta(days=days)).timestamp())
    data = {"m": meta, "d": discord_id, "e": expires}
    if thread:
        data["t"] = thread
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest()[:16])
    return f"{body}.{sig}"


def read_token(secret: str, token: str, now: dt.datetime) -> dict | None:
    """正しく、期限内なら {"meta", "discord_id", "thread"}。それ以外は None。"""
    if not secret or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest()[:16])
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(body))
        expires = int(data["e"])
    except (ValueError, KeyError, TypeError):
        return None
    if now.timestamp() > expires:
        return None
    thread = str(data.get("t") or "")
    return {"meta": str(data.get("m") or ""), "discord_id": data.get("d"),
            "thread": thread if THREAD_RE.match(thread) else ""}


def thread_url(thread: str) -> str:
    """「サーバー番号/スレッド番号」から、Discord でそのスレッドを開くリンク。形がおかしければ空。"""
    return f"https://discord.com/channels/{thread}" if THREAD_RE.match(thread or "") else ""
