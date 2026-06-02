"""Cross-role helpers shared by the control plane, data plane and
boot-mount roles. Dependency-free (stdlib only) so the data-plane and
boot-mount processes can import it without paho-mqtt installed."""


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
