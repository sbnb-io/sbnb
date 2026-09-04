################################################################################
#
# thin-provisioning-tools
#
################################################################################

THIN_PROVISIONING_TOOLS_VERSION = 1.3.2
THIN_PROVISIONING_TOOLS_SITE = $(call github,device-mapper-utils,thin-provisioning-tools,v$(THIN_PROVISIONING_TOOLS_VERSION))
THIN_PROVISIONING_TOOLS_LICENSE = GPL-3.0-only
THIN_PROVISIONING_TOOLS_LICENSE_FILES = COPYING
THIN_PROVISIONING_TOOLS_DEPENDENCIES = host-clang lvm2 udev

# devicemapper-sys is both a target dependency and a build dependency. Avoid
# advertising target libdevmapper to the host build script. This uses the
# crate's supported cross-build feature without modifying the upstream source.
THIN_PROVISIONING_TOOLS_CARGO_BUILD_OPTS = \
	--features devicemapper/disable_cargo_metadata
THIN_PROVISIONING_TOOLS_CARGO_INSTALL_OPTS = \
	--features devicemapper/disable_cargo_metadata

define THIN_PROVISIONING_TOOLS_INSTALL_COMMAND_LINKS
	$(foreach tool,cache_check cache_dump cache_metadata_size cache_repair \
		cache_restore cache_writeback era_check era_dump era_invalidate \
		era_repair era_restore thin_check thin_delta thin_dump thin_ls \
		thin_metadata_pack thin_metadata_size thin_metadata_unpack \
		thin_migrate thin_repair thin_restore thin_rmap thin_shrink thin_trim,\
		ln -sf ../bin/pdata_tools $(TARGET_DIR)/usr/sbin/$(tool)$(sep))
endef
THIN_PROVISIONING_TOOLS_POST_INSTALL_TARGET_HOOKS += THIN_PROVISIONING_TOOLS_INSTALL_COMMAND_LINKS

$(eval $(cargo-package))
