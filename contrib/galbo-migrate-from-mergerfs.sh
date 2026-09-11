#!/bin/bash
# ONE-OFF MIGRATION SCRIPT — NOT A GENERAL TOOL. DO NOT RUN THIS ON YOUR MACHINE.
#
# Kept as a record of how the development machine was converted from a mergerfs union of four
# independently-formatted ext4 drives into a single btrfs raid1 pool. It hardcodes that machine's
# filesystem labels (braid-01..04), mount points, Samba unit names and directory ownership, and it
# runs `wipefs -a` on every drive it finds. Read it, take the shape of it, write your own.
#
# Creating a pool from scratch is three commands; see the README.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "sudo で実行してください" >&2; exit 1; }

# --- 対象ドライブをラベルから解決 -------------------------------------------
PARTS=()
DISKS=()
for n in 01 02 03 04; do
    p=$(blkid -L "braid-$n" 2>/dev/null || true)
    [ -n "$p" ] || { echo "LABEL=braid-$n が見つかりません" >&2; exit 1; }
    d=$(lsblk -no PKNAME "$p")
    PARTS+=("$p"); DISKS+=("/dev/$d")
done
echo "解体対象: ${PARTS[*]}  (ディスク: ${DISKS[*]})"
for d in "${DISKS[@]}"; do
    case "$d" in /dev/sda|/dev/sdc) echo "異常: $d はシステムディスクです。中止" >&2; exit 1;; esac
done
lsblk -o NAME,SIZE,MODEL,LABEL,MOUNTPOINTS "${DISKS[@]}"

# --- 1. 解体 -----------------------------------------------------------------
echo "== Samba 停止"
systemctl stop smbd nmbd
echo "== umount"
umount /srv/pool 2>/dev/null || true
for n in 01 02 03 04; do umount "/mnt/braid/$n" 2>/dev/null || true; done
echo "== fstab から mergerfs/braid-0N 行を削除 (バックアップ: /etc/fstab.backup.*)"
cp -a /etc/fstab "/etc/fstab.backup.$(date +%Y%m%d-%H%M%S)"
sed -i -E '/^# --- braid pool/d; /^LABEL=braid-0[0-9][[:space:]]/d; /fuse\.mergerfs/d' /etc/fstab
systemctl daemon-reload
echo "== 署名の消去"
for p in "${PARTS[@]}"; do wipefs -a "$p"; done
for d in "${DISKS[@]}"; do wipefs -a "$d"; done
rmdir /mnt/braid/0{1,2,3,4} /mnt/braid 2>/dev/null || true

# --- 2. btrfs raid1 作成 -----------------------------------------------------
echo "== mkfs.btrfs raid1"
mkfs.btrfs -f -L braid -d raid1 -m raid1 "${DISKS[@]}"
udevadm settle

echo "== マウントポイント保護 (未マウント時の誤書き込み防止)"
mkdir -p /srv/pool
chattr +i /srv/pool

echo "== fstab 追記"
cat >> /etc/fstab <<'EOF'

# --- braid pool: btrfs raid1 (managed by braidctl) ---
LABEL=braid  /srv/pool  btrfs  noatime,nofail,x-systemd.device-timeout=120  0 0
EOF
systemctl daemon-reload
mount /srv/pool

echo "== 共有ディレクトリ再作成 (所有権・モードは旧構成と同じ)"
mkdir -p /srv/pool/share /srv/pool/timemachine
chown braid:braid /srv/pool/share /srv/pool/timemachine
chmod 2775 /srv/pool/share
chmod 2700 /srv/pool/timemachine

echo "== Samba 再開"
systemctl start smbd nmbd

echo
btrfs filesystem show /srv/pool
btrfs filesystem usage -T /srv/pool | sed -n '1,12p'
df -h /srv/pool
echo "完了"
