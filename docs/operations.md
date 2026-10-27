# Operations

Day-to-day runbook. Everything here assumes `braidctl` is installed and the timers are enabled.

## Checking on the pool

```bash
sudo braidctl status
```

The last line is the one to read. It is one of:

| Line | Meaning |
|---|---|
| `Any single drive can be lost and re-protected` | Green. Nothing to do |
| `Warning: losing <dev> cannot be re-protected …` | Amber. The pool survives that drive's death, but would stay non-redundant until you add capacity. Consider adding a drive |
| `! N drive(s) missing. Can re-protect with what remains` | The monitor is about to fix this, or is fixing it |
| `!! N drive(s) missing. Cannot re-protect …  add <size> more` | Red. Add a drive |

Also useful:

```bash
sudo braidctl status --json | jq .          # machine-readable
journalctl -u braid-monitor -n 50           # what the monitor has been doing
journalctl -u braid-monitor -f              # follow a rebuild
sudo btrfs filesystem usage -T /srv/pool    # per-device allocation, raw btrfs view
sudo btrfs balance status /srv/pool         # is a rebuild/balance running?
sudo btrfs scrub status /srv/pool           # last scrub result
systemctl list-timers 'braid-*'
```

## The web UI

Optional, off until you turn it on, and nothing about the pool depends on it.

```bash
sudo systemctl enable --now braid-web.socket   # pid 1 holds the socket; nothing is resident
sudo braidctl web --url                        # the URL, with its token
ssh -L 8765:127.0.0.1:8765 nas                 # how to reach it from a laptop or phone
```

It shows what `braidctl status` shows, and runs what you would otherwise type: scrub, a
monitor pass, add, remove, and mount / mount-degraded when the pool is down. Every button
opens one dialog naming the exact command, because that is the point — it runs
`braidctl remove 4`, which you could have typed yourself.

Nothing runs inside a request. Each action starts a transient systemd unit
(`braid-job-<id>`) owned by pid 1, so a remove that takes hours survives the page being
closed, the server being stopped, or the session idling out. Progress and the log come
back through `/var/lib/braid/jobs/<id>.{json,log}`; `journalctl -u 'braid-job-*'` has the
same output.

Refusals come from the CLI's own interlocks, so the UI cannot offer what the CLI would
decline: a drive that is the system disk, mounted, held by LUKS/LVM, matching
`protected_devices`, or already a member is listed with that reason and no button. A
remove that would not leave room for the data is shown disabled with the shortfall.

`add --force` — the one irreversible operation, since it runs `wipefs` — is not reachable
from the browser unless `[web] allow_force_add = true` (the same switch offers `wipe-stale` on
a stale member's banner), and even then it asks you to type
back the drive's serial (or model and size) before it will run. The recommended way to
admit a used disk is still a terminal:

```bash
sudo braidctl add --force /dev/sdX     # while looking at lsblk
```

Anything long-running on the pool is listed under Running with a Cancel button, whoever
started it — a job from the page, the scrub timer, a balance, someone
at a terminal. The button runs `braidctl cancel scrub|balance|remove`, which is the same
thing from a shell, and it runs inside the request (it returns in seconds) rather than as a
job, because the job slot is held by the thing being stopped.

| Stopping | What is left | |
|---|---|---|
| scrub | progress kept | `btrfs scrub resume /srv/pool` continues it |
| balance | the chunks already moved; still raid1 | just less evenly filled |
| device remove | the drive, still a member, with its size back | data already moved stays moved, mirrored |

It refuses what braidctl would undo or suffer from: a remove or balance the monitor is
running to re-protect the pool (the next pass would start it again), the compaction
balance inside a remove (cancel the remove), and a balance converting chunks back to
raid1, which would leave data written while degraded on one drive with nothing to restart it.

To take the whole thing away, `read_only = true` refuses every mutation at the
dispatcher, and `allow_mount_actions = false` drops the mount buttons.

The token lives in `/etc/braid/web-token` (0600, created on first start). Rotating it is
`rm` plus a restart, which invalidates every open session:

```bash
sudo rm /etc/braid/web-token
sudo systemctl restart braid-web.socket
```

Evidence lives where the rest of braidctl's does — `journalctl -u braid-web`. Refusals are
logged by class (`bad-host`, `bad-credential`), never with the value that was offered.

If anything about it looks wrong, `sudo systemctl stop braid-web.socket`. Nothing about the
pool depends on it.

## Adding a drive

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,LABEL,MOUNTPOINTS    # identify it, carefully
sudo braidctl add /dev/sdX
```

`add` refuses a drive that carries any filesystem signature or partition table. That refusal is the
point — it is the last thing standing between a typo and somebody's other disk. If the drive really
is scrap, repeat with `--force`, which wipes signatures before adding.

`add` moves no data. Usable capacity rises immediately, and new chunks go to the drives with the
most unallocated space, so the new drive takes new writes first. To spread what is already there
over every drive, run a balance when the hours of I/O suit you:

```bash
sudo braidctl balance             # or Balance now on the page; `braidctl cancel balance` stops it
```

`status` reports `REBUILDING` while it runs. The pool is fully usable and protected throughout, and a
cancelled balance leaves it that way, just less evenly filled. If a drive is lost (or fails SMART
with `auto_evict_on_errors`) while it runs, the monitor cancels the balance and re-protects the pool;
start it again afterwards.

`add` refuses while the devid it would be given was removed earlier in the same mount (a `remove
missing`, or the monitor's re-protection, when the evicted drive had the highest devid). On Linux 7.0
the kernel leaves that devid's sysfs entry behind, the add reuses the devid, and reading the entry
afterwards panics the kernel. Remount the pool or reboot, then add.

When the old drives are fully allocated, the new one alone cannot take a raid1 chunk (each needs
room on two drives), so its space is not writable until a balance has run. `add` says so. Before this, `add` started the
balance itself in the background; from the web view that balance was killed along with the job
seconds later, so the two ways of adding behaved differently.

## Removing a drive on purpose

```bash
sudo braidctl remove 3            # devid, from status
sudo braidctl remove /dev/sdg     # or path
```

This relocates everything off the drive first, so it takes a while and the drive is safe to unplug
when it returns. If the remaining drives could not hold the data, the command refuses and tells you
how much capacity to add first — it will not start a job that cannot finish.

## A drive fails

**Do nothing.** That is the design. Within five minutes the monitor notices, waits
`debounce_sec` (default ten minutes) in case it was a loose cable, then either re-protects the pool
on the remaining drives or emails you that it can't.

To watch it happen: `journalctl -u braid-monitor -f`.

To skip the wait, because you know the drive is gone:

```bash
sudo braidctl monitor --debounce 0
```

Once the pool reports `PROTECTED` again, the dead drive is no longer a member and the pool is
smaller. Replacing it is just `braidctl add` whenever the new drive arrives — there is no urgency
and no degraded window, because the rebuild is already done.

If instead you got *"Cannot re-protect"*: the data is intact and still readable and writable, but
there is no second copy. Add a drive of at least the size named in the message, then the monitor
will complete the rebuild on its next pass.

## A drive comes back

Plug it in. The next monitor pass re-attaches it and starts a scrub to rewrite whatever it missed
while it was away. Nothing to type.

If it had already been evicted — you got the "re-protected" email, and only then plugged it back in
— it is no longer a member. It still carries the pool's superblock: btrfs evicted it while it was
absent and could not scratch it. braidctl goes by the filesystem's own device list, so it is not
counted, the pool mounts normally, and `status` shows a `Stale member:` line for it; the monitor
sends a one-off note naming it. Erase it before using it for anything else:

```bash
sudo braidctl wipe-stale /dev/sdX     # refuses anything that is not a stale member of this pool
sudo braidctl add --force /dev/sdX    # or put it back in the pool
```

## Boot with a drive missing

`braid-mount.service` runs `braidctl mount --wait 120` at boot: it retries quietly while USB
enumeration settles, then falls back to a degraded mount and emails you. It is ordered before
`smbd`, so file sharing does not start against an unmounted pool — and the immutable mount point
means that even if something does, it fails loudly instead of writing to the root filesystem.

To do it by hand:

```bash
sudo braidctl mount               # normal, falling back to degraded
sudo braidctl mount-degraded      # degraded only
```

The pool is deliberately absent from `/etc/fstab`; see [design.md](design.md) for why. This also
means `mount /srv/pool` will not work — use `braidctl mount`.

## The pool went away entirely

If enough drives disappear that btrfs cannot place a write — the whole enclosure lost power, the
USB bus reset — `braidctl guard` unmounts the pool on purpose, and you will have an email saying
so. That is deliberate: a mount with no working devices accepts writes and discards them without
an error, so removing the mount is the only way the failure becomes visible.

Nothing to do. When the drives come back, udev triggers a monitor pass and the pool is remounted
within about a second. `journalctl -u braid-guard -u braid-monitor` shows both halves.

## Scrubbing

A scrub reads every block and verifies it against its checksum, repairing from the good copy when
they disagree. It runs monthly from `braid-scrub.timer`; a failure emails you, a clean run is quiet
by default.

```bash
sudo braidctl scrub               # run one now (blocks until finished)
sudo btrfs scrub status -R /srv/pool     # the per-class counters braidctl judges on
sudo btrfs scrub status -R -d /srv/pool  # ... per device
```

Run one after any event that interrupted writes — a power loss, an unplanned removal, a drive that
came back.

`braidctl status` prints a last-scrub line with its reading of the result. `csum`, `read` or `super`
errors are damage. A `verify`-only result with clean device counters is not, by itself, evidence of
damage: a block freed and reused while the scrub was reading it produces exactly the same counter,
and that is ordinary on a pool taking a large backup. It is reported as a warning that asks for one
human look, not as a failure.

To settle one, re-scrub when the pool is idle. To check a specific block the kernel named, the
journal gives you the file:

```
BTRFS warning (device sda): scrub: header error at logical ... root 5 inode 2584 ... (path: timemachine/...)
```

Read that file back — btrfs verifies every block it returns, so a clean read of both copies is
proof the data is sound. btrfs picks the mirror by PID parity, so read it more than once:

```bash
sync && echo 1 | sudo tee /proc/sys/vm/drop_caches
sudo dd if=/srv/pool/<the path> of=/dev/null bs=1M     # repeat; different PIDs hit different mirrors
sudo btrfs device stats /srv/pool                      # must stay at zero
```

If it reads clean, record that instead of leaving the pool amber until the next monthly scrub:

```bash
sudo braidctl scrub --ack
```

The acknowledgement names that scrub run and that result; a later scrub reporting anything different
raises it again. `braidctl scrub --ack` refuses when the result is real damage.

## Replacing the whole pool's configuration

`/etc/braid/braid.toml` is read on every invocation; there is no daemon to restart. The values worth
knowing:

| Key | Default | Notes |
|---|---|---|
| `mountpoint`, `label` | `/srv/pool`, `braid` | Must match the filesystem label and `braid-mount.service` |
| `debounce_sec` | `600` | Grace period before a rebuild. Raise it for flaky USB |
| `rebuild_margin` | `0.95` | Fraction of post-loss usable capacity allowed to be occupied |
| `warn_percent` | `90` | Capacity warning threshold |
| `chunk_headroom_warn` | `16 GiB` | Unallocated space left on the device a new chunk must land on, below which `status` warns |
| `chunk_headroom_fail` | `2 GiB` | Same, below which allocation is about to fail outright |
| `samba_units` | `["smbd"]` | Stopped only for the degraded-remount fallback |
| `auto_evict_on_errors` | `false` | Evict drives that are failing but still present |
| `protected_devices` | `[]` | Substrings of `/dev/disk/by-id` names that `add` must always refuse |
| `notify.config_file` | nas-monitor's config | Any JSON with a `notification.email` object |

## Converting to three copies

Two copies tolerate one drive. If two of your drives share a USB enclosure — or a power brick, or a
controller — that is one failure taking out two members, which `raid1` cannot survive:

```bash
sudo btrfs balance start -dconvert=raid1c3 -mconvert=raid1c3 /srv/pool
```

Usable capacity drops to roughly a third of raw. `braidctl` reports and rebuilds `raid1c3` pools
normally. Converting back is the same command with `raid1`.

## When something is genuinely wrong

```bash
sudo btrfs filesystem show /srv/pool        # what does btrfs think the members are?
sudo btrfs device stats /srv/pool           # per-device error counters (never reset by themselves)
sudo dmesg | grep -i btrfs | tail -40
sudo smartctl -a -d sat /dev/sdX            # -d sat is needed behind most USB-SATA bridges
```

If the pool will not mount at all and `mount -o degraded` also fails, stop and do not experiment.
`btrfs rescue` and `btrfs restore` read data out of a filesystem that cannot be mounted; both are
safer than anything that writes. `btrfs check --repair` is the last resort, not the first.
