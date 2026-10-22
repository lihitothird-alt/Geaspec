"""予約サイトを自動で新しい版にする (運営の Windows PC で、GeaspecSite.cmd から動かしているとき)。

1. サイトが動いている間、ときどき GitHub に新しい版が出ていないか確かめる
2. 出ていたら、GeaspecSite-<版>.zip をダウンロードして、アプリのフォルダの update に展開しておく
3. 予約で使われていない時間になったら、サイトを止めて EXIT_UPDATE で終わる
4. GeaspecSite.cmd が update の中身でファイルを入れ替えて、もう一度起動する

データ (予約・アカウント・写真) は %LocalAppData%\\GeaspecSite にあるので、入れ替えても消えない。
入れ替えの途中で止まっても、update が残っていれば次の起動のときに入れ替え直す。
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import sqlite3
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

REPO = "https://github.com/lihitothird-alt/Geaspec"
# GeaspecSite.cmd はこの終わり方を見て、入れ替えてから起動し直す
EXIT_UPDATE = 75
FIRST_CHECK_SEC = 2 * 60
CHECK_EVERY_SEC = 30 * 60
BUSY_RETRY_SEC = 5 * 60
# 予約の始まる何分前からは入れ替えない (止まるのは 1 分ほどだが、つなぐところで困らないように)
QUIET_BEFORE_MINUTES = 30
STAGE = "update"
READY = "ready.txt"


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+(?:\.\d+)+)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def latest_version(open_url=urllib.request.urlopen) -> str | None:
    """GitHub の「最新の版」のページが、どの版のタグに飛ぶかで調べる (API の回数制限にかからない)。"""
    try:
        with open_url(urllib.request.Request(f"{REPO}/releases/latest", method="HEAD"), timeout=30) as r:
            url = r.geturl()
    except OSError:
        return None
    m = re.search(r"/tag/agent-v(\d+(?:\.\d+)+)$", url)
    return m.group(1) if m else None


def staged_folder(app_dir: Path) -> Path:
    return app_dir / STAGE / "GeaspecSite"


def is_staged(app_dir: Path) -> bool:
    return (app_dir / STAGE / READY).exists()


def download(version: str, app_dir: Path, open_url=urllib.request.urlopen) -> bool:
    """新しい版を update に用意する。中身を確かめてから、最後に ready.txt を置く (途中のものは使わせない)。"""
    stage = app_dir / STAGE
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    archive = stage / f"GeaspecSite-{version}.zip"
    url = f"{REPO}/releases/download/agent-v{version}/GeaspecSite-{version}.zip"
    try:
        with open_url(url, timeout=600) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        folder = staged_folder(app_dir)
        with zipfile.ZipFile(archive) as z:
            z.extractall(folder)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"新しい版 {version} をダウンロードできませんでした（あとでもう一度試します）: {exc}")
        shutil.rmtree(stage, ignore_errors=True)
        return False
    archive.unlink(missing_ok=True)
    version_py = folder / "app" / "version.py"
    complete = ((folder / "python" / "python.exe").exists() and (folder / "site_launcher.py").exists()
                and version_py.exists() and f'VERSION = "{version}"' in version_py.read_text(encoding="utf-8"))
    if not complete:
        print(f"新しい版 {version} のファイルがそろっていませんでした（あとでもう一度試します）。")
        shutil.rmtree(stage, ignore_errors=True)
        return False
    (stage / READY).write_text(version, encoding="utf-8")
    return True


def bookings_busy(db_path: Path, now: dt.datetime | None = None) -> bool:
    """いま使われている予約か、もうすぐ始まる予約があるか (あれば入れ替えを待つ)。"""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    soon = now + dt.timedelta(minutes=QUIET_BEFORE_MINUTES)
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute(
                "SELECT 1 FROM bookings b JOIN slots s ON s.id = b.slot_id "
                "WHERE (b.status = 'active' OR (b.status = 'confirmed' AND s.start_at < ? AND s.end_at > ?)) LIMIT 1",
                (soon.strftime(fmt), now.strftime(fmt)),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False  # まだ予約の表がない (はじめての起動) など
    return row is not None


class Updater:
    """30 分ごとに新しい版を確かめ、用意できて予約もなければ stop() を呼ぶ。"""

    def __init__(self, app_dir: Path, current: str, db_path: Path, stop: Callable[[], None], *,
                 latest=latest_version, fetch=download, busy=bookings_busy, sleep=time.sleep) -> None:
        self.app_dir, self.current, self.db_path, self.stop = app_dir, current, db_path, stop
        self.latest, self.fetch, self.busy, self.sleep = latest, fetch, busy, sleep
        self.ready_version = ""
        # サイトを止めて入れ替える (True のときだけ、EXIT_UPDATE で終わる)
        self.restarting = False

    def check_once(self) -> bool:
        """新しい版を用意できていれば True。"""
        if is_staged(self.app_dir):
            self.ready_version = (self.app_dir / STAGE / READY).read_text(encoding="utf-8").strip()
            return True
        latest = self.latest()
        if not latest or not is_newer(latest, self.current):
            return False
        print(f"新しい版 {latest} が出ています。ダウンロードしています…")
        if not self.fetch(latest, self.app_dir):
            return False
        self.ready_version = latest
        return True

    def run(self) -> None:
        wait = FIRST_CHECK_SEC  # 起動して少ししたら 1 回目 (そのあとは 30 分ごと)
        while True:
            self.sleep(wait)
            wait = CHECK_EVERY_SEC
            try:
                if not self.check_once():
                    continue
            except Exception as exc:  # noqa: BLE001 - 更新を確かめられなくても、サイトは動かし続ける
                print(f"新しい版を確かめられませんでした（あとでもう一度試します）: {exc}")
                continue
            while self.busy(self.db_path):
                self.sleep(BUSY_RETRY_SEC)
            print()
            print(f"新しい版 {self.ready_version} に入れ替えます。1 分ほどサイトが止まり、自動で起動し直します。")
            self.restarting = True
            self.stop()
            return

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="geaspec-update", daemon=True)
        thread.start()
        return thread


def enabled(config: dict, app_dir: Path, platform: str) -> bool:
    """zip を展開して GeaspecSite.cmd から動かしている Windows の PC だけ (開発用やクラウドのサーバーでは使わない)。

    止めたいときは、設定ファイル (site_config.json) に "auto_update": false と書く。
    """
    return (platform == "win32" and config.get("auto_update", True) is not False
            and (app_dir / "python" / "python.exe").exists() and (app_dir / "GeaspecSite.cmd").exists())
