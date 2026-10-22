"""Cloudflare Tunnel で、予約サイトを自分のドメイン (例: https://geaspec.com) に公開する。

Tailscale Funnel は海外の中継を通ることがあって遅いので、Cloudflare の東京の入り口から届くようにする。
はじめての 1 回だけ、ブラウザで Cloudflare にログインして、ドメインを選んで「Authorize」を押す。
あとは自動で、トンネルを作り、ドメインをこの PC (またはサーバー) につなぐ。

鍵 (cert.pem と tunnel.json) はデータのフォルダ (GeaspecSite/cloudflared) に置くので、
データのフォルダごとクラウドのサーバーに引っ越せば、そのまま使える。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable

TUNNEL_NAME = "geaspec-site"
DOWNLOADS = {
    "win32": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "linux": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
}

Runner = Callable[..., subprocess.CompletedProcess]


def tunnel_dir(data: Path) -> Path:
    return data / "cloudflared"


def find_cloudflared(data: Path, download: bool = True) -> Path | None:
    """cloudflared のプログラム。入っていなければ、データのフォルダにダウンロードする。"""
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    exe = tunnel_dir(data) / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    if exe.exists():
        return exe
    url = DOWNLOADS.get(sys.platform)
    if not download or not url:
        return None
    print("Cloudflare のプログラム (cloudflared) をダウンロードしています…")
    exe.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - 決まった GitHub の URL だけ
            exe.write_bytes(r.read())
    except OSError as exc:
        print(f"ダウンロードできませんでした: {exc}")
        return None
    if sys.platform != "win32":
        exe.chmod(0o755)
    return exe


def tunnel_id(data: Path) -> str:
    try:
        return str(json.loads((tunnel_dir(data) / "tunnel.json").read_text(encoding="utf-8"))["TunnelID"])
    except (OSError, ValueError, KeyError):
        return ""


def ensure_tunnel(exe: Path, data: Path, domain: str, run: Runner = subprocess.run) -> bool:
    """トンネルを用意して、ドメインをこのトンネルにつなぐ。はじめてのときだけブラウザでの許可がいる。"""
    d = tunnel_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    cert, cred = d / "cert.pem", d / "tunnel.json"
    if not cert.exists():
        print()
        print(f"Cloudflare にログインします。ブラウザが開いたら（開かなければ下のリンクを開いて）、「{domain}」を選んで")
        print("「Authorize」を押してください。はじめての 1 回だけです。")
        run([str(exe), "tunnel", "login"], check=False)
        default = Path.home() / ".cloudflared" / "cert.pem"
        if not default.exists():
            print("Cloudflare の許可が確かめられませんでした。この画面を閉じて、もう一度起動してください。")
            return False
        shutil.copyfile(default, cert)
    base = [str(exe), "tunnel", "--origincert", str(cert)]
    if not cred.exists():
        # 前に同じ名前のトンネルを作っていて鍵がないときは、消して作り直す (鍵は作ったときにしか受け取れない)
        result = run(base + ["create", "--credentials-file", str(cred), TUNNEL_NAME], check=False)
        if result.returncode != 0 or not cred.exists():
            run(base + ["delete", "-f", TUNNEL_NAME], check=False)
            run(base + ["create", "--credentials-file", str(cred), TUNNEL_NAME], check=False)
        if not cred.exists():
            print("Cloudflare のトンネルを作れませんでした。この画面の写真を運営（開発）に送ってください。")
            return False
    result = run(base + ["route", "dns", "--overwrite-dns", tunnel_id(data) or TUNNEL_NAME, domain], check=False)
    if result.returncode != 0:
        print(f"「{domain}」をトンネルにつなげませんでした。Cloudflare でこのドメインを使っているか確かめてください。")
        return False
    return True


def write_config(data: Path, domain: str, port: int) -> Path:
    """トンネルの設定: このドメインへの通信を、この PC のサイトに渡す (ほかは 404)。"""
    d = tunnel_dir(data)
    cred = (d / "tunnel.json").as_posix()
    path = d / "config.yml"
    path.write_text(f"tunnel: {tunnel_id(data)}\ncredentials-file: {cred}\n"
                    f"ingress:\n  - hostname: {domain}\n    service: http://localhost:{port}\n"
                    "  - service: http_status:404\n", encoding="utf-8")
    return path


def start_tunnel(exe: Path, data: Path, domain: str, port: int, popen=subprocess.Popen) -> subprocess.Popen | None:
    """トンネルを動かし続ける (この画面を閉じると止まる)。様子は cloudflared/tunnel.log に書く。"""
    if not tunnel_id(data):
        return None
    config = write_config(data, domain, port)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = open(tunnel_dir(data) / "tunnel.log", "a", encoding="utf-8")  # noqa: SIM115 - トンネルが動いている間ずっと書く
    return popen([str(exe), "tunnel", "--no-autoupdate", "--config", str(config), "run"],
                 stdout=log, stderr=subprocess.STDOUT, **kwargs)
