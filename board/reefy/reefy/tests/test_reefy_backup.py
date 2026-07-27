"""Unit tests for the reefy-backup runner.

reefy-backup is a /usr/bin script (no .py), so load it by path. Tests the
fs-type-aware snapshot mount plus bounded remote-repository readiness and
best-effort post-archive maintenance."""

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


class BorgReadinessTests(unittest.TestCase):
    def test_dns_recovers_then_missing_repo_is_initialized(self):
        repo = 'ssh://backup.invalid/./synthetic-repository'
        env = {'BORG_RSH': 'synthetic'}
        responses = [
            (False, '', 'Temporary failure in name resolution'),
            (False, '', f'Repository {repo} does not exist.'),
            (True, '', ''),
        ]

        with mock.patch.object(
                reefy_backup, 'run_borg', side_effect=responses) as run_borg, \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[0.0, 100.0, 100.0, 200.0, 299.0, 300.0]), \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep, \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            ready = reefy_backup.ensure_repo_ready(
                repo, env, 'instance-synthetic'
            )

        self.assertTrue(ready)
        self.assertEqual(
            [call.args[0] for call in run_borg.call_args_list],
            [
                ['info', repo],
                ['info', repo],
                ['init', '--encryption=repokey-blake2', repo],
            ],
        )
        self.assertEqual(
            [call.kwargs['timeout'] for call in run_borg.call_args_list],
            [
                reefy_backup.REPO_INFO_ATTEMPT_TIMEOUT_S,
                reefy_backup.REPO_INFO_ATTEMPT_TIMEOUT_S,
                reefy_backup.REPO_INIT_ATTEMPT_TIMEOUT_S,
            ],
        )
        sleep.assert_called_once_with(reefy_backup.REPO_RETRY_DELAY_S)
        publish.assert_not_called()

    def test_info_timeout_publishes_existing_repo_access_error(self):
        repo = 'ssh://backup.invalid/./synthetic-repository'
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['borg', 'info', repo],
            timeout=1,
            stderr=b'synthetic command timeout',
        )

        with mock.patch.object(
                reefy_backup, 'REPO_ACCESS_DEADLINE_S', 1), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[0.0, 0.0, 2.0]), \
                mock.patch.object(
                    reefy_backup.subprocess, 'run', side_effect=timeout), \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep, \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            ready = reefy_backup.ensure_repo_ready(
                repo, {'BORG_RSH': 'synthetic'}, 'instance-synthetic'
            )

        self.assertFalse(ready)
        sleep.assert_not_called()
        publish.assert_called_once_with({
            'instance_uuid': 'instance-synthetic',
            'status': 'error',
            'message': 'repo access failed',
        })

    def test_init_timeout_publishes_existing_repo_init_error(self):
        repo = 'ssh://backup.invalid/./synthetic-repository'
        missing = types.SimpleNamespace(
            returncode=2,
            stdout='',
            stderr=f'Repository {repo} does not exist.',
        )
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['borg', 'init', repo],
            timeout=1,
            stderr=b'synthetic command timeout',
        )

        with mock.patch.object(
                reefy_backup, 'REPO_INIT_DEADLINE_S', 1), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[0.0, 0.0, 0.0, 0.0, 2.0]), \
                mock.patch.object(
                    reefy_backup.subprocess, 'run',
                    side_effect=[missing, timeout]), \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep, \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            ready = reefy_backup.ensure_repo_ready(
                repo, {'BORG_RSH': 'synthetic'}, 'instance-synthetic'
            )

        self.assertFalse(ready)
        sleep.assert_not_called()
        publish.assert_called_once_with({
            'instance_uuid': 'instance-synthetic',
            'status': 'error',
            'message': 'repo init failed',
        })

    def test_permanent_identity_and_passphrase_errors_are_fatal(self):
        for message in (
                'Passphrase supplied in BORG_PASSPHRASE is incorrect',
                'Warning: Identity file /synthetic/key not accessible',
                'Invalid location format: synthetic'):
            with self.subTest(message=message):
                self.assertEqual(
                    reefy_backup._repo_error_kind(message), 'fatal')


class PublishStatusTests(unittest.TestCase):
    def test_nonzero_publish_is_retried(self):
        results = [
            types.SimpleNamespace(returncode=1),
            types.SimpleNamespace(returncode=0),
        ]
        with mock.patch.object(
                reefy_backup.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep:
            published = reefy_backup.publish_status({
                'instance_uuid': 'instance-synthetic',
                'status': 'success',
            })

        self.assertTrue(published)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(
            reefy_backup.STATUS_PUBLISH_RETRY_DELAY_S)

    def test_publish_command_errors_exhaust_bounded_retries(self):
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=OSError('synthetic command failure')) as run, \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep:
            published = reefy_backup.publish_status({
                'instance_uuid': 'instance-synthetic',
                'status': 'error',
            })

        self.assertFalse(published)
        self.assertEqual(run.call_count,
                         reefy_backup.STATUS_PUBLISH_ATTEMPTS)
        self.assertEqual(
            sleep.call_count, reefy_backup.STATUS_PUBLISH_ATTEMPTS - 1)


class SnapshotReleaseTests(unittest.TestCase):
    def test_lv_removed_even_when_unmount_times_out(self):
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['umount', '/synthetic/snapshot'], timeout=15)
        removed = types.SimpleNamespace(returncode=0, stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[timeout, removed]) as run, \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertTrue(released)
        self.assertEqual(run.call_count, 2)

    def test_failed_lvremove_reports_unreleased_snapshot(self):
        unmounted = types.SimpleNamespace(returncode=0, stderr='')
        not_removed = types.SimpleNamespace(
            returncode=5, stderr='synthetic busy LV')
        still_present = types.SimpleNamespace(
            returncode=0, stdout=' reefy_snap_synthetic\n', stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[unmounted, not_removed, still_present]), \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertFalse(released)

    def test_repeated_release_succeeds_when_lvm_confirms_absence(self):
        missing_mount = types.SimpleNamespace(
            returncode=32, stderr='synthetic not mounted')
        missing_lv = types.SimpleNamespace(
            returncode=5, stderr='synthetic LV not found')
        absent = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[missing_mount, missing_lv, absent]), \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertTrue(released)

    def test_lvremove_timeout_succeeds_when_lvm_confirms_absence(self):
        unmounted = types.SimpleNamespace(returncode=0, stderr='')
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['lvremove'], timeout=15)
        absent = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[unmounted, timeout, absent]), \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertTrue(released)


class MaintenanceDeadlineTests(unittest.TestCase):
    def test_expired_deadline_skips_borg_command(self):
        with mock.patch.object(
                reefy_backup.time, 'monotonic', return_value=11), \
                mock.patch.object(reefy_backup, 'run_borg') as run:
            ok, _, error = reefy_backup.run_maintenance_borg(
                ['compact', 'synthetic-repository'], {}, timeout=300,
                deadline=10)

        self.assertFalse(ok)
        self.assertEqual(error, 'maintenance deadline reached')
        run.assert_not_called()


class BackupCompletionTests(unittest.TestCase):
    def test_missing_ssh_key_publishes_terminal_error(self):
        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/synthetic/data'],
        }
        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=False), \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertFalse(succeeded)
        publish.assert_called_once_with({
            'instance_uuid': 'instance-synthetic',
            'status': 'error',
            'message': 'backup SSH key missing',
        })

    def test_success_survives_maintenance_failures(self):
        events = []
        captured_env = {}
        maintenance_kwargs = {}

        def ensure_ready(repo_path, env, instance_uuid):
            events.append('ready')
            captured_env.update(env)
            return True

        def run_borg(args, env, **kwargs):
            command = args[0]
            events.append(command)
            if command == 'create':
                return (
                    True,
                    '{"archive":{"stats":{"compressed_size":321}}}',
                    '',
                )
            maintenance_kwargs[command] = kwargs
            if command == 'prune':
                raise RuntimeError('synthetic prune failure')
            return False, '', 'synthetic compact failure'

        def release_snapshot(snap_name, snap_mnt):
            events.append('release')
            return True

        statuses = []

        def publish_status(payload):
            events.append('publish')
            statuses.append(payload)
            return True

        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/mnt/reefy-data/apps/instance-synthetic/data'],
        }

        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'ensure_repo_ready',
                    side_effect=ensure_ready), \
                mock.patch.object(
                    reefy_backup, 'snapshot_volume',
                    return_value=(
                        'reefy_snap_synthetic',
                        '/synthetic/snapshot/data',
                    )), \
                mock.patch.object(
                    reefy_backup, 'release_snapshot',
                    side_effect=release_snapshot), \
                mock.patch.object(
                    reefy_backup, 'run_borg', side_effect=run_borg), \
                mock.patch.object(
                    reefy_backup, 'publish_status',
                    side_effect=publish_status), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[10.0, 15.0, 15.0, 15.0, 15.0]), \
                mock.patch.object(reefy_backup.time, 'time', return_value=1234):
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertTrue(succeeded)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]['status'], 'success')
        self.assertEqual(statuses[0]['instance_uuid'], 'instance-synthetic')
        self.assertEqual(statuses[0]['duration_s'], 5)
        self.assertEqual(statuses[0]['size_bytes'], 321)
        self.assertLess(events.index('release'), events.index('publish'))
        self.assertLess(events.index('publish'), events.index('prune'))
        self.assertLess(events.index('publish'), events.index('compact'))
        self.assertIn('prune', events)
        self.assertIn('compact', events)
        self.assertEqual(maintenance_kwargs['prune']['retries'], 1)
        self.assertEqual(maintenance_kwargs['compact']['retries'], 1)

        borg_rsh = captured_env['BORG_RSH']
        for option in (
                '-o ConnectTimeout=15',
                '-o ConnectionAttempts=1',
                '-o ServerAliveInterval=15',
                '-o ServerAliveCountMax=2'):
            self.assertIn(option, borg_rsh)

    def test_unreleased_snapshot_prevents_success_publication(self):
        statuses = []
        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/mnt/reefy-data/apps/instance-synthetic/data'],
        }

        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'ensure_repo_ready', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'snapshot_volume',
                    return_value=(
                        'reefy_snap_synthetic', '/synthetic/snapshot/data')), \
                mock.patch.object(
                    reefy_backup, 'release_snapshot', return_value=False), \
                mock.patch.object(
                    reefy_backup, 'run_borg',
                    return_value=(
                        True,
                        '{"archive":{"stats":{"compressed_size":1}}}',
                        '')), \
                mock.patch.object(
                    reefy_backup, 'publish_status',
                    side_effect=lambda payload: statuses.append(payload)), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic', return_value=10), \
                mock.patch.object(reefy_backup.time, 'time', return_value=1234):
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertFalse(succeeded)
        self.assertEqual([status['status'] for status in statuses], ['error'])
        self.assertEqual(statuses[0]['message'], 'snapshot release failed')

    def test_unpublished_success_skips_maintenance_and_returns_failure(self):
        commands = []

        def run_borg(args, env, **kwargs):
            commands.append(args[0])
            if args[0] == 'create':
                return (
                    True,
                    '{"archive":{"stats":{"compressed_size":1}}}',
                    '',
                )
            return True, '', ''

        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/mnt/reefy-data/apps/instance-synthetic/data'],
        }

        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'ensure_repo_ready', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'snapshot_volume',
                    return_value=(
                        'reefy_snap_synthetic', '/synthetic/snapshot/data')), \
                mock.patch.object(
                    reefy_backup, 'release_snapshot', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'run_borg', side_effect=run_borg), \
                mock.patch.object(
                    reefy_backup, 'publish_status', return_value=False), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic', return_value=10), \
                mock.patch.object(reefy_backup.time, 'time', return_value=1234):
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertFalse(succeeded)
        self.assertEqual(commands, ['create'])


if __name__ == '__main__':
    unittest.main()
