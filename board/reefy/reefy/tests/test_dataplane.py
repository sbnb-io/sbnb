"""Unit tests for reefy.dataplane (no device, no MQTT broker).

Focus: the event-routing fix (data plane publishes via reefy-mqtt-pub,
non-fatally) and the data-side behavior of the split methods."""

import json
import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)
from reefy import dataplane, shared
from reefy.storage import Storage


def _make_dp():
    # __init__ reads mqtt.conf/device-uuid which don't exist on a dev box;
    # load_mqtt_config returns {} -> safe defaults, device_uuid None.
    return dataplane.DataPlane(Storage())


class ImportIsolationTests(unittest.TestCase):
    def test_dataplane_imports_without_paho(self):
        self.assertFalse(hasattr(dataplane, 'mqtt'))

    def test_no_control_isms(self):
        # The data plane must not carry the control-only flag/branch.
        src = open(dataplane.__file__).read()
        self.assertNotIn('_is_data_plane', src)
        self.assertNotIn('_varlink_call', src)

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

    def test_send_command_response_is_noop(self):
        # cmd_id is always None over Varlink; must be a no-op (no client).
        self.assertIsNone(self.dp._send_command_response(None, status='running'))


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


class ApplyPathTests(unittest.TestCase):
    """Drive the full data-side apply path (the Varlink ApplyState entry)
    through _apply_state_command -> _apply_state -> _apply_desired_state
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

    def _apply(self, dp):
        """Run an ApplyState with every worker/storage/host call mocked,
        a temp desired-state path, and return (result, mocks)."""
        import tempfile
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
                mock.patch.object(dp._storage, '_prepare_app_dirs') as m_dirs, \
                mock.patch.object(shared, 'set_hostname') as m_host, \
                mock.patch.object(shared, 'get_default_hostname', return_value='def-host'):
            res = dp._dp_apply_state(json.dumps(self.REPRESENTATIVE_STATE))
        return res, {**m, 'compose': m_compose, 'caps': m_caps,
                     'dirs': m_dirs, 'host': m_host}

    def test_apply_state_succeeds_end_to_end(self):
        res, _ = self._apply(_make_dp())
        # Returns the Varlink success shape - no NameError/AttributeError/
        # mode crash anywhere in the dispatch.
        self.assertEqual(res, {'ok': True, 'error': ''})

    def test_apply_dispatches_each_section(self):
        res, m = self._apply(_make_dp())
        self.assertEqual(res['ok'], True)
        m['host'].assert_called_with('reefy-test')          # hostname applied
        m['caps'].assert_called()                           # caps pushed to storage
        m['_apply_user_ssh_keys'].assert_called_once_with(['ssh-ed25519 AAAAC3Nz'])
        m['_sync_app_users'].assert_called_once_with([{'uuid': 'i1', 'name': 'app1'}])
        m['dirs'].assert_called_once()                      # app dirs prepared
        m['compose'].assert_called_once()                   # compose applied

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
                mock.patch.object(shared, 'set_hostname'), \
                mock.patch.object(shared, 'get_default_hostname', return_value='d'):
            res = dp._dp_apply_state(json.dumps(self.REPRESENTATIVE_STATE))
        # The data plane emits a stage=error event on compose failure.
        stages = [c.args[1].get('stage')
                  for c in m_emit.call_args_list if c.args and c.args[0] == 'stage']
        self.assertIn('error', stages)
        # KNOWN GAP (deferred follow-up): _dp_apply_state still returns ok=True
        # on an apply that returned False - only an *exception* yields not-ok -
        # so control would publish 'ready' despite the failure. The hardening
        # (propagate apply failure as not-ok) is the separate observability
        # follow-up.
        self.assertTrue(res['ok'])

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


class ReconcileTests(unittest.TestCase):
    """_dp_reconcile: re-apply the data plane's own saved state (re-sync).
    Control calls this on connect instead of reading desired-state.json
    (the data plane is the sole reader+writer); _boot_apply uses the same
    path at startup. Runs under the apply lock and reports `applied`."""

    def setUp(self):
        self.dp = _make_dp()

    def test_reports_applied_when_state_exists(self):
        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(self.dp, '_apply_desired_state') as ad:
            res = self.dp._dp_reconcile()
        self.assertEqual(res, {'ok': True, 'applied': True, 'error': ''})
        ad.assert_called_once()
        self.assertTrue(self.dp._apply_lock.acquire(blocking=False),
                        'apply lock not released after reconcile')

    def test_reports_not_applied_when_no_state(self):
        # No saved state -> applied False, but _apply_desired_state still
        # runs (it resets the hostname to the MAC-based default).
        with mock.patch.object(dataplane.os.path, 'exists', return_value=False), \
                mock.patch.object(self.dp, '_apply_desired_state') as ad:
            res = self.dp._dp_reconcile()
        self.assertEqual(res, {'ok': True, 'applied': False, 'error': ''})
        ad.assert_called_once()

    def test_skips_apply_when_lock_held(self):
        # A command apply already holds the lock -> reconcile must not run
        # _apply_desired_state concurrently (it would race the same work).
        self.dp._apply_lock.acquire()
        try:
            with mock.patch.object(dataplane.os.path, 'exists',
                                   return_value=True), \
                    mock.patch.object(self.dp, '_apply_desired_state') as ad:
                res = self.dp._dp_reconcile()
            ad.assert_not_called()
            self.assertEqual(res, {'ok': True, 'applied': True, 'error': ''})
        finally:
            self.dp._apply_lock.release()

    def test_drains_state_queued_during_reconcile(self):
        # State control forwarded while reconcile held the lock must be
        # applied after, not dropped.
        forwarded = {'forwarded': True}

        def queue_during(*a, **k):
            self.dp._pending_state = forwarded

        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(self.dp, '_apply_desired_state',
                                  side_effect=queue_during), \
                mock.patch.object(self.dp, '_apply_state') as as_:
            res = self.dp._dp_reconcile()
        as_.assert_called_once_with(forwarded)
        self.assertIsNone(self.dp._pending_state)
        self.assertTrue(res['ok'])

    def test_failure_returns_error_and_releases_lock(self):
        with mock.patch.object(dataplane.os.path, 'exists', return_value=True), \
                mock.patch.object(self.dp, '_apply_desired_state',
                                  side_effect=RuntimeError('boom')):
            res = self.dp._dp_reconcile()
        self.assertFalse(res['ok'])
        self.assertIn('boom', res['error'])
        self.assertTrue(self.dp._apply_lock.acquire(blocking=False),
                        'apply lock not released after a failed reconcile')

    def test_boot_apply_delegates_to_reconcile(self):
        with mock.patch.object(self.dp, '_dp_reconcile') as rec:
            self.dp._boot_apply()
        rec.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
