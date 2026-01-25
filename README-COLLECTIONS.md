# SBNB Compute Ansible Collection

The `sbnb.compute` collection provides modules and roles for managing QEMU virtual machines with GPU passthrough, Tailscale networking, and optional AMD SEV-SNP confidential computing support.

## Prerequisites

- Docker installed and running on target host
- Python docker library: `pip install docker`
- Tailscale authentication key (get from https://login.tailscale.com/admin/settings/keys)

## Quick Start

Create a VM with all GPUs attached, using maximum available resources:

```bash
ansible-playbook -i myhost, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=my-vm \
  -e sbnb_vm_vcpu=max \
  -e sbnb_vm_mem=max
```

Note: The trailing comma after `myhost,` is required for single-host inventory.

## Examples

### Create a Basic VM

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=dev-vm
```

### Create a VM with Custom Resources

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=dev-vm \
  -e sbnb_vm_vcpu=8 \
  -e sbnb_vm_mem=32G \
  -e sbnb_vm_image_size=100G
```

### Create a VM with Maximum Resources

Automatically uses all available CPUs minus 2 and all available memory minus 2GB:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=max-vm \
  -e sbnb_vm_vcpu=max \
  -e sbnb_vm_mem=max
```

### Create a VM Without GPUs

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=no-gpu-vm \
  -e sbnb_vm_attach_gpus=false
```

### Create a VM with Specific GPUs

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=gpu-vm \
  -e '{"sbnb_vm_attach_gpus": ["0000:01:00.0", "0000:41:00.0"]}'
```

### Create a VM with Data Disk

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=data-vm \
  -e sbnb_vm_data_disk_name=my-data \
  -e sbnb_vm_data_disk_size=500G
```

### Create a VM with Root Password (for Console Access)

Useful when Tailscale is unavailable and you need console access:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=console-vm \
  -e sbnb_vm_root_password=mysecretpassword
```

Access console via: `docker attach <vm-name>`

### Create a VM with Custom Tailscale Tags

Tags must be pre-authorized in your Tailscale ACL policy:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=tagged-vm \
  -e sbnb_vm_tailscale_tags=tag:sbnb,tag:dev
```

### Create a VM with Confidential Computing (AMD SEV-SNP)

Requires AMD EPYC processor with SEV-SNP support:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=secure-vm \
  -e sbnb_vm_confidential_computing=true
```

### Stop a VM

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/stop-vm.yml \
  -e sbnb_vm_name=my-vm
```

### Remove a VM (Keep Boot Disk)

By default, the boot disk is preserved for later reuse:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/stop-vm.yml \
  -e sbnb_vm_name=my-vm \
  -e sbnb_vm_remove=true
```

### Remove a VM and Delete Boot Disk

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/stop-vm.yml \
  -e sbnb_vm_name=my-vm \
  -e sbnb_vm_remove=true \
  -e sbnb_vm_persist_boot_image=false
```

### Skip Host Configuration

If storage, networking, and Docker are already configured:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=my-vm \
  -e sbnb_configure_storage=false \
  -e sbnb_configure_networking=false \
  -e sbnb_configure_docker=false
```

### Multiple Hosts

```bash
ansible-playbook -i "host1,host2,host3," collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_vcpu=max \
  -e sbnb_vm_mem=max
```

Note: VM names will be auto-generated (e.g., `sbnb-vm-quickly-happy-dolphin`).

## Using the Module Directly

For more control, use the `sbnb.compute.qemu_vm` module in your own playbooks:

```yaml
- name: Manage VMs
  hosts: all
  tasks:
    - name: Create VM with all options
      sbnb.compute.qemu_vm:
        name: my-custom-vm
        state: present
        vcpu: 16
        mem: 64G
        image_size: 200G
        tskey: "{{ tailscale_key }}"
        gpus: true
        confidential_computing: false
        data_disk_name: my-data
        data_disk_size: 1T
        persist_boot_image: true
        root_password: "{{ root_pass }}"
        tailscale_tags: "tag:sbnb,tag:prod"
```

## Configuration Reference

### VM Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `sbnb_vm_name` | auto-generated | VM name (also used as Tailscale hostname) |
| `sbnb_vm_state` | `present` | `present`, `absent`, `started`, `stopped` |
| `sbnb_vm_vcpu` | `2` | vCPUs or `max` for auto-calculation |
| `sbnb_vm_mem` | `4G` | Memory (e.g., `4G`, `64G`) or `max` |
| `sbnb_vm_image_size` | `10G` | Boot disk size |
| `sbnb_vm_image_url` | Ubuntu Noble | Cloud image URL |
| `sbnb_vm_tskey` | **required** | Tailscale auth key |
| `sbnb_vm_tailscale_tags` | `tag:sbnb` | Tailscale tags to advertise |
| `sbnb_vm_attach_gpus` | `true` | `true`/`false` or list of PCI addresses |
| `sbnb_vm_attach_pcie_devices` | `[]` | Additional PCIe devices to passthrough |
| `sbnb_vm_confidential_computing` | `false` | Enable AMD SEV-SNP |
| `sbnb_vm_data_disk_name` | - | Optional data disk name |
| `sbnb_vm_data_disk_size` | - | Data disk size (required if name set) |
| `sbnb_vm_persist_boot_image` | `true` | Keep boot disk across restarts |
| `sbnb_vm_root_password` | - | Optional root password for console |

### Host Configuration Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `sbnb_configure_storage` | `true` | Configure LVM storage |
| `sbnb_configure_networking` | `true` | Configure bridge networking |
| `sbnb_configure_docker` | `true` | Configure Docker daemon |
| `sbnb_storage_mount` | `/mnt/sbnb-data` | Storage mount point |
| `sbnb_bridge_name` | `br0` | Network bridge name |
| `sbnb_docker_image` | `sbnb/svsm` | Container image for QEMU |

## Advanced: Using Inventory Files

For managing multiple hosts or complex configurations, use inventory files.

### Basic Inventory File

Create `inventory.yml`:

```yaml
all:
  hosts:
    gpu-server-1:
      ansible_host: 192.168.1.10
    gpu-server-2:
      ansible_host: 192.168.1.11
  vars:
    sbnb_vm_tskey: "tskey-auth-xxxxx"
    sbnb_vm_vcpu: max
    sbnb_vm_mem: max
```

Run:

```bash
ansible-playbook -i inventory.yml collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml
```

### Inventory with Host-Specific Settings

```yaml
all:
  hosts:
    small-server:
      ansible_host: 192.168.1.10
      sbnb_vm_vcpu: 4
      sbnb_vm_mem: 16G
      sbnb_vm_attach_gpus: false

    large-gpu-server:
      ansible_host: 192.168.1.11
      sbnb_vm_vcpu: max
      sbnb_vm_mem: max
      sbnb_vm_attach_gpus: true
      sbnb_vm_confidential_computing: true

  vars:
    sbnb_vm_tskey: "tskey-auth-xxxxx"
    sbnb_vm_image_size: 100G
```

### Inventory with Groups

```yaml
all:
  children:
    dev_servers:
      hosts:
        dev-1:
          ansible_host: 192.168.1.10
        dev-2:
          ansible_host: 192.168.1.11
      vars:
        sbnb_vm_tailscale_tags: "tag:sbnb,tag:dev"
        sbnb_vm_vcpu: 4
        sbnb_vm_mem: 16G

    prod_servers:
      hosts:
        prod-1:
          ansible_host: 192.168.1.20
        prod-2:
          ansible_host: 192.168.1.21
      vars:
        sbnb_vm_tailscale_tags: "tag:sbnb,tag:prod"
        sbnb_vm_vcpu: max
        sbnb_vm_mem: max
        sbnb_vm_confidential_computing: true

  vars:
    sbnb_vm_tskey: "tskey-auth-xxxxx"
```

Run on specific group:

```bash
ansible-playbook -i inventory.yml collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e target_hosts=prod_servers
```

### Using group_vars and host_vars Directories

For large deployments, organize variables in directories:

```
inventory/
├── hosts.yml
├── group_vars/
│   ├── all.yml
│   ├── dev_servers.yml
│   └── prod_servers.yml
└── host_vars/
    ├── dev-1.yml
    └── prod-1.yml
```

`inventory/group_vars/all.yml`:
```yaml
sbnb_vm_tskey: "tskey-auth-xxxxx"
sbnb_vm_image_size: 50G
```

`inventory/group_vars/prod_servers.yml`:
```yaml
sbnb_vm_vcpu: max
sbnb_vm_mem: max
sbnb_vm_confidential_computing: true
sbnb_vm_tailscale_tags: "tag:sbnb,tag:prod"
```

`inventory/host_vars/prod-1.yml`:
```yaml
sbnb_vm_name: prod-primary
sbnb_vm_data_disk_name: prod-data
sbnb_vm_data_disk_size: 2T
```

Run:

```bash
ansible-playbook -i inventory/ collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml
```

## Debugging

Use `-v` flags to increase verbosity for troubleshooting:

| Flag | Level | Description |
|------|-------|-------------|
| `-v` | 1 | Show task results |
| `-vv` | 2 | Show task input parameters |
| `-vvv` | 3 | Show connection details and QEMU command |
| `-vvvv` | 4 | Show plugin internals, connection scripts |

Example:

```bash
ansible-playbook -i gpu-server, -vvv collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey=tskey-auth-xxxxx \
  -e sbnb_vm_name=debug-vm
```

At `-vvv` level, the `qemu_vm` module will output the full QEMU command being executed.

## Connecting to VMs

Once a VM is running, connect via Tailscale SSH:

```bash
ssh <vm-name>
```

For console access (if root_password was set):

```bash
docker attach <vm-name>
```

Detach from console with `Ctrl+P Ctrl+Q`.
