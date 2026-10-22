"""Geaspec 貸し手PC用 Host Agent (Windows 11)

セッションの流れ:
    1. check_vpn        VPNインターフェースが上がっているか確認 (任意)
    2. prepare_session  VRChat / SteamVR / ストリーマーを終了し、VRChatの痕跡を全削除
    3. start_session    ストリーマー (Virtual Desktop / ALVR) を起動し、数秒後に VRChat を起動
    4. wait_for_session 指定時間の経過 / Ctrl+C / コンソールを閉じる操作 を待つ
    5. cleanup_session  再びプロセスを終了し、VRChatの痕跡を全削除
    6. セッションレポート(JSON)を保存し、StorageBackend へアップロード

依存: Python 3.10+ 標準ライブラリ + psutil

使い方:
    python host_agent.py --duration 30
    python host_agent.py --duration 30 --vpn-interface Tailscale
    python host_agent.py --cleanup-only
    python host_agent.py --duration 1 --dry-run

    # サーバーモード: 予約サイトに問い合わせ、予約の時間になったら自動でセッションを行う
    set PCVR_AGENT_TOKEN=pcvr_xxx
    python host_agent.py --server https://example.com
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import getpass
import io
import json
import logging
import logging.handlers
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Protocol

import psutil

if sys.platform == "win32":
    import winreg
else:  # テスト・dry-run 用に Windows 以外でも import できるようにする
    winreg = None  # type: ignore[assignment]

AGENT_NAME = "GeaspecAgent"
AGENT_VERSION = "0.7.31"
log = logging.getLogger(AGENT_NAME)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# VRChat の Steam アプリ番号は 438100 (https://store.steampowered.com/app/438100/)
DEFAULT_VRCHAT_TARGET = "steam://rungameid/438100"
# 0.5.5 までの既定値 (番号の誤り)。Steam が「ライセンスがありません」と出すので、読み込み時に直す
WRONG_VRCHAT_TARGET = "steam://rungameid/438110"
DEFAULT_ALVR_PATH = r"C:\Program Files\ALVR\ALVR Launcher.exe"
DEFAULT_VD_PATH = r"C:\Program Files\Virtual Desktop Streamer\VirtualDesktop.Streamer.exe"

# 終了順序に意味がある: VRChat -> SteamVR -> ALVR
# (VRChat が先に終わらないと、終了時にレジストリやファイルを書き戻してしまう)
VRCHAT_PROCESSES = (
    "VRChat.exe",
    # EasyAntiCheat の起動ラッパー。貸し出し専用PCであることを前提に対象へ含める
    "start_protected_game.exe",
)
STEAMVR_PROCESSES = (
    "vrmonitor.exe",
    "vrserver.exe",
    "vrcompositor.exe",
    "vrdashboard.exe",
    "vrwebhelper.exe",
    "vrstartup.exe",
)
ALVR_PROCESSES = (
    "ALVR Dashboard.exe",
    "ALVR Launcher.exe",
)
# VirtualDesktop.Service.exe (Windows サービス) は止めない。止めると再起動まで接続できなくなる
VD_PROCESSES = ("VirtualDesktop.Streamer.exe",)

STREAMERS = {
    "vd": {"label": "Virtual Desktop", "processes": VD_PROCESSES, "path": DEFAULT_VD_PATH, "env": "VD_STREAMER_PATH"},
    "alvr": {"label": "ALVR", "processes": ALVR_PROCESSES, "path": DEFAULT_ALVR_PATH, "env": "ALVR_PATH"},
}

VRCHAT_REGISTRY_KEY = r"Software\VRChat\VRChat"

# Known Folder ID (Windows)
FOLDERID_LOCAL_APPDATA_LOW = "{A520A1A4-1780-4FF6-BD18-167343C5AF16}"
FOLDERID_PICTURES = "{33E28130-4E1E-4676-835A-98395C3BC3BB}"

# 残り時間をアナウンスするタイミング (秒)
ANNOUNCE_AT = (30 * 60, 10 * 60, 5 * 60, 60)


@dataclass
class AgentConfig:
    duration_min: float | None
    vrchat_target: str = DEFAULT_VRCHAT_TARGET
    streamer: str = "vd"
    streamer_path: Path = Path(DEFAULT_VD_PATH)
    # サーバーモードで渡される借り手の Meta ユーザー名 (Virtual Desktop の接続許可用)
    borrower_meta_username: str | None = None
    launch_delay_sec: float = 2.0
    # 借り手が VRChat を閉じたら、この秒数のあとにもう一度起動する (VRChat の外に出られないようにする)
    vrchat_restart_after_sec: float = 10.0
    vrchat_check_sec: float = 5.0
    # VR の中 (左手首) に残り時間を出す
    vr_timer: bool = True
    kill_timeout_sec: float = 10.0
    vpn_interface: str | None = None
    registry_baseline: Path | None = None
    keep_screenshots: bool = False
    report_dir: Path | None = None
    dry_run: bool = False
    # テストで差し替えられるよう、削除対象ディレクトリは外から渡せるようにしておく
    wipe_targets: list[Path] = field(default_factory=list)
    # VRChat のスクリーンショットの保存先 (None なら Pictures\VRChat)
    screenshot_dir: Path | None = None


# ---------------------------------------------------------------------------
# Windows パス解決
# ---------------------------------------------------------------------------


def _known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath でフォルダを解決する (OneDrive リダイレクトにも追従)。"""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out)
        )
        if hr != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception as exc:  # noqa: BLE001 - フォールバックするので落とさない
        log.debug("SHGetKnownFolderPath に失敗: %s", exc)
        return None


def _user_profile() -> Path | None:
    profile = os.environ.get("USERPROFILE")
    return Path(profile) if profile else None


def local_appdata_low() -> Path | None:
    """%USERPROFILE%\\AppData\\LocalLow (LocalLow は Local の中ではなく隣にある)。"""
    found = _known_folder(FOLDERID_LOCAL_APPDATA_LOW)
    if found:
        return found
    profile = _user_profile()
    return profile / "AppData" / "LocalLow" if profile else None


def pictures_dir() -> Path | None:
    found = _known_folder(FOLDERID_PICTURES)
    if found:
        return found
    profile = _user_profile()
    return profile / "Pictures" if profile else None


def vrchat_data_dir() -> Path | None:
    base = local_appdata_low()
    return base / "VRChat" / "VRChat" if base else None


def default_wipe_targets(keep_screenshots: bool) -> list[Path]:
    """削除対象。ファイル名を列挙せず、VRChat のフォルダごと消す。

    - LocalLow\\VRChat\\VRChat : Cookie / config.json / ログ / キャッシュ (Cache-WindowsPlayer 含む)
    - %TEMP%\\VRChat           : Unity の一時ファイル
    - Pictures\\VRChat          : 借り手が撮ったスクリーンショット
    """
    targets: list[Path] = []
    data = vrchat_data_dir()
    if data:
        targets.append(data)
    temp = os.environ.get("TEMP")
    if temp:
        targets.append(Path(temp) / "VRChat")
    if not keep_screenshots:
        pics = pictures_dir()
        if pics:
            targets.append(pics / "VRChat")
    return targets


def agent_home() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / AGENT_NAME
    try:
        return Path.home() / ".local" / "share" / AGENT_NAME
    except RuntimeError:
        # ホームフォルダが分からない環境 (環境変数が空など) でも落とさない
        return Path.cwd() / AGENT_NAME


# ---------------------------------------------------------------------------
# ストレージ (将来クラウドストレージに差し替えるための拡張ポイント)
# ---------------------------------------------------------------------------


class StorageBackend(Protocol):
    def upload(self, local_path: Path, remote_key: str) -> None: ...


class LocalDirectoryStorage:
    """ローカルフォルダへコピーする実装。

    OneDrive / Google Drive などの同期フォルダを指定すれば、
    それだけでクラウドに上がる。S3 等へ直接送る場合は同じ upload() を持つ
    クラスを追加して build_storage() で切り替える。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def upload(self, local_path: Path, remote_key: str) -> None:
        dest = self.root / remote_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)


def build_storage(cfg: AgentConfig) -> StorageBackend | None:
    if cfg.report_dir:
        return LocalDirectoryStorage(cfg.report_dir)
    return None


# ---------------------------------------------------------------------------
# 低レベル操作
# ---------------------------------------------------------------------------


class AgentError(Exception):
    pass


def kill_processes(
    names: Iterable[str], timeout: float, dry_run: bool = False
) -> list[str]:
    """名前が一致するプロセスを terminate -> 待機 -> kill する。失敗内容を返す。"""
    wanted = {n.lower() for n in names}
    errors: list[str] = []
    targets: list[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name.lower() in wanted:
            targets.append(proc)

    if not targets:
        return errors

    for proc in targets:
        label = f"{proc.info.get('name')} (pid={proc.pid})"
        if dry_run:
            log.info("[dry-run] 終了対象: %s", label)
            continue
        log.info("プロセスを終了します: %s", label)
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            errors.append(f"{label} の終了が拒否されました: {exc}")
            log.error("%s の終了が拒否されました (管理者権限が必要な可能性): %s", label, exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label} の終了に失敗: {exc}")
            log.error("%s の終了に失敗: %s", label, exc)

    if dry_run:
        return errors

    _, alive = psutil.wait_procs(targets, timeout=timeout)
    for proc in alive:
        log.warning("応答しないため強制終了します: pid=%s", proc.pid)
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:  # noqa: BLE001
            log.error("pid=%s の強制終了に失敗: %s", proc.pid, exc)

    if alive:
        _, still_alive = psutil.wait_procs(alive, timeout=timeout)
        for proc in still_alive:
            msg = f"pid={proc.pid} が終了しませんでした"
            errors.append(msg)
            log.error(msg)
    return errors


def is_safe_wipe_target(path: Path) -> bool:
    """誤設定 (環境変数が空など) でドライブやホームを消す事故を防ぐ。"""
    if not path.is_absolute():
        return False
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    if len(resolved.parts) < 3:
        return False
    if resolved.name.lower() != "vrchat":
        return False
    profile = _user_profile()
    if profile and resolved == profile.resolve(strict=False):
        return False
    return True


# クラウドに同期されるフォルダ。中身を消すとクラウド側からも消えるため、削除しない
CLOUD_SYNC_ENV = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
CLOUD_SYNC_NAMES = ("onedrive", "dropbox", "google drive", "googledrive", "icloud drive", "iclouddrive")


def cloud_sync_root_of(path: Path) -> str | None:
    """path がクラウド同期フォルダの中なら、その名前を返す。"""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    for env in CLOUD_SYNC_ENV:
        value = os.environ.get(env)
        if not value:
            continue
        try:
            root = Path(value).resolve(strict=False)
        except OSError:
            continue
        if resolved == root or root in resolved.parents:
            return root.name or value
    # 環境変数がなくても、フォルダ名で判定する (例: "OneDrive - 長野県立大学")
    for part in resolved.parts:
        if part.lower().startswith(CLOUD_SYNC_NAMES):
            return part
    return None


def _clear_readonly_and_retry(func, path, _exc_info) -> None:
    """rmtree 用: 読み取り専用属性を外して再実行する。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:  # noqa: BLE001 - 外側のリトライに任せる
        pass


def wipe_directory(
    path: Path, retries: int = 3, dry_run: bool = False
) -> list[str]:
    """ディレクトリを丸ごと削除する。ロック中のファイルに備えてリトライする。"""
    if not is_safe_wipe_target(path):
        msg = f"安全でないパスのため削除を中止しました: {path}"
        log.error(msg)
        return [msg]
    if not path.exists() and not path.is_symlink():
        log.info("存在しないためスキップ: %s", path)
        return []
    if dry_run:
        log.info("[dry-run] 削除対象: %s", path)
        return []

    delay = 1.0
    for attempt in range(1, retries + 1):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(path, onexc=_clear_readonly_and_retry)
                else:
                    shutil.rmtree(path, onerror=_clear_readonly_and_retry)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("削除に失敗 (%d/%d): %s: %s", attempt, retries, path, exc)

        if not path.exists():
            log.info("削除しました: %s", path)
            return []
        if attempt < retries:
            time.sleep(delay)
            delay *= 2

    leftovers = _list_leftovers(path)
    msg = f"削除しきれませんでした: {path} (残り {len(leftovers)} 件)"
    log.error(msg)
    for item in leftovers[:20]:
        log.error("  残存: %s", item)
    return [msg]


def _list_leftovers(path: Path) -> list[Path]:
    try:
        return [p for p in path.rglob("*")] or [path]
    except OSError:
        return [path]


def delete_registry_tree(subkey: str, dry_run: bool = False) -> list[str]:
    """HKCU\\<subkey> を配下ごと削除する。VRChat の PlayerPrefs (ログイン状態含む)。"""
    if winreg is None:
        log.info("Windows 以外のためレジストリ操作をスキップ: HKCU\\%s", subkey)
        return []
    if dry_run:
        log.info("[dry-run] レジストリ削除対象: HKCU\\%s", subkey)
        return []
    try:
        _delete_reg_tree(winreg.HKEY_CURRENT_USER, subkey)
        log.info("レジストリを削除しました: HKCU\\%s", subkey)
        return []
    except FileNotFoundError:
        log.info("レジストリキーが無いためスキップ: HKCU\\%s", subkey)
        return []
    except Exception as exc:  # noqa: BLE001
        msg = f"レジストリ削除に失敗: HKCU\\{subkey}: {exc}"
        log.error(msg)
        return [msg]


def _delete_reg_tree(root, subkey: str) -> None:
    access = winreg.KEY_READ | winreg.KEY_WRITE
    with winreg.OpenKey(root, subkey, 0, access) as key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_reg_tree(root, f"{subkey}\\{child}")
    winreg.DeleteKey(root, subkey)


def import_registry_baseline(reg_file: Path, dry_run: bool = False) -> list[str]:
    """貸し手が用意した既定設定 (.reg) を書き戻す。画質・音量などの初期値用。"""
    if dry_run:
        log.info("[dry-run] レジストリ既定値を読み込み: %s", reg_file)
        return []
    if not reg_file.is_file():
        msg = f"レジストリ既定値ファイルが見つかりません: {reg_file}"
        log.error(msg)
        return [msg]
    try:
        subprocess.run(
            ["reg", "import", str(reg_file)],
            check=True,
            capture_output=True,
            text=True,
        )
        log.info("レジストリ既定値を読み込みました: %s", reg_file)
        return []
    except Exception as exc:  # noqa: BLE001
        msg = f"レジストリ既定値の読み込みに失敗: {exc}"
        log.error(msg)
        return [msg]


def is_interface_up(name: str) -> bool:
    stats = psutil.net_if_stats().get(name)
    return bool(stats and stats.isup)


TASK_MANAGER_POLICY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"


def set_task_manager_locked(locked: bool, dry_run: bool = False) -> list[str]:
    """利用中だけ、このユーザーでタスクマネージャーを開けないようにする。問題があれば警告文を返す。"""
    if dry_run:
        log.info("[dry-run] タスクマネージャーを%s", "止める" if locked else "戻す")
        return []
    if winreg is None:
        return []
    try:
        if locked:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TASK_MANAGER_POLICY_KEY) as key:
                winreg.SetValueEx(key, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TASK_MANAGER_POLICY_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "DisableTaskMgr")
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"タスクマネージャーの設定を変えられませんでした: {exc}"]
    return []


def is_process_running(names: Iterable[str]) -> bool:
    wanted = {n.lower() for n in names}
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() in wanted:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# ---------------------------------------------------------------------------
# Agent 本体
# ---------------------------------------------------------------------------


class HostAgent:
    def __init__(self, cfg: AgentConfig, home: Path | None = None) -> None:
        self.cfg = cfg
        self.home = home or agent_home()
        self.state_file = self.home / "session_state.json"
        self.session_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.stop_event = threading.Event()
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.started_at: dt.datetime | None = None
        self.ended_at: dt.datetime | None = None
        self.end_reason = "unknown"
        self._cleanup_lock = threading.Lock()
        self._cleaned_up = False
        # 終了処理で、VRChat を閉じた後・削除する前に呼ぶ (スクリーンショットの送信用)
        self.before_wipe: Callable[[], None] | None = None
        if not cfg.wipe_targets:
            cfg.wipe_targets = default_wipe_targets(cfg.keep_screenshots)

    # --- 1. VPN ---------------------------------------------------------

    def check_vpn(self) -> bool:
        iface = self.cfg.vpn_interface
        if not iface:
            log.warning("VPN インターフェースが未指定のため確認をスキップします (--vpn-interface)")
            return True
        if is_interface_up(iface):
            log.info("VPN インターフェース '%s' は有効です", iface)
            return True
        available = ", ".join(sorted(psutil.net_if_stats().keys()))
        log.error("VPN インターフェース '%s' が有効ではありません。利用可能: %s", iface, available)
        return False

    # --- 2. 前処理 -------------------------------------------------------

    def prepare_session(self) -> None:
        log.info("=== 前処理を開始します ===")
        previous = self._read_state()
        if previous.get("status") == "running":
            log.warning(
                "前回のセッション (%s) は正常に終了していません。痕跡を削除します",
                previous.get("session_id"),
            )
        self._terminate_all()
        self._wipe_all()
        self._write_state("prepared")
        log.info("=== 前処理が完了しました ===")

    # --- 3. 起動 ---------------------------------------------------------

    def start_session(self) -> None:
        log.info("=== セッションを開始します ===")
        self._authorize_borrower()
        label = STREAMERS[self.cfg.streamer]["label"]
        exe = self.cfg.streamer_path
        if self.cfg.dry_run:
            log.info("[dry-run] %s を起動: %s", label, exe)
        else:
            if not exe.is_file():
                raise AgentError(f"{label} が見つかりません: {exe}")
            try:
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
                log.info("%s を起動しました: %s", label, exe)
            except OSError as exc:
                raise AgentError(f"{label} の起動に失敗: {exc}") from exc

        time.sleep(self.cfg.launch_delay_sec)

        self._launch_vrchat()
        # 借り手が VD でパソコンの画面を開いても、タスクマネージャーから他のアプリを触れないようにする
        self.warnings += set_task_manager_locked(True, self.cfg.dry_run)

        self.started_at = dt.datetime.now()
        self._write_state("running")

    def _launch_vrchat(self) -> None:
        target = self.cfg.vrchat_target
        if self.cfg.dry_run:
            log.info("[dry-run] VRChat を起動: %s", target)
            return
        try:
            if "://" in target:
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                exe = Path(target)
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
            log.info("VRChat を起動しました: %s", target)
        except (OSError, AttributeError) as exc:
            raise AgentError(f"VRChat の起動に失敗: {exc}") from exc

    # --- 4. 待機 ---------------------------------------------------------

    def wait_for_session(self) -> None:
        duration_sec = (self.cfg.duration_min or 0) * 60
        deadline = time.monotonic() + duration_sec
        log.info("セッション時間: %.1f 分 (Ctrl+C で途中終了)", duration_sec / 60)
        timer = self._start_vr_timer(deadline)
        try:
            self._wait_loop(deadline, duration_sec)
        finally:
            if timer:
                timer.stop()

    def _start_vr_timer(self, deadline: float):
        if not self.cfg.vr_timer or self.cfg.dry_run:
            return None
        try:
            from vr_timer import start_vr_timer

            return start_vr_timer(deadline, self.home / "vr_timer")
        except Exception as exc:  # noqa: BLE001 - タイマーが出なくても利用は続ける
            log.warning("VR の中のタイマーを始められませんでした: %s", exc)
            return None

    def _wait_loop(self, deadline: float, duration_sec: float) -> None:
        announced: set[int] = set()
        last_health_check = 0.0
        last_vrchat_check = 0.0
        vpn_was_up = True
        vrchat_seen = False
        vrchat_gone_at: float | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.end_reason = "duration_elapsed"
                log.info("セッション時間が終了しました")
                return
            if self.stop_event.wait(min(1.0, remaining)):
                log.info("停止要求を受け取りました")
                return

            for mark in ANNOUNCE_AT:
                if remaining <= mark and mark not in announced and duration_sec > mark:
                    announced.add(mark)
                    log.info("残り %d 分です", mark // 60)

            now = time.monotonic()
            if now - last_health_check >= 30:
                last_health_check = now
                if self.cfg.vpn_interface and not self.cfg.dry_run:
                    up = is_interface_up(self.cfg.vpn_interface)
                    if vpn_was_up and not up:
                        log.warning("VPN '%s' が切断されました", self.cfg.vpn_interface)
                    elif not vpn_was_up and up:
                        log.info("VPN '%s' が復帰しました", self.cfg.vpn_interface)
                    vpn_was_up = up

            # VRChat が閉じられたら、少し待ってからもう一度起動する (借り手は VRChat の中だけで過ごす)
            if not self.cfg.dry_run and now - last_vrchat_check >= self.cfg.vrchat_check_sec:
                last_vrchat_check = now
                if is_process_running(VRCHAT_PROCESSES[:1]):
                    vrchat_seen, vrchat_gone_at = True, None
                elif vrchat_seen:
                    if vrchat_gone_at is None:
                        vrchat_gone_at = now
                        log.info("VRChat が閉じられました。%d 秒後にもう一度起動します", int(self.cfg.vrchat_restart_after_sec))
                    elif now - vrchat_gone_at >= self.cfg.vrchat_restart_after_sec:
                        # 起動に時間がかかっても二重に起動しないよう、次に確かめるまで 1 分あける
                        vrchat_gone_at = now + 60
                        try:
                            self._launch_vrchat()
                        except AgentError as exc:
                            log.error("%s", exc)
                            self.warnings.append(str(exc))

    # --- 5. 終了処理 -----------------------------------------------------

    def cleanup_session(self) -> None:
        """何度呼ばれても 1 回だけ実行する (Ctrl+C とウィンドウ閉じが重なっても安全)。"""
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            log.info("=== 終了処理を開始します ===")
            self.warnings += set_task_manager_locked(False, self.cfg.dry_run)
            self._terminate_all()
            if self.before_wipe:
                try:
                    self.before_wipe()
                except Exception as exc:  # noqa: BLE001 - 送信に失敗しても削除は必ず行う
                    log.exception("削除前の処理に失敗")
                    self.warnings.append(f"削除前の処理に失敗: {exc}")
            self._wipe_all()
            self.ended_at = dt.datetime.now()
            self._write_state("completed" if not self.errors else "completed_with_errors")
            self._save_report()
            self._cleaned_up = True
            if self.errors:
                log.error("=== 終了処理が完了しました (エラー %d 件) ===", len(self.errors))
            else:
                log.info("=== 終了処理が完了しました。クリーンな状態です ===")

    def _authorize_borrower(self) -> None:
        """借り手の Meta ユーザー名だけが接続できるようにする。

        TODO: Virtual Desktop Streamer が許可ユーザー名をどこに保存しているか
        (設定ファイルかレジストリか) を実機で確認して実装する。
        それまでは、運営が VD Streamer の画面で手動設定する。
        """
        name = self.cfg.borrower_meta_username
        if not name or self.cfg.streamer != "vd":
            return
        msg = f"VD の接続許可ユーザーの自動設定は未実装です。手動で '{name}' を設定してください"
        log.warning(msg)
        self.warnings.append(msg)

    def request_stop(self, reason: str) -> None:
        self.end_reason = reason
        self.stop_event.set()

    # --- 内部処理 --------------------------------------------------------

    def _terminate_all(self) -> None:
        streamer = STREAMERS[self.cfg.streamer]["processes"]
        for group in (VRCHAT_PROCESSES, STEAMVR_PROCESSES, streamer):
            self.errors += kill_processes(group, self.cfg.kill_timeout_sec, self.cfg.dry_run)

    def _wipe_all(self) -> None:
        for target in self.cfg.wipe_targets:
            synced = cloud_sync_root_of(target)
            if synced:
                if target.exists():
                    msg = (
                        f"クラウド同期フォルダ ({synced}) の中なので削除しませんでした: {target}。"
                        "借り手のデータが残っている可能性があります。"
                        "OneDrive などと連携していない、貸し出し専用のローカルユーザーを使ってください"
                    )
                    log.error(msg)
                    self.errors.append(msg)
                else:
                    log.info("クラウド同期フォルダの中ですが、存在しないためスキップ: %s", target)
                continue
            self.errors += wipe_directory(target, dry_run=self.cfg.dry_run)
        self.errors += delete_registry_tree(VRCHAT_REGISTRY_KEY, self.cfg.dry_run)
        if self.cfg.registry_baseline:
            self.errors += import_registry_baseline(self.cfg.registry_baseline, self.cfg.dry_run)

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            log.warning("状態ファイルを読めません: %s", exc)
            return {}

    def _write_state(self, status: str) -> None:
        if self.cfg.dry_run:
            return
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(
                    {
                        "session_id": self.session_id,
                        "status": status,
                        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("状態ファイルを書けません: %s", exc)

    def build_report(self) -> dict:
        return {
            "session_id": self.session_id,
            "host": os.environ.get("COMPUTERNAME", ""),
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "ended_at": self.ended_at.isoformat(timespec="seconds") if self.ended_at else None,
            "planned_duration_min": self.cfg.duration_min,
            "end_reason": self.end_reason,
            "streamer": self.cfg.streamer,
            "clean": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
            "dry_run": self.cfg.dry_run,
        }

    def _save_report(self) -> None:
        if self.cfg.dry_run:
            log.info("[dry-run] レポート: %s", json.dumps(self.build_report(), ensure_ascii=False))
            return
        try:
            report_dir = self.home / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            path = report_dir / f"session-{self.session_id}.json"
            path.write_text(
                json.dumps(self.build_report(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("セッションレポートを保存しました: %s", path)
            storage = build_storage(self.cfg)
            if storage:
                storage.upload(path, f"reports/{path.name}")
                log.info("セッションレポートをアップロードしました")
        except Exception as exc:  # noqa: BLE001
            log.error("セッションレポートの保存/アップロードに失敗: %s", exc)



# ---------------------------------------------------------------------------
# スクリーンショットを借り手に届ける
# ---------------------------------------------------------------------------

SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SCREENSHOT_MAX_BYTES = 200 * 1024 * 1024


def screenshot_dir(cfg: AgentConfig) -> Path | None:
    if cfg.screenshot_dir:
        return cfg.screenshot_dir
    pics = pictures_dir()
    return pics / "VRChat" if pics else None


def collect_screenshots(directory: Path, max_bytes: int = SCREENSHOT_MAX_BYTES) -> tuple[bytes | None, int, list[str]]:
    """フォルダ内の画像を zip にまとめる。(zip, 枚数, 警告) を返す。

    セッション開始時に前の借り手の写真は削除済みなので、ここにあるのは今回の写真だけ。
    """
    warnings: list[str] = []
    if not directory.is_dir():
        return None, 0, warnings
    files = sorted(
        (f for f in directory.rglob("*") if f.is_file() and f.suffix.lower() in SCREENSHOT_EXTENSIONS),
        key=lambda f: f.stat().st_mtime,
    )
    if not files:
        return None, 0, warnings
    buf = io.BytesIO()
    total = 0
    count = 0
    # PNG・JPEG はすでに圧縮されているので、zip では圧縮しない (速い)
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for f in files:
            size = f.stat().st_size
            if total + size > max_bytes:
                warnings.append(f"容量の上限を超えたため {len(files) - count} 枚は送信しませんでした")
                break
            zf.write(f, arcname=f.relative_to(directory).as_posix())
            total += size
            count += 1
    return (buf.getvalue() if count else None), count, warnings


def deliver_screenshots(agent: "HostAgent", client: "ServerClient", booking_id: int) -> None:
    """VRChat を閉じた後・削除する前に、写真をサーバーへ送る。送れなくても削除は続ける。"""
    if agent.cfg.keep_screenshots:
        return
    directory = screenshot_dir(agent.cfg)
    if not directory:
        return
    data, count, warnings = collect_screenshots(directory)
    agent.warnings += warnings
    if not data:
        log.info("送信するスクリーンショットはありません")
        return
    if agent.cfg.dry_run:
        log.info("[dry-run] スクリーンショット %d 枚を送信 (%d KB)", count, len(data) // 1024)
        return
    log.info("スクリーンショット %d 枚を借り手に送信します (%d KB)", count, len(data) // 1024)
    if client.upload_screenshots(booking_id, data, count):
        log.info("スクリーンショットを送信しました")
    else:
        msg = f"スクリーンショット {count} 枚を送信できませんでした (PC からは削除します)"
        log.error(msg)
        agent.warnings.append(msg)


# ---------------------------------------------------------------------------
# コンソールイベント (ウィンドウの×ボタン / ログオフ / シャットダウン)
# ---------------------------------------------------------------------------

CTRL_C_EVENT = 0
_console_handler_ref = None  # GC されないよう参照を保持
_current_agent: HostAgent | None = None  # サーバーモードではセッションごとに差し替わる


def set_current_agent(agent: HostAgent | None) -> None:
    global _current_agent
    _current_agent = agent


def install_console_handler() -> None:
    """Ctrl+C 以外の終了イベントでも、実行中のセッションの cleanup を走らせる。

    Ctrl+C は Python 標準の KeyboardInterrupt に任せる (False を返す)。
    ウィンドウを閉じた場合 Windows は約 5 秒で強制終了するため、
    間に合わなかった分は次回起動時の prepare_session で削除される。
    """
    global _console_handler_ref
    if sys.platform != "win32" or _console_handler_ref is not None:
        return
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event: int) -> bool:
        if event == CTRL_C_EVENT:
            return False
        log.warning("コンソール終了イベント (%d) を検知しました", event)
        agent = _current_agent
        if agent is not None:
            agent.request_stop(f"console_event_{event}")
            agent.cleanup_session()
        return True

    _console_handler_ref = handler_type(handler)
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True):
        log.warning("コンソールイベントハンドラを登録できませんでした")


# ---------------------------------------------------------------------------
# セッション実行 (CLI モードとサーバーモードで共通)
# ---------------------------------------------------------------------------


def run_session(agent: HostAgent, on_started=None) -> bool:
    """1 セッションを最後まで実行する。Ctrl+C で中断された場合は True を返す。

    どの段階で失敗しても、必ず cleanup_session まで行う。
    """
    interrupted = False
    set_current_agent(agent)
    try:
        agent.prepare_session()
        agent.start_session()
        if on_started:
            on_started()
        agent.wait_for_session()
    except KeyboardInterrupt:
        interrupted = True
        agent.end_reason = "keyboard_interrupt"
        log.info("Ctrl+C を検知しました")
    except AgentError as exc:
        agent.end_reason = "start_failed"
        agent.errors.append(str(exc))
        log.error("%s", exc)
    except Exception as exc:  # noqa: BLE001 - 想定外でも必ず cleanup する
        agent.end_reason = "unexpected_error"
        agent.errors.append(repr(exc))
        log.exception("想定外のエラー")
    finally:
        try:
            agent.cleanup_session()
        except KeyboardInterrupt:
            # 終了処理中の 2 回目の Ctrl+C でも、もう一度だけやり直す
            interrupted = True
            log.warning("終了処理中です。もう一度終了処理を実行します")
            agent.cleanup_session()
        set_current_agent(None)
    return interrupted


# ---------------------------------------------------------------------------
# サーバーモード: 予約サイトの指示でセッションを行う
# ---------------------------------------------------------------------------


class AuthError(Exception):
    pass


@dataclass
class AgentStatus:
    """GUI に見せる今の状態。待機スレッドが更新し、GUI が定期的に読む。"""

    # stopped / idle (予約待ち) / unreachable (サーバーに届かない) / session (利用中) /
    # cleaning (後片付け中) / auth_error (トークンが無効)
    state: str = "stopped"
    booking_id: int | None = None
    session_ends_at: dt.datetime | None = None
    # 利用の長さ (秒)。画面の残り時間のバーに使う
    session_total_seconds: float = 0.0
    last_poll_at: dt.datetime | None = None
    # 次の予約 (この PC のローカル時刻)。画面に「あと何分で開始」を出す
    next_booking_id: int | None = None
    next_starts_at: dt.datetime | None = None
    next_ends_at: dt.datetime | None = None
    # ok / pending (運営の承認待ち) / paused (運営が受付を止めている)
    host_state: str = "ok"
    # 新しい予約に気づいた回数。GUI はこれが増えたらウィンドウを前に出す
    new_bookings: int = 0
    seen_booking_ids: set = field(default_factory=set)


STATUS = AgentStatus()


def server_time_to_local(value: str) -> dt.datetime:
    """サーバーの日時 (ISO 8601, UTC) を、この PC のローカル時刻 (タイムゾーンなし) にする。"""
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().replace(tzinfo=None)


def local_to_server_time(value: dt.datetime) -> str:
    """ローカル時刻 (タイムゾーンなし) を、サーバーに送る ISO 8601 (UTC) にする。"""
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_next_booking(info: dict | None) -> None:
    """ポーリングの結果から、次の予約の表示を更新する。"""
    if not info:
        STATUS.next_booking_id = STATUS.next_starts_at = STATUS.next_ends_at = None
        return
    try:
        booking_id = int(info["booking_id"])
        starts_at = server_time_to_local(str(info["starts_at"]))
        ends_at = server_time_to_local(str(info["ends_at"]))
    except (KeyError, TypeError, ValueError):
        return
    if booking_id not in STATUS.seen_booking_ids:
        STATUS.seen_booking_ids.add(booking_id)
        log.info("予約が入っています: #%s %s〜%s", booking_id, f"{starts_at:%m/%d %H:%M}", f"{ends_at:%H:%M}")
        STATUS.new_bookings += 1
    STATUS.next_booking_id, STATUS.next_starts_at, STATUS.next_ends_at = booking_id, starts_at, ends_at


CONNECTION_CODE_PREFIX = "geaspec1:"


def parse_connection_code(text: str) -> tuple[str, str]:
    """管理画面の「接続コード」から (予約サイトの URL, トークン) を取り出す。"""
    code = "".join(text.split())  # 貼り付けで入った改行・空白を取る
    if not code.lower().startswith(CONNECTION_CODE_PREFIX):
        raise ValueError("接続コードは geaspec1: で始まる文字列です。届いたコードをそのまま貼り付けてください")
    raw = code[len(CONNECTION_CODE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        server, token = str(data["server"]).rstrip("/"), str(data["token"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("接続コードが途中で切れているようです。もう一度、全部コピーして貼り付けてください") from None
    if not server.startswith(("http://", "https://")) or not token.startswith("pcvr_"):
        raise ValueError("接続コードの中身が正しくありません。運営に新しいコードを発行してもらってください")
    return server, token


class ServerClient:
    """予約サイトの Agent API を呼ぶ (標準ライブラリのみ)。

    貸し手 PC はホームルーターの内側にあり外から接続できないため、
    Agent の側から定期的に問い合わせる (ポーリング) 方式にしている。
    """

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: dict | None = None,
        raw: bytes | None = None, content_type: str = "application/json", timeout: float | None = None,
        error_body: bool = False,
    ) -> dict | None:
        data = raw if raw is not None else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        )
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
                "User-Agent": f"{AGENT_NAME}/{AGENT_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthError("Agent トークンが無効です。管理画面で発行したトークンを確認してください") from exc
            log.error("サーバーがエラーを返しました: %s %s -> %s", method, path, exc.code)
            if error_body:
                try:
                    detail = json.loads(exc.read())
                except (OSError, ValueError):
                    detail = {}
                if isinstance(detail, dict):
                    return {"error": str(detail.get("error") or detail.get("detail") or f"エラー {exc.code}")}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("サーバーに接続できません: %s", exc)
        return None

    def poll(self) -> dict | None:
        return self._request("GET", "/api/agent/poll")

    def login(self, email: str, password: str, pc_name: str, machine_id: str) -> dict | None:
        """貸し手のメールアドレスとパスワードで、この PC を予約サイトに登録する。

        成功すると {"token", "host", "pending"}、失敗すると {"error": 理由}、届かなければ None。
        """
        body = {"email": email, "password": password, "pc_name": pc_name, "machine_id": machine_id}
        return self._request("POST", "/api/agent/login", body, error_body=True)

    def send_specs(self, specs: dict) -> None:
        """この PC の性能を予約サイトに送る (借りる人が空き枠の一覧で見られる)。"""
        self._request("POST", "/api/agent/specs", specs)

    def hello(self) -> dict | None:
        """接続テスト。届けば {"host": PC の名前} が返る。"""
        return self._request("GET", "/api/agent/hello")

    def get_schedule(self, start: dt.datetime, days: int) -> dict | None:
        query = urllib.parse.urlencode({"start": local_to_server_time(start), "days": days})
        return self._request("GET", f"/api/agent/schedule?{query}")

    def put_schedule(self, start: dt.datetime, days: int, price_yen: int, starts: Iterable[dt.datetime]) -> dict | None:
        """受付する時間を保存する。失敗したら {"error": 理由} か None (届かない) を返す。"""
        body = {
            "start": local_to_server_time(start), "days": days, "price_yen": price_yen,
            "starts": [local_to_server_time(t) for t in sorted(starts)],
        }
        return self._request("PUT", "/api/agent/schedule", body, error_body=True)

    def booking_status(self, booking_id: int) -> str | None:
        resp = self._request("GET", f"/api/agent/bookings/{booking_id}")
        return resp.get("status") if resp else None

    def started(self, booking_id: int) -> None:
        self._request("POST", f"/api/agent/bookings/{booking_id}/started", {})

    def upload_screenshots(self, booking_id: int, data: bytes, count: int) -> bool:
        delay = 2.0
        for attempt in range(3):
            resp = self._request(
                "POST", f"/api/agent/bookings/{booking_id}/screenshots?count={count}",
                raw=data, content_type="application/zip", timeout=300,
            )
            if resp is not None:
                return True
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
        return False

    def finished(self, booking_id: int, report: dict) -> bool:
        delay = 2.0
        for _ in range(4):
            if self._request("POST", f"/api/agent/bookings/{booking_id}/finished", report) is not None:
                return True
            time.sleep(delay)
            delay *= 2
        # 届かなくても、サーバーは次回のポーリングで cleanup を指示してくるので再送される
        log.error("終了報告を送れませんでした: booking=%s", booking_id)
        return False


def _watch_cancellation(agent: HostAgent, client: ServerClient, booking_id: int, done: threading.Event,
                        interval: float = 30.0) -> None:
    """セッション中に運営がキャンセルしたら止める。"""
    while not done.wait(interval):
        try:
            status = client.booking_status(booking_id)
        except AuthError:
            return
        if status is not None and status not in ("active", "confirmed"):
            log.warning("サーバー側で予約が '%s' になったため終了します", status)
            agent.request_stop("server_cancelled")
            return


def run_server_job(job: dict, cfg: AgentConfig, client: ServerClient, home: Path,
                   watch_interval: float = 30.0) -> bool:
    """サーバーの指示 1 件を処理する。Ctrl+C で中断された場合は True。"""
    action = job.get("action")
    booking_id = int(job.get("booking_id", 0))

    if action == "cleanup":
        log.info("予約 #%s の後片付けを行います", booking_id)
        STATUS.state, STATUS.booking_id = "cleaning", booking_id
        agent = HostAgent(replace(cfg, duration_min=None, wipe_targets=list(cfg.wipe_targets)), home)
        agent.session_id = f"booking-{booking_id}"
        agent.end_reason = "server_cleanup"
        agent.before_wipe = lambda: deliver_screenshots(agent, client, booking_id)
        agent.cleanup_session()
        client.finished(booking_id, agent.build_report())
        STATUS.state, STATUS.booking_id = "idle", None
        return False

    if action != "start":
        return False

    remaining = int(job.get("remaining_seconds", 0))
    if remaining <= 0:
        return run_server_job({"action": "cleanup", "booking_id": booking_id}, cfg, client, home)

    log.info(
        "予約 #%s を%s開始します (残り %d 分)",
        booking_id, "再" if job.get("resume") else "", remaining // 60,
    )
    session_cfg = replace(
        cfg,
        duration_min=remaining / 60,
        borrower_meta_username=job.get("borrower_meta_username") or None,
        wipe_targets=list(cfg.wipe_targets),
    )
    agent = HostAgent(session_cfg, home)
    agent.session_id = f"booking-{booking_id}"
    agent.before_wipe = lambda: deliver_screenshots(agent, client, booking_id)
    if not agent.check_vpn():
        agent.end_reason = "vpn_down"
        agent.errors.append("VPN が無効なため開始できませんでした")
        agent.cleanup_session()
        client.finished(booking_id, agent.build_report())
        return False

    STATUS.state, STATUS.booking_id = "session", booking_id
    STATUS.session_ends_at = dt.datetime.now() + dt.timedelta(seconds=remaining)
    STATUS.session_total_seconds = float(remaining)
    done = threading.Event()
    watcher = threading.Thread(
        target=_watch_cancellation, args=(agent, client, booking_id, done, watch_interval), daemon=True
    )
    watcher.start()
    try:
        interrupted = run_session(agent, on_started=lambda: client.started(booking_id))
    finally:
        done.set()
    client.finished(booking_id, agent.build_report())
    STATUS.state, STATUS.booking_id, STATUS.session_ends_at = "idle", None, None
    return interrupted


GPU_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def detect_specs() -> dict:
    """この PC の性能 (グラフィックボード・CPU・メモリ)。借りる人が予約前に見られるよう、予約サイトに送る。

    管理者の権限がなくても読めるレジストリと psutil だけを使う。読めないものは空にする。
    """
    specs: dict = {"ram_gb": round(psutil.virtual_memory().total / 1024 ** 3)}
    if sys.platform != "win32":
        return specs
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            specs["cpu"] = " ".join(str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).split())
    except OSError:
        pass
    gpus: list[tuple[int, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GPU_CLASS_KEY) as cls:
            for i in range(32):
                try:
                    name = winreg.EnumKey(cls, i)
                except OSError:
                    break
                if not name.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, name) as key:
                        desc = str(winreg.QueryValueEx(key, "DriverDesc")[0]).strip()
                        try:
                            vram = int(winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0])
                        except (OSError, TypeError, ValueError):
                            vram = 0
                except OSError:
                    continue
                if desc and "Microsoft" not in desc and "Virtual" not in desc:
                    gpus.append((vram, desc))
    except OSError:
        pass
    if gpus:
        vram, desc = max(gpus)  # いちばん VRAM の多いもの (内蔵 GPU より、ゲーム用のボード)
        specs["gpu"] = desc
        if vram:
            specs["vram_gb"] = round(vram / 1024 ** 3)
    return specs


def serve(cfg: AgentConfig, client: ServerClient, home: Path, poll_interval: float,
          stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()
    log.info("予約の受付を始めました: %s (%.0f 秒ごとに確認)", client.base_url, poll_interval)
    # 前回が途中で止まっていても、タスクマネージャーを止めたままにしない
    set_task_manager_locked(False)
    install_console_handler()
    specs_sent = False
    try:
        while not stop.is_set():
            job = client.poll()
            STATUS.last_poll_at = dt.datetime.now()
            STATUS.state = "unreachable" if job is None else "idle"
            if job is not None and not specs_sent:
                # サイトに届いてから 1 回だけ送る (届かないうちに送ると、再送を待つぶん最初の確認が遅れる)
                specs_sent = True
                try:
                    client.send_specs(detect_specs())
                except Exception:  # noqa: BLE001 - 性能が送れなくても貸し出しは続ける
                    log.warning("PC の性能を送れませんでした", exc_info=True)
            if job is not None and job.get("action") == "none":
                update_next_booking(job.get("next"))
                STATUS.host_state = str(job.get("host_state") or "ok")
            elif job and job.get("action") == "start":
                update_next_booking(None)
            if job and job.get("action") in ("start", "cleanup"):
                if run_server_job(job, cfg, client, home):
                    log.info("Ctrl+C によりサーバーモードを終了します")
                    return 0
                continue  # 終わったらすぐ次を確認する
            stop.wait(poll_interval)
    except AuthError as exc:
        log.error("%s", exc)
        STATUS.state = "auth_error"
        return 2
    except KeyboardInterrupt:
        log.info("サーバーモードを終了します")
    finally:
        if STATUS.state != "auth_error":
            STATUS.state = "stopped"
    return 0


# ---------------------------------------------------------------------------
# 設定ファイル (インストーラーや初回起動時の質問で作る)
# ---------------------------------------------------------------------------

CONFIG_KEYS = {
    "server": str,
    "token": str,
    "vpn_interface": str,
    "streamer": str,
    "streamer_path": str,
    "vrchat_target": str,
    "report_dir": str,
    "keep_screenshots": bool,
    "poll_interval": (int, float),
    # 貸し出し専用ユーザーとして同意済みの Windows ユーザー名
    "lending_user": str,
    # この PC を見分ける ID (ログインし直しても同じ PC として扱うため)
    "machine_id": str,
}


def computer_name() -> str:
    """Windows の「PC の名前」。予約サイトの管理画面で、どの PC か見分けるのに使う。"""
    import platform

    return os.environ.get("COMPUTERNAME") or platform.node()


def builtin_server() -> str:
    """アプリに組み込まれた予約サイトのアドレス (ビルド時に server_url.txt を同梱する)。"""
    try:
        return (Path(__file__).resolve().parent / "server_url.txt").read_text(encoding="utf-8-sig").strip().rstrip("/")
    except OSError:
        return ""


def default_config_path() -> Path:
    return agent_home() / "config.json"


def load_config_file(path: Path) -> dict:
    """設定ファイルを読む。壊れていても落とさず、空として扱う。"""
    try:
        # インストーラーやメモ帳が付ける BOM を許容する
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"設定ファイルを読めません ({path}): {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    config = {
        k: v for k, v in data.items()
        if k in CONFIG_KEYS and isinstance(v, CONFIG_KEYS[k]) and v != ""
    }
    if config.get("vrchat_target") == WRONG_VRCHAT_TARGET:
        config["vrchat_target"] = DEFAULT_VRCHAT_TARGET
    return config


def save_config_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_setup_wizard(path: Path, input_fn=input) -> bool:
    """初回起動時に、予約サイトの URL とトークンを対話で聞いて保存する。"""
    current = load_config_file(path)
    print("=" * 60)
    print(f" Geaspec Agent {AGENT_VERSION} の初期設定")
    print(" 管理画面で PC を追加したときに表示された情報を入力してください。")
    print("=" * 60)

    def ask(label: str, default: str | None, check) -> str | None:
        hint = f" [{default}]" if default else ""
        while True:
            try:
                value = input_fn(f"{label}{hint}: ").strip() or (default or "")
            except EOFError:
                return None
            error = check(value)
            if not error:
                return value
            print(f"  → {error}")

    server = ask(
        "予約サイトの URL (例: https://geaspec.example.com)",
        current.get("server"),
        lambda v: None if v.startswith(("https://", "http://")) else "http:// か https:// で始まる URL を入力してください",
    )
    if server is None:
        return False
    token = ask(
        "Agent トークン (pcvr_ で始まる文字列)",
        current.get("token"),
        lambda v: None if v.startswith("pcvr_") and len(v) > 10 else "pcvr_ で始まるトークンを貼り付けてください",
    )
    if token is None:
        return False
    vpn = ask(
        "VPN のインターフェース名 (Tailscale なら通常 Tailscale。確認しない場合は - )",
        current.get("vpn_interface", "Tailscale"),
        lambda v: None,
    )
    if vpn is None:
        return False

    data = {**current, "server": server.rstrip("/"), "token": token}
    if vpn and vpn != "-":
        data["vpn_interface"] = vpn
    else:
        data.pop("vpn_interface", None)
    save_config_file(path, data)
    print(f"保存しました: {path}")
    return True


def current_windows_user() -> str:
    return os.environ.get("USERNAME") or getpass.getuser()


def is_confirmed_lending_user(config: dict, user: str | None = None) -> bool:
    confirmed = config.get("lending_user", "")
    return bool(confirmed) and confirmed.lower() == (user or current_windows_user()).lower()


def lending_user_summary(targets: list[Path]) -> str:
    """貸し出し専用ユーザーとして登録する前に見せる説明 (削除するもの・しないもの)。"""
    synced = [t for t in targets if cloud_sync_root_of(t)]
    lines = ["予約が終わるたびに、次のデータを削除します（元に戻せません）:"]
    lines += [f"  - {t}" for t in targets if t not in synced]
    lines.append(f"  - レジストリ HKCU\\{VRCHAT_REGISTRY_KEY}（VRChat のログイン状態と設定）")
    if synced:
        lines.append("")
        lines.append("クラウド同期フォルダの中にあるため、削除しないもの（借り手のデータが残るので要確認）:")
        lines += [f"  - {t}" for t in synced]
    if any(os.environ.get(env) for env in CLOUD_SYNC_ENV):
        lines.append("")
        lines.append("⚠ このユーザーは OneDrive と連携しています。普段使いのユーザーではありませんか？")
    lines.append("")
    lines.append("普段使いのユーザーではなく、貸し出し専用に作ったユーザーで使ってください。")
    return "\n".join(lines)


def register_lending_user(path: Path) -> str:
    user = current_windows_user()
    data = load_config_file(path)
    data["lending_user"] = user
    save_config_file(path, data)
    return user


def confirm_lending_user(path: Path, targets: list[Path], input_fn=input) -> bool:
    """このユーザーを貸し出し専用として登録してよいか、削除対象を見せて確認する。

    普段使いのユーザーに入れてしまい、自分の VRChat データやクラウドの写真を
    消してしまう事故を防ぐため、同意がない限り削除は行わない。
    """
    user = current_windows_user()
    print("=" * 60)
    print(f" Windows ユーザー「{user}」を貸し出し専用として登録します")
    print("=" * 60)
    print(lending_user_summary(targets))
    try:
        answer = input_fn("このユーザーは貸し出し専用で、上のデータを毎回削除してよい場合は yes と入力: ").strip()
    except EOFError:
        return False
    if answer.lower() != "yes":
        print("登録を中止しました。データは削除していません。")
        return False
    register_lending_user(path)
    print(f"「{user}」を貸し出し専用ユーザーとして登録しました。")
    return True


def ensure_lending_user(args: argparse.Namespace, cfg: AgentConfig) -> bool:
    """削除を伴う操作の前に、貸し出し専用ユーザーとして同意済みか確かめる。"""
    if cfg.dry_run:
        return True
    if is_confirmed_lending_user(load_config_file(args.config_path)):
        return True
    if _interactive():
        return confirm_lending_user(args.config_path, cfg.wipe_targets)
    log.error(
        "このユーザー (%s) は貸し出し専用として登録されていないため、削除を行いません。"
        "スタートメニューの「接続設定の変更」から登録してください",
        current_windows_user(),
    )
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def setup_logging(home: Path, dry_run: bool) -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    if sys.stdout is not None:  # pythonw (GUI) では標準出力がない
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    if dry_run:
        return
    try:
        home.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            home / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError as exc:
        log.warning("ログファイルを開けません: %s", exc)


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("0 より大きい値を指定してください")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """引数を解釈する。優先順位: コマンドライン > 環境変数 > 設定ファイル > 既定値"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path)
    known, _ = pre.parse_known_args(argv)
    config_path = known.config or default_config_path()
    file = load_config_file(config_path)

    def env_or_file(env: str, key: str, fallback=None):
        return os.environ.get(env) or file.get(key) or fallback

    parser = argparse.ArgumentParser(description="Geaspec 貸し手PC用 Host Agent")
    parser.add_argument("--version", action="version", version=f"{AGENT_NAME} {AGENT_VERSION}")
    parser.add_argument("--config", type=Path, help="設定ファイル (既定: %%LocalAppData%%\\GeaspecAgent\\config.json)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--duration", type=positive_float, help="セッション時間 (分)")
    mode.add_argument("--cleanup-only", action="store_true", help="プロセス終了と痕跡削除だけを行う")
    mode.add_argument("--server", help="予約サイトの URL。指定するとサーバーの指示でセッションを行う")
    mode.add_argument("--setup", action="store_true", help="予約サイトの URL とトークンを設定し直す")
    parser.add_argument(
        "--token",
        default=env_or_file("PCVR_AGENT_TOKEN", "token"),
        help="管理画面で発行した Agent トークン [env: PCVR_AGENT_TOKEN]",
    )
    parser.add_argument(
        "--poll-interval", type=positive_float, default=float(file.get("poll_interval", 15.0)),
        help="サーバーへの問い合わせ間隔 (秒)",
    )
    parser.add_argument(
        "--streamer",
        choices=sorted(STREAMERS),
        default=env_or_file("PCVR_STREAMER", "streamer", "vd"),
        help="ストリーミングソフト (vd=Virtual Desktop, alvr=ALVR) [env: PCVR_STREAMER]",
    )
    parser.add_argument(
        "--streamer-path",
        type=Path,
        default=Path(file["streamer_path"]) if "streamer_path" in file else None,
        help="ストリーマーの実行ファイル [env: VD_STREAMER_PATH / ALVR_PATH]",
    )
    parser.add_argument(
        "--vrchat-target",
        default=env_or_file("VRCHAT_TARGET", "vrchat_target", DEFAULT_VRCHAT_TARGET),
        help="VRChat の起動先 (steam:// URL または VRChat.exe のパス) [env: VRCHAT_TARGET]",
    )
    parser.add_argument(
        "--vpn-interface",
        default=env_or_file("PCVR_VPN_INTERFACE", "vpn_interface"),
        help="必須とする VPN のネットワークインターフェース名 (例: Tailscale) [env: PCVR_VPN_INTERFACE]",
    )
    parser.add_argument(
        "--registry-baseline",
        type=Path,
        help="削除後に読み込む VRChat 既定設定の .reg ファイル (ログイン情報を含めないこと)",
    )
    parser.add_argument(
        "--keep-screenshots", action="store_true", default=bool(file.get("keep_screenshots", False)),
        help="Pictures\\VRChat を削除しない",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=env_or_file("PCVR_REPORT_DIR", "report_dir"),
        help="セッションレポートのコピー先 (クラウド同期フォルダ等) [env: PCVR_REPORT_DIR]",
    )
    parser.add_argument("--launch-delay", type=float, default=2.0, help="ストリーマー起動から VRChat 起動までの秒数")
    parser.add_argument("--kill-timeout", type=float, default=10.0, help="プロセス終了の待機秒数")
    parser.add_argument("--dry-run", action="store_true", help="実際には終了・削除・起動しない")
    parser.add_argument("--pause", action="store_true", help="終了前に Enter キーを待つ (ショートカット用)")
    args = parser.parse_args(argv)
    args.config_path = config_path

    # 何も指定されなければ、設定ファイルの予約サイトに接続する (ダブルクリック起動)
    if not (args.duration or args.cleanup_only or args.server or args.setup):
        args.server = env_or_file("PCVR_SERVER", "server")
    args.needs_setup = args.setup or not (args.duration or args.cleanup_only or args.server)
    if args.server and not args.token and not args.needs_setup:
        parser.error("--server には --token (または環境変数 PCVR_AGENT_TOKEN・設定ファイル) が必要です")
    return args


def config_from_args(args: argparse.Namespace) -> AgentConfig:
    streamer = STREAMERS[args.streamer]
    streamer_path = args.streamer_path or Path(os.environ.get(streamer["env"], streamer["path"]))
    return AgentConfig(
        duration_min=args.duration,
        vrchat_target=args.vrchat_target,
        streamer=args.streamer,
        streamer_path=streamer_path,
        launch_delay_sec=args.launch_delay,
        kill_timeout_sec=args.kill_timeout,
        vpn_interface=args.vpn_interface,
        registry_baseline=args.registry_baseline,
        keep_screenshots=args.keep_screenshots,
        report_dir=Path(args.report_dir) if args.report_dir else None,
        dry_run=args.dry_run,
    )


def _interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def main(argv: list[str] | None = None) -> int:
    """終了コード: 0=正常, 1=クリーンアップにエラーあり, 2=開始前に中止"""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(f"Geaspec Agent {AGENT_VERSION}")
        except Exception:  # noqa: BLE001 - タイトルは飾りなので失敗しても続ける
            pass
    # 出力先が日本語を表せない文字コードでも、ログのせいで落ちないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    try:
        if args.needs_setup:
            if not _interactive():
                print("予約サイトの URL とトークンが未設定です。--setup で設定してください", file=sys.stderr)
                return 2
            if not run_setup_wizard(args.config_path):
                return 2
            if args.setup:
                config = load_config_file(args.config_path)
                if not is_confirmed_lending_user(config):
                    confirm_lending_user(args.config_path, default_wipe_targets(bool(config.get("keep_screenshots"))))
                return 0
            # 保存した設定で読み込み直して、そのまま待機を始める
            args = parse_args([a for a in (argv or []) if a != "--setup"])
            if args.needs_setup:
                return 2
        return _run(args)
    finally:
        if args.pause and _interactive():
            try:
                input("\nEnter キーで閉じます")
            except EOFError:
                pass


def _run(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    home = agent_home()
    setup_logging(home, cfg.dry_run)
    log.info("%s %s", AGENT_NAME, AGENT_VERSION)

    if sys.platform != "win32" and not cfg.dry_run:
        log.error("このスクリプトは Windows 専用です (動作確認は --dry-run で可能)")
        return 2

    if not cfg.wipe_targets:
        cfg.wipe_targets = default_wipe_targets(cfg.keep_screenshots)
    for target in cfg.wipe_targets:
        log.info("削除対象: %s", target)

    if not ensure_lending_user(args, cfg):
        return 2

    if args.server:
        return serve(cfg, ServerClient(args.server, args.token), home, args.poll_interval)

    agent = HostAgent(cfg, home)
    if args.cleanup_only:
        agent.end_reason = "cleanup_only"
        agent.cleanup_session()
        return 1 if agent.errors else 0

    if not agent.check_vpn():
        return 2

    install_console_handler()
    run_session(agent)
    return 1 if agent.errors else 0


if __name__ == "__main__":
    sys.exit(main())
