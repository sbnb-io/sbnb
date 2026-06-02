"""Regression tests for reefy.control.

Source-level only: control imports paho at module top, so we inspect the
file text via find_spec (which does NOT execute the module) to stay
runnable in a paho-less env alongside the other tests."""

import importlib.util
import unittest

import _bootstrap  # noqa: F401  (puts the reefy package on sys.path)


def _control_src():
    spec = importlib.util.find_spec('reefy.control')
    with open(spec.origin) as f:
        return f.read()


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


if __name__ == '__main__':
    unittest.main()
