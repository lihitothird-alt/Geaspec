import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vr_timer as v  # noqa: E402


class TextTest(unittest.TestCase):
    def test_timer_text_rounds_minutes_up_and_shows_seconds_at_the_end(self):
        self.assertEqual(v.timer_text(42 * 60), "残り 42分")
        self.assertEqual(v.timer_text(41 * 60 + 1), "残り 42分")
        self.assertEqual(v.timer_text(59.6), "残り 59秒")
        self.assertEqual(v.timer_text(-3), "残り 0秒")

    def test_notices_at_10_5_1_minutes_once_each(self):
        shown: set[int] = set()
        self.assertIsNone(v.due_notice(11 * 60, shown))
        self.assertEqual(v.due_notice(600, shown), 600)
        self.assertIsNone(v.due_notice(590, shown))
        self.assertEqual(v.due_notice(299, shown), 300)
        self.assertEqual(v.due_notice(60, shown), 60)
        self.assertEqual(v.notice_text(300), "あと 5分で終了します")

    def test_notices_already_past_at_start_are_skipped(self):
        shown: set[int] = set()
        self.assertIsNone(v.due_notice(3 * 60, shown))  # 10分・5分は出さない
        self.assertEqual(v.due_notice(60, shown), 60)

    def test_render_makes_a_transparent_png(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = v.render("残り 42分", v.WATCH_SIZE, Path(tmp) / "w.png")
            with Image.open(path) as img:  # 閉じないと Windows では一時フォルダを消せない
                self.assertEqual(img.size, v.WATCH_SIZE)
                self.assertEqual(img.mode, "RGBA")


class FakeOverlay:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            return len(self.calls) if name == "createOverlay" else None
        return call


def fake_openvr(overlay):
    m = types.SimpleNamespace(
        VRApplication_Overlay=2, k_unTrackedDeviceIndex_Hmd=0, k_unTrackedDeviceIndexInvalid=0xFFFFFFFF,
        TrackedControllerRole_LeftHand=1,
    )

    class Matrix:
        def __init__(self):
            self.m = [[0.0] * 4 for _ in range(3)]

    system = types.SimpleNamespace(getTrackedDeviceIndexForControllerRole=lambda role: 3)
    m.HmdMatrix34_t = Matrix
    m.init = mock.Mock(return_value=system)
    m.shutdown = mock.Mock()
    m.VROverlay = lambda: overlay
    return m


class OverlayTest(unittest.TestCase):
    def test_shows_watch_on_left_hand_and_cleans_up(self):
        overlay = FakeOverlay()
        ovr = fake_openvr(overlay)
        now = {"t": 0.0}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"openvr": ovr}):
            timer = v.VrTimer(deadline=601.0, folder=Path(tmp), clock=lambda: now["t"])
            timer.folder.mkdir(exist_ok=True)

            real_stop = timer.stop_event

            def tick(_seconds):
                now["t"] += 2
                if now["t"] >= 6:
                    real_stop.set()
                return real_stop.is_set()
            timer.stop_event = types.SimpleNamespace(is_set=real_stop.is_set, wait=tick, set=real_stop.set)
            timer._show_until_stopped()
        names = [c[0] for c in overlay.calls]
        self.assertEqual(names.count("createOverlay"), 2)
        attach = [c for c in overlay.calls if c[0] == "setOverlayTransformTrackedDeviceRelative"]
        self.assertEqual(attach[-1][1][1], 3)  # 左手のコントローラー
        self.assertIn("setOverlayFromFile", names)
        self.assertEqual(names.count("destroyOverlay"), 2)
        ovr.shutdown.assert_called_once()
        self.assertEqual(timer.shown, {600})  # 残り 10 分のお知らせを出した

    def test_without_openvr_the_session_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"openvr": None}):
            timer = v.VrTimer(deadline=10.0, folder=Path(tmp))
            with self.assertLogs("vr_timer", level="WARNING"):
                timer._run()


class WaitForSteamVrTest(unittest.TestCase):
    def test_does_not_start_steamvr_by_itself(self):
        overlay = FakeOverlay()
        ovr = fake_openvr(overlay)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"openvr": ovr}), \
                mock.patch.object(v, "steamvr_running", return_value=False):
            timer = v.VrTimer(deadline=10 ** 9, folder=Path(tmp))
            waits = []

            def wait(seconds):
                waits.append(seconds)
                if len(waits) >= 3:
                    timer.stop_event.set()
                return timer.stop_event.is_set()
            timer.stop_event.wait = wait
            timer._run()
        ovr.init.assert_not_called()  # SteamVR が動くまで接続しない (接続すると SteamVR が起動してしまう)
        self.assertEqual(waits, [5, 5, 5])
