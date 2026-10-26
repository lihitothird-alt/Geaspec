"""テスター用 Discord ボット。

テスターが Discord のボタンだけで、申し込み → テスト開始 → 計測 → アンケートまで自分で進められるようにする。
記録は tester_store.TesterStore にたまり、運営は /geaspec-results で集計と CSV を受け取る。

- はじめて動いたとき、「運営」ロールと 2 つのチャンネルを自動で作る (/geaspec-setup でやり直せる)
- 申し込みチャンネル: 「申し込む」ボタンを置く。テスターはボタンを押すだけ
- テスターごとのスレッド: 申し込むと、本人と運営だけが見られるスレッドができる。テストはここで進める
- 運営チャンネル: 申し込み・テスト開始・「困った」・結果のお知らせが届く
- テスターがスレッドに書いたことは意見として記録し、運営チャンネルにも回す

ボタンの押下は custom_id ("gs:...") だけで処理するので、ボットを再起動しても前のボタンが使える。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands

from . import booking_notice, feedback, invite, version
from .tester_store import (FOLLOWUPS, GROUPS, LENDER_QUESTIONS, MAX_WORLDS, MISSIONS, QUESTIONS, TesterStore,
                           format_duration, option_label, summary_lines)

log = logging.getLogger(__name__)

IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
OPS_ROLE_NAME = "運営"
OPS_CHANNEL_NAME = "運営"
PANEL_CHANNEL_NAME = "テスター申し込み"
NEWS_NAME = "更新情報"  # この言葉を名前に含むチャンネル・スレッド・フォーラムに、新しい版のお知らせを書く
NEWS_CHANNEL_NAME = "更新情報"  # どこにもなければ、この名前で作る
# 前の版で絵文字付きの名前で作ったチャンネル。絵文字のない名前に付け直す
OLD_CHANNEL_NAMES = {"🙋テスター申し込み": PANEL_CHANNEL_NAME, "📢更新情報": NEWS_CHANNEL_NAME}
WELCOME_CHANNEL_NAME = "はじめに"  # 運営だけが書ける案内
QUESTION_CHANNEL_NAME = "質問・困りごと"  # 誰でも書ける相談の場
# ボットのアイコン (サイトのアプリアイコンと同じ Geaspec のロゴ)。変えたら AVATAR_VERSION を上げる
AVATAR_PATH = Path(__file__).resolve().parent / "static" / "icons" / "icon-512.png"
AVATAR_VERSION = "logo-1"
DEFAULT_HOURS = "平日は19時以降、土日は終日"
DEFAULT_AGENT_URL = "https://github.com/lihitothird-alt/Geaspec/releases/latest"
# 貸す人の PC に入れる Virtual Desktop Streamer (無料)。借りる人は Quest の VD アプリだけでよい
STREAMER_URL = "https://www.vrdesktop.net/"
BOOKING_CHECK_SECONDS = 60
# テスターは LG の家の外からつなぐので、VD Streamer でこれがオンになっていないとつながらない
REMOTE_SETTING = "Virtual Desktop Streamer の「Allow remote connections」（外からの接続を許可）"


class SetupError(Exception):
    """ボットに足りない権限がある。メッセージは運営に見せる文。"""
MAX_SHOTS_PER_SESSION = 10


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_int(text: str) -> int | None:
    """「72fps」「約 45 ms」のような入力から数字を取り出す。空なら None。"""
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def make_view(*items: discord.ui.Item) -> discord.ui.View:
    """ボタンを並べるだけの View。押されたときの処理は on_interaction で行うので、すぐ止めておく。"""
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    view.stop()
    return view


def button(label: str, custom_id: str, style=discord.ButtonStyle.secondary, *, row: int | None = None,
           disabled: bool = False) -> discord.ui.Button:
    return discord.ui.Button(label=label, custom_id=custom_id, style=style, row=row, disabled=disabled)


def panel_text(hours: str, site_url: str = "") -> str:
    """申し込みチャンネルのパネル。くわしい説明は Web サイトに置き、ここは大まかな流れだけ。"""
    guide = f"\nくわしい説明（画像つき）:\n{site_url}/guide\n" if site_url else ""
    return f"""VR ヘッドセット（Quest など）から、ほかの人のゲーミング PC を借りて PC 版 VRChat を遊ぶテストです。参加は無料です。

**流れ**
1. 下の「テスターに申し込む」を押す（PC を借りる人・貸す人をえらべます）
2. あなた専用のスレッドに、手順のカードが届きます（「次へ」で 1 つずつ読めます）
3. 予約サイトで予約して、当日は予約ページの手順どおりに進めるだけ

**借りる人の条件**: Quest に **Virtual Desktop**（Meta のストアの有料アプリ）が入っていること
{guide}
**テストができる時間: {hours}**"""


NOTES_HINT = "迷ったこと・気づいたこと・こうしてほしいことは、いつでもこのスレッドに書いてください。そのまま運営に届きます。"


WIPE_CHECK_TEXT = (
    "**貸し出しが終わったら、データが消えているか確かめてください**（テスト項目です）\n"
    "1. Geaspec アプリの「動きの記録」に「終了処理が完了しました。クリーンな状態です」と出ている\n"
    "2. 貸し出し用の Windows ユーザーの「ピクチャ」→「VRChat」フォルダが空になっている\n"
    "3. Steam から VRChat を起動すると、借りた人のアカウントではなくログイン画面が出る（確かめたら閉じてください。"
    "自分のアカウントで入った場合も、次に貸す前にログアウトしてください）\n"
    "確かめたら、下のボタンを押してください。貸すたびに押してもらえると助かります。"
)


def wipe_check_view() -> discord.ui.View:
    return make_view(button("✅ 全部消えていた", "gs:wipe:ok", discord.ButtonStyle.success),
                     button("⚠ 残っていた", "gs:wipe:left", discord.ButtonStyle.danger))


def vd_install_text(tester: dict) -> str:
    """Quest の Virtual Desktop を持っていない人への案内。持っている人には何も出さない。"""
    if tester.get("has_vd") != 0:
        return ""
    return ("**⓪ Quest に Virtual Desktop を入れる**\n"
            "　テストへの参加には、Quest 用の **Virtual Desktop**（Meta のストアで買う有料のアプリ）が必要です（参加の条件です）。"
            "Quest のストアで「Virtual Desktop」を検索して入れてください\n"
            "　入れたら一度開いて、Meta アカウントでそのまま使える状態にしておいてください（PC 用のソフトは入れなくて大丈夫です）\n\n")


def survey_message(answers: dict) -> tuple[str, discord.ui.View]:
    """次の質問 (全部答えたら、ひとこと欄への案内) を作る。"""
    key = TesterStore.next_question(answers)
    if key is None:
        text = ("**アンケートは以上です。ありがとうございました！**\n"
                "いちばん迷ったところや、ひとこと感想があれば「書く」から（任意）。\n"
                "最後に、下のチェックリストの「送信してテストを終える」を押してください。")
        return text, make_view(button("✍ 書く（任意）", "gs:comment"))
    asked = TesterStore.asked_questions(answers)
    index = [q["key"] for q in asked].index(key)
    q = asked[index]
    text = f"**アンケート {index + 1} / {len(asked)}**\n{q['text']}"
    if q.get("multi"):
        select = discord.ui.Select(
            custom_id=f"gs:a:{key}", placeholder="えらんでください（いくつでも）", min_values=1,
            max_values=len(q["options"]), options=[discord.SelectOption(label=l, value=v) for v, l in q["options"]])
        return text, make_view(select)
    items = [button(label, f"gs:a:{key}:{value}", row=i // 5) for i, (value, label) in enumerate(q["options"])]
    return text, make_view(*items)


def followup_message(user_id: int, answers: dict) -> tuple[str, discord.ui.View]:
    keys = [q["key"] for q in FOLLOWUPS]
    index = next(i for i, k in enumerate(keys) if k not in answers)
    q = FOLLOWUPS[index]
    head = f"<@{user_id}> テストから3日たちました。最後に4つだけ教えてください\n" if index == 0 else ""
    text = f"{head}**{index + 1} / {len(FOLLOWUPS)}** {q['text']}"
    return text, make_view(*[button(label, f"gs:f:{q['key']}:{value}", row=i // 5)
                             for i, (value, label) in enumerate(q["options"])])


def lender_survey_message(user_id: int, answers: dict) -> tuple[str, discord.ui.View]:
    """PC を貸した人へのアンケートの次の質問 (全部答えたら、ひとこと欄への案内)。"""
    key = TesterStore.next_lender_question(answers)
    if key is None:
        return ("**最後に（任意）**\n貸してみて気づいたこと・こうしてほしいことがあれば書いてください。\n"
                "なければ「スキップして終わる」で完了です。",
                make_view(button("✍ 書く", "gs:lc", discord.ButtonStyle.primary), button("スキップして終わる", "gs:ls")))
    index = [q["key"] for q in LENDER_QUESTIONS].index(key)
    q = LENDER_QUESTIONS[index]
    head = (f"<@{user_id}> 貸してくれてありがとうございます！ 貸す側の感想も聞かせてください"
            f"（ボタンで{len(LENDER_QUESTIONS)}問・1分ほど）\n") if index == 0 else ""
    text = f"{head}**貸した人のアンケート {index + 1} / {len(LENDER_QUESTIONS)}**\n{q['text']}"
    if q.get("multi"):
        select = discord.ui.Select(
            custom_id=f"gs:l:{key}", placeholder="えらんでください（いくつでも）", min_values=1,
            max_values=len(q["options"]), options=[discord.SelectOption(label=l, value=v) for v, l in q["options"]])
        return text, make_view(select)
    return text, make_view(*[button(label, f"gs:l:{key}:{value}", row=i // 5) for i, (value, label) in enumerate(q["options"])])


# 予約してから「送信」までを 1 つのチェックリストにまとめる。終わったものに ✅ が付き、必要なものがそろうと送信できる
MISSION_SHORT = {"heavy": "重いワールド", "crowd": "人の多いインスタンス", "photo": "自撮り"}
CHECK_REQUIRED = ("booked", "started", "entered", "photo", "survey")
CHECK_LABELS = {
    "booked": "1. 予約する",
    "started": "2. 開始 5分前に「テストを始める」を押して、Quest の Virtual Desktop でつなぐ",
    "entered": "3. VRChat にログインして、ワールドに入ったら「ワールドに入れた」を押す",
    "photo": "4. FPS と Ping が写った写真を、このスレッドに貼る（撮れなかったら「数字で入力」）",
    "survey": "6. 遊び終わったら「遊び終わった」を押して、アンケートに答える",
}


def checklist_state(session: dict | None, booked: bool) -> dict:
    """チェックリストのどこまで終わったか。session は今回のテスト (まだ始めていなければ None)。"""
    if session is None:
        return {"booked": booked, "started": False, "entered": False, "photo": False, "survey": False,
                "missions": {}, "done": False}
    answers = json.loads(session["answers"])
    return {
        "booked": True,  # テストを始めたなら予約はできている
        "started": True,
        "entered": bool(session["entered_at"]),
        "photo": bool(json.loads(session["screenshots"]) or json.loads(session["fps_worlds"])
                      or session["ping_ms"] is not None),
        "survey": session["status"] in ("survey", "done") and bool(session["connected"])
        and TesterStore.next_question(answers) is None,
        "missions": json.loads(session["missions"]),
        "done": session["status"] == "done",
    }


def checklist_message(state: dict, booking_line: str = "") -> tuple[str, discord.ui.View]:
    mark = lambda ok: "✅" if ok else "⬜"  # noqa: E731
    lines = ["**📋 テストのチェックリスト**"]
    if booking_line:
        lines.append(booking_line)
    lines += [f"{mark(state[k])} {CHECK_LABELS[k]}" for k in ("booked", "started", "entered", "photo")]
    missions = " ／ ".join(f"{name} {mark(state['missions'].get(k))}" for k, name in MISSION_SHORT.items())
    lines.append(f"{mark(all(state['missions'].get(k) for k in MISSION_SHORT))} 5. （できたら）{missions}")
    lines.append(f"{mark(state['survey'])} {CHECK_LABELS['survey']}")
    ready = all(state[k] for k in CHECK_REQUIRED)
    items: list[discord.ui.Item] = []
    if state["done"]:
        lines.append("\n**送信しました。テストは完了です。ありがとうございました！**")
    elif ready:
        lines.append("\nそろいました！ 下の「送信してテストを終える」を押してください（運営に届きます）。")
        items.append(button("📤 送信してテストを終える", "gs:submit", discord.ButtonStyle.success))
    else:
        lines.append("\n1〜4 と 6 がそろうと、「送信してテストを終える」ボタンが出ます。")
        if not state["started"]:
            items.append(button("▶ テストを始める", "gs:start", discord.ButtonStyle.primary))
        elif not state["photo"]:
            items.append(button("📝 数字で入力", "gs:perf"))
    return "\n".join(lines), make_view(*items)


def play_view(missions: dict) -> discord.ui.View:
    items = [button(("✅ " if missions.get(k) else "⬜ ") + label, f"gs:m:{k}", row=0) for k, label in MISSIONS.items()]
    items += [
        button("🆘 困った", "gs:help", discord.ButtonStyle.danger, row=1),
        button("🏁 遊び終わった", "gs:done", discord.ButtonStyle.success, row=1),
    ]
    return make_view(*items)


def welcome_view(tester: dict) -> discord.ui.View:
    """借りる人に、Quest の Virtual Desktop を持っているかだけ聞く (持っていなければ入れ方を送る)。"""
    vd = discord.ui.Select(custom_id="gs:vd", placeholder="Virtual Desktop を持っていますか？", options=[
        discord.SelectOption(label="持っている", value="1", default=tester.get("has_vd") == 1),
        discord.SelectOption(label="持っていない", value="0", default=tester.get("has_vd") == 0)])
    return make_view(vd)


def apply_choice_view() -> discord.ui.View:
    """申し込みボタンのあとに、借りる側か貸す側かをえらんでもらう。"""
    return make_view(button("🥽 Quest だけ（PC を借りて遊ぶ）", "gs:as:borrow", discord.ButtonStyle.primary),
                     button("🖥 PCVR を持っている（自分の PC を貸す）", "gs:as:lend"))


class ApplyModal(discord.ui.Modal):
    def __init__(self, bot: "TesterBot", tester: dict | None, lend: bool = False) -> None:
        super().__init__(title="テスターに申し込む（PC を貸す）" if lend else "テスターに申し込む", timeout=1800)
        self.bot = bot
        self.lend = lend
        t = tester or {}
        self.vrchat = discord.ui.TextInput(label="VRChat の表示名（任意）", max_length=60, required=False,
                                           default=t.get("vrchat") or None)
        self.meta = discord.ui.TextInput(
            label="Meta ユーザー名（表示名ではありません）", max_length=60, default=t.get("meta") or None,
            required=not lend,
            placeholder="Meta Horizon アプリのプロフィールで見られます。VD を持っている人は VD と同じ")
        self.gear = discord.ui.TextInput(
            label="PC の中身（グラフィックボードなど）" if lend else "使っている機材", max_length=100,
            default=t.get("gear") or None,
            placeholder="例: RTX 4070 + Ryzen 7 ／ Quest 3" if lend else "例: Quest 3 だけ ／ Quest 3 + RTX 3060")
        self.region = discord.ui.TextInput(label="住んでいる地方（だいたいでOK）", max_length=30,
                                           default=t.get("region") or None, placeholder="例: 関東")
        self.schedule = discord.ui.TextInput(label="都合のいい日時", max_length=100, required=False,
                                             default=t.get("schedule") or None, placeholder="例: 平日の夜・土日")
        for item in (self.vrchat, self.meta, self.gear, self.region, self.schedule):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.on_apply(interaction, self)


class PerfModal(discord.ui.Modal):
    def __init__(self, bot: "TesterBot", worlds: list[str]) -> None:
        super().__init__(title="性能の数字（わかるものだけ）", timeout=900)
        self.bot = bot
        self.worlds = worlds[:MAX_WORLDS]
        self.fps_inputs = [discord.ui.TextInput(label=f"FPS: {w}"[:45], max_length=10, required=False, placeholder="例: 72")
                           for w in self.worlds]
        self.ping = discord.ui.TextInput(label="Ping（VRChat の表示）", max_length=10, required=False, placeholder="例: 30")
        for item in (*self.fps_inputs, self.ping):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.on_perf(interaction, self)


class CommentModal(discord.ui.Modal):
    def __init__(self, bot: "TesterBot", connected: bool) -> None:
        super().__init__(title="ひとこと（任意）", timeout=1800)
        self.bot = bot
        self.stuck = discord.ui.TextInput(
            label="いちばん迷ったところ" if connected else "どこで止まりましたか？",
            style=discord.TextStyle.paragraph, required=False, max_length=1000)
        self.comment = discord.ui.TextInput(label="ひとこと感想", style=discord.TextStyle.paragraph,
                                            required=False, max_length=1000)
        self.add_item(self.stuck)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.on_comment(interaction, self)


class LenderCommentModal(discord.ui.Modal):
    def __init__(self, bot: "TesterBot") -> None:
        super().__init__(title="貸してみて（任意）", timeout=1800)
        self.bot = bot
        self.comment = discord.ui.TextInput(label="気づいたこと・こうしてほしいこと", style=discord.TextStyle.paragraph,
                                            required=False, max_length=1000)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.on_lender_comment(interaction, self)


def _version_key(ver: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", ver))


def unannounced_versions(last: str) -> list[str]:
    """前に「更新情報」に書いた版 last より新しく、今の版までの版を古い順に。はじめてなら今の版だけ。"""
    now = _version_key(version.VERSION)
    if last:
        newer = sorted((v for v in version.CHANGES if _version_key(last) < _version_key(v) <= now), key=_version_key)
        if newer:
            return newer
    return [version.VERSION]


class TesterBot(discord.Client):
    def __init__(self, store: TesterStore, shots_dir: Path, site_url: str, *, site_password: str = "", invite_secret: str = "",
                 site_db: Path | None = None, message_content: bool = True, site_api: str = "", api_secret: str = "",
                 clock: Callable[[], dt.datetime] = utcnow, say: Callable[[str], None] | None = None) -> None:
        intents = discord.Intents.default()
        intents.message_content = message_content  # スクショ (添付画像) を受け取るため
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True))
        self.store = store
        self.shots_dir = shots_dir
        self.site_url = site_url
        self.site_password = site_password
        self.invite_secret = invite_secret  # サイトの SECRET_KEY。あれば合言葉の代わりに本人専用リンクを渡す
        self.site_db = site_db
        # 「いま終わる」をサイトに頼む先 (同じ PC で動くサイト) と署名の鍵。なければ予約ページから終えてもらう
        self.site_api = site_api.rstrip("/")
        self.api_secret = api_secret
        self.clock = clock
        self.say = say or log.info
        self.tree = app_commands.CommandTree(self)
        self._add_commands()

    # 準備
    async def setup_hook(self) -> None:
        self.loop.create_task(self.booking_loop())

    def hours(self) -> str:
        return self.store.get_setting("hours", DEFAULT_HOURS)

    def worlds(self) -> list[str]:
        return self.store.worlds()

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self._sync(guild)
        self.say(f"Discord ボットが動いています（{', '.join(g.name for g in self.guilds) or 'サーバー未参加'}）")
        if self.guilds:
            self.store.set_setting("guild_id", str(self.guilds[0].id))
        if self.guilds and not self.store.get_setting("panel_channel_id"):
            await self.first_setup(self.guilds[0])
        await self.ensure_avatar(self.user)
        if self.guilds:
            await self.rename_old_channels(self.guilds[0])
            await self.ensure_guide_channels(self.guilds[0])
            await self.announce_version(self.guilds[0])
        for tester in self.store.waiting_testers():
            try:
                await self.send_ready_now(tester)
            except discord.HTTPException as exc:
                log.warning("準備の案内を送れませんでした (%s): %s", tester["user_id"], exc)

    def news_text(self, ver: str = "") -> str:
        ver = ver or version.VERSION
        lines = [f"🆕 **Geaspec {ver}** になりました", ""]
        lines += [f"・{c}" for c in version.CHANGES.get(ver, [])]
        if ver == version.VERSION:
            lines += ["", f"ダウンロード（運営・貸し手ベータ向け）: {version.RELEASE_URL}"]
        return "\n".join(lines)[:1990]

    async def announce_version(self, guild: discord.Guild) -> bool:
        """新しい版ではじめて動いたときに、名前に「更新情報」を含む場所へ 1 回だけ書く。

        前に書いた版から間が空いていたら (途中の版を飛ばして入れ替えたとき)、その間の版の分も古い順に書く。
        """
        last = self.store.get_setting("announced_version")
        if last == version.VERSION:
            return False
        ok, message = await self.post_news(guild, unannounced_versions(last))
        self.say(f"Discord ボット: {message}")
        return ok

    async def post_news(self, guild: discord.Guild, versions: list[str]) -> tuple[bool, str]:
        """versions のお知らせを書く。(書けたか, 運営向けの説明) を返す。"""
        targets = [c for c in [*guild.text_channels, *getattr(guild, "forums", []), *guild.threads]
                   if NEWS_NAME in c.name]
        if not targets:
            if not guild.me.guild_permissions.manage_channels:
                return False, (f"名前に「{NEWS_NAME}」を含むチャンネルがなく、作る権限もないので、"
                               "新しい版のお知らせは書きませんでした。")
            try:
                # 誰でも読めて、書けるのは運営 (サーバーの管理者) とボットだけ
                targets = [await guild.create_text_channel(NEWS_CHANNEL_NAME, reason="Geaspec の更新情報", overwrites={
                    guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                })]
            except discord.HTTPException as exc:
                return False, f"「{NEWS_CHANNEL_NAME}」チャンネルを作れませんでした（{exc}）"
        target = targets[0]
        try:
            for ver in versions:
                if isinstance(target, discord.ForumChannel):
                    await target.create_thread(name=f"Geaspec {ver}", content=self.news_text(ver))
                else:
                    if isinstance(target, discord.Thread) and target.archived:
                        await target.edit(archived=False)
                    await target.send(self.news_text(ver))
        except discord.HTTPException as exc:
            return False, f"「{target.name}」に新しい版のお知らせを書けませんでした（{exc}）"
        self.store.set_setting("announced_version", version.VERSION)
        return True, f"「{target.name}」に {', '.join(versions)} のお知らせを書きました"

    async def cmd_news(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await self.post_news(interaction.guild, [version.VERSION])
        head = f"いま動いているのは Geaspec {version.VERSION} です。"
        await interaction.followup.send(head + message, ephemeral=True)

    async def first_setup(self, guild: discord.Guild) -> None:
        """はじめて動いたときに、ロールとチャンネルを作って申し込みボタンを置く。"""
        try:
            panel, ops_channel, ops_role = await self.prepare(guild)
        except SetupError as exc:
            self.say(f"Discord ボット: 自動で準備できませんでした。{exc}")
            return
        except discord.HTTPException as exc:
            self.say(f"Discord ボット: 自動で準備できませんでした（{exc}）。Discord で /geaspec-setup を実行してください。")
            return
        self.say(f"Discord ボット: 「{panel.name}」「{ops_channel.name}」チャンネルと「{ops_role.name}」ロールを用意しました。")

    def welcome_text(self, panel: str, question: str) -> str:
        return "\n".join([
            "**同じ場所で、同じ景色を。**",
            "同じワールドにいても、Quest と PC では見えている景色が違います。Geaspec は、ゲーミング PC を持つ人の"
            "空いている時間を予約して、いつもの Quest のまま PCVR の VRChat を遊べるサービスです。"
            "いまは無料・招待制のテスト中です。",
            "",
            "**遊ぶのに必要なもの**",
            "・Quest（スタンドアロンの VR ヘッドセット）",
            "・Virtual Desktop（有料アプリ）",
            "・自分の VRChat アカウントと、2 段階認証のコードを受け取るスマホ",
            "",
            "**はじめかた**",
            f"1. {panel} の「テスターに申し込む」ボタンを押してください",
            "2. あなた専用のスレッドに、予約と接続の案内が届きます",
            f"3. 予約サイト: {self.site_url}",
            "",
            "**お願い**",
            "・貸してくださる方の PC を大切に使ってください",
            "・招待リンクや、予約サイトのログイン情報を人に渡さないでください",
            "・VRChat の利用規約を守ってください",
            "",
            f"わからないことは {question} でお気軽にどうぞ。",
        ])

    async def rename_old_channels(self, guild: discord.Guild) -> None:
        """前の版が作った絵文字付きのチャンネル名を、絵文字のない名前に付け直す。"""
        if not guild.me.guild_permissions.manage_channels:
            return
        for channel in list(guild.text_channels):
            new = OLD_CHANNEL_NAMES.get(channel.name)
            if new is None:
                continue
            try:
                await channel.edit(name=new, reason="チャンネル名から絵文字を外す")
            except discord.HTTPException as exc:
                self.say(f"Discord ボット: 「{channel.name}」の名前を変えられませんでした（{exc}）")

    async def ensure_avatar(self, user) -> None:
        """ボットのアイコンを Geaspec のロゴにする。変えるのは 1 回だけ (Discord はアイコンの変更回数に制限がある)。"""
        if user is None or self.store.get_setting("avatar") == AVATAR_VERSION:
            return
        try:
            await user.edit(avatar=AVATAR_PATH.read_bytes())
        except (OSError, discord.HTTPException) as exc:
            self.say(f"Discord ボット: アイコンを変えられませんでした（{exc}）")
            return
        self.store.set_setting("avatar", AVATAR_VERSION)
        self.say("Discord ボット: アイコンを Geaspec のロゴにしました。")

    async def ensure_guide_channels(self, guild: discord.Guild) -> None:
        """「はじめに」と「質問・困りごと」を 1 回だけ用意する。運営が後で消したら、作り直さない。"""
        if self.store.get_setting("guide_channels"):
            return
        if not guild.me.guild_permissions.manage_channels:
            self.say("Discord ボット: チャンネルを作る権限がないので、「はじめに」と「質問・困りごと」は作りませんでした。")
            return
        reason = "Geaspec の案内"
        try:
            question = next((c for c in guild.text_channels if "質問" in c.name), None)
            if question is None:
                question = await guild.create_text_channel(QUESTION_CHANNEL_NAME, reason=reason)
            if not any("はじめに" in c.name for c in guild.text_channels):
                welcome = await guild.create_text_channel(WELCOME_CHANNEL_NAME, reason=reason, overwrites={
                    guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                })
                panel = discord.utils.get(guild.text_channels, id=int(self.store.get_setting("panel_channel_id") or 0))
                await welcome.send(self.welcome_text(panel.mention if panel else f"「{PANEL_CHANNEL_NAME}」", question.mention))
        except discord.HTTPException as exc:
            self.say(f"Discord ボット: 「はじめに」「質問・困りごと」を用意できませんでした（{exc}）")
            return
        self.store.set_setting("guide_channels", "1")
        self.say("Discord ボット: 「はじめに」と「質問・困りごと」を用意しました。")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync(guild)

    async def _sync(self, guild: discord.Guild) -> None:
        # サーバーごとに登録すると、コマンドがすぐ使えるようになる
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            log.warning("コマンドを登録できませんでした (%s): %s", guild.name, exc)

    def _add_commands(self) -> None:
        manage = app_commands.default_permissions(manage_guild=True)

        @self.tree.command(name="geaspec-setup", description="申し込みボタン・運営ロール・運営チャンネルを用意する（運営用）")
        @app_commands.describe(here="このチャンネルに申し込みボタンを置く（いいえ: 専用のチャンネルを作る）",
                               ops_channel="お知らせを受け取るチャンネル（空なら「運営」チャンネルを作る）",
                               ops_role="運営のロール（空なら「運営」ロールを作る）",
                               pc_name="Virtual Desktop の一覧に出る PC の名前（空ならそのまま。はじめは LG）",
                               hours="テストができる時間（空ならそのまま。はじめは「平日は19時以降、土日は終日」）",
                               worlds="FPS を測るワールド（カンマで区切って 3 つまで）")
        @app_commands.guild_only()
        @manage
        async def setup(interaction: discord.Interaction, here: bool = False,
                        ops_channel: discord.TextChannel | None = None, ops_role: discord.Role | None = None,
                        pc_name: str = "", hours: str = "", worlds: str = "") -> None:
            for key, value in (("hours", hours), ("worlds", worlds)):
                if value.strip():
                    self.store.set_setting(key, value.strip()[:80])
            await self.cmd_setup(interaction, here, ops_channel, ops_role, pc_name)

        @self.tree.command(name="geaspec-link", description="予約サイトに入るためのリンクを受け取る・テスターに送る（運営用）")
        @app_commands.describe(member="このテスターのスレッドに、本人専用のリンクを送る（空なら自分用のリンクを受け取る）")
        @app_commands.guild_only()
        @manage
        async def link(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
            await self.cmd_link(interaction, member)

        @self.tree.command(name="geaspec-results", description="テストの集計と CSV を受け取る（運営用）")
        @app_commands.guild_only()
        @manage
        async def results(interaction: discord.Interaction) -> None:
            await self.cmd_results(interaction)

        @self.tree.command(name="geaspec-news", description="いまの版の更新情報を「更新情報」チャンネルに書き直す（運営用）")
        @app_commands.guild_only()
        @manage
        async def news(interaction: discord.Interaction) -> None:
            await self.cmd_news(interaction)

        @self.tree.command(name="geaspec-erase", description="テスターの名前・Meta ユーザー名などを消す（運営用・テストが終わったら）")
        @app_commands.guild_only()
        @manage
        async def erase(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "テスターの名前・VRChat の名前・Meta ユーザー名・都合のいい日時を消します。アンケートの答えと時間は残ります。",
                view=make_view(button("消す", "gs:erase", discord.ButtonStyle.danger)), ephemeral=True)

    async def cmd_link(self, interaction: discord.Interaction, member: discord.abc.User | None) -> None:
        if member is None:
            await interaction.response.send_message(
                f"運営用のリンクです（{invite.LINK_DAYS}日有効・ほかの人には渡さないでください）\n"
                f"{self.site_link({'user_id': interaction.user.id})}", ephemeral=True)
            return
        tester = self.store.tester(member.id)
        thread = await self._existing_thread(tester) if tester else None
        if thread is None:
            await interaction.response.send_message("この人の申し込み（スレッド）が見つかりませんでした。", ephemeral=True)
            return
        await thread.send(f"<@{member.id}> 予約サイトに入るための、あなた専用のリンクです（{invite.LINK_DAYS}日有効・"
                          f"ほかの人には渡さないでください）\n{self.site_link(tester)}",
                          view=make_view(button("🔗 サイトを開く", "gs:link")))
        await interaction.response.send_message(f"{thread.mention} にリンクを送りました。", ephemeral=True)

    # 権限
    def is_ops(self, user: discord.abc.User) -> bool:
        if not isinstance(user, discord.Member):
            return False
        role_id = int(self.store.get_setting("ops_role_id", "0") or 0)
        return user.guild_permissions.manage_guild or any(r.id == role_id for r in user.roles)

    def ops_role_mention(self) -> str:
        role_id = self.store.get_setting("ops_role_id")
        return f"<@&{role_id}>" if role_id else "運営"

    async def notify_ops(self, content: str, view: discord.ui.View | None = None) -> None:
        channel_id = int(self.store.get_setting("ops_channel_id", "0") or 0)
        channel = self.get_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(content, view=view) if view else await channel.send(content)
        except discord.HTTPException as exc:
            log.warning("運営チャンネルに送れませんでした: %s", exc)

    # コマンド
    async def prepare(self, guild: discord.Guild, *, invoker: discord.Member | None = None,
                      panel: discord.TextChannel | None = None, ops_channel: discord.TextChannel | None = None,
                      ops_role: discord.Role | None = None, pc_name: str = "", force_panel: bool = False) -> tuple:
        """運営ロール・運営チャンネル・申し込みチャンネルをそろえて、申し込みボタンを置く。

        同じ名前のものがあれば作らずに使う。作るにはボットに「ロールの管理」「チャンネルの管理」が要る。
        """
        me = guild.me
        perms = me.guild_permissions
        need = []
        if ops_role is None and discord.utils.get(guild.roles, name=OPS_ROLE_NAME) is None and not perms.manage_roles:
            need.append("ロールの管理")
        makes_channel = (ops_channel is None and discord.utils.get(guild.text_channels, name=OPS_CHANNEL_NAME) is None) or (
            panel is None and discord.utils.get(guild.text_channels, name=PANEL_CHANNEL_NAME) is None)
        if makes_channel:
            need += [n for n, ok in (("チャンネルの管理", perms.manage_channels), ("ロールの管理", perms.manage_roles))
                     if not ok and n not in need]
        if need:
            raise SetupError(
                "ボットに「" + "」「".join(need) + "」の権限がありません。Discord のサーバー設定 → ロール → "
                f"{me.display_name} → 権限 でオンにしてから、/geaspec-setup を実行してください。")

        reason = "Geaspec テスターの受付"
        if ops_role is None:
            ops_role = discord.utils.get(guild.roles, name=OPS_ROLE_NAME) or await guild.create_role(
                name=OPS_ROLE_NAME, mentionable=True, reason=reason)
        # 実行した人 (自動のときはサーバーの持ち主) に運営ロールを付ける
        member = invoker
        if member is None:
            try:
                member = guild.get_member(guild.owner_id) or await guild.fetch_member(guild.owner_id)
            except discord.HTTPException:
                member = None
        if member is not None and perms.manage_roles and ops_role < me.top_role and ops_role not in member.roles:
            await member.add_roles(ops_role, reason=reason)

        if ops_channel is None:
            ops_channel = discord.utils.get(guild.text_channels, name=OPS_CHANNEL_NAME) or await guild.create_text_channel(
                OPS_CHANNEL_NAME, reason=reason, overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    ops_role: discord.PermissionOverwrite(view_channel=True),
                    me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                })
        if panel is None:
            panel = discord.utils.get(guild.text_channels, name=PANEL_CHANNEL_NAME) or await guild.create_text_channel(
                PANEL_CHANNEL_NAME, reason=reason, overwrites={
                    # テスターはボタンを押すだけ。自分のスレッドの中では書ける
                    guild.default_role: discord.PermissionOverwrite(
                        send_messages=False, send_messages_in_threads=True, create_public_threads=False,
                        create_private_threads=False, add_reactions=False),
                    me: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, send_messages_in_threads=True,
                        create_private_threads=True, manage_threads=True, embed_links=True, read_message_history=True),
                })

        self.store.set_setting("ops_channel_id", str(ops_channel.id))
        self.store.set_setting("ops_role_id", str(ops_role.id))
        if pc_name.strip() or not self.store.get_setting("pc_name"):
            self.store.set_setting("pc_name", pc_name.strip() or "LG")
        # 同じチャンネルに 2 つ目のボタンは置かない (PC の名前を変えるだけのとき など)
        if not force_panel and self.store.get_setting("panel_channel_id") == str(panel.id):
            return panel, ops_channel, ops_role
        embed = discord.Embed(title="Geaspec テスト", description=panel_text(self.hours(), self.site_url), color=0x5B3CF0)
        await panel.send(embed=embed, view=make_view(
            button("🙋 テスターに申し込む", "gs:apply", discord.ButtonStyle.primary)))
        self.store.set_setting("panel_channel_id", str(panel.id))
        await ops_channel.send(
            f"準備ができました。{panel.mention} に申し込みボタンを置きました。\n"
            "・申し込み・テスト開始・「困った」・テスト完了のお知らせは、このチャンネルに届きます\n"
            "・申し込みが来ると、テスターのスレッドに予約の案内が自動で届きます。予約が入るまでに、LG の Virtual Desktop Streamer に"
            "その人の Meta ユーザー名を入れておいてください\n"
            f"・テスターは家の外からつなぐので、LG の {REMOTE_SETTING}に必ずチェックを入れておいてください\n"
            "・予約が入ったときと、開始 15 分前にもここでお知らせします\n"
            "・集計と表は `/geaspec-results`、テストが終わったら `/geaspec-erase` で名前などを消せます\n"
            f"・VD の一覧に出る PC の名前: {self.store.get_setting('pc_name', 'LG')}（違うときは `/geaspec-setup pc_name:名前`）\n"
            f"・テストができる時間: {self.hours()}（変えるときは `/geaspec-setup hours:…`）\n"
            f"・FPS を測るワールド: {'／'.join(self.worlds())}（変えるときは `/geaspec-setup worlds:A, B, C`）")
        return panel, ops_channel, ops_role

    async def cmd_setup(self, interaction: discord.Interaction, here: bool, ops_channel: discord.TextChannel | None,
                        ops_role: discord.Role | None, pc_name: str) -> None:
        panel = None
        if here:
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message("ふつうのテキストチャンネルで実行してください。", ephemeral=True)
                return
            panel = interaction.channel
            perms = panel.permissions_for(panel.guild.me)
            missing = [name for name, ok in (
                ("プライベートスレッドを作成", perms.create_private_threads),
                ("Threadsでメッセージを送る", perms.send_messages_in_threads),
                ("スレッドを管理", perms.manage_threads),
                ("メッセージを送る", perms.send_messages),
            ) if not ok]
            if missing:
                await interaction.response.send_message(
                    "このチャンネルで、ボットに次の権限がありません: " + "、".join(missing), ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            panel, ops_channel, ops_role = await self.prepare(
                interaction.guild, invoker=interaction.user if isinstance(interaction.user, discord.Member) else None,
                panel=panel, ops_channel=ops_channel, ops_role=ops_role, pc_name=pc_name, force_panel=here)
        except SetupError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        note = "" if ops_role.mentionable else (
            f"\n※ {ops_role.name} ロールの「このロールに対して@mentionを許可する」をオンにすると、「困った」のときに通知が届きます。")
        await interaction.followup.send(
            f"申し込みボタンは {panel.mention}、お知らせは {ops_channel.mention} です。\n"
            f"VD の一覧に出る PC の名前: {self.store.get_setting('pc_name', 'LG')} ／ テストができる時間: {self.hours()} ／ "
            f"FPS を測るワールド: {'／'.join(self.worlds())}{note}", ephemeral=True)

    async def cmd_results(self, interaction: discord.Interaction) -> None:
        summary = self.store.summary()
        embed = discord.Embed(title="テストの集計", description="\n".join(summary_lines(summary)), color=0x5B3CF0)
        forms = feedback.load_from_site_db(self.site_db) if self.site_db else None
        stamp = f"{self.clock().astimezone():%Y%m%d-%H%M}"
        files = [discord.File(io.BytesIO(text.encode("utf-8-sig")), filename=f"geaspec-{kind}-{stamp}.csv")
                 for kind, text in (("tests", self.store.to_csv(forms)), ("lenders", self.store.lenders_csv()),
                                    ("notes", self.store.notes_csv()))]
        await interaction.response.send_message(embed=embed, files=files, ephemeral=True)

    # ボタン・選択
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        if not custom_id.startswith("gs:"):
            return
        try:
            # 7 日動きがないとスレッドは自動でしまわれ、そのままだと返事を書き込めない
            if isinstance(interaction.channel, discord.Thread) and interaction.channel.archived:
                await interaction.channel.edit(archived=False)
            await self.dispatch_component(interaction, custom_id.split(":")[1:])
        except Exception:  # noqa: BLE001 - 1 回の失敗でボットを止めない
            log.exception("ボタンの処理に失敗しました: %s", custom_id)
            await self.reply(interaction, "うまく処理できませんでした。少し待ってからもう一度押すか、このスレッドに書いてください。")

    async def reply(self, interaction: discord.Interaction, text: str, **kwargs) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kwargs)

    async def dispatch_component(self, interaction: discord.Interaction, parts: list[str]) -> None:
        action = parts[0]
        if action == "apply":
            await interaction.response.send_message("どちらで参加しますか？", view=apply_choice_view(), ephemeral=True)
        elif action == "as":
            lend = parts[1:2] == ["lend"]
            await interaction.response.send_modal(ApplyModal(self, self.store.tester(interaction.user.id), lend=lend))
        elif action in ("group", "vd"):
            await self.on_profile(interaction, action)
        elif action == "ready":
            await self.on_ready_pressed(interaction, int(parts[1]))
        elif action == "erase":
            if not self.is_ops(interaction.user):
                await self.reply(interaction, "運営だけが使えます。")
                return
            count = self.store.erase_personal()
            await interaction.response.edit_message(content=f"{count}人分の名前などを消しました。", view=None)
        else:
            await self.on_session_action(interaction, action, parts[1:])

    # 申し込み
    async def on_apply(self, interaction: discord.Interaction, modal: ApplyModal) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if getattr(interaction, "guild_id", None):
            self.store.set_setting("guild_id", str(interaction.guild_id))
        user = interaction.user
        before = self.store.tester(user.id)
        tester = self.store.apply(
            user.id, user.display_name, vrchat=modal.vrchat.value.strip(), meta=modal.meta.value.strip(),
            gear=modal.gear.value.strip(), region=modal.region.value.strip(), schedule=modal.schedule.value.strip(),
            now=self.clock())
        current = (before or {}).get("grp") or ""
        self.store.set_group(user.id, "C" if modal.lend else (current if current in ("A", "B") else "A"))
        tester = self.store.tester(user.id)
        thread = await self._existing_thread(tester)
        if thread is None:
            parent = interaction.channel
            if isinstance(parent, discord.Thread):
                parent = parent.parent
            thread = await parent.create_thread(
                name=f"{user.display_name} さんのテスト"[:100], type=discord.ChannelType.private_thread,
                invitable=False, auto_archive_duration=10080)
            await thread.add_user(user)
            self.store.set_thread(user.id, thread.id)
            tester = self.store.tester(user.id)
            head = (f"{user.mention} さん、申し込みありがとうございます！ここがあなた専用のスレッドです"
                    "（運営とあなただけが見られます）。")
            if modal.lend:
                await thread.send(head + "\nこのあと、PC を貸す準備の手順が届きます。")
            else:
                await thread.send(head + "\nQuest の Virtual Desktop を持っているか、えらんでください"
                                  "（えらばなくても、このまま予約に進めます）。", view=welcome_view(tester))
        if modal.lend:
            if self.store.mark_ready(user.id, self.clock()):
                await self.send_extras(thread, tester)
            elif not self.store.get_setting(f"sent_lender:{user.id}"):
                await self.send_extras(thread, tester)  # 借りる側で申し込んだあとに、貸す側でも申し込んだ
        else:
            await self.send_ready_now(tester, thread)
        await interaction.followup.send(f"受け付けました。続きは {thread.mention} でどうぞ。", ephemeral=True)

        updated = "（申し込み内容の更新）" if before else ""
        lines = [f"**新しい申し込み**（{'PC を貸す' if modal.lend else 'PC を借りる'}）{updated} {user.mention} {thread.mention}",
                 f"VRChat: {tester['vrchat']} ／ 機材: {tester['gear']} ／ 地方: {tester['region']} ／ 都合: {tester['schedule'] or '—'}",
                 f"Meta ユーザー名: `{tester['meta']}`"]
        if before and before["meta"] and before["meta"] != tester["meta"]:
            lines.append(f"【注意】Meta ユーザー名が変わりました: `{before['meta']}` → `{tester['meta']}`")
            if tester["status"] == "ready":
                lines.append("→ LG の Virtual Desktop Streamer に入れたユーザー名も直してください（古いままだとつながりません）。")
        if tester["status"] != "ready":
            lines.append("→ 案内はスレッドに自動で届きます。予約が入るまでに、LG の Virtual Desktop Streamer に"
                         "このユーザー名を追加しておいてください。")
        await self.notify_ops("\n".join(lines))

    async def _existing_thread(self, tester: dict) -> discord.Thread | None:
        if not tester.get("thread_id"):
            return None
        thread = self.get_channel(tester["thread_id"])
        if thread is None:
            try:
                thread = await self.fetch_channel(tester["thread_id"])
            except discord.HTTPException:
                return None
        if isinstance(thread, discord.Thread) and thread.archived:
            await thread.edit(archived=False)
        return thread if isinstance(thread, discord.Thread) else None

    async def on_profile(self, interaction: discord.Interaction, kind: str) -> None:
        tester = self.store.tester_by_thread(interaction.channel_id)
        if tester is None or tester["user_id"] != interaction.user.id:
            await self.reply(interaction, "申し込んだ本人だけがえらべます。")
            return
        value = (interaction.data.get("values") or [""])[0]
        if kind == "group":
            self.store.set_group(tester["user_id"], value)
        else:
            self.store.set_has_vd(tester["user_id"], value == "1")
        await interaction.response.edit_message(view=welcome_view(self.store.tester(tester["user_id"])))
        await self.send_extras(interaction.channel, self.store.tester(tester["user_id"]))

    async def send_extras(self, thread, tester: dict) -> None:
        """えらんだ答えに合わせた追加の案内 (それぞれ 1 回だけ)。VD を持っていない人と、PC を貸せる人 (C) に。"""
        uid = tester["user_id"]
        if tester.get("has_vd") == 0 and not self.store.get_setting(f"sent_vd:{uid}"):
            self.store.set_setting(f"sent_vd:{uid}", "1")
            await thread.send(vd_install_text(tester).rstrip())
        if tester.get("grp") == "C" and not self.store.get_setting(f"sent_lender:{uid}"):
            self.store.set_setting(f"sent_lender:{uid}", "1")
            text, view = self.card("lend", tester, 0)
            await thread.send(text, view=view)

    async def send_ready_now(self, tester: dict, thread: discord.Thread | None = None) -> bool:
        """運営を待たずに、スレッドへ予約の案内を送る (1 回だけ)。"""
        if not self.store.mark_ready(tester["user_id"], self.clock()):
            return False
        thread = thread or await self._existing_thread(tester)
        if thread:
            await self.send_ready(thread, self.store.tester(tester["user_id"]))
        await self.notify_ops(f"<@{tester['user_id']}> さんに予約の案内を送りました{' ' + thread.mention if thread else ''}。"
                              f"LG の Virtual Desktop Streamer に `{tester['meta']}` が入っているか確かめてください。")
        return True

    async def on_ready_pressed(self, interaction: discord.Interaction, user_id: int) -> None:
        if not self.is_ops(interaction.user):
            await self.reply(interaction, "運営だけが押せます。")
            return
        tester = self.store.tester(user_id)
        if tester is None:
            await self.reply(interaction, "この申し込みは見つかりませんでした。")
            return
        first = self.store.mark_ready(user_id, self.clock())
        await interaction.response.edit_message(view=make_view(
            button(f"✅ 登録済み（{interaction.user.display_name}）", "gs:noop", disabled=True)))
        thread = await self._existing_thread(tester)
        if first and thread:
            await self.send_ready(thread, self.store.tester(user_id))

    def site_link(self, tester: dict | None, next_path: str = "") -> str:
        """本人専用のリンク。開くとサイトに入れて、申し込みの Meta ユーザー名が新規登録に引き継がれる。"""
        if not self.invite_secret:
            return self.site_url + next_path
        token = invite.make_token(self.invite_secret, meta=(tester or {}).get("meta", ""),
                                  discord_id=(tester or {}).get("user_id", 0), now=self.clock(),
                                  thread=self.thread_ref(tester))
        url = f"{self.site_url}/join?t={token}"
        return url + (f"&next={urllib.parse.quote(next_path, safe='')}" if next_path else "")

    def thread_ref(self, tester: dict | None) -> str:
        """「サーバー番号/スレッド番号」。サイトはこれで、予約のあとにスレッドへ戻るボタンを出す。"""
        guild, thread = self.store.get_setting("guild_id"), (tester or {}).get("thread_id")
        return f"{guild}/{thread}" if guild and thread else ""

    def site_lines(self, tester: dict | None = None) -> str:
        if self.invite_secret:
            return (f"予約サイト（あなた専用のリンク）: {self.site_link(tester)}\n"
                    f"　ほかの人には渡さないでください。{invite.LINK_DAYS}日で使えなくなったら「サイトを開く」ボタンで新しいリンクが届きます\n")
        text = f"予約サイト: {self.site_url}\n"
        if self.site_password:
            text += f"　サイトを開くと合言葉を聞かれます。合言葉: `{self.site_password}`（ほかの人には教えないでください）\n"
        return text

    def installer_url(self) -> str:
        """貸し手のアプリのインストーラー (いま動いているサイトと同じ版)。/geaspec-setup で変えた URL があればそちら。"""
        return self.store.get_setting("agent_url") or (
            f"https://github.com/lihitothird-alt/Geaspec/releases/download/agent-v{version.VERSION}/"
            f"GeaspecAgent-Setup-{version.VERSION}.exe")

    # Discord では短いカードを 1 枚ずつ見せる。「次へ」を押すたびに、同じメッセージが次の手順に変わる。
    # くわしい説明 (画像つき) は Web サイトの説明ページに置く
    def card_pages(self, kind: str, tester: dict | None) -> list[tuple[str, str]]:
        guide = f"{self.site_url}/guide"
        if kind == "lend":
            return [
                ("① Virtual Desktop Streamer を入れる",
                 "自分の PC に **Virtual Desktop Streamer**（無料）を入れて、「Allow remote connections」にチェックを入れます。\n"
                 f"ダウンロード（「Download Streamer」ボタン）:\n{STREAMER_URL}"),
                ("② Geaspec アプリを入れる",
                 "ダウンロードして開きます。青い画面が出たら「詳細情報」→「実行」→「インストール」。\n"
                 "貸し出し専用の Windows ユーザーで入れるのがおすすめです（そのユーザーの VRChat の設定は毎回消えます）。\n"
                 f"ダウンロード:\n{self.installer_url()}\n画像つきの説明:\n{guide}?for=lend"),
                ("③ ログインする",
                 "予約サイトでアカウントを作り、アプリに同じメールアドレスとパスワードでログインします。"
                 "できたら下の「アプリを入れた」を押してください。運営が確認すると、ここにお知らせが届きます。\n"
                 f"予約サイト:\n{self.site_link(tester)}"),
                ("④ 貸せる時間を決める",
                 "予約サイトの「貸し出し」ページのカレンダーで、貸せる時間を選んで「保存」。"
                 "あとは PC の電源を入れておくだけです。アプリは画面右下のアイコンで動き、時間になると自動で貸し出して、"
                 f"終わると借りた人のデータを消します。\n{NOTES_HINT}\n貸し出しページ:\n{self.site_url}/lend"),
            ]
        vd = ("まず Quest に **Virtual Desktop**（Meta のストアの有料アプリ）を入れておいてください。\n"
              if (tester or {}).get("has_vd") == 0 else "")
        return [
            ("① 予約する",
             vd + "予約サイトで好きな時間を選んで「予約する」を押します。下の「サイトを開く」からも入れます。\n"
             + self.site_lines(tester).rstrip()),
            ("② 当日までの準備",
             "・VPN を切る\n・VRChat に**メールアドレスとパスワード**でログインできるようにしておく（Steam のボタンは使いません）\n"
             f"・家の Wi-Fi で参加する\nくわしい説明:\n{guide}#login"),
            ("③ 当日のテスト",
             "予約ページの「テストのチェックリスト」どおりに進めるだけです。遊び終わったら、FPS などの数字とアンケート"
             f"（1〜2分）を送って完了です。\nテストができる時間: {self.hours()}\n{NOTES_HINT}\n画像つきの説明:\n{guide}"),
        ]

    def card(self, kind: str, tester: dict | None, index: int) -> tuple[str, discord.ui.View]:
        pages = self.card_pages(kind, tester)
        index = max(0, min(index, len(pages) - 1))
        title, body = pages[index]
        mention = f"<@{tester['user_id']}> " if tester else ""
        text = f"{mention}**{title}**（{index + 1}/{len(pages)}）\n{body}"
        items = [button("◀ 戻る", f"gs:pg:{kind}:{index - 1}", row=0, disabled=index == 0),
                 button("次へ ▶", f"gs:pg:{kind}:{index + 1}", discord.ButtonStyle.primary, row=0,
                        disabled=index == len(pages) - 1)]
        if kind == "lend":
            items += [button("📥 アプリを入れた", "gs:lenderapp", discord.ButtonStyle.success, row=1),
                      button("🔗 サイトを開く", "gs:link", row=1)]
        else:
            items += [button("🔗 サイトを開く", "gs:link", row=1),
                      button("🆘 予約で迷った", "gs:bookhelp", discord.ButtonStyle.danger, row=1)]
        return text, make_view(*items)

    async def on_page(self, interaction: discord.Interaction, tester: dict, args: list[str]) -> None:
        kind = args[0] if args and args[0] in ("borrow", "lend") else "borrow"
        try:
            index = int(args[1]) if len(args) > 1 else 0
        except ValueError:
            index = 0
        text, view = self.card(kind, tester, index)
        await interaction.response.edit_message(content=text, view=view)

    async def send_ready(self, thread: discord.Thread, tester: dict) -> None:
        """予約の案内。短いカードで、「次へ」を押すと次の手順に変わる。"""
        user_id = tester["user_id"]
        text, view = self.card("borrow", tester, 0)
        await thread.send(text, view=view)
        if tester.get("has_vd") == 0:
            self.store.set_setting(f"sent_vd:{user_id}", "1")  # 上の案内に入れたので、もう送らない
        await self.send_extras(thread, tester)

    # チェックリスト (予約した → テスト → 写真・アンケート → 送信)
    def booking_info(self, uid: int) -> dict:
        try:
            return json.loads(self.store.get_setting(f"booking:{uid}") or "{}")
        except ValueError:
            return {}

    def booking_line(self, uid: int) -> str:
        b = self.booking_info(uid)
        return f"予約: **{b['when']}** ／ PC「{b['host']}」" if b.get("when") else ""

    def tester_for(self, booking: dict) -> dict | None:
        """予約した人のテスター記録。サイトに残したスレッドで探し、なければ Meta ユーザー名で探す。"""
        tid = booking_notice.thread_id(booking.get("thread", ""))
        return (self.store.tester_by_thread(tid) if tid else None) or self.store.tester_by_meta(booking.get("meta", ""))

    def lender_for(self, booking_or_host: dict) -> dict | None:
        tid = booking_notice.thread_id(booking_or_host.get("owner_thread", ""))
        return self.store.tester_by_thread(tid) if tid else None

    def current_session(self, tester: dict) -> dict | None:
        """チェックリストに出す今回のテスト。前のテストが送信済みで、そのあとに予約し直したら、まだ始めていない扱い。"""
        s = self.store.latest_session(tester["thread_id"]) if tester.get("thread_id") else None
        if s is None or s["status"] != "done":
            return s
        cycle = self.store.get_setting(f"cycle:{tester['user_id']}")
        return s if cycle and s["opened_at"] >= cycle else None

    def _now_iso(self) -> str:
        return self.clock().astimezone(dt.timezone.utc).isoformat(timespec="seconds")

    def booking_page(self, booking_id: int) -> str:
        """予約ページ (テストのチェックリストがある)。URL は行の最後に置く (後ろの文字までリンクにされないように)。"""
        return f"{self.site_url}/bookings/{booking_id}"

    def remember_booking(self, uid: int, booking: dict) -> None:
        """予約の日時と PC を覚えておく (Discord の「テストを始める」で PC の名前を出すため)。"""
        self.store.set_setting(f"booking:{uid}", json.dumps(
            {"id": booking["id"], "when": booking_notice.when_text(booking["start"], booking["end"]),
             "host": booking["host"]}, ensure_ascii=False))
        self.store.set_setting(f"checklist_booking:{uid}", str(booking["id"]))

    async def begin_checklist(self, tester: dict, booking: dict | None, thread=None) -> None:
        """予約できた。予約の日時を覚えて、まっさらなチェックリストを出す (テストの途中なら、その続き)。"""
        uid = tester["user_id"]
        if booking:
            self.remember_booking(uid, booking)
        if self.store.open_session(tester["thread_id"]) is None:
            self.store.set_setting(f"cycle:{uid}", self._now_iso())
        await self.show_checklist(tester, thread)

    async def show_checklist(self, tester: dict, thread=None, *, move: bool = True) -> None:
        """チェックリストを、スレッドのいちばん下に出し直す (move=False なら、その場で書き換える)。"""
        thread = thread or await self._existing_thread(tester)
        if thread is None:
            return
        uid = tester["user_id"]
        booked = bool(self.store.get_setting(f"cycle:{uid}"))
        text, view = checklist_message(checklist_state(self.current_session(tester), booked), self.booking_line(uid))
        old = self.store.get_setting(f"checklist_msg:{uid}")
        partial = getattr(thread, "get_partial_message", None)
        if old and partial:
            try:
                if not move:
                    await partial(int(old)).edit(content=text, view=view)
                    return
                await partial(int(old)).delete()
            except (discord.HTTPException, ValueError):
                pass
        sent = await thread.send(text, view=view)
        if getattr(sent, "id", None):
            self.store.set_setting(f"checklist_msg:{uid}", str(sent.id))

    async def on_submit(self, interaction, tester, session, args) -> None:
        state = checklist_state(session, True)
        missing = [CHECK_LABELS[k] for k in CHECK_REQUIRED if not state[k]]
        if missing:
            await self.reply(interaction, "まだ終わっていない項目があります。\n" + "\n".join(f"⬜ {m}" for m in missing))
            return
        self.store.complete(session["id"], self.clock(), stuck=session["stuck"], comment=session["comment"])
        text, view = checklist_message(checklist_state(self.store.session(session["id"]), True),
                                       self.booking_line(tester["user_id"]))
        await interaction.response.edit_message(content=text, view=view)
        await interaction.followup.send(self.thanks_text(session["id"], tester), view=self.again_view())
        await self.report_done(session["id"])

    # いま終わる (早く遊び終わったら、時間を待たずにログイン情報を消す)
    async def on_end_now(self, interaction: discord.Interaction, tester: dict, confirmed: bool) -> None:
        if not confirmed:
            await interaction.response.send_message(
                "いま終わりにしますか？ VRChat が閉じて、PC からあなたのログイン情報が消えます（もう一度は入れません）。",
                view=make_view(button("はい、いま終わる", "gs:endnow:yes", discord.ButtonStyle.danger)), ephemeral=True)
            return
        uid = tester["user_id"]
        now = self.clock()
        running = [b for b in (booking_notice.load_bookings(self.site_db, now) if self.site_db else [])
                   if (b["status"] == "active" or (b["status"] == "confirmed" and b["start"] <= now))
                   and (t := self.tester_for(b)) and t["user_id"] == uid]
        if not running:
            await self.reply(interaction, "いま利用中の予約が見つかりませんでした（もう終わっているかもしれません）。")
            return
        ok = await self.end_booking(running[0]["id"])
        if not ok:
            await self.reply(interaction, "うまく終われませんでした。予約サイトの予約ページの「いま終わる」を押してください"
                                          "（時間になれば、自動で消えます）。")
            return
        await self.reply(interaction, "終わりにしました。1分ほどで VRChat が閉じて、PC からログイン情報が消えます。"
                                      "ありがとうございました！")
        await self.notify_ops(f"<@{uid}> さんが「いま終わる」を押しました（{running[0]['host']}）<#{interaction.channel_id}>")

    async def end_booking(self, booking_id: int) -> bool:
        """同じ PC で動いているサイトに、予約をいま終わりにするよう頼む。"""
        if not (self.site_api and self.api_secret):
            return False

        def call() -> bool:
            req = urllib.request.Request(
                f"{self.site_api}/api/bot/bookings/{booking_id}/end", data=b"", method="POST",
                headers={"X-Geaspec-Signature": invite.bot_signature(self.api_secret, booking_id)})
            try:
                with urllib.request.urlopen(req, timeout=15) as res:
                    return bool(json.loads(res.read() or b"{}").get("ok"))
            except (OSError, ValueError):
                log.exception("「いま終わる」をサイトに頼めませんでした")
                return False

        return await asyncio.to_thread(call)

    # 貸す人へのお知らせ (承認された・貸し終わった)
    async def send_lender_notices(self) -> int:
        if self.site_db is None:
            return 0
        sent = 0
        for host in booking_notice.load_lender_hosts(self.site_db):
            key = f"host_ok:{host['id']}"
            if not host["approved"] or self.store.get_setting(key):
                continue
            self.store.set_setting(key, "1")
            lender = self.lender_for(host)
            thread = await self._existing_thread(lender) if lender else None
            if thread is None:
                continue
            await thread.send(
                f"<@{lender['user_id']}> PC「{host['name']}」が承認されました。貸し出せるようになりました！\n"
                "Geaspec アプリのカレンダーで、貸せる時間のマスをクリックして「保存」してください。保存すると、予約サイトの空き枠に出ます。")
            sent += 1
        # 貸し終わった予約 (ボットが止まっていた間の分も、2 日前まではさかのぼる)
        for b in booking_notice.load_bookings(self.site_db, self.clock() - dt.timedelta(days=2), finished=True):
            key = f"lent_done:{b['id']}"
            if not b["owner_thread"] or self.store.get_setting(key):
                continue
            self.store.set_setting(key, "1")
            lender = self.lender_for(b)
            thread = await self._existing_thread(lender) if lender else None
            if thread is None:
                continue
            uid = lender["user_id"]
            await thread.send(f"<@{uid}> 貸し出しが終わりました（{booking_notice.when_text(b['start'], b['end'])}）。"
                              "ありがとうございました！\n\n" + WIPE_CHECK_TEXT, view=wipe_check_view())
            if self.store.start_lender_survey(uid, self.clock()):
                text, view = lender_survey_message(uid, {})
                await thread.send(text, view=view)
            sent += 1
        return sent

    # テスト本体
    async def on_wipe_check(self, interaction: discord.Interaction, tester: dict, ok: bool) -> None:
        """貸した人が、貸し出しのあとにデータが消えたかを確かめた結果。押すたびに記録して運営に知らせる。"""
        key = f"wipe_{'ok' if ok else 'left'}:{tester['user_id']}"
        count = int(self.store.get_setting(key, "0") or 0) + 1
        self.store.set_setting(key, str(count))
        if ok:
            await self.reply(interaction, f"ありがとうございます！記録しました（{count}回目）。次に貸したあとも、同じように確かめてください。")
            await self.notify_ops(f"<@{tester['user_id']}> さんの PC: 貸し出しのあと、データが消えていることを確かめました（{count}回目）")
        else:
            await interaction.response.send_message(
                f"{self.ops_role_mention()} 教えてくれてありがとうございます。運営がすぐ確認します。"
                "何が残っていたか（写真・VRChat のログイン・ほかのファイルなど）を、このスレッドに書いてください。"
                "画面の写真もあると助かります（借りた人の名前などが写っていたら隠してください）。")
            await self.notify_ops(f"【要確認】<@{tester['user_id']}> さんの PC で、貸し出しのあとにデータが残っていました "
                                  f"<#{interaction.channel_id}>")
        # はじめて確かめてくれたときに、貸す側のアンケートを 1 回だけ送る
        if self.store.start_lender_survey(tester["user_id"], self.clock()):
            thread = interaction.channel or await self._existing_thread(tester)
            if thread is not None:
                text, view = lender_survey_message(tester["user_id"], {})
                await thread.send(text, view=view)

    async def on_lender_action(self, interaction: discord.Interaction, tester: dict, action: str, args: list[str]) -> None:
        uid = tester["user_id"]
        row = self.store.lender_survey(uid)
        if row is None or row["done_at"]:
            await self.reply(interaction, "このアンケートはもう終わっています。ありがとうございました。")
            return
        if action == "lc":
            await interaction.response.send_modal(LenderCommentModal(self))
            return
        if action == "ls":
            await self.finish_lender(interaction, tester, "", from_modal=False)
            return
        if not args:
            await self.reply(interaction, "その答えは受け付けられませんでした。")
            return
        key = args[0]
        value: str | list[str] = args[1] if len(args) > 1 else list((interaction.data or {}).get("values") or [])
        try:
            self.store.answer_lender(uid, key, value)
        except ValueError:
            await self.reply(interaction, "その答えは受け付けられませんでした。")
            return
        text, view = lender_survey_message(uid, json.loads(self.store.lender_survey(uid)["answers"]))
        await interaction.response.edit_message(content=text, view=view)

    async def on_lender_comment(self, interaction: discord.Interaction, modal: LenderCommentModal) -> None:
        tester = self.store.tester_by_thread(interaction.channel_id)
        row = self.store.lender_survey(tester["user_id"]) if tester else None
        if row is None or row["done_at"]:
            await self.reply(interaction, "このアンケートはもう終わっています。ありがとうございました。")
            return
        await self.finish_lender(interaction, tester, modal.comment.value, from_modal=True)

    async def finish_lender(self, interaction: discord.Interaction, tester: dict, comment: str, *, from_modal: bool) -> None:
        uid = tester["user_id"]
        self.store.finish_lender(uid, self.clock(), comment)
        text = ("**ありがとうございました！ 記録しました。**\n"
                "ほかにも気づいたことがあれば、いつでもこのスレッドに書いてください（運営に届きます）。")
        if from_modal:
            await interaction.response.send_message(text)
        else:
            await interaction.response.edit_message(content=text, view=None)
        a = json.loads(self.store.lender_survey(uid)["answers"])
        picks = [f"{q['text'].split('？')[0]}: " +
                 ("・".join(option_label(q["key"], x) for x in a[q["key"]]) if isinstance(a[q["key"]], list)
                  else option_label(q["key"], a[q["key"]]))
                 for q in LENDER_QUESTIONS if q["key"] in a]
        lines = [f"<@{uid}> さんが貸した人のアンケートに答えました", *picks]
        if comment.strip():
            lines.append(f"ひとこと: {comment.strip()}")
        await self.notify_ops("\n".join(lines))

    async def on_session_action(self, interaction: discord.Interaction, action: str, args: list[str]) -> None:
        tester = self.store.tester_by_thread(interaction.channel_id)
        if tester is None:
            await self.reply(interaction, "このボタンは、テスター専用のスレッドで使ってください。")
            return
        if interaction.user.id != tester["user_id"] and not self.is_ops(interaction.user):
            await self.reply(interaction, "このスレッドのテスターだけが押せます。")
            return
        if action == "start":
            await self.on_start(interaction, tester)
            return
        if action == "pg":
            await self.on_page(interaction, tester, args)
            return
        if action == "noop":
            await interaction.response.defer()
            return
        if action == "link":
            await self.reply(interaction, f"あなた専用のリンクです（{invite.LINK_DAYS}日有効・ほかの人には渡さないでください）\n"
                                          f"{self.site_link(tester)}")
            return
        if action in ("booked", "bookhelp", "lenderapp"):
            await self.on_tester_step(interaction, tester, action)
            return
        if action == "wipe":
            await self.on_wipe_check(interaction, tester, args[:1] == ["ok"])
            return
        if action == "f":
            await self.on_followup(interaction, tester, args)
            return
        if action in ("l", "lc", "ls"):
            await self.on_lender_action(interaction, tester, action, args)
            return
        if action == "endnow":
            await self.on_end_now(interaction, tester, args[:1] == ["yes"])
            return
        session = self.store.open_session(interaction.channel_id)
        if session is None:
            await self.reply(interaction, "このテストはもう終わっています。もう一度試すときは「テストを始める」を押してください。")
            return
        handler = {
            "entered": self.on_entered, "help": self.on_help, "giveup": self.on_giveup, "done": self.on_done,
            "m": self.on_mission, "perf": self.on_perf_button, "a": self.on_answer, "comment": self.on_comment_button,
            "skip": self.on_skip, "submit": self.on_submit,
        }.get(action)
        if handler:
            await handler(interaction, tester, session, args)

    async def on_tester_step(self, interaction: discord.Interaction, tester: dict, action: str) -> None:
        uid = tester["user_id"]
        if action == "booked":
            booking = None
            if self.site_db is not None:  # サイトに本当に予約があるか確かめる (押し間違い・予約し忘れを防ぐ)
                mine = [b for b in booking_notice.load_bookings(self.site_db, self.clock())
                        if b["status"] in ("confirmed", "active") and (t := self.tester_for(b)) and t["user_id"] == uid]
                if not mine:
                    await self.reply(interaction, "予約が見つかりませんでした。予約サイトの「予約一覧」に出ているか確かめてください。"
                                                  "出ているのにこの表示になるときは「予約で迷った」を押してください。")
                    return
                booking = mine[0]
            if self.store.mark_booked(uid, self.clock()):
                await self.notify_ops(f"<@{uid}> さんが予約できました <#{interaction.channel_id}>")
            await self.reply(interaction, "記録しました！ 下のチェックリストに沿って進めてください。")
            await self.begin_checklist(tester, booking)
        elif action == "bookhelp":
            count = self.store.add_booking_help(uid)
            await interaction.response.send_message(
                f"{self.ops_role_mention()} <@{uid}> さんが予約で迷っています（{count}回目）。\n"
                "どの画面で止まったか、このスレッドに書いてもらえると助かります（スクショも歓迎です）。")
            await self.notify_ops(f"<@{uid}> さんが予約で迷っています <#{interaction.channel_id}>")
        else:
            self.store.mark_lender_app(uid, self.clock())
            # 押すたびに運営を呼ぶ (前に押したときに見落とされていても、もう一度届く)
            await self.notify_ops(f"{self.ops_role_mention()} <@{uid}> さんが貸し手のアプリを入れました。"
                                  f"管理画面で PC を承認してください: {self.site_url}/admin <#{interaction.channel_id}>")
            await self.reply(interaction, "運営に知らせました！ 承認されたら、ここでお知らせします。\n"
                                          "まだなら、アプリに予約サイトと同じメールアドレスとパスワードでログインしてください"
                                          "（ログインすると、運営の画面に PC が出ます）。")

    async def on_start(self, interaction: discord.Interaction, tester: dict) -> None:
        if tester["status"] != "ready":
            await self.reply(interaction, "まだ申し込みが終わっていません。申し込みチャンネルの「テスターに申し込む」から申し込んでください。")
            return
        if self.store.open_session(interaction.channel_id) is None and not self.store.get_setting(f"cycle:{tester['user_id']}"):
            self.store.set_setting(f"cycle:{tester['user_id']}", self._now_iso())
        # Virtual Desktop の一覧には、予約した PC の名前 (Windows のコンピューター名) が出る
        pc = self.booking_info(tester["user_id"]).get("host") or self.store.get_setting("pc_name", "LG")
        session, created = self.store.start_session(tester["user_id"], interaction.channel_id, self.clock(), pc=pc)
        if not created:
            await self.reply(interaction, "テストはもう始まっています。上のメッセージのボタンを使ってください。")
            return
        await interaction.response.send_message(
            "**計測をはじめました**\n"
            "1. Quest で Virtual Desktop を開く\n"
            f"2. パソコンの一覧から「{pc}」を選ぶ\n"
            "3. VRChat が自動で立ち上がったら、**メールアドレス（またはユーザー名）とパスワード** で自分のアカウントにログイン（Steam のボタンは押さない）\n"
            "　※ もし **自分ではない人のアカウントでログインされていたら**、何も触らずに「困った」を押してください（テストは中止します）\n"
            "パソコンの画面は「Geaspec で貸し出し中」の幕で隠れていますが、それで正常です。\n\n"
            "ワールドに入れたら「ワールドに入れた」を押してください。",
            view=make_view(button("✅ ワールドに入れた", "gs:entered", discord.ButtonStyle.success),
                           button("🆘 困った", "gs:help", discord.ButtonStyle.danger),
                           button("つながらなかった", "gs:giveup")))
        await self.notify_ops(f"<@{tester['user_id']}> さんがテストを始めました <#{interaction.channel_id}>")
        await self.show_checklist(tester, interaction.channel)

    async def on_entered(self, interaction, tester, session, args) -> None:
        if session["status"] != "connecting":
            await self.reply(interaction, "もう記録してあります。")
            return
        seconds = self.store.mark_entered(session["id"], self.clock())
        await interaction.response.edit_message(
            content=f"ワールドに入れました（Virtual Desktop を開いてから **{format_duration(seconds)}**）", view=None)
        worlds = "」「".join(self.worlds())
        await interaction.followup.send(
            "楽しんでください！ 遊びながら、できたらお願いします。\n\n"
            "**FPS と Ping の写真**\n"
            "1. VRChat のメニュー（Launch Pad）の歯車（設定）→ いちばん下の「デバッグ」→「FPS と Ping の表示」をオン。"
            f"画像: {self.site_url}/guide\n"
            f"2. 「{worlds}」に行き、それぞれ**30秒ほど歩き回りながら**、FPS と Ping が写るように Quest のスクリーンショットを撮る"
            "（右手の Meta ボタンを押したまま、トリガーを引く）\n"
            "3. 撮った写真を、**このスレッドに貼る**（テストが終わってから、スマホからでも大丈夫です）。"
            "撮れなかったら、見えた数字をこのスレッドに書くだけでも大丈夫です\n"
            "4. VRChat のカメラで **自撮り** を 1枚撮る（終わったあと、手元に届くかを確かめます。好きなポーズでどうぞ）\n\n"
            "左手首に残り時間が出ます。遊び終わったら「遊び終わった」を押してください。",
            view=play_view(json.loads(session["missions"])))
        await self.notify_ops(f"<@{tester['user_id']}> さんがワールドに入りました（{format_duration(seconds)}）")
        await self.show_checklist(tester, interaction.channel)

    async def on_mission(self, interaction, tester, session, args) -> None:
        if not args or args[0] not in MISSIONS:
            return
        missions = self.store.toggle_mission(session["id"], args[0])
        await interaction.response.edit_message(view=play_view(missions))
        await self.show_checklist(tester, interaction.channel, move=False)

    async def on_perf_button(self, interaction, tester, session, args) -> None:
        await interaction.response.send_modal(PerfModal(self, self.worlds()))

    async def on_perf(self, interaction: discord.Interaction, modal: PerfModal) -> None:
        session = self.store.open_session(interaction.channel_id)
        if session is None:
            await self.reply(interaction, "このテストはもう終わっています。")
            return
        fps = {w: parse_int(item.value) for w, item in zip(modal.worlds, modal.fps_inputs)}
        ping = parse_int(modal.ping.value)
        self.store.set_performance(session["id"], fps_by_world=fps, ping_ms=ping)
        shown = lambda v, unit="": "—" if v is None else f"{v}{unit}"  # noqa: E731
        fps_text = "・".join(f"{w} {shown(v)}" for w, v in fps.items())
        await self.reply(interaction, f"記録しました（FPS {fps_text} ／ Ping {shown(ping, 'ms')}）。"
                                      "ありがとうございます！")
        tester = self.store.tester(session["user_id"])
        if tester:
            await self.show_checklist(tester, interaction.channel)

    async def on_help(self, interaction, tester, session, args) -> None:
        step = "つなぐところ" if session["status"] == "connecting" else "遊んでいる途中"
        if session["status"] == "survey":
            step = "アンケート"
        count = self.store.add_help(session["id"], step, self.clock())
        await interaction.response.send_message(
            f"{self.ops_role_mention()} <@{tester['user_id']}> さんが困っています（{step}）。\n"
            "運営を呼びました。何が起きたか、このスレッドに書いてもらえると助かります。")
        await self.notify_ops(f"<@{tester['user_id']}> さんが困っています（{step}・{count}回目） <#{interaction.channel_id}>")

    async def on_giveup(self, interaction, tester, session, args) -> None:
        if session["status"] != "connecting":
            await self.reply(interaction, "もう記録してあります。")
            return
        self.store.finish_play(session["id"], self.clock(), connected=False)
        await interaction.response.edit_message(content="つながらなかったことを記録しました。", view=None)
        await interaction.followup.send(
            "ごめんなさい！運営から連絡します。どこで止まったか教えてもらえると、とても助かります。",
            view=make_view(button("✍ 書く", "gs:comment", discord.ButtonStyle.primary),
                           button("スキップして終わる", "gs:skip")))
        await self.notify_ops(f"<@{tester['user_id']}> さんがつながりませんでした <#{interaction.channel_id}>")

    async def on_done(self, interaction, tester, session, args) -> None:
        if session["status"] != "playing":
            await self.reply(interaction, "もう記録してあります。")
            return
        self.store.finish_play(session["id"], self.clock(), connected=True)
        await interaction.response.edit_message(
            content="遊び終わりました。おつかれさまでした！\n"
                    "FPS と Ping の写真がまだなら、あとでこのスレッドに貼ってください。\n\n"
                    "予約の時間がまだ残っていたら、「いま終わる」を押してください。すぐに VRChat を閉じて、"
                    "PC からあなたのログイン情報を消します（押さなくても、時間になれば自動で消えます）。",
            view=make_view(button("⏹ いま終わる（PC を返す）", "gs:endnow", discord.ButtonStyle.danger)))
        text, view = survey_message(json.loads(session["answers"]))
        await interaction.followup.send(text, view=view)

    async def on_answer(self, interaction, tester, session, args) -> None:
        if session["status"] != "survey" or not args:
            await self.reply(interaction, "このアンケートはもう終わっています。")
            return
        key = args[0]
        value: str | list[str] = args[1] if len(args) > 1 else list(interaction.data.get("values") or [])
        try:
            self.store.answer(session["id"], key, value)
        except ValueError:
            await self.reply(interaction, "その答えは受け付けられませんでした。")
            return
        answers = json.loads(self.store.session(session["id"])["answers"])
        text, view = survey_message(answers)
        await interaction.response.edit_message(content=text, view=view)
        if TesterStore.next_question(answers) is None and session["connected"]:
            await self.show_checklist(tester, interaction.channel)  # アンケートが終わった。送信ボタンを出す

    async def on_comment_button(self, interaction, tester, session, args) -> None:
        if session["status"] != "survey":
            await self.reply(interaction, "アンケートのあとで書けます。")
            return
        await interaction.response.send_modal(CommentModal(self, bool(session["connected"])))

    async def on_comment(self, interaction: discord.Interaction, modal: CommentModal) -> None:
        session = self.store.open_session(interaction.channel_id)
        if session is None or session["status"] != "survey":
            await self.reply(interaction, "このテストはもう終わっています。")
            return
        tester = self.store.tester(session["user_id"])
        if session["connected"]:
            # つながったテストは、チェックリストの「送信」で終える。ここでは記録だけ
            self.store.save_comment(session["id"], stuck=modal.stuck.value, comment=modal.comment.value)
            await interaction.response.send_message("ありがとうございます！ 記録しました。"
                                                    "最後に、チェックリストの「送信してテストを終える」を押してください。",
                                                    ephemeral=True)
            if tester:
                await self.show_checklist(tester, interaction.channel)
            return
        self.store.complete(session["id"], self.clock(), stuck=modal.stuck.value, comment=modal.comment.value)
        await interaction.response.send_message(self.thanks_text(session["id"], tester), view=self.again_view())
        await self.report_done(session["id"])

    async def on_skip(self, interaction, tester, session, args) -> None:
        if session["status"] != "survey":
            await self.reply(interaction, "アンケートのあとで押せます。")
            return
        self.store.complete(session["id"], self.clock())
        await interaction.response.edit_message(content=self.thanks_text(session["id"], tester), view=self.again_view())
        await self.report_done(session["id"])

    def thanks_text(self, session_id: int, tester: dict | None = None) -> str:
        return ("**ありがとうございました！ 記録しました。**\n"
                "時間になると自動で終わり、ログイン情報はパソコンから消えます。\n\n"
                "FPS と Ping の写真がまだなら、このスレッドに貼ってください。\n\n"
                "VRChat のカメラで撮った写真（自撮りなど）は、時間が終わると予約サイトの予約ページからダウンロードできます（7日間）。\n"
                "よければ 15分ほどお話も聞かせてください（運営から連絡します）。\n"
                + NOTES_HINT)

    @staticmethod
    def again_view() -> discord.ui.View:
        return make_view(button("▶ もう一度テストする", "gs:start"))

    async def report_done(self, session_id: int, who: str = "") -> None:
        s = self.store.session(session_id)
        who = who or (f"<@{s['user_id']}> さん" if s["user_id"] > 0 else "予約サイトから参加した人")
        a = json.loads(s["answers"])
        enter = "—"
        if s["entered_at"]:
            enter = format_duration((dt.datetime.fromisoformat(s["entered_at"])
                                     - dt.datetime.fromisoformat(s["opened_at"])).total_seconds())
        picks = [f"{q['text'].split('？')[0]}: {option_label(q['key'], a[q['key']])}"
                 for q in QUESTIONS if q["key"] in ("quality", "again", "nps", "price") and q["key"] in a]
        lines = [f"{who}のテストが完了（{'つながった' if s['connected'] else 'つながらなかった'}・"
                 f"入るまで {enter}・困った {s['help_count']}回）", *picks]
        if s["stuck"]:
            lines.append(f"迷ったところ: {s['stuck']}")
        if s["comment"]:
            lines.append(f"ひとこと: {s['comment']}")
        await self.notify_ops("\n".join(lines))

    # 予約のお知らせ (予約サイトのデータベースを 1 分ごとに見る)
    async def booking_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.send_booking_notices()
                await self.send_lender_notices()
                await self.send_web_events()
            except Exception:  # noqa: BLE001 - 次の回にやり直す
                log.exception("予約のお知らせを送れませんでした")
            await asyncio.sleep(BOOKING_CHECK_SECONDS)

    async def send_web_events(self) -> int:
        """予約ページで進めたテストのお知らせ (始めた・困った・終わった) を運営チャンネルに伝える。"""
        sent = 0
        for e in self.store.pending_web_events():
            if e["kind"] == "done" and e["session_id"]:
                await self.report_done(e["session_id"], who=e["text"].split(" のテスト")[0])
            else:
                await self.notify_ops((f"{self.ops_role_mention()} " if e["urgent"] else "") + e["text"])
            self.store.mark_web_event_sent(e["id"], self.clock())
            sent += 1
        return sent

    async def send_booking_notices(self) -> int:
        if self.site_db is None:
            return 0
        now = self.clock()
        bookings = booking_notice.load_bookings(self.site_db, now)
        sent = 0
        for kind, b in booking_notice.due_notices(bookings, now, self.store.notice_sent):
            if kind == "soon" and self.store.notice_sent(b["id"], "soon"):
                continue  # 予約と同時に「すぐ始められます」を送った
            tester = self.tester_for(b)
            thread = await self._existing_thread(tester) if tester else None
            when = booking_notice.when_text(b["start"], b["end"])
            who = booking_notice.who(b) + (f" <@{tester['user_id']}>" if tester else "")
            where = f" {thread.mention}" if thread else ""
            if kind == "booked":
                await self.notify_ops(f"予約が入りました: **{when}**（{b['host']}）{who}{where}\n"
                                      f"→ {b['host']} の Virtual Desktop Streamer に `{b['meta']}` が入っているか確かめてください")
                if tester:
                    self.store.mark_booked(tester["user_id"], now)
                starts_now = b["start"] <= now + dt.timedelta(minutes=booking_notice.SOON_MINUTES)
                if starts_now and thread:  # 本人には「すぐ始められます」と書くので、「まもなく」は送らない
                    self.store.mark_notice(b["id"], "soon", now)
                # 本人が先に「予約した」を押していれば、もう案内してある
                if thread and self.store.get_setting(f"checklist_booking:{tester['user_id']}") != str(b["id"]):
                    if starts_now:  # 「今すぐ入る」や、まもなく始まる予約: 待たずにそのまま始めてもらう
                        guide = ("すぐに始められます。予約ページの「テストのチェックリスト」で「テストを始める」を押して、"
                                 "Quest で Virtual Desktop を開いてください。")
                    else:
                        guide = (f"開始 {booking_notice.SOON_MINUTES} 分前に、ここでお知らせします。"
                                 "当日は予約ページの「テストのチェックリスト」に沿って進めてください。")
                    await thread.send(f"<@{tester['user_id']}> 予約を受け付けました: **{when}**\n{guide}\n"
                                      f"予約ページ: {self.booking_page(b['id'])}")
                    self.remember_booking(tester["user_id"], b)
                lender = self.lender_for(b)
                lender_thread = await self._existing_thread(lender) if lender else None
                if lender_thread and (not tester or lender["user_id"] != tester["user_id"]):
                    await lender_thread.send(
                        f"<@{lender['user_id']}> あなたの PC「{b['host']}」に予約が入りました: **{when}**\n"
                        "時間になると Geaspec アプリが自動で準備します。PC の電源を入れて、アプリを動かしたままにしてください。")
            elif kind == "soon":
                lines = [f"{booking_notice.SOON_MINUTES} 分後に予約があります: **{when}**（{b['host']}）{who}{where}",
                         f"確認: {b['host']} の電源が入っている／Geaspec アプリが動いている／{REMOTE_SETTING}にチェック"
                         f"／VD Streamer に `{b['meta']}` が入っている"]
                # 前の人が使っている最中は、アプリがサイトに問い合わせないので「連絡なし」とは見なさない
                in_use = any(o["host"] == b["host"] and o["status"] == "active" for o in bookings)
                if not in_use and not booking_notice.agent_running(b, now):
                    lines.append(f"【注意】{self.ops_role_mention()} {b['host']} の Geaspec アプリから、しばらく連絡がありません。"
                                 "このままだとテスターがつなげません。PC の電源とアプリを確かめてください。")
                await self.notify_ops("\n".join(lines))
                if thread:
                    await thread.send(
                        f"<@{tester['user_id']}> まもなく始まります（**{when}**）\n"
                        "予約ページの「テストを始める」を押して、Quest で Virtual Desktop を開いてください。\n"
                        f"予約ページ: {self.booking_page(b['id'])}")
            else:
                await self.notify_ops(f"予約がキャンセルされました: {when}（{b['host']}）{who}{where}")
            self.store.mark_notice(b["id"], kind, now)
            sent += 1
        return sent

    async def on_followup(self, interaction: discord.Interaction, tester: dict, args: list[str]) -> None:
        session = self.store.latest_session(interaction.channel_id)
        if session is None or session["status"] != "done" or len(args) < 2:
            await self.reply(interaction, "この質問はもう終わっています。")
            return
        try:
            nxt = self.store.answer_followup(session["id"], args[0], args[1])
        except ValueError:
            await self.reply(interaction, "その答えは受け付けられませんでした。")
            return
        if nxt is None:
            await interaction.response.edit_message(content="ありがとうございました！ 記録しました。", view=None)
            await self.notify_ops(f"<@{tester['user_id']}> さんが 3日後の質問に答えました <#{interaction.channel_id}>")
            return
        text, view = followup_message(tester["user_id"], json.loads(self.store.session(session["id"])["answers"]))
        await interaction.response.edit_message(content=text, view=view)

    # スクショ
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        if (message.content or "").strip():
            await self.save_note(message)
        if not message.attachments:
            return
        # テストが終わってから (スマホから) 貼ることが多いので、終わったテストにも付ける
        session = self.store.open_session(message.channel.id) or self.store.latest_session(message.channel.id)
        if session is None or session["user_id"] != message.author.id:
            return
        had_photo = bool(json.loads(self.store.session(session["id"])["screenshots"]))
        saved = 0
        for att in message.attachments:
            if (att.content_type or "").split(";")[0] not in IMAGE_TYPES:
                continue
            if len(json.loads(self.store.session(session["id"])["screenshots"])) >= MAX_SHOTS_PER_SESSION:
                break
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(att.filename).suffix.lower() or ".png"
            path = self.shots_dir / f"test{session['id']:03d}-{att.id}{suffix}"
            await att.save(path)
            self.store.add_screenshot(session["id"], str(path))
            saved += 1
        if saved:
            try:
                await message.add_reaction("📸")
            except discord.HTTPException:
                pass
            tester = self.store.tester(session["user_id"])
            if not had_photo and session["status"] != "done" and tester:
                await self.show_checklist(tester, message.channel)  # 「4. 写真」に ✅ を付ける


    async def save_note(self, message: discord.Message) -> None:
        """テスターが自分のスレッドに書いたことを意見として記録し、運営に回す。"""
        tester = self.store.tester_by_thread(message.channel.id)
        if tester is None or tester["user_id"] != message.author.id:
            return
        text = message.content.strip()
        self.store.add_note(tester["user_id"], message.channel.id, text, self.clock())
        role = "貸す人" if tester.get("grp") == "C" else "借りる人"
        quoted = "\n".join("> " + line for line in text[:500].splitlines())
        await self.notify_ops(f"💬 <@{tester['user_id']}> さん（{role}）がスレッドに書きました {message.channel.mention}\n{quoted}")
        try:
            await message.add_reaction("📝")
        except discord.HTTPException:
            pass


async def _run(token: str, store: TesterStore, shots_dir: Path, site_url: str, say: Callable[[str], None],
               site_password: str = "", invite_secret: str = "", site_api: str = "", api_secret: str = "") -> None:
    message_content = True
    while True:
        bot = TesterBot(store, shots_dir, site_url, site_password=site_password, invite_secret=invite_secret,
                        site_db=shots_dir.parent / "geaspec.db", message_content=message_content, say=say,
                        site_api=site_api, api_secret=api_secret)
        try:
            await bot.start(token)
            return
        except discord.PrivilegedIntentsRequired:
            if not message_content:
                return
            say("Discord ボット: 「Message Content Intent」がオフなので、スクショの記録なしで動かします。"
                "Discord の開発者ページの Bot 画面でオンにすると、スクショも記録できます。")
            message_content = False
        except discord.LoginFailure:
            say("Discord ボット: トークンが正しくありません。設定ファイルの discord_token を確かめてください。")
            return
        except Exception as exc:  # noqa: BLE001 - インターネットが切れているときなど。少し待ってやり直す
            say(f"Discord ボット: Discord につながりません。1分後にもう一度試します（{exc}）")
            await asyncio.sleep(60)
        finally:
            if not bot.is_closed():
                await bot.close()


def start_tester_bot(token: str, data: Path, site_url: str, site_password: str = "", invite_secret: str = "",
                     say: Callable[[str], None] = print, site_api: str = "", api_secret: str = "") -> threading.Thread:
    """予約サイトと同じ黒い画面の中で、別のスレッドとしてボットを動かす。"""
    data.mkdir(parents=True, exist_ok=True)
    store = TesterStore(data / "tester_bot.db")

    def target() -> None:
        try:
            asyncio.run(_run(token, store, data / "tester_screenshots", site_url, say, site_password, invite_secret,
                             site_api, api_secret))
        except Exception as exc:  # noqa: BLE001 - ボットが止まってもサイトは動かし続ける
            say(f"Discord ボットが止まりました: {exc}")

    thread = threading.Thread(target=target, name="geaspec-discord-bot", daemon=True)
    thread.start()
    return thread
