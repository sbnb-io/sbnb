################################################################################
#
# sevctl
#
################################################################################

SEVCTL_VERSION = 0.6.0
SEVCTL_SITE = $(call github,virtee,sevctl,v$(SEVCTL_VERSION))
SEVCTL_LICENSE = Apache-2.0
SEVCTL_LICENSE_FILES = LICENSE

$(eval $(cargo-package))
