#!/bin/bash
set -euxo pipefail

# Place efi and raw images into buildroot/output/images dir.
#
# Multi-flavor: build initramfs once (flavor-independent), then loop
# create_efi.sh + create_raw.sh per flavor with different
# CMDLINE_OVERRIDE values. Flavors differ ONLY in the kernel cmdline
# baked into the UKI - kernel + rootfs + initramfs are identical, so
# the per-flavor overhead is ~30s each (objcopy + losetup + mkfs +
# zip + qemu-img convert), small fraction of the multi-minute build.
#
# Add a flavor by appending to the FLAVORS array. CI workflow picks
# up new flavors automatically via the `reefy-*.efi` glob.
# Kernel boot args shared by every flavor. Per-flavor extras (e.g.
# reefy.dev_shell=1) get appended below.
COMMON_CMDLINE="console=tty0 console=ttyS0,115200 panic=10 intel_iommu=on module_blacklist=nouveau,nvidiafb,snd_hda_intel,r8169"

# prod: `quiet` for a silent boot - kernel log messages stay off the
#   monitor; the device-state banner still shows at the login prompt via
#   /etc/issue (agetty), which `quiet` does not affect. dev: verbose (no
#   quiet) for full boot logs on console/serial, plus reefy.dev_shell=1.
declare -A FLAVORS=(
    [prod]="${COMMON_CMDLINE} quiet"
    [dev]="${COMMON_CMDLINE} reefy.dev_shell=1"
    [debug-shell]="${COMMON_CMDLINE} systemd.show_status=1 systemd.log_level=debug systemd.log_target=console udev.log_level=debug loglevel=7 reefy.dev_shell=1 reefy.initramfs_shell=1"
)

pushd ${BINARIES_DIR}

echo "Building initramfs with squashfs rootfs inside (flavor-independent)"
sudo -E "${BR2_EXTERNAL_REEFY_PATH}"/board/reefy/reefy/scripts/create_initramfs.sh

for flavor in "${!FLAVORS[@]}"; do
    cmdline="${FLAVORS[$flavor]}"
    echo "=== Flavor: $flavor ==="

    # TODO: avoid calling sudo
    echo "Building reefy-${flavor}.efi uefi uki image"
    CMDLINE_OVERRIDE="${cmdline}" \
    OUTPUT="reefy-${flavor}.efi" \
        sudo -E "${BR2_EXTERNAL_REEFY_PATH}"/board/reefy/reefy/scripts/create_efi.sh

    echo "Building reefy-${flavor}.raw bootable image"
    EFI="reefy-${flavor}.efi" \
    IMG_FILE="reefy-${flavor}.raw" \
    VHD_FILE="reefy-${flavor}.vhd" \
        sudo -E "${BR2_EXTERNAL_REEFY_PATH}"/board/reefy/reefy/scripts/create_raw.sh
done

popd
