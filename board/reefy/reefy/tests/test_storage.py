"""Unit tests for reefy.storage (pure logic; no device, no root).

Also asserts the module imports without paho-mqtt - the data-plane and
boot-mount processes depend on that."""

import types
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)
from reefy import shared, storage


class ImportIsolationTests(unittest.TestCase):
    def test_storage_imports_without_paho(self):
        # If reefy.storage pulled in paho (not installed in this env) the
        # module-level import above would have failed.
        self.assertFalse(hasattr(storage, 'mqtt'))

    def test_constants_track_shared(self):
        self.assertEqual(storage.Storage.STORAGE_VG, shared.STORAGE_VG)
        self.assertEqual(storage.Storage.STATE_LV, shared.STATE_LV)
        self.assertEqual(storage.Storage.REEFY_DATA_MOUNT_OPTS,
                         shared.REEFY_DATA_MOUNT_OPTS)


class VolumeLvNameTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()

    def test_deterministic(self):
        a = self.s._volume_lv_name('/mnt/reefy-data/apps/x/config')
        b = self.s._volume_lv_name('/mnt/reefy-data/apps/x/config')
        self.assertEqual(a, b)

    def test_prefix_and_shape(self):
        n = self.s._volume_lv_name('/mnt/reefy-data/apps/x/config')
        self.assertTrue(n.startswith('reefy_backup_'))
        # reefy_backup_ + 12 hex chars; well under the LVM name limit and
        # free of the dm-mapper '--' escape.
        self.assertEqual(len(n), len('reefy_backup_') + 12)
        self.assertNotIn('--', n)

    def test_distinct_paths_distinct_names(self):
        a = self.s._volume_lv_name('/mnt/reefy-data/apps/x/config')
        b = self.s._volume_lv_name('/mnt/reefy-data/apps/x/data')
        self.assertNotEqual(a, b)


class FsMountOptsTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()

    def _run_returning(self, fstype):
        return mock.Mock(return_value=types.SimpleNamespace(stdout=fstype))

    def test_xfs_excludes_commit(self):
        with mock.patch.object(storage.subprocess, 'run',
                               self._run_returning('xfs\n')):
            self.assertEqual(self.s._fs_mount_opts('/dev/x'), 'noatime,discard')

    def test_ext4_uses_default_opts(self):
        with mock.patch.object(storage.subprocess, 'run',
                               self._run_returning('ext4\n')):
            self.assertEqual(self.s._fs_mount_opts('/dev/x'),
                             shared.REEFY_DATA_MOUNT_OPTS)

    def test_blank_falls_back_to_default(self):
        with mock.patch.object(storage.subprocess, 'run',
                               self._run_returning('')):
            self.assertEqual(self.s._fs_mount_opts('/dev/x'),
                             shared.REEFY_DATA_MOUNT_OPTS)


class VolumeCapsTests(unittest.TestCase):
    def test_set_volume_caps(self):
        s = storage.Storage()
        s.set_volume_caps({'/mnt/reefy-data/apps/x/media': 90})
        self.assertEqual(s._volume_caps['/mnt/reefy-data/apps/x/media'], 90)

    def test_constructor_aliases_dict(self):
        caps = {}
        s = storage.Storage(caps)
        self.assertIs(s._volume_caps, caps)


if __name__ == '__main__':
    unittest.main()
