"""Regression tests for reefy.control.

Most tests inspect source via find_spec. Behavior tests load the module
with a minimal paho stub so they also run in the paho-less test image."""

import importlib.util
import json
import sys
import types
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)


def _control_src():
    spec = importlib.util.find_spec('reefy.control')
    with open(spec.origin) as f:
        return f.read()


def _load_control_module():
    """Load reefy.control without requiring paho in the unit-test image."""
    source_spec = importlib.util.find_spec('reefy.control')
    module_spec = importlib.util.spec_from_file_location(
        'reefy_control_behavior_test', source_spec.origin)
    module = importlib.util.module_from_spec(module_spec)

    paho = types.ModuleType('paho')
    paho.__path__ = []
    paho_mqtt = types.ModuleType('paho.mqtt')
    paho_mqtt.__path__ = []
    paho_client = types.ModuleType('paho.mqtt.client')
    paho.mqtt = paho_mqtt
    paho_mqtt.client = paho_client

    modules = {
        module_spec.name: module,
        'paho': paho,
        'paho.mqtt': paho_mqtt,
        'paho.mqtt.client': paho_client,
    }
    with mock.patch.dict(sys.modules, modules):
        module_spec.loader.exec_module(module)
    return module


class BootApplyRegressionTests(unittest.TestCase):
    def test_no_control_side_offline_boot_apply(self):
        # The data plane owns boot reconcile (reefy.dataplane._boot_apply).
        # A control-side offline apply at boot delegated over Varlink to a
        # socket that wasn't up yet ("data plane unreachable") and held the
        # apply lock so the legit on-connect apply got skipped. It must
        # stay gone - control's boot job is to call home.
        src = _control_src()
        self.assertNotIn('Applying saved desired state (offline)', src)
        self.assertNotIn('skipping offline apply', src)


class StateOwnershipRegressionTests(unittest.TestCase):
    def test_control_never_writes_desired_state(self):
        # The data plane is the SOLE writer of desired-state.json: it reads
        # the prior file as old_state for diff-based cleanup (LV reclaim,
        # static-IP removal) before overwriting. Control writing the file
        # first clobbered that read (old_state == new_state -> reclaim a
        # no-op, the e2e backup-lvm failure). Control must only forward
        # state over Varlink, never persist it.
        src = _control_src()
        self.assertNotIn("DESIRED_STATE_PATH, 'w'", src)
        self.assertNotIn('Saved desired state', src)

    def test_control_resyncs_via_reconcile(self):
        # On connect, control asks the data plane to re-apply its own saved
        # state via the Reconcile Varlink call, rather than reading
        # desired-state.json and forwarding it.
        src = _control_src()
        self.assertIn("_submit_and_wait_apply('SubmitReconcile')", src)


class HardwareInventoryRegressionTests(unittest.TestCase):
    def test_lsblk_collects_all_columns_without_changing_size_format(self):
        # Full disk topology is needed to diagnose model, transport, and
        # logical-sector incompatibilities from stored hw_info. Keep sizes
        # human-readable because existing API consumers display them as-is.
        src = _control_src()
        self.assertIn("('lsblk', ['lsblk', '-J', '-O'])", src)
        self.assertNotIn("['lsblk', '-J', '-b', '-O']", src)


class VarlinkStartupRaceTests(unittest.TestCase):
    def test_varlink_call_waits_out_reconciler_startup(self):
        # reefy-control and reefy-reconciler start in parallel; control
        # connects to MQTT and fires reconcile-on-connect a few seconds
        # before the reconciler binds /run/reefy/reconciler.sock. The old
        # _varlink_call burned its short retry budget (3x2s) before the
        # socket existed and logged a spurious "data plane unreachable:
        # [Errno 2] No such file or directory" on every boot. The fix
        # treats "socket not ready yet" (ENOENT / connection refused) as a
        # transient startup condition and keeps retrying for a grace
        # window that outlasts the reconciler's startup. Source-level
        # (control imports paho at module top, so it can't be executed
        # here) - guards the grace + not-ready detection from regressing.
        src = _control_src()
        self.assertIn('startup_grace_s', src)
        self.assertIn('FileNotFoundError', src)
        self.assertIn('not_ready', src)


class DesiredStateErrorPropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = _load_control_module()

    def _plane(self):
        plane = self.control.ControlPlane()
        plane.mode = 'device'
        plane.topic_prefix = 'reefy/synthetic-public'
        return plane

    def test_apply_command_submits_and_waits_for_its_request(self):
        plane = self._plane()
        calls = []

        def varlink(method, **kwargs):
            calls.append((method, kwargs))
            if method == 'SubmitApply':
                return {'ok': True, 'request_id': 'synthetic-request'}
            return {
                'found': True,
                'request_id': 'synthetic-request',
                'status': 'succeeded',
                'error': '',
                'warnings': [],
                'applied': True,
            }

        with mock.patch.object(plane, '_varlink_call', side_effect=varlink), \
                mock.patch.object(plane, '_publish_stage'), \
                mock.patch.object(plane, '_publish_status'), \
                mock.patch.object(plane, '_publish_state_hash'), \
                mock.patch.object(
                    self.control.shared, 'wait_for_tunnel_health'), \
                mock.patch.object(self.control, 'log'):
            result = plane._apply_state_command(
                {'state': {'sequence': 'active'}})

        self.assertEqual(result, 'State applied')
        self.assertEqual(calls[0][0], 'SubmitApply')
        self.assertEqual(calls[1], (
            'WaitApply', {'request_id': 'synthetic-request'}))

    def test_apply_failure_publishes_exact_error_and_command_fails(self):
        error = (
            'No safe common LUKS sector size for selected devices '
            'required=4096: /dev/sdb logical=512 physical=512')
        plane = self._plane()
        payload = {
            'action': 'apply_state',
            'state': {'storage': {'devices': ['sda', 'sdb']}},
        }

        def varlink(method, **kwargs):
            if method == 'SubmitApply':
                return {'ok': True, 'request_id': 'synthetic-failed'}
            return {
                'found': True,
                'request_id': 'synthetic-failed',
                'status': 'failed',
                'error': error,
                'warnings': [],
                'applied': True,
            }

        with mock.patch.object(
                plane, '_varlink_call', side_effect=varlink), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(plane, '_publish_status') as status, \
                mock.patch.object(plane, '_publish_state_hash') as state_hash, \
                mock.patch.object(plane, '_send_command_response') as response, \
                mock.patch.object(
                    self.control.shared, 'wait_for_tunnel_health') as wait, \
                mock.patch.object(self.control, 'log'):
            plane._run_command('_apply_state_command', payload, 'cmd-1')

        self.assertEqual(stage.call_args_list, [
            mock.call('applying', 'Applying desired state'),
            mock.call('error', error),
        ])
        response.assert_called_once_with(
            'cmd-1', status='error', error=error)
        status.assert_not_called()
        state_hash.assert_not_called()
        wait.assert_not_called()

    def test_reconcile_failure_stays_online_and_publishes_exact_error(self):
        error = 'Selected storage device(s) not found: /dev/sdb'
        plane = self._plane()

        def varlink(method, **kwargs):
            if method == 'SubmitReconcile':
                return {'ok': True, 'request_id': 'synthetic-reconcile'}
            return {
                'found': True,
                'request_id': 'synthetic-reconcile',
                'status': 'failed',
                'error': error,
                'warnings': [],
                'applied': True,
            }

        with mock.patch.object(
                plane, '_varlink_call', side_effect=varlink), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(plane, '_publish_status') as status, \
                mock.patch.object(plane, '_publish_state_hash') as state_hash, \
                mock.patch.object(
                    self.control.shared, 'wait_for_tunnel_health') as wait, \
                mock.patch.object(self.control, 'log'):
            plane._handle_device_connect(mock.sentinel.client)

        status.assert_called_once_with('online', 'Device connected')
        stage.assert_called_once_with('error', error)
        state_hash.assert_not_called()
        wait.assert_not_called()

    def test_successful_apply_publishes_ready_with_storage_warning(self):
        plane = self._plane()
        warnings = [
            {
                'code': 'storage.cap_not_enforced',
                'instance_uuid': 'synthetic-one',
                'volume': 'media',
            },
            {
                'code': 'storage.cap_not_enforced',
                'instance_uuid': 'synthetic-two',
                'volume': 'cache',
            },
        ]

        def varlink(method, **kwargs):
            if method == 'SubmitApply':
                return {'ok': True, 'request_id': 'synthetic-warning'}
            return {
                'found': True,
                'request_id': 'synthetic-warning',
                'status': 'succeeded_with_warnings',
                'error': '',
                'warnings': warnings,
                'applied': True,
            }

        with mock.patch.object(
                plane, '_varlink_call', side_effect=varlink), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(plane, '_publish_status'), \
                mock.patch.object(plane, '_publish_state_hash'), \
                mock.patch.object(
                    self.control.shared, 'wait_for_tunnel_health'), \
                mock.patch.object(self.control, 'log'):
            plane._apply_and_publish(state={'synthetic': True})

        self.assertEqual(stage.call_args_list, [
            mock.call('applying', 'Applying desired state'),
            mock.call(
                'ready',
                'Device ready with warnings: storage caps not enforced for '
                'synthetic-one/media, synthetic-two/cache'),
        ])

    def test_reconcile_publishes_ready_with_storage_warning(self):
        plane = self._plane()
        warning = {
            'code': 'storage.cap_not_enforced',
            'instance_uuid': 'synthetic-app',
            'volume': 'media',
        }

        def varlink(method, **kwargs):
            if method == 'SubmitReconcile':
                return {'ok': True, 'request_id': 'synthetic-reconcile'}
            return {
                'found': True,
                'request_id': 'synthetic-reconcile',
                'status': 'succeeded_with_warnings',
                'error': '',
                'warnings': [warning],
                'applied': True,
            }

        with mock.patch.object(
                plane, '_varlink_call', side_effect=varlink), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(plane, '_publish_status'), \
                mock.patch.object(plane, '_publish_state_hash'), \
                mock.patch.object(
                    self.control.shared, 'wait_for_tunnel_health'), \
                mock.patch.object(self.control, 'log'):
            plane._handle_device_connect(mock.sentinel.client)

        stage.assert_called_once_with(
            'ready',
            'Device ready with warnings: storage cap not enforced for '
            'synthetic-app/media')

    def test_empty_warnings_keep_plain_ready_message(self):
        plane = self._plane()
        self.assertEqual(plane._ready_stage_message([]), 'Device ready')

    def test_older_completion_cannot_overwrite_newer_applying_stage(self):
        plane = self._plane()
        plane._latest_apply_request_id = 'newer-request'
        older = {
            'found': True,
            'request_id': 'older-request',
            'status': 'succeeded',
            'error': '',
            'warnings': [],
            'applied': True,
        }
        with mock.patch.object(
                plane, '_apply_desired_state', return_value=older), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(plane, '_publish_status') as status, \
                mock.patch.object(plane, '_publish_state_hash') as state_hash, \
                mock.patch.object(
                    self.control.shared, 'wait_for_tunnel_health') as wait, \
                mock.patch.object(self.control, 'log'):
            plane._apply_and_publish(state={'synthetic': True})

        stage.assert_called_once_with('applying', 'Applying desired state')
        status.assert_not_called()
        state_hash.assert_not_called()
        wait.assert_not_called()

    def test_stage_and_command_error_payloads_are_redacted(self):
        secret = 'synthetic-control-boundary-secret'
        plane = self._plane()
        plane.device_uuid = 'synthetic-device'
        plane.current_uuid = 'synthetic-device'
        plane.client = mock.Mock()

        plane._publish_stage('error', f'client_secret={secret}')
        stage_payload = json.loads(plane.client.publish.call_args.args[1])
        self.assertNotIn(secret, stage_payload['message'])
        self.assertIn('[REDACTED]', stage_payload['message'])

        plane.client.reset_mock()
        plane._send_command_response(
            'synthetic-command', status='error', error=f'token={secret}')
        response_payload = json.loads(plane.client.publish.call_args.args[1])
        self.assertNotIn(secret, response_payload['error'])
        self.assertIn('[REDACTED]', response_payload['error'])

        plane.client.reset_mock()
        plane._send_command_response(
            'synthetic-command', message=f'credentials=["{secret}"]')
        response_payload = json.loads(plane.client.publish.call_args.args[1])
        self.assertNotIn(secret, response_payload['message'])
        self.assertIn('[REDACTED]', response_payload['message'])

    def test_control_varlink_error_is_redacted(self):
        secret = 'synthetic-sidecar-secret'
        plane = self._plane()
        plane.device_uuid = 'synthetic-device'
        plane.client = mock.Mock()
        plane.client.is_connected.return_value = True
        plane.client.publish.side_effect = RuntimeError(f'api_key={secret}')

        result = plane._ctl_publish_event('notify', '{}')

        self.assertFalse(result['ok'])
        self.assertNotIn(secret, result['error'])
        self.assertIn('[REDACTED]', result['error'])


class SensitiveDownloadLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = _load_control_module()

    def _plane(self):
        plane = self.control.ControlPlane()
        plane.mode = 'device'
        return plane

    def test_bootstrap_failure_does_not_log_url_or_command_headers(self):
        secret = 'synthetic-bootstrap-signature'
        header_secret = 'synthetic-bootstrap-device'
        url = f'https://download.invalid/bundle?signature={secret}'
        failure = self.control.subprocess.CalledProcessError(
            22, ['curl', '-H', f'X-Device-UUID: {header_secret}', url])
        messages = []
        with mock.patch.object(
                self.control.subprocess, 'run', side_effect=failure), \
                mock.patch.object(
                    self.control, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            self._plane()._download_and_run_bootstrap(url, header_secret)

        rendered = '\n'.join(messages)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(header_secret, rendered)
        self.assertIn('Bootstrap command failed (exit 22)', rendered)

    def test_firmware_download_failure_does_not_log_url_or_stderr(self):
        secret = 'synthetic-firmware-signature'
        url = f'https://download.invalid/image?signature={secret}'
        result = types.SimpleNamespace(
            returncode=22, stdout='', stderr=f'failed URL {url}')
        messages = []
        plane = self._plane()
        with mock.patch.object(
                self.control.subprocess, 'run', return_value=result), \
                mock.patch.object(self.control.os.path, 'exists', return_value=False), \
                mock.patch.object(self.control.os, 'makedirs'), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(
                    self.control, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            plane._update_firmware({
                'url': url,
                'version': 'synthetic-version',
            })

        rendered = '\n'.join(
            messages + [str(call) for call in stage.call_args_list])
        self.assertNotIn(secret, rendered)
        self.assertIn('Firmware download failed (curl exit 22)', rendered)

    def test_reflash_download_failure_does_not_log_url_or_stderr(self):
        secret = 'synthetic-reflash-signature'
        url = f'https://download.invalid/reflash?signature={secret}'
        result = types.SimpleNamespace(
            returncode=22, stdout='', stderr=f'failed URL {url}')
        messages = []
        plane = self._plane()
        plane._storage._find_usb_disk = mock.Mock(
            return_value='/dev/synthetic-disk')
        plane._storage._find_data_dir = mock.Mock(
            return_value='/mnt/reefy-data')
        with mock.patch.object(
                self.control.subprocess, 'run', return_value=result), \
                mock.patch.object(
                    self.control.os, 'statvfs',
                    return_value=types.SimpleNamespace(
                        f_bavail=10_000, f_frsize=4096)), \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(
                    self.control, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            plane._reset_to_bootstrap({
                'reflash': True,
                'image_url': url,
                'size': 0,
            })

        rendered = '\n'.join(
            messages + [str(call) for call in stage.call_args_list])
        self.assertNotIn(secret, rendered)
        self.assertIn('Reflash download failed (curl exit 22)', rendered)

    def test_customer_bundle_failure_does_not_publish_url_or_header(self):
        secret = 'synthetic-config-signature'
        header_secret = 'synthetic-config-device'
        url = f'https://download.invalid/config?signature={secret}'
        failure = self.control.subprocess.CalledProcessError(
            22, ['curl', '-H', f'X-Device-UUID: {header_secret}', url])
        messages = []
        plane = self._plane()
        plane.device_uuid = header_secret
        with mock.patch.object(
                self.control.subprocess, 'run', side_effect=failure), \
                mock.patch.object(self.control.os, 'makedirs'), \
                mock.patch.object(plane, '_publish_status') as status, \
                mock.patch.object(
                    self.control, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            plane._apply_config({
                'bundle_url': url,
                'version': 'synthetic-version',
            })

        rendered = '\n'.join(
            messages + [str(call) for call in status.call_args_list])
        self.assertNotIn(secret, rendered)
        self.assertNotIn(header_secret, rendered)
        self.assertIn('Configuration command failed (exit 22)', rendered)


class SensitivePasswordRotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = _load_control_module()

    def _run_rotation(self, subprocess_side_effect):
        plane = self.control.ControlPlane()
        plane.mode = 'device'
        responses = mock.Mock()
        messages = []
        with mock.patch.object(
                self.control.os, 'urandom', return_value=bytes(12)), \
                mock.patch.object(self.control.os, 'makedirs'), \
                mock.patch.object(self.control.os, 'chmod'), \
                mock.patch('builtins.open', mock.mock_open()), \
                mock.patch.object(
                    self.control.subprocess, 'run',
                    side_effect=subprocess_side_effect), \
                mock.patch.object(
                    plane, '_send_command_response', responses), \
                mock.patch.object(
                    self.control, 'log',
                    side_effect=lambda source, message: messages.append(message)):
            plane._run_command('_rotate_device_password', {}, 'synthetic-command')
        return responses, '\n'.join(messages)

    def test_password_hash_timeout_does_not_surface_password_argv(self):
        password = 'a' * 12
        failure = self.control.subprocess.TimeoutExpired(
            ['mkpasswd', password], 5)

        responses, messages = self._run_rotation(failure)

        rendered = messages + str(responses.call_args_list)
        self.assertNotIn(password, rendered)
        self.assertIn('Password rotation command failed', rendered)

    def test_shadow_update_timeout_does_not_surface_password_hash_argv(self):
        password = 'a' * 12
        password_hash = 'synthetic-shadow-hash'

        def run(command, **kwargs):
            if command[0] == 'mkpasswd':
                return types.SimpleNamespace(
                    returncode=0, stdout=password_hash + '\n', stderr='')
            raise self.control.subprocess.TimeoutExpired(command, 5)

        responses, messages = self._run_rotation(run)

        rendered = messages + str(responses.call_args_list)
        self.assertNotIn(password, rendered)
        self.assertNotIn(password_hash, rendered)
        self.assertIn('Password rotation command failed', rendered)

    def test_raw_tracebacks_are_not_written_to_device_logs(self):
        self.assertNotIn('traceback.print_exc()', _control_src())


if __name__ == '__main__':
    unittest.main()


class ControlPublishEventTests(unittest.TestCase):
    """The sidecar publish path (io.reefy.Control.PublishEvent) lets
    reefy-app-api publish through control's persistent MQTT connection
    without holding the device key. These guard its safety properties."""

    def test_suffix_is_allowlisted(self):
        src = _control_src()
        self.assertIn("CONTROL_PUBLISH_ALLOWED = ('notify',)", src)
        self.assertIn('suffix not in self.CONTROL_PUBLISH_ALLOWED', src)

    def test_publish_requires_connected_client_and_valid_json(self):
        src = _control_src()
        self.assertIn('def _ctl_publish_event', src)
        self.assertIn('self.client.is_connected()', src)
        self.assertIn('json.loads(payload)', src)

    def test_serves_dedicated_sidecar_socket_not_reconciler(self):
        # Must be its OWN socket dir so a sidecar that mounts it can't
        # also reach the reconciler socket in /run/reefy.
        src = _control_src()
        self.assertIn(
            "CONTROL_VARLINK_ADDRESS = 'unix:/run/reefy-sidecar/control.sock'",
            src)
        self.assertIn("io.reefy.Control", src)


class FirmwareUpdateExitCodeTests(unittest.TestCase):
    def test_reefy_update_exit_code_is_logged_and_named(self):
        # A spurious non-zero exit from reefy-update (e.g. an errexit-unsafe
        # cleanup trap firing after a successful write) once masqueraded as
        # an mkfs failure because only stderr was surfaced - reefy-update
        # writes harmless warnings (mkfs.fat's "codepage 850" fallback
        # notice) to stderr. The real exit code is the source of truth and
        # must be logged on every run + named in the error message.
        src = _control_src()
        self.assertIn('[reefy-update] exit code', src)
        self.assertIn('reefy-update failed (exit', src)
