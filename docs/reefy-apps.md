# Reefy apps

## What a Reefy app is

A Reefy app is a versioned container package that the Reefy service can
install and operate on an adopted device. The package is more than a Docker
image. Its manifest can declare:

- display metadata and release history;
- the container port and launch command;
- persistent volumes, ownership, backup, and capacity policy;
- environment defaults and generated credential files;
- GPU, device, memory, process, and networking requirements; and
- supported Reefy platform capabilities.

Reefy converts the package and a user's per-instance choices into the device's
desired state. The device creates storage, renders Docker Compose, starts the
container, and reports lifecycle events through MQTT.

The current catalog is curated by Reefy. An `app.json` file is not uploaded or
executed directly by an arbitrary device user.

## Catalog and release model

At runtime, the Reefy service reads app definitions from the `app_catalog`
PostgreSQL table and keeps an in-memory catalog. The bundled `apps/`
directories are a cold-start fallback when the table is empty or unavailable.

Catalog sources can be:

- authored manifests in `reefy-service/apps/<slug>/`; or
- a manifest fetched from a pinned external repository and path declared in
  `catalog-sources.yaml`.

The publishing tool validates and assembles the app definition, preserves
release history, upserts it into the catalog table, and asks the running
service to reload. Publishing one app does not require a backend-container
restart.

When a user installs an app, the selected image is pinned on that instance.
A later catalog update can appear as an available version, but it does not
silently retarget existing instances. The user chooses when to change the
version, and the next desired-state apply recreates the container from the
selected image.

## Installation flow

```text
Catalog app + user settings
          |
          v
device_instances row
  instance UUID, name, slot, image, env, host port
          |
          v
reefy-service builds complete desired state
          |
          v
MQTT apply_state command
          |
          v
reefy-control -> Varlink -> reefy-reconciler
          |
          |-- prepare persistent volumes
          |-- restore backup when requested
          |-- write seed and rendered files
          |-- generate Docker Compose
          `-- docker compose up -d --pull missing --remove-orphans
```

The stable instance UUID is used as the Compose service name and storage
directory. A display name can change without moving app data.

## Minimal manifest

```json
{
  "slug": "hello-reefy",
  "name": "Hello Reefy",
  "description": "Small example web application",
  "image": "ghcr.io/example/hello-reefy:1.0.0",
  "version": "1.0.0",
  "default_port": 8080,
  "volumes": {
    "data": {
      "mount": "/var/lib/hello-reefy",
      "uid": 1000,
      "backup": ["borgbase"]
    }
  },
  "env": {
    "LOG_LEVEL": "info"
  },
  "gpu": false,
  "tags": ["example", "web"]
}
```

The directory name must equal `slug`. For an authored app this file would be:

```text
apps/hello-reefy/app.json
```

The complete field contract is maintained in the
[Reefy App Spec](https://github.com/reefyai/reefy-service/blob/main/APP-SPEC.md).

## Core manifest fields

| Field | Meaning |
|---|---|
| `slug` | Stable catalog identifier and directory name. |
| `name`, `description`, `icon`, `tags` | Catalog presentation and search metadata. |
| `image` | Default container image reference. Production packages should use an immutable tag or digest. |
| `version` | Human-readable Reefy package version shown to users and injected as `REEFY_APP_VERSION`. |
| `releases` | Selectable `[{"version": ..., "image": ...}]` history, newest first. |
| `default_port` | One container TCP port for the app UI or API. Omit for terminal-only apps. |
| `entrypoint`, `command`, `working_dir` | Container process overrides. |
| `env` | Default environment merged with per-instance overrides. |
| `volumes` | Persistent bind mounts with ownership, backup, exclusions, and optional cap. |
| `gpu` | Request NVIDIA CDI access when the device reports an NVIDIA GPU. |
| `needs_llm` | Permit automatic Reefy LLM-proxy wiring. Defaults to true. |
| `capabilities` | Supported device-side Reefy API permissions requested at install. |

Restricted fields such as `privileged`, `devices`, `host_mounts`,
`cap_add`, `network_mode`, `sysctls`, `tmpfs`, and `cgroupns` can weaken
container isolation. They are for reviewed catalog apps with a specific
hardware or kernel requirement, not general defaults.

## Port allocation and routing

Reefy allocates ports per device in pairs. The first instance receives:

```text
host_port = 10001
tty_port  = 10002
```

The next instance starts after the highest reserved value, so it normally
receives 10003 and 10004. `tty_port` remains reserved for database and client
compatibility; interactive terminals now use the MQTT terminal bridge and do
not listen on that TCP port.

For an ordinary bridge-network app whose `default_port` is 8080:

```text
user-facing LAN port       10001
loopback internal port     20001  (host_port + 10000)
container port              8080  (default_port)
compose bind               127.0.0.1:20001:8080
```

`reefy-proxy` terminates authenticated LAN HTTPS on port 10001 and forwards to
the loopback address. Remote access uses an authenticated slot hostname such
as:

```text
https://<device>--s<slot>--<public-id>.reefy.ai
```

The app's container port is not exposed directly on all host interfaces.
Different apps can reuse the same `default_port` because every container has
its own network namespace.

Host-network apps are reviewed exceptions. A fixed-port host app is routed to
its declared `default_port`. A manifest with `dynamic_port: true` receives the
allocated internal value through an environment variable named
`APP_PORT_<default_port>` and must listen on that value.

The fixed and dynamic ranges are tracked in the
[Reefy port allocation registry](https://github.com/reefyai/reefy-service/blob/main/docs/PORT-ALLOCATION.md).

## Container networks

Normal apps join the default Compose bridge and receive one DNS alias derived
from their instance name. There is no shared bare app-slug alias, because two
instances of the same app would collide or round-robin unexpectedly.

Conditional platform networks are added only when needed:

- `reefy-llm`: joins apps automatically when the device has attached LLM
  credentials, the app has not opted out, and the user did not provide their
  own supported LLM environment variables.
- `reefy-app-api`: joins apps that declare a supported capability and receive
  a scoped token.

Host-network apps cannot resolve these sidecars by Compose service name and
therefore do not receive automatic sidecar wiring.

## Environment injected by Reefy

Every user app receives identity metadata after user overrides are merged, so
an override cannot impersonate a different instance:

| Variable | Value |
|---|---|
| `REEFY_APP` | Display name. |
| `REEFY_APP_SLUG` | Catalog slug. |
| `REEFY_APP_VERSION` | Version associated with the installed image. |
| `REEFY_APP_INSTANCE` | User-visible instance name. |
| `REEFY_DEVICE_UUID` | Stable device UUID. |
| `REEFY_DEVICE_NAME` | Device display name. |

An app routed through the LLM proxy also receives an OpenAI-compatible base
URL. A capability-enabled app receives `REEFY_API_URL` and a per-instance JWT
whose claims contain the instance ID, app slug, and granted capabilities.

## Reefy app API

The device-side `reefy-app-api` is emitted only when at least one installed app
requests a supported capability. Version 1 supports `notify`.

Install is the grant. The backend mints a token scoped to the manifest's
declared and platform-supported capabilities. The sidecar holds no device
certificate. It forwards notification metadata to `reefy-control` through a
dedicated Varlink socket, and attachments use a shared volume that
`reefy-proxy` can serve through a token-validated route.

A manifest requests it with:

```json
{
  "capabilities": ["notify"]
}
```

Unsupported capability names are not added to the token.

## Persistent volumes and seed files

A volume declaration maps a stable host path into the container:

```text
/mnt/reefy-data/apps/<instance-uuid>/<volume-name>
```

```json
{
  "volumes": {
    "config": {
      "mount": "/app/config",
      "uid": 1000,
      "backup": ["borgbase"],
      "excludes": ["cache/**"],
      "cap_pct": 20
    }
  }
}
```

- `uid` controls ownership of the host directory.
- `backup` selects supported backup targets. `[]` means no backup.
- `excludes` adds app-specific shell-glob exclusions.
- `cap_pct` gives the volume a thin LV limited to that percentage of the app
  pool.

Files under `seed/<volume-name>/` are base64-embedded in desired state and
written only when the destination does not exist. A restored file therefore
wins over a seed. Seed files are for first-run defaults, not configuration
updates.

Backup-enabled volumes get their own thin LV for snapshot consistency.
Containers whose requested restore fails are excluded from that Compose apply
so they cannot start against empty data.

## Generated files and credentials

`template_files` can render a catalog-owned `string.Template` with an attached
credential payload. Reefy writes the result under the instance's
`.credentials` directory with an allow-listed path, mode, and UID, then
bind-mounts it at the requested container path.

The browser sees only the credential `data_key`, not the template or target
path. Files are written with `if_absent` behavior so an app that refreshes its
own token is not overwritten by the next resync.

The separate `files` manifest field downloads public files into declared
volumes when absent. It is useful for models or static assets that should not
be baked into the image.

## GPU and device access

`"gpu": true` requests `nvidia.com/gpu=all` through NVIDIA CDI only when the
device inventory reports an NVIDIA controller. On a device without NVIDIA
CDI, Reefy omits the directive instead of making Docker fail the app.

Entries in `devices` are also optional at runtime. The device data plane drops
a mapping whose host `/dev` node does not exist, allowing an app to use an
accelerator when present and fall back otherwise.

`privileged`, broad host mounts, and host networking cross stronger isolation
boundaries. Catalog reviewers should prefer a small `cap_add` or specific
device mapping whenever possible.

## Versioning

The Reefy version identifies the full package, including manifest settings,
seed files, templates, and integration behavior. For a Reefy revision based on
an upstream release, use:

```text
<upstream-tag>-reefy.<revision>
```

Use an unchanged upstream image for a manifest-only wrapper when independent
rollback of wrapper revisions is not needed. Build a Reefy derivative image
when files inside the image change or every wrapper revision must remain
independently selectable.

The full rules, including release de-duplication and OCI labels, are in the
[App Spec versioning section](https://github.com/reefyai/reefy-service/blob/main/APP-SPEC.md#versioning-apps-based-on-upstream-containers).

## Adding or publishing an app

For an authored app in `reefy-service`:

1. Create `apps/<slug>/app.json` and optional `seed/` or template files.
2. Validate that `slug` equals the directory name and test desired-state
   rendering.
3. Add or update the source entry in `catalog-sources.yaml` when needed.
4. Publish the app to a target deployment with `reefy-deploy admin
   app-publish <slug>`.
5. Verify the catalog response, install on a test device, and inspect the
   generated desired state and container health.

`admin app-publish-all` bootstraps every curated source. Normal single-app
updates should use `app-publish` so unrelated catalog entries do not move.

## Related architecture

- [Desired state](https://reefy.ai/docs/internals/desired-state)
- [Storage architecture](https://reefy.ai/docs/internals/storage-architecture)
- [Security model](https://reefy.ai/docs/security-model)
- [Reefy App Spec](https://github.com/reefyai/reefy-service/blob/main/APP-SPEC.md)
- [Port allocation registry](https://github.com/reefyai/reefy-service/blob/main/docs/PORT-ALLOCATION.md)
