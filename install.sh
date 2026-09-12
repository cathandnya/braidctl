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

for u in braid-mount.service braid-monitor.service braid-monitor.timer \
         braid-monitor-udev.service braid-scrub.service braid-scrub.timer \
         braid-web.socket braid-web.service; do
    install -m 644 "$SRC/systemd/$u" "/etc/systemd/system/$u"
done

# The optional web view. Installed but never enabled: a machine that upgrades braidctl
# and does not run the enable command below gains a few dormant files and no port.
install -d -m 755 /usr/local/lib/braidctl /usr/local/share/braidctl/web
install -m 644 "$SRC/braidweb.py" /usr/local/lib/braidctl/braidweb.py
install -m 644 "$SRC"/web/index.html "$SRC"/web/app.css "$SRC"/web/app.js /usr/local/share/braidctl/web/

# The udev rule has to name the pool's filesystem label literally, so stamp in whatever
# the config says rather than assuming the default.
LABEL=$(sed -n 's/^[[:space:]]*label[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' /etc/braid/braid.toml | head -1)
LABEL=${LABEL:-braid}
sed "s/ENV{ID_FS_LABEL}==\"braid\"/ENV{ID_FS_LABEL}==\"$LABEL\"/" \
    "$SRC/systemd/99-braid.rules" > /etc/udev/rules.d/99-braid.rules
chmod 644 /etc/udev/rules.d/99-braid.rules
udevadm control --reload-rules

systemctl daemon-reload
systemctl enable braid-mount.service
systemctl enable --now braid-monitor.timer braid-scrub.timer

echo "udev rule installed for LABEL=$LABEL"

echo "installed: /usr/local/sbin/braidctl, /etc/braid/braid.toml"
systemctl list-timers 'braid-*' --no-pager

cat <<'HINT'

web view installed but not enabled (loopback + token, read-only):
  sudo systemctl enable --now braid-web.socket
  sudo braidctl web --url        # prints http://127.0.0.1:8765/?token=...
Reach it from elsewhere with `ssh -L 8765:127.0.0.1:8765 <host>` or `tailscale serve`.
HINT
