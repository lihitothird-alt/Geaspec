"""お得意さん関係: フォロー・評価。"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from test_app import db, make_app, now, signup  # noqa: F401 - now はフィクスチャ
from app.models import Booking, Host, Review


@pytest.fixture
def world(tmp_path, now):  # noqa: F811
    """運営・貸し手 (承認済みの PC と 2 つの枠)・借り手 2 人。"""
    app, _ = make_app(tmp_path)
    admin = TestClient(app)
    signup(admin, "admin@example.com")
    lender = TestClient(app)
    signup(lender, "lender@example.com")
    lender.post("/account", data={"meta_username": "q_lender", "nickname": "ひかり"})
    body = {"email": "lender@example.com", "password": "password123", "pc_name": "LG", "machine_id": "machine-0001"}
    token = TestClient(app).post("/api/agent/login", json=body).json()["token"]
    admin.post("/admin/hosts/1/approve", data={})
    agent = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    agent.put("/api/agent/schedule", json={
        "start": "2026-10-10T00:00:00Z", "days": 1, "price_yen": 500,
        "starts": ["2026-10-10T10:00:00Z", "2026-10-10T11:00:00Z"],
    })
    alice = TestClient(app)
    signup(alice, "alice@example.com")
    bob = TestClient(app)
    signup(bob, "bob@example.com")
    lender_id = db(app).query(Host).one().owner_id
    return app, lender, agent, alice, bob, lender_id


def visible(client) -> bool:
    return "/slots/1/confirm" in client.get("/slots").text


def test_follow_lender_and_filter_slots(world):
    app, lender, agent, alice, bob, lender_id = world
    page = alice.get("/slots").text
    assert "ひかり さん" in page  # 貸し手の名前 (メールアドレスは出さない)
    assert "lender@example.com" not in page
    assert "/slots/1/confirm" not in alice.get("/slots?view=following").text

    alice.post(f"/lenders/{lender_id}/follow", data={"on": 1})
    assert "/slots/1/confirm" in alice.get("/slots?view=following").text
    assert "フォロー中" in alice.get("/following").text
    assert "フォロワー 1 人" in bob.get(f"/lenders/{lender_id}").text
    assert "フォロワー" in lender.get("/lend").text and "ユーザー3 さん" in lender.get("/lend").text

    alice.post(f"/lenders/{lender_id}/follow", data={"on": 0})
    assert "/slots/1/confirm" not in alice.get("/slots?view=following").text
    # 自分はフォローできない
    assert "自分はフォローできません" in lender.post(f"/lenders/{lender_id}/follow", data={"on": 1}).text
    # 予約は今までどおりだれでもできる
    bob.post("/slots/1/book")
    assert db(app).get(Booking, 1).user.email == "bob@example.com"


def test_followers_get_mail_when_new_times_open(world):
    app, lender, agent, alice, bob, lender_id = world
    alice.post(f"/lenders/{lender_id}/follow", data={"on": 1})
    mails = app.state.mailer.sent
    del mails[:]
    week = {"start": "2026-10-10T00:00:00Z", "days": 1, "price_yen": 500}
    agent.put("/api/agent/schedule", json={**week, "starts": ["2026-10-10T10:00:00Z", "2026-10-10T11:00:00Z", "2026-10-10T12:00:00Z"]})
    assert [(to, subj) for to, subj, _ in mails] == [("alice@example.com", "【Geaspec】ひかり さんが新しい時間を公開しました")]
    # 6 時間のうちは何度保存しても 1 回だけ
    agent.put("/api/agent/schedule", json={**week, "starts": ["2026-10-10T10:00:00Z", "2026-10-10T13:00:00Z"]})
    assert len(mails) == 1


def test_reviews_both_ways_after_the_session(world, now):  # noqa: F811
    app, lender, agent, alice, bob, lender_id = world
    alice.post("/account", data={"meta_username": "q_alice", "nickname": "アリス"})
    alice.post("/slots/1/book")
    # 終わるまでは評価できない
    assert "評価できません" in alice.post("/bookings/1/review", data={"stars": 5}).text

    now["t"] = dt.datetime(2026, 10, 10, 10, 0)
    agent.get("/api/agent/poll")
    agent.post("/api/agent/bookings/1/started")
    now["t"] = dt.datetime(2026, 10, 10, 11, 0)
    agent.post("/api/agent/bookings/1/finished", json={"end_reason": "duration_elapsed", "clean": True})

    page = alice.get("/bookings/1").text
    assert "ひかり さん（貸し手）を評価する" in page and "フォローする" in page
    alice.post("/bookings/1/review", data={"stars": 4, "comment": "  快適でした  "})
    assert "評価を送りました（星 4）" in alice.get("/bookings/1").text
    assert "評価できません" in alice.post("/bookings/1/review", data={"stars": 1}).text  # 2 回目は不可
    profile = alice.get(f"/lenders/{lender_id}").text
    assert "快適でした" in profile and "アリス さん" in profile and "4.0" in profile

    # 貸し手が借り手を評価する (貸し手ページから)
    assert "アリス さんを評価する" in lender.get("/lend").text
    r = lender.post("/bookings/1/review", data={"stars": 5, "comment": "丁寧に使ってくれました"}, follow_redirects=False)
    assert r.headers["location"] == "/lend"
    assert "評価済み" in lender.get("/lend").text
    reviews = db(app).query(Review).order_by(Review.id).all()
    assert [(r.role, r.stars) for r in reviews] == [("borrower", 4), ("lender", 5)]
    # 関係のない人は評価できない・星の範囲外は不可
    assert bob.post("/bookings/1/review", data={"stars": 5}).status_code == 404


def test_nickname_never_shows_an_email(world):
    app, lender, agent, alice, bob, lender_id = world
    r = alice.post("/account", data={"meta_username": "q_alice", "nickname": "a@b.c"})
    assert r.status_code == 400
    alice.post("/account", data={"meta_username": "q_alice", "nickname": "  アリス   さん "})
    assert "アリス さん" in alice.get("/account").text
