# Testing

A tool that removes devices from a live filesystem should not be taken on faith. Everything below
can be reproduced on loop devices in a few minutes, without risking a disk.

## Loop-device testbed

```bash
sudo contrib/loop-testbed.sh up          # 10G + 10G + 15G + 5G, btrfs raid1, LABEL=braidtest
sudo braidctl -c /var/tmp/braidctl-test/test.toml status
```

The generated `test.toml` points `braidctl` at the test pool, shortens the debounce to five seconds,
stops it from touching any services, and sends notifications nowhere (they still appear in the log).
Tear it down with `sudo contrib/loop-testbed.sh down`.

Fill it to a realistic level first — with almost nothing stored, every capacity check passes and the
interesting refusals never trigger:

```bash
sudo dd if=/dev/zero of=/var/tmp/braidctl-test/mnt/data.bin bs=1M count=13000 status=none && sync
```

Throughout, `T=/var/tmp/braidctl-test` and `C="-c $T/test.toml"`.

### Losing a drive

```bash
sudo umount $T/mnt && sudo losetup -d /dev/loopN     # loopN = the 5G one, from losetup -a
sudo braidctl $C monitor                             # mounts -o degraded, reports the loss
sudo braidctl $C monitor --debounce 0                # rebuilds onto the survivors
sudo braidctl $C status                              # PROTECTED again, on three devices
```

### Losing a drive the pool cannot afford to lose

Same thing, but detach the 15G device while 13 GiB is stored. The remaining 10+10+5 GiB can hold
12.5 GiB at two copies, which is less than what's stored, so:

```
NOTIFY Drive addition required (cannot re-protect)
Missing drive(s):
  - devid 3 WDC ... 16.11 GB (loop5)

Remaining usable capacity 10.74 GB is less than the 13.65 GB in use, so redundancy cannot be
restored as things stand.
Add at least 3.63 GB more: sudo braidctl add /dev/sdX
The data is still readable and writable, but losing one more drive loses it.
```

and nothing is removed. The pool stays mounted, readable and writable. The message repeats once a
day until it is resolved.

### A drive coming back

```bash
sudo losetup -f $T/img3.img && sudo udevadm trigger   # reattach before the debounce expires
sudo braidctl $C monitor                              # remounts to re-attach it, starts a scrub
```

Once a drive has actually been evicted, reattaching it produces a note that it is a stale member and
no action — `braidctl $C add --force /dev/loopN` puts it back.

### Refusals

```bash
sudo braidctl $C remove 3        # the 15G device: refused, names the shortfall
sudo braidctl $C remove 4        # the 5G device: proceeds, relocating data first
sudo braidctl $C add /dev/loopN  # a device with a btrfs signature: refused
sudo braidctl $C add --force /dev/loopN
sudo braidctl $C scrub
```

## Simulating failure on real hardware

Do not use `/sys/bus/usb/drivers/usb/unbind` — many enclosures present two disks behind one USB
device, and unbinding takes out both. Detach a single disk at the SCSI layer instead:

```bash
echo 1 | sudo tee /sys/block/sdX/device/delete            # disappears, exactly like unplugging
echo '- - -' | sudo tee /sys/class/scsi_host/hostN/scan   # bring it back (hostN from the readlink below)
readlink -f /sys/block/sdX/device                         # find hostN before you delete it
```

## Results

Recorded on the development machine: Ubuntu 26.04, Linux 7.0, btrfs-progs 6.17.1, four USB drives of
1.82 / 1.82 / 2.27 / 0.91 TiB serving Samba.

| Scenario | Where | Result |
|---|---|---|
| Mixed-size pool, capacity reporting | hardware | 6.82 TiB raw, 3.41 TiB usable — matches `min(sum/2, sum−max)` |
| Data spread across devices | hardware | 10 GiB written; two copies landed on the two emptiest devices |
| Drive loss, pool stays online | hardware | `/sys/block/sdg/device/delete`; 512 MiB written afterwards, no error |
| Detection | hardware | Reported on the next monitor pass, drive identified by model and size from the inventory |
| **Automatic rebuild** | hardware | `device remove` succeeded **live** — no unmount, Samba never stopped. PROTECTED on three drives |
| Scrub after rebuild | hardware | No errors |
| Evicted drive returning | hardware | Reported as a stale member; `add` refused it, `add --force` re-admitted it; balance restored 3.75 TB usable |
| SMART reporting | hardware | Flagged `reallocated=1` on a 46,000-hour drive; suppressed on later passes as it was not increasing |
| Rebuild refused for lack of capacity | loop | Correct shortfall reported, nothing removed |
| Drive returning before eviction | loop | Re-attached by remount, scrub started |
| `remove` refused / accepted | loop | Refused the drive that would not fit, relocated and removed the one that would |
| `add` refusal and `--force` | both | Signature detected, refused, forced through |
| Degraded mount at boot | loop | Mounts `-o degraded` when the normal mount fails |

Not yet exercised: a full reboot with a drive absent, and long-term behaviour under a real Time
Machine workload.
