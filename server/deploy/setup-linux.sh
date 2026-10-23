#!/usr/bin/env bash
# Geaspec の予約サイトと Discord ボットを、Linux のサーバー (Google Cloud の無料の e2-micro など) で 24 時間動かす。
#
# サーバーの SSH 画面で、次の 2 行を貼り付けて Enter:
#   curl -fsSLO https://raw.githubusercontent.com/lihitothird-alt/Geaspec/main/server/deploy/setup-linux.sh
#   bash setup-linux.sh lg
#
# 自分のドメイン (geaspec.com など) を設定していれば、Cloudflare で公開するので Tailscale はいらない。
# ドメインがないときだけ Tailscale で公開する。最後の「lg」は、そのときのアドレス https://lg.〇〇.ts.net の「lg」の部分
# (先に Tailscale の管理画面で、前の PC の名前を変えておく)。
#
# - いまの PC のデータ (%LocalAppData%\GeaspecSite を zip にしたもの) を GeaspecSite.zip という名前で
#   ホームに置いておくと、予約・アカウント・テスターの記録をそのまま引き継ぐ
# - 新しい版が出たら、予約のない時間に自動で入れ替わる (手で入れ替えるときは、同じ 2 行をもう一度貼る)
set -euo pipefail

REPO="https://github.com/lihitothird-alt/Geaspec"
NAME="${1:-lg}"
ME="$(id -un)"
APP="/opt/geaspec"
DATA="$HOME/GeaspecSite"
SERVICE="geaspec-site"

say() { printf '\n=== %s\n' "$*"; }

say "1/6 メモリの予備 (スワップ) を用意します"
if ! swapon --show | grep -q .; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

say "2/6 必要なソフトを入れます"
sudo apt-get update -y -qq
sudo apt-get install -y -qq python3-venv git unzip curl >/dev/null

say "3/6 Geaspec の最新版を取ってきます"
TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" | sed 's#.*/tag/##')"
case "$TAG" in agent-v*) ;; *) echo "最新版がわかりませんでした ($TAG)"; exit 1 ;; esac
echo "版: $TAG"
sudo mkdir -p "$APP"
sudo chown "$ME" "$APP"
rm -rf "$APP/src.new"
git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" "$REPO.git" "$APP/src.new"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/src.new/server/requirements.txt"
rm -rf "$APP/src.old"
[ -d "$APP/src" ] && mv "$APP/src" "$APP/src.old"
mv "$APP/src.new" "$APP/src"

say "4/6 データを用意します"
mkdir -p "$DATA"
if [ -f "$HOME/GeaspecSite.zip" ] && [ ! -f "$DATA/geaspec.db" ]; then
  tmp="$(mktemp -d)"
  unzip -q "$HOME/GeaspecSite.zip" -d "$tmp"
  conf="$(find "$tmp" -name site_config.json -print -quit)"
  if [ -z "$conf" ]; then
    echo "GeaspecSite.zip の中に site_config.json がありません。%LocalAppData%\\GeaspecSite フォルダを zip にしてください"
    exit 1
  fi
  cp -a "$(dirname "$conf")"/. "$DATA"/
  rm -rf "$tmp"
  echo "前の PC のデータを引き継ぎました"
fi
# まだ決めていない設定 (はじめてなら全部、新しい版で増えたもの) を聞き、自分のドメインがあれば Cloudflare の許可をもらう
echo "設定を確かめます（聞かれたら答えてください）"
(cd "$APP/src/server" && LOCALAPPDATA="$HOME" "$APP/venv/bin/python" site_launcher.py --setup </dev/tty)

DOMAIN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("domain", ""))' "$DATA/site_config.json")"
if [ -n "$DOMAIN" ]; then
  # 自分のドメイン (Cloudflare) で公開するので、Tailscale はいらない
  say "5/6 https://$DOMAIN で公開します（Cloudflare）"
  DNS="$DOMAIN"
else
  say "5/6 Tailscale でインターネットに公開する準備をします"
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  if ! tailscale status >/dev/null 2>&1; then
    echo "下に出るリンクをブラウザで開いて、いつもの Tailscale のアカウントでログインしてください"
    sudo tailscale up --hostname="$NAME"
  fi
  sudo tailscale set --operator="$ME"
  # 公開の許可は、はじめての 1 回だけブラウザで求められることがある (リンクが出たら開いて許可する)
  tailscale funnel --bg 8000
  DNS="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  echo "サイトのアドレス: https://$DNS"
  case "$DNS" in
    "$NAME".*) ;;
    *) echo "【注意】アドレスが「$NAME.〜」になっていません。Tailscale の管理画面で前の PC の名前を変えてから、"
       echo "        このサーバーの名前を「$NAME」に変えてください (貸し手のアプリが見つけられないため)" ;;
  esac
fi

say "6/6 自動で起動するようにします"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Geaspec booking site and Discord bot
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$APP/src/server
Environment=PYTHONUNBUFFERED=1
Environment=LOCALAPPDATA=$HOME
ExecStart=$APP/venv/bin/python site_launcher.py
StandardInput=null
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
# 新しい版が出たら、予約のない時間に自動で入れ替える (30 分ごとに確かめる)
sudo install -m 755 "$APP/src/server/deploy/update-linux.sh" "$APP/update.sh"
sudo tee "/etc/systemd/system/geaspec-update.service" >/dev/null <<UNIT
[Unit]
Description=Update Geaspec to the latest release

[Service]
Type=oneshot
ExecStart=$APP/update.sh $DATA $ME
UNIT
sudo tee "/etc/systemd/system/geaspec-update.timer" >/dev/null <<UNIT
[Unit]
Description=Check for a new Geaspec release every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE"
sudo systemctl enable -q --now geaspec-update.timer
sudo systemctl restart "$SERVICE"
ok=""
for _ in $(seq 1 30); do
  sleep 5
  if curl -fsS -o /dev/null http://127.0.0.1:8000/healthz; then ok=1; break; fi
done
if [ -n "$ok" ]; then
  echo
  echo "できました。サイトとボットが動いています: https://$DNS"
  echo "このサーバーが動いている限り、PC を消しても止まりません。"
  echo "前の PC の GeaspecSite (黒い画面) は、もう起動しないでください (予約が 2 か所に分かれてしまいます)。"
else
  echo "うまく起動しませんでした。次の画面を運営 (開発) に送ってください:"
  sudo journalctl -u "$SERVICE" -n 40 --no-pager
  exit 1
fi
