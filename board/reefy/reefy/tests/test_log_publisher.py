"""Unit tests for the standalone device log publisher."""

import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest

import _bootstrap  # noqa: F401


PUBLISHER_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'rootfs-overlay', 'usr', 'bin',
    'reefy-log-publisher')


def _load_publisher():
    fake_paho = types.ModuleType('paho')
    fake_paho.__path__ = []
    fake_mqtt_pkg = types.ModuleType('paho.mqtt')
    fake_mqtt_pkg.__path__ = []
    fake_client = types.ModuleType('paho.mqtt.client')
    fake_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    fake_client.Client = object
    fake_paho.mqtt = fake_mqtt_pkg
    fake_mqtt_pkg.client = fake_client
    old_modules = {
        name: sys.modules.get(name)
        for name in ('paho', 'paho.mqtt', 'paho.mqtt.client')
    }
    sys.modules['paho'] = fake_paho
    sys.modules['paho.mqtt'] = fake_mqtt_pkg
    sys.modules['paho.mqtt.client'] = fake_client
    try:
        loader = importlib.machinery.SourceFileLoader(
            'reefy_log_publisher_test', PUBLISHER_PATH)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        for name, old in old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class JournalPayloadTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load_publisher()

    def test_outbound_message_is_redacted(self):
        secret = 'synthetic-publisher-secret'
        payload = self.publisher.journal_entry_payload({
            'MESSAGE': f'client_secret={secret}',
            '_SYSTEMD_UNIT': 'reefy-reconciler.service',
            'PRIORITY': '6',
        }, now=lambda: 123.0)
        self.assertEqual(payload['source'], 'reefy-reconciler')
        self.assertEqual(payload['ts'], 123.0)
        self.assertNotIn(secret, payload['msg'])
        self.assertIn('[REDACTED]', payload['msg'])

    def test_outbound_header_gets_final_redaction_pass(self):
        secret = 'synthetic-digest-response'
        payload = self.publisher.journal_entry_payload({
            'MESSAGE': f'Authorization: Digest response={secret}',
            '_SYSTEMD_UNIT': 'reefy-control.service',
            'PRIORITY': '6',
        }, now=lambda: 124.0)
        self.assertNotIn(secret, payload['msg'])
        self.assertEqual(payload['msg'], 'Authorization: [REDACTED]')

    def test_error_priority_preserves_error_source(self):
        payload = self.publisher.journal_entry_payload({
            'MESSAGE': 'synthetic failure',
            '_SYSTEMD_UNIT': 'reefy-control.service',
            'PRIORITY': '3',
        }, now=lambda: 456.0)
        self.assertEqual(payload['source'], 'error')

    def test_malformed_priorities_fall_back_to_normal_source(self):
        for priority in (None, {}, float('inf'), 'not-a-priority'):
            with self.subTest(priority=priority):
                payload = self.publisher.journal_entry_payload({
                    'MESSAGE': 'synthetic diagnostic',
                    '_SYSTEMD_UNIT': 'reefy-control.service',
                    'PRIORITY': priority,
                }, now=lambda: 789.0)
                self.assertEqual(payload['source'], 'reefy-control')

    def test_own_and_empty_messages_are_skipped(self):
        self.assertIsNone(self.publisher.journal_entry_payload({
            'MESSAGE': 'internal',
            '_SYSTEMD_UNIT': 'reefy-log-publisher.service',
        }))
        self.assertIsNone(self.publisher.journal_entry_payload({'MESSAGE': ''}))

    def test_mqtt_client_id_is_unique_per_device(self):
        first = self.publisher.mqtt_client_id(
            '11111111-1111-4111-8111-111111111111')
        second = self.publisher.mqtt_client_id(
            '22222222-2222-4222-8222-222222222222')

        self.assertEqual(
            first, 'reefy-log-11111111-1111-4111-8111-111111111111')
        self.assertNotEqual(first, second)
