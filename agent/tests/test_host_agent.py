import io
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import host_agent  # noqa: E402
from host_agent import AgentConfig, HostAgent  # noqa: E402


class FakeProc:
    def __init__(self, pid, name, dies_on_terminate=True):
        self.pid = pid
        self.info = {"name": name}
        self.dies_on_terminate = dies_on_terminate
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def fake_wait_procs(procs, timeout):
    gone = [p for p in procs if (p.terminated and p.dies_on_terminate) or p.killed]
    alive = [p for p in procs if p not in gone]
    return gone, alive


class KillProcessesTest(unittest.TestCase):
    def test_terminates_only_matching_names_case_insensitive(self):
        vrchat = FakeProc(1, "vrchat.exe")
        other = FakeProc(2, "explorer.exe")
        with mock.patch.object(host_agent.psutil, "process_iter", return_value=[vrchat, other]), \
             mock.patch.object(host_agent.psutil, "wait_procs", side_effect=fake_wait_procs):
            errors = host_agent.kill_processes(["VRChat.exe"], timeout=0)
        self.assertEqual(errors, [])
        self.assertTrue(vrchat.terminated)
        self.assertFalse(other.terminated)

    def test_kills_when_terminate_is_ignored(self):
        stubborn = FakeProc(3, "ALVR Dashboard.exe", dies_on_terminate=False)
        with mock.patch.object(host_agent.psutil, "process_iter", return_value=[stubborn]), \
             mock.patch.object(host_agent.psutil, "wait_procs", side_effect=fake_wait_procs):
            errors = host_agent.kill_processes(["ALVR Dashboard.exe"], timeout=0)
        self.assertEqual(errors, [])
        self.assertTrue(stubborn.killed)

    def test_access_denied_is_reported_not_raised(self):
        proc = FakeProc(4, "vrserver.exe")
        proc.terminate = mock.Mock(side_effect=host_agent.psutil.AccessDenied(4))
        with mock.patch.object(host_agent.psutil, "process_iter", return_value=[proc]), \
             mock.patch.object(host_agent.psutil, "wait_procs", return_value=([proc], [])):
            errors = host_agent.kill_processes(["vrserver.exe"], timeout=0)
        self.assertEqual(len(errors), 1)

    def test_dry_run_does_not_terminate(self):
        proc = FakeProc(5, "VRChat.exe")
        with mock.patch.object(host_agent.psutil, "process_iter", return_value=[proc]):
            host_agent.kill_processes(["VRChat.exe"], timeout=0, dry_run=True)
        self.assertFalse(proc.terminated)


class SafetyGuardTest(unittest.TestCase):
    def test_rejects_relative_path(self):
        # USERPROFILE が空だった場合に組み立てられうるパス
        self.assertFalse(host_agent.is_safe_wipe_target(Path("AppData/LocalLow/VRChat/VRChat")))

    def test_rejects_non_vrchat_dir(self):
        self.assertFalse(host_agent.is_safe_wipe_target(Path("/home/user/Documents")))

    def test_rejects_shallow_path(self):
        self.assertFalse(host_agent.is_safe_wipe_target(Path("/VRChat")))

    def test_accepts_vrchat_dir(self):
        # OS に合った絶対パスにする (Windows では "/home/..." はドライブがないので絶対パスではない)
        base = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(host_agent.is_safe_wipe_target(base / "AppData" / "LocalLow" / "VRChat" / "VRChat"))

    def test_agent_home_survives_missing_home_directory(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(host_agent.Path, "home", side_effect=RuntimeError("no home")):
            self.assertEqual(host_agent.agent_home(), Path.cwd() / host_agent.AGENT_NAME)


class WipeDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_vrchat_dir(self):
        target = self.root / "LocalLow" / "VRChat" / "VRChat"
        (target / "HTTPCache").mkdir(parents=True)
        (target / "Cookies").write_text("auth=secret")
        (target / "config.json").write_text("{}")
        (target / "output_log_2026.txt").write_text("log")
        readonly = target / "HTTPCache" / "data_0"
        readonly.write_text("x")
        os.chmod(readonly, stat.S_IREAD)
        return target

    def test_removes_everything_including_readonly(self):
        target = self._make_vrchat_dir()
        errors = host_agent.wipe_directory(target)
        self.assertEqual(errors, [])
        self.assertFalse(target.exists())

    def test_missing_dir_is_ok(self):
        self.assertEqual(host_agent.wipe_directory(self.root / "x" / "VRChat"), [])

    def test_dry_run_keeps_files(self):
        target = self._make_vrchat_dir()
        host_agent.wipe_directory(target, dry_run=True)
        self.assertTrue((target / "Cookies").exists())

    def test_unsafe_target_is_refused(self):
        keep = self.root / "Documents"
        keep.mkdir()
        errors = host_agent.wipe_directory(keep)
        self.assertEqual(len(errors), 1)
        self.assertTrue(keep.exists())

    def test_reports_error_when_deletion_keeps_failing(self):
        target = self._make_vrchat_dir()
        with mock.patch.object(host_agent.shutil, "rmtree", side_effect=PermissionError("locked")), \
             mock.patch.object(host_agent.time, "sleep"):
            errors = host_agent.wipe_directory(target, retries=2)
        self.assertEqual(len(errors), 1)


class HostAgentFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "LocalLow" / "VRChat" / "VRChat"
        self.target.mkdir(parents=True)
        (self.target / "Cookies").write_text("auth=secret")
        patches = [
            mock.patch.object(host_agent, "kill_processes", return_value=[]),
            mock.patch.object(host_agent, "delete_registry_tree", return_value=[]),
            mock.patch.object(host_agent, "is_process_running", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, **kwargs):
        cfg = AgentConfig(duration_min=kwargs.pop("duration_min", 0.001), wipe_targets=[self.target], **kwargs)
        return HostAgent(cfg, home=self.root / "agent")

    def test_cleanup_wipes_and_writes_report(self):
        report_dir = self.root / "cloud"
        agent = self._agent(report_dir=report_dir)
        agent.cleanup_session()
        self.assertFalse(self.target.exists())
        self.assertEqual(len(list((self.root / "agent" / "reports").glob("*.json"))), 1)
        self.assertEqual(len(list((report_dir / "reports").glob("*.json"))), 1)
        self.assertIn('"completed"', (self.root / "agent" / "session_state.json").read_text())

    def test_cleanup_runs_once(self):
        agent = self._agent()
        agent.cleanup_session()
        host_agent.kill_processes.reset_mock()
        agent.cleanup_session()
        host_agent.kill_processes.assert_not_called()

    def test_wait_ends_after_duration(self):
        agent = self._agent(duration_min=0.01)  # 0.6 秒
        start = time.monotonic()
        agent.wait_for_session()
        self.assertGreaterEqual(time.monotonic() - start, 0.5)
        self.assertEqual(agent.end_reason, "duration_elapsed")

    def test_wait_stops_on_request(self):
        agent = self._agent(duration_min=10)
        threading.Timer(0.2, agent.request_stop, args=("test",)).start()
        start = time.monotonic()
        agent.wait_for_session()
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(agent.end_reason, "test")

    def test_vrchat_is_started_again_when_the_borrower_closes_it(self):
        agent = self._agent(duration_min=0.1, vrchat_check_sec=0.1, vrchat_restart_after_sec=0.3)
        running = iter([True, True] + [False] * 1000)
        with mock.patch.object(host_agent, "is_process_running", side_effect=lambda names: next(running)), \
                mock.patch.object(agent, "_launch_vrchat") as launch:
            agent.wait_for_session()
        launch.assert_called_once()  # 1 秒ごとに確認。起動し直したあとは 1 分あけるので、6 秒の間に 1 回だけ

    def test_task_manager_is_locked_only_during_the_session(self):
        exe = self.root / "streamer.exe"
        exe.write_text("")
        agent = self._agent(streamer_path=exe, launch_delay_sec=0, vrchat_target=str(self.root / "vrchat.exe"))
        with mock.patch.object(host_agent, "set_task_manager_locked", return_value=[]) as lock, \
                mock.patch.object(host_agent.subprocess, "Popen"):
            agent.start_session()
            lock.assert_called_with(True, False)
            agent.cleanup_session()
            lock.assert_called_with(False, False)

    def test_start_fails_when_streamer_missing(self):
        agent = self._agent(streamer_path=self.root / "missing.exe", launch_delay_sec=0)
        with self.assertRaises(host_agent.AgentError):
            agent.start_session()

    def test_prepare_warns_about_unfinished_previous_session(self):
        agent = self._agent()
        agent.home.mkdir(parents=True)
        agent.state_file.write_text('{"session_id": "old", "status": "running"}')
        with self.assertLogs(host_agent.log, level="WARNING") as logs:
            agent.prepare_session()
        self.assertTrue(any("old" in line for line in logs.output))
        self.assertFalse(self.target.exists())


class FakeClient:
    def __init__(self, jobs, statuses=None):
        self.jobs = list(jobs)
        self.statuses = statuses or {}
        self.started_ids = []
        self.reports = {}
        self.base_url = "http://test"

    def poll(self):
        return self.jobs.pop(0) if self.jobs else {"action": "none"}

    def booking_status(self, booking_id):
        return self.statuses.get(booking_id, "active")

    def started(self, booking_id):
        self.started_ids.append(booking_id)

    def finished(self, booking_id, report):
        self.reports[booking_id] = report
        return True

    upload_ok = True

    def upload_screenshots(self, booking_id, data, count):
        self.uploads = getattr(self, "uploads", [])
        self.uploads.append((booking_id, data, count))
        return self.upload_ok


class ServerModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "LocalLow" / "VRChat" / "VRChat"
        self.target.mkdir(parents=True)
        (self.target / "Cookies").write_text("auth=secret")
        self.exe = self.root / "VirtualDesktop.Streamer.exe"
        self.exe.write_text("")
        patches = [
            mock.patch.object(host_agent, "kill_processes", return_value=[]),
            mock.patch.object(host_agent, "delete_registry_tree", return_value=[]),
            mock.patch.object(host_agent, "is_process_running", return_value=False),
            mock.patch.object(host_agent.subprocess, "Popen"),
            mock.patch.object(host_agent.os, "startfile", create=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cfg = AgentConfig(duration_min=None, streamer_path=self.exe, launch_delay_sec=0,
                               wipe_targets=[self.target])

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_job_runs_session_and_reports(self):
        client = FakeClient([])
        job = {"action": "start", "booking_id": 7, "remaining_seconds": 1, "borrower_meta_username": "quest_taro"}
        interrupted = host_agent.run_server_job(job, self.cfg, client, self.root / "agent")
        self.assertFalse(interrupted)
        self.assertEqual(client.started_ids, [7])
        report = client.reports[7]
        self.assertTrue(report["clean"])
        self.assertEqual(report["session_id"], "booking-7")
        self.assertEqual(report["end_reason"], "duration_elapsed")
        self.assertTrue(any("quest_taro" in w for w in report["warnings"]))
        self.assertFalse(self.target.exists())
        host_agent.subprocess.Popen.assert_called_once()

    def test_cancellation_on_server_stops_session(self):
        client = FakeClient([], statuses={8: "cancelled"})
        job = {"action": "start", "booking_id": 8, "remaining_seconds": 600}
        start = time.monotonic()
        host_agent.run_server_job(job, self.cfg, client, self.root / "agent", watch_interval=0.1)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(client.reports[8]["end_reason"], "server_cancelled")

    def test_cleanup_job_wipes_and_reports(self):
        client = FakeClient([])
        host_agent.run_server_job({"action": "cleanup", "booking_id": 9}, self.cfg, client, self.root / "agent")
        self.assertFalse(self.target.exists())
        self.assertEqual(client.reports[9]["end_reason"], "server_cleanup")
        self.assertEqual(client.started_ids, [])

    def test_serve_processes_jobs_until_stopped(self):
        stop = threading.Event()
        client = FakeClient([{"action": "cleanup", "booking_id": 1}])
        original_poll = client.poll

        def poll():
            job = original_poll()
            if job["action"] == "none":
                stop.set()
            return job

        client.poll = poll
        code = host_agent.serve(self.cfg, client, self.root / "agent", poll_interval=0.01, stop=stop)
        self.assertEqual(code, 0)
        self.assertIn(1, client.reports)

    def test_serve_exits_on_invalid_token(self):
        client = FakeClient([])
        client.poll = mock.Mock(side_effect=host_agent.AuthError("bad"))
        self.assertEqual(host_agent.serve(self.cfg, client, self.root / "agent", poll_interval=0.01), 2)


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        self.path.write_text(text, encoding="utf-8-sig")  # インストーラーは BOM 付きで書く

    def test_config_file_starts_server_mode_on_double_click(self):
        self.write('{"server": "https://geaspec.example", "token": "pcvr_abcdefghij", "vpn_interface": "Tailscale"}')
        with mock.patch.dict(os.environ, {}, clear=True):
            args = host_agent.parse_args(["--config", str(self.path)])
        self.assertFalse(args.needs_setup)
        self.assertEqual(args.server, "https://geaspec.example")
        self.assertEqual(args.token, "pcvr_abcdefghij")
        self.assertEqual(args.vpn_interface, "Tailscale")

    def test_priority_cli_then_env_then_file(self):
        self.write('{"server": "https://file", "token": "pcvr_fromfile1", "streamer": "alvr"}')
        with mock.patch.dict(os.environ, {"PCVR_AGENT_TOKEN": "pcvr_fromenv12"}, clear=True):
            args = host_agent.parse_args(["--config", str(self.path), "--streamer", "vd"])
        self.assertEqual(args.token, "pcvr_fromenv12")
        self.assertEqual(args.streamer, "vd")
        self.assertEqual(args.server, "https://file")

    def test_cleanup_only_does_not_connect_to_server(self):
        self.write('{"server": "https://file", "token": "pcvr_fromfile1"}')
        with mock.patch.dict(os.environ, {}, clear=True):
            args = host_agent.parse_args(["--config", str(self.path), "--cleanup-only"])
        self.assertIsNone(args.server)
        self.assertFalse(args.needs_setup)

    def test_broken_or_wrongly_typed_config_is_ignored(self):
        self.write("{not json")
        with mock.patch("sys.stderr"):
            self.assertEqual(host_agent.load_config_file(self.path), {})
        self.write('{"server": 123, "token": "pcvr_ok_token", "unknown": 1}')
        self.assertEqual(host_agent.load_config_file(self.path), {"token": "pcvr_ok_token"})

    def test_vrchat_is_launched_by_its_real_steam_id(self):
        self.assertEqual(host_agent.DEFAULT_VRCHAT_TARGET, "steam://rungameid/438100")
        # 0.5.5 までの誤った番号が設定ファイルに残っていても直す
        self.write('{"vrchat_target": "steam://rungameid/438110"}')
        self.assertEqual(host_agent.load_config_file(self.path), {"vrchat_target": "steam://rungameid/438100"})
        self.write('{"vrchat_target": "C:/Games/VRChat/VRChat.exe"}')
        self.assertEqual(host_agent.load_config_file(self.path), {"vrchat_target": "C:/Games/VRChat/VRChat.exe"})

    def test_setup_wizard_validates_and_saves(self):
        answers = iter([
            "geaspec.example",            # URL でない → 聞き直し
            "https://geaspec.example/",
            "abc",                        # トークンでない → 聞き直し
            "pcvr_0123456789abcdef",
            "",                           # 既定値 Tailscale
        ])
        with mock.patch("builtins.print"):
            ok = host_agent.run_setup_wizard(self.path, input_fn=lambda _: next(answers))
        self.assertTrue(ok)
        saved = host_agent.load_config_file(self.path)
        self.assertEqual(saved, {
            "server": "https://geaspec.example",
            "token": "pcvr_0123456789abcdef",
            "vpn_interface": "Tailscale",
        })

    def test_setup_wizard_can_skip_vpn_check(self):
        answers = iter(["https://x.example", "pcvr_0123456789abcdef", "-"])
        with mock.patch("builtins.print"):
            host_agent.run_setup_wizard(self.path, input_fn=lambda _: next(answers))
        self.assertNotIn("vpn_interface", host_agent.load_config_file(self.path))


class CloudSyncGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detects_onedrive_by_env_and_by_folder_name(self):
        onedrive = self.root / "OneDrive"
        with mock.patch.dict(os.environ, {"OneDrive": str(onedrive)}):
            self.assertEqual(host_agent.cloud_sync_root_of(onedrive / "画像" / "VRChat"), "OneDrive")
        with mock.patch.dict(os.environ, {}, clear=True):
            # 実機で見えた形: OneDrive - 長野県立大学\そのほか\画像\VRChat
            target = self.root / "OneDrive - 長野県立大学" / "そのほか" / "画像" / "VRChat"
            self.assertEqual(host_agent.cloud_sync_root_of(target), "OneDrive - 長野県立大学")
            self.assertIsNone(host_agent.cloud_sync_root_of(self.root / "Pictures" / "VRChat"))

    def test_existing_synced_folder_is_kept_and_reported(self):
        synced = self.root / "OneDrive - School" / "Pictures" / "VRChat"
        synced.mkdir(parents=True)
        (synced / "shot.png").write_text("x")
        local = self.root / "LocalLow" / "VRChat" / "VRChat"
        local.mkdir(parents=True)
        missing = self.root / "OneDrive - School" / "Temp" / "VRChat"
        cfg = AgentConfig(duration_min=None, wipe_targets=[synced, local, missing])
        with mock.patch.object(host_agent, "kill_processes", return_value=[]), \
             mock.patch.object(host_agent, "delete_registry_tree", return_value=[]), \
             mock.patch.dict(os.environ, {}, clear=True):
            agent = HostAgent(cfg, home=self.root / "agent")
            agent.cleanup_session()
        self.assertTrue((synced / "shot.png").exists())  # クラウドの写真は消さない
        self.assertFalse(local.exists())                  # ふつうのフォルダは消す
        self.assertEqual(len(agent.errors), 1)            # 存在するクラウド側だけ要確認
        self.assertFalse(agent.build_report()["clean"])


class LendingUserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.targets = [Path(self.tmp.name) / "LocalLow" / "VRChat" / "VRChat"]

    def tearDown(self):
        self.tmp.cleanup()

    def _confirm(self, answer, user="geaspec"):
        with mock.patch.dict(os.environ, {"USERNAME": user}, clear=True), mock.patch("builtins.print"):
            return host_agent.confirm_lending_user(self.path, self.targets, input_fn=lambda _: answer)

    def test_yes_registers_current_user(self):
        self.assertTrue(self._confirm("yes"))
        config = host_agent.load_config_file(self.path)
        self.assertTrue(host_agent.is_confirmed_lending_user(config, "geaspec"))
        self.assertTrue(host_agent.is_confirmed_lending_user(config, "GEASPEC"))
        self.assertFalse(host_agent.is_confirmed_lending_user(config, "LICHT"))

    def test_anything_but_yes_cancels(self):
        self.assertFalse(self._confirm("y"))
        self.assertFalse(self.path.exists())

    def _ensure(self, interactive, dry_run=False, user="LICHT"):
        args = mock.Mock(config_path=self.path)
        cfg = AgentConfig(duration_min=None, dry_run=dry_run, wipe_targets=self.targets)
        with mock.patch.dict(os.environ, {"USERNAME": user}, clear=True), \
             mock.patch.object(host_agent, "_interactive", return_value=interactive), \
             mock.patch.object(host_agent, "confirm_lending_user", return_value=False) as confirm:
            with self.assertLogs(host_agent.log, level="INFO") as logs:
                host_agent.log.info("start")
                ok = host_agent.ensure_lending_user(args, cfg)
        return ok, confirm, logs.output

    def test_unregistered_user_is_refused_when_not_interactive(self):
        ok, confirm, logs = self._ensure(interactive=False)
        self.assertFalse(ok)
        confirm.assert_not_called()
        self.assertTrue(any("削除を行いません" in line for line in logs))

    def test_unregistered_user_is_asked_when_interactive(self):
        ok, confirm, _ = self._ensure(interactive=True)
        self.assertFalse(ok)
        confirm.assert_called_once()

    def test_dry_run_needs_no_registration(self):
        ok, confirm, _ = self._ensure(interactive=False, dry_run=True)
        self.assertTrue(ok)
        confirm.assert_not_called()

    def test_registered_user_passes_but_other_user_does_not(self):
        host_agent.save_config_file(self.path, {"lending_user": "geaspec"})
        self.assertTrue(self._ensure(interactive=False, user="geaspec")[0])
        self.assertFalse(self._ensure(interactive=False, user="LICHT")[0])


class ScreenshotDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.shots = self.root / "Pictures" / "VRChat"
        (self.shots / "2026-10").mkdir(parents=True)
        (self.shots / "2026-10" / "VRChat_1.png").write_bytes(b"png1")
        (self.shots / "2026-10" / "VRChat_2.jpg").write_bytes(b"jpg2")
        (self.shots / "notes.txt").write_text("not an image")
        patches = [
            mock.patch.object(host_agent, "kill_processes", return_value=[]),
            mock.patch.object(host_agent, "delete_registry_tree", return_value=[]),
            mock.patch.object(host_agent.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, **kw):
        cfg = AgentConfig(duration_min=None, wipe_targets=[self.shots], screenshot_dir=self.shots, **kw)
        return HostAgent(cfg, home=self.root / "agent")

    def test_collect_zips_only_images(self):
        data, count, warnings = host_agent.collect_screenshots(self.shots)
        self.assertEqual(count, 2)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual(sorted(zf.namelist()), ["2026-10/VRChat_1.png", "2026-10/VRChat_2.jpg"])
        self.assertEqual(warnings, [])

    def test_collect_respects_size_limit(self):
        data, count, warnings = host_agent.collect_screenshots(self.shots, max_bytes=5)
        self.assertEqual(count, 1)
        self.assertEqual(len(warnings), 1)

    def test_photos_are_sent_then_deleted(self):
        agent = self._agent()
        client = FakeClient([])
        agent.before_wipe = lambda: host_agent.deliver_screenshots(agent, client, 7)
        agent.cleanup_session()
        self.assertEqual(client.uploads[0][0], 7)
        self.assertEqual(client.uploads[0][2], 2)
        self.assertFalse(self.shots.exists())  # 送信のあと PC からは消える

    def test_failed_upload_still_deletes_and_warns(self):
        agent = self._agent()
        client = FakeClient([])
        client.upload_ok = False
        agent.before_wipe = lambda: host_agent.deliver_screenshots(agent, client, 7)
        agent.cleanup_session()
        self.assertFalse(self.shots.exists())
        self.assertTrue(any("送信できませんでした" in w for w in agent.warnings))

    def test_crash_in_delivery_does_not_block_wipe(self):
        agent = self._agent()
        agent.before_wipe = mock.Mock(side_effect=RuntimeError("boom"))
        agent.cleanup_session()
        self.assertFalse(self.shots.exists())

    def test_keep_screenshots_skips_upload(self):
        agent = self._agent(keep_screenshots=True)
        client = FakeClient([])
        host_agent.deliver_screenshots(agent, client, 7)
        self.assertFalse(hasattr(client, "uploads"))


class CliTest(unittest.TestCase):
    def test_no_mode_and_no_config_needs_setup(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            args = host_agent.parse_args(["--config", str(Path(tmp) / "none.json")])
        self.assertTrue(args.needs_setup)

    def test_main_without_config_is_not_stuck_when_not_interactive(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(host_agent, "_interactive", return_value=False), mock.patch("sys.stderr"):
            self.assertEqual(host_agent.main(["--config", str(Path(tmp) / "none.json")]), 2)

    def test_rejects_non_positive_duration(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            host_agent.parse_args(["--duration", "0"])

    def test_streamer_path_follows_streamer_choice(self):
        with mock.patch.dict(os.environ, {"ALVR_PATH": r"D:\ALVR\ALVR Dashboard.exe"}):
            alvr = host_agent.config_from_args(host_agent.parse_args(["--duration", "30", "--streamer", "alvr"]))
            vd = host_agent.config_from_args(host_agent.parse_args(["--duration", "30"]))
        self.assertEqual(str(alvr.streamer_path), r"D:\ALVR\ALVR Dashboard.exe")
        self.assertEqual(str(vd.streamer_path), host_agent.DEFAULT_VD_PATH)

    def test_server_mode_requires_token(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"), mock.patch.dict(os.environ, {}, clear=True):
            host_agent.parse_args(["--server", "https://example.com"])

    def test_dry_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp, "USERPROFILE": tmp, "TEMP": tmp}), \
             mock.patch.object(host_agent, "kill_processes", return_value=[]):
            code = host_agent.main(["--duration", "0.005", "--dry-run", "--launch-delay", "0"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
