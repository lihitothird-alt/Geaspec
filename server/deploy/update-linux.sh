#!/usr/bin/env bash
# クラウドのサーバーの Geaspec を、新しい版が出ていたら入れ替える。
# setup-linux.sh が /opt/geaspec/update.sh に置き、systemd のタイマーが 30 分ごとに動かす。
#   使い方: update.sh <データのフォルダ> <動かすユーザー>
# 予約で使われている間 (もうすぐ始まる予約があるときも) は入れ替えず、次の回にまた確かめる。
set -euo pipefail

REPO="https://github.com/lihitothird-alt/Geaspec"
APP="/opt/geaspec"
SERVICE="geaspec-site"
DATA="${1:?データのフォルダを指定してください}"
ME="${2:?動かすユーザーを指定してください}"

TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" | sed 's#.*/tag/##')"
case "$TAG" in agent-v*) ;; *) echo "最新版がわかりませんでした ($TAG)"; exit 0 ;; esac
NEW="${TAG#agent-v}"
CUR="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$APP/src/server/app/version.py" 2>/dev/null || true)"
if [ "$NEW" = "$CUR" ] || [ "$(printf '%s\n%s\n' "$CUR" "$NEW" | sort -V | tail -1)" != "$NEW" ]; then
  exit 0
fi

if ! "$APP/venv/bin/python" - "$APP/src/server" "$DATA/geaspec.db" <<'PY'
import pathlib
import sys

sys.path.insert(0, sys.argv[1])
try:
    import auto_update
except ImportError:  # 0.7.31 より前の版には、予約を確かめる部品がない
    sys.exit(0)

sys.exit(1 if auto_update.bookings_busy(pathlib.Path(sys.argv[2])) else 0)
PY
then
  echo "新しい版 $NEW が出ていますが、予約で使われているので、あとで入れ替えます"
  exit 0
fi

echo "新しい版 $NEW に入れ替えます (いまの版: ${CUR:-不明})"
as_me() { runuser -u "$ME" -- "$@"; }
as_me rm -rf "$APP/src.new"
as_me git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" "$REPO.git" "$APP/src.new"
as_me "$APP/venv/bin/pip" install -q --disable-pip-version-check -r "$APP/src.new/server/requirements.txt"
as_me rm -rf "$APP/src.old"
as_me mv "$APP/src" "$APP/src.old"
as_me mv "$APP/src.new" "$APP/src"
if [ -f "$APP/src/server/deploy/update-linux.sh" ]; then install -m 755 "$APP/src/server/deploy/update-linux.sh" "$APP/update.sh"; fi
systemctl restart "$SERVICE"
echo "新しい版 $NEW にしました"
