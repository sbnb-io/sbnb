import os
import shutil
import stat
import subprocess
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(__file__), '..', 'rootfs-overlay',
                      'usr', 'bin', 'reefy-ssh-hostkeys')
UNIT = os.path.join(os.path.dirname(__file__), '..', 'rootfs-overlay',
                    'usr', 'lib', 'systemd', 'system',
                    'reefy-ssh-hostkeys.service')


class SshHostKeysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, 'state-ssh')
        self.etc = os.path.join(self.tmp, 'etc-ssh')
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.state)
        os.makedirs(self.etc)
        os.makedirs(self.bin)
        self.keygen = os.path.join(self.bin, 'ssh-keygen')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _run(self):
        env = os.environ.copy()
        env.update({
            'REEFY_SSH_HOSTKEY_STATE_DIR': self.state,
            'REEFY_SSH_HOSTKEY_ETC_DIR': self.etc,
            'REEFY_SSH_KEYGEN': self.keygen,
        })
        return subprocess.run(['sh', SCRIPT], env=env, text=True,
                              capture_output=True)

    def _write_fake_keygen(self):
        with open(self.keygen, 'w') as f:
            f.write("""#!/bin/sh
set -eu
type=
out=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -t) shift; type="$1" ;;
    -f) shift; out="$1" ;;
  esac
  shift
done
printf 'generated-%s-private\\n' "$type" > "$out"
printf 'generated-%s-public\\n' "$type" > "$out.pub"
""")
        os.chmod(self.keygen, 0o755)

    def test_generates_and_saves_missing_host_keys(self):
        self._write_fake_keygen()

        r = self._run()

        self.assertEqual(r.returncode, 0, r.stderr)
        for t in ('rsa', 'ecdsa', 'ed25519'):
            for suffix in ('', '.pub'):
                name = f'ssh_host_{t}_key{suffix}'
                self.assertTrue(os.path.exists(os.path.join(self.etc, name)))
                self.assertTrue(os.path.exists(os.path.join(self.state, name)))
        private_mode = stat.S_IMODE(
            os.stat(os.path.join(self.state, 'ssh_host_ed25519_key')).st_mode)
        public_mode = stat.S_IMODE(
            os.stat(os.path.join(self.state, 'ssh_host_ed25519_key.pub')).st_mode)
        self.assertEqual(private_mode, 0o600)
        self.assertEqual(public_mode, 0o644)

    def test_restores_saved_keys_without_regenerating(self):
        with open(self.keygen, 'w') as f:
            f.write("#!/bin/sh\nexit 42\n")
        os.chmod(self.keygen, 0o755)
        for t in ('rsa', 'ecdsa', 'ed25519'):
            with open(os.path.join(self.state, f'ssh_host_{t}_key'), 'w') as f:
                f.write(f'saved-{t}-private\n')
            with open(os.path.join(self.state, f'ssh_host_{t}_key.pub'), 'w') as f:
                f.write(f'saved-{t}-public\n')
        with open(os.path.join(self.etc, 'ssh_host_ed25519_key'), 'w') as f:
            f.write('stale-private\n')

        r = self._run()

        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.etc, 'ssh_host_ed25519_key')) as f:
            self.assertEqual(f.read(), 'saved-ed25519-private\n')

    def test_unit_runs_after_storage_and_before_sshd(self):
        with open(UNIT) as f:
            unit = f.read()
        self.assertIn('After=reefy-storage.service', unit)
        self.assertIn('Before=sshd.service', unit)
        self.assertIn('RequiresMountsFor=/mnt/reefy-data', unit)

    def test_unit_is_enabled_in_multi_user_target(self):
        link = os.path.join(
            os.path.dirname(__file__), '..', 'rootfs-overlay', 'etc',
            'systemd', 'system', 'multi-user.target.wants',
            'reefy-ssh-hostkeys.service')
        self.assertTrue(os.path.islink(link))
        self.assertEqual(
            os.readlink(link),
            '/usr/lib/systemd/system/reefy-ssh-hostkeys.service')
