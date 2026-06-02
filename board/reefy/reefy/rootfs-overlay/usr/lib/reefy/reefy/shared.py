"""Cross-role helpers shared by the control plane, data plane and
boot-mount roles. Dependency-free (stdlib only) so the data-plane and
boot-mount processes can import it without paho-mqtt installed."""

import subprocess
import time


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


# --- Hostname / health helpers (used by control and data plane) ---

def get_default_hostname():
    """MAC-based default hostname via the shared helper script."""
    result = subprocess.run(
        ['reefy-derive-hostname'], capture_output=True, text=True)
    return result.stdout.strip()


def set_hostname(hostname):
    """Set system hostname via hostnamectl and restart avahi for mDNS."""
    current = subprocess.run(
        ['hostname'], capture_output=True, text=True).stdout.strip()
    if current == hostname:
        return
    subprocess.run(['hostnamectl', 'set-hostname', hostname], capture_output=True)
    # Restart avahi so it re-announces the new hostname on the network
    subprocess.run(['systemctl', 'restart', 'avahi-daemon'], capture_output=True)
    log('mqtt', f'Hostname changed: {current} -> {hostname}')


def wait_for_tunnel_health(timeout=60, interval=2):
    """Poll reefy-proxy at localhost:8080 until it responds or timeout."""
    import urllib.request
    import urllib.error

    log('reconciler', 'Waiting for tunnel proxy health...')
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request('http://localhost:8080/', method='HEAD')
            urllib.request.urlopen(req, timeout=2)
            print("[mqtt] Tunnel proxy health check passed")
            return
        except urllib.error.HTTPError:
            # Any HTTP response (404, 403, etc.) means proxy is alive
            print("[mqtt] Tunnel proxy health check passed")
            return
        except (urllib.error.URLError, OSError):
            # Connection refused / timeout - proxy not ready yet
            pass
        time.sleep(interval)

    log('mqtt', f'Tunnel proxy health check timed out after {timeout}s')


def instance_uuids_in_compose(compose):
    """Extract user instance uuids from compose services, filtering out
    infrastructure containers (cloudflared, reefy-proxy) and the
    auxiliary -tty pairs."""
    services = (compose or {}).get('services') or {}
    return [k for k in services.keys()
            if not k.endswith('-tty')
            and k not in ('cloudflared', 'reefy-proxy')]


def find_wireless_iface():
    """First wireless interface name via `iw dev`, or None."""
    try:
        result = subprocess.run(['iw', 'dev'], capture_output=True,
                                text=True, timeout=5)
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('Interface '):
                return line.split()[1]
    except Exception:
        pass
    return None
