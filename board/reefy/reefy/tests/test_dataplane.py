"""Unit tests for reefy.dataplane (no device, no MQTT broker).

Focus: the event-routing fix (data plane publishes via reefy-mqtt-pub,
non-fatally) and the data-side behavior of the split methods."""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)
from reefy import dataplane, shared
from reefy.storage import Storage


def _make_dp():
    # __init__ reads mqtt.conf/device-uuid which don't exist on a dev box;
    # load_mqtt_config returns {} -> safe defaults, device_uuid None.
    result_dir = os.path.join(tempfile.mkdtemp(), 'apply-results')
    with mock.patch.object(
            dataplane.DataPlane, 'APPLY_RESULTS_DIR', result_dir):
        return dataplane.DataPlane(Storage())


class ImportIsolationTests(unittest.TestCase):
    def test_dataplane_imports_without_paho(self):
        self.assertFalse(hasattr(dataplane, 'mqtt'))

    def test_no_control_isms(self):
        # The data plane must not carry the control-only flag/branch.
        src = open(dataplane.__file__).read()
        self.assertNotIn('_is_data_plane', src)
        self.assertNotIn('_varlink_call', src)
        self.assertNotIn('traceback.print_exc()', src)

    def test_reclaim_runs_after_compose_up(self):
        # lvremove of a deleted instance's volume must happen AFTER docker
        # compose up --remove-orphans tears down its container; before it,
        # the volume is still bind-mounted and lvremove fails "filesystem
        # in use", leaking the LV (the e2e backup-lvm failure / prod bug).
        src = open(dataplane.__file__).read()
        self.assertLess(
            src.index('_apply_compose(compose)'),
            src.index('_reclaim_deleted_instance_lvs('),
            'reclaim must run after compose up (container teardown), '
            'else lvremove fails "filesystem in use"')


class AppsV2Tests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()
        self.tempdir = tempfile.mkdtemp()
        self.dp.DESIRED_STATE_PATH = os.path.join(
            self.tempdir, 'desired-state.json')
        self.dp.DESIRED_STATE_V2_PATH = os.path.join(
            self.tempdir, 'desired-state-v2.json')
        self.dp.COMPOSE_PATH = os.path.join(
            self.tempdir, 'docker-compose.json')
        self.dp.PROJECTS_DIR = os.path.join(self.tempdir, 'projects')

    @staticmethod
    def _state():
        return {
            'schema_version': 2,
            'revision': 'sha256:' + ('a' * 64),
            'host': {'hostname': 'synthetic-device'},
            'system_project': {
                'project_name': 'reefy-system',
                'compose': {'services': {'proxy': {'image': 'proxy:1'}}},
            },
            'instances': [
                {'instance_uuid': 'app-a', 'instance_name': 'a'},
                {'instance_uuid': 'app-b', 'instance_name': 'b'},
            ],
            'apps': [
                {
                    'instance_uuid': 'app-a',
                    'project_name': 'reefy-app-a',
                    'desired_status': 'running',
                    'compose': {'services': {'app': {'image': 'a:1'}}},
                    'volumes': [], 'files': [], 'artifacts': [],
                },
                {
                    'instance_uuid': 'app-b',
                    'project_name': 'reefy-app-b',
                    'desired_status': 'running',
                    'compose': {'services': {'app': {'image': 'b:1'}}},
                    'volumes': [], 'files': [], 'artifacts': [],
                },
            ],
        }

    def _seed_legacy_state(self):
        legacy = {
            'services': {
                'app-a': {'image': 'a:0'},
                'app-b': {'image': 'b:0'},
                'reefy-llm-proxy': {
                    'image': 'llm-proxy:0',
                    'container_name': 'reefy-llm-proxy',
                },
                'reefy-app-api': {
                    'image': 'app-api:0',
                    'container_name': 'reefy-app-api',
                },
            },
        }
        Path(self.dp.DESIRED_STATE_PATH).write_text('{}')
        Path(self.dp.COMPOSE_PATH).write_text(json.dumps(legacy))
        return legacy

    def test_v2_state_is_saved_separately_and_blocks_downgrade(self):
        state = self._state()
        with mock.patch.object(
                self.dp, '_apply_desired_state', return_value=True):
            self.assertTrue(self.dp._apply_state({'state': state}))
            self.assertTrue(os.path.exists(self.dp.DESIRED_STATE_V2_PATH))
            self.assertFalse(os.path.exists(self.dp.DESIRED_STATE_PATH))
            self.assertFalse(self.dp._apply_state({
                'state': {'hostname': 'legacy', 'compose': {'services': {}}},
            }))

    def test_app_projects_start_concurrently(self):
        barrier = threading.Barrier(2)
        entered = []

        def reconcile(app, migration, restore_failed, prepared=None):
            entered.append(app['project_name'])
            barrier.wait(timeout=2)
            return True, ('', [])

        with mock.patch.object(
                self.dp, '_v2_migration_pending', return_value=False), \
                mock.patch.object(
                    self.dp, '_apply_project_compose',
                    return_value=True), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    side_effect=reconcile), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(self.dp, '_prepare_app_artifacts'):
            warnings = self.dp._apply_v2_projects(self._state())

        self.assertEqual(warnings, [])
        self.assertCountEqual(entered, ['reefy-app-a', 'reefy-app-b'])

    def test_one_app_failure_does_not_block_another(self):
        calls = []

        def reconcile(app, migration, restore_failed, prepared=None):
            calls.append(app['project_name'])
            if app['instance_uuid'] == 'app-a':
                return False, ('app_project_failed', [])
            return True, ('', [])

        with mock.patch.object(
                self.dp, '_v2_migration_pending', return_value=False), \
                mock.patch.object(
                    self.dp, '_apply_project_compose',
                    return_value=True), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    side_effect=reconcile), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(self.dp, '_prepare_app_artifacts'):
            warnings = self.dp._apply_v2_projects(self._state())

        self.assertIn('reefy-app-b', calls)
        self.assertEqual(warnings, [{
            'code': 'app_project_failed',
            'instance_uuid': 'app-a',
            'volume': '',
        }])

    def test_artifact_failure_schedules_one_shot_backoff_retry(self):
        state = self._state()
        state['apps'] = [state['apps'][0]]
        timer = mock.Mock()
        timer.is_alive.return_value = True
        with mock.patch.object(
                self.dp, '_v2_migration_pending', return_value=False), \
                mock.patch.object(
                    self.dp, '_apply_project_compose', return_value=True), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(
                    self.dp, '_prepare_app_artifacts', return_value=False), \
                mock.patch.object(
                    dataplane.threading, 'Timer', return_value=timer) as factory:
            warnings = self.dp._apply_v2_projects(state)

        self.assertEqual(warnings[0]['code'], 'artifact_prepare_failed')
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], 10)
        self.assertTrue(timer.daemon)
        timer.start.assert_called_once()

        self.dp._schedule_artifact_retry()
        factory.assert_called_once()

    def test_artifact_retry_callback_queues_reconcile_without_polling(self):
        callbacks = []

        class FakeTimer:
            daemon = False

            def __init__(self, _delay, callback):
                callbacks.append(callback)

            def start(self):
                pass

            def cancel(self):
                pass

            def is_alive(self):
                return False

        with mock.patch.object(dataplane.threading, 'Timer', FakeTimer), \
                mock.patch.object(self.dp, '_submit_apply_job') as submit:
            self.dp._schedule_artifact_retry()
            callbacks[0]()

        submit.assert_called_once_with('reconcile')

    def test_system_project_failure_does_not_block_apps(self):
        calls = []

        def apply_project(name, compose, instance_uuid=None, **_kwargs):
            calls.append(name)
            return name != 'reefy-system'

        with mock.patch.object(
                self.dp, '_v2_migration_pending', return_value=False), \
                mock.patch.object(
                    self.dp, '_apply_project_compose',
                    side_effect=apply_project), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    return_value=(True, ('', []))) as apply_app, \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(self.dp, '_prepare_app_artifacts'):
            warnings = self.dp._apply_v2_projects(self._state())

        self.assertEqual(calls, ['reefy-system'])
        self.assertEqual(apply_app.call_count, 2)
        self.assertEqual(warnings[0]['code'], 'system_project_failed')

    def test_migration_removes_legacy_system_before_starting_v2(self):
        self._seed_legacy_state()
        commands = []
        order = []

        def run(path, project, args, timeout):
            commands.append((path, project, args, timeout))
            order.append(f'{project}:{args[0]}')
            return True, ''

        def prepare_system(*_args, **_kwargs):
            order.append('reefy-system:pull')
            return {'ok': True}

        def prepare_app(app, _restore_failed):
            order.append(f'{app["project_name"]}:pull')
            return True, '', {'ok': True}

        def start_system(_prepared, before_start=None):
            self.assertTrue(before_start())
            order.append('reefy-system:up')
            return True

        with mock.patch.object(
                self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(
                    self.dp, '_prepare_system_project_compose',
                    side_effect=prepare_system), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app', side_effect=prepare_app), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    side_effect=start_system), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    return_value=(True, ('', []))), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(self.dp, '_commit_v2_migration') as commit:
            warnings = self.dp._apply_v2_projects(self._state())

        self.assertEqual(warnings, [])
        first_legacy_mutation = min(
            index for index, value in enumerate(order)
            if value in ('reefy-system:rm', 'state:stop', 'state:rm'))
        for project in ('reefy-system', 'reefy-app-a', 'reefy-app-b'):
            self.assertLess(order.index(f'{project}:pull'),
                            first_legacy_mutation)
        self.assertLess(order.index('state:rm'),
                        order.index('reefy-system:up'))
        self.assertEqual(commands[1][2], [
            'stop', 'reefy-llm-proxy', 'reefy-app-api'])
        self.assertEqual(commands[2][2], [
            'rm', '-f', 'reefy-llm-proxy', 'reefy-app-api'])
        commit.assert_called_once()

    def test_migration_system_failure_restores_legacy_and_skips_apps(self):
        self._seed_legacy_state()
        commands = []

        def run(path, project, args, timeout):
            commands.append((path, project, args, timeout))
            return True, ''

        def fail_start(_prepared, before_start=None):
            self.assertTrue(before_start())
            return False

        with mock.patch.object(
                self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(
                    self.dp, '_prepare_system_project_compose',
                    return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app',
                    return_value=(True, '', {'ok': True})), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    side_effect=fail_start), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app') as apply_app, \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(self.dp, '_commit_v2_migration') as commit:
            warnings = self.dp._apply_v2_projects(self._state())

        self.assertEqual(warnings, [{
            'code': 'system_project_failed',
            'instance_uuid': '',
            'volume': '',
        }])
        apply_app.assert_not_called()
        commit.assert_not_called()
        self.assertIn(
            (self.dp.COMPOSE_PATH, 'state',
             ['up', '-d', '--pull', 'missing'], 300),
            commands)

    def test_migration_app_failure_rolls_back_every_v2_project(self):
        self._seed_legacy_state()
        state = self._state()
        for app in state['apps']:
            self.dp._write_project_compose(
                app['project_name'], app['compose'])
        commands = []

        def run(path, project, args, timeout):
            commands.append((path, project, args, timeout))
            return True, ''

        def reconcile(app, migration, restore_failed, prepared=None):
            if app['instance_uuid'] == 'app-b':
                return False, ('app_project_failed', [])
            return True, ('', [])

        def start_system(_prepared, before_start=None):
            self.assertTrue(before_start())
            return True

        with mock.patch.object(
                self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(
                    self.dp, '_prepare_system_project_compose',
                    return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app',
                    return_value=(True, '', {'ok': True})), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    side_effect=start_system), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app', side_effect=reconcile), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(self.dp, '_commit_v2_migration') as commit:
            warnings = self.dp._apply_v2_projects(state)

        self.assertEqual(warnings, [{
            'code': 'app_project_failed',
            'instance_uuid': 'app-b',
            'volume': '',
        }])
        commit.assert_not_called()
        for project in ('reefy-app-a', 'reefy-app-b'):
            self.assertTrue(any(
                call[1] == project and call[2] == ['stop']
                for call in commands))
        self.assertIn(
            (self.dp.COMPOSE_PATH, 'state',
             ['up', '-d', '--pull', 'missing'], 300),
            commands)

    def test_migration_commit_down_failure_keeps_legacy_source(self):
        self._seed_legacy_state()
        with mock.patch.object(
                self.dp, '_run_compose_command',
                return_value=(False, 'synthetic down failure')):
            self.assertFalse(self.dp._commit_v2_migration())

        self.assertTrue(os.path.exists(self.dp.DESIRED_STATE_PATH))
        self.assertTrue(os.path.exists(self.dp.COMPOSE_PATH))
        self.assertFalse((Path(self.tempdir) / 'apps-v2-migrated').exists())

    def test_commit_failure_rolls_back_and_reports_system_warning(self):
        legacy = self._seed_legacy_state()
        state = self._state()
        health_calls = []

        def prepare_app(app, _restore_failed):
            return True, '', {
                'ok': True,
                'project_name': app['project_name'],
            }

        with mock.patch.object(
                self.dp, '_prepare_system_project_compose',
                return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app', side_effect=prepare_app), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    side_effect=lambda _prepared, before_start=None:
                    before_start()), \
                mock.patch.object(
                    self.dp, '_prepare_v2_system_handoff',
                    return_value=True), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    return_value=(True, ('', []))), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(
                    self.dp, '_commit_v2_migration', return_value=False), \
                mock.patch.object(
                    self.dp, '_rollback_v2_migration', return_value=True), \
                mock.patch.object(
                    self.dp, '_publish_health_status',
                    side_effect=lambda *args, **kwargs:
                    health_calls.append((args, kwargs))):
            warnings = self.dp._apply_v2_projects(state)

        self.assertIn({
            'code': 'system_project_failed',
            'instance_uuid': '',
            'volume': '',
        }, warnings)
        running = {
            args[0]: kwargs.get('image')
            for args, kwargs in health_calls
            if args[1] == 'running'
        }
        self.assertEqual(
            len([args for args, _kwargs in health_calls
                 if args[1] == 'running']),
            2)
        self.assertEqual(running, {
            'app-a': legacy['services']['app-a']['image'],
            'app-b': legacy['services']['app-b']['image'],
        })

    def test_migration_preflight_abort_resets_only_prepared_bystander(self):
        legacy = self._seed_legacy_state()
        state = self._state()
        events = []

        def prepare_app(app, _restore_failed):
            self.dp._publish_health_status(
                app['instance_uuid'], 'starting')
            if app['instance_uuid'] == 'app-b':
                self.dp._publish_health_status(
                    app['instance_uuid'], 'failed',
                    message='synthetic preflight failure')
                return False, 'app_project_failed', None
            return True, '', {
                'ok': True,
                'project_name': app['project_name'],
            }

        with mock.patch.object(
                self.dp, '_prepare_system_project_compose',
                return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app', side_effect=prepare_app), \
                mock.patch.object(
                    self.dp, '_publish_health_status',
                    side_effect=lambda *args, **kwargs:
                    events.append((args, kwargs))), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project') as start:
            warnings = self.dp._apply_v2_projects(state)

        start.assert_not_called()
        self.assertEqual(
            [args[1] for args, _kwargs in events if args[0] == 'app-a'],
            ['starting', 'running'])
        self.assertEqual(
            [args[1] for args, _kwargs in events if args[0] == 'app-b'],
            ['starting', 'failed'])
        app_a_running = next(
            kwargs for args, kwargs in events
            if args[:2] == ('app-a', 'running'))
        self.assertEqual(
            app_a_running['image'], legacy['services']['app-a']['image'])
        self.assertEqual(warnings[0]['instance_uuid'], 'app-b')

    def test_migration_retry_intent_reaches_only_target_preflight(self):
        self._seed_legacy_state()
        state = self._state()
        intent = {
            'project_name': 'reefy-app-a',
            'signature': 'synthetic-signature',
        }

        with mock.patch.object(
                self.dp, '_prepare_system_project_compose',
                return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app',
                    return_value=(False, 'app_project_failed', None)) as prepare:
            self.dp._apply_v2_projects(state, force_retry=intent)

        calls = {
            call.args[0]['project_name']: call
            for call in prepare.call_args_list
        }
        self.assertEqual(
            calls['reefy-app-a'].kwargs['force_retry'], intent)
        self.assertNotIn('force_retry', calls['reefy-app-b'].kwargs)

    def test_migration_app_failure_rollback_resets_bystander_health(self):
        legacy = self._seed_legacy_state()
        state = self._state()
        events = []

        def prepare_app(app, _restore_failed):
            self.dp._publish_health_status(app['instance_uuid'], 'starting')
            return True, '', {
                'ok': True,
                'project_name': app['project_name'],
            }

        def reconcile(app, migration, restore_failed, prepared=None):
            if app['instance_uuid'] == 'app-b':
                self.dp._publish_health_status(
                    'app-b', 'failed', message='synthetic start failure')
                return False, ('app_project_failed', [])
            return True, ('', [])

        with mock.patch.object(
                self.dp, '_prepare_system_project_compose',
                return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app', side_effect=prepare_app), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    return_value=True), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app', side_effect=reconcile), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(
                    self.dp, '_rollback_v2_migration', return_value=True), \
                mock.patch.object(
                    self.dp, '_publish_health_status',
                    side_effect=lambda *args, **kwargs:
                    events.append((args, kwargs))):
            self.dp._apply_v2_projects(state)

        app_a = [
            (args[1], kwargs.get('image'))
            for args, kwargs in events if args[0] == 'app-a'
        ]
        app_b = [
            args[1] for args, _kwargs in events if args[0] == 'app-b'
        ]
        self.assertEqual(app_a, [
            ('starting', None),
            ('running', legacy['services']['app-a']['image']),
        ])
        self.assertEqual(app_b, ['starting', 'failed'])

    def test_migration_success_publishes_one_committed_running_per_app(self):
        self._seed_legacy_state()
        state = self._state()
        events = []

        def prepare_app(app, _restore_failed):
            self.dp._publish_health_status(app['instance_uuid'], 'starting')
            return True, '', {
                'ok': True,
                'project_name': app['project_name'],
                '_running_health': {
                    'message': None,
                    'image': app['compose']['services']['app']['image'],
                },
            }

        with mock.patch.object(
                self.dp, '_prepare_system_project_compose',
                return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app', side_effect=prepare_app), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    return_value=True), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    return_value=(True, ('', []))), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(
                    self.dp, '_commit_v2_migration', return_value=True), \
                mock.patch.object(
                    self.dp, '_publish_health_status',
                    side_effect=lambda *args, **kwargs:
                    events.append((args, kwargs))):
            warnings = self.dp._apply_v2_projects(state)

        self.assertEqual(warnings, [])
        for app in state['apps']:
            instance_uuid = app['instance_uuid']
            app_events = [
                (args[1], kwargs.get('image'))
                for args, kwargs in events if args[0] == instance_uuid
            ]
            self.assertEqual(app_events, [
                ('starting', None),
                ('running', app['compose']['services']['app']['image']),
            ])

    def test_legacy_app_stop_failure_is_sticky_and_terminal(self):
        app = self._state()['apps'][0]
        compose = app['compose']
        context = self.dp._required_app_failure_context(
            app['project_name'], compose, 'app')
        prepared = {
            'ok': True,
            'project_name': app['project_name'],
            'instance_uuid': app['instance_uuid'],
            'required_context': context,
        }

        with mock.patch.object(
                self.dp, '_run_compose_command',
                return_value=(False, 'synthetic stop failure')), \
                mock.patch.object(
                    self.dp, '_start_prepared_app_project') as start, \
                mock.patch.object(
                    self.dp, '_publish_health_status') as health:
            result = self.dp._reconcile_v2_app(
                app, migration=True, restore_failed=False,
                prepared=prepared)

        self.assertEqual(result, (False, 'app_stop_failed'))
        start.assert_not_called()
        self.assertEqual(
            [call.args[1] for call in health.call_args_list], ['failed'])
        marker = self.dp._read_failed_sig_record(context['path'])
        self.assertEqual(marker['phase'], 'start')

    def test_rollback_cleanup_failure_does_not_confirm_legacy_running(self):
        self._seed_legacy_state()
        state = self._state()
        for app in state['apps']:
            self.dp._write_project_compose(
                app['project_name'], app['compose'])
        self.dp._write_project_compose(
            'reefy-system', state['system_project']['compose'])
        commands = []

        def run(_path, project, args, timeout):
            commands.append((project, args))
            if project == 'reefy-app-a' and args == ['stop']:
                return False, 'synthetic stop failure'
            return True, ''

        with mock.patch.object(
                self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(dataplane, 'log') as logger:
            self.assertFalse(self.dp._rollback_v2_migration(
                state, 'reefy-system'))

        self.assertIn(
            ('state', ['up', '-d', '--pull', 'missing']), commands)
        self.assertTrue(any(
            'rollback cleanup failures' in str(call.args[1])
            for call in logger.call_args_list))

    def test_migration_clears_sticky_system_failure_after_name_release(self):
        self._seed_legacy_state()
        system_dir = Path(self.dp.PROJECTS_DIR) / 'reefy-system'
        system_dir.mkdir(parents=True)
        failed = system_dir / '.failed-compose-sig'
        optional = system_dir / '.failed-compose-sig.optional'
        failed.write_text('same-signature\ncontainer name conflict')
        optional.write_text('same-signature\ncontainer name conflict')

        with mock.patch.object(
                self.dp, '_run_compose_command', return_value=(True, '')):
            ok = self.dp._prepare_v2_system_handoff(
                'reefy-system', self._state()['system_project']['compose'],
                ['reefy-llm-proxy', 'reefy-app-api'])

        self.assertTrue(ok)
        self.assertFalse(failed.exists())
        self.assertFalse(optional.exists())

    def test_migration_prepares_artifact_before_stopping_legacy_app(self):
        app = self._state()['apps'][0]
        order = []

        def run(_path, project, args, timeout):
            if project == 'state' and args[:1] == ['stop']:
                order.append('stop-legacy')
            return True, ''

        def start_prepared(_prepared, publish_running=True):
            self.assertFalse(publish_running)
            order.append('start-v2')
            return True, []

        with mock.patch.object(
                self.dp, '_prepare_app_artifacts',
                side_effect=lambda _app: order.append('prepare') or True), \
                mock.patch.object(
                    self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(
                    self.dp, '_prepare_app_project_compose',
                    side_effect=lambda *_args, **_kwargs:
                    (order.append('pull-images') or {
                        'ok': True, 'project_name': 'reefy-app-a'})), \
                mock.patch.object(
                    self.dp, '_start_prepared_app_project',
                    side_effect=start_prepared):
            result = self.dp._reconcile_v2_app(
                app, migration=True, restore_failed=False)

        self.assertEqual(result, (True, ('', [])))
        self.assertEqual(order, [
            'prepare', 'pull-images', 'stop-legacy', 'start-v2'])

    def test_optional_warning_does_not_block_migration_commit(self):
        with mock.patch.object(
                self.dp, '_v2_migration_pending', return_value=True), \
                mock.patch.object(
                    self.dp, '_read_json', return_value={}), \
                mock.patch.object(
                    self.dp, '_prepare_system_project_compose',
                    return_value={'ok': True}), \
                mock.patch.object(
                    self.dp, '_prepare_v2_app',
                    return_value=(True, '', {'ok': True})), \
                mock.patch.object(
                    self.dp, '_start_prepared_system_project',
                    side_effect=lambda _prepared, before_start=None:
                    before_start()), \
                mock.patch.object(
                    self.dp, '_reconcile_v2_app',
                    return_value=(True, ('', ['diagnostics']))), \
                mock.patch.object(self.dp, '_remove_absent_v2_projects'), \
                mock.patch.object(
                    self.dp, '_run_compose_command', return_value=(True, '')), \
                mock.patch.object(self.dp, '_commit_v2_migration') as commit:
            warnings = self.dp._apply_v2_projects(self._state())

        commit.assert_called_once()
        self.assertEqual(
            [warning['code'] for warning in warnings],
            ['optional_service_failed', 'optional_service_failed'])

    def test_service_die_event_immediately_queues_repair(self):
        with open(self.dp.DESIRED_STATE_V2_PATH, 'w') as stream:
            json.dump(self._state(), stream)
        event = {
            'Action': 'die',
            'Actor': {'Attributes': {
                'ai.reefy.lifecycle': 'service',
                'ai.reefy.instance_uuid': 'app-a',
                'com.docker.compose.project': 'reefy-app-a',
            }},
        }
        worker = mock.Mock()
        with mock.patch.object(
                dataplane.threading, 'Thread', return_value=worker) as thread:
            self.assertTrue(self.dp._handle_docker_event(event))
        thread.assert_called_once()
        worker.start.assert_called_once()

        event['Actor']['Attributes']['exitCode'] = '17'
        self.assertFalse(self.dp._handle_docker_event(event))
        event['Actor']['Attributes']['exitCode'] = '0'
        event['Actor']['Attributes']['ai.reefy.lifecycle'] = 'init'
        self.assertFalse(self.dp._handle_docker_event(event))

    def test_service_die_event_is_suppressed_during_pending_migration(self):
        with open(self.dp.DESIRED_STATE_V2_PATH, 'w') as stream:
            json.dump(self._state(), stream)
        Path(self.dp.DESIRED_STATE_PATH).write_text('{}')
        event = {
            'Action': 'die',
            'Actor': {'Attributes': {
                'ai.reefy.lifecycle': 'service',
                'ai.reefy.instance_uuid': 'app-a',
                'com.docker.compose.project': 'reefy-app-a',
                'exitCode': '0',
            }},
        }

        with mock.patch.object(dataplane.threading, 'Thread') as thread:
            self.assertFalse(self.dp._handle_docker_event(event))

        thread.assert_not_called()

    def test_runtime_compose_strips_completed_init_and_dependency(self):
        compose = {'services': {
            'setup': {
                'image': 'setup:1',
                'labels': {'ai.reefy.lifecycle': 'init'},
            },
            'web': {
                'image': 'web:1',
                'depends_on': {
                    'setup': {'condition': 'service_completed_successfully'},
                    'database': {'condition': 'service_healthy'},
                },
            },
            'database': {'image': 'database:1'},
        }}

        runtime = self.dp._runtime_app_compose(compose)

        self.assertNotIn('setup', runtime['services'])
        self.assertEqual(
            runtime['services']['web']['depends_on'],
            {'database': {'condition': 'service_healthy'}})
        self.assertIn('setup', compose['services'])

    def test_init_fingerprint_prevents_rerun(self):
        compose = {'services': {
            'setup': {
                'image': 'setup:1',
                'labels': {
                    'ai.reefy.lifecycle': 'init',
                    'ai.reefy.optional': 'false',
                },
            },
            'web': {'image': 'web:1'},
        }}
        calls = []

        def run(_path, _project, args, timeout):
            calls.append(args)
            return True, ''

        with mock.patch.object(
                self.dp, '_run_compose_command', side_effect=run):
            self.assertEqual(
                self.dp._run_app_init_services(
                    'reefy-app-a', compose, 'app-a'),
                (True, {}, None))
            self.assertEqual(
                self.dp._run_app_init_services(
                    'reefy-app-a', compose, 'app-a'),
                (True, {}, None))

        self.assertEqual(calls, [
            ['run', '--pull', 'never', '--rm', 'setup']])

    def test_optional_service_failure_keeps_app_running(self):
        compose = {'services': {
            'web': {
                'image': 'web:1',
                'labels': {
                    'ai.reefy.lifecycle': 'service',
                    'ai.reefy.optional': 'false',
                },
            },
            'diagnostics': {
                'image': 'missing.invalid/diagnostics:1',
                'labels': {
                    'ai.reefy.lifecycle': 'service',
                    'ai.reefy.optional': 'true',
                },
            },
        }}

        def start(_path, _project, services, **_kwargs):
            if services == ['diagnostics']:
                return False, 'synthetic optional failure', (
                    'docker compose up failed')
            return True, '', ''

        with mock.patch.object(
                self.dp, '_pull_project_images',
                return_value=(True, '', '')), \
                mock.patch.object(
                    self.dp, '_start_project_services', side_effect=start), \
                mock.patch.object(self.dp, '_publish_health_status'):
            result = self.dp._apply_app_project_compose(
                'reefy-app-a', compose, 'app-a', 'web')

        self.assertEqual(result, (True, ['diagnostics']))


class AppsV2PullRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()
        self.tempdir = tempfile.mkdtemp()
        self.dp.DESIRED_STATE_PATH = os.path.join(
            self.tempdir, 'desired-state.json')
        self.dp.DESIRED_STATE_V2_PATH = os.path.join(
            self.tempdir, 'desired-state-v2.json')
        self.dp.COMPOSE_PATH = os.path.join(
            self.tempdir, 'docker-compose.json')
        self.dp.PROJECTS_DIR = os.path.join(self.tempdir, 'projects')

    @staticmethod
    def _health_statuses(health):
        return [call.args[1] for call in health.call_args_list]

    @staticmethod
    def _compose_with_init():
        return {'services': {
            'seed': {'image': 'synthetic/seed:1'},
            'setup': {
                'image': 'synthetic/setup:1',
                'labels': {'ai.reefy.lifecycle': 'init'},
                'depends_on': {'seed': {'condition': 'service_started'}},
            },
            'app': {
                'image': 'synthetic/app:1',
                'depends_on': {
                    'setup': {
                        'condition': 'service_completed_successfully'},
                    'seed': {'condition': 'service_started'},
                },
            },
        }}

    def _write_v2_state(self, compose=None):
        compose = compose or {'services': {
            'app': {'image': 'synthetic/app:1'},
        }}
        state = {
            'schema_version': 2,
            'apps': [{
                'instance_uuid': 'synthetic-app',
                'project_name': 'reefy-synthetic-app',
                'primary_service': 'app',
                'desired_status': 'running',
                'compose': compose,
                'artifacts': [],
            }],
        }
        Path(self.dp.DESIRED_STATE_V2_PATH).write_text(json.dumps(state))
        return state

    def test_app_pipeline_pulls_before_init_and_start_with_explicit_flags(self):
        compose = self._compose_with_init()
        calls = []

        def pull(_path, _project, args, timeout):
            calls.append(('pull', args, timeout))
            return True, 'pull complete'

        def run(_path, _project, args, timeout):
            calls.append((args[0], args, timeout))
            return True, 'complete'

        with mock.patch.object(
                self.dp, '_run_compose_command_streaming',
                side_effect=pull), \
                mock.patch.object(
                    self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(self.dp, '_publish_health_status') as health:
            result = self.dp._apply_app_project_compose(
                'reefy-synthetic-app', compose, 'synthetic-app', 'app')

        self.assertEqual(result, (True, []))
        self.assertEqual(calls[0][0], 'pull')
        self.assertEqual(calls[0][1], [
            'pull', '--policy', 'missing', 'app', 'seed', 'setup'])
        self.assertLessEqual(calls[0][2], 3600)
        self.assertGreater(calls[0][2], 3500)
        self.assertEqual(calls[1][1], [
            'run', '--pull', 'never', '--rm', 'setup'])
        up = next(call for call in calls if call[0] == 'up')
        self.assertEqual(up[1][:4], ['up', '-d', '--pull', 'never'])
        self.assertIn('--remove-orphans', up[1])
        self.assertLessEqual(up[2], 180)
        statuses = [call.args[1] for call in health.call_args_list]
        self.assertEqual(statuses[0], 'starting')
        self.assertEqual(statuses[-1], 'running')

    def test_pull_failure_skips_compose_up_and_persists_pull_phase(self):
        compose = {'services': {'app': {'image': 'synthetic/missing:1'}}}
        with mock.patch.object(
                self.dp, '_run_compose_command_streaming',
                return_value=(False, 'manifest unknown')) as pull, \
                mock.patch.object(
                    self.dp, '_run_compose_command') as run, \
                mock.patch.object(self.dp, '_publish_health_status'):
            self.assertFalse(self.dp._apply_project_compose(
                'reefy-synthetic-app', compose,
                instance_uuid='synthetic-app'))

        pull.assert_called_once()
        run.assert_not_called()
        marker = self.dp._read_failed_sig_record(os.path.join(
            self.dp.PROJECTS_DIR, 'reefy-synthetic-app',
            '.failed-compose-sig'))
        self.assertEqual(marker['phase'], 'pull')
        self.assertEqual(marker['services'], ['app'])
        self.assertEqual(
            marker['reason'], 'image not found or access denied')

    def test_pull_retries_share_one_monotonic_deadline(self):
        clock = [0.0]
        timeouts = []

        def monotonic():
            return clock[0]

        def pull(*_args, **kwargs):
            timeouts.append(kwargs['timeout'])
            clock[0] += 100
            return False, 'temporary registry timeout'

        def sleep(delay):
            clock[0] += delay

        with mock.patch.object(
                dataplane.time, 'monotonic', side_effect=monotonic), \
                mock.patch.object(
                    dataplane.time, 'sleep', side_effect=sleep), \
                mock.patch.object(
                    self.dp, '_run_compose_command_streaming',
                    side_effect=pull):
            ok, _output, reason = self.dp._pull_project_images(
                '/synthetic/compose.json', 'reefy-synthetic-app', ['app'],
                deadline=360, max_retries=5)

        self.assertFalse(ok)
        self.assertEqual(timeouts, [360, 250, 130])
        self.assertEqual(clock[0], 360)
        self.assertEqual(reason, 'docker compose pull timed out')

    def test_deterministic_pull_failure_does_not_retry(self):
        with mock.patch.object(
                self.dp, '_run_compose_command_streaming',
                return_value=(
                    False, 'unauthorized: authentication required')) as pull, \
                mock.patch.object(dataplane.time, 'sleep') as sleep:
            ok, _output, reason = self.dp._pull_project_images(
                '/synthetic/compose.json', 'reefy-synthetic-app', ['app'],
                deadline=dataplane.time.monotonic() + 3600)

        self.assertFalse(ok)
        pull.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(reason, 'image not found or access denied')

    def test_pull_no_space_and_storage_corruption_fail_without_prune(self):
        cases = (
            ('no space left on device', 'out of disk space'),
            ('failed to register layer', 'docker storage error'),
        )
        for output, expected_reason in cases:
            with self.subTest(output=output), \
                    mock.patch.object(
                        self.dp, '_run_compose_command_streaming',
                        return_value=(False, output)) as pull, \
                    mock.patch.object(self.dp, '_prune_docker') as prune, \
                    mock.patch.object(dataplane.time, 'sleep') as sleep:
                ok, _output, reason = self.dp._pull_project_images(
                    '/synthetic/compose.json', 'reefy-synthetic-app',
                    ['app'], deadline=dataplane.time.monotonic() + 3600)

            self.assertFalse(ok)
            pull.assert_called_once()
            prune.assert_not_called()
            sleep.assert_not_called()
            self.assertEqual(reason, expected_reason)

    def test_start_retries_share_180_second_deadline(self):
        clock = [0.0]
        timeouts = []

        def monotonic():
            return clock[0]

        def run(*_args, **kwargs):
            timeouts.append(kwargs['timeout'])
            clock[0] += 100
            return False, 'temporary daemon timeout'

        def sleep(delay):
            clock[0] += delay

        with mock.patch.object(
                dataplane.time, 'monotonic', side_effect=monotonic), \
                mock.patch.object(
                    dataplane.time, 'sleep', side_effect=sleep), \
                mock.patch.object(
                    self.dp, '_run_compose_command', side_effect=run):
            ok, _output, reason = self.dp._start_project_services(
                '/synthetic/compose.json', 'reefy-synthetic-app', ['app'])

        self.assertFalse(ok)
        self.assertEqual(timeouts, [180, 70])
        self.assertGreaterEqual(clock[0], 180)
        self.assertEqual(reason, 'docker compose up timed out')

    def test_start_deterministic_failures_do_not_backoff(self):
        cases = (
            ('manifest unknown', 'image not found or access denied'),
            ('no space left on device', 'out of disk space'),
            ('failed to register layer', 'docker storage error'),
        )
        for output, expected_reason in cases:
            with self.subTest(output=output), \
                    mock.patch.object(
                        self.dp, '_run_compose_command',
                        return_value=(False, output)) as run, \
                    mock.patch.object(dataplane.time, 'sleep') as sleep:
                ok, _output, reason = self.dp._start_project_services(
                    '/synthetic/compose.json', 'reefy-synthetic-app',
                    ['app'])

            self.assertFalse(ok)
            run.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(reason, expected_reason)

    def test_completed_required_and_optional_init_images_are_not_pulled(self):
        compose = self._compose_with_init()
        compose['services']['optional-setup'] = {
            'image': 'synthetic/optional-setup:1',
            'labels': {
                'ai.reefy.lifecycle': 'init',
                'ai.reefy.optional': 'true',
            },
        }
        project_name = 'reefy-synthetic-app'
        project_dir = Path(self.dp.PROJECTS_DIR) / project_name
        project_dir.mkdir(parents=True)
        for service_name in ('setup', 'optional-setup'):
            signature = self.dp._compose_sig({
                'service': service_name,
                'definition': compose['services'][service_name],
            })
            (project_dir / f'.init-{service_name}.sig').write_text(
                signature + '\n')
        pulled = []

        def pull(_path, _project, services, _deadline, **_kwargs):
            pulled.append(services)
            return True, '', ''

        with mock.patch.object(
                self.dp, '_pull_project_images', side_effect=pull), \
                mock.patch.object(self.dp, '_publish_health_status'):
            prepared = self.dp._prepare_app_project_compose(
                project_name, compose, 'synthetic-app', 'app')

        self.assertTrue(prepared['ok'])
        self.assertEqual(pulled, [['app', 'seed']])
        self.assertNotIn('setup', pulled[0])
        self.assertNotIn('optional-setup', pulled[0])

    def test_pending_init_pull_includes_its_dependencies(self):
        plan = self.dp._app_project_plan(
            'reefy-synthetic-app', self._compose_with_init(), 'app')
        self.assertEqual(plan['required_services'], [
            'app', 'seed', 'setup'])

    def test_completed_init_is_absent_from_pull_and_pending_run_compose(self):
        compose = {'services': {
            'bootstrap': {
                'image': 'synthetic/bootstrap:1',
                'labels': {'ai.reefy.lifecycle': 'init'},
            },
            'migrate': {
                'image': 'synthetic/migrate:1',
                'labels': {'ai.reefy.lifecycle': 'init'},
                'depends_on': {
                    'bootstrap': {
                        'condition': 'service_completed_successfully'},
                },
            },
            'app': {'image': 'synthetic/app:1'},
        }}
        project_name = 'reefy-synthetic-app'
        project_dir = Path(self.dp.PROJECTS_DIR) / project_name
        project_dir.mkdir(parents=True)
        signature = self.dp._init_service_signature(
            'bootstrap', compose['services']['bootstrap'])
        (project_dir / '.init-bootstrap.sig').write_text(signature + '\n')

        plan = self.dp._app_project_plan(project_name, compose, 'app')
        self.assertEqual(plan['required_services'], ['app', 'migrate'])
        run_composes = []
        run_services = []

        def run(compose_path, _project, args, timeout):
            run_composes.append(json.loads(Path(compose_path).read_text()))
            run_services.append(args[-1])
            return True, ''

        with mock.patch.object(
                self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(self.dp, '_publish_health_status'):
            result = self.dp._run_app_init_services(
                project_name, compose, 'synthetic-app',
                required_init_services=plan['required_init_services'])

        self.assertEqual(result, (True, {}, None))
        self.assertEqual(run_services, ['migrate'])
        self.assertNotIn('bootstrap', run_composes[0]['services'])
        self.assertNotIn(
            'depends_on', run_composes[0]['services']['migrate'])

    def test_optional_init_in_required_closure_is_required(self):
        relationships = ('required_init', 'required_runtime')
        for relationship in relationships:
            with self.subTest(relationship=relationship):
                bootstrap = {
                    'image': 'synthetic/bootstrap:1',
                    'labels': {
                        'ai.reefy.lifecycle': 'init',
                        'ai.reefy.optional': 'true',
                    },
                }
                services = {
                    'bootstrap': bootstrap,
                    'app': {'image': 'synthetic/app:1'},
                }
                if relationship == 'required_init':
                    services['migrate'] = {
                        'image': 'synthetic/migrate:1',
                        'labels': {'ai.reefy.lifecycle': 'init'},
                        'depends_on': ['bootstrap'],
                    }
                else:
                    services['app']['depends_on'] = ['bootstrap']
                compose = {'services': services}
                project_name = f'reefy-synthetic-{relationship}'
                plan = self.dp._app_project_plan(
                    project_name, compose, 'app')

                self.assertIn('bootstrap', plan['required_services'])
                self.assertIn(
                    'bootstrap', plan['required_init_services'])
                self.assertNotIn(
                    'bootstrap', [entry['name']
                                  for entry in plan['optional']])

                init_calls = []

                def run(_path, _project, args, timeout):
                    init_calls.append(args[-1])
                    if args[-1] == 'bootstrap':
                        return False, 'synthetic bootstrap failure'
                    return True, ''

                with mock.patch.object(
                        self.dp, '_pull_project_images',
                        return_value=(True, '', '')), \
                        mock.patch.object(
                            self.dp, '_run_compose_command', side_effect=run), \
                        mock.patch.object(
                            self.dp, '_start_project_services') as start, \
                        mock.patch.object(
                            self.dp, '_publish_health_status') as health:
                    result = self.dp._apply_app_project_compose(
                        project_name, compose, 'synthetic-app', 'app')

                self.assertEqual(result, (False, []))
                self.assertEqual(init_calls, ['bootstrap'])
                start.assert_not_called()
                statuses = self._health_statuses(health)
                self.assertEqual(statuses.count('failed'), 1)
                self.assertNotIn('running', statuses)

    def test_optional_pull_failure_does_not_block_required_start(self):
        compose = {'services': {
            'app': {'image': 'synthetic/app:1'},
            'diagnostics': {
                'image': 'synthetic/diagnostics:1',
                'labels': {'ai.reefy.optional': 'true'},
            },
        }}

        def pull(_path, _project, services, _deadline, **_kwargs):
            if services == ['diagnostics']:
                return False, 'temporary registry timeout', (
                    'docker compose pull failed')
            return True, '', ''

        with mock.patch.object(
                self.dp, '_pull_project_images', side_effect=pull), \
                mock.patch.object(self.dp, '_publish_health_status'):
            prepared = self.dp._prepare_app_project_compose(
                'reefy-synthetic-app', compose,
                'synthetic-app', 'app')

        self.assertTrue(prepared['ok'])
        self.assertIn('diagnostics', prepared['optional_failures'])
        marker = self.dp._read_failed_sig_record(os.path.join(
            self.dp.PROJECTS_DIR, 'reefy-synthetic-app',
            '.failed-compose-sig.optional-diagnostics'))
        self.assertEqual(marker['phase'], 'pull')
        self.assertFalse(os.path.exists(os.path.join(
            self.dp.PROJECTS_DIR, 'reefy-synthetic-app',
            '.failed-compose-sig')))

    def test_optional_runtime_does_not_start_after_init_dependency_fails(self):
        compose = {'services': {
            'app': {'image': 'synthetic/app:1'},
            'setup': {
                'image': 'synthetic/setup:1',
                'labels': {
                    'ai.reefy.lifecycle': 'init',
                    'ai.reefy.optional': 'true',
                },
            },
            'diagnostics': {
                'image': 'synthetic/diagnostics:1',
                'labels': {'ai.reefy.optional': 'true'},
                'depends_on': {
                    'setup': {
                        'condition': 'service_completed_successfully'},
                },
            },
        }}
        starts = []

        def run(_path, _project, args, timeout):
            self.assertEqual(args[-1], 'setup')
            return False, 'synthetic setup failure'

        def start(_path, _project, services, **_kwargs):
            starts.append(services)
            return True, '', ''

        with mock.patch.object(
                self.dp, '_pull_project_images',
                return_value=(True, '', '')), \
                mock.patch.object(
                    self.dp, '_run_compose_command', side_effect=run), \
                mock.patch.object(
                    self.dp, '_start_project_services', side_effect=start), \
                mock.patch.object(
                    self.dp, '_publish_health_status') as health:
            result = self.dp._apply_app_project_compose(
                'reefy-synthetic-app', compose,
                'synthetic-app', 'app')

        self.assertEqual(result, (True, ['diagnostics', 'setup']))
        self.assertEqual(starts, [['app']])
        diagnostics_marker = self.dp._read_failed_sig_record(os.path.join(
            self.dp.PROJECTS_DIR, 'reefy-synthetic-app',
            '.failed-compose-sig.optional-diagnostics'))
        self.assertEqual(diagnostics_marker['phase'], 'init')
        statuses = self._health_statuses(health)
        self.assertEqual(statuses.count('running'), 1)
        self.assertEqual(statuses.count('failed'), 0)

    def test_legacy_optional_runtime_marker_still_skips_retry(self):
        compose = {'services': {
            'app': {'image': 'synthetic/app:1'},
            'setup': {
                'image': 'synthetic/setup:1',
                'labels': {
                    'ai.reefy.lifecycle': 'init',
                    'ai.reefy.optional': 'true',
                },
            },
            'diagnostics': {
                'image': 'synthetic/diagnostics:1',
                'labels': {'ai.reefy.optional': 'true'},
                'depends_on': ['setup'],
            },
        }}
        project_name = 'reefy-synthetic-app'
        runtime = self.dp._runtime_app_compose(compose)
        legacy_signature = self.dp._compose_sig({
            'compose': runtime,
            'services': ['diagnostics'],
        })
        marker_path = Path(self.dp.PROJECTS_DIR) / project_name / (
            '.failed-compose-sig.optional-diagnostics')
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text(
            legacy_signature + '\nsynthetic legacy optional failure')
        pulls = []
        starts = []

        def pull(_path, _project, services, _deadline, **_kwargs):
            pulls.append(services)
            return True, '', ''

        def start(_path, _project, services, **_kwargs):
            starts.append(services)
            return True, '', ''

        with mock.patch.object(
                self.dp, '_pull_project_images', side_effect=pull), \
                mock.patch.object(
                    self.dp, '_run_compose_command', return_value=(True, '')), \
                mock.patch.object(
                    self.dp, '_start_project_services', side_effect=start), \
                mock.patch.object(self.dp, '_publish_health_status'):
            result = self.dp._apply_app_project_compose(
                project_name, compose, 'synthetic-app', 'app')

        self.assertEqual(result, (True, ['diagnostics']))
        self.assertEqual(pulls, [['app'], ['setup']])
        self.assertEqual(starts, [['app']])
        self.assertTrue(marker_path.exists())

    def test_failure_marker_is_atomic_json_and_reads_legacy_format(self):
        marker_path = os.path.join(
            self.tempdir, 'project', '.failed-compose-sig')
        os.makedirs(os.path.dirname(marker_path))
        Path(marker_path).write_text('legacy-signature\nlegacy reason')
        legacy = self.dp._read_failed_sig_record(marker_path)
        self.assertEqual(legacy['version'], 1)
        self.assertEqual(legacy['phase'], 'legacy')
        self.assertEqual(legacy['reason'], 'legacy reason')

        with mock.patch.object(
                dataplane.os, 'replace', wraps=os.replace) as replace:
            self.dp._write_failed_sig_path(
                marker_path, 'current-signature',
                'password=synthetic-private-value',
                phase='init', services=['setup', 'app'])
        replace.assert_called_once_with(marker_path + '.tmp', marker_path)
        current = json.loads(Path(marker_path).read_text())
        self.assertEqual(current['version'], 2)
        self.assertEqual(current['phase'], 'init')
        self.assertEqual(current['services'], ['app', 'setup'])
        self.assertEqual(current['reason'], 'password=[REDACTED]')
        self.assertNotIn('synthetic-private-value', Path(marker_path).read_text())
        self.assertFalse(os.path.exists(marker_path + '.tmp'))

        old_content = Path(marker_path).read_text()
        with mock.patch.object(
                dataplane.os, 'replace', side_effect=OSError('synthetic')), \
                mock.patch.object(dataplane, 'log'):
            self.dp._write_failed_sig_path(
                marker_path, 'replacement-signature', 'replacement reason',
                phase='pull', services=['app'])
        self.assertEqual(Path(marker_path).read_text(), old_content)
        self.assertFalse(os.path.exists(marker_path + '.tmp'))

    def test_retry_recovers_each_current_required_failure_phase(self):
        compose = {'services': {'app': {'image': 'synthetic/app:1'}}}
        self._write_v2_state(compose)
        project_name = 'reefy-synthetic-app'
        context = self.dp._failure_marker_context(
            project_name, compose, ['app'])
        other_dir = Path(self.dp.PROJECTS_DIR) / 'reefy-other-app'
        other_dir.mkdir(parents=True)
        other_marker = other_dir / '.failed-compose-sig'
        other_marker.write_text('other-signature\nother reason')

        for phase in ('pull', 'init', 'start'):
            with self.subTest(phase=phase):
                self.dp._write_failed_sig_path(
                    context['path'], context['signature'],
                    'synthetic required failure', phase=phase,
                    services=['app'])
                optional_path = context['path'] + '.optional-diagnostics'
                self.dp._write_failed_sig_path(
                    optional_path, 'optional-signature',
                    'synthetic optional failure', phase='pull',
                    services=['diagnostics'])
                with mock.patch.object(
                        self.dp, '_pull_project_images',
                        return_value=(True, '', '')), \
                        mock.patch.object(
                            self.dp, '_start_project_services',
                            return_value=(True, '', '')), \
                        mock.patch.object(
                            self.dp, '_publish_health_status') as health, \
                        mock.patch.object(
                            dataplane.subprocess, 'run') as healthy_restart:
                    message = self.dp._restart_instance({
                        'instance_uuid': 'synthetic-app'})

                self.assertEqual(
                    message, 'Instance synthetic-app restarted')
                healthy_restart.assert_not_called()
                self.assertFalse(os.path.exists(context['path']))
                self.assertFalse(os.path.exists(optional_path))
                self.assertTrue(other_marker.exists())
                statuses = [call.args[1] for call in health.call_args_list]
                self.assertIn('starting', statuses)
                self.assertEqual(statuses[-1], 'running')

    def test_pending_migration_required_retry_uses_full_serialized_job(self):
        compose = {'services': {'app': {'image': 'synthetic/app:1'}}}
        self._write_v2_state(compose)
        Path(self.dp.DESIRED_STATE_PATH).write_text('{}')
        project_name = 'reefy-synthetic-app'
        context = self.dp._required_app_failure_context(
            project_name, compose, 'app')
        self.dp._write_failed_sig_path(
            context['path'], context['signature'], 'synthetic pull failure',
            phase='pull', services=context['services'])
        migration_marker = Path(self.tempdir) / 'apps-v2-migrated'

        def wait(_request_id):
            self.dp._clear_failed_sig_path(context['path'])
            migration_marker.write_text('2\n')
            return {
                'status': 'succeeded',
                'error': '',
                'warnings': [],
            }

        with mock.patch.object(
                self.dp, '_submit_apply_job',
                return_value={
                    'ok': True, 'request_id': 'synthetic-request',
                    'error': '',
                }) as submit, \
                mock.patch.object(
                    self.dp, '_wait_apply_result', side_effect=wait), \
                mock.patch.object(
                    self.dp, '_apply_app_project_compose') as direct_apply, \
                mock.patch.object(dataplane.subprocess, 'run') as direct_run:
            message = self.dp._restart_instance({
                'instance_uuid': 'synthetic-app'})

        self.assertEqual(message, 'Instance synthetic-app restarted')
        submit.assert_called_once_with(
            'reconcile',
            force_retry={
                'project_name': project_name,
                'signature': context['signature'],
            },
            wait_for_idle=True)
        direct_apply.assert_not_called()
        direct_run.assert_not_called()

    def test_pending_migration_healthy_and_optional_restart_reject(self):
        compose = {'services': {
            'app': {'image': 'synthetic/app:1'},
            'diagnostics': {
                'image': 'synthetic/diagnostics:1',
                'labels': {'ai.reefy.optional': 'true'},
            },
        }}
        self._write_v2_state(compose)
        Path(self.dp.DESIRED_STATE_PATH).write_text('{}')
        project_name = 'reefy-synthetic-app'
        project_dir = Path(self.dp.PROJECTS_DIR) / project_name
        project_dir.mkdir(parents=True)

        for optional_marker in (False, True):
            with self.subTest(optional_marker=optional_marker):
                optional_path = project_dir / (
                    '.failed-compose-sig.optional-diagnostics')
                if optional_marker:
                    optional_path.write_text(
                        'synthetic-signature\nsynthetic optional failure')
                else:
                    optional_path.unlink(missing_ok=True)
                with mock.patch.object(
                        self.dp, '_submit_apply_job') as submit, \
                        mock.patch.object(
                            self.dp, '_apply_app_project_compose') as apply, \
                        mock.patch.object(
                            dataplane.subprocess, 'run') as run:
                    with self.assertRaisesRegex(
                            RuntimeError, 'migration is in progress'):
                        self.dp._restart_instance({
                            'instance_uuid': 'synthetic-app'})

                submit.assert_not_called()
                apply.assert_not_called()
                run.assert_not_called()
                if optional_marker:
                    self.assertTrue(optional_path.exists())

    def test_force_retry_clears_only_matching_current_signature(self):
        compose = {'services': {'app': {'image': 'synthetic/app:1'}}}
        project_name = 'reefy-synthetic-app'
        context = self.dp._required_app_failure_context(
            project_name, compose, 'app')
        self.dp._write_failed_sig_path(
            context['path'], context['signature'], 'synthetic failure',
            phase='pull', services=context['services'])

        with mock.patch.object(
                self.dp, '_pull_project_images') as pull, \
                mock.patch.object(self.dp, '_publish_health_status'):
            stale = self.dp._prepare_app_project_compose(
                project_name, compose, 'synthetic-app', 'app',
                force_retry=True, force_retry_signature='stale-signature')
        self.assertFalse(stale['ok'])
        pull.assert_not_called()
        self.assertTrue(os.path.exists(context['path']))

        with mock.patch.object(
                self.dp, '_pull_project_images',
                return_value=(True, '', '')) as pull, \
                mock.patch.object(self.dp, '_publish_health_status'):
            current = self.dp._prepare_app_project_compose(
                project_name, compose, 'synthetic-app', 'app',
                force_retry=True,
                force_retry_signature=context['signature'])
        self.assertTrue(current['ok'])
        pull.assert_called_once()
        self.assertFalse(os.path.exists(context['path']))

    def test_optional_marker_keeps_healthy_primary_only_restart(self):
        compose = {'services': {
            'app': {'image': 'synthetic/app:1'},
            'diagnostics': {
                'image': 'synthetic/diagnostics:1',
                'labels': {'ai.reefy.optional': 'true'},
            },
        }}
        self._write_v2_state(compose)
        compose_path = self.dp._write_project_compose(
            'reefy-synthetic-app', compose)
        optional_path = os.path.join(
            os.path.dirname(compose_path),
            '.failed-compose-sig.optional-diagnostics')
        Path(optional_path).write_text(
            'optional-signature\nsynthetic optional failure')
        result = mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(
                dataplane.subprocess, 'run', return_value=result) as run, \
                mock.patch.object(
                    self.dp, '_apply_app_project_compose') as recovery:
            self.dp._restart_instance({'instance_uuid': 'synthetic-app'})

        recovery.assert_not_called()
        compose_runs = [
            call.args[0] for call in run.call_args_list
            if call.args and call.args[0] and call.args[0][0] == 'docker']
        self.assertEqual(compose_runs[-1], [
            'docker', 'compose', '-f', compose_path, '-p',
            'reefy-synthetic-app', 'up', '-d', '--force-recreate',
            '--no-deps', 'app'])
        self.assertTrue(os.path.exists(optional_path))

    def test_required_failure_phases_publish_one_terminal_failed(self):
        for phase in ('pull', 'init', 'start'):
            with self.subTest(phase=phase):
                project_name = f'reefy-synthetic-{phase}'
                compose = {'services': {
                    'app': {'image': 'synthetic/app:1'},
                }}
                if phase == 'init':
                    compose['services']['setup'] = {
                        'image': 'synthetic/setup:1',
                        'labels': {'ai.reefy.lifecycle': 'init'},
                    }

                def pull(*_args, **_kwargs):
                    if phase == 'pull':
                        return False, 'manifest unknown', (
                            'image not found or access denied')
                    return True, '', ''

                def run(_path, _project, args, timeout):
                    if phase == 'init' and args[0] == 'run':
                        return False, 'synthetic init failure'
                    return True, ''

                def start(*_args, **_kwargs):
                    if phase == 'start':
                        return False, 'synthetic start failure', (
                            'docker compose up failed')
                    return True, '', ''

                with mock.patch.object(
                        self.dp, '_pull_project_images', side_effect=pull), \
                        mock.patch.object(
                            self.dp, '_run_compose_command', side_effect=run), \
                        mock.patch.object(
                            self.dp, '_start_project_services',
                            side_effect=start), \
                        mock.patch.object(
                            self.dp, '_publish_health_status') as health:
                    result = self.dp._apply_app_project_compose(
                        project_name, compose, 'synthetic-app', 'app')

                self.assertEqual(result[0], False)
                statuses = self._health_statuses(health)
                self.assertEqual(statuses.count('failed'), 1)
                self.assertEqual(statuses.count('running'), 0)
                self.assertEqual(statuses[-1], 'failed')

    def test_optional_failure_success_has_one_running_and_no_failed(self):
        compose = {'services': {
            'app': {'image': 'synthetic/app:1'},
            'diagnostics': {
                'image': 'synthetic/diagnostics:1',
                'labels': {'ai.reefy.optional': 'true'},
            },
        }}

        def pull(_path, _project, services, _deadline, **_kwargs):
            if services == ['diagnostics']:
                return False, 'manifest unknown', (
                    'image not found or access denied')
            return True, '', ''

        with mock.patch.object(
                self.dp, '_pull_project_images', side_effect=pull), \
                mock.patch.object(
                    self.dp, '_start_project_services',
                    return_value=(True, '', '')), \
                mock.patch.object(
                    self.dp, '_publish_health_status') as health:
            result = self.dp._apply_app_project_compose(
                'reefy-synthetic-app', compose, 'synthetic-app', 'app')

        self.assertEqual(result, (True, ['diagnostics']))
        statuses = self._health_statuses(health)
        self.assertEqual(statuses.count('running'), 1)
        self.assertEqual(statuses.count('failed'), 0)
        self.assertEqual(statuses[-1], 'running')

    def test_sticky_skip_publishes_failed_without_starting(self):
        compose = {'services': {'app': {'image': 'synthetic/app:1'}}}
        project_name = 'reefy-synthetic-app'
        context = self.dp._required_app_failure_context(
            project_name, compose, 'app')
        self.dp._write_failed_sig_path(
            context['path'], context['signature'], 'synthetic failure',
            phase='pull', services=context['services'])

        with mock.patch.object(
                self.dp, '_pull_project_images') as pull, \
                mock.patch.object(
                    self.dp, '_publish_health_status') as health:
            result = self.dp._apply_app_project_compose(
                project_name, compose, 'synthetic-app', 'app')

        self.assertEqual(result, (False, []))
        pull.assert_not_called()
        self.assertEqual(self._health_statuses(health), ['failed'])

    def test_multiple_init_progress_events_end_in_one_running(self):
        compose = {'services': {
            'first': {
                'image': 'synthetic/first:1',
                'labels': {'ai.reefy.lifecycle': 'init'},
            },
            'second': {
                'image': 'synthetic/second:1',
                'labels': {'ai.reefy.lifecycle': 'init'},
                'depends_on': ['first'],
            },
            'app': {'image': 'synthetic/app:1'},
        }}
        with mock.patch.object(
                self.dp, '_pull_project_images',
                return_value=(True, '', '')), \
                mock.patch.object(
                    self.dp, '_run_compose_command',
                    return_value=(True, '')), \
                mock.patch.object(
                    self.dp, '_start_project_services',
                    return_value=(True, '', '')), \
                mock.patch.object(
                    self.dp, '_publish_health_status') as health:
            result = self.dp._apply_app_project_compose(
                'reefy-synthetic-app', compose,
                'synthetic-app', 'app')

        self.assertEqual(result, (True, []))
        statuses = self._health_statuses(health)
        self.assertEqual(statuses.count('starting'), 5)
        self.assertEqual(statuses.count('running'), 1)
        self.assertEqual(statuses.count('failed'), 0)

    def test_different_project_apply_pulls_overlap(self):
        barrier = threading.Barrier(2)
        entered = []
        results = []

        def pull(_path, project_name, *_args, **_kwargs):
            entered.append(project_name)
            barrier.wait(timeout=2)
            return True, '', ''

        def apply(project_name):
            results.append(self.dp._apply_project_compose(
                project_name,
                {'services': {'app': {'image': 'synthetic/app:1'}}}))

        with mock.patch.object(
                self.dp, '_pull_project_images', side_effect=pull), \
                mock.patch.object(
                    self.dp, '_start_project_services',
                    return_value=(True, '', '')):
            threads = [
                threading.Thread(target=apply, args=(project_name,))
                for project_name in (
                    'reefy-synthetic-a', 'reefy-synthetic-b')
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(
            entered, ['reefy-synthetic-a', 'reefy-synthetic-b'])
        self.assertEqual(results, [True, True])

    def test_same_project_apply_lock_serializes_pull_and_start(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        calls = []

        def pull(*_args, **_kwargs):
            calls.append('pull')
            if len(calls) == 1:
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=2))
            return True, '', ''

        results = []

        def apply():
            results.append(self.dp._apply_project_compose(
                'reefy-synthetic-app',
                {'services': {'app': {'image': 'synthetic/app:1'}}}))

        with mock.patch.object(
                self.dp, '_pull_project_images', side_effect=pull), \
                mock.patch.object(
                    self.dp, '_start_project_services',
                    return_value=(True, '', '')):
            first = threading.Thread(target=apply)
            second = threading.Thread(target=apply)
            first.start()
            self.assertTrue(first_entered.wait(timeout=1))
            second.start()
            self.assertEqual(calls, ['pull'])
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, ['pull', 'pull'])
        self.assertEqual(results, [True, True])

    def test_streaming_timeout_keeps_partial_output_and_bounds_tail(self):
        real_popen = dataplane.subprocess.Popen
        script = (
            'import os,time; '
            'os.write(1, b"partial-output-without-newline" + b"x"*20000); '
            'time.sleep(5)')

        def popen(_command, **kwargs):
            return real_popen([sys.executable, '-c', script], **kwargs)

        with mock.patch.object(
                dataplane.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(dataplane, 'log'):
            ok, output = self.dp._run_compose_command_streaming(
                '/synthetic/compose.json', 'reefy-synthetic-app',
                ['pull', '--policy', 'missing', 'app'], timeout=0.5)

        self.assertFalse(ok)
        self.assertIn('partial-output-without-newline', output)
        self.assertIn('[truncated]', output)
        self.assertIn('timed out', output)
        self.assertLess(
            len(output),
            self.dp.COMPOSE_OUTPUT_TAIL_LINES
            * (self.dp.COMPOSE_OUTPUT_LINE_CHARS + 100))

    def test_streaming_truncation_redacts_boundary_spanning_secret(self):
        real_popen = dataplane.subprocess.Popen
        marker = ' [truncated]'
        retained = self.dp.COMPOSE_OUTPUT_LINE_CHARS - len(marker)
        secret = 'synthetic-boundary-secret-remainder'
        line = (
            'x' * (retained - len(' password=') - 4)
            + ' password=' + secret)
        script = (
            'import os; '
            f'os.write(1, {line.encode()!r})')

        def popen(_command, **kwargs):
            return real_popen([sys.executable, '-c', script], **kwargs)

        with mock.patch.object(
                dataplane.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(dataplane, 'log') as logger:
            ok, output = self.dp._run_compose_command_streaming(
                '/synthetic/compose.json', 'reefy-synthetic-app',
                ['pull', '--policy', 'missing', 'app'], timeout=5)

        self.assertTrue(ok)
        logged = '\n'.join(
            str(call.args[1]) for call in logger.call_args_list)
        for rendered in (logged, output):
            self.assertIn('password=[REDACTED]', rendered)
            self.assertIn('[truncated]', rendered)
            self.assertNotIn(secret, rendered)
            self.assertNotIn('boundary-secret-remainder', rendered)

    def test_streaming_preserves_early_failure_classification_signals(self):
        real_popen = dataplane.subprocess.Popen
        cases = (
            ('manifest unknown', 'image_missing'),
            ('no space left on device', 'no_space'),
        )
        for signal_text, expected in cases:
            script = (
                'import sys; '
                f'print({signal_text!r}); '
                '[print(f"progress {index}") for index in range(300)]; '
                'sys.exit(1)')

            def popen(_command, **kwargs):
                return real_popen([sys.executable, '-c', script], **kwargs)

            with self.subTest(signal=signal_text), \
                    mock.patch.object(
                        dataplane.subprocess, 'Popen', side_effect=popen), \
                    mock.patch.object(dataplane, 'log'):
                ok, output = self.dp._run_compose_command_streaming(
                    '/synthetic/compose.json', 'reefy-synthetic-app',
                    ['pull', '--policy', 'missing', 'app'], timeout=5)

            self.assertFalse(ok)
            self.assertIn(signal_text, output)
            self.assertEqual(
                self.dp._classify_compose_failure(output), expected)
            self.assertLessEqual(
                len(output.splitlines()),
                self.dp.COMPOSE_OUTPUT_TAIL_LINES + 1)

    def test_streaming_classifies_signal_after_truncated_cr_progress(self):
        real_popen = dataplane.subprocess.Popen
        script = (
            'import os,sys; '
            'os.write(1, b"x"*5000 + b"\\rmanifest unknown\\r"); '
            'sys.exit(1)')

        def popen(_command, **kwargs):
            return real_popen([sys.executable, '-c', script], **kwargs)

        with mock.patch.object(
                dataplane.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(dataplane, 'log'):
            ok, output = self.dp._run_compose_command_streaming(
                '/synthetic/compose.json', 'reefy-synthetic-app',
                ['pull', '--policy', 'missing', 'app'], timeout=5)

        self.assertFalse(ok)
        self.assertIn('manifest unknown', output)
        self.assertEqual(
            self.dp._classify_compose_failure(output), 'image_missing')
        self.assertLessEqual(
            len(output),
            self.dp.COMPOSE_OUTPUT_TAIL_LINES
            * (self.dp.COMPOSE_OUTPUT_LINE_CHARS + 100))

    def test_classified_reason_is_kept_in_published_bounded_tail(self):
        output = '\n'.join([
            'manifest unknown',
            *[f'progress {index}' for index in range(300)],
        ])
        rendered = self.dp._bounded_output_tail(
            output, 'image not found or access denied')
        self.assertEqual(
            rendered.splitlines()[0],
            'image not found or access denied')
        self.assertLessEqual(len(rendered.splitlines()), 5)

    def test_process_group_termination_escalates_and_waits(self):
        proc = mock.Mock(pid=43210)
        proc.wait.side_effect = [
            dataplane.subprocess.TimeoutExpired(['docker'], 5), None]
        with mock.patch.object(dataplane.os, 'killpg') as killpg:
            self.dp._terminate_compose_process(proc)

        self.assertEqual(killpg.call_args_list, [
            mock.call(43210, dataplane.signal.SIGTERM),
            mock.call(43210, dataplane.signal.SIGKILL),
        ])
        self.assertEqual(proc.wait.call_count, 2)


class EventPublishingTests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()

    def test_publish_event_shells_to_reefy_mqtt_pub(self):
        with mock.patch.object(dataplane.subprocess, 'run') as run:
            self.dp._publish_event('stage', {'stage': 'ready'})
        args = run.call_args[0][0]
        self.assertEqual(args[0], 'reefy-mqtt-pub')
        self.assertEqual(args[1], 'stage')
        self.assertEqual(json.loads(args[2])['stage'], 'ready')

    def test_publish_event_is_non_fatal(self):
        # A publish failure must never propagate (that was the original
        # crash that aborted apply/restore work).
        with mock.patch.object(dataplane.subprocess, 'run',
                               side_effect=OSError('boom')):
            self.dp._publish_event('stage', {'stage': 'x'})  # must not raise

    def test_restore_status_payload(self):
        with mock.patch.object(self.dp, '_publish_event') as pe:
            self.dp._publish_restore_status('inst1', 'success', 'arch1')
        suffix, payload = pe.call_args[0]
        self.assertEqual(suffix, 'instance/status')
        self.assertEqual(payload, {'instance_uuid': 'inst1', 'action': 'restore',
                                   'status': 'success', 'archive': 'arch1'})

    def test_health_status_payload(self):
        with mock.patch.object(self.dp, '_publish_event') as pe:
            self.dp._publish_health_status('inst1', 'failed', message='oops')
        suffix, payload = pe.call_args[0]
        self.assertEqual(suffix, 'instance/status')
        self.assertEqual(payload['action'], 'health')
        self.assertEqual(payload['status'], 'failed')
        self.assertEqual(payload['message'], 'oops')
        self.assertNotIn('image', payload, 'no image key unless reported')

    def test_health_status_payload_with_image(self):
        with mock.patch.object(self.dp, '_publish_event') as pe:
            self.dp._publish_health_status('inst1', 'running',
                                           image='ghcr.io/x/app:1')
        _, payload = pe.call_args[0]
        self.assertEqual(payload['status'], 'running')
        self.assertEqual(payload['image'], 'ghcr.io/x/app:1')

    def test_send_command_response_is_noop(self):
        # cmd_id is always None over Varlink; must be a no-op (no client).
        self.assertIsNone(self.dp._send_command_response(None, status='running'))

    def test_stage_message_is_redacted_before_publish(self):
        secret = 'synthetic-stage-secret'
        with mock.patch.object(self.dp, '_publish_event') as publish:
            self.dp._publish_stage('error', f'password={secret}')
        payload = publish.call_args.args[1]
        self.assertNotIn(secret, payload['message'])
        self.assertIn('[REDACTED]', payload['message'])


class SensitiveFailureLoggingTests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()

    def test_wifi_timeout_does_not_log_password_from_command_argv(self):
        secret = 'synthetic-wifi-password'
        failure = dataplane.subprocess.TimeoutExpired(
            ['wifi-setup', 'sample-network', secret], 30)
        messages = []
        with mock.patch.object(
                dataplane.subprocess, 'run', side_effect=failure), \
                mock.patch.object(
                    dataplane, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            self.dp._apply_wifi({
                'ssid': 'sample-network',
                'password': secret,
            })

        rendered = '\n'.join(messages)
        self.assertNotIn(secret, rendered)
        self.assertIn('WiFi setup timed out', rendered)

    def test_wifi_failure_does_not_log_command_output(self):
        secret = 'synthetic-wifi-output-secret'
        failure = types.SimpleNamespace(
            returncode=17, stdout=f'unlabelled {secret}', stderr='')
        messages = []
        with mock.patch.object(
                dataplane.subprocess, 'run', return_value=failure), \
                mock.patch.object(
                    dataplane, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            self.dp._apply_wifi({
                'ssid': 'sample-network',
                'password': 'synthetic-password',
            })

        rendered = '\n'.join(messages)
        self.assertNotIn(secret, rendered)
        self.assertIn('WiFi setup failed (exit 17)', rendered)

    def test_varlink_error_is_redacted_before_return(self):
        secret = 'synthetic-varlink-secret'
        with mock.patch.object(
                self.dp, '_submit_apply_job',
                side_effect=RuntimeError(f'access_token={secret}')):
            result = self.dp._dp_submit_apply('{"synthetic": true}')

        self.assertFalse(result['ok'])
        self.assertNotIn(secret, result['error'])
        self.assertIn('[REDACTED]', result['error'])


class RunDataPlaneWiringTests(unittest.TestCase):
    """Regression for the Varlink handler binding: a class body cannot see
    run_data_plane's local `service`, so `class _Handler: service = service`
    raised NameError and the data plane never served (device stuck adopting)."""

    def test_run_data_plane_wires_handler_without_nameerror(self):
        import sys
        import types as _types

        served = {}

        fake = _types.ModuleType('varlink')

        class _FakeService:
            def __init__(self, **kw):
                pass

            def interface(self, name):
                def deco(cls):
                    return cls
                return deco

        class _FakeRequestHandler:
            pass

        class _FakeThreadingServer:
            def __init__(self, addr, handler):
                served['addr'] = addr
                served['handler'] = handler

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def serve_forever(self):
                served['served'] = True
                raise KeyboardInterrupt  # stop the otherwise-infinite serve

        fake.Service = _FakeService
        fake.RequestHandler = _FakeRequestHandler
        fake.ThreadingServer = _FakeThreadingServer

        sys.modules['varlink'] = fake
        try:
            dp = _make_dp()
            with mock.patch.object(dataplane.os, 'makedirs'), \
                    self.assertRaises(KeyboardInterrupt):
                dp.run_data_plane()
        finally:
            del sys.modules['varlink']

        self.assertTrue(served.get('served'), 'serve_forever was never reached')
        # The handler must carry the service (the bug NameError'd before this).
        self.assertIs(served['handler'].service.__class__, _FakeService)

    def test_varlink_contract_keeps_legacy_and_async_methods(self):
        usr_dir = Path(dataplane.__file__).parents[3]
        interface = (
            usr_dir / 'share' / 'varlink' / 'io.reefy.Reconciler.varlink'
        ).read_text()
        for method in (
                'SubmitApply', 'SubmitReconcile', 'GetApply', 'WaitApply',
                'ApplyState', 'Reconcile'):
            self.assertIn(f'method {method}', interface)
        self.assertIn('warnings: []ApplyWarning', interface)


class ApplyPathTests(unittest.TestCase):
    """Drive the full data-side submit/result path
    through the scheduler -> _apply_state -> _apply_desired_state
    with workers + syscalls mocked. This is the local stand-in for the
    e2e golden_path's reconcile step: it executes the exact orchestration
    that crashed at runtime before (mode check, class-scope, dispatch),
    catching that whole class of never-run-code bugs in milliseconds."""

    REPRESENTATIVE_STATE = {
        'hostname': 'reefy-test',
        'wifi': None,
        'storage': None,
        'network': None,
        'user_ssh_keys': ['ssh-ed25519 AAAAC3Nz'],
        'instances': [{'uuid': 'i1', 'name': 'app1'}],
        'app_volumes': [{'host_path': '/mnt/reefy-data/apps/i1/data'}],
        'backup': {'instances': [
            {'instance_uuid': 'i1', 'paths': ['/mnt/reefy-data/apps/i1/data']}]},
        'files': [],
        'compose': {'services': {'i1': {'image': 'x'}, 'i1-tty': {'image': 'y'}}},
        'volume_caps': {'/mnt/reefy-data/apps/i1/media': 90},
    }

    def test_running_apply_finishes_and_only_latest_pending_state_runs(self):
        dp = _make_dp()
        started = threading.Event()
        release = threading.Event()
        applied = []

        def apply(payload):
            sequence = payload['state']['sequence']
            applied.append(sequence)
            if sequence == 'active':
                started.set()
                self.assertTrue(release.wait(timeout=5))
            return True

        with mock.patch.object(dp, '_apply_state', side_effect=apply):
            active = dp._submit_apply_job(
                'apply', state={'sequence': 'active'})
            self.assertTrue(started.wait(timeout=5))
            obsolete = dp._submit_apply_job(
                'apply', state={'sequence': 'obsolete'})
            latest = dp._submit_apply_job(
                'apply', state={'sequence': 'latest'})
            obsolete_result = dp._wait_apply_result(obsolete['request_id'])
            self.assertEqual(obsolete_result['status'], 'superseded')
            release.set()
            active_result = dp._wait_apply_result(active['request_id'])
            latest_result = dp._wait_apply_result(latest['request_id'])

        self.assertEqual(active_result['status'], 'succeeded')
        self.assertEqual(latest_result['status'], 'succeeded')
        self.assertEqual(applied, ['active', 'latest'])

    def test_pending_state_runs_after_active_state_fails(self):
        dp = _make_dp()
        started = threading.Event()
        release = threading.Event()
        applied = []

        def apply(payload):
            sequence = payload['state']['sequence']
            applied.append(sequence)
            if sequence == 'active':
                started.set()
                self.assertTrue(release.wait(timeout=5))
                return False
            return True

        with mock.patch.object(dp, '_apply_state', side_effect=apply):
            active = dp._submit_apply_job(
                'apply', state={'sequence': 'active'})
            self.assertTrue(started.wait(timeout=5))
            pending = dp._submit_apply_job(
                'apply', state={'sequence': 'pending'})
            release.set()
            active_result = dp._wait_apply_result(active['request_id'])
            pending_result = dp._wait_apply_result(pending['request_id'])

        self.assertEqual(active_result['status'], 'failed')
        self.assertEqual(pending_result['status'], 'succeeded')
        self.assertEqual(applied, ['active', 'pending'])

    def test_wait_for_idle_retry_does_not_supersede_newer_pending_apply(self):
        dp = _make_dp()
        active_started = threading.Event()
        release_active = threading.Event()
        pending_started = threading.Event()
        release_pending = threading.Event()
        order = []
        retry_submission = []

        def apply(payload):
            sequence = payload['state']['sequence']
            order.append(sequence)
            if sequence == 'active':
                active_started.set()
                self.assertTrue(release_active.wait(timeout=5))
            if sequence == 'newer':
                pending_started.set()
                self.assertTrue(release_pending.wait(timeout=5))
            return True

        def reconcile(old_state=None, force_retry=None):
            order.append(('retry', force_retry))
            return True

        def submit_retry():
            submission = dp._submit_apply_job(
                'reconcile',
                force_retry={
                    'project_name': 'reefy-synthetic-app',
                    'signature': 'synthetic-signature',
                },
                wait_for_idle=True)
            retry_submission.append(submission)

        with mock.patch.object(dp, '_apply_state', side_effect=apply), \
                mock.patch.object(
                    dp, '_apply_desired_state', side_effect=reconcile):
            active = dp._submit_apply_job(
                'apply', state={'sequence': 'active'})
            self.assertTrue(active_started.wait(timeout=5))
            newer = dp._submit_apply_job(
                'apply', state={'sequence': 'newer'})
            retry_thread = threading.Thread(target=submit_retry)
            retry_thread.start()
            self.assertEqual(retry_submission, [])

            release_active.set()
            self.assertTrue(pending_started.wait(timeout=5))
            self.assertEqual(
                dp._get_apply_result(newer['request_id'])['status'],
                'running')
            self.assertEqual(retry_submission, [])
            release_pending.set()
            retry_thread.join(timeout=5)

            active_result = dp._wait_apply_result(active['request_id'])
            newer_result = dp._wait_apply_result(newer['request_id'])
            retry_result = dp._wait_apply_result(
                retry_submission[0]['request_id'])

        self.assertFalse(retry_thread.is_alive())
        self.assertEqual(active_result['status'], 'succeeded')
        self.assertEqual(newer_result['status'], 'succeeded')
        self.assertEqual(retry_result['status'], 'succeeded')
        self.assertEqual(order, [
            'active',
            'newer',
            ('retry', {
                'project_name': 'reefy-synthetic-app',
                'signature': 'synthetic-signature',
            }),
        ])

    def test_unknown_result_returns_found_false_without_waiting(self):
        dp = _make_dp()
        expected = {
            'found': False,
            'request_id': '',
            'status': '',
            'error': '',
            'warnings': [],
            'applied': False,
        }
        self.assertEqual(dp._get_apply_result('synthetic-missing'), expected)
        self.assertEqual(dp._wait_apply_result('synthetic-missing'), expected)

    def test_submit_rejects_request_when_initial_record_cannot_persist(self):
        dp = _make_dp()
        with mock.patch.object(
                dp._apply_results, 'create', return_value=False), \
                mock.patch.object(threading, 'Thread') as thread:
            result = dp._submit_apply_job(
                'apply', state={'sequence': 'synthetic'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['request_id'], '')
        thread.assert_not_called()

    def _apply(self, dp, warnings=None):
        """Submit an apply with every worker/storage/host call mocked,
        a temp desired-state path, and return (result, mocks)."""
        tmpdir = tempfile.mkdtemp()
        dp.DESIRED_STATE_PATH = os.path.join(tmpdir, 'desired-state.json')
        patches = {
            '_apply_wifi': mock.DEFAULT, '_apply_network': mock.DEFAULT,
            '_apply_user_ssh_keys': mock.DEFAULT, '_sync_app_users': mock.DEFAULT,
            '_apply_backup_config': mock.DEFAULT, '_apply_files': mock.DEFAULT,
            '_apply_storage': mock.DEFAULT,
        }
        with mock.patch.multiple(dp, **patches) as m, \
                mock.patch.object(dp, '_restore_instances', return_value=set()), \
                mock.patch.object(dp, '_apply_compose', return_value=True) as m_compose, \
                mock.patch.object(dp._storage, 'set_volume_caps') as m_caps, \
                mock.patch.object(
                    dp._storage, '_prepare_app_dirs',
                    return_value=warnings or []) as m_dirs, \
                mock.patch.object(
                    dp._storage, '_reclaim_deleted_instance_lvs') as m_reclaim, \
                mock.patch.object(shared, 'set_hostname') as m_host, \
                mock.patch.object(shared, 'get_default_hostname',
                                  return_value='def-host'):
            submission = dp._dp_submit_apply(
                json.dumps(self.REPRESENTATIVE_STATE))
            res = dp._wait_apply_result(submission['request_id'])
        return res, {**m, 'compose': m_compose, 'caps': m_caps,
                     'dirs': m_dirs, 'host': m_host,
                     'reclaim': m_reclaim}

    def test_apply_state_succeeds_end_to_end(self):
        res, _ = self._apply(_make_dp())
        self.assertEqual(res['status'], 'succeeded')

    def test_apply_dispatches_each_section(self):
        res, m = self._apply(_make_dp())
        self.assertEqual(res['status'], 'succeeded')
        m['host'].assert_called_with('reefy-test')          # hostname applied
        m['caps'].assert_called()                           # caps pushed to storage
        m['_apply_user_ssh_keys'].assert_called_once_with(['ssh-ed25519 AAAAC3Nz'])
        m['_sync_app_users'].assert_called_once_with([{'uuid': 'i1', 'name': 'app1'}])
        m['dirs'].assert_called_once()                      # app dirs prepared
        m['compose'].assert_called_once()                   # compose applied

    def test_cap_warnings_are_returned_with_affected_volumes(self):
        warnings = [
            {
                'code': 'storage.cap_not_enforced',
                'instance_uuid': 'synthetic-app',
                'volume': 'media',
            },
        ]
        res, mocks = self._apply(_make_dp(), warnings=warnings)
        self.assertEqual(res['status'], 'succeeded_with_warnings')
        self.assertEqual(res['warnings'], warnings)
        mocks['compose'].assert_called_once()

    def test_desired_state_log_contains_only_allowlisted_counts(self):
        import tempfile

        dp = _make_dp()
        dp.DESIRED_STATE_PATH = os.path.join(
            tempfile.mkdtemp(), 'desired-state.json')
        canaries = [
            'opaque-env-value-synthetic',
            'wifi-passphrase-synthetic',
            'backup-secret-synthetic',
            'file-content-synthetic',
        ]
        state = {
            'hostname': 'sample-node',
            'wifi': {'password': canaries[1]},
            'instances': [{'instance_uuid': 'sample-instance'}],
            'app_volumes': [{'path': '/mnt/reefy-data/apps/sample/data'}],
            'backup': {'passphrase': canaries[2], 'instances': [{}]},
            'files': [{'content_b64': canaries[3]}],
            'storage': {'devices': ['sample-disk']},
            'compose': {'services': {
                'sample-instance': {
                    'environment': {'PLANET_COLOR': canaries[0]},
                },
            }},
            'synthetic_extension': {'nested': canaries[0]},
        }
        messages = []
        with mock.patch.object(
                dp, '_apply_desired_state', return_value=True), \
                mock.patch.object(
                    dataplane, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            self.assertTrue(dp._apply_state({'state': state}))

        with open(dp.DESIRED_STATE_PATH) as handle:
            self.assertEqual(json.load(handle), state)
        joined = '\n'.join(messages)
        self.assertIn(
            'Saved desired state (instances=1, services=1, app_volumes=1, '
            'files=1, storage_devices=1, backup_instances=1)', joined)
        self.assertFalse(
            any(canary in joined for canary in canaries),
            'desired-state summary exposed a synthetic secret value')
        self.assertNotIn('sample-node', joined)
        self.assertNotIn('/mnt/reefy-data/apps/sample/data', joined)

    def test_desired_state_summary_tolerates_wrong_optional_types(self):
        summary = dataplane._desired_state_log_summary({
            'instances': {},
            'app_volumes': 'wrong',
            'files': None,
            'storage': {'devices': 'wrong'},
            'backup': [],
            'compose': {'services': []},
        })
        self.assertEqual(
            summary,
            'Saved desired state (instances=0, services=0, app_volumes=0, '
            'files=0, storage_devices=0, backup_instances=0)')

    def test_storage_exception_is_live_but_persistent_record_is_generic(self):
        import tempfile
        dp = _make_dp()
        dp.DESIRED_STATE_PATH = os.path.join(
            tempfile.mkdtemp(), 'desired-state.json')
        state = dict(self.REPRESENTATIVE_STATE)
        state['storage'] = {'devices': ['sda', 'sdb']}
        error = (
            'No safe common LUKS sector size for selected devices '
            'required=4096: /dev/sdb logical=512 physical=512')

        with mock.patch.object(dp, '_apply_wifi'), \
                mock.patch.object(
                    dp, '_apply_storage', side_effect=RuntimeError(error)), \
                mock.patch.object(dp._storage, 'set_volume_caps'), \
                mock.patch.object(shared, 'set_hostname'), \
                mock.patch.object(
                    shared, 'get_default_hostname', return_value='d'):
            res = dp._dp_apply_state(json.dumps(state))

        self.assertFalse(res['ok'])
        self.assertEqual(res['error'], error)
        result_files = os.listdir(dp._apply_results.directory)
        self.assertEqual(len(result_files), 1)
        with open(os.path.join(
                dp._apply_results.directory, result_files[0])) as handle:
            persisted = json.load(handle)
        self.assertNotIn('/dev/sdb', json.dumps(persisted))
        self.assertNotIn('sda', json.dumps(persisted))

    def test_compose_failure_publishes_error_stage(self):
        import tempfile
        dp = _make_dp()
        dp.DESIRED_STATE_PATH = os.path.join(tempfile.mkdtemp(), 'ds.json')
        with mock.patch.multiple(
                dp, _apply_wifi=mock.DEFAULT, _apply_network=mock.DEFAULT,
                _apply_user_ssh_keys=mock.DEFAULT, _sync_app_users=mock.DEFAULT,
                _apply_backup_config=mock.DEFAULT, _apply_files=mock.DEFAULT,
                _apply_storage=mock.DEFAULT), \
                mock.patch.object(dp, '_restore_instances', return_value=set()), \
                mock.patch.object(dp, '_apply_compose', return_value=False), \
                mock.patch.object(dp, '_publish_event') as m_emit, \
                mock.patch.object(dp._storage, 'set_volume_caps'), \
                mock.patch.object(dp._storage, '_prepare_app_dirs'), \
                mock.patch.object(
                    dp._storage, '_reclaim_deleted_instance_lvs'), \
                mock.patch.object(shared, 'set_hostname'), \
                mock.patch.object(shared, 'get_default_hostname', return_value='d'):
            res = dp._dp_apply_state(json.dumps(self.REPRESENTATIVE_STATE))
        # The data plane emits a stage=error event on compose failure.
        stages = [c.args[1].get('stage')
                  for c in m_emit.call_args_list if c.args and c.args[0] == 'stage']
        self.assertIn('error', stages)
        # The Varlink result must preserve the apply failure so control does
        # not publish ready after the data plane emitted error.
        self.assertFalse(res['ok'])
        self.assertIn('apply failed', res['error'])

    def _apply_custom(self, dp, state):
        """Run _dp_apply_state on a caller-supplied state with the same
        worker/host mocks as _apply, plus a _publish_event spy. Returns
        (result, compose_mock, emit_mock)."""
        import tempfile
        dp.DESIRED_STATE_PATH = os.path.join(tempfile.mkdtemp(), 'ds.json')
        with mock.patch.multiple(
                dp, _apply_wifi=mock.DEFAULT, _apply_network=mock.DEFAULT,
                _apply_user_ssh_keys=mock.DEFAULT, _sync_app_users=mock.DEFAULT,
                _apply_backup_config=mock.DEFAULT, _apply_files=mock.DEFAULT,
                _apply_storage=mock.DEFAULT), \
                mock.patch.object(dp, '_restore_instances', return_value=set()), \
                mock.patch.object(dp, '_apply_compose', return_value=True) as m_compose, \
                mock.patch.object(dp, '_publish_event') as m_emit, \
                mock.patch.object(dp._storage, 'set_volume_caps'), \
                mock.patch.object(dp._storage, '_prepare_app_dirs'), \
                mock.patch.object(
                    dp._storage, '_reclaim_deleted_instance_lvs'), \
                mock.patch.object(shared, 'set_hostname'), \
                mock.patch.object(shared, 'get_default_hostname', return_value='d'):
            res = dp._dp_apply_state(json.dumps(state))
        return res, m_compose, m_emit

    def test_inconsistent_state_refuses_apply(self):
        """A registered instance missing from compose services (the
        backend catalog-gap bug) must NEVER reach `docker compose up
        --remove-orphans` - that would delete the still-running
        container. The apply aborts with an error stage first."""
        state = {
            'hostname': 'reefy-test', 'wifi': None, 'storage': None,
            'network': None, 'user_ssh_keys': [], 'app_volumes': [],
            'backup': {}, 'files': [],
            'instances': [{'instance_uuid': 'i1', 'instance_name': 'qr',
                           'app_slug': 'qr-access', 'uid': 0}],
            # compose has only infra, NO service keyed 'i1'
            'compose': {'services': {'reefy-proxy': {'image': 'p'}}},
        }
        _, m_compose, m_emit = self._apply_custom(_make_dp(), state)
        m_compose.assert_not_called()
        stages = [c.args[1].get('stage') for c in m_emit.call_args_list
                  if c.args and c.args[0] == 'stage']
        self.assertIn('error', stages)

    def test_consistent_state_reaches_compose(self):
        """Counterpart: every registered instance has its service, so the
        apply proceeds to _apply_compose (no false-positive refusal)."""
        state = {
            'hostname': 'reefy-test', 'wifi': None, 'storage': None,
            'network': None, 'user_ssh_keys': [], 'app_volumes': [],
            'backup': {}, 'files': [],
            'instances': [{'instance_uuid': 'i1', 'instance_name': 'qr',
                           'app_slug': 'qr-access', 'uid': 0}],
            'compose': {'services': {'i1': {'image': 'x'}}},
        }
        _, m_compose, _ = self._apply_custom(_make_dp(), state)
        m_compose.assert_called_once()


class ComposeRetryPolicyTests(unittest.TestCase):
    """_apply_compose: fail-fast on deterministic failures (a) + sticky
    terminal-failure guard so the reconcile loop stops re-pulling an
    unchanged, already-failed compose (b)."""

    GOOD = {'services': {'i1': {'image': 'ghcr.io/x/app:1'}}}

    def _mkdp(self):
        """A DataPlane whose compose + sticky-sig paths live in a fresh
        temp dir, so the disk-backed sticky guard persists across _run
        calls on the same dp (as it would across a real restart)."""
        import tempfile
        dp = _make_dp()
        d = tempfile.mkdtemp()
        dp.COMPOSE_PATH = os.path.join(d, 'docker-compose.json')
        dp._FAILED_SIG_PATH = os.path.join(d, '.failed-compose-sig')
        return dp

    def _run(self, dp, compose, output, rc, prune_reclaims=True):
        """Run _apply_compose with `docker compose up` mocked to emit
        `output` + exit `rc` on EVERY attempt (real file I/O for the
        compose + sticky-sig files). `prune_reclaims` is the mocked
        _prune_docker return (did prune free space?). Returns
        (result, n_compose_up_calls, prune_mock, health_mock)."""
        import io
        n = {'c': 0}

        def fake_popen(cmd, **kw):
            n['c'] += 1
            m = mock.MagicMock()
            m.stdout = io.StringIO(output + '\n')
            m.returncode = rc
            return m

        with mock.patch.object(dataplane.subprocess, 'Popen', side_effect=fake_popen), \
                mock.patch.object(dp, '_prune_docker', return_value=prune_reclaims) as m_prune, \
                mock.patch.object(dp, '_publish_health_status') as m_health, \
                mock.patch.object(dataplane.time, 'sleep'), \
                mock.patch.object(dataplane.shared, 'instance_uuids_in_compose',
                                  return_value=['i1']):
            res = dp._apply_compose(compose)
        return res, n['c'], m_prune, m_health

    @staticmethod
    def _failed_msgs(m_health):
        return [c.kwargs.get('message', '') for c in m_health.call_args_list
                if len(c.args) >= 2 and c.args[1] == 'failed']

    def test_no_space_prune_freed_nothing_gives_up(self):
        dp = self._mkdp()
        res, n, m_prune, m_health = self._run(
            dp, self.GOOD,
            'failed to register layer: ...: no space left on device', 1,
            prune_reclaims=False)
        self.assertFalse(res)
        self.assertEqual(n, 1, 'prune freed nothing -> give up after 1 attempt')
        m_prune.assert_called_once()
        self.assertTrue(any('space' in m.lower() or 'disk' in m.lower()
                            for m in self._failed_msgs(m_health)))
        self.assertTrue(os.path.exists(dp._FAILED_SIG_PATH),
                        'terminal failure must persist the sticky sig')

    def test_no_space_prune_freed_space_retries_once(self):
        dp = self._mkdp()
        res, n, m_prune, _ = self._run(
            dp, self.GOOD, 'no space left on device', 1, prune_reclaims=True)
        self.assertFalse(res)
        self.assertEqual(n, 2, 'prune freed space -> one retry, then give up')
        m_prune.assert_called_once()

    def test_image_missing_fails_after_one_attempt(self):
        res, n, m_prune, _ = self._run(
            self._mkdp(), self.GOOD,
            'app:badtag: manifest unknown: manifest unknown', 1)
        self.assertFalse(res)
        self.assertEqual(n, 1, 'image_missing is non-retryable')
        m_prune.assert_not_called()

    def test_transient_keeps_full_retry_budget(self):
        res, n, _, _ = self._run(
            self._mkdp(), self.GOOD, 'dial tcp: i/o timeout', 1)
        self.assertFalse(res)
        self.assertEqual(n, 5, 'transient errors keep retrying')

    def test_sticky_skips_repull_until_state_changes(self):
        dp = self._mkdp()
        res1, n1, _, _ = self._run(dp, self.GOOD, 'no space left on device', 1)
        self.assertFalse(res1)
        self.assertEqual(n1, 2)
        self.assertTrue(os.path.exists(dp._FAILED_SIG_PATH))
        # same compose again -> skipped entirely (reads the persisted sig)
        res2, n2, _, m_health2 = self._run(dp, self.GOOD, 'unused', 0)
        self.assertFalse(res2)
        self.assertEqual(n2, 0, 'unchanged failed compose must not re-pull')
        self.assertTrue(self._failed_msgs(m_health2), 'still surfaces failed')
        # a CHANGED compose is re-attempted
        other = {'services': {'i1': {'image': 'ghcr.io/x/app:2'}}}
        res3, n3, _, _ = self._run(dp, other, 'no space left on device', 1)
        self.assertGreaterEqual(n3, 1, 'changed compose must be retried')

    def test_success_clears_sticky_guard(self):
        dp = self._mkdp()
        with open(dp._FAILED_SIG_PATH, 'w') as f:
            f.write('stale-sig-from-another-compose\nold reason')
        res, n, _, _ = self._run(dp, self.GOOD, 'Started i1', 0)
        self.assertTrue(res)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(dp._FAILED_SIG_PATH),
                         'success clears the persisted sticky sig')

    def test_success_reports_running_image(self):
        dp = self._mkdp()
        res, _, _, m_health = self._run(dp, self.GOOD, 'Started i1', 0)
        self.assertTrue(res)
        running = [c for c in m_health.call_args_list
                   if c.args[1] == 'running']
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].kwargs.get('image'), 'ghcr.io/x/app:1',
                         "running event must carry the instance's image")


class ComposeMutationLockTests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()
        state_dir = tempfile.mkdtemp()
        self.dp.COMPOSE_PATH = os.path.join(
            state_dir, 'docker-compose.json')
        Path(self.dp.COMPOSE_PATH).write_text('{}')

    @staticmethod
    def _result(returncode=0, stderr=''):
        return mock.Mock(returncode=returncode, stderr=stderr)

    def test_restart_waits_for_inflight_compose_apply(self):
        apply_entered = threading.Event()
        release_apply = threading.Event()
        restart_entered = threading.Event()
        compose_run = threading.Event()
        errors = []

        def blocking_apply(_compose):
            apply_entered.set()
            if not release_apply.wait(timeout=2):
                raise TimeoutError('test did not release compose apply')
            return True

        def run_apply():
            try:
                self.dp._apply_compose({'services': {}})
            except Exception as error:
                errors.append(error)

        def fake_run(*_args, **_kwargs):
            compose_run.set()
            return self._result()

        def run_restart():
            restart_entered.set()
            try:
                self.dp._restart_instance(
                    {'instance_uuid': 'synthetic-app'}, cmd_id=None)
            except Exception as error:
                errors.append(error)

        with mock.patch.object(
                self.dp, '_apply_compose_locked',
                side_effect=blocking_apply), \
                mock.patch.object(
                    dataplane.subprocess, 'run', side_effect=fake_run):
            apply_thread = threading.Thread(target=run_apply, daemon=True)
            apply_thread.start()
            self.assertTrue(apply_entered.wait(timeout=1))

            restart_thread = threading.Thread(
                target=run_restart, daemon=True)
            restart_thread.start()
            self.assertTrue(restart_entered.wait(timeout=1))
            self.assertFalse(
                compose_run.wait(timeout=0.1),
                'restart must not enter Compose while apply owns the lock')

            release_apply.set()
            self.assertTrue(compose_run.wait(timeout=1))
            apply_thread.join(timeout=1)
            restart_thread.join(timeout=1)

        self.assertFalse(apply_thread.is_alive())
        self.assertFalse(restart_thread.is_alive())
        self.assertEqual(errors, [])

    def test_lock_released_after_restart_failure(self):
        with mock.patch.object(
                dataplane.subprocess, 'run',
                return_value=self._result(1, 'synthetic compose failure')):
            with self.assertRaisesRegex(
                    RuntimeError, 'synthetic compose failure'):
                self.dp._restart_instance(
                    {'instance_uuid': 'synthetic-app'}, cmd_id=None)

        self.assertFalse(self.dp._compose_mutation_lock.locked())

    def test_lock_released_after_apply_failure(self):
        with mock.patch.object(
                self.dp, '_apply_compose_locked',
                side_effect=RuntimeError('synthetic apply failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic apply failure'):
                self.dp._apply_compose({'services': {}})

        self.assertFalse(self.dp._compose_mutation_lock.locked())


class DataSideBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()

    def test_restart_instance_requires_uuid(self):
        with self.assertRaises(ValueError):
            self.dp._restart_instance({}, cmd_id=None)

    def test_config_defaults_when_unprovisioned(self):
        # No mqtt.conf on a dev box -> safe defaults, no crash on construct.
        self.assertEqual(self.dp.port, 443)
        self.assertEqual(self.dp.topic_prefix, 'reefy')


class AppLifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.dp = _make_dp()
        self.tempdir = tempfile.mkdtemp()
        self.dp.DESIRED_STATE_PATH = os.path.join(
            self.tempdir, 'desired-state.json')
        self.dp.DESIRED_STATE_V2_PATH = os.path.join(
            self.tempdir, 'desired-state-v2.json')
        self.dp.PROJECTS_DIR = os.path.join(self.tempdir, 'projects')

    @staticmethod
    def _app(status='running', generation=1):
        return {
            'instance_uuid': 'synthetic-app',
            'project_name': 'reefy-synthetic-app',
            'desired_status': status,
            'desired_generation': generation,
            'primary_service': 'app',
            'compose': {
                'services': {'app': {'image': 'example.invalid/app:1'}},
            },
        }

    def _write_state(self, app):
        Path(self.dp.DESIRED_STATE_V2_PATH).write_text(json.dumps({
            'schema_version': 2,
            'apps': [app],
        }))

    def test_reconcile_health_keeps_its_captured_generation(self):
        old_app = self._app(generation=1)
        self._write_state(self._app(status='stopped', generation=2))

        with self.dp._app_reconcile_context(old_app):
            fields = self.dp._lifecycle_health_fields('synthetic-app')

        self.assertEqual(fields, {
            'desired_status': 'running',
            'observed_generation': 1,
        })

    def test_stopped_target_stops_project_and_publishes_terminal_status(self):
        app = self._app(status='stopped', generation=4)
        self._write_state(app)
        prepared = {
            'ok': True,
            'stopped': True,
            'project_name': app['project_name'],
            'compose': app['compose'],
            'instance_uuid': app['instance_uuid'],
        }

        with mock.patch.object(self.dp, '_write_project_compose'), \
                mock.patch.object(
                    self.dp, '_run_compose_command',
                    return_value=(True, '')) as compose, \
                mock.patch.object(
                    self.dp, '_clear_project_failed_sigs'), \
                mock.patch.object(
                    self.dp, '_publish_health_status') as health:
            result = self.dp._reconcile_v2_app(
                app, migration=False, restore_failed=False,
                prepared=prepared)

        self.assertEqual(result, (True, ''))
        self.assertEqual(compose.call_args.args[2], ['stop'])
        health.assert_called_once_with('synthetic-app', 'stopped')

    def test_restart_rejects_stopped_or_busy_app(self):
        stopped = self._app(status='stopped', generation=2)
        self._write_state(stopped)
        with self.assertRaisesRegex(RuntimeError, 'App is stopped'):
            self.dp._restart_instance({
                'instance_uuid': 'synthetic-app'}, cmd_id='cmd-stopped')

        running = self._app(status='running', generation=3)
        self._write_state(running)
        with mock.patch.object(
                self.dp, '_project_is_busy', return_value=True):
            with self.assertRaisesRegex(
                    RuntimeError, 'operation is in progress'):
                self.dp._restart_instance({
                    'instance_uuid': 'synthetic-app'}, cmd_id='cmd-busy')

    def test_new_generation_interrupts_a_long_streaming_pull(self):
        app = self._app(status='running', generation=1)
        self.dp._set_project_targets({
            'schema_version': 2, 'apps': [app]})
        real_popen = dataplane.subprocess.Popen

        def spawn_sleeper(*_args, **kwargs):
            return real_popen(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                **kwargs)

        timer = threading.Timer(
            0.1,
            lambda: self.dp._set_project_targets({
                'schema_version': 2,
                'apps': [self._app(status='stopped', generation=2)],
            }))
        timer.start()
        started = time.monotonic()
        try:
            with self.dp._app_reconcile_context(app), \
                    mock.patch.object(
                        dataplane.subprocess, 'Popen',
                        side_effect=spawn_sleeper):
                ok, output = self.dp._run_compose_command_streaming(
                    '/synthetic/compose.json', app['project_name'],
                    ['pull', '--policy', 'missing'], timeout=5)
        finally:
            timer.join(timeout=1)

        self.assertFalse(ok)
        self.assertIn(self.dp.APP_RECONCILE_SUPERSEDED, output)
        self.assertLess(time.monotonic() - started, 2)

    def test_operation_id_and_phase_are_in_health_event(self):
        app = self._app(status='running', generation=8)
        self._write_state(app)
        with self.dp._health_operation('synthetic-app', 'cmd-synthetic'), \
                mock.patch.object(
                    self.dp, '_publish_instance_event') as publish:
            self.dp._publish_health_status(
                'synthetic-app', 'starting', phase='pull')

        extra = publish.call_args.kwargs['extra']
        self.assertEqual(extra, {
            'desired_status': 'running',
            'observed_generation': 8,
            'phase': 'pull',
            'operation_id': 'cmd-synthetic',
        })


class ApplyStorageTests(unittest.TestCase):
    """Storage reconciliation must prepare the complete selected disk set
    before handing any mapper to LVM."""

    def setUp(self):
        self.dp = _make_dp()

    @staticmethod
    def _result(returncode=0, stdout='', stderr=''):
        return mock.Mock(
            returncode=returncode, stdout=stdout, stderr=stderr)

    def _unmounted_run(self, luks_devices=(), vg_exists=False):
        luks_devices = set(luks_devices)

        def run(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                return self._result(stdout='')
            if cmd[0] == 'vgs':
                return self._result(returncode=0 if vg_exists else 1)
            if cmd[:2] == ['cryptsetup', 'isLuks']:
                return self._result(
                    returncode=0 if cmd[2] in luks_devices else 1)
            return self._result()

        return run

    def test_mounted_extension_failure_propagates(self):
        mounted_source = f'/dev/{self.dp.STORAGE_VG}/{self.dp.STORAGE_LV}'
        with mock.patch.object(
                dataplane.subprocess, 'run',
                return_value=self._result(stdout=mounted_source)), \
                mock.patch.object(
                    self.dp._storage, '_find_new_storage_disks',
                    return_value=['sdb']), \
                mock.patch.object(
                    self.dp._storage, '_extend_storage',
                    side_effect=RuntimeError('sector mismatch')) as extend:
            with self.assertRaisesRegex(RuntimeError, 'sector mismatch'):
                self.dp._apply_storage({'devices': ['sda', 'sdb']})

        extend.assert_called_once_with(['sdb'])

    def test_mounted_disk_discovery_failure_propagates(self):
        mounted_source = f'/dev/{self.dp.STORAGE_VG}/{self.dp.STORAGE_LV}'
        with mock.patch.object(
                dataplane.subprocess, 'run',
                return_value=self._result(stdout=mounted_source)), \
                mock.patch.object(
                    self.dp._storage, '_find_new_storage_disks',
                    side_effect=RuntimeError('pvs failed')), \
                mock.patch.object(
                    self.dp._storage, '_extend_storage') as extend:
            with self.assertRaisesRegex(RuntimeError, 'pvs failed'):
                self.dp._apply_storage({'devices': ['sda', 'sdb']})

        extend.assert_not_called()

    def test_existing_mapper_constrains_batched_fresh_provisioning(self):
        existing_mapper = '/dev/mapper/reefy-sda'
        fresh_mappers = [
            '/dev/mapper/reefy-sdb',
            '/dev/mapper/reefy-sdc',
        ]
        all_mappers = [existing_mapper, *fresh_mappers]
        existing_paths = {
            '/dev/sda', existing_mapper,
            '/dev/sdb', '/dev/sdc',
        }

        with mock.patch.object(
                dataplane.subprocess, 'run',
                side_effect=self._unmounted_run()) as run, \
                mock.patch.object(
                    dataplane.os.path, 'exists',
                    side_effect=lambda path: path in existing_paths), \
                mock.patch.object(dataplane.os, 'makedirs'), \
                mock.patch.object(
                    self.dp._storage, '_find_reefy_key_partition',
                    return_value='/dev/reefy-key'), \
                mock.patch.object(
                    self.dp._storage, '_require_common_mapper_sector_size',
                    side_effect=[512, 512]) as common_sector, \
                mock.patch.object(
                    self.dp._storage, '_provision_luks_stack',
                    return_value=fresh_mappers) as provision, \
                mock.patch.object(
                    self.dp._storage, '_ensure_lvm_stack') as ensure_lvm, \
                mock.patch.object(
                    self.dp._storage, '_active_reefy_lv_path',
                    return_value='/dev/reefy_vg/reefy_default'):
            self.dp._apply_storage({'devices': ['sda', 'sdb', 'sdc']})

        provision.assert_called_once()
        self.assertEqual(
            provision.call_args.args[:2],
            ([('/dev/sdb', 'reefy-sdb'), ('/dev/sdc', 'reefy-sdc')],
             '/dev/reefy-key'))
        self.assertEqual(provision.call_args.kwargs['sector_size'], 512)
        self.assertFalse(provision.call_args.kwargs['write_keyfile'])
        common_sector.assert_has_calls([
            mock.call([existing_mapper]),
            mock.call(all_mappers),
        ])
        self.assertEqual(ensure_lvm.call_args.args[0], all_mappers)
        self.assertTrue(any(call.args[0][0] == 'mount'
                            for call in run.call_args_list))

    def test_existing_vg_constrains_fresh_disk_without_selected_mapper(self):
        fresh_mapper = '/dev/mapper/reefy-sdb'
        with mock.patch.object(
                dataplane.subprocess, 'run',
                side_effect=self._unmounted_run(vg_exists=True)), \
                mock.patch.object(
                    dataplane.os.path, 'exists',
                    side_effect=lambda path: path == '/dev/sdb'), \
                mock.patch.object(dataplane.os, 'makedirs'), \
                mock.patch.object(
                    self.dp._storage, '_find_reefy_key_partition',
                    return_value='/dev/reefy-key'), \
                mock.patch.object(
                    self.dp._storage, '_vg_mapper_sector_size',
                    return_value=512) as vg_sector, \
                mock.patch.object(
                    self.dp._storage, '_require_common_mapper_sector_size',
                    return_value=512), \
                mock.patch.object(
                    self.dp._storage, '_provision_luks_stack',
                    return_value=[fresh_mapper]) as provision, \
                mock.patch.object(
                    self.dp._storage, '_ensure_lvm_stack'), \
                mock.patch.object(
                    self.dp._storage, '_active_reefy_lv_path',
                    return_value='/dev/reefy/reefy_default'):
            self.dp._apply_storage({'devices': ['sdb']})

        vg_sector.assert_called_once_with()
        self.assertEqual(provision.call_args.kwargs['sector_size'], 512)
        self.assertFalse(provision.call_args.kwargs['write_keyfile'])

    def test_partial_fresh_result_never_reaches_lvm(self):
        existing_paths = {'/dev/sda', '/dev/sdb'}
        with mock.patch.object(
                dataplane.subprocess, 'run',
                side_effect=self._unmounted_run()) as run, \
                mock.patch.object(
                    dataplane.os.path, 'exists',
                    side_effect=lambda path: path in existing_paths), \
                mock.patch.object(
                    self.dp._storage, '_find_reefy_key_partition',
                    return_value='/dev/reefy-key'), \
                mock.patch.object(
                    self.dp._storage, '_provision_luks_stack',
                    return_value=['/dev/mapper/reefy-sda']) as provision, \
                mock.patch.object(
                    self.dp._storage, '_ensure_lvm_stack') as ensure_lvm:
            with self.assertRaisesRegex(
                    RuntimeError, 'Prepared 1 of 2 fresh storage devices'):
                self.dp._apply_storage({'devices': ['sda', 'sdb']})

        self.assertIsNone(provision.call_args.kwargs['sector_size'])
        self.assertFalse(provision.call_args.kwargs['write_keyfile'])
        ensure_lvm.assert_not_called()
        run.assert_any_call(
            ['cryptsetup', 'luksClose', 'reefy-sda'],
            capture_output=True, timeout=30)

    def test_incompatible_fresh_preflight_closes_new_existing_mapper(self):
        existing_paths = {'/dev/sda', '/dev/sdb'}
        preflight_error = RuntimeError(
            'No compatible LUKS sector size for selected devices')

        with mock.patch.object(
                dataplane.subprocess, 'run',
                side_effect=self._unmounted_run({'/dev/sda'})) as run, \
                mock.patch.object(
                    dataplane.os.path, 'exists',
                    side_effect=lambda path: path in existing_paths), \
                mock.patch.object(
                    self.dp._storage, '_find_reefy_key_partition',
                    return_value='/dev/reefy-key'), \
                mock.patch.object(
                    self.dp._storage, '_require_common_mapper_sector_size',
                    return_value=4096) as common_sector, \
                mock.patch.object(
                    self.dp._storage, '_provision_luks_stack',
                    side_effect=preflight_error) as provision, \
                mock.patch.object(
                    self.dp._storage, '_ensure_lvm_stack') as ensure_lvm:
            with self.assertRaisesRegex(
                    RuntimeError, 'No compatible LUKS sector size'):
                self.dp._apply_storage({'devices': ['sda', 'sdb']})

        common_sector.assert_called_once_with(['/dev/mapper/reefy-sda'])
        self.assertEqual(provision.call_args.kwargs['sector_size'], 4096)
        self.assertFalse(provision.call_args.kwargs['write_keyfile'])
        ensure_lvm.assert_not_called()
        run.assert_any_call(
            ['cryptsetup', 'luksClose', 'reefy-sda'],
            capture_output=True, timeout=30)


class ReconcileTests(unittest.TestCase):
    """_dp_reconcile: re-apply the data plane's own saved state (re-sync).
    Control calls this on connect instead of reading desired-state.json
    (the data plane is the sole reader+writer); _boot_apply uses the same
    request scheduler and reports `applied`."""

    def setUp(self):
        self.dp = _make_dp()

    def test_reports_applied_when_state_exists(self):
        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    self.dp, '_apply_desired_state', return_value=True) as ad:
            submission = self.dp._dp_submit_reconcile()
            res = self.dp._wait_apply_result(submission['request_id'])
        self.assertEqual(res['status'], 'succeeded')
        self.assertTrue(res['applied'])
        ad.assert_called_once()

    def test_reports_not_applied_when_no_state(self):
        # No saved state -> applied False, but _apply_desired_state still
        # runs (it resets the hostname to the MAC-based default).
        with mock.patch.object(dataplane.os.path, 'exists', return_value=False), \
                mock.patch.object(
                    self.dp, '_apply_desired_state', return_value=True) as ad:
            submission = self.dp._dp_submit_reconcile()
            res = self.dp._wait_apply_result(submission['request_id'])
        self.assertEqual(res['status'], 'succeeded')
        self.assertFalse(res['applied'])
        ad.assert_called_once()

    def test_exception_returns_redacted_failed_result(self):
        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(self.dp, '_apply_desired_state',
                                  side_effect=RuntimeError('boom')):
            submission = self.dp._dp_submit_reconcile()
            res = self.dp._wait_apply_result(submission['request_id'])
        self.assertEqual(res['status'], 'failed')
        self.assertIn('boom', res['error'])

    def test_false_apply_result_returns_failed_result(self):
        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(self.dp, '_apply_desired_state',
                                  return_value=False):
            submission = self.dp._dp_submit_reconcile()
            res = self.dp._wait_apply_result(submission['request_id'])
        self.assertEqual(res['status'], 'failed')
        self.assertIn('apply failed', res['error'])

    def test_legacy_reconcile_method_keeps_original_shape(self):
        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    self.dp, '_apply_desired_state', return_value=True):
            res = self.dp._dp_reconcile()
        self.assertEqual(res, {'ok': True, 'applied': True, 'error': ''})

    def test_boot_apply_delegates_to_reconcile(self):
        with mock.patch.object(self.dp, '_dp_reconcile') as rec:
            self.dp._boot_apply()
        rec.assert_called_once_with()


class DropAbsentDevicesTests(unittest.TestCase):
    """_drop_absent_devices makes /dev passthrough optional: absent /dev
    nodes are dropped (degrade to omission) while CDI refs and present
    devices pass through."""

    @staticmethod
    def _exists(present):
        return lambda p: p in present

    def test_present_dev_kept(self):
        compose = {'services': {'a': {'devices': ['/dev/dri']}}}
        skipped = dataplane._drop_absent_devices(
            compose, exists=self._exists({'/dev/dri'}))
        self.assertEqual(compose['services']['a']['devices'], ['/dev/dri'])
        self.assertEqual(skipped, [])

    def test_absent_dev_dropped(self):
        compose = {'services': {'a': {'devices': ['/dev/dri']}}}
        skipped = dataplane._drop_absent_devices(
            compose, exists=self._exists(set()))
        self.assertEqual(compose['services']['a']['devices'], [])
        self.assertEqual(skipped, [('a', '/dev/dri')])

    def test_cdi_and_nondev_kept_even_if_missing(self):
        compose = {'services': {'a': {'devices': ['nvidia.com/gpu=all']}}}
        skipped = dataplane._drop_absent_devices(
            compose, exists=self._exists(set()))
        self.assertEqual(compose['services']['a']['devices'],
                         ['nvidia.com/gpu=all'])
        self.assertEqual(skipped, [])

    def test_mixed_list(self):
        compose = {'services': {'a': {'devices': [
            'nvidia.com/gpu=all',          # CDI -> keep
            '/dev/dri:/dev/dri:rwm',       # present -> keep (host path parsed)
            '/dev/kvm',                    # absent -> drop
        ]}}}
        skipped = dataplane._drop_absent_devices(
            compose, exists=self._exists({'/dev/dri'}))
        self.assertEqual(compose['services']['a']['devices'],
                         ['nvidia.com/gpu=all', '/dev/dri:/dev/dri:rwm'])
        self.assertEqual(skipped, [('a', '/dev/kvm')])

    def test_service_without_devices_untouched(self):
        compose = {'services': {'a': {'image': 'x'}}}
        self.assertEqual(dataplane._drop_absent_devices(compose), [])
        self.assertNotIn('devices', compose['services']['a'])


class BestEffortCdiTests(unittest.TestCase):
    def test_reads_json_and_yaml_provider_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'intel.json').write_text(json.dumps({
                'cdiVersion': '0.6.0',
                'kind': 'intel.com/npu',
                'devices': [{'name': 'all', 'containerEdits': {}}],
            }))
            Path(directory, 'nvidia.yaml').write_text(
                'cdiVersion: 0.6.0\n'
                'kind: nvidia.com/gpu\n'
                'devices:\n'
                '  - name: "0"\n'
                '    containerEdits: {}\n'
                '  - name: all\n'
                '    containerEdits: {}\n')
            Path(directory, 'amd.json').write_text(json.dumps({
                'cdiVersion': '0.6.0',
                'kind': 'amd.com/gpu',
                'devices': [{'name': 'all', 'containerEdits': {}}],
            }))

            resources = dataplane._cdi_resources((directory,))

        self.assertEqual(resources, {
            'amd.com/gpu=all',
            'intel.com/npu=all',
            'nvidia.com/gpu=0',
            'nvidia.com/gpu=all',
        })

    def test_only_unavailable_cdi_requests_are_removed(self):
        compose = {'services': {'app': {'devices': [
            '/dev/dri:/dev/dri',
            'intel.com/gpu=all',
            'intel.com/npu=all',
            'synthetic-non-cdi',
        ]}}}

        skipped = dataplane._drop_unavailable_cdi_devices(
            compose, {'intel.com/npu=all'})

        self.assertEqual(compose['services']['app']['devices'], [
            '/dev/dri:/dev/dri',
            'intel.com/npu=all',
            'synthetic-non-cdi',
        ])
        self.assertEqual(skipped, [('app', 'intel.com/gpu=all')])

    def test_optional_artifact_failure_does_not_block_app(self):
        dp = _make_dp()
        failed = mock.Mock(returncode=1, stdout='activation details\n',
                           stderr='provider failed\n')
        app = {
            'project_name': 'reefy-app-synthetic',
            'instance_uuid': 'synthetic',
            'artifacts': [{
                'name': 'intel-accelerator',
                'kind': 'host-extension',
                'ref': 'example.invalid/provider@sha256:' + ('a' * 64),
                'required': False,
            }],
        }
        with mock.patch.object(
                dataplane.subprocess, 'run', return_value=failed), \
                mock.patch.object(dp, '_publish_health_status'), \
                mock.patch.object(dataplane, 'log') as logger:
            self.assertTrue(dp._prepare_app_artifacts(app))

        messages = [call.args[1] for call in logger.call_args_list]
        self.assertTrue(any('activation details' in value for value in messages))
        self.assertTrue(any('provider failed' in value for value in messages))
        self.assertTrue(any('exit 1' in value for value in messages))

    def test_required_artifact_failure_still_blocks_app(self):
        dp = _make_dp()
        failed = mock.Mock(returncode=1, stdout='', stderr='')
        app = {
            'project_name': 'reefy-app-synthetic',
            'instance_uuid': 'synthetic',
            'artifacts': [{
                'name': 'required-data',
                'kind': 'app',
                'ref': 'example.invalid/data@sha256:' + ('b' * 64),
            }],
        }
        with mock.patch.object(
                dataplane.subprocess, 'run', return_value=failed), \
                mock.patch.object(dp, '_publish_health_status'):
            self.assertFalse(dp._prepare_app_artifacts(app))

    def test_different_artifacts_for_one_app_prepare_concurrently(self):
        dp = _make_dp()
        barrier = threading.Barrier(2)

        def prepare(*_args, **_kwargs):
            barrier.wait(timeout=2)
            return mock.Mock(returncode=0, stdout='', stderr='')

        app = {
            'project_name': 'reefy-app-synthetic',
            'instance_uuid': 'synthetic',
            'artifacts': [{
                'name': name,
                'kind': 'host-extension',
                'ref': f'example.invalid/{name}@sha256:' + character * 64,
                'required': False,
            } for name, character in (
                ('nvidia-driver', 'a'),
                ('intel-accelerator', 'b'),
            )],
        }
        with mock.patch.object(
                dataplane.subprocess, 'run', side_effect=prepare), \
                mock.patch.object(dp, '_publish_health_status'):
            self.assertTrue(dp._prepare_app_artifacts(app))


if __name__ == '__main__':
    unittest.main()
