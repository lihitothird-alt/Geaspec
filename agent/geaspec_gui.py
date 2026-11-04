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
import time
import uuid
from pathlib import Path

import host_agent as h
import tray

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


STANDARD_USER_INTRO = (
    "貸し出しには、ふだん使っているユーザーとは別の「標準ユーザー」を使います。",
    "いま使っているユーザーを標準に変えるだけでは、あなたのファイルが借りる人に見えてしまいます。",
)
STANDARD_USER_STEPS = (
    "設定 → アカウント → 他のユーザー で、新しいユーザーを作る\n"
    "（メールを聞かれたら「このユーザーのサインイン情報がありません」→「Microsoft アカウントを持たないユーザーを追加する」）",
    "そのユーザーの種類を「標準ユーザー」にする（「アカウントの種類の変更」）",
    "そのユーザーでサインインして、Geaspec の Web サイト（貸し出しのページ）から Geaspec Agent を入れる",
)


STEAM_ACCOUNT_INTRO = (
    "借りた人は、VR の中で Steam の画面を開けてしまうことがあります（アプリがメニューを止めますが、念のため）。",
    "ふだん使っている Steam アカウントのままでは貸さないでください。",
)
STEAM_ACCOUNT_STEPS = (
    "Steam で、貸し出し専用の新しいアカウントを作る（無料）。クレジットカードやウォレットのお金は入れない",
    "そのアカウントで VRChat だけを入れる（ほかのゲームやフレンドは入れない）",
    "Steam ファミリーで、ふだんのアカウントを「大人」、貸し出し専用のアカウントを「子ども」にして、"
    "ストア・コミュニティ・チャットを止め、ライブラリは VRChat だけにする",
    "VRChat のプロパティで「ゲーム中に Steam オーバーレイを有効にする」をオフにする",
    "この PC の Steam に、貸し出し専用のアカウントでログインする",
)


def nbsp(text: str) -> str:
    """Tk は空白のところで行を折り返すので、日本語の文の中の「Steam で」などで切れないよう、空白を詰める。"""
    return text.replace(" ", "\u00a0")


def open_uri(uri: str) -> None:
    """steam:// などを、その PC で開く。"""
    if sys.platform == "win32":
        os.startfile(uri)  # noqa: S606 - Steam のページなどを開くだけ
    else:
        import webbrowser

        webbrowser.open(uri)


def place_center(win, over=None) -> None:
    """ウィンドウを画面の真ん中に出す。over があり、その上に収まるなら over の真ん中に重ねる。"""
    win.update_idletasks()
    min_w, min_h = win.minsize()
    w = max(win.winfo_reqwidth(), min_w, win.winfo_width() if win.winfo_ismapped() else 0)
    hgt = max(win.winfo_reqheight(), min_h, win.winfo_height() if win.winfo_ismapped() else 0)
    if over is not None and over.winfo_viewable() and over.winfo_width() >= w and over.winfo_height() >= hgt:
        x = over.winfo_rootx() + (over.winfo_width() - w) // 2
        y = over.winfo_rooty() + (over.winfo_height() - hgt) // 2
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - hgt) // 2 - 20  # 下のタスクバーのぶん少し上げる
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


def open_user_settings(root) -> None:
    """標準ユーザーの作り方をアプリと同じ見た目の小さな画面で見せ、Windows の「他のユーザー」の設定を開ける。"""
    import tkinter as tk
    from tkinter import ttk

    family = pick_font_family(root)
    top = tk.Toplevel(root)
    top.title("貸し出し用のユーザーを作る")
    top.configure(bg=COLORS["bg"])
    top.transient(root)
    top.resizable(False, False)
    body = tk.Frame(top, bg=COLORS["bg"])
    body.pack(fill="both", expand=True, padx=24, pady=20)
    tk.Label(body, text="貸し出し用のユーザーを作る", font=(family, 14, "bold"), fg=COLORS["ink"],
             bg=COLORS["bg"], anchor="w").pack(fill="x")
    for i, line in enumerate(STANDARD_USER_INTRO):
        tk.Label(body, text=nbsp(line), font=(family, 10, "bold" if i else "normal"),
                 fg=COLORS["bad"] if i else COLORS["muted"], bg=COLORS["bg"], anchor="w", justify="left",
                 wraplength=460).pack(fill="x", pady=(8 if i == 0 else 4, 0))
    steps = tk.Frame(body, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
    steps.pack(fill="x", pady=(14, 0))
    for n, line in enumerate(STANDARD_USER_STEPS, 1):
        row = tk.Frame(steps, bg=COLORS["card"])
        row.pack(fill="x", padx=14, pady=(10 if n == 1 else 4, 10 if n == len(STANDARD_USER_STEPS) else 0))
        tk.Label(row, text=str(n), width=2, font=(family, 10, "bold"), fg=COLORS["on_mint"], bg=COLORS["mint"]).pack(
            side="left", anchor="n")
        tk.Label(row, text=nbsp(line), font=(family, 10), fg=COLORS["ink"], bg=COLORS["card"], anchor="w", justify="left",
                 wraplength=400).pack(side="left", padx=(10, 0), fill="x")
    buttons = tk.Frame(body, bg=COLORS["bg"])
    buttons.pack(fill="x", pady=(16, 0))

    def go() -> None:
        top.destroy()
        if sys.platform == "win32":
            os.startfile("ms-settings:otherusers")  # noqa: S606 - Windows の設定画面を開くだけ

    ttk.Button(buttons, text="Windows の設定を開く", style="Primary.TButton", command=go).pack(side="left")
    ttk.Button(buttons, text="閉じる", command=top.destroy).pack(side="left", padx=10)
    place_center(top, over=root)
    top.grab_set()


def open_steam_account(root, controller, on_done=None) -> None:
    """貸し出し専用の Steam アカウントの作り方を見せ、いまログインしているアカウントを貸し出し専用として記録する。"""
    import tkinter as tk
    from tkinter import ttk

    family = pick_font_family(root)
    account = h.steam_logged_in_account(h.steam_root())
    top = tk.Toplevel(root)
    top.title("貸し出し専用の Steam アカウント")
    top.configure(bg=COLORS["bg"])
    top.transient(root)
    top.resizable(False, False)
    body = tk.Frame(top, bg=COLORS["bg"])
    body.pack(fill="both", expand=True, padx=24, pady=20)
    tk.Label(body, text="貸し出し専用の Steam アカウント", font=(family, 14, "bold"), fg=COLORS["ink"],
             bg=COLORS["bg"], anchor="w").pack(fill="x")
    for i, line in enumerate(STEAM_ACCOUNT_INTRO):
        tk.Label(body, text=nbsp(line), font=(family, 10, "bold" if i else "normal"),
                 fg=COLORS["bad"] if i else COLORS["muted"], bg=COLORS["bg"], anchor="w", justify="left",
                 wraplength=460).pack(fill="x", pady=(8 if i == 0 else 4, 0))
    steps = tk.Frame(body, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
    steps.pack(fill="x", pady=(14, 0))
    for n, line in enumerate(STEAM_ACCOUNT_STEPS, 1):
        row = tk.Frame(steps, bg=COLORS["card"])
        row.pack(fill="x", padx=14, pady=(10 if n == 1 else 4, 10 if n == len(STEAM_ACCOUNT_STEPS) else 0))
        tk.Label(row, text=str(n), width=2, font=(family, 10, "bold"), fg=COLORS["on_mint"], bg=COLORS["mint"]).pack(
            side="left", anchor="n")
        tk.Label(row, text=nbsp(line), font=(family, 10), fg=COLORS["ink"], bg=COLORS["card"], anchor="w", justify="left",
                 wraplength=400).pack(side="left", padx=(10, 0), fill="x")
    now = f"いま Steam にログインしているアカウント: {account}" if account else "Steam にログインしていません"
    tk.Label(body, text=now, font=(family, 10, "bold"), fg=COLORS["ink"], bg=COLORS["bg"], anchor="w").pack(
        fill="x", pady=(14, 0))
    buttons = tk.Frame(body, bg=COLORS["bg"])
    buttons.pack(fill="x", pady=(12, 0))

    def confirm() -> None:
        controller.confirm_steam_account(account)
        top.destroy()
        if on_done:
            on_done()

    ok = ttk.Button(buttons, text="このアカウントは貸し出し専用です", style="Primary.TButton", command=confirm)
    ok.pack(side="left")
    if not account:
        ok.state(["disabled"])
    ttk.Button(buttons, text="閉じる", command=top.destroy).pack(side="left", padx=10)
    place_center(top, over=root)
    top.grab_set()


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

    def confirm_steam_account(self, account: str | None) -> None:
        """いま Steam にログインしているアカウントを、貸し出し専用として記録する。"""
        if not account:
            return
        data = {**h.load_config_file(self.config_path), "lending_steam_account": account}
        h.save_config_file(self.config_path, data)
        self.reload()
        self._disk_checked_at = -1e9  # すぐに確かめ直す

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

    # --- 貸し出せる状態かのチェックリスト ---------------------------------

    DISK_CHECK_SEC = 30.0

    def readiness(self, now: float | None = None) -> list[tuple[str, str, str]]:
        """貸し出せる状態かのチェックリスト [(項目, ok / ng / wait, できていないときのひとこと)]。すべて自動で確かめる。

        ログインと貸し出し専用ユーザーは、できていなければホームではなく設定の画面が出るので、ここには入れない。
        ファイルを見る項目 (Steam など) は 30 秒ごとにだけ確かめ直す (画面は 0.5 秒ごとに呼ぶため)。
        """
        now = time.monotonic() if now is None else now
        if now - getattr(self, "_disk_checked_at", -1e9) >= self.DISK_CHECK_SEC:
            root = h.steam_root()
            self._disk = (self.cfg.streamer_path.is_file(), root is not None, h.is_steam_app_installed(root),
                          h.is_admin_account(), h.steam_logged_in_account(root))
            self._disk_checked_at = now
        streamer_ok, steam_ok, vrchat_ok, admin, steam_account = self._disk
        st = h.STATUS
        items = []
        site = "予約サイトにつながっている"
        if st.state in ("idle", "session", "cleaning"):
            items.append((site, "ok", ""))
        elif st.state == "auth_error":
            items.append((site, "ng", "「その他」→「ログインし直す」"))
        elif st.state == "unreachable":
            items.append((site, "ng", "未接続"))
        else:
            items.append((site, "wait", "確認中"))
        if st.last_poll_at is None:
            items.append(("運営の承認", "wait", "確認中"))
        else:
            approval = {"pending": ("wait", "待機中"), "paused": ("ng", "停止中")}
            items.append(("運営の承認", *approval.get(st.host_state, ("ok", ""))))
        label = h.STREAMERS.get(self.cfg.streamer, {}).get("label", "Virtual Desktop")
        items.append((f"{label} Streamer が入っている", "ok" if streamer_ok else "ng", "この PC に入れてください"))
        items.append(("Steam が入っている", "ok" if steam_ok else "ng", "この PC に入れてください"))
        items.append(("VRChat が入っている", "ok" if vrchat_ok else "ng", "Steam で入れる"))
        # ふだんの Steam アカウントで貸すと、借りた人が VR の中から Steam を触れてしまう
        confirmed = self.config.get("lending_steam_account", "")
        label = "貸し出し専用の Steam アカウントを使っている"
        if not steam_ok:
            items.append((label, "ng", "先に Steam を入れてください"))
        elif not steam_account:
            items.append((label, "ng", "Steam にログインしてください"))
        elif not confirmed:
            items.append((label, "ng", "確かめる（作り方）"))
        elif confirmed.lower() != steam_account.lower():
            items.append((label, "ng", f"いまは {steam_account} でログインしています"))
        else:
            items.append((label, "ok", ""))
        items.append(("貸し出し用の標準ユーザーを使っている", {False: "ok", True: "ng", None: "wait"}[admin],
                      "別に作ってください（やり方）" if admin else "確認中"))
        return items

    def home_summary(self, items: list[tuple[str, str, str]], now: dt.datetime) -> tuple[str, str, str, str]:
        """ホームのいちばん上に出す (見出しの上の小さな文字, 見出し, 説明, 色)。ほかの場所と同じことは書かない。"""
        st = h.STATUS
        if st.state == "session":
            end = f"{st.session_ends_at:%H:%M} まで" if st.session_ends_at else "準備中"
            return "いまの状態", f"貸し出し中（{end}）", "借りている人が VR で使っています。PC はつけたままにしてください", "busy"
        if st.state == "cleaning":
            return "いまの状態", "片づけ中", "借りた人のデータを消しています。電源は切らないでください", "warn"
        if st.state == "unreachable":
            return "いまの状態", "予約サイトにつながりません", "インターネットを確認してください。つながると自動で戻ります", "bad"
        if st.state == "auth_error":
            return "いまの状態", "ログインし直してください", "「その他」→「ログインし直す」から、もう一度ログインします", "bad"
        if st.last_poll_at is not None and st.host_state == "pending":
            return ("いまの状態", "運営の承認を待っています",
                    "承認されると予約を受けられるようになります。あなたがすることはありません", "warn")
        if st.last_poll_at is not None and st.host_state == "paused":
            return "いまの状態", "運営が受付を止めています", "くわしくは運営に聞いてください", "warn"
        missing = [label for label, state, _ in items if state == "ng"]
        if missing:
            return "いまの状態", f"準備があと {len(missing)} つ必要です", "下の ! の項目を確認してください", "bad"
        if any(state == "wait" for _, state, _ in items):
            return "いまの状態", "確認しています", "少し待つと表示が変わります", "muted"
        if st.next_starts_at:
            end = f"{st.next_ends_at:%H:%M}" if st.next_ends_at else ""
            left = (st.next_starts_at - now).total_seconds()
            when = f"あと {until_text(left)}で" if left > 60 else "まもなく"
            return ("次の貸し出し", f"{day_label(st.next_starts_at)} {st.next_starts_at:%H:%M}〜{end}",
                    f"{when}自動で始まります。PC はつけたままにしてください", "ok")
        return "いまの状態", "貸し出しの準備ができています", "Web サイトで貸し出す時間を決めると、予約を受けられます", "ok"

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
                "確認が終わると予約を受けられるようになります。先に Web サイトで貸し出す時間を決めておけます",
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
            title = f"あと {until_text(left)}で貸し出しを開始します" if left > 60 else "まもなく貸し出しを開始します"
            detail = (
                f"予約 #{st.next_booking_id} · {day_label(st.next_starts_at)} "
                f"{st.next_starts_at:%H:%M}〜{st.next_ends_at:%H:%M}\n"
                "時間になると自動で準備します。PC とこのウィンドウは開いたままにしてください"
            )
            tone = "busy"
        elif st.state == "idle":
            detail = f"まだ予約はありません。貸し出す時間は Web サイトで決めます · 最終確認 {last}"
        elif st.state == "unreachable":
            detail = f"インターネットにつながっているか確認してください · {int(self.poll_interval)} 秒ごとに自動でやり直します"
        elif st.state == "auth_error":
            detail = "「ログインし直す」から、もう一度ログインしてください"
        else:
            detail = "開始しています…" if self.running() else "待機していません"
        return title, detail, tone


# ---------------------------------------------------------------------------
# 画面 (tkinter は Windows 版 Python に標準で入っている)
# ---------------------------------------------------------------------------

# Geaspec VR のサイトと同じ色: 落ち着いた灰色の面に、淡い青をひとつだけ使う (文字の青は読めるよう濃いめ)
COLORS = {
    "bg": "#f1f5f9",
    "card": "#ffffff",
    "ink": "#2f3537",
    "muted": "#687073",
    "line": "#dbe5ed",
    "primary": "#1f6aa3",
    "mint": "#9ccdf2",
    "on_mint": "#0e3352",
    "hover": "#e6edf3",
    "ok": "#2fae8a",
    "busy": "#1f6aa3",
    "warn": "#d9a520",
    "bad": "#d0464f",
    "warn_bg": "#fff3c2",
}


def pick_font_family(root) -> str:
    from tkinter import font

    families = set(font.families(root))
    for name in ("Yu Gothic UI", "Meiryo UI", "Segoe UI", "Noto Sans CJK JP", "WenQuanYi Zen Hei"):
        if name in families:
            return name
    return font.nametofont("TkDefaultFont").actual("family")


def pick_light_family(root, fallback: str) -> str:
    """幕の大きな数字に使う細い書体 (なければふだんの書体)。"""
    from tkinter import font

    families = set(font.families(root))
    for name in ("Segoe UI Light", "Yu Gothic UI Light", "Segoe UI Semilight", "Yu Gothic UI Semilight"):
        if name in families:
            return name
    return fallback


# Geaspec のロゴ (サイトの SVG と同じ形、88×48 の座標)。幕のいちばん上に描く
LOGO_FRAME = ((10, 4), (78, 4), (84, 10), (84, 38), (78, 44), (50, 44), (44, 39), (38, 44), (10, 44), (4, 38), (4, 10))
LOGO_GEARS = (
    ((30, 24), "41.18 21.32 44.44 22.64 44.44 25.36 41.18 26.68 40.62 28.40 42.48 31.38 40.88 33.59 37.47 32.74 "
               "36.01 33.81 35.76 37.31 33.16 38.15 30.90 35.46 29.10 35.46 26.84 38.15 24.24 37.31 23.99 33.81 "
               "22.53 32.74 19.12 33.59 17.52 31.38 19.38 28.40 18.82 26.68 15.56 25.36 15.56 22.64 18.82 21.32 "
               "19.38 19.60 17.52 16.62 19.12 14.41 22.53 15.26 23.99 14.19 24.24 10.69 26.84 9.85 29.10 12.54 "
               "30.90 12.54 33.16 9.85 35.76 10.69 36.01 14.19 37.47 15.26 40.88 14.41 42.48 16.62 40.62 19.60"),
    ((58, 24), "69.46 24.90 72.15 27.16 71.31 29.76 67.81 30.01 66.74 31.47 67.59 34.88 65.38 36.48 62.40 34.62 "
               "60.68 35.18 59.36 38.44 56.64 38.44 55.32 35.18 53.60 34.62 50.62 36.48 48.41 34.88 49.26 31.47 "
               "48.19 30.01 44.69 29.76 43.85 27.16 46.54 24.90 46.54 23.10 43.85 20.84 44.69 18.24 48.19 17.99 "
               "49.26 16.53 48.41 13.12 50.62 11.52 53.60 13.38 55.32 12.82 56.64 9.56 59.36 9.56 60.68 12.82 "
               "62.40 13.38 65.38 11.52 67.59 13.12 66.74 16.53 67.81 17.99 71.31 18.24 72.15 20.84 69.46 23.10"),
)


def draw_logo(canvas, cx: float, cy: float, width: float, color: str, bg: str) -> None:
    """キャンバスの (cx, cy) を中心に、幅 width でロゴを描く。"""
    k = width / 88
    x0, y0 = cx - 44 * k, cy - 24 * k

    def pt(x: float, y: float) -> tuple[float, float]:
        return x0 + x * k, y0 + y * k

    frame = [c for x, y in LOGO_FRAME + LOGO_FRAME[:1] for c in pt(x, y)]
    canvas.create_line(*frame, fill=color, width=max(1.0, 2.6 * k), joinstyle="round", capstyle="round")
    for (hx, hy), points in LOGO_GEARS:
        nums = [float(v) for v in points.split()]
        canvas.create_polygon(*[c for x, y in zip(nums[0::2], nums[1::2]) for c in pt(x, y)], fill=color, outline="")
        canvas.create_oval(*pt(hx - 4.2, hy - 4.2), *pt(hx + 4.2, hy + 4.2), fill=bg, outline="")


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
    "bg": "#1a2127", "text": "#e6ecec", "muted": "#8f989a", "accent": "#a8d5f5",
    "primary": "#9ccdf2", "track": "#2c363e", "yellow": "#f3d27a", "mint": "#9ccdf2", "logo": "#e6ecec",
    "foot": "#c3cacb",
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
            "mode": "cleaning", "pill": "貸し出しは終了しました", "pill_bg": COVER["mint"], "pill_fg": COVER["bg"],
            "big": "データを片づけています" + "." * dots, "lines": ("ご利用ありがとうございました", "ふつうは数分で終わります"),
            "footer": "片づけが終わると、この画面は自動で消えます。電源は切らないでください",
        }
    if ends_at is None:
        return {
            "mode": "preparing", "pill": "Geaspec で貸し出し中", "pill_bg": COVER["primary"], "pill_fg": COVER["bg"],
            "big": "準備しています" + "." * dots, "lines": ("まもなく始まります", ""),
            "footer": "このパソコンは利用中のため操作できません",
        }
    left = (ends_at - now).total_seconds()
    progress = max(0.0, min(1.0, left / total_seconds)) if total_seconds > 0 else 0.0
    ending = left <= ENDING_SOON_SECONDS
    color = COVER["yellow"] if ending else COVER["primary"]
    return {
        "mode": "ending" if ending else "running",
        "pill": "まもなく終了します" if ending else "Geaspec で貸し出し中",
        "pill_bg": color, "pill_fg": COVER["bg"],
        "countdown": countdown_text(left), "countdown_fg": COVER["yellow"] if ending else COVER["text"],
        "end_label": f"{ends_at:%H:%M} に終了", "progress": progress, "bar": color,
        "lines": ("写真の撮り忘れはありませんか？", "終わる時間になると、自動で終了します") if ending else
                 ("ゲームはそのまま続いています", "デスクトップ表示を閉じると、VR に戻れます"),
        "footer": f"このパソコンは貸し出し中です。{ends_at:%H:%M} に自動で元に戻ります",
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
        self.light = pick_light_family(window.root, self.family)
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
        """1080p を基準に、画面の大きさに合わせて描く (全部のモニターをまとめた範囲の中央)。

        ロゴ・状態・残り時間・ひとこと、の順に、真ん中に縦に並べるだけにする (色はミントと黄色の 2 つだけ)。
        """
        c = self.canvas
        c.delete("all")
        w, hgt = self.size
        k = max(0.5, min(w / 1920, hgt / 1080))
        cx = w / 2

        def font(px: float, bold: bool = False, light: bool = False):
            family = self.light if light else self.family
            return (family, -max(8, int(px * k)), "bold") if bold else (family, -max(8, int(px * k)))

        ending = content["mode"] == "ending"
        accent = COVER["yellow"] if ending else COVER["accent"]
        y = hgt * 0.5 - 300 * k
        draw_logo(c, cx, y + 30 * k, 112 * k, COVER["logo"], COVER["bg"])
        y += 60 * k + 48 * k
        c.create_text(cx, y + 14 * k, text=content["pill"], font=font(28, True), fill=accent)
        y += 28 * k + 36 * k
        if "countdown" in content:
            c.create_text(cx, y + 12 * k, text="のこり", font=font(24), fill=COVER["muted"])
            y += 24 * k + 4 * k
            c.create_text(cx, y + 90 * k, text=content["countdown"], font=font(180, light=True),
                          fill=content["countdown_fg"])
            y += 180 * k + 28 * k
            bw, bh = 520 * k, max(2, 4 * k)
            self._round_rect(cx - bw / 2, y, cx + bw / 2, y + bh, COVER["track"])
            if content["progress"] > 0.01:
                self._round_rect(cx - bw / 2, y, cx - bw / 2 + bw * content["progress"], y + bh, accent)
            y += bh + 72 * k
        else:
            # 文字は中央に固定し、点だけを右に足す (点が増えても文字が左右に動かないように)
            words = content["big"].rstrip(".")
            item = c.create_text(cx, y + 30 * k, text=words, font=font(60, light=True), fill=COVER["text"])
            c.create_text(c.bbox(item)[2], y + 30 * k, text="." * (len(content["big"]) - len(words)),
                          font=font(60, light=True), fill=COVER["text"], anchor="w")
            y += 60 * k + 56 * k
        first, second = content["lines"]
        if first:
            c.create_text(cx, y + 16 * k, text=first, font=font(32), fill=COVER["text"])
            y += 32 * k * 1.5
        if second:
            c.create_text(cx, y + 13 * k, text=second, font=font(24), fill=COVER["muted"])
        c.create_text(cx, hgt - 64 * k, text=content["footer"], font=font(28), fill=COVER["foot"])

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
    """Geaspec Agent のウィンドウ。はじめての設定も、ホームも、すべてこの 1 つのウィンドウの中で行う。"""

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
        try:
            root.iconbitmap(default=str(tray.ICON_PATH))  # ウィンドウとタスクバーに Geaspec のロゴを出す (Windows)
        except tk.TclError:
            pass
        # 高さは中身に合わせる (たたんだり開いたりすると、ウィンドウもそれに合わせて伸び縮みする)
        root.minsize(600, 300)
        root.configure(bg=COLORS["bg"])
        family = pick_font_family(root)
        self.font = (family, 10)
        self.font_small = (family, 9)
        self.font_bold = (family, 10, "bold")
        self.font_title = (family, 16, "bold")
        self.font_brand = (family, 13, "bold")

        # ボタンや入力欄も Windows の標準の見た目ではなく、サイトと同じ色にそろえる (clam は色を変えられる)
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        flat = {"borderwidth": 1, "relief": "flat", "focuscolor": COLORS["mint"]}
        style.configure("TButton", font=self.font, padding=(12, 6), background=COLORS["card"], foreground=COLORS["ink"],
                        bordercolor="#c9d0d1", lightcolor=COLORS["card"], darkcolor=COLORS["card"], **flat)
        style.map("TButton", background=[("disabled", COLORS["bg"]), ("active", COLORS["hover"])],
                  foreground=[("disabled", COLORS["muted"])], bordercolor=[("focus", COLORS["mint"])])
        for name, size, pad in (("Primary.TButton", 10, (14, 6)), ("Big.TButton", 12, (20, 10))):
            style.configure(name, font=(family, size, "bold"), padding=pad, background=COLORS["mint"],
                            foreground=COLORS["on_mint"], bordercolor=COLORS["mint"], lightcolor=COLORS["mint"],
                            darkcolor=COLORS["mint"])
            style.map(name, background=[("disabled", COLORS["line"]), ("active", "#86c0ec")],
                      bordercolor=[("active", "#86c0ec")], foreground=[("disabled", COLORS["muted"])])
        # 文字だけのボタン (補助の操作)
        style.configure("Link.TButton", font=self.font, padding=(6, 4), background=COLORS["bg"],
                        foreground=COLORS["primary"], bordercolor=COLORS["bg"], lightcolor=COLORS["bg"],
                        darkcolor=COLORS["bg"], relief="flat", borderwidth=0)
        style.map("Link.TButton", background=[("active", COLORS["hover"])], bordercolor=[("active", COLORS["hover"])],
                  foreground=[("disabled", COLORS["muted"])])
        style.configure("TCheckbutton", font=self.font, background=COLORS["bg"], foreground=COLORS["ink"],
                        indicatorbackground=COLORS["card"], indicatorforeground=COLORS["primary"])
        style.map("TCheckbutton", background=[("active", COLORS["bg"])])
        for name in ("TEntry", "TSpinbox"):
            style.configure(name, fieldbackground=COLORS["card"], foreground=COLORS["ink"], bordercolor=COLORS["line"],
                            lightcolor=COLORS["card"], darkcolor=COLORS["card"], arrowcolor=COLORS["muted"],
                            background=COLORS["card"], padding=4)
            style.map(name, bordercolor=[("focus", COLORS["mint"])], lightcolor=[("focus", COLORS["mint"])])
        style.configure("TScrollbar", background=COLORS["line"], troughcolor=COLORS["bg"], bordercolor=COLORS["bg"],
                        lightcolor=COLORS["line"], darkcolor=COLORS["line"], arrowcolor=COLORS["muted"], relief="flat")
        style.configure("TMenubutton", font=self.font, padding=(10, 4), background=COLORS["bg"],
                        foreground=COLORS["primary"], bordercolor=COLORS["bg"], lightcolor=COLORS["bg"],
                        darkcolor=COLORS["bg"], arrowcolor=COLORS["primary"], relief="flat")
        style.map("TMenubutton", background=[("active", COLORS["hover"])])

        header = tk.Frame(root, bg=COLORS["bg"])
        header.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(header, text="Geaspec", font=self.font_brand, fg=COLORS["ink"], bg=COLORS["bg"]).pack(side="left")
        # Geaspec VR の貸し手のアプリだとわかるように、サイトと同じ「VR」の印を付ける
        tk.Label(header, text=" VR ", font=(family, 8, "bold"), fg=COLORS["on_mint"], bg=COLORS["mint"]).pack(
            side="left", padx=(6, 0))

        # ここに「はじめての設定」か「ホーム（時刻・次の貸し出し・準備のチェック）」のどちらかを入れる
        self.content = tk.Frame(root, bg=COLORS["bg"])
        self.content.pack(fill="both", expand=True)

        # 動きの記録は、画面に出していないときも集めておく
        self.log_lines: list[str] = []

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.cover = CoverWindow(self)
        self.seen_new_bookings = h.STATUS.new_bookings
        # 画面右下 (通知領域) のアイコン。ふだんはウィンドウを閉じて、ここで動き続ける
        self.tray = tray.Tray()
        self.told_about_tray = False
        if sys.platform == "win32":
            self.tray.start()
        self.show_start_page()
        place_center(root)  # 左上ではなく、画面の真ん中に出す
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
        self.handle_tray()
        if h.STATUS.new_bookings != self.seen_new_bookings:
            self.seen_new_bookings = h.STATUS.new_bookings
            if self.hidden():
                self.tray.notify("新しい予約が入りました。時間になると自動で準備します")
            else:
                self.bring_to_front()
        self.drain_logs()
        if self.closing and not self.c.running():
            self.tray.stop()
            self.root.destroy()
            return
        self.root.after(500, self.tick)

    # --- 通知領域のアイコン -------------------------------------------------

    def handle_tray(self) -> None:
        title, _detail, tone = self.c.status_text()
        self.tray.update(title, tone)
        for action in self.tray.poll():
            if action == "web":
                self.open_lend_page()
            elif action == "show":
                self.show_window()
            elif action == "log":
                self.show_window()
                if self.home is not None and not self.home.showing_log():
                    self.home.toggle_log()
            elif action == "quit":
                self.show_window()
                self.quit_app()

    def hidden(self) -> bool:
        try:
            return self.root.state() == "withdrawn"
        except self.tk.TclError:
            return False

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.told_about_tray:
            self.told_about_tray = True
            self.tray.notify("Geaspec は画面右下のアイコンで動き続けます。終了するときは、アイコンを右クリック →「終了」")

    def show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except self.tk.TclError:
            pass

    def open_lend_page(self) -> None:
        """貸し出す時間は Web サイトで決める (ログインしていれば、そのまま貸し出しページが開く)。"""
        import webbrowser

        if self.c.server:
            webbrowser.open(f"{self.c.server}/lend#schedule")

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
        """ウィンドウの × は、アイコンだけにする (準備が終わっていれば)。終了はアイコンのメニューから。"""
        if self.closing:
            return
        if self.tray.active and self.home is not None:
            self.hide_to_tray()
            return
        self.quit_app()

    def quit_app(self) -> None:
        from tkinter import messagebox

        if self.closing:
            return
        if self.c.in_session():
            message = "利用中です。終了すると今のセッションを終了し、PC のデータを削除します。\n終了しますか？"
        elif self.c.running():
            message = "Agent を終了すると、予約を受け付けなくなります。\n終了しますか？"
        else:
            self.tray.stop()
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
            "アカウントがまだない人は、「アカウントを作る」から作れます（Geaspec で PC を借りるときのアカウントがあれば、それで大丈夫です）。",
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
        self.login_button = ttk.Button(buttons, text="ログイン", style="Primary.TButton", command=self.login)
        self.login_button.pack(side="left")
        ttk.Button(buttons, text="アカウントを作る", command=self.signup).pack(side="left", padx=10)
        ttk.Button(buttons, text="詳しい設定", style="Link.TButton",
                   command=lambda: SettingsDialog(self.w)).pack(side="right")
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
        admin = h.is_admin_account()
        self.heading(
            f"このユーザー（{user}）は貸し出しに使えません" if admin else f"Windows ユーザー「{user}」を貸し出し専用にします",
            "借りた人のデータを残さないよう、利用が終わるたびに、このユーザーの次のものを消します（元に戻せません）。",
        )
        for line in ("VRChat のログイン情報と設定", "VRChat のキャッシュと一時ファイル",
                     "VRChat で撮った写真（借りた人に届けてから消します）"):
            tk.Label(self.body, text=f"・{line}", font=self.w.font, fg=COLORS["ink"], bg=COLORS["bg"],
                     anchor="w").pack(fill="x", padx=(8, 0))
        link = tk.Label(self.body, text="消す場所をくわしく見る", font=self.w.font_small, fg=COLORS["primary"],
                        bg=COLORS["bg"], cursor="hand2", anchor="w")
        link.pack(fill="x", padx=(8, 0), pady=(2, 8))
        link.bind("<Button-1>", lambda e: self.w.show_targets())
        if admin is not None:
            kind = tk.Frame(self.body, bg=COLORS["bg"])
            kind.pack(fill="x", pady=(10, 0))
            tk.Label(kind, text="このユーザーの種類：", font=self.w.font, fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left")
            tk.Label(kind, text="管理者 ⚠" if admin else "標準ユーザー ✓", font=self.w.font_bold,
                     fg=COLORS["warn" if admin else "ok"], bg=COLORS["bg"]).pack(side="left")
            if admin:
                # 管理者のユーザーでは貸し出させない (借りる人が、ほかのユーザーのファイルも見られてしまうため)
                tk.Label(self.body, text="ふだん使っているユーザーとは別に、貸し出し用の「標準ユーザー」を作り、"
                         "そこで Agent を入れてください。", font=self.w.font, fg=COLORS["ink"], bg=COLORS["bg"],
                         anchor="w", justify="left", wraplength=560).pack(fill="x", pady=(2, 0))
                buttons = tk.Frame(self.body, bg=COLORS["bg"])
                buttons.pack(fill="x", pady=(14, 0))
                ttk.Button(buttons, text="やり方を見る", style="Primary.TButton",
                           command=lambda: open_user_settings(self.w.root)).pack(side="left")
                ttk.Button(buttons, text="アプリを閉じる", command=self.w.quit_app).pack(side="left", padx=10)
                return
        warning = self.w.c.lending_warning()
        if warning:
            tk.Label(self.body, text=f"⚠ {warning}", font=self.w.font, fg=COLORS["warn"], bg=COLORS["bg"],
                     anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(10, 0))
        self.agree = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.body, text=f"「{user}」は貸し出し専用です。上のデータを毎回削除してかまいません",
            variable=self.agree, command=self.toggle_start,
        ).pack(anchor="w", pady=(12, 0))
        buttons = tk.Frame(self.body, bg=COLORS["bg"])
        buttons.pack(fill="x", pady=(14, 0))
        self.start_button = ttk.Button(buttons, text="登録してはじめる", style="Primary.TButton", command=self.register)
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
    """ホーム: いまどうなっているか (1 枚)、準備のチェック、利用中の守り だけを出す。

    貸し出す時間を決めるのは Web サイト (このアプリではしない)。同じことを 2 か所に書かない。
    """

    MARKS = {"ok": ("✓", "ok"), "ng": ("!", "bad"), "wait": ("…", "muted")}
    GUARDS = (
        "キーボードとマウスの操作を止めます（VR の操作はそのまま使えます）",
        "画面を幕でかくして、パソコンの中を見せません",
        "終わったら VRChat から自動でログアウトして、借りた人のデータを消します",
        "借りる人は、標準ユーザーの外（あなたのファイル）には入れません",
        "VR の中の Steam のメニューを止めます。VRChat 以外のゲームが起動されたら閉じます",
        "PC のスピーカーとマイクを消音し、借りた人には VRChat の音だけを届けます",
    )

    def __init__(self, window: AgentWindow) -> None:
        tk = window.tk
        from tkinter import ttk

        self.w = window
        frame = self.frame = tk.Frame(window.content, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True)
        self.middle = tk.Frame(frame, bg=COLORS["bg"])
        self.middle.pack(fill="both", expand=True)
        self.main = tk.Frame(self.middle, bg=COLORS["bg"])
        self.main.pack(fill="both", expand=True)

        # --- いまの状態 (1 枚だけ) ---
        hero = self.card(self.main)
        top = tk.Frame(hero, bg=COLORS["card"])
        top.pack(fill="x", padx=18, pady=(14, 0))
        self.kicker = tk.Label(top, text="", font=window.font_small, fg=COLORS["muted"], bg=COLORS["card"])
        self.kicker.pack(side="left")
        light = pick_light_family(window.root, window.font[0])
        self.clock = tk.Label(top, text="", font=(light, 11), fg=COLORS["muted"], bg=COLORS["card"])
        self.clock.pack(side="right")
        row = tk.Frame(hero, bg=COLORS["card"])
        row.pack(fill="x", padx=18, pady=(2, 0))
        self.dot = tk.Canvas(row, width=14, height=14, bg=COLORS["card"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self.dot_item = self.dot.create_oval(2, 2, 12, 12, fill=COLORS["warn"], outline="")
        self.headline = tk.Label(row, text="", font=(window.font[0], 16, "bold"), fg=COLORS["ink"], bg=COLORS["card"],
                                 anchor="w")
        self.headline.pack(side="left", fill="x")
        self.sub = tk.Label(hero, text="", font=window.font, fg=COLORS["muted"], bg=COLORS["card"], anchor="w",
                            justify="left", wraplength=520)
        self.sub.pack(fill="x", padx=(40, 18), pady=(2, 16))

        # --- 準備 (自動で確かめる)。全部できていれば 1 行にたたむ。貸し出し中は出さない ---
        checks = self.checks_card = self.card(self.main)
        self.check_head = tk.Label(checks, text="", font=window.font, fg=COLORS["ink"], bg=COLORS["card"], anchor="w",
                                   cursor="hand2")
        self.check_head.pack(fill="x", padx=18, pady=(12, 12))
        self.check_head.bind("<Button-1>", lambda e: self.toggle_checks())
        self.check_open: bool | None = None  # None: 自動 (問題があるときだけ開く)
        self.check_body = tk.Frame(checks, bg=COLORS["card"])
        self.check_rows: list[tuple] = []
        for _ in range(len(window.c.readiness())):
            r = tk.Frame(self.check_body, bg=COLORS["card"])
            r.pack(fill="x", padx=18, pady=1)
            mark = tk.Label(r, text="", width=2, font=window.font_bold, bg=COLORS["card"])
            mark.pack(side="left")
            name = tk.Label(r, text="", font=window.font, fg=COLORS["ink"], bg=COLORS["card"], anchor="w")
            name.pack(side="left")
            hint = tk.Label(r, text="", font=window.font_small, fg=COLORS["bad"], bg=COLORS["card"], anchor="e")
            hint.pack(side="right")
            self.check_rows.append((mark, name, hint))
        tk.Frame(self.check_body, height=12, bg=COLORS["card"]).pack()

        # --- 貸し出し中の守り (いつも自動で行う約束なので、チェックとは別の印。ふだんはたたむ) ---
        guard = self.guard_card = self.card(self.main)
        self.guard_head = tk.Label(guard, text="", font=window.font, fg=COLORS["primary"], bg=COLORS["card"],
                                   anchor="w", cursor="hand2")
        self.guard_head.pack(fill="x", padx=18, pady=(12, 12))
        self.guard_head.bind("<Button-1>", lambda e: self.toggle_guard())
        self.guard_body = tk.Frame(guard, bg=COLORS["card"])
        for text in self.GUARDS:
            tk.Label(self.guard_body, text=f"・{text}", font=window.font, fg=COLORS["ink"], bg=COLORS["card"],
                     anchor="w").pack(fill="x", padx=(26, 18), pady=1)
        tk.Frame(self.guard_body, height=12, bg=COLORS["card"]).pack()
        self.guard_open = False
        self.show_guard()

        # --- 動きの記録 (同じ場所に切り替えて出す) ---
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

        # --- 操作: 貸し出す時間は Web サイトで決める。ふだん使わないものは「その他」 ---
        actions = tk.Frame(frame, bg=COLORS["bg"])
        actions.pack(fill="x", padx=18, pady=(2, 14))
        self.web_button = ttk.Button(actions, text="", style="Primary.TButton", command=window.open_lend_page)
        self.web_button.pack(side="left")
        more = ttk.Menubutton(actions, text="その他", width=6)
        self.menu = tk.Menu(more, tearoff=False, font=window.font, bg=COLORS["card"], fg=COLORS["ink"],
                            activebackground=COLORS["hover"], activeforeground=COLORS["ink"], relief="flat", bd=1)
        self.menu.add_command(label="動きの記録", command=self.toggle_log)
        self.log_index = self.menu.index("end")
        self.menu.add_command(label="消すものを確認", command=window.show_targets)
        self.menu.add_command(label="今すぐ削除", command=window.cleanup_now)
        self.cleanup_index = self.menu.index("end")
        self.menu.add_command(label="ログインし直す", command=self.relogin)
        self.menu.add_separator()
        self.menu.add_command(label=f"PC ユーザー: {h.current_windows_user()}", state="disabled")
        self.menu.add_command(label=f"バージョン {h.AGENT_VERSION}", state="disabled")
        more["menu"] = self.menu
        more.pack(side="right")
        if not window.tray.active:  # アイコンで動き続けられないときだけ、閉じると止まることを書いておく
            tk.Label(frame, text="ウィンドウを閉じると Agent も終了し、予約を受け付けなくなります。",
                     font=window.font_small, fg=COLORS["muted"], bg=COLORS["bg"], anchor="w").pack(
                fill="x", padx=18, pady=(0, 10))
        self.update_status()

    def card(self, parent, title: str = ""):
        tk = self.w.tk
        box = tk.Frame(parent, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        box.pack(fill="x", padx=18, pady=(0, 12))
        if title:
            tk.Label(box, text=title, font=self.w.font_small, fg=COLORS["muted"], bg=COLORS["card"], anchor="w").pack(
                fill="x", padx=18, pady=(12, 6))
        return box

    def update_status(self) -> None:
        c = self.w.c
        now = dt.datetime.now()
        items = c.readiness()
        kicker, headline, sub, tone = c.home_summary(items, now)
        self.kicker.configure(text=kicker)
        self.clock.configure(text=f"現在 {day_label(now)} {now:%H:%M:%S}")
        self.headline.configure(text=headline)
        self.sub.configure(text=sub)
        self.dot.itemconfigure(self.dot_item, fill=COLORS.get(tone, COLORS["muted"]))
        self.menu.entryconfigure(self.cleanup_index, state="disabled" if c.in_session() else "normal")
        busy = h.STATUS.state in ("session", "cleaning")
        self.web_button.configure(text="予約を Web で見る" if busy else "貸し出す時間を決める（Web サイト）",
                                  style="TButton" if any(state == "ng" for _, state, _ in items) else "Primary.TButton")
        if busy == bool(self.checks_card.winfo_ismapped()):
            if busy:
                self.checks_card.pack_forget()
            else:
                self.checks_card.pack(fill="x", padx=18, pady=(0, 12), before=self.guard_card)
        for (mark, name, hint), (label, state, tip) in zip(self.check_rows, items):
            symbol, color = self.MARKS[state]
            mark.configure(text=symbol, fg=COLORS[color])
            name.configure(text=label)
            hint.configure(text="" if state == "ok" else tip, fg=COLORS["bad" if state == "ng" else "muted"])
            action = self.fix_action(label) if state == "ng" else None
            hint.configure(cursor="hand2" if action else "", font=self.w.font_small + (("underline",) if action else ()))
            hint.unbind("<Button-1>")
            if action:
                hint.bind("<Button-1>", lambda e, run=action: run())
        all_ok = all(state == "ok" for _, state, _ in items)
        opened = (not all_ok) if self.check_open is None else self.check_open
        arrow = "▾" if opened else "▸"
        if all_ok:
            self.check_head.configure(text=f"{arrow} 貸し出しの準備：すべて OK（{len(items)} 項目）", fg=COLORS["ok"])
        else:
            ng = sum(state == "ng" for _, state, _ in items)
            wait = sum(state == "wait" for _, state, _ in items)
            parts = ([f"要対応 {ng} 件"] if ng else []) + ([f"確認中 {wait} 件"] if wait else [])
            self.check_head.configure(text=f"{arrow} 貸し出しの準備：{'・'.join(parts)}",
                                      fg=COLORS["bad"] if ng else COLORS["ink"])
        if opened != self.check_body.winfo_ismapped():
            if opened:
                self.check_body.pack(fill="x")
            else:
                self.check_body.pack_forget()

    def fix_action(self, label: str):
        """その行で直せるもの (押すと開く)。"""
        if label.startswith("貸し出し用の標準ユーザー"):
            return lambda: open_user_settings(self.w.root)
        if label.startswith("貸し出し専用の Steam"):
            return lambda: open_steam_account(self.w.root, self.w.c, self.update_status)
        if label.startswith("VRChat"):
            return lambda: open_uri("steam://store/438100")
        return None

    def toggle_checks(self) -> None:
        shown = bool(self.check_body.winfo_ismapped())
        self.check_open = not shown
        self.update_status()

    def show_guard(self) -> None:
        self.guard_head.configure(text=("▾ " if self.guard_open else "▸ ") + "貸し出し中のあなたの PC の守り方")
        if self.guard_open:
            self.guard_body.pack(fill="x")
        else:
            self.guard_body.pack_forget()

    def toggle_guard(self) -> None:
        self.guard_open = not self.guard_open
        self.show_guard()

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

    def showing_log(self) -> bool:
        return bool(self.log_frame.winfo_ismapped())

    def toggle_log(self) -> None:
        """ホームと動きの記録を切り替える (ウィンドウは増やさない)。"""
        if self.log_frame.winfo_ismapped():
            self.log_frame.pack_forget()
            self.main.pack(fill="both", expand=True)
            self.menu.entryconfigure(self.log_index, label="動きの記録")
        else:
            self.main.pack_forget()
            self.log_frame.pack(fill="both", expand=True, padx=18, pady=(0, 4))
            self.menu.entryconfigure(self.log_index, label="ホームに戻る")
            self.log_text.see("end")

    def relogin(self) -> None:
        from tkinter import messagebox

        if self.w.c.in_session():
            messagebox.showinfo("ログインし直す", "利用中はログインし直せません。終わってからもう一度押してください。", parent=self.w.root)
            return
        self.w.show_setup(step=0)


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
        place_center(top, over=window.root)
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


URL_SCHEME_KEY = r"Software\Classes\geaspec"


def url_protocol_command(executable: str, script: str) -> str:
    """geaspec://open で開くときのコマンド。黒い窓が出ないよう、あれば pythonw.exe を使う。"""
    exe = Path(executable)
    windowed = exe.with_name("pythonw.exe")
    if exe.name.lower() == "python.exe" and windowed.exists():
        exe = windowed
    return f'"{exe}" "{script}" "%1"'


def register_url_protocol() -> None:
    """説明ページの「アプリに戻る」(geaspec://open) で開けるようにする。

    インストーラーも登録するが、zip で入れた人や古い版から上げた人のために、起動するたびに今の場所で登録し直す。
    """
    if sys.platform != "win32":
        return
    import winreg

    command = url_protocol_command(sys.executable, str(Path(__file__).resolve()))
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, URL_SCHEME_KEY) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Geaspec")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, URL_SCHEME_KEY + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except OSError as exc:
        h.log.warning("geaspec:// の登録に失敗しました: %s", exc)


def find_agent_window() -> int:
    """開いている Agent のウィンドウを探す。版が違っても見つかるよう、タイトルの頭で探す。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        return hwnd
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle, _lparam):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(handle, buffer, 256)
        if buffer.value.startswith("Geaspec Agent ") and user32.IsWindowVisible(handle):
            found.append(handle)
            return False
        return True

    user32.EnumWindows(visit, 0)
    if found:
        return found[0]
    hidden: list[int] = []  # アイコンだけで動いているとき (ウィンドウを隠している)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit_hidden(handle, _lparam):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(handle, buffer, 256)
        if buffer.value.startswith("Geaspec Agent "):
            hidden.append(handle)
            return False
        return True

    user32.EnumWindows(visit_hidden, 0)
    return hidden[0] if hidden else 0


def bring_existing_window_to_front() -> bool:
    """もう開いているウィンドウを前に出す。説明ページの「アプリに戻る」(geaspec://open) から開かれたときに使う。

    Windows はほかのアプリが勝手に前へ出るのを止めるので、いま前にあるウィンドウの入力に一時的に相乗りしてから前に出す。
    """
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = find_agent_window()
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
    current = kernel32.GetCurrentThreadId()
    foreground = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(foreground) and foreground != current and bool(user32.AttachThreadInput(foreground, current, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(foreground, current, False)
    if user32.GetForegroundWindow() != hwnd:
        user32.FlashWindow(hwnd, True)  # それでも前に出せなければ、タスクバーで光らせて知らせる
    return True


def main() -> int:
    if not acquire_single_instance():
        if bring_existing_window_to_front():
            return 0
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Geaspec Agent", "Geaspec Agent はすでに起動しています。画面右下のアイコンを確認してください。")
        return 0
    if sys.platform == "win32":
        try:  # 高解像度の画面で文字がぼやけないようにする
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    controller = AgentController()
    h.setup_logging(controller.home, dry_run=False)
    register_url_protocol()
    log_queue: "queue.Queue[str]" = queue.Queue()
    h.log.addHandler(QueueLogHandler(log_queue))
    h.log.info("Geaspec Agent %s を起動しました", h.AGENT_VERSION)
    window = AgentWindow(controller, log_queue)
    # 準備が終わっていれば、ウィンドウは出さずにアイコンだけで動く (「アプリに戻る」から開いたときは出す)
    opened_by_link = any(arg.startswith("geaspec:") for arg in sys.argv[1:])
    if window.tray.active and window.home is not None and not opened_by_link:
        window.root.withdraw()
    window.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
