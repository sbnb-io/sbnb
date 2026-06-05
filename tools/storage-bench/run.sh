#!/usr/bin/env bash
#
# storage-bench: thin-pool chunk-size / filesystem benchmark harness.
#
# Codifies the methodology in docs/storage-chunk-size-study.md so any
# device's storage can be characterized reproducibly. Builds the real
# stack (LUKS2 + LVM thin-pool + ext4/xfs) on a spare block device and
# measures: hardware info, inode tax, reclaim under fragmented deletes,
# sequential/random read+write throughput, allocation IOPS, system CPU,
# and the raw-device baseline (incl. the SLC-cache write cliff).
#
# DESTRUCTIVE: it reformats the target device. Run only on a spare
# internal drive (NOT the boot/USB device). The script refuses a device
# that is mounted or already a member of an LVM VG.
#
# Filesystems are auto-detected (mkfs.ext4 / mkfs.xfs); missing ones are
# skipped and logged - no silent gaps.
#
# Usage:
#   sudo ./run.sh <device> [quick|full]
#   e.g. sudo ./run.sh /dev/nvme0n1 full
#
# Tooling required: cryptsetup, lvm2, fio, e2freefrag, blkdiscard,
# python3, and at least one of mkfs.ext4 / mkfs.xfs.

set -uo pipefail

DEV="${1:?usage: run.sh <device> [quick|full]}"
MODE="${2:-full}"

LUKSNAME=stbench
MAP="/dev/mapper/$LUKSNAME"
VG=stbenchvg
MNT=/mnt/stbench
PASS=stbench-throwaway

if [ "$MODE" = quick ]; then
    CHUNKS="1024"
    NFILE_1M=1000          # 1 GiB of 1 MiB files
    NFILE_256K=4000        # 1 GiB of 256 KiB files
    FIO_RT=8
    SLC_GB=20
    TAX_VOLS="100"
    VOLG=40
else
    CHUNKS="512 1024 2048 4096"
    NFILE_1M=8000          # 8 GiB
    NFILE_256K=32000       # 8 GiB
    FIO_RT=30
    SLC_GB=300
    TAX_VOLS="100 250 400"
    VOLG=100
fi

# Optional 3rd arg overrides the SLC-cliff write size (GiB). Big drives
# (e.g. 4 TB) have a large dynamic SLC cache; push this higher to reach
# the post-cache sustained-write floor.
SLC_GB="${3:-$SLC_GB}"

log() { echo "## $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---- safety -----------------------------------------------------------
[ -b "$DEV" ] || die "$DEV is not a block device"
if lsblk -nro MOUNTPOINT "$DEV" | grep -q .; then
    die "$DEV (or a partition) is mounted - refusing"
fi
if pvs --noheadings -o pv_name 2>/dev/null | grep -qw "$DEV"; then
    die "$DEV is an LVM PV in use - refusing"
fi

# ---- filesystem detection --------------------------------------------
FS_LIST=""
command -v mkfs.ext4 >/dev/null 2>&1 && FS_LIST="$FS_LIST ext4"
command -v mkfs.xfs  >/dev/null 2>&1 && FS_LIST="$FS_LIST xfs"
[ -n "$FS_LIST" ] || die "no mkfs.ext4 or mkfs.xfs available"
for fs in ext4 xfs; do
    echo "$FS_LIST" | grep -qw "$fs" || log "SKIP: $fs (mkfs.$fs not present)"
done

# ---- helpers ----------------------------------------------------------
chunks_used() { dmsetup status "${VG}-tpool-tpool" | awk '{split($6,a,"/"); print a[1]}'; }
cpu_snap()    { awk '/^cpu /{print $2+$3+$4+$8+$9+$10, $6, $2+$3+$4+$5+$6+$7+$8+$9+$10}' /proc/stat; }
vg_down()     { umount "$MNT" 2>/dev/null; vgremove -fy "$VG" >/dev/null 2>&1; }
# TRIM the (LUKS-mapped) data area back to a fresh SSD state. blkdiscard
# on the crypt map passes discard through to the device (allow_discards),
# so each cell starts WITHOUT the prior cell's GC/SLC carryover - makes
# the chunk/fs comparison apples-to-apples. (Not used inside the SLC
# cliff test, which intentionally measures the cache filling.)
discard()     { blkdiscard "$MAP" 2>/dev/null || true; }

mkpool() { # chunk_kib  vol_gib
    vg_down
    discard
    pvcreate -fy "$MAP" >/dev/null 2>&1
    vgcreate -y "$VG" "$MAP" >/dev/null 2>&1
    lvcreate -y --type thin-pool -l 90%FREE --chunksize "${1}k" -Zn -n tpool "$VG" >/dev/null 2>&1
    lvcreate -y --thin -V "${2}G" -n tvol "$VG/tpool" >/dev/null 2>&1
}

mkfs_mount() { # fs  [extra mkfs args]
    local fs="$1"; shift
    if [ "$fs" = ext4 ]; then mkfs.ext4 -qF "$@" /dev/$VG/tvol >/dev/null 2>&1
    else mkfs.xfs -f /dev/$VG/tvol >/dev/null 2>&1; fi
    mkdir -p "$MNT"; mount -o noatime,discard /dev/$VG/tvol "$MNT"
}

# fio JSON helpers (write json to a file, parse with python to avoid quoting)
fio_metric() { # json_path  read|write  bw|iops
    python3 -c "import json;d=json.load(open('$1'));print(int(d['jobs'][0]['$2']['$3'$( [ "$3" = bw_bytes ] && echo '' )]))" 2>/dev/null || echo 0
}
seq_write_mb() { fio --name=sw --filename="$MNT/fio.dat" --rw=write --bs=1M --size=12G --direct=1 --ioengine=libaio --iodepth=8 --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['bw_bytes']//1048576))"; }
rand_write_iops() { fio --name=rw --filename="$MNT/fio.dat" --rw=randwrite --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=32 --ramp_time=4 --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['iops']))"; }
seq_read_mb() { fio --name=sr --filename="$MNT/fio.dat" --rw=read --bs=1M --size=12G --direct=1 --ioengine=libaio --iodepth=8 --ramp_time=2 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['read']['bw_bytes']//1048576))"; }
rand_read_iops() { fio --name=rr --filename="$MNT/fio.dat" --rw=randread --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=32 --ramp_time=3 --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['read']['iops']))"; }

# Parallel (numjobs) variants for the concurrency/saturation matrix. seq
# jobs use offset_increment so each writes/reads its own region (true
# aggregate seq bandwidth); random jobs share the span (group-reported).
NJ=4
seq_write_mb_p()   { fio --name=swp --filename="$MNT/fio.dat" --rw=write --bs=1M --size=8G --offset_increment=10G --numjobs=$NJ --group_reporting --direct=1 --ioengine=libaio --iodepth=8 --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['bw_bytes']//1048576))"; }
seq_read_mb_p()    { fio --name=srp --filename="$MNT/fio.dat" --rw=read  --bs=1M --size=8G --offset_increment=10G --numjobs=$NJ --group_reporting --direct=1 --ioengine=libaio --iodepth=8 --ramp_time=2 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['read']['bw_bytes']//1048576))"; }
rand_write_iops_p(){ fio --name=rwp --filename="$MNT/fio.dat" --rw=randwrite --bs=4k --size=8G --numjobs=$NJ --group_reporting --direct=1 --ioengine=libaio --iodepth=32 --ramp_time=4 --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['iops']))"; }
rand_read_iops_p() { fio --name=rrp --filename="$MNT/fio.dat" --rw=randread  --bs=4k --size=8G --numjobs=$NJ --group_reporting --direct=1 --ioengine=libaio --iodepth=32 --ramp_time=3 --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['read']['iops']))"; }

# run a metric fn while sampling system-wide CPU%; sets VAL, CPU
measure() { read -r b1 _ t1 < <(cpu_snap); VAL="$("$@")"; read -r b2 _ t2 < <(cpu_snap); local dt=$((t2-t1)); [ "$dt" -le 0 ] && dt=1; CPU=$(( (b2-b1)*100/dt )); }

write_files() { # count  size_kib
    python3 -c "
import os
buf=b'\0'*(1024*$2)
for i in range($1): open('$MNT/f%07d'%i,'wb').write(buf)
os.sync()"
}
delete_random_half() { # count
    python3 -c "
import os,random
random.seed(42)
for i in random.sample(range($1),$1//2): os.remove('$MNT/f%07d'%i)
os.sync()"
}

# ======================================================================
log "STORAGE BENCH  device=$DEV  mode=$MODE  fs=[$FS_LIST]"
date -u 2>/dev/null || true

# ---- hardware / software info ----------------------------------------
log "HARDWARE / SOFTWARE"
echo "cpu:    $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')  ($(nproc) logical CPUs)"
echo "mem:    $(awk '/MemTotal/{printf "%.1f GiB", $2/1048576}' /proc/meminfo)"
echo "kernel: $(uname -srmo)"
echo "fio:    $(fio --version 2>/dev/null)"
DNAME=$(basename "$DEV")
echo "dev:    $DEV  model='$(cat /sys/block/$DNAME/device/model 2>/dev/null | xargs)'  size=$(lsblk -dno SIZE "$DEV")"
nvme id-ctrl "$DEV" 2>/dev/null | grep -iE '^mn |^fr |^sn ' | sed 's/^/nvme:   /' || true

# ---- raw-device baseline (FRESH bare device, no LUKS/thin/fs) ---------
# Runs FIRST on a just-TRIMmed device so it reflects datasheet conditions
# (not a GC-dirtied drive). seq at qd64; random at 4k, small span, high
# parallelism (closest to the rated max-IOPS conditions). seqW writes the
# region first so seqR/randR hit real data, not deallocated zeros.
log "RAW BASELINE (fresh device; compare to datasheet seq/IOPS)"
blkdiscard "$DEV" 2>/dev/null || true; sync; sleep 5
rawbw()   { python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['$1']['bw_bytes']//1048576))" 2>/dev/null || echo 0; }
rawiops() { python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['$1']['iops']))" 2>/dev/null || echo 0; }
fio --name=bsw --filename="$DEV" --rw=write --bs=1M --size=64G --direct=1 --ioengine=libaio --iodepth=64 --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw seqW  (1M qd64):              $(rawbw write) MB/s"
fio --name=bsr --filename="$DEV" --rw=read  --bs=1M --size=64G --direct=1 --ioengine=libaio --iodepth=64 --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw seqR  (1M qd64):              $(rawbw read) MB/s"
fio --name=brr --filename="$DEV" --rw=randread  --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=128 --numjobs=4 --group_reporting --runtime=$FIO_RT --time_based --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw randR (4k qd128 x4, 8G span): $(rawiops read) IOPS"
fio --name=brw --filename="$DEV" --rw=randwrite --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=128 --numjobs=4 --group_reporting --runtime=$FIO_RT --time_based --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw randW (4k qd128 x4, 8G span): $(rawiops write) IOPS"

# ---- matched-to-matrix random (apples-to-apples bare vs stack) -------
# Same fio knobs as the stack matrix random cells (4k, qd32, 8 GiB,
# 1 job and 4 jobs) so bare/stack overhead is like-for-like - the
# datasheet baseline above uses qd128 x4 to chase the rated peak, which
# is NOT comparable to the matrix's qd32. Pre-write the 8 GiB region
# sequentially first (as the matrix fills its file), so reads hit real
# data and writes are in-place overwrites. Reads run before writes to
# keep the region seq-laid-out for the read points.
fio --name=mpf  --filename="$DEV" --rw=write     --bs=1M --size=8G --direct=1 --ioengine=libaio --iodepth=8 >/dev/null 2>&1
fio --name=mrr1 --filename="$DEV" --rw=randread  --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=32              --runtime=$FIO_RT --time_based --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw randR (4k qd32, 8G, 1 job):   $(rawiops read) IOPS"
fio --name=mrr4 --filename="$DEV" --rw=randread  --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=32 --numjobs=4 --group_reporting --runtime=$FIO_RT --time_based --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw randR (4k qd32, 8G, 4 jobs):  $(rawiops read) IOPS"
fio --name=mrw1 --filename="$DEV" --rw=randwrite --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=32              --runtime=$FIO_RT --time_based --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw randW (4k qd32, 8G, 1 job):   $(rawiops write) IOPS"
fio --name=mrw4 --filename="$DEV" --rw=randwrite --bs=4k --size=8G --direct=1 --ioengine=libaio --iodepth=32 --numjobs=4 --group_reporting --runtime=$FIO_RT --time_based --ramp_time=3 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
echo "raw randW (4k qd32, 8G, 4 jobs):  $(rawiops write) IOPS"

# ---- LUKS setup (crypt layer is constant across all tests) -----------
log "LUKS SETUP (luks2, allow-discards)"
cryptsetup status "$LUKSNAME" >/dev/null 2>&1 && cryptsetup close "$LUKSNAME"
wipefs -a "$DEV" >/dev/null 2>&1
printf '%s' "$PASS" | cryptsetup luksFormat --batch-mode --type luks2 "$DEV" - || die "luksFormat failed"
printf '%s' "$PASS" | cryptsetup luksOpen --allow-discards "$DEV" "$LUKSNAME" - || die "luksOpen failed"
echo "luks:   $(cryptsetup luksDump "$DEV" | awk -F: '/cipher:/{print $2; exit}' | xargs) | allow_discards=$(dmsetup table "$LUKSNAME" | grep -qo allow_discards && echo yes || echo no)"

# ---- inode tax --------------------------------------------------------
log "INODE TAX (empty-volume mapped MiB; ext4 forced lazy_itable_init=0)"
echo "# chunk effect @ 400G volume"
for fs in $FS_LIST; do for ck in $CHUNKS; do
    mkpool "$ck" 400
    if [ "$fs" = ext4 ]; then mkfs_mount ext4 -E lazy_itable_init=0,lazy_journal_init=0; else mkfs_mount xfs; fi
    sync; sleep 1; e=$(chunks_used); printf 'taxC fs=%-4s chunk=%5sK vol=400G empty=%6d MiB (%.2f%%)\n' "$fs" "$ck" "$((e*ck/1024))" "$(awk "BEGIN{print $((e*ck/1024))/(400*1024)*100}")"
    vg_down
done; done
echo "# volume scaling @ 1M chunk"
for fs in $FS_LIST; do for v in $TAX_VOLS; do
    mkpool 1024 "$v"
    if [ "$fs" = ext4 ]; then mkfs_mount ext4 -E lazy_itable_init=0,lazy_journal_init=0; else mkfs_mount xfs; fi
    sync; sleep 1; e=$(chunks_used); printf 'taxV fs=%-4s chunk=1024K vol=%4sG empty=%6d MiB (%.2f%%)\n' "$fs" "$v" "$((e*1024/1024))" "$(awk "BEGIN{print $((e))/($v*1024)*100}")"
    vg_down
done; done

# ---- consolidated matrix (throughput + CPU + reclaim) ----------------
log "MATRIX (empty, seqW+cpu, randW+cpu, reclaim 1MiB-files random-50%)"
for fs in $FS_LIST; do for ck in $CHUNKS; do
    mkpool "$ck" "$VOLG"; mkfs_mount "$fs"
    emp=$(chunks_used)
    measure seq_write_mb; bw=$VAL; bwcpu=$CPU
    measure rand_write_iops; io=$VAL; iocpu=$CPU
    rm -f "$MNT/fio.dat"; sync; fstrim "$MNT" >/dev/null 2>&1; sleep 1
    write_files "$NFILE_1M" 1024; a=$(chunks_used)
    delete_random_half "$NFILE_1M"; sync; sleep 1; fstrim "$MNT" >/dev/null 2>&1; sleep 1; b=$(chunks_used)
    recl=$(( (a-b)*ck/1024 )); deld=$(( NFILE_1M/2 ))
    printf 'mtrx fs=%-4s chunk=%5sK empty=%5dMiB seqW=%5dMB/s cpu=%3d%% randW=%6dIOPS cpu=%3d%% reclaim=%4dMiB/%dMiB (%d%%)\n' \
        "$fs" "$ck" "$((emp*ck/1024))" "$bw" "$bwcpu" "$io" "$iocpu" "$recl" "$deld" "$(( recl*100/deld ))"
    vg_down
done; done

# ---- parallel matrix (numjobs=NJ: concurrency / saturation) ----------
# Second throughput table at parallelism - confirms chunk-flatness holds
# under load and shows how far parallel IO closes the gap to the raw
# baseline. (Reclaim/inode tax are job-independent, so not repeated.)
log "PARALLEL MATRIX (numjobs=$NJ: seqW+cpu, randW+cpu, seqR, randR)"
for fs in $FS_LIST; do for ck in $CHUNKS; do
    mkpool "$ck" "$VOLG"; mkfs_mount "$fs"
    measure seq_write_mb_p;    pbw=$VAL; pbwcpu=$CPU
    measure rand_write_iops_p; pio=$VAL; piocpu=$CPU
    measure seq_read_mb_p;     prr=$VAL
    measure rand_read_iops_p;  pir=$VAL
    printf 'pmtx fs=%-4s chunk=%5sK seqW=%5dMB/s cpu=%3d%% randW=%6dIOPS cpu=%3d%% seqR=%5dMB/s randR=%6dIOPS\n' \
        "$fs" "$ck" "$pbw" "$pbwcpu" "$pio" "$piocpu" "$prr" "$pir"
    vg_down
done; done

# ---- reclaim 256KiB small-file stress --------------------------------
log "RECLAIM 256KiB-files random-50%"
for fs in $FS_LIST; do for ck in $CHUNKS; do
    mkpool "$ck" "$VOLG"; mkfs_mount "$fs"
    write_files "$NFILE_256K" 256; a=$(chunks_used)
    delete_random_half "$NFILE_256K"; sync; sleep 1; fstrim "$MNT" >/dev/null 2>&1; sleep 1; b=$(chunks_used)
    recl=$(( (a-b)*ck/1024 )); deld=$(( NFILE_256K/4 ))
    printf 'r256 fs=%-4s chunk=%5sK reclaim=%4dMiB/%dMiB (%d%%)\n' "$fs" "$ck" "$recl" "$deld" "$(( recl*100/deld ))"
    vg_down
done; done

# ---- read matrix ------------------------------------------------------
log "READ MATRIX (seqR+cpu, randR+cpu; fills 12G first)"
for fs in $FS_LIST; do for ck in $CHUNKS; do
    mkpool "$ck" "$VOLG"; mkfs_mount "$fs"
    fio --name=fl --filename="$MNT/fio.dat" --rw=write --bs=1M --size=12G --direct=1 --ioengine=libaio --iodepth=8 >/dev/null 2>&1
    measure seq_read_mb; br=$VAL; brcpu=$CPU
    measure rand_read_iops; ir=$VAL; ircpu=$CPU
    printf 'read fs=%-4s chunk=%5sK seqR=%5dMB/s cpu=%3d%% randR=%6dIOPS (~%dMB/s) cpu=%3d%%\n' \
        "$fs" "$ck" "$br" "$brcpu" "$ir" "$((ir*4/1024))" "$ircpu"
    vg_down
done; done

# ---- random allocation IOPS (fs-independent; raw thin LV) ------------
log "RANDOM 4k ALLOCATION (fresh pool, raw LV)"
for ck in $CHUNKS; do
    mkpool "$ck" "$VOLG"
    fio --name=al --filename=/dev/$VG/tvol --rw=randwrite --bs=4k --size=60G --direct=1 --ioengine=libaio --iodepth=32 --ramp_time=3 --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
    io=$(python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['iops']))" 2>/dev/null || echo 0)
    printf 'allo chunk=%5sK randAlloc=%6d IOPS\n' "$ck" "$io"
    vg_down
done

# ---- sustained raw write / SLC cliff (DESTRUCTIVE) -------------------
# The clean raw seq/rand baseline ran UP FRONT on a fresh device. This
# is the SUSTAINED-write test: write SLC_GB to find the SLC->TLC cliff.
log "RAW SUSTAINED WRITE / SLC CLIFF (DESTRUCTIVE - tears down LUKS rig)"
vg_down; cryptsetup close "$LUKSNAME" 2>/dev/null
blkdiscard "$DEV" 2>/dev/null || true; sync; sleep 5   # fresh SLC start
fio --name=w --filename="$DEV" --rw=write --bs=1M --direct=1 --ioengine=libaio --iodepth=8 --size=${SLC_GB}G --write_bw_log=/tmp/sbw --log_avg_msec=1000 --output-format=json --output=/tmp/sb.json >/dev/null 2>&1
python3 -c "
import json, glob
try:
    print('raw seqW overall avg:', int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['bw_bytes']//1048576), 'MB/s')
    f=sorted(glob.glob('/tmp/sbw*bw*.log'))[0]
    bw=[int(r.split(',')[1])/1024 for r in open(f) if r.strip()]
    peak=max(bw[:8]) if len(bw)>8 else max(bw)
    tail=sum(bw[-15:])/max(1,len(bw[-15:]))
    cliff=next((i for i,v in enumerate(bw) if i>3 and v<0.6*peak), None)
    print('raw seqW peak(SLC):', round(peak), 'MB/s | sustained tail:', round(tail), 'MB/s')
    if cliff is not None: print('raw seqW cliff at ~%ds (~%d GiB written)' % (cliff, sum(bw[:cliff])/1024))
    else: print('raw seqW: no cliff within %dG (SLC not exhausted)' % $SLC_GB)
    print('raw seqW curve MB/s ~every 20s:', [round(bw[i]) for i in range(0,len(bw),20)])
except Exception as e:
    print('raw seqW parse error:', e)
"
io=$(fio --name=ww1 --filename="$DEV" --rw=randwrite --bs=4k --size=100G --direct=1 --ioengine=libaio --iodepth=32 --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['iops']))" 2>/dev/null)
echo "raw randW 4k qd32 1job: ${io} IOPS"
io=$(fio --name=ww4 --filename="$DEV" --rw=randwrite --bs=4k --size=100G --direct=1 --ioengine=libaio --iodepth=32 --numjobs=4 --group_reporting --runtime=$FIO_RT --time_based --output-format=json --output=/tmp/sb.json >/dev/null 2>&1; python3 -c "import json;print(int(json.load(open('/tmp/sb.json'))['jobs'][0]['write']['iops']))" 2>/dev/null)
echo "raw randW 4k qd32 4job: ${io} IOPS"

log "DONE (LUKS rig torn down; device wiped). Recreate by re-running."
