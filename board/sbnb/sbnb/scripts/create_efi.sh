#!/bin/bash

set -euxo pipefail

STUB=/usr/lib/systemd/boot/efi/linuxx64.efi.stub
KERNEL="${BINARIES_DIR}"/bzImage
INITRD="${BINARIES_DIR}"/rootfs-sbnb.cpio
OS_RELEASE="${TARGET_DIR}"/etc/os-release
CMDLINE="console=tty0 console=ttyS0 earlyprintk verbose dyndbg=\"module firmware_class +p; module microcode +p; module ccp +p\""
CMDLINE_TMP=$(mktemp)
OUTPUT=sbnb.efi

echo -n ${CMDLINE} > ${CMDLINE_TMP}

objcopy \
    --add-section .osrel="${OS_RELEASE}" --change-section-vma .osrel=0x20000 \
    --add-section .cmdline="${CMDLINE_TMP}" --change-section-vma .cmdline=0x30000 \
    --add-section .linux="${KERNEL}" --change-section-vma .linux=0x2000000 \
    --add-section .initrd="${INITRD}" --change-section-vma .initrd=0x3000000 \
    "${STUB}" "${OUTPUT}"

echo Output: "${OUTPUT}"
