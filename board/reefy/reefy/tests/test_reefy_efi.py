"""Regression test for reefy-efi's A/B-update EXIT trap.

The OTA update path in reefy-efi runs under `set -e` (set near the top of
the script). The EXIT-trap that cleans up the temp mount MUST be
errexit-safe: on the SUCCESS path the mount is already released before the
script exits, so the trap's umount/umount-l/rmdir all fail. Without a
guard, `set -e` then makes reefy-efi exit non-zero AFTER a fully
successful firmware write - and the control plane reads any non-zero exit
as a failed OTA and skips the reboot, so the device silently never
updates (the symptom that broke the dev e2e firmware-update fixture).

This test extracts the real trap line from the shipped script and proves
it cannot poison the exit status under errexit.
"""

import os
import subprocess
import unittest

REEFY_EFI = os.path.join(os.path.dirname(__file__), '..', 'rootfs-overlay',
                         'usr', 'bin', 'reefy-efi')


def _trap_line():
    with open(REEFY_EFI) as f:
        for line in f:
            s = line.strip()
            if s.startswith('trap ') and 'INACTIVE_MNT' in s and 'EXIT' in s:
                return s
    raise AssertionError(
        'could not find the INACTIVE_MNT EXIT trap in reefy-efi')


def _source():
    with open(REEFY_EFI) as f:
        return f.read()


class ExitTrapErrexitSafetyTests(unittest.TestCase):
    def test_trap_does_not_poison_exit_under_errexit(self):
        # Reproduce the success-path cleanup: INACTIVE_MNT names a temp dir
        # that's already been removed (the real flow unmounts + rmdirs it
        # before exit), so the trap's umount/umount-l/rmdir all fail. Under
        # `set -e` the script must STILL exit 0 - otherwise a successful OTA
        # is reported as failed and the reboot is skipped.
        trap = _trap_line()
        script = (
            'set -e\n'
            'INACTIVE_MNT="$(mktemp -d)"\n'
            'rmdir "$INACTIVE_MNT"\n'      # mount already released
            f'{trap}\n'
            'true\n'
        )
        # /bin/sh on the device is busybox ash; test the portable shells
        # available in CI too. All must agree the exit is clean.
        for shell in ('sh', 'bash', 'dash'):
            if subprocess.run(['sh', '-c', f'command -v {shell}'],
                              capture_output=True).returncode != 0:
                continue
            rc = subprocess.run([shell, '-c', script]).returncode
            self.assertEqual(
                rc, 0,
                f'{shell}: reefy-efi EXIT trap poisons the exit status '
                f'under set -e (rc={rc}); a successful OTA would be '
                f'reported as a failure and the reboot skipped')

    def test_trap_is_errexit_guarded_in_source(self):
        # Belt-and-suspenders: keep the `|| true` guard so a future edit
        # cannot silently reintroduce the regression.
        trap = _trap_line()
        self.assertIn(
            '|| true', trap,
            f'EXIT trap dropped its errexit guard: {trap!r}')


class SetNextCommandTests(unittest.TestCase):
    def test_set_next_command_is_exposed(self):
        src = _source()
        self.assertIn('reefy-efi set-next <a|b>', src)
        self.assertIn('set-next) shift; cmd_set_next "$@" ;;', src)
        self.assertIn('fix|update|confirm|set-next|status', src)

    def test_set_next_accepts_only_a_or_b(self):
        src = _source()
        self.assertIn('a) LABEL="reefy-a" ;;', src)
        self.assertIn('b) LABEL="reefy-b" ;;', src)
        self.assertIn('Usage: reefy-efi set-next <a|b>', src)

    def test_set_next_verifies_bootnext(self):
        src = _source()
        self.assertIn('efibootmgr -n "$BOOTNUM"', src)
        self.assertIn("grep '^BootNext:'", src)
        self.assertIn('BootNext not set', src)
        self.assertIn('Next boot set to ${LABEL}', src)


if __name__ == '__main__':
    unittest.main()
