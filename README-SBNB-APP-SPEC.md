# SBNB App Specification v0.2

An SBNB App is a packaged application that runs on SBNB bare metal infrastructure. This document defines the specification for developing, packaging, and deploying apps within the SBNB ecosystem.

## Overview

An SBNB App is an Ansible role with a playbook entrypoint. Apps can be developed by the SBNB team or by third-party developers. When a user deploys an app through [console.sbnb.io](https://console.sbnb.io), the platform provisions a VM on a bare metal host and executes the app's Ansible playbook against it. The execution log is streamed to the console web UI in real time.

All SBNB Apps run as Docker containers. The platform handles VM provisioning, Docker setup, secret injection, volume management, backup, and HTTPS exposure. App developers focus on their application logic.

### Lifecycle

```
User configures app in console.sbnb.io
        ↓
Platform provisions VM on bare metal host
        ↓
Platform creates volumes, injects secrets, restores backups
        ↓
Ansible playbook executes against VM
        ↓
Outputs are displayed to user in console web UI
```

### Data and Code Separation

SBNB enforces a strict separation between **code** (container images) and **data** (volumes).

**Why this matters:**

- **Hardware can disappear at any time.** Bare metal machines may fail or be swapped without notice. When this happens, the platform recreates everything from scratch: code is pulled as fresh container images, and data is restored from backups. If your app writes important data outside of declared volumes, that data will be lost.
- **Updates replace code, not data.** When an app is updated, the platform pulls a new container image and restarts the container. Declared volumes persist across updates. Any data written inside the container filesystem (e.g., `/var/lib/`, `/tmp/`, `/home/`) is destroyed on update.
- **Only declared volumes survive.** The platform only knows about volumes declared in `sbnb_myapp_volumes`. Data written to any other location — inside the container or on the host filesystem — is ephemeral and will not survive container recreation, app updates, or machine replacement.

**Rules for app developers:**

1. All persistent data must be written to declared volumes (mounted via `container_path`).
2. Never write important state to undeclared paths inside the container. It will be lost.
3. Mark volumes with `backup: true` if the data must survive machine replacement. Volumes without backup are recreated empty on a new machine.
4. Treat the container as disposable — it can be stopped, replaced, or moved to a different machine at any time.

```
┌─────────────────────────────────────┐
│           Container (Code)          │   Disposable. Pulled fresh
│  /app, /usr, /var, /tmp, ...        │   on deploy/update.
│                                     │
│  ┌──────────┐  ┌──────────┐        │
│  │ /config  │  │  /data   │        │   Declared volumes.
│  │ backup:  │  │ backup:  │        │   Persist across updates.
│  │  true    │  │  false   │        │   backup:true survives
│  └──────────┘  └──────────┘        │   machine replacement.
└─────────────────────────────────────┘
```

### Idempotency

App playbooks must be **idempotent** — running the same playbook multiple times against a running service should either repair it or be a no-op if everything is already in the correct state.

The platform may re-run the app playbook at its discretion, for example when a health check fails and the platform attempts automatic recovery. App developers should not assume the playbook runs only once. Design tasks to converge to the desired state rather than perform one-time setup actions.

**Guidelines:**

- Use `force: false` when writing config files to avoid overwriting user changes.
- Check if resources exist before creating them.
- Avoid tasks with side effects that break when executed twice (e.g., appending to a file without checking if the content is already there).

## App Structure

An SBNB App is an Ansible role hosted in a Git repository. The platform clones the repository and runs the app's entrypoint playbook against the target VM.

### Repository Layout

The app repo must contain an Ansible role and a playbook entrypoint:

```
myapp-repo/
  roles/
    myapp/
      defaults/main.yml      # Input defaults and documentation
      tasks/main.yml          # App logic
      meta/main.yml           # Metadata (description, logo, dependencies)
      templates/              # Config templates (optional)
      files/                  # Static files (optional)

  playbooks/
    run-myapp.yml            # Entrypoint playbook
```

Apps may also be packaged as Ansible collections:

```
myapp-repo/
  collections/
    ansible_collections/
      myorg/
        myapp/
          roles/myapp/...
          playbooks/run-myapp.yml
```

### App Registration

When registering an app (in the catalog or as a custom app URL), three fields identify the app's code:

| Field | Required | Description |
|-------|----------|-------------|
| `git_url` | yes | Git repository URL (e.g., `https://github.com/myorg/myapp.git`). |
| `git_ref` | no | Branch or tag to check out. Default: `main`. |
| `playbook_path` | yes | Path to the entrypoint playbook, relative to the repo root. |

**Example — standalone role repo:**
```
git_url: https://github.com/myorg/myapp.git
git_ref: v1.2.0
playbook_path: playbooks/run-myapp.yml
```

**Example — Ansible collection repo:**
```
git_url: https://github.com/sbnb-io/sbnb.git
git_ref: main
playbook_path: collections/ansible_collections/sbnb/compute/playbooks/run-openclaw.yml
```

The platform clones the repository once and caches it locally. On subsequent deploys, the platform pulls the latest changes for the specified `git_ref`. The cached repo is mounted into the provisioning container so Ansible can discover the roles and collections it contains.

### Entrypoint Playbook

The playbook is the execution entrypoint. It includes prerequisite roles before the app role:

```yaml
---
- name: Deploy My App
  hosts: all
  become: true
  gather_facts: false

  pre_tasks:
    - name: Wait for host to become reachable
      ansible.builtin.wait_for_connection:
        timeout: 600
        delay: 5

    - name: Gather facts
      ansible.builtin.setup:

  roles:
    - role: sbnb.compute.docker_vm    # Docker setup (required)
    - role: sbnb.compute.nvidia       # GPU drivers (if app needs GPU)
    - role: sbnb.compute.myapp
```

The `gather_facts: false` + `wait_for_connection` + `setup` pattern is required. When the platform provisions a new VM, the playbook starts before the VM is fully booted. The `wait_for_connection` pre-task waits up to 10 minutes for the VM to become reachable before proceeding.

### Role Metadata

```yaml
# meta/main.yml
---
galaxy_info:
  author: Your Name
  description: Short description of the app
  version: "1.0.0"
  logo_url: https://example.com/myapp-logo.png
  license: MIT
  galaxy_tags:
    - sbnb
    - myapp

# Minimum hardware requirements (optional, not enforced by default)
sbnb_requirements:
  min_vcpu: 4
  min_mem_gb: 8
  gpu: true
```

The `logo_url` is displayed in the console.sbnb.io app catalog. Use a square image, minimum 128x128 pixels.

The `sbnb_requirements` section is optional. When specified, the platform uses it to validate that the target machine meets minimum requirements before deployment and to display hardware requirements in the app catalog. All fields are optional — omit any that are not applicable.

The `version` field follows [semantic versioning](https://semver.org/). It is the **app developer's responsibility** to handle data migrations between versions — updating config formats, running database schema migrations, converting stored state, etc. Apps should store their current version in a declared volume (e.g., in a config file) and check it on startup to determine if migrations are needed.

## Inputs

Apps accept inputs through Ansible variables defined in `defaults/main.yml`. All input variables must be prefixed with `sbnb_{appname}_` to avoid conflicts.

### Parameters

Standard configuration parameters with sensible defaults:

```yaml
# defaults/main.yml
---
# Container image
sbnb_myapp_image: org/myapp:latest

# App-specific settings
sbnb_myapp_port: 8080
```

### Secrets

Secrets (API keys, tokens, passwords) must never be hardcoded in the app. Users define their secrets in console.sbnb.io, and the platform injects them at execution time using template placeholders.

**How it works:**

1. The app developer declares secret placeholders in the playbook or config templates:

```yaml
# playbooks/run-myapp.yml
---
- name: Deploy My App
  hosts: all
  become: true

  roles:
    - role: sbnb.compute.docker_vm
    - role: sbnb.compute.myapp
      vars:
        sbnb_myapp_api_key: "{MYAPP_API_KEY}"
        sbnb_myapp_bot_token: "{MYAPP_BOT_TOKEN}"
```

2. The user sets the actual secret values in console.sbnb.io (e.g., `MYAPP_API_KEY = sk-abc123...`).

3. The platform replaces `{MYAPP_API_KEY}` with the real value before executing the playbook. This keeps secrets out of version control.

**In the Ansible role**, the app developer declares which secrets the app accepts:

```yaml
# defaults/main.yml

# Secret: API key for external service (required, injected by platform)
# sbnb_myapp_api_key: ""

# Secret: Bot token (required, injected by platform)
# sbnb_myapp_bot_token: ""
```

Commented-out variables serve as documentation — they tell the platform and the user what secrets the app expects.

**Security guidelines:**
- Never log secrets in debug output. If a secret must be shown to the user (e.g., auto-generated password), use the outputs mechanism (see Outputs section).
- Prefer auto-generation over requiring users to supply secrets when possible.

### Config Files

Apps may accept user-provided configuration files or generate them from templates:

```yaml
# defaults/main.yml

# Optional: user-provided config file (local path on controller)
sbnb_myapp_local_config: ""

# Whether to overwrite existing config
sbnb_myapp_config_force: false
```

**Config handling pattern:**

```yaml
# tasks/main.yml

- name: Copy user-provided config
  ansible.builtin.copy:
    src: "{{ sbnb_myapp_local_config }}"
    dest: "{{ sbnb_myapp_config_volume }}/config.yaml"
    mode: '0644'
    force: "{{ sbnb_myapp_config_force }}"
  when: sbnb_myapp_local_config | length > 0

- name: Generate default config from template
  ansible.builtin.template:
    src: config.yaml.j2
    dest: "{{ sbnb_myapp_config_volume }}/config.yaml"
    mode: '0644'
    force: false
  when: sbnb_myapp_local_config | length == 0
```

### Volumes

App developers declare named volumes with a container mount path. The platform handles everything else: host-side storage placement, directory creation, mounting, and lifecycle. App developers do not need to know or manage host paths.

**Volume declaration in defaults:**

```yaml
# defaults/main.yml

sbnb_myapp_volumes:
  - name: config
    container_path: /config
    backup: true
    description: "Configuration files and secrets"
  - name: data
    container_path: /data
    backup: false
    description: "Runtime data (regenerated automatically)"
  - name: media
    container_path: /media
    backup: false
    description: "Media files (large, not backed up)"
```

Each volume has:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Volume name, unique within the app. Used for backup naming. |
| `container_path` | yes | Mount path inside the container. |
| `backup` | no | `true` to enable platform-managed backup. Default: `false`. |
| `description` | no | Human-readable description for console.sbnb.io. |

**Platform behavior:**
- The platform creates host directories and mounts them into the container automatically. App developers do not need to create directories or construct volume mounts.
- The platform constructs Docker volume mounts from the volume declarations and passes them to the container.

**Backup flag:**
- `backup: true` signals the platform to back up this volume regularly. Backups are restored automatically on the next app deployment if the volume is empty (e.g., after migrating to a new machine). Be mindful of what you mark for backup — it may involve storage costs.
- `backup: false` (default) means the volume is ephemeral or too large to back up.

**What to mark for backup:**

| Backup | Content type |
|--------|-------------|
| `true` | Configuration files, secrets, application state, databases, user-generated content that cannot be recreated |
| `false` | Media files, recordings, logs, caches, container images, model files (downloaded fresh on deploy) |

## Outputs

Outputs are information returned to the platform after app execution. The platform displays selected outputs to the user in the console.sbnb.io web UI.

> **Important:** Outputs with `show_to_user: true` are displayed prominently in the console.sbnb.io web UI — on the app instance detail page and in the deployment success screen. Use this for URLs, credentials, and getting-started instructions that the user needs immediately after deployment. Keep `show_to_user: false` for internal/debug values like container IDs.

### Output Format

Apps can produce any number of outputs by appending to the `sbnb_outputs` list. Each output has a `show_to_user` flag controlling whether it appears in the console web UI. Apps can emit outputs at any point during execution — they are not limited to a single predefined block.

```yaml
# tasks/main.yml

# Early in execution — report the access URL
- name: Report app URL
  ansible.builtin.set_fact:
    sbnb_outputs: "{{ sbnb_outputs | default([]) + [item] }}"
  loop:
    - name: web_ui_url
      value: "https://{{ tailscale_hostname }}:8443/"
      label: "Web UI"
      show_to_user: true

# Later — report additional info after container starts
- name: Report app status
  ansible.builtin.set_fact:
    sbnb_outputs: "{{ sbnb_outputs | default([]) + [item] }}"
  loop:
    - name: instructions
      value: "Login to the web console to configure your app. Your configuration will be backed up and restored automatically."
      label: "Getting Started"
      show_to_user: true
    - name: container_id
      value: "{{ container_result.container.Id[:12] }}"
      label: "Container ID"
      show_to_user: false
```

Each output has:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Machine-readable identifier. |
| `value` | yes | The output value — any free-form string (URL, credential, instructions, etc.). |
| `label` | yes | Human-readable label for display. |
| `show_to_user` | no | `true` to display in console web UI. Default: `false`. |

Only outputs with `show_to_user: true` are displayed in the console.sbnb.io web UI. Other outputs are available in the execution log for debugging.

### HTTPS Exposure

Apps can expose any TCP port (web UI, API endpoint, webhook, etc.) over HTTPS with a valid TLS certificate. The platform handles Tailscale Serve configuration automatically — the app just declares the port mappings.

```yaml
# defaults/main.yml

sbnb_myapp_https_ports:
  - listen: 8443
    target: 8080
  - listen: 8444
    target: 3000
```

- `listen` — the external HTTPS port exposed via Tailscale with a valid TLS certificate, accessible from the user's tailnet.
- `target` — the local port inside the container. This port is not exposed to the network directly.

This eliminates HTTPS setup complexity for app developers — no certificates, no TLS configuration, no reverse proxy. The platform provides production-grade HTTPS automatically.

The resulting URLs are available to the app via the `tailscale_hostname` fact (set by the platform) for use in outputs:

```
https://{tailscale_hostname}:{listen}/
```

## Container Deployment

All SBNB Apps run as Docker containers.

### Basic Container

```yaml
- name: Start app container
  community.docker.docker_container:
    name: "{{ sbnb_myapp_container_name }}"
    image: "{{ sbnb_myapp_image }}"
    pull: true
    restart_policy: unless-stopped
    ports:
      - "{{ sbnb_myapp_port }}:{{ sbnb_myapp_port }}"
    env: "{{ sbnb_myapp_env | default({}) }}"
  register: container_result
  retries: 10
  delay: 5
  until: container_result is succeeded
```

Volume mounts are constructed by the platform from `sbnb_myapp_volumes` and injected automatically. The app role may also specify additional volumes directly if needed.

### GPU Container (NVIDIA)

For apps requiring GPU acceleration, include the `sbnb.compute.nvidia` role in the playbook and use the NVIDIA runtime:

```yaml
- name: Start GPU app container
  community.docker.docker_container:
    name: "{{ sbnb_myapp_container_name }}"
    image: "{{ sbnb_myapp_image }}"
    pull: true
    runtime: nvidia
    device_requests:
      - driver: nvidia
        count: -1
        capabilities:
          - - gpu
            - nvidia
    shm_size: "{{ sbnb_myapp_shm_size | default('512M') }}"
    restart_policy: unless-stopped
```

### Health Check (Optional)

Apps may implement a health check endpoint. If provided, the platform uses it to verify successful deployment:

```yaml
- name: Wait for app to be healthy
  ansible.builtin.uri:
    url: "http://localhost:{{ sbnb_myapp_port }}/health"
    method: GET
  register: health_check
  retries: 30
  delay: 2
  until: health_check.status == 200
```

## Backup and Restore

Volumes with `backup: true` are managed by the platform's backup system.

### Backup Contract

- The platform selects the appropriate backup mechanism (implementation detail, may evolve).
- Backups are encrypted at rest with a platform-managed key.
- Backup frequency and retention are managed by the platform.
- App developers control only which volumes are backed up via the `backup` flag.

### Restore Behavior

On app deployment, if a backup exists and the target volume is empty, the platform restores the backup before the Ansible role runs. Apps should handle the case where restored data already exists:

```yaml
- name: Check if config exists (may have been restored from backup)
  ansible.builtin.stat:
    path: "{{ sbnb_myapp_config_volume }}/config.yaml"
  register: config_stat

- name: Generate fresh config only if none exists
  ansible.builtin.template:
    src: config.yaml.j2
    dest: "{{ sbnb_myapp_config_volume }}/config.yaml"
    mode: '0644'
  when: not config_stat.stat.exists
```

## Naming Conventions

| Item | Convention | Example |
|------|-----------|---------|
| Role name | lowercase, single word or hyphenated | `frigate`, `openclaw` |
| Playbook | `run-{appname}.yml` | `run-frigate.yml` |
| Variables | `sbnb_{appname}_*` | `sbnb_frigate_port` |
| Container name | `{appname}` (default) | `frigate` |
| Volume names | lowercase, descriptive | `config`, `data`, `media` |

## Required Variables

Every SBNB App must define these in `defaults/main.yml`:

```yaml
# Required by spec
sbnb_myapp_image: org/myapp:latest     # Container image
sbnb_myapp_volumes: []                 # Volume declarations (see Volumes section)
```

The platform manages container naming and state internally. Apps default to running state.

## Example: Minimal App

A minimal SBNB App that runs a web service:

```yaml
# roles/myapp/defaults/main.yml
---
sbnb_myapp_image: org/myapp:latest
sbnb_myapp_port: 8080

sbnb_myapp_https_ports:
  - listen: 8443
    target: 8080

sbnb_myapp_volumes:
  - name: config
    container_path: /config
    backup: true
    description: "App configuration"
  - name: data
    container_path: /data
    backup: false
    description: "Runtime data"
```

```yaml
# roles/myapp/tasks/main.yml
---
- name: Check if config exists (may be restored from backup)
  ansible.builtin.stat:
    path: "{{ sbnb_myapp_config_volume }}/config.yaml"
  register: config_stat

- name: Generate default config
  ansible.builtin.template:
    src: config.yaml.j2
    dest: "{{ sbnb_myapp_config_volume }}/config.yaml"
    mode: '0644'
  when: not config_stat.stat.exists

- name: Start container
  community.docker.docker_container:
    name: myapp
    image: "{{ sbnb_myapp_image }}"
    pull: true
    restart_policy: unless-stopped
    ports:
      - "{{ sbnb_myapp_port }}:{{ sbnb_myapp_port }}"
  register: container_result
  retries: 10
  delay: 5
  until: container_result is succeeded

- name: Set app outputs
  ansible.builtin.set_fact:
    sbnb_outputs: "{{ sbnb_outputs | default([]) + outputs }}"
  vars:
    outputs:
      - name: web_ui_url
        value: "https://{{ tailscale_hostname }}:8443/"
        label: "Web UI"
        show_to_user: true
      - name: instructions
        value: "Login to the web console to configure your app. Your configuration will be backed up and restored automatically."
        label: "Getting Started"
        show_to_user: true
```

```yaml
# playbooks/run-myapp.yml
---
- name: Deploy My App
  hosts: all
  become: true
  gather_facts: false

  pre_tasks:
    - name: Wait for host to become reachable
      ansible.builtin.wait_for_connection:
        timeout: 600
        delay: 5

    - name: Gather facts
      ansible.builtin.setup:

  roles:
    - role: sbnb.compute.docker_vm
    - role: sbnb.compute.myapp
```

## Publishing

There are two ways to make an app available to users:

- **SBNB App Catalog.** [console.sbnb.io](https://console.sbnb.io) offers a curated catalog of vetted apps that have passed security review by the SBNB team. To include your app in the catalog, submit a request to the SBNB team with your `git_url`, `git_ref`, and `playbook_path`.
- **Custom app.** Users can deploy any app they trust by providing `git_url`, `git_ref`, and `playbook_path` in the console.sbnb.io web UI. The repository must be publicly accessible (or accessible with credentials configured by the user). Custom apps are not reviewed by the SBNB team — the user assumes responsibility for the app's behavior and security.

## Changelog

- **v0.2** (2026-02-19): Added repository packaging (`git_url`, `git_ref`, `playbook_path`), required `wait_for_connection` pre-task pattern, collection repo layout.
- **v0.1** (2026-02-18): Initial specification.
