#!/bin/bash
# Build a throwaway braidctl pool out of loop devices, so the failure and recovery paths can be
# exercised without touching real disks. Everything it creates lives under one directory and is
# removed by `down`.
#
#   sudo ./loop-testbed.sh up    [dir]   # create 10G/10G/15G/5G sparse images, mkfs, mount
#   sudo ./loop-testbed.sh down  [dir]   # unmount, detach, delete
#
# Default dir is /var/tmp/braidctl-test (NOT /tmp — a 13 GiB test file will not fit in tmpfs).
# Writes <dir>/test.toml for use as `braidctl -c <dir>/test.toml ...`.
set -euo pipefail

DIR="${2:-/var/tmp/braidctl-test}"
LABEL=braidtest
SIZES=(10G 10G 15G 5G)

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

loops() { losetup -j "$DIR/$1.img" 2>/dev/null | cut -d: -f1; }

case "${1:-}" in
up)
    mkdir -p "$DIR/mnt"
    devs=()
    for i in "${!SIZES[@]}"; do
        img="$DIR/img$i.img"
        truncate -s "${SIZES[$i]}" "$img"
        devs+=("$(losetup --show -f "$img")")
    done
    echo "loop devices: ${devs[*]}"
    mkfs.btrfs -q -f -L "$LABEL" -d raid1 -m raid1 "${devs[@]}"
    mount -o noatime "LABEL=$LABEL" "$DIR/mnt"
    cat > "$DIR/test.toml" <<EOF
mountpoint = "$DIR/mnt"
label = "$LABEL"
debounce_sec = 5
samba_units = []
state_file = "$DIR/state.json"
# Without this a test run overwrites the real /var/lib/braid/status.json, which is what
# a dashboard and the web view read (and what the web view seeds SMART from).
status_file = "$DIR/status.json"
smart_args = []

[web]
listen = "127.0.0.1:8765"
token_file = "$DIR/web-token"
jobs_dir = "$DIR/jobs"
smart_ttl_sec = 30
idle_exit_sec = 0
[notify]
config_file = "$DIR/no-such-file.json"
EOF
    echo
    echo "pool up. use it with:"
    echo "  sudo braidctl -c $DIR/test.toml status"
    echo "simulate a drive failure with:"
    echo "  sudo umount $DIR/mnt && sudo losetup -d ${devs[3]}"
    ;;
down)
    umount "$DIR/mnt" 2>/dev/null || true
    for img in "$DIR"/img*.img; do
        [ -e "$img" ] || continue
        for d in $(losetup -j "$img" | cut -d: -f1); do losetup -d "$d" || true; done
    done
    rm -rf "$DIR"
    echo "removed $DIR"
    ;;
*)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
