#!/bin/bash

set -euxo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
IMG_FILE_SRC="${SCRIPT_DIR}"/../buildroot/output/images/sbnb.raw
IMG_FILE="${SCRIPT_DIR}"/../buildroot/output/images/sbnb-qemu.raw
cp ${IMG_FILE_SRC} ${IMG_FILE}

# Start QEMU with OVMF (uefi) BIOS and supply sbnb image as boot disk
qemu-system-x86_64 \
        -nographic \
        -machine q35 \
        -cpu host \
        -accel kvm -m 16G -smp 2 \
        -bios /usr/share/ovmf/OVMF.fd \
        -netdev user,id=n1 -device virtio-net-pci,netdev=n1 \
        -vnc :0 \
        -hda ${IMG_FILE}
