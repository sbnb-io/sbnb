"""Unit tests for bounded, secret-free apply result persistence."""

import json
import os
import stat
import tempfile
import unittest
import uuid
from unittest import mock

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)
from reefy.apply_results import ApplyResultStore


class ApplyResultStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = os.path.join(tempfile.mkdtemp(), 'apply-results')
        self.store = ApplyResultStore(self.directory)

    @staticmethod
    def _request_id():
        return str(uuid.uuid4())

    def test_create_persists_before_return_with_private_permissions(self):
        request_id = self._request_id()
        self.assertTrue(self.store.create(request_id, 'apply'))
        path = os.path.join(self.directory, f'{request_id}.json')
        self.assertTrue(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as handle:
            record = json.load(handle)
        self.assertEqual(record['status'], 'queued')
        self.assertNotIn('state', record)

    def test_result_contains_only_redacted_error_and_path_free_warnings(self):
        request_id = self._request_id()
        secret = 'synthetic-result-secret'
        self.assertTrue(self.store.create(request_id, 'apply'))
        self.assertTrue(self.store.update(
            request_id,
            'succeeded_with_warnings',
            error=f'password={secret} failed at /mnt/synthetic/private/file',
            warnings=[{
                'code': 'storage.cap_not_enforced',
                'instance_uuid': 'synthetic-instance',
                'volume': 'media',
                'path': '/synthetic/path/must-not-persist',
                'extra': secret,
            }],
            applied=True,
        ))
        record = self.store.get(request_id)
        rendered = json.dumps(record)
        self.assertNotIn(secret, rendered)
        self.assertNotIn('/synthetic/path', rendered)
        self.assertNotIn('/mnt/synthetic/private/file', rendered)
        self.assertIn('[PATH]', record['error'])
        self.assertEqual(record['warnings'], [{
            'code': 'storage.cap_not_enforced',
            'instance_uuid': 'synthetic-instance',
            'volume': 'media',
        }])

    def test_restart_marks_queued_and_running_records_failed(self):
        queued = self._request_id()
        running = self._request_id()
        self.assertTrue(self.store.create(queued, 'apply'))
        self.assertTrue(self.store.create(running, 'reconcile'))
        self.assertTrue(self.store.update(running, 'running'))

        reloaded = ApplyResultStore(self.directory)
        reloaded.fail_interrupted()

        self.assertEqual(reloaded.get(queued)['status'], 'failed')
        self.assertEqual(reloaded.get(running)['status'], 'failed')
        self.assertIn('restart', reloaded.get(running)['error'])

    def test_only_latest_terminal_records_are_retained(self):
        now = [0]

        def clock():
            now[0] += 1
            return now[0]

        store = ApplyResultStore(
            self.directory, terminal_limit=2, clock=clock)
        request_ids = []
        for _ in range(3):
            request_id = self._request_id()
            request_ids.append(request_id)
            self.assertTrue(store.create(request_id, 'apply'))
            self.assertTrue(store.update(request_id, 'succeeded'))

        self.assertIsNone(store.get(request_ids[0]))
        self.assertIsNotNone(store.get(request_ids[1]))
        self.assertIsNotNone(store.get(request_ids[2]))

    def test_create_failure_does_not_accept_request(self):
        request_id = self._request_id()
        with mock.patch.object(self.store, '_write', return_value=False):
            self.assertFalse(self.store.create(request_id, 'apply'))
        self.assertIsNone(self.store.get(request_id))


if __name__ == '__main__':
    unittest.main()
