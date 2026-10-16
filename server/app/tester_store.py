"""テスター用 Discord ボットの記録 (申し込み・テストの時刻・アンケート) と集計。

Discord には依存しない。ボット (tester_bot.py) はここに書き込み、運営は集計と CSV を受け取る。
データは予約サイトとは別の SQLite ファイルに置く。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sqlite3
import statistics
import threading
from pathlib import Path

GROUPS = {
    "A": "A. Quest だけ",
    "B": "B. PCVR（重いと FPS 50〜60）",
    "C": "C. いい PC を持っている",
}

# アンケート。テスト計画書の「アンケート（終了直後）」と同じ順番・選択肢
QUESTIONS: list[dict] = [
    {"key": "ease", "text": "予約から VRChat に入るまで、分かりやすかったですか？",
     "options": [("1", "1 とても分かりにくい"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 とても分かりやすい")]},
    {"key": "quality", "text": "画質と遅れに満足しましたか？",
     "options": [("1", "1 不満"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 満足")]},
    {"key": "stutter", "text": "カクつき（映像が止まる・飛ぶ）はどのくらい気になりましたか？",
     "options": [("1", "1 とても気になった"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 まったく気にならなかった")]},
    {"key": "sick", "text": "VR酔いはしましたか？",
     "options": [("none", "まったくしない"), ("little", "少し"), ("some", "けっこう"), ("bad", "ひどい")]},
    {"key": "compare", "text": "いつもの環境（Quest 単体 or 自分の PC）と比べてどうでしたか？",
     "options": [("1", "かなり悪い"), ("2", "少し悪い"), ("3", "同じくらい"), ("4", "少し良い"), ("5", "かなり良い")]},
    {"key": "timer", "text": "手首のタイマーと、終わりのお知らせはどうでしたか？",
     "options": [("good", "見やすかった"), ("missed", "気づかなかった"), ("annoying", "じゃまだった")]},
    {"key": "design", "text": "予約サイトの見た目（デザイン）はどうでしたか？",
     "options": [("1", "1 悪い"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 良い")]},
    {"key": "name", "text": "「Geaspec（ギアスペック）」という名前はどうですか？",
     "options": [("1", "1 いまいち"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 とても良い")]},
    {"key": "trust", "text": "安心して使えそうだと思いましたか？",
     "options": [("1", "1 不安"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 安心")]},
    {"key": "line", "text": "家のインターネット回線は？",
     "options": [("fiber", "光回線"), ("home_router", "ホームルーター"), ("catv", "ケーブルテレビ"),
                 ("mobile", "スマホの回線・テザリング"), ("unknown", "わからない")]},
    {"key": "speed", "text": "回線の速さ（下り）は？（「スピードテスト」で調べると出ます）",
     "options": [("50", "〜50Mbps"), ("100", "50〜100Mbps"), ("300", "100〜300Mbps"), ("301", "300Mbps より上"),
                 ("unknown", "わからない")]},
    {"key": "wifi_band", "text": "Quest をつないだ Wi-Fi は？",
     "options": [("6", "6GHz（Wi-Fi 6E）"), ("5", "5GHz"), ("2.4", "2.4GHz"), ("unknown", "わからない")]},
    {"key": "router_distance", "text": "Quest と Wi-Fi ルーターの距離は？",
     "options": [("same", "同じ部屋"), ("next", "となりの部屋"), ("far", "離れた部屋・別の階"), ("unknown", "わからない")]},
    {"key": "quest", "text": "使った Quest は？",
     "options": [("q2", "Quest 2"), ("q3", "Quest 3"), ("q3s", "Quest 3S"), ("pro", "Quest Pro"), ("other", "その他")]},
    {"key": "own_pc", "text": "自分のパソコンは？",
     "options": [("none", "持っていない"), ("no_vr", "VR はできないパソコンならある"), ("vr", "VR ができるパソコンがある")]},
    # PC を買うことと比べてどうか (VR ができる PC を持っている人には聞かない)
    {"key": "pc_checked", "text": "VR ができるパソコン（ゲーミング PC）の値段を、最後に調べたのはいつですか？",
     "skip_if": {"own_pc": "vr"},
     "options": [("week", "1週間以内"), ("month", "1か月以内"), ("half", "半年以内"), ("older", "それより前"),
                 ("never", "調べたことがない")]},
    {"key": "pc_effect", "text": "Geaspec があったら、ゲーミング PC を買うことについてどう思いますか？",
     "skip_if": {"own_pc": "vr"},
     "options": [("no_need", "買わなくてよくなる"), ("later", "買うのを先に延ばす"), ("same", "変わらない"),
                 ("want_more", "試してみて、むしろ欲しくなった")]},
    {"key": "vrc_days", "text": "VRChat で遊ぶのは週に何日くらい？",
     "options": [("daily", "ほぼ毎日"), ("3_5", "週に3〜5日"), ("1_2", "週に1〜2日"), ("less", "それより少ない")]},
    {"key": "play_times", "text": "よく遊ぶ時間帯は？（いくつでも）", "multi": True,
     "options": [("wd_eve", "平日の夕方（〜19時）"), ("wd_night", "平日の夜（19〜24時）"), ("wd_late", "平日の深夜（24時〜）"),
                 ("we_day", "休日の昼"), ("we_night", "休日の夜"), ("we_late", "休日の深夜")]},
    {"key": "session_length", "text": "PC を借りるなら、1回にどのくらい借りたい？",
     "options": [("30", "30分"), ("60", "1時間"), ("120", "2時間"), ("180", "3時間以上")]},
    {"key": "min_fps", "text": "この FPS を下回ったら嫌だ、という線は？",
     "options": [("30", "30"), ("45", "45"), ("60", "60"), ("72", "72"), ("90", "90"), ("unknown", "わからない")]},
    {"key": "again", "text": "また使いたいですか？",
     "options": [("yes", "ぜひ"), ("maybe", "たぶん"), ("rather_not", "あまり"), ("no", "使わない")]},
    {"key": "nps", "text": "友達にどのくらいすすめたいですか？（0 すすめない 〜 10 ぜったいすすめる）",
     "options": [(str(n), str(n)) for n in range(11)]},
    {"key": "price", "text": "1時間いくらなら払いますか？",
     "options": [("0", "無料なら使う"), ("100", "〜100円"), ("200", "〜200円"), ("400", "〜400円"), ("600", "〜600円"), ("800", "〜800円"),
                 ("1000", "〜1,000円"), ("1001", "それ以上")]},
    {"key": "when", "text": "どんなときに使いたいですか？（いくつでも）", "multi": True,
     "options": [("event", "イベント"), ("heavy", "重いワールド"), ("photo", "写真撮影"),
                 ("friends", "友達と遊ぶとき"), ("other", "その他")]},
]
QUESTION_KEYS = [q["key"] for q in QUESTIONS]

# 3 日後にスレッドで聞く質問。体験した直後の気分が落ち着いてからの本音を取る
FOLLOWUP_DAYS = 3
FOLLOWUPS: list[dict] = [
    {"key": "again_3d", "text": "少し時間がたって、また使いたいと思いますか？",
     "options": [("yes", "ぜひ"), ("maybe", "たぶん"), ("rather_not", "あまり"), ("no", "使わない")]},
    {"key": "told", "text": "このサービスのことを、友達に話しましたか？",
     "options": [("yes", "話した"), ("will", "話そうと思っている"), ("no", "話していない")]},
    {"key": "photos_3d", "text": "テスト中に VRChat のカメラで撮った自撮りは、予約サイトの予約ページから受け取れましたか？",
     "options": [("got", "受け取れた"), ("failed", "受け取れなかった"), ("none", "撮っていない")]},
    {"key": "price_3d", "text": "あらためて、1時間いくらなら払いますか？",
     "options": [("0", "無料なら使う"), ("100", "〜100円"), ("200", "〜200円"), ("400", "〜400円"), ("600", "〜600円"), ("800", "〜800円"),
                 ("1000", "〜1,000円"), ("1001", "それ以上")]},
]
FOLLOWUP_KEYS = [q["key"] for q in FOLLOWUPS]

# PC を貸した人へのアンケート (はじめてデータ消去を確かめたあとに 1 回だけ)。正式リリースの値段決めに使う
LENDER_QUESTIONS: list[dict] = [
    {"key": "worry", "text": "貸す前、どのくらい不安でしたか？",
     "options": [("high", "とても不安"), ("some", "少し不安"), ("little", "あまり不安ではない"), ("none", "まったく不安ではない")]},
    {"key": "worry_what", "text": "何が心配でしたか？（いくつでも）", "multi": True,
     "options": [("account", "アカウントやパスワード"), ("files", "写真やファイルを見られる"), ("break", "PC が壊れる・おかしくなる"),
                 ("power", "電気代"), ("stranger", "知らない人が使うこと"), ("time", "使いたいときに使えない"),
                 ("nothing", "特になし")]},
    {"key": "setup", "text": "準備（アプリを入れる〜貸せる時間を決める）は分かりやすかったですか？",
     "options": [("easy", "簡単"), ("ok", "ふつう"), ("hard", "難しい"), ("very_hard", "とても難しい")]},
    {"key": "lend_again", "text": "また貸したいと思いますか？",
     "options": [("yes", "ぜひ"), ("maybe", "たぶん"), ("rather_not", "あまり"), ("no", "貸さない")]},
    {"key": "lend_price", "text": "1時間いくらもらえたら貸しますか？",
     "options": [("0", "無料でも貸す"), ("50", "〜50円"), ("100", "〜100円"), ("200", "〜200円"), ("300", "〜300円"),
                 ("500", "〜500円"), ("501", "それ以上")]},
    {"key": "lend_hours", "text": "1週間に何時間くらい貸せそうですか？",
     "options": [("2", "〜2時間"), ("5", "〜5時間"), ("10", "〜10時間"), ("20", "〜20時間"), ("21", "それ以上")]},
]
LENDER_KEYS = [q["key"] for q in LENDER_QUESTIONS]

# 遊んでいる途中のお願い
MISSIONS = {"heavy": "重いワールドに行った", "crowd": "人の多いインスタンスに入った", "photo": "VRChat のカメラで自撮りした"}

OPEN_STATUSES = ("connecting", "playing", "survey")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS testers (
    user_id INTEGER PRIMARY KEY,
    discord_name TEXT NOT NULL DEFAULT '',
    vrchat TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '',
    gear TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    schedule TEXT NOT NULL DEFAULT '',
    grp TEXT NOT NULL DEFAULT '',
    has_vd INTEGER,
    thread_id INTEGER,
    status TEXT NOT NULL DEFAULT 'applied',
    applied_at TEXT NOT NULL,
    ready_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entered_at TEXT,
    ended_at TEXT,
    connected INTEGER,
    help_count INTEGER NOT NULL DEFAULT 0,
    help_steps TEXT NOT NULL DEFAULT '[]',
    missions TEXT NOT NULL DEFAULT '{}',
    fps INTEGER,
    fps_worlds TEXT NOT NULL DEFAULT '{}',
    ping_ms INTEGER,
    latency_ms INTEGER,
    screenshots TEXT NOT NULL DEFAULT '[]',
    answers TEXT NOT NULL DEFAULT '{}',
    stuck TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    followup_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS lenders (
    user_id INTEGER PRIMARY KEY,
    answers TEXT NOT NULL DEFAULT '{}',
    comment TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_notices (
    booking_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (booking_id, kind)
);
"""

# あとから増やした列。前の版で作ったファイルにも足す
ADDED_COLUMNS = {
    "testers": {"booked_at": "TEXT", "booking_help": "INTEGER NOT NULL DEFAULT 0", "lender_app_at": "TEXT"},
    "sessions": {"fps_worlds": "TEXT NOT NULL DEFAULT '{}'", "ping_ms": "INTEGER", "completed_at": "TEXT", "followup_sent_at": "TEXT",
                 # どの PC で測ったか (Virtual Desktop の一覧に出る名前)。PC ごとの実測 FPS を出すのに使う
                 "pc": "TEXT NOT NULL DEFAULT ''"},
}


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


def option_label(key: str, value: str) -> str:
    for q in QUESTIONS + FOLLOWUPS + LENDER_QUESTIONS:
        if q["key"] == key:
            return dict(q["options"]).get(value, value)
    return value


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds >= 86400:
        d, rest = divmod(seconds, 86400)
        return f"{d}日{rest // 3600}時間"
    if seconds >= 3600:
        h, rest = divmod(seconds, 3600)
        return f"{h}時間{rest // 60}分"
    m, s = divmod(seconds, 60)
    return f"{m}分{s:02d}秒" if m else f"{s}秒"


class TesterStore:
    __test__ = False  # pytest にテストのクラスと間違えられないように

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, ddl in cols.items():
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # 設定 (運営チャンネル・運営ロールなど)
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # テスター
    def apply(self, user_id: int, discord_name: str, *, vrchat: str, meta: str, gear: str, region: str,
              schedule: str, now: dt.datetime) -> dict:
        """申し込み。2回目は内容を書き換える (スレッドと準備の状態はそのまま)。"""
        self._run(
            "INSERT INTO testers (user_id, discord_name, vrchat, meta, gear, region, schedule, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "discord_name = excluded.discord_name, vrchat = excluded.vrchat, meta = excluded.meta, "
            "gear = excluded.gear, region = excluded.region, schedule = excluded.schedule",
            (user_id, discord_name, vrchat, meta, gear, region, schedule, _iso(now)),
        )
        return self.tester(user_id)

    def set_group(self, user_id: int, grp: str) -> None:
        if grp not in GROUPS:
            raise ValueError(grp)
        self._run("UPDATE testers SET grp = ? WHERE user_id = ?", (grp, user_id))

    def set_has_vd(self, user_id: int, has_vd: bool) -> None:
        self._run("UPDATE testers SET has_vd = ? WHERE user_id = ?", (int(has_vd), user_id))

    def set_thread(self, user_id: int, thread_id: int) -> None:
        self._run("UPDATE testers SET thread_id = ? WHERE user_id = ?", (thread_id, user_id))

    def mark_ready(self, user_id: int, now: dt.datetime) -> bool:
        """準備の案内を送る状態にする。はじめてのときだけ True。"""
        cur = self._run("UPDATE testers SET status = 'ready', ready_at = ? WHERE user_id = ? AND status = 'applied'",
                        (_iso(now), user_id))
        return cur.rowcount == 1

    def waiting_testers(self) -> list[dict]:
        """申し込んだのに、まだ予約の案内を受け取っていない人 (前の版で運営のボタンや2つの答えを待っていた人)。"""
        return self._all("SELECT * FROM testers WHERE status = 'applied' AND thread_id IS NOT NULL")

    def mark_booked(self, user_id: int, now: dt.datetime) -> bool:
        """予約できた。はじめてのときだけ True。"""
        cur = self._run("UPDATE testers SET booked_at = ? WHERE user_id = ? AND booked_at IS NULL", (_iso(now), user_id))
        return cur.rowcount == 1

    def add_booking_help(self, user_id: int) -> int:
        self._run("UPDATE testers SET booking_help = booking_help + 1 WHERE user_id = ?", (user_id,))
        return self.tester(user_id)["booking_help"]

    def mark_lender_app(self, user_id: int, now: dt.datetime) -> bool:
        cur = self._run("UPDATE testers SET lender_app_at = ? WHERE user_id = ? AND lender_app_at IS NULL",
                        (_iso(now), user_id))
        return cur.rowcount == 1

    def tester(self, user_id: int) -> dict | None:
        return self._one("SELECT * FROM testers WHERE user_id = ?", (user_id,))

    def tester_by_thread(self, thread_id: int) -> dict | None:
        return self._one("SELECT * FROM testers WHERE thread_id = ?", (thread_id,))

    def tester_by_meta(self, meta: str) -> dict | None:
        """予約サイトの Meta ユーザー名から、申し込んだテスターを探す (大文字・小文字は区別しない)。"""
        meta = meta.strip()
        if not meta:
            return None
        return self._one("SELECT * FROM testers WHERE lower(trim(meta)) = lower(?) ORDER BY applied_at DESC LIMIT 1",
                         (meta,))

    # 予約のお知らせ (同じ予約に同じお知らせを 2 回送らない)
    def notice_sent(self, booking_id: int, kind: str) -> bool:
        return self._one("SELECT 1 FROM booking_notices WHERE booking_id = ? AND kind = ?", (booking_id, kind)) is not None

    def mark_notice(self, booking_id: int, kind: str, now: dt.datetime) -> None:
        self._run("INSERT OR IGNORE INTO booking_notices (booking_id, kind, sent_at) VALUES (?, ?, ?)",
                  (booking_id, kind, _iso(now)))

    # テスト 1 回分
    def open_session(self, thread_id: int) -> dict | None:
        return self._one(
            f"SELECT * FROM sessions WHERE thread_id = ? AND status IN ({','.join('?' * len(OPEN_STATUSES))}) "
            "ORDER BY id DESC LIMIT 1", (thread_id, *OPEN_STATUSES))

    def latest_session(self, thread_id: int) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (thread_id,))

    def session(self, session_id: int) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def start_session(self, user_id: int, thread_id: int, now: dt.datetime, pc: str = "") -> tuple[dict, bool]:
        """テストを始める。途中のテストがあればそれを返す (2つ目は作らない)。pc はつなぐ PC の名前。"""
        current = self.open_session(thread_id)
        if current:
            return current, False
        cur = self._run("INSERT INTO sessions (user_id, thread_id, status, opened_at, pc) VALUES (?, ?, 'connecting', ?, ?)",
                        (user_id, thread_id, _iso(now), pc.strip()))
        return self.session(cur.lastrowid), True

    def mark_entered(self, session_id: int, now: dt.datetime) -> float:
        """ワールドに入れた。VD を開いてから何秒かかったかを返す。"""
        s = self.session(session_id)
        if s["entered_at"] is None:
            self._run("UPDATE sessions SET entered_at = ?, connected = 1, status = 'playing' WHERE id = ?",
                      (_iso(now), session_id))
            s = self.session(session_id)
        return (_parse(s["entered_at"]) - _parse(s["opened_at"])).total_seconds()

    def add_help(self, session_id: int, step: str, now: dt.datetime) -> int:
        s = self.session(session_id)
        steps = json.loads(s["help_steps"])
        steps.append({"step": step, "at": _iso(now)})
        self._run("UPDATE sessions SET help_count = help_count + 1, help_steps = ? WHERE id = ?",
                  (json.dumps(steps, ensure_ascii=False), session_id))
        return s["help_count"] + 1

    def toggle_mission(self, session_id: int, key: str) -> dict:
        if key not in MISSIONS:
            raise ValueError(key)
        missions = json.loads(self.session(session_id)["missions"])
        missions[key] = not missions.get(key, False)
        self._run("UPDATE sessions SET missions = ? WHERE id = ?", (json.dumps(missions), session_id))
        return missions

    def set_performance(self, session_id: int, *, fps_by_world: dict[str, int | None], ping_ms: int | None = None,
                        latency_ms: int | None = None) -> None:
        """fps_by_world: {ワールド名: FPS}、ping_ms: VRChat の Ping、latency_ms: VD の遅れ (任意)。"""
        fps = {k: v for k, v in fps_by_world.items() if v is not None}
        self._run("UPDATE sessions SET fps_worlds = ?, ping_ms = ?, latency_ms = ? WHERE id = ?",
                  (json.dumps(fps, ensure_ascii=False), ping_ms, latency_ms, session_id))

    @staticmethod
    def world_fps(row: dict) -> dict[str, int]:
        fps = json.loads(row.get("fps_worlds") or "{}")
        if not fps and row.get("fps") is not None:  # 0.7.5 までの記録 (重いワールド 1 つだけ)
            fps = {"重いワールド": row["fps"]}
        return fps

    def world_names(self, rows: list[dict] | None = None) -> list[str]:
        names: list[str] = []
        for r in rows if rows is not None else self.rows():
            for name in self.world_fps(r):
                if name not in names:
                    names.append(name)
        return names

    def add_screenshot(self, session_id: int, path: str) -> int:
        shots = json.loads(self.session(session_id)["screenshots"])
        shots.append(path)
        self._run("UPDATE sessions SET screenshots = ? WHERE id = ?", (json.dumps(shots, ensure_ascii=False), session_id))
        return len(shots)

    def finish_play(self, session_id: int, now: dt.datetime, *, connected: bool) -> None:
        """遊び終わった (connected=True) / つながらずにやめた (connected=False)。アンケートへ進む。"""
        self._run("UPDATE sessions SET ended_at = COALESCE(ended_at, ?), connected = ?, status = 'survey' WHERE id = ?",
                  (_iso(now), int(connected), session_id))

    def answer(self, session_id: int, key: str, value: str | list[str]) -> str | None:
        """アンケートに答える。次の質問のキー (最後なら None) を返す。"""
        q = next((q for q in QUESTIONS if q["key"] == key), None)
        if q is None:
            raise ValueError(key)
        allowed = {v for v, _ in q["options"]}
        values = value if isinstance(value, list) else [value]
        if not values or any(v not in allowed for v in values) or (len(values) > 1 and not q.get("multi")):
            raise ValueError(value)
        answers = json.loads(self.session(session_id)["answers"])
        answers[key] = values if q.get("multi") else values[0]
        self._run("UPDATE sessions SET answers = ? WHERE id = ?", (json.dumps(answers, ensure_ascii=False), session_id))
        return self.next_question(answers)

    @staticmethod
    def asked_questions(answers: dict) -> list[dict]:
        """この人に聞く質問 (前の答えによって聞かないものを除く)。"""
        return [q for q in QUESTIONS
                if not any(answers.get(k) == v for k, v in q.get("skip_if", {}).items())]

    @staticmethod
    def next_question(answers: dict) -> str | None:
        return next((q["key"] for q in TesterStore.asked_questions(answers) if q["key"] not in answers), None)

    def save_comment(self, session_id: int, *, stuck: str = "", comment: str = "") -> None:
        """ひとことだけ記録する (テストはまだ終えない。最後に「送信」で終える)。"""
        self._run("UPDATE sessions SET stuck = ?, comment = ? WHERE id = ?", (stuck.strip(), comment.strip(), session_id))

    def complete(self, session_id: int, now: dt.datetime, *, stuck: str = "", comment: str = "") -> None:
        self._run("UPDATE sessions SET stuck = ?, comment = ?, status = 'done', completed_at = ? WHERE id = ?",
                  (stuck.strip(), comment.strip(), _iso(now), session_id))

    def followups_due(self, now: dt.datetime) -> list[dict]:
        """アンケートまで終わって 3 日たち、まだ 3 日後の質問を送っていないテスト。テスターごとに最新の 1 回だけ。"""
        limit = _iso(now - dt.timedelta(days=FOLLOWUP_DAYS))
        rows = self._all("SELECT * FROM sessions WHERE status = 'done' AND followup_sent_at IS NULL "
                         "AND completed_at IS NOT NULL AND completed_at <= ? ORDER BY id", (limit,))
        latest = {}
        for r in rows:
            latest[r["user_id"]] = r
        return list(latest.values())

    def mark_followup_sent(self, session_id: int, now: dt.datetime) -> None:
        # 同じテスターの前の回にも、もう送らない
        s = self.session(session_id)
        self._run("UPDATE sessions SET followup_sent_at = ? WHERE user_id = ? AND id <= ? AND followup_sent_at IS NULL",
                  (_iso(now), s["user_id"], session_id))

    def answer_followup(self, session_id: int, key: str, value: str) -> str | None:
        """3 日後の質問に答える。次の質問のキー (最後なら None) を返す。"""
        q = next((q for q in FOLLOWUPS if q["key"] == key), None)
        if q is None or value not in {v for v, _ in q["options"]}:
            raise ValueError(value)
        answers = json.loads(self.session(session_id)["answers"])
        answers[key] = value
        self._run("UPDATE sessions SET answers = ? WHERE id = ?", (json.dumps(answers, ensure_ascii=False), session_id))
        return next((k for k in FOLLOWUP_KEYS if k not in answers), None)

    # PC を貸した人へのアンケート
    def start_lender_survey(self, user_id: int, now: dt.datetime) -> bool:
        """はじめてなら始めて True。もう送ってあれば False。"""
        cur = self._run("INSERT OR IGNORE INTO lenders (user_id, sent_at) VALUES (?, ?)", (user_id, _iso(now)))
        return cur.rowcount == 1

    def lender_survey(self, user_id: int) -> dict | None:
        return self._one("SELECT * FROM lenders WHERE user_id = ?", (user_id,))

    def answer_lender(self, user_id: int, key: str, value: str | list[str]) -> str | None:
        """貸した人のアンケートに答える。次の質問のキー (最後なら None) を返す。"""
        row = self.lender_survey(user_id)
        q = next((q for q in LENDER_QUESTIONS if q["key"] == key), None)
        if row is None or q is None:
            raise ValueError(key)
        values = value if isinstance(value, list) else [value]
        if not values or any(v not in {v for v, _ in q["options"]} for v in values) or (len(values) > 1 and not q.get("multi")):
            raise ValueError(value)
        answers = json.loads(row["answers"])
        answers[key] = values if q.get("multi") else values[0]
        self._run("UPDATE lenders SET answers = ? WHERE user_id = ?", (json.dumps(answers, ensure_ascii=False), user_id))
        return self.next_lender_question(answers)

    @staticmethod
    def next_lender_question(answers: dict) -> str | None:
        return next((k for k in LENDER_KEYS if k not in answers), None)

    def finish_lender(self, user_id: int, now: dt.datetime, comment: str = "") -> None:
        self._run("UPDATE lenders SET comment = ?, done_at = ? WHERE user_id = ?", (comment.strip(), _iso(now), user_id))

    # スレッドに書かれた意見・気づいたこと
    def add_note(self, user_id: int, thread_id: int, text: str, now: dt.datetime) -> None:
        self._run("INSERT INTO notes (user_id, thread_id, text, at) VALUES (?, ?, ?, ?)",
                  (user_id, thread_id, text.strip()[:2000], _iso(now)))

    def notes(self) -> list[dict]:
        return self._all("SELECT n.*, t.grp FROM notes n LEFT JOIN testers t ON t.user_id = n.user_id ORDER BY n.id")

    def erase_personal(self) -> int:
        """テストが終わったら、名前・Meta ユーザー名・連絡先を消す (アンケートの答えと時間は残す)。"""
        cur = self._run("UPDATE testers SET discord_name = '', vrchat = '', meta = '', schedule = ''")
        return cur.rowcount

    # 集計
    def rows(self) -> list[dict]:
        return self._all(
            "SELECT s.*, t.grp, t.has_vd, t.region, t.gear, t.vrchat, t.ready_at, t.booked_at, t.booking_help "
            "FROM sessions s "
            "LEFT JOIN testers t ON t.user_id = s.user_id ORDER BY s.id")

    def summary(self) -> dict:
        testers = self._all("SELECT * FROM testers")
        rows = self.rows()
        started = [r for r in rows]
        connected = [r for r in rows if r["connected"] == 1]
        enter_secs = [(_parse(r["entered_at"]) - _parse(r["opened_at"])).total_seconds()
                      for r in rows if r["entered_at"]]
        answered = [dict(r, a=json.loads(r["answers"])) for r in rows]

        def values(key: str, rs=answered) -> list[str]:
            return [r["a"][key] for r in rs if key in r["a"]]

        def avg(key: str, rs=answered) -> float | None:
            v = [int(x) for x in values(key, rs)]
            return round(statistics.mean(v), 2) if v else None

        def share(key: str, good: set[str], rs=answered) -> float | None:
            v = values(key, rs)
            return round(sum(1 for x in v if x in good) / len(v), 3) if v else None

        price = sorted(int(x) for x in values("price"))
        ready = [t for t in testers if t["ready_at"]]
        booked = [t for t in ready if t["booked_at"]]
        book_secs = [(_parse(t["booked_at"]) - _parse(t["ready_at"])).total_seconds() for t in booked]
        ping = [r["ping_ms"] for r in rows if r["ping_ms"] is not None]
        groups = {}
        for g in ("A", "B"):
            rs = [r for r in answered if r["grp"] == g]
            groups[g] = {"sessions": len(rs), "quality": avg("quality", rs),
                         "again": share("again", {"yes", "maybe"}, rs)}
        with_vd = [t for t in testers if t["has_vd"] is not None]
        fps_by_world = {name: statistics.median([self.world_fps(r)[name] for r in rows if name in self.world_fps(r)])
                        for name in self.world_names(rows)}
        lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
        nps = [int(x) for x in values("nps")]
        return {
            "applied": len(testers),
            "ready": sum(1 for t in testers if t["status"] == "ready"),
            "sessions": len(started),
            "done": sum(1 for r in rows if r["status"] == "done"),
            "connected_rate": round(len(connected) / len(started), 3) if started else None,
            "enter_median_sec": statistics.median(enter_secs) if enter_secs else None,
            "help_ok_rate": round(sum(1 for r in started if r["help_count"] <= 1) / len(started), 3) if started else None,
            "quality_avg": avg("quality"),
            "ease_avg": avg("ease"),
            "sick_count": sum(1 for x in values("sick") if x in ("some", "bad")),
            "again_rate": share("again", {"yes", "maybe"}),
            "nps_top_rate": round(sum(1 for n in nps if n >= 9) / len(nps), 3) if nps else None,
            "price_median": statistics.median_low(price) if price else None,
            "has_vd_rate": round(sum(t["has_vd"] for t in with_vd) / len(with_vd), 3) if with_vd else None,
            "booked_rate": round(len(booked) / len(ready), 3) if ready else None,
            "book_median_sec": statistics.median(book_secs) if book_secs else None,
            "booking_help": sum(t["booking_help"] or 0 for t in testers),
            "stutter_avg": avg("stutter"),
            "again_3d_rate": share("again_3d", {"yes", "maybe"}),
            "told_rate": share("told", {"yes"}),
            "ping_median": statistics.median(ping) if ping else None,
            "lender_app": sum(1 for t in testers if t["lender_app_at"]),
            "fps_by_world": fps_by_world,
            "fps_by_pc": self.fps_by_pc(rows),
            "latency_median": statistics.median(lat) if lat else None,
            "screenshots": sum(len(json.loads(r["screenshots"])) for r in rows),
            "groups": groups,
            **self.lender_summary(),
            "pc_checked_recent_rate": share("pc_checked", {"week", "month"}),
            "pc_effect": {v: share("pc_effect", {v}) for v, _ in next(q for q in QUESTIONS if q["key"] == "pc_effect")["options"]},
        }

    def fps_by_pc(self, rows: list[dict] | None = None) -> dict[str, dict]:
        """PC ごとの実測。{PC の名前: {"tests": 測った回数, "fps": {ワールド: 真ん中の値}, "ping": 真ん中の値}}。

        将来、空き枠の一覧でスペックの代わりに見せる (「テスターの実測: 集会場 72fps」)。
        """
        rows = self.rows() if rows is None else rows
        out: dict[str, dict] = {}
        for pc in sorted({r["pc"] for r in rows if r.get("pc")}):
            rs = [r for r in rows if r.get("pc") == pc and (self.world_fps(r) or r["ping_ms"] is not None)]
            if not rs:
                continue
            fps = {name: round(statistics.median([self.world_fps(r)[name] for r in rs if name in self.world_fps(r)]))
                   for name in self.world_names(rs)}
            ping = [r["ping_ms"] for r in rs if r["ping_ms"] is not None]
            out[pc] = {"tests": len(rs), "fps": fps, "ping": round(statistics.median(ping)) if ping else None}
        return out

    def lender_summary(self) -> dict:
        rows = [json.loads(r["answers"]) for r in self._all("SELECT answers FROM lenders")]
        price = sorted(int(a["lend_price"]) for a in rows if "lend_price" in a)
        again = [a["lend_again"] for a in rows if "lend_again" in a]
        worry = [a["worry"] for a in rows if "worry" in a]
        return {
            "lenders_answered": sum(1 for a in rows if a),
            "lend_price_median": price[(len(price) - 1) // 2] if price else None,
            "lend_again_rate": round(sum(1 for x in again if x in ("yes", "maybe")) / len(again), 3) if again else None,
            "lend_worry_rate": round(sum(1 for x in worry if x in ("high", "some")) / len(worry), 3) if worry else None,
            "notes": len(self._all("SELECT id FROM notes")),
        }

    def lenders_csv(self) -> str:
        """1 行 = PC を貸した人 1 人。"""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["番号", "答えた日", *[q["text"] for q in LENDER_QUESTIONS], "ひとこと"])
        for n, r in enumerate(self._all("SELECT * FROM lenders ORDER BY sent_at"), 1):
            a = json.loads(r["answers"])
            cells = []
            for q in LENDER_QUESTIONS:
                v = a.get(q["key"])
                cells.append("・".join(option_label(q["key"], x) for x in v) if isinstance(v, list)
                             else option_label(q["key"], v) if v is not None else "")
            w.writerow([n, r["done_at"] or "", *cells, r["comment"]])
        return out.getvalue()

    def notes_csv(self) -> str:
        """スレッドに書かれた意見。名前は入れず、借りる人か貸す人かだけ。"""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["日時", "借りる人／貸す人", "書いたこと"])
        for r in self.notes():
            w.writerow([r["at"], "貸す人" if r["grp"] == "C" else "借りる人", r["text"]])
        return out.getvalue()

    def to_csv(self, feedback_by_session: dict[int, dict] | None = None) -> str:
        """1 行 = テスト 1 回。評価フォームの答え (サイトの /feedback) があれば、同じ行の右側につなげる。

        Excel で開けるよう、呼び出し側で BOM 付き UTF-8 にする。
        """
        from . import feedback as fb

        def num(v):
            return "" if v is None else v

        fb_cols = fb.columns() if feedback_by_session is not None else []
        rows = self.rows()
        worlds = self.world_names(rows)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["回", "グループ", "VDを持っていた", "地方", "機材", "準備できてから予約まで(分)", "予約で迷った回数",
                    "開始", "PC", "ワールドに入るまで(秒)", "つながった", "困った回数", "困った場面", *MISSIONS.values(),
                    *[f"FPS: {w}" for w in worlds], "Ping(ms)", "VDの遅れ(ms)", "スクショ枚数",
                    *[q["text"] for q in QUESTIONS], "迷ったところ", "ひとこと",
                    *[f"3日後: {q['text']}" for q in FOLLOWUPS], *[f"フォーム: {label}" for _, label in fb_cols]])
        for r in rows:
            a = json.loads(r["answers"])
            m = json.loads(r["missions"])
            fps = self.world_fps(r)
            enter = ((_parse(r["entered_at"]) - _parse(r["opened_at"])).total_seconds() if r["entered_at"] else None)
            booked = (round((_parse(r["booked_at"]) - _parse(r["ready_at"])).total_seconds() / 60)
                      if r["booked_at"] and r["ready_at"] else None)
            answers = []
            for q in QUESTIONS + FOLLOWUPS:
                v = a.get(q["key"])
                if isinstance(v, list):
                    answers.append("・".join(option_label(q["key"], x) for x in v))
                else:
                    answers.append(option_label(q["key"], v) if v is not None else "")
            form = (feedback_by_session or {}).get(r["id"], {})
            w.writerow([
                r["id"], r["grp"] or "", {1: "はい", 0: "いいえ"}.get(r["has_vd"], ""), r["region"] or "", r["gear"] or "",
                num(booked), r["booking_help"] or 0,
                r["opened_at"], r.get("pc") or "", num(int(enter) if enter is not None else None),
                {1: "はい", 0: "いいえ"}.get(r["connected"], ""),
                r["help_count"], "・".join(h["step"] for h in json.loads(r["help_steps"])),
                *["はい" if m.get(k) else "" for k in MISSIONS], *[num(fps.get(w)) for w in worlds], num(r["ping_ms"]),
                num(r["latency_ms"]), len(json.loads(r["screenshots"])),
                *answers[:len(QUESTIONS)], r["stuck"], r["comment"], *answers[len(QUESTIONS):],
                *[fb.cell(form, k) for k, _ in fb_cols],
            ])
        return out.getvalue()


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


def summary_lines(s: dict) -> list[str]:
    """テスト計画書の「測るもの」と同じ目標で、達成していれば ✅、まだなら ⚠️ を付ける。"""

    def mark(ok: bool | None) -> str:
        return "・" if ok is None else ("✅" if ok else "⚠️")

    enter = s["enter_median_sec"]
    price = s["price_median"]
    lines = [
        f"申し込み {s['applied']}人 ・ 準備済み {s['ready']}人 ・ テスト {s['sessions']}回（アンケートまで完了 {s['done']}回）",
        "",
        f"{mark(None if s['booked_rate'] is None else s['booked_rate'] >= 0.8)} 予約まで進めた割合: {_pct(s['booked_rate'])}（目標 80%以上）"
        f" ／ 予約までの時間（真ん中の値）: {'—' if s['book_median_sec'] is None else format_duration(s['book_median_sec'])}"
        f" ／ 予約で迷った: {s['booking_help']}回",
        f"{mark(None if s['connected_rate'] is None else s['connected_rate'] >= 0.9)} つながった割合: {_pct(s['connected_rate'])}（目標 90%以上）",
        f"{mark(None if enter is None else enter <= 300)} ワールドに入るまで（真ん中の値）: "
        f"{'—' if enter is None else format_duration(enter)}（目標 5分以内）",
        f"{mark(None if s['help_ok_rate'] is None else s['help_ok_rate'] >= 0.8)} 「困った」が1回以下だった割合: {_pct(s['help_ok_rate'])}",
        f"{mark(None if s['quality_avg'] is None else s['quality_avg'] >= 4)} 画質・遅れの満足度: "
        f"{'—' if s['quality_avg'] is None else s['quality_avg']}（目標 平均4以上）",
        f"{mark(None if s['done'] == 0 else s['sick_count'] <= 1)} 「けっこう／ひどく」酔った人: {s['sick_count']}人（目標 1人以下）",
        f"{mark(None if s['again_rate'] is None else s['again_rate'] >= 0.7)} また使いたい: {_pct(s['again_rate'])}（目標 70%以上）",
        f"{mark(None if s['nps_top_rate'] is None else s['nps_top_rate'] >= 0.5)} すすめたい度 9〜10: {_pct(s['nps_top_rate'])}（目標 半分以上）",
        f"・ 払ってもいい金額（真ん中の値）: {'—' if price is None else option_label('price', str(price))}",
        f"・ 分かりやすさ: {'—' if s['ease_avg'] is None else s['ease_avg']} ／ VD を持っていた人: {_pct(s['has_vd_rate'])}",
        "・ FPS（真ん中の値）: " + ("／".join(f"{w} {v}" for w, v in s["fps_by_world"].items()) or "—") + " ／ "
        f"Ping {'—' if s['ping_median'] is None else str(s['ping_median']) + 'ms'} ／ "
        f"VD の遅れ {'—' if s['latency_median'] is None else str(s['latency_median']) + 'ms'} ／ スクショ {s['screenshots']}枚",
        *[f"・ PC「{pc}」の実測（{d['tests']}回）: "
          + ("／".join(f"{w} {v}fps" for w, v in d["fps"].items()) or "FPS —")
          + f" ／ Ping {'—' if d['ping'] is None else str(d['ping']) + 'ms'}" for pc, d in s["fps_by_pc"].items()],
        f"・ カクつきの気にならなさ: {'—' if s['stutter_avg'] is None else s['stutter_avg']}（5 がまったく気にならない）",
        f"・ 3日後もまた使いたい: {_pct(s['again_3d_rate'])} ／ 友達に話した: {_pct(s['told_rate'])}",
        f"・ 貸し手ベータのアプリを入れた人: {s['lender_app']}人",
        f"・ ゲーミング PC の値段を1か月以内に調べた: {_pct(s['pc_checked_recent_rate'])} ／ Geaspec があれば PC を: "
        + "・".join(f"{option_label('pc_effect', k)} {_pct(v)}" for k, v in s["pc_effect"].items()),
        f"・ 貸した人のアンケート: {s['lenders_answered']}人 ／ もらえたら貸す金額（真ん中の値）: "
        f"{'—' if s['lend_price_median'] is None else option_label('lend_price', str(s['lend_price_median']))}"
        f" ／ また貸したい: {_pct(s['lend_again_rate'])} ／ 貸す前に不安だった: {_pct(s['lend_worry_rate'])}",
        f"・ スレッドに書かれた意見: {s['notes']}件",
    ]
    for g in ("A", "B"):
        d = s["groups"][g]
        lines.append(f"・ {GROUPS[g]}: {d['sessions']}回 ・ 満足度 {'—' if d['quality'] is None else d['quality']} ・ "
                     f"また使いたい {_pct(d['again'])}")
    return lines
