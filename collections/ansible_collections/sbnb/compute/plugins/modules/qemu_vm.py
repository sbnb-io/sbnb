#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2024, SBNB Team
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: qemu_vm
short_description: Manage SBNB QEMU virtual machines with SVSM support
version_added: "1.0.0"
description:
  - Create, start, stop, and remove QEMU virtual machines
  - Supports GPU passthrough, AMD SEV-SNP confidential computing
  - Runs QEMU inside a Docker container with custom SVSM-enabled binary
  - Automatically configures cloud-init for Tailscale SSH access

options:
  name:
    description:
      - Name of the virtual machine
      - Used as container name and hostname
    type: str
    required: true

  state:
    description:
      - Desired state of the VM
      - C(present) ensures VM exists and is running
      - C(absent) ensures VM is removed
      - C(started) same as present
      - C(stopped) ensures VM is stopped but not removed
    type: str
    choices: ['present', 'absent', 'started', 'stopped']
    default: present

  vcpu:
    description:
      - Number of virtual CPUs
      - Set to "max" to auto-calculate (total CPUs - 2, reserves 2 for hypervisor)
    type: raw
    default: 2

  mem:
    description:
      - Memory allocation (e.g., "4G", "64G")
      - Set to "max" to auto-calculate (total memory - 2GB, reserves 2GB for hypervisor)
    type: str
    default: "4G"

  image_url:
    description:
      - URL to download the base cloud image
    type: str
    default: "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

  image_size:
    description:
      - Size to resize the boot disk to
    type: str
    default: "10G"

  tskey:
    description:
      - Tailscale authentication key for VM network access
      - Required when state is present/started
    type: str
    no_log: true

  gpus:
    description:
      - GPU passthrough configuration
      - C(auto) detects and attaches all NVIDIA/AMD GPUs
      - Provide a list of PCI addresses for specific GPUs
      - C(false) or empty to disable
    type: raw
    default: false

  pcie_devices:
    description:
      - List of PCIe device addresses to pass through
    type: list
    elements: str
    default: []

  confidential_computing:
    description:
      - Enable AMD SEV-SNP confidential computing
    type: bool
    default: false

  data_disk_name:
    description:
      - Name for optional secondary data disk
      - Disk is created in storage_path/data/
    type: str

  data_disk_size:
    description:
      - Size of the data disk
      - Defaults to 100G if data_disk_name is specified but size is not
    type: str

  storage_path:
    description:
      - Path to VM storage directory
    type: path
    default: /mnt/sbnb-data

  bridge:
    description:
      - Network bridge name for VM networking
    type: str
    default: br0

  container_image:
    description:
      - Docker image containing QEMU with SVSM support
    type: str
    default: sbnb/svsm

  pull_image:
    description:
      - Whether to pull the container image before starting
    type: str
    choices: ['always', 'missing', 'never']
    default: always

  persist_boot_image:
    description:
      - Whether to preserve the boot disk
      - When true, boot disk is kept across restarts and not deleted on remove
      - When false, boot disk is recreated on each start and deleted on remove
    type: bool
    default: true

  root_password:
    description:
      - Root password for console access
      - If provided, sets root password via cloud-init
      - Enables password authentication for root
    type: str
    no_log: true

  tailscale_tags:
    description:
      - Tailscale tags to advertise when joining the network
      - Comma-separated list of tags (e.g., "tag:sbnb,tag:dev")
      - Tags must be pre-authorized in Tailscale ACL policy
    type: str
    default: "tag:sbnb"

  use_standard_qemu:
    description:
      - Use standard Ubuntu QEMU instead of SVSM build
      - When true, uses ubuntu:24.04 container and installs qemu-system-x86
      - Useful for debugging or when SVSM QEMU has issues
    type: bool
    default: false

  disable_kvm:
    description:
      - Disable KVM hardware acceleration and use TCG (software emulation)
      - VM will be very slow but useful to isolate KVM-specific bugs
      - For debugging only
    type: bool
    default: false

  mem_prealloc:
    description:
      - Preallocate all VM memory at startup
      - May help with memory-related corruption issues
      - For debugging only
    type: bool
    default: false

requirements:
  - docker (Python library)
  - Docker daemon running on target host

author:
  - SBNB Team
'''

EXAMPLES = r'''
# Start a simple VM
- name: Start development VM
  sbnb.compute.qemu_vm:
    name: dev-vm-01
    vcpu: 4
    mem: "8G"
    tskey: "{{ tailscale_key }}"

# Start a GPU-enabled ML training VM
- name: Start ML training VM
  sbnb.compute.qemu_vm:
    name: ml-trainer-01
    state: present
    vcpu: 32
    mem: "64G"
    image_size: "100G"
    tskey: "{{ tailscale_key }}"
    gpus: auto
    data_disk_name: datasets
    data_disk_size: "500G"

# Start a confidential computing VM
- name: Start confidential VM
  sbnb.compute.qemu_vm:
    name: secure-vm-01
    vcpu: 8
    mem: "16G"
    tskey: "{{ tailscale_key }}"
    confidential_computing: true

# Stop a VM (keeps container and data)
- name: Stop VM
  sbnb.compute.qemu_vm:
    name: dev-vm-01
    state: stopped

# Remove a VM (keeps boot disk by default)
- name: Remove VM
  sbnb.compute.qemu_vm:
    name: dev-vm-01
    state: absent

# Remove a VM and delete boot disk
- name: Remove VM and disk
  sbnb.compute.qemu_vm:
    name: dev-vm-01
    state: absent
    persist_boot_image: false

# Pass specific GPUs
- name: Start VM with specific GPUs
  sbnb.compute.qemu_vm:
    name: gpu-vm-01
    tskey: "{{ tailscale_key }}"
    gpus:
      - "0000:01:00.0"
      - "0000:01:00.1"
'''

RETURN = r'''
name:
  description: VM name
  returned: always
  type: str
  sample: "dev-vm-01"

container_id:
  description: Docker container ID
  returned: when state is present/started
  type: str
  sample: "a1b2c3d4e5f6789..."

container_short_id:
  description: Short container ID (12 chars)
  returned: when state is present/started
  type: str
  sample: "a1b2c3d4e5f6"

state:
  description: Current VM state
  returned: always
  type: str
  sample: "running"

gpus_attached:
  description: List of GPU PCI addresses attached to VM
  returned: when gpus are attached
  type: list
  sample: ["0000:01:00.0", "0000:41:00.0"]

image_path:
  description: Path to VM boot image
  returned: when state is present/started
  type: str
  sample: "/mnt/sbnb-data/images/dev-vm-01/dev-vm-01.qcow2"

qemu_command:
  description: Full QEMU command used to start VM (only with increased verbosity)
  returned: when state is present/started and verbosity > 0
  type: str
'''

import os
import json
import hashlib
import shutil
import traceback

from ansible.module_utils.basic import AnsibleModule

# Try to import docker
try:
    import docker
    from docker.errors import NotFound as DockerNotFound
    from docker.errors import ImageNotFound as DockerImageNotFound
    from docker.errors import DockerException
    HAS_DOCKER = True
except ImportError:
    HAS_DOCKER = False
    DockerNotFound = Exception
    DockerImageNotFound = Exception
    DockerException = Exception


class QemuVmError(Exception):
    """Custom exception for QEMU VM errors"""
    pass


def normalize_size(value, param_name):
    """Normalize size values by adding 'G' suffix if missing.

    QEMU interprets bare numbers as bytes, which is almost never intended.
    If a user passes '100' they almost certainly mean '100G'.
    """
    if value is None:
        return value

    value = str(value)

    # If it's already has a unit suffix, return as-is
    if value[-1].upper() in ('K', 'M', 'G', 'T', 'B'):
        return value

    # If it's a bare number, append 'G' and warn
    if value.isdigit():
        return f"{value}G"

    return value


def get_system_cpu_count():
    """Get total CPU count from the system."""
    try:
        # Try /proc/cpuinfo first for accurate count
        with open('/proc/cpuinfo', 'r') as f:
            return sum(1 for line in f if line.startswith('processor'))
    except (IOError, OSError):
        # Fall back to os.cpu_count()
        return os.cpu_count() or 1


def get_system_memory_mb():
    """Get available system memory in MB from /proc/meminfo."""
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    # MemAvailable is in kB, convert to MB
                    return int(line.split()[1]) // 1024
    except (IOError, OSError, ValueError, IndexError):
        pass
    return 4096  # Default fallback


def resolve_max_resources(vcpu, mem):
    """Resolve 'max' values for vcpu and mem to actual system values.

    Returns (vcpu, mem) tuple with resolved values.
    Reserves 2 CPUs and 2GB RAM for the hypervisor.
    """
    resolved_vcpu = vcpu
    resolved_mem = mem

    if str(vcpu).lower() == 'max':
        total_cpus = get_system_cpu_count()
        resolved_vcpu = max(total_cpus - 2, 1)

    if str(mem).lower() == 'max':
        total_mem_mb = get_system_memory_mb()
        resolved_mem_mb = max(total_mem_mb - 2048, 1024)
        resolved_mem = f"{resolved_mem_mb}M"

    return resolved_vcpu, resolved_mem


class QemuVm:
    """Manages QEMU virtual machines running in Docker containers"""

    # GPU vendor IDs
    NVIDIA_VENDOR = "10de"
    AMD_VENDOR = "1002"
    VGA_CLASS = "0300"

    def __init__(self, module):
        self.module = module
        self.params = module.params
        self.check_mode = module.check_mode

        # Resolve "max" values for vcpu and mem
        self.params['vcpu'], self.params['mem'] = resolve_max_resources(
            self.params['vcpu'], self.params['mem']
        )

        # Ensure vcpu is an integer
        self.params['vcpu'] = int(self.params['vcpu'])

        # Normalize size parameters (auto-append 'G' if bare number)
        self.params['mem'] = normalize_size(self.params['mem'], 'mem')
        self.params['image_size'] = normalize_size(self.params['image_size'], 'image_size')
        if self.params.get('data_disk_size'):
            self.params['data_disk_size'] = normalize_size(self.params['data_disk_size'], 'data_disk_size')

        # Initialize Docker client
        self.docker = None
        if HAS_DOCKER:
            try:
                self.docker = docker.from_env()
            except DockerException as e:
                module.fail_json(msg=f"Failed to connect to Docker: {e}")

        # Set up paths
        self.name = self.params['name']
        self.storage_path = self.params['storage_path']
        self.vm_dir = os.path.join(self.storage_path, 'images', self.name)
        self.boot_image = os.path.join(self.vm_dir, f"{self.name}.qcow2")
        self.seed_iso = os.path.join(self.vm_dir, f"seed-{self.name}.iso")
        self.data_dir = os.path.join(self.storage_path, 'data')

        # Result tracking
        self.result = {
            'changed': False,
            'name': self.name,
            'state': 'absent',
            'gpus_attached': [],
        }

    def run(self):
        """Main entry point"""
        state = self.params['state']

        if state in ('present', 'started'):
            return self.ensure_present()
        elif state == 'stopped':
            return self.ensure_stopped()
        elif state == 'absent':
            return self.ensure_absent()

    # =========================================================================
    # State Management
    # =========================================================================

    def ensure_present(self):
        """Ensure VM exists and is running"""
        # Validate tskey is provided
        if not self.params.get('tskey'):
            self.module.fail_json(msg="tskey is required when state is present/started")

        existing = self.get_container()

        if existing:
            if existing.status == 'running':
                self.result['state'] = 'running'
                self.result['container_id'] = existing.id
                self.result['container_short_id'] = existing.short_id
                self.result['image_path'] = self.boot_image
                return self.result
            else:
                # Container exists but not running - remove it and recreate
                if not self.check_mode:
                    existing.remove(force=True)
                # Fall through to create new container

        # VM doesn't exist (or was just removed), create it
        self.result['changed'] = True

        if self.check_mode:
            self.result['state'] = 'would_create'
            return self.result

        # Prepare VM assets
        self.prepare_vm_directory()
        self.download_image()
        self.prepare_boot_image()
        self.create_cloud_init()

        # Handle GPU passthrough
        gpus = self.setup_gpu_passthrough()
        self.result['gpus_attached'] = gpus

        # Handle PCIe passthrough
        self.setup_pcie_passthrough()

        # Prepare optional data disk
        data_disk_path = self.prepare_data_disk()

        # Build and execute QEMU command
        qemu_cmd = self.build_qemu_command(gpus, data_disk_path)
        self.result['qemu_command'] = qemu_cmd

        # Start container
        container = self.start_container(qemu_cmd)
        self.result['container_id'] = container.id
        self.result['container_short_id'] = container.short_id
        self.result['state'] = 'running'
        self.result['image_path'] = self.boot_image

        return self.result

    def ensure_stopped(self):
        """Ensure VM is stopped"""
        existing = self.get_container()

        if not existing:
            self.result['state'] = 'absent'
            return self.result

        if existing.status != 'running':
            self.result['state'] = 'stopped'
            self.result['container_id'] = existing.id
            self.result['container_short_id'] = existing.short_id
            return self.result

        self.result['changed'] = True

        if not self.check_mode:
            existing.stop(timeout=30)

        self.result['state'] = 'stopped'
        self.result['container_id'] = existing.id
        self.result['container_short_id'] = existing.short_id
        return self.result

    def ensure_absent(self):
        """Ensure VM is removed"""
        existing = self.get_container()

        if not existing:
            self.result['state'] = 'absent'
            return self.result

        self.result['changed'] = True

        if not self.check_mode:
            existing.remove(force=True)

            # Clean up VM directory if persist_boot_image is disabled
            if not self.params.get('persist_boot_image') and os.path.exists(self.vm_dir):
                shutil.rmtree(self.vm_dir)

        self.result['state'] = 'absent'
        return self.result

    # =========================================================================
    # Container Management
    # =========================================================================

    def get_container(self):
        """Get existing container by name"""
        if not self.docker:
            return None

        try:
            return self.docker.containers.get(self.name)
        except DockerNotFound:
            return None

    def start_container(self, qemu_cmd):
        """Start the QEMU container"""
        use_standard = self.params.get('use_standard_qemu', False)

        # Determine container image
        if use_standard:
            # Use pre-built standard QEMU image (no on-the-fly installation)
            container_image = 'sbnb/qemu-standard'
            full_cmd = qemu_cmd
        else:
            container_image = self.params['container_image']
            full_cmd = qemu_cmd

        # Pull image if requested
        pull = self.params['pull_image']
        if pull == 'always':
            self.docker.images.pull(container_image)
        elif pull == 'missing':
            try:
                self.docker.images.get(container_image)
            except DockerImageNotFound:
                self.docker.images.pull(container_image)

        # Prepare devices list
        devices = ['/dev/kvm:/dev/kvm']

        # Add SEV device if it exists (for confidential computing)
        if os.path.exists('/dev/sev'):
            devices.append('/dev/sev:/dev/sev')

        # Container configuration
        # Use sh -c to run the command string (includes mkdir/echo for bridge.conf)
        # tty and stdin_open enable interactive serial console via 'docker attach'
        container = self.docker.containers.run(
            image=container_image,
            name=self.name,
            command=['sh', '-c', full_cmd],
            detach=True,
            tty=True,
            stdin_open=True,
            privileged=True,
            network_mode='host',
            devices=devices,
            volumes={
                '/sys': {'bind': '/sys', 'mode': 'rw'},
                '/dev': {'bind': '/dev', 'mode': 'rw'},
                self.storage_path: {'bind': self.storage_path, 'mode': 'rw'},
            },
        )

        return container

    # =========================================================================
    # VM Preparation
    # =========================================================================

    def run_in_container(self, cmd, check_rc=True):
        """Run a command inside a container for VM preparation

        Args:
            cmd: Command to run
            check_rc: Whether to fail on non-zero return code
        """
        use_standard = self.params.get('use_standard_qemu', False)

        if use_standard:
            # Use pre-built standard QEMU image (has qemu-utils, wget, curl)
            container_image = 'sbnb/qemu-standard'
        else:
            container_image = self.params['container_image']
        prep_cmd = cmd

        # Ensure image is available
        pull = self.params['pull_image']

        if pull == 'always':
            try:
                self.docker.images.pull(container_image)
            except Exception:
                pass  # Will fail later if image not available
        elif pull == 'missing':
            try:
                self.docker.images.get(container_image)
            except DockerImageNotFound:
                self.docker.images.pull(container_image)

        # Run command in container
        full_cmd = [
            'docker', 'run', '--rm',
            '-v', f'{self.storage_path}:{self.storage_path}',
            container_image,
            'sh', '-c', prep_cmd
        ]

        rc, stdout, stderr = self.module.run_command(full_cmd, check_rc=check_rc)
        return rc, stdout, stderr

    def prepare_vm_directory(self):
        """Create VM directory structure"""
        os.makedirs(self.vm_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

    def download_image(self):
        """Download base cloud image, updating if server has newer version"""
        image_url = self.params['image_url']
        image_filename = os.path.basename(image_url)
        images_dir = os.path.join(self.storage_path, 'images')
        cached_image = os.path.join(images_dir, image_filename)

        # Use curl -z to only download if remote file is newer than local
        # -z uses the file's modification time to check against server
        # -L follows redirects, -O writes to filename from URL
        cmd = f'cd {images_dir} && curl -L -z {image_filename} -O {image_url}'
        rc, stdout, stderr = self.run_in_container(cmd, check_rc=False)
        # curl returns 0 even if file wasn't downloaded (not modified)
        if rc != 0:
            self.module.fail_json(msg=f"Failed to download image: {stderr}")

        self.cached_image = cached_image

    def prepare_boot_image(self):
        """Copy and resize boot image"""
        # If persist_boot_image is enabled and image exists, skip recreation
        if self.params.get('persist_boot_image') and os.path.exists(self.boot_image):
            return

        # Remove any existing boot image
        if os.path.exists(self.boot_image):
            os.remove(self.boot_image)

        # Copy from cache using container
        cmd = f'cp {self.cached_image} {self.boot_image}'
        self.run_in_container(cmd, check_rc=True)

        # Resize image using qemu-img in container
        cmd = f'qemu-img resize {self.boot_image} {self.params["image_size"]}'
        self.run_in_container(cmd, check_rc=True)

    def create_cloud_init(self):
        """Create cloud-init ISO"""
        user_data_path = os.path.join(self.vm_dir, 'user-data')
        meta_data_path = os.path.join(self.vm_dir, 'meta-data')

        # Build optional root password section
        root_password = self.params.get('root_password')
        password_section = ""
        if root_password:
            password_section = f"""
chpasswd:
  list: |
    root:{root_password}
  expire: false
ssh_pwauth: true
"""

        # Write user-data with Tailscale setup
        # - runcmd runs only on first boot (installs tailscale, authenticates with key)
        # - systemd service ensures Tailscale stays connected on every boot
        # Note: MAC address is deterministic (based on VM name), so Netplan config
        # written by cloud-init will match on subsequent boots
        user_data = f"""#cloud-config
{password_section}write_files:
  - path: /usr/local/bin/tailscale-up.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # Wait for tailscaled to be ready
      for i in $(seq 1 30); do
        tailscale status >/dev/null 2>&1 && break
        sleep 1
      done
      # Check if Tailscale needs login (no saved auth state)
      # If so, skip - cloud-init runcmd handles first-time auth
      STATE=$(tailscale status --json 2>/dev/null | grep -o '"BackendState":"[^"]*"' | cut -d'"' -f4)
      if [ "$STATE" = "NeedsLogin" ]; then
        echo "Tailscale needs login, skipping (cloud-init handles first auth)"
        exit 0
      fi
      # Reconnect using saved state (with timeout to prevent hanging)
      tailscale up --ssh --timeout=30s || true
  - path: /etc/systemd/system/tailscale-up.service
    content: |
      [Unit]
      Description=Ensure Tailscale is connected
      After=tailscaled.service
      Wants=tailscaled.service

      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/tailscale-up.sh
      RemainAfterExit=yes

      [Install]
      WantedBy=multi-user.target

runcmd:
  - hostname {self.name}
  - echo {self.name} > /etc/hostname
  - curl -fsSL https://tailscale.com/install.sh | sh
  - systemctl daemon-reload
  - systemctl enable tailscale-up.service
  - tailscale up --ssh --advertise-tags={self.params['tailscale_tags']} --auth-key={self.params['tskey']}
"""
        with open(user_data_path, 'w') as f:
            f.write(user_data)

        # Write empty meta-data
        with open(meta_data_path, 'w') as f:
            f.write('')

        # Generate ISO using genisoimage inside container
        cmd = f'genisoimage -output {self.seed_iso} -volid cidata -joliet -rock {user_data_path} {meta_data_path}'
        self.run_in_container(cmd, check_rc=True)

    def prepare_data_disk(self):
        """Prepare optional data disk"""
        data_disk_name = self.params.get('data_disk_name')
        if not data_disk_name:
            return None

        data_disk_path = os.path.join(self.data_dir, f"{data_disk_name}.qcow2")

        if not os.path.exists(data_disk_path):
            size = self.params.get('data_disk_size', '100G')
            # Create disk using qemu-img in container
            cmd = f'qemu-img create -f qcow2 {data_disk_path} {size}'
            self.run_in_container(cmd, check_rc=True)

        return data_disk_path

    # =========================================================================
    # GPU/PCIe Passthrough
    # =========================================================================

    def setup_gpu_passthrough(self):
        """Setup GPU passthrough and return list of GPU addresses"""
        gpus_param = self.params['gpus']

        if not gpus_param:
            return []

        # Handle string "true"/"True" from command line as well as boolean True
        if gpus_param == 'auto' or gpus_param is True or str(gpus_param).lower() == 'true':
            gpus = self.detect_gpus()
        elif isinstance(gpus_param, list):
            gpus = gpus_param
        else:
            return []

        # Bind GPUs to vfio-pci
        for gpu in gpus:
            self.bind_to_vfio(gpu)

        return gpus

    def detect_gpus(self):
        """Auto-detect NVIDIA and AMD GPUs"""
        gpus = []

        # Detect NVIDIA GPUs
        rc, stdout, stderr = self.module.run_command(
            f"lspci -nn | grep -i {self.NVIDIA_VENDOR} | awk '{{print $1}}'",
            use_unsafe_shell=True
        )
        if rc == 0 and stdout.strip():
            gpus.extend(stdout.strip().split('\n'))

        # Detect AMD GPUs (VGA class only)
        rc, stdout, stderr = self.module.run_command(
            f"lspci -d {self.AMD_VENDOR}::{self.VGA_CLASS} | awk '{{print $1}}'",
            use_unsafe_shell=True
        )
        if rc == 0 and stdout.strip():
            gpus.extend(stdout.strip().split('\n'))

        return [g for g in gpus if g]  # Filter empty strings

    def setup_pcie_passthrough(self):
        """Setup PCIe device passthrough"""
        devices = self.params.get('pcie_devices', [])
        for device in devices:
            self.bind_to_vfio(device)

    def bind_to_vfio(self, pci_address):
        """Bind a PCI device to vfio-pci driver"""
        # Get vendor:device ID
        rc, stdout, stderr = self.module.run_command(
            f"lspci -n -s {pci_address} | awk '{{print $3}}'",
            use_unsafe_shell=True
        )

        if rc != 0 or not stdout.strip():
            self.module.warn(f"Could not get vendor:device for {pci_address}")
            return

        vendor_device = stdout.strip().replace(':', ' ')

        # Write to vfio-pci new_id
        vfio_path = '/sys/bus/pci/drivers/vfio-pci/new_id'
        try:
            with open(vfio_path, 'w') as f:
                f.write(vendor_device)
        except IOError:
            # May already be bound, not fatal
            pass

    # =========================================================================
    # QEMU Command Building
    # =========================================================================

    def build_qemu_command(self, gpus, data_disk_path):
        """Build the QEMU command line"""
        mac_address = self.generate_mac_address()
        bridge = self.params['bridge']
        use_standard = self.params.get('use_standard_qemu', False)
        disable_kvm = self.params.get('disable_kvm', False)
        mem_prealloc = self.params.get('mem_prealloc', False)

        # KVM or TCG (software emulation)
        if disable_kvm:
            accel_opts = ['-accel', 'tcg']
            cpu_opts = ['-cpu', 'qemu64']  # Can't use host CPU without KVM
        else:
            accel_opts = ['-enable-kvm']
            cpu_opts = ['-cpu', 'host']

        if use_standard:
            # Standard QEMU from Ubuntu packages
            cmd_parts = [
                'mkdir -p /etc/qemu &&',
                'echo "allow all" > /etc/qemu/bridge.conf &&',
                '/usr/bin/qemu-system-x86_64',
            ]
            cmd_parts.extend(accel_opts)
            cmd_parts.extend(cpu_opts)
            cmd_parts.extend([
                '-smp', str(self.params['vcpu']),
                '-object', 'iothread,id=iothread0',
                '-device', 'virtio-scsi-pci,id=scsi0,iothread=iothread0',
                '-nographic',
                '-serial', 'mon:stdio',
            ])
        else:
            # SVSM QEMU build with IOMMU support
            cmd_parts = [
                'mkdir -p /usr/qemu-svsm/etc/qemu &&',
                'echo "allow all" > /usr/qemu-svsm/etc/qemu/bridge.conf &&',
                '/usr/qemu-svsm/bin/qemu-system-x86_64',
            ]
            cmd_parts.extend(accel_opts)
            cmd_parts.extend(cpu_opts)
            cmd_parts.extend([
                '-smp', str(self.params['vcpu']),
                '-object', 'iothread,id=iothread0',
                '-device', 'virtio-scsi-pci,id=scsi0,disable-legacy=on,iommu_platform=on,iothread=iothread0',
                '-nographic',
                '-serial', 'mon:stdio',
            ])

        # Boot disk - use cache=none to bypass host page cache (matches working config)
        cmd_parts.extend([
            '-drive', f'file={self.boot_image},if=none,id=disk0,format=qcow2,snapshot=off,cache=none',
            '-device', 'scsi-hd,drive=disk0,bootindex=0',
            '-cdrom', self.seed_iso,
        ])

        # Optional data disk
        if data_disk_path:
            cmd_parts.extend([
                '-drive', f'file={data_disk_path},if=none,id=datadisk0,format=qcow2,snapshot=off,cache=none',
                '-device', 'scsi-hd,drive=datadisk0,serial=sbnb-data-disk',
            ])

        # Networking - use bridge for proper network connectivity
        cmd_parts.extend([
            '-device', f'virtio-net-pci,netdev=net0,mac={mac_address}',
            '-netdev', f'bridge,id=net0,br={bridge}',
        ])

        # Machine type and memory
        if self.params['confidential_computing']:
            mem = self.params['mem']
            cmd_parts.extend([
                '-machine', 'q35,confidential-guest-support=sev0,memory-backend=ram1,igvm-cfg=igvm0',
                '-object', f'memory-backend-memfd,id=ram1,size={mem},share=true,prealloc=false,reserve=false',
                '-object', 'sev-snp-guest,id=sev0,cbitpos=51,reduced-phys-bits=1',
            ])
        else:
            cmd_parts.extend([
                '-machine', 'q35',
                '-m', self.params['mem'],
                '-bios', '/usr/share/ovmf/OVMF.fd',
            ])
            # Optional memory preallocation for debugging
            if mem_prealloc:
                cmd_parts.append('-mem-prealloc')

        # GPU passthrough
        for gpu in gpus:
            cmd_parts.extend(['-device', f'vfio-pci,host={gpu}'])

        # PCIe passthrough
        for device in self.params.get('pcie_devices', []):
            cmd_parts.extend(['-device', f'vfio-pci,host={device}'])

        return ' '.join(cmd_parts)

    def generate_mac_address(self):
        """Generate a deterministic MAC address based on VM name.

        Uses QEMU's locally administered prefix (52:54:00) combined with
        a hash of the VM name. This ensures the same VM always gets the
        same MAC address, which is critical for persisted boot images
        where cloud-init writes Netplan config matching by MAC address.
        """
        # Create a hash of the VM name
        name_hash = hashlib.md5(self.name.encode()).hexdigest()
        # Use first 6 hex chars for the last 3 octets
        return f"52:54:00:{name_hash[0:2]}:{name_hash[2:4]}:{name_hash[4:6]}"


# =============================================================================
# Module Entry Point
# =============================================================================

def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type='str', required=True),
            state=dict(type='str', default='present',
                      choices=['present', 'absent', 'started', 'stopped']),
            vcpu=dict(type='raw', default=2),
            mem=dict(type='str', default='4G'),
            image_url=dict(type='str',
                          default='https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img'),
            image_size=dict(type='str', default='10G'),
            tskey=dict(type='str', no_log=True),
            gpus=dict(type='raw', default=False),
            pcie_devices=dict(type='list', elements='str', default=[]),
            confidential_computing=dict(type='bool', default=False),
            data_disk_name=dict(type='str'),
            data_disk_size=dict(type='str'),
            storage_path=dict(type='path', default='/mnt/sbnb-data'),
            bridge=dict(type='str', default='br0'),
            container_image=dict(type='str', default='sbnb/svsm'),
            pull_image=dict(type='str', default='always',
                           choices=['always', 'missing', 'never']),
            persist_boot_image=dict(type='bool', default=True),
            root_password=dict(type='str', no_log=True),
            tailscale_tags=dict(type='str', default='tag:sbnb'),
            use_standard_qemu=dict(type='bool', default=False),
            disable_kvm=dict(type='bool', default=False),
            mem_prealloc=dict(type='bool', default=False),
        ),
        supports_check_mode=True,
    )

    if not HAS_DOCKER:
        module.fail_json(
            msg="The docker Python library is required. Install with: pip install docker"
        )

    try:
        vm = QemuVm(module)
        result = vm.run()
        module.exit_json(**result)
    except QemuVmError as e:
        module.fail_json(msg=str(e))
    except Exception as e:
        module.fail_json(
            msg=f"Unexpected error: {e}",
            exception=traceback.format_exc()
        )


if __name__ == '__main__':
    main()
