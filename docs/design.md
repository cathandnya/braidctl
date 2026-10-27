# Design

Why this tool exists, why it sits on btrfs, and how the automatic rebuild works.

## The requirement

A replacement for Drobo's BeyondRAID, reduced to four properties:

1. Pool drives of differing sizes together.
2. Report raw capacity, usable capacity, and used capacity.
3. When a drive is removed or fails, recover redundancy automatically — using the drives that
   remain, as long as the data still fits in what they can hold.
4. Rebuild appropriately when drives are added or removed.

Property 3 is what separates BeyondRAID from ordinary RAID. Conventional arrays treat a failure as
a hole to be plugged: the array stays degraded, emulating the dead member, until a replacement
arrives. BeyondRAID instead *re-lays-out* onto the survivors. If you were storing 400 GB in a pool
that can still hold 2 TB after the failure, there is nothing to wait for — the array is fully
redundant again in the time it takes to copy 400 GB.

## Evaluating the options

| Candidate | Mixed sizes | Capacity reporting | Auto re-redundancy on survivors | Add/remove | Verdict |
|---|---|---|---|---|---|
| **btrfs `raid1`** | ✅ 1 GiB chunks, two copies on two different devices | ✅ via `btrfs fi usage` | ⚠️ mechanism exists (`device remove missing` + balance), but manual | ✅ online | **Chosen.** Only the automation layer is missing |
| bcachefs `replicas=2` | ✅ | ✅ | ⚠️ manual | ✅ | No longer merged in-tree as of Linux 6.17, and not packaged on the target distro |
| ZFS | ❌ a vdev is sized by its smallest member | ✅ | ❌ replacement-based | ⚠️ RAIDZ expansion only | Fails requirement 1 |
| mergerfs + SnapRAID | ✅ | ✅ | ❌ parity snapshots; recovery is a manual `fix` | ✅ | Data written since the last sync is unprotected. Poor fit for anything continuously rewritten, e.g. Time Machine |
| Unraid-style (largest drive = parity) | ✅ | ✅ | ❌ emulates the failed disk until replaced | ✅ | Fails requirement 3 |
| Synology SHR (stacked md + LVM) | ⚠️ via per-size md layers | ✅ | ❌ shrinking reshapes are slow and risky | ⚠️ grow yes, shrink effectively no | Fails requirement 3 |
| Ceph / MooseFS, single node | ✅ | ✅ | ✅ re-replicates automatically — exactly the right semantics | ✅ | Correct but absurd for one box; the RAM and CPU cost dwarf the problem |
| Custom device-mapper / ublk target | ✅ | ✅ | ✅ | ✅ | A true BeyondRAID clone, and a multi-month project that ends up reimplementing btrfs `raid1` — chunk tables, the write hole, crash consistency — with fewer eyes on it |

The conclusion is that **nothing needs to be invented at the block layer.** btrfs already allocates
in chunks and already places the two copies of a chunk on two different devices, choosing devices by
free space. That is precisely BeyondRAID's trick. What Drobo added on top was firmware: a capacity
display, failure detection, a policy for when to re-lay-out, and the execution of it. That is a few
hundred lines of Python, not a filesystem.

## Capacity arithmetic

Every chunk exists twice, on two distinct devices. So the pool can hold

```
usable = min( sum(sizes) / 2 ,  sum(sizes) - max(size) )
```

The first term is the obvious one. The second says a single oversized drive can never be more than
half-filled, because each of its chunks needs a partner elsewhere. For 1.82 + 1.82 + 2.27 + 0.91 TiB
the two terms are 3.41 and 4.91, so usable is 3.41 TiB.

`status` does not print this formula's output. It prints btrfs's own accounting — `Used` plus
`Free (estimated)` — because real allocation drifts from the ideal as chunks age, and the measured
number is the one that governs whether the next write succeeds. The formula is used for the
*counterfactual*: given this set of survivors, what would usable capacity become? That question is
asked for each drive to produce the final line of `status`, and asked again by `remove` before it
agrees to do anything, and by `monitor` before it decides a rebuild is possible:

```python
usable_after = raid1_usable([sizes of drives that would remain])
can_rebuild  = used <= usable_after * rebuild_margin      # margin defaults to 0.95
```

The margin exists because btrfs needs unallocated room to maneuver — a `device remove` that has to
squeeze into the last 2% of the pool fails with `ENOSPC` halfway through and leaves you worse off
than not trying.

### What that last line is allowed to be alarmed about

Two different capacity facts can be true at once, and only one of them is a warning.

*Running out of room for new chunks* has a deadline. btrfs places a chunk as one extent on each of
two devices, picking the two with the most unallocated space, so allocation stops when the
second-most-free device does — while `df` still reports space free, because that space is inside
chunks already allocated. Past that point writes fail, and balance has nowhere to relocate to, so
the pool can no longer be fixed from inside. `chunk_headroom()` measures exactly this, and it is
what `chunk_headroom_warn` / `_fail` fire on. The used percentage is only a proxy for it, and a poor
one: uneven allocation can hit the wall at 80% used, or leave room to spare at 95%.

*Not being able to re-protect after losing the largest drive* has no deadline. Every byte stays
two-copy at any fill level; what is missing is the room to get back to two copies after a failure,
and the only fix is hardware. On a mixed-size pool it turns true around 70% full — an ordinary place
to be — and never clears on its own. Reported as a warning it would be permanently amber, which is
how a status line stops being read, so it is `info`. It becomes a real warning only when *no* drive
can be lost and re-protected, which is a different situation and does deserve one.

## The monitor state machine

`braidctl monitor` runs from a timer every five minutes, under a lock, and is idempotent. Its state
lives in `/var/lib/braid/state.json`: when each missing device was first noticed, per-device btrfs
error counters and SMART reallocation counts from the previous run (so it can report *changes*
rather than re-reporting a stable defect daily), notification timestamps for rate limiting, and an
inventory of device models and sizes — kept so that when a drive vanishes the tool can still say
*which* drive it was, rather than "devid 4".

Each pass:

1. **Ensure mounted.** If not: try a normal mount, then `-o degraded`, then report failure. This is
   what recovers a boot where a drive was missing, since `fstab` mounting will have failed.
2. **Re-attach returning members.** Any device carrying the pool's fsid and a devid that the
   filesystem thinks is missing is a drive that came back. Remount to pick it up, then start a
   scrub so the copies it missed while absent are rewritten. A device with the right fsid but a
   devid the filesystem no longer knows is an *evicted* member — report it, touch nothing.
   Membership, here and when deciding whether a normal mount can succeed, comes from the chunk
   tree's device items (devid and device uuid) as read through the newest superblock, not from
   label and fsid: a drive evicted while absent keeps both, and counted as a member it made the
   pool mount degraded with nothing missing. Evictions are also recorded in `state.json` with the
   pool's generation, which decides when the device list cannot be read.
3. **Record losses.** New missing devices get a timestamp and an immediate notification. Devices
   that reappeared get their timestamp cleared.
4. **Check health.** Rising btrfs error counters and SMART degradation are reported, rate limited to
   once a day. SMART reallocated-sector counts are compared against the previous reading, so an old
   drive sitting at a stable "1" is not a daily alarm, while one that ticks to "2" is.
5. **Rebuild, or explain why not.** For devices missing longer than `debounce_sec`, compute
   `can_rebuild`. If true, rebuild. If false, send the "add more capacity" notification, repeated
   daily, naming the shortfall.
6. **Warn on capacity** above `warn_percent`.

## The rebuild

```
compaction balance (-dusage=50 -musage=50)   consolidate half-empty chunks, freeing unallocated space
btrfs device remove <devid>                  live; btrfs re-mirrors the orphaned chunks
  └─ on failure ────────────────────────────  stop the configured services (Samba)
                                              umount; mount -o degraded
                                              btrfs device remove missing
                                              restart services
btrfs balance -dconvert=raid1,soft           only if chunks were left single-copy
```

The live path is tried first and, in testing on Linux 7.0, is what actually happens: the kernel
tracks the missing device and `device remove <devid>` succeeds on a mounted pool with no
interruption to clients. The degraded-remount path is a fallback for kernels or situations where it
doesn't, and is the only part of the design that costs downtime — tens of seconds.

The compaction balance first is not cosmetic. `device remove` must relocate every chunk on the
departing device into unallocated space elsewhere, and a pool whose chunks are half-empty can report
plenty of *free* space while having almost no *unallocated* space. Compacting first is the
difference between a rebuild that works and an `ENOSPC` at 80% full.

## Why the pool is not in /etc/fstab

Put a multi-device btrfs in `/etc/fstab` and systemd will unmount it when *one* member
disappears. That is not a theory; it is what happened here, and it makes the redundancy
pointless — the array survives, and the mount does not.

`systemd-fstab-generator` resolves `LABEL=braid` to a single device node and gives the mount
unit an implicit `Requires=` **plus `StopPropagatedFrom=`** on it. `StopPropagatedFrom=` is
the part that hurts: when that one device unit goes away, the stop propagates to the mount.
For a 4-drive pool the label resolves to whichever member udev linked last, so the mount ends
up hostage to an arbitrary one of them:

```
20:28:19.256  kernel:  usb 2-2: USB disconnect              (hub reset — every pool drive went away)
20:28:19.327  systemd: Unmounting srv-pool.mount...          (dev-sdd.device vanished)
20:28:19.346  kernel:  BTRFS info: last unmount of filesystem
20:28:20.523  kernel:  usb 1-2: new high-speed USB device    (all of them came straight back)
```

The drives were gone for under a second. The pool stayed down until something remounted it.

`x-systemd.device-timeout=` does not help — it is a start-time timeout. **`x-systemd.device-bound=false`
does not help either**, which is worth knowing because the documentation reads as though it
should: it works while the unit is loaded from the fragment alone, but once the filesystem is
actually mounted systemd re-derives the device dependencies from `/proc/self/mountinfo`, where
no `x-systemd.*` option survives, and `StopPropagatedFrom=` comes back. Measured, not assumed:

```
(fstab entry, unit inactive)   StopPropagatedFrom=            ← option honoured
(fstab entry, unit active)     StopPropagatedFrom=dev-sdd.device
                               → deleting sdd unmounts the pool
(no fstab entry)               StopPropagatedFrom=
                               → deleting sdd leaves the pool mounted and writable
```

The last case is what `braid-mount.service` buys: a mount with no fragment gets only
`Requires=`, and `Requires=` alone does not propagate a stop. Losing a drive then does what it
should — nothing, until the monitor decides whether to re-protect.

The cost is that `mount /srv/pool` no longer works from memory; it is
`braidctl mount`, which is also what the service runs, with `--wait` so a boot can outlast
USB enumeration.

## The other half: a bus re-enumeration is not a drive failure

Keeping the mount alive when a device disappears fixes the common case — one drive dying,
three still working — but it exposes a nastier one. When a USB bus re-enumerates, every drive
comes back under a *new* device node, and btrfs will not swap new nodes into an already-mounted
filesystem. The kernel just logs:

```
BTRFS warning: duplicate device /dev/sdd devid 1 generation 224 scanned by (udev-worker)
```

The mount survives, pointing at block devices that no longer exist. All members read as
MISSING. And writes to it **appear to succeed** — measured here with `dd` + `sync` + `md5sum`,
all of which returned cleanly for 128 MB that did not exist after the filesystem was
remounted. Nothing in the pool reports an error; the error counters stay at zero.

Silent loss is worse than the loud failure it replaced, so the window has to be small. Only a
remount repairs the state, which the monitor already does when it sees members return under new
nodes — but the timer runs every five minutes, and five minutes of a filesystem quietly
discarding writes is not acceptable.

Hence `99-braid.rules`: when a block device carrying the pool's label appears, udev starts
`braid-monitor-udev.service` immediately. Measured on the same failure:

```
20:53:54.189  first device re-attached
20:53:54.343  braid-monitor-udev.service starts     (+154 ms)
20:53:54.553  remount begins                        (+364 ms)
```

Several members reappear at once and each pulls the unit in, so `monitor --if-idle` exits
quietly when another pass already holds the lock.

### Closing the window: unmount what cannot be written

Detecting the return quickly still leaves the outage itself, during which the filesystem happily
accepts writes it is going to throw away. The honest response is to stop pretending: if the pool
cannot place a write, the mount should not be there.

`braidctl guard` does that. When a member device is removed, udev runs it; it counts live devices
and compares against what the profile needs to write — two for `raid1`, three for `raid1c3` — and
if the pool is below that it lazily unmounts. The next `open` then fails, so Samba or Time Machine
reports an error and retries, which is the correct outcome and the opposite of losing data quietly.

The threshold is the whole design. **Losing one drive out of a healthy `raid1` must do nothing** —
surviving that is the entire point of the pool, and unmounting there would reintroduce the fstab
bug this document opens with. Verified both ways:

```
delete 1 of 4   guard: 3/4 devices present, a 2-copy pool can still write
                → pool stays mounted, writes succeed
delete 4 of 4   guard: only 0 of 4 devices present; this pool needs 2 to write
                → lazy unmount; the next write fails loudly instead of vanishing
                → devices return, monitor remounts, PROTECTED
```

A `remove` event cannot start a unit through `SYSTEMD_WANTS` — systemd only does that for
`add`/`change` — so the rule launches it with `systemd-run --no-block`, which keeps the work off
the udev worker.

**Residual risk, stated plainly:** a process that already holds the file open keeps writing into
the detached filesystem; only new opens fail. And a write issued in the moments between the
devices vanishing and the guard unmounting can still be lost. The window is now small and bounded
rather than open-ended, but it is not zero. If the bus on your machine re-enumerates often, fix
that first — it is not something the filesystem can paper over.

## Why the mount point is immutable

The failure that actually destroys NAS installations is not a disk dying. It is the pool failing to
mount at boot while the services that write to it start anyway — so Samba, or Time Machine, or a
sync job cheerfully writes to `/srv/pool` on the *root* filesystem until `/` fills and the machine
falls over, with the data now in a place nobody thinks to look.

`chattr +i` on the empty mount point makes those writes fail immediately. When the pool is mounted,
the immutable flag belongs to the directory underneath and is invisible. It costs nothing and closes
the failure mode entirely. `install.sh` does not set it for you, because setting it requires the
pool to be unmounted; the README shows it as part of pool creation.

## What is deliberately not implemented

- **Encryption.** Orthogonal; put LUKS under the members if you want it, or see danneu/braid.
- **raid5/raid6.** btrfs's parity RAID retains the write hole. Two copies or three, nothing else.
- **A daemon.** A timer that runs a short idempotent program is easier to reason about, easier to
  test, and cannot wedge.
- **Automatic eviction of failing-but-present drives.** Available behind
  `auto_evict_on_errors = true`, off by default: a drive throwing occasional errors is usually still
  contributing readable copies, and removing it converts a partial problem into a full rebuild.
- **Any dependency outside the standard library.** A tool that manages the storage the machine boots
  into should not be able to break because a package upgraded.
