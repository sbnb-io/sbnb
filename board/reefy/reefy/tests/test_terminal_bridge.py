"""Regression tests for reefy-terminal-bridge subscriber accounting."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


BRIDGE_PATH = (
    Path(__file__).resolve().parents[1]
    / 'rootfs-overlay/usr/bin/reefy-terminal-bridge'
)


def _load_bridge():
    fake_paho = types.ModuleType('paho')
    fake_paho_mqtt = types.ModuleType('paho.mqtt')
    fake_client = types.ModuleType('paho.mqtt.client')
    fake_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=object())
    fake_client.Client = object
    sys.modules.setdefault('paho', fake_paho)
    sys.modules.setdefault('paho.mqtt', fake_paho_mqtt)
    sys.modules.setdefault('paho.mqtt.client', fake_client)

    loader = importlib.machinery.SourceFileLoader(
        'reefy_terminal_bridge_for_test', str(BRIDGE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bridge = _load_bridge()


class TerminalBridgeSubscriberTests(unittest.TestCase):
    def setUp(self):
        bridge._sessions.clear()

    def test_client_id_subscribe_is_idempotent(self):
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=10),
            1)
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=20),
            1)
        session = bridge.get_session('app1')
        self.assertEqual(session['subscribers'], {'tab-a': True})
        self.assertEqual(session['last_activity'], 20)

    def test_mqtt_client_id_is_unique_per_device(self):
        first = bridge.mqtt_client_id(
            '11111111-1111-4111-8111-111111111111')
        second = bridge.mqtt_client_id(
            '22222222-2222-4222-8222-222222222222')

        self.assertEqual(
            first, 'reefy-terminal-11111111-1111-4111-8111-111111111111')
        self.assertNotEqual(first, second)

    def test_multiple_client_ids_count_as_multiple_viewers(self):
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=10),
            1)
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-b'}, now=11),
            2)
        self.assertEqual(
            bridge.unsubscribe_session('app1', {'client_id': 'tab-a'}),
            1)
        self.assertEqual(
            bridge.unsubscribe_session('app1', {'client_id': 'tab-b'}),
            0)

    def test_unknown_client_id_unsubscribe_is_idempotent(self):
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=10),
            1)
        self.assertEqual(
            bridge.unsubscribe_session('app1', {'client_id': 'missing'}),
            1)

    def test_legacy_messages_keep_counter_fallback(self):
        self.assertEqual(bridge.subscribe_session('app1', {}, now=10), 1)
        self.assertEqual(bridge.subscribe_session('app1', {}, now=11), 2)
        self.assertEqual(bridge.unsubscribe_session('app1', {}), 1)
        self.assertEqual(bridge.unsubscribe_session('app1', {}), 0)
        self.assertEqual(bridge.unsubscribe_session('app1', {}), 0)

    def test_subscribe_records_container_metadata(self):
        self.assertEqual(
            bridge.subscribe_session(
                'app1',
                {'client_id': 'tab-a', 'container': 'state-app1-1', 'name': 'Dev'},
                now=10),
            1)
        session = bridge.get_session('app1')
        self.assertEqual(session['container'], 'state-app1-1')
        self.assertEqual(session['name'], 'Dev')

    def test_v2_resolution_uses_live_container_resolver(self):
        state = {'schema_version': 2, 'apps': []}
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir) / 'desired-state-v2.json'
            state_path.write_text(json.dumps(state))
            with mock.patch.object(bridge, 'STATE_DIR', state_dir), \
                    mock.patch.object(
                        bridge, 'resolve_container',
                        return_value='state-app1-1') as resolver:
                self.assertEqual(
                    bridge._resolve_app_container(
                        'app1', 'reefy-app-app1-app-1'),
                    'state-app1-1')

        resolver.assert_called_once_with(
            state, 'app1', 'reefy-app-app1-app-1')


if __name__ == '__main__':
    unittest.main()
