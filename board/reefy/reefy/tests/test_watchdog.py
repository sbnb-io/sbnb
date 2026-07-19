"""Unit tests for infrastructure liveness checks and recovery."""

import io
import os
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import _bootstrap  # noqa: F401
from reefy import watchdog


PROXY_CHECK = {
    'service': 'reefy-proxy',
    'url': 'http://127.0.0.1:8080/',
    'kind': 'http-response',
}


class ProbeTests(unittest.TestCase):
    @mock.patch.object(watchdog.urllib.request, 'urlopen')
    def test_cloudflared_requires_ready_connection(self, urlopen):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"status":200,"readyConnections":4}')
        urlopen.return_value = response

        self.assertTrue(watchdog.probe(
            'http://127.0.0.1:20241/ready', 'cloudflared'))

        response.__enter__.return_value.read.return_value = (
            b'{"status":200,"readyConnections":0}')
        self.assertFalse(watchdog.probe(
            'http://127.0.0.1:20241/ready', 'cloudflared'))

    @mock.patch.object(watchdog.urllib.request, 'urlopen')
    def test_proxy_http_error_still_proves_liveness(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            PROXY_CHECK['url'], 403, 'Forbidden', {}, io.BytesIO())
        self.assertTrue(watchdog.probe(
            PROXY_CHECK['url'], PROXY_CHECK['kind']))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    @mock.patch.object(watchdog.subprocess, 'run')
    @mock.patch.object(watchdog, 'probe', return_value=False)
    @mock.patch.object(watchdog, 'find_container', return_value='container123456')
    def test_restarts_after_three_consecutive_failures(
            self, find_container, probe, run):
        run.return_value = types.SimpleNamespace(returncode=0)

        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=100))
        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=101))
        run.assert_not_called()

        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=102))
        run.assert_called_once_with(
            ['docker', 'restart', '--time', '5', 'container123456'],
            capture_output=True,
            text=True,
            timeout=watchdog.RESTART_TIMEOUT_SECONDS,
        )
        self.assertFalse(os.path.exists(os.path.join(
            self.tempdir.name, 'reefy-proxy-failures')))

    @mock.patch.object(watchdog.subprocess, 'run')
    @mock.patch.object(watchdog, 'probe', return_value=False)
    @mock.patch.object(watchdog, 'find_container', return_value='container123456')
    def test_cooldown_suppresses_restart(self, find_container, probe, run):
        with open(os.path.join(
                self.tempdir.name, 'reefy-proxy-failures'), 'w') as stream:
            stream.write('2')
        with open(os.path.join(
                self.tempdir.name, 'reefy-proxy-cooldown'), 'w') as stream:
            stream.write('500')

        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=200))
        run.assert_not_called()

    @mock.patch.object(watchdog, 'probe', return_value=True)
    @mock.patch.object(watchdog, 'find_container', return_value='container123456')
    def test_healthy_probe_clears_failure_count(self, find_container, probe):
        failure_file = os.path.join(
            self.tempdir.name, 'reefy-proxy-failures')
        with open(failure_file, 'w') as stream:
            stream.write('2')

        self.assertTrue(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=100))
        self.assertFalse(os.path.exists(failure_file))

    @mock.patch.object(watchdog, 'probe')
    @mock.patch.object(watchdog, 'find_container', return_value=None)
    def test_absent_optional_service_is_skipped(self, find_container, probe):
        self.assertTrue(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=100))
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
