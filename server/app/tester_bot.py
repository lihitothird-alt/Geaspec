"""テスター用 Discord ボット。

テスターが Discord のボタンだけで、申し込み → テスト開始 → 計測 → アンケートまで自分で進められるようにする。
記録は tester_store.TesterStore にたまり、運営は /geaspec-results で集計と CSV を受け取る。

- はじめて動いたとき、「運営」ロールと 2 つのチャンネルを自動で作る (/geaspec-setup でやり直せる)
- 申し込みチャンネル: 「申し込む」ボタンを置く。テスターはボタンを押すだけ
- テスターごとのスレッド: 申し込むと、本人と運営だけが見られるスレッドができる。テストはここで進める
- 運営チャンネル: 申し込み・テスト開始・「困った」・結果のお知らせが届く

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
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands

from . import booking_notice, feedback, invite, version
from .tester_store import (FOLLOWUPS, GROUPS, MISSIONS, QUESTIONS, TesterStore, format_duration, option_label,
                           summary_lines)

log = logging.getLogger(__name__)

IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
OPS_ROLE_NAME = "運営"
OPS_CHANNEL_NAME = "運営"
PANEL_CHANNEL_NAME = "🙋テスター申し込み"
NEWS_NAME = "更新情報"  # この言葉を名前に含むチャンネル・スレッド・フォーラムに、新しい版のお知らせを書く
NEWS_CHANNEL_NAME = "📢更新情報"  # どこにもなければ、この名前で作る
DEFAULT_HOURS = "平日は19時以降、土日は終日"
# FPS を測るワールド (/geaspec-setup worlds: で変えられる。最大 3 つ)
DEFAULT_WORLDS = "日本語話者向け集会場, Japan Shrine, Nagisa"
MAX_WORLDS = 3
DEFAULT_AGENT_URL = "https://github.com/lihitothird-alt/Geaspec/releases/latest"
FOLLOWUP_CHECK_SECONDS = 30 * 60
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
    return f"""Quest から遠くのゲーミング PC を借りて、PC 版 VRChat を遊ぶテストです。
お金もうけではない、このコミュニティ限定の実験です。参加は無料です。

**流れ**
1. 下の「テスターに申し込む」を押す
2. あなた専用のスレッドができます。運営の準備ができたら、そこでお知らせします
3. スレッドに届くあなた専用のリンクから、予約サイトで予約する
4. 当日、スレッドの「テストを始める」を押して、Virtual Desktop でつなぐ
5. 遊び終わったら、ボタンで答えるアンケート（3分）と、評価フォーム（5分）

**申し込む前に**
・Meta ユーザー名: スマホの Meta Horizon アプリでプロフィールを開くと見られます（表示名ではなく、ユーザー名のほう）。Virtual Desktop を持っている人は、VD で使っているものと同じです
・当日は PC 版の VRChat に、自分のアカウントでログインします。VRChat のメールアドレス（またはユーザー名）とパスワードを用意してください。2段階認証をしている人は、確認コードを受け取るスマホも手元に
{f"・画像つきの説明: {site_url}/guide" + chr(10) if site_url else ""}
**テストができる時間: {hours}**
貸し出す PC は運営がふだんも使っているので、それ以外の時間はテストできないことが多いです。
この時間の外では、ボタンを押しても反応しないことがあります（そのときは時間内にもう一度押してください）。

あなたのパソコンやルーターの設定は何もいりません。
記録するのは、つながるまでの時間・「困った」の回数・FPS などの数字・アンケートの答えです。名前を出して公開することはありません。"""


def prep_text(site_url: str = "") -> str:
    """申し込んだ人に、当日までにやっておくことをまとめて伝える。"""
    guide = f"（画像つきの説明: {site_url}/guide#fps）" if site_url else ""
    return (
        "**申し込んだら、やっておくこと**\n"
        "1. **VPN を切る**: Quest・スマホ・パソコン・家のルーターで VPN を使っている人は、テストの間はオフにしてください。"
        "つながらない原因になります\n"
        "2. **VRChat のログイン情報を用意する**: メールアドレス（またはユーザー名）とパスワード。"
        "2段階認証をしている人は、確認コードを受け取るスマホも手元に\n"
        "3. **当日、VRChat に入ったら FPS と Ping の表示をオンにする**: Launch Pad の歯車（設定）→ いちばん下の"
        f"「デバッグ」→「FPS と Ping の表示」{guide}。貸し出しの PC は毎回設定が消えるので、テストのたびにオンにしてください\n"
        "4. **（自分の PC を貸す人だけ）** 自分の PC の Virtual Desktop Streamer で「Allow remote connections」"
        "（外からの接続を許可）にチェックを入れる。借りるだけの人は何もしなくて大丈夫です"
    )


def survey_message(answers: dict) -> tuple[str, discord.ui.View]:
    """次の質問 (全部答えたら、ひとこと欄への案内) を作る。"""
    key = TesterStore.next_question(answers)
    if key is None:
        text = ("**最後に（任意）**\nいちばん迷ったところや、ひとこと感想があれば書いてください。\n"
                "なければ「スキップして終わる」で完了です。")
        return text, make_view(button("✍ 書く", "gs:comment", discord.ButtonStyle.primary),
                               button("スキップして終わる", "gs:skip"))
    index = [q["key"] for q in QUESTIONS].index(key)
    q = QUESTIONS[index]
    text = f"**アンケート {index + 1} / {len(QUESTIONS)}**\n{q['text']}"
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
    head = f"<@{user_id}> テストから3日たちました。最後に3つだけ教えてください\n" if index == 0 else ""
    text = f"{head}**{index + 1} / {len(FOLLOWUPS)}** {q['text']}"
    return text, make_view(*[button(label, f"gs:f:{q['key']}:{value}", row=i // 5)
                             for i, (value, label) in enumerate(q["options"])])


def play_view(missions: dict) -> discord.ui.View:
    items = [button(("✅ " if missions.get(k) else "⬜ ") + label, f"gs:m:{k}", row=0) for k, label in MISSIONS.items()]
    items += [
        button("📊 数字を入れる", "gs:perf", row=0),
        button("🆘 困った", "gs:help", discord.ButtonStyle.danger, row=1),
        button("🏁 遊び終わった", "gs:done", discord.ButtonStyle.success, row=1),
    ]
    return make_view(*items)


def welcome_view(tester: dict) -> discord.ui.View:
    grp = discord.ui.Select(custom_id="gs:group", placeholder="あなたに近いのは？", options=[
        discord.SelectOption(label=label, value=key, default=tester.get("grp") == key) for key, label in GROUPS.items()])
    vd = discord.ui.Select(custom_id="gs:vd", placeholder="Virtual Desktop を持っていますか？", options=[
        discord.SelectOption(label="持っている", value="1", default=tester.get("has_vd") == 1),
        discord.SelectOption(label="持っていない", value="0", default=tester.get("has_vd") == 0)])
    return make_view(grp, vd)


class ApplyModal(discord.ui.Modal):
    def __init__(self, bot: "TesterBot", tester: dict | None) -> None:
        super().__init__(title="テスターに申し込む", timeout=1800)
        self.bot = bot
        t = tester or {}
        self.vrchat = discord.ui.TextInput(label="VRChat の表示名（任意）", max_length=60, required=False,
                                           default=t.get("vrchat") or None)
        self.meta = discord.ui.TextInput(
            label="Meta ユーザー名（表示名ではありません）", max_length=60, default=t.get("meta") or None,
            placeholder="Meta Horizon アプリのプロフィールで見られます。VD を持っている人は VD と同じ")
        self.gear = discord.ui.TextInput(label="使っている機材", max_length=100, default=t.get("gear") or None,
                                         placeholder="例: Quest 3 だけ ／ Quest 3 + RTX 3060")
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
                 site_db: Path | None = None, message_content: bool = True,
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
        self.clock = clock
        self.say = say or log.info
        self.tree = app_commands.CommandTree(self)
        self._add_commands()

    # 準備
    async def setup_hook(self) -> None:
        self.loop.create_task(self.followup_loop())
        self.loop.create_task(self.booking_loop())

    def hours(self) -> str:
        return self.store.get_setting("hours", DEFAULT_HOURS)

    def worlds(self) -> list[str]:
        raw = self.store.get_setting("worlds", DEFAULT_WORLDS)
        return [w.strip() for w in re.split(r"[,、，/／]", raw) if w.strip()][:MAX_WORLDS]

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self._sync(guild)
        self.say(f"Discord ボットが動いています（{', '.join(g.name for g in self.guilds) or 'サーバー未参加'}）")
        if self.guilds and not self.store.get_setting("panel_channel_id"):
            await self.first_setup(self.guilds[0])
        if self.guilds:
            await self.announce_version(self.guilds[0])

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
            "・申し込みが来たら、LG の Virtual Desktop Streamer に Meta ユーザー名を入れて「VD に登録した」を押してください\n"
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
        data = io.BytesIO(self.store.to_csv(forms).encode("utf-8-sig"))
        name = f"geaspec-tests-{self.clock().astimezone():%Y%m%d-%H%M}.csv"
        await interaction.response.send_message(embed=embed, file=discord.File(data, filename=name), ephemeral=True)

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
            await interaction.response.send_modal(ApplyModal(self, self.store.tester(interaction.user.id)))
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
        user = interaction.user
        before = self.store.tester(user.id)
        tester = self.store.apply(
            user.id, user.display_name, vrchat=modal.vrchat.value.strip(), meta=modal.meta.value.strip(),
            gear=modal.gear.value.strip(), region=modal.region.value.strip(), schedule=modal.schedule.value.strip(),
            now=self.clock())
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
            await thread.send(
                f"{user.mention} さん、申し込みありがとうございます！ここがあなた専用のスレッドです（運営とあなただけが見られます）。\n"
                "下の2つをえらんでください。運営が準備できたら、ここでお知らせします。",
                view=welcome_view(tester))
            await thread.send(prep_text(self.site_url))
        await interaction.followup.send(f"受け付けました。続きは {thread.mention} でどうぞ。", ephemeral=True)

        updated = "（申し込み内容の更新）" if before else ""
        lines = [f"**新しい申し込み**{updated} {user.mention} {thread.mention}",
                 f"VRChat: {tester['vrchat']} ／ 機材: {tester['gear']} ／ 地方: {tester['region']} ／ 都合: {tester['schedule'] or '—'}",
                 f"Meta ユーザー名: `{tester['meta']}`"]
        if before and before["meta"] and before["meta"] != tester["meta"]:
            lines.append(f"【注意】Meta ユーザー名が変わりました: `{before['meta']}` → `{tester['meta']}`")
            if tester["status"] == "ready":
                lines.append("→ LG の Virtual Desktop Streamer に入れたユーザー名も直してください（古いままだとつながりません）。")
        if tester["status"] == "ready":
            await self.notify_ops("\n".join(lines))
        else:
            lines.append("→ LG の Virtual Desktop Streamer にこのユーザー名を追加し、"
                         "「Allow remote connections」にチェックが入っているのを確かめたら、下のボタンを押してください。")
            await self.notify_ops("\n".join(lines), make_view(
                button("✅ VD に登録した", f"gs:ready:{user.id}", discord.ButtonStyle.success)))

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
                                  discord_id=(tester or {}).get("user_id", 0), now=self.clock())
        url = f"{self.site_url}/join?t={token}"
        return url + (f"&next={urllib.parse.quote(next_path, safe='')}" if next_path else "")

    def site_lines(self, tester: dict | None = None) -> str:
        if self.invite_secret:
            return (f"予約サイト（あなた専用のリンク）: {self.site_link(tester)}\n"
                    f"　ほかの人には渡さないでください。{invite.LINK_DAYS}日で使えなくなったら「サイトを開く」ボタンで新しいリンクが届きます\n")
        text = f"予約サイト: {self.site_url}\n"
        if self.site_password:
            text += f"　サイトを開くと合言葉を聞かれます。合言葉: `{self.site_password}`（ほかの人には教えないでください）\n"
        return text

    async def send_ready(self, thread: discord.Thread, tester: dict) -> None:
        """準備ができたお知らせ。グループ C (いい PC を持っている人) には、貸す側の案内も出す。"""
        user_id = tester["user_id"]
        if tester.get("grp") == "C":
            agent_url = self.store.get_setting("agent_url", DEFAULT_AGENT_URL)
            await thread.send(
                f"<@{user_id}> 準備ができました！ あなたには **PC を貸す側** の目線でもお話を聞かせてほしいです。\n\n"
                "**① 15分ほどのお話**: 「貸すならいくらほしいか」「何が心配か」などを聞かせてください（運営から日程の連絡をします）\n"
                "**② 貸し手ベータ（希望する人だけ）**: 自分の PC に Geaspec アプリを入れて、貸し出しを試せます\n"
                f"　1. {agent_url} から `GeaspecAgent-…-setup.exe` をダウンロード\n"
                "　2. 「Windows によって PC が保護されました」と出たら「詳細情報」→「実行」（まだ署名をしていないためです）\n"
                "　3. 予約サイトでアカウントを作り、アプリにそのメールアドレスとパスワードでログイン\n"
                "　4. 運営が確認して承認すると、貸し出せるようになります\n"
                "　※ 貸し出し中は PC の画面が幕で覆われ、終わると借りた人のデータが自動で消えます\n"
                "**③ 借りる側も試せます**: " + self.site_lines(tester) +
                f"テストができる時間: {self.hours()}",
                view=make_view(button("📥 アプリを入れた", "gs:lenderapp", discord.ButtonStyle.primary),
                               button("🔗 サイトを開く", "gs:link"),
                               button("▶ テストを始める", "gs:start")))
            return
        await thread.send(
            f"<@{user_id}> 準備ができました！\n\n"
            "**① 予約する**\n" + self.site_lines(tester) +
            ("　1. リンクを開いて、メールアドレスとパスワードを決める（Meta ユーザー名は申し込みのものを使います）\n"
             if self.invite_secret else
             "　1. 「新規登録」でメールアドレスとパスワードを決める\n"
             "　2. 「アカウント」で Meta ユーザー名を入れる（ここで書いたものと同じ）\n") +
            "　・「空き枠」から好きな時間を選んで「予約する」\n"
            "　予約できると、ここに日時のお知らせが届きます。迷ったら「予約で迷った」を押してください\n\n"
            "**② 当日**\n"
            "　**開始 5分前**になったら「テストを始める」を押して、Quest で Virtual Desktop を開いてください\n"
            f"テストができる時間: {self.hours()}\n"
            "家の Wi-Fi で参加してください（スマホのテザリングだとつながらないことがあります）。途中でやめても大丈夫です。",
            view=make_view(button("🔗 サイトを開く", "gs:link"),
                           button("🆘 予約で迷った", "gs:bookhelp", discord.ButtonStyle.danger),
                           button("▶ テストを始める", "gs:start", discord.ButtonStyle.primary)))

    # テスト本体
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
        if action == "f":
            await self.on_followup(interaction, tester, args)
            return
        session = self.store.open_session(interaction.channel_id)
        if session is None:
            await self.reply(interaction, "このテストはもう終わっています。もう一度試すときは「テストを始める」を押してください。")
            return
        handler = {
            "entered": self.on_entered, "help": self.on_help, "giveup": self.on_giveup, "done": self.on_done,
            "m": self.on_mission, "perf": self.on_perf_button, "a": self.on_answer, "comment": self.on_comment_button,
            "skip": self.on_skip,
        }.get(action)
        if handler:
            await handler(interaction, tester, session, args)

    async def on_tester_step(self, interaction: discord.Interaction, tester: dict, action: str) -> None:
        uid = tester["user_id"]
        if action == "booked":
            if self.store.mark_booked(uid, self.clock()):
                await self.notify_ops(f"<@{uid}> さんが予約できました <#{interaction.channel_id}>")
            await self.reply(interaction, "記録しました！ 当日、開始 5分前に「テストを始める」を押してください。")
        elif action == "bookhelp":
            count = self.store.add_booking_help(uid)
            await interaction.response.send_message(
                f"{self.ops_role_mention()} <@{uid}> さんが予約で迷っています（{count}回目）。\n"
                "どの画面で止まったか、このスレッドに書いてもらえると助かります（スクショも歓迎です）。")
            await self.notify_ops(f"<@{uid}> さんが予約で迷っています <#{interaction.channel_id}>")
        else:
            if self.store.mark_lender_app(uid, self.clock()):
                await self.notify_ops(f"<@{uid}> さんが貸し手ベータのアプリを入れました。管理画面で PC を承認してください "
                                      f"<#{interaction.channel_id}>")
            await self.reply(interaction, "ありがとうございます！ 運営が確認して承認したら、ここでお知らせします。")

    async def on_start(self, interaction: discord.Interaction, tester: dict) -> None:
        if tester["status"] != "ready":
            await self.reply(interaction, "まだ運営の準備（Virtual Desktop への登録）が終わっていません。お知らせを待ってください。")
            return
        session, created = self.store.start_session(tester["user_id"], interaction.channel_id, self.clock())
        if not created:
            await self.reply(interaction, "テストはもう始まっています。上のメッセージのボタンを使ってください。")
            return
        pc = self.store.get_setting("pc_name", "LG")
        await interaction.response.send_message(
            "**計測をはじめました**\n"
            "1. Quest で Virtual Desktop を開く\n"
            f"2. パソコンの一覧から「{pc}」を選ぶ\n"
            "3. VRChat が自動で立ち上がったら、自分のアカウントでログイン\n"
            "パソコンの画面は「Geass 利用中」の幕で隠れていますが、それで正常です。\n\n"
            "ワールドに入れたら「ワールドに入れた」を押してください。",
            view=make_view(button("✅ ワールドに入れた", "gs:entered", discord.ButtonStyle.success),
                           button("🆘 困った", "gs:help", discord.ButtonStyle.danger),
                           button("つながらなかった", "gs:giveup")))
        await self.notify_ops(f"<@{tester['user_id']}> さんがテストを始めました <#{interaction.channel_id}>")

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
            "**数字の測り方**\n"
            "1. VRChat のメニュー（Launch Pad）の歯車（設定）→ いちばん下の「デバッグ」→「FPS と Ping の表示」をオン"
            f"（画像: {self.site_url}/guide#fps）\n"
            f"2. 「{worlds}」に行き、それぞれ**スポーン地点で 30秒立ったまま** FPS と Ping を見る\n"
            "3. 「数字を入れる」から入力する（画面のスクショを**このスレッドに貼る**だけでもOK）\n\n"
            "左手首に残り時間が出ます。遊び終わったら「遊び終わった」を押してください。",
            view=play_view(json.loads(session["missions"])))
        await self.notify_ops(f"<@{tester['user_id']}> さんがワールドに入りました（{format_duration(seconds)}）")

    async def on_mission(self, interaction, tester, session, args) -> None:
        if not args or args[0] not in MISSIONS:
            return
        missions = self.store.toggle_mission(session["id"], args[0])
        await interaction.response.edit_message(view=play_view(missions))

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
        await interaction.response.edit_message(content="遊び終わりました。おつかれさまでした！", view=None)
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
        text, view = survey_message(json.loads(self.store.session(session["id"])["answers"]))
        await interaction.response.edit_message(content=text, view=view)

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
        self.store.complete(session["id"], self.clock(), stuck=modal.stuck.value, comment=modal.comment.value)
        tester = self.store.tester(session["user_id"])
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
                "**最後に、評価フォーム（5分）に答えてください**\n"
                f"{self.site_link(tester, f'/feedback?t={session_id}')}\n"
                "サイトの見た目・名前・使いやすさの 6段階評価と、回線の速さ・住んでいる場所・払ってもいい金額などを聞きます。\n\n"
                f"3日後にもう一度だけ、ここで3つ質問させてください。よければ 15分ほどお話も聞かせてください（運営から連絡します）。")

    @staticmethod
    def again_view() -> discord.ui.View:
        return make_view(button("▶ もう一度テストする", "gs:start"))

    async def report_done(self, session_id: int) -> None:
        s = self.store.session(session_id)
        a = json.loads(s["answers"])
        enter = "—"
        if s["entered_at"]:
            enter = format_duration((dt.datetime.fromisoformat(s["entered_at"])
                                     - dt.datetime.fromisoformat(s["opened_at"])).total_seconds())
        picks = [f"{q['text'].split('？')[0]}: {option_label(q['key'], a[q['key']])}"
                 for q in QUESTIONS if q["key"] in ("quality", "again", "nps", "price") and q["key"] in a]
        lines = [f"<@{s['user_id']}> さんのテストが完了（{'つながった' if s['connected'] else 'つながらなかった'}・"
                 f"入るまで {enter}・困った {s['help_count']}回）", *picks]
        if s["stuck"]:
            lines.append(f"迷ったところ: {s['stuck']}")
        if s["comment"]:
            lines.append(f"ひとこと: {s['comment']}")
        await self.notify_ops("\n".join(lines))

    # 3 日後の質問
    async def followup_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.send_followups()
            except Exception:  # noqa: BLE001 - 次の回にやり直す
                log.exception("3 日後の質問を送れませんでした")
            await asyncio.sleep(FOLLOWUP_CHECK_SECONDS)

    async def send_followups(self) -> int:
        sent = 0
        for s in self.store.followups_due(self.clock()):
            tester = self.store.tester(s["user_id"])
            thread = await self._existing_thread(tester) if tester else None
            if thread is not None:
                text, view = followup_message(s["user_id"], json.loads(s["answers"]))
                await thread.send(text, view=view)
                sent += 1
            self.store.mark_followup_sent(s["id"], self.clock())
        return sent

    # 予約のお知らせ (予約サイトのデータベースを 1 分ごとに見る)
    async def booking_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.send_booking_notices()
            except Exception:  # noqa: BLE001 - 次の回にやり直す
                log.exception("予約のお知らせを送れませんでした")
            await asyncio.sleep(BOOKING_CHECK_SECONDS)

    async def send_booking_notices(self) -> int:
        if self.site_db is None:
            return 0
        now = self.clock()
        bookings = booking_notice.load_bookings(self.site_db, now)
        sent = 0
        for kind, b in booking_notice.due_notices(bookings, now, self.store.notice_sent):
            tester = self.store.tester_by_meta(b["meta"])
            thread = await self._existing_thread(tester) if tester else None
            when = booking_notice.when_text(b["start"], b["end"])
            who = booking_notice.who(b) + (f" <@{tester['user_id']}>" if tester else "")
            where = f" {thread.mention}" if thread else ""
            if kind == "booked":
                await self.notify_ops(f"予約が入りました: **{when}**（{b['host']}）{who}{where}")
                if tester:
                    self.store.mark_booked(tester["user_id"], now)
                if thread:
                    await thread.send(f"<@{tester['user_id']}> 予約を受け付けました: **{when}**\n"
                                      f"開始 {booking_notice.SOON_MINUTES} 分前に、ここでお知らせします。")
            elif kind == "soon":
                lines = [f"{booking_notice.SOON_MINUTES} 分後に予約があります: **{when}**（{b['host']}）{who}{where}",
                         f"確認: {b['host']} の電源が入っている／Geaspec アプリが動いている／{REMOTE_SETTING}にチェック"]
                # 前の人が使っている最中は、アプリがサイトに問い合わせないので「連絡なし」とは見なさない
                in_use = any(o["host"] == b["host"] and o["status"] == "active" for o in bookings)
                if not in_use and not booking_notice.agent_running(b, now):
                    lines.append(f"【注意】{self.ops_role_mention()} {b['host']} の Geaspec アプリから、しばらく連絡がありません。"
                                 "このままだとテスターがつなげません。PC の電源とアプリを確かめてください。")
                await self.notify_ops("\n".join(lines))
                if thread:
                    await thread.send(
                        f"<@{tester['user_id']}> まもなく始まります（**{when}**）\n"
                        "開始 5 分前になったら「テストを始める」を押して、Quest で Virtual Desktop を開いてください。",
                        view=make_view(button("▶ テストを始める", "gs:start", discord.ButtonStyle.primary)))
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
        if message.author.bot or not isinstance(message.channel, discord.Thread) or not message.attachments:
            return
        session = self.store.open_session(message.channel.id)
        if session is None or session["user_id"] != message.author.id:
            return
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


async def _run(token: str, store: TesterStore, shots_dir: Path, site_url: str, say: Callable[[str], None],
               site_password: str = "", invite_secret: str = "") -> None:
    message_content = True
    while True:
        bot = TesterBot(store, shots_dir, site_url, site_password=site_password, invite_secret=invite_secret, site_db=shots_dir.parent / "geaspec.db",
                        message_content=message_content, say=say)
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
                     say: Callable[[str], None] = print) -> threading.Thread:
    """予約サイトと同じ黒い画面の中で、別のスレッドとしてボットを動かす。"""
    data.mkdir(parents=True, exist_ok=True)
    store = TesterStore(data / "tester_bot.db")

    def target() -> None:
        try:
            asyncio.run(_run(token, store, data / "tester_screenshots", site_url, say, site_password, invite_secret))
        except Exception as exc:  # noqa: BLE001 - ボットが止まってもサイトは動かし続ける
            say(f"Discord ボットが止まりました: {exc}")

    thread = threading.Thread(target=target, name="geaspec-discord-bot", daemon=True)
    thread.start()
    return thread
