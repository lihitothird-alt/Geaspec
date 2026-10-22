"""予約ページで進めるテスト (チェックリスト → 計測 → 写真・数字 → アンケート)。

Discord のボットと同じ記録 (tester_bot.db) に書くので、ボットの集計 (/geaspec-results) と CSV にそのまま入る。
運営へのお知らせ (始めた・困った・終わった) は記録に残し、ボットが運営チャンネルに伝える。
Discord のスレッドから来た人はそのテスターとして、そうでない人はサイトの番号をマイナスにした人として記録する
(ボットのテスターの番号と重ならない)。
"""

from __future__ import annotations

import datetime as dt
import json

from . import booking_notice
from .models import Booking, BookingStatus, User
from .tester_store import MISSIONS, TesterStore, format_duration

# 開始の何分前から「テストを始める」を押せるか (Virtual Desktop を開いて待ってもらう)
START_EARLY_MINUTES = 10
MAX_PHOTOS = 10
MAX_PHOTO_MB = 15
PHOTO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_FPS = 240
MAX_PING_MS = 2000


class WebTestError(ValueError):
    pass


def identity(store: TesterStore, user: User) -> tuple[int, int]:
    """記録に使う (人の番号, スレッドの番号)。Discord のテスターなら、ボットと同じ番号にする。"""
    tid = booking_notice.thread_id(user.discord_thread or "")
    tester = store.tester_by_thread(tid) if tid else None
    if tester and tester.get("thread_id"):
        return tester["user_id"], tester["thread_id"]
    return -user.id, -user.id


def who(booking: Booking) -> str:
    """運営へのお知らせに書く「だれか」。メールアドレスは書かない。"""
    meta = booking.user.meta_username or "未設定"
    text = f"予約 #{booking.id}（{booking.slot.host.name}・Meta: {meta}）"
    thread = booking_notice.thread_id(booking.user.discord_thread or "")
    return text + (f" <#{thread}>" if thread else "")


def can_start(booking: Booking, now: dt.datetime) -> bool:
    return (booking.status in (BookingStatus.CONFIRMED, BookingStatus.ACTIVE)
            and booking.slot.start_at - dt.timedelta(minutes=START_EARLY_MINUTES) <= now < booking.slot.play_end_at)


def state(session: dict | None) -> dict:
    """チェックリストのどこまで終わったか (ボットのチェックリストと同じ決め方)。"""
    if session is None:
        return {"started": False, "status": "", "entered": False, "photo": False, "survey": False, "done": False,
                "connected": None, "missions": {}, "fps": {}, "ping": None, "photos": 0, "enter_text": "",
                "help_count": 0}
    answers = json.loads(session["answers"])
    fps = TesterStore.world_fps(session)
    photos = len(json.loads(session["screenshots"]))
    enter_text = ""
    if session["entered_at"]:
        enter_text = format_duration((dt.datetime.fromisoformat(session["entered_at"])
                                      - dt.datetime.fromisoformat(session["opened_at"])).total_seconds())
    return {
        "started": True,
        "status": session["status"],
        "entered": bool(session["entered_at"]),
        "photo": bool(photos or fps or session["ping_ms"] is not None),
        "survey": TesterStore.next_question(answers) is None,
        "done": session["status"] == "done",
        "connected": session["connected"],
        "missions": json.loads(session["missions"]),
        "fps": fps,
        "ping": session["ping_ms"],
        "photos": photos,
        "enter_text": enter_text,
        "help_count": session["help_count"],
    }


def help_step(session: dict | None) -> str:
    if session is None:
        return "始める前"
    return {"connecting": "つなぐところ", "playing": "遊んでいる途中", "survey": "アンケート"}.get(session["status"], "終わったあと")


def parse_performance(form: dict[str, list[str]], worlds: list[str]) -> tuple[dict[str, int], int | None]:
    """FPS (ワールドごと) と Ping。空の欄は入れなかったことにする。"""

    def number(raw: str, label: str, top: int) -> int | None:
        raw = raw.strip().replace(",", "")
        if not raw:
            return None
        try:
            value = round(float(raw))
        except ValueError as exc:
            raise WebTestError(f"「{label}」は数字で入れてください") from exc
        if not 0 <= value <= top:
            raise WebTestError(f"「{label}」の数字を確かめてください")
        return value

    fps = {}
    for i, name in enumerate(worlds):
        value = number((form.get(f"fps_{i}") or [""])[0], f"{name} の FPS", MAX_FPS)
        if value is not None:
            fps[name] = value
    ping = number((form.get("ping") or [""])[0], "Ping", MAX_PING_MS)
    return fps, ping


def asked_questions(own_pc: str | None) -> list[dict]:
    """アンケートの質問。VR ができる PC を持っている人には、PC を買うことの質問をしない。"""
    return TesterStore.asked_questions({"own_pc": own_pc} if own_pc else {})


def parse_survey(form: dict[str, list[str]]) -> dict:
    """アンケートの答えを確かめる。{質問のキー: 値 (いくつでもえらべる質問はリスト)}。"""
    own_pc = (form.get("own_pc") or [""])[0] or None
    answers: dict = {}
    for q in asked_questions(own_pc):
        allowed = {v for v, _ in q["options"]}
        values = [v for v in form.get(q["key"], []) if v]
        if not values:
            raise WebTestError(f"「{q['text']}」に答えてください")
        if any(v not in allowed for v in values) or (len(values) > 1 and not q.get("multi")):
            raise WebTestError(f"「{q['text']}」の答えを確かめてください")
        answers[q["key"]] = values if q.get("multi") else values[0]
    return answers


def missions_from(form: dict[str, list[str]]) -> dict[str, bool]:
    picked = set(form.get("mission", []))
    return {key: key in picked for key in MISSIONS}

