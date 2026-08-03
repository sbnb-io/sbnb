################################################################################
#
# nvidia-userspace
#
################################################################################

NVIDIA_USERSPACE_VERSION = 595.84
NVIDIA_USERSPACE_SITE = https://us.download.nvidia.com/XFree86/Linux-x86_64/$(NVIDIA_USERSPACE_VERSION)
NVIDIA_USERSPACE_SOURCE = NVIDIA-Linux-x86_64-$(NVIDIA_USERSPACE_VERSION).run
NVIDIA_USERSPACE_LICENSE = NVIDIA Proprietary
NVIDIA_USERSPACE_LICENSE_FILES = LICENSE
NVIDIA_USERSPACE_REDISTRIBUTE = NO

NVIDIA_USERSPACE_RUNDIR = $(@D)/NVIDIA-Linux-x86_64-$(NVIDIA_USERSPACE_VERSION)

define NVIDIA_USERSPACE_EXTRACT_CMDS
	chmod +x $(NVIDIA_USERSPACE_DL_DIR)/$(NVIDIA_USERSPACE_SOURCE)
	cd $(@D) && $(NVIDIA_USERSPACE_DL_DIR)/$(NVIDIA_USERSPACE_SOURCE) \
		--extract-only
endef

define NVIDIA_USERSPACE_INSTALL_TARGET_CMDS
	# nvidia-smi monitoring tool
	$(INSTALL) -m 0755 $(NVIDIA_USERSPACE_RUNDIR)/nvidia-smi \
		$(TARGET_DIR)/usr/bin/

	# NVIDIA Management Library
	$(INSTALL) -m 0755 $(NVIDIA_USERSPACE_RUNDIR)/libnvidia-ml.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/
	ln -sf libnvidia-ml.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/libnvidia-ml.so.1
	ln -sf libnvidia-ml.so.1 \
		$(TARGET_DIR)/usr/lib/libnvidia-ml.so

	# CUDA Driver API
	$(INSTALL) -m 0755 $(NVIDIA_USERSPACE_RUNDIR)/libcuda.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/
	ln -sf libcuda.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/libcuda.so.1
	ln -sf libcuda.so.1 \
		$(TARGET_DIR)/usr/lib/libcuda.so

	# PTX JIT compiler (needed by CUDA apps)
	$(INSTALL) -m 0755 $(NVIDIA_USERSPACE_RUNDIR)/libnvidia-ptxjitcompiler.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/
	ln -sf libnvidia-ptxjitcompiler.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/libnvidia-ptxjitcompiler.so.1
	ln -sf libnvidia-ptxjitcompiler.so.1 \
		$(TARGET_DIR)/usr/lib/libnvidia-ptxjitcompiler.so

	# NVDEC video decoder (needed by FFmpeg h264_cuvid/hevc_cuvid)
	$(INSTALL) -m 0755 $(NVIDIA_USERSPACE_RUNDIR)/libnvcuvid.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/
	ln -sf libnvcuvid.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/libnvcuvid.so.1
	ln -sf libnvcuvid.so.1 \
		$(TARGET_DIR)/usr/lib/libnvcuvid.so

	# NVENC video encoder
	$(INSTALL) -m 0755 $(NVIDIA_USERSPACE_RUNDIR)/libnvidia-encode.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/
	ln -sf libnvidia-encode.so.$(NVIDIA_USERSPACE_VERSION) \
		$(TARGET_DIR)/usr/lib/libnvidia-encode.so.1
	ln -sf libnvidia-encode.so.1 \
		$(TARGET_DIR)/usr/lib/libnvidia-encode.so
endef

$(eval $(generic-package))
