"""Geaspec Agent の画面 (GUI)。

貸し手 PC で起動しておくと、予約サイトに定期的に問い合わせ、予約の時間になったら
自動でセッションを行う。処理そのものは host_agent.py にあり、ここは画面と操作だけ。

    pythonw geaspec_gui.py
"""

from __future__ import annotations

import datetime as dt
import logging
import logging.handlers
import os
import queue
import sys
import threading
import uuid
from pathlib import Path

import host_agent as h

POLL_INTERVAL_DEFAULT = 15.0

STATE_LABELS = {
    "stopped": ("停止中", "warn"),
    "idle": ("待機中", "ok"),
    "unreachable": ("予約サイトに接続できません", "bad"),
    "session": ("利用中", "busy"),
    "cleaning": ("後片付け中", "warn"),
    "auth_error": ("ログインし直してください", "bad"),
}


def until_text(seconds: float) -> str:
    """残り時間を「1時間23分」のような短い文章にする。"""
    minutes = int(seconds + 59) // 60  # 切り上げ: 「あと 0 分」と出さない
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)}日{minutes % (24 * 60) // 60}時間"
    if minutes >= 60:
        return f"{minutes // 60}時間{minutes % 60}分"
    return f"{max(minutes, 1)}分"


WEEKDAYS = "月火水木金土日"


def day_label(value: dt.datetime) -> str:
    return f"{value:%m/%d}（{WEEKDAYS[value.weekday()]}）"


def validate_settings(server: str, token: str) -> str | None:
    if not server.startswith(("https://", "http://")):
        return "予約サイトの URL は http:// か https:// で始めてください"
    if not (token.startswith("pcvr_") and len(token) > 10):
        return "合言葉は pcvr_ で始まる文字です。運営から届いたものをそのまま貼り付けてください"
    return None


class AgentController:
    """画面に依存しない部分 (設定の読み書き、待機の開始・停止、状態の文章)。"""

    def __init__(self, config_path: Path | None = None, home: Path | None = None) -> None:
        self.config_path = config_path or h.default_config_path()
        self.home = home or h.agent_home()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.cleanup_thread: threading.Thread | None = None
        self.reload()

    # --- 設定 -----------------------------------------------------------

    def reload(self) -> None:
        self.config = h.load_config_file(self.config_path)
        args = h.parse_args(["--config", str(self.config_path), "--cleanup-only"])
        self.cfg = h.config_from_args(args)
        if not self.cfg.wipe_targets:
            self.cfg.wipe_targets = h.default_wipe_targets(self.cfg.keep_screenshots)
        self.poll_interval = float(self.config.get("poll_interval", POLL_INTERVAL_DEFAULT))

    @property
    def server(self) -> str:
        return self.config.get("server") or h.builtin_server()

    @property
    def token(self) -> str:
        return self.config.get("token", "")

    def needs_setup(self) -> bool:
        return not (self.server and self.token)

    def is_lending_user(self) -> bool:
        return h.is_confirmed_lending_user(self.config)

    def save_settings(self, server: str, token: str, vpn: str) -> str | None:
        server, token, vpn = server.strip().rstrip("/"), token.strip(), vpn.strip()
        if token.lower().startswith(h.CONNECTION_CODE_PREFIX):  # 管理画面の「接続コード」を貼った
            try:
                server, token = h.parse_connection_code(token)
            except ValueError as exc:
                return str(exc)
        error = validate_settings(server, token)
        if error:
            return error
        data = {**h.load_config_file(self.config_path), "server": server, "token": token}
        if vpn and vpn != "-":
            data["vpn_interface"] = vpn
        else:
            data.pop("vpn_interface", None)
        h.save_config_file(self.config_path, data)
        self.reload()
        return None

    def login(self, email: str, password: str, server: str = "") -> tuple[str, bool]:
        """貸し手のメールアドレスとパスワードでこの PC を登録し、(PC の名前, 承認待ちか) を返す。

        問題があれば、画面にそのまま出せる文章の ValueError。
        """
        server = (server or self.server).strip().rstrip("/")
        email = email.strip()
        if not server.startswith(("https://", "http://")):
            raise ValueError("予約サイトのアドレスを入れてください（https:// で始まるもの）")
        if "@" not in email or not password:
            raise ValueError("メールアドレスとパスワードを入れてください")
        machine_id = self.config.get("machine_id") or uuid.uuid4().hex
        result = h.ServerClient(server, "", timeout=15).login(email, password, h.computer_name(), machine_id)
        if result is None:
            raise ValueError("予約サイトにつながりません。インターネットにつながっているか確認してください")
        if result.get("error") or not str(result.get("token", "")).startswith("pcvr_"):
            raise ValueError(str(result.get("error") or "ログインできませんでした"))
        data = {**h.load_config_file(self.config_path), "token": result["token"], "machine_id": machine_id}
        if server != h.builtin_server():
            data["server"] = server
        else:
            data.pop("server", None)
        h.save_config_file(self.config_path, data)
        self.reload()
        h.STATUS.host_state = "pending" if result.get("pending") else "ok"
        return str(result.get("host") or "この PC"), bool(result.get("pending"))

    def client(self) -> h.ServerClient:
        return h.ServerClient(self.server, self.token)

    def register_lending_user(self) -> str:
        user = h.register_lending_user(self.config_path)
        self.reload()
        return user

    def lending_summary(self) -> str:
        return h.lending_user_summary(self.cfg.wipe_targets)

    def lending_warning(self) -> str:
        """ふだん使いのユーザーらしいとき (OneDrive などとつながっている) の注意。なければ空。"""
        if any(os.environ.get(env) for env in h.CLOUD_SYNC_ENV) or any(
            h.cloud_sync_root_of(t) for t in self.cfg.wipe_targets
        ):
            return ("このユーザーは OneDrive などのクラウドとつながっています。ふだん使いのユーザーではありませんか？"
                    "クラウドの中の写真は消せないため、借りた人の写真が残ることがあります。")
        return ""

    # --- 待機の開始・停止 -----------------------------------------------

    def can_start(self) -> bool:
        return not self.needs_setup() and self.is_lending_user()

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def in_session(self) -> bool:
        return h.STATUS.state in ("session", "cleaning")

    def start(self) -> bool:
        if self.running() or not self.can_start():
            return False
        self.stop_event = threading.Event()
        client = h.ServerClient(self.server, self.token)
        cfg = self.cfg
        self.thread = threading.Thread(
            target=h.serve, args=(cfg, client, self.home, self.poll_interval, self.stop_event),
            name="geaspec-serve", daemon=True,
        )
        self.thread.start()
        return True

    def request_stop(self) -> None:
        """待機をやめる。利用中なら、今のセッションを終了して後片付けまで行う。"""
        self.stop_event.set()
        agent = h._current_agent
        if agent is not None:
            agent.request_stop("agent_closed")

    def cleanup_now(self) -> str | None:
        """「今すぐ削除」。問題があれば理由を返す。"""
        if self.in_session():
            return "利用中は削除できません"
        if not self.is_lending_user():
            return "このユーザーは貸し出し専用として登録されていません"
        if self.cleanup_thread and self.cleanup_thread.is_alive():
            return "削除中です"

        def run() -> None:
            agent = h.HostAgent(self.cfg, self.home)
            agent.end_reason = "cleanup_now"
            agent.cleanup_session()

        self.cleanup_thread = threading.Thread(target=run, name="geaspec-cleanup", daemon=True)
        self.cleanup_thread.start()
        return None

    # --- 画面に出す文章 ---------------------------------------------------

    def status_text(self, now: dt.datetime | None = None) -> tuple[str, str, str]:
        """(見出し, 説明, 色の種類) を返す。"""
        now = now or dt.datetime.now()
        if self.needs_setup():
            return "はじめにログインしてください", "Geaspec のメールアドレスとパスワードを入れます", "warn"
        if not self.is_lending_user():
            return (
                "貸し出し専用ユーザーとして未登録",
                "このユーザーでは何も削除しません。登録すると予約の受付を始めます",
                "warn",
            )
        st = h.STATUS
        if st.state == "idle" and st.host_state == "pending":
            return (
                "運営の確認待ちです",
                "確認が終わると予約を受けられるようになります。先に下のカレンダーで貸し出す時間を選んでおけます",
                "warn",
            )
        if st.state == "idle" and st.host_state == "paused":
            return "運営が受付を止めています", "くわしくは運営に聞いてください", "warn"
        title, tone = STATE_LABELS.get(st.state, (st.state, "warn"))
        last = st.last_poll_at.strftime("%H:%M:%S") if st.last_poll_at else "-"
        if st.state == "session":
            title = f"利用中 · 予約 #{st.booking_id}"
            if st.session_ends_at:
                left = max(0, int((st.session_ends_at - now).total_seconds()))
                detail = f"残り {left // 60:02d}:{left % 60:02d}（{st.session_ends_at:%H:%M} に自動で終了します）"
            else:
                detail = "セッションを準備しています"
        elif st.state == "cleaning":
            detail = f"予約 #{st.booking_id} の後片付けをしています"
        elif st.state == "idle" and st.next_starts_at:
            left = (st.next_starts_at - now).total_seconds()
            title = f"あと {until_text(left)}で Geass を開始します" if left > 60 else "まもなく Geass を開始します"
            detail = (
                f"予約 #{st.next_booking_id} · {day_label(st.next_starts_at)} "
                f"{st.next_starts_at:%H:%M}〜{st.next_ends_at:%H:%M}\n"
                "時間になると自動で準備します。PC とこのウィンドウは開いたままにしてください"
            )
            tone = "busy"
        elif st.state == "idle":
            detail = f"まだ予約はありません。下のカレンダーで貸し出す時間を選べます · 最終確認 {last}"
        elif st.state == "unreachable":
            detail = f"インターネットにつながっているか確認してください · {int(self.poll_interval)} 秒ごとに自動でやり直します"
        elif st.state == "auth_error":
            detail = "「ログインし直す」から、もう一度ログインしてください"
        else:
            detail = "開始しています…" if self.running() else "待機していません"
        return title, detail, tone


class ScheduleModel:
    """「貸し出す時間」のカレンダー (1 マス = 1 時間) の中身。画面に依存しない。

    時刻はすべてこの PC のローカル時刻 (タイムゾーンなし)。
    """

    HOUR = dt.timedelta(hours=1)
    LOCKED = ("booked", "holding", "past", "fixed")

    def __init__(self, week_start: dt.datetime, days: int = 7) -> None:
        self.week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        self.days = days
        self.locked: dict[dt.datetime, str] = {}  # 予約あり・支払い待ちなど、触れないマス
        self.original: set[dt.datetime] = set()  # サーバーで受付中のマス
        self.selected: set[dt.datetime] = set()  # 画面で選ばれているマス
        self.price_yen = 500
        self.trial_minutes = 0
        self.host = ""

    @property
    def end(self) -> dt.datetime:
        return self.week_start + dt.timedelta(days=self.days)

    def load(self, data: dict) -> None:
        self.locked.clear()
        self.original.clear()
        self.price_yen = int(data.get("price_yen") or self.price_yen)
        self.trial_minutes = int(data.get("trial_minutes") or 0)
        self.host = str(data.get("host") or "")
        for item in data.get("slots", []):
            start = h.server_time_to_local(item["start"])
            end = h.server_time_to_local(item["end"])
            state = item.get("state")
            if item.get("editable"):
                if state == "open":
                    self.original.add(start)
                continue
            label = state if state in ("booked", "holding", "past") else "fixed"
            t = start.replace(minute=0, second=0, microsecond=0)
            while t < end:
                self.locked[t] = label
                t += self.HOUR
        self.selected = set(self.original)

    def cell(self, day: int, hour: int) -> dt.datetime:
        return self.week_start + dt.timedelta(days=day, hours=hour)

    def state(self, t: dt.datetime, now: dt.datetime) -> str:
        """open / empty / booked / holding / past / fixed"""
        if t in self.locked:
            return self.locked[t]
        if t < now:
            return "past"
        return "open" if t in self.selected else "empty"

    def can_edit(self, t: dt.datetime, now: dt.datetime) -> bool:
        return t not in self.locked and t >= now and self.week_start <= t < self.end

    def set(self, t: dt.datetime, on: bool, now: dt.datetime) -> bool:
        """マスを選ぶ・外す。変わったら True。"""
        if not self.can_edit(t, now) or (t in self.selected) == on:
            return False
        if on:
            self.selected.add(t)
        else:
            self.selected.discard(t)
        return True

    def copy_from(self, previous: "ScheduleModel", now: dt.datetime) -> int:
        """前の週に出していた時間 (予約が入ったものも含む) を、この週にも選ぶ。選んだマスの数を返す。"""
        shift = self.week_start - previous.week_start
        offered = previous.original | {t for t, st in previous.locked.items() if st in ("booked", "holding", "past")}
        return sum(self.set(t + shift, True, now) for t in sorted(offered))

    def dirty(self) -> bool:
        return self.selected != self.original

    def starts_to_save(self, now: dt.datetime) -> list[dt.datetime]:
        return sorted(t for t in self.selected if t >= now)

    def counts(self, now: dt.datetime) -> dict[str, int]:
        result: dict[str, int] = {}
        for d in range(self.days):
            for hr in range(24):
                st = self.state(self.cell(d, hr), now)
                result[st] = result.get(st, 0) + 1
        return result


# ---------------------------------------------------------------------------
# 画面 (tkinter は Windows 版 Python に標準で入っている)
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#f7f5ff",
    "card": "#ffffff",
    "ink": "#1c1733",
    "muted": "#554e6e",
    "line": "#ddd6f3",
    "primary": "#5b3cf0",
    "ok": "#13a07a",
    "busy": "#5b3cf0",
    "warn": "#c77700",
    "bad": "#c41f4b",
    "warn_bg": "#fff3c2",
    # カレンダーのマス
    "cell_empty": "#ffffff",
    "cell_open": "#3ee0b5",
    "cell_booked": "#5b3cf0",
    "cell_holding": "#ffd84a",
    "cell_past": "#ece8f7",
    "cell_fixed": "#d9f9ef",
}


def pick_font_family(root) -> str:
    from tkinter import font

    families = set(font.families(root))
    for name in ("Yu Gothic UI", "Meiryo UI", "Segoe UI", "Noto Sans CJK JP", "WenQuanYi Zen Hei"):
        if name in families:
            return name
    return font.nametofont("TkDefaultFont").actual("family")


def should_cover(state: str) -> bool:
    """利用中と後片付け中は、パソコンの画面 (デスクトップ) を覆って見せない。"""
    return state in ("session", "cleaning")


def virtual_screen(root) -> tuple[int, int, int, int]:
    """すべてのモニターを合わせた範囲 (x, y, 幅, 高さ)。"""
    if sys.platform == "win32":
        try:
            import ctypes

            metric = ctypes.windll.user32.GetSystemMetrics
            x, y, w, hgt = metric(76), metric(77), metric(78), metric(79)  # SM_*VIRTUALSCREEN
            if w > 0 and hgt > 0:
                return x, y, w, hgt
        except (AttributeError, OSError):
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


COVER = {
    "bg": "#110e21", "text": "#ece9ff", "muted": "#8a83a8", "accent": "#a996ff",
    "primary": "#5b3cf0", "track": "#241e3d", "yellow": "#ffd84a", "mint": "#3ee0b5",
}
ENDING_SOON_SECONDS = 5 * 60


def countdown_text(seconds: float) -> str:
    """1 時間未満は 42:13、それ以上は 1:02:13。"""
    s = max(0, int(seconds))
    h_, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h_}:{m:02d}:{sec:02d}" if h_ else f"{m}:{sec:02d}"


def cover_content(state: str, ends_at: dt.datetime | None, total_seconds: float,
                  now: dt.datetime, dots: int = 3) -> dict:
    """幕に出す文言と色 (デザイン案: 利用中 / 残り 5 分 / 片づけ中)。"""
    if state == "cleaning":
        return {
            "mode": "cleaning", "pill": "Geass は終了しました", "pill_bg": COVER["mint"], "pill_fg": COVER["bg"],
            "big": "データを片づけています" + "." * dots, "lines": ("ご利用ありがとうございました", ""),
            "footer": "片づけが終わるまで、電源を切らないでください",
        }
    if ends_at is None:
        return {
            "mode": "preparing", "pill": "Geass 利用中", "pill_bg": COVER["primary"], "pill_fg": COVER["text"],
            "big": "準備しています" + "." * dots, "lines": ("まもなく始まります", ""),
            "footer": "このパソコンは利用中のため操作できません",
        }
    left = (ends_at - now).total_seconds()
    progress = max(0.0, min(1.0, left / total_seconds)) if total_seconds > 0 else 0.0
    ending = left <= ENDING_SOON_SECONDS
    color = COVER["yellow"] if ending else COVER["primary"]
    return {
        "mode": "ending" if ending else "running",
        "pill": "まもなく終了します" if ending else "Geass 利用中",
        "pill_bg": color, "pill_fg": COVER["bg"] if ending else COVER["text"],
        "countdown": countdown_text(left), "countdown_fg": COVER["yellow"] if ending else COVER["text"],
        "end_label": f"{ends_at:%H:%M} に終了", "progress": progress, "bar": color,
        "lines": ("写真の撮り忘れはありませんか？", "終わる時間になると、自動で終了します") if ending else
                 ("ゲームはそのまま続いています", "デスクトップ表示を閉じると、VR に戻れます"),
        "footer": "このパソコンは利用中のため操作できません",
    }


class CoverWindow:
    """利用中にパソコンの画面全体を覆う幕。

    借り手が Virtual Desktop でパソコンの画面を開いても、見えるのはこの幕だけになる。
    VRChat の VR の映像は SteamVR が直接 Quest に送るので、幕の影響は受けない。
    幕の下で他のアプリを開いても、幕より前には出てこない。閉じるボタンはない。
    文言と配置はデザイン案どおり: 状態の札・大きな残り時間・終了時刻・残りのバー・ひとこと。
    """

    def __init__(self, window: "AgentWindow") -> None:
        tk = window.tk
        self.window = window
        top = self.top = tk.Toplevel(window.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=COVER["bg"], cursor="arrow")
        top.protocol("WM_DELETE_WINDOW", lambda: None)
        top.bind("<Alt-F4>", lambda e: "break")
        self.canvas = tk.Canvas(top, bg=COVER["bg"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.family = window.font[0]
        self.visible = False
        self.styled = False
        self.size = (0, 0)
        self.dots = 0

    def _no_activate(self) -> None:
        """幕を出しても VRChat から入力を奪わないようにし、Alt+Tab の一覧にも出さない (Windows のみ)。"""
        if self.styled or sys.platform != "win32":
            return
        self.styled = True
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = -20, 0x80, 0x08000000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except (AttributeError, OSError):
            pass

    def _round_rect(self, x1: float, y1: float, x2: float, y2: float, fill: str) -> None:
        r = (y2 - y1) / 2
        c = self.canvas
        c.create_oval(x1, y1, x1 + 2 * r, y2, fill=fill, outline="")
        c.create_oval(x2 - 2 * r, y1, x2, y2, fill=fill, outline="")
        c.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")

    def _draw(self, content: dict) -> None:
        """1080p を基準に、画面の大きさに合わせて描く (全部のモニターをまとめた範囲の中央)。"""
        c = self.canvas
        c.delete("all")
        w, hgt = self.size
        k = max(0.5, min(w / 1920, hgt / 1080))
        cx = w / 2

        def font(px: float, bold: bool = False):
            return (self.family, -max(8, int(px * k)), "bold") if bold else (self.family, -max(8, int(px * k)))

        y = hgt * 0.45 - 260 * k
        # 状態の札
        pill = c.create_text(cx, 0, text=content["pill"], font=font(36, True), fill=content["pill_fg"])
        bx1, _, bx2, _ = c.bbox(pill)
        pill_h = 36 * k + 24 * k
        self._round_rect(bx1 - 32 * k, y, bx2 + 32 * k, y + pill_h, content["pill_bg"])
        c.tag_raise(pill)
        c.coords(pill, cx, y + pill_h / 2)
        y += pill_h + 40 * k
        if "countdown" in content:
            c.create_text(cx, y + 16 * k, text="のこり", font=font(32), fill=COVER["muted"])
            y += 32 * k + 8 * k
            c.create_text(cx, y + 100 * k, text=content["countdown"], font=font(200, True), fill=content["countdown_fg"])
            y += 200 * k + 8 * k
            c.create_text(cx, y + 18 * k, text=content["end_label"], font=font(36), fill=COVER["accent"])
            y += 36 * k + 32 * k
            bw, bh = 640 * k, 12 * k
            self._round_rect(cx - bw / 2, y, cx + bw / 2, y + bh, COVER["track"])
            if content["progress"] > 0.02:
                self._round_rect(cx - bw / 2, y, cx - bw / 2 + bw * content["progress"], y + bh, content["bar"])
            y += bh + 64 * k
        else:
            y += 40 * k
            # 文字は中央に固定し、点だけを右に足す (点が増えても文字が左右に動かないように)
            words = content["big"].rstrip(".")
            item = c.create_text(cx, y + 28 * k, text=words, font=font(56, True), fill=COVER["text"])
            c.create_text(c.bbox(item)[2], y + 28 * k, text="." * (len(content["big"]) - len(words)),
                          font=font(56, True), fill=COVER["text"], anchor="w")
            y += 56 * k + 64 * k
        first, second = content["lines"]
        if first:
            c.create_text(cx, y + 18 * k, text=first, font=font(36), fill=COVER["text"])
            y += 36 * k * 1.4
        if second:
            c.create_text(cx, y + 14 * k, text=second, font=font(28), fill=COVER["muted"])
        c.create_text(cx, hgt - 56 * k, text=content["footer"], font=font(22), fill=COVER["muted"])

    def show(self, state: str, ends_at: dt.datetime | None, total_seconds: float) -> None:
        if not self.visible:
            x, y, w, hgt = virtual_screen(self.window.root)
            self.size = (w, hgt)
            self.top.geometry(f"{w}x{hgt}{x:+d}{y:+d}")
            self.top.deiconify()
            self._no_activate()
            self.visible = True
        self.dots = self.dots % 3 + 1
        self._draw(cover_content(state, ends_at, total_seconds, dt.datetime.now(), self.dots))
        # ほかのウィンドウが前に出ても、毎回いちばん前に戻す
        self.top.attributes("-topmost", True)
        self.top.lift()

    def hide(self) -> None:
        if self.visible:
            self.top.withdraw()
            self.visible = False


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class AgentWindow:
    """Geaspec Agent のウィンドウ。はじめての設定も、カレンダーも、すべてこの 1 つのウィンドウの中で行う。"""

    MAX_LOG_LINES = 1000

    def __init__(self, controller: AgentController, log_queue: "queue.Queue[str]") -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.c = controller
        self.log_queue = log_queue
        self.closing = False
        self.home: HomeView | None = None

        root = self.root = tk.Tk()
        root.title(WINDOW_TITLE)
        root.geometry("700x760")
        root.minsize(640, 600)
        root.configure(bg=COLORS["bg"])
        family = pick_font_family(root)
        self.font = (family, 10)
        self.font_small = (family, 9)
        self.font_bold = (family, 10, "bold")
        self.font_title = (family, 16, "bold")
        self.font_brand = (family, 13, "bold")

        style = ttk.Style(root)
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        style.configure("TButton", font=self.font, padding=(12, 6))
        style.configure("Primary.TButton", font=self.font_bold)
        style.configure("Big.TButton", font=(family, 12, "bold"), padding=(18, 10))
        style.configure("TCheckbutton", font=self.font, background=COLORS["bg"])

        header = tk.Frame(root, bg=COLORS["bg"])
        header.pack(fill="x", padx=18, pady=(12, 4))
        tk.Label(header, text="Geaspec", font=self.font_brand, fg=COLORS["primary"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(header, text=f" Agent {h.AGENT_VERSION}", font=self.font, fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(
            header, text=f"PC ユーザー: {h.current_windows_user()}", font=self.font, fg=COLORS["muted"], bg=COLORS["bg"]
        ).pack(side="right")

        # ここに「はじめての設定」か「ホーム（状態とカレンダー）」のどちらかを入れる
        self.content = tk.Frame(root, bg=COLORS["bg"])
        self.content.pack(fill="both", expand=True)

        # 動きの記録は、画面に出していないときも集めておく
        self.log_lines: list[str] = []

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.cover = CoverWindow(self)
        self.seen_new_bookings = h.STATUS.new_bookings
        self.show_start_page()
        self.tick()

    # --- 画面の切り替え -----------------------------------------------------

    def clear(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        self.home = None

    def show_start_page(self) -> None:
        if self.c.needs_setup():
            self.show_setup()
        elif not self.c.is_lending_user():
            self.show_setup(step=1)
        else:
            self.show_home()
            self.restart_waiting()

    def show_setup(self, step: int = 0) -> None:
        self.clear()
        SetupView(self, step)

    def show_home(self) -> None:
        self.clear()
        self.home = HomeView(self)

    # --- 裏での処理 ---------------------------------------------------------

    def run_async(self, work, done) -> None:
        """通信などの時間がかかる処理を裏で行い、終わったら画面のスレッドで done(結果, 例外) を呼ぶ。"""
        box: dict = {}

        def worker() -> None:
            try:
                box["result"] = work()
            except Exception as exc:  # noqa: BLE001 - 画面に出す
                box["error"] = exc

        thread = threading.Thread(target=worker, name="geaspec-ui-work", daemon=True)
        thread.start()

        def check() -> None:
            if thread.is_alive():
                self.root.after(100, check)
            else:
                done(box.get("result"), box.get("error"))

        self.root.after(100, check)

    def tick(self) -> None:
        self.update_cover()
        if self.home is not None:
            self.home.update_status()
        if h.STATUS.new_bookings != self.seen_new_bookings:
            self.seen_new_bookings = h.STATUS.new_bookings
            self.bring_to_front()
            if self.home is not None:
                self.home.schedule.load()  # 予約が入ったマスを紫にする
        self.drain_logs()
        if self.closing and not self.c.running():
            self.root.destroy()
            return
        self.root.after(500, self.tick)

    def update_cover(self) -> None:
        st = h.STATUS
        if not should_cover(st.state):
            self.cover.hide()
            return
        try:
            self.cover.show(st.state, st.session_ends_at, st.session_total_seconds)
        except self.tk.TclError:
            pass

    def drain_logs(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_lines = (self.log_lines + lines)[-self.MAX_LOG_LINES:]
        if self.home is not None:
            self.home.append_log(lines)

    def bring_to_front(self) -> None:
        """新しい予約が入ったら、ウィンドウを前に出して知らせる。"""
        if self.cover.visible:
            return  # 利用中は幕の前に出さない
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(2000, lambda: self.root.attributes("-topmost", False))
            self.root.bell()
        except self.tk.TclError:
            pass

    # --- 操作 ---------------------------------------------------------------

    def restart_waiting(self) -> None:
        """ログインし直したら、新しい設定で待機をやり直す。"""
        if self.c.running() and not self.c.in_session():
            self.c.request_stop()
            self.c.thread.join(timeout=5)
        if not self.c.running():
            self.c.start()

    def show_targets(self) -> None:
        self.info("消すもの（ここでは何も消しません）", self.c.lending_summary())

    def cleanup_now(self) -> None:
        from tkinter import messagebox

        if not messagebox.askyesno(
            "今すぐ削除", "VRChat などを閉じて、ログインの情報・一時ファイル・写真を消します。\nよろしいですか？",
            icon="warning", default="no", parent=self.root,
        ):
            return
        error = self.c.cleanup_now()
        if error:
            messagebox.showwarning("今すぐ削除", error, parent=self.root)

    def open_log_folder(self) -> None:
        self.c.home.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.c.home)  # type: ignore[attr-defined]
        else:
            self.info("記録のフォルダ", str(self.c.home))

    def info(self, title: str, message: str) -> None:
        from tkinter import messagebox

        messagebox.showinfo(title, message, parent=self.root)

    def on_close(self) -> None:
        from tkinter import messagebox

        if self.closing:
            return
        if self.home is not None and not self.home.schedule.ask_discard():
            return
        if self.c.in_session():
            message = "利用中です。終了すると今のセッションを終了し、PC のデータを削除します。\n終了しますか？"
        elif self.c.running():
            message = "Agent を終了すると、予約を受け付けなくなります。\n終了しますか？"
        else:
            self.root.destroy()
            return
        if not messagebox.askyesno("Geaspec Agent を終了", message, icon="warning", default="no", parent=self.root):
            return
        self.closing = True
        if self.home is not None:
            self.home.status_title.configure(text="終了しています…")
        self.c.request_stop()  # 後片付けが終わると tick() がウィンドウを閉じる


class SetupView:
    """はじめての設定 (ウィンドウの中に表示する)。① ログイン → ② 貸し出し専用ユーザーの確認。"""

    STEPS = ("ログイン", "ユーザーの確認")

    def __init__(self, window: AgentWindow, step: int = 0) -> None:
        tk = window.tk
        self.w = window
        frame = self.frame = tk.Frame(window.content, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True, padx=28, pady=(8, 20))
        steps = tk.Frame(frame, bg=COLORS["bg"])
        steps.pack(fill="x", pady=(4, 8))
        for i, name in enumerate(self.STEPS):
            current = i == step
            color = COLORS["primary"] if current else (COLORS["ok"] if i < step else COLORS["muted"])
            tk.Label(
                steps, text=f"{'✓' if i < step else i + 1} {name}", font=window.font_bold if current else window.font,
                fg=color, bg=COLORS["bg"],
            ).pack(side="left")
            if i < len(self.STEPS) - 1:
                tk.Label(steps, text="  ―  ", font=window.font, fg=COLORS["line"], bg=COLORS["bg"]).pack(side="left")
        self.body = tk.Frame(frame, bg=COLORS["bg"])
        self.body.pack(fill="both", expand=True)
        (self.page_login if step == 0 else self.page_user)()

    def heading(self, title: str, text: str) -> None:
        tk = self.w.tk
        tk.Label(self.body, text=title, font=self.w.font_title, fg=COLORS["ink"], bg=COLORS["bg"], anchor="w").pack(
            fill="x", pady=(8, 6)
        )
        tk.Label(
            self.body, text=text, font=self.w.font, fg=COLORS["muted"], bg=COLORS["bg"], justify="left",
            anchor="w", wraplength=600,
        ).pack(fill="x", pady=(0, 14))

    def field(self, label: str, var, show: str = "") -> object:
        from tkinter import ttk

        tk = self.w.tk
        tk.Label(self.body, text=label, font=self.w.font_bold, fg=COLORS["ink"], bg=COLORS["bg"], anchor="w").pack(fill="x")
        entry = ttk.Entry(self.body, textvariable=var, width=48, font=self.w.font, show=show)
        entry.pack(anchor="w", pady=(2, 12), ipady=3)
        return entry

    # --- ① ログイン ---------------------------------------------------------

    def page_login(self) -> None:
        tk = self.w.tk
        from tkinter import ttk

        self.heading(
            "Geaspec のアカウントでログイン",
            "予約サイトで使っているメールアドレスとパスワードを入れてください。\n"
            "アカウントがまだない人は、「アカウントを作る」から作れます（借りる人と同じアカウントで大丈夫です）。",
        )
        c = self.w.c
        self.server = tk.StringVar(value=c.server)
        self.email = tk.StringVar()
        self.password = tk.StringVar()
        if not c.server:
            # 予約サイトのアドレスがアプリに入っていないときだけ聞く
            self.field("予約サイトのアドレス（運営から教えてもらったもの）", self.server)
        first = self.field("メールアドレス", self.email)
        self.field("パスワード", self.password, show="•")
        first.focus_set()
        self.message = tk.Label(
            self.body, text="", font=self.w.font, bg=COLORS["bg"], fg=COLORS["muted"], justify="left", anchor="w",
            wraplength=600,
        )
        self.message.pack(fill="x")
        buttons = tk.Frame(self.body, bg=COLORS["bg"])
        buttons.pack(fill="x", pady=(10, 0))
        self.login_button = ttk.Button(buttons, text="ログイン", style="Big.TButton", command=self.login)
        self.login_button.pack(side="left")
        ttk.Button(buttons, text="アカウントを作る", command=self.signup).pack(side="left", padx=10)
        ttk.Button(buttons, text="詳しい設定", command=lambda: SettingsDialog(self.w)).pack(side="right")
        self.w.root.bind("<Return>", lambda e: self.login())

    def say(self, text: str, tone: str = "muted") -> None:
        self.message.configure(text=text, fg=COLORS.get(tone, COLORS["muted"]))

    def signup(self) -> None:
        import webbrowser

        server = self.server.get().strip().rstrip("/")
        if not server.startswith(("https://", "http://")):
            self.say("先に予約サイトのアドレスを入れてください", "bad")
            return
        webbrowser.open(server + "/signup")

    def login(self) -> None:
        if not self.login_button.winfo_exists() or self.login_button.instate(["disabled"]):
            return
        email, password, server = self.email.get(), self.password.get(), self.server.get()
        self.say("ログインしています…")
        self.login_button.state(["disabled"])

        def done(result, error) -> None:
            if not self.frame.winfo_exists():
                return
            self.login_button.state(["!disabled"])
            if error:
                self.say(str(error), "bad")
                return
            self.w.root.unbind("<Return>")
            name, pending = result
            h.log.info("「%s」として予約サイトにログインしました%s", name, "（運営の確認待ち）" if pending else "")
            if self.w.c.is_lending_user():
                self.w.show_home()
                self.w.restart_waiting()
            else:
                self.w.show_setup(step=1)

        self.w.run_async(lambda: self.w.c.login(email, password, server), done)

    # --- ② 貸し出し専用ユーザー ---------------------------------------------

    def page_user(self) -> None:
        tk = self.w.tk
        from tkinter import ttk

        user = h.current_windows_user()
        self.heading(
            f"Windows ユーザー「{user}」を貸し出し専用にします",
            "借りた人のデータを残さないよう、利用が終わるたびに、このユーザーの次のものを消します（元に戻せません）。",
        )
        for line in ("VRChat のログイン情報と設定", "VRChat のキャッシュと一時ファイル",
                     "VRChat で撮った写真（借りた人に届けてから消します）"):
            tk.Label(self.body, text=f"・{line}", font=self.w.font, fg=COLORS["ink"], bg=COLORS["bg"],
                     anchor="w").pack(fill="x", padx=(8, 0))
        warning = self.w.c.lending_warning()
        if warning:
            tk.Label(self.body, text=f"⚠ {warning}", font=self.w.font, fg=COLORS["warn"], bg=COLORS["bg"],
                     anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(10, 0))
        link = tk.Label(self.body, text="消す場所をくわしく見る", font=self.w.font_small, fg=COLORS["primary"],
                        bg=COLORS["bg"], cursor="hand2", anchor="w")
        link.pack(fill="x", pady=(8, 0))
        link.bind("<Button-1>", lambda e: self.w.show_targets())
        self.agree = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.body, text=f"「{user}」は貸し出し専用です。上のデータを毎回削除してかまいません",
            variable=self.agree, command=self.toggle_start,
        ).pack(anchor="w", pady=(12, 0))
        buttons = tk.Frame(self.body, bg=COLORS["bg"])
        buttons.pack(fill="x", pady=(14, 0))
        self.start_button = ttk.Button(buttons, text="登録してはじめる", style="Big.TButton", command=self.register)
        self.start_button.pack(side="left")
        self.start_button.state(["disabled"])

    def toggle_start(self) -> None:
        self.start_button.state(["!disabled"] if self.agree.get() else ["disabled"])

    def register(self) -> None:
        user = self.w.c.register_lending_user()
        h.log.info("「%s」を貸し出し専用ユーザーとして登録しました", user)
        self.w.show_home()
        self.w.restart_waiting()


class HomeView:
    """ホーム: 今の状態 (次の予約まであと何分など) と、貸し出す時間のカレンダー。"""

    def __init__(self, window: AgentWindow) -> None:
        tk = window.tk
        from tkinter import ttk

        self.w = window
        frame = self.frame = tk.Frame(window.content, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True)

        # --- 状態 ---
        card = tk.Frame(frame, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        card.pack(fill="x", padx=18, pady=(4, 8))
        row = tk.Frame(card, bg=COLORS["card"])
        row.pack(fill="x", padx=16, pady=(12, 2))
        self.dot = tk.Canvas(row, width=16, height=16, bg=COLORS["card"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 10))
        self.dot_item = self.dot.create_oval(2, 2, 14, 14, fill=COLORS["warn"], outline="")
        self.status_title = tk.Label(row, text="", font=window.font_title, fg=COLORS["ink"], bg=COLORS["card"])
        self.status_title.pack(side="left")
        self.status_detail = tk.Label(
            card, text="", font=window.font, fg=COLORS["muted"], bg=COLORS["card"], justify="left", anchor="w",
            wraplength=600,
        )
        self.status_detail.pack(fill="x", padx=(42, 16), pady=(0, 12))

        # --- カレンダー / 動きの記録 (同じ場所を切り替える) ---
        self.middle = tk.Frame(frame, bg=COLORS["bg"])
        self.middle.pack(fill="both", expand=True)
        self.schedule = ScheduleView(window, self.middle)
        self.log_frame = tk.Frame(self.middle, bg=COLORS["line"])
        self.log_text = tk.Text(
            self.log_frame, wrap="word", font=window.font, bg=COLORS["card"], fg=COLORS["ink"],
            relief="flat", padx=10, pady=8, state="disabled",
        )
        scroll = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self.append_log(window.log_lines)

        # --- その他の操作 ---
        actions = tk.Frame(frame, bg=COLORS["bg"])
        actions.pack(fill="x", padx=18, pady=(6, 4))
        self.log_button = ttk.Button(actions, text="動きの記録", command=self.toggle_log)
        self.log_button.pack(side="left")
        ttk.Button(actions, text="消すものを確認", command=window.show_targets).pack(side="left", padx=6)
        self.cleanup_button = ttk.Button(actions, text="今すぐ削除", command=window.cleanup_now)
        self.cleanup_button.pack(side="left")
        ttk.Button(actions, text="ログインし直す", command=self.relogin).pack(side="right")
        tk.Label(
            frame, text="ウィンドウを閉じると Agent も終了し、予約を受け付けなくなります。",
            font=window.font_small, fg=COLORS["muted"], bg=COLORS["bg"], anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 10))
        self.update_status()

    def update_status(self) -> None:
        c = self.w.c
        title, detail, tone = c.status_text()
        self.status_title.configure(text=title)
        self.status_detail.configure(text=detail)
        self.dot.itemconfigure(self.dot_item, fill=COLORS.get(tone, COLORS["warn"]))
        self.cleanup_button.state(["disabled"] if c.in_session() else ["!disabled"])

    def append_log(self, lines: list[str]) -> None:
        if not lines:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        excess = int(self.log_text.index("end-1c").split(".")[0]) - AgentWindow.MAX_LOG_LINES
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def toggle_log(self) -> None:
        """カレンダーと動きの記録を切り替える (ウィンドウは増やさない)。"""
        if self.log_frame.winfo_ismapped():
            self.log_frame.pack_forget()
            self.schedule.frame.pack(fill="both", expand=True)
            self.log_button.configure(text="動きの記録")
        else:
            self.schedule.frame.pack_forget()
            self.log_frame.pack(fill="both", expand=True, padx=18, pady=(0, 4))
            self.log_button.configure(text="カレンダーに戻る")
            self.log_text.see("end")

    def relogin(self) -> None:
        from tkinter import messagebox

        if self.w.c.in_session():
            messagebox.showinfo("ログインし直す", "利用中はログインし直せません。終わってからもう一度押してください。", parent=self.w.root)
            return
        if self.schedule.ask_discard():
            self.w.show_setup(step=0)


class ScheduleView:
    """「貸し出す時間」のカレンダー。マスをクリック (ドラッグでまとめて) して受付時間を選ぶ。"""

    CELL_W, CELL_H, GAP = 78, 22, 3
    LEFT = 52
    VISIBLE_HOURS = 12

    def __init__(self, window: AgentWindow, parent) -> None:
        tk = window.tk
        from tkinter import ttk

        self.w = window
        self.model = ScheduleModel(dt.datetime.now())
        self.paint: bool | None = None
        self.rects: dict[dt.datetime, int] = {}
        self.busy = False
        font = window.font

        frame = self.frame = tk.Frame(parent, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True)

        bar = tk.Frame(frame, bg=COLORS["bg"])
        bar.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(bar, text="貸し出す時間", font=window.font_title, bg=COLORS["bg"], fg=COLORS["ink"]).pack(side="left")
        ttk.Button(bar, text="次の週 ▶", command=lambda: self.move(7)).pack(side="right")
        ttk.Button(bar, text="今日", command=lambda: self.move(0)).pack(side="right", padx=4)
        ttk.Button(bar, text="◀ 前の週", command=lambda: self.move(-7)).pack(side="right")

        info = tk.Frame(frame, bg=COLORS["bg"])
        info.pack(fill="x", padx=18, pady=(0, 4))
        self.range_label = tk.Label(info, text="", font=window.font_bold, bg=COLORS["bg"], fg=COLORS["ink"])
        self.range_label.pack(side="left")
        for key, label in (("past", "過ぎた時間"), ("holding", "支払い待ち"), ("booked", "予約あり"), ("open", "受付中")):
            tk.Label(info, text=label, font=window.font_small, bg=COLORS["bg"], fg=COLORS["muted"]).pack(side="right", padx=(3, 10))
            sample = tk.Canvas(info, width=16, height=12, bg=COLORS["bg"], highlightthickness=0)
            sample.create_rectangle(1, 1, 15, 11, fill=COLORS[f"cell_{key}"], outline=COLORS["line"])
            sample.pack(side="right")
        tk.Label(
            frame, text="貸せる時間のマスをクリックしてください（なぞるとまとめて選べます）。1 マス = 1 時間です。",
            font=window.font_small, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w",
        ).pack(fill="x", padx=18)

        # 曜日の見出し (スクロールしない) と、時間のマス (縦にスクロール)
        grid_w = self.LEFT + 7 * self.CELL_W
        self.head = tk.Canvas(frame, width=grid_w, height=40, bg=COLORS["bg"], highlightthickness=0)
        self.head.pack(padx=18, anchor="w")
        grid = tk.Frame(frame, bg=COLORS["bg"])
        grid.pack(padx=18, anchor="w")
        self.canvas = tk.Canvas(
            grid, width=grid_w, height=self.VISIBLE_HOURS * self.CELL_H, bg=COLORS["bg"], highlightthickness=0,
            scrollregion=(0, 0, grid_w, 24 * self.CELL_H),
        )
        scroll = ttk.Scrollbar(grid, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left")
        scroll.pack(side="left", fill="y")
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "paint", None))
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        footer = tk.Frame(frame, bg=COLORS["bg"])
        footer.pack(fill="x", padx=18, pady=(8, 0))
        tk.Label(footer, text="1 時間の料金", font=window.font_bold, bg=COLORS["bg"], fg=COLORS["ink"]).pack(side="left")
        self.price = tk.StringVar(value="500")
        ttk.Spinbox(footer, from_=50, to=100000, increment=50, width=7, textvariable=self.price, font=font).pack(
            side="left", padx=6
        )
        tk.Label(footer, text="円", font=font, bg=COLORS["bg"], fg=COLORS["ink"]).pack(side="left")
        self.save_button = ttk.Button(footer, text="保存", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(footer, text="前の週と同じにする", command=self.copy_previous).pack(side="right", padx=6)
        ttk.Button(footer, text="すべて外す", command=self.clear_week).pack(side="right")

        self.summary = tk.Label(
            frame, text="", font=window.font_small, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w", justify="left",
            wraplength=640,
        )
        self.summary.pack(fill="x", padx=18, pady=(4, 0))

        self.draw_grid()
        self.canvas.yview_moveto(12 / 24)  # はじめは 12 時から (夜の時間が見えるように)
        self.load()

    @property
    def parent_window(self):
        return self.w.root

    # --- 表示 ---------------------------------------------------------------

    def cell_box(self, day: int, hour: int) -> tuple[int, int, int, int]:
        x = self.LEFT + day * self.CELL_W
        y = hour * self.CELL_H
        return x + 1, y + 1, x + self.CELL_W - self.GAP, y + self.CELL_H - self.GAP

    def draw_grid(self) -> None:
        m, font = self.model, self.w.font
        self.head.delete("all")
        self.canvas.delete("all")
        self.rects.clear()
        for d in range(m.days):
            day = m.cell(d, 0)
            color = "#2456d6" if day.weekday() == 5 else ("#c41f4b" if day.weekday() == 6 else COLORS["ink"])
            x = self.LEFT + d * self.CELL_W + (self.CELL_W - self.GAP) / 2
            self.head.create_text(x, 12, text=f"{day:%m/%d}", font=self.w.font_bold, fill=color)
            self.head.create_text(x, 30, text=WEEKDAYS[day.weekday()], font=font, fill=color)
        for hr in range(24):
            self.canvas.create_text(
                self.LEFT - 8, hr * self.CELL_H + self.CELL_H / 2, text=f"{hr}:00", anchor="e", font=font,
                fill=COLORS["muted"],
            )
            for d in range(m.days):
                self.rects[m.cell(d, hr)] = self.canvas.create_rectangle(*self.cell_box(d, hr), width=1)
        self.range_label.configure(text=f"{day_label(m.week_start)} 〜 {day_label(m.end - dt.timedelta(days=1))}")
        self.refresh()

    def refresh(self) -> None:
        now = dt.datetime.now()
        for t, rect in self.rects.items():
            self.paint_cell(t, rect, now)
        counts = self.model.counts(now)
        text = f"この週の受付: {counts.get('open', 0)} 時間 · 予約あり: {counts.get('booked', 0)} 時間"
        if self.model.dirty():
            text += " · まだ保存していません"
        if self.model.trial_minutes:
            text += f"\nはじめて利用する人は最初の {self.model.trial_minutes} 分が無料です（その分は貸し手の負担になります）。"
        self.summary.configure(text=text, fg=COLORS["warn"] if self.model.dirty() else COLORS["muted"])

    def paint_cell(self, t: dt.datetime, rect: int, now: dt.datetime) -> None:
        state = self.model.state(t, now)
        outline = COLORS["line"] if state in ("empty", "past") else COLORS["ink"]
        self.canvas.itemconfigure(rect, fill=COLORS[f"cell_{state}"], outline=outline)

    # --- 操作 ---------------------------------------------------------------

    def cell_at(self, event) -> dt.datetime | None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        day, hour = int((x - self.LEFT) // self.CELL_W), int(y // self.CELL_H)
        if x < self.LEFT or not (0 <= day < self.model.days and 0 <= hour < 24):
            return None
        return self.model.cell(day, hour)

    def on_press(self, event) -> None:
        t = self.cell_at(event)
        now = dt.datetime.now()
        if self.busy or t is None or not self.model.can_edit(t, now):
            self.paint = None
            return
        self.paint = t not in self.model.selected
        self.apply(t, now)

    def on_drag(self, event) -> None:
        t = self.cell_at(event)
        if self.paint is not None and t is not None:
            self.apply(t, dt.datetime.now())

    def apply(self, t: dt.datetime, now: dt.datetime) -> None:
        if self.model.set(t, bool(self.paint), now):
            self.refresh()

    def clear_week(self) -> None:
        now = dt.datetime.now()
        for t in list(self.model.selected):
            self.model.set(t, False, now)
        self.refresh()

    def ask_discard(self) -> bool:
        """保存していない変更があれば聞く。続けてよければ True。"""
        from tkinter import messagebox

        if not self.model.dirty():
            return True
        answer = messagebox.askyesnocancel("貸し出す時間", "カレンダーの変更を保存しますか？", parent=self.parent_window)
        if answer is None:
            return False
        if answer:
            self.save()
            return False  # 保存が終わってから操作し直してもらう
        return True

    def move(self, days: int) -> None:
        if self.busy or not self.ask_discard():
            return
        start = self.model.week_start + dt.timedelta(days=days) if days else dt.datetime.now()
        self.model = ScheduleModel(start)
        self.draw_grid()
        self.load()

    # --- 通信 ---------------------------------------------------------------

    def set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        self.save_button.state(["disabled"] if busy else ["!disabled"])
        if text:
            self.summary.configure(text=text, fg=COLORS["muted"])

    def alive(self) -> bool:
        return bool(self.frame.winfo_exists())

    def load(self) -> None:
        if self.busy:
            return
        model, client = self.model, self.w.c.client()
        self.set_busy(True, "予約サイトから読み込んでいます…")

        def done(data, error) -> None:
            if not self.alive() or model is not self.model:
                return
            self.set_busy(False)
            if error or data is None:
                self.summary.configure(text="読み込めませんでした。インターネットにつながっているか確認してください。", fg=COLORS["bad"])
                return
            keep = model.selected - model.original if model.dirty() else set()
            model.load(data)
            now = dt.datetime.now()
            for t in keep:  # 予約が入って読み直したときも、保存前の選択は残す
                model.set(t, True, now)
            self.price.set(str(model.price_yen))
            self.refresh()

        self.w.run_async(lambda: client.get_schedule(model.week_start, model.days), done)

    def copy_previous(self) -> None:
        if self.busy:
            return
        model, client = self.model, self.w.c.client()
        previous = ScheduleModel(model.week_start - dt.timedelta(days=7))
        self.set_busy(True, "前の週を読み込んでいます…")

        def done(data, error) -> None:
            if not self.alive() or model is not self.model:
                return
            self.set_busy(False)
            if error or data is None:
                self.summary.configure(text="前の週を読み込めませんでした。", fg=COLORS["bad"])
                return
            previous.load(data)
            added = model.copy_from(previous, dt.datetime.now())
            self.refresh()
            if not added:
                self.summary.configure(text="前の週と同じ時間はすでに選ばれているか、選べる時間がありません。")

        self.w.run_async(lambda: client.get_schedule(previous.week_start, previous.days), done)

    def save(self) -> None:
        from tkinter import messagebox

        if self.busy:
            return
        try:
            price = int(self.price.get())
        except ValueError:
            price = 0
        if price < 50:
            messagebox.showwarning("貸し出す時間", "料金は 50 円以上の数字で入力してください。", parent=self.parent_window)
            return
        model, client = self.model, self.w.c.client()
        starts = model.starts_to_save(dt.datetime.now())
        self.set_busy(True, "保存しています…")

        def done(result, error) -> None:
            if not self.alive():
                return
            self.set_busy(False)
            if error or result is None:
                messagebox.showwarning(
                    "貸し出す時間", "保存できませんでした。インターネットにつながっているか確認してください。",
                    parent=self.parent_window,
                )
                self.refresh()
                return
            if result.get("error"):
                messagebox.showwarning("貸し出す時間", f"保存できませんでした: {result['error']}", parent=self.parent_window)
                self.refresh()
                return
            model.load(result)
            self.refresh()
            message = f"保存しました（追加 {result.get('added', 0)} 件・取り消し {result.get('removed', 0)} 件）"
            h.log.info("貸し出す時間: %s", message)
            if result.get("errors"):
                messagebox.showwarning(
                    "貸し出す時間", "保存できなかった時間があります:\n" + "\n".join(result["errors"]), parent=self.parent_window
                )
            else:
                self.summary.configure(text=message + "。予約サイトに並びました。", fg=COLORS["ok"])

        self.w.run_async(lambda: client.put_schedule(model.week_start, model.days, price, starts), done)


class SettingsDialog:
    """詳しい設定。ふつうは使わない (運営に言われたときだけ)。"""

    def __init__(self, window: AgentWindow) -> None:
        tk = window.tk
        from tkinter import ttk

        self.w = window
        top = self.top = tk.Toplevel(window.root)
        top.title("詳しい設定")
        top.configure(bg=COLORS["bg"])
        top.transient(window.root)
        top.resizable(False, False)
        body = tk.Frame(top, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        tk.Label(
            body, text="ふつうはログインするだけで大丈夫です。運営に言われたときだけ、ここに入力してください。",
            font=window.font, bg=COLORS["bg"], fg=COLORS["muted"],
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        c = window.c
        self.server = tk.StringVar(value=c.server)
        self.token = tk.StringVar(value=c.token)
        self.vpn = tk.StringVar(value=c.config.get("vpn_interface", "Tailscale"))
        fields = [
            ("予約サイトのアドレス", self.server, ""),
            ("合言葉（pcvr_ か geaspec1: で始まる文字）", self.token, "•"),
            ("VPN の名前（ふつうは Tailscale のまま）", self.vpn, ""),
        ]
        self.entries = []
        for i, (label, var, show) in enumerate(fields, start=1):
            tk.Label(body, text=label, font=window.font_bold, bg=COLORS["bg"], fg=COLORS["ink"]).grid(
                row=i * 2 - 1, column=0, columnspan=2, sticky="w"
            )
            entry = ttk.Entry(body, textvariable=var, width=52, font=window.font, show=show)
            entry.grid(row=i * 2, column=0, columnspan=2, sticky="we", pady=(2, 10))
            self.entries.append(entry)
        buttons = tk.Frame(body, bg=COLORS["bg"])
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="キャンセル", command=top.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", style="Primary.TButton", command=self.save).pack(side="right", padx=(0, 8))
        top.grab_set()

    def save(self) -> None:
        from tkinter import messagebox

        error = self.w.c.save_settings(self.server.get(), self.token.get(), self.vpn.get())
        if error:
            messagebox.showwarning("詳しい設定", error, parent=self.top)
            return
        self.top.destroy()
        h.log.info("詳しい設定を保存しました")
        self.w.show_start_page()


# ---------------------------------------------------------------------------


WINDOW_TITLE = f"Geaspec Agent {h.AGENT_VERSION}"


def acquire_single_instance() -> bool:
    """二重起動すると予約を二重に処理してしまうので、1 つだけにする。"""
    if sys.platform != "win32":
        return True
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\GeaspecAgent")
    acquire_single_instance.handle = handle  # GC されないよう保持
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def bring_existing_window_to_front() -> bool:
    """もう開いているウィンドウを前に出す。説明ページの「アプリに戻る」(geaspec://open) から開かれたときに使う。"""
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE: 最小化されていても元に戻す
    user32.SetForegroundWindow(hwnd)
    return True


def main() -> int:
    if not acquire_single_instance():
        if bring_existing_window_to_front():
            return 0
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Geaspec Agent", "Geaspec Agent はすでに起動しています。タスクバーを確認してください。")
        return 0
    if sys.platform == "win32":
        try:  # 高解像度の画面で文字がぼやけないようにする
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    controller = AgentController()
    h.setup_logging(controller.home, dry_run=False)
    log_queue: "queue.Queue[str]" = queue.Queue()
    h.log.addHandler(QueueLogHandler(log_queue))
    h.log.info("Geaspec Agent %s を起動しました", h.AGENT_VERSION)
    window = AgentWindow(controller, log_queue)
    window.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
