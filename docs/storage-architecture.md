# Storage architecture

## Overview

Reefy separates boot media from persistent device and application data:

| Mount point | Backing storage | Purpose |
|---|---|---|
| `/mnt/reefy` | Currently active EFI slot, read-only | Boot image and bootstrap or device-specific files. |
| `/mnt/reefy-data` | Reefy LVM data LV on internal disks or USB fallback | Docker data, apps, desired state, credentials, and backups. |
| `/mnt/reefy-data/state` | Optional thick state LV | Control-plane identity and state isolated from thin-pool exhaustion. |
| `/mnt/reefy-data/apps/<instance>/<volume>` | Directory or per-volume thin LV | App data with declared ownership, backup, and optional capacity cap. |

Fresh storage is provisioned during adoption, not by the boot oneshot. At
later boots, the storage service only discovers, opens, activates, and mounts
the existing stack.

## Boot-device partition layout

```text
Reefy boot disk
|-- partition 1, 1 GiB, FAT, EFI System Partition, label reefy-a
|-- partition 2, 1 GiB, FAT, EFI System Partition, label reefy-b
|-- partition 3, 1 MiB, Microsoft Reserved type, raw LUKS key material
`-- partition 4, remaining space, optional USB persistent-data fallback
```

Partitions 1 and 2 exist in the flashed image. Adoption creates partition 3
when it is absent. If no internal disk is selected, adoption also creates
partition 4 from 2050 MiB to the end of the boot disk.

The helper that constructs partition device names handles both conventional
names such as `/dev/sda4` and digit-ending devices such as
`/dev/nvme0n1p4`.

## Persistent storage stack

Current installations use the same stack on internal disks and the USB
fallback:

```text
physical device or devices
  -> LUKS2 container per device
  -> LVM physical volume per opened mapper
  -> volume group reefy
       |-- thick LV reefy_state, XFS
       |-- thin pool reefy_pool, 512 KiB chunks
       |    |-- thin LV reefy_default, XFS
       |    `-- thin LV per capped or backup-enabled app volume, XFS
       `-- legacy flat LV data, ext4, mounted when present
```

Multiple selected internal disks become physical volumes in the same LVM
volume group. Reefy does not add mirroring or parity, so this is capacity
aggregation rather than a redundant storage design.

### Default data LV

`reefy_default` has a virtual size equal to the thin pool and is mounted at
`/mnt/reefy-data`. It contains Docker storage, ordinary app-volume
directories, caches, and the state directory when the separate state LV is
not available.

Fresh default LVs use XFS because dynamic inode allocation avoids the large
up-front inode-table cost seen with full-pool-size ext4 volumes. Existing ext4
or f2fs filesystems are detected and mounted without reformatting.

### Thick state LV

Fresh volume groups reserve `reefy_state` before the thin pool consumes the
remaining space. Its size is:

```text
min(4 GiB, 10 percent of VG size), with a 256 MiB floor
```

It is a thick XFS LV outside the thin pool and mounts at
`/mnt/reefy-data/state`. This keeps device identity, MQTT configuration,
desired state, LAN certificates, and control-plane files writable even if app
or Docker activity fills the thin pool.

Existing devices whose pool already owns all free extents cannot add this LV
in place. They continue storing state on `reefy_default` until reprovisioned
or manually migrated.

### Per-app thin LVs

An app volume receives its own thin LV when it is backup-enabled or declares a
capacity percentage. Reefy derives a stable LV name from the absolute host
path, creates XFS on first use, and mounts it before Docker.

Backup volumes need their own LV so Reefy can take a consistent LVM snapshot.
Capped volumes use the thin LV's virtual size as containment: when a manifest
sets `cap_pct`, only that percentage of the pool is addressable by the volume.
An uncapped backup LV uses the full pool virtual size but consumes physical
chunks only as data is written.

Reefy does not mount a new LV over a non-empty legacy directory. It preserves
the existing files and leaves that volume on the default LV rather than
hiding data.

## Encryption and key handling

Every newly provisioned persistent data device is formatted as LUKS2. A single
fresh 44-character base64 key is written at the start of partition 3 and used
for all data devices provisioned in that operation.

The key partition is raw, has no filesystem, and is marked with the Microsoft
Reserved GPT type so desktop operating systems normally ignore it. The full
1 MiB partition is filled with random data before the key bytes are written at
the known offset.

LUKS is opened with discard pass-through and crypto-CPU submission enabled.
These flags are persisted in the LUKS header on provisioning. Filesystems also
mount with discard so deleted thin-pool chunks can reach the physical device.

This design protects an internal data disk removed without the Reefy boot
disk. It does not protect against an attacker who obtains both the boot disk
and the encrypted data disks, because the unlock key is on the boot disk. A
future TPM-sealed key can strengthen that boundary without changing the LUKS
or LVM layout.

## Adoption flow

Before adoption, `/mnt/reefy-data` may be only a writable rootfs-overlay
directory. Adoption calls the shared storage implementation:

1. Find the disk containing `reefy-a` or `reefy-b`.
2. Create partition 3 when missing.
3. Look for existing Reefy-encrypted internal disks that the current key can
   open, supporting a restore or reattachment case.
4. If the desired storage list names internal disks, tear down existing
   device-mapper layers, wipe signatures and the first 4 MiB, and provision
   those whole disks.
5. Otherwise create partition 4 and provision it as the USB fallback.
6. Write one fresh key, create and open the LUKS containers, and create LVM
   physical volumes.
7. Create or extend VG `reefy`.
8. Create `reefy_state`, then `reefy_pool`, then `reefy_default` when this is a
   fresh layout.
9. Copy bootstrap state aside, mount the persistent LV, restore state, mount
   `reefy_state`, and create standard directories.

The new storage becomes active during the adoption apply. No reboot is
required.

Provisioning selected disks is destructive by design. The dashboard and
desired state identify the devices to use; the data plane wipes them before
creating Reefy's encrypted stack.

## Boot flow

`boot-reefy-storage.sh` follows an internal-first, USB-fallback policy:

1. Mount the active A/B ESP read-only at `/mnt/reefy`.
2. Resolve partition 3 as the key file.
3. Scan every non-boot block device for LUKS and try Reefy's key.
4. Activate VG `reefy` and mount `reefy_default`, or the legacy `data` LV,
   when found.
5. Mount `reefy_state` at the nested state path when it exists.
6. If no internal stack mounted, open USB partition 4 and mount the same LVM
   layout or a supported legacy direct filesystem.
7. If neither path is persistent, create bootstrap state directories in the
   writable rootfs overlay.

The script detects the actual LV filesystem before choosing mount options.
XFS gets `noatime,discard`; ext4 keeps
`noatime,commit=60,discard`; legacy f2fs uses `noatime`.

## App-volume boot ordering

`reefy-app-volumes.service` runs after base storage and before Docker. It reads
the persisted desired state, collects backup paths and capped-volume paths,
and mounts each thin LV through a shared file lock.

This closes a boot race where Docker could restore a container before its
bind-mount target was mounted. The operation is idempotent, and the running
reconciler uses the same locked primitive for app install or removal.

When an app instance is deleted, the data plane waits until Compose has
removed its container, unmounts its per-volume LVs, and removes them. Merely
disabling backup does not delete the volume because the instance still appears
elsewhere in desired state.

## Directory structure

```text
/mnt/reefy/
|-- EFI/Boot/bootx64.efi
|-- mqtt/
|   |-- mqtt.conf
|   |-- ca.crt
|   |-- bootstrap.crt
|   `-- bootstrap.key
`-- reefy/                         device-specific files preserved by OTA

/mnt/reefy-data/
|-- state/
|   |-- mqtt.conf
|   |-- device-uuid
|   |-- device.crt
|   |-- device.key
|   |-- desired-state.json
|   |-- docker-compose.json
|   |-- backup/
|   |-- lan/
|   `-- llm-proxy/
|-- apps/
|   `-- <instance-uuid>/
|       `-- <volume-name>/
|-- docker/
`-- cache/
```

Docker's configured data root is `/mnt/reefy-data/docker`.

## Compatibility behavior

The implementation retains read and mount compatibility for earlier layouts:

- `sbnb-a` and `sbnb-b` labels can still identify a legacy boot disk.
- A flat VG `reefy` LV named `data` is mounted when `reefy_default` is absent.
- Existing ext4 app LVs are mounted and never reformatted.
- A legacy direct f2fs or ext4 filesystem inside USB LUKS partition 4 remains
  mountable.
- Existing non-empty app directories are not shadowed by new thin LVs.

Compatibility is intentionally conservative. Devices keep working, but some
new capabilities such as snapshot-backed per-volume backup require a fresh
thin-pool layout.

## Implementation files

| File | Responsibility |
|---|---|
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/storage.py` | Provisioning, LUKS/LVM lifecycle, mounts, app volumes, and reclaim. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/shared.py` | Shared layout constants and partition-name helper. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/boot-reefy-storage.sh` | Boot discovery and mount flow. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-mount-volumes` | Pre-Docker per-app mount entry point. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/reefy-storage.service` | Base storage boot barrier. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/reefy-app-volumes.service` | Per-app volume boot barrier. |
| `board/reefy/reefy/rootfs-overlay/etc/docker/daemon.json` | Docker data-root configuration. |
| `docs/storage-chunk-size-study.md` | Measurements behind the 512 KiB thin-pool chunk and XFS choices. |
