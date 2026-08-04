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
echo "REEFY_DESIRED_STATE_SCHEMA=2" >> "${OS_RELEASE}"

# Internal exact-build identity for externally packaged kernel modules. Keep
# the public IMAGE_VERSION format unchanged. Hash the actual kernel image,
# configuration, and exported symbol CRCs so an incremental rebuild cannot
# accidentally select modules from different bytes with the same uname -r.
PINNED_KERNEL=$(sed -n 's/^BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION="v\([0-9.]*\)"$/\1/p' "${BR2_CONFIG}")
LINUX_BUILD_DIR=''
for candidate in "${BUILD_DIR}"/linux-*; do
  if [ "$(cat "${candidate}/include/config/kernel.release" 2>/dev/null || true)" = "${PINNED_KERNEL}" ] \
      && [ -f "${candidate}/arch/x86/boot/bzImage" ] \
      && [ -f "${candidate}/.config" ] \
      && [ -f "${candidate}/Module.symvers" ]; then
    if [ -n "${LINUX_BUILD_DIR}" ]; then
      echo "ERROR: multiple exact kernel build trees for ${PINNED_KERNEL}" >&2
      exit 1
    fi
    LINUX_BUILD_DIR=${candidate}
  fi
done
if [ -n "${LINUX_BUILD_DIR}" ] \
    && [ -f "${LINUX_BUILD_DIR}/arch/x86/boot/bzImage" ] \
    && [ -f "${LINUX_BUILD_DIR}/.config" ] \
    && [ -f "${LINUX_BUILD_DIR}/Module.symvers" ]; then
  KERNEL_ABI_SHA256=$(sha256sum \
    "${LINUX_BUILD_DIR}/.config" "${LINUX_BUILD_DIR}/Module.symvers" \
    | sha256sum | awk '{print $1}')
  BUILD_IDENTITY_SALT=${REEFY_E2E_BUILD_IDENTITY_SALT:-}
  if [ -n "${BUILD_IDENTITY_SALT}" ] \
      && [[ ! "${BUILD_IDENTITY_SALT}" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
    echo "ERROR: invalid E2E build identity salt" >&2
    exit 1
  fi
  if [ -z "${REEFY_BUILD_ID:-}" ]; then
    BASE_REEFY_BUILD_ID=$(sha256sum \
      "${LINUX_BUILD_DIR}/arch/x86/boot/bzImage" \
      "${LINUX_BUILD_DIR}/.config" \
      "${LINUX_BUILD_DIR}/Module.symvers" \
      | sha256sum | awk '{print $1}')
    REEFY_BUILD_ID=${BASE_REEFY_BUILD_ID}
    if [ -n "${BUILD_IDENTITY_SALT}" ]; then
      REEFY_BUILD_ID=$(printf '%s\0%s\0%s\0' \
        reefy-e2e-build-id-v1 "${BASE_REEFY_BUILD_ID}" \
        "${BUILD_IDENTITY_SALT}" | sha256sum | awk '{print $1}')
    fi
  elif [ -n "${BUILD_IDENTITY_SALT}" ]; then
    echo "ERROR: cannot combine REEFY_BUILD_ID with an E2E identity salt" >&2
    exit 1
  fi
  echo "REEFY_BUILD_ID=${REEFY_BUILD_ID}" >> "${OS_RELEASE}"
  echo "REEFY_KERNEL_ABI_SHA256=${KERNEL_ABI_SHA256}" >> "${OS_RELEASE}"
else
  echo "ERROR: cannot calculate Reefy kernel build identity" >&2
  exit 1
fi

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

# Prune kernel modules of superseded kernel versions from TARGET_DIR.
# Same stale-file class as above: Buildroot has no uninstall mechanism
# (pkg dirclean removes only build dirs + install stamps, never files
# already installed in TARGET_DIR), so after a kernel version bump the
# old /lib/modules/<old-version> dir lingers and ships in the rootfs.
# The CI "Verify pinned kernel version" step also requires exactly one
# version dir. Every package that installs kernel modules (linux,
# r8125/26/27/8168, nvidia-open-gpu) is dircleaned on version change and
# reinstalls under the pinned version, so purging old dirs here is safe.
# Fail-safe: if the pin can't be read, purge nothing - CI verify catches it.
if [ -n "${PINNED_KERNEL}" ] && [ -d "${TARGET_DIR}/lib/modules" ]; then
  find "${TARGET_DIR}/lib/modules" -mindepth 1 -maxdepth 1 -type d \
    ! -name "${PINNED_KERNEL}" -exec rm -rf {} +
fi

# Stage Intel GPU and NPU inputs from the exact kernel and firmware package
# build trees before removing their vendor-specific payload from Reefy OS.
# Reading the build trees, rather than TARGET_DIR, keeps incremental builds
# reproducible after a previous post-build pass has already pruned the files.
INTEL_STAGE="${BASE_DIR}/reefy-artifacts/intel"
INTEL_MODULES="${INTEL_STAGE}/modules-root/lib/modules/${PINNED_KERNEL}/extra/intel"
INTEL_FIRMWARE="${INTEL_STAGE}/firmware-root"
LINUX_FIRMWARE_VERSION=$(sed -n \
  's/^LINUX_FIRMWARE_VERSION = \(.*\)$/\1/p' \
  "${BR2_EXTERNAL_REEFY_PATH}/buildroot/package/linux-firmware/linux-firmware.mk")
INTEL_NPU_FIRMWARE_VERSION=$(sed -n \
  's/^INTEL_NPU_FIRMWARE_VERSION = \(.*\)$/\1/p' \
  "${BR2_EXTERNAL_REEFY_PATH}/package/intel-npu-firmware/intel-npu-firmware.mk")
LINUX_FIRMWARE_BUILD="${BUILD_DIR}/linux-firmware-${LINUX_FIRMWARE_VERSION}"
INTEL_NPU_FIRMWARE_BUILD="${BUILD_DIR}/intel-npu-firmware-${INTEL_NPU_FIRMWARE_VERSION}"
rm -rf "${INTEL_STAGE}"
mkdir -p "${INTEL_MODULES}" \
  "${INTEL_FIRMWARE}/lib/firmware/i915" \
  "${INTEL_FIRMWARE}/lib/firmware/xe" \
  "${INTEL_FIRMWARE}/lib/firmware/intel/vpu" \
  "${INTEL_FIRMWARE}/usr/share/licenses/intel-provider"

for module in \
    "${LINUX_BUILD_DIR}/drivers/gpu/drm/i915/i915.ko" \
    "${LINUX_BUILD_DIR}/drivers/gpu/drm/xe/xe.ko" \
    "${LINUX_BUILD_DIR}/drivers/accel/ivpu/intel_vpu.ko"; do
  if [ ! -f "${module}" ]; then
    echo "ERROR: missing Intel provider module ${module}" >&2
    exit 1
  fi
  cp "${module}" "${INTEL_MODULES}/"
done
if [ -f "${LINUX_BUILD_DIR}/drivers/gpu/drm/i915/kvmgt.ko" ]; then
  cp "${LINUX_BUILD_DIR}/drivers/gpu/drm/i915/kvmgt.ko" \
    "${INTEL_MODULES}/"
fi
for module in "${INTEL_MODULES}"/*.ko; do
  "${HOST_DIR}/bin/x86_64-buildroot-linux-gnu-strip" --strip-debug "${module}"
done

for directory in i915 xe; do
  if [ ! -d "${LINUX_FIRMWARE_BUILD}/${directory}" ]; then
    echo "ERROR: missing Intel firmware directory ${directory}" >&2
    exit 1
  fi
  cp -a "${LINUX_FIRMWARE_BUILD}/${directory}/." \
    "${INTEL_FIRMWARE}/lib/firmware/${directory}/"
done
if [ ! -d "${INTEL_NPU_FIRMWARE_BUILD}/intel/vpu" ]; then
  echo "ERROR: missing Intel VPU firmware input" >&2
  exit 1
fi
cp -a "${INTEL_NPU_FIRMWARE_BUILD}/intel/vpu/." \
  "${INTEL_FIRMWARE}/lib/firmware/intel/vpu/"
for license in LICENSE.i915 LICENSE.xe; do
  cp "${LINUX_FIRMWARE_BUILD}/${license}" \
    "${INTEL_FIRMWARE}/usr/share/licenses/intel-provider/${license}"
done

# NVIDIA packages in the Buildroot configuration produce exact inputs for the
# external provider artifact. None of their driver payload belongs in the
# immutable Reefy OS image. Remove stale files from incremental TARGET_DIR
# builds as well as files installed by older package recipes.
rm -f "${TARGET_DIR}/usr/bin/nvidia-smi" \
      "${TARGET_DIR}/usr/bin/nvidia-ctk" \
      "${TARGET_DIR}/usr/bin/nvidia-cdi-hook" \
      "${TARGET_DIR}/usr/bin/nvidia-cdi-setup.sh" \
      "${TARGET_DIR}/usr/sbin/nvidia-smi" \
      "${TARGET_DIR}/usr/lib/systemd/system/nvidia-cdi-generate.service" \
      "${TARGET_DIR}/etc/systemd/system/nvidia-cdi-generate.service" \
      "${TARGET_DIR}/etc/systemd/system/multi-user.target.wants/nvidia-cdi-generate.service" \
      "${TARGET_DIR}"/usr/lib/libcuda.so* \
      "${TARGET_DIR}"/usr/lib/libnvcuvid.so* \
      "${TARGET_DIR}"/usr/lib/libnvidia-*.so* \
      "${TARGET_DIR}"/usr/lib/libEGL_nvidia.so* \
      "${TARGET_DIR}"/usr/lib/libGLESv1_CM_nvidia.so* \
      "${TARGET_DIR}"/usr/lib/libGLESv2_nvidia.so* \
      "${TARGET_DIR}"/usr/lib/libGLX_nvidia.so* \
      "${TARGET_DIR}"/usr/lib/libnvoptix.so* \
      "${TARGET_DIR}"/usr/lib/libvdpau_nvidia.so* \
      "${TARGET_DIR}"/usr/lib/libnvidia-drm_gbm.so \
      "${TARGET_DIR}"/usr/lib/_nvngx.dll \
      "${TARGET_DIR}"/etc/vulkan/icd.d/nvidia*.json \
      "${TARGET_DIR}"/etc/glvnd/egl_vendor.d/10_nvidia.json
rm -rf "${TARGET_DIR}/lib/firmware/nvidia" \
       "${TARGET_DIR}/usr/lib/firmware/nvidia"
if [ -d "${TARGET_DIR}/lib/modules" ]; then
  find "${TARGET_DIR}/lib/modules" -type f -name 'nvidia*.ko*' -delete
  # The in-tree AMD module is enabled only to expose the exact shared DRM
  # kernel ABI used by the separately published AMD 31.40 provider. Never
  # ship that older in-tree driver beside the external provider module.
  find "${TARGET_DIR}/lib/modules" -type f -name 'amdgpu.ko*' -delete
  # Intel GPU and NPU modules are staged above for the exact-build provider.
  # Shared DRM helpers remain in the base OS for all vendors.
  find "${TARGET_DIR}/lib/modules" -type f \
    \( -name 'i915.ko*' -o -name 'xe.ko*' -o -name 'intel_vpu.ko*' \
       -o -name 'kvmgt.ko*' \) -delete
  find "${TARGET_DIR}/lib/modules" -depth -type d -empty -delete
fi
rm -rf "${TARGET_DIR}/lib/firmware/i915" \
       "${TARGET_DIR}/lib/firmware/xe" \
       "${TARGET_DIR}/lib/firmware/intel/vpu"
if [ -d "${TARGET_DIR}/lib/firmware/intel" ]; then
  find "${TARGET_DIR}/lib/firmware/intel" -depth -type d -empty -delete
fi
if [ -n "${PINNED_KERNEL}" ] && [ -x "${HOST_DIR}/sbin/depmod" ]; then
  "${HOST_DIR}/sbin/depmod" -a -b "${TARGET_DIR}" "${PINNED_KERNEL}"
fi
