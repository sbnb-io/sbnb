# Reefy apps

## What a Reefy app is

A Reefy app is a versioned container package that Reefy can install and
operate on an adopted device. The package is more than a Docker image. Its
manifest can declare:

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

Reefy maintains a curated catalog of reviewed app packages and their available
versions. Catalog entries describe what users can install; they do not grant
arbitrary manifests permission to run on a device.

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
Reefy records the selected version and settings
          |
          v
Device receives its requested configuration
          |
          v
Device prepares storage, applies configuration, and starts the app
```

Each installation has a stable identity, so changing its display name does not
move or recreate its persistent data.

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

## Core manifest fields

| Field | Meaning |
|---|---|
| `slug` | Stable catalog identifier. Keep it unchanged across releases. |
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

Reefy assigns each web app an available user-facing port on its device. Apps
declare only the port used inside their container; they do not choose or share
the device-facing port.

For an ordinary bridge-network app whose `default_port` is 8080:

```text
device LAN URL       https://<device>:<assigned-port>
container listener  8080
```

Reefy terminates authenticated LAN HTTPS and forwards requests without
exposing the container listener directly. Remote access uses an authenticated
slot hostname such as:

```text
https://<device>--s<slot>--<public-id>.reefy.ai
```

The app's container port is not exposed directly on all host interfaces.
Different apps can reuse the same `default_port` because every container has
its own network namespace.

Host-network apps are reviewed exceptions. A fixed-port host app is routed to
its declared `default_port`. A manifest with `dynamic_port: true` receives the
assigned value through an environment variable named
`APP_PORT_<default_port>` and must listen on that value.

## App connectivity

Normal apps use an isolated container network. Multiple installations of the
same app can therefore reuse the same container port without conflicts.

Optional Reefy integrations are added only when needed:

- LLM-aware apps can use an attached provider without embedding provider
  credentials in the app package.
- Apps that declare a supported capability receive access limited to that
  capability.

Host-network apps are reviewed separately and do not receive automatic
container-network integrations.

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

An app connected to Reefy's LLM integration also receives an OpenAI-compatible
base URL. A capability-enabled app receives `REEFY_API_URL` and a credential
limited to that installation and its granted capabilities.

## Reefy app API

Version 1 of the Reefy app API supports `notify`. Installing an app grants only
the capabilities declared by its reviewed manifest and supported by the
platform.

A manifest requests it with:

```json
{
  "capabilities": ["notify"]
}
```

Unsupported capability names are not granted.

## Persistent volumes and seed files

A volume declaration gives an app stable Reefy-managed storage at the
container mount point it requests:

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

Seed files are written only when the destination does not exist. A restored
file therefore wins over a seed. Seed files are for first-run defaults, not
configuration updates.

Backup-enabled volumes get their own thin LV for snapshot consistency.
Containers whose requested restore fails are excluded from that Compose apply
so they cannot start against empty data.

## Generated files and credentials

`template_files` lets a reviewed app request a generated credential file at a
specific container path. Reefy creates it only when absent, so an app that
refreshes its own credential is not overwritten by the next resync.

The separate `files` manifest field downloads public files into declared
volumes when absent. It is useful for models or static assets that should not
be baked into the image.

## GPU and device access

`"gpu": true` requests `nvidia.com/gpu=all` through NVIDIA CDI only when the
device inventory reports an NVIDIA controller. On a device without NVIDIA
CDI, Reefy omits the directive instead of making Docker fail the app.

Entries in `devices` are also optional at runtime. The device data plane drops
a mapping whose host `/dev` node does not exist, allowing an app to use an
accelerator when present and fall back otherwise. Explicit CDI requests such
as `intel.com/npu=all` select their matching host provider artifact. Provider
download and activation are best effort: Reefy logs activation output, removes
any CDI request the provider did not publish, and continues starting the app.
This guarantees container lifecycle, not application-level acceleration, so
the application remains responsible for a non-accelerated fallback.

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

## Authoring an app

App sources are maintained in a dedicated app repository. Each package
contains `app.json` and may include seed or template files. Before publication,
validate the manifest, test installation on a Reefy device, verify persistent
data across updates, and confirm that requested privileges are no broader than
necessary. Catalog publication is limited to reviewed packages.

## Related architecture

- [Desired state](https://reefy.ai/docs/internals/desired-state)
- [Storage architecture](https://reefy.ai/docs/internals/storage-architecture)
- [Security model](https://reefy.ai/docs/security-model)
