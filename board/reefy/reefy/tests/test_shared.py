"""Unit tests for reefy.shared (pure, device-free)."""

import types
import unittest
from unittest import mock

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


class InstanceUuidsInComposeTests(unittest.TestCase):
    def test_filters_infra_and_tty(self):
        compose = {'services': {
            'app1': {}, 'app1-tty': {}, 'cloudflared': {},
            'reefy-proxy': {}, 'app2': {}}}
        self.assertEqual(
            sorted(shared.instance_uuids_in_compose(compose)), ['app1', 'app2'])

    def test_empty_compose(self):
        self.assertEqual(shared.instance_uuids_in_compose({}), [])
        self.assertEqual(shared.instance_uuids_in_compose(None), [])


class FindWirelessIfaceTests(unittest.TestCase):
    def test_parses_iw_dev(self):
        out = 'phy#0\n\tInterface wlan0\n\t\ttype managed\n'
        with mock.patch.object(shared.subprocess, 'run',
                               return_value=types.SimpleNamespace(stdout=out)):
            self.assertEqual(shared.find_wireless_iface(), 'wlan0')

    def test_none_when_no_iface(self):
        with mock.patch.object(shared.subprocess, 'run',
                               return_value=types.SimpleNamespace(stdout='phy#0\n')):
            self.assertIsNone(shared.find_wireless_iface())


class ImportIsolationTests(unittest.TestCase):
    def test_shared_has_no_paho_dependency(self):
        # Importing reefy.shared in this process (paho not required) is
        # itself the assertion; re-import to be explicit.
        import importlib
        importlib.import_module('reefy.shared')


if __name__ == '__main__':
    unittest.main()
