"""通知領域 (画面右下の小さなアイコン) に Geaspec を置く。

Geaspec Agent はふだんウィンドウを出さず、ここで静かに動き続ける (Tailscale と同じ形)。
右クリックのメニューから、Web サイトで貸し出す時間を決めたり、ウィンドウを開いたり、終了したりする。
メニューの操作はアイコンのスレッドで届くので、キューに入れて画面のスレッド (tkinter) で処理する。
"""

from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger("geaspec.tray")

# 状態の色 (画面の色と同じ)
TONE_COLORS = {"ok": "#13a07a", "busy": "#5b3cf0", "warn": "#c77700", "bad": "#c41f4b"}


def icon_image(tone: str = "ok", size: int = 64):
    """Geaspec の紫の丸に、状態の色の点を付けたアイコン。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 3, size - 3), fill="#5b3cf0")
    # 歯車のかわりの白い輪 (Geaspec のロゴの雰囲気)
    m = size // 4
    d.ellipse((m, m, size - m - 1, size - m - 1), outline="white", width=max(3, size // 10))
    dot = size // 3
    d.ellipse((size - dot - 1, size - dot - 1, size - 1, size - 1), fill=TONE_COLORS.get(tone, TONE_COLORS["warn"]),
              outline="white", width=max(2, size // 32))
    return img


class Tray:
    """通知領域のアイコン。pystray が使えない環境 (開発用の Linux など) では何もしない。"""

    ACTIONS = ("show", "web", "log", "quit")

    def __init__(self) -> None:
        self.actions: "queue.Queue[str]" = queue.Queue()
        self.icon = None
        self.status = "起動しています…"
        self.tone = ""

    @property
    def active(self) -> bool:
        return self.icon is not None

    def start(self) -> bool:
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001 - 入っていなければウィンドウだけで動かす
            log.info("通知領域のアイコンは使いません: %s", exc)
            return False
        item, menu = pystray.MenuItem, pystray.Menu
        self.icon = pystray.Icon(
            "Geaspec", icon_image("warn"), "Geaspec",
            menu(
                item(lambda _: self.status, None, enabled=False),
                menu.SEPARATOR,
                item("貸し出す時間を決める（Web サイト）", lambda: self.actions.put("web")),
                item("Geaspec を開く", lambda: self.actions.put("show"), default=True),
                item("動きの記録", lambda: self.actions.put("log")),
                menu.SEPARATOR,
                item("終了", lambda: self.actions.put("quit")),
            ),
        )
        threading.Thread(target=self._run, name="geaspec-tray", daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            self.icon.run()
        except Exception:  # noqa: BLE001 - アイコンが出せなくても、Agent は動かし続ける
            log.exception("通知領域のアイコンを出せませんでした")
            self.icon = None
            self.actions.put("show")  # 隠したウィンドウを出し直す (アイコンがないと開けないため)

    def update(self, status: str, tone: str) -> None:
        """状態が変わったときだけ、アイコンの色・マウスを乗せたときの文字・メニューの 1 行目を変える。"""
        if not self.active or (status, tone) == (self.status, self.tone):
            return
        self.status = status
        try:
            if tone != self.tone:
                self.icon.icon = icon_image(tone)
            self.tone = tone
            self.icon.title = f"Geaspec: {status}"[:120]
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            log.debug("アイコンを更新できませんでした", exc_info=True)

    def notify(self, message: str) -> None:
        if not self.active:
            return
        try:
            self.icon.notify(message, "Geaspec")
        except Exception:  # noqa: BLE001
            log.debug("お知らせを出せませんでした", exc_info=True)

    def poll(self) -> list[str]:
        """メニューで押されたもの (画面のスレッドから呼ぶ)。"""
        done = []
        while True:
            try:
                done.append(self.actions.get_nowait())
            except queue.Empty:
                return done

    def stop(self) -> None:
        if self.active:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
