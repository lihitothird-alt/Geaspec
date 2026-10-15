"""予約サイトの予約を読んで、Discord で知らせる内容を決める。

Discord ボットは予約サイトと同じ黒い画面の中で動くので、サイトのデータベース (geaspec.db) を
読むだけで予約に気づける。書き込みはしない。知らせたかどうかはボット側の記録 (tester_bot.db) に残す。

- 予約が入った → 運営チャンネルと、その人のテスター用スレッドに日時を書く
- 開始 15 分前 → テスターには「もうすぐ」、運営には PC のアプリが動いているかを書く
- 知らせたあとでキャンセルされた → 運営に書く
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SOON_MINUTES = 15
# PC のアプリはふだん数十秒ごとにサイトへ問い合わせる。これより長く来ていなければ止まっているとみなす
AGENT_SILENT_MINUTES = 5
JST = dt.timezone(dt.timedelta(hours=9), "JST")
WEEKDAYS = "月火水木金土日"

QUERY = """
SELECT b.id, b.status, s.start_at, s.end_at, h.name, h.last_seen_at, u.meta_username, u.nickname
FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN hosts h ON h.id = s.host_id JOIN users u ON u.id = b.user_id
WHERE b.status IN ('confirmed', 'active', 'cancelled') AND s.end_at > ?
ORDER BY s.start_at
"""


def _utc(value) -> dt.datetime | None:
    """サイトの日時 (UTC・タイムゾーンなしの文字列) を UTC の日時にする。"""
    if not value:
        return None
    t = dt.datetime.fromisoformat(str(value))
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


def when_text(start: dt.datetime, end: dt.datetime) -> str:
    s, e = start.astimezone(JST), end.astimezone(JST)
    end = f"{e:%m/%d}（{WEEKDAYS[e.weekday()]}）{e:%H:%M}" if e.date() != s.date() else f"{e:%H:%M}"
    return f"{s:%m/%d}（{WEEKDAYS[s.weekday()]}）{s:%H:%M}〜{end}"


def load_bookings(path: Path, now: dt.datetime) -> list[dict]:
    """まだ終わっていない予約 (確定・利用中・キャンセル) を読む。読めなければ空。"""
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cutoff = now.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            rows = con.execute(QUERY, (cutoff,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    bookings = []
    for bid, status, start, end, host, seen, meta, nickname in rows:
        try:
            bookings.append({"id": bid, "status": status, "start": _utc(start), "end": _utc(end), "host": host,
                             "last_seen": _utc(seen), "meta": meta or "", "nickname": nickname or ""})
        except ValueError:
            continue
    return bookings


def due_notices(bookings: list[dict], now: dt.datetime, already) -> list[tuple[str, dict]]:
    """いま送るお知らせ [(種類, 予約)]。already(予約番号, 種類) は、もう送ったかどうか。"""
    due = []
    for b in bookings:
        if b["status"] == "cancelled":
            if already(b["id"], "booked") and not already(b["id"], "cancelled"):
                due.append(("cancelled", b))
            continue
        if not already(b["id"], "booked"):
            due.append(("booked", b))
        soon_from = b["start"] - dt.timedelta(minutes=SOON_MINUTES)
        if soon_from <= now < b["start"] + dt.timedelta(minutes=5) and not already(b["id"], "soon"):
            due.append(("soon", b))
    return due


def agent_running(booking: dict, now: dt.datetime) -> bool:
    seen = booking.get("last_seen")
    return seen is not None and now - seen <= dt.timedelta(minutes=AGENT_SILENT_MINUTES)


def who(booking: dict) -> str:
    name = booking["nickname"] or "（ニックネームなし）"
    return f"{name}（Meta: {booking['meta']}）" if booking["meta"] else name
