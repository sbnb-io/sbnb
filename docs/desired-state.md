# Desired state architecture

## Overview

Desired state is how Reefy converges an adopted device on its
requested configuration. Reefy sends an authenticated configuration request,
and the device applies it only when it differs from the state already accepted.

The device is split into two processes:

- `reefy-control` owns MQTT, command dispatch, status, and stage reporting.
- `reefy-reconciler` owns storage, files, backups, and Docker Compose. Control
  calls it over the local `io.reefy.Reconciler` Varlink interface.

This split keeps the MQTT control path available if storage or container work
is slow, out of memory, or temporarily wedged.

## Data flow

```text
Requested device configuration
              |
              v
Authenticated control channel ---------> reefy-control
                                               |
                                               | local Varlink call
                                               v
                                         reefy-reconciler
                                           persist request
                                           reconcile host + apps
                                               |
Device status and convergence <---------------'
```

## Configuration contents

The requested configuration can include:

1. Device identity, hostname, tunnel credentials, hardware, and user.
2. Installed app instances, their pinned images, ports, slots, and overrides.
3. User SSH keys, Wi-Fi, static network addresses, and storage selection.
4. Optional provider integrations and app capabilities.
5. App volumes, seed files, restore requests, and backup configuration.

Reefy treats the request as one coherent configuration. If it cannot safely
represent every installed app, it leaves the device's accepted configuration
unchanged rather than sending a partial request that could remove a workload.

## App ports and routes

Each web app receives an available device-facing port. For a normal
bridge-network app, Reefy forwards authenticated traffic to the app's declared
container port without exposing that listener directly:

```text
LAN URL port       assigned by Reefy
container listener declared by the app, for example 8080
```

LAN and remote access pass through Reefy's authenticated routing layer. The
app port is never bound directly to all host interfaces.

Host-network apps are an exception. Reefy routes to the app's declared port,
or injects `APP_PORT_<default_port>` when the manifest declares
`dynamic_port: true`.

Interactive terminals use Reefy's authenticated terminal bridge and do not
open a separate public listener per app.

## When state is pushed

Reefy publishes a configuration update when:

- a device is adopted;
- an adopted device comes online with pending changes;
- a user changes apps, versions, environment, Wi-Fi, network, storage, SSH
  keys, backup settings, or attached providers; or
- the user requests a manual resync.

An unchanged request is not applied again. A genuine configuration change is
sent immediately.

## Device-side apply order

For a new command, the data plane reads the old persisted state before saving
the new document. That old/new diff is required for safe cleanup of removed
static IPs and deleted per-app logical volumes.

The current apply order is:

1. Persist `/mnt/reefy-data/state/desired-state.json`.
2. Set the requested hostname, or restore the MAC-derived default.
3. Apply Wi-Fi, using the old state to remove obsolete configuration.
4. Provision or activate encrypted storage when requested.
5. Apply static network addresses with old/new diff cleanup.
6. Rewrite user SSH keys and synchronize per-app system users.
7. Create and mount app volumes, including capped or backup-backed thin LVs.
8. Write backup configuration and restore requested archives before apps start.
9. Apply allow-listed rendered files under `/mnt/reefy-data/apps/` or
   `/mnt/reefy-data/state/`.
10. Write Docker Compose and run `docker compose up -d --pull missing
    --remove-orphans`.
11. Reclaim per-volume LVs belonging to instances removed from the new state.

Before Compose runs, the reconciler removes optional device mappings whose
`/dev` nodes are absent. It also verifies that every registered instance has a
matching Compose service. An inconsistent state is rejected rather than
allowed to remove an app as an orphan.

## Concurrency and boot reconciliation

Both control and data-plane paths serialize applies. If a newer state arrives
while an apply is running, the newest pending payload is queued and drained
before the lock is released.

At boot, `reefy-reconciler` starts applying its saved state in a background
thread while it brings up the Varlink socket. On MQTT connection,
`reefy-control` calls `Reconcile` rather than reading or overwriting the state
file itself. This preserves the data plane's ownership of old/new diffs and
closes the startup race between offline reconciliation and the first server
push.

## Convergence

After a successful apply, the device reports a deterministic fingerprint of
the configuration it accepted. A matching fingerprint means no newer request
needs to be applied. Per-app health remains the authority for whether every
workload is actually running.

Control reports `applying` before the local data-plane call, reports
convergence after success, waits for proxy health, and then reports `ready`.
An apply failure reports `error` and does not claim convergence.

## Compose failure policy

Compose is attempted at most five times. Failures are classified:

| Class | Behavior |
|---|---|
| Image missing or access denied | Fail immediately because retrying cannot fix the reference. |
| No space | Prune images once without volumes; retry only if space was reclaimed. |
| Docker layer or overlay corruption | Prune once with anonymous volumes, then retry. |
| Network, registry, timeout, or other transient error | Exponential backoff of 10, 20, 40, 80 seconds between attempts. |

After terminal failure, the reconciler stores a signature of the failed
Compose document. An unchanged state is not repeatedly pulled on every
reconnect. A changed state or a successful apply clears the guard.

## Device implementation files

| File | Responsibility |
|---|---|
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/control.py` | Authenticated command handling, stage reporting, and local data-plane calls. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/dataplane.py` | Persist and apply configuration, Compose, backups, files, and cleanup. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/storage.py` | Encrypted storage and per-volume lifecycle. |
| `board/reefy/reefy/rootfs-overlay/usr/share/varlink/io.reefy.Reconciler.varlink` | Control-to-data-plane method contract. |
