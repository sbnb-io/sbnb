################################################################################
#
# nvidia-gsp-firmware
#
################################################################################

NVIDIA_GSP_FIRMWARE_VERSION = 595.84
NVIDIA_GSP_FIRMWARE_SITE = https://us.download.nvidia.com/XFree86/Linux-x86_64/$(NVIDIA_GSP_FIRMWARE_VERSION)
NVIDIA_GSP_FIRMWARE_SOURCE = NVIDIA-Linux-x86_64-$(NVIDIA_GSP_FIRMWARE_VERSION).run
NVIDIA_GSP_FIRMWARE_LICENSE = NVIDIA Proprietary
NVIDIA_GSP_FIRMWARE_LICENSE_FILES = LICENSE
NVIDIA_GSP_FIRMWARE_REDISTRIBUTE = NO

# The .run file is a self-extracting shell script, not a standard archive.
# It ignores --target and always extracts to $CWD/NVIDIA-Linux-x86_64-<ver>/
define NVIDIA_GSP_FIRMWARE_EXTRACT_CMDS
	chmod +x $(NVIDIA_GSP_FIRMWARE_DL_DIR)/$(NVIDIA_GSP_FIRMWARE_SOURCE)
	cd $(@D) && $(NVIDIA_GSP_FIRMWARE_DL_DIR)/$(NVIDIA_GSP_FIRMWARE_SOURCE) \
		--extract-only
endef

define NVIDIA_GSP_FIRMWARE_INSTALL_TARGET_CMDS
	$(INSTALL) -d $(TARGET_DIR)/lib/firmware/nvidia/$(NVIDIA_GSP_FIRMWARE_VERSION)
	$(INSTALL) -m 0644 \
		$(@D)/NVIDIA-Linux-x86_64-$(NVIDIA_GSP_FIRMWARE_VERSION)/firmware/gsp_*.bin \
		$(TARGET_DIR)/lib/firmware/nvidia/$(NVIDIA_GSP_FIRMWARE_VERSION)/
endef

$(eval $(generic-package))
