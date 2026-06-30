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
import tempfile
import textwrap
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

    def test_pipefail_clear_is_errexit_safe(self):
        src = _source()
        self.assertIn('set +o pipefail 2>/dev/null || true', src)


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


class ConfirmCommandFreshEntryTests(unittest.TestCase):
    def test_confirm_uses_fresh_entry_commit(self):
        src = _source()
        self.assertIn('commit_current_slot_with_fresh_entries', src)
        self.assertIn('cmd_confirm() {', src)
        self.assertNotIn('efibootmgr -o "${CURRENT},${DEFAULT}"', src)

    def test_fresh_commit_matches_entries_by_label_and_partuuid(self):
        src = _source()
        self.assertIn('boot_nums_for_label_guid()', src)
        self.assertIn('grep -i -- "$GUID"', src)
        self.assertIn('GUID_A=$(blkid -s PARTUUID', src)
        self.assertIn('GUID_B=$(blkid -s PARTUUID', src)

    def test_fresh_commit_creates_before_deleting_old_entries(self):
        src = _source()
        create_a = src.index(
            'efibootmgr -c -d "$DISK" -p 1 -L "reefy-a"')
        create_b = src.index(
            'efibootmgr -c -d "$DISK" -p 2 -L "reefy-b"')
        set_order = src.index('efibootmgr -o "${NEW_ACTIVE},${NEW_INACTIVE}"')
        delete_a = src.index('efibootmgr -b "$OLD_A" -B')
        delete_b = src.index('efibootmgr -b "$OLD_B" -B')
        self.assertLess(create_a, set_order)
        self.assertLess(create_b, set_order)
        self.assertLess(set_order, delete_a)
        self.assertLess(set_order, delete_b)

    def test_fresh_commit_identifies_new_entries_by_set_difference(self):
        src = _source()
        self.assertIn('new_boot_num_from_sets()', src)
        self.assertIn('NEW_A=$(new_boot_num_from_sets "$OLD_A_SET" "$AFTER_A_SET")', src)
        self.assertIn('NEW_B=$(new_boot_num_from_sets "$OLD_B_SET" "$AFTER_B_SET")', src)
        self.assertIn('Final reefy-a entry is Boot${FINAL_A_SET}', src)
        self.assertIn('Final reefy-b entry is Boot${FINAL_B_SET}', src)


class ConfirmCommandFunctionalTests(unittest.TestCase):
    def _run_confirm_with_fake_efi(self, active_label):
        old_a = '0000'
        old_b = '0001'
        current = old_a if active_label == 'reefy-a' else old_b
        default = old_b if active_label == 'reefy-a' else old_a

        with tempfile.TemporaryDirectory() as td:
            bin_dir = os.path.join(td, 'bin')
            state_dir = os.path.join(td, 'state')
            mnt = os.path.join(td, 'mnt', 'reefy')
            os.makedirs(bin_dir)
            os.makedirs(state_dir)
            os.makedirs(mnt)

            state_path = os.path.join(state_dir, 'entries')
            current_path = os.path.join(state_dir, 'current')
            order_path = os.path.join(state_dir, 'order')
            next_path = os.path.join(state_dir, 'next')
            calls_path = os.path.join(state_dir, 'calls')

            with open(current_path, 'w') as f:
                f.write(current)
            with open(order_path, 'w') as f:
                f.write(f'{default},{current}')
            with open(next_path, 'w') as f:
                f.write('0002')
            with open(state_path, 'w') as f:
                f.write('\n'.join([
                    '0000|reefy-a|GUID-A',
                    '0001|reefy-b|GUID-B',
                ]) + '\n')

            fake_efibootmgr = r'''#!/bin/sh
set -eu
STATE_DIR="${REEFY_EFI_TEST_STATE}"
ENTRIES="${STATE_DIR}/entries"
CURRENT_FILE="${STATE_DIR}/current"
ORDER_FILE="${STATE_DIR}/order"
NEXT_FILE="${STATE_DIR}/next"
CALLS_FILE="${STATE_DIR}/calls"

log_call() {
    printf '%s\n' "$*" >> "$CALLS_FILE"
}

print_entries() {
    while IFS='|' read -r num label guid; do
        [ -n "$num" ] || continue
        printf 'Boot%s* %s\tHD(1,GPT,%s,0x800,0x200000)/\\EFI\\Boot\\bootx64.efi\n' "$num" "$label" "$guid"
    done < "$ENTRIES"
}

print_normal() {
    printf 'BootCurrent: %s\n' "$(cat "$CURRENT_FILE")"
    if [ -s "$NEXT_FILE" ]; then
        printf 'BootNext: %s\n' "$(cat "$NEXT_FILE")"
    fi
    printf 'Timeout: 1 seconds\n'
    printf 'BootOrder: %s\n' "$(cat "$ORDER_FILE")"
    print_entries
}

next_num() {
    for n in $(seq 0 9999); do
        num=$(printf '%04X' "$n")
        if ! grep -q "^${num}|" "$ENTRIES"; then
            printf '%s\n' "$num"
            return 0
        fi
    done
    return 1
}

if [ "$#" -eq 0 ]; then
    print_normal
    exit 0
fi

case "$1" in
    -v)
        print_normal
        exit 0
        ;;
    -c)
        part=
        label=
        while [ "$#" -gt 0 ]; do
            case "$1" in
                -p) shift; part="$1" ;;
                -L) shift; label="$1" ;;
            esac
            shift || true
        done
        num=$(next_num)
        if [ "$part" = "1" ]; then guid="GUID-A"; else guid="GUID-B"; fi
        printf '%s|%s|%s\n' "$num" "$label" "$guid" >> "$ENTRIES"
        order=$(cat "$ORDER_FILE")
        printf '%s,%s\n' "$num" "$order" > "$ORDER_FILE"
        log_call "create:${num}:${label}:${part}"
        print_normal
        exit 0
        ;;
    -o)
        shift
        printf '%s\n' "$1" > "$ORDER_FILE"
        log_call "order:$1"
        print_normal
        exit 0
        ;;
    -b)
        shift
        num="$1"
        shift
        if [ "${1:-}" = "-B" ]; then
            tmp="${ENTRIES}.tmp"
            grep -v "^${num}|" "$ENTRIES" > "$tmp" || true
            mv "$tmp" "$ENTRIES"
            log_call "delete:${num}"
            print_normal
            exit 0
        fi
        ;;
esac

echo "unsupported efibootmgr args: $*" >&2
exit 2
'''

            helper_scripts = {
                'efibootmgr': fake_efibootmgr,
                'findmnt': '#!/bin/sh\nprintf "%s\\n" "$REEFY_EFI_TEST_MOUNTED_DEV"\n',
                'lsblk': '#!/bin/sh\nprintf "sda\\n"\n',
                'blkid': textwrap.dedent('''\
                    #!/bin/sh
                    case "$*" in
                        *sda1*) printf 'GUID-A\\n' ;;
                        *sda2*) printf 'GUID-B\\n' ;;
                        *) exit 1 ;;
                    esac
                    '''),
                'mount': '#!/bin/sh\nexit 0\n',
                'sync': '#!/bin/sh\nexit 0\n',
            }
            for name, script in helper_scripts.items():
                path = os.path.join(bin_dir, name)
                with open(path, 'w') as f:
                    f.write(script)
                os.chmod(path, 0o755)

            env = os.environ.copy()
            env['PATH'] = f'{bin_dir}:{env["PATH"]}'
            env['REEFY_EFI_TEST_STATE'] = state_dir
            env['REEFY_EFI_TEST_ROOT'] = td
            env['REEFY_EFI_TEST_DISK'] = os.path.join(td, 'dev', 'sda')
            env['REEFY_EFI_TEST_MOUNTED_DEV'] = (
                env['REEFY_EFI_TEST_DISK'] + '1'
                if active_label == 'reefy-a'
                else env['REEFY_EFI_TEST_DISK'] + '2')

            os.makedirs(os.path.dirname(env['REEFY_EFI_TEST_DISK']))

            proc = subprocess.run(
                ['sh', REEFY_EFI, 'confirm'],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                proc.returncode, 0,
                f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}')

            with open(order_path) as f:
                order = f.read().strip()
            with open(state_path) as f:
                entries = [line.strip().split('|') for line in f if line.strip()]
            with open(calls_path) as f:
                calls = [line.strip() for line in f if line.strip()]

            return order, entries, calls, proc.stdout

    def test_confirm_from_a_creates_fresh_entries_and_keeps_a_first(self):
        order, entries, calls, stdout = self._run_confirm_with_fake_efi('reefy-a')

        self.assertEqual(order, '0002,0003')
        self.assertEqual(entries, [
            ['0002', 'reefy-a', 'GUID-A'],
            ['0003', 'reefy-b', 'GUID-B'],
        ])
        self.assertEqual(calls, [
            'create:0002:reefy-a:1',
            'create:0003:reefy-b:2',
            'order:0002,0003',
            'delete:0000',
            'delete:0001',
        ])
        self.assertIn('Committed reefy-a as default with fresh entries', stdout)

    def test_confirm_from_b_creates_fresh_entries_and_keeps_b_first(self):
        order, entries, calls, stdout = self._run_confirm_with_fake_efi('reefy-b')

        self.assertEqual(order, '0003,0002')
        self.assertEqual(entries, [
            ['0002', 'reefy-a', 'GUID-A'],
            ['0003', 'reefy-b', 'GUID-B'],
        ])
        self.assertEqual(calls, [
            'create:0002:reefy-a:1',
            'create:0003:reefy-b:2',
            'order:0003,0002',
            'delete:0000',
            'delete:0001',
        ])
        self.assertIn('Committed reefy-b as default with fresh entries', stdout)


if __name__ == '__main__':
    unittest.main()
