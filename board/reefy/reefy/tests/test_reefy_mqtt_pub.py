"""Unit tests for confirmed delivery in the reefy-mqtt-pub helper."""

import importlib.util
import os
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock


_PATH = os.path.join(os.path.dirname(__file__), '..', 'rootfs-overlay',
                     'usr', 'bin', 'reefy-mqtt-pub')


def _load_module():
    paho = types.ModuleType('paho')
    paho.__path__ = []
    mqtt_package = types.ModuleType('paho.mqtt')
    mqtt_package.__path__ = []
    mqtt_client = types.ModuleType('paho.mqtt.client')
    paho.mqtt = mqtt_package
    mqtt_package.client = mqtt_client

    loader = SourceFileLoader('reefy_mqtt_pub', _PATH)
    spec = importlib.util.spec_from_loader('reefy_mqtt_pub', loader)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
            'paho': paho,
            'paho.mqtt': mqtt_package,
            'paho.mqtt.client': mqtt_client,
    }):
        loader.exec_module(module)
    return module


reefy_mqtt_pub = _load_module()


class PublishConfirmationTests(unittest.TestCase):
    def test_confirmed_publish_returns(self):
        info = mock.Mock()
        info.is_published.return_value = True
        client = mock.Mock()
        client.publish.return_value = info

        reefy_mqtt_pub.publish_confirmed(
            client, 'synthetic/topic', '{}', timeout=7)

        client.publish.assert_called_once_with(
            'synthetic/topic', '{}', qos=1)
        info.wait_for_publish.assert_called_once_with(timeout=7)

    def test_unconfirmed_publish_raises(self):
        info = mock.Mock()
        info.is_published.return_value = False
        client = mock.Mock()
        client.publish.return_value = info

        with self.assertRaisesRegex(TimeoutError, 'did not confirm'):
            reefy_mqtt_pub.publish_confirmed(
                client, 'synthetic/topic', '{}', timeout=7)

    def test_main_cleans_up_client_when_confirmation_fails(self):
        client = mock.Mock()
        config = {
            'MQTT_BROKER': 'broker.invalid',
            'MQTT_PORT': '443',
            'MQTT_TOPIC_PREFIX': 'synthetic',
        }

        with mock.patch.object(
                reefy_mqtt_pub, 'load_config', return_value=config), \
                mock.patch.object(
                    reefy_mqtt_pub.os.path, 'exists', return_value=True), \
                mock.patch('builtins.open',
                           mock.mock_open(read_data='synthetic-device')), \
                mock.patch.object(
                    reefy_mqtt_pub.mqtt, 'Client', return_value=client,
                    create=True), \
                mock.patch.object(
                    reefy_mqtt_pub, 'publish_confirmed',
                    side_effect=TimeoutError('synthetic timeout')), \
                mock.patch.object(
                    reefy_mqtt_pub.sys, 'argv',
                    ['reefy-mqtt-pub', 'instance/status', '{}']):
            with self.assertRaisesRegex(TimeoutError, 'synthetic timeout'):
                reefy_mqtt_pub.main()

        client.disconnect.assert_called_once_with()
        client.loop_stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
