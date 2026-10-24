import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import site_launcher as s  # noqa: E402


def test_first_settings_are_asked_once():
    answers = iter(["not-an-email", "owner@example.com", "himitsu", "geaspec@gmail.com", "abcd efgh ijkl mnop",
                    " bot-token ", " https://Geaspec.com/ "])
    config = s.ask_first_settings({"public": False}, input_fn=lambda prompt: next(answers))
    assert config["admin_email"] == "owner@example.com"
    assert config["site_password"] == "himitsu"
    assert config["mail_user"] == "geaspec@gmail.com"
    assert config["mail_password"] == "abcdefghijklmnop"
    assert config["discord_token"] == "bot-token"
    assert config["domain"] == "geaspec.com"
    assert len(config["secret_key"]) == 64
    # 2 回目からは何も聞かない
    again = s.ask_first_settings(config, input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError(prompt)))
    assert again == config


def test_public_address_from_tailscale_status():
    assert s.public_address({"Self": {"DNSName": "licht-pc.tail1234.ts.net."}}) == "https://licht-pc.tail1234.ts.net"
    assert s.public_address({}) == ""


def test_site_environment_keeps_data_outside_the_app(tmp_path):
    config = {"secret_key": "k", "admin_email": "owner@example.com", "site_password": "",
              "env": {"STRIPE_SECRET_KEY": "sk_test_x"}}
    env = s.site_environment(config, "https://pc.ts.net", tmp_path)
    assert env["BASE_URL"] == "https://pc.ts.net"
    assert env["DATABASE_URL"] == f"sqlite:///{(tmp_path / 'geaspec.db').as_posix()}"
    assert env["MEDIA_DIR"] == str(tmp_path / "media")
    assert env["PAYMENT_PROVIDERS"] == "fake"
    assert env["STRIPE_SECRET_KEY"] == "sk_test_x"
    assert s.site_environment(config, "", tmp_path)["BASE_URL"] == "http://localhost:8000"


def test_tailscale_must_be_connected():
    assert s.tailscale_running({"BackendState": "Running"})
    assert not s.tailscale_running({"BackendState": "Stopped", "Self": {"DNSName": "pc.ts.net."}})
    assert not s.tailscale_running({})


def test_mail_is_asked_once_for_existing_sites_and_can_be_skipped(tmp_path):
    old = {"secret_key": "k", "admin_email": "owner@example.com", "site_password": "x"}
    config = s.ask_first_settings(old, input_fn=lambda prompt: "")
    assert config["mail_user"] == "" and config["mail_password"] == "" and "domain" not in config
    # メールはもう聞かない。ドメインは、まだないなら次の起動でまた聞く
    asked = []
    s.ask_first_settings(config, input_fn=lambda prompt: asked.append(prompt) or "")
    assert asked == ["ドメイン: "]
    env = s.site_environment({**config, "mail_user": "g@gmail.com", "mail_password": "p"}, "", tmp_path)
    assert env["SMTP_USER"] == "g@gmail.com" and env["SMTP_PASSWORD"] == "p"


def test_discord_bot_starts_only_with_a_token(tmp_path, monkeypatch):
    assert s.start_discord_bot({"discord_token": ""}, "https://pc.ts.net", tmp_path) is False
    import app.tester_bot as bot

    started = []
    monkeypatch.setattr(bot, "start_tester_bot", lambda *args, **kw: started.append((args, kw)))
    closed = {"discord_token": "t", "site_password": "himitsu", "public": False}
    assert s.start_discord_bot(closed, "", tmp_path) is True
    # 公開しているサイトでは、古い合言葉をテスターに伝えない
    assert s.start_discord_bot({"discord_token": "t", "site_password": "himitsu"}, "", tmp_path) is True
    api = {"site_api": "http://127.0.0.1:8000", "api_secret": ""}
    assert started == [(("t", tmp_path, "http://localhost:8000", "himitsu"), api),
                       (("t", tmp_path, "http://localhost:8000", ""), api)]


def test_with_a_discord_bot_the_site_uses_personal_links_instead_of_a_password(tmp_path, monkeypatch):
    import app.tester_bot as bot

    config = {"discord_token": "t", "secret_key": "k", "admin_email": "a@example.com", "site_password": "himitsu",
              "public": False}
    env = s.site_environment(config, "", tmp_path)
    assert env["INVITE_ONLY"] == "1" and env["SITE_PASSWORD"] == ""
    started = []
    monkeypatch.setattr(bot, "start_tester_bot", lambda *args, **kw: started.append((args, kw)))
    assert s.start_discord_bot(config, "", tmp_path) is True
    # 「いま終わる」は、同じ PC のサイトに署名つきで頼む
    assert started == [(("t", tmp_path, "http://localhost:8000"),
                        {"invite_secret": "k", "site_api": "http://127.0.0.1:8000", "api_secret": "k"})]
    # 合言葉に戻すこともできる
    back = s.site_environment({**config, "invite_only": False}, "", tmp_path)
    assert back["INVITE_ONLY"] == "0" and back["SITE_PASSWORD"] == "himitsu"
    # ボットがないときは、合言葉を聞く
    asked = s.ask_first_settings({"admin_email": "a@example.com", "mail_user": "", "public": False},
                                 input_fn=lambda _: "pw")
    assert asked["site_password"] == "pw"
    assert "site_password" not in s.ask_first_settings(
        {"admin_email": "a@example.com", "mail_user": "", "discord_token": "t"}, input_fn=lambda _: "")


def test_site_is_public_by_default_even_with_a_discord_bot(tmp_path, monkeypatch):
    import app.tester_bot as bot

    config = {"discord_token": "t", "secret_key": "k", "admin_email": "a@example.com", "site_password": "himitsu"}
    env = s.site_environment(config, "", tmp_path)
    assert env["INVITE_ONLY"] == "0" and env["SITE_PASSWORD"] == ""
    # ボットは、Meta ユーザー名とスレッドを引き継ぐ本人専用リンクを、これまでどおり渡す
    started = []
    monkeypatch.setattr(bot, "start_tester_bot", lambda *args, **kw: started.append((args, kw)))
    s.start_discord_bot(config, "", tmp_path)
    assert started[0][1]["invite_secret"] == "k"
    # はじめての設定で、合言葉は聞かない
    assert "site_password" not in s.ask_first_settings({"admin_email": "a@example.com", "mail_user": ""},
                                                       input_fn=lambda _: "")


def test_setup_only_saves_the_settings_and_does_not_start(tmp_path, monkeypatch):
    """クラウドのサーバーに入れるときは、設定だけ聞いて終わる (起動はサーバーが自動で行う)。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["site_launcher.py", "--setup"])
    monkeypatch.setattr(s, "port_in_use", lambda port: False)
    answers = iter(["owner@example.com", "", "", ""])
    ask = s.ask_first_settings
    monkeypatch.setattr(s, "ask_first_settings",
                        lambda config, **kw: ask(config, input_fn=lambda prompt: next(answers), **kw))
    monkeypatch.setattr(s, "start_discord_bot", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("started")))
    assert s.main() == 0
    assert '"admin_email": "owner@example.com"' in (tmp_path / "GeaspecSite" / "site_config.json").read_text(
        encoding="utf-8")


def test_port_in_use_detects_an_old_window():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert s.port_in_use(port) is True
    assert s.port_in_use(port) is False


def test_a_slow_first_start_is_waited_for(monkeypatch, capsys):
    calls = []

    def wait(port, timeout):
        calls.append(timeout)
        return len(calls) > 1  # 1 回目は間に合わず、2 回目で起動した

    monkeypatch.setattr(s, "wait_until_ready", wait)
    monkeypatch.setattr(s.webbrowser, "open", lambda url: None)
    s.after_start(None, "", s.Path("."))
    out = capsys.readouterr().out
    assert calls == [s.FIRST_WAIT_SEC, s.LONG_WAIT_SEC]
    assert "時間がかかっています" in out and "起動できませんでした" not in out


def test_own_domain_becomes_the_site_address_and_is_not_asked_on_the_server(tmp_path):
    config = {"secret_key": "k", "admin_email": "a@example.com", "domain": "geaspec.com"}
    assert s.public_url(config, "https://lg.tail.ts.net") == "https://geaspec.com"
    assert s.public_url({}, "https://lg.tail.ts.net") == "https://lg.tail.ts.net"
    assert s.site_environment(config, s.public_url(config, ""), tmp_path)["BASE_URL"] == "https://geaspec.com"
    # クラウドのサーバーで自動で起動したとき (入力できない) は、あとから足した質問で止まらない
    old = {"secret_key": "k", "admin_email": "a@example.com", "mail_user": "", "discord_token": ""}
    assert "domain" not in s.ask_first_settings(old, input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
                                                interactive=False)


def test_cloudflare_tunnel_is_set_up_once_with_keys_in_the_data_folder(tmp_path, monkeypatch):
    import json
    import subprocess

    import cloudflare_tunnel as cf

    home = tmp_path / "home"
    monkeypatch.setattr(cf.Path, "home", lambda: home)
    calls = []

    def run(args, check=False):
        calls.append(args[2:] if args[1:3] == ["tunnel", "--origincert"] else args[1:])
        if args[1:] == ["tunnel", "login"]:
            (home / ".cloudflared").mkdir(parents=True)
            (home / ".cloudflared" / "cert.pem").write_text("cert")
        if "create" in args:
            Path(args[args.index("--credentials-file") + 1]).write_text(json.dumps({"TunnelID": "abc-123"}))
        return subprocess.CompletedProcess(args, 0)

    data = tmp_path / "GeaspecSite"
    assert cf.ensure_tunnel(Path("cloudflared"), data, "geaspec.com", run=run) is True
    assert calls[0] == ["tunnel", "login"]
    assert (data / "cloudflared" / "cert.pem").read_text() == "cert"
    assert calls[-1][-4:] == ["dns", "--overwrite-dns", "abc-123", "geaspec.com"]
    # 2 回目からはログインもトンネル作りもしない (向き先だけ確かめ直す)
    calls.clear()
    assert cf.ensure_tunnel(Path("cloudflared"), data, "geaspec.com", run=run) is True
    assert len(calls) == 1 and "route" in calls[0]

    started = []
    cf.start_tunnel(Path("cloudflared"), data, "geaspec.com", 8000, popen=lambda args, **kw: started.append(args) or object())
    config = (data / "cloudflared" / "config.yml").read_text()
    assert "tunnel: abc-123" in config and "hostname: geaspec.com" in config and "http://localhost:8000" in config
    assert started[0][-1] == "run" and "--config" in started[0]


def test_cloudflare_diagnosis_says_which_step_is_missing(tmp_path):
    import cloudflare_tunnel as cf

    data = tmp_path / "GeaspecSite"
    (data / "cloudflared").mkdir(parents=True)
    (data / "cloudflared" / "cert.pem").write_text("cert")

    def no_dns(host):
        raise OSError("not found")

    def offline(url):
        raise OSError("ERR_NAME_NOT_RESOLVED")

    lines = cf.diagnose(data, "geaspec.com", Path("cloudflared.exe"), resolve=no_dns, fetch=offline)
    text = "\n".join(lines)
    assert "✅ Cloudflare のログイン" in text and "❌ トンネルがまだ作られていません" in text
    assert "❌ DNS（住所の登録）がありません" in text and "❌ https://geaspec.com がまだ開けません" in text
    assert (data / "cloudflared" / "diagnosis.txt").read_text(encoding="utf-8").startswith("Cloudflare の診断")

    # 全部そろっていれば ✅ だけ
    (data / "cloudflared" / "tunnel.json").write_text('{"TunnelID": "abc"}')
    (data / "cloudflared" / "tunnel.log").write_text("INF Registered tunnel connection connIndex=0\n")
    lines = cf.diagnose(data, "geaspec.com", Path("cloudflared.exe"), resolve=lambda h: ["104.21.0.1"], fetch=lambda u: 200)
    assert all(line.startswith("✅") for line in lines[1:])


def test_leftover_tunnel_from_the_data_folder_is_stopped_on_windows(monkeypatch):
    """前の版で黒い画面を閉じても残っていたトンネルは、次の起動で止める (残っていると 502 になる)。"""
    import cloudflare_tunnel as cf

    ran = []
    monkeypatch.setattr(cf.sys, "platform", "win32")
    cf.stop_leftovers(Path("C:/data/GeaspecSite/cloudflared/cloudflared.exe"), run=lambda args, **kw: ran.append(args))
    assert ran == [["taskkill", "/F", "/IM", "cloudflared.exe"]]
    # 自分で入れた cloudflared (データのフォルダの外) は止めない
    cf.stop_leftovers(Path("C:/Program Files/cloudflared/bin/cloudflared.exe"), run=lambda args, **kw: ran.append(args))
    monkeypatch.setattr(cf.sys, "platform", "linux")
    cf.stop_leftovers(Path("/home/u/GeaspecSite/cloudflared/cloudflared"), run=lambda args, **kw: ran.append(args))
    assert len(ran) == 1


def test_bundled_certificates_are_trusted(monkeypatch):
    """Windows の証明書の一覧が足りなくても、https の確認ができるように certifi の一覧を使う。"""
    import ssl
    import urllib.request

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    s.trust_bundled_certificates()
    import certifi

    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    ctx = ssl.create_default_context()  # urllib・Discord ボット (aiohttp) が使うのと同じ作り方
    assert ctx.cert_store_stats()["x509_ca"] > 100
    # すでに決めてあれば変えない
    monkeypatch.setenv("SSL_CERT_FILE", "/custom.pem")
    s.trust_bundled_certificates()
    assert os.environ["SSL_CERT_FILE"] == "/custom.pem"
