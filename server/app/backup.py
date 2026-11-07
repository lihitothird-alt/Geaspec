"""予約サイトのデータベースのバックアップ。

1日1回、データベースのコピーを同じフォルダの backups/ に取る。古いものは KEEP 日分だけ残す。
SQLite の backup を使うので、サイトが動いているあいだでも安全にコピーできる。
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)
KEEP = 14


def sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url[len(prefix):])


def backup_once(database_url: str, today: dt.date | None = None, keep: int = KEEP) -> Path | None:
    src = sqlite_path(database_url)
    if src is None or not src.exists():
        return None
    folder = src.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{src.stem}-{(today or dt.date.today()).isoformat()}.db"
    tmp = dest.with_suffix(".tmp")
    # with sqlite3.connect() は閉じないので、明示的に閉じる (Windows では開いたままだと名前を変えられない)
    a, b = sqlite3.connect(src), sqlite3.connect(tmp)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    tmp.replace(dest)
    for old in sorted(folder.glob(f"{src.stem}-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def start_daily(database_url: str) -> None:
    """起動してすぐ1回、あとは24時間ごとにバックアップする (裏で動く)。"""
    if sqlite_path(database_url) is None:
        return

    def loop() -> None:
        stop = threading.Event()
        while True:
            try:
                path = backup_once(database_url)
                if path:
                    log.info("データベースをバックアップしました: %s", path)
            except Exception:  # バックアップの失敗でサイトを止めない
                log.exception("データベースのバックアップに失敗しました")
            stop.wait(24 * 3600)

    threading.Thread(target=loop, name="db-backup", daemon=True).start()
