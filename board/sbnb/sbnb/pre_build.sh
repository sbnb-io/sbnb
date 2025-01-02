#!/bin/sh
set -x

env

# Generate kernel config fragment with a path to firmware
# TODO: find proper way to specify path to fw
cat > ${CONFIG_DIR}/kernel-config-firmware << EOF
CONFIG_EXTRA_FIRMWARE="amd-ucode/microcode_amd.bin amd-ucode/microcode_amd_fam15h.bin amd-ucode/microcode_amd_fam16h.bin amd-ucode/microcode_amd_fam17h.bin amd-ucode/microcode_amd_fam19h.bin amd/amd_sev_fam19h_model01h.sbin"
CONFIG_EXTRA_FIRMWARE_DIR="${BR2_EXTERNAL_SBNB_PATH}/board/sbnb/sbnb/rootfs-overlay/usr/lib/firmware"
EOF
