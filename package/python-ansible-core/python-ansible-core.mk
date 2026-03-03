################################################################################
#
# python-ansible-core
#
################################################################################

PYTHON_ANSIBLE_CORE_VERSION = 2.20.3
PYTHON_ANSIBLE_CORE_SOURCE = ansible_core-$(PYTHON_ANSIBLE_CORE_VERSION).tar.gz
PYTHON_ANSIBLE_CORE_SITE = https://files.pythonhosted.org/packages/source/a/ansible-core
PYTHON_ANSIBLE_CORE_SETUP_TYPE = pep517
PYTHON_ANSIBLE_CORE_LICENSE = GPL-3.0-or-later
PYTHON_ANSIBLE_CORE_LICENSE_FILES = COPYING

# Runtime dependencies for ansible-core
PYTHON_ANSIBLE_CORE_DEPENDENCIES = \
	python3 \
	python-cryptography \
	python-jinja2 \
	python-packaging \
	python-pyyaml \
	python-resolvelib

$(eval $(python-package))
