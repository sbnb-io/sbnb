# Boot architecture

## Overview

Reefy boots a read-only Buildroot system from one of two EFI system
partitions. Persistent state and apps live under `/mnt/reefy-data` and are
mounted before Docker starts. Network access is deliberately not a boot
prerequisite: the saved desired state is reconciled offline, and the control
plane connects to MQTT whenever a network becomes available.

The boot design has four important properties:

- A/B EFI slots make firmware updates testable and recoverable.
- Storage and per-app mounts form the container startup barrier.
- MQTT control and storage/container reconciliation run in separate processes.
- Non-critical initialization runs in parallel and does not hold up apps.

## Boot dependency graph

```text
local-fs.target
  |-- reefy-boot-watchdog.service
  |-- reefy-boot-confirm.service
  |
  `-- reefy-storage.service
        |-- reefy-app-volumes.service
        |     `-- docker.service
        |            `-- reefy-reconciler.service
        |
        |-- reefy-hostname.service
        |     `-- reefy-control.service
        |
        |-- reefy-wifi-early.service -> network-online.target
        |-- reefy-init.service
        |-- reefy-cmds.service
        |-- reefy-tunnel.service, when configured
        `-- reefy-artifacts-prepare.service
```

The diagram shows the ordering constraints, not a single serial chain. The
hostname, Wi-Fi, initialization, command, tunnel, and cached artifact branches
can run in parallel after base storage is ready.

`reefy-control` does not wait for `network-online.target`, Docker, or the data
plane. It applies cached identity, starts its reconnect loop, and remains the
communication path even when the app layer is unhealthy. `reefy-reconciler`
waits for storage, per-app mounts, and Docker because it owns Compose work.

## UEFI and A/B slots

The boot device uses two 1 GiB FAT EFI system partitions:

| Partition | Label | Role |
|---|---|---|
| 1 | `reefy-a` | EFI slot A |
| 2 | `reefy-b` | EFI slot B |

UEFI `BootCurrent` and the mounted partition identify the active slot.
`reefy-efi fix` validates entries by both label and partition UUID, removes
stale or duplicate entries, recreates missing entries, and keeps the Reefy
pair at the front of standard `BootOrder`.

An update formats and writes the inactive slot, copies device-specific files,
and sets standard UEFI `BootNext` for a one-shot trial. If `BootNext` cannot be
verified, Reefy falls back to updating the active EFI image too rather than
claiming that a safe A/B trial was scheduled.

See [A/B EFI boot and recovery](https://reefy.ai/docs/internals/a-b-firmware-updates)
for confirmation, fallback, and firmware-specific behavior.

## Base storage barrier

`reefy-storage.service` runs `/usr/bin/boot-reefy-storage.sh` as a oneshot
before Docker. It performs discovery and mounting only. Fresh partition and
filesystem creation happens later during adoption in the data plane.

The boot script:

1. Determines the active A/B slot and mounts that ESP read-only at
   `/mnt/reefy`.
2. Runs `reefy-efi fix` to repair standard boot entries.
3. Reads the LUKS key from boot partition 3.
4. Tries to open Reefy-encrypted internal disks and activate VG `reefy`.
5. Mounts `reefy_default`, or the legacy `data` LV, at `/mnt/reefy-data`.
6. Mounts the optional thick `reefy_state` LV at
   `/mnt/reefy-data/state`.
7. Falls back to the encrypted USB partition 4 stack when no internal
   Reefy VG is available.
8. Uses the writable rootfs overlay for bootstrap state when no persistent
   stack exists yet.

Opening LUKS enables discard pass-through and crypto-CPU submission. Mount
options are selected by filesystem so current XFS volumes and legacy ext4 or
f2fs volumes remain bootable.

## Per-app volume barrier

`reefy-app-volumes.service` reads the saved desired state and mounts capped or
backup-enabled per-app thin LVs before Docker. This prevents Docker's
`restart: unless-stopped` restoration from binding an empty directory before
the real volume is mounted.

The Docker drop-in uses `Wants=` rather than `Requires=`. The volume service
has a 120-second timeout, so a wedged mount degrades to Docker starting and a
later reconciler retry instead of permanently blocking the application layer.

## Control and data plane

### `reefy-control.service`

The control process starts after base storage and the MAC-derived hostname. It
loads bootstrap MQTT configuration from the active ESP and an adopted-device
override from persistent state. It has no network-online dependency and
restarts continuously with a 10-second delay.

Its responsibilities include:

- MQTT connection, registration, status, and commands;
- desired-state stages and hash publication;
- firmware update command execution; and
- Varlink calls into the data plane.

### `reefy-reconciler.service`

The data plane starts after Docker and the per-app mount barrier. It owns:

- persisted desired state;
- Wi-Fi, static network, SSH-key, and app-user reconciliation;
- encrypted storage and app volumes;
- backup, restore, and generated files; and
- Docker Compose application.

The local Varlink socket is bound promptly while saved-state reconciliation
runs in a background thread. A server state arriving during that boot apply is
queued and applied afterward.

## Wi-Fi and network readiness

`reefy-wifi-early.service` reads saved Wi-Fi settings after storage and before
`network-online.target`. The network wait override uses `--any --timeout=1`.
This short timeout is intentional: cached images and desired state can run
offline, while systemd-networkd and the MQTT reconnect loop react when Ethernet
or Wi-Fi becomes usable.

Services that truly require a network, such as optional `reefy-mgmt.service`,
can still order themselves after `network-online.target`. Core device boot does
not.

## Parallel non-critical initialization

`reefy-init.service` runs `/usr/bin/boot-reefy-init.sh` after storage. The
script does not use shell `errexit`, so individual credential or banner
failures are logged without aborting the remaining work. It creates the local
`reefy` account and first-boot credentials, preserves development SSH access
when configured, and refreshes the console banner.

Other parallel branches include:

- `reefy-cmds.service` for boot-device custom commands;
- `reefy-tunnel.service` when `/mnt/reefy/tunnel-start.sh` exists;
- `reefy-artifacts-prepare.service` for cached host-extension activation; and
- `reefy-hostname.service`, which waits for a physical interface and derives a
  stable hostname before control registration.

The boot artifact pass never downloads and does not block Docker. The per-app
reconciler prepares each missing artifact before starting only that app. A
trusted provider activator performs host-global work such as loading NVIDIA
modules and atomically generating the runtime CDI definition.

## First boot and adoption

A freshly flashed device already contains the two EFI slots. It may not yet
have partitions 3 and 4 or any persistent app storage.

The first boot therefore uses ephemeral rootfs-overlay state to register and
reach the adoption flow. During adoption, `reefy-reconciler`:

1. creates the 1 MiB key partition at slot 3 when absent;
2. provisions selected internal disks when configured, otherwise creates USB
   data partition 4;
3. writes a fresh key and creates LUKS2 containers;
4. builds VG `reefy`, a thick state LV, and the thin-pool layout;
5. migrates bootstrap state into persistent storage; and
6. mounts the finished stack without requiring a reboot.

Subsequent boots only discover and mount that storage.

## A/B boot health

On a normal boot, `BootCurrent` is already the first standard `BootOrder`
entry, so both A/B safety units exit without changing the slot.

After a `BootNext` trial:

- `reefy-boot-confirm` waits up to 300 seconds for `reefy-storage` and
  `reefy-control` to be active;
- successful health checks call `reefy-efi confirm`, which commits the active
  slot with freshly allocated standard boot entries; and
- `reefy-boot-watchdog` reboots through sysrq after 360 seconds if confirmation
  never stops it, allowing the previous default slot to boot again.

Docker and every app are not part of firmware confirmation. The control plane
must remain reachable so application problems can be repaired without
rejecting an otherwise healthy OS image.

## Failure boundaries

| Failure | Expected effect |
|---|---|
| Active ESP cannot be mounted | Bootstrap and EFI configuration are unavailable; storage logs a warning and leaves ephemeral state. |
| Persistent LUKS or LV cannot be mounted | Device uses another known stack or bootstrap overlay; control can still start when identity is available. |
| Per-app LV mount times out | Docker is allowed to start; reconciler retries mounts during state application. |
| Network is absent | Saved apps reconcile offline; MQTT keeps reconnecting. |
| Data plane crashes or hangs | MQTT control stays separate and continues restarting independently. |
| Docker fails | Control remains online; app health and desired-state stages expose the failure. |
| New A/B image does not reach control health | Boot confirmation refuses the slot and the boot watchdog returns to the previous default. |

## Implementation files

| File | Responsibility |
|---|---|
| `board/reefy/reefy/rootfs-overlay/usr/bin/boot-reefy-storage.sh` | Discover and mount active ESP and persistent storage. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-mount-volumes` | Mount per-app volumes before Docker. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-efi` | EFI entry repair, A/B update, and confirmation. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/boot-reefy-init.sh` | Local credentials and console banner. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-wifi-early` | Apply saved Wi-Fi before network readiness. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/control.py` | MQTT control plane. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/dataplane.py` | Desired-state data plane. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/storage.py` | Persistent and per-app storage implementation. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/` | Reefy unit definitions. |
