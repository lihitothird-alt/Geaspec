"""テストの評価フォーム (/feedback) と、管理画面の集計。"""

import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import feedback  # noqa: E402
from test_app import make_app, signup  # noqa: E402


def full_answers(**over):
    data = {key: "6" for key, _ in feedback.RATINGS}
    data["download_flow"] = "0"  # 使っていない
    for q in feedback.CHOICES:
        data[q["key"]] = q["options"][0][0]
    data["play_times"] = ["2", "5"]
    data.update({"down_mbps": "320.5", "up_mbps": "180", "speed_ping": "", "good": "画面がきれい"})
    data.update(over)
    return data


def test_parse_checks_every_question():
    form = {k: v if isinstance(v, list) else [v] for k, v in full_answers().items()}
    answers = feedback.parse(form)
    assert answers["design"] == 6 and answers["download_flow"] == 0
    assert answers["prefecture"] == "北海道" and answers["play_times"] == ["2", "5"]
    assert answers["down_mbps"] == 320.5 and "speed_ping" not in answers
    for broken in ({"design": ["7"]}, {"prefecture": ["月"]}, {"line": ["1", "2"]}, {"up_mbps": ["はやい"]}):
        try:
            feedback.parse({**form, **broken})
        except feedback.FeedbackError:
            continue
        raise AssertionError(broken)


def test_form_saves_and_admin_sees_summary(tmp_path):
    app, _ = make_app(tmp_path)
    tester = TestClient(app)
    page = tester.get("/feedback?t=12")
    assert page.status_code == 200 and 'name="t" value="12"' in page.text
    assert "この FPS を下回ったら嫌だ" in page.text and "fast.com" in page.text

    # 答えが足りないと、入れた内容を残したまま戻す
    missing = full_answers(t="12")
    del missing["quest"]
    r = tester.post("/feedback", data=missing)
    assert r.status_code == 400 and "使った Quest" in r.text and 'value="320.5"' in r.text

    r = tester.post("/feedback", data=full_answers(t="12", min_fps="3"))
    assert r.status_code == 200 and "ありがとうございました" in r.text
    tester.post("/feedback", data=full_answers(design="2", t="abc"))

    admin = TestClient(app)
    assert admin.get("/admin/feedback", follow_redirects=False).status_code == 303  # ログインしていない
    assert admin.get("/admin/feedback.csv", follow_redirects=False).status_code == 303
    signup(admin, "admin@example.com")
    page = admin.get("/admin/feedback")
    assert page.status_code == 200 and "答え 2 件" in page.text and "4.0" in page.text  # (6+2)/2
    rows = list(csv.reader(io.StringIO(admin.get("/admin/feedback.csv").content.decode("utf-8-sig"))))
    assert rows[0][1] == "テストの回" and rows[1][1] == "12" and rows[2][1] == ""
    record = dict(zip(rows[0], rows[1]))
    assert record["この FPS を下回ったら嫌だ、という線"] == "60"
    assert record["上り（アップロード）(Mbps)"] == "180.0"
    assert record["アプリのダウンロード・インストールまでの流れ（使った人だけ）"] == ""

    # Discord ボットはサイトのデータベースから、テストの回ごとに読み出せる
    loaded = feedback.load_from_site_db(tmp_path / "test.db")
    assert list(loaded) == [12] and loaded[12]["min_fps"] == "3"
    assert json.dumps(loaded[12], ensure_ascii=False)


def test_feedback_is_behind_the_site_password(tmp_path):
    app, _ = make_app(tmp_path, site_password="aikotoba")
    r = TestClient(app).get("/feedback?t=3", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/site-login?next=/feedback")
