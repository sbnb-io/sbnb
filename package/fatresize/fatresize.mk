################################################################################
#
# fatresize
#
################################################################################

FATRESIZE_VERSION = v1.1.0
FATRESIZE_SITE = https://github.com/ya-mouse/fatresize
FATRESIZE_SITE_METHOD = git

FATRESIZE_LICENSE = GPL-3.0+
FATRESIZE_LICENSE_FILES = COPYING
FATRESIZE_INSTALL_STAGING = YES

$(eval $(autotools-package))
