"""Geaspec の予約サイトを、自分の Windows PC で動かしてインターネットに公開する。

「GeaspecSite.cmd」をダブルクリックすると、これが動く。

1. はじめてのときだけ、管理者のメールアドレスなどを聞いて保存する
2. 予約サイトを起動し、準備ができたらブラウザで開く (この黒い画面を閉じるとサイトも止まる)
3. Tailscale Funnel でこの PC のサイトを https://<PC名>.<tailnet>.ts.net として公開する

データ (予約・アカウント・写真) はアプリのフォルダではなく
%LocalAppData%\\GeaspecSite に置くので、新しい版に入れ替えても消えない。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

PORT = 8000
FIRST_WAIT_SEC = 60
LONG_WAIT_SEC = 600
HERE = Path(__file__).resolve().parent
TAILSCALE_PATHS = (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe")


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "GeaspecSite"


def load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def invite_mode(config: dict) -> bool:
    """Discord ボットがあれば、合言葉の代わりにボットが渡す本人専用リンクで入ってもらう ("invite_only": false で合言葉に戻す)。"""
    return (bool(str(config.get("discord_token") or "").strip()) and bool(config.get("secret_key"))
            and config.get("invite_only", True) is not False)


def ask_first_settings(config: dict, input_fn=input) -> dict:
    """はじめてのときだけ聞く。決まっているものは聞かない。"""
    config = dict(config)
    config.setdefault("secret_key", secrets.token_hex(32))
    while "@" not in config.get("admin_email", ""):
        print()
        print("管理者（運営）のメールアドレスを入れてください。")
        print("このメールアドレスでサイトに登録すると「管理」画面が使えます。")
        config["admin_email"] = input_fn("メールアドレス: ").strip()
    if "site_password" not in config and not invite_mode(config):
        print()
        print("サイトの合言葉を決めてください。知っている人だけがサイトを見られるようになります。")
        print("（合言葉なしで公開する場合は、何も入れずに Enter）")
        config["site_password"] = input_fn("合言葉: ").strip()
    if "mail_user" not in config:
        print()
        print("予約が入ったときなどに、お知らせメールを送れます。送り元にする Gmail アドレスを入れてください。")
        print("（メールを送らない場合は、何も入れずに Enter。あとで設定ファイルを消すと、もう一度聞きます）")
        config["mail_user"] = input_fn("Gmail アドレス: ").strip()
        config["mail_password"] = ""
        if config["mail_user"]:
            print()
            print("Gmail の「アプリ パスワード」(16 文字) を入れてください。ふだんのパスワードではありません。")
            print("作り方: https://myaccount.google.com/apppasswords を開き、名前に Geaspec と入れて「作成」を押す")
            print("（2 段階認証をオンにしていないと、このページは使えません）")
            config["mail_password"] = input_fn("アプリ パスワード: ").replace(" ", "").strip()
    if "discord_token" not in config:
        print()
        print("テスター用の Discord ボットを使えます（テスターが Discord のボタンでテストを進め、記録が自動でたまります）。")
        print("ボットのトークンを入れてください。作り方は README の「テスター用 Discord ボット」にあります。")
        print("（使わない場合は、何も入れずに Enter）")
        config["discord_token"] = input_fn("Discord ボットのトークン: ").strip()
    config.setdefault("payment_providers", "fake")
    return config


def find_tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return next((p for p in TAILSCALE_PATHS if Path(p).exists()), None)


def public_address(status: dict) -> str:
    """`tailscale status --json` から、この PC の公開アドレス (https://名前.ts.net) を作る。"""
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return f"https://{name}" if name else ""


def tailscale_status(tailscale: str) -> dict:
    try:
        out = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True, timeout=20, check=False)
        data = json.loads(out.stdout or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def tailscale_running(status: dict) -> bool:
    """Tailscale がつながっているか (アプリでオフにしていると Stopped になる)。"""
    return status.get("BackendState") == "Running"


def start_funnel(tailscale: str) -> bool:
    """この PC の PORT 番をインターネットに公開する。初回は Tailscale がブラウザで許可を求めることがある。"""
    print()
    print("Tailscale Funnel でサイトを公開します…")
    print("（はじめてのときは下にリンクが出ます。ブラウザで開いて「許可」を押すと続きます）")
    try:
        result = subprocess.run([tailscale, "funnel", "--bg", str(PORT)], timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"公開できませんでした: {exc}")
        return False
    return result.returncode == 0


def site_environment(config: dict, address: str, data: Path) -> dict[str, str]:
    env = {
        "SECRET_KEY": config["secret_key"],
        "ADMIN_EMAILS": config["admin_email"],
        "SITE_PASSWORD": "" if invite_mode(config) else config.get("site_password", ""),
        "INVITE_ONLY": "1" if invite_mode(config) else "0",
        "PAYMENT_PROVIDERS": config.get("payment_providers", "fake"),
        "DATABASE_URL": f"sqlite:///{(data / 'geaspec.db').as_posix()}",
        "MEDIA_DIR": str(data / "media"),
        "BASE_URL": address or f"http://localhost:{PORT}",
        "SMTP_USER": config.get("mail_user", ""),
        "SMTP_PASSWORD": config.get("mail_password", ""),
    }
    # Stripe・PayPal・Tailscale API などの鍵は、設定ファイルの "env" にそのまま書けば渡す
    extra = config.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def port_in_use(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_until_ready(port: int, timeout: float = 60.0) -> bool:
    """サイトが返事をするようになるまで待つ (ブラウザを開くのが早すぎると「接続できません」になるため)。"""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def after_start(tailscale: str | None, address: str, data: Path, bot_on: bool = False) -> None:
    """サイトが動き出してから行うこと: ブラウザを開き、インターネットに公開する。

    公開の許可 (はじめの 1 回だけ) を待っている間も、この PC ではサイトを使えるようにする。
    """
    # 新しい版をはじめて起動したときは、Windows のウイルス検査などで数分かかることがある。すぐにあきらめない
    if not wait_until_ready(PORT, FIRST_WAIT_SEC):
        print("サイトの起動に時間がかかっています。もう少し待ちます（はじめて起動したときは数分かかることがあります）…")
        if not wait_until_ready(PORT, LONG_WAIT_SEC):
            print("サイトを起動できませんでした。この画面の写真を運営に送ってください。")
            return
        print("サイトが起動しました。")
    webbrowser.open(f"http://localhost:{PORT}")
    published = bool(tailscale and address) and start_funnel(tailscale)
    print()
    print("-" * 60)
    print(f" この PC で開くアドレス: http://localhost:{PORT}")
    if published:
        print(f" サイトのアドレス（ほかの人に伝える）: {address}")
    elif tailscale and address:
        print(" インターネットへの公開はまだです。上にリンクが出ていたら、ブラウザで開いて許可してから、")
        print(" この画面を閉じてもう一度起動してください。")
    print(f" 設定とデータの場所: {data}")
    if bot_on:
        print(" テスター用の Discord ボットも動いています（記録: tester_bot.db、スクショ: tester_screenshots）。")
    print(" この黒い画面を閉じると、サイトも止まります。")
    print("-" * 60)


def start_discord_bot(config: dict, address: str, data: Path) -> bool:
    """トークンが設定されていれば、テスター用の Discord ボットを動かす。動かせなくてもサイトは動かす。"""
    token = str(config.get("discord_token") or "").strip()
    if not token:
        return False
    try:
        from app.tester_bot import start_tester_bot
    except ImportError as exc:
        print(f"Discord ボットを読み込めませんでした: {exc}")
        return False
    # サイトに入るためのリンク (または合言葉) は、準備ができたテスターの専用スレッド (本人と運営だけが見られる) で伝える
    # 「いま終わる」は、同じ PC で動くサイトに直接頼む (SECRET_KEY の署名つき)
    api = {"site_api": f"http://127.0.0.1:{PORT}", "api_secret": str(config.get("secret_key") or "")}
    if invite_mode(config):
        start_tester_bot(token, data, address or f"http://localhost:{PORT}", invite_secret=config["secret_key"], **api)
    else:
        start_tester_bot(token, data, address or f"http://localhost:{PORT}", str(config.get("site_password") or ""), **api)
    return True


def main() -> int:
    import threading

    sys.path.insert(0, str(HERE))
    from app.version import VERSION

    print("=" * 60)
    print(f" Geaspec 予約サイト {VERSION}")
    print("=" * 60)
    if port_in_use(PORT):
        # 前の版の黒い画面が開いたままだと、新しい版はサイトもボットも動かせない (前の版が動き続ける)
        print()
        print("前に起動した Geaspec の黒い画面がまだ開いています。")
        print("タスクバーで前の黒い画面を探して閉じてから、もう一度 GeaspecSite.cmd を起動してください。")
        return 1
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    config_path = data / "site_config.json"
    config = ask_first_settings(load_config(config_path))
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    address = ""
    tailscale = find_tailscale()
    if not tailscale:
        print()
        print("Tailscale が見つかりません。この PC の中だけでサイトを動かします（インターネットには公開されません）。")
    else:
        status = tailscale_status(tailscale)
        if tailscale_running(status):
            address = public_address(status)
        else:
            print()
            print("Tailscale がオフになっています。この PC の中だけでサイトを動かします。")
            print("公開するときは、画面右下のタスクバーにある Tailscale のアイコンを右クリックして「Connect」を押し、")
            print("この画面を閉じてから、もう一度起動してください。")

    os.environ.update(site_environment(config, address, data))
    bot_on = start_discord_bot(config, address, data)
    print()
    print("サイトを起動しています…")
    threading.Thread(target=after_start, args=(tailscale, address, data, bot_on), daemon=True).start()

    import uvicorn

    uvicorn.run(
        "app.main:app", host="127.0.0.1", port=PORT, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
