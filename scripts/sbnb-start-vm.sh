#!/bin/bash

# This script is used to start a virtual machine (VM) with specific configurations.
# The configurations are provided through a JSON file passed as an argument to the script.
# The script sets up the VM with the specified number of virtual CPUs, memory, hostname, 
# and other configurations such as attaching GPUs or PCIe devices, and enabling confidential computing.
# It also downloads the specified VM image, prepares the cloud-init configuration, and starts the VM using QEMU.

# Example VM JSON configuration:
# {
#   "vcpu": 2,                       # Number of virtual CPUs
#   "mem": "4G",                     # Amount of memory
#   "tskey": "your_tailscale_key",   # Tailscale authentication key
#   "hostname": "custom-hostname",   # Hostname for the VM (optional, will be autogenerated if not provided)
#   "attach_gpus": false,            # Whether to attach GPUs (all available GPUs in the system will be attached if true)
#   "attach_pcie_devices": [         # List of PCIe devices to attach
#     "0000:00:1c.0", 
#     "0000:00:1d.0"
#   ],
#   "confidential_computing": false, # Whether to enable confidential computing
#   "image_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img", # URL of the VM image (optional)
#   "image_size": "10G"              # Size of the VM image (optional)
# }

set -euxo pipefail

# Default values
DEFAULT_STORAGE="/mnt/sbnb-data/images"
DEFAULT_VCPU=2
DEFAULT_MEM="4G"
DEFAULT_TSKEY=""
DEFAULT_HOSTNAME=""
DEFAULT_ATTACH_GPUS=false
DEFAULT_ATTACH_PCIE_DEVICES=()
DEFAULT_CONFIDENTIAL_COMPUTING=false
DEFAULT_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
DEFAULT_IMAGE_SIZE="10G"

# Usage message
usage() {
  echo "Usage: $0 -f <path_to_json_config>"
  exit 1
}

# Parse arguments
while getopts "f:" opt; do
  case ${opt} in
    f) CONFIG_FILE=${OPTARG} ;;
    *) usage ;;
  esac
done

if [ -z "${CONFIG_FILE}" ]; then
  usage
fi

# Install required packages
apt-get update && apt-get install -y jq xxd pciutils curl genisoimage

# Parse JSON configuration
VCPU=$(jq -r '.vcpu // empty' ${CONFIG_FILE})
MEM=$(jq -r '.mem // empty' ${CONFIG_FILE})
TSKEY=$(jq -r '.tskey // empty' ${CONFIG_FILE})
HOSTNAME=$(jq -r '.hostname // empty' ${CONFIG_FILE})
ATTACH_GPUS=$(jq -r '.attach_gpus // empty' ${CONFIG_FILE})
ATTACH_PCIE_DEVICES=($(jq -r '.attach_pcie_devices // empty | .[]' ${CONFIG_FILE}))
CONFIDENTIAL_COMPUTING=$(jq -r '.confidential_computing // empty' ${CONFIG_FILE})
IMAGE_URL=$(jq -r '.image_url // empty' ${CONFIG_FILE})
IMAGE_SIZE=$(jq -r '.image_size // empty' ${CONFIG_FILE})

# Set default values if variables are empty
STORAGE=${DEFAULT_STORAGE}
VCPU=${VCPU:-$DEFAULT_VCPU}
MEM=${MEM:-$DEFAULT_MEM}
TSKEY=${TSKEY:-$DEFAULT_TSKEY}
HOSTNAME=${HOSTNAME:-$DEFAULT_HOSTNAME}
ATTACH_GPUS=${ATTACH_GPUS:-$DEFAULT_ATTACH_GPUS}
CONFIDENTIAL_COMPUTING=${CONFIDENTIAL_COMPUTING:-$DEFAULT_CONFIDENTIAL_COMPUTING}
IMAGE_URL=${IMAGE_URL:-$DEFAULT_IMAGE_URL}
IMAGE_SIZE=${IMAGE_SIZE:-$DEFAULT_IMAGE_SIZE}

if [ -z "${HOSTNAME}" ]; then
  HOSTNAME="sbnb-vm-$(xxd -l6 -p /dev/random)"
fi

VM_FOLDER="${STORAGE}/${HOSTNAME}"
BOOT_IMAGE="${VM_FOLDER}/${HOSTNAME}.qcow2"
SEED_IMAGE="${VM_FOLDER}/seed-${HOSTNAME}.iso"

mkdir -p ${VM_FOLDER}
cd ${STORAGE}

cat > ${VM_FOLDER}/user-data << EOF
#cloud-config
runcmd:
  - hostname ${HOSTNAME}
  - echo ${HOSTNAME} > /etc/hostname
  - curl -fsSL https://tailscale.com/install.sh | sh
  - tailscale up --ssh --auth-key=${TSKEY}
EOF

touch ${VM_FOLDER}/meta-data

genisoimage -output ${SEED_IMAGE} -volid cidata -joliet -rock ${VM_FOLDER}/user-data ${VM_FOLDER}/meta-data

# Extract the image filename from the URL
IMAGE_FILENAME=$(basename ${IMAGE_URL})

# Download the latest image only if etag changed
curl -z ${IMAGE_FILENAME} -O "${IMAGE_URL}" || true

cp ${IMAGE_FILENAME} ${BOOT_IMAGE}
qemu-img resize ${BOOT_IMAGE} ${IMAGE_SIZE}

# Map Nvidia GPU to vfio-pci if required
if [ "${ATTACH_GPUS}" = true ]; then
  for gpu in $(lspci -nn | grep -i 10de | awk '{print $1}'); do
    vendor_device_id=$(lspci -n -s ${gpu} | awk '{print $3}')
    vendor_id=$(echo ${vendor_device_id} | cut -d: -f1)
    device_id=$(echo ${vendor_device_id} | cut -d: -f2)
    echo "${vendor_id} ${device_id}" > /sys/bus/pci/drivers/vfio-pci/new_id || true
  done
fi

for pcie in "${ATTACH_PCIE_DEVICES[@]}"; do
  vendor_device_id=$(lspci -n -s ${pcie} | awk '{print $3}')
  vendor_id=$(echo ${vendor_device_id} | cut -d: -f1)
  device_id=$(echo ${vendor_device_id} | cut -d: -f2)
  echo "${vendor_id} ${device_id}" > /sys/bus/pci/drivers/vfio-pci/new_id || true
done

# Start the VM
QEMU_CMD="/usr/qemu-svsm/bin/qemu-system-x86_64 \
  -enable-kvm \
  -cpu EPYC-Milan-v2 \
  -smp ${VCPU} \
  -netdev user,id=vmnic -device e1000,netdev=vmnic,romfile= \
  -drive file=${BOOT_IMAGE},if=none,id=disk0,format=qcow2,snapshot=off \
  -device virtio-scsi-pci,id=scsi0,disable-legacy=on,iommu_platform=on \
  -device scsi-hd,drive=disk0,bootindex=0 \
  -cdrom ${SEED_IMAGE} \
  -nographic"

if [ "${CONFIDENTIAL_COMPUTING}" = true ]; then
  QEMU_CMD+=" -machine q35,confidential-guest-support=sev0,memory-backend=ram1,igvm-cfg=igvm0 \
  -object memory-backend-memfd,id=ram1,size=${MEM},share=true,prealloc=false,reserve=false \
  -object sev-snp-guest,id=sev0,cbitpos=51,reduced-phys-bits=1"
else
  QEMU_CMD+=" -machine q35 -m ${MEM} -bios /usr/share/ovmf/OVMF.fd"
fi

if [ "${ATTACH_GPUS}" = true ]; then
  for gpu in $(lspci -nn | grep -i 10de | awk '{print $1}'); do
    QEMU_CMD+=" -device vfio-pci,host=${gpu}"
  done
fi

for pcie in "${ATTACH_PCIE_DEVICES[@]}"; do
  QEMU_CMD+=" -device vfio-pci,host=${pcie}"
done

# Start sbnb-vm-cleaner.sh with a delay of 30 seconds if it exists
CLEANER_SCRIPT="sbnb-vm-cleaner.sh"
DELAY=30
if [ -x "$(which ${CLEANER_SCRIPT})" ]; then
  (sleep ${DELAY} && ${CLEANER_SCRIPT}) &
fi

eval ${QEMU_CMD}
