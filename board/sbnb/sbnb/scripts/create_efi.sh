#!/bin/bash

set -euxo pipefail

# This script creates a custom bootable EFI binary by combining an EFI stub,
# Linux kernel, initrd, osrel and cmdline.

# Define variables
STUB=/usr/lib/systemd/boot/efi/linuxx64.efi.stub
KERNEL="${BINARIES_DIR}/bzImage"
INITRD="${BINARIES_DIR}/rootfs-sbnb.cpio"
OS_RELEASE="${TARGET_DIR}/etc/os-release"
CMDLINE="console=tty0 console=ttyS0 earlyprintk verbose dyndbg=\"module firmware_class +p; module microcode +p; module ccp +p\""
CMDLINE_TMP=$(mktemp)
OUTPUT=sbnb.efi

# Write the command line to a temporary file
echo -n "${CMDLINE}" > "${CMDLINE_TMP}"

# Calculate offsets for sections
# Based on https://github.com/andreyv/sbupdate/issues/56
stub_line=$(objdump -h "${STUB}" | tail -2 | head -1)
stub_size=0x$(echo "$stub_line" | awk '{print $3}')
stub_offs=0x$(echo "$stub_line" | awk '{print $4}')
osrel_offs=$((stub_size + stub_offs))
cmdline_offs=$((osrel_offs + $(stat -c%s "${OS_RELEASE}")))
splash_offs=$((cmdline_offs + $(stat -c%s "${CMDLINE_TMP}")))
linux_offs=$((splash_offs))
initrd_offs=$((linux_offs + $(stat -c%s "${KERNEL}")))

# Use objcopy to add sections to the EFI stub
objcopy \
    --add-section .osrel="${OS_RELEASE}" --change-section-vma .osrel=$(printf 0x%x $osrel_offs) \
    --add-section .cmdline="${CMDLINE_TMP}" --change-section-vma .cmdline=$(printf 0x%x $cmdline_offs) \
    --add-section .linux="${KERNEL}" --change-section-vma .linux=$(printf 0x%x $linux_offs) \
    --add-section .initrd="${INITRD}" --change-section-vma .initrd=$(printf 0x%x $initrd_offs) \
    "${STUB}" "${OUTPUT}"

# Output the result
echo "Output: ${OUTPUT}"
