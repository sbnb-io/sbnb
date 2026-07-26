"""Regression tests for reefy.control.

Most tests inspect source via find_spec. Behavior tests load the module
with a minimal paho stub so they also run in the paho-less test image."""

import importlib.util
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
        self.assertIn("_varlink_call('Reconcile')", src)


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
        return plane

    def test_apply_failure_publishes_exact_error_and_command_fails(self):
        error = (
            'No safe common LUKS sector size for selected devices '
            'required=4096: /dev/sdb logical=512 physical=512')
        plane = self._plane()
        payload = {
            'action': 'apply_state',
            'state': {'storage': {'devices': ['sda', 'sdb']}},
        }

        with mock.patch.object(
                plane, '_varlink_call',
                return_value={'ok': False, 'error': error}), \
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
        self.assertTrue(plane._apply_lock.acquire(blocking=False))
        plane._apply_lock.release()

    def test_reconcile_failure_stays_online_and_publishes_exact_error(self):
        error = 'Selected storage device(s) not found: /dev/sdb'
        plane = self._plane()

        with mock.patch.object(
                plane, '_varlink_call',
                return_value={'ok': False, 'error': error}), \
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
