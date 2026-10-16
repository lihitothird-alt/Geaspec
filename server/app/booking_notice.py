"""予約サイトの予約を読んで、Discord で知らせる内容を決める。

Discord ボットは予約サイトと同じ黒い画面の中で動くので、サイトのデータベース (geaspec.db) を
読むだけで予約に気づける。書き込みはしない。知らせたかどうかはボット側の記録 (tester_bot.db) に残す。

- 予約が入った → 運営チャンネルと、その人のテスター用スレッドに日時を書く
- 開始 15 分前 → テスターには「もうすぐ」、運営には PC のアプリが動いているかを書く
- 知らせたあとでキャンセルされた → 運営に書く
- 貸す人には、PC が承認されたとき・予約が入ったとき・貸し終わったときに、その人のスレッドに書く
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
SELECT b.id, b.status, s.start_at, s.end_at, h.name, h.last_seen_at, u.meta_username, u.nickname,
       {threads}
FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN hosts h ON h.id = s.host_id JOIN users u ON u.id = b.user_id
LEFT JOIN users o ON o.id = h.owner_id
WHERE b.status IN ({statuses}) AND s.end_at > ?{extra}
ORDER BY s.start_at
"""
# Discord のスレッドの列は 0.7.17 で足した。サイトがまだ古い版のデータベースなら空として読む
THREAD_COLUMNS = "u.discord_thread, o.discord_thread"
NO_THREAD_COLUMNS = "'', ''"
LIVE_STATUSES = "'confirmed', 'active', 'cancelled'"


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


def load_bookings(path: Path, now: dt.datetime, *, finished: bool = False) -> list[dict]:
    """まだ終わっていない予約 (確定・利用中・キャンセル) を読む。finished なら、now 以降に終わった利用済みの予約。

    読めなければ空。thread は借りる人の、owner_thread は貸す人 (PC の持ち主) の Discord のスレッド。
    """
    if not path.exists():
        return []
    statuses = "'completed'" if finished else LIVE_STATUSES
    # 終わった予約は、PC のアプリが終了処理 (データの削除) まで報告したものだけ
    extra = " AND b.finished_at IS NOT NULL" if finished else ""
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cutoff = now.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            try:
                rows = con.execute(QUERY.format(threads=THREAD_COLUMNS, statuses=statuses, extra=extra),
                                   (cutoff,)).fetchall()
            except sqlite3.OperationalError:
                rows = con.execute(QUERY.format(threads=NO_THREAD_COLUMNS, statuses=statuses, extra=extra),
                                   (cutoff,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    bookings = []
    for bid, status, start, end, host, seen, meta, nickname, thread, owner_thread in rows:
        try:
            bookings.append({"id": bid, "status": status, "start": _utc(start), "end": _utc(end), "host": host,
                             "last_seen": _utc(seen), "meta": meta or "", "nickname": nickname or "",
                             "thread": thread or "", "owner_thread": owner_thread or ""})
        except ValueError:
            continue
    return bookings


HOSTS_QUERY = """
SELECT h.id, h.name, h.active, h.pending, o.discord_thread
FROM hosts h JOIN users o ON o.id = h.owner_id
"""


def load_lender_hosts(path: Path) -> list[dict]:
    """持ち主が Discord のスレッドから登録した PC。承認されたかを貸す人に知らせるのに使う。読めなければ空。"""
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(HOSTS_QUERY).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [{"id": hid, "name": name, "approved": bool(active) and not pending, "owner_thread": thread}
            for hid, name, active, pending, thread in rows if thread]


def thread_id(ref: str) -> int | None:
    """「サーバー番号/スレッド番号」のスレッド番号。"""
    try:
        return int(ref.split("/")[1]) if ref else None
    except (IndexError, ValueError):
        return None


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
