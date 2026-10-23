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


def stop_leftovers(exe: Path, run: Runner = subprocess.run) -> None:
    """前に起動したときのトンネルが残っていたら止める (Windows)。

    前の版では、黒い画面を閉じてもトンネルだけが残り、サイトがないのに Cloudflare からの通信を受けて
    「Bad gateway (502)」になっていた。データのフォルダに置いた cloudflared だけを止める。
    """
    if sys.platform != "win32" or exe.parent.name != "cloudflared":
        return
    run(["taskkill", "/F", "/IM", exe.name], check=False, capture_output=True)


def _close_with_this_window(proc: subprocess.Popen) -> None:
    """Windows: この黒い画面 (Python) が終わったら、トンネルも一緒に終わるようにする (ジョブにまとめる)。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Limits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("Basic", Limits), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    info = ExtendedLimits()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = 拡張の制限
    kernel32.AssignProcessToJobObject(job, int(proc._handle))
    _JOBS.append(job)  # 手放すとジョブが閉じて、トンネルが止まってしまう


_JOBS: list = []


def start_tunnel(exe: Path, data: Path, domain: str, port: int, popen=subprocess.Popen) -> subprocess.Popen | None:
    """トンネルを動かし続ける (この画面を閉じると止まる)。様子は cloudflared/tunnel.log に書く。"""
    if not tunnel_id(data):
        return None
    config = write_config(data, domain, port)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = open(tunnel_dir(data) / "tunnel.log", "a", encoding="utf-8")  # noqa: SIM115 - トンネルが動いている間ずっと書く
    proc = popen([str(exe), "tunnel", "--no-autoupdate", "--config", str(config), "run"],
                 stdout=log, stderr=subprocess.STDOUT, **kwargs)
    if sys.platform == "win32":
        try:
            _close_with_this_window(proc)
        except Exception as exc:  # noqa: BLE001 - できなくてもトンネルは動かす (次の起動で前のものを止める)
            print(f"（トンネルを画面と一緒に止める設定ができませんでした: {exc}）")
    return proc


def diagnose(data: Path, domain: str, exe: Path | None, *, resolve=None, fetch=None) -> list[str]:
    """自分のドメインで開けないとき、どこで止まっているかを 1 つずつ確かめる (画面に出して、ファイルにも残す)。"""
    import socket
    import urllib.request

    resolve = resolve or (lambda host: sorted({a[4][0] for a in socket.getaddrinfo(host, 443)}))

    def default_fetch(url: str) -> int:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - 自分のサイトだけ
            return r.status

    fetch = fetch or default_fetch
    d = tunnel_dir(data)
    lines = [f"Cloudflare の診断（{domain}）"]

    def check(ok: bool, good: str, bad: str) -> bool:
        lines.append(("✅ " + good) if ok else ("❌ " + bad))
        return ok

    check(exe is not None, f"cloudflared のプログラム: {exe}", "cloudflared のプログラムがありません（ダウンロードできなかった・ウイルス対策に消された）")
    check((d / "cert.pem").exists(), "Cloudflare のログイン（Authorize）: 済み",
          "Cloudflare のログイン（Authorize）がまだです。ブラウザで geaspec.com を選んで「Authorize」を押してください")
    tid = tunnel_id(data)
    check(bool(tid), f"トンネル: {tid}", "トンネルがまだ作られていません")
    try:
        addresses = resolve(domain)
    except OSError:
        addresses = []
    check(bool(addresses), f"DNS（住所の登録）: {', '.join(addresses[:2])}",
          f"DNS（住所の登録）がありません。{domain} をトンネルに向ける設定（route dns）ができていません")
    log = d / "tunnel.log"
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:] if log.exists() else []
    connected = any("Registered tunnel connection" in line for line in tail)
    check(connected, "トンネルの接続: Cloudflare につながっています",
          "トンネルが Cloudflare につながっていません" + ("（下の記録を見てください）" if tail else "（記録がありません）"))
    try:
        status = fetch(f"https://{domain}/healthz")
    except OSError as exc:
        status = 0
        lines.append(f"　（開いたときのエラー: {exc}）")
    check(status == 200, f"https://{domain} がインターネットから開けます", f"https://{domain} がまだ開けません")
    if tail and not connected:
        lines += ["--- cloudflared の記録（最後の 5 行） ---", *tail[-5:]]
    try:
        (d / "diagnosis.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return lines
