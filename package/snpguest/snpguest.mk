################################################################################
#
# snpguest
#
################################################################################

SNPGUEST_VERSION = 0.8.0
SNPGUEST_SITE = $(call github,virtee,snpguest,v$(SNPGUEST_VERSION))
SNPGUEST_LICENSE = Apache-2.0
SNPGUEST_LICENSE_FILES = LICENSE

$(eval $(cargo-package))
