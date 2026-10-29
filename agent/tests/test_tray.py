"""画面右下 (通知領域) のアイコンのテスト。本物の pystray の代わりに、まねをする部品を使う。"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tray  # noqa: E402


class FakeIcon:
    def __init__(self, name, icon, title, menu):
        self.icon, self.title, self.menu = icon, title, menu
        self.notes, self.menu_updates, self.ran, self.stopped = [], 0, False, False

    def run(self):
        self.ran = True

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title):
        self.notes.append((title, message))

    def stop(self):
        self.stopped = True


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return super().__new__(cls, items)


class FakeItem:
    def __init__(self, text, action, enabled=True, default=False):
        self.text, self.action, self.enabled, self.default = text, action, enabled, default


def fake_pystray():
    return types.SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeItem)


class TrayTest(unittest.TestCase):
    def test_menu_actions_reach_the_window_thread(self):
        t = tray.Tray()
        with mock.patch.dict(sys.modules, {"pystray": fake_pystray()}):
            self.assertTrue(t.start())
        items = [i for i in t.icon.menu if isinstance(i, FakeItem)]
        labels = [i.text if isinstance(i.text, str) else i.text(None) for i in items]
        self.assertEqual(labels, ["起動しています…", "貸し出す時間を決める（Web サイト）", "Geaspec を開く", "動きの記録", "終了"])
        self.assertTrue(next(i for i in items if i.text == "Geaspec を開く").default)  # アイコンをクリックで開く
        for item in items[1:]:
            item.action()
        self.assertEqual(t.poll(), ["web", "show", "log", "quit"])
        self.assertEqual(t.poll(), [])

    def test_status_changes_update_the_icon_once(self):
        t = tray.Tray()
        with mock.patch.dict(sys.modules, {"pystray": fake_pystray()}):
            t.start()
        t.update("待機中", "ok")
        t.update("待機中", "ok")  # 同じなら何もしない
        self.assertEqual(t.icon.title, "Geaspec: 待機中")
        self.assertEqual(t.icon.menu_updates, 1)
        self.assertEqual(t.icon.menu[0].text(None), "待機中")
        t.notify("新しい予約が入りました")
        self.assertEqual(t.icon.notes, [("Geaspec", "新しい予約が入りました")])
        t.stop()
        self.assertTrue(t.icon.stopped)

    def test_without_pystray_the_app_still_runs(self):
        t = tray.Tray()
        with mock.patch.dict(sys.modules, {"pystray": None}):
            self.assertFalse(t.start())
        self.assertFalse(t.active)
        t.update("待機中", "ok")
        t.notify("x")
        self.assertEqual(t.poll(), [])

    def test_icon_image_has_the_state_color(self):
        img = tray.icon_image("bad")
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.getpixel((53, 53))[:3], (0xC4, 0x1F, 0x4B))

    def test_icon_image_uses_the_logo(self):
        from pathlib import Path
        from unittest import mock

        logo = Path(__file__).resolve().parents[1] / "packaging" / "geaspec.ico"
        with mock.patch.object(tray, "ICON_PATH", logo):
            img = tray.icon_image("ok")
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.getpixel((12, 12))[:3], (255, 255, 255))  # ロゴの白い背景
        self.assertEqual(img.getpixel((53, 53))[:3], (0x13, 0xA0, 0x7A))  # 状態の点はそのまま


if __name__ == "__main__":
    unittest.main()
