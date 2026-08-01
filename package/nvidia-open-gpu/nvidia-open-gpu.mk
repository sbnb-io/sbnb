################################################################################
#
# nvidia-open-gpu
#
################################################################################

NVIDIA_OPEN_GPU_VERSION = 595.84
NVIDIA_OPEN_GPU_SITE = https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags
NVIDIA_OPEN_GPU_SOURCE = $(NVIDIA_OPEN_GPU_VERSION).tar.gz
NVIDIA_OPEN_GPU_LICENSE = MIT, GPL-2.0
NVIDIA_OPEN_GPU_LICENSE_FILES = COPYING

# Cannot use kernel-module infrastructure: NVIDIA's build is 3-phase
# (OS-agnostic core, modeset core, then kbuild). The standard kernel-module
# infra only does phase 3.
NVIDIA_OPEN_GPU_DEPENDENCIES = linux

NVIDIA_OPEN_GPU_MAKE_ENV = \
	$(TARGET_MAKE_ENV) \
	$(LINUX_MAKE_ENV)

NVIDIA_OPEN_GPU_MAKE_OPTS = \
	TARGET_ARCH=$(if $(BR2_aarch64),aarch64,x86_64) \
	CC="$(TARGET_CC)" \
	CXX="$(TARGET_CXX)" \
	LD="$(TARGET_LD)" \
	AR="$(TARGET_AR)" \
	OBJCOPY="$(TARGET_OBJCOPY)" \
	SYSSRC="$(LINUX_DIR)" \
	SYSOUT="$(LINUX_DIR)" \
	IGNORE_CC_MISMATCH=1

define NVIDIA_OPEN_GPU_BUILD_CMDS
	$(NVIDIA_OPEN_GPU_MAKE_ENV) $(MAKE) -C $(@D) \
		$(NVIDIA_OPEN_GPU_MAKE_OPTS) \
		modules -j$(PARALLEL_JOBS)
endef

define NVIDIA_OPEN_GPU_INSTALL_TARGET_CMDS
	$(NVIDIA_OPEN_GPU_MAKE_ENV) $(MAKE) -C $(LINUX_DIR) \
		$(LINUX_MAKE_FLAGS) \
		M=$(@D)/kernel-open \
		INSTALL_MOD_PATH=$(TARGET_DIR) \
		modules_install
	rm -rf $(BASE_DIR)/reefy-artifacts/nvidia/modules-root
	$(NVIDIA_OPEN_GPU_MAKE_ENV) $(MAKE) -C $(LINUX_DIR) \
		$(LINUX_MAKE_FLAGS) \
		M=$(@D)/kernel-open \
		INSTALL_MOD_PATH=$(BASE_DIR)/reefy-artifacts/nvidia/modules-root \
		modules_install
endef

$(eval $(generic-package))
