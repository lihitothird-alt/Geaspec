"""テスター用 Discord ボット: 記録・集計と、ボタンを押したときの流れ。

Discord には接続せず、ボタンを押したときに届くもの (Interaction) を作り物で置き換えて確かめる。
"""

import asyncio
import csv
import datetime as dt
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

from app import tester_bot as tb  # noqa: E402
from app import tester_store as ts  # noqa: E402
from app.tester_store import QUESTIONS, format_duration, summary_lines  # noqa: E402

TesterStore = ts.TesterStore

T0 = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)


def at(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


@pytest.fixture
def store(tmp_path):
    s = TesterStore(tmp_path / "tester_bot.db")
    yield s
    s.close()


def apply(store, user_id, grp="A", has_vd=True, thread_id=None):
    store.apply(user_id, f"user{user_id}", vrchat=f"vrc{user_id}", meta=f"meta{user_id}", gear="Quest 3",
                region="関東", schedule="土日", now=T0)
    store.set_group(user_id, grp)
    store.set_has_vd(user_id, has_vd)
    store.set_thread(user_id, thread_id or user_id * 10)
    store.mark_ready(user_id, T0)


ALL_GOOD = {"ease": "5", "quality": "5", "stutter": "5", "sick": "none", "compare": "5", "timer": "good", "again": "yes",
            "nps": "10", "price": "600", "when": ["event", "heavy"]}


def run_session(store, user_id, enter_min=3.0, answers=ALL_GOOD, helps=0):
    s, created = store.start_session(user_id, user_id * 10, at(0))
    assert created
    for _ in range(helps):
        store.add_help(s["id"], "つなぐところ", at(1))
    store.mark_entered(s["id"], at(enter_min))
    store.finish_play(s["id"], at(55), connected=True)
    for key, value in answers.items():
        store.answer(s["id"], key, value)
    store.complete(s["id"], at(56), comment="たのしかった")
    return s["id"]


def test_one_open_session_per_thread(store):
    apply(store, 1)
    first, created = store.start_session(1, 10, at(0))
    again, created_again = store.start_session(1, 10, at(1))
    assert created and not created_again and again["id"] == first["id"]
    assert store.mark_entered(first["id"], at(4.5)) == 270
    # 2 回押しても最初の時刻のまま
    assert store.mark_entered(first["id"], at(9)) == 270


def test_survey_goes_in_order_and_rejects_bad_answers(store):
    apply(store, 1)
    s, _ = store.start_session(1, 10, at(0))
    store.finish_play(s["id"], at(55), connected=True)
    assert store.answer(s["id"], "ease", "4") == "quality"
    with pytest.raises(ValueError):
        store.answer(s["id"], "quality", "9")
    with pytest.raises(ValueError):
        store.answer(s["id"], "price", ["200", "400"])  # 1 つだけえらぶ質問
    assert store.answer(s["id"], "when", ["photo", "friends"]) == "quality"  # 飛ばした質問が先に来る
    assert json.loads(store.session(s["id"])["answers"])["when"] == ["photo", "friends"]


def test_summary_marks_targets(store):
    apply(store, 1, grp="A")
    apply(store, 2, grp="A")
    apply(store, 3, grp="B", has_vd=False)
    run_session(store, 1, enter_min=2)
    run_session(store, 2, enter_min=4, helps=2,
                answers=dict(ALL_GOOD, quality="3", again="no", nps="5", price="200", sick="bad"))
    # 3 人目はつながらなかった
    s, _ = store.start_session(3, 30, at(0))
    store.finish_play(s["id"], at(10), connected=False)
    store.complete(s["id"], at(11), stuck="VD の一覧に出てこない")

    summary = store.summary()
    assert summary["applied"] == 3 and summary["sessions"] == 3 and summary["done"] == 3
    assert summary["connected_rate"] == pytest.approx(0.667)
    assert summary["enter_median_sec"] == 180
    assert summary["help_ok_rate"] == pytest.approx(0.667)
    assert summary["quality_avg"] == 4
    assert summary["sick_count"] == 1
    assert summary["again_rate"] == 0.5
    assert summary["nps_top_rate"] == 0.5
    assert summary["price_median"] == 200
    assert summary["has_vd_rate"] == pytest.approx(0.667)
    assert summary["groups"]["A"]["sessions"] == 2

    text = "\n".join(summary_lines(summary))
    assert "⚠️ つながった割合: 67%" in text
    assert "✅ ワールドに入るまで（真ん中の値）: 3分00秒" in text
    assert "✅ 画質・遅れの満足度: 4" in text
    assert "〜200円" in text


def test_summary_with_no_data(store):
    lines = summary_lines(store.summary())
    assert lines[0].startswith("申し込み 0人")
    assert all("None" not in line for line in lines)


def test_csv_opens_with_labels(store):
    apply(store, 1)
    run_session(store, 1)
    rows = list(csv.reader(io.StringIO(store.to_csv())))
    header, row = rows[0], rows[1]
    record = dict(zip(header, row))
    assert record["ワールドに入るまで(秒)"] == "180"
    assert record["つながった"] == "はい"
    assert record[QUESTIONS[-1]["text"]] == "イベント・重いワールド"
    assert record["ひとこと"] == "たのしかった"
    assert "meta1" not in store.to_csv()  # Meta ユーザー名は CSV に出さない


def test_erase_keeps_answers(store):
    apply(store, 1)
    run_session(store, 1)
    assert store.erase_personal() == 1
    t = store.tester(1)
    assert t["meta"] == "" and t["vrchat"] == "" and t["grp"] == "A"
    assert store.summary()["quality_avg"] == 5


def test_helpers():
    assert tb.parse_int("約 72 fps") == 72
    assert tb.parse_int("") is None
    assert format_duration(185) == "3分05秒"
    assert format_duration(42) == "42秒"


# ---- ボタンを押したときの流れ ----

class Recorder:
    def __init__(self):
        self.sent = []


class FakeResponse:
    def __init__(self, log):
        self.log = log
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.log.append(("send", content, kw))

    async def edit_message(self, **kw):
        self.done = True
        self.log.append(("edit", kw.get("content"), kw))

    async def send_modal(self, modal):
        self.done = True
        self.log.append(("modal", modal, {}))

    async def defer(self, **kw):
        self.done = True


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kw):
        self.log.append(("followup", content, kw))


class FakeUser:
    def __init__(self, uid, name="テスター"):
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"


class FakeThread:
    def __init__(self, tid):
        self.id = tid
        self.mention = f"<#{tid}>"
        self.archived = False
        self.users = []
        self.sent = []

    async def add_user(self, user):
        self.users.append(user.id)

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


class FakeChannel:
    def __init__(self):
        self.threads = []

    async def create_thread(self, **kw):
        assert kw["type"] is discord.ChannelType.private_thread and kw["invitable"] is False
        thread = FakeThread(900 + len(self.threads))
        self.threads.append(thread)
        return thread


class FakeInteraction:
    def __init__(self, user, channel_id, custom_id="", values=None, channel=None):
        self.log = []
        self.type = discord.InteractionType.component
        self.user = user
        self.channel_id = channel_id
        self.channel = channel
        self.data = {"custom_id": custom_id}
        if values is not None:
            self.data["values"] = values
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)


class Field:
    def __init__(self, value):
        self.value = value


def custom_ids(view):
    return [item.custom_id for item in view.children]


def test_full_flow_from_apply_to_survey(tmp_path, monkeypatch):
    async def scenario():
        store = TesterStore(tmp_path / "bot.db")
        now = [at(0)]
        bot = tb.TesterBot(store, tmp_path / "shots", "https://lg.example.ts.net", site_password="aikotoba",
                           clock=lambda: now[0])
        store.set_setting("worlds", "ひまり旅館、日本語話者向け集会場")
        ops_log = []

        async def notify_ops(content, view=None):
            ops_log.append((content, view))

        monkeypatch.setattr(bot, "notify_ops", notify_ops)
        threads = {}

        async def existing_thread(tester):
            return threads.get(tester.get("thread_id"))

        monkeypatch.setattr(bot, "_existing_thread", existing_thread)
        tester, ops = FakeUser(1, "ひかり"), FakeUser(99, "運営")
        monkeypatch.setattr(bot, "is_ops", lambda user: user.id == 99)

        async def press(user, channel_id, custom_id, values=None, channel=None):
            i = FakeInteraction(user, channel_id, custom_id, values, channel)
            await bot.on_interaction(i)
            return i

        # 申し込み → 専用スレッドができる
        panel = FakeChannel()
        i = await press(tester, 1, "gs:apply", channel=panel)
        # まず、借りる側か貸す側かをえらぶ
        assert custom_ids(i.log[0][2]["view"]) == ["gs:as:borrow", "gs:as:lend"] and i.log[0][2]["ephemeral"]
        i = await press(tester, 1, "gs:as:borrow", channel=panel)
        assert isinstance(i.log[0][1], tb.ApplyModal) and not i.log[0][1].lend
        modal = i.log[0][1]
        for name, value in dict(vrchat="hikari", meta="hikari_meta", gear="Quest 3", region="関西", schedule="").items():
            setattr(modal, name, Field(value))
        submit = FakeInteraction(tester, 1, channel=panel)
        await bot.on_apply(submit, modal)
        thread = panel.threads[0]
        threads[thread.id] = thread
        assert thread.users == [1]
        assert custom_ids(thread.sent[0][1]["view"]) == ["gs:vd"]
        # 申し込んだらすぐ、短いカードが 1 枚だけ届く (運営も、2つの答えも待たない)。長い説明は Web サイト
        assert store.tester(1)["status"] == "ready" and len(thread.sent) == 2
        assert "**① 予約する**（1/3）" in thread.sent[-1][0]
        pages = "\n".join(f"{t}\n{b}" for t, b in bot.card_pages("borrow", store.tester(1)))
        assert "VPN を切る" in pages and "https://lg.example.ts.net/guide" in pages and "Allow remote connections" not in pages
        # 「次へ」を押すと、同じメッセージが次の手順に変わる
        i = await press(tester, thread.id, "gs:pg:borrow:1")
        assert i.log[0][0] == "edit" and "**② 当日までの準備**（2/3）" in i.log[0][1]
        assert custom_ids(i.log[0][2]["view"])[:2] == ["gs:pg:borrow:0", "gs:pg:borrow:2"]
        assert any("hikari_meta" in c for c, _ in ops_log)
        sent_before = len(thread.sent)

        # 2つの答えは記録だけ。VD を持っている A の人には、追加の案内は出ない
        await press(tester, thread.id, "gs:group", ["A"])
        await press(tester, thread.id, "gs:vd", ["1"])
        assert store.tester(1)["grp"] == "A" and store.tester(1)["has_vd"] == 1
        assert len(thread.sent) == sent_before
        # 前の版の「VD に登録した」ボタンは、運営以外は押せない (押しても 2 回は送らない)
        i = await press(tester, 5, "gs:ready:1")
        assert "運営だけ" in i.log[0][1]
        await press(ops, 5, "gs:ready:1")
        assert len(thread.sent) == sent_before
        ready_text = thread.sent[-1][0]
        assert "Virtual Desktop**（Meta" not in ready_text  # VD を持っている人には出さない
        assert "https://lg.example.ts.net" in ready_text and "`aikotoba`" in ready_text
        assert "平日は19時以降、土日は終日" in pages and "予約ページの「テストのチェックリスト」" in pages
        assert custom_ids(thread.sent[-1][1]["view"]) == ["gs:pg:borrow:-1", "gs:pg:borrow:1", "gs:link", "gs:bookhelp"]

        # 予約で迷った → 予約した (前の版のメッセージのボタン。準備から予約までの時間を記録し、チェックリストを出す)
        await press(tester, thread.id, "gs:bookhelp")
        now[0] = at(0.5)
        await press(tester, thread.id, "gs:booked")
        await press(tester, thread.id, "gs:booked")  # 2 回押しても最初の時刻のまま
        t = store.tester(1)
        assert t["booking_help"] == 1 and t["booked_at"] is not None
        assert sum("予約できました" in c for c, _ in ops_log) == 1
        checklist, kw = thread.sent[-1]
        assert "テストのチェックリスト" in checklist and "✅ 1. 予約する" in checklist and "⬜ 2." in checklist
        assert custom_ids(kw["view"]) == ["gs:start"]

        # 準備のあとで Meta ユーザー名を直すと、VD も直すよう運営に知らせる
        modal = (await press(tester, 1, "gs:as:borrow", channel=panel)).log[0][1]
        assert modal.meta.default == "hikari_meta"
        for name, value in dict(vrchat="hikari", meta="hikari_meta2", gear="Quest 3", region="関西", schedule="").items():
            setattr(modal, name, Field(value))
        await bot.on_apply(FakeInteraction(tester, 1, channel=panel), modal)
        assert len(panel.threads) == 1  # スレッドは作り直さない
        assert "`hikari_meta` → `hikari_meta2`" in ops_log[-1][0] and "Virtual Desktop Streamer" in ops_log[-1][0]
        assert ops_log[-1][1] is None and store.tester(1)["status"] == "ready"

        # 別の人はスレッドのボタンを押せない
        i = await press(FakeUser(2), thread.id, "gs:start")
        assert "テスターだけ" in i.log[0][1]

        # 開始 → 困った → 入れた。チェックリストは進むたびに、いちばん下に出し直す
        now[0] = at(1)
        i = await press(tester, thread.id, "gs:start")
        assert custom_ids(i.log[0][2]["view"]) == ["gs:entered", "gs:help", "gs:giveup"]
        assert "「LG」を選ぶ" in i.log[0][1]  # サイトにつないでいないときは、設定の PC 名
        assert "✅ 2." in thread.sent[-1][0] and "⬜ 3." in thread.sent[-1][0]
        now[0] = at(3)
        await press(tester, thread.id, "gs:help")
        now[0] = at(4.5)
        i = await press(tester, thread.id, "gs:entered")
        assert "3分30秒" in i.log[0][1]
        assert "gs:done" in custom_ids(i.log[1][2]["view"])
        assert "✅ 3." in thread.sent[-1][0] and custom_ids(thread.sent[-1][1]["view"]) == ["gs:perf"]

        # 写真が撮れなかったので数字で入れる → 「4. 写真」に ✅
        i = await press(tester, thread.id, "gs:perf")
        perf = i.log[0][1]
        perf.fps_inputs, perf.ping = [Field("72"), Field("")], Field("40")
        await bot.on_perf(FakeInteraction(tester, thread.id), perf)
        assert "✅ 4." in thread.sent[-1][0] and "送信してテストを終える」ボタンが出ます" in thread.sent[-1][0]

        # お願いのチェック。FPS と Ping は数字を入れずに、写真をスレッドに貼ってもらう
        i = await press(tester, thread.id, "gs:m:heavy")
        assert i.log[0][2]["view"].children[0].label.startswith("✅")
        assert "gs:perf" not in custom_ids(i.log[0][2]["view"])

        # 遊び終わった → アンケートを全部ボタンで答える
        now[0] = at(56)
        i = await press(tester, thread.id, "gs:done")
        view = i.log[1][2]["view"]
        for q in QUESTIONS:
            if q.get("skip_if", {}).get("own_pc") == "vr":
                continue  # 最後の選択肢「VR ができるパソコンがある」を選んだので、PC の値段の質問は出ない
            if q.get("multi"):
                assert custom_ids(view) == [f"gs:a:{q['key']}"]
                i = await press(tester, thread.id, f"gs:a:{q['key']}", [q["options"][0][0]])
            else:
                assert f"gs:a:{q['key']}:{q['options'][-1][0]}" in custom_ids(view)
                i = await press(tester, thread.id, f"gs:a:{q['key']}:{q['options'][-1][0]}")
            view = i.log[0][2]["view"]
        assert custom_ids(view) == ["gs:comment"]
        # アンケートが終わると、チェックリストに「送信」が出る
        assert "✅ 6." in thread.sent[-1][0] and custom_ids(thread.sent[-1][1]["view"]) == ["gs:submit"]

        # ひとこと (任意) は記録だけで、まだ終わらない
        i = await press(tester, thread.id, "gs:comment")
        comment = i.log[0][1]
        comment.stuck, comment.comment = Field("一覧で迷った"), Field("きれいだった")
        noted = FakeInteraction(tester, thread.id)
        await bot.on_comment(noted, comment)
        assert "送信してテストを終える" in noted.log[0][1] and store.latest_session(thread.id)["status"] == "survey"
        assert not any("テストが完了" in c for c, _ in ops_log)

        # 送信 → 運営に届いて完了
        done = await press(tester, thread.id, "gs:submit")
        assert "送信しました。テストは完了です" in done.log[0][1] and custom_ids(done.log[0][2]["view"]) == []
        assert "ありがとうございました" in done.log[1][1]
        assert "/feedback" not in done.log[1][1] and "写真" in done.log[1][1]  # アンケートは Discord だけで終わる
        assert custom_ids(done.log[1][2]["view"]) == ["gs:start"]

        s = store.latest_session(thread.id)
        assert s["status"] == "done" and s["help_count"] == 1
        assert json.loads(s["missions"]) == {"heavy": True}
        assert json.loads(s["answers"])["price"] == "1001"
        assert s["stuck"] == "一覧で迷った" and s["comment"] == "きれいだった" and s["ping_ms"] == 40
        assert any("テストが完了" in c for c, _ in ops_log)

        # 終わったあとに古いボタンを押しても記録は変わらない
        i = await press(tester, thread.id, "gs:a:nps:0")
        assert "終わっています" in i.log[0][1]
        assert json.loads(store.session(s["id"])["answers"])["nps"] == "10"
        store.close()

    asyncio.run(scenario())


def test_giveup_records_not_connected(tmp_path, monkeypatch):
    async def scenario():
        store = TesterStore(tmp_path / "bot.db")
        apply(store, 1, thread_id=500)
        bot = tb.TesterBot(store, tmp_path / "shots", "https://x", clock=lambda: at(0))

        async def notify_ops(content, view=None):
            pass

        monkeypatch.setattr(bot, "notify_ops", notify_ops)
        monkeypatch.setattr(bot, "is_ops", lambda user: False)
        tester = FakeUser(1)
        for cid in ("gs:start", "gs:giveup"):
            i = FakeInteraction(tester, 500, cid)
            await bot.on_interaction(i)
        assert custom_ids(i.log[1][2]["view"]) == ["gs:comment", "gs:skip"]
        i = FakeInteraction(tester, 500, "gs:skip")
        await bot.on_interaction(i)
        s = store.latest_session(500)
        assert s["status"] == "done" and s["connected"] == 0 and s["entered_at"] is None
        assert store.summary()["connected_rate"] == 0
        store.close()

    asyncio.run(scenario())


def test_bot_retries_until_discord_answers(tmp_path, monkeypatch):
    attempts, said = [], []

    async def fake_start(self, token):
        attempts.append(self.intents.message_content)
        if len(attempts) == 1:
            raise discord.PrivilegedIntentsRequired(None)
        if len(attempts) == 2:
            raise OSError("ネットがない")

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(tb.TesterBot, "start", fake_start)
    monkeypatch.setattr(tb.asyncio, "sleep", no_wait)
    store = TesterStore(tmp_path / "bot.db")
    asyncio.run(tb._run("t", store, tmp_path, "u", said.append))
    # スクショ用の権限がなければ、それなしでつなぎ直す。ネットが切れていたら待ってやり直す
    assert attempts == [True, False, False]
    assert "Message Content Intent" in said[0] and "1分後" in said[1]
    store.close()


# ---- はじめての準備 (ロールとチャンネルを自動で作る) ----

class FakeRole:
    def __init__(self, rid, name, position, mentionable=False):
        self.id, self.name, self.position, self.mentionable = rid, name, position, mentionable

    def __lt__(self, other):
        return self.position < other.position


class FakeGuildChannel(FakeThread):
    def __init__(self, cid, name, overwrites=None):
        super().__init__(cid)
        self.name = name
        self.overwrites = overwrites or {}

    async def edit(self, name=None, reason=None):
        self.name = name


class FakeMember:
    def __init__(self, uid):
        self.id = uid
        self.roles = []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)


class FakeGuild:
    def __init__(self, manage=True):
        from types import SimpleNamespace

        self.default_role = FakeRole(0, "@everyone", 0)
        self.roles = [self.default_role]
        self.text_channels = [FakeGuildChannel(1, "雑談")]
        self.forums, self.threads = [], []
        self.me = FakeMember(50)
        self.me.display_name, self.me.top_role = "Geaspec", FakeRole(50, "Geaspec", 10)
        self.me.guild_permissions = SimpleNamespace(manage_roles=manage, manage_channels=manage)
        self.owner_id = 7
        self.owner = FakeMember(7)

    def get_member(self, uid):
        return self.owner if uid == self.owner_id else None

    async def fetch_member(self, uid):
        return self.get_member(uid)

    async def create_role(self, name, mentionable, reason=None):
        role = FakeRole(100 + len(self.roles), name, 1, mentionable)
        self.roles.append(role)
        return role

    async def create_text_channel(self, name, reason=None, overwrites=None):
        channel = FakeGuildChannel(200 + len(self.text_channels), name, overwrites)
        self.text_channels.append(channel)
        return channel


def test_first_setup_builds_role_and_channels(tmp_path):
    async def scenario():
        store = TesterStore(tmp_path / "bot.db")
        said = []
        bot = tb.TesterBot(store, tmp_path, "https://x", say=said.append)

        # 権限がないときは、何をオンにすればいいかを伝える
        await bot.first_setup(FakeGuild(manage=False))
        assert "ロールの管理" in said[-1] and "チャンネルの管理" in said[-1]
        assert store.get_setting("panel_channel_id") == ""

        guild = FakeGuild()
        await bot.first_setup(guild)
        assert "用意しました" in said[-1]
        role = next(r for r in guild.roles if r.name == tb.OPS_ROLE_NAME)
        assert role.mentionable and role in guild.owner.roles
        ops = next(c for c in guild.text_channels if c.name == tb.OPS_CHANNEL_NAME)
        panel = next(c for c in guild.text_channels if c.name == tb.PANEL_CHANNEL_NAME)
        assert ops.overwrites[guild.default_role].view_channel is False
        assert ops.overwrites[role].view_channel is True
        everyone = panel.overwrites[guild.default_role]
        assert everyone.send_messages is False and everyone.send_messages_in_threads is True
        assert custom_ids(panel.sent[0][1]["view"]) == ["gs:apply"]
        assert "準備ができました" in ops.sent[0][0]
        assert store.get_setting("ops_role_id") == str(role.id)
        assert store.get_setting("pc_name") == "LG"

        # もう一度実行しても、作り直さず、ボタンも増やさない。PC の名前だけ変えられる
        await bot.prepare(guild, pc_name="LG-PC")
        assert len(guild.text_channels) == 3 and len(guild.roles) == 2
        assert len(panel.sent) == 1
        assert store.get_setting("pc_name") == "LG-PC"
        store.close()

    asyncio.run(scenario())


# ---- 3 日後の質問・グループ C・評価フォームとつなげた表 ----

def make_bot(tmp_path, monkeypatch, now, threads=None, ops_log=None):
    store = TesterStore(tmp_path / "bot.db")
    bot = tb.TesterBot(store, tmp_path / "shots", "https://x", clock=lambda: now[0])

    async def notify_ops(content, view=None):
        (ops_log if ops_log is not None else []).append(content)

    async def existing_thread(tester):
        return (threads or {}).get(tester.get("thread_id"))

    monkeypatch.setattr(bot, "notify_ops", notify_ops)
    monkeypatch.setattr(bot, "_existing_thread", existing_thread)
    monkeypatch.setattr(bot, "is_ops", lambda user: user.id == 99)
    return store, bot


def test_followup_buttons_from_older_messages_still_work(tmp_path, monkeypatch):
    """3 日後の質問はもう送らない。前の版で届いたメッセージのボタンは、押せば記録する。"""
    async def scenario():
        now = [at(0)]
        thread = FakeThread(10)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread}, ops_log)
        apply(store, 1, thread_id=10)
        first = run_session(store, 1)
        second = run_session(store, 1)
        assert not hasattr(bot, "send_followups")

        tester = FakeUser(1)
        for cid in ("gs:f:again_3d:maybe", "gs:f:told:yes", "gs:f:photos_3d:got", "gs:f:price_3d:800"):
            i = FakeInteraction(tester, 10, cid)
            await bot.on_interaction(i)
        assert "ありがとうございました" in i.log[0][1]
        answers = json.loads(store.session(second)["answers"])
        assert answers["again_3d"] == "maybe" and answers["price_3d"] == "800"
        assert "again_3d" not in json.loads(store.session(first)["answers"])
        assert store.summary()["again_3d_rate"] == 1.0 and store.summary()["told_rate"] == 1.0
        assert any("3日後の質問に答えました" in c for c in ops_log)
        store.close()

    asyncio.run(scenario())


def test_group_c_gets_lender_guide(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(30)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {30: thread}, ops_log)
        store.apply(3, "pc持ち", vrchat="v", meta="m3", gear="RTX 4080", region="関東", schedule="", now=T0)
        store.set_group(3, "C")
        store.set_thread(3, 30)
        store.set_setting("hours", "土日だけ")
        assert await bot.send_ready_now(store.tester(3)) is True
        (ready, ready_kw), (text, kw) = thread.sent[-2], thread.sent[-1]
        assert "① 予約する" in ready and "土日だけ" in "\n".join(b for _, b in bot.card_pages("borrow", store.tester(3)))
        assert "**① Virtual Desktop Streamer を入れる**（1/4）" in text
        lend = "\n".join(f"{t}\n{b}" for t, b in bot.card_pages("lend", store.tester(3)))
        assert "GeaspecAgent-Setup-" in lend and "詳細情報" in lend and "/lend" in lend
        # データが消えたかの確認は、貸し終わったときに届く (申し込んだときには、まだ出さない)
        assert "データが消えているか" not in lend
        assert custom_ids(kw["view"]) == ["gs:pg:lend:-1", "gs:pg:lend:1", "gs:lenderapp", "gs:link"]
        await bot.send_extras(thread, store.tester(3))  # 2 回は送らない
        assert thread.sent[-1][0] == text and len(thread.sent) == 2

        # 貸したあとにデータが消えたかを確かめて押す。残っていたら運営を呼ぶ
        i = FakeInteraction(FakeUser(3), 30, "gs:wipe:ok")
        await bot.on_interaction(i)
        assert "1回目" in i.log[0][1] and "消えていることを確かめました" in ops_log[-1]
        i = FakeInteraction(FakeUser(3), 30, "gs:wipe:left")
        await bot.on_interaction(i)
        assert "何が残っていたか" in i.log[0][1] and "要確認" in ops_log[-1]
        i = FakeInteraction(FakeUser(3), 30, "gs:lenderapp")
        await bot.on_interaction(i)
        assert "承認" in ops_log[-1] and store.summary()["lender_app"] == 1
        store.close()

    asyncio.run(scenario())


def test_panel_shows_hours():
    assert "平日は19時以降、土日は終日" in tb.panel_text(tb.DEFAULT_HOURS)
    assert "https://x/guide" in tb.panel_text("土日", "https://x") and "/guide" not in tb.panel_text("土日")
    assert "次へ" in tb.panel_text("土日")  # くわしいことはカードと Web サイトで


def test_csv_joins_the_feedback_form(store):
    apply(store, 1)
    sid = run_session(store, 1)
    form = {"design": 5, "download_flow": 0, "prefecture": "大阪府", "down_mbps": 512.0, "min_fps": "3", "good": "速い"}
    rows = list(csv.reader(io.StringIO(store.to_csv({sid: form}))))
    record = dict(zip(rows[0], rows[1]))
    assert record["フォーム: 住んでいる都道府県"] == "大阪府"
    assert record["フォーム: 下り（ダウンロード）(Mbps)"] == "512.0"
    assert record["フォーム: この FPS を下回ったら嫌だ、という線"] == "60"
    assert record["フォーム: サイトの見た目（デザイン）"] == "5"
    assert record["フォーム: アプリのダウンロード・インストールまでの流れ（使った人だけ）"] == ""
    assert record["準備できてから予約まで(分)"] == ""
    # フォームがなければ、その列は付けない
    assert not any(h.startswith("フォーム:") for h in next(csv.reader(io.StringIO(store.to_csv()))))


def test_long_durations():
    assert format_duration(3 * 3600 + 120) == "3時間2分"
    assert format_duration(2 * 86400 + 5 * 3600) == "2日5時間"


def test_default_worlds_and_fps_by_world(tmp_path, store):
    async def scenario():
        bot = tb.TesterBot(store, tmp_path, "https://x")
        assert bot.worlds() == ["日本語話者向け集会場", "Japan Shrine", "Nagisa"]
        assert len(tb.PerfModal(bot, bot.worlds()).children) == 4  # FPS × 3 ワールド + Ping (Discord の入力欄は 5 つまで)
        store.set_setting("worlds", "A, B, C, D")
        assert bot.worlds() == ["A", "B", "C"]

    asyncio.run(scenario())
    apply(store, 1)
    apply(store, 2)
    a, b = run_session(store, 1), run_session(store, 2)
    store.set_performance(a, fps_by_world={"Nagisa": 60, "Japan Shrine": 80}, ping_ms=25)
    store.set_performance(b, fps_by_world={"Nagisa": 70, "Japan Shrine": None})
    assert store.summary()["fps_by_world"] == {"Nagisa": 65, "Japan Shrine": 80}
    rows = list(csv.reader(io.StringIO(store.to_csv())))
    assert "FPS: Nagisa" in rows[0] and dict(zip(rows[0], rows[2]))["FPS: Japan Shrine"] == ""
    assert "Nagisa 65" in "\n".join(summary_lines(store.summary()))


def test_new_version_is_announced_once(tmp_path, store):
    from app import version

    async def scenario():
        said = []
        bot = tb.TesterBot(store, tmp_path, "https://x", say=said.append)
        # 作る権限がなければ、書かずに知らせる
        assert await bot.announce_version(FakeGuild(manage=False)) is False
        assert "権限" in said[-1]
        # どこにもなければ「📢更新情報」を作って書く。テスターは書き込めない
        guild = FakeGuild()
        assert await bot.announce_version(guild) is True
        news = next(c for c in guild.text_channels if c.name == tb.NEWS_CHANNEL_NAME)
        assert news.overwrites[guild.default_role].send_messages is False
        assert version.RELEASE_URL in news.sent[0][0] and version.VERSION in news.sent[0][0]
        assert await bot.announce_version(guild) is False  # 同じ版では 2 回書かない
        assert len(news.sent) == 1
        # すでに「更新情報」を名前に含むチャンネルがあれば、そこに書く
        store.set_setting("announced_version", "")
        other = FakeGuild()
        mine = FakeGuildChannel(9, "お知らせ・更新情報")
        other.text_channels.append(mine)
        assert await bot.announce_version(other) is True
        assert len(mine.sent) == 1 and len(other.text_channels) == 2

    asyncio.run(scenario())


def test_old_channel_names_lose_their_emoji(tmp_path, store):
    async def scenario():
        bot = tb.TesterBot(store, tmp_path, "https://x", say=lambda _: None)
        guild = FakeGuild()
        guild.text_channels += [FakeGuildChannel(5, "🙋テスター申し込み"), FakeGuildChannel(6, "📢更新情報")]
        await bot.rename_old_channels(guild)
        assert [c.name for c in guild.text_channels] == ["雑談", tb.PANEL_CHANNEL_NAME, tb.NEWS_CHANNEL_NAME]
        assert "🙋" not in tb.PANEL_CHANNEL_NAME and "📢" not in tb.NEWS_CHANNEL_NAME

    asyncio.run(scenario())


def test_bot_avatar_is_set_once(tmp_path, store):
    from types import SimpleNamespace

    class FakeBotUser:
        def __init__(self, fail=False):
            self.edits, self.fail = [], fail

        async def edit(self, **kw):
            if self.fail:
                raise discord.HTTPException(SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited")
            self.edits.append(kw)

    async def scenario():
        said = []
        bot = tb.TesterBot(store, tmp_path, "https://x", say=said.append)
        # 失敗したら次に起動したときにもう一度試す
        await bot.ensure_avatar(FakeBotUser(fail=True))
        assert "アイコン" in said[-1] and not store.get_setting("avatar")
        user = FakeBotUser()
        await bot.ensure_avatar(user)
        assert user.edits[0]["avatar"] == tb.AVATAR_PATH.read_bytes() and user.edits[0]["avatar"][:4] == b"\x89PNG"
        await bot.ensure_avatar(user)  # 同じアイコンでは 2 回変えない
        assert len(user.edits) == 1

    asyncio.run(scenario())


def test_guide_channels_are_made_once(tmp_path, store):
    async def scenario():
        said = []
        bot = tb.TesterBot(store, tmp_path, "https://geaspec.example", say=said.append)
        # 作る権限がなければ、作らずに知らせる
        await bot.ensure_guide_channels(FakeGuild(manage=False))
        assert "権限" in said[-1] and not store.get_setting("guide_channels")
        guild = FakeGuild()
        panel = FakeGuildChannel(5, tb.PANEL_CHANNEL_NAME)
        guild.text_channels.append(panel)
        store.set_setting("panel_channel_id", "5")
        await bot.ensure_guide_channels(guild)
        welcome = next(c for c in guild.text_channels if c.name == tb.WELCOME_CHANNEL_NAME)
        question = next(c for c in guild.text_channels if c.name == tb.QUESTION_CHANNEL_NAME)
        # 「はじめに」は運営だけが書ける。「質問・困りごと」は誰でも書ける
        assert welcome.overwrites[guild.default_role].send_messages is False and question.overwrites == {}
        text = welcome.sent[0][0]
        assert panel.mention in text and question.mention in text and "https://geaspec.example" in text
        # 運営が消しても作り直さない
        guild.text_channels.remove(welcome)
        await bot.ensure_guide_channels(guild)
        assert len(guild.text_channels) == 3
        # すでに似た名前のチャンネルがあれば、それを使う
        store.set_setting("guide_channels", "")
        other = FakeGuild()
        other.text_channels += [FakeGuildChannel(8, "はじめに・ルール"), FakeGuildChannel(9, "質問")]
        await bot.ensure_guide_channels(other)
        assert len(other.text_channels) == 3 and other.text_channels[1].sent == []

    asyncio.run(scenario())


def test_skipped_versions_are_announced_in_order(tmp_path, store, monkeypatch):
    from app import version

    monkeypatch.setattr(version, "VERSION", "0.7.9")
    monkeypatch.setattr(version, "CHANGES", {"0.7.9": ["九"], "0.7.8": ["八"], "0.7.7": ["七"], "0.7.6": ["六"]})
    assert tb.unannounced_versions("0.7.6") == ["0.7.7", "0.7.8", "0.7.9"]
    assert tb.unannounced_versions("") == ["0.7.9"]  # はじめては今の版だけ
    assert tb.unannounced_versions("0.7.10") == ["0.7.9"]  # 古い版に戻したとき

    async def scenario():
        bot = tb.TesterBot(store, tmp_path, "https://x", say=lambda _: None)
        store.set_setting("announced_version", "0.7.6")
        guild = FakeGuild()
        news = FakeGuildChannel(9, "📢更新情報")
        guild.text_channels.append(news)
        assert await bot.announce_version(guild) is True
        texts = [t for t, _ in news.sent]
        assert ["0.7.7" in texts[0], "0.7.8" in texts[1], "0.7.9" in texts[2]] == [True] * 3
        # ダウンロードのリンクは今の版にだけ付ける
        assert version.RELEASE_URL not in texts[0] and version.RELEASE_URL in texts[2]
        assert store.get_setting("announced_version") == "0.7.9"

        # /geaspec-news: 今の版をもう一度書き、動いている版を運営に返す
        inter = FakeInteraction(FakeUser(7), 1)
        inter.guild = guild
        await bot.cmd_news(inter)
        assert len(news.sent) == 4 and "0.7.9" in news.sent[-1][0]
        assert "0.7.9" in inter.log[-1][1] and "書きました" in inter.log[-1][1]

    asyncio.run(scenario())


# ---- 予約のお知らせ ----

def make_site_db(path, bookings, last_seen):
    """予約サイトと同じ形のデータベースを作る。bookings は [(Meta ユーザー名, 開始, 状態)]。"""
    from sqlalchemy.orm import Session

    from app.db import Base, make_engine
    from app.models import Booking, Host, Slot, User

    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    naive = lambda t: t.astimezone(dt.timezone.utc).replace(tzinfo=None)  # noqa: E731
    with Session(engine) as db:
        host = Host(name="LG", token_hash="h", last_seen_at=naive(last_seen) if last_seen else None)
        db.add(host)
        db.flush()
        ids = []
        for i, (meta, start, status) in enumerate(bookings):
            user = User(email=f"u{i}@example.com", password_hash="x", meta_username=meta, nickname=f"なまえ{i}")
            slot = Slot(host_id=host.id, start_at=naive(start), end_at=naive(start + dt.timedelta(hours=1)), price_yen=0)
            db.add_all([user, slot])
            db.flush()
            booking = Booking(slot_id=slot.id, user_id=user.id, status=status)
            db.add(booking)
            db.flush()
            ids.append(booking.id)
        db.commit()
    engine.dispose()
    return ids


def set_status(path, booking_id, status):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    con.commit()
    con.close()


def test_booking_notices(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(10)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread}, ops_log)
        apply(store, 1, thread_id=10)  # Meta ユーザー名は meta1
        site = tmp_path / "geaspec.db"
        # テスターの予約 (2 時間後)・テスター以外の予約・支払い待ち・もう終わった予約
        mine, other, _, _ = make_site_db(site, [
            ("Meta1", at(120), "confirmed"), ("stranger", at(300), "confirmed"),
            ("meta1", at(200), "pending_payment"), ("meta1", at(-120), "completed")], last_seen=at(0))
        bot.site_db = site

        assert await bot.send_booking_notices() == 2
        assert sum("予約が入りました" in c for c in ops_log) == 2
        assert "10/19（月）23:00〜" in ops_log[0] and "<@1>" in ops_log[0] and "stranger" in ops_log[1]
        assert "予約を受け付けました" in thread.sent[-1][0] and store.tester(1)["booked_at"]
        # テストは予約ページで進める (Discord のチェックリストは出さない)
        assert f"予約ページ: https://x/bookings/{mine}" in thread.sent[-1][0]
        assert "予約: **10/19（月）23:00〜" in bot.booking_line(1)
        assert await bot.send_booking_notices() == 0  # 同じお知らせは 2 回送らない

        # 開始 15 分前。PC のアプリから 1 時間連絡がないので、運営に注意を出す
        now[0] = at(105)
        assert await bot.send_booking_notices() == 1
        assert "まもなく始まります" in thread.sent[-1][0] and f"/bookings/{mine}" in thread.sent[-1][0]
        assert "Allow remote connections" in ops_log[-1] and "連絡がありません" in ops_log[-1]

        # キャンセルは運営に知らせる
        set_status(site, other, "cancelled")
        assert await bot.send_booking_notices() == 1
        assert "キャンセル" in ops_log[-1] and "stranger" in ops_log[-1]
        assert await bot.send_booking_notices() == 0
        store.close()

    asyncio.run(scenario())


def test_no_agent_warning_when_pc_is_running(tmp_path, monkeypatch):
    async def scenario():
        now = [at(-15)]
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {}, ops_log)
        bot.site_db = tmp_path / "geaspec.db"
        make_site_db(bot.site_db, [("x", at(0), "confirmed")], last_seen=at(-16))
        assert await bot.send_booking_notices() == 2  # 予約が入った + まもなく
        assert "連絡がありません" not in ops_log[-1]
        # サイトのデータベースがまだない・読めないときは何もしない
        bot.site_db = tmp_path / "none.db"
        assert await bot.send_booking_notices() == 0
        store.close()

    asyncio.run(scenario())


# ---- 本人専用リンク ----

def test_invite_token_expires_and_rejects_tampering():
    from app import invite

    token = invite.make_token("s3cret", meta="hikari", discord_id=7, now=T0)
    assert invite.read_token("s3cret", token, T0 + dt.timedelta(days=13)) == {"meta": "hikari", "discord_id": 7, "thread": ""}
    with_thread = invite.make_token("s3cret", meta="hikari", discord_id=7, now=T0, thread="111/222")
    assert invite.read_token("s3cret", with_thread, T0)["thread"] == "111/222"
    assert invite.thread_url("111/222") == "https://discord.com/channels/111/222"
    assert invite.thread_url("javascript:alert(1)") == "" and invite.thread_url("") == ""
    assert invite.read_token("s3cret", token, T0 + dt.timedelta(days=15)) is None
    assert invite.read_token("other", token, T0) is None
    body, sig = token.rsplit(".", 1)
    assert invite.read_token("s3cret", body + "x." + sig, T0) is None
    assert invite.read_token("s3cret", "", T0) is None


def test_ready_message_gives_a_personal_link_instead_of_a_password(tmp_path, monkeypatch):
    from app import invite

    async def scenario():
        now = [T0]
        thread = FakeThread(10)
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread})
        bot.invite_secret = "s3cret"
        apply(store, 1, thread_id=10)
        await bot.send_ready(thread, store.tester(1))
        text, kw = thread.sent[-1]
        assert "合言葉" not in text and "https://x/join?t=" in text
        assert custom_ids(kw["view"])[2:] == ["gs:link", "gs:bookhelp"]
        token = text.split("/join?t=")[1].split()[0]
        assert invite.read_token("s3cret", token, T0)["meta"] == "meta1"
        assert invite.read_token("s3cret", token, T0)["thread"] == ""  # サーバー番号がまだ分からない
        store.set_setting("guild_id", "555")
        token = bot.site_link(store.tester(1)).split("/join?t=")[1]
        assert invite.read_token("s3cret", token, T0)["thread"] == "555/10"  # サイトから、このスレッドに戻れる

        i = FakeInteraction(FakeUser(1), 10, "gs:link")
        await bot.on_interaction(i)
        assert "/join?t=" in i.log[0][1] and i.log[0][2].get("ephemeral")

        assert "/feedback" not in bot.thanks_text(5, store.tester(1))
        store.close()

    asyncio.run(scenario())


def test_ops_can_send_a_link_to_a_tester_who_was_ready_before(tmp_path, monkeypatch):
    async def scenario():
        thread = FakeThread(10)
        store, bot = make_bot(tmp_path, monkeypatch, [T0], {10: thread})
        bot.invite_secret = "s3cret"
        apply(store, 1, thread_id=10)
        i = FakeInteraction(FakeUser(99), 500)
        await bot.cmd_link(i, FakeUser(1))
        assert "/join?t=" in thread.sent[-1][0] and custom_ids(thread.sent[-1][1]["view"]) == ["gs:link"]
        assert "送りました" in i.log[0][1]
        mine = FakeInteraction(FakeUser(99), 500)
        await bot.cmd_link(mine, None)
        assert "/join?t=" in mine.log[0][1] and mine.log[0][2].get("ephemeral")
        store.close()

    asyncio.run(scenario())


def test_testers_waiting_from_an_older_version_get_their_guide(tmp_path, store, monkeypatch):
    async def scenario():
        bot = tb.TesterBot(store, tmp_path, "https://x")
        sent_ops = []

        async def notify_ops(content, view=None):
            sent_ops.append(content)

        monkeypatch.setattr(bot, "notify_ops", notify_ops)
        thread = FakeThread(500)

        async def existing_thread(tester):
            return thread if tester.get("thread_id") == 500 else None

        monkeypatch.setattr(bot, "_existing_thread", existing_thread)
        store.apply(1, "a", vrchat="", meta="no_vd_user", gear="Quest 3", region="関東", schedule="", now=at(0))
        store.set_thread(1, 500)
        store.set_group(1, "A")
        store.set_has_vd(1, False)
        store.apply(2, "b", vrchat="", meta="half", gear="Quest 3", region="関東", schedule="", now=at(0))
        store.set_thread(2, 501)
        store.apply(3, "c", vrchat="", meta="no_thread", gear="Quest 3", region="関東", schedule="", now=at(0))
        # 2つの答えがなくても送る。スレッドがない人には送れない
        assert [t["user_id"] for t in store.waiting_testers()] == [1, 2]

        for tester in store.waiting_testers():
            assert await bot.send_ready_now(tester) is True
        assert store.waiting_testers() == []
        texts = [t for t, _ in thread.sent]
        assert len(texts) == 1  # 短いカード 1 枚。VD の入れ方はカードの中に入れたので、別には送らない
        assert "Virtual Desktop**（Meta のストア" in texts[0] and "① 予約する" in texts[0]
        assert any("no_vd_user" in c for c in sent_ops)

    asyncio.run(scenario())


def test_urls_are_not_followed_by_text_discord_would_link(tmp_path, monkeypatch):
    import re
    # Discord は URL のすぐ後ろの「）。貸し出しの」までリンクにしてしまう。URL の後ろは空白か行の終わりにする
    store, bot = make_bot(tmp_path, monkeypatch, [at(0)])
    cards = [b for kind in ("borrow", "lend") for _, b in bot.card_pages(kind, None)]
    for text in (*cards, tb.panel_text("土日", "https://x")):
        for m in re.finditer(r"https?://\S+", text):
            assert m.group(0).isascii(), m.group(0)
    store.close()


def test_lenders_get_setup_steps_instead_of_booking(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {}, ops_log)
        panel = FakeChannel()
        user = FakeUser(7, "貸す人")
        i = FakeInteraction(user, 1, "gs:as:lend", channel=panel)
        await bot.on_interaction(i)
        modal = i.log[0][1]
        assert modal.lend and modal.meta.required is False
        for name, value in dict(vrchat="", meta="", gear="RTX 4070", region="関東", schedule="").items():
            setattr(modal, name, Field(value))
        await bot.on_apply(FakeInteraction(user, 1, channel=panel), modal)
        thread = panel.threads[0]
        texts = [t for t, _ in thread.sent]
        assert store.tester(7)["grp"] == "C" and store.tester(7)["status"] == "ready"
        assert not any("次にやること: 予約する" in t for t in texts)  # 借りる人向けの案内は出さない
        assert "① Virtual Desktop Streamer を入れる" in texts[-1] and "Allow remote connections" in texts[-1]
        steps = "\n".join(f"{t}\n{b}" for t, b in bot.card_pages("lend", store.tester(7)))
        assert f"/releases/download/agent-v{tb.version.VERSION}/GeaspecAgent-Setup-{tb.version.VERSION}.exe" in steps
        assert "貸せる時間" in steps and "PC を貸す" in ops_log[-1]
        store.close()

    asyncio.run(scenario())


def test_lenders_answer_their_own_survey_after_the_first_wipe_check(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(40)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {40: thread}, ops_log)
        store.apply(4, "貸す人", vrchat="", meta="", gear="RTX 4070", region="関東", schedule="", now=T0)
        store.set_group(4, "C")
        store.set_thread(4, 40)
        lender = FakeUser(4)

        await bot.on_interaction(FakeInteraction(lender, 40, "gs:wipe:ok"))
        text, kw = thread.sent[-1]
        assert "貸した人のアンケート 1 / 6" in text and custom_ids(kw["view"])[0] == "gs:l:worry:high"
        await bot.on_interaction(FakeInteraction(lender, 40, "gs:wipe:ok"))  # 2 回目の確認では送らない
        assert len(thread.sent) == 1

        await bot.on_interaction(FakeInteraction(lender, 40, "gs:l:worry:some"))
        await bot.on_interaction(FakeInteraction(lender, 40, "gs:l:worry_what", values=["account", "power"]))
        bad = FakeInteraction(lender, 40, "gs:l:setup:nope")
        await bot.on_interaction(bad)
        assert "受け付けられません" in bad.log[0][1]
        for cid in ("gs:l:setup:easy", "gs:l:lend_again:yes", "gs:l:lend_price:100", "gs:l:lend_hours:10"):
            i = FakeInteraction(lender, 40, cid)
            await bot.on_interaction(i)
        assert "最後に" in i.log[0][1] and custom_ids(i.log[0][2]["view"]) == ["gs:lc", "gs:ls"]

        i = FakeInteraction(lender, 40, "gs:lc")
        await bot.on_interaction(i)
        modal = i.log[0][1]
        modal.comment = Field("電気代が気になった")
        done = FakeInteraction(lender, 40)
        await modal.on_submit(done)
        assert "ありがとうございました" in done.log[0][1]
        assert "貸した人のアンケートに答えました" in ops_log[-1] and "〜100円" in ops_log[-1]
        assert "アカウントやパスワード・電気代" in ops_log[-1] and "電気代が気になった" in ops_log[-1]
        late = FakeInteraction(lender, 40, "gs:ls")
        await bot.on_interaction(late)
        assert "もう終わっています" in late.log[0][1]

        s = store.summary()
        assert s["lenders_answered"] == 1 and s["lend_price_median"] == 100 and s["lend_again_rate"] == 1.0
        assert any("もらえたら貸す金額" in line and "〜100円" in line for line in tb.summary_lines(s))
        csv_text = store.lenders_csv()
        assert "1時間いくらもらえたら貸しますか？" in csv_text and "〜100円" in csv_text and "電気代が気になった" in csv_text
        store.close()

    asyncio.run(scenario())


class FakeMessage:
    def __init__(self, author, channel, content="", attachments=()):
        author.bot = False
        self.author = author
        self.channel = channel
        self.content = content
        self.attachments = list(attachments)
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class FakeThreadChannel(discord.Thread):
    """isinstance(..., discord.Thread) を通すためだけの入れ物。"""

    def __init__(self, tid):  # noqa: D107 - 親の __init__ は呼ばない
        self.id = tid

    @property
    def mention(self):
        return f"<#{self.id}>"


def test_anything_testers_write_in_their_thread_reaches_ops(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {}, ops_log)
        apply(store, 1, thread_id=50)
        channel = FakeThreadChannel(50)

        msg = FakeMessage(FakeUser(1), channel, "予約ページの日付が分かりにくかったです")
        await bot.on_message(msg)
        assert msg.reactions == ["📝"]
        assert "借りる人" in ops_log[-1] and "> 予約ページの日付が分かりにくかったです" in ops_log[-1]

        # 運営が同じスレッドに書いたもの・ほかのスレッドのものは記録しない
        await bot.on_message(FakeMessage(FakeUser(99), channel, "運営です"))
        await bot.on_message(FakeMessage(FakeUser(1), FakeThreadChannel(51), "別の場所"))
        assert len(store.notes()) == 1 and len(ops_log) == 1
        notes = store.notes_csv()
        assert "借りる人" in notes and "日付が分かりにくかった" in notes and "テスター" not in notes
        assert store.summary()["notes"] == 1
        store.close()

    asyncio.run(scenario())


def test_testers_are_told_they_can_write_anytime(tmp_path, monkeypatch):
    store, bot = make_bot(tmp_path, monkeypatch, [at(0)])
    assert tb.NOTES_HINT in bot.card_pages("borrow", None)[-1][1] and tb.NOTES_HINT in bot.card_pages("lend", None)[-1][1]
    assert tb.NOTES_HINT in bot.thanks_text(1)
    store.close()


def test_pc_price_questions_only_for_people_without_a_vr_pc():
    without = TesterStore.asked_questions({"own_pc": "none"})
    with_pc = TesterStore.asked_questions({"own_pc": "vr"})
    assert len(without) == len(QUESTIONS) and len(with_pc) == len(QUESTIONS) - 2
    keys = [q["key"] for q in without]
    assert keys.index("pc_checked") == keys.index("own_pc") + 1 and keys.index("pc_effect") == keys.index("own_pc") + 2
    before = {k: "x" for k in keys[:keys.index("pc_checked")]}

    async def scenario():
        text, _ = tb.survey_message(before | {"own_pc": "none"})
        assert "値段を、最後に調べたのはいつ" in text and f"/ {len(QUESTIONS)}" in text
        text, _ = tb.survey_message(before | {"own_pc": "vr"})
        assert "週に何日" in text and f"/ {len(QUESTIONS) - 2}" in text

    asyncio.run(scenario())


def test_summary_counts_pc_price_answers(store):
    apply(store, 1, thread_id=10)
    apply(store, 2, thread_id=11)
    run_session(store, 1, answers=ALL_GOOD | {"own_pc": "none", "pc_checked": "week", "pc_effect": "no_need"})
    run_session(store, 2, answers=ALL_GOOD | {"own_pc": "no_vr", "pc_checked": "older", "pc_effect": "want_more"})
    s = store.summary()
    assert s["pc_checked_recent_rate"] == 0.5 and s["pc_effect"]["no_need"] == 0.5 and s["pc_effect"]["later"] == 0.0
    assert any("1か月以内に調べた: 50%" in line and "買わなくてよくなる 50%" in line for line in summary_lines(s))


def test_booking_that_starts_now_says_start_right_away(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(10)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread}, ops_log)
        apply(store, 1, thread_id=10)
        site = tmp_path / "geaspec.db"
        make_site_db(site, [("meta1", at(-20), "confirmed")], last_seen=at(0))  # 20 分前に始まった枠に「今すぐ入る」
        bot.site_db = site
        assert await bot.send_booking_notices() == 1
        assert len(thread.sent) == 1 and "すぐに始められます" in thread.sent[0][0]
        assert "予約ページの「テストのチェックリスト」" in thread.sent[0][0]
        assert await bot.send_booking_notices() == 0
        store.close()

    asyncio.run(scenario())


def test_long_booking_shows_the_end_date():
    from app import booking_notice
    start = dt.datetime(2026, 10, 10, 4, tzinfo=dt.timezone.utc)
    assert booking_notice.when_text(start, start + dt.timedelta(hours=1)) == "10/10（土）13:00〜14:00"
    assert booking_notice.when_text(start, start + dt.timedelta(days=2)) == "10/10（土）13:00〜10/12（月）13:00"


# ---- 予約した → チェックリスト、貸す人へのお知らせ ----

def make_lending_db(path, *, borrower_thread="", owner_thread="", status="confirmed", start=None, approved=True):
    """貸す人の PC (コンピューター名 GAMING-PC) に、借りる人が 1 件予約したサイトのデータベース。"""
    from sqlalchemy.orm import Session

    from app.db import Base, make_engine
    from app.models import Booking, Host, Slot, User

    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    naive = lambda t: t.astimezone(dt.timezone.utc).replace(tzinfo=None)  # noqa: E731
    start = start or at(120)
    with Session(engine) as db:
        owner = User(email="lender@example.com", password_hash="x", discord_thread=owner_thread)
        borrower = User(email="b@example.com", password_hash="x", meta_username="other_name", discord_thread=borrower_thread)
        db.add_all([owner, borrower])
        db.flush()
        host = Host(name="GAMING-PC", token_hash="h", owner_id=owner.id, active=approved, pending=not approved,
                    last_seen_at=naive(at(0)))
        db.add(host)
        db.flush()
        slot = Slot(host_id=host.id, start_at=naive(start), end_at=naive(start + dt.timedelta(hours=1)), price_yen=0)
        db.add(slot)
        db.flush()
        booking = Booking(slot_id=slot.id, user_id=borrower.id, status=status)
        db.add(booking)
        db.commit()
        ids = (host.id, booking.id)
    engine.dispose()
    return ids


def test_booked_button_checks_the_site_and_shows_the_booked_pc(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(10)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread}, ops_log)
        apply(store, 1, thread_id=10)
        bot.site_db = tmp_path / "geaspec.db"
        tester = FakeUser(1)

        # まだ予約していないのに押した → チェックリストは出さない
        make_lending_db(bot.site_db, borrower_thread="555/999")  # 別の人の予約
        i = FakeInteraction(tester, 10, "gs:booked")
        await bot.on_interaction(i)
        assert "予約が見つかりませんでした" in i.log[0][1] and thread.sent == [] and store.tester(1)["booked_at"] is None

        # サイトに残したスレッドで、本人の予約が見つかる (Meta ユーザー名が申し込みと違っていても)
        bot.site_db.unlink()
        make_lending_db(bot.site_db, borrower_thread="555/10")
        i = FakeInteraction(tester, 10, "gs:booked")
        await bot.on_interaction(i)
        checklist = thread.sent[-1][0]
        assert "下のチェックリスト" in i.log[0][1]
        assert "予約: **10/19（月）23:00〜10/20（火）00:00** ／ PC「GAMING-PC」" in checklist
        # 自動のお知らせは、チェックリストを出し直さない (本人がもう押した)
        count = len(thread.sent)
        assert await bot.send_booking_notices() == 1
        assert len(thread.sent) == count and "予約が入りました" in ops_log[-1]

        # 当日: Virtual Desktop で選ぶ PC の名前は、予約した PC
        now[0] = at(115)
        i = FakeInteraction(tester, 10, "gs:start")
        await bot.on_interaction(i)
        assert "「GAMING-PC」を選ぶ" in i.log[0][1]
        assert store.latest_session(10)["pc"] == "GAMING-PC"  # どの PC で測ったかを残す

        # 送信は、そろうまで押せない
        i = FakeInteraction(tester, 10, "gs:submit")
        await bot.on_interaction(i)
        assert "まだ終わっていない項目" in i.log[0][1] and "3. VRChat にログイン" in i.log[0][1]
        assert store.latest_session(10)["status"] == "connecting"
        store.close()

    asyncio.run(scenario())


def test_next_booking_starts_a_fresh_checklist(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(10)
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread})
        apply(store, 1, thread_id=10)
        run_session(store, 1)  # 1 回目は送信まで終わっている
        now[0] = at(60 * 24)
        await bot.begin_checklist(store.tester(1), None)
        assert "✅ 1. 予約する" in thread.sent[-1][0] and "⬜ 2." in thread.sent[-1][0]
        assert custom_ids(thread.sent[-1][1]["view"]) == ["gs:start"]
        store.close()

    asyncio.run(scenario())


def test_lenders_hear_about_approval_bookings_and_the_end_of_lending(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(40)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {40: thread}, ops_log)
        store.apply(4, "貸す人", vrchat="", meta="", gear="RTX 4070", region="関東", schedule="", now=T0)
        store.set_group(4, "C")
        store.set_thread(4, 40)
        bot.site_db = tmp_path / "geaspec.db"
        host_id, booking_id = make_lending_db(bot.site_db, owner_thread="555/40")

        # 承認された → 貸す人のスレッドに、カレンダーで時間を決めるよう書く (1 回だけ)
        assert await bot.send_lender_notices() == 1
        assert "「GAMING-PC」が承認されました" in thread.sent[-1][0] and "カレンダー" in thread.sent[-1][0]
        assert await bot.send_lender_notices() == 0

        # 予約が入った → 貸す人にも知らせる
        await bot.send_booking_notices()
        assert "あなたの PC「GAMING-PC」に予約が入りました" in thread.sent[-1][0]

        # 貸し終わった → データが消えたかの確認と、はじめてなら貸す人のアンケート
        set_status(bot.site_db, booking_id, "completed")
        now[0] = at(200)
        assert await bot.send_lender_notices() == 0  # PC のアプリが削除まで終えた報告を待つ
        import sqlite3
        con = sqlite3.connect(bot.site_db)
        con.execute("UPDATE bookings SET finished_at = '2026-10-19 15:00:00' WHERE id = ?", (booking_id,))
        con.commit()
        con.close()
        assert await bot.send_lender_notices() == 1
        (done, done_kw), (survey, survey_kw) = thread.sent[-2], thread.sent[-1]
        assert "貸し出しが終わりました" in done and "データが消えているか" in done
        assert custom_ids(done_kw["view"]) == ["gs:wipe:ok", "gs:wipe:left"]
        assert "貸した人のアンケート 1 / 6" in survey
        assert await bot.send_lender_notices() == 0
        # 確認ボタンを押しても、アンケートは 2 回送らない
        await bot.on_interaction(FakeInteraction(FakeUser(4), 40, "gs:wipe:ok"))
        assert thread.sent[-1][0] == survey
        store.close()

    asyncio.run(scenario())


def test_unapproved_pc_is_not_announced(tmp_path, monkeypatch):
    async def scenario():
        thread = FakeThread(40)
        store, bot = make_bot(tmp_path, monkeypatch, [at(0)], {40: thread})
        store.apply(4, "貸す人", vrchat="", meta="", gear="", region="", schedule="", now=T0)
        store.set_thread(4, 40)
        bot.site_db = tmp_path / "geaspec.db"
        make_lending_db(bot.site_db, owner_thread="555/40", approved=False)
        assert await bot.send_lender_notices() == 0 and thread.sent == []
        store.close()

    asyncio.run(scenario())


def test_end_now_asks_once_more_and_asks_the_site(tmp_path, monkeypatch):
    async def scenario():
        now = [at(0)]
        thread = FakeThread(10)
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {10: thread}, ops_log)
        apply(store, 1, thread_id=10)
        bot.site_db = tmp_path / "geaspec.db"
        _, booking_id = make_lending_db(bot.site_db, borrower_thread="555/10", status="active", start=at(-10))
        asked = []

        async def end_booking(bid):
            asked.append(bid)
            return True

        monkeypatch.setattr(bot, "end_booking", end_booking)
        tester = FakeUser(1)
        i = FakeInteraction(tester, 10, "gs:endnow")
        await bot.on_interaction(i)
        assert "いま終わりにしますか" in i.log[0][1] and i.log[0][2]["ephemeral"]
        assert custom_ids(i.log[0][2]["view"]) == ["gs:endnow:yes"] and asked == []
        i = FakeInteraction(tester, 10, "gs:endnow:yes")
        await bot.on_interaction(i)
        assert asked == [booking_id] and "ログイン情報が消えます" in i.log[0][1]
        assert "いま終わる" in ops_log[-1]

        # 利用中の予約がなければ、頼まない
        set_status(bot.site_db, booking_id, "completed")
        i = FakeInteraction(tester, 10, "gs:endnow:yes")
        await bot.on_interaction(i)
        assert "見つかりませんでした" in i.log[0][1] and asked == [booking_id]
        store.close()

    asyncio.run(scenario())


def test_end_booking_without_the_site_api_says_to_use_the_booking_page(tmp_path, monkeypatch):
    store, bot = make_bot(tmp_path, monkeypatch, [at(0)])
    assert asyncio.run(bot.end_booking(1)) is False
    store.close()


def test_fps_is_kept_per_pc(store):
    """PC ごとの実測 FPS。将来、空き枠の一覧でスペックの代わりに見せる。"""
    for uid, pc, fps, ping in ((1, "GAMING-PC", 72, 40), (2, "GAMING-PC", 68, 50), (3, "LG", 90, 30), (4, "", 50, 99)):
        apply(store, uid)
        s, _ = store.start_session(uid, uid * 10, at(0), pc=pc)
        store.set_performance(s["id"], fps_by_world={"集会場": fps}, ping_ms=ping)
    apply(store, 5)
    store.start_session(5, 50, at(0), pc="LG")  # 数字のないテストは数えない
    by_pc = store.fps_by_pc()
    assert by_pc == {"GAMING-PC": {"tests": 2, "fps": {"集会場": 70}, "ping": 45},
                     "LG": {"tests": 1, "fps": {"集会場": 90}, "ping": 30}}
    text = "\n".join(summary_lines(store.summary()))
    assert "PC「GAMING-PC」の実測（2回）: 集会場 70fps ／ Ping 45ms" in text
    rows = list(csv.reader(io.StringIO(store.to_csv())))
    assert dict(zip(rows[0], rows[1]))["PC"] == "GAMING-PC"


def test_tests_run_on_the_booking_page_reach_the_ops_channel(tmp_path, monkeypatch):
    """予約ページで進めたテストのお知らせは、ボットが運営チャンネルに 1 回だけ伝える。困ったときは運営ロールを呼ぶ。"""
    async def scenario():
        now = [T0]
        ops_log = []
        store, bot = make_bot(tmp_path, monkeypatch, now, {}, ops_log)
        store.set_setting("ops_role_id", "7")
        s, _ = store.start_session(-5, -5, T0, pc="LG", booking_id=3)
        store.add_web_event("start", "予約 #3（LG・Meta: q） がテストを始めました", T0, session_id=s["id"])
        store.add_web_event("help", "予約 #3（LG・Meta: q） が困っています", T0, session_id=s["id"], urgent=True)
        store.mark_entered(s["id"], T0 + dt.timedelta(minutes=2))
        store.finish_play(s["id"], T0 + dt.timedelta(minutes=30), connected=True)
        store.complete(s["id"], T0 + dt.timedelta(minutes=31), comment="楽しかった")
        store.add_web_event("done", "予約 #3（LG・Meta: q） のテストが完了しました", T0, session_id=s["id"])

        assert await bot.send_web_events() == 3
        assert ops_log[0] == "予約 #3（LG・Meta: q） がテストを始めました"
        assert ops_log[1].startswith("<@&7> ") and "困っています" in ops_log[1]
        assert ops_log[2].startswith("予約 #3（LG・Meta: q）のテストが完了（つながった・入るまで 2分00秒")
        assert "ひとこと: 楽しかった" in ops_log[2] and "<@-5>" not in ops_log[2]
        assert await bot.send_web_events() == 0
        store.close()

    asyncio.run(scenario())


def test_a_new_booking_page_test_drops_the_unfinished_one(store):
    first, _ = store.start_session(-5, -5, T0, booking_id=1)
    again, created = store.start_session(-5, -5, T0, booking_id=1)
    assert again["id"] == first["id"] and not created
    second, created = store.start_session(-5, -5, T0 + dt.timedelta(days=1), booking_id=2)
    assert created and store.session(first["id"])["status"] == "dropped"
    assert store.session_for_booking(2)["id"] == second["id"]


def test_summary_answers_the_five_questions_of_the_test(store):
    """簡単さ・安全・FPS の上がり方・遅れ・払ってもいい金額を、テストごとの数字から出す。"""
    def run(uid, *, fps, ping, latency, answers):
        s, _ = store.start_session(-uid, -uid, T0, booking_id=uid)
        store.mark_entered(s["id"], T0 + dt.timedelta(minutes=2))
        store.set_performance(s["id"], fps_by_world={"日本語話者向け集会場": fps}, ping_ms=ping, latency_ms=latency)
        store.finish_play(s["id"], T0 + dt.timedelta(minutes=50), connected=True)
        for key, value in answers.items():
            store.answer(s["id"], key, value)
        store.complete(s["id"], T0 + dt.timedelta(minutes=51))

    base = {"ease": "5", "trust": "4", "quality": "4", "compare": "5", "price": "200"}
    run(1, fps=72, ping=40, latency=25, answers={**base, "safety": ["none"], "usual_fps": "45", "region": "kanto"})
    run(2, fps=60, ping=120, latency=55, answers={**base, "safety": ["saw_pc"], "usual_fps": "unknown",
                                                     "region": "kyushu", "price": "400"})
    s = store.summary()
    assert s["safety_answered"] == 2 and s["safety_issues"] == 1
    assert s["fps_gain_median"] == 27  # 72 − 45 (いつもの FPS がわからない人は数えない)
    assert s["latency_ok_rate"] == 0.5 and s["ping_ok_rate"] == 0.5 and s["compare_better_rate"] == 1.0
    assert s["by_region"]["九州・沖縄"] == {"tests": 1, "latency": 55, "ping": 120}
    assert ("〜200円", 1) in s["price_counts"] and ("〜400円", 1) in s["price_counts"]
    text = "\n".join(tb.summary_lines(s))
    for head in ("① 簡単につながるか", "② 安全につながるか", "③ FPS がどれだけ上がるか", "④ 遅れが現実的な範囲か",
                 "⑤ いくらなら払うか"):
        assert head in text
    assert "⚠️ 気になることがあった: 1回 ／ 2回" in text and "いつもの環境より（真ん中の値）: +27 FPS" in text
    assert "関東（1回）: VD の遅れ 25ms ／ Ping 40ms" in text
