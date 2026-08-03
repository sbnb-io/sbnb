"""Static integration guard for the shell entrypoint's shared resolver."""

import unittest
from pathlib import Path


SHELL_PATH = (
    Path(__file__).resolve().parents[1]
    / 'rootfs-overlay/usr/bin/reefy-app-shell'
)


class AppShellTests(unittest.TestCase):
    def test_app_shell_uses_live_container_resolver(self):
        source = SHELL_PATH.read_text()
        self.assertIn(
            'from reefy.containers import resolve_app_container', source)
        self.assertIn('resolve_app_container(', source)
        self.assertIn('f"state-{uuid}-1"', source)


if __name__ == '__main__':
    unittest.main()
