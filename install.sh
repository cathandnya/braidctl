#!/bin/bash
# braidctl と systemd ユニットを配置して timer を有効にする。
#   sudo ~/rc/braid/install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "sudo で実行してください" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "$SRC/braidctl.py" /usr/local/sbin/braidctl
mkdir -p /etc/braid /var/lib/braid
[ -e /etc/braid/braid.toml ] || install -m 644 "$SRC/braid.toml.example" /etc/braid/braid.toml

for u in braid-monitor.service braid-monitor.timer braid-scrub.service braid-scrub.timer; do
    install -m 644 "$SRC/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now braid-monitor.timer braid-scrub.timer

echo "installed: /usr/local/sbin/braidctl, /etc/braid/braid.toml"
systemctl list-timers 'braid-*' --no-pager
