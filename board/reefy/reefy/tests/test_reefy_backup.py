"""Unit tests for the reefy-backup snapshot mount options.

reefy-backup is a /usr/bin script (no .py), so load it by path. Tests the
fs-type-aware snapshot mount: XFS snapshots share the origin's UUID and
can't recover a log read-only, so they need nouuid+norecovery (the
storage redesign switched per-app volumes to XFS, which broke the old
`-o ro` mount)."""

import importlib.util
import os
import types
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), '..', 'rootfs-overlay',
                     'usr', 'bin', 'reefy-backup')
# reefy-backup has no .py extension, so use an explicit source loader
# (spec_from_file_location can't infer a loader and returns None).
_loader = SourceFileLoader('reefy_backup', _PATH)
_spec = importlib.util.spec_from_loader('reefy_backup', _loader)
reefy_backup = importlib.util.module_from_spec(_spec)
_loader.exec_module(reefy_backup)


class SnapshotMountOptsTests(unittest.TestCase):
    def _mount_opts_for(self, fstype):
        """Run snapshot_volume with lvcreate/blkid/mount mocked; return the
        mount option string that would be used for a snapshot of `fstype`."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd and cmd[0] == 'blkid':
                return types.SimpleNamespace(returncode=0, stdout=fstype + '\n', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(reefy_backup.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup.os, 'makedirs'):
            reefy_backup.snapshot_volume(
                '/mnt/reefy-data/apps/i1/config', 'i1', 1234567890)

        mount_cmd = next(c for c in calls if c and c[0] == 'mount')
        # mount -o <opts> <dev> <mnt>
        return mount_cmd[mount_cmd.index('-o') + 1]

    def test_xfs_snapshot_uses_nouuid_norecovery(self):
        opts = self._mount_opts_for('xfs')
        self.assertIn('nouuid', opts)
        self.assertIn('norecovery', opts)
        self.assertTrue(opts.split(',')[0] == 'ro', f'expected read-only: {opts}')

    def test_ext4_snapshot_plain_ro(self):
        self.assertEqual(self._mount_opts_for('ext4'), 'ro')

    def test_volume_lv_name_matches_storage(self):
        # Must mirror reefy.storage.Storage._volume_lv_name so the snapshot
        # targets the right LV.
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..',
                                        'rootfs-overlay', 'usr', 'lib', 'reefy'))
        from reefy.storage import Storage
        path = '/mnt/reefy-data/apps/i1/config'
        self.assertEqual(reefy_backup.volume_lv_name(path),
                         Storage()._volume_lv_name(path))


if __name__ == '__main__':
    unittest.main()
