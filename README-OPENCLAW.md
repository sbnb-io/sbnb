# OpenClaw Ansible Role

Deploy and manage [OpenClaw](https://openclaw.ai/) - your personal AI assistant - as a Docker container on [AI Linux (Sbnb Linux)](https://github.com/sbnb-io/sbnb). Just bring a bare metal PC and you'll have the OS and OpenClaw running in minutes, fully automated.

## Quick Start

```bash
export VM_HOST=your-vm-hostname

# Deploy OpenClaw
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/run-openclaw.yml
```

After deployment, the playbook outputs the Web UI URL with authentication token.

## Configuration

OpenClaw configuration is done via the CLI after initial deployment. The role preserves existing config on subsequent runs.

### Set Telegram Bot Token

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='config set channels.telegram.botToken YOUR_BOT_TOKEN'"
```

### Set OpenAI API Key

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='config set env.OPENAI_API_KEY sk-proj-XXX'"
```

### Set Default Model

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='config set agents.defaults.model.primary openai/gpt-5.1-codex'"
```

### Apply Configuration Changes

After changing config, restart the container to apply:

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/run-openclaw.yml
```

### Telegram Pairing

Now you're ready to use your Telegram bot. Send `/start` to your bot in Telegram and it will respond with a pairing code. Approve the pairing request:

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='pairing approve telegram PAIRING_CODE'"
```

### Web UI Access

You can access the OpenClaw Web UI using the URL displayed after deployment. When accessing the Web UI for the first time, you'll need to approve your device. Approve all pending device requests:

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-approve-devices.yml
```

## Available Playbooks

| Playbook | Description |
|----------|-------------|
| `run-openclaw.yml` | Deploy/restart OpenClaw container |
| `openclaw-cli.yml` | Run any OpenClaw CLI command |
| `openclaw-approve-devices.yml` | Approve all pending device pairing requests |

## Common Operations

### Run Any CLI Command

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='YOUR_COMMAND'"
```

### List Devices

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='devices list'"
```

### Check Health

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='doctor'"
```

### View Logs

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='logs'"
```

### View Current Configuration

Read the config file directly:

```bash
ansible -i $VM_HOST, all -b -m command -a "docker exec openclaw cat /home/node/.openclaw/openclaw.json"
```

Or get full system status including configuration:

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='status --all --deep'"
```

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `sbnb_openclaw_container_name` | `openclaw` | Docker container name |
| `sbnb_openclaw_image` | `ghcr.io/openclaw/openclaw:latest` | Docker image |
| `sbnb_openclaw_port` | `18789` | Gateway port |
| `sbnb_openclaw_data_path` | `/mnt/sbnb-data/openclaw/.openclaw` | Data directory |
| `sbnb_openclaw_bind` | `lan` | Bind mode: `lan` or `loopback` |
| `sbnb_openclaw_tailscale_serve` | `true` | Enable Tailscale Serve for HTTPS |
| `sbnb_openclaw_https_port` | `8443` | HTTPS port via Tailscale Serve |
| `sbnb_openclaw_state` | `started` | Container state: `started` or `absent` |
| `sbnb_openclaw_recreate` | `false` | Force recreate container |

## Supported Model Providers

| Provider | Environment Variable | Example Model |
|----------|---------------------|---------------|
| OpenAI | `OPENAI_API_KEY` | `openai/gpt-5.1-codex`, `openai/gpt-4o` |
| Anthropic | `ANTHROPIC_API_KEY` | `anthropic/claude-opus-4-6` |
| Google | `GEMINI_API_KEY` | `google/gemini-3-pro-preview` |
| Groq | `GROQ_API_KEY` | `groq/*` |
| Ollama | (local) | `ollama/llama3.3` |

## Interactive Setup (SSH)

For the full onboarding wizard, SSH to the VM:

```bash
ssh $VM_HOST
sudo docker exec -it openclaw node dist/index.js onboard
```

## Stop OpenClaw

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/run-openclaw.yml \
  -e "sbnb_openclaw_state=absent"
```

## Migration

Migrate from an existing OpenClaw installation to AI Linux.

### Step 1: Create Backup on Source Machine

SSH to your existing OpenClaw host and create a backup archive:

```bash
# .openclaw folder contains all configs and data
# Usually located at ~/.openclaw, /home/openclaw/.openclaw, or /home/node/.openclaw
cd ~
tar -czvf openclaw-backup.tgz .openclaw
```

### Step 2: Deploy with Backup Restore

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/run-openclaw.yml \
  -e "sbnb_openclaw_backup_local_file=~/openclaw-backup.tgz"
```

The playbook automatically copies the backup to the remote host and restores it.

The backup includes your configuration, approved devices, conversation history, and credentials. After restore, verify with:

```bash
ansible-playbook -i $VM_HOST, collections/ansible_collections/sbnb/compute/playbooks/openclaw-cli.yml \
  -e "cmd='doctor'"
```

## Documentation

- [OpenClaw Documentation](https://docs.openclaw.ai/)
- [Configuration Reference](https://docs.openclaw.ai/gateway/configuration)
- [Model Providers](https://docs.openclaw.ai/concepts/model-providers)
- [CLI Reference](https://docs.openclaw.ai/cli)
- [Security Guide](https://docs.openclaw.ai/gateway/security)
