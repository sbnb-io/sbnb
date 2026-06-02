################################################################################
#
# python-varlink
#
################################################################################

# 32.x is GitHub-only (not published to PyPI). It dropped the deprecated
# setuptools_scm_git_archive build-req that broke 31.0.0's offline build,
# and is pyproject-only (no setup.py) so it builds via pep517.
PYTHON_VARLINK_VERSION = 32.1.0
PYTHON_VARLINK_SITE = $(call github,varlink,python,$(PYTHON_VARLINK_VERSION))
PYTHON_VARLINK_SETUP_TYPE = pep517
PYTHON_VARLINK_LICENSE = Apache-2.0
PYTHON_VARLINK_LICENSE_FILES = LICENSE.txt
# Version is dynamic via setuptools_scm; the GitHub tarball has no git
# metadata, so pin it explicitly (keeps the version single-sourced here).
PYTHON_VARLINK_DEPENDENCIES = host-python-setuptools-scm
PYTHON_VARLINK_ENV = SETUPTOOLS_SCM_PRETEND_VERSION=$(PYTHON_VARLINK_VERSION)

$(eval $(python-package))
