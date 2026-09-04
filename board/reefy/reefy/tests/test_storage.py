"""Unit tests for reefy.storage (pure logic; no device, no root).

Also asserts the module imports without paho-mqtt - the data-plane and
boot-mount processes depend on that."""

import json
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


class FreshKeyfileTests(unittest.TestCase):
    def test_generated_key_is_stdin_not_process_argv(self):
        secret = 'synthetic-luks-key-material'
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ['openssl', 'rand', '-base64']:
                return types.SimpleNamespace(
                    returncode=0, stdout=secret + '\n', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(storage.subprocess, 'run', side_effect=fake_run):
            storage.Storage()._write_fresh_keyfile('/dev/synthetic-key')

        command, kwargs = calls[-1]
        self.assertEqual(
            command, ['dd', 'of=/dev/synthetic-key', 'conv=notrunc'])
        self.assertNotIn(secret, ' '.join(command))
        self.assertEqual(kwargs['input'], secret.encode())


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


class OwnedXfsRepairTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()
        self.path = '/mnt/reefy-data/apps/synthetic/config'
        self.lv_name = self.s._volume_lv_name(self.path)
        self.lv_path = f'/dev/{self.s.STORAGE_VG}/{self.lv_name}'

    @staticmethod
    def _result(returncode=0, stdout='', stderr=''):
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)

    def _repair_patches(self, run):
        return (
            mock.patch.object(storage.os.path, 'exists', return_value=True),
            mock.patch.object(storage.shutil, 'which', return_value='/sbin/xfs_repair'),
            mock.patch.object(storage.os, 'makedirs'),
            mock.patch.object(storage.os, 'open', return_value=17),
            mock.patch.object(storage.os, 'close'),
            mock.patch.object(storage.subprocess, 'run', side_effect=run),
            mock.patch.object(storage, 'log'),
        )

    def test_dirty_log_uses_guarded_log_reset(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[0] == 'findmnt':
                return self._result(returncode=1)
            if command[0] == 'blkid':
                return self._result(stdout='xfs\n')
            if command == ['xfs_repair', self.lv_path]:
                return self._result(returncode=2)
            return self._result()

        patches = self._repair_patches(fake_run)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6]:
            self.assertTrue(self.s._repair_owned_xfs_volume(
                self.path, self.lv_name, self.lv_path))

        self.assertEqual(commands[-2:], [
            ['xfs_repair', self.lv_path],
            ['xfs_repair', '-L', self.lv_path],
        ])

    def test_non_log_repair_failure_never_forces_log_reset(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[0] == 'findmnt':
                return self._result(returncode=1)
            if command[0] == 'blkid':
                return self._result(stdout='xfs\n')
            return self._result(returncode=1)

        patches = self._repair_patches(fake_run)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6]:
            self.assertFalse(self.s._repair_owned_xfs_volume(
                self.path, self.lv_name, self.lv_path))

        self.assertIn(['xfs_repair', self.lv_path], commands)
        self.assertNotIn(['xfs_repair', '-L', self.lv_path], commands)

    def test_never_repairs_non_xfs_or_mounted_volume(self):
        cases = [
            [self._result(returncode=0, stdout=self.lv_path + '\n')],
            [self._result(returncode=1), self._result(stdout='ext4\n')],
        ]
        for responses in cases:
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                return responses.pop(0)

            patches = self._repair_patches(fake_run)
            with self.subTest(case=len(cases) - len(responses)), \
                    patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                self.assertFalse(self.s._repair_owned_xfs_volume(
                    self.path, self.lv_name, self.lv_path))
            self.assertFalse(any(command[0] == 'xfs_repair'
                                 for command in commands))

    def test_once_per_boot_marker_blocks_repeat_attempt(self):
        def fake_run(command, **kwargs):
            if command[0] == 'findmnt':
                return self._result(returncode=1)
            if command[0] == 'blkid':
                return self._result(stdout='xfs\n')
            raise AssertionError(f'unexpected command: {command}')

        patches = self._repair_patches(fake_run)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(
                    storage.os, 'open', side_effect=FileExistsError), \
                patches[4], patches[5], patches[6]:
            self.assertFalse(self.s._repair_owned_xfs_volume(
                self.path, self.lv_name, self.lv_path))


class VolumeCapsTests(unittest.TestCase):
    def test_block_device_aliases_compare_by_kernel_device_identity(self):
        s = storage.Storage()
        mapper = '/dev/mapper/reefy-synthetic'
        vg_alias = '/dev/reefy/synthetic'
        same_device = storage.os.makedev(253, 10)
        records = {
            mapper: types.SimpleNamespace(
                st_mode=storage.stat.S_IFBLK, st_rdev=same_device),
            vg_alias: types.SimpleNamespace(
                st_mode=storage.stat.S_IFBLK, st_rdev=same_device),
        }
        with mock.patch.object(
                storage.os, 'stat', side_effect=lambda path: records[path]):
            self.assertTrue(s._same_block_device(mapper, vg_alias))

    def test_different_block_devices_never_compare_equal(self):
        records = {
            '/dev/synthetic-a': types.SimpleNamespace(
                st_mode=storage.stat.S_IFBLK,
                st_rdev=storage.os.makedev(253, 10)),
            '/dev/synthetic-b': types.SimpleNamespace(
                st_mode=storage.stat.S_IFBLK,
                st_rdev=storage.os.makedev(253, 11)),
        }
        with mock.patch.object(
                storage.os, 'stat', side_effect=lambda path: records[path]):
            self.assertFalse(storage.Storage._same_block_device(
                '/dev/synthetic-a', '/dev/synthetic-b'))

    def test_unexpected_mount_error_is_actionable_and_path_free(self):
        path = '/mnt/reefy-data/apps/synthetic-instance/media'
        error = storage.Storage._unexpected_mount_error(path)
        message = str(error)
        self.assertIn('synthetic-instance/media', message)
        self.assertIn('Setup stopped to protect existing data', message)
        self.assertIn('Do not format or delete storage', message)
        self.assertIn('Inspect the volume mapping, then Resync', message)
        self.assertNotIn('/mnt/reefy-data', message)

    def test_set_volume_caps(self):
        s = storage.Storage()
        s.set_volume_caps({'/mnt/reefy-data/apps/x/media': 90})
        self.assertEqual(s._volume_caps['/mnt/reefy-data/apps/x/media'], 90)

    def test_constructor_aliases_dict(self):
        caps = {}
        s = storage.Storage(caps)
        self.assertIs(s._volume_caps, caps)

    def test_capped_size_aligns_to_512_byte_mapper(self):
        size = storage.Storage._capped_virtual_size(10_003, 90, 512)
        self.assertEqual(size, 8_704)

    def test_capped_size_aligns_to_4096_byte_mapper(self):
        size = storage.Storage._capped_virtual_size(10_003, 90, 4096)
        self.assertEqual(size, 8_192)

    def test_capped_size_uses_exact_decimal_math(self):
        size = storage.Storage._capped_virtual_size(81_920, '12.5', 4096)
        self.assertEqual(size, 8_192)

    def test_capped_size_keeps_exactly_aligned_value(self):
        size = storage.Storage._capped_virtual_size(40_960, 80, 4096)
        self.assertEqual(size, 32_768)

    def test_capped_size_also_aligns_to_lvm_extent(self):
        extent = 4 * 1024 * 1024
        size = storage.Storage._capped_virtual_size(
            100_000_000, 90, 4096, extent)
        self.assertEqual(size % extent, 0)
        self.assertLessEqual(size, 90_000_000)
        self.assertLess(90_000_000 - size, extent)

    def test_capped_size_rejects_unsafe_inputs(self):
        cases = [
            (40_960, 0, 512),
            (40_960, -1, 512),
            (40_960, 101, 512),
            (40_960, 'not-a-number', 512),
            (40_960, 'NaN', 512),
            (100, 1, 4096),
            (40_960, 90, 512, 0),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                storage.Storage._capped_virtual_size(*args)

    def test_lvcreate_receives_mapper_aligned_virtual_size(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/media'
        s = storage.Storage({path: 90})
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvs':
                return types.SimpleNamespace(
                    returncode=0, stdout='10003\n', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=False), \
                mock.patch.object(
                    s, '_lv_virtual_size', side_effect=[10_003, 8_192]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=4096), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=4096), \
                mock.patch.object(s, '_fs_mount_opts', return_value='opts'), \
                mock.patch.object(
                    s, '_remember_owned_volume', return_value=True), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False), \
                mock.patch.object(storage.os, 'makedirs'):
            self.assertTrue(s._ensure_volume_lv(path))

        lvcreate = next(cmd for cmd in commands if cmd[0] == 'lvcreate')
        self.assertEqual(lvcreate[lvcreate.index('--virtualsize') + 1], '8192B')

    def test_existing_mount_failure_repairs_then_retries(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/config'
        s = storage.Storage()
        mount_calls = []

        def fake_run(command, **kwargs):
            if command[0] == 'findmnt':
                return types.SimpleNamespace(
                    returncode=1, stdout='', stderr='')
            if command[0] == 'mount':
                mount_calls.append(command)
                return types.SimpleNamespace(
                    returncode=32 if len(mount_calls) == 1 else 0,
                    stdout='', stderr='Structure needs cleaning')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=True), \
                mock.patch.object(s, '_fs_mount_opts', return_value='opts'), \
                mock.patch.object(
                    s, '_repair_owned_xfs_volume', return_value=True) as repair, \
                mock.patch.object(
                    s, '_remember_owned_volume', return_value=True), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os, 'makedirs'), \
                mock.patch.object(storage, 'log'):
            self.assertTrue(s._ensure_volume_lv(
                path, allow_create=False, expect_existing=True))

        self.assertEqual(len(mount_calls), 2)
        repair.assert_called_once_with(
            path, s._volume_lv_name(path),
            f'/dev/{s.STORAGE_VG}/{s._volume_lv_name(path)}')

    def test_cap_failure_falls_back_to_default_directory_and_warns(self):
        path = '/mnt/reefy-data/apps/synthetic/media'
        s = storage.Storage({path: 90})
        volumes = [{'path': path, 'uid': 1000}]
        with mock.patch.object(s, '_ensure_volume_lv', return_value=False), \
                mock.patch.object(
                    s, '_lv_metadata_names', return_value=set()), \
                mock.patch.object(storage.os.path, 'exists', return_value=False), \
                mock.patch.object(storage.os, 'makedirs') as makedirs, \
                mock.patch.object(storage.os, 'stat', side_effect=OSError), \
                mock.patch.object(storage.subprocess, 'run'):
            warnings = s._prepare_app_dirs(volumes)

        self.assertEqual(warnings, [s._cap_warning_for_path(path)])
        makedirs.assert_called_once_with(path, mode=0o755, exist_ok=True)

    def test_lvcreate_failure_returns_fallback_signal(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/media'
        s = storage.Storage({path: 90})

        def fake_run(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvs':
                return types.SimpleNamespace(
                    returncode=0, stdout='1048576\n', stderr='')
            if cmd[0] == 'lvcreate':
                return types.SimpleNamespace(
                    returncode=5, stdout='', stderr='synthetic failure')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=False), \
                mock.patch.object(
                    s, '_lv_virtual_size', return_value=1_048_576), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(
                    s, '_remove_new_volume_lv', return_value=True) as cleanup, \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False):
            self.assertFalse(s._ensure_volume_lv(path))
        cleanup.assert_called_once_with(
            f'{s.STORAGE_VG}/{s._volume_lv_name(path)}')

    def test_existing_oversized_lv_is_preserved_but_cap_warns(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 80})
        lv_name = s._volume_lv_name(path)

        def fake_run(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvs':
                size = ('40960\n' if cmd[-1].endswith('/reefy_pool')
                        else '36864\n')
                return types.SimpleNamespace(
                    returncode=0, stdout=size, stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=True), \
                mock.patch.object(
                    s, '_lv_virtual_size', side_effect=[40_960, 36_864]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=4096), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=4096), \
                mock.patch.object(s, '_fs_mount_opts', return_value='opts'), \
                mock.patch.object(
                    s, '_remember_owned_volume', return_value=True), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=True), \
                mock.patch.object(storage.os, 'makedirs'):
            self.assertFalse(s._ensure_volume_lv(path))

        self.assertEqual(lv_name, s._volume_lv_name(path))

    def test_mkfs_failure_removes_only_new_lv_for_clean_retry(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        commands = []
        pool_size = 1_048_576
        desired_size = s._capped_virtual_size(pool_size, 90, 512, 512)

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvs':
                return types.SimpleNamespace(
                    returncode=0, stdout='1048576\n', stderr='')
            if cmd[0] == 'mkfs.xfs':
                return types.SimpleNamespace(
                    returncode=1, stdout='', stderr='synthetic mkfs failure')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists',
                    side_effect=[False, True, False]), \
                mock.patch.object(
                    s, '_lv_virtual_size',
                    side_effect=[pool_size, desired_size]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False):
            self.assertFalse(s._ensure_volume_lv(path))

        lv_ref = f'{s.STORAGE_VG}/{s._volume_lv_name(path)}'
        self.assertIn(['lvremove', '-f', lv_ref], commands)

    def test_mkfs_timeout_removes_new_lv_before_failing_open(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        commands = []
        pool_size = 1_048_576
        desired_size = s._capped_virtual_size(pool_size, 90, 512, 512)

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvs':
                return types.SimpleNamespace(
                    returncode=0, stdout='1048576\n', stderr='')
            if cmd[0] == 'mkfs.xfs':
                raise storage.subprocess.TimeoutExpired(cmd, 60)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists',
                    side_effect=[False, True, False]), \
                mock.patch.object(
                    s, '_lv_virtual_size',
                    side_effect=[pool_size, desired_size]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False), \
                mock.patch.object(storage.os, 'makedirs'), \
                mock.patch.object(
                    s, '_lv_metadata_names', return_value=set()), \
                mock.patch.object(
                    storage.os, 'stat',
                    return_value=types.SimpleNamespace(st_uid=0, st_gid=0)):
            warnings = s._prepare_app_dirs([{'path': path}])

        self.assertEqual(warnings, [s._cap_warning_for_path(path)])
        lv_ref = f'{s.STORAGE_VG}/{s._volume_lv_name(path)}'
        self.assertIn(['lvremove', '-f', lv_ref], commands)

    def test_lvcreate_timeout_attempts_cleanup_before_failing_open(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvs':
                return types.SimpleNamespace(
                    returncode=0, stdout='1048576\n', stderr='')
            if cmd[0] == 'lvcreate':
                raise storage.subprocess.TimeoutExpired(cmd, 15)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists',
                    side_effect=[False, True, False]), \
                mock.patch.object(
                    s, '_lv_virtual_size', return_value=1_048_576), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False), \
                mock.patch.object(storage.os, 'makedirs'), \
                mock.patch.object(
                    s, '_lv_metadata_names', return_value=set()), \
                mock.patch.object(
                    storage.os, 'stat',
                    return_value=types.SimpleNamespace(st_uid=0, st_gid=0)):
            warnings = s._prepare_app_dirs([{'path': path}])

        self.assertEqual(warnings, [s._cap_warning_for_path(path)])
        lv_ref = f'{s.STORAGE_VG}/{s._volume_lv_name(path)}'
        self.assertIn(['lvremove', '-f', lv_ref], commands)

    def test_metadata_lv_without_device_node_is_never_recreated_or_removed(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        desired_size = s._capped_virtual_size(1_048_576, 90, 512, 512)
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=True), \
                mock.patch.object(
                    s, '_lv_virtual_size',
                    side_effect=[1_048_576, desired_size]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(s, '_fs_mount_opts', return_value='opts'), \
                mock.patch.object(
                    s, '_remember_owned_volume', return_value=True), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False), \
                mock.patch.object(storage.os, 'makedirs'):
            s._ensure_volume_lv(path)

        self.assertFalse(any(cmd[0] in ('lvcreate', 'mkfs.xfs', 'lvremove')
                             for cmd in commands))

    def test_post_create_size_above_cap_is_removed_before_format(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 80})
        desired_size = s._capped_virtual_size(40_960, 80, 4096, 4096)
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=False), \
                mock.patch.object(
                    s, '_lv_virtual_size',
                    side_effect=[40_960, desired_size + 4096]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=4096), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=4096), \
                mock.patch.object(
                    s, '_remove_new_volume_lv', return_value=True) as cleanup, \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os.path, 'exists', return_value=False):
            self.assertFalse(s._ensure_volume_lv(path))

        cleanup.assert_called_once_with(
            f'{s.STORAGE_VG}/{s._volume_lv_name(path)}')
        self.assertFalse(any(cmd[0] == 'mkfs.xfs' for cmd in commands))

    def test_cleanup_does_not_treat_lvm_inspection_error_as_absent(self):
        s = storage.Storage()
        with mock.patch.object(
                s, '_lv_metadata_exists', return_value=None), \
                mock.patch.object(storage.subprocess, 'run') as run:
            self.assertFalse(
                s._remove_new_volume_lv('reefy/reefy_backup_synthetic'))
        run.assert_not_called()

    def test_cleanup_uses_authoritative_absence_after_command_error(self):
        s = storage.Storage()
        failures = [
            types.SimpleNamespace(returncode=5, stdout='', stderr='synthetic'),
            storage.subprocess.TimeoutExpired(['lvremove'], 15),
        ]
        for outcome in failures:
            behavior = ({'return_value': outcome}
                        if not isinstance(outcome, BaseException)
                        else {'side_effect': outcome})
            with self.subTest(kind=type(outcome).__name__), \
                    mock.patch.object(
                        s, '_lv_metadata_exists',
                        side_effect=[True, False]), \
                    mock.patch.object(
                        storage.subprocess, 'run', **behavior):
                self.assertTrue(
                    s._remove_new_volume_lv('reefy/reefy_backup_synthetic'))

    def test_metadata_inspection_process_errors_are_indeterminate(self):
        s = storage.Storage()
        failures = [
            storage.subprocess.TimeoutExpired(['lvs'], 10),
            OSError('synthetic lvs failure'),
        ]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__), \
                    mock.patch.object(
                        storage.subprocess, 'run', side_effect=failure):
                self.assertIsNone(
                    s._lv_metadata_exists('reefy_backup_synthetic'))

    def test_cap_geometry_timeout_becomes_nonfatal_cap_warning_signal(self):
        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        with mock.patch.object(
                s, '_lv_virtual_size', return_value=1_048_576), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size',
                    side_effect=storage.subprocess.TimeoutExpired(
                        ['pvs'], 10)):
            self.assertIsNone(s._desired_volume_size(path))

    def test_cleanup_failure_never_falls_back_to_default_storage(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})

        def fake_run(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            if cmd[0] == 'lvcreate':
                return types.SimpleNamespace(
                    returncode=5, stdout='', stderr='synthetic failure')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=False), \
                mock.patch.object(
                    s, '_lv_virtual_size', return_value=1_048_576), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(
                    s, '_remove_new_volume_lv', return_value=False), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run):
            with self.assertRaises(
                    storage.ExistingVolumeUnavailableError):
                s._ensure_volume_lv(path)

    def test_expected_volume_requires_accessible_thin_pool(self):
        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage()
        with mock.patch.object(s, '_has_thin_pool', return_value=False):
            with self.assertRaises(
                    storage.ExistingVolumeUnavailableError):
                s._ensure_volume_lv(path, allow_create=False,
                                    expect_existing=True)

    def test_findmnt_error_never_creates_or_mounts_volume(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return types.SimpleNamespace(returncode=2, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run):
            self.assertFalse(s._ensure_volume_lv(path))

        self.assertFalse(any(command[0] in ('lvcreate', 'mount')
                             for command in commands))

    def test_new_volume_tag_failure_is_cleaned_before_fallback(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})

        def fake_run(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                return types.SimpleNamespace(returncode=1, stdout='', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_lv_metadata_exists', return_value=False), \
                mock.patch.object(
                    s, '_lv_virtual_size', side_effect=[1_048_576, 943_616]), \
                mock.patch.object(
                    s, '_vg_mapper_sector_size', return_value=512), \
                mock.patch.object(
                    s, '_vg_extent_size', return_value=512), \
                mock.patch.object(s, '_fs_mount_opts', return_value='opts'), \
                mock.patch.object(
                    s, '_remember_owned_volume', return_value=False), \
                mock.patch.object(
                    s, '_remove_mounted_new_volume') as cleanup, \
                mock.patch.object(
                    storage.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(storage.os.path, 'isdir', return_value=False), \
                mock.patch.object(storage.os, 'makedirs'):
            self.assertFalse(s._ensure_volume_lv(path))

        cleanup.assert_called_once_with(path, s._volume_lv_name(path))

    def test_existing_volume_tag_failure_preserves_mounted_volume(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage()
        lv_path = f'/dev/{s.STORAGE_VG}/{s._volume_lv_name(path)}'
        mounted = types.SimpleNamespace(
            returncode=0,
            stdout='/dev/mapper/reefy-reefy_backup_direct\n', stderr='')
        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_same_block_device', return_value=True) as same_device, \
                mock.patch.object(
                    storage.subprocess, 'run', return_value=mounted), \
                mock.patch.object(
                    s, '_remember_owned_volume', return_value=False):
            self.assertTrue(s._ensure_volume_lv(
                path, allow_create=False, expect_existing=True))
        same_device.assert_called_once_with(mounted.stdout.strip(), lv_path)

    def test_wrong_existing_volume_reports_app_and_operator_action(self):
        import contextlib

        path = '/mnt/reefy-data/apps/synthetic-instance/cache'
        s = storage.Storage()
        mounted = types.SimpleNamespace(
            returncode=0, stdout='/dev/synthetic-wrong\n', stderr='')
        with mock.patch.object(s, '_has_thin_pool', return_value=True), \
                mock.patch.object(
                    s, '_volume_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    s, '_same_block_device', return_value=False), \
                mock.patch.object(
                    storage.subprocess, 'run', return_value=mounted):
            with self.assertRaisesRegex(
                    storage.ExistingVolumeUnavailableError,
                    'synthetic-instance/cache.*Do not format.*Resync'):
                s._ensure_volume_lv(
                    path, allow_create=False, expect_existing=True)

    def test_uncapped_volume_exception_keeps_existing_failure_behavior(self):
        path = '/mnt/reefy-data/apps/synthetic/config'
        s = storage.Storage()
        with mock.patch.object(
                s, '_ensure_volume_lv', side_effect=RuntimeError('synthetic')), \
                mock.patch.object(
                    s, '_lv_metadata_names', return_value=set()):
            with self.assertRaises(RuntimeError):
                s._prepare_app_dirs([{'path': path}], backup_paths={path})

    def test_backup_volume_false_result_is_fatal_with_thin_pool(self):
        paths = [
            ('/mnt/reefy-data/apps/synthetic/config', {}),
            ('/mnt/reefy-data/apps/synthetic/data', {
                '/mnt/reefy-data/apps/synthetic/data': 90,
            }),
        ]
        for path, caps in paths:
            s = storage.Storage(caps)
            with self.subTest(capped=bool(caps)), \
                    mock.patch.object(
                        s, '_ensure_volume_lv', return_value=False), \
                    mock.patch.object(
                        s, '_lv_metadata_names',
                        return_value={s.STORAGE_POOL}), \
                    mock.patch.object(
                        s, '_dedicated_volume_mount_status',
                        return_value=False):
                with self.assertRaises(RuntimeError):
                    s._prepare_app_dirs(
                        [{'path': path}], backup_paths={path})

    def test_backup_volume_false_result_keeps_legacy_fallback(self):
        path = '/mnt/reefy-data/apps/synthetic/config'
        s = storage.Storage()
        with mock.patch.object(
                s, '_ensure_volume_lv', return_value=False), \
                mock.patch.object(
                    s, '_lv_metadata_names',
                    return_value={s.STORAGE_LV}), \
                mock.patch.object(
                    storage.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    storage.os, 'stat',
                    return_value=types.SimpleNamespace(st_uid=0, st_gid=0)):
            self.assertEqual(
                s._prepare_app_dirs(
                    [{'path': path}], backup_paths={path}),
                [])

    def test_capped_backup_keeps_mounted_lv_and_reports_only_cap_warning(self):
        path = '/mnt/reefy-data/apps/synthetic/data'
        s = storage.Storage({path: 90})
        with mock.patch.object(
                s, '_ensure_volume_lv', return_value=False), \
                mock.patch.object(
                    s, '_dedicated_volume_mount_status', return_value=True), \
                mock.patch.object(
                    s, '_lv_metadata_names',
                    return_value={s.STORAGE_POOL,
                                  s._volume_lv_name(path)}), \
                mock.patch.object(
                    storage.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    storage.os, 'stat',
                    return_value=types.SimpleNamespace(st_uid=0, st_gid=0)):
            warnings = s._prepare_app_dirs(
                [{'path': path}], backup_paths={path})

        self.assertEqual(warnings, [s._cap_warning_for_path(path)])


class SensitiveDownloadLoggingTests(unittest.TestCase):
    def test_download_failures_do_not_log_signed_url_or_exception_argv(self):
        path = '/mnt/reefy-data/apps/synthetic/files'
        secret = 'synthetic-download-signature'
        url = f'https://download.invalid/file?signature={secret}'
        volume = {
            'path': path,
            'uid': 1000,
            'files': [{'name': 'asset.bin', 'url': url}],
        }
        failures = [
            storage.subprocess.CalledProcessError(
                22, ['curl', '-fSL', url]),
            storage.subprocess.TimeoutExpired(
                ['curl', '-fSL', url], 600),
        ]

        for failure in failures:
            messages = []
            with self.subTest(kind=type(failure).__name__), \
                    mock.patch.object(
                        storage.os.path, 'exists',
                        side_effect=lambda candidate: candidate == path), \
                    mock.patch.object(storage.os, 'makedirs'), \
                    mock.patch.object(
                        storage.os, 'stat',
                        return_value=types.SimpleNamespace(
                            st_uid=1000, st_gid=1000)), \
                    mock.patch.object(
                        storage.subprocess, 'run', side_effect=failure), \
                    mock.patch.object(
                        storage, 'log',
                        side_effect=lambda source, message: messages.append(message)), \
                    mock.patch.object(
                        storage.Storage, '_lv_metadata_names',
                        return_value=set()):
                warnings = storage.Storage()._prepare_app_dirs([volume])

            rendered = '\n'.join(messages)
            self.assertEqual(warnings, [])
            self.assertNotIn(secret, rendered)
            self.assertIn('Download', rendered)

    def test_programming_error_on_capped_volume_is_not_hidden(self):
        path = '/mnt/reefy-data/apps/synthetic/cache'
        s = storage.Storage({path: 90})
        with mock.patch.object(
                s, '_ensure_volume_lv', side_effect=AssertionError('bug')), \
                mock.patch.object(
                    s, '_lv_metadata_names', return_value=set()):
            with self.assertRaises(AssertionError):
                s._prepare_app_dirs([{'path': path}])

    def test_capped_backup_exception_keeps_backup_failure_behavior(self):
        path = '/mnt/reefy-data/apps/synthetic/data'
        s = storage.Storage({path: 90})
        with mock.patch.object(
                s, '_ensure_volume_lv', side_effect=RuntimeError('synthetic')), \
                mock.patch.object(
                    s, '_lv_metadata_names', return_value=set()):
            with self.assertRaises(RuntimeError):
                s._prepare_app_dirs(
                    [{'path': path}], backup_paths={path})


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
    """Only tagged Reefy LVs whose complete owner disappeared are removed."""

    def setUp(self):
        self.s = storage.Storage()
        self.cfg = '/mnt/reefy-data/apps/i1/config'
        self.dat = '/mnt/reefy-data/apps/i1/data'

    def _reclaim(self, old_state, new_state, new_backup_paths,
                 volume_paths=None, pretagged=None, tag_fail=None,
                 findmnt_rc=1, umount_rc=0, metadata_available=True):
        import contextlib
        removed = []
        commands = []
        volume_paths = set(volume_paths or set())
        pretagged = set(pretagged or set())
        tag_fail = set(tag_fail or set())

        def collect_paths(obj, found):
            if isinstance(obj, str) and '/apps/' in obj:
                found.add(obj)
            elif isinstance(obj, dict):
                for key, value in obj.items():
                    collect_paths(key, found)
                    collect_paths(value, found)
            elif isinstance(obj, (list, tuple, set)):
                for value in obj:
                    collect_paths(value, found)

        known_paths = set(volume_paths)
        collect_paths(old_state, known_paths)
        metadata = {
            self.s._volume_lv_name(path): set()
            for path in known_paths
        }
        for path in pretagged:
            lv_name = self.s._volume_lv_name(path)
            metadata.setdefault(lv_name, set()).update({
                self.s.MANAGED_VOLUME_TAG,
                self.s._owner_tag_for_path(path),
            })

        def remember(path):
            if path in tag_fail:
                return False
            lv_name = self.s._volume_lv_name(path)
            if lv_name not in metadata:
                return False
            metadata[lv_name].update({
                self.s.MANAGED_VOLUME_TAG,
                self.s._owner_tag_for_path(path),
            })
            return True

        def fake_run(cmd, **kw):
            commands.append(cmd)
            if cmd[0] == 'umount':
                rc = umount_rc
            elif cmd[0] == 'findmnt':
                rc = findmnt_rc
            else:
                rc = 0
            if cmd[0] == 'lvremove':
                removed.append(cmd[-1])
                metadata.pop(cmd[-1].rsplit('/', 1)[-1], None)
            stdout = ''
            if cmd[0] == 'findmnt':
                stdout = '/mnt/reefy-data/apps/synthetic/volume\n'
            return types.SimpleNamespace(
                returncode=rc, stdout=stdout, stderr='')

        with mock.patch.object(self.s, '_has_thin_pool', return_value=True), \
                mock.patch.object(self.s, '_volume_lock',
                                  return_value=contextlib.nullcontext()), \
                mock.patch.object(
                    self.s, '_remember_owned_volume', side_effect=remember), \
                mock.patch.object(
                    self.s, '_lv_metadata_with_tags',
                    side_effect=(lambda: {
                        name: set(tags) for name, tags in metadata.items()
                    } if metadata_available else None)), \
                mock.patch.object(
                    self.s, '_lv_metadata_exists',
                    side_effect=lambda name: name in metadata), \
                mock.patch.object(storage.subprocess, 'run', side_effect=fake_run):
            self.s._reclaim_deleted_instance_lvs(
                old_state, new_state, new_backup_paths)
        self._metadata_after = {
            name: set(tags) for name, tags in metadata.items()
        }
        self._reclaim_commands = commands
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

    def test_reclaims_deleted_instance_cap_only_volume(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        old = {
            'volume_caps': {cache: 90},
            'instances': [{'uuid': 'i1'}],
            'app_volumes': [{'path': cache}],
        }
        new = {
            'volume_caps': {},
            'instances': [],
            'app_volumes': [],
        }
        removed = self._reclaim(old, new, set())
        self.assertEqual(removed, [self._lv(cache)])

    def test_keeps_removed_cap_while_instance_uuid_is_still_live(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        old = {'volume_caps': {cache: 90}}
        new = {
            'volume_caps': {},
            'instances': [{'instance_uuid': 'i1'}],
            'app_volumes': [],
        }
        removed = self._reclaim(old, new, set())
        self.assertEqual(removed, [])

    def test_failed_unmount_prevents_lv_removal(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        old = {'volume_caps': {cache: 90}}
        new = {'volume_caps': {}, 'instances': [], 'app_volumes': []}
        removed = self._reclaim(
            old, new, set(), findmnt_rc=0, umount_rc=1)
        self.assertTrue(any(cmd[0] == 'umount'
                            for cmd in self._reclaim_commands))
        self.assertEqual(removed, [])
        self.assertIn(self.s._volume_lv_name(cache), self._metadata_after)

    def test_tagged_volume_is_retried_after_old_state_is_gone(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        current = {'volume_caps': {}, 'instances': [], 'app_volumes': []}
        removed = self._reclaim(
            current, current, set(), volume_paths={cache},
            pretagged={cache})
        self.assertEqual(removed, [self._lv(cache)])

    def test_untagged_legacy_volume_is_never_removed(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        removed = self._reclaim(
            {}, {}, set(), volume_paths={cache})
        self.assertEqual(removed, [])
        self.assertIn(self.s._volume_lv_name(cache), self._metadata_after)

    def test_failed_legacy_adoption_preserves_untagged_volume(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        old = {'volume_caps': {cache: 90}}
        removed = self._reclaim(
            old, {}, set(), tag_fail={cache})
        self.assertEqual(removed, [])
        self.assertIn(self.s._volume_lv_name(cache), self._metadata_after)

    def test_findmnt_error_never_removes_volume(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        old = {'volume_caps': {cache: 90}}
        new = {'volume_caps': {}, 'instances': [], 'app_volumes': []}
        removed = self._reclaim(old, new, set(), findmnt_rc=2)
        self.assertEqual(removed, [])
        self.assertFalse(any(cmd[0] in ('umount', 'lvremove')
                             for cmd in self._reclaim_commands))
        self.assertIn(self.s._volume_lv_name(cache), self._metadata_after)

    def test_metadata_failure_never_removes_volume(self):
        cache = '/mnt/reefy-data/apps/i1/cache'
        removed = self._reclaim(
            {'volume_caps': {cache: 90}}, {}, set(),
            metadata_available=False)
        self.assertEqual(removed, [])
        self.assertFalse(any(cmd[0] == 'lvremove'
                             for cmd in self._reclaim_commands))

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

    def test_legacy_instance_name_is_a_live_volume_owner(self):
        legacy = '/mnt/reefy-data/apps/legacy-app/cache'
        old = {'volume_caps': {legacy: 90}}
        new = {
            'volume_caps': {},
            'instances': [{'instance_name': 'legacy-app'}],
            'app_volumes': [],
        }
        removed = self._reclaim(old, new, set())
        self.assertEqual(removed, [])

    def test_legacy_name_remains_live_when_instance_also_has_uuid(self):
        legacy = '/mnt/reefy-data/apps/legacy-app/cache'
        old = {'volume_caps': {legacy: 90}}
        new = {
            'volume_caps': {},
            'instances': [{
                'instance_uuid': 'synthetic-uuid',
                'instance_name': 'legacy-app',
            }],
            'app_volumes': [],
        }
        removed = self._reclaim(old, new, set())
        self.assertEqual(removed, [])

    def test_noop_without_thin_pool(self):
        old = {'backup': {'instances': [
            {'instance_uuid': 'i1', 'paths': [self.cfg]}]}}
        with mock.patch.object(self.s, '_has_thin_pool', return_value=False), \
                mock.patch.object(storage.subprocess, 'run') as run:
            self.s._reclaim_deleted_instance_lvs(old, {}, set())
        run.assert_not_called()


class OwnedVolumeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.s = storage.Storage()

    def test_metadata_accepts_only_exact_app_volume_roots(self):
        valid = '/mnt/reefy-data/apps/synthetic/cache'
        invalid = [
            '/mnt/reefy-data/apps/synthetic',
            '/mnt/reefy-data/apps/synthetic/cache/child',
            '/mnt/reefy-data/apps/synthetic/cache/',
            '/mnt/reefy-data/apps/../state/cache',
        ]
        self.assertTrue(self.s._valid_owned_volume_path(valid))
        for path in invalid:
            with self.subTest(path=path):
                self.assertFalse(self.s._valid_owned_volume_path(path))

    def test_cap_warning_identifies_instance_and_volume_without_path(self):
        warning = self.s._cap_warning_for_path(
            '/mnt/reefy-data/apps/synthetic-instance/media')
        self.assertEqual(warning, {
            'code': 'storage.cap_not_enforced',
            'instance_uuid': 'synthetic-instance',
            'volume': 'media',
        })
        self.assertNotIn('path', warning)

    def test_remember_volume_attaches_managed_and_owner_tags(self):
        path = '/mnt/reefy-data/apps/synthetic/cache'
        result = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(
                self.s, '_volume_tags', return_value=set()), \
                mock.patch.object(
                    storage.subprocess, 'run', return_value=result) as run:
            self.assertTrue(self.s._remember_owned_volume(path))
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'lvchange')
        self.assertIn(self.s.MANAGED_VOLUME_TAG, command)
        self.assertIn(self.s._owner_tag_for_path(path), command)

    def test_boot_remounts_owned_lv_without_housekeeping(self):
        path = '/mnt/reefy-data/apps/synthetic/cache'
        lv_name = self.s._volume_lv_name(path)
        state = {
            'volume_caps': {},
            'backup': {'instances': []},
            'app_volumes': [{'path': path, 'uid': 0}],
        }
        with mock.patch.object(
                storage.os.path, 'exists', return_value=True), \
                mock.patch('builtins.open',
                           mock.mock_open(read_data=json.dumps(state))), \
                mock.patch.object(
                    self.s, '_lv_metadata_names', return_value={lv_name}), \
                mock.patch.object(
                    self.s, '_ensure_volume_lv', return_value=True) as ensure, \
                mock.patch.object(
                    self.s, '_reclaim_deleted_instance_lvs') as reclaim:
            self.s.boot_mount()

        ensure.assert_called_once_with(
            path, allow_create=False, expect_existing=True)
        reclaim.assert_not_called()

    def test_boot_inventory_failure_mounts_apps_independently(self):
        first = '/mnt/reefy-data/apps/synthetic-a/cache'
        second = '/mnt/reefy-data/apps/synthetic-b/cache'
        state = {
            'volume_caps': {first: 50, second: 50},
            'backup': {'instances': []},
            'app_volumes': [
                {'path': first, 'uid': 0},
                {'path': second, 'uid': 0},
            ],
        }
        attempts = []

        def ensure(path, **kwargs):
            attempts.append((path, kwargs))
            if path == first:
                raise storage.ExistingVolumeUnavailableError(
                    'synthetic failure')
            return True

        with mock.patch.object(
                storage.os.path, 'exists', return_value=True), \
                mock.patch('builtins.open',
                           mock.mock_open(read_data=json.dumps(state))), \
                mock.patch.object(
                    self.s, '_lv_metadata_names', return_value=None), \
                mock.patch.object(
                    self.s, '_ensure_volume_lv', side_effect=ensure):
            self.s.boot_mount()

        self.assertEqual([path for path, _ in attempts], [first, second])
        for _, kwargs in attempts:
            self.assertFalse(kwargs['allow_create'])
            self.assertFalse(kwargs['expect_existing'])


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
