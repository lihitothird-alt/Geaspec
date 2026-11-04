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
