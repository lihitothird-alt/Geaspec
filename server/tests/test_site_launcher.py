import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import site_launcher as s  # noqa: E402


def test_first_settings_are_asked_once():
    answers = iter(["not-an-email", "owner@example.com", "himitsu", "geaspec@gmail.com", "abcd efgh ijkl mnop",
                    " bot-token "])
    config = s.ask_first_settings({}, input_fn=lambda prompt: next(answers))
    assert config["admin_email"] == "owner@example.com"
    assert config["site_password"] == "himitsu"
    assert config["mail_user"] == "geaspec@gmail.com"
    assert config["mail_password"] == "abcdefghijklmnop"
    assert config["discord_token"] == "bot-token"
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
    assert config["mail_user"] == "" and config["mail_password"] == ""
    s.ask_first_settings(config, input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError(prompt)))
    env = s.site_environment({**config, "mail_user": "g@gmail.com", "mail_password": "p"}, "", tmp_path)
    assert env["SMTP_USER"] == "g@gmail.com" and env["SMTP_PASSWORD"] == "p"


def test_discord_bot_starts_only_with_a_token(tmp_path, monkeypatch):
    assert s.start_discord_bot({"discord_token": ""}, "https://pc.ts.net", tmp_path) is False
    import app.tester_bot as bot

    started = []
    monkeypatch.setattr(bot, "start_tester_bot", lambda *args: started.append(args))
    assert s.start_discord_bot({"discord_token": "t", "site_password": "himitsu"}, "", tmp_path) is True
    assert started == [("t", tmp_path, "http://localhost:8000", "himitsu")]


def test_with_a_discord_bot_the_site_uses_personal_links_instead_of_a_password(tmp_path, monkeypatch):
    import app.tester_bot as bot

    config = {"discord_token": "t", "secret_key": "k", "admin_email": "a@example.com", "site_password": "himitsu"}
    env = s.site_environment(config, "", tmp_path)
    assert env["INVITE_ONLY"] == "1" and env["SITE_PASSWORD"] == ""
    started = []
    monkeypatch.setattr(bot, "start_tester_bot", lambda *args, **kw: started.append((args, kw)))
    assert s.start_discord_bot(config, "", tmp_path) is True
    assert started == [(("t", tmp_path, "http://localhost:8000"), {"invite_secret": "k"})]
    # 合言葉に戻すこともできる
    back = s.site_environment({**config, "invite_only": False}, "", tmp_path)
    assert back["INVITE_ONLY"] == "0" and back["SITE_PASSWORD"] == "himitsu"
    # ボットがないときは、合言葉を聞く
    asked = s.ask_first_settings({"admin_email": "a@example.com", "mail_user": ""}, input_fn=lambda _: "pw")
    assert asked["site_password"] == "pw"
    assert "site_password" not in s.ask_first_settings(
        {"admin_email": "a@example.com", "mail_user": "", "discord_token": "t"}, input_fn=lambda _: "")


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
