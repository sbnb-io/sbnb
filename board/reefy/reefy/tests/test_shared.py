"""Unit tests for reefy.shared (pure, device-free)."""

import unittest

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)
from reefy import shared


class PartDevTests(unittest.TestCase):
    def test_nvme_gets_p_separator(self):
        self.assertEqual(shared._part_dev('nvme0n1', '3'), 'nvme0n1p3')

    def test_mmcblk_gets_p_separator(self):
        self.assertEqual(shared._part_dev('mmcblk0', '1'), 'mmcblk0p1')

    def test_sata_usb_no_separator(self):
        self.assertEqual(shared._part_dev('sda', '3'), 'sda3')

    def test_accepts_int_partnum(self):
        self.assertEqual(shared._part_dev('sda', 1), 'sda1')
        self.assertEqual(shared._part_dev('nvme0n1', 4), 'nvme0n1p4')


class ImportIsolationTests(unittest.TestCase):
    def test_shared_has_no_paho_dependency(self):
        # Importing reefy.shared in this process (paho not required) is
        # itself the assertion; re-import to be explicit.
        import importlib
        importlib.import_module('reefy.shared')


if __name__ == '__main__':
    unittest.main()
