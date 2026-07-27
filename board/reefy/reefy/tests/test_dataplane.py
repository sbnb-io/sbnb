"""Unit tests for reefy.dataplane (no device, no MQTT broker).

Focus: the event-routing fix (data plane publishes via reefy-mqtt-pub,
non-fatally) and the data-side behavior of the split methods."""

import json
import os
import tempfile
import threading
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


if __name__ == '__main__':
    unittest.main()
