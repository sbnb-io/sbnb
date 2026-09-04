################################################################################
#
# thin-provisioning-tools
#
################################################################################

THIN_PROVISIONING_TOOLS_VERSION = 1.3.3
THIN_PROVISIONING_TOOLS_SITE = $(call github,device-mapper-utils,thin-provisioning-tools,v$(THIN_PROVISIONING_TOOLS_VERSION))
THIN_PROVISIONING_TOOLS_LICENSE = GPL-3.0-only
THIN_PROVISIONING_TOOLS_LICENSE_FILES = COPYING
THIN_PROVISIONING_TOOLS_DEPENDENCIES = host-clang lvm2 systemd

define THIN_PROVISIONING_TOOLS_INSTALL_COMMAND_LINKS
	$(foreach tool,thin_check thin_dump thin_repair thin_restore,\
		ln -sf pdata_tools $(TARGET_DIR)/usr/sbin/$(tool)$(sep))
endef
THIN_PROVISIONING_TOOLS_POST_INSTALL_TARGET_HOOKS += THIN_PROVISIONING_TOOLS_INSTALL_COMMAND_LINKS

$(eval $(cargo-package))
