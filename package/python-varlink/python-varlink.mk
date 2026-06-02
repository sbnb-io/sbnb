################################################################################
#
# python-varlink
#
################################################################################

PYTHON_VARLINK_VERSION = 31.0.0
PYTHON_VARLINK_SOURCE = varlink-$(PYTHON_VARLINK_VERSION).tar.gz
PYTHON_VARLINK_SITE = https://files.pythonhosted.org/packages/e6/90/172069117da79f1b62a29417dac7c7e544dda82bfb28af18167d1fb3aaaf
PYTHON_VARLINK_SETUP_TYPE = setuptools
PYTHON_VARLINK_LICENSE = Apache-2.0
PYTHON_VARLINK_LICENSE_FILES = LICENSE.txt

$(eval $(python-package))
