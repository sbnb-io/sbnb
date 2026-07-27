"""Persistent, secret-free desired-state apply result records."""

import copy
import json
import os
import re
import threading
import time

from reefy.redaction import redact_log_message


TERMINAL_STATUSES = {
    'succeeded',
    'succeeded_with_warnings',
    'failed',
    'superseded',
}
VALID_STATUSES = TERMINAL_STATUSES | {'queued', 'running'}
REQUEST_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$')
PATH_RE = re.compile(
    r'(?<![A-Za-z0-9:])/'
    r'(?:[A-Za-z0-9._+\-]+/)*[A-Za-z0-9._+\-]+')


def sanitize_apply_error(error):
    """Redact secrets and filesystem paths from persistent results."""
    redacted = redact_log_message(error or '')
    return PATH_RE.sub('[PATH]', redacted)[:500]


class ApplyResultStore:
    """Atomic apply-result storage with bounded terminal history."""

    def __init__(self, directory, terminal_limit=32, clock=time.time):
        self.directory = directory
        self.terminal_limit = terminal_limit
        self._clock = clock
        self._lock = threading.RLock()
        self._results = {}
        self._load()

    @staticmethod
    def _sanitize_warnings(warnings):
        sanitized = []
        for warning in warnings or []:
            if not isinstance(warning, dict):
                continue
            code = str(warning.get('code') or '')[:100]
            instance_uuid = str(warning.get('instance_uuid') or '')[:100]
            volume = str(warning.get('volume') or '')[:100]
            if code and instance_uuid and volume:
                sanitized.append({
                    'code': code,
                    'instance_uuid': instance_uuid,
                    'volume': volume,
                })
        return sanitized

    @classmethod
    def _sanitize_record(cls, record):
        if not isinstance(record, dict):
            return None
        request_id = str(record.get('request_id') or '')
        status = str(record.get('status') or '')
        if not REQUEST_ID_RE.fullmatch(request_id) or status not in VALID_STATUSES:
            return None
        kind = str(record.get('kind') or '')
        if kind not in ('apply', 'reconcile'):
            return None
        try:
            created_at = float(record.get('created_at') or 0)
            updated_at = float(record.get('updated_at') or created_at)
        except (TypeError, ValueError):
            return None
        return {
            'request_id': request_id,
            'kind': kind,
            'status': status,
            'error': sanitize_apply_error(record.get('error')),
            'warnings': cls._sanitize_warnings(record.get('warnings')),
            'applied': bool(record.get('applied', False)),
            'created_at': created_at,
            'updated_at': updated_at,
        }

    def _path(self, request_id):
        return os.path.join(self.directory, f'{request_id}.json')

    def _load(self):
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return
        for name in entries:
            if not name.endswith('.json'):
                continue
            request_id = name[:-5]
            if not REQUEST_ID_RE.fullmatch(request_id):
                continue
            try:
                with open(os.path.join(self.directory, name)) as handle:
                    record = self._sanitize_record(json.load(handle))
            except (OSError, json.JSONDecodeError):
                continue
            if record and record['request_id'] == request_id:
                self._results[request_id] = record

    def _write(self, record):
        temp_path = None
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            os.chmod(self.directory, 0o700)
            path = self._path(record['request_id'])
            temp_path = f'{path}.{os.getpid()}.tmp'
            with open(temp_path, 'w') as handle:
                json.dump(record, handle, separators=(',', ':'), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            return True
        except OSError:
            return False
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def create(self, request_id, kind):
        with self._lock:
            now = self._clock()
            record = self._sanitize_record({
                'request_id': request_id,
                'kind': kind,
                'status': 'queued',
                'error': '',
                'warnings': [],
                'applied': False,
                'created_at': now,
                'updated_at': now,
            })
            if record is None or request_id in self._results:
                return False
            self._results[request_id] = record
            if not self._write(record):
                self._results.pop(request_id, None)
                return False
            return True

    def update(self, request_id, status, error='', warnings=None,
               applied=False):
        with self._lock:
            current = self._results.get(request_id)
            if current is None or status not in VALID_STATUSES:
                return False
            record = dict(current)
            record.update({
                'status': status,
                'error': sanitize_apply_error(error),
                'warnings': self._sanitize_warnings(warnings),
                'applied': bool(applied),
                'updated_at': self._clock(),
            })
            self._results[request_id] = record
            written = self._write(record)
            if status in TERMINAL_STATUSES:
                self._prune()
            return written

    def get(self, request_id):
        with self._lock:
            record = self._results.get(request_id)
            return copy.deepcopy(record) if record else None

    def fail_interrupted(self):
        with self._lock:
            for request_id, record in list(self._results.items()):
                if record['status'] not in TERMINAL_STATUSES:
                    self.update(
                        request_id,
                        'failed',
                        error='apply interrupted by data-plane restart',
                        applied=record.get('applied', False),
                    )

    def _prune(self):
        terminal = sorted(
            (record for record in self._results.values()
             if record['status'] in TERMINAL_STATUSES),
            key=lambda record: (record['updated_at'], record['request_id']),
            reverse=True,
        )
        for record in terminal[self.terminal_limit:]:
            request_id = record['request_id']
            self._results.pop(request_id, None)
            try:
                os.unlink(self._path(request_id))
            except OSError:
                pass
