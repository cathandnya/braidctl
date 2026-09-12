# braidctl

Drobo BeyondRAID-style storage pool management for btrfs RAID1 on Linux.

Mix drives of any size into one pool, see a single honest "usable capacity" number, and — when a
drive dies — get your redundancy back **automatically, without plugging in a replacement drive**.

```
$ sudo braidctl status
braid  /srv/pool   PROTECTED (RAID1)
Raw    7.50 TB   Usable    3.75 TB   Used  412.31 GB (11%)   Free    3.34 TB

ID  device   model                       size     alloc  errors  health
 1  sdd      ThinkPlus                2.00 TB 206.16 GB       0  OK 40°C
 2  sde      ThinkPlus                2.00 TB 204.03 GB       0  OK 41°C
 3  sdf      WDC WD25EZRX-00MMMB0     2.50 TB 309.24 GB       0  OK 47°C
 4  sdg      WDC WD10EADS-00L5B1      1.00 TB 105.19 GB       0  WARN reallocated=1 44°C

Any single drive can be lost and re-protected (losing the largest, sdf 2.50 TB,
leaves 2.50 TB usable >= 412.31 GB used)
```

## Why

[Drobo](https://en.wikipedia.org/wiki/Drobo) was liquidated in 2023 with no successor. Three things
made BeyondRAID pleasant to live with:

1. **Any drives, any sizes.** Add a drive you had lying around. Pull one out. Nothing has to match.
2. **One number.** Not "7.5 TB raw across 4 disks in a 2-way mirror" — just *how much can I store*.
3. **Self-healing without a spare.** Lose a drive and the array re-lays-out its data across the
   drives that are left. If what you're storing still fits, you are fully redundant again — before
   you have even ordered a replacement.

btrfs already implements the hard part of (1) and (3): its `raid1` profile mirrors in 1 GiB chunks
rather than whole disks, so member drives can be any size, and `device add` / `device remove`
re-place those chunks online. What's missing is everything Drobo did in firmware around it: the
capacity display, failure detection, the decision of *whether* a rebuild is even possible, and
actually pulling the trigger. That layer is `braidctl`.

## What btrfs gives you vs. what braidctl adds

| | btrfs | braidctl |
|---|---|---|
| Mixed-size drives, chunk-level mirroring | ✅ | |
| Online `device add` / `device remove` | ✅ | |
| Checksums, scrub, self-healing reads | ✅ | |
| "How much can I actually store?" | `btrfs fi usage`, if you know how to read it | one line, plus per-drive *survivability* |
| Notices a drive vanished | an error in `dmesg` | polls, debounces, and tells you |
| Rebuilds after a failure | you type the commands | automatic, if capacity allows |
| Decides a rebuild is *impossible* | — | says so, and tells you how many GB to add |
| Mounts a degraded pool at boot | mount just fails | falls back to `-o degraded` and reports |
| Refuses to eat the wrong disk | `device add` will happily take your system disk | multiple interlocks |

## Usable capacity

With two copies of everything, the ideal usable capacity of a set of drives is

```
usable = min( sum(sizes) / 2 ,  sum(sizes) - max(size) )
```

The second term is the one people forget: a 4 TB drive among three 1 TB drives can only ever be
half-used, because there is nothing big enough to hold the other copy. `braidctl status` reports
btrfs's measured figure rather than this ideal, but uses the formula to answer the question that
actually matters — *if this drive dies, can the rest still hold my data?* — for every drive in the
pool. That is the last line of `status`, and it is the difference between a green light and a red one.

## When a drive dies

```
drive disappears
  └─ notify "drive lost, waiting N seconds"                 (records first-seen-missing time)
     ├─ comes back within debounce_sec (default 600)
     │    └─ remount to re-attach it, then scrub to resync what it missed
     └─ still gone
          ├─ remaining drives can hold the data
          │    └─ compaction balance → btrfs device remove <devid>
          │       (if the kernel won't do that live: stop Samba, remount -o degraded,
          │        remove missing, restart Samba — tens of seconds of downtime)
          │    └─ notify "re-protected". Pool is PROTECTED again on fewer drives.
          └─ they can't
               └─ notify "add %s more of drive" — repeated daily, Drobo's red light.
                  Data stays readable and writable. It is simply no longer redundant.
```

At boot with a drive missing, the normal `fstab` mount fails (harmlessly, thanks to `nofail`) and
the monitor mounts the pool `-o degraded` three minutes in, then follows the same path.

A drive that is dying rather than dead — SMART failures, pending sectors, a rising btrfs error
counter — is reported but not acted on, because pulling a drive that still serves reads is usually
worse than keeping it. Set `auto_evict_on_errors = true` if you disagree.

## Install

Requirements: Linux with systemd, btrfs-progs, Python 3.11+ (for `tomllib`), and `smartctl` for
health reporting. No Python packages — the tool is a single stdlib-only file.

```bash
git clone https://github.com/cathandnya/braidctl
sudo braidctl/install.sh        # /usr/local/sbin/braidctl, /etc/braid/braid.toml, systemd timers
```

Then create a pool, if you don't have one — **this erases the drives**:

```bash
sudo mkfs.btrfs -L braid -d raid1 -m raid1 /dev/sdX /dev/sdY /dev/sdZ
sudo mkdir -p /srv/pool && sudo chattr +i /srv/pool   # see "Safety" below
sudo systemctl start braid-mount.service              # mounts it; enabled at boot by install.sh
sudo braidctl status
```

**Do not add the pool to `/etc/fstab`.** systemd binds an fstab mount to a single backing
device and unmounts the filesystem the moment that device disappears — for a redundant
multi-device pool that is exactly the wrong response to losing a drive, and a harmless USB
bus re-enumeration triggers it too. `braid-mount.service` mounts the pool instead, which
leaves the mount with no device binding at all. The reasoning and the measurements are in
[docs/design.md](docs/design.md).

The pool's filesystem label, the config directory and the systemd units are all named `braid` — the
pool. `braidctl` is the command that manages it. Edit `/etc/braid/braid.toml` if your mount point,
label, or the services to pause during a rebuild differ from the defaults.

## Commands

```bash
sudo braidctl status              # capacity, protection state, per-drive health
sudo braidctl status --json       # same, for scripts
sudo braidctl add /dev/sdX        # add a drive (refuses one with data unless --force), then balance
sudo braidctl remove 3            # remove devid 3 — only if what's left can still hold the data
sudo braidctl remove /dev/sdg     # by path
sudo braidctl remove missing      # evict a dead drive by hand (what monitor does for you)
sudo braidctl monitor             # one detection pass; run from the timer every 5 minutes
sudo braidctl scrub               # verify every block against its checksum
sudo braidctl scrub --ack         # record the last result as reviewed (refused for real damage)
sudo braidctl mount               # mount the pool (--wait N to retry while drives appear)
sudo braidctl mount-degraded      # mount with a drive missing, by hand
```

Replacing a failed drive is therefore: nothing (the monitor evicts it and re-protects the pool),
then `braidctl add` when the new drive arrives. There is no "rebuild window" to sweat through,
because the rebuild already happened.

Notifications go out by email. By default `braidctl` borrows the SMTP block from an existing
nas-monitor-style config; point `notify.config_file` at any JSON
file with a `notification.email` object of `{smtp_server, smtp_port, username, password, from, to,
use_tls}`. Without one, everything still lands in the journal — `journalctl -u braid-monitor`.

## Reading a scrub result

btrfs counts scrub errors by class, and the classes are not equally serious. `braidctl status` says
which it was, because the difference decides whether you have lost data or lost an afternoon:

| Counter | Meaning | braidctl |
|---|---|---|
| `csum`, `read`, `super` | a block could not be read, or did not match its checksum | **fail** — real damage, named per drive. `corrected > 0` with nothing uncorrectable is a **warn**: the mirror repaired it, but a drive handed back bad data |
| `verify` only, device error counters clean | a tree block's header was not what the extent tree expected | **warn — look at it once.** Metadata damage looks like this, and so does a block that was freed and reused while the scrub was still reading it, which is ordinary under heavy write |
| `verify` plus device I/O errors | as above, but the drives agree something went wrong | **fail** |

A `verify`-only result on a busy pool is a question, not a verdict. Re-scrub while the pool is idle
to settle it; if you have already checked the data and found it sound, record that:

```bash
sudo braidctl scrub --ack
```

The acknowledgement is bound to that scrub run and that result, so the next scrub reporting anything
different raises it again. Real damage (`csum`/`read`/`super`) cannot be acknowledged away — the
command refuses. Nothing is deleted either way: `btrfs scrub status` keeps its own record.

## Safety

Destroying an array by mistyping a device node is a real risk for a tool like this, so:

- **`add` refuses** the system disk, anything mounted, anything with a holder (LUKS, LVM, MD),
  anything matching `protected_devices`, and — without `--force` — anything carrying a filesystem
  signature or partitions.
- **`remove` refuses** if the remaining drives couldn't hold the data, and says how much to add.
- **A returning drive is never silently re-added.** If it still belongs to the pool it is
  re-attached and resynced; if it was already evicted, you get a note suggesting `add --force`,
  and nothing is touched.
- **The mount point is immutable.** `chattr +i /srv/pool` means that if the pool is *not* mounted,
  writes to that path fail instead of quietly filling your root filesystem — a classic way to lose
  a NAS's data to a full `/`.
- **One mutation at a time.** `monitor` holds a lock; `add`/`remove` refuse while a balance runs.
- **Rebuilds are debounced.** A USB bus glitch that clears in seconds must not trigger a re-layout.
- **A udev rule watches members appearing and disappearing.** After a bus re-enumeration every
  drive comes back under a new device node, which leaves the mount pointing at dead ones and
  silently discarding writes until it is remounted; the rule shortens that from the timer's five
  minutes to under a second. And if so many devices vanish that the pool can no longer place a
  write, `braidctl guard` unmounts it, so writes fail loudly rather than being accepted and
  thrown away. Losing a single drive from a healthy `raid1` deliberately does nothing — see
  [docs/design.md](docs/design.md).

## Limitations

- **`raid1` means two copies, not two *drives* of tolerance.** Any single drive can die. Two cannot.
  Note that several USB enclosures present two disks as one device — if that enclosure fails, you
  lose both, and btrfs has no idea they were related. Either split them across enclosures, or use
  three copies: `btrfs balance start -dconvert=raid1c3 -mconvert=raid1c3 /srv/pool` (usable capacity
  drops to roughly a third of raw). `braidctl` reports and rebuilds `raid1c3` pools too.
- `raid5`/`raid6` are deliberately unsupported: btrfs's parity RAID still has the write hole.
- A rebuild that needs the degraded-remount fallback interrupts file sharing for tens of seconds.
- Rebuilds and balances are I/O-heavy; on USB, everything else gets slow while they run.
- Above ~90% full, `balance` and `device remove` start failing with `ENOSPC`. `status` warns.
- The pool may keep the `degraded` mount option after a successful rebuild until it's next
  remounted. Harmless.
- Capacities are decimal (1 TB = 10¹² bytes), like the drive labels and like Drobo. `df` uses TiB
  and will always look smaller.

## Status

Developed and tested on one machine — Ubuntu 26.04, 4 mixed USB drives (1.8/1.8/2.3/0.9 TB) serving
Samba and Time Machine. Drive-loss, auto-rebuild, re-add, scrub and the capacity-refusal paths are
exercised on real hardware and on loop devices; see [docs/testing.md](docs/testing.md) to reproduce
any of it before trusting it with data. Treat it as what it is: a small tool doing a sharp thing to
your disks. Have backups.

See [docs/design.md](docs/design.md) for why btrfs RAID1, and
[docs/operations.md](docs/operations.md) for the day-to-day runbook.

## License

MIT
