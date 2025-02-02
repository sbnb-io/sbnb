#!/bin/bash
set -euxo pipefail

# Add Sbnb Linux build version to /etc/os-release using the current date and GitHub run number.
DATE=$(date +%Y.%m.%d)
RUN_NUMBER=${GITHUB_RUN_NUMBER:-00}
IMAGE_VERSION="${DATE}-${RUN_NUMBER}"
OS_RELEASE="${TARGET_DIR}/etc/os-release"
echo "IMAGE_ID=sbnb-linux" >> "${OS_RELEASE}"
echo "IMAGE_VERSION=${IMAGE_VERSION}" >> "${OS_RELEASE}"

# Mount efivarfs to access UEFI variables
# Remount as read-write as needed
FSTAB="${TARGET_DIR}/etc/fstab"
if ! grep -q efivarfs ${FSTAB};then
  echo "efivarfs /sys/firmware/efi/efivars efivarfs ro,nosuid,nodev,noexec 0 0" >> ${FSTAB}
fi
