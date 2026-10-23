"""予約サイトの自動更新のテスト。GitHub には行かず、まねをする部品を使う。"""

import datetime as dt
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_update as au  # noqa: E402


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", url=""):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def site_zip(version, complete=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("python/python.exe", b"exe")
        z.writestr("site_launcher.py", "")
        # 中身がそろっていない zip は、ちがう版の version.py が入っているものにする
        z.writestr("app/version.py", f'VERSION = "{version if complete else "0.0.1"}"\n')
    return buf.getvalue()


def test_versions_compare_by_number():
    assert au.is_newer("0.7.32", "0.7.31")
    assert au.is_newer("0.10.0", "0.9.9")  # 文字の順ではなく数の順
    assert not au.is_newer("0.7.31", "0.7.31")
    assert not au.is_newer("", "0.7.31")


def test_latest_version_follows_the_release_page():
    seen = []

    def open_url(req, timeout):
        seen.append(req.get_method())
        return FakeResponse(url=f"{au.REPO}/releases/tag/agent-v0.7.32")

    assert au.latest_version(open_url) == "0.7.32"
    assert seen == ["HEAD"]
    assert au.latest_version(lambda req, timeout: FakeResponse(url=f"{au.REPO}/releases")) is None

    def offline(req, timeout):
        raise OSError("offline")

    assert au.latest_version(offline) is None


def test_download_stages_the_new_version_and_marks_it_ready(tmp_path):
    urls = []

    def open_url(url, timeout):
        urls.append(url)
        return FakeResponse(site_zip("0.7.32"))

    assert au.download("0.7.32", tmp_path, open_url)
    assert urls == [f"{au.REPO}/releases/download/agent-v0.7.32/GeaspecSite-0.7.32.zip"]
    assert au.is_staged(tmp_path)
    assert (au.staged_folder(tmp_path) / "python" / "python.exe").exists()
    assert not list((tmp_path / au.STAGE).glob("*.zip"))  # zip は展開したら消す


def test_incomplete_download_is_thrown_away(tmp_path):
    assert not au.download("0.7.32", tmp_path, lambda url, timeout: FakeResponse(site_zip("0.7.32", complete=False)))
    assert not (tmp_path / au.STAGE).exists()
    assert not au.download("0.7.32", tmp_path, lambda url, timeout: FakeResponse(b"not a zip"))
    assert not (tmp_path / au.STAGE).exists()


def make_db(path, status, start, end):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE slots (id INTEGER PRIMARY KEY, start_at DATETIME, end_at DATETIME)")
    con.execute("CREATE TABLE bookings (id INTEGER PRIMARY KEY, slot_id INTEGER, status VARCHAR)")
    con.execute("INSERT INTO slots VALUES (1, ?, ?)", (f"{start:%Y-%m-%d %H:%M:%S}.000000", f"{end:%Y-%m-%d %H:%M:%S}.000000"))
    con.execute("INSERT INTO bookings VALUES (1, 1, ?)", (status,))
    con.commit()
    con.close()


def test_waits_for_bookings_in_use_or_about_to_start(tmp_path):
    now = dt.datetime(2026, 10, 22, 12, 0)
    hour = dt.timedelta(hours=1)
    cases = [
        ("active", now - 2 * hour, now - hour, True),  # 終わる時間を過ぎても、使っている間は待つ
        ("confirmed", now + dt.timedelta(minutes=20), now + hour, True),  # もうすぐ始まる
        ("confirmed", now - hour, now + hour, True),  # いま使う時間
        ("confirmed", now + 2 * hour, now + 3 * hour, False),  # まだ先
        ("cancelled", now - hour, now + hour, False),
        ("completed", now - hour, now + hour, False),
    ]
    for i, (status, start, end, busy) in enumerate(cases):
        db = tmp_path / f"{i}.db"
        make_db(db, status, start, end)
        assert au.bookings_busy(db, now) is busy, status
    assert au.bookings_busy(tmp_path / "missing.db", now) is False


def test_updater_downloads_waits_for_a_quiet_time_then_stops_the_site(tmp_path):
    stopped, sleeps, busy = [], [], iter([True, True, False])

    def fetch(version, app_dir):
        (app_dir / au.STAGE).mkdir()
        (app_dir / au.STAGE / au.READY).write_text(version)
        return True

    u = au.Updater(tmp_path, "0.7.31", tmp_path / "geaspec.db", lambda: stopped.append(True),
                   latest=lambda: "0.7.32", fetch=fetch, busy=lambda db: next(busy), sleep=sleeps.append)
    u.run()
    assert stopped == [True]
    assert u.ready_version == "0.7.32"
    assert u.restarting
    assert sleeps == [au.FIRST_CHECK_SEC, au.BUSY_RETRY_SEC, au.BUSY_RETRY_SEC]


def test_updater_keeps_checking_when_there_is_nothing_new(tmp_path):
    calls = []

    class Stop(Exception):
        pass

    def sleep(sec):
        calls.append(sec)
        if len(calls) > 3:
            raise Stop

    latest = iter([None, "0.7.31", "0.7.30"])
    u = au.Updater(tmp_path, "0.7.31", tmp_path / "db", lambda: None, latest=lambda: next(latest),
                   fetch=lambda v, d: (_ for _ in ()).throw(AssertionError("no download")), sleep=sleep)
    try:
        u.run()
    except Stop:
        pass
    assert calls == [au.FIRST_CHECK_SEC, au.CHECK_EVERY_SEC, au.CHECK_EVERY_SEC, au.CHECK_EVERY_SEC]
    assert u.ready_version == ""
    assert not u.restarting


def test_only_runs_from_the_windows_bundle(tmp_path):
    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "python.exe").write_text("")
    (tmp_path / "GeaspecSite.cmd").write_text("")
    assert au.enabled({}, tmp_path, "win32")
    assert not au.enabled({"auto_update": False}, tmp_path, "win32")
    assert not au.enabled({}, tmp_path, "linux")
    assert not au.enabled({}, tmp_path.parent, "win32")
