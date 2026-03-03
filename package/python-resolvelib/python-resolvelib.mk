################################################################################
#
# python-resolvelib
#
################################################################################

PYTHON_RESOLVELIB_VERSION = 1.2.1
PYTHON_RESOLVELIB_SOURCE = resolvelib-$(PYTHON_RESOLVELIB_VERSION).tar.gz
PYTHON_RESOLVELIB_SITE = https://files.pythonhosted.org/packages/1d/14/4669927e06631070edb968c78fdb6ce8992e27c9ab2cde4b3993e22ac7af
PYTHON_RESOLVELIB_SETUP_TYPE = setuptools
PYTHON_RESOLVELIB_LICENSE = ISC
PYTHON_RESOLVELIB_LICENSE_FILES = LICENSE

$(eval $(python-package))
