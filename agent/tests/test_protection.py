"""利用中の守り: キーボードとマウスを止める仕組みと、管理者アカウントかどうかの確認。"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import host_agent as h  # noqa: E402


class InputBlockerTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows のフックは Windows でだけ確かめられる")
    def test_hooks_install_and_remove_on_windows(self):
        blocker = h.InputBlocker()
        self.assertTrue(blocker.start())
        self.assertTrue(blocker.active)
        self.assertTrue(blocker.start())  # 2 回目は何もしない
        blocker.stop()
        self.assertFalse(blocker.active)
        self.assertIsNone(blocker.thread)
        blocker.stop()  # 止めたあとにもう一度呼んでも大丈夫

    @unittest.skipIf(sys.platform == "win32", "Windows 以外での動き")
    def test_does_nothing_outside_windows(self):
        blocker = h.InputBlocker()
        self.assertFalse(blocker.start())
        blocker.stop()
        self.assertIsNone(h.is_admin_account())


class SessionUsesBlockerTest(unittest.TestCase):
    def test_cleanup_always_releases_input(self):
        cfg = h.AgentConfig(duration_min=1, dry_run=True, wipe_targets=[Path("/nonexistent-geaspec")])
        agent = h.HostAgent(cfg, home=Path(self.id().replace(".", "_")))
        with mock.patch.object(agent.input_blocker, "stop") as stop, \
                mock.patch.object(agent, "_terminate_all"), mock.patch.object(agent, "_wipe_all"), \
                mock.patch.object(agent, "_write_state"), mock.patch.object(agent, "_save_report"):
            agent.cleanup_session()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class SteamGuardTest(unittest.TestCase):
    """VR の中から Steam を触られないようにする仕組み。"""

    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.steam = self.tmp / "Steam"
        (self.steam / "config").mkdir(parents=True)
        self.home = self.tmp / "agent"
        self.lock = h.SteamVRLock(self.home, root_finder=lambda: self.steam)
        self.settings = self.steam / "config" / "steamvr.vrsettings"

    def read(self):
        import json

        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_menu_is_locked_during_lending_and_restored_after(self):
        import json

        original = {"dashboard": {"enableDashboard": True}, "steamvr": {"supersampleScale": 1.2}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.lock.apply(), [])
        data = self.read()
        self.assertFalse(data["dashboard"]["enableDashboard"])
        self.assertTrue(data["dashboard"]["arcadeMode"])
        self.assertFalse(data["steamvr"]["enableHomeApp"])
        self.assertEqual(data["steamvr"]["supersampleScale"], 1.2)  # ほかの設定は変えない
        self.assertTrue(self.lock.is_applied())
        # 2 回止めても、元の値 (1 回目の前) を覚えている
        self.assertEqual(self.lock.apply(), [])
        self.assertEqual(self.lock.restore(), [])
        self.assertEqual(self.read(), original)
        self.assertFalse(self.lock.backup.exists())
        self.assertEqual(self.lock.restore(), [])  # 戻したあとにもう一度呼んでも何もしない

    def test_restore_after_a_crash_and_when_the_file_did_not_exist(self):
        self.assertEqual(self.lock.apply(), [])
        self.assertTrue(self.lock.is_applied())
        # アプリが落ちて、次に起動したアプリが戻す
        again = h.SteamVRLock(self.home, root_finder=lambda: self.steam)
        self.assertEqual(again.restore(), [])
        self.assertEqual(self.read(), {})

    def test_no_steam_means_the_lock_fails(self):
        lock = h.SteamVRLock(self.home, root_finder=lambda: None)
        self.assertTrue(lock.apply())
        self.assertFalse(lock.is_applied())

    def test_lending_does_not_start_when_the_menu_cannot_be_locked(self):
        cfg = h.AgentConfig(duration_min=1, wipe_targets=[self.tmp / "wipe"], launch_delay_sec=0)
        agent = h.HostAgent(cfg, home=self.home)
        agent.steamvr_lock = h.SteamVRLock(self.home, root_finder=lambda: None)
        with mock.patch.object(h.HostAgent, "_guards_steam", return_value=True), \
                mock.patch.object(h, "kill_processes", return_value=[]):
            with self.assertRaises(h.AgentError) as caught:
                agent.start_session()
        self.assertIn("貸し出しを始めませんでした", str(caught.exception))

    def test_other_steam_games_are_found(self):
        import os

        lib = self.tmp / "Library"
        common = lib / "steamapps" / "common"
        procs = [
            {"pid": 1, "name": "VRChat.exe", "exe": str(common / "VRChat" / "VRChat.exe")},
            {"pid": 2, "name": "vrserver.exe", "exe": str(common / "SteamVR" / "bin" / "vrserver.exe")},
            {"pid": 3, "name": "BeatSaber.exe", "exe": str(common / "Beat Saber" / "Beat Saber.exe")},
            {"pid": 4, "name": "steam.exe", "exe": str(self.steam / "steam.exe")},
            {"pid": 5, "name": "nothing", "exe": None},
        ]
        found = h.other_steam_games([self.steam, lib], procs)
        self.assertEqual(found, [(3, "BeatSaber.exe")])
        self.assertEqual(os.sep in str(common), True)

    def test_logged_in_steam_account(self):
        (self.steam / "config" / "loginusers.vdf").write_text('''"users"
{
	"76561198000000001"
	{
		"AccountName"		"main_account"
		"PersonaName"		"Me"
		"MostRecent"		"0"
	}
	"76561198000000002"
	{
		"AccountName"		"geaspec_lend"
		"PersonaName"		"Lend"
		"MostRecent"		"1"
	}
}
''', encoding="utf-8")
        self.assertEqual(h.steam_logged_in_account(self.steam), "geaspec_lend")
        self.assertIsNone(h.steam_logged_in_account(None))
        self.assertIsNone(h.steam_logged_in_account(self.tmp))


class FakeAudio:
    """音の機器とアプリの音 (Windows の代わり)。"""

    def __init__(self):
        self.mute = {"spk": False, "mic": False, "hp": True, "vd": False}
        self.names = {"spk": "スピーカー (Realtek)", "mic": "マイク (Realtek)", "hp": "ヘッドホン", "vd": "Virtual Desktop Audio"}
        self.apps = {"chrome.exe": False, "VRChat.exe": False, "": False}
        self.pids = {"chrome.exe": 100, "VRChat.exe": 200, "": 0}

    def endpoints(self):
        return [h.AudioEndpoint(i, n) for i, n in self.names.items()]

    def get_mute(self, i):
        return self.mute[i]

    def set_mute(self, i, m):
        self.mute[i] = m

    def sessions(self):
        return [h.AudioSessionInfo(name or "system", self.pids[name], name) for name in self.apps]

    def set_session_mute(self, key, m):
        self.apps["" if key == "system" else key] = m


class AudioGuardTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.home = Path(tempfile.mkdtemp())
        self.audio = FakeAudio()
        self.guard = h.AudioGuard(self.home, backend_factory=lambda: self.audio)

    def test_only_vrchat_reaches_the_headset_and_the_pc_stays_silent(self):
        self.assertEqual(self.guard.apply(), [])
        # PC のスピーカー・ヘッドホン・マイクは消音。VD の出入り口はそのまま
        self.assertEqual(self.audio.mute, {"spk": True, "mic": True, "hp": True, "vd": False})
        # VRChat 以外のアプリと Windows の音は止める
        self.assertEqual(self.audio.apps, {"chrome.exe": True, "VRChat.exe": False, "": True})
        # 途中でつないだイヤホンも消音する
        self.audio.names["usb"] = "USB イヤホン"
        self.audio.mute["usb"] = False
        self.guard.apply()
        self.assertTrue(self.audio.mute["usb"])
        # 終わったら元どおり (はじめから消音だったヘッドホンは消音のまま)
        self.assertEqual(self.guard.restore(), [])
        self.assertEqual(self.audio.mute, {"spk": False, "mic": False, "hp": True, "vd": False, "usb": False})
        self.assertEqual(self.audio.apps, {"chrome.exe": False, "VRChat.exe": False, "": False})
        self.assertFalse(self.guard.backup.exists())

    def test_restore_after_a_crash(self):
        self.guard.apply()
        again = h.AudioGuard(self.home, backend_factory=lambda: self.audio)
        self.assertEqual(again.restore(), [])
        self.assertFalse(self.audio.mute["spk"])

    def test_problems_do_not_stop_lending(self):
        def broken():
            raise OSError("no audio service")

        guard = h.AudioGuard(self.home, backend_factory=broken)
        self.assertEqual(len(guard.apply()), 1)
        self.assertEqual(guard.restore(), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows の音の設定は Windows でだけ読める")
    def test_windows_audio_can_be_listed(self):
        try:
            endpoints = h.PycawAudio().endpoints()
        except OSError as exc:  # CI の PC には音のしくみがないことがある
            self.skipTest(f"audio service unavailable: {exc}")
        self.assertIsInstance(endpoints, list)
