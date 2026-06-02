"""Test bootstrap: put the on-device reefy lib dir on sys.path so tests
import the package exactly as the /usr/bin entry scripts do
(`sys.path.insert(0, '/usr/lib/reefy')` -> `import reefy.<mod>`).

Not a test module (leading underscore) so unittest discovery skips it.
These tests live OUTSIDE rootfs-overlay/ on purpose - the overlay ships
verbatim to the device and must not carry tests."""

import os
import sys

LIB_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__),
    '..', 'rootfs-overlay', 'usr', 'lib', 'reefy'))

if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)
