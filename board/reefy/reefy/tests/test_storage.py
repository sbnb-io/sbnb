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


class MountStateLvTests(unittest.TestCase):
    """_mount_state_lv: the holistic state-LV mount used by every provision
    path (and the boot shell's port source). Skips when the LV is absent or
    already mounted; mounts XFS with the discard opts; seeds an empty LV from
    the tmpfs state dir."""

    def _capture(self, present=True, already_mounted=False, fstype='xfs',
                 tmp_empty=True, sdir_has=False):
        s = storage.Storage()
        mounts, cps = [], []

        def fake_run(cmd, **kw):
            if cmd and cmd[0] == 'mountpoint':
                rc = 0 if already_mounted else 1
                return types.SimpleNamespace(returncode=rc, stdout='', stderr='')
            if cmd and cmd[0] == 'blkid':
                return types.SimpleNamespace(returncode=0, stdout=fstype + '\n',
                                             stderr='')
            if cmd and cmd[0] == 'mount':
                mounts.append(cmd)
            if cmd and cmd[0] == 'cp':
                cps.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        def fake_listdir(p):
            if p == '/tmp/_st':
                return [] if tmp_empty else ['x']
            return ['device.key'] if sdir_has else []

        with mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'exists', return_value=present), \
                mock.patch.object(storage.os, 'makedirs'), \
                mock.patch.object(storage.tempfile, 'mkdtemp',
                                  return_value='/tmp/_st'), \
                mock.patch.object(storage.os, 'listdir', side_effect=fake_listdir), \
                mock.patch.object(storage.os.path, 'isdir', return_value=True), \
                mock.patch.object(storage.os, 'rmdir'):
            s._mount_state_lv()
        final = [c for c in mounts if c[-1].endswith('/state')]
        return mounts, cps, final

    def test_absent_lv_is_noop(self):
        mounts, _, _ = self._capture(present=False)
        self.assertEqual(mounts, [], 'mounted a state LV that does not exist')

    def test_already_mounted_skips(self):
        mounts, _, _ = self._capture(already_mounted=True)
        self.assertEqual(mounts, [], 're-mounted an already-mounted state LV')

    def test_xfs_mounts_state_with_discard_opts(self):
        _, _, final = self._capture(fstype='xfs')
        self.assertTrue(final, 'state LV was not mounted at /mnt/reefy-data/state')
        opts = final[0][final[0].index('-o') + 1]
        self.assertEqual(opts, 'noatime,discard')

    def test_seeds_empty_lv_from_tmpfs_state(self):
        # Fresh LV (tmp empty) + existing tmpfs state dir has content -> copy.
        _, cps, _ = self._capture(tmp_empty=True, sdir_has=True)
        self.assertTrue(cps, 'empty state LV was not seeded from the state dir')

    def test_nonempty_lv_not_overwritten(self):
        # LV already has data -> never clobber it.
        _, cps, _ = self._capture(tmp_empty=False, sdir_has=True)
        self.assertEqual(cps, [], 'overwrote a non-empty state LV')


if __name__ == '__main__':
    unittest.main()
