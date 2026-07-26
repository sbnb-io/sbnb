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


class ReclaimDeletedInstanceLvsTests(unittest.TestCase):
    """_reclaim_deleted_instance_lvs: lvremove a deleted instance's volume
    LVs (the prod LV-leak guard the e2e backup-lvm scenario covers), but
    KEEP a volume whose instance is still live (merely un-backup-flagged)."""

    def setUp(self):
        self.s = storage.Storage()
        self.cfg = '/mnt/reefy-data/apps/i1/config'
        self.dat = '/mnt/reefy-data/apps/i1/data'

    def _reclaim(self, old_state, new_state, new_backup_paths, lv_present=True):
        import contextlib
        removed = []

        def fake_run(cmd, **kw):
            rc = 0 if (cmd[0] != 'lvs' or lv_present) else 5
            if cmd[0] == 'lvremove':
                removed.append(cmd[-1])
            return types.SimpleNamespace(returncode=rc, stdout='', stderr='')

        with mock.patch.object(self.s, '_has_thin_pool', return_value=True), \
                mock.patch.object(self.s, '_volume_lock',
                                  return_value=contextlib.nullcontext()), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run):
            self.s._reclaim_deleted_instance_lvs(
                old_state, new_state, new_backup_paths)
        return removed

    def _lv(self, path):
        return f'{self.s.STORAGE_VG}/{self.s._volume_lv_name(path)}'

    def test_reclaims_deleted_instance_volumes(self):
        old = {'backup': {'instances': [
            {'instance_uuid': 'i1', 'paths': [self.cfg, self.dat]}]}}
        new = {'instances': [], 'app_volumes': [],
               'backup': {'instances': []}}
        removed = self._reclaim(old, new, set())
        self.assertCountEqual(removed, [self._lv(self.cfg), self._lv(self.dat)])

    def test_keeps_volume_of_still_live_instance(self):
        # config leaves the backup set but instance i1 is still live
        # (present in app_volumes/instances) -> must NOT be reclaimed.
        old = {'backup': {'instances': [
            {'instance_uuid': 'i1', 'paths': [self.cfg, self.dat]}]}}
        new = {'instances': [{'uuid': 'i1'}],
               'app_volumes': [{'host_path': self.cfg},
                               {'host_path': self.dat}],
               'backup': {'instances': [
                   {'instance_uuid': 'i1', 'paths': [self.dat]}]}}
        removed = self._reclaim(old, new, {self.dat})
        self.assertEqual(removed, [], 'reclaimed a still-live instance volume')

    def test_noop_without_thin_pool(self):
        old = {'backup': {'instances': [
            {'instance_uuid': 'i1', 'paths': [self.cfg]}]}}
        with mock.patch.object(self.s, '_has_thin_pool', return_value=False), \
                mock.patch.object(storage.subprocess, 'run') as run:
            self.s._reclaim_deleted_instance_lvs(old, {}, set())
        run.assert_not_called()


class LuksSectorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()

    @staticmethod
    def _geometry(logical, physical):
        return {
            'logical': logical,
            'physical': physical,
            'size': 8 * 1024 ** 3,
        }

    def test_512n_and_512e_choose_512(self):
        geometries = {
            '/dev/512n': self._geometry(512, 512),
            '/dev/512e': self._geometry(512, 4096),
        }
        self.assertEqual(self.s._choose_luks_sector_size(geometries), 512)

    def test_two_512e_disks_choose_4096(self):
        geometries = {
            '/dev/a': self._geometry(512, 4096),
            '/dev/b': self._geometry(512, 4096),
        }
        self.assertEqual(self.s._choose_luks_sector_size(geometries), 4096)

    def test_512e_and_4kn_choose_4096(self):
        geometries = {
            '/dev/512e': self._geometry(512, 4096),
            '/dev/4kn': self._geometry(4096, 4096),
        }
        self.assertEqual(self.s._choose_luks_sector_size(geometries), 4096)

    def test_512n_and_4kn_are_rejected(self):
        geometries = {
            '/dev/512n': self._geometry(512, 512),
            '/dev/4kn': self._geometry(4096, 4096),
        }
        with self.assertRaisesRegex(RuntimeError,
                                    'No safe common LUKS sector size'):
            self.s._choose_luks_sector_size(geometries)


class LuksProvisionTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()
        self.targets = [('/dev/a', 'reefy-a'), ('/dev/b', 'reefy-b')]

    @staticmethod
    def _result(returncode=0, stdout='', stderr=''):
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)

    def test_preflight_failure_happens_before_key_rotation_or_wipe(self):
        def exists(path):
            return path == '/dev/a'

        with mock.patch.object(storage.os.path, 'exists', side_effect=exists), \
                mock.patch.object(self.s, '_write_fresh_keyfile') as write_key, \
                mock.patch.object(self.s, '_force_wipe_device') as wipe:
            with self.assertRaisesRegex(RuntimeError,
                                        'storage device.*not found'):
                self.s._provision_luks_stack(self.targets, '/dev/key')

        write_key.assert_not_called()
        wipe.assert_not_called()

    def test_batch_uses_one_explicit_shared_sector_size(self):
        with mock.patch.object(
                self.s, '_preflight_luks_targets', return_value=512), \
                mock.patch.object(self.s, '_write_fresh_keyfile'), \
                mock.patch.object(self.s, '_force_wipe_device'), \
                mock.patch.object(
                    self.s, '_read_blockdev_int', return_value=512), \
                mock.patch.object(
                    storage.subprocess, 'run', return_value=self._result()) as run:
            pvs = self.s._provision_luks_stack(self.targets, '/dev/key')

        self.assertEqual(pvs,
                         ['/dev/mapper/reefy-a', '/dev/mapper/reefy-b'])
        format_commands = [
            call.args[0] for call in run.call_args_list
            if call.args[0][:2] == ['cryptsetup', 'luksFormat']
        ]
        self.assertEqual(len(format_commands), 2)
        for command in format_commands:
            self.assertEqual(command.count('--sector-size'), 1)
            flag = command.index('--sector-size')
            self.assertEqual(command[flag + 1], '512')

    def test_format_failure_is_fatal(self):
        def fake_run(command, **kwargs):
            if command[:2] == ['cryptsetup', 'luksFormat']:
                return self._result(returncode=2, stderr='format failed')
            return self._result()

        with mock.patch.object(
                self.s, '_preflight_luks_targets', return_value=512), \
                mock.patch.object(self.s, '_write_fresh_keyfile'), \
                mock.patch.object(self.s, '_force_wipe_device'), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run) as run:
            with self.assertRaisesRegex(RuntimeError,
                                        'LUKS format failed.*format failed'):
                self.s._provision_luks_stack(self.targets, '/dev/key')

        commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any(command[:2] == ['cryptsetup', 'luksOpen']
                             for command in commands))
        self.assertEqual(
            sum(command[:2] == ['cryptsetup', 'luksFormat']
                for command in commands), 1)

    def test_second_format_failure_closes_first_mapper(self):
        format_count = 0

        def fake_run(command, **kwargs):
            nonlocal format_count
            if command[:2] == ['cryptsetup', 'luksFormat']:
                format_count += 1
                if format_count == 2:
                    return self._result(returncode=2,
                                        stderr='second format failed')
            return self._result()

        with mock.patch.object(
                self.s, '_preflight_luks_targets', return_value=512), \
                mock.patch.object(self.s, '_write_fresh_keyfile'), \
                mock.patch.object(self.s, '_force_wipe_device'), \
                mock.patch.object(
                    self.s, '_read_blockdev_int', return_value=512), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run) as run:
            with self.assertRaisesRegex(
                    RuntimeError, 'second format failed'):
                self.s._provision_luks_stack(self.targets, '/dev/key')

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(format_count, 2)
        self.assertIn(['cryptsetup', 'luksClose', 'reefy-a'], commands)

    def test_open_failure_is_fatal(self):
        def fake_run(command, **kwargs):
            if command[:2] == ['cryptsetup', 'luksOpen']:
                return self._result(returncode=2, stderr='open failed')
            return self._result()

        with mock.patch.object(
                self.s, '_preflight_luks_targets', return_value=512), \
                mock.patch.object(self.s, '_write_fresh_keyfile'), \
                mock.patch.object(self.s, '_force_wipe_device'), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run) as run:
            with self.assertRaisesRegex(RuntimeError,
                                        'LUKS open failed.*open failed'):
                self.s._provision_luks_stack(self.targets, '/dev/key')

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            sum(command[:2] == ['cryptsetup', 'luksFormat']
                for command in commands), 1)
        self.assertEqual(
            sum(command[:2] == ['cryptsetup', 'luksOpen']
                for command in commands), 1)

    def test_mapper_sector_mismatch_is_fatal_and_closes_mapper(self):
        with mock.patch.object(
                self.s, '_preflight_luks_targets', return_value=512), \
                mock.patch.object(self.s, '_write_fresh_keyfile'), \
                mock.patch.object(self.s, '_force_wipe_device'), \
                mock.patch.object(
                    self.s, '_read_blockdev_int', return_value=4096), \
                mock.patch.object(
                    storage.subprocess, 'run', return_value=self._result()) as run:
            with self.assertRaisesRegex(RuntimeError,
                                        'has logical sector size 4096'):
                self.s._provision_luks_stack(
                    [self.targets[0]], '/dev/key')

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['cryptsetup', 'luksClose', 'reefy-a'], commands)

    def test_mixed_mapper_sizes_stop_before_lvm_commands(self):
        sector_sizes = {
            '/dev/mapper/reefy-a': 512,
            '/dev/mapper/reefy-b': 4096,
        }
        with mock.patch.object(
                self.s, '_read_blockdev_int',
                side_effect=lambda path, option: sector_sizes[path]), \
                mock.patch.object(storage.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError,
                                        'sector sizes do not match'):
                self.s._ensure_lvm_stack(list(sector_sizes))

        run.assert_not_called()

    def test_pv_inspection_failure_stops_before_pvcreate(self):
        result = self._result(returncode=5, stderr='locking failed')
        with mock.patch.object(
                self.s, '_require_common_mapper_sector_size',
                return_value=512), \
                mock.patch.object(
                    storage.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, 'Cannot inspect PV'):
                self.s._ensure_lvm_stack(['/dev/mapper/reefy-a'])

        commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any(command[0] == 'pvcreate'
                             for command in commands))

    def test_existing_vg_extends_with_complete_new_pv_set_once(self):
        pvs = ['/dev/mapper/reefy-a', '/dev/mapper/reefy-b']

        with mock.patch.object(
                self.s, '_require_common_mapper_sector_size',
                return_value=512), \
                mock.patch.object(
                    self.s, '_pv_vg_name', return_value=None), \
                mock.patch.object(
                    storage.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    storage.subprocess, 'run',
                    return_value=self._result()) as run:
            self.assertEqual(self.s._ensure_lvm_stack(pvs), 'existing')

        extend_commands = [
            call.args[0] for call in run.call_args_list
            if call.args[0][0] == 'vgextend'
        ]
        self.assertEqual(
            extend_commands,
            [['vgextend', self.s.STORAGE_VG, *pvs]],
        )


class InternalStorageFailFastTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()
        self.config = {'devices': ['sda', 'sdb']}

    @staticmethod
    def _result(returncode=0, stdout='', stderr=''):
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)

    def test_missing_selected_disk_is_fatal_before_luks_or_lvm(self):
        with mock.patch.object(
                storage.os.path, 'exists',
                side_effect=lambda path: path == '/dev/sda'), \
                mock.patch.object(storage.subprocess, 'run',
                                  return_value=self._result()), \
                mock.patch.object(
                    self.s, '_provision_luks_stack') as provision, \
                mock.patch.object(self.s, '_ensure_lvm_stack') as ensure_lvm:
            with self.assertRaisesRegex(RuntimeError,
                                        'storage device.*not found'):
                self.s._setup_internal_persistent(
                    self.config, '/dev/key')

        provision.assert_not_called()
        ensure_lvm.assert_not_called()

    def test_missing_boot_disk_is_fatal(self):
        with mock.patch.object(
                storage.subprocess, 'run',
                return_value=self._result(stdout='tmpfs\n')), \
                mock.patch.object(
                    self.s, '_find_reefy_disk', return_value=(None, None)):
            with self.assertRaisesRegex(
                    RuntimeError, 'Cannot find Reefy boot disk'):
                self.s._ensure_persistent_storage(self.config)

    def test_partial_luks_result_never_reaches_lvm(self):
        with mock.patch.object(storage.os.path, 'exists', return_value=True), \
                mock.patch.object(storage.subprocess, 'run',
                                  return_value=self._result()), \
                mock.patch.object(
                    self.s, '_provision_luks_stack',
                    return_value=['/dev/mapper/reefy-sda']), \
                mock.patch.object(self.s, '_ensure_lvm_stack') as ensure_lvm:
            with self.assertRaisesRegex(RuntimeError, 'Prepared 1 of 2'):
                self.s._setup_internal_persistent(
                    self.config, '/dev/key')

        ensure_lvm.assert_not_called()

    def test_existing_lv_mount_failure_refuses_destructive_reprovision(self):
        def fake_run(command, **kwargs):
            if command[0] == 'findmnt':
                return self._result(stdout='tmpfs\n')
            if command[:3] == ['lsblk', '-dpno', 'NAME']:
                return self._result(stdout='')
            if command[0] == 'vgs':
                return self._result()
            return self._result()

        with mock.patch.object(
                self.s, '_find_reefy_disk',
                return_value=('/dev/sdz', '/dev/sdz1')), \
                mock.patch.object(storage.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(
                    self.s, '_active_reefy_lv_path',
                    return_value='/dev/reefy/reefy_default'), \
                mock.patch.object(
                    self.s, '_finalize_data_mount', return_value=False), \
                mock.patch.object(
                    self.s, '_setup_internal_persistent') as setup, \
                mock.patch.object(self.s, '_write_fresh_keyfile') as write_key:
            with self.assertRaisesRegex(
                    RuntimeError, 'refusing destructive reprovisioning'):
                self.s._ensure_persistent_storage(
                    self.config)

        setup.assert_not_called()
        write_key.assert_not_called()


class ExtendStorageSectorTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()

    @staticmethod
    def _result(returncode=0, stdout='', stderr=''):
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)

    def test_extension_preserves_key_and_requires_vg_sector_size(self):
        with mock.patch.object(
                self.s, '_find_reefy_key_partition',
                return_value='/dev/key'), \
                mock.patch.object(
                    self.s, '_vg_mapper_sector_size', return_value=4096), \
                mock.patch.object(
                    self.s, '_provision_luks_stack',
                    return_value=['/dev/mapper/reefy-sdb']) as provision, \
                mock.patch.object(storage.subprocess, 'run',
                                  return_value=self._result()), \
                mock.patch.object(storage, 'log'):
            self.s._extend_storage(['sdb'])

        provision.assert_called_once_with(
            [('/dev/sdb', 'reefy-sdb')],
            '/dev/key',
            _log=mock.ANY,
            sector_size=4096,
            write_keyfile=False,
        )

    def test_extension_sector_preflight_failure_stops_before_lvm(self):
        with mock.patch.object(
                self.s, '_find_reefy_key_partition',
                return_value='/dev/key'), \
                mock.patch.object(
                    self.s, '_vg_mapper_sector_size', return_value=4096), \
                mock.patch.object(
                    self.s, '_provision_luks_stack',
                    side_effect=RuntimeError(
                        'No safe common LUKS sector size required=4096')), \
                mock.patch.object(storage.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError,
                                        'required=4096'):
                self.s._extend_storage(['sdb'])

        run.assert_not_called()

    def test_new_disk_discovery_failure_is_fatal(self):
        with mock.patch.object(
                storage.subprocess, 'run',
                return_value=self._result(
                    returncode=5, stderr='locking failed')):
            with self.assertRaisesRegex(
                    RuntimeError, 'Cannot inspect VG.*storage devices'):
                self.s._find_new_storage_disks(['sda', 'sdb'])


if __name__ == '__main__':
    unittest.main()
