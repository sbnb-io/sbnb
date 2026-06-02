"""Cross-role helpers shared by the control plane, data plane and
boot-mount roles. Dependency-free (stdlib only) so the data-plane and
boot-mount processes can import it without paho-mqtt installed."""


# --- Filesystem / storage layout (single source for all role modules) ---

REEFY_DATA_MNT = '/mnt/reefy-data'
DESIRED_STATE_PATH = '/mnt/reefy-data/state/desired-state.json'
COMPOSE_PATH = '/mnt/reefy-data/state/docker-compose.json'

STORAGE_VG = 'reefy'
STORAGE_LV = 'reefy_default'
STORAGE_POOL = 'reefy_pool'
# Thick LV (real blocks, OUTSIDE the thin pool) holding
# /mnt/reefy-data/state. Guarantees control-plane state always has space
# even when the app pool hits 100% - so comms/identity survive a full
# disk instead of going read-only with everything else. Created only on
# fresh provision (the pool takes 100%FREE, so an existing device has no
# room and keeps state on reefy_default).
STATE_LV = 'reefy_state'
# Legacy LV name used by pre-thin-pool installs - keep mounting it for
# backward compat; those devices don't get the snapshot-backed backup
# path until they factory-reset into the new layout.
LEGACY_STORAGE_LV = 'data'
# ext4 mount opts used everywhere we mount a reefy LV (default LV,
# per-volume backup LVs, USB-p4 fallback). Single source so all mount
# sites stay in sync. `discard` is the TRIM-passthrough half of the
# chain (the LUKS half is --allow-discards at open time); without both,
# the drive's FTL doesn't reclaim freed blocks. Kept identical for
# default + per-volume LVs since they live on the same drive with the
# same wear semantics.
REEFY_DATA_MOUNT_OPTS = 'noatime,commit=60,discard'


def _part_dev(disk, partnum):
    """Build the kernel partition device path for a given disk + part
    number. The kernel inserts a 'p' separator when the disk name ends
    in a digit (NVMe: nvme0n1 -> nvme0n1p1; mmcblk: mmcblk0 -> mmcblk0p1)
    and omits it otherwise (SATA/USB: sda -> sda1).

    Historically this code ran only on USB-boot devices (sda) so the
    naive f'{disk}{n}' worked. Booting Reefy on NVMe (EC2, mini-PC with
    M.2 boot) needs the 'p'."""
    if disk and disk[-1].isdigit():
        return f'{disk}p{partnum}'
    return f'{disk}{partnum}'


def log(source, msg):
    """Log a message to stdout (captured by journald → reefy-log-publisher → MQTT)."""
    print(f"[{source}] {msg}")
