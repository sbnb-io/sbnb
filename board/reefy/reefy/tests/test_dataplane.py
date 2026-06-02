"""Unit tests for reefy.dataplane (no device, no MQTT broker).

Focus: the event-routing fix (data plane publishes via reefy-mqtt-pub,
non-fatally) and the data-side behavior of the split methods."""

import json
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)
from reefy import dataplane
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


if __name__ == '__main__':
    unittest.main()
