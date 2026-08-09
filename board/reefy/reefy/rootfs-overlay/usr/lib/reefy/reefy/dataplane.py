"""reefy.dataplane - the data-plane process (reefy-reconciler).

Serves the io.reefy.Reconciler Varlink interface and performs all the
storage/container work (apply desired-state, compose, restore, backup,
network, files, users) using reefy.storage.Storage. Runs as its own
process so a crash/OOM/hang here cannot take down the MQTT control
plane. It has no MQTT client of its own: dashboard events are published
by shelling out to reefy-mqtt-pub (device certs), which is non-fatal so
a publish failure can never abort the work.
"""

import base64
import codecs
from collections import deque
import contextlib
import hashlib
import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid as uuid_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

from reefy import shared
from reefy.apply_results import ApplyResultStore, TERMINAL_STATUSES
from reefy.shared import _part_dev, log
from reefy.storage import Storage


CDI_SPEC_DIRS = ('/etc/cdi', '/var/run/cdi', '/run/cdi')
_CDI_REQUEST = re.compile(
    r'^[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9_.-]*=[^=]+$')


def _drop_absent_devices(compose, exists=os.path.exists):
    """Remove device-passthrough entries whose /dev node is absent on this
    host, in place. Docker hard-fails the container start on a missing
    --device, so this makes any declared device optional: an app can ask
    for e.g. /dev/dri (iGPU accel) and it's simply omitted where it does
    not exist. CDI refs (e.g. nvidia.com/gpu=all) and any non-/dev entry
    are kept untouched. Returns the list of (service, /dev path) skipped."""
    skipped = []
    for sname, svc in (compose.get('services') or {}).items():
        devs = svc.get('devices')
        if not devs:
            continue
        kept = []
        for d in devs:
            host = d.split(':', 1)[0]
            if host.startswith('/dev/') and not exists(host):
                skipped.append((sname, host))
                continue
            kept.append(d)
        svc['devices'] = kept
    return skipped


def _cdi_resources(spec_dirs=CDI_SPEC_DIRS):
    """Return exact CDI resource names published by provider hooks.

    CDI permits JSON or YAML specifications. Reefy's Intel provider emits
    JSON, while NVIDIA Container Toolkit emits a small YAML document. This
    parser deliberately extracts only the top-level kind and device names;
    it does not interpret vendor hardware or container edits.
    """
    resources = set()
    paths = []
    for directory in spec_dirs:
        try:
            paths.extend(
                entry.path for entry in os.scandir(directory)
                if entry.is_file()
                and entry.name.rsplit('.', 1)[-1].lower()
                in ('json', 'yaml', 'yml'))
        except OSError:
            continue
    for path in sorted(set(paths)):
        try:
            with open(path, encoding='utf-8') as stream:
                content = stream.read(4 * 1024 * 1024 + 1)
        except OSError:
            continue
        if len(content) > 4 * 1024 * 1024:
            continue
        try:
            document = json.loads(content)
        except ValueError:
            document = None
        if isinstance(document, dict):
            kind = document.get('kind')
            devices = document.get('devices') or []
            if isinstance(kind, str):
                resources.update(
                    f'{kind}={device["name"]}'
                    for device in devices
                    if isinstance(device, dict)
                    and isinstance(device.get('name'), str))
            continue

        kind = ''
        names = []
        in_devices = False
        for line in content.splitlines():
            if line.startswith('kind:'):
                kind = line.split(':', 1)[1].strip().strip('"\'')
                continue
            if line == 'devices:':
                in_devices = True
                continue
            if in_devices and line and not line[0].isspace():
                in_devices = False
            if in_devices:
                match = re.match(r'^\s*-\s+name:\s*(.+?)\s*$', line)
                if match:
                    names.append(match.group(1).strip().strip('"\''))
        if kind:
            resources.update(f'{kind}={name}' for name in names if name)
    return resources


def _drop_unavailable_cdi_devices(compose, available=None):
    """Remove requested CDI devices not published by an activation hook.

    Provider activation is best effort. Docker rejects an unresolved CDI
    request before starting the container, so omit only those exact requests
    and let the application use its non-accelerated fallback.
    """
    available = _cdi_resources() if available is None else set(available)
    skipped = []
    for service_name, service in (compose.get('services') or {}).items():
        devices = service.get('devices')
        if not devices:
            continue
        kept = []
        for device in devices:
            if (isinstance(device, str) and _CDI_REQUEST.fullmatch(device)
                    and device not in available):
                skipped.append((service_name, device))
                continue
            kept.append(device)
        service['devices'] = kept
    return skipped


def _desired_state_log_summary(state):
    """Return a value-free structural summary for desired-state logging."""
    state = state if isinstance(state, dict) else {}
    compose = state.get('compose')
    services = compose.get('services') if isinstance(compose, dict) else None
    backup = state.get('backup')
    backup_instances = (
        backup.get('instances') if isinstance(backup, dict) else None)
    storage = state.get('storage')
    storage_devices = (
        storage.get('devices') if isinstance(storage, dict) else None)

    def list_count(value):
        return len(value) if isinstance(value, list) else 0

    return (
        'Saved desired state '
        f'(instances={list_count(state.get("instances"))}, '
        f'services={len(services) if isinstance(services, dict) else 0}, '
        f'app_volumes={list_count(state.get("app_volumes"))}, '
        f'files={list_count(state.get("files"))}, '
        f'storage_devices={list_count(storage_devices)}, '
        f'backup_instances={list_count(backup_instances)})'
    )


class DataPlane:
    # Shared constants (single source in reefy.shared).
    DESIRED_STATE_PATH = shared.DESIRED_STATE_PATH
    DESIRED_STATE_V2_PATH = shared.DESIRED_STATE_V2_PATH
    COMPOSE_PATH = shared.COMPOSE_PATH
    PROJECTS_DIR = shared.PROJECTS_DIR
    REEFY_DATA_MNT = shared.REEFY_DATA_MNT
    REEFY_DATA_MOUNT_OPTS = shared.REEFY_DATA_MOUNT_OPTS
    STORAGE_VG = shared.STORAGE_VG
    STORAGE_LV = shared.STORAGE_LV
    LEGACY_STORAGE_LV = shared.LEGACY_STORAGE_LV
    VARLINK_ADDRESS = shared.VARLINK_ADDRESS
    VARLINK_INTERFACE_DIR = shared.VARLINK_INTERFACE_DIR
    # Data-plane-only constants.
    BACKUP_DIR = '/mnt/reefy-data/state/backup'
    BACKUP_CONFIG_PATH = '/mnt/reefy-data/state/backup/config.json'
    BACKUP_SERVICE = 'reefy-backup'
    APPLY_RESULTS_DIR = '/mnt/reefy-data/state/apply-results'
    # Sticky terminal-failure guard: persisted signature of the last
    # compose that failed non-recoverably, so neither the reconcile loop
    # nor a reconciler restart / reboot re-pulls a doomed (multi-GB) image
    # again. Cleared on a changed compose or a successful apply.
    _FAILED_SIG_PATH = '/mnt/reefy-data/state/.failed-compose-sig'
    COMPOSE_PULL_BUDGET_SECONDS = 3600
    COMPOSE_PULL_MAX_ATTEMPTS = 5
    COMPOSE_START_TIMEOUT_SECONDS = 180
    COMPOSE_OUTPUT_TAIL_LINES = 200
    COMPOSE_OUTPUT_LINE_CHARS = 4000
    APP_RECONCILE_SUPERSEDED = 'app project superseded by newer desired state'
    _DEV_INJECTED_KEY_PATH = '/mnt/reefy/reefy/dev/authorized_keys'
    _FILES_ALLOWED_ROOTS = (
        '/mnt/reefy-data/apps/',
        '/mnt/reefy-data/state/',
    )

    def __init__(self, storage):
        self._storage = storage
        # Same dict object Storage reads in _ensure_volume_lv / _prepare_app_dirs.
        self._volume_caps = storage._volume_caps
        self._job_condition = threading.Condition()
        # The Varlink server dispatches each request in its own thread, so an
        # app restart can otherwise race the apply worker while both mutate
        # the same Compose project and container names.
        self._compose_mutation_lock = threading.Lock()
        self._project_locks_guard = threading.Lock()
        self._project_locks = {}
        self._project_targets_lock = threading.Lock()
        self._project_targets = {}
        self._app_reconcile_local = threading.local()
        self._health_operation_local = threading.local()
        self._artifact_retry_lock = threading.Lock()
        self._artifact_retry_timer = None
        self._artifact_retry_attempt = 0
        self._running_job = None
        self._pending_job = None
        self._last_apply_warnings = []
        self._runtime_result_errors = {}
        self._runtime_error_order = []
        self._apply_results = ApplyResultStore(self.APPLY_RESULTS_DIR)
        self._apply_results.fail_interrupted()
        # The data plane never runs control's setup(); load the MQTT
        # identity the backup config/timer need from the same files
        # reefy-mqtt-pub reads.
        cfg = shared.load_mqtt_config()
        self.broker = cfg.get('MQTT_BROKER')
        self.port = int(cfg.get('MQTT_PORT', '443'))
        self.topic_prefix = cfg.get('MQTT_TOPIC_PREFIX', 'reefy')
        self.device_uuid = shared.read_device_uuid()

    # --- Event publishing (no MQTT client; shell out to reefy-mqtt-pub) ---

    def _result_response(self, record):
        if record is None:
            return {
                'found': False,
                'request_id': '',
                'status': '',
                'error': '',
                'warnings': [],
                'applied': False,
            }
        return {
            'found': True,
            'request_id': record['request_id'],
            'status': record['status'],
            'error': self._runtime_result_errors.get(
                record['request_id'], record.get('error') or ''),
            'warnings': record.get('warnings') or [],
            'applied': bool(record.get('applied', False)),
        }

    def _remember_runtime_error(self, request_id, error):
        if not error:
            return
        self._runtime_result_errors[request_id] = error
        self._runtime_error_order.append(request_id)
        while len(self._runtime_error_order) > 34:
            expired = self._runtime_error_order.pop(0)
            self._runtime_result_errors.pop(expired, None)

    def _submit_apply_job(
            self, kind, state=None, force_retry=None, wait_for_idle=False):
        """Persist and enqueue one apply, replacing only the pending job."""
        if kind == 'apply':
            self._set_project_targets(state)
        request_id = str(uuid_mod.uuid4())
        job = {
            'request_id': request_id,
            'kind': kind,
            'state': state,
            'force_retry': force_retry,
        }
        with self._job_condition:
            while (wait_for_idle
                   and (self._running_job is not None
                        or self._pending_job is not None)):
                self._job_condition.wait()
            if not self._apply_results.create(request_id, kind):
                return {
                    'ok': False,
                    'request_id': '',
                    'error': 'cannot persist apply request',
                }
            if self._running_job is None:
                self._running_job = job
                try:
                    threading.Thread(
                        target=self._apply_job_worker,
                        args=(job,),
                        daemon=True,
                    ).start()
                except Exception:
                    self._running_job = None
                    self._apply_results.update(
                        request_id,
                        'failed',
                        error='cannot start apply worker',
                    )
                    self._job_condition.notify_all()
                    return {
                        'ok': False,
                        'request_id': request_id,
                        'error': 'cannot start apply worker',
                    }
            else:
                if self._pending_job is not None:
                    self._apply_results.update(
                        self._pending_job['request_id'], 'superseded')
                self._pending_job = job
            self._job_condition.notify_all()
        return {'ok': True, 'request_id': request_id, 'error': ''}

    def _apply_job_worker(self, job):
        """Finish the running job, then the latest pending job, serially."""
        while job is not None:
            request_id = job['request_id']
            self._apply_results.update(request_id, 'running')
            self._last_apply_warnings = []
            applied = False
            error = ''
            persistent_error = ''
            try:
                if job['kind'] == 'apply':
                    applied = True
                    ok = self._apply_state({'state': job['state']})
                else:
                    applied = os.path.exists(self._active_state_path())
                    ok = self._apply_desired_state(
                        force_retry=job.get('force_retry'))
                warnings = list(self._last_apply_warnings)
                if ok is False:
                    status = 'failed'
                    error = 'desired-state apply failed'
                    persistent_error = error
                elif warnings:
                    status = 'succeeded_with_warnings'
                else:
                    status = 'succeeded'
            except Exception as exception:
                status = 'failed'
                warnings = list(self._last_apply_warnings)
                error = shared.redact_log_message(exception)[:500]
                persistent_error = (
                    f'desired-state apply failed '
                    f'({type(exception).__name__})')
                log('mqtt',
                    f'[data-plane] apply job failed '
                    f'({type(exception).__name__}): {error}')
            self._remember_runtime_error(request_id, error)
            self._apply_results.update(
                request_id,
                status,
                error=persistent_error,
                warnings=warnings,
                applied=applied,
            )
            with self._job_condition:
                self._job_condition.notify_all()
                if self._pending_job is None:
                    self._running_job = None
                    job = None
                else:
                    job = self._pending_job
                    self._pending_job = None
                    self._running_job = job

    def _get_apply_result(self, request_id):
        return self._result_response(self._apply_results.get(request_id))

    def _wait_apply_result(self, request_id):
        with self._job_condition:
            while True:
                record = self._apply_results.get(request_id)
                if record is None or record['status'] in TERMINAL_STATUSES:
                    return self._result_response(record)
                self._job_condition.wait()

    def _publish_event(self, topic_suffix, payload):
        """Publish one MQTT event via reefy-mqtt-pub (device certs).
        Non-fatal: a publish failure must never abort apply/restore work."""
        try:
            subprocess.run(['reefy-mqtt-pub', topic_suffix, json.dumps(payload)],
                           capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            log('mqtt', f'[data-plane] event publish timed out ({topic_suffix})')
        except Exception as e:
            log('mqtt',
                f'[data-plane] event publish failed ({topic_suffix}) '
                f'({type(e).__name__})')

    def _publish_stage(self, stage, message=''):
        message = shared.redact_log_message(message)
        self._publish_event('stage', {'stage': stage, 'message': message,
                                      'timestamp': time.time()})

    def _publish_status(self, status, message=''):
        message = shared.redact_log_message(message)
        self._publish_event('status', {'status': status, 'message': message,
                                       'timestamp': time.time()})

    def _publish_instance_event(self, iuuid, action, status, extra=None):
        payload = {'instance_uuid': iuuid, 'action': action, 'status': status}
        if extra:
            payload.update(extra)
        self._publish_event('instance/status', payload)

    def _publish_restore_status(self, iuuid, status, archive, error=None):
        extra = {'archive': archive}
        if error:
            extra['error'] = shared.redact_log_message(error)[:500]
        self._publish_instance_event(iuuid, 'restore', status, extra=extra)

    @staticmethod
    def _app_desired_status(app):
        return (app or {}).get('desired_status') or 'running'

    @staticmethod
    def _app_desired_generation(app):
        value = (app or {}).get('desired_generation')
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    def _lifecycle_health_fields(self, iuuid):
        current = getattr(self._app_reconcile_local, 'value', None)
        if current and current[2] == iuuid and current[1] is not None:
            return {
                'desired_status': current[3],
                'observed_generation': current[1],
            }
        state = self._read_json(self._active_state_path())
        if not self._is_v2_state(state):
            return {}
        app = next((candidate for candidate in state.get('apps') or []
                    if candidate.get('instance_uuid') == iuuid), None)
        generation = self._app_desired_generation(app)
        if app is None or generation is None:
            return {}
        return {
            'desired_status': self._app_desired_status(app),
            'observed_generation': generation,
        }

    def _publish_health_status(
            self, iuuid, status, message=None, image=None, phase=None):
        extra = self._lifecycle_health_fields(iuuid)
        if message:
            extra['message'] = shared.redact_log_message(message)[:500]
        if image:
            # Running image, reported on 'running' so the server can show
            # the version actually on the device (vs the desired one).
            extra['image'] = image
        if phase:
            extra['phase'] = phase
        operation = getattr(self._health_operation_local, 'value', None)
        if operation and operation[0] == iuuid and operation[1]:
            extra['operation_id'] = operation[1]
        self._publish_instance_event(iuuid, 'health', status, extra=extra)

    @contextlib.contextmanager
    def _health_operation(self, instance_uuid, operation_id):
        previous = getattr(self._health_operation_local, 'value', None)
        self._health_operation_local.value = (instance_uuid, operation_id)
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._health_operation_local.value
                except AttributeError:
                    pass
            else:
                self._health_operation_local.value = previous

    @contextlib.contextmanager
    def _app_reconcile_context(self, app):
        previous = getattr(self._app_reconcile_local, 'value', None)
        self._app_reconcile_local.value = (
            (app or {}).get('project_name') or '',
            self._app_desired_generation(app),
            (app or {}).get('instance_uuid') or '',
            self._app_desired_status(app),
        )
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._app_reconcile_local.value
                except AttributeError:
                    pass
            else:
                self._app_reconcile_local.value = previous

    def _is_current_app_superseded(self):
        current = getattr(self._app_reconcile_local, 'value', None)
        if not current or not current[0]:
            return False
        with self._project_targets_lock:
            target = self._project_targets.get(current[0], current[1])
        return target != current[1]

    def _set_project_targets(self, state):
        """Publish the newest per-project generation to running workers."""
        if not self._is_v2_state(state):
            return
        targets = {
            app.get('project_name'): self._app_desired_generation(app)
            for app in state.get('apps') or []
            if app.get('project_name')
        }
        with self._project_targets_lock:
            for project_name in list(self._project_targets):
                if project_name not in targets:
                    self._project_targets[project_name] = object()
            self._project_targets.update(targets)

    def _wait_for_app_supersession(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if self._is_current_app_superseded():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25, remaining))

    def _send_command_response(self, cmd_id, status=None, message=None, error=None):
        # Data-plane work is invoked over Varlink (cmd_id is always None);
        # command responses are published by the control process.
        return

    @staticmethod
    def _is_v2_state(state):
        return isinstance(state, dict) and state.get('schema_version') == 2

    def _valid_v2_lifecycle(self, state):
        """Accept legacy running entries while validating new lifecycle data."""
        for app in state.get('apps') or []:
            desired_status = app.get('desired_status')
            if desired_status is not None and desired_status not in (
                    'running', 'stopped'):
                log('mqtt', 'ERROR: invalid app desired_status')
                return False
            if 'desired_generation' in app:
                generation = app.get('desired_generation')
                if (not isinstance(generation, int)
                        or isinstance(generation, bool)
                        or generation <= 0):
                    log('mqtt', 'ERROR: invalid app desired_generation')
                    return False
        return True

    def _active_state_path(self):
        if os.path.exists(self.DESIRED_STATE_V2_PATH):
            return self.DESIRED_STATE_V2_PATH
        return self.DESIRED_STATE_PATH

    def _project_lock(self, project_name):
        with self._project_locks_guard:
            return self._project_locks.setdefault(
                project_name, threading.RLock())

    def _project_is_busy(self, project_name):
        lock = self._project_lock(project_name)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return not acquired

    def _project_compose_path(self, project_name):
        return os.path.join(
            self.PROJECTS_DIR, project_name, 'compose.json')

    @staticmethod
    def _flatten_v2_state(state):
        """Return the legacy-shaped host/storage view of schema v2."""
        host = json.loads(json.dumps(state.get('host') or {}))
        host['instances'] = json.loads(json.dumps(
            state.get('instances') or []))
        app_volumes = []
        files = list(host.get('files') or [])
        volume_caps = {}
        backup_instances = []
        for app in state.get('apps') or []:
            app_volumes.extend(json.loads(json.dumps(
                app.get('volumes') or [])))
            files.extend(json.loads(json.dumps(app.get('files') or [])))
            volume_caps.update(app.get('volume_caps') or {})
            if app.get('backup'):
                backup_instances.append(json.loads(json.dumps(
                    app['backup'])))
        if app_volumes:
            host['app_volumes'] = app_volumes
        if files:
            host['files'] = files
        if volume_caps:
            host['volume_caps'] = volume_caps
        backup_policy = json.loads(json.dumps(
            state.get('backup_policy') or {}))
        if backup_instances:
            backup_policy['instances'] = backup_instances
        if backup_policy:
            host['backup'] = backup_policy
        return host

    def _apply_state_command(self, payload, cmd_id=None):
        """Compatibility wrapper around the request/result scheduler."""
        submission = self._submit_apply_job(
            'apply', state=payload.get('state'))
        if not submission['ok']:
            return False
        result = self._wait_apply_result(submission['request_id'])
        return result['status'] in ('succeeded', 'succeeded_with_warnings')

    def _backup_now(self, payload, cmd_id=None):
        """Trigger immediate backup for a specific instance via reefy-backup."""
        instance_uuid = payload.get('instance_uuid')
        if not instance_uuid:
            print("[mqtt] backup_now: missing instance_uuid")
            return
        config_path = '/mnt/reefy-data/state/backup/config.json'
        if not os.path.exists(config_path):
            print("[mqtt] backup_now: no backup config found")
            return
        log('mqtt', f'Starting manual backup for instance {instance_uuid}')
        threading.Thread(
            target=self._run_backup,
            args=(instance_uuid,),
            daemon=True,
        ).start()

    def _run_backup(self, instance_uuid=None):
        """Run reefy-backup, optionally for a single instance.
        Stream stdout/stderr line by line so logs appear in real time."""
        cmd = ['/usr/bin/reefy-backup']
        if instance_uuid:
            cmd.append(instance_uuid)
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'  # Unbuffered Python output
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env
            )
            start_time = time.time()
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log('backup', line)
                if time.time() - start_time > 3600:
                    proc.kill()
                    print("[mqtt] Backup timed out (1h)")
                    return
            proc.wait()
            if proc.returncode != 0:
                log('mqtt', f'Backup failed (exit {proc.returncode})')
        except Exception as e:
            log('mqtt', f'Backup error: {e}')

    def _retry_pending_migration_app(self, app, compose, failed_record):
        """Retry one failed preflight through the serialized full handoff."""
        instance_uuid = app.get('instance_uuid') or ''
        project_name = app.get('project_name') or ''
        primary_service = app.get('primary_service') or 'app'
        retry_intent = {
            'project_name': project_name,
            'signature': failed_record.get('signature') or '',
        }
        submitted = self._submit_apply_job(
            'reconcile', force_retry=retry_intent, wait_for_idle=True)
        if not submitted.get('ok'):
            raise RuntimeError(
                submitted.get('error')
                or 'could not enqueue migration retry')
        log('mqtt', f'Queued migration retry for {project_name}')
        result = self._wait_apply_result(submitted['request_id'])
        latest_state = self._read_json(self._active_state_path())
        latest_app = None
        if self._is_v2_state(latest_state):
            latest_app = next((
                candidate for candidate in latest_state.get('apps') or []
                if (candidate.get('instance_uuid') == instance_uuid
                    and candidate.get('project_name') == project_name)), None)
        current_failure = {}
        if latest_app is not None:
            latest_compose = json.loads(json.dumps(
                latest_app.get('compose') or {}))
            _drop_absent_devices(latest_compose)
            _drop_unavailable_cdi_devices(latest_compose)
            current_failure = self._current_required_app_failure(
                project_name, latest_compose,
                latest_app.get('primary_service') or 'app')
        retry_succeeded = (
            result.get('status') in ('succeeded', 'succeeded_with_warnings')
            and not self._v2_migration_pending()
            and latest_app is not None
            and self._app_desired_status(latest_app) == 'running'
            and not current_failure)
        if retry_succeeded:
            return f'Instance {instance_uuid} restarted'

        reason = (
            current_failure.get('reason')
            or result.get('error')
            or 'migration retry did not complete')
        raise RuntimeError(reason)

    def _restart_instance(self, payload, cmd_id=None):
        instance_uuid = payload.get('instance_uuid')
        with self._health_operation(instance_uuid, cmd_id):
            return self._restart_instance_inner(payload, cmd_id=cmd_id)

    def _restart_instance_inner(self, payload, cmd_id=None):
        """Recreate an app instance container with current config.

        Uses `up -d --force-recreate` rather than `restart`. Plain
        `restart` keeps the same container - it bounces the process
        but never re-reads the compose file, so any config change
        the user made (env vars, ports, GPU directive, etc.) since
        the container was last created stays UNapplied. Recreate
        guarantees the running container reflects the current
        compose. Costs ~1-2s extra; in-memory state isn't a concern
        for our apps since durable state lives in mounted volumes.
        """
        instance_uuid = payload.get('instance_uuid')
        if not instance_uuid:
            raise ValueError('missing instance_uuid')

        svc_id = instance_uuid
        self._send_command_response(cmd_id, status='running',
                                    message=f'Restarting {svc_id}...')

        state = self._read_json(self._active_state_path())
        if self._is_v2_state(state):
            app = next((candidate for candidate in state.get('apps') or []
                        if candidate.get('instance_uuid') == instance_uuid),
                       None)
            if app is None:
                raise RuntimeError('App project not found')
            if self._app_desired_status(app) == 'stopped':
                raise RuntimeError('App is stopped')
            project_name = app.get('project_name') or ''
            if self._project_is_busy(project_name):
                raise RuntimeError('App lifecycle operation is in progress')
            compose_path = self._project_compose_path(project_name)
            compose = json.loads(json.dumps(app.get('compose') or {}))
            for _service, host_path in _drop_absent_devices(compose):
                log('reconciler',
                    f'{project_name}: skipping absent device {host_path}')
            for _service, resource in _drop_unavailable_cdi_devices(compose):
                log('reconciler',
                    f'{project_name}: skipping unavailable CDI resource '
                    f'{resource}')
            with self._project_lock(project_name):
                failed_record = self._current_required_app_failure(
                    project_name, compose,
                    app.get('primary_service') or 'app')
            if self._v2_migration_pending():
                if not failed_record:
                    raise RuntimeError(
                        'Apps-v2 migration is in progress; retry later')
                return self._retry_pending_migration_app(
                    app, compose, failed_record)

            with self._project_lock(project_name):
                failed_record = self._current_required_app_failure(
                    project_name, compose,
                    app.get('primary_service') or 'app')
                if failed_record:
                    log('mqtt',
                        f'Retrying failed app project {project_name}')
                    if not self._prepare_app_artifacts(app):
                        self._publish_health_status(
                            instance_uuid, 'failed',
                            message='artifact prepare failed')
                        raise RuntimeError('artifact prepare failed')
                    ok, _optional_failures = (
                        self._apply_app_project_compose(
                            project_name, compose,
                            instance_uuid=instance_uuid,
                            primary_service=(
                                app.get('primary_service') or 'app'),
                            force_retry=True))
                    if not ok:
                        current = self._read_failed_sig_record(
                            os.path.join(
                                self.PROJECTS_DIR, project_name,
                                '.failed-compose-sig'))
                        raise RuntimeError(
                            current.get('reason')
                            or 'app project recovery failed')
                    log('mqtt',
                        f'App project {project_name} recovered')
                    return f'Instance {svc_id} restarted'

                self._publish_health_status(
                    instance_uuid, 'starting', phase='start')
                result = subprocess.run(
                    ['docker', 'compose', '-f', compose_path, '-p',
                     project_name, 'up', '-d', '--force-recreate',
                     '--no-deps', app.get('primary_service') or 'app'],
                    capture_output=True, text=True,
                    timeout=self.COMPOSE_START_TIMEOUT_SECONDS)
                if result.returncode != 0:
                    raise RuntimeError(
                        result.stderr.strip()
                        or 'docker compose up --force-recreate failed '
                           f'(exit {result.returncode})')
                image = (((app.get('compose') or {}).get('services') or {})
                         .get(app.get('primary_service') or 'app') or {}).get(
                             'image')
                self._publish_health_status(
                    instance_uuid, 'running', image=image)
            log('mqtt', f'App project {project_name} recreated')
            return f'Instance {svc_id} restarted'

        with self._compose_mutation_lock:
            if not os.path.exists(self.COMPOSE_PATH):
                raise RuntimeError('No docker-compose.json found')
            log('mqtt',
                f'Recreating instance {svc_id} with current compose config')

            result = subprocess.run(
                ['docker', 'compose', '-f', self.COMPOSE_PATH, '-p', 'state',
                 'up', '-d', '--force-recreate', '--no-deps', svc_id],
                capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip()
                    or 'docker compose up --force-recreate failed '
                       f'(exit {result.returncode})')

        log('mqtt', f'Instance {svc_id} recreated')
        return f'Instance {svc_id} restarted'

    def _apply_state(self, payload):
        """Handle apply_state command — save and apply desired state."""
        state = payload.get('state', {})
        if not state:
            print("[mqtt] ERROR: Empty state in apply_state")
            return False

        incoming_v2 = self._is_v2_state(state)
        if incoming_v2 and not self._valid_v2_lifecycle(state):
            return False
        if not incoming_v2 and os.path.exists(self.DESIRED_STATE_V2_PATH):
            log('mqtt', 'ERROR: refusing desired-state schema downgrade after '
                'Apps-v2 migration')
            return False
        target_path = (
            self.DESIRED_STATE_V2_PATH if incoming_v2
            else self.DESIRED_STATE_PATH)

        # Read old state before overwriting (for diff-based cleanup)
        old_state = None
        try:
            old_path = self._active_state_path()
            if os.path.exists(old_path):
                with open(old_path) as f:
                    old_state = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

        # Save to persistent storage
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            tmp_path = target_path + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
            log('mqtt', _desired_state_log_summary(state))
        except OSError as e:
            log('mqtt', f'ERROR: Failed to save desired state: {e}')
            return False

        # A changed desired state is an immediate event and supersedes any
        # delayed retry scheduled for the previous artifact requirements.
        self._reset_artifact_retry()

        # Data plane applies directly; the control process publishes the
        # applying/ready stages around its Varlink call.
        return self._apply_desired_state(old_state=old_state)

    def _apply_desired_state(self, old_state=None, force_retry=None):
        """Load saved desired state and apply it (hostname, compose, proxy).
        If no desired state exists, reset hostname to MAC-based default.
        old_state: previous desired state for diff-based cleanup (None on boot).
        Returns True on success, False on failure."""
        self._last_apply_warnings = []
        active_path = self._active_state_path()
        if not os.path.exists(active_path):
            shared.set_hostname(shared.get_default_hostname())
            return True

        try:
            with open(active_path) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log('mqtt', f'ERROR: Failed to read desired state: {e}')
            return False

        v2_state = state if self._is_v2_state(state) else None
        runtime_state = (
            self._flatten_v2_state(state) if v2_state is not None else state)
        runtime_old_state = (
            self._flatten_v2_state(old_state)
            if self._is_v2_state(old_state) else old_state)
        state = runtime_state
        old_state = runtime_old_state

        # Fair-share volume caps (path -> % of pool) consumed by Storage's
        # _ensure_volume_lv when it creates per-volume LVs. Push onto the
        # Storage instance (it owns the dict the volume ops read).
        self._volume_caps = state.get('volume_caps') or {}
        self._storage.set_volume_caps(self._volume_caps)

        # Apply hostname (or revert to default if not specified)
        hostname = state.get('hostname', '') or shared.get_default_hostname()
        shared.set_hostname(hostname)

        # Apply WiFi config (before compose — connectivity may be needed for image pulls)
        wifi = state.get('wifi')
        old_wifi = old_state.get('wifi') if old_state else None
        self._apply_wifi(wifi, old_wifi)

        # Apply storage config (LUKS + LVM encrypted internal storage)
        storage = state.get('storage')
        if storage and storage.get('devices'):
            self._apply_storage(storage)

        # Apply network config (static IPs)
        network = state.get('network')
        old_network = old_state.get('network') if old_state else None
        self._apply_network(network, old_network)

        # Apply user-registered SSH public keys. The user adds them in
        # their account settings on the cloud dashboard, the cloud
        # ships them in desired-state, and we rewrite authorized_keys
        # atomically each tick. Empty list = delete the file (back to
        # password-only auth).
        self._apply_user_ssh_keys(state.get('user_ssh_keys', []))

        # Mirror installed app instances to `app-<name>` system users so
        # per-app SSH (`ssh app-<name>@host`) routes via the sshd_config
        # ForceCommand into the right container. Runs after SSH-keys
        # apply so any newly-added user inherits the latest authorized_keys
        # set on first connection.
        self._sync_app_users(state.get('instances', []))

        # Build the set of paths that should be backed by per-volume
        # thin LVs (one LV per backup-true volume — see PLAN-backup.md
        # §"Storage Layout"). Pulled from the backup section so dirs
        # creation knows whether to mount an LV or just mkdir.
        backup = state.get('backup')
        backup_paths = set()
        if backup:
            for inst in backup.get('instances', []):
                backup_paths.update(inst.get('paths', []))

        # Prepare app data directories (host path mounts with correct ownership)
        app_volumes = state.get('app_volumes', [])
        if app_volumes:
            warnings = self._storage._prepare_app_dirs(
                app_volumes, backup_paths=backup_paths)
            if isinstance(warnings, list):
                self._last_apply_warnings = warnings

        # Apply backup config (SSH keys, config, systemd timer)
        if backup:
            self._apply_backup_config(backup)

        # Restore instances from backup before starting containers
        # Returns set of instance_uuids that failed restore — those must not start
        failed_restores = set()
        if backup and backup.get('instances'):
            failed_restores = self._restore_instances(backup)

        # Apply the generic `files` directive: write any files the
        # backend has rendered (credential bootstrap bundles, configs,
        # certs, ...) to the host before docker compose up so that
        # any compose service that bind-mounts them sees real files
        # at run-time (docker creates an empty DIR at the host path
        # if the source doesn't exist when the bind-mount is
        # established). Done AFTER _prepare_app_dirs so the per-
        # instance parent dir exists.
        self._apply_files(state.get('files', []))

        # Schema v2 owns one system project plus one project per app. Each app
        # reconciles independently; one bad app becomes a warning rather than
        # failing the complete device apply. Legacy firmware keeps the
        # monolithic path below unchanged.
        if v2_state is not None:
            project_failures = self._apply_v2_projects(
                v2_state, failed_restores=failed_restores,
                force_retry=force_retry)
            self._last_apply_warnings.extend(project_failures)

        # Write legacy compose file and run the monolithic project.
        compose = state.get('compose')
        if v2_state is None and compose:
            # Remove services for instances whose restore failed
            if failed_restores and 'services' in compose:
                for iuuid in failed_restores:
                    if iuuid in compose['services']:
                        del compose['services'][iuuid]
                        log('mqtt', f'Excluded {iuuid} from compose (restore failed)')
                    tty_svc = f'{iuuid}-tty'
                    if tty_svc in compose['services']:
                        del compose['services'][tty_svc]

            # Make device passthrough optional: drop any /dev node that is
            # absent on this host so it degrades to omission instead of
            # hard-failing the container start (docker errors on a missing
            # --device). Lets an app ask for e.g. /dev/dri (iGPU accel) and
            # fall back to its CPU path where there's no GPU.
            for _sname, _host in _drop_absent_devices(compose):
                log('mqtt', f'{_sname}: skipping absent device {_host}')

            # Defense-in-depth: refuse an internally-inconsistent state.
            # Every registered instance must have a matching compose
            # service. A gap (e.g. a backend catalog miss that dropped an
            # app from `services` but left it in `instances`) would, under
            # `docker compose up --remove-orphans` below, DELETE that
            # instance's running container - the cross-visitor data-loss
            # bug we hit. Abort instead so the container survives until a
            # correct state arrives. failed_restores are excluded: those
            # were intentionally dropped from services just above. A
            # genuine uninstall removes the instance from BOTH lists, so
            # this never blocks normal app deletion.
            services = compose.get('services', {})
            orphaned = [
                i for i in state.get('instances', [])
                if i.get('instance_uuid')
                and i['instance_uuid'] not in services
                and i['instance_uuid'] not in failed_restores
            ]
            if orphaned:
                names = ', '.join(
                    f"{i.get('instance_name', '?')}"
                    f"({(i.get('instance_uuid') or '?')[:8]})"
                    for i in orphaned)
                msg = (f'inconsistent desired-state: {len(orphaned)} '
                       f'registered instance(s) missing from compose '
                       f'services [{names}] - refusing apply (would '
                       f'--remove-orphans delete them)')
                log('mqtt', f'ERROR: {msg}')
                self._publish_stage('error', msg)
                return False

            if not self._apply_compose(compose):
                self._publish_stage('error', 'docker compose up failed')
                return False

        # Reclaim per-volume LVs whose owning instance was deleted since the
        # last apply, so a removed app frees its pool space instead of
        # leaking an orphaned LV. Done AFTER docker compose up (which runs
        # with --remove-orphans) so the deleted instance's container is gone
        # and no longer bind-mounts the volume - otherwise umount/lvremove
        # fail with "filesystem in use" and the LV leaks (the prod bug, and
        # the e2e backup-lvm failure). Keyed on the *instance* being gone
        # (uuid absent from the whole new state), NOT merely a path leaving
        # the backup set - a volume that only un-backup-flags keeps live
        # data and must never be reclaimed. e2e covers both directions.
        # Always inspect self-identifying managed LVs. Their LVM tags survive
        # reboot, so a prior busy unmount/lvremove gets another reclaim
        # attempt even when old_state is unavailable or unchanged.
        self._storage._reclaim_deleted_instance_lvs(
            old_state or {}, state, backup_paths)

        return True

    def _publish_migration_legacy_running(
            self, state, legacy_compose, instance_uuids):
        """Restore terminal health for legacy apps left or put back live."""
        instance_uuids = set(instance_uuids or ())
        legacy_services = legacy_compose.get('services') or {}
        for app in state.get('apps') or []:
            instance_uuid = app.get('instance_uuid') or ''
            if (instance_uuid not in instance_uuids
                    or self._app_desired_status(app) != 'running'
                    or instance_uuid not in legacy_services):
                continue
            image = (legacy_services.get(instance_uuid) or {}).get('image')
            self._publish_health_status(
                instance_uuid, 'running', image=image)

    def _publish_migration_v2_running(self, state, prepared_apps):
        """Publish one committed terminal event for each running v2 app."""
        for app in state.get('apps') or []:
            instance_uuid = app.get('instance_uuid') or ''
            prepared = prepared_apps.get(instance_uuid)
            if (prepared is None
                    or self._app_desired_status(app) != 'running'):
                continue
            terminal = prepared.get('_running_health') or {}
            image = terminal.get('image')
            if image is None:
                primary_service = app.get('primary_service') or 'app'
                image = ((app.get('compose') or {}).get('services') or {}).get(
                    primary_service, {}).get('image')
            self._publish_health_status(
                instance_uuid, 'running',
                message=terminal.get('message'), image=image)

    def _apply_v2_projects(
            self, state, failed_restores=None, force_retry=None):
        """Reconcile the system project, then all app projects concurrently.

        Returns structured ApplyWarning records. A project failure does not
        block unrelated projects or make the device-wide desired state fail.
        The one-time v1-to-v2 handoff is stricter: apps remain on the legacy
        project until the v2 system project is running, and any app failure
        rolls the whole handoff back so platform DNS never splits across the
        legacy and v2 networks.
        """
        failed_restores = set(failed_restores or ())
        warnings = []
        system = state.get('system_project') or {}
        system_name = system.get('project_name') or 'reefy-system'
        system_compose = system.get('compose') or {'services': {}}
        migration = self._v2_migration_pending()
        migration_failed = False
        failed_app_ids = set()
        legacy_compose = self._read_json(self.COMPOSE_PATH) if migration else {}
        legacy_instances = {
            row.get('instance_uuid')
            for row in (state.get('instances') or [])
            if row.get('instance_uuid')
        }

        legacy_system_services = [
            name for name in (legacy_compose.get('services') or {})
            if name not in legacy_instances
        ]

        apps = list(state.get('apps') or [])
        prepared_apps = {}
        if migration:
            # Migration is a two-phase handoff. Pull every required image
            # while the complete legacy project is still running, then
            # release names and start the already-prepared v2 projects.
            prepared_system = self._prepare_system_project_compose(
                system_name, system_compose, migration=True)
            if not prepared_system.get('ok'):
                warnings.append({
                    'code': 'system_project_failed',
                    'instance_uuid': '',
                    'volume': '',
                })
                self._reset_artifact_retry()
                return warnings

            if apps:
                with ThreadPoolExecutor(max_workers=len(apps)) as pool:
                    futures = {}
                    for app in apps:
                        restore_failed = (
                            app.get('instance_uuid') in failed_restores)
                        if (force_retry
                                and force_retry.get('project_name')
                                == app.get('project_name')):
                            future = pool.submit(
                                self._prepare_v2_app, app, restore_failed,
                                force_retry=force_retry)
                        else:
                            future = pool.submit(
                                self._prepare_v2_app, app, restore_failed)
                        futures[future] = app
                    for future in as_completed(futures):
                        app = futures[future]
                        try:
                            ok, code, prepared = future.result()
                        except Exception as exception:
                            ok = False
                            code = (
                                'app_project_exception_'
                                f'{type(exception).__name__}')
                            prepared = None
                            log('reconciler',
                                f'{app.get("project_name")}: {code}')
                        if ok:
                            prepared_apps[
                                app.get('instance_uuid') or ''] = prepared
                            continue
                        if code == 'app_project_superseded':
                            migration_failed = True
                            continue
                        migration_failed = True
                        failed_app_ids.add(app.get('instance_uuid') or '')
                        warnings.append({
                            'code': code or 'app_project_failed',
                            'instance_uuid': app.get('instance_uuid') or '',
                            'volume': '',
                        })
            if migration_failed:
                self._publish_migration_legacy_running(
                    state, legacy_compose, prepared_apps)
                if any(
                        warning.get('code') == 'artifact_prepare_failed'
                        for warning in warnings):
                    self._schedule_artifact_retry()
                else:
                    self._reset_artifact_retry()
                return warnings

            def system_handoff():
                return self._prepare_v2_system_handoff(
                    system_name, system_compose,
                    legacy_system_services)

            if not self._start_prepared_system_project(
                    prepared_system, before_start=system_handoff):
                warnings.append({
                    'code': 'system_project_failed',
                    'instance_uuid': '',
                    'volume': '',
                })
                if self._rollback_v2_migration(state, system_name):
                    self._publish_migration_legacy_running(
                        state, legacy_compose, prepared_apps)
                self._reset_artifact_retry()
                return warnings
        elif not self._apply_project_compose(
                system_name, system_compose, instance_uuid=None):
            warnings.append({
                'code': 'system_project_failed',
                'instance_uuid': '',
                'volume': '',
            })

        desired_projects = {
            app.get('project_name') for app in (state.get('apps') or [])
            if app.get('project_name')
        }
        self._remove_absent_v2_projects(desired_projects)

        if apps:
            with ThreadPoolExecutor(max_workers=len(apps)) as pool:
                futures = {
                    pool.submit(
                        self._reconcile_v2_app, app, migration,
                        app.get('instance_uuid') in failed_restores,
                        prepared_apps.get(
                            app.get('instance_uuid') or '')): app
                    for app in apps
                }
                for future in as_completed(futures):
                    app = futures[future]
                    try:
                        ok, outcome = future.result()
                    except Exception as exception:
                        ok = False
                        outcome = (
                            f'app_project_exception_{type(exception).__name__}',
                            [])
                        log('reconciler',
                            f'{app.get("project_name")}: {outcome[0]}')
                    if isinstance(outcome, tuple):
                        code, optional_failures = outcome
                    else:
                        code, optional_failures = outcome, []
                    if not ok:
                        if code == 'app_project_superseded':
                            if migration:
                                migration_failed = True
                            continue
                        migration_failed = True
                        failed_app_ids.add(
                            app.get('instance_uuid') or '')
                        warnings.append({
                            'code': code or 'app_project_failed',
                            'instance_uuid': app.get('instance_uuid') or '',
                            'volume': '',
                        })
                    warnings.extend({
                        'code': 'optional_service_failed',
                        'instance_uuid': app.get('instance_uuid') or '',
                        'volume': service_name,
                    } for service_name in optional_failures)

        if migration:
            if migration_failed:
                if self._rollback_v2_migration(state, system_name):
                    self._publish_migration_legacy_running(
                        state, legacy_compose,
                        set(prepared_apps) - failed_app_ids)
            else:
                if not self._commit_v2_migration():
                    warnings.append({
                        'code': 'system_project_failed',
                        'instance_uuid': '',
                        'volume': '',
                    })
                    if self._rollback_v2_migration(state, system_name):
                        self._publish_migration_legacy_running(
                            state, legacy_compose, prepared_apps)
                else:
                    self._publish_migration_v2_running(
                        state, prepared_apps)
        if any(
                warning.get('code') == 'artifact_prepare_failed'
                for warning in warnings):
            self._schedule_artifact_retry()
        else:
            self._reset_artifact_retry()
        return warnings

    def _prepare_v2_system_handoff(
            self, system_name, system_compose, legacy_system_services):
        """Release legacy system container names before starting v2.

        `docker compose stop` is insufficient for services with an explicit
        `container_name`: the stopped container still reserves that name and
        prevents the v2 system project from creating its replacement. Remove
        only legacy system services here. Legacy app containers stay stopped
        at their individual final handoffs and remain available for rollback.
        """
        with self._project_lock(system_name):
            compose_path = self._write_project_compose(
                system_name, system_compose)

            # Remove debris from a prior partial attempt before releasing the
            # working legacy services. This is safe because migration has not
            # committed and the legacy compose file remains authoritative.
            ok, output = self._run_compose_command(
                compose_path, system_name, ['rm', '-s', '-f'], timeout=180)
            if not ok:
                reason = self._failure_reason(
                    self._classify_compose_failure(output), output)
                log('reconciler',
                    f'{system_name}: could not remove partial system '
                    f'project: {reason}')
                return False

            if legacy_system_services:
                ok, _ = self._run_compose_command(
                    self.COMPOSE_PATH, 'state',
                    ['stop', *legacy_system_services], timeout=180)
                if not ok:
                    return False
                ok, _ = self._run_compose_command(
                    self.COMPOSE_PATH, 'state',
                    ['rm', '-f', *legacy_system_services], timeout=180)
                if not ok:
                    return False

            # A previous name collision may have cached this exact Compose
            # signature as failed. Releasing the legacy names changes the
            # runtime precondition, so allow the prepared v2 start to retry.
            self._clear_project_failed_sigs(system_name)
            return True

    def _rollback_v2_migration(self, state, system_name):
        """Restore the monolithic v1 project after a handoff failure."""
        cleanup_failures = []
        for app in state.get('apps') or []:
            project_name = app.get('project_name') or ''
            compose_path = self._project_compose_path(project_name)
            if project_name and os.path.exists(compose_path):
                with self._project_lock(project_name):
                    ok, output = self._run_compose_command(
                        compose_path, project_name, ['stop'], timeout=180)
                    if not ok:
                        cleanup_failures.append(
                            f'{project_name}: '
                            f'{self._bounded_output_tail(output, "stop failed")}')

        system_path = self._project_compose_path(system_name)
        if os.path.exists(system_path):
            with self._project_lock(system_name):
                ok, output = self._run_compose_command(
                    system_path, system_name, ['rm', '-s', '-f'], timeout=180)
                if not ok:
                    cleanup_failures.append(
                        f'{system_name}: '
                        f'{self._bounded_output_tail(output, "remove failed")}')

        if cleanup_failures:
            log('reconciler',
                'Apps-v2 rollback cleanup failures: '
                + ' | '.join(cleanup_failures))

        ok, output = self._run_compose_command(
            self.COMPOSE_PATH, 'state',
            ['up', '-d', '--pull', 'missing'], timeout=300)
        if not ok:
            reason = self._failure_reason(
                self._classify_compose_failure(output), output)
            log('reconciler', f'Apps-v2 migration rollback failed: {reason}')
        return ok and not cleanup_failures

    def _clear_project_failed_sigs(self, project_name):
        with self._project_lock(project_name):
            project_dir = os.path.dirname(
                self._project_compose_path(project_name))
            try:
                entries = os.scandir(project_dir)
            except OSError:
                return
            with entries:
                for entry in entries:
                    if not entry.name.startswith('.failed-compose-sig'):
                        continue
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass

    def _schedule_artifact_retry(self):
        """Queue one exponential, event-driven retry for unavailable bytes.

        This is a one-shot timer created only by a failed transfer. It is not
        periodic state polling. A new desired state or a successful reconcile
        cancels and resets it immediately.
        """
        with self._artifact_retry_lock:
            if (self._artifact_retry_timer is not None
                    and self._artifact_retry_timer.is_alive()):
                return
            delay = min(10 * (2 ** self._artifact_retry_attempt), 300)
            self._artifact_retry_attempt += 1

            def retry():
                with self._artifact_retry_lock:
                    self._artifact_retry_timer = None
                log('reconciler',
                    f'artifact backoff elapsed; queueing reconcile '
                    f'(delay={delay}s)')
                self._submit_apply_job('reconcile')

            timer = threading.Timer(delay, retry)
            timer.daemon = True
            self._artifact_retry_timer = timer
            timer.start()

    def _reset_artifact_retry(self):
        with self._artifact_retry_lock:
            timer = self._artifact_retry_timer
            self._artifact_retry_timer = None
            self._artifact_retry_attempt = 0
            if timer is not None:
                timer.cancel()

    def _prepare_v2_app(self, app, restore_failed, force_retry=None):
        with self._app_reconcile_context(app):
            return self._prepare_v2_app_inner(
                app, restore_failed, force_retry=force_retry)

    def _prepare_v2_app_inner(self, app, restore_failed, force_retry=None):
        instance_uuid = app.get('instance_uuid') or ''
        project_name = app.get('project_name') or ''
        if not instance_uuid or not project_name:
            return False, 'invalid_app_project', None
        if restore_failed:
            self._publish_health_status(
                instance_uuid, 'failed', message='restore failed')
            return False, 'restore_failed', None

        compose = json.loads(json.dumps(app.get('compose') or {}))
        for _service, host_path in _drop_absent_devices(compose):
            log('reconciler',
                f'{project_name}: skipping absent device {host_path}')

        if self._app_desired_status(app) == 'stopped':
            return True, '', {
                'ok': True,
                'stopped': True,
                'project_name': project_name,
                'compose': compose,
                'instance_uuid': instance_uuid,
            }

        if not self._prepare_app_artifacts(app):
            if self._is_current_app_superseded():
                return False, 'app_project_superseded', None
            self._publish_health_status(
                instance_uuid, 'failed',
                message='artifact prepare failed', phase='artifact')
            return False, 'artifact_prepare_failed', None

        for _service, resource in _drop_unavailable_cdi_devices(compose):
            log('reconciler',
                f'{project_name}: skipping unavailable CDI resource '
                f'{resource}')

        prepared = self._prepare_app_project_compose(
            project_name, compose, instance_uuid=instance_uuid,
            primary_service=app.get('primary_service') or 'app',
            force_retry=bool(force_retry),
            force_retry_signature=(
                force_retry.get('signature') if force_retry else None))
        if not prepared.get('ok'):
            return False, prepared.get('code') or 'app_project_failed', None
        return True, '', prepared

    def _reconcile_v2_app(
            self, app, migration, restore_failed, prepared=None):
        with self._app_reconcile_context(app):
            return self._reconcile_v2_app_inner(
                app, migration, restore_failed, prepared=prepared)

    def _reconcile_v2_app_inner(
            self, app, migration, restore_failed, prepared=None):
        instance_uuid = app.get('instance_uuid') or ''
        project_name = app.get('project_name') or ''
        if prepared is None:
            ok, code, prepared = self._prepare_v2_app(
                app, restore_failed)
            if not ok:
                return False, code

        with self._project_lock(project_name):
            if prepared.get('stopped'):
                if migration:
                    ok, output = self._run_compose_command(
                        self.COMPOSE_PATH, 'state',
                        ['stop', instance_uuid], timeout=180)
                    if not ok:
                        self._publish_health_status(
                            instance_uuid, 'failed',
                            message=self._bounded_output_tail(
                                output, 'legacy app stop failed'),
                            phase='stop')
                        return False, 'app_stop_failed'
                self._write_project_compose(
                    project_name, prepared['compose'])
                ok, output = self._run_compose_command(
                    self._project_compose_path(project_name), project_name,
                    ['stop'], timeout=180)
                if not ok:
                    self._publish_health_status(
                        instance_uuid, 'failed',
                        message=self._bounded_output_tail(
                            output, 'app stop failed'), phase='stop')
                    return False, 'app_stop_failed'
                self._clear_project_failed_sigs(project_name)
                self._publish_health_status(instance_uuid, 'stopped')
                return True, ''

            # Keep a runnable legacy service alive through artifact and image
            # preparation. Stop it only when startup can use cached images.
            if migration:
                ok, output = self._run_compose_command(
                    self.COMPOSE_PATH, 'state', ['stop', instance_uuid],
                    timeout=180)
                if not ok:
                    reason = 'legacy app stop failed'
                    context = prepared.get('required_context')
                    if context:
                        self._write_failed_sig_path(
                            context['path'], context['signature'], reason,
                            phase='start', services=context['services'])
                    self._publish_health_status(
                        instance_uuid, 'failed',
                        message=self._bounded_output_tail(output, reason))
                    return False, 'app_stop_failed'

            ok, optional_failures = self._start_prepared_app_project(
                prepared, publish_running=not migration)
            if prepared.get('_superseded'):
                return False, 'app_project_superseded'
            if not ok and migration:
                self._run_compose_command(
                    self.COMPOSE_PATH, 'state', ['start', instance_uuid],
                    timeout=180)
            return ok, (
                '' if ok else 'app_project_failed', optional_failures)

    def _prepare_app_artifacts(self, app):
        artifacts = app.get('artifacts') or []
        if not artifacts:
            return True
        self._publish_health_status(
            app.get('instance_uuid') or '', 'starting', phase='artifact')
        with ThreadPoolExecutor(max_workers=min(2, len(artifacts))) as pool:
            futures = [
                pool.submit(self._prepare_one_app_artifact, app, artifact)
                for artifact in artifacts
            ]
            return all([future.result() for future in futures])

    def _prepare_one_app_artifact(self, app, artifact):
        ref = artifact.get('ref') or ''
        required = artifact.get('required', True) is not False
        name = artifact.get('name') or artifact.get('id') or 'artifact'
        digest = ref.rpartition('@')[2] or 'unpinned'
        prefix = f'{app.get("project_name") or "app"}: {name}@{digest}'
        if not ref:
            log('reconciler', f'{prefix}: missing artifact reference')
            return not required
        try:
            result = subprocess.run(
                ['reefy-artifacts', 'prepare', '--ref', ref,
                 '--kind', artifact.get('kind') or 'app'],
                capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as exception:
            log('reconciler',
                f'{prefix}: artifact prepare failed '
                f'({type(exception).__name__})')
            for output in (
                    getattr(exception, 'stdout', None),
                    getattr(exception, 'stderr', None)):
                if output:
                    for line in str(output).splitlines():
                        log('reconciler', f'{prefix}: {line}')
            return not required
        for stream_name, output in (
                ('stdout', result.stdout), ('stderr', result.stderr)):
            for line in (output or '').splitlines():
                log('reconciler', f'{prefix} [{stream_name}]: {line}')
        if result.returncode != 0:
            log('reconciler',
                f'{prefix}: artifact prepare failed '
                f'(exit {result.returncode})')
            return not required
        return True

    @staticmethod
    def _service_lifecycle(service):
        labels = service.get('labels') or {}
        return labels.get('ai.reefy.lifecycle') or 'service'

    @staticmethod
    def _service_is_optional(service):
        labels = service.get('labels') or {}
        return labels.get('ai.reefy.optional') == 'true'

    @staticmethod
    def _service_dependencies(service):
        depends_on = service.get('depends_on') or {}
        if isinstance(depends_on, dict):
            return list(depends_on)
        if isinstance(depends_on, list):
            return list(depends_on)
        return []

    def _service_closure(self, compose, roots, excluded=None):
        """Return roots and their declared dependencies in stable order."""
        services = compose.get('services') or {}
        excluded = set(excluded or ())
        pending = list(roots or ())
        result = set()
        while pending:
            service_name = pending.pop()
            if (service_name in result or service_name in excluded
                    or service_name not in services):
                continue
            result.add(service_name)
            pending.extend(self._service_dependencies(services[service_name]))
        return sorted(result)

    @staticmethod
    def _compose_without_services(compose, removed_services):
        """Copy Compose while removing services and references to them."""
        result = json.loads(json.dumps(compose))
        services = result.get('services') or {}
        removed_services = set(removed_services or ())
        for name in removed_services:
            services.pop(name, None)
        for service in services.values():
            depends_on = service.get('depends_on')
            if isinstance(depends_on, dict):
                service['depends_on'] = {
                    name: value for name, value in depends_on.items()
                    if name not in removed_services
                }
                if not service['depends_on']:
                    service.pop('depends_on')
            elif isinstance(depends_on, list):
                service['depends_on'] = [
                    name for name in depends_on
                    if name not in removed_services]
                if not service['depends_on']:
                    service.pop('depends_on')
        return result

    def _init_service_signature(self, service_name, service):
        return self._compose_sig({
            'service': service_name,
            'definition': service,
        })

    def _completed_init_services(self, project_name, compose):
        project_dir = os.path.dirname(
            self._project_compose_path(project_name))
        completed = set()
        for service_name, service in (
                compose.get('services') or {}).items():
            if self._service_lifecycle(service) != 'init':
                continue
            signature = self._init_service_signature(
                service_name, service)
            try:
                with open(os.path.join(
                        project_dir,
                        f'.init-{service_name}.sig')) as stream:
                    if stream.read().strip() == signature:
                        completed.add(service_name)
            except OSError:
                pass
        return completed

    def _ordered_pending_init_services(
            self, compose, completed, skipped_optional):
        services = compose.get('services') or {}
        pending = {
            name for name, service in services.items()
            if self._service_lifecycle(service) == 'init'
            and name not in completed
            and name not in skipped_optional
        }
        ordered = []
        visiting = set()
        visited = set()

        def visit(service_name):
            if service_name in visited or service_name not in pending:
                return
            if service_name in visiting:
                return
            visiting.add(service_name)
            for dependency in self._service_dependencies(
                    services[service_name]):
                visit(dependency)
            visiting.remove(service_name)
            visited.add(service_name)
            ordered.append(service_name)

        for service_name in services:
            visit(service_name)
        return ordered

    def _runtime_app_compose(self, compose):
        """Remove completed init definitions from the long-running project.

        Init definitions remain authoritative in desired state. The runtime
        Compose file excludes them after fingerprinted execution so a later
        Compose or Docker restart cannot accidentally rerun an init job.
        """
        services = compose.get('services') or {}
        init_names = {
            name for name, service in services.items()
            if self._service_lifecycle(service) == 'init'
        }
        return self._compose_without_services(compose, init_names)

    def _run_app_init_services(
            self, project_name, compose, instance_uuid,
            skipped_optional=None, required_init_services=None):
        """Run changed init services without allowing an implicit pull.

        Returns (required_ok, optional_failures, required_failure), where
        failure values retain the bounded Compose diagnostic output.
        """
        skipped_optional = set(skipped_optional or ())
        if required_init_services is None:
            required_init_services = {
                name for name, service in (
                    compose.get('services') or {}).items()
                if (self._service_lifecycle(service) == 'init'
                    and not self._service_is_optional(service))
            }
        else:
            required_init_services = set(required_init_services)
        optional_failures = {}
        with self._project_lock(project_name):
            services = compose.get('services') or {}
            completed = self._completed_init_services(
                project_name, compose)
            ordered = self._ordered_pending_init_services(
                compose, completed, skipped_optional)
            execution_compose = self._compose_without_services(
                compose, completed | skipped_optional)
            project_dir = os.path.dirname(
                self._project_compose_path(project_name))
            for service_name in ordered:
                service = services[service_name]
                compose_path = self._write_project_compose(
                    project_name, execution_compose)
                signature = self._init_service_signature(
                    service_name, service)
                signature_path = os.path.join(
                    project_dir, f'.init-{service_name}.sig')
                self._publish_health_status(
                    instance_uuid, 'starting',
                    message=f'running init service {service_name}')
                ok, output = self._run_compose_command(
                    compose_path, project_name,
                    ['run', '--pull', 'never', '--rm', service_name],
                    timeout=1800)
                if not ok:
                    log('reconciler',
                        f'{project_name}: init service {service_name} failed')
                    if (self._service_is_optional(service)
                            and service_name not in required_init_services):
                        optional_failures[service_name] = output
                        execution_compose = self._compose_without_services(
                            execution_compose, [service_name])
                        continue
                    return False, optional_failures, (
                        service_name, output)
                temporary = f'{signature_path}.tmp'
                with open(temporary, 'w') as stream:
                    stream.write(f'{signature}\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, signature_path)
                execution_compose = self._compose_without_services(
                    execution_compose, [service_name])
        return True, optional_failures, None

    def _app_project_plan(self, project_name, compose, primary_service):
        runtime = self._runtime_app_compose(compose)
        all_services = compose.get('services') or {}
        runtime_services = runtime.get('services') or {}
        completed_init = self._completed_init_services(
            project_name, compose)
        required_init = [
            name for name, service in all_services.items()
            if self._service_lifecycle(service) == 'init'
            and not self._service_is_optional(service)
            and name not in completed_init
        ]
        optional_init = [
            name for name, service in all_services.items()
            if self._service_lifecycle(service) == 'init'
            and self._service_is_optional(service)
            and name not in completed_init
        ]
        required_runtime = [
            name for name, service in runtime_services.items()
            if not self._service_is_optional(service)
        ]
        if primary_service not in required_runtime:
            return None

        required_services = set(self._service_closure(
            compose, required_init + required_runtime,
            excluded=completed_init))
        required_init_services = {
            name for name in required_services
            if self._service_lifecycle(all_services[name]) == 'init'
        }

        optional = []
        for kind, names, source in (
                ('init', optional_init, compose),
                ('runtime', [
                    name for name, service in runtime_services.items()
                    if self._service_is_optional(service)], runtime)):
            for service_name in names:
                if service_name in required_services:
                    continue
                optional.append({
                    'kind': kind,
                    'name': service_name,
                    'services': self._service_closure(
                        (compose if kind == 'runtime' else source),
                        [service_name], excluded=completed_init),
                    'init_dependencies': {
                        dependency for dependency in self._service_closure(
                            compose, [service_name],
                            excluded=completed_init)
                        if (dependency != service_name
                            and self._service_lifecycle(
                                all_services[dependency]) == 'init')
                    },
                })
        return {
            'runtime': runtime,
            'required_init': required_init,
            'required_runtime': required_runtime,
            'required_services': sorted(required_services),
            'required_init_services': required_init_services,
            'completed_init': completed_init,
            'optional': optional,
        }

    def _required_app_failure_context(
            self, project_name, compose, primary_service, plan=None):
        plan = plan or self._app_project_plan(
            project_name, compose, primary_service)
        if plan is None:
            return None
        legacy_required = [
            name for name, service in (
                plan['runtime'].get('services') or {}).items()
            if not self._service_is_optional(service)
        ]
        legacy_signature = self._compose_sig({
            'compose': plan['runtime'],
            'services': legacy_required,
        })
        return self._failure_marker_context(
            project_name, compose, plan['required_services'],
            compatible_signatures=[legacy_signature])

    def _current_required_app_failure(
            self, project_name, compose, primary_service):
        """Return the current unsuffixed required-project failure only."""
        path = os.path.join(
            self.PROJECTS_DIR, project_name, '.failed-compose-sig')
        record = self._read_failed_sig_record(path)
        if not record:
            return {}
        if record.get('version') == 2 and record.get('services'):
            current_signature = self._compose_sig({
                'compose': compose,
                'services': sorted(record['services']),
            })
            if current_signature == record.get('signature'):
                return record
        context = self._required_app_failure_context(
            project_name, compose, primary_service)
        if context and record.get('signature') in context['signatures']:
            return record
        return {}

    def _failure_marker_context(
            self, project_name, compose, services=None,
            failure_suffix='', compatible_signatures=None):
        service_names = sorted(set(
            services if services is not None
            else (compose.get('services') or {})))
        if services is None:
            signature = self._compose_sig(compose)
        else:
            signature = self._compose_sig({
                'compose': compose,
                'services': service_names,
            })
        failed_name = '.failed-compose-sig'
        if failure_suffix:
            safe_suffix = re.sub(
                r'[^A-Za-z0-9_.-]', '_', failure_suffix)
            failed_name += f'.{safe_suffix}'
        path = os.path.join(
            self.PROJECTS_DIR, project_name, failed_name)
        signatures = {signature}
        signatures.update(compatible_signatures or ())
        return {
            'path': path,
            'signature': signature,
            'signatures': signatures,
            'services': service_names,
        }

    def _matching_failed_record(self, context):
        record = self._read_failed_sig_record(context['path'])
        if record.get('signature') in context['signatures']:
            return record
        return {}

    @classmethod
    def _bounded_output_tail(cls, output, fallback=''):
        lines = (output or '').splitlines()
        safe_fallback = shared.redact_log_message(fallback) if fallback else ''
        if safe_fallback:
            tail = [safe_fallback, *lines[-4:]]
        else:
            tail = lines[-5:]
        return '\n'.join(
            shared.redact_log_message(line)[:cls.COMPOSE_OUTPUT_LINE_CHARS]
            for line in tail)

    def _prepare_app_project_compose(
            self, project_name, compose, instance_uuid, primary_service,
            force_retry=False, force_retry_signature=None):
        """Write and explicitly pull every app image before any startup."""
        with self._project_lock(project_name):
            plan = self._app_project_plan(
                project_name, compose, primary_service)
            if plan is None:
                self._publish_health_status(
                    instance_uuid, 'failed',
                    message='primary service is not required')
                return {'ok': False, 'code': 'app_project_failed'}

            compose_path = self._write_project_compose(project_name, compose)
            required_context = self._required_app_failure_context(
                project_name, compose, primary_service, plan=plan)
            failed_record = self._current_required_app_failure(
                project_name, compose, primary_service)
            if (force_retry and failed_record
                    and (force_retry_signature is None
                         or failed_record.get('signature')
                         == force_retry_signature)):
                self._clear_project_failed_sigs(project_name)
                failed_record = {}
            if failed_record:
                reason = failed_record.get('reason') or 'app startup failed'
                self._publish_health_status(
                    instance_uuid, 'failed', message=reason)
                return {
                    'ok': False,
                    'code': 'app_project_failed',
                    'failed_record': failed_record,
                }

            self._publish_health_status(
                instance_uuid, 'starting', phase='pull')
            pull_deadline = (
                time.monotonic() + self.COMPOSE_PULL_BUDGET_SECONDS)
            ok, output, reason = self._pull_project_images(
                compose_path, project_name, plan['required_services'],
                pull_deadline, max_retries=self.COMPOSE_PULL_MAX_ATTEMPTS)
            if not ok:
                if reason == self.APP_RECONCILE_SUPERSEDED:
                    return {
                        'ok': False,
                        'code': 'app_project_superseded',
                    }
                self._write_failed_sig_path(
                    required_context['path'], required_context['signature'],
                    reason, phase='pull',
                    services=required_context['services'])
                self._publish_health_status(
                    instance_uuid, 'failed',
                    message=self._bounded_output_tail(output, reason),
                    phase='pull')
                return {'ok': False, 'code': 'app_project_failed'}

            optional_failures = {}
            optional_ready = set()
            optional_contexts = {}
            for optional in plan['optional']:
                service_name = optional['name']
                compatible_signatures = []
                if optional['kind'] == 'runtime':
                    compatible_signatures.append(self._compose_sig({
                        'compose': plan['runtime'],
                        'services': [service_name],
                    }))
                context = self._failure_marker_context(
                    project_name, compose, optional['services'],
                    failure_suffix=f'optional-{service_name}',
                    compatible_signatures=compatible_signatures)
                optional_contexts[service_name] = context
                failed_record = self._matching_failed_record(context)
                if failed_record:
                    optional_failures[service_name] = (
                        failed_record.get('reason')
                        or 'optional service failed')
                    continue
                unavailable_init = []
                if optional['kind'] == 'runtime':
                    unavailable_init = sorted(
                        dependency
                        for dependency in optional['init_dependencies']
                        if (dependency
                            not in plan['required_init_services']
                            and dependency not in optional_ready))
                if unavailable_init:
                    reason = 'optional init dependency unavailable'
                    optional_failures[service_name] = reason
                    self._write_failed_sig_path(
                        context['path'], context['signature'], reason,
                        phase='pull', services=context['services'])
                    continue
                ok, output, reason = self._pull_project_images(
                    compose_path, project_name, optional['services'],
                    pull_deadline, max_retries=1)
                if not ok:
                    if reason == self.APP_RECONCILE_SUPERSEDED:
                        return {
                            'ok': False,
                            'code': 'app_project_superseded',
                        }
                    optional_failures[service_name] = output or reason
                    self._write_failed_sig_path(
                        context['path'], context['signature'], reason,
                        phase='pull', services=context['services'])
                    continue
                optional_ready.add(service_name)

            return {
                'ok': True,
                'project_name': project_name,
                'compose': compose,
                'compose_path': compose_path,
                'instance_uuid': instance_uuid,
                'primary_service': primary_service,
                'plan': plan,
                'required_context': required_context,
                'optional_contexts': optional_contexts,
                'optional_ready': optional_ready,
                'optional_failures': optional_failures,
            }

    def _start_prepared_app_project(self, prepared, publish_running=True):
        project_name = prepared['project_name']
        instance_uuid = prepared['instance_uuid']
        compose = prepared['compose']
        plan = prepared['plan']
        required_context = prepared['required_context']
        optional_failures = dict(prepared['optional_failures'])
        optional_contexts = prepared['optional_contexts']
        optional_ready = prepared['optional_ready']
        with self._project_lock(project_name):
            if self._is_current_app_superseded():
                prepared['_superseded'] = True
                return False, []
            self._publish_health_status(
                instance_uuid, 'starting', phase='init')
            skipped_optional = {
                optional['name'] for optional in plan['optional']
                if optional['kind'] == 'init'
                and optional['name'] not in optional_ready
            }
            init_ok, init_failures, required_init_failure = (
                self._run_app_init_services(
                    project_name, compose, instance_uuid,
                    skipped_optional=skipped_optional,
                    required_init_services=plan[
                        'required_init_services']))
            optional_failures.update(init_failures)
            for service_name, output in init_failures.items():
                context = optional_contexts[service_name]
                self._write_failed_sig_path(
                    context['path'], context['signature'],
                    'docker compose init failed', phase='init',
                    services=context['services'])
            for optional in plan['optional']:
                service_name = optional['name']
                if (optional['kind'] == 'init'
                        and service_name in optional_ready
                        and service_name not in init_failures):
                    self._clear_failed_sig_path(
                        optional_contexts[service_name]['path'])
            if not init_ok:
                _service_name, output = required_init_failure
                reason = 'docker compose init failed'
                self._write_failed_sig_path(
                    required_context['path'],
                    required_context['signature'], reason, phase='init',
                    services=required_context['services'])
                self._publish_health_status(
                    instance_uuid, 'failed',
                    message=self._bounded_output_tail(output, reason),
                    phase='init')
                return False, sorted(optional_failures)

            unavailable_init = skipped_optional | set(init_failures)
            for optional in plan['optional']:
                if optional['kind'] != 'runtime':
                    continue
                service_name = optional['name']
                failed_dependencies = sorted(
                    optional['init_dependencies'] & unavailable_init)
                if not failed_dependencies:
                    continue
                reason = 'optional init dependency failed'
                optional_failures[service_name] = reason
                optional_ready.discard(service_name)
                context = optional_contexts[service_name]
                self._write_failed_sig_path(
                    context['path'], context['signature'], reason,
                    phase='init', services=context['services'])

            runtime = plan['runtime']
            runtime_path = self._write_project_compose(
                project_name, runtime)
            if self._is_current_app_superseded():
                prepared['_superseded'] = True
                return False, sorted(optional_failures)
            self._publish_health_status(
                instance_uuid, 'starting', phase='start')
            ok, output, reason = self._start_project_services(
                runtime_path, project_name, plan['required_runtime'],
                remove_orphans=True,
                max_retries=self.COMPOSE_PULL_MAX_ATTEMPTS)
            if not ok:
                if reason == self.APP_RECONCILE_SUPERSEDED:
                    prepared['_superseded'] = True
                    return False, sorted(optional_failures)
                self._write_failed_sig_path(
                    required_context['path'],
                    required_context['signature'], reason, phase='start',
                    services=required_context['services'])
                self._publish_health_status(
                    instance_uuid, 'failed',
                    message=self._bounded_output_tail(output, reason),
                    phase='start')
                return False, sorted(optional_failures)
            self._clear_failed_sig_path(required_context['path'])

            for optional in plan['optional']:
                if optional['kind'] != 'runtime':
                    continue
                service_name = optional['name']
                if service_name not in optional_ready:
                    continue
                ok, output, reason = self._start_project_services(
                    runtime_path, project_name, [service_name],
                    remove_orphans=False, max_retries=1)
                context = optional_contexts[service_name]
                if ok:
                    for dependency_name in optional['services']:
                        dependency_context = optional_contexts.get(
                            dependency_name)
                        if dependency_context is not None:
                            self._clear_failed_sig_path(
                                dependency_context['path'])
                            optional_failures.pop(dependency_name, None)
                else:
                    optional_failures[service_name] = output or reason
                    self._write_failed_sig_path(
                        context['path'], context['signature'], reason,
                        phase='start', services=context['services'])

            message = None
            if optional_failures:
                message = 'optional services failed: ' + ', '.join(
                    sorted(optional_failures))
            image = ((runtime.get('services') or {}).get(
                prepared['primary_service']) or {}).get('image')
            prepared['_running_health'] = {
                'message': message,
                'image': image,
            }
            if publish_running:
                self._publish_health_status(
                    instance_uuid, 'running', message=message, image=image)
            return True, sorted(optional_failures)

    def _apply_app_project_compose(
            self, project_name, compose, instance_uuid, primary_service,
            force_retry=False, prepared=None):
        with self._project_lock(project_name):
            prepared = prepared or self._prepare_app_project_compose(
                project_name, compose, instance_uuid, primary_service,
                force_retry=force_retry)
            if not prepared.get('ok'):
                return False, []
            return self._start_prepared_app_project(prepared)

    def _prepare_system_project_compose(
            self, project_name, compose, migration=False):
        """Pull a system project without starting or stopping containers."""
        with self._project_lock(project_name):
            compose_path = self._write_project_compose(project_name, compose)
            context = self._failure_marker_context(
                project_name, compose, services=None)
            failed_record = self._matching_failed_record(context)
            if (failed_record
                    and not (migration and failed_record.get('phase')
                             in ('legacy', 'start'))):
                return {
                    'ok': False,
                    'failed_record': failed_record,
                }
            deadline = time.monotonic() + self.COMPOSE_PULL_BUDGET_SECONDS
            ok, output, reason = self._pull_project_images(
                compose_path, project_name, None, deadline,
                max_retries=self.COMPOSE_PULL_MAX_ATTEMPTS)
            if not ok:
                self._write_failed_sig_path(
                    context['path'], context['signature'], reason,
                    phase='pull', services=context['services'])
                return {
                    'ok': False,
                    'output': output,
                    'reason': reason,
                }
            return {
                'ok': True,
                'project_name': project_name,
                'compose': compose,
                'compose_path': compose_path,
                'services': None,
                'context': context,
            }

    def _start_prepared_system_project(
            self, prepared, before_start=None):
        project_name = prepared['project_name']
        context = prepared['context']
        with self._project_lock(project_name):
            if before_start is not None and not before_start():
                reason = 'docker compose start handoff failed'
                self._write_failed_sig_path(
                    context['path'], context['signature'], reason,
                    phase='start', services=context['services'])
                return False
            ok, output, reason = self._start_project_services(
                prepared['compose_path'], project_name,
                prepared['services'], remove_orphans=True,
                max_retries=self.COMPOSE_PULL_MAX_ATTEMPTS)
            if ok:
                self._clear_failed_sig_path(context['path'])
                return True
            self._write_failed_sig_path(
                context['path'], context['signature'], reason,
                phase='start', services=context['services'])
            log('reconciler',
                f'{project_name}: '
                f'{self._bounded_output_tail(output, reason)}')
            return False

    def _apply_project_compose(
            self, project_name, compose, instance_uuid=None,
            primary_service='app', services=None, remove_orphans=True,
            failure_suffix='', max_retries=5):
        lock = self._project_lock(project_name)
        with lock:
            compose_path = self._write_project_compose(project_name, compose)
            selected_services = (
                None if services is None else sorted(set(services)))
            context = self._failure_marker_context(
                project_name, compose,
                services if services is not None else None,
                failure_suffix=failure_suffix)
            failed_record = self._matching_failed_record(context)
            if failed_record:
                if instance_uuid:
                    self._publish_health_status(
                        instance_uuid, 'failed',
                        message=failed_record.get('reason') or '')
                return False
            if instance_uuid:
                self._publish_health_status(instance_uuid, 'starting')
            pull_deadline = (
                time.monotonic() + self.COMPOSE_PULL_BUDGET_SECONDS)
            ok, output, reason = self._pull_project_images(
                compose_path, project_name, selected_services,
                pull_deadline, max_retries=max_retries)
            if not ok:
                self._write_failed_sig_path(
                    context['path'], context['signature'], reason,
                    phase='pull', services=context['services'])
                if instance_uuid:
                    self._publish_health_status(
                        instance_uuid, 'failed',
                        message=self._bounded_output_tail(output, reason))
                return False

            ok, output, reason = self._start_project_services(
                compose_path, project_name, selected_services,
                remove_orphans=remove_orphans, max_retries=max_retries)
            if ok:
                self._clear_failed_sig_path(context['path'])
                if instance_uuid:
                    image = ((compose.get('services') or {}).get(
                        primary_service) or {}).get('image')
                    self._publish_health_status(
                        instance_uuid, 'running', image=image)
                return True

            self._write_failed_sig_path(
                context['path'], context['signature'], reason,
                phase='start', services=context['services'])
            if instance_uuid:
                self._publish_health_status(
                    instance_uuid, 'failed',
                    message=self._bounded_output_tail(output, reason))
            return False

    def _pull_project_images(
            self, compose_path, project_name, services, deadline,
            max_retries=5):
        pull_all = services is None
        services = sorted(set(services or ()))
        if not pull_all and not services:
            return True, '', ''
        last_output = ''
        reason = 'docker compose pull failed'
        for attempt in range(1, max_retries + 1):
            if self._is_current_app_superseded():
                return False, '', self.APP_RECONCILE_SUPERSEDED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_output = self._bounded_output_tail(
                    last_output, 'docker compose pull timed out')
                return False, last_output, 'docker compose pull timed out'
            service_summary = ','.join(services) if services else 'all'
            log('reconciler',
                f'{project_name}: docker compose pull '
                f'(attempt {attempt}/{max_retries}, '
                f'services={service_summary})')
            ok, output = self._run_compose_command_streaming(
                compose_path, project_name,
                ['pull', '--policy', 'missing', *services],
                timeout=remaining)
            if ok:
                return True, output, ''
            if self.APP_RECONCILE_SUPERSEDED in output:
                return False, output, self.APP_RECONCILE_SUPERSEDED
            last_output = output
            classification = self._classify_compose_failure(output)
            reason = self._failure_reason(
                classification, output, phase='pull')
            if classification in (
                    'image_missing', 'no_space', 'storage_corruption'):
                break
            if attempt >= max_retries:
                break
            delay = min(10 * (2 ** (attempt - 1)), 60)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = 'docker compose pull timed out'
                break
            if self._wait_for_app_supersession(min(delay, remaining)):
                return False, last_output, self.APP_RECONCILE_SUPERSEDED
        return False, last_output, reason

    def _start_project_services(
            self, compose_path, project_name, services,
            remove_orphans=True, max_retries=5):
        services = sorted(set(services or ()))
        last_output = ''
        reason = 'docker compose up failed'
        deadline = time.monotonic() + self.COMPOSE_START_TIMEOUT_SECONDS
        for attempt in range(1, max_retries + 1):
            if self._is_current_app_superseded():
                return False, last_output, self.APP_RECONCILE_SUPERSEDED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, last_output, 'docker compose up timed out'
            args = ['up', '-d', '--pull', 'never']
            if remove_orphans:
                args.append('--remove-orphans')
            args.extend(services)
            ok, output = self._run_compose_command(
                compose_path, project_name, args,
                timeout=remaining)
            if ok:
                return True, output, ''
            last_output = output
            classification = self._classify_compose_failure(output)
            reason = self._failure_reason(classification, output)
            if classification in (
                    'image_missing', 'no_space', 'storage_corruption'):
                break
            if attempt < max_retries:
                delay = min(10 * (2 ** (attempt - 1)), 60)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'docker compose up timed out'
                    break
                if self._wait_for_app_supersession(min(delay, remaining)):
                    return False, last_output, self.APP_RECONCILE_SUPERSEDED
        return False, last_output, reason

    def _write_project_compose(self, project_name, compose):
        with self._project_lock(project_name):
            compose_path = self._project_compose_path(project_name)
            os.makedirs(os.path.dirname(compose_path), exist_ok=True)
            tmp_path = compose_path + '.tmp'
            with open(tmp_path, 'w') as stream:
                json.dump(compose, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, compose_path)
            return compose_path

    def _run_compose_command_streaming(
            self, compose_path, project_name, args, timeout):
        """Run a long Compose command with bounded, redacted diagnostics."""
        command = [
            'docker', 'compose', '-f', compose_path, '-p', project_name,
            *args,
        ]
        tail = deque(maxlen=self.COMPOSE_OUTPUT_TAIL_LINES)
        classification_signals = {}
        proc = None
        selector = None
        partial = ''
        discarding_line = False
        classification_scan_tail = ''
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

        canonical_signals = {
            'no_space': 'no space left on device',
            'image_missing': 'manifest unknown',
            'storage_corruption': 'failed to register layer',
        }

        def scan_failure_signals(decoded):
            nonlocal classification_scan_tail
            scan = classification_scan_tail + decoded
            classification = self._classify_compose_failure(scan)
            if classification != 'transient':
                classification_signals.setdefault(
                    classification, canonical_signals[classification])
            classification_scan_tail = scan[-128:]

        def line_boundary(value):
            newline = value.find('\n')
            carriage_return = value.find('\r')
            candidates = [
                position for position in (newline, carriage_return)
                if position >= 0
            ]
            return min(candidates) if candidates else -1

        def remember(line):
            safe = shared.redact_log_message(line.rstrip('\r\n'))
            if len(safe) > self.COMPOSE_OUTPUT_LINE_CHARS:
                safe = safe[:self.COMPOSE_OUTPUT_LINE_CHARS] + ' [truncated]'
            if safe:
                classification = self._classify_compose_failure(safe)
                if classification != 'transient':
                    classification_signals.setdefault(classification, safe)
                log('compose', safe)
                tail.append(safe)

        def rendered_output():
            tail_lines = list(tail)
            signals = [
                line for line in classification_signals.values()
                if line not in tail_lines
            ]
            return '\n'.join(signals + tail_lines)

        def consume(chunk, final=False):
            nonlocal discarding_line, partial
            decoded = decoder.decode(chunk, final=final)
            scan_failure_signals(decoded)
            truncation_marker = ' [truncated]'
            retained_chars = max(
                1, self.COMPOSE_OUTPUT_LINE_CHARS
                - len(truncation_marker))
            while decoded:
                if discarding_line:
                    newline = line_boundary(decoded)
                    if newline < 0:
                        decoded = ''
                        break
                    remember(partial + truncation_marker)
                    partial = ''
                    discarding_line = False
                    decoded = decoded[newline + 1:]
                    continue

                newline = line_boundary(decoded)
                segment = decoded if newline < 0 else decoded[:newline]
                available = retained_chars - len(partial)
                if len(segment) > available:
                    partial += segment[:available]
                    if newline < 0:
                        discarding_line = True
                        decoded = ''
                        break
                    remember(partial + truncation_marker)
                    partial = ''
                    decoded = decoded[newline + 1:]
                    continue

                partial += segment
                if newline < 0:
                    decoded = ''
                    break
                remember(partial)
                partial = ''
                decoded = decoded[newline + 1:]

            if final:
                if discarding_line:
                    remember(partial + truncation_marker)
                elif partial:
                    remember(partial)
                partial = ''
                discarding_line = False

        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, start_new_session=True)
            output_fd = proc.stdout.fileno()
            os.set_blocking(output_fd, False)
            selector = selectors.DefaultSelector()
            selector.register(output_fd, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            timed_out = False
            superseded = False
            reached_eof = False
            while not reached_eof:
                if self._is_current_app_superseded():
                    superseded = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(timeout=min(0.25, remaining))
                if not events:
                    continue
                try:
                    chunk = os.read(output_fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    reached_eof = True
                    break
                consume(chunk)

            if timed_out or superseded:
                self._terminate_compose_process(proc)
                while True:
                    try:
                        chunk = os.read(output_fd, 4096)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    consume(chunk)
                consume(b'', final=True)
                if superseded:
                    tail.append(self.APP_RECONCILE_SUPERSEDED)
                    return False, rendered_output()
                tail.append(
                    f'docker compose {args[0]} timed out after '
                    f'{max(1, int(timeout))} seconds')
                return False, rendered_output()

            consume(b'', final=True)
            remaining = max(0.1, deadline - time.monotonic())
            try:
                returncode = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._terminate_compose_process(proc)
                tail.append(
                    f'docker compose {args[0]} timed out after '
                    f'{max(1, int(timeout))} seconds')
                return False, rendered_output()
            return returncode == 0, rendered_output()
        except OSError as exception:
            if proc is not None:
                self._terminate_compose_process(proc)
            return False, (
                f'docker compose error: {type(exception).__name__}')
        finally:
            if selector is not None:
                selector.close()
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()

    @staticmethod
    def _terminate_compose_process(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _run_compose_command(compose_path, project_name, args, timeout):
        try:
            result = subprocess.run(
                ['docker', 'compose', '-f', compose_path, '-p', project_name,
                 *args],
                capture_output=True, text=True, timeout=timeout)
            output = '\n'.join(filter(None, [result.stdout, result.stderr]))
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, 'docker compose command timed out'
        except OSError as exception:
            return False, f'docker compose error: {type(exception).__name__}'

    @staticmethod
    def _read_json(path):
        try:
            with open(path) as stream:
                return json.load(stream)
        except (OSError, json.JSONDecodeError):
            return {}

    def _v2_migration_pending(self):
        marker = os.path.join(
            os.path.dirname(self.DESIRED_STATE_V2_PATH), 'apps-v2-migrated')
        return (os.path.exists(self.DESIRED_STATE_PATH)
                and not os.path.exists(marker))

    def _commit_v2_migration(self):
        if os.path.exists(self.COMPOSE_PATH):
            ok, output = self._run_compose_command(
                self.COMPOSE_PATH, 'state', ['down', '--remove-orphans'],
                timeout=300)
            if not ok:
                reason = self._failure_reason(
                    self._classify_compose_failure(output), output)
                log('reconciler',
                    f'Apps-v2 migration commit failed: {reason}')
                return False
        state_dir = os.path.dirname(self.DESIRED_STATE_V2_PATH)
        marker = os.path.join(state_dir, 'apps-v2-migrated')
        marker_tmp = marker + '.tmp'
        with open(marker_tmp, 'w') as stream:
            stream.write('2\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, marker)
        for legacy_path in (self.DESIRED_STATE_PATH, self.COMPOSE_PATH):
            try:
                os.remove(legacy_path)
            except FileNotFoundError:
                pass
        log('reconciler', 'Apps-v2 migration committed')
        return True

    def _remove_absent_v2_projects(self, desired_projects):
        if not os.path.isdir(self.PROJECTS_DIR):
            return
        for entry in os.scandir(self.PROJECTS_DIR):
            if (not entry.is_dir() or entry.name == 'reefy-system'
                    or entry.name in desired_projects):
                continue
            with self._project_lock(entry.name):
                compose_path = os.path.join(entry.path, 'compose.json')
                if os.path.exists(compose_path):
                    self._run_compose_command(
                        compose_path, entry.name,
                        ['down', '--remove-orphans'], timeout=300)
                shutil.rmtree(entry.path, ignore_errors=True)

    @staticmethod
    def _read_failed_sig_record(path):
        try:
            with open(path) as stream:
                content = stream.read()
        except OSError:
            return {}
        try:
            record = json.loads(content)
        except (TypeError, ValueError):
            signature, _, reason = content.partition('\n')
            signature = signature.strip()
            if not signature:
                return {}
            return {
                'version': 1,
                'signature': signature,
                'services': [],
                'phase': 'legacy',
                'reason': reason.strip(),
            }
        if not isinstance(record, dict) or not record.get('signature'):
            return {}
        services = record.get('services')
        if not isinstance(services, list):
            services = []
        return {
            'version': record.get('version') or 2,
            'signature': str(record['signature']),
            'services': sorted(
                str(service) for service in services),
            'phase': record.get('phase') or 'legacy',
            'reason': str(record.get('reason') or ''),
        }

    @staticmethod
    def _read_failed_sig_path(path):
        record = DataPlane._read_failed_sig_record(path)
        return record.get('signature'), record.get('reason', '')

    @staticmethod
    def _write_failed_sig_path(
            path, signature, reason, phase='start', services=None):
        temporary = path + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            record = {
                'version': 2,
                'signature': signature,
                'services': sorted(set(services or ())),
                'phase': phase,
                'reason': shared.redact_log_message(reason)[:500],
            }
            with open(temporary, 'w') as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exception:
            log('reconciler',
                f'cannot persist project failure ({type(exception).__name__})')
            try:
                os.remove(temporary)
            except OSError:
                pass

    @staticmethod
    def _clear_failed_sig_path(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _apply_files(self, files):
        """Apply the generic `files` directive from desired-state.

        Each entry: { host_path, content_b64, uid, mode, if_absent }.
        Writes content_b64 (base64-decoded) atomically to host_path
        with the given ownership + permissions, but only if the
        target is under _FILES_ALLOWED_ROOTS. The if_absent flag
        prevents overwriting an existing file - the credential use
        case relies on this so an agent self-refreshing inside the
        container can't be clobbered by a subsequent state push.

        Errors on individual entries are logged but never abort the
        whole apply - a single bad entry (e.g. malformed base64)
        shouldn't take the rest of the device down.
        """
        if not files:
            return
        for spec in files:
            host_path = os.path.realpath(spec.get('host_path', ''))
            if not host_path or not any(
                    host_path.startswith(root)
                    for root in self._FILES_ALLOWED_ROOTS):
                log('mqtt',
                    f'files: rejected path outside allow-list: '
                    f'{spec.get("host_path", "")!r}')
                continue
            if spec.get('if_absent') and os.path.exists(host_path):
                continue
            try:
                content = base64.b64decode(
                    spec.get('content_b64', ''), validate=True)
            except (ValueError, TypeError) as e:
                log('mqtt',
                    f'files: bad content_b64 for {host_path}: {e}')
                continue
            try:
                os.makedirs(os.path.dirname(host_path), exist_ok=True)
                tmp = host_path + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(content)
                mode = int(spec.get('mode', '0600'), 8)
                os.chmod(tmp, mode)
                uid = int(spec.get('uid', 0))
                # gid == uid: matches the convention used by
                # _prepare_app_dirs and the per-app entrypoints
                # that chown the data volume to a single (uid,uid).
                os.chown(tmp, uid, uid)
                os.rename(tmp, host_path)
                log('mqtt',
                    f'files: wrote {host_path} '
                    f'({len(content)} bytes, mode={spec.get("mode", "0600")})')
            except OSError as e:
                log('mqtt', f'files: failed to write {host_path}: {e}')

    def _apply_wifi(self, wifi, old_wifi=None):
        """Configure or disconnect WiFi based on desired state diff."""
        ssid = wifi.get('ssid', '') if wifi else ''
        old_ssid = old_wifi.get('ssid', '') if old_wifi else ''

        if ssid:
            # Connect or change network
            if ssid != old_ssid:
                password = wifi.get('password', '')
                log('mqtt', f'Configuring WiFi: {ssid}')
                try:
                    result = subprocess.run(
                        ['wifi-setup', ssid, password],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, timeout=30
                    )
                    if result.returncode == 0:
                        print("[mqtt] WiFi configured successfully")
                    else:
                        log('mqtt',
                            f'WiFi setup failed (exit {result.returncode})')
                except subprocess.TimeoutExpired:
                    log('mqtt', 'WiFi setup timed out')
                except Exception as e:
                    log('mqtt', f'WiFi setup error ({type(e).__name__})')
        elif old_ssid:
            # WiFi was configured but now removed — disconnect
            log('mqtt', f'Disconnecting WiFi (was: {old_ssid})')
            try:
                iface = shared.find_wireless_iface()
                if iface:
                    subprocess.run(
                        ['systemctl', 'stop', f'wpa_supplicant@{iface}'],
                        capture_output=True, timeout=10)
                    subprocess.run(
                        ['systemctl', 'disable', f'wpa_supplicant@{iface}'],
                        capture_output=True, timeout=10)
                    conf = f'/etc/wpa_supplicant/wpa_supplicant-{iface}.conf'
                    if os.path.exists(conf):
                        os.remove(conf)
                    print(f"[mqtt] WiFi disconnected ({iface})")
            except Exception as e:
                log('mqtt', f'WiFi disconnect error: {e}')

    def _apply_network(self, network, old_network=None):
        """Configure secondary static IP addresses on network interfaces.

        Idempotent: adds addresses from new state, removes addresses that were
        in old_state but not in new state. Never touches addresses not managed
        by desired state.

        On boot (old_network=None): only adds, never removes.
        On state update: diffs old vs new to determine removals.
        """
        desired = set()
        if network and network.get('addresses'):
            for entry in network['addresses']:
                addr = entry.get('addr', '')
                dev = entry.get('dev', '')
                label = entry.get('label', '')
                if addr and dev:
                    desired.add((addr, dev, label))

        old_desired = set()
        if old_network and old_network.get('addresses'):
            for entry in old_network['addresses']:
                addr = entry.get('addr', '')
                dev = entry.get('dev', '')
                label = entry.get('label', '')
                if addr and dev:
                    old_desired.add((addr, dev, label))

        to_add = desired - old_desired
        to_remove = old_desired - desired

        if not to_add and not to_remove:
            return

        if to_add:
            log('reconciler', f'{f'Adding {len(to_add)} static IP(s)'}')
        if to_remove:
            log('reconciler', f'{f'Removing {len(to_remove)} static IP(s)'}')

        # Add new addresses
        for addr, dev, label in to_add:
            cmd = ['ip', 'addr', 'add', addr, 'dev', dev]
            if label:
                cmd += ['label', label]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    log('mqtt', f'Added {addr} on {dev} label={label}')
                elif 'RTNETLINK answers: File exists' in r.stderr:
                    log('mqtt', f'Address {addr} already exists on {dev}')
                else:
                    log('mqtt', f'Failed to add {addr}: {r.stderr.strip()}')
            except Exception as e:
                log('reconciler', f'{f'Error adding {addr}: {e}'}')

        # Remove addresses that were in old state but not in new
        for addr, dev, label in to_remove:
            try:
                r = subprocess.run(
                    ['ip', 'addr', 'del', addr, 'dev', dev],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    log('mqtt', f'Removed {addr} from {dev}')
                else:
                    log('reconciler', f'{f'Failed to remove {addr}: {r.stderr.strip()}'}')
            except Exception as e:
                log('reconciler', f'{f'Error removing {addr}: {e}'}')

        log('reconciler', f'{'Network config applied'}')

        # Re-publish status with updated ip_addr so dashboard reflects changes
        self._publish_status('online', 'Network config updated')

    def _boot_apply(self):
        """Apply persisted desired state once on startup (thread target).
        The data plane owns boot reconcile (control just calls home); same
        request scheduler as on-reconnect reconcile, so a state forwarded
        while the data plane is still booting becomes the latest pending
        request instead of running concurrently."""
        self._dp_reconcile()

    def _docker_event_loop(self):
        """Repair desired service containers immediately after a die event."""
        while True:
            try:
                process = subprocess.Popen(
                    ['docker', 'events', '--filter', 'type=container',
                     '--format', '{{json .}}'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    self._handle_docker_event(event)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(1)

    def _handle_docker_event(self, event):
        action = event.get('Action') or event.get('status')
        if action != 'die':
            return False
        attributes = ((event.get('Actor') or {}).get('Attributes') or {})
        if attributes.get('ai.reefy.lifecycle') != 'service':
            return False
        # Docker owns nonzero crash recovery through unlimited on-failure.
        # Reefy only fills the exit-code-0 gap that policy intentionally
        # leaves alone.
        if str(attributes.get('exitCode', '0')) != '0':
            return False
        project = attributes.get('com.docker.compose.project')
        instance_uuid = attributes.get('ai.reefy.instance_uuid')
        if not project or not instance_uuid:
            return False
        state = self._read_json(self._active_state_path())
        if not self._is_v2_state(state):
            return False
        if self._v2_migration_pending():
            return False
        app = next((candidate for candidate in state.get('apps') or []
                    if (candidate.get('project_name') == project
                        and candidate.get('instance_uuid') == instance_uuid
                        and self._app_desired_status(candidate) == 'running')),
                   None)
        if app is None:
            return False
        log('reconciler',
            f'{project}: service die event detected; scheduling repair')
        threading.Thread(
            target=self._reconcile_v2_app,
            args=(app, False, False), daemon=True).start()
        return True

    def run_data_plane(self):
        """Data-plane entrypoint (reefy-reconciler): serve the Varlink
        interface and apply persisted desired state on startup. No MQTT -
        the control process owns that. Storage/container work runs here,
        isolated so a crash/OOM/hang can't take down control. Each Varlink
        call is handled in its own thread (ThreadingServer)."""
        import varlink
        os.makedirs('/run/reefy', exist_ok=True)

        service = varlink.Service(
            vendor='Reefy', product='reconciler', version='1',
            url='io.reefy.Reconciler',
            interface_dir=self.VARLINK_INTERFACE_DIR)
        recon = self

        @service.interface('io.reefy.Reconciler')
        class _Reconciler:
            def SubmitApply(self, state, _more=False):
                return recon._dp_submit_apply(state)

            def SubmitReconcile(self, _more=False):
                return recon._dp_submit_reconcile()

            def GetApply(self, request_id, _more=False):
                return recon._get_apply_result(request_id)

            def WaitApply(self, request_id, _more=False):
                return recon._wait_apply_result(request_id)

            def ApplyState(self, state, _more=False):
                return recon._dp_apply_state(state)

            def Reconcile(self, _more=False):
                return recon._dp_reconcile()

            def BackupNow(self, instance_uuid, _more=False):
                return recon._dp_backup_now(instance_uuid)

            def RestartInstance(
                    self, instance_uuid, operation_id, _more=False):
                return recon._dp_restart_instance(
                    instance_uuid, operation_id=operation_id)

        # Bind the service as a class attr after definition: a class body
        # can't see run_data_plane's local `service` (class bodies don't
        # close over enclosing-function locals), so `service = service`
        # inside the body raises NameError.
        class _Handler(varlink.RequestHandler):
            pass
        _Handler.service = service

        # Clean up a stale socket from a previous run.
        sock_path = self.VARLINK_ADDRESS.split(':', 1)[1]
        try:
            if os.path.exists(sock_path):
                os.unlink(sock_path)
        except OSError:
            pass

        # Apply saved state on startup in the BACKGROUND so the Varlink
        # socket binds immediately below. Control starts ~4s earlier (to
        # call home) and forwards state on connect; previously the socket
        # only opened after this boot apply finished its docker compose up,
        # so control's forward raced a missing socket ("data plane
        # unreachable"). The boot request uses the same scheduler, so a
        # forwarded apply enters the pending slot instead of running
        # concurrently.
        threading.Thread(target=self._boot_apply, daemon=True).start()
        threading.Thread(target=self._docker_event_loop, daemon=True).start()

        log('mqtt', f'[data-plane] serving Varlink at {self.VARLINK_ADDRESS}')
        with varlink.ThreadingServer(self.VARLINK_ADDRESS, _Handler) as server:
            server.serve_forever()

    def _dp_apply_state(self, state):
        """Backward-compatible synchronous wrapper for older controls."""
        submission = self._dp_submit_apply(state)
        if not submission['ok']:
            return {'ok': False, 'error': submission['error']}
        result = self._wait_apply_result(submission['request_id'])
        ok = result['status'] in (
            'succeeded', 'succeeded_with_warnings', 'superseded')
        return {'ok': ok, 'error': '' if ok else result['error']}

    def _dp_submit_apply(self, state):
        try:
            decoded = json.loads(state)
            if not isinstance(decoded, dict) or not decoded:
                raise ValueError('desired state must be a non-empty object')
            return self._submit_apply_job('apply', state=decoded)
        except Exception as e:
            error = shared.redact_log_message(e)[:500]
            log('mqtt', f'[data-plane] SubmitApply failed: {error}')
            return {'ok': False, 'request_id': '', 'error': error}

    def _dp_submit_reconcile(self):
        return self._submit_apply_job('reconcile')

    def _dp_reconcile(self):
        """Backward-compatible synchronous wrapper for older controls."""
        submission = self._dp_submit_reconcile()
        if not submission['ok']:
            return {
                'ok': False,
                'applied': False,
                'error': submission['error'],
            }
        result = self._wait_apply_result(submission['request_id'])
        ok = result['status'] in (
            'succeeded', 'succeeded_with_warnings', 'superseded')
        return {
            'ok': ok,
            'applied': result['applied'],
            'error': '' if ok else result['error'],
        }

    def _dp_backup_now(self, instance_uuid):
        try:
            self._backup_now({'instance_uuid': instance_uuid}, cmd_id=None)
            return {'ok': True, 'message': 'backup started', 'error': ''}
        except Exception as e:
            error = shared.redact_log_message(e)[:500]
            log('mqtt', f'[data-plane] BackupNow failed: {error}')
            return {'ok': False, 'message': '', 'error': error}

    def _dp_restart_instance(self, instance_uuid, operation_id=''):
        try:
            self._restart_instance(
                {'instance_uuid': instance_uuid},
                cmd_id=operation_id or None)
            return {'ok': True, 'error': ''}
        except Exception as e:
            error = shared.redact_log_message(e)[:500]
            log('mqtt', f'[data-plane] RestartInstance failed: {error}')
            return {'ok': False, 'error': error}

    def _apply_user_ssh_keys(self, keys):
        """Rewrite /etc/ssh/authorized_keys.d/reefy atomically.

        Sources, concatenated in this order:
         1. The dev/e2e ESP-injected key (if /mnt/reefy/reefy/dev/
            authorized_keys exists). Boot-init copies it on every
            boot; we preserve it so a state apply doesn't kick the
            e2e harness out.
         2. Cloud-registered keys (`keys`) - what the user pasted in
            their account settings on the dashboard.

        The cloud is the source of truth for (2), so we always
        overwrite (not append) - additions and removals from the
        dashboard propagate within one reconcile tick. Empty `keys`
        and no dev-injected key = delete the file (back to
        password-only auth).

        File path lives under /etc/ssh/ as root:root mode 0644 because
        sshd reads authorized_keys AS THE TARGET USER after dropping
        privileges. The per-app SSH path (`ssh app-<name>@host`) drops
        to the `app-<name>` system user, which can't read /home/reefy.
        Stashing the file in a globally-readable system path (writable
        only by root) keeps the file the single source of truth shared
        between the reefy user and all app-* users - configured via
        AuthorizedKeysFile in sshd_config.d/reefy-apps.conf.

        Atomic rename to avoid leaving a half-written authorized_keys
        if the host loses power mid-write."""
        ssh_dir = '/etc/ssh/authorized_keys.d'
        auth_file = os.path.join(ssh_dir, 'reefy')

        cloud_keys = [k.strip() for k in (keys or []) if k and k.strip()]
        dev_keys = []
        try:
            if os.path.exists(self._DEV_INJECTED_KEY_PATH):
                with open(self._DEV_INJECTED_KEY_PATH) as f:
                    dev_keys = [line.strip() for line in f
                                if line.strip() and not line.startswith('#')]
        except OSError as e:
            log('mqtt', f'dev-injected key read failed: {e}')

        all_keys = dev_keys + cloud_keys

        if not all_keys:
            try:
                if os.path.exists(auth_file):
                    os.remove(auth_file)
                    log('mqtt', 'Removed authorized_keys (no SSH keys)')
            except OSError as e:
                log('mqtt', f'authorized_keys remove failed: {e}')
            return

        try:
            os.makedirs(ssh_dir, mode=0o755, exist_ok=True)
            tmp = auth_file + '.tmp'
            with open(tmp, 'w') as f:
                f.write('\n'.join(all_keys) + '\n')
            os.chmod(tmp, 0o644)
            os.rename(tmp, auth_file)
            log('mqtt', f'Wrote authorized_keys with '
                       f'{len(dev_keys)} dev + {len(cloud_keys)} cloud key(s)')
        except OSError as e:
            log('mqtt', f'ERROR: authorized_keys write failed: {e}')

    def _sync_app_users(self, instances):
        """Mirror desired-state.instances to `app-<name>` system users so
        per-app SSH (`ssh app-<name>@host`) lands in the right container
        via the sshd_config.d/reefy-apps.conf ForceCommand. The reefy
        user's authorized_keys file is shared (set via AuthorizedKeysFile
        in sshd_config), so adding a key on the dashboard grants access
        to all app shells in one go.

        Runs every state apply (cloud push or boot-from-saved). Idempotent:
        adduser is skipped if the user already exists, deluser only fires
        for users that no longer have a matching instance.

        Uses busybox `adduser`/`deluser`/`addgroup` (Reefy OS = buildroot,
        no shadow-utils). Flag mapping:
          -D            do not prompt for password
          -S            system user (UID < 1000)
          -G nobody     primary group must exist; busybox `-S` defaults
                        to "nogroup" which Reefy OS does not ship
          -s /bin/sh    sshd executes ForceCommand via the user login
                        shell (`<shell> -c <cmd>`), so /sbin/nologin
                        would block it with "account not available".
                        PasswordAuthentication=no + ForceCommand in the
                        Match block still constrain access.
          (no -H/-h)    Let busybox create /home/<user> normally, owned
                        by the user. Things like `docker` (~/.docker/),
                        `tmux` (~/.tmux.conf), or any tool the user
                        invokes inside the container that bind-mounts
                        their host HOME would otherwise be surprised
                        by HOME=/.

        After adduser:
          - `passwd -u` clears the locked-account flag (`adduser -D`
            leaves shadow entry as `!`, which sshd refuses even for
            publickey auth).
          - `adduser <user> docker` grants access to /var/run/docker.sock
            so reefy-app-shell can `docker exec` into the container.
            Requires CONFIG_FEATURE_ADDUSER_TO_GROUP=y in the buildroot
            busybox config (set in board/reefy/reefy/busybox.fragment);
            without it, busybox silently treats the second arg as a
            stray and membership is never updated. Bounded by
            ForceCommand to that one container."""
        try:
            wanted = set()
            for inst in instances or []:
                name = (inst.get('instance_name') or '').strip()
                if name:
                    wanted.add(f'app-{name}')

            existing = set()
            with open('/etc/passwd') as f:
                for line in f:
                    user = line.split(':', 1)[0]
                    if user.startswith('app-'):
                        existing.add(user)

            for user in wanted - existing:
                proc = subprocess.run(
                    ['adduser', '-D', '-S',
                     '-G', 'nobody', '-s', '/bin/sh', user],
                    capture_output=True, text=True)
                if proc.returncode != 0:
                    log('mqtt',
                        f'adduser {user} failed (rc={proc.returncode}): '
                        f'{proc.stderr.strip()}')
                    continue
                unlock = subprocess.run(
                    ['passwd', '-u', user],
                    capture_output=True, text=True)
                if unlock.returncode != 0:
                    log('mqtt',
                        f'passwd -u {user} failed (rc={unlock.returncode}): '
                        f'{unlock.stderr.strip()}')
                docker_grp = subprocess.run(
                    ['adduser', user, 'docker'],
                    capture_output=True, text=True)
                if docker_grp.returncode != 0:
                    log('mqtt',
                        f'adduser {user} docker failed '
                        f'(rc={docker_grp.returncode}): '
                        f'{docker_grp.stderr.strip()}')
                log('mqtt', f'Created system user {user}')

            for user in existing - wanted:
                proc = subprocess.run(
                    ['deluser', user],
                    capture_output=True, text=True)
                if proc.returncode == 0:
                    log('mqtt', f'Removed system user {user}')
                else:
                    log('mqtt',
                        f'deluser {user} failed (rc={proc.returncode}): '
                        f'{proc.stderr.strip()}')
        except Exception as e:
            log('mqtt', f'_sync_app_users non-fatal error: {e}')

    def _apply_storage(self, storage):
        """Set up LUKS-encrypted internal storage with LVM, mounted at /mnt/reefy-data.

        Supports multiple disks combined into a single LVM volume.
        Each disk is LUKS-encrypted with the same key as the USB key partition.
        Desired state format: {"devices": ["nvme0n1", "sdc"]}
        """
        devices = storage.get('devices', [])
        if not devices:
            return

        # Accept either the new thin LV or the legacy flat LV. VG and
        # LV names use underscore separators, so /dev/mapper/<vg>-<lv>
        # is a simple single-dash join (no dm-mapper escape hack).
        acceptable_sources = set()
        for lv in (self.STORAGE_LV, self.LEGACY_STORAGE_LV):
            acceptable_sources.add(f'/dev/{self.STORAGE_VG}/{lv}')
            acceptable_sources.add(f'/dev/mapper/{self.STORAGE_VG}-{lv}')

        # If reefy-data is already mounted from one of our LVs, only
        # check whether new disks showed up that we should extend into.
        try:
            reefy_data_src = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE', self.REEFY_DATA_MNT],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            reefy_data_src = ''
        if reefy_data_src in acceptable_sources:
            new_disks = self._storage._find_new_storage_disks(devices)
            if not new_disks:
                log('mqtt', f'Internal storage already at {self.REEFY_DATA_MNT}')
                return
            self._storage._extend_storage(new_disks)
            return

        key_part = self._storage._find_reefy_key_partition()
        if not key_part:
            raise RuntimeError('Cannot find reefy LUKS key partition')

        luks_key_size = 44

        subprocess.run(['modprobe', 'dm_crypt'], capture_output=True)
        subprocess.run(['modprobe', 'dm_mod'], capture_output=True)

        vg_exists = subprocess.run(
            ['vgs', self.STORAGE_VG], capture_output=True
        ).returncode == 0

        # Classify the complete selected set before changing any fresh disk.
        # Existing LUKS devices are opened first so their mapper geometry can
        # constrain the one explicit sector size used for every fresh target.
        luks_pvs = []
        existing_luks = []
        fresh_targets = []
        expected_pvs = 0
        for dev_name in devices:
            target = f'/dev/{dev_name}'
            if not os.path.exists(target):
                if vg_exists:
                    log('reconciler',
                        f'Storage device {target} is offline; continuing '
                        f'with the existing VG in degraded mode')
                    continue
                raise RuntimeError(f'Storage device {target} not found')
            expected_pvs += 1
            luks_name = f'reefy-{dev_name}'
            mapper_path = f'/dev/mapper/{luks_name}'
            if os.path.exists(mapper_path):
                luks_pvs.append(mapper_path)
                continue

            is_luks = subprocess.run(
                ['cryptsetup', 'isLuks', target],
                capture_output=True, timeout=5).returncode == 0
            if is_luks:
                existing_luks.append((target, luks_name, mapper_path))
            else:
                fresh_targets.append((target, luks_name))

        newly_opened_names = []
        try:
            for target, luks_name, mapper_path in existing_luks:
                r = subprocess.run(
                    ['cryptsetup', 'luksOpen', target, luks_name,
                     '--perf-submit_from_crypt_cpus',
                     '--key-file', key_part,
                     '--keyfile-size', str(luks_key_size)],
                    capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    raise RuntimeError(
                        f'LUKS open failed on {target}: '
                        f'{self._storage._process_error(r)}')
                newly_opened_names.append(luks_name)
                luks_pvs.append(mapper_path)

            if vg_exists:
                required_sector_size = (
                    self._storage._vg_mapper_sector_size())
                if luks_pvs:
                    self._storage._require_common_mapper_sector_size(
                        list(luks_pvs),
                        required_sector_size=required_sector_size,
                    )
            elif luks_pvs:
                required_sector_size = (
                    self._storage._require_common_mapper_sector_size(
                        list(luks_pvs)))
            else:
                required_sector_size = None

            if fresh_targets:
                fresh_pvs = self._storage._provision_luks_stack(
                    fresh_targets,
                    key_part,
                    _log=lambda message: log('reconciler', message),
                    sector_size=required_sector_size,
                    write_keyfile=False,
                )
                newly_opened_names.extend(
                    os.path.basename(path) for path in fresh_pvs)
                if len(fresh_pvs) != len(fresh_targets):
                    raise RuntimeError(
                        f'Prepared {len(fresh_pvs)} of '
                        f'{len(fresh_targets)} fresh storage devices')
                luks_pvs.extend(fresh_pvs)

            if not luks_pvs:
                raise RuntimeError('No storage devices could be prepared')
            if len(luks_pvs) != expected_pvs:
                raise RuntimeError(
                    f'Prepared {len(luks_pvs)} of {expected_pvs} available '
                    f'selected storage devices')
            self._storage._require_common_mapper_sector_size(list(luks_pvs))
        except Exception:
            for mapper_name in reversed(newly_opened_names):
                try:
                    subprocess.run(
                        ['cryptsetup', 'luksClose', mapper_name],
                        capture_output=True, timeout=30)
                except Exception:
                    pass
            raise

        # _ensure_lvm_stack handles fresh-VG-create, existing-VG-extend,
        # and the legacy-LV mount path uniformly.
        self._storage._ensure_lvm_stack(luks_pvs, _log=lambda m: log('reconciler', m))
        lv_path = self._storage._active_reefy_lv_path()
        if not lv_path:
            raise RuntimeError('No reefy LV after LVM setup')

        # Mount at /mnt/reefy-data (migrate state from overlay if needed)
        subprocess.run(['cp', '-a', f'{self.REEFY_DATA_MNT}/state', '/tmp/reefy-state'],
                       capture_output=True, timeout=10)
        subprocess.run(['umount', self.REEFY_DATA_MNT],
                       capture_output=True, timeout=5)
        result = subprocess.run(
            ['mount', '-o', self.REEFY_DATA_MOUNT_OPTS, lv_path, self.REEFY_DATA_MNT],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            subprocess.run(['cp', '-a', '/tmp/reefy-state', f'{self.REEFY_DATA_MNT}/state'],
                           capture_output=True, timeout=10)
            subprocess.run(['rm', '-rf', '/tmp/reefy-state'],
                           capture_output=True, timeout=5)
            os.makedirs(f'{self.REEFY_DATA_MNT}/state/lan', exist_ok=True)
            os.makedirs(f'{self.REEFY_DATA_MNT}/apps', exist_ok=True)
            os.makedirs(f'{self.REEFY_DATA_MNT}/docker', exist_ok=True)
            dev_list = ', '.join(devices)
            log('mqtt', f'Mounted {dev_list} at {self.REEFY_DATA_MNT}')
            subprocess.run(['systemctl', 'restart', 'docker'],
                           capture_output=True, timeout=60)
        else:
            subprocess.run(['cp', '-a', '/tmp/reefy-state', f'{self.REEFY_DATA_MNT}/state'],
                           capture_output=True, timeout=10)
            raise RuntimeError(f'Mount failed: {result.stderr}')

    def _apply_backup_config(self, backup):
        """Write backup SSH keys, config JSON, and install/update systemd timer.

        backup section from desired state:
        {
            "schedule": "03:00",
            "retention": {"keep_last": 30},
            "instances": [
                {
                    "instance_uuid": "...",
                    "archive_prefix": "...",
                    "repo_path": "ssh://...",
                    "ssh_key": "base64-encoded-ed25519-private-key",
                    "passphrase": "...",
                    "paths": ["/mnt/reefy-data/apps/uuid/data"],
                    "container_name": "state-uuid-1",
                    "restore_from": "optional-archive-name"
                }
            ]
        }
        """
        os.makedirs(self.BACKUP_DIR, exist_ok=True)

        instances = backup.get('instances', [])
        if not instances:
            return

        # Write per-instance SSH keys
        for inst in instances:
            iuuid = inst.get('instance_uuid', '')
            ssh_key_b64 = inst.get('ssh_key', '')
            if not iuuid or not ssh_key_b64:
                continue
            key_dir = os.path.join(self.BACKUP_DIR, iuuid)
            os.makedirs(key_dir, exist_ok=True)
            key_path = os.path.join(key_dir, 'id_ed25519')
            try:
                key_data = base64.b64decode(ssh_key_b64)
                with open(key_path, 'wb') as f:
                    f.write(key_data)
                os.chmod(key_path, 0o600)
            except Exception as e:
                log('mqtt', f'Failed to write SSH key for {iuuid}: {e}')

        # Write config JSON (used by reefy-backup)
        config = {
            'schedule': backup.get('schedule', '03:00'),
            'retention': backup.get('retention', {'keep_last': 30}),
            'topic_prefix': self.topic_prefix,
            'device_uuid': self.device_uuid,
            'instances': [],
        }
        # Pass through whatever the cloud sent for this instance,
        # minus ssh_key (already extracted to a per-instance key file
        # above). New fields the cloud adds later (excludes,
        # pinned_archives, retention overrides, …) flow into
        # config.json without a code change here, and the consumer
        # (reefy-backup) just inst.get(field, default).
        for inst in instances:
            config['instances'].append(
                {k: v for k, v in inst.items() if k != 'ssh_key'})

        with open(self.BACKUP_CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        os.chmod(self.BACKUP_CONFIG_PATH, 0o600)
        log('mqtt', f'Wrote backup config ({len(instances)} instances)')

        # Install/update systemd timer for scheduled backups
        schedule = backup.get('schedule', '03:00')
        self._install_backup_timer(schedule)

    def _install_backup_timer(self, schedule):
        """Install or update systemd timer for daily backup at given time (HH:MM UTC)."""
        timer_path = f'/etc/systemd/system/{self.BACKUP_SERVICE}.timer'
        service_path = f'/etc/systemd/system/{self.BACKUP_SERVICE}.service'

        timer_content = f"""[Unit]
Description=reefy daily backup timer

[Timer]
OnCalendar=*-*-* {schedule}:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
"""
        service_content = f"""[Unit]
Description=reefy backup service

[Service]
Type=oneshot
ExecStart=/usr/bin/reefy-backup
TimeoutStartSec=3600
Environment=MQTT_BROKER={self.broker}
Environment=MQTT_PORT={self.port}
"""

        # Only rewrite if changed
        changed = False
        for path, content in [(timer_path, timer_content), (service_path, service_content)]:
            existing = ''
            if os.path.exists(path):
                with open(path, 'r') as f:
                    existing = f.read()
            if existing != content:
                with open(path, 'w') as f:
                    f.write(content)
                changed = True
                log('mqtt', f'Wrote {path}')

        if changed:
            subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, timeout=10)

        # Enable and start timer
        subprocess.run(
            ['systemctl', 'enable', '--now', f'{self.BACKUP_SERVICE}.timer'],
            capture_output=True, timeout=10
        )
        log('mqtt', f'Backup timer enabled (schedule={schedule} UTC)')

    def _restore_instances(self, backup):
        """Restore instances that have restore_from set, before containers start.

        For each instance with restore_from (exact archive name from server):
        1. Check for .restored marker → skip if exists (already restored)
        2. borg extract the specified archive
        3. Fix ownership
        4. Write .restored marker
        5. Report restore completion via MQTT

        Returns set of instance_uuids whose restore failed — these must NOT be
        started (excluded from compose) to prevent running with empty data.
        """
        failed = set()
        for inst in backup.get('instances', []):
            restore_from = inst.get('restore_from')
            if not restore_from:
                continue

            iuuid = inst['instance_uuid']
            marker_dir = os.path.join(self.BACKUP_DIR, iuuid)
            marker_path = os.path.join(marker_dir, '.restored')

            # Skip if already restored
            if os.path.exists(marker_path):
                log('mqtt', f'Instance {iuuid} already restored, skipping')
                continue

            repo_path = inst['repo_path']
            passphrase = inst['passphrase']
            paths = inst.get('paths', [])
            key_path = os.path.join(self.BACKUP_DIR, iuuid, 'id_ed25519')

            if not os.path.exists(key_path):
                log('mqtt', f'No SSH key for {iuuid}, cannot restore')
                self._publish_restore_status(
                    iuuid, 'error', restore_from,
                    error='No SSH key on device')
                failed.add(iuuid)
                continue

            log('backup', f'{f'Restoring {iuuid} from backup {restore_from}'}')
            self._publish_stage('applying', f'Restoring {iuuid} from backup')
            self._publish_restore_status(iuuid, 'started', restore_from)
            log('mqtt', f'Restoring instance {iuuid} from {restore_from}')

            env = os.environ.copy()
            env['BORG_PASSPHRASE'] = passphrase
            env['BORG_RSH'] = f'ssh -i {key_path} -o StrictHostKeyChecking=accept-new'
            env['BORG_RELOCATED_REPO_ACCESS_IS_OK'] = 'yes'

            # Server provides the exact archive name — use it directly
            archive_name = restore_from

            # Restore via `borg extract` straight into the pre-mounted
            # per-volume LVs (no FUSE, no staging tmp, no double-space).
            # We previously used `borg mount` + rsync but the mount
            # parent daemonizes and exits 0 even when the daemon dies,
            # so failures were invisible. `borg extract` is synchronous
            # - rc != 0 means a real failure with stderr captured.
            #
            # Layout: snapshot-based archives have the volume name at
            # the top level (`config/...`); legacy docker-pause archives
            # bury volumes under `mnt/<reefy|sbnb>-data/apps/<old_iuuid>/
            # <vol>/`. Detect the layout from the first archived path
            # and pass --strip-components 4 for the legacy case so files
            # land at new_inst_dir/<vol>/... either way.
            new_inst_dir = f'/mnt/reefy-data/apps/{iuuid}'
            os.makedirs(new_inst_dir, mode=0o755, exist_ok=True)

            log('mqtt', f'Restoring archive {archive_name} via borg extract')
            try:
                list_proc = subprocess.run(
                    ['borg', 'list', '--short',
                     f'{repo_path}::{archive_name}'],
                    env=env, capture_output=True, text=True, timeout=120
                )
                if list_proc.returncode != 0:
                    err_msg = (list_proc.stderr or "").strip()[:500]
                    log('mqtt',
                        f'borg list failed (rc={list_proc.returncode}): '
                        f'{err_msg}')
                    self._publish_restore_status(
                        iuuid, 'error', archive_name,
                        error=f'borg list rc={list_proc.returncode}: {err_msg}')
                    failed.add(iuuid)
                    continue
                first_path = next(
                    (l.strip() for l in list_proc.stdout.splitlines()
                     if l.strip()), '')
                strip_n = 0
                parts = first_path.split('/')
                if len(parts) >= 5 and parts[0] == 'mnt' and \
                        parts[1] in ('reefy-data', 'sbnb-data') and \
                        parts[2] == 'apps':
                    strip_n = 4

                cmd = ['borg', '--log-json', 'extract', '--progress']
                if strip_n:
                    cmd += ['--strip-components', str(strip_n)]
                cmd.append(f'{repo_path}::{archive_name}')
                EXTRACT_TIMEOUT = 6 * 3600  # 6h cap for huge archives
                extract_proc = subprocess.Popen(
                    cmd, env=env, cwd=new_inst_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )

                # Stream borg's --log-json stderr through to our log.
                # Pass everything except in-flight progress_percent
                # frames, which can fire many times per second on big
                # archives - throttle those to one every PROGRESS_EVERY_S.
                # log_message lines (warnings, errors) and the final
                # `finished: true` always pass through.
                PROGRESS_EVERY_S = 5.0
                last_progress_t = 0.0
                deadline = time.time() + EXTRACT_TIMEOUT
                for raw in iter(extract_proc.stderr.readline, b''):
                    if time.time() > deadline:
                        extract_proc.kill()
                        break
                    line = raw.decode('utf-8', errors='replace').rstrip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        log('mqtt', f'borg: {line}')
                        continue
                    if obj.get('type') == 'progress_percent' \
                            and not obj.get('finished'):
                        now = time.time()
                        if now - last_progress_t < PROGRESS_EVERY_S:
                            continue
                        last_progress_t = now
                    log('mqtt', f'borg: {line}')

                extract_proc.wait()
                if extract_proc.returncode != 0:
                    log('mqtt',
                        f'borg extract failed (rc={extract_proc.returncode})')
                    self._publish_restore_status(
                        iuuid, 'error', archive_name,
                        error=f'borg extract rc={extract_proc.returncode}')
                    failed.add(iuuid)
                    continue
                log('mqtt', f'borg extract completed for {iuuid}')

            except Exception as e:
                log('mqtt', f'Restore error: {e}')
                self._publish_restore_status(
                    iuuid, 'error', archive_name, error=str(e))
                failed.add(iuuid)
                continue

            # Write .restored marker
            os.makedirs(marker_dir, exist_ok=True)
            with open(marker_path, 'w') as f:
                f.write(archive_name + '\n')
            log('mqtt', f'Restore complete for {iuuid} (archive={archive_name})')

            # Report restore success on the unified instance/status
            # channel. Backend dispatches on action='restore' and
            # clears restore_from for this instance, breaking the
            # restore-retry loop on subsequent applies.
            self._publish_restore_status(iuuid, 'success', archive_name)

        return failed

    def _apply_compose(self, compose):
        """Serialize full-project Compose apply against app restarts."""
        with self._compose_mutation_lock:
            return self._apply_compose_locked(compose)

    def _apply_compose_locked(self, compose):
        """Write compose JSON and run docker compose up, streaming output to logs.
        Caller must hold _compose_mutation_lock. Returns True on success,
        False on failure."""
        os.makedirs(os.path.dirname(self.COMPOSE_PATH), exist_ok=True)

        # Diagnostic: log which services' config changed vs current
        # file. If a container later doesn't reflect a logged change,
        # `docker compose up` missed the diff (e.g. the `devices`
        # field bug seen with ollama+CDI). Hash, not full diff, to
        # keep logs short.
        try:
            old = json.load(open(self.COMPOSE_PATH)) if os.path.exists(self.COMPOSE_PATH) else {}
            def _h(c): return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:8]
            changed = [n for n, c in compose.get('services', {}).items()
                       if _h(c) != _h(old.get('services', {}).get(n, {}))]
            if changed:
                log('reconciler', f'compose changes: {",".join(changed)}')
        except Exception as e:
            log('reconciler', f'compose-diff log failed: {e}')

        with open(self.COMPOSE_PATH, 'w') as f:
            json.dump(compose, f, indent=2)
        log('mqtt', f'Wrote {self.COMPOSE_PATH}')

        # Per-instance health: announce 'starting' upfront so the
        # dashboard shows a spinner badge on each card while compose
        # is in flight. After compose returns we emit either 'running'
        # (clears any prior failed badge) or 'failed' (sticks with the
        # last few output lines as the error message).
        instance_uuids = shared.instance_uuids_in_compose(compose)

        # (b) Sticky terminal failure: if this exact compose already
        # failed non-recoverably, don't re-pull it - the outer reconcile
        # loop would otherwise restart the (often multi-GB) doomed pull on
        # every event. Re-surface the failure and wait for a CHANGED
        # desired-state (free space / fix the image / uninstall the app ->
        # different sig -> retried).
        sig = self._compose_sig(compose)
        failed_sig, failed_reason = self._read_failed_sig()
        if sig == failed_sig:
            log('mqtt', 'compose unchanged since terminal failure '
                f'({failed_reason}); skipping re-pull until desired-state '
                'changes')
            for iuuid in instance_uuids:
                self._publish_health_status(
                    iuuid, 'failed', message=failed_reason)
            return False

        # Per-instance health: announce 'starting' upfront so the dashboard
        # shows a spinner badge while compose is in flight.
        for iuuid in instance_uuids:
            self._publish_health_status(iuuid, 'starting')

        max_retries = 5
        backoff = 10
        pruned = False
        last_output = ''
        reason = 'docker compose up failed'
        for attempt in range(1, max_retries + 1):
            output_lines = []
            try:
                log('reconciler', f'docker compose up (attempt {attempt}/{max_retries})')
                proc = subprocess.Popen(
                    ['docker', 'compose', '-f', self.COMPOSE_PATH, 'up', '-d', '--pull', 'missing', '--remove-orphans'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in proc.stdout:
                    line = line.rstrip('\n')
                    log('compose', f'{line}')
                    output_lines.append(line)
                proc.wait(timeout=120)
                if proc.returncode == 0:
                    log('mqtt', 'docker compose up OK')
                    self._clear_failed_sig()  # success clears the guard
                    services = compose.get('services', {})
                    for iuuid in instance_uuids:
                        self._publish_health_status(
                            iuuid, 'running',
                            image=services.get(iuuid, {}).get('image'))
                    return True
                log('mqtt', f'docker compose up failed (attempt {attempt}/{max_retries})')
            except subprocess.TimeoutExpired:
                proc.kill()
                output_lines.append('docker compose up timed out')
                log('mqtt', f'docker compose up timed out (attempt {attempt}/{max_retries})')
            except Exception as e:
                output_lines.append(f'error: {e}')
                log('mqtt', f'docker compose up error (attempt {attempt}/{max_retries}): {e}')

            last_output = '\n'.join(output_lines)
            cls = self._classify_compose_failure(last_output)
            reason = self._failure_reason(cls, last_output)

            # (a) Fail-fast on deterministic, non-retryable failures so we
            # don't burn the full 5x re-pull budget (each a full image
            # pull) on an error that will never self-heal.
            if cls == 'image_missing':
                log('mqtt', f'non-retryable: {reason}; giving up')
                break
            if cls == 'no_space':
                if pruned:
                    log('mqtt', f'non-retryable: {reason} (prune did not help); giving up')
                    break
                pruned = True
                # Prune once; only retry if it actually freed space - a
                # retry after a 0B prune is just another full doomed pull.
                if not self._prune_docker(volumes=False):
                    log('mqtt', f'non-retryable: {reason} (prune reclaimed nothing); giving up')
                    break
                log('mqtt', 'prune reclaimed space; retrying compose up once')
                continue
            if cls == 'storage_corruption' and not pruned:
                pruned = True
                self._prune_docker(volumes=True)   # broken overlay layers
                continue

            # transient (or recovery already attempted): backoff + retry
            if attempt < max_retries:
                delay = backoff * (2 ** (attempt - 1))
                log('mqtt', f'Retrying in {delay}s...')
                time.sleep(delay)

        # Terminal failure: record a sticky signature so the reconcile
        # loop won't re-pull this unchanged compose, and publish a failed
        # badge with the reason + last output lines.
        log('mqtt', f'docker compose up failed: {reason}')
        self._write_failed_sig(sig, reason)
        tail = '\n'.join(last_output.splitlines()[-5:]) if last_output else reason
        for iuuid in instance_uuids:
            self._publish_health_status(iuuid, 'failed', message=tail)
        return False

    @staticmethod
    def _compose_sig(compose):
        """Stable signature of a compose dict, for the sticky-failure guard."""
        return hashlib.sha256(
            json.dumps(compose, sort_keys=True).encode()).hexdigest()

    def _read_failed_sig(self):
        """(sig, reason) of the last terminal compose failure, or (None, '')."""
        try:
            with open(self._FAILED_SIG_PATH) as f:
                sig, _, reason = f.read().partition('\n')
            return (sig.strip() or None), reason.strip()
        except Exception:
            return None, ''

    def _write_failed_sig(self, sig, reason):
        try:
            os.makedirs(os.path.dirname(self._FAILED_SIG_PATH), exist_ok=True)
            with open(self._FAILED_SIG_PATH, 'w') as f:
                f.write(f'{sig}\n{reason}')
        except Exception as e:
            log('mqtt', f'failed to persist sticky-failure sig: {e}')

    def _clear_failed_sig(self):
        try:
            os.remove(self._FAILED_SIG_PATH)
        except FileNotFoundError:
            pass
        except Exception as e:
            log('mqtt', f'failed to clear sticky-failure sig: {e}')

    @staticmethod
    def _classify_compose_failure(output):
        """Bucket Docker Compose output into a retry policy class.

        Order matters: `no_space` is checked FIRST because the kernel's
        "no space left on device" often rides on a "failed to register
        layer: ..." line, which would otherwise look like recoverable
        storage corruption."""
        o = (output or '').lower()
        if 'no space left on device' in o:
            return 'no_space'              # deterministic until space is freed
        image_missing = [
            'manifest unknown',
            'not found: manifest',
            'no such image',
            'pull access denied',
            'repository does not exist',
            'requested access to the resource is denied',
            'denied: requested access',
            'unauthorized: authentication required',
            'manifest for ',                # "...not found: manifest unknown"
        ]
        if any(s in o for s in image_missing):
            return 'image_missing'         # bad ref; retrying never helps
        storage_corruption = [
            'failed to register layer',
            'layer does not exist',
            'error creating overlay mount',
            'failed to mount overlay',
        ]
        if any(s in o for s in storage_corruption):
            return 'storage_corruption'    # prune may repair broken layers
        return 'transient'                 # network/5xx/timeout -> keep retrying

    @staticmethod
    def _failure_reason(cls, output, phase='up'):
        return {
            'no_space': 'out of disk space',
            'image_missing': 'image not found or access denied',
            'storage_corruption': 'docker storage error',
        }.get(cls, f'docker compose {phase} failed')

    def _prune_docker(self, volumes=False):
        """Reclaim docker space; `volumes=True` also drops anonymous
        volumes (only for broken-layer recovery, never routine no-space).
        Returns True if prune actually reclaimed space - retrying a
        no_space pull is pointless when prune freed nothing."""
        cmd = ['docker', 'system', 'prune', '-a', '-f']
        if volumes:
            cmd.append('--volumes')
        reclaimed_any = False
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120)
            for line in (result.stdout or '').strip().split('\n'):
                if line.strip():
                    log('mqtt', f'prune: {line}')
                if 'Total reclaimed space:' in line:
                    amount = line.split(':', 1)[1].strip()
                    reclaimed_any = amount not in ('0B', '0 B', '0')
        except Exception as e:
            log('mqtt', f'Docker prune failed: {e}')
        return reclaimed_any


def main_data_plane():
    """Data-plane executable entrypoint (reefy-reconciler)."""
    try:
        DataPlane(Storage()).run_data_plane()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        error = shared.redact_log_message(e)
        log('mqtt',
            f'[data-plane] fatal ({type(e).__name__}): {error}')
        sys.exit(1)
    sys.exit(0)
