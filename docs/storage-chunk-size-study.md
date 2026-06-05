# Thin-pool chunk size and filesystem study

Date: 2026-06-03

## Why this study exists

A production device ran its LVM thin pool to exhaustion. After the
immediate incident was cleared, ~500 GiB stayed stranded: the pool
reported ~92% full while the filesystems on top held far less real
data, and `fstrim` reclaimed almost none of it. Two distinct causes
were found:

1. **ext4 inode tables.** Each "empty" per-app volume still pinned
   ~63 GiB in the pool. `mkfs.ext4` allocates a fixed inode table
   sized to the volume (~2% of capacity), which is live metadata that
   cannot be trimmed.
2. **Chunk-granularity fragmentation.** A dm-thin chunk returns to the
   pool only when the *entire* chunk is discarded. Small-file,
   interleaved delete patterns (e.g. multi-camera NVR clips) leave a
   live sliver in most chunks, so deleted space is never reclaimed.

The storage redesign switched from ext4 to XFS and from a 2 MiB chunk
to a 4 MiB chunk. XFS addresses cause (1). The 4 MiB chunk was chosen
for a claimed throughput/metadata benefit ("+5-12% IOPS, 32x fewer
dm-thin metadata ops per write"). A larger chunk makes cause (2)
*worse*, so the choice needed to be settled with measurements rather
than assertions.

The question: for our workload (LUKS + thin pool, snapshot-based
backups, NVR-style small-file churn), what is the right filesystem and
chunk size?

## Test environments

- **Loopback rig** - a disposable host, thin pool on a loopback file,
  ext4 only (no `mkfs.xfs` available). Used to establish the
  reclaim-vs-chunk-size curve cheaply. Reclaim is a logical dm-thin
  property, so a loopback backing store is valid for it.
- **Device A (real NVMe)** - a spare NVMe SSD behind LUKS, matching the
  production stack (`cryptsetup luks2` with `--allow-discards`,
  `lvcreate --type thin-pool ... -Zn`, XFS or ext4, mounted
  `noatime,discard`). Used for the full matrix: inode tax, reclaim,
  sequential/random read+write throughput, allocation IOPS, and CPU.

  | Component | Detail |
  |-----------|--------|
  | CPU       | AMD Ryzen 5 3550H, 4 cores / 8 threads (8 logical CPUs) |
  | Memory    | ~16 GiB |
  | NVMe      | AirDisk 512 GB SSD (476.9 GiB), fw SN27911 - budget PCIe 3.0 x4 DRAM-less |
  | Kernel    | Linux 6.18.26 x86_64 |
  | LUKS      | LUKS2, aes-xts-plain64, 512-bit key, 512 B sector |
  | fio       | 3.41 |

  CPU percentages in the Device A results below are across all 8 logical CPUs.

- **Device B (faster NVMe)** - a quality PCIe 4.0 drive on a PCIe 4.0
  CPU (see "Second device" section). Used to confirm the conclusions
  hold at full device speed. (Originally bus-capped to PCIe 3.0 x4 by a
  Ryzen 5 5500; re-run after swapping in a Ryzen 5 5600X lifted the link
  to PCIe 4.0 x4 - both runs reach the same chunk-relative conclusions,
  the 5600X just at ~5.5 GB/s instead of ~3.5.)

  | Component | Detail |
  |-----------|--------|
  | CPU       | AMD Ryzen 5 5600X, 6 cores / 12 threads (12 logical CPUs) - Vermeer, PCIe 4.0 |
  | Memory    | ~31 GiB |
  | NVMe      | Samsung 990 EVO Plus 4TB, fw 2B2QKXG7 - rated 7250/6300 MB/s, PCIe 4.0 x4 / 5.0 x2, TLC/HMB; link negotiated **PCIe 4.0 x4 (16 GT/s)** |
  | Kernel    | Linux 6.18.26 x86_64 |
  | LUKS      | LUKS2, aes-xts-plain64, 512 B sector |
  | fio       | 3.41 |

The Device A and Device B runs are produced by the codified harness
`tools/storage-bench/run.sh` (`sudo run.sh <device> full`), which
reproduces this entire methodology on any spare block device.

## Methodology

All cells were driven from a shell harness. Common rig per cell:

```
cryptsetup luksFormat --type luks2 <dev>        # once; crypt layer is constant
cryptsetup luksOpen --allow-discards <dev> benchcrypt
pvcreate -fy /dev/mapper/benchcrypt
vgcreate benchvg /dev/mapper/benchcrypt
lvcreate --type thin-pool -l 90%FREE --chunksize <CK> -Zn -n tpool benchvg
lvcreate --thin -V 100G -n tvol benchvg/tpool
mkfs.xfs -f /dev/benchvg/tvol        # or: mkfs.ext4 -qF ...
mount -o noatime,discard /dev/benchvg/tvol /mnt/bench
```

Pool occupancy was read directly from the thin-pool target as a chunk
count (independent of any filesystem accounting):

```
dmsetup status benchvg-tpool-tpool | awk '{split($6,a,"/"); print a[1]}'
# mapped MiB = chunks * chunk_KiB / 1024
```

### Inode tax (empty-volume overhead)

Mounted a freshly-made filesystem and measured mapped chunks before
writing any data. ext4 was forced with
`-E lazy_itable_init=0,lazy_journal_init=0` so the full inode table is
written at mkfs time - otherwise ext4 zeroes it lazily after mount and
the tax is understated (this lazy init is why the production stranding
built up over weeks rather than appearing immediately). Measured
across chunk sizes (fixed 400 GiB volume) and across volume sizes
(fixed 1 MiB chunk).

### Reclaim

Wrote N fixed-size files, recorded mapped chunks, deleted a
**deterministic random 50%** (`random.seed(42)`), `sync`, `fstrim`,
then re-measured. The random-50% pattern models detection-based
retention that scatters survivors across the volume (the worst-case
NVR delete pattern). Two file sizes: 1 MiB (segment-like) and 256 KiB
(small-file stress). Reclaim is reported as a percentage of deleted
bytes returned to the pool.

### Sequential write throughput

```
fio --rw=write --bs=1M --size=12G --direct=1 --ioengine=libaio \
    --iodepth=8 --ramp_time=4
```

`libaio` (Linux async) keeps the device queue full; `direct=1` bypasses
page cache for true device throughput; `ramp_time` excludes the SLC
cache burst.

### Random write IOPS (steady-state overwrite)

```
fio --rw=randwrite --bs=4k --size=8G --direct=1 --ioengine=libaio \
    --iodepth=32 --ramp_time=5 --runtime=30 --time_based
```

Run against the already-written file, so writes are in-place overwrites
of already-mapped chunks (no allocation, no snapshot).

### Random write allocation IOPS

`fio randwrite 4k qd32` against a **fresh** raw thin LV (no filesystem),
so each write to new space forces a chunk allocation + metadata insert.
This is the one path where larger chunks could reduce per-write
metadata work.

### Sequential and random read

Reads need real data on disk first: with `-Zn`, unallocated chunks read
back as zeros with no device I/O. So each cell first filled a 12 GiB
file, then:

```
fio --rw=read     --bs=1M --size=12G --direct=1 --ioengine=libaio --iodepth=8  --ramp_time=2
fio --rw=randread --bs=4k --size=8G  --direct=1 --ioengine=libaio --iodepth=32 --ramp_time=3 --runtime=30 --time_based
```

`direct=1` bypasses the page cache so reads hit the device (and the
LUKS decrypt path), not RAM.

### CPU

System-wide CPU was sampled from `/proc/stat` (first `cpu` line) before
and after each fio run, so it includes kernel worker threads - the
dmcrypt and dm-thin kworkers - not just the fio process. Reported as
busy% across all 8 logical CPUs, plus iowait%.

### SSD state control

An SSD's write performance degrades with accumulated un-trimmed
writes. An early throughput run produced physically impossible results
(smaller chunks "faster" than larger) purely because cells inherited
the previous cell's degraded SSD state. The fix: `blkdiscard` the whole
device between cells to reset it. All throughput/CPU numbers below use
this reset.

## Results

### Consolidated matrix (real NVMe + LUKS, 4c/8t, 100 GiB volume)

Reclaim is for the 1 MiB-file, random-50% pattern. randW speed =
IOPS x 4 KiB.

| fs   | chunk        | inode tax | seq write | seq CPU | rand write       | rand CPU | reclaim |
|------|--------------|-----------|-----------|---------|------------------|----------|---------|
| ext4 | 512 K        | 2150 MB   | 2279 MB/s | 63%     | 70.6k (~276 MB/s)| 24%      | 100%    |
| ext4 | 1 M          | 2183 MB   | 2221 MB/s | 62%     | 70.0k (~273 MB/s)| 24%      | 100%    |
| ext4 | 2 M (current)| 2248 MB   | 2272 MB/s | 61%     | 71.3k (~279 MB/s)| 24%      | 49%     |
| ext4 | 4 M          | 2376 MB   | 2241 MB/s | 66%     | 71.8k (~281 MB/s)| 24%      | 12%     |
| xfs  | 512 K        | 72 MB     | 2336 MB/s | 62%     | 68.5k (~268 MB/s)| 24%      | 73%     |
| xfs  | 1 M          | 81 MB     | 2211 MB/s | 64%     | 70.8k (~277 MB/s)| 25%      | 41%     |
| xfs  | 2 M          | 98 MB     | 2249 MB/s | 65%     | 69.0k (~270 MB/s)| 23%      | 17%     |
| xfs  | 4 M (redesign)| 132 MB   | 2338 MB/s | 65%     | 71.0k (~278 MB/s)| 24%      | 7%      |

iowait was 0% in every cell (the NVMe never stalls the CPU).

### Inode tax detail

Driven by volume size; chunk size only rounds it. ext4 forced full
init.

Volume scaling (fixed 1 MiB chunk):

| volume | ext4          | xfs         |
|--------|---------------|-------------|
| 100 G  | 2183 MB (2.1%)| 81 MB (0.08%)|
| 250 G  | 5171 MB (2.0%)| 142 MB (0.06%)|
| 400 G  | 7649 MB (1.9%)| 217 MB (0.05%)|

Chunk effect (fixed 400 GiB volume): ext4 7539 MB (512 K) to 8304 MB
(4 M); xfs 208 MB (512 K) to 268 MB (4 M). Extrapolated to a
full-pool-sized volume (~3.6 TiB virtual), ext4 costs ~70 GiB; XFS
~0.2 GiB - matching the ~63 GiB per "empty" volume seen in the
production incident.

### Reclaim curves (% of deleted bytes returned)

| chunk | ext4, 1 MiB | xfs, 1 MiB | ext4, 256 KiB | xfs, 256 KiB |
|-------|-------------|------------|---------------|--------------|
| 512 K | 100%        | 73%        | 50%           | 12%          |
| 1 M   | 100%        | 41%        | 12%           | 5%           |
| 2 M   | 49%         | 17%        | 0%            | 0%           |
| 4 M   | 12%         | 7%         | 0%            | 0%           |

### Read performance (real NVMe + LUKS, 100 GiB volume)

randR speed = IOPS x 4 KiB.

| fs   | chunk | seq read  | seq CPU | rand read         | rand CPU |
|------|-------|-----------|---------|-------------------|----------|
| ext4 | 512 K | 2393 MB/s | 41%     | 58.1k (~227 MB/s) | 23%      |
| ext4 | 1 M   | 2413 MB/s | 40%     | 58.7k (~229 MB/s) | 23%      |
| ext4 | 2 M   | 2374 MB/s | 39%     | 59.2k (~231 MB/s) | 23%      |
| ext4 | 4 M   | 2372 MB/s | 40%     | 57.9k (~226 MB/s) | 22%      |
| xfs  | 512 K | 1965 MB/s | 37%     | 61.6k (~240 MB/s) | 23%      |
| xfs  | 1 M   | 1950 MB/s | 35%     | 58.9k (~230 MB/s) | 23%      |
| xfs  | 2 M   | 1804 MB/s | 34%     | 61.1k (~238 MB/s) | 23%      |
| xfs  | 4 M   | 2002 MB/s | 35%     | 60.9k (~237 MB/s) | 23%      |

Reads are flat across chunk size (reads of already-mapped chunks are
just block->chunk lookups, like overwrites). One filesystem-level note:
XFS sequential read runs ~17% slower than ext4 here (~1.8-2.0 vs
~2.4 GB/s), while XFS random read is marginally faster. Neither depends
on chunk size, so neither affects the chunk decision.

### Random 4 KiB allocation IOPS (fresh pool, raw LV)

| chunk | IOPS   |
|-------|--------|
| 512 K | 22,466 |
| 1 M   | 21,289 |
| 2 M   | 21,130 |
| 4 M   | 21,152 |

### Drive spec vs measured (raw-device baseline)

To separate the SSD's own behavior from the LUKS+thin+fs stack, raw
`/dev/nvme0n1` was benchmarked directly - reads non-destructively, then
writes destructively after tearing the rig down.

Drive: AirDisk 512 GB, PCIe 3.0 x4, DRAM-less (HMB). Rated 3200 MB/s
read / 1700 MB/s write; 4K IOPS unpublished.

| metric | rated | raw device | full stack (LUKS+thin+fs) |
|--------|-------|-----------|---------------------------|
| seq read (1M) | up to 3200 MB/s | 2436 MB/s | 2393 (ext4) / 1950 (xfs) |
| seq write (1M), SLC burst | up to 1700 MB/s | 2652 MB/s peak | 2200-2300 |
| seq write (1M), sustained post-SLC | - | ~51 MB/s | not isolated (matrix is burst) |
| rand read 4k qd32, 1 job | - | 110k IOPS | 58-62k |
| rand read 4k qd32, 4 jobs | - | 426k IOPS | - |
| rand write 4k qd32, 8 GiB hot region | - | - | ~70k |
| rand write 4k qd32, 100 GiB span, 1 job | - | 6.7k IOPS | - |
| rand write 4k qd32, 100 GiB span, 4 jobs | - | 5.9k IOPS | - |

Observations:

1. **Sequential read matches, and the stack is free.** Raw 2436 MB/s is
   ~76% of the 3200 marketing figure (ratings are best-case). Full-stack
   ext4 read (2393) equals raw - LUKS AES-XTS decrypt + thin + ext4 add
   ~0 sequential-read overhead. XFS's ~1950 is an fs-layout gap, not
   stack overhead.

2. **Every seq-write number in this study is SLC-cache burst.** Raw
   sustained write collapses from ~2650 MB/s to **~51 MB/s** once the
   dynamic SLC cache fills (~108 GiB on this near-empty drive). The
   matrix wrote only 8-12 GiB, well inside the cache, so its 2.2-2.3 GB/s
   is burst, not sustained. The 1700 MB/s rating is likewise optimistic.

3. **Random write is dominated by working-set size, not the stack.** The
   matrix overwrote an 8 GiB hot region (fits SLC, coalesces) and saw
   ~70k IOPS; raw drive-wide random write over a 100 GiB span is only
   ~6.7k IOPS and does not scale with jobs - the DRAM-less FTL is the
   limit. Both are correct for their working set; the true drive-wide
   sustained random write is low.

4. **Random read through the stack is ~half the raw single-job rate**
   (110k -> ~60k), the cost of per-4k crypto + thin mapping; the drive
   itself has far more headroom (426k at 4 jobs).

Practical note: this budget DRAM-less class has a hard sustained-write
floor (~50 MB/s post-SLC) and low drive-wide random-write IOPS. Bulk
sustained writes - e.g. restoring a large backup - can exceed the SLC
cache and stall at tens of MB/s. Frigate's steady recording rate stays
within SLC replenishment, so normal operation is unaffected. None of
this depends on chunk size.

## Analysis

1. **Throughput is flat across every chunk size and both
   filesystems** - reads and writes alike. Sequential write
   ~2.2-2.3 GB/s, sequential read ~2.4 GB/s (ext4) / ~1.9 GB/s (xfs),
   random overwrite ~70k IOPS (~275 MB/s), random read ~58-62k IOPS
   (~230 MB/s) - all flat across chunk size within noise. The claimed
   "+5-12% IOPS" for a larger chunk does not appear. The only
   cross-filesystem read gap (XFS seq read ~17% below ext4) is
   chunk-independent and minor relative to the inode-tax and reclaim
   differences that drive the decision.

2. **CPU is flat too** (~62% of 8 logical CPUs on seq write, ~24% on
   random).
   The "32x fewer metadata ops" of large chunks is real but invisible:
   total CPU is dominated by **LUKS encryption** (cost scales with data
   volume at ~2.3 GB/s), and the dm-thin metadata B-tree work is a
   rounding error next to it.

3. **Even random allocation - the one path that should favor large
   chunks - is flat** (~21-22.5k IOPS), if anything marginally faster
   at 512 K. Each 4 KiB write to new space is one allocation = one tree
   insert regardless of chunk size; larger chunks reduce total chunk
   *count* (metadata *size*), not metadata *ops per write*.

4. **XFS eliminates the inode tax** (72 MB vs ext4's 2150 MB at 100 G;
   ~0.2 vs ~70 GiB at full-volume scale).

5. **Reclaim collapses as chunk grows**, and **XFS reclaims worse than
   ext4 at any given chunk** - XFS's allocator spreads files across
   allocation groups, so they share chunks even when the chunk is no
   larger than a file. Smaller chunks are the only lever that recovers
   reclaim under fragmented deletes.

Across all measured axes - sequential read/write bandwidth, random
read/write IOPS, allocation IOPS, CPU, iowait - **chunk size makes no
difference**. The only metrics that move are inode tax (filesystem) and
reclaim (chunk size).

Two findings beyond the chunk question (from the raw-device baseline):

6. **The stack is nearly free for sequential I/O; its only real cost is
   per-4k crypto on small random ops.** Full-stack sequential read
   (2393 MB/s, ext4) equals the raw device (2436), so LUKS + thin + ext4
   add ~0 sequential overhead - the AES-XTS decrypt keeps pace at
   2.4 GB/s. The measurable cost is on small random operations, where
   crypto + thin mapping roughly halve single-job random read (raw 110k
   -> ~60k IOPS). The drive itself has ample headroom (426k at 4 jobs).

7. **The drive, not the stack, sets the sustained-write floor.** Every
   seq-write figure here is SLC-cache burst; raw sustained write drops
   from ~2650 MB/s to ~51 MB/s after the SLC cache fills (~108 GiB on a
   near-empty drive), and drive-wide random write is only ~6.7k IOPS
   (DRAM-less FTL, no job scaling). This budget-class behavior is the
   likely mechanism behind large-restore write stalls - a bulk restore
   exceeds the SLC cache and stalls at tens of MB/s - and it is
   independent of chunk size. Frigate's steady recording rate stays
   within SLC replenishment, so normal operation is unaffected.

## Recommendation

**XFS + 512 KiB chunk.**

- XFS removes the inode tax (the primary cause of the production
  stranding).
- 512 KiB gives the best reclaim of any XFS configuration (73% vs the
  redesign's 7% at 4 MiB - a ~10x improvement) under fragmented
  small-file deletes.
- Throughput, IOPS, and CPU are identical to 4 MiB.
- Metadata LV cost at 512 KiB is negligible at our pool sizes
  (~0.5 GiB for a 3.6 TiB pool; max allowed is ~16 GiB).

The redesign's filesystem choice (XFS) was correct. Its chunk size
(4 MiB) optimized a metric that does not exist on this stack and pays a
large reclaim penalty for it. Change `--chunksize 4M` to `512K`.

## Second device: faster NVMe (full PCIe 4.0)

Device B repeats the full matrix on a much faster drive (Samsung 990 EVO
Plus 4TB, TLC) and a PCIe 4.0 CPU (Ryzen 5 5600X, Zen 3, 12 logical
CPUs). The M.2 link negotiates **PCIe 4.0 x4 (16 GT/s)**, so this shows
the drive's real bandwidth - ~5.5 GB/s write, ~6.5 GB/s read - rather
than the ~3.5 GB/s PCIe-3 ceiling the original Ryzen 5 5500 (Cezanne,
PCIe 3.0 only) run was capped at. The chunk-relative conclusions are
identical across both runs; only the absolute throughput moved.

Matrix (100 GiB volume; reclaim = 1 MiB files, random-50%):

randW/randR are 4 KiB jobs; speed = IOPS x 4 KiB shown in parens.

| fs | chunk | inode tax | seqW | seqW CPU | randW IOPS (MB/s) | randR IOPS (MB/s) | reclaim |
|----|-------|-----------|------|----------|-------------------|-------------------|---------|
| ext4 | 512 K | 551 MB | 5468 MB/s | 26% | 166.5k (650) | 117.4k (458) | 100% |
| ext4 | 1 M | 584 MB | 5678 MB/s | 26% | 167.6k (655) | 118.0k (461) | 100% |
| ext4 | 2 M | 650 MB | 5575 MB/s | 26% | 166.1k (649) | 119.1k (465) | 49% |
| ext4 | 4 M | 780 MB | 5456 MB/s | 28% | 169.9k (664) | 121.4k (474) | 12% |
| xfs | 512 K | 83 MB | 5605 MB/s | 26% | 170.6k (666) | 121.6k (475) | 55% |
| xfs | 1 M | 92 MB | 5570 MB/s | 25% | 170.5k (666) | 121.2k (473) | 32% |
| xfs | 2 M | 110 MB | 5344 MB/s | 27% | 170.0k (664) | 121.6k (475) | 19% |
| xfs | 4 M | 144 MB | 5422 MB/s | 26% | 170.6k (666) | 121.3k (474) | 7% |

(seq read ~6.3-6.6 GB/s and random allocation ~167-172k IOPS were also
flat across all chunk sizes. Read CPU ~12-13%.)

**Raw baseline (fresh bare device, no LUKS/thin/fs)** - run FIRST on a
just-`blkdiscard`ed drive, so it reflects datasheet conditions:

| metric | measured | rated | % of rated |
|--------|----------|-------|------------|
| seq read (1M qd64) | 6874 MB/s | 7250 | **95%** (~98% of the PCIe 4.0 x4 ceiling) |
| seq write (1M qd64) | 5455 MB/s | 6300 | 87% |
| rand read (4k, 8 GiB span, qd128 x4) | 995,922 IOPS | 1,050,000 | **95%** |
| rand write (4k, 8 GiB span, qd128 x4) | 798,729 IOPS | 1,400,000 | 57% |

So the bare drive *does* reach ~95% of rated read bandwidth and rated
random-read IOPS under datasheet-like conditions (small span, high
parallelism, fresh device). Random write tops out at ~57% (SLC /
controller).

The qd128 x4 figures above chase the datasheet peak and are NOT
comparable to the matrix (which runs random at qd32). So the harness also
measures bare random at the matrix's exact knobs - 4k, qd32, 8 GiB,
pre-written region, 1 job and 4 jobs - giving an apples-to-apples bare
baseline for the stack-overhead table below:

| bare random (qd32, 8 GiB) | 1 job | 4 jobs | 1->4 scaling |
|---------------------------|-------|--------|--------------|
| rand read  | 247k IOPS | 981k IOPS | 3.96x (near-linear) |
| rand write | 236k IOPS | 802k IOPS | 3.40x |

The bare device scales random almost linearly with concurrency; the key
finding below is that the *stack* does not.

**Parallel matrix (numjobs=4, through the full LUKS+thin+fs stack):**

| fs | chunk | seqW | seqW CPU | randW IOPS | randW CPU | randR IOPS |
|----|-------|------|----------|------------|-----------|------------|
| ext4 | 512 K | 5530 MB/s | 43% | 312.1k | 41% | 391.9k |
| ext4 | 1 M | 5538 MB/s | 43% | 312.2k | 40% | 399.9k |
| ext4 | 2 M | 5585 MB/s | 44% | 310.9k | 40% | 401.1k |
| ext4 | 4 M | 5510 MB/s | 41% | 309.3k | 40% | 399.8k |
| xfs | 512 K | 5617 MB/s | 45% | 310.7k | 40% | 391.8k |
| xfs | 1 M | 5592 MB/s | 45% | 309.9k | 40% | 402.6k |
| xfs | 2 M | 5643 MB/s | 46% | 311.4k | 40% | 401.0k |
| xfs | 4 M | 5545 MB/s | 44% | 312.9k | 40% | 401.5k |

Parallelism (4 jobs) roughly **doubles random write** (167k -> ~311k)
and **~3x's random read** (120k -> ~400k) through the stack vs single
job; seq write is already saturated single-job (~5.5 GB/s). **Chunk size
stays flat under load** - the same no-difference across 512 K..4 M as
the single-job table. (Parallel seqR is omitted: that probe reads data
the random-write phase just fragmented, so it is not a representative
seq number; the clean seq read is 6.5-6.9 GB/s, above.)

### Stack overhead (Device B, gen4): LUKS + thin + XFS vs the bare drive

With the bare baseline measured at the matrix's exact knobs, the
stack-vs-bare overhead is apples-to-apples on every axis:

| axis | bare device | LUKS+thin+XFS | overhead |
|------|-------------|---------------|----------|
| seq write (1M) | 5455 MB/s | ~5377 MB/s | ~1% |
| seq read (1M) | 6874 MB/s | ~6419 MB/s | ~7% |
| rand read 4k qd32, 1 job | 247k IOPS | 121k IOPS | **51%** |
| rand read 4k qd32, 4 jobs | 981k IOPS | 400k IOPS | **59%** |
| rand write 4k qd32, 1 job | 236k IOPS | 170k IOPS | **28%** |
| rand write 4k qd32, 4 jobs | 802k IOPS | 311k IOPS | **61%** |

Stack figures are the XFS rows (ext4 is within ~1%); random read is the
read-matrix (1 job) / parallel-matrix (4 jobs), random write the matrix /
parallel-matrix. Bare and matched-bare are from the same rerun; the
single-job and parallel matrices above reproduced within ~1% on it
(chunk-flatness and reclaim ordering unchanged).

- **Sequential is nearly free** - ~1% on writes (Zen 3 AES-NI encrypts at
  line rate, ~26% system CPU; XFS extents stay contiguous) and ~7% on
  reads (dm-crypt decrypt + thin block->chunk mapping).
- **Random read costs about half** - the per-4k crypto + thin-mapping tax.
  It widens slightly with concurrency (51% at 1 job -> 59% at 4 jobs) but
  the stack still scales 3.3x from 1 -> 4 jobs, close to the drive's 4.0x.
- **Random write is the worst axis, and it degrades under load** - 28% at
  1 job but **61% at 4 jobs**, because the stack scales concurrent random
  write only **1.83x** (1 -> 4 jobs) while the bare device scales **3.4x**.
  The single `dmcrypt_write` / dm-thin write-ordering thread serializes
  concurrent writes: at 4 writers the stack delivers only ~39% of the
  drive's random-write IOPS. This is the write-amplification mechanism
  behind the production incident, now quantified.

Bottom line: at gen4 the stack is ~free for sequential I/O (~1% write,
~7% read); its real cost is small random ops - ~50% on reads and up to
~60% on concurrent writes - dominated by single-threaded crypto/thin
serialization. None of it depends on chunk size or filesystem.

### Where the random loss is (layered, Device B)

The overhead table shows random costs ~50-60% but not *which* layer. A
focused run measured the same qd32 / 8 GiB / 4k random at each layer in
isolation (1 job and 4 jobs, read and write):

| layer | randR 1job | randR 4job | randW 1job | randW 4job |
|-------|-----------|-----------|-----------|-----------|
| bare device | 246k | 996k | 235k | 798k |
| + LUKS (dm-crypt, 512-sector) | 198k | 649k | 353k | 419k |
| + dm-thin (raw LV, 512K chunk) | 128k | 429k | 183k | 352k |
| + XFS (full stack) | 121k | 400k | 170k | 311k |

Reading the 4-job columns (the multi-container case):

- **dm-crypt is the biggest single cost** - alone it takes random read
  -35% (996k -> 649k) and random write **-47%** (798k -> 419k). dm-crypt
  routes writes through a single ordered workqueue to preserve write
  ordering; under concurrency that one thread serializes. (Curiously it
  *helps* single-job random write - 235k -> 353k - by batching; the cost
  only shows up with multiple concurrent writers.)
- **dm-thin is the second layer** - another -34% read / -16% write on top
  of LUKS (per-chunk mapping + a single dm-thin worker).
- **XFS is nearly free** - 429k -> 400k read, 352k -> 311k write (~7-12%).

LUKS tuning options were then swept at the concurrent (qd32, 4-job)
random settings to find what recovers the dm-crypt loss. Most do not;
one does (all on the LUKS layer alone, vs the bare device's 996k / 798k):

| LUKS option | randR 4job | randW 4job | verdict |
|-------------|-----------|-----------|---------|
| default (AES-256-XTS, 512-sector) | 649k | 419k | baseline |
| 4096-byte sector | 675k | 423k | no random gain (big *seq* win only) |
| AES-128-XTS | 654k | 414k | no change - not CPU-bound (AES-NI) |
| `same_cpu_crypt` | 518k | 426k | worse |
| `no_read/no_write_workqueue` | 562k | 490k | net-negative (gutted 1-job) |
| **`submit_from_crypt_cpus`** | 642k | **799k** | **randW back to bare** |

`submit_from_crypt_cpus` disables dm-crypt's single post-encryption
write-submission thread - exactly the serialization point - submitting
encrypted bios from the (multiple) crypt CPUs instead. At the LUKS layer
it fully recovers concurrent random **write** (419k -> 799k = bare) and
does not regress reads or single-job. It is an *open-time* flag, not a
format property, so it applies to existing encrypted volumes on the next
luksOpen / reboot - no reformat or re-provision.

It is write-only, though: the dm-crypt **read** drop is untouched (randR
4job stays ~650k vs bare 996k, ~-36%; 1job ~198k vs 246k, ~-20%), and no
tested flag recovers it:

| flags (+ submit_from_crypt_cpus) | randR 1job | randR 4job |
|----------------------------------|-----------|-----------|
| (write fix only) | 199k | 655k |
| + high_priority | 198k | 647k |
| + no_read_workqueue | 144k | 561k |

`high_priority` (WQ_HIGHPRI on the crypt workqueues) does nothing for
reads in isolation - it targets kworker *starvation under CPU contention*,
which an isolated fio run does not create, and the kernel warns it
"degrades general responsiveness" (wrong tradeoff for an edge box whose
containers are the point). `no_read_workqueue` makes reads worse; AES-128
and 4k-sector make no difference. The reason writes were fixable and
reads are not: writes had a *single* post-encryption submission thread to
delete, whereas read decryption already runs on the **unbound** kcryptd
across all CPUs - the ~655k-IOPS cap is the aggregate per-bio `queue_work`
dispatch overhead, with no single-threaded bottleneck to remove (and
bypassing the queue overloads the NVMe completion softirq). So dm-crypt
is *fully* fixed for writes and *inherently* ~-36% on concurrent reads.

The gain survives the full stack (dm-thin's single worker then caps it,
but the win is still large and free):

| full stack (LUKS+thin+XFS), qd32 4-job | randR | randW |
|----------------------------------------|-------|-------|
| default | 411k | 321k |
| + `submit_from_crypt_cpus` | 408k | **442k (+38%)** |

So one LUKS open flag buys **+38% concurrent random write** end to end
(321k -> 442k), reads unchanged; dm-thin's single worker is then the next
ceiling. The other levers (4k sector, lighter cipher, no-workqueue,
same_cpu_crypt) do not help random on this NVMe.

**dm-thin is the residual ceiling, and it has no tuning knob.** With the
LUKS fix applied, swapping the thin LV for a thick LV (both on
LUKS+XFS) quantifies dm-thin's own cost:

| qd32 4-job, LUKS(submit_from_crypt_cpus)+XFS | randR | randW |
|----------------------------------------------|-------|-------|
| LUKS only (no LVM/fs) | 642k | 799k |
| thick LV + XFS | 566k | 612k |
| thin LV + XFS (512K chunk) | 411k | 444k |

dm-thin costs ~27% over a thick LV on concurrent random. Unlike dm-crypt
there is no parallelize-the-worker flag - the single per-pool worker +
metadata lock is architectural. The other thin knobs do not help here:
chunk size is IOPS-flat (above) and bigger hurts reclaim; `-Zn`
(skip-zeroing) is already set; discard passdown is already on. The only
way to recover the ~27% is a **thick** LV, which loses snapshots - and we
require thin snapshots for borg backups, so thick is not an option for
any backed-up volume. A purely ephemeral, never-backed-up hot volume
could be thick, but nothing in this workload needs >440k random IOPS.

This matches published dm-thin benchmarks. A linux-lvm list report with a
near-identical setup (512K chunk, `-Zn`, fio 4k single thread) measured
thin at ~-42% random read / ~-40% random write vs a thick LV (147k/132k
vs 251k/222k IOPS), with the penalty vanishing for large (512K) I/O -
the same shape as ours (single-job ~-35%/-48%, large-I/O ~free). The
documented cause is the same too: thin-pool metadata operations are the
bottleneck. We already apply the recommended thin tunings (`-Zn`,
snapshot-justified chunk; metadata-on-separate-device is N/A on a single
drive). Ref:
<https://linux-lvm.redhat.narkive.com/83kdqE43/performance-penalty-for-4k-requests-on-thin-provisioned-volume>.

These results line up with published dm-crypt NVMe benchmarks. Longcat's
2024 NVMe run sees the same ~24% random-read drop and the same "LUKS
random write *above* bare at low concurrency" workqueue-batching effect,
and likewise finds no_read/no_write_workqueue and 4k-sector give no
random gain. Cloudflare's well-known 4.3x from bypassing the workqueues
was an older kernel + high-parallelism ramdisk; on a modern AES-NI NVMe
the unbound crypt workqueue already parallelizes well, so that bypass no
longer helps - `submit_from_crypt_cpus` (a different lever) does.
Refs: <https://long-cat.net/blog/2024/05/09/optimizing-nvme-performance-with-dmcrypt>,
<https://blog.cloudflare.com/speeding-up-linux-disk-encryption/>.

System / kernel tuning was also swept and **none of it moves these
caps** - they are algorithmic (per-bio dispatch, single dm-thin worker),
not resource-starvation:

| lever | result |
|-------|--------|
| `performance` governor | no effect - `powersave` already boosts under load |
| `rq_affinity=2` | no effect |
| I/O scheduler | already `none` (optimal for NVMe) |
| CPU mitigations | already off - Zen 3 is "Not affected" by nearly all |
| **C-states off** (`cpu_dma_latency=0`, the `tuned` latency-profile trick) | **-15 to -21%** |

The C-state result is an AMD gotcha: forcing cores out of deep idle stops
Zen 3's boost from pushing the working cores to ~4.6 GHz, so the
dispatch-bound path runs *slower* (randR 1job 121k -> 96k, latency 263 ->
333 us). This is the opposite of the old Intel "C-state exit latency
hurts I/O" rule. Do **not** install `tuned`: its high-performance
profiles set exactly these two knobs (governor=performance, no-op here;
C-state disable, harmful here), and it is a heavyweight daemon for a
Buildroot OS. The current defaults (powersave + C-states on + `none`
scheduler) are already optimal for this workload.

Practical read: dm-crypt's concurrent-write cost is NOT fully inherent -
`submit_from_crypt_cpus` recovers a big chunk for free and is worth
adopting in storage-setup's luksOpen. Beyond that, even the un-tuned
stack delivers ~300-440k random IOPS, far beyond any container's real
demand; the production slowdown was sustained-bulk-write *latency* (a
large restore serializing through these single threads), best mitigated
by also bounding the bulk writer (ionice + bandwidth-limit on borg
extract) and capping the dirty-page burst (vm.dirty_bytes).

What holds (the conclusions are device- and bus-independent):

- **Chunk size is flat on every axis** - seq/rand read+write, IOPS,
  allocation, CPU - now at full PCIe 4.0 speed (~5.5 GB/s write,
  ~6.5 GB/s read). No "+5-12%" for larger chunks appears at gen4 either.
- **Inode tax is identical** (ext4 ~1.9% of volume, XFS ~0.05%) - a
  filesystem-architecture cost, independent of the drive or the bus.
- **Reclaim collapses with chunk size**, XFS worse than ext4 - same
  shape as Device A and the capped run; 512 K reclaims best.

What the uncapping confirms:

- **Throughput is now drive/stack-real, not bus-limited.** Lifting the
  link from PCIe 3.0 to 4.0 raised seq throughput from ~3.5 to ~5.5 GB/s
  write / ~6.5 GB/s read - and the chunk-flatness and the recommendation
  are unchanged. Absolute speed was never the deciding axis.
- **CPU stays low** (~26% on seq write at 5.5 GB/s). Zen 3 AES-NI keeps
  LUKS cheap; the single `dmcrypt_write` write-ordering thread - flagged
  in the capped run as the likely next bottleneck once the cap lifted -
  did **not** cap it: seq write scaled cleanly past 3.5 to 5.5 GB/s.
- **SLC: no cliff within 400 GiB.** Raw sequential write held a flat
  ~5.4 GB/s for the whole 400 GiB run (peak 5491, tail 5435; curve
  ~[5320, 5532, 5355, 5460]) - the dynamic SLC cache did not exhaust. The
  capped run's apparent "cliff at ~297 GiB -> ~1.5 GB/s" was the drive
  dipping under PCIe-3 pressure, not a real TLC floor; at gen4 the native
  write keeps up. (Contrast Device A's budget DRAM-less drive falling to
  ~51 MB/s post-SLC.)
- Raw drive-wide random write settled at ~70-72k IOPS (qd32, 1 or 4 jobs).
- A clean raw-read baseline needs a fresh/just-discarded device: the
  up-front "Raw baseline" (run before any stack tests, on a `blkdiscard`ed
  drive) reads **6898 MB/s (95% of rated)**. Running raw reads AFTER the
  heavy write phases returns a GC-depressed figure instead - which is why
  the harness now runs the raw baseline first, with a per-cell discard.

### Rated vs measured random IOPS (Device B)

The through-stack matrix random figures (~168k write / ~120k read) sit
below the rated 1,050,000 read / 1,400,000 write IOPS - but that is
working-set + parallelism, not the storage design. The rated numbers ARE
reachable under their (small-span, high-QD, fresh) conditions; on a
realistic large span the DRAM-less-class FTL is the limit:

| test | result | ceiling hit |
|------|--------|-------------|
| raw randR, **8 GiB span**, qd128 x4 (fresh) | **~995k IOPS** | matches the 1.05M rating - small span fits the host-memory buffer |
| raw randR, 100 GiB span, qd32 x1 job | ~179k IOPS (~700 MB/s) | **DRAM-less FTL**: a large mapping table thrashes the small host buffer |
| raw randR, 100 GiB span, qd32 x4 jobs | ~284k IOPS (~1.1 GB/s) | parallelism helps but the large-span FTL still dominates |
| raw randW, **8 GiB span**, qd128 x4 (fresh) | ~805k IOPS | ~57% of the 1.4M burst rating (SLC / controller) |
| raw randW, 100 GiB span, qd32 x1-4 jobs | ~70k IOPS | **controller** on a drive-wide (non-coalescing) random write |

Takeaways: (1) at the rated conditions (small span, high QD, fresh) the
drive hits ~995k read / ~805k write - ~95% / ~57% of rated; (2) on a
realistic large working set it is FTL-limited (~179k single / ~284k
parallel random read), which is why the through-stack matrix numbers sit
where they do; (3) none of these depend on chunk size or filesystem.

Note on the crypto path: in the capped run a single `dmcrypt_write`
write-ordering thread ran ~50% of one core at 3.2 GB/s and was flagged as
the likely next bottleneck once the bus cap lifted. The gen4 re-run
settles that: seq write scaled to ~5.5 GB/s at ~26% system CPU, so that
single thread is not the ceiling at this speed.

## Caveats

- The random-50% delete is a deterministic synthetic proxy for
  detection-scattered NVR retention; real workloads vary. FIFO deletion
  of contiguous segments reclaims well at any chunk size - the penalty
  applies to interleaved/scattered deletes.
- Throughput/CPU/IOPS are single 30 s runs per cell; differences under
  ~5% are within noise.
- The test device is a 4-core / 8-thread x86 (Ryzen 5 3550H). On a
  smaller-core edge device, LUKS encryption could become the throughput
  bottleneck at ~2.3 GB/s - but that is an encryption concern,
  independent of chunk size.
- Write-throughput figures are SLC-cache burst (8-12 GiB writes).
  Sustained post-cache write on this DRAM-less drive is ~50 MB/s - see
  "Drive spec vs measured (raw-device baseline)".
- This study covers a single NVMe SSD. Spinning disks (different seek
  and discard behavior) were not measured.
