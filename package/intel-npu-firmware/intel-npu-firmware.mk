################################################################################
#
# intel-npu-firmware
#
################################################################################

INTEL_NPU_FIRMWARE_VERSION = 20260622
INTEL_NPU_FIRMWARE_SITE = $(BR2_KERNEL_MIRROR)/linux/kernel/firmware
INTEL_NPU_FIRMWARE_SOURCE = linux-firmware-$(INTEL_NPU_FIRMWARE_VERSION).tar.xz
INTEL_NPU_FIRMWARE_LICENSE = Intel Proprietary
INTEL_NPU_FIRMWARE_LICENSE_FILES = LICENSE.intel_vpu
INTEL_NPU_FIRMWARE_REDISTRIBUTE = NO

define INTEL_NPU_FIRMWARE_INSTALL_TARGET_CMDS
	$(INSTALL) -d $(TARGET_DIR)/lib/firmware/intel/vpu
	$(INSTALL) -m 0644 $(@D)/intel/vpu/vpu_*.bin \
		$(TARGET_DIR)/lib/firmware/intel/vpu/
endef

$(eval $(generic-package))
