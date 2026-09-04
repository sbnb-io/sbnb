################################################################################
#
# thin-provisioning-tools
#
################################################################################

THIN_PROVISIONING_TOOLS_VERSION = 1.3.2
THIN_PROVISIONING_TOOLS_SITE = $(call github,device-mapper-utils,thin-provisioning-tools,v$(THIN_PROVISIONING_TOOLS_VERSION))
THIN_PROVISIONING_TOOLS_LICENSE = GPL-3.0-only
THIN_PROVISIONING_TOOLS_LICENSE_FILES = COPYING
THIN_PROVISIONING_TOOLS_DEPENDENCIES = lvm2

# The Reefy patch removes thin_migrate's dependency-only crates. Prune those
# entries from the upstream lock file using only the already-vendored sources.
define THIN_PROVISIONING_TOOLS_CONFIGURE_CMDS
	cd $(@D) && $(TARGET_MAKE_ENV) $(PKG_CARGO_ENV) cargo update --offline
endef

define THIN_PROVISIONING_TOOLS_INSTALL_COMMAND_LINKS
	$(foreach tool,thin_check thin_dump thin_repair thin_restore,\
		ln -sf ../bin/pdata_tools $(TARGET_DIR)/usr/sbin/$(tool)$(sep))
endef
THIN_PROVISIONING_TOOLS_POST_INSTALL_TARGET_HOOKS += THIN_PROVISIONING_TOOLS_INSTALL_COMMAND_LINKS

$(eval $(cargo-package))
