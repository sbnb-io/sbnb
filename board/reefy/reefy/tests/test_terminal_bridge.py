"""Regression tests for reefy-terminal-bridge subscriber accounting."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types
import unittest
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


if __name__ == '__main__':
    unittest.main()
