#!/bin/bash
# Install braidctl and its systemd timers.
#
#   sudo ./install.sh
#
# Does not create or modify a pool — see the README for that. An existing
# /etc/braid/braid.toml is left alone.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "$SRC/braidctl.py" /usr/local/sbin/braidctl
mkdir -p /etc/braid /var/lib/braid
[ -e /etc/braid/braid.toml ] || install -m 644 "$SRC/braid.toml.example" /etc/braid/braid.toml

for u in braid-mount.service braid-monitor.service braid-monitor.timer braid-scrub.service braid-scrub.timer; do
    install -m 644 "$SRC/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable braid-mount.service
systemctl enable --now braid-monitor.timer braid-scrub.timer

echo "installed: /usr/local/sbin/braidctl, /etc/braid/braid.toml"
systemctl list-timers 'braid-*' --no-pager
