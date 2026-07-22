# Desired state architecture

## Overview

Desired state is how the Reefy service converges an adopted device on its
requested configuration. The service builds one complete JSON document for a
device, sends it as an MQTT command, and compares a deterministic state hash to
avoid unnecessary re-applies.

The device is split into two processes:

- `reefy-control` owns MQTT, command dispatch, status, and stage reporting.
- `reefy-reconciler` owns storage, files, backups, and Docker Compose. Control
  calls it over the local `io.reefy.Reconciler` Varlink interface.

This split keeps the MQTT control path available if storage or container work
is slow, out of memory, or temporarily wedged.

## Data flow

```text
Reefy service                              Reefy device
-------------                              ------------
build_desired_state(uuid)
  read device, instances, catalog,
  SSH keys, providers, and backup config
  build compose, routes, files, volumes

MQTT commands topic  --------------------> reefy-control
  {"action":"apply_state","state":{...}}       |
                                                  | Varlink ApplyState
                                                  v
                                            reefy-reconciler
                                              read old state
                                              save new state
                                              reconcile host + apps

retained state_hash <---------------------- reefy-control
device status/stage <---------------------- reefy-control
```

The command topic is:

```text
reefy/{public_id}/devices/{device_uuid}/commands
```

The retained hash topic is:

```text
reefy/{public_id}/devices/{device_uuid}/state_hash
```

Command delivery uses MQTT QoS 1. The state hash is retained and uses the
client's default QoS 0; a later publication replaces the retained value.

## State document

The state is an internal server-to-firmware contract. A representative shape
is:

```json
{
  "hostname": "seahorse",
  "compose": {
    "services": {
      "cloudflared": {},
      "reefy-proxy": {},
      "reefy-llm-proxy": {},
      "reefy-app-api": {},
      "<instance-uuid>": {}
    },
    "networks": {},
    "volumes": {}
  },
  "instances": [
    {
      "instance_name": "openclaw",
      "instance_uuid": "a1b2c3d4",
      "app_slug": "openclaw",
      "uid": 1000
    }
  ],
  "app_volumes": [
    {
      "path": "/mnt/reefy-data/apps/a1b2c3d4/data",
      "uid": 1000,
      "seed_files": {}
    }
  ],
  "volume_caps": {},
  "files": [],
  "user_ssh_keys": [],
  "wifi": {"ssid": "MyNetwork", "password": "..."},
  "storage": {"devices": ["nvme0n1"]},
  "network": {"addresses": ["192.0.2.10/24"]},
  "backup": {
    "schedule": "03:17",
    "retention": {"keep_last": 30},
    "instances": []
  }
}
```

Optional blocks are omitted when they are not needed. Infrastructure services
are also conditional. For example, `reefy-llm-proxy` appears only when the
device has an attached LLM provider, and `reefy-app-api` appears only when an
installed app declares a supported capability.

## Server-side build

`app/services/desired_state.py` in `reefy-service` is the builder and hash
authority. It combines:

1. Device identity, hostname, tunnel credentials, hardware, and user.
2. Installed app instances, their pinned images, ports, slots, and overrides.
3. The DB-backed app catalog, with the bundled `apps/` directory as a cold-start
   fallback when the catalog table is unavailable or empty.
4. User SSH keys, Wi-Fi, static network addresses, and storage selection.
5. LLM-provider credentials and capability-scoped app API tokens.
6. App volume, seed-file, rendered-file, restore, and backup configuration.

Instance rows are ordered by database ID before rendering. Stable ordering is
required because dictionary and list order contributes to the state hash.

If an installed instance references an app missing from the catalog, the
builder aborts the whole state build. Omitting only that app would make
`docker compose up --remove-orphans` delete a valid running container.

## App ports and routes

Each app instance receives a `host_port` starting at 10001. For a normal
bridge-network app, Reefy maps a loopback-only internal port to the app's
declared container port:

```text
LAN URL port       host_port                 10001
loopback bind      host_port + 10000         20001
container listener app.json default_port    for example 8080
compose mapping    127.0.0.1:20001:8080
```

`reefy-proxy` terminates LAN HTTPS on the allocated LAN port and forwards to
the loopback bind. Remote tunnel access uses the instance slot hostname and
also forwards through `reefy-proxy`. The app port is never bound directly to
all host interfaces.

Host-network apps are an exception. Reefy routes to the app's declared port,
or injects `APP_PORT_<default_port>` when the manifest declares
`dynamic_port: true`.

Terminals no longer use ttyd containers or per-app terminal sidecars. The
`reefy-terminal-bridge` carries host and container terminal sessions over
MQTT. The `tty_port` database field remains allocated for compatibility but is
not a listening terminal port.

## When state is pushed

The service builds and publishes state when:

- a device is adopted;
- an adopted device reports online and its saved hash differs;
- a user changes apps, versions, environment, Wi-Fi, network, storage, SSH
  keys, backup settings, or attached providers; or
- the user requests a manual resync.

Repeated online messages are deduplicated. The same server hash is not pushed
again inside the apply cooldown, while a genuinely new hash is sent
immediately.

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

## Hash and convergence

Both sides compute:

```text
first 16 hexadecimal characters of
SHA-256(json.dumps(state, sort_keys=True))
```

After a successful apply, control publishes the saved-state hash as retained
MQTT state. The service stores it on the device row and compares it with the
next server build. A matching hash means the device accepted that exact state
document and no newer configuration needs to be pushed. Per-instance health
events remain the authority for whether every app is actually running.

Control reports `applying` before the Varlink call, republishes online status
and the hash after success, waits for proxy health, and then reports `ready`.
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

## Implementation files

| Repository | File | Responsibility |
|---|---|---|
| `reefy-service` | `app/services/desired_state.py` | Build state, Compose, routes, files, and hash. |
| `reefy-service` | `app/services/mqtt.py` | Online hash comparison and deduplicated publish. |
| `reefy` | `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/control.py` | MQTT command handling, stage reporting, and Varlink client. |
| `reefy` | `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/dataplane.py` | Persist and apply state, Compose, backups, files, and cleanup. |
| `reefy` | `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/storage.py` | Encrypted storage and per-volume lifecycle. |
| `reefy` | `board/reefy/reefy/rootfs-overlay/usr/share/varlink/io.reefy.Reconciler.varlink` | Control-to-data-plane method contract. |
