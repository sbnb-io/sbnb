#!/bin/bash

set -euxo pipefail

# This script creates a custom bootable EFI binary by combining an EFI stub,
# Linux kernel, initrd, osrel and cmdline.

# Define variables
STUB=/usr/lib/systemd/boot/efi/linuxx64.efi.stub
KERNEL="${BINARIES_DIR}/bzImage"
INITRD="${BINARIES_DIR}/rootfs-reefy.cpio"
OS_RELEASE="${TARGET_DIR}/etc/os-release"
# Kernel cmdline for the UKI. The default is the production cmdline (silent
# boot via VGA). Override for debugging:
#   CMDLINE_OVERRIDE="console=ttyS0,115200 debug" make
#   CMDLINE_EXTRA="debug loglevel=8"             make   # appended to default
DEFAULT_CMDLINE="console=tty0 quiet panic=10 intel_iommu=on module_blacklist=nouveau,nvidiafb,snd_hda_intel,r8169"
if [[ -n "${CMDLINE_OVERRIDE:-}" ]]; then
    CMDLINE="${CMDLINE_OVERRIDE}"
else
    CMDLINE="${DEFAULT_CMDLINE}${CMDLINE_EXTRA:+ ${CMDLINE_EXTRA}}"
fi
echo "create_efi.sh: kernel cmdline = ${CMDLINE}"
CMDLINE_TMP=$(mktemp)
# Output filename. Defaults to reefy.efi for backward-compat with
# hand builds; post_image.sh overrides per flavor (reefy-prod.efi /
# reefy-dev.efi / ...) when running the multi-flavor loop.
OUTPUT="${OUTPUT:-reefy.efi}"

# Write the command line to a temporary file
echo -n "${CMDLINE}" > "${CMDLINE_TMP}"

# Calculate alignment
align="$(objdump -p "${STUB}" | awk '{ if ($1 == "SectionAlignment"){print $2} }')"
align=$((16#$align))

# Calculate offsets for sections
stub_line=$(objdump -h "${STUB}" | tail -2 | head -1)
stub_size=0x$(echo "$stub_line" | awk '{print $3}')
stub_offs=0x$(echo "$stub_line" | awk '{print $4}')
osrel_offs=$((stub_size + stub_offs))
osrel_offs=$((osrel_offs + align - osrel_offs % align))
cmdline_offs=$((osrel_offs + $(stat -Lc%s "${OS_RELEASE}")))
cmdline_offs=$((cmdline_offs + align - cmdline_offs % align))
splash_offs=$((cmdline_offs + $(stat -Lc%s "${CMDLINE_TMP}")))
splash_offs=$((splash_offs + align - splash_offs % align))
linux_offs=$((splash_offs))
initrd_offs=$((linux_offs + $(stat -Lc%s "${KERNEL}")))
initrd_offs=$((initrd_offs + align - initrd_offs % align))

# Use objcopy to add sections to the EFI stub
objcopy \
    --add-section .osrel="${OS_RELEASE}" --change-section-vma .osrel=$(printf 0x%x $osrel_offs) \
    --add-section .cmdline="${CMDLINE_TMP}" --change-section-vma .cmdline=$(printf 0x%x $cmdline_offs) \
    --add-section .linux="${KERNEL}" --change-section-vma .linux=$(printf 0x%x $linux_offs) \
    --add-section .initrd="${INITRD}" --change-section-vma .initrd=$(printf 0x%x $initrd_offs) \
    "${STUB}" "${OUTPUT}"

# Output the result
echo "Output: ${OUTPUT}"
