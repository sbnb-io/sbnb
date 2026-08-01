"""Tests for app container resolution during Apps v2 migration."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REEFY_LIB = (
    Path(__file__).resolve().parents[1]
    / 'rootfs-overlay/usr/lib/reefy'
)
sys.path.insert(0, str(REEFY_LIB))

from reefy.containers import resolve_app_container


def _v2_state():
    return {
        'schema_version': 2,
        'apps': [{
            'instance_uuid': 'app1',
            'project_name': 'reefy-app-app1',
            'primary_service': 'web',
        }],
    }


class AppContainerResolutionTests(unittest.TestCase):
    def test_v2_prefers_running_primary_container(self):
        running = {'reefy-app-app1-web-1', 'state-app1-1'}
        self.assertEqual(
            resolve_app_container(
                _v2_state(), 'app1', 'state-app1-1', running.__contains__),
            'reefy-app-app1-web-1')

    def test_pending_migration_falls_back_to_running_legacy_container(self):
        self.assertEqual(
            resolve_app_container(
                _v2_state(), 'app1', 'state-app1-1',
                {'state-app1-1'}.__contains__),
            'state-app1-1')

    def test_missing_containers_preserve_desired_v2_failure_target(self):
        self.assertEqual(
            resolve_app_container(
                _v2_state(), 'app1', 'state-app1-1', lambda _name: False),
            'reefy-app-app1-web-1')

    def test_v1_keeps_requested_container_without_docker_probe(self):
        def unexpected_probe(_name):
            raise AssertionError('v1 resolution must not inspect Docker')

        self.assertEqual(
            resolve_app_container(
                {'schema_version': 1}, 'app1', 'state-custom-1',
                unexpected_probe),
            'state-custom-1')


if __name__ == '__main__':
    unittest.main()
