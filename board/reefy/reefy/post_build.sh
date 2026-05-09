#!/bin/bash
set -euxo pipefail

# Add Reefy Linux build version to /etc/os-release using the current date if IMAGE_VERSION is not defined.
# Auto-bumps the sequence number (-00, -01, ...) on repeated same-day builds.
DATE=$(date +%Y.%m.%d)
OS_RELEASE="${TARGET_DIR}/usr/lib/os-release"
# Buildroot overwrites os-release with '>' before post_build.sh runs,
# so persist last version in a side file to detect same-day rebuilds.
VERSION_FILE="${BUILD_DIR}/.reefy-last-version"
if [ -z "${IMAGE_VERSION:-}" ]; then
  SEQ=0
  if [ -f "${VERSION_FILE}" ]; then
    PREV=$(grep -oP "^${DATE}-\d+" "${VERSION_FILE}" || true)
    if [ -n "${PREV}" ]; then
      PREV_SEQ=${PREV##*-}
      SEQ=$((10#${PREV_SEQ} + 1))
    fi
  fi
  IMAGE_VERSION=$(printf '%s-%02d' "${DATE}" "${SEQ}")
fi
echo "${IMAGE_VERSION}" > "${VERSION_FILE}"
echo "IMAGE_ID=reefy-linux" >> "${OS_RELEASE}"
echo "IMAGE_VERSION=${IMAGE_VERSION}" >> "${OS_RELEASE}"

# Mount efivarfs to access UEFI variables
# Remount as read-write as needed
FSTAB="${TARGET_DIR}/etc/fstab"
if ! grep -q efivarfs ${FSTAB};then
  echo "efivarfs /sys/firmware/efi/efivars efivarfs ro,nosuid,nodev,noexec 0 0" >> ${FSTAB}
fi

# Make sure sshd reads /etc/ssh/sshd_config.d/*.conf so our reefy-apps
# drop-in (Match User app-* + ForceCommand) is honored. Buildroot's
# openssh ships the unmodified upstream sshd_config which does NOT
# include the drop-in directory by default.
SSHD_CONFIG="${TARGET_DIR}/etc/ssh/sshd_config"
if [ -f "${SSHD_CONFIG}" ] && ! grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "${SSHD_CONFIG}"; then
  echo 'Include /etc/ssh/sshd_config.d/*.conf' >> "${SSHD_CONFIG}"
fi

# Remove network-online.target from Docker's unit file.
# Docker doesn't need network for boot — cached images start offline
# (--pull missing). Drop-in After= reset doesn't work in systemd 257,
# so we patch the unit file directly.
DOCKER_UNIT="${TARGET_DIR}/usr/lib/systemd/system/docker.service"
if [ -f "${DOCKER_UNIT}" ]; then
  sed -i 's/network-online.target //g' "${DOCKER_UNIT}"
fi
