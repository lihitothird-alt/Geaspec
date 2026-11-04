"""GUI の裏側 (AgentController) のテスト。画面 (tkinter) は使わない。"""

import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geaspec_gui as g  # noqa: E402
import host_agent as h  # noqa: E402


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.env = mock.patch.dict(os.environ, {"USERNAME": "geaspec"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        h.STATUS = h.AgentStatus()

    def tearDown(self):
        self.tmp.cleanup()

    def controller(self):
        return g.AgentController(config_path=self.config, home=self.root / "home")

    def test_first_run_needs_setup_and_cannot_start(self):
        c = self.controller()
        self.assertTrue(c.needs_setup())
        self.assertFalse(c.start())
        self.assertEqual(c.status_text()[0], "はじめにログインしてください")

    def test_save_settings_validates_and_writes_config(self):
        c = self.controller()
        self.assertIn("http", c.save_settings("example.com", "pcvr_0123456789", "Tailscale"))
        self.assertIn("pcvr_", c.save_settings("https://x.example", "abc", ""))
        self.assertIsNone(c.save_settings("https://x.example/", "pcvr_0123456789", "-"))
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved, {"server": "https://x.example", "token": "pcvr_0123456789"})
        self.assertFalse(c.needs_setup())

    def test_unregistered_user_is_asked_before_anything(self):
        c = self.controller()
        c.save_settings("https://x.example", "pcvr_0123456789", "")
        self.assertFalse(c.can_start())
        self.assertIn("未登録", c.status_text()[0])
        self.assertIsNotNone(c.cleanup_now())  # 登録前は削除しない
        self.assertEqual(c.register_lending_user(), "geaspec")
        self.assertTrue(c.can_start())

    def test_status_text_for_each_state(self):
        c = self.controller()
        c.save_settings("https://x.example", "pcvr_0123456789", "")
        c.register_lending_user()
        now = dt.datetime(2026, 10, 8, 21, 0, 0)
        h.STATUS.state, h.STATUS.last_poll_at = "idle", now
        self.assertEqual(c.status_text(now)[:1], ("待機中",))
        self.assertIn("21:00:00", c.status_text(now)[1])
        self.assertIn("まだ予約はありません", c.status_text(now)[1])
        h.STATUS.state, h.STATUS.booking_id = "session", 12
        h.STATUS.session_ends_at = now + dt.timedelta(minutes=42, seconds=10)
        title, detail, tone = c.status_text(now)
        self.assertEqual(title, "利用中 · 予約 #12")
        self.assertIn("残り 42:10", detail)
        self.assertEqual(tone, "busy")
        self.assertEqual(c.cleanup_now(), "利用中は削除できません")
        h.STATUS.state = "auth_error"
        self.assertEqual(c.status_text(now)[2], "bad")

    def test_start_and_stop_background_wait(self):
        c = self.controller()
        c.save_settings("http://127.0.0.1:9", "pcvr_0123456789", "-")  # 届かないサーバー
        c.register_lending_user()
        c.poll_interval = 0.05
        # 性能の送信は、サイトに届かないうちは待たされる (Windows では再送で数秒)。それで最初の確認が遅れないこと
        with mock.patch.object(h.ServerClient, "poll", return_value=None), \
                mock.patch.object(h.ServerClient, "send_specs", side_effect=lambda *_: time.sleep(5)):
            self.assertTrue(c.start())
            self.assertFalse(c.start())  # 二重には起動しない
            deadline = time.time() + 2
            while h.STATUS.state != "unreachable" and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(h.STATUS.state, "unreachable")
            c.request_stop()
            c.thread.join(timeout=2)
        self.assertFalse(c.running())
        self.assertEqual(h.STATUS.state, "stopped")

    def test_request_stop_ends_running_session(self):
        c = self.controller()
        agent = mock.Mock()
        with mock.patch.object(h, "_current_agent", agent):
            c.request_stop()
        agent.request_stop.assert_called_once_with("agent_closed")


    def test_countdown_to_next_booking(self):
        c = self.controller()
        c.save_settings("https://x.example", "pcvr_0123456789", "")
        c.register_lending_user()
        now = dt.datetime(2026, 10, 10, 18, 37, 0)
        h.STATUS.state = "idle"
        h.update_next_booking({
            "booking_id": 7,
            "starts_at": h.local_to_server_time(dt.datetime(2026, 10, 10, 20, 0)),
            "ends_at": h.local_to_server_time(dt.datetime(2026, 10, 10, 21, 0)),
        })
        title, detail, tone = c.status_text(now)
        self.assertEqual(title, "あと 1時間23分で貸し出しを開始します")
        self.assertIn("予約 #7 · 10/10（土） 20:00〜21:00", detail)
        self.assertEqual(tone, "busy")
        self.assertEqual(c.status_text(dt.datetime(2026, 10, 10, 19, 37))[0], "あと 23分で貸し出しを開始します")
        self.assertEqual(c.status_text(dt.datetime(2026, 10, 10, 19, 59, 30))[0], "まもなく貸し出しを開始します")
        # 同じ予約を何度知らされても「新しい予約」は 1 回だけ
        self.assertEqual(h.STATUS.new_bookings, 1)
        h.update_next_booking({"booking_id": 7, "starts_at": "2026-10-10T11:00:00Z", "ends_at": "2026-10-10T12:00:00Z"})
        self.assertEqual(h.STATUS.new_bookings, 1)
        h.update_next_booking(None)
        self.assertEqual(c.status_text(now)[0], "待機中")

    def test_login_registers_this_pc(self):
        c = self.controller()
        with mock.patch.object(h, "builtin_server", return_value=""):
            with self.assertRaisesRegex(ValueError, "アドレス"):
                c.login("lender@example.com", "password123")
            ok = {"token": "pcvr_0123456789", "host": "LENDER-PC", "pending": True}
            with mock.patch.object(h.ServerClient, "login", return_value=ok) as login:
                self.assertEqual(c.login(" lender@example.com ", "password123", "https://geaspec.example/"),
                                 ("LENDER-PC", True))
            email, password, pc_name, machine_id = login.call_args.args
            self.assertEqual((email, password), ("lender@example.com", "password123"))
            saved = json.loads(self.config.read_text())
            self.assertEqual(saved["server"], "https://geaspec.example")
            self.assertEqual(saved["token"], "pcvr_0123456789")
            self.assertEqual(saved["machine_id"], machine_id)
            self.assertEqual(h.STATUS.host_state, "pending")
            # ログインし直しても同じ PC の ID を送る
            with mock.patch.object(h.ServerClient, "login", return_value=ok) as login:
                c.login("lender@example.com", "password123")
            self.assertEqual(login.call_args.args[3], machine_id)
            with mock.patch.object(h.ServerClient, "login", return_value={"error": "メールアドレスかパスワードが違います"}):
                with self.assertRaisesRegex(ValueError, "パスワードが違います"):
                    c.login("lender@example.com", "nope")
            with mock.patch.object(h.ServerClient, "login", return_value=None):
                with self.assertRaisesRegex(ValueError, "つながりません"):
                    c.login("lender@example.com", "password123")

    def test_builtin_server_is_used_without_asking(self):
        c = self.controller()
        with mock.patch.object(h, "builtin_server", return_value="https://built.in"):
            self.assertEqual(c.server, "https://built.in")
            ok = {"token": "pcvr_0123456789", "host": "PC", "pending": False}
            with mock.patch.object(h.ServerClient, "login", return_value=ok):
                c.login("lender@example.com", "password123")
            self.assertNotIn("server", json.loads(self.config.read_text()))  # 組み込みのアドレスは保存しない
            self.assertFalse(c.needs_setup())

    def test_pending_pc_says_waiting_for_approval(self):
        c = self.controller()
        c.save_settings("https://x.example", "pcvr_0123456789", "")
        c.register_lending_user()
        h.STATUS.state, h.STATUS.host_state = "idle", "pending"
        self.assertEqual(c.status_text()[0], "運営の確認待ちです")
        h.STATUS.host_state = "paused"
        self.assertEqual(c.status_text()[0], "運営が受付を止めています")

    def test_advanced_settings_accept_connection_code(self):
        import base64

        c = self.controller()
        raw = json.dumps({"server": "https://geaspec.example/", "token": "pcvr_0123456789"}).encode()
        code = "geaspec1:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertIsNone(c.save_settings("", code, ""))
        self.assertEqual((c.server, c.token), ("https://geaspec.example", "pcvr_0123456789"))
        self.assertIn("途中で切れて", c.save_settings("", code[:30], ""))

def server_slot(start, hours=1, state="open", editable=True):
    return {
        "start": h.local_to_server_time(start),
        "end": h.local_to_server_time(start + dt.timedelta(hours=hours)),
        "state": state, "editable": editable, "price_yen": 800,
    }


class ReadinessTest(unittest.TestCase):
    """ホームのチェックリスト (自動で確かめる項目) と「次の貸し出し」の文。"""

    NOW = dt.datetime(2026, 11, 4, 20, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"USERNAME": "geaspec"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        h.STATUS = h.AgentStatus()

    def steam(self, with_vrchat: bool, extra_library: bool = False) -> Path:
        steam = self.root / "Steam"
        (steam / "steamapps").mkdir(parents=True)
        (steam / "steam.exe").write_text("")
        library = self.root / "Games" if extra_library else steam
        (library / "steamapps").mkdir(parents=True, exist_ok=True)
        (steam / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n "1"\n {\n  "path"  "%s"\n }\n}\n' % str(library).replace("\\", "\\\\"))
        if with_vrchat:
            (library / "steamapps" / "appmanifest_438100.acf").write_text("")
        return steam

    def test_vrchat_found_in_an_extra_steam_library(self):
        steam = self.steam(with_vrchat=True, extra_library=True)
        self.assertTrue(h.is_steam_app_installed(steam))
        self.assertFalse(h.is_steam_app_installed(self.steam_without()))
        self.assertFalse(h.is_steam_app_installed(None))

    def steam_without(self) -> Path:
        other = self.root / "Other"
        (other / "steamapps").mkdir(parents=True)
        return other

    def test_checklist_reports_each_condition(self):
        c = g.AgentController(config_path=self.root / "config.json", home=self.root / "home")
        c.save_settings("https://x.example", "pcvr_0123456789", "")
        c.register_lending_user()
        steam = self.steam(with_vrchat=False)
        with mock.patch.object(h, "steam_root", return_value=steam):
            items = {label: (state, tip) for label, state, tip in c.readiness(now=0)}
        self.assertEqual(items["Geaspec にログイン"][0], "ok")
        self.assertEqual(items["貸し出し専用の Windows ユーザー"][0], "ok")
        self.assertEqual(items["予約サイトとつながっている"][0], "wait")
        self.assertEqual(items["運営の承認"][0], "wait")
        self.assertEqual(items["Steam"][0], "ok")
        self.assertEqual(items["VRChat"], ("ng", "Steam で VRChat を入れてください"))
        self.assertEqual(items["Virtual Desktop Streamer"][0], "ng")

        h.STATUS.state, h.STATUS.last_poll_at, h.STATUS.host_state = "unreachable", self.NOW, "pending"
        (steam / "steamapps" / "appmanifest_438100.acf").write_text("")
        with mock.patch.object(h, "steam_root", return_value=steam):
            cached = {label: state for label, state, _ in c.readiness(now=10)}
            fresh = {label: state for label, state, _ in c.readiness(now=40)}
        self.assertEqual(cached["VRChat"], "ng")  # ファイルは 30 秒ごとにだけ見直す
        self.assertEqual(fresh["VRChat"], "ok")
        self.assertEqual((fresh["予約サイトとつながっている"], fresh["運営の承認"]), ("ng", "wait"))

    def test_next_lending_text(self):
        st = h.AgentStatus()
        self.assertEqual(g.next_lending_text(st, self.NOW)[0], "まだ予約はありません")
        st.state = "idle"
        st.next_starts_at, st.next_ends_at = self.NOW + dt.timedelta(hours=2, minutes=15), self.NOW + dt.timedelta(hours=4)
        when, left = g.next_lending_text(st, self.NOW)
        self.assertEqual(when, "11/04（水） 22:15〜00:00")
        self.assertTrue(left.startswith("あと 2時間15分で始まります"))
        st.state, st.session_ends_at = "session", self.NOW + dt.timedelta(minutes=30)
        self.assertEqual(g.next_lending_text(st, self.NOW)[0], "いま貸し出し中（20:30 まで）")


class CoverTest(unittest.TestCase):
    def test_desktop_is_covered_only_while_lending(self):
        self.assertTrue(g.should_cover("session"))
        self.assertTrue(g.should_cover("cleaning"))
        for state in ("idle", "stopped", "unreachable", "auth_error"):
            self.assertFalse(g.should_cover(state), state)


class LendingWarningTest(unittest.TestCase):
    def test_warns_only_when_the_user_is_linked_to_onedrive(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = g.AgentController(config_path=Path(tmp) / "config.json", home=Path(tmp))
            with mock.patch.dict(os.environ, {"OneDrive": r"C:\\Users\\me\\OneDrive"}):
                self.assertIn("OneDrive", c.lending_warning())
            env = {k: v for k, v in os.environ.items() if k not in g.h.CLOUD_SYNC_ENV}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(g.h, "cloud_sync_root_of", return_value=None):
                self.assertEqual(c.lending_warning(), "")


class CoverContentTest(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 10, 12, 0, 0)

    def test_countdown_text(self):
        self.assertEqual(g.countdown_text(42 * 60 + 13), "42:13")
        self.assertEqual(g.countdown_text(3723), "1:02:03")
        self.assertEqual(g.countdown_text(-5), "0:00")

    def test_running_then_ending_soon_then_cleaning(self):
        ends = self.NOW + dt.timedelta(minutes=42, seconds=13)
        c = g.cover_content("session", ends, 3600, self.NOW)
        self.assertEqual((c["mode"], c["pill"], c["countdown"], c["end_label"]), ("running", "Geaspec で貸し出し中", "42:13", "12:42 に終了"))
        self.assertAlmostEqual(c["progress"], (42 * 60 + 13) / 3600)
        self.assertIn("ゲームはそのまま続いています", c["lines"])
        c = g.cover_content("session", self.NOW + dt.timedelta(minutes=4), 3600, self.NOW)
        self.assertEqual((c["mode"], c["pill"], c["countdown_fg"]), ("ending", "まもなく終了します", g.COVER["yellow"]))
        c = g.cover_content("cleaning", None, 3600, self.NOW, dots=2)
        self.assertEqual((c["mode"], c["big"]), ("cleaning", "データを片づけています.."))
        self.assertNotIn("countdown", c)


class UrlProtocolTest(unittest.TestCase):
    """説明ページの「アプリに戻る」(geaspec://open) で開くコマンド。"""

    def test_prefers_pythonw_next_to_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "python.exe").write_text("")
            (folder / "pythonw.exe").write_text("")
            command = g.url_protocol_command(str(folder / "python.exe"), r"C:\Geaspec\geaspec_gui.py")
        self.assertEqual(command, f'"{folder / "pythonw.exe"}" "C:\\Geaspec\\geaspec_gui.py" "%1"')

    def test_keeps_executable_without_pythonw(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python.exe"
            exe.write_text("")
            command = g.url_protocol_command(str(exe), "geaspec_gui.py")
        self.assertEqual(command, f'"{exe}" "geaspec_gui.py" "%1"')

    def test_does_nothing_outside_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            g.register_url_protocol()
            self.assertFalse(g.bring_existing_window_to_front())
