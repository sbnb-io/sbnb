#!/bin/sh
set -euxo pipefail

# Phase 1: Critical path — storage setup.
# Must complete before Docker, MQTT, and other services can start.
#
# A/B partition layout:
#   p1: ESP "reefy-a" (1 GiB)
#   p2: ESP "reefy-b" (1 GiB)
#   p3: Key partition (1 MiB, msftres) — LUKS passphrase  [created during adoption]
#   p4: Data partition (rest of disk) — LUKS-encrypted f2fs [created during adoption]
#
# This script only discovers and mounts existing storage.
# Partition creation happens in the reconciler during adoption.
# If no persistent storage exists (fresh USB), tmpfs is used for bootstrap.

REEFY_MNT="/mnt/reefy"
REEFY_DATA_MNT="/mnt/reefy-data"
LUKS_NAME="reefy-data"
LUKS_KEY_SIZE=44
# ext4 mount opts for reefy LVs. `discard` is the FS-level half of
# the TRIM passthrough chain (LUKS half is --allow-discards); without
# it, deleted blocks aren't communicated to the drive at unlink time,
# causing FTL write amplification under high-churn workloads. Kept
# identical in reefy.shared.REEFY_DATA_MOUNT_OPTS - change
# both if you change one.
REEFY_DATA_MOUNT_OPTS="noatime,commit=60,discard"

# Build the kernel partition device path for a given disk + part
# number. Kernel inserts a 'p' separator when the disk name ends in
# a digit (NVMe: nvme0n1 -> nvme0n1p1, mmcblk: mmcblk0 -> mmcblk0p1).
# USB-boot (sda) skips the separator. Same helper as reefy-efi +
# reefy.shared::_part_dev.
part_dev() {
    DISKARG="$1"
    PARTNUM="$2"
    case "$DISKARG" in
        *[0-9]) echo "${DISKARG}p${PARTNUM}" ;;
        *)      echo "${DISKARG}${PARTNUM}" ;;
    esac
}

# Mount the thick state LV (reefy_state) at /mnt/reefy-data/state, if it
# exists (new layout). Guarantees control-plane state has space even when
# the app thin pool is full. No-op on legacy devices (state stays on
# reefy_default). Seeds a freshly-created (empty) LV from any existing
# state on reefy_default before mounting, so we never shadow it. Run
# after reefy_default is mounted at /mnt/reefy-data, before any service
# reads state. XFS opts (noatime,discard) - no ext4 commit=.
mount_state_lv() {
    SLV="/dev/reefy/reefy_state"
    [ -e "${SLV}" ] || return 0
    SDIR="${REEFY_DATA_MNT}/state"
    mountpoint -q "${SDIR}" 2>/dev/null && return 0
    mkdir -p "${SDIR}"
    # Seed: if the LV is empty but reefy_default already has state
    # (fresh provision wrote bootstrap state there), copy it into the LV
    # first so mounting doesn't hide it.
    TMP=$(mktemp -d)
    if mount -o noatime,discard "${SLV}" "${TMP}" 2>/dev/null; then
        if [ -z "$(ls -A "${TMP}" 2>/dev/null)" ] && \
           [ -n "$(ls -A "${SDIR}" 2>/dev/null)" ]; then
            cp -a "${SDIR}/." "${TMP}/" 2>/dev/null || true
        fi
        umount "${TMP}" 2>/dev/null || true
    fi
    rmdir "${TMP}" 2>/dev/null || true
    if mount -o noatime,discard "${SLV}" "${SDIR}" 2>/dev/null; then
        echo "[reefy] Mounted reefy_state at ${SDIR}"
    fi
}

# Hostname-setting moved out of boot-reefy-storage.sh — it used to run
# here before virtio_net had finished probing, causing ~25% of QEMU
# boots to land with a random hostname (see 2026-04 investigation).
# Now handled by reefy-hostname.service, triggered by udev on physical
# NIC add (80-reefy-hostname.rules) with a 180s timer backstop.

# Determine active boot slot from UEFI BootCurrent
get_active_slot() {
    CURRENT_BOOTNUM=$(efibootmgr 2>/dev/null | grep '^BootCurrent:' | awk '{print $2}')
    ACTIVE_LABEL=$(efibootmgr 2>/dev/null | grep "^Boot${CURRENT_BOOTNUM}\*" | \
        sed 's/Boot[0-9A-F]*\* //' | awk '{print $1}')

    if [ "$ACTIVE_LABEL" = "reefy-a" ]; then
        INACTIVE_LABEL="reefy-b"
    elif [ "$ACTIVE_LABEL" = "reefy-b" ]; then
        INACTIVE_LABEL="reefy-a"
    else
        # Booted from a UEFI auto-created entry (not reefy-a/reefy-b).
        # Determine which partition by checking the verbose entry for partition GUID.
        REEFY_A_DEV=$(blkid -L reefy-a 2>/dev/null) || true
        REEFY_B_DEV=$(blkid -L reefy-b 2>/dev/null) || true
        if [ -n "$REEFY_A_DEV" ] || [ -n "$REEFY_B_DEV" ]; then
            GUID_A=$(blkid -s PARTUUID -o value "$REEFY_A_DEV" 2>/dev/null) || true
            GUID_B=$(blkid -s PARTUUID -o value "$REEFY_B_DEV" 2>/dev/null) || true
            ENTRY_DETAIL=$(efibootmgr -v 2>/dev/null | grep "^Boot${CURRENT_BOOTNUM}\*")
            if [ -n "$GUID_A" ] && echo "$ENTRY_DETAIL" | grep -qi "$GUID_A" 2>/dev/null; then
                ACTIVE_LABEL="reefy-a"
                INACTIVE_LABEL="reefy-b"
                echo "[reefy] Mapped Boot${CURRENT_BOOTNUM} to reefy-a (by partition GUID)"
            elif [ -n "$GUID_B" ] && echo "$ENTRY_DETAIL" | grep -qi "$GUID_B" 2>/dev/null; then
                ACTIVE_LABEL="reefy-b"
                INACTIVE_LABEL="reefy-a"
                echo "[reefy] Mapped Boot${CURRENT_BOOTNUM} to reefy-b (by partition GUID)"
            else
                ACTIVE_LABEL=""
                INACTIVE_LABEL=""
            fi
        else
            # Legacy single-ESP layout (no A/B partitions)
            ACTIVE_LABEL=""
            INACTIVE_LABEL=""
        fi
    fi
}

# Mount the active ESP (the slot we booted from)
mount_reefy_usb() {
    get_active_slot

    if [ -n "${ACTIVE_LABEL}" ]; then
        REEFY_DEV=$(blkid -L "${ACTIVE_LABEL}" 2>/dev/null) || true
    else
        # Fallback: try A/B labels, then legacy single-ESP label "reefy"
        REEFY_DEV=$(blkid -L "reefy-a" 2>/dev/null || blkid -L "reefy-b" 2>/dev/null || blkid -L "reefy" 2>/dev/null) || true
    fi

    if [ -n "${REEFY_DEV}" ]; then
        mkdir -p "${REEFY_MNT}" || true
        mount -o ro "${REEFY_DEV}" "${REEFY_MNT}" || true
        echo "[reefy] Mounted ${REEFY_DEV} (${ACTIVE_LABEL:-first-boot}) at ${REEFY_MNT}"
    else
        echo "[reefy] No device with label reefy-a or reefy-b found."
    fi
}

# Ensure UEFI boot entries exist for both A/B partitions (idempotent).
# Also removes duplicate auto-created entries from UEFI firmware.
ensure_boot_entries() {
    reefy-efi fix
}

# Create/mount encrypted data partition
# Key = partition 3, Data = partition 4 (A/B layout)
setup_data_partition() {
    [ -z "${REEFY_DEV}" ] && return 0

    if mountpoint -q "${REEFY_DATA_MNT}" 2>/dev/null; then
        echo "[reefy] ${REEFY_DATA_MNT} already mounted"
        return 0
    fi

    PARENT_NAME=$(lsblk -no PKNAME "${REEFY_DEV}" 2>/dev/null)
    [ -z "${PARENT_NAME}" ] && return 0
    REEFY_DISK="/dev/${PARENT_NAME}"
    KEY_PART="$(part_dev "${REEFY_DISK}" 3)"
    DATA_PART="$(part_dev "${REEFY_DISK}" 4)"

    mkdir -p "${REEFY_DATA_MNT}"
    modprobe dm_crypt 2>/dev/null || true

    # If LUKS partition exists, try to open and mount
    if [ -b "${DATA_PART}" ]; then
        if cryptsetup isLuks "${DATA_PART}" 2>/dev/null; then
            # --perf-submit_from_crypt_cpus drops dm-crypt's single
            # write-submission thread (recovers concurrent random write;
            # see docs/storage-chunk-size-study.md). --persistent stores
            # it in the header, so existing devices pick it up on this
            # first boot after the update and keep it thereafter.
            if cryptsetup luksOpen "${DATA_PART}" "${LUKS_NAME}" \
                --allow-discards --perf-submit_from_crypt_cpus --persistent \
                --key-file "${KEY_PART}" --keyfile-size "${LUKS_KEY_SIZE}" 2>/dev/null; then
                FS_TYPE=$(blkid -o value -s TYPE "/dev/mapper/${LUKS_NAME}" 2>/dev/null)
                if [ "${FS_TYPE}" = "LVM2_member" ]; then
                    # New layout: LUKS contains a VG with a thin pool and
                    # an LV mounted at /mnt/reefy-data. Activate the VG
                    # and mount the right LV (new `reefy_default`, or
                    # legacy `data` if this device pre-dates the rework).
                    vgscan >/dev/null 2>&1
                    vgchange -ay "${STORAGE_VG}" >/dev/null 2>&1
                    for lv in "${STORAGE_LV}" "${LEGACY_STORAGE_LV}"; do
                        lv_path="/dev/${STORAGE_VG}/${lv}"
                        [ -e "${lv_path}" ] || continue
                        # fs-aware opts: a fresh reefy_default is XFS, which
                        # rejects ext4's commit= (mount would fail); legacy
                        # ext4 devices keep the full opts. Detect per-LV so
                        # both layouts mount correctly across an upgrade.
                        LV_FS=$(blkid -o value -s TYPE "${lv_path}" 2>/dev/null)
                        if [ "${LV_FS}" = "xfs" ]; then
                            LV_OPTS="noatime,discard"
                        else
                            LV_OPTS="${REEFY_DATA_MOUNT_OPTS}"
                        fi
                        if mount -o "${LV_OPTS}" "${lv_path}" \
                                "${REEFY_DATA_MNT}" 2>/dev/null; then
                            echo "[reefy] Mounted LVM LV ${lv} (${LV_FS:-ext4}) at ${REEFY_DATA_MNT}"
                            mount_state_lv
                            return 0
                        fi
                    done
                    echo "[reefy] LVM on USB p4 but no mountable LV found"
                else
                    case "${FS_TYPE}" in
                        f2fs)  MOUNT_OPTS="noatime" ;;
                        *)     MOUNT_OPTS="${REEFY_DATA_MOUNT_OPTS}" ;;
                    esac
                    if mount -o "${MOUNT_OPTS}" "/dev/mapper/${LUKS_NAME}" \
                            "${REEFY_DATA_MNT}" 2>/dev/null; then
                        echo "[reefy] Mounted USB data partition (${FS_TYPE}) at ${REEFY_DATA_MNT}"
                        return 0
                    fi
                fi
            else
                echo "[reefy] USB data partition key mismatch, skipping"
            fi
        fi
    fi

    # Fallback: ensure state dir exists on rootfs overlay (already in RAM).
    # No mount needed — rootfs is writable overlay. Ephemeral until adoption
    # creates real persistent storage.
    mkdir -p "${REEFY_DATA_MNT}/state/lan"
    echo "[reefy] No persistent storage, using rootfs overlay for bootstrap"
}

STORAGE_VG="reefy"
# New LVM layout uses `reefy_default` (a thin LV); legacy installs had
# a flat `data` LV. Mount whichever one is actually present.
STORAGE_LV="reefy_default"
LEGACY_STORAGE_LV="data"
THIN_POOL_LV="reefy_pool"
LVM_THIN_TOOLS_CONFIG='global { thin_check_executable="/usr/sbin/thin_check" thin_repair_executable="/usr/sbin/thin_repair" }'

# Repair an inactive Reefy thin pool after normal activation has failed.
# LVM invokes the upstream thin_repair tool, writes its output to the spare
# metadata LV, and swaps that LV into the pool. LVM keeps the damaged metadata
# as a visible *_metaN LV, so this does not overwrite the only damaged copy.
#
# A completely overwritten superblock cannot provide its data block size.
# That geometry is also stored in the LVM VG metadata, so pass it explicitly
# along with the transaction ID and number of data blocks. This is the same
# failure shape produced when unrelated data replaces metadata block zero.
repair_thin_pool() {
    REPAIR_MARKER="/run/reefy-thin-pool-repaired"
    POOL="${STORAGE_VG}/${THIN_POOL_LV}"
    TMETA="${STORAGE_VG}/${THIN_POOL_LV}_tmeta"
    PMSPARE="${STORAGE_VG}/lvol0_pmspare"

    [ ! -e "${REPAIR_MARKER}" ] || return 1
    : > "${REPAIR_MARKER}"

    [ -x /usr/sbin/thin_check ] || return 1
    [ -x /usr/sbin/thin_repair ] || return 1
    [ "$(lvs --noheadings -o segtype "${POOL}" 2>/dev/null | xargs)" = "thin-pool" ] || return 1
    [ "$(lvs --noheadings -o lv_active "${POOL}" 2>/dev/null | xargs)" = "inactive" ] || return 1

    TMETA_SECTORS=$(lvs --noheadings --units s --nosuffix -o lv_size \
        "${TMETA}" 2>/dev/null | xargs)
    PMSPARE_SECTORS=$(lvs --noheadings --units s --nosuffix -o lv_size \
        "${PMSPARE}" 2>/dev/null | xargs)
    CHUNK_SECTORS=$(lvs --noheadings --units s --nosuffix -o chunksize \
        "${POOL}" 2>/dev/null | xargs)
    DATA_SECTORS=$(lvs --noheadings --units s --nosuffix -o lv_size \
        "${TMETA%_tmeta}_tdata" 2>/dev/null | xargs)
    TRANSACTION_ID=$(lvs --noheadings -o transaction_id \
        "${POOL}" 2>/dev/null | xargs)

    case "${TMETA_SECTORS}:${PMSPARE_SECTORS}:${CHUNK_SECTORS}:${DATA_SECTORS}:${TRANSACTION_ID}" in
        *[!0-9:]*|*::*|:*|*:) return 1 ;;
    esac
    [ "${PMSPARE_SECTORS}" -ge "${TMETA_SECTORS}" ] || return 1
    [ "${CHUNK_SECTORS}" -gt 0 ] || return 1
    [ $((DATA_SECTORS % CHUNK_SECTORS)) -eq 0 ] || return 1
    NR_DATA_BLOCKS=$((DATA_SECTORS / CHUNK_SECTORS))

    echo "[reefy] Thin-pool activation failed; attempting guarded metadata repair"
    REPAIR_CONFIG="global { thin_check_executable=\"/usr/sbin/thin_check\" thin_repair_executable=\"/usr/sbin/thin_repair\" thin_repair_options=[\"--data-block-size\",\"${CHUNK_SECTORS}\",\"--transaction-id\",\"${TRANSACTION_ID}\",\"--nr-data-blocks\",\"${NR_DATA_BLOCKS}\"] }"
    if lvconvert --config "${REPAIR_CONFIG}" --repair --yes "${POOL}"; then
        echo "[reefy] Thin-pool metadata repaired; damaged metadata retained by LVM"
        return 0
    fi

    echo "[reefy] WARNING: Thin-pool metadata repair failed"
    return 1
}

# Try to use internal drive as /mnt/reefy-data instead of slow USB.
# Opens LUKS on all internal drives with our key, activates LVM VG,
# then mounts the LV directly as /mnt/reefy-data (replacing USB mount).
# Runs independently — does not require USB p4 to be mounted.
# If no internal drive found, USB stays mounted (fallback).
setup_internal_storage() {
    [ -z "${REEFY_DEV}" ] && return 0

    PARENT_NAME=$(lsblk -no PKNAME "${REEFY_DEV}" 2>/dev/null)
    [ -z "${PARENT_NAME}" ] && return 0
    KEY_PART="/dev/${PARENT_NAME}3"
    [ ! -b "${KEY_PART}" ] && return 0

    modprobe dm_crypt 2>/dev/null || true
    modprobe dm_mod 2>/dev/null || true

    # Open LUKS on all internal drives with our key
    for dev in $(lsblk -dpno NAME 2>/dev/null); do
        [ "${dev}" = "/dev/${PARENT_NAME}" ] && continue
        cryptsetup isLuks "${dev}" 2>/dev/null || continue
        luks_name="reefy-$(basename ${dev})"
        [ -e "/dev/mapper/${luks_name}" ] && continue
        cryptsetup luksOpen "${dev}" "${luks_name}" \
            --allow-discards --perf-submit_from_crypt_cpus --persistent \
            --key-file "${KEY_PART}" --keyfile-size "${LUKS_KEY_SIZE}" 2>/dev/null || continue
        echo "[reefy] Opened LUKS on ${dev}"
    done

    # Scan for LVM and activate VG
    vgscan >/dev/null 2>&1
    vgs "${STORAGE_VG}" >/dev/null 2>&1 || return 0
    # vgchange asks LVM to run upstream thin_check before pool activation.
    if ! vgchange --config "${LVM_THIN_TOOLS_CONFIG}" \
            -ay "${STORAGE_VG}" >/dev/null 2>&1; then
        if repair_thin_pool && \
                vgchange --config "${LVM_THIN_TOOLS_CONFIG}" \
                    -ay "${STORAGE_VG}" >/dev/null 2>&1; then
            echo "[reefy] Activated repaired thin pool"
        else
            # Keep the thick identity LV available even when app storage is
            # unrecoverable. This prevents a storage failure from making an
            # adopted device appear factory-fresh to the control plane.
            lvchange -ay "${STORAGE_VG}/reefy_state" >/dev/null 2>&1 || true
            mount_state_lv
            return 0
        fi
    fi
    lv_path=""
    for lv in "${STORAGE_LV}" "${LEGACY_STORAGE_LV}"; do
        if [ -e "/dev/${STORAGE_VG}/${lv}" ]; then
            lv_path="/dev/${STORAGE_VG}/${lv}"
            break
        fi
    done
    [ -z "${lv_path}" ] && return 0

    # Mount internal drive as /mnt/reefy-data. fs-aware opts: a fresh
    # reefy_default is XFS, which REJECTS ext4's commit= ("xfs: Unknown
    # parameter 'commit'" -> mount fails -> device falls back to bootstrap
    # and loses its identity on reboot). Legacy ext4 devices keep the full
    # opts. mount_data() (USB p4 path) already does this; the internal-
    # drive path must too. Detect per-LV so both layouts remount.
    echo "[reefy] Internal drive found, mounting ${lv_path} as ${REEFY_DATA_MNT}..."
    mkdir -p "${REEFY_DATA_MNT}"
    LV_FS=$(blkid -o value -s TYPE "${lv_path}" 2>/dev/null)
    if [ "${LV_FS}" = "xfs" ]; then
        INTERNAL_OPTS="noatime,discard"
    else
        INTERNAL_OPTS="${REEFY_DATA_MOUNT_OPTS}"
    fi
    mount -o "${INTERNAL_OPTS}" "${lv_path}" "${REEFY_DATA_MNT}" || {
        echo "[reefy] WARNING: Internal drive mount failed"
        return 0
    }

    # Ensure required directories exist (first time on internal drive)
    mkdir -p "${REEFY_DATA_MNT}/state/lan"
    mkdir -p "${REEFY_DATA_MNT}/apps"
    mkdir -p "${REEFY_DATA_MNT}/docker"

    mount_state_lv

    echo "[reefy] Mounted internal drive as ${REEFY_DATA_MNT}"
}

# Main execution
# Order matters: try internal drive first (fast), fall back to USB p4 (slow).
# If neither exists (fresh USB), use tmpfs for bootstrap.
mount_reefy_usb

if ! mountpoint -q "${REEFY_MNT}" 2>/dev/null; then
    echo "[reefy] WARNING: ${REEFY_MNT} not mounted — USB dongle may need re-flashing with A/B image"
    mkdir -p "${REEFY_DATA_MNT}/state/lan"
    exit 0
fi

ensure_boot_entries
setup_internal_storage
setup_data_partition
