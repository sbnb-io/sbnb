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
- **Real-NVMe test device** - a spare NVMe SSD behind LUKS, matching
  the production stack (`cryptsetup luks2` with `--allow-discards`,
  `lvcreate --type thin-pool ... -Zn`, XFS or ext4, mounted
  `noatime,discard`). Used for the full matrix: inode tax, reclaim,
  sequential/random read+write throughput, allocation IOPS, and CPU.

  | Component | Detail |
  |-----------|--------|
  | CPU       | AMD Ryzen 5 3550H, 4 cores / 8 threads (8 logical CPUs) |
  | Memory    | ~16 GiB (15,801,620 kB) |
  | NVMe      | AirDisk 512 GB SSD (476.9 GiB), fw SN27911 |
  | Kernel    | Linux 6.18.26 x86_64 |
  | LUKS      | LUKS2, aes-xts-plain64, 512-bit key, 512 B sector |
  | fio       | 3.41 |

  CPU percentages below are across all 8 logical CPUs.

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
