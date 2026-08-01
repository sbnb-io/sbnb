################################################################################
#
# nvidia-container-toolkit
#
################################################################################

NVIDIA_CONTAINER_TOOLKIT_VERSION = 1.18.2
NVIDIA_CONTAINER_TOOLKIT_SITE = $(call github,NVIDIA,nvidia-container-toolkit,v$(NVIDIA_CONTAINER_TOOLKIT_VERSION))
NVIDIA_CONTAINER_TOOLKIT_LICENSE = Apache-2.0
NVIDIA_CONTAINER_TOOLKIT_LICENSE_FILES = LICENSE

NVIDIA_CONTAINER_TOOLKIT_GOMOD = github.com/NVIDIA/nvidia-container-toolkit

NVIDIA_CONTAINER_TOOLKIT_LDFLAGS = \
	-s -w \
	-X $(NVIDIA_CONTAINER_TOOLKIT_GOMOD)/internal/info.version=$(NVIDIA_CONTAINER_TOOLKIT_VERSION)

# go-nvml uses dlopen("libnvidia-ml.so.1") at runtime, so NVML symbols
# must not be resolved at link time.
NVIDIA_CONTAINER_TOOLKIT_EXTLDFLAGS = \
	-Wl,--unresolved-symbols=ignore-in-object-files -Wl,-z,lazy

NVIDIA_CONTAINER_TOOLKIT_TAGS = cgo

NVIDIA_CONTAINER_TOOLKIT_BUILD_TARGETS = \
	cmd/nvidia-ctk \
	cmd/nvidia-cdi-hook

NVIDIA_CONTAINER_TOOLKIT_INSTALL_TARGET = NO

define NVIDIA_CONTAINER_TOOLKIT_STAGE_PROVIDER_INPUTS
	rm -rf $(BASE_DIR)/reefy-artifacts/nvidia/toolkit
	$(INSTALL) -D -m 0755 $(@D)/bin/nvidia-ctk \
		$(BASE_DIR)/reefy-artifacts/nvidia/toolkit/nvidia-ctk
	$(INSTALL) -D -m 0755 $(@D)/bin/nvidia-cdi-hook \
		$(BASE_DIR)/reefy-artifacts/nvidia/toolkit/nvidia-cdi-hook
endef

NVIDIA_CONTAINER_TOOLKIT_POST_BUILD_HOOKS += \
	NVIDIA_CONTAINER_TOOLKIT_STAGE_PROVIDER_INPUTS

$(eval $(golang-package))
