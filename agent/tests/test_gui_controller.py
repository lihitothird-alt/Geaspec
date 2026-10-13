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
        with mock.patch.object(h.ServerClient, "poll", return_value=None):
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
        self.assertEqual(title, "あと 1時間23分で Geass を開始します")
        self.assertIn("予約 #7 · 10/10（土） 20:00〜21:00", detail)
        self.assertEqual(tone, "busy")
        self.assertEqual(c.status_text(dt.datetime(2026, 10, 10, 19, 37))[0], "あと 23分で Geass を開始します")
        self.assertEqual(c.status_text(dt.datetime(2026, 10, 10, 19, 59, 30))[0], "まもなく Geass を開始します")
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


class ScheduleModelTest(unittest.TestCase):
    WEEK = dt.datetime(2026, 10, 10)
    NOW = dt.datetime(2026, 10, 10, 12, 30)

    def model(self):
        m = g.ScheduleModel(self.WEEK)
        m.load({"price_yen": 800, "trial_minutes": 30, "host": "PC-1", "slots": [
            server_slot(dt.datetime(2026, 10, 10, 20)),
            server_slot(dt.datetime(2026, 10, 10, 21), state="booked", editable=False),
            server_slot(dt.datetime(2026, 10, 11, 9), hours=2, state="open", editable=False),  # 別に作った 2 時間枠
            server_slot(dt.datetime(2026, 10, 11, 20), state="closed"),
        ]})
        return m

    def test_load_marks_cells(self):
        m = self.model()
        now = self.NOW
        self.assertEqual((m.price_yen, m.trial_minutes, m.host), (800, 30, "PC-1"))
        self.assertEqual(m.state(dt.datetime(2026, 10, 10, 20), now), "open")
        self.assertEqual(m.state(dt.datetime(2026, 10, 10, 21), now), "booked")
        self.assertEqual(m.state(dt.datetime(2026, 10, 11, 9), now), "fixed")
        self.assertEqual(m.state(dt.datetime(2026, 10, 11, 10), now), "fixed")
        self.assertEqual(m.state(dt.datetime(2026, 10, 11, 20), now), "empty")  # 受付をやめた枠
        self.assertEqual(m.state(dt.datetime(2026, 10, 10, 12), now), "past")
        self.assertFalse(m.dirty())

    def test_toggle_only_free_future_cells(self):
        m = self.model()
        now = self.NOW
        self.assertTrue(m.set(dt.datetime(2026, 10, 10, 22), True, now))
        self.assertFalse(m.set(dt.datetime(2026, 10, 10, 22), True, now))  # 変化なし
        self.assertFalse(m.set(dt.datetime(2026, 10, 10, 21), False, now))  # 予約あり
        self.assertFalse(m.set(dt.datetime(2026, 10, 10, 11), True, now))  # 過ぎた時間
        self.assertFalse(m.set(dt.datetime(2026, 10, 17, 11), True, now))  # 範囲外
        self.assertTrue(m.set(dt.datetime(2026, 10, 10, 20), False, now))
        self.assertTrue(m.dirty())
        self.assertEqual(m.starts_to_save(now), [dt.datetime(2026, 10, 10, 22)])
        counts = m.counts(now)
        self.assertEqual((counts["open"], counts["booked"]), (1, 1))

    def test_copy_previous_week(self):
        previous = self.model()
        week = g.ScheduleModel(self.WEEK + dt.timedelta(days=7))
        added = week.copy_from(previous, self.NOW)
        # 受付中と予約ありだった時間を、1 週間後に選ぶ (別に作った枠と、受付をやめた枠は写さない)
        self.assertEqual(added, 2)
        self.assertEqual(sorted(week.selected), [dt.datetime(2026, 10, 17, 20), dt.datetime(2026, 10, 17, 21)])

    def test_server_time_round_trip(self):
        t = dt.datetime(2026, 10, 10, 20, 0)
        self.assertEqual(h.server_time_to_local(h.local_to_server_time(t)), t)
        self.assertTrue(h.local_to_server_time(t).endswith("Z"))

    def test_until_text(self):
        self.assertEqual(g.until_text(10), "1分")
        self.assertEqual(g.until_text(83 * 60), "1時間23分")
        self.assertEqual(g.until_text(2 * 86400 + 3600), "2日1時間")


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual((c["mode"], c["pill"], c["countdown"], c["end_label"]), ("running", "Geass 利用中", "42:13", "12:42 に終了"))
        self.assertAlmostEqual(c["progress"], (42 * 60 + 13) / 3600)
        self.assertIn("ゲームはそのまま続いています", c["lines"])
        c = g.cover_content("session", self.NOW + dt.timedelta(minutes=4), 3600, self.NOW)
        self.assertEqual((c["mode"], c["pill"], c["countdown_fg"]), ("ending", "まもなく終了します", g.COVER["yellow"]))
        c = g.cover_content("cleaning", None, 3600, self.NOW, dots=2)
        self.assertEqual((c["mode"], c["big"]), ("cleaning", "データを片づけています.."))
        self.assertNotIn("countdown", c)
