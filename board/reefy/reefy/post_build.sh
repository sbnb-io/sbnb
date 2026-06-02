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
# drop-in (Match User app-* + ForceCommand + shared AuthorizedKeysFile)
# is honored. Buildroot's openssh ships the unmodified upstream
# sshd_config which does NOT include the drop-in directory by default.
#
# Must be at the TOP of the file: sshd's first-occurrence-wins rule
# means drop-ins only override upstream defaults (like
# AuthorizedKeysFile) if processed first. Strip any existing Include
# anywhere in the file before prepending - prior firmware revisions
# appended at the bottom, which silently nullified the drop-in's
# global directives.
SSHD_CONFIG="${TARGET_DIR}/etc/ssh/sshd_config"
if [ -f "${SSHD_CONFIG}" ]; then
  sed -i '/^[[:space:]]*Include[[:space:]]\+\/etc\/ssh\/sshd_config\.d\/\*\.conf/d' "${SSHD_CONFIG}"
  sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' "${SSHD_CONFIG}"
fi

# Remove network-online.target from Docker's unit file.
# Docker doesn't need network for boot — cached images start offline
# (--pull missing). Drop-in After= reset doesn't work in systemd 257,
# so we patch the unit file directly.
DOCKER_UNIT="${TARGET_DIR}/usr/lib/systemd/system/docker.service"
if [ -f "${DOCKER_UNIT}" ]; then
  sed -i 's/network-online.target //g' "${DOCKER_UNIT}"
fi

# Prune orphaned reefy files from the (cached, incrementally-built)
# TARGET_DIR. Buildroot's rootfs-overlay copy is ADDITIVE: a file
# deleted/renamed in the overlay still lingers in TARGET_DIR from prior
# builds and ships anyway. This stranded a stale reefy-mqtt.service plus
# the old /usr/bin/reefy-mqtt-reconciler monolith, which ran a duplicate
# MQTT client (same client-id as reefy-control) and broke adoption.
# Scope strictly to OUR files so we never touch buildroot/package files
# (which would force a full, slow rebuild). NOTE: a blanket "remove any
# reefy-* not in the overlay" is unsafe - reefy-cmds/reefy-ttyd units are
# package-provided, not overlay-provided.
OVERLAY_DIR="$(cd "$(dirname "$0")" && pwd)/rootfs-overlay"

# /usr/lib/reefy is wholly ours: mirror it so deleted modules (e.g. the
# old reefy_reconciler.py monolith) can't linger.
if command -v rsync >/dev/null 2>&1 && [ -d "${OVERLAY_DIR}/usr/lib/reefy" ]; then
  rsync -a --delete "${OVERLAY_DIR}/usr/lib/reefy/" "${TARGET_DIR}/usr/lib/reefy/"
fi

# Files this branch renamed away from (overlay no longer ships them).
# Append future renamed/deleted reefy files here.
rm -f "${TARGET_DIR}/usr/bin/reefy-mqtt-reconciler" \
      "${TARGET_DIR}/usr/lib/systemd/system/reefy-mqtt.service" \
      "${TARGET_DIR}/etc/systemd/system/multi-user.target.wants/reefy-mqtt.service"
