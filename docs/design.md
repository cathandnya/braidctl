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
