# SBNB Compute Collection

Ansible collection for managing SBNB virtual machines with QEMU/SVSM support.

## Features

- **LVM Storage Configuration**: Automatically discovers and configures unpartitioned drives into a logical volume
- **Bridge Networking**: Sets up systemd-networkd bridge for VM network access
- **Docker Configuration**: Configures Docker daemon to use the storage mount
- **QEMU VM Management**: Custom module for managing VMs with:
  - GPU passthrough (NVIDIA and AMD)
  - PCIe device passthrough
  - AMD SEV-SNP confidential computing
  - Tailscale SSH access
  - Optional data disks

## Requirements

- Ansible 2.14+
- Python 3.9+
- Docker (on target hosts)
- `docker` Python library: `pip install docker`

### Collection Dependencies

```yaml
dependencies:
  community.general: ">=7.0.0"
  community.docker: ">=3.0.0"
  ansible.posix: ">=1.5.0"
```

Install dependencies:
```bash
ansible-galaxy collection install community.general community.docker ansible.posix
```

## Installation

### From source (development)

```bash
# Clone the repository
git clone https://github.com/sbnb-io/sbnb.git
cd sbnb

# Add to ansible.cfg
[defaults]
collections_path = ./collections
```

### From Galaxy (when published)

```bash
ansible-galaxy collection install sbnb.compute
```

## Quick Start

### 1. Configure Inventory

Edit `inventory/hosts.yml`:
```yaml
all:
  children:
    gpu_hosts:
      hosts:
        worker-gpu-01:
          ansible_host: 192.168.1.101
```

### 2. Set Tailscale Key

```bash
export SBNB_VM_TSKEY="tskey-auth-xxxxx"
```

Or add to `inventory/group_vars/all.yml`:
```yaml
sbnb_vm_tskey: "{{ lookup('env', 'SBNB_VM_TSKEY') }}"
```

### 3. Run Playbook

```bash
# Start a VM
ansible-playbook sbnb.compute.start-vm \
  -i collections/ansible_collections/sbnb/compute/inventory/hosts.yml \
  -e sbnb_vm_tskey=$SBNB_VM_TSKEY

# Preview changes (dry run)
ansible-playbook sbnb.compute.start-vm \
  -i inventory/hosts.yml \
  --check --diff

# Start with custom configuration
ansible-playbook sbnb.compute.start-vm \
  -i inventory/hosts.yml \
  -e sbnb_vm_name=my-vm \
  -e sbnb_vm_vcpu=16 \
  -e sbnb_vm_mem=32G \
  -e sbnb_vm_attach_gpus=true
```

## Roles

### sbnb.compute.vm

Main role that orchestrates all components.

```yaml
- hosts: workers
  roles:
    - role: sbnb.compute.vm
      vars:
        sbnb_vm_tskey: "{{ tailscale_key }}"
        sbnb_vm_vcpu: 8
        sbnb_vm_mem: "16G"
        sbnb_vm_attach_gpus: true
```

#### Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `sbnb_vm_name` | auto-generated | VM hostname |
| `sbnb_vm_state` | `present` | VM state: present, absent, started, stopped |
| `sbnb_vm_vcpu` | `2` | Number of vCPUs |
| `sbnb_vm_mem` | `"4G"` | Memory allocation |
| `sbnb_vm_image_size` | `"10G"` | Boot disk size |
| `sbnb_vm_tskey` | **required** | Tailscale authentication key |
| `sbnb_vm_attach_gpus` | `false` | GPU passthrough: `true`, `auto`, or list of PCI addresses |
| `sbnb_vm_confidential_computing` | `false` | Enable AMD SEV-SNP |
| `sbnb_vm_data_disk_name` | - | Optional data disk name |
| `sbnb_vm_data_disk_size` | - | Data disk size |
| `sbnb_configure_storage` | `true` | Configure LVM storage |
| `sbnb_configure_networking` | `true` | Configure bridge networking |
| `sbnb_configure_docker` | `true` | Configure Docker daemon |

### sbnb.compute.storage

Configures LVM storage from unpartitioned drives.

```yaml
- hosts: workers
  roles:
    - role: sbnb.compute.storage
      vars:
        sbnb_storage_mount: /mnt/sbnb-data
        sbnb_vg_name: sbnb-vg
        sbnb_lv_name: sbnb-lv
```

### sbnb.compute.networking

Configures bridge networking for VMs.

```yaml
- hosts: workers
  roles:
    - role: sbnb.compute.networking
      vars:
        sbnb_bridge_name: br0
```

### sbnb.compute.docker

Configures Docker daemon to use storage mount.

```yaml
- hosts: workers
  roles:
    - role: sbnb.compute.docker
      vars:
        sbnb_docker_data_root: /mnt/sbnb-data/docker
```

## Modules

### sbnb.compute.qemu_vm

Manages QEMU VMs running in Docker containers.

```yaml
- name: Start a VM
  sbnb.compute.qemu_vm:
    name: my-vm
    state: present
    vcpu: 8
    mem: "16G"
    tskey: "{{ tailscale_key }}"
    gpus: auto
    confidential_computing: false
  register: vm

- name: Show VM info
  debug:
    msg: "VM {{ vm.name }} running in container {{ vm.container_short_id }}"
```

#### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `name` | yes | - | VM name |
| `state` | no | `present` | present, absent, started, stopped |
| `vcpu` | no | `2` | Number of vCPUs |
| `mem` | no | `"4G"` | Memory |
| `tskey` | yes* | - | Tailscale key (*required for present/started) |
| `gpus` | no | `false` | GPU passthrough |
| `pcie_devices` | no | `[]` | PCIe devices to pass through |
| `confidential_computing` | no | `false` | Enable AMD SEV-SNP |
| `image_url` | no | Ubuntu Noble | Cloud image URL |
| `image_size` | no | `"10G"` | Boot disk size |
| `data_disk_name` | no | - | Secondary disk name |
| `data_disk_size` | no | - | Secondary disk size |
| `storage_path` | no | `/mnt/sbnb-data` | Storage directory |
| `bridge` | no | `br0` | Network bridge |
| `container_image` | no | `sbnb/svsm` | QEMU container image |
| `persist_boot_image` | no | `true` | Keep boot disk across restarts and on remove |

#### Return Values

| Key | Description |
|-----|-------------|
| `name` | VM name |
| `state` | Current state |
| `container_id` | Docker container ID |
| `container_short_id` | Short container ID |
| `gpus_attached` | List of attached GPU PCI addresses |
| `image_path` | Path to VM boot image |

## Playbooks

The collection includes example playbooks:

- `sbnb.compute.start-vm` - Start a VM
- `sbnb.compute.stop-vm` - Stop a VM
- `sbnb.compute.remove-vm` - Remove a VM

## Variable Precedence

Variables are loaded in this order (later overrides earlier):

1. Role defaults (`roles/vm/defaults/main.yml`)
2. Group vars (`inventory/group_vars/all.yml`)
3. Group-specific vars (`inventory/group_vars/gpu_hosts.yml`)
4. Host vars (`inventory/host_vars/worker-01.yml`)
5. Playbook vars
6. Extra vars (`-e`)

## Examples

### Start a simple VM

```yaml
- hosts: workers
  tasks:
    - sbnb.compute.qemu_vm:
        name: dev-vm
        tskey: "{{ lookup('env', 'SBNB_VM_TSKEY') }}"
```

### Start a GPU-enabled ML VM

```yaml
- hosts: gpu_workers
  tasks:
    - sbnb.compute.qemu_vm:
        name: ml-trainer
        vcpu: 64
        mem: "128G"
        tskey: "{{ tailscale_key }}"
        gpus: auto
        data_disk_name: datasets
        data_disk_size: "1T"
```

### Start a confidential computing VM

```yaml
- hosts: secure_workers
  tasks:
    - sbnb.compute.qemu_vm:
        name: secure-workload
        vcpu: 8
        mem: "32G"
        tskey: "{{ tailscale_key }}"
        confidential_computing: true
```

## License

MIT

## Author

SBNB Team
