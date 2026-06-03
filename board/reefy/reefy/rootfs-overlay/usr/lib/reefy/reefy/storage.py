"""reefy.storage - all on-disk state setup for the device: LUKS, LVM
(thin pool + per-app thin volumes), filesystems (XFS/ext4), the thick
state LV, mounts and reclaim. Used by every role (control provisioning,
data-plane apply, boot-mount oneshot), so it is dependency-free of paho:
this module must import cleanly without paho-mqtt installed.
"""

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid as uuid_mod
from io import BytesIO

from reefy import shared
from reefy.shared import _part_dev, log


class Storage:
    # Re-bound from reefy.shared (single source) so method bodies keep
    # using self.<CONST> unchanged.
    STORAGE_VG = shared.STORAGE_VG
    STORAGE_LV = shared.STORAGE_LV
    STORAGE_POOL = shared.STORAGE_POOL
    STATE_LV = shared.STATE_LV
    LEGACY_STORAGE_LV = shared.LEGACY_STORAGE_LV
    REEFY_DATA_MOUNT_OPTS = shared.REEFY_DATA_MOUNT_OPTS
    DESIRED_STATE_PATH = shared.DESIRED_STATE_PATH

    def __init__(self, volume_caps=None):
        # Per-app fair-share caps (host_path -> %% of pool); read by
        # _ensure_volume_lv / _prepare_app_dirs. Callers (data-plane
        # apply, boot_mount) set this before invoking volume ops.
        self._volume_caps = volume_caps if volume_caps is not None else {}

    def set_volume_caps(self, caps):
        self._volume_caps = caps or {}

    def _write_fresh_keyfile(self, key_part, _log=None):
        """Write 44 high-entropy bytes (base64 of urandom(32)) to
        key_part. Called immediately before luksFormat: the new LUKS
        volume will be keyed with these bytes, so any prior keyfile
        contents (real, stale, or degraded from a partial wipe)
        become meaningless and are safely overwritten.
        """
        if _log:
            _log('Writing fresh LUKS keyfile...')
        subprocess.run(
            ['dd', 'if=/dev/urandom', f'of={key_part}', 'bs=1M', 'count=1'],
            capture_output=True, timeout=10)
        passphrase = subprocess.run(
            ['openssl', 'rand', '-base64', '32'],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        subprocess.run(
            ['sh', '-c', f'echo -n "{passphrase}" | dd of={key_part} conv=notrunc'],
            capture_output=True, timeout=10)

    def _provision_luks_stack(self, targets, key_part, _log=None):
        """Provision fresh LUKS volumes on `targets`, all keyed by a
        single freshly-written keyfile. Returns the /dev/mapper/<name>
        paths that opened successfully - the PV list for the next LVM
        stack step.

        `targets` is a list of (device_path, mapper_name) tuples; the
        USB-p4 fallback passes `[('/dev/sda4', 'reefy-data')]`, the
        internal-drive path passes one tuple per device in
        storage_config.devices with mapper names `reefy-<dev>`.

        For each target: tear down any prior dm/LUKS layers
        (`_force_wipe_device`), luksFormat with a 600s budget that
        captures stdout/stderr/dmesg on timeout (bare TimeoutExpired
        drops the captured output, leaving only the command line and
        budget - unactionable upstream), then luksOpen.

        Fresh keyfile is written ONCE up front. All targets in this
        call share the same key, which matches the existing single-
        key-unlocks-all-reefy-volumes invariant the boot scripts
        depend on.
        """
        self._write_fresh_keyfile(key_part, _log)

        luks_pvs = []
        for target, mapper_name in targets:
            if _log:
                _log(f'LUKS formatting {target}...')
            self._force_wipe_device(target)

            try:
                result = subprocess.run(
                    ['cryptsetup', 'luksFormat', target,
                     '--key-file', key_part, '--keyfile-size', '44',
                     '--batch-mode'],
                    capture_output=True, timeout=600, check=False)
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or b'').decode(errors='replace').strip()
                err = (e.stderr or b'').decode(errors='replace').strip()
                try:
                    dmesg = subprocess.run(
                        ['dmesg', '--ctime'], capture_output=True,
                        text=True, timeout=5
                    ).stdout.strip().splitlines()[-20:]
                    dmesg_tail = '\n'.join(dmesg)
                except Exception:
                    dmesg_tail = '(dmesg unavailable)'
                raise RuntimeError(
                    f'cryptsetup luksFormat on {target} timed out after '
                    f'{e.timeout}s.\nstdout: {out or "(empty)"}\n'
                    f'stderr: {err or "(empty)"}\n'
                    f'dmesg tail:\n{dmesg_tail}') from e

            if result.returncode != 0:
                err = result.stderr.decode(errors='replace').strip()
                if _log:
                    _log(f'LUKS format failed on {target}: {err}')
                continue

            # --allow-discards lets dm-crypt pass TRIM down to the
            # drive; --persistent stores the flag in the LUKS header
            # so subsequent opens (boot-reefy-storage.sh) inherit it.
            # Required for dm-thin discard_passdown and FTL recovery.
            result = subprocess.run(
                ['cryptsetup', 'luksOpen', target, mapper_name,
                 '--allow-discards', '--persistent',
                 '--key-file', key_part, '--keyfile-size', '44'],
                capture_output=True, timeout=120)
            if result.returncode != 0:
                continue

            luks_pvs.append(f'/dev/mapper/{mapper_name}')

        return luks_pvs

    def _ensure_persistent_storage(self, storage_config=None, _log=None):
        """Set up persistent storage during adoption. Replaces tmpfs with real storage.

        If storage_config has internal drives → LUKS/LVM → mount as /mnt/reefy-data.
        Otherwise → create USB p3 (key) + p4 (data) → mount as /mnt/reefy-data.
        """
        # Check if already on persistent storage
        mount_type = subprocess.run(
            ['findmnt', '-n', '-o', 'FSTYPE', '/mnt/reefy-data'],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if mount_type and mount_type != 'tmpfs':
            return  # already persistent (real filesystem mounted)

        disk, _ = self._find_reefy_disk()
        if not disk:
            print("[mqtt] ERROR: Cannot find reefy USB disk")
            return

        key_part = _part_dev(disk, 3)

        # Ensure the keyfile partition slot exists so both the restore
        # scan below (reads existing key bytes, if any) and the
        # provision paths (overwrite with fresh bytes via
        # _provision_luks_stack -> _write_fresh_keyfile, immediately
        # before luksFormat) have a device node to operate on. Do NOT
        # write key bytes here: that decision belongs to the provision
        # paths so a fresh LUKS header is always keyed with fresh
        # bytes, regardless of whether the partition was just created
        # or had degraded contents from a partial wipe.
        if not os.path.exists(key_part):
            if _log:
                _log('Creating key partition...')
            subprocess.run(
                ['sh', '-c', f'printf "Fix\\nFix\\n" | parted ---pretend-input-tty {disk} print'],
                capture_output=True, timeout=15)
            subprocess.run(
                ['parted', '-s', disk, 'mkpart', 'primary', '2049MiB', '2050MiB'],
                capture_output=True, timeout=15)
            subprocess.run(
                ['parted', '-s', disk, 'set', '3', 'msftres', 'on'],
                capture_output=True, timeout=15)
            subprocess.run(['partprobe', disk], capture_output=True, timeout=10)
            time.sleep(1)

            if not os.path.exists(key_part):
                print("[mqtt] ERROR: Key partition not created")
                return

        # Try to open existing internal drives with our key (restore scenario)
        parent = subprocess.run(
            ['lsblk', '-no', 'PKNAME', subprocess.run(
                ['sh', '-c', 'blkid -L reefy-a 2>/dev/null || blkid -L reefy-b 2>/dev/null'],
                capture_output=True, text=True, timeout=5).stdout.strip()],
            capture_output=True, text=True, timeout=5).stdout.strip()

        subprocess.run(['modprobe', 'dm_crypt'], capture_output=True, timeout=5)
        subprocess.run(['modprobe', 'dm_mod'], capture_output=True, timeout=5)

        for dev in subprocess.run(
            ['lsblk', '-dpno', 'NAME'], capture_output=True, text=True, timeout=5
        ).stdout.strip().split('\n'):
            dev = dev.strip()
            if not dev or (parent and dev == f'/dev/{parent}'):
                continue
            result = subprocess.run(['cryptsetup', 'isLuks', dev], capture_output=True, timeout=5)
            if result.returncode != 0:
                continue
            luks_name = f'reefy-{os.path.basename(dev)}'
            if os.path.exists(f'/dev/mapper/{luks_name}'):
                continue
            result = subprocess.run(
                ['cryptsetup', 'luksOpen', dev, luks_name,
                 '--key-file', key_part, '--keyfile-size', '44'],
                capture_output=True, timeout=120)
            if result.returncode == 0:
                if _log:
                    _log(f'Opened existing LUKS on {dev}')

        # Check if we can activate LVM from opened drives
        subprocess.run(['vgscan'], capture_output=True, timeout=10)
        if subprocess.run(['vgs', 'reefy'], capture_output=True, timeout=5).returncode == 0:
            subprocess.run(['vgchange', '-ay', 'reefy'], capture_output=True, timeout=10)
            # Prefer the new thin LV (reefy_default) over the legacy
            # flat `data` LV from pre-thin-pool installs.
            lv_path = self._active_reefy_lv_path()
            if lv_path:
                if _log:
                    _log('Mounting existing internal storage...')
                if self._finalize_data_mount(lv_path, _log, 'existing internal'):
                    return

        # Try setting up new internal drives if specified in storage config
        if storage_config and storage_config.get('devices'):
            if _log:
                _log('Setting up new internal drives...')
            self._setup_internal_persistent(storage_config, key_part, _log)
            mount_type = subprocess.run(
                ['findmnt', '-n', '-o', 'FSTYPE', '/mnt/reefy-data'],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if mount_type and mount_type != 'tmpfs':
                return

        # Fall back to USB p4 — wrap in LUKS + LVM (same stack as
        # internal drives; unifies single-disk and multi-disk paths
        # so there's one code path to maintain and USB users get
        # snapshot-backed backups too).
        if _log:
            _log('Creating USB data partition...')
        data_part = _part_dev(disk, 4)
        if not os.path.exists(data_part):
            subprocess.run(
                ['parted', '-s', disk, 'mkpart', 'reefy-data', '2050MiB', '100%'],
                capture_output=True, timeout=15)
            subprocess.run(['partprobe', disk], capture_output=True, timeout=10)
            time.sleep(1)

        if not os.path.exists(data_part):
            print("[mqtt] ERROR: USB data partition not created")
            return

        # Fresh LUKS on the USB data partition via shared provision
        # helper (handles wipe + luksFormat + luksOpen + timeout
        # capture + fresh-key write). Mapper name `reefy-data` matches
        # what boot-reefy-storage.sh's setup_data_partition expects on
        # subsequent boots.
        luks_pvs = self._provision_luks_stack(
            [(data_part, 'reefy-data')], key_part, _log)
        if not luks_pvs:
            print("[mqtt] ERROR: USB data partition LUKS provision failed")
            return

        # Build VG + thin pool + reefy_default LV on top of the LUKS-
        # opened mapper. Helper handles the fresh-install path here.
        self._ensure_lvm_stack(luks_pvs, _log)
        lv_path = self._active_reefy_lv_path()
        if not lv_path:
            print("[mqtt] ERROR: No reefy LV after USB LVM setup")
            return

        # Migrate state from tmpfs, mount persistent (+ state LV + dirs).
        self._finalize_data_mount(lv_path, _log, 'USB, LVM thin')
        print("[mqtt] Persistent storage created on USB (LVM thin)")

    def _setup_internal_persistent(self, storage_config, key_part, _log=None):
        """Set up internal drives as /mnt/reefy-data during adoption."""
        devices = storage_config.get('devices', [])
        if not devices:
            return

        subprocess.run(['modprobe', 'dm_crypt'], capture_output=True, timeout=5)
        subprocess.run(['modprobe', 'dm_mod'], capture_output=True, timeout=5)

        targets = []
        for dev_name in devices:
            target = f'/dev/{dev_name}'
            if not os.path.exists(target):
                if _log:
                    _log(f'Device {target} not found, skipping')
                continue
            # Mapper names `reefy-<dev_name>` match what
            # boot-reefy-storage.sh's setup_internal_storage expects.
            targets.append((target, f'reefy-{dev_name}'))

        luks_pvs = self._provision_luks_stack(targets, key_part, _log)
        if not luks_pvs:
            return

        # Build VG + thin pool + reefy_default LV (helper handles
        # existing-VG extend / legacy-LV mount paths too).
        self._ensure_lvm_stack(luks_pvs, _log)
        lv_path = self._active_reefy_lv_path()
        if not lv_path:
            if _log:
                _log('No reefy_default or legacy LV after LVM setup')
            return

        # Migrate state from tmpfs, mount persistent (+ state LV + dirs).
        dev_list = ', '.join(devices)
        self._finalize_data_mount(lv_path, _log, f'internal: {dev_list}')
        print(f"[mqtt] Persistent storage created on internal drives: {dev_list}")

    def _find_usb_disk(self):
        """Find the reefy/sbnb USB dongle block device (e.g. /dev/sda)."""
        disk, _ = self._find_reefy_disk()
        return disk

    def _find_data_dir(self):
        """Find the persistent data mount (reefy-data or sbnb-data)."""
        for d in ('/mnt/reefy-data', '/mnt/sbnb-data'):
            if os.path.ismount(d):
                return d
        return None

    def _ensure_lvm_stack(self, luks_pvs, _log=None):
        """Bring up the LVM stack on top of the given LUKS-mapped PVs.

        Guarantees after return:
          - VG `reefy` exists (creates from scratch if missing, extends
            with any new PVs if present).
          - Thin pool `reefy_pool` exists, occupying essentially all
            VG free space (100%FREE).
          - Thin LV `reefy_default` exists (xfs on fresh create; legacy
            devices keep ext4, mountable at
            /mnt/reefy-data).

        Legacy compat: if the VG already has the old flat `data` LV
        (from a pre-thin-pool install) and no `reefy_default`, does
        nothing new — caller is expected to mount `reefy/data`. Those
        devices have backups disabled until they factory-reset into
        the new layout; see PLAN-backup.md §"Migration".

        Returns:
          'new'     — created the pool + default LV from scratch (fresh install)
          'existing' — pool + default LV already in place (restore/upgrade)
          'legacy'   — only the legacy flat LV is present (caller mounts it)
        """
        for pv in luks_pvs:
            subprocess.run(['pvcreate', '-ff', '-y', pv],
                           capture_output=True, text=True, timeout=15)

        vg_exists = subprocess.run(
            ['vgs', self.STORAGE_VG], capture_output=True
        ).returncode == 0
        if not vg_exists:
            result = subprocess.run(
                ['vgcreate', self.STORAGE_VG] + luks_pvs,
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(f'VG create failed: {result.stderr}')
            if _log:
                _log(f'Created VG {self.STORAGE_VG}')
        else:
            # Extend VG with any not-yet-member PVs.
            for pv in luks_pvs:
                subprocess.run(['vgextend', self.STORAGE_VG, pv],
                               capture_output=True, text=True, timeout=15)
            subprocess.run(['vgchange', '-ay', self.STORAGE_VG],
                           capture_output=True, timeout=15)

        default_lv_path = f'/dev/{self.STORAGE_VG}/{self.STORAGE_LV}'
        legacy_lv_path = f'/dev/{self.STORAGE_VG}/{self.LEGACY_STORAGE_LV}'

        if os.path.exists(default_lv_path):
            if _log:
                _log(f'Using existing {self.STORAGE_LV} thin LV')
            return 'existing'

        if os.path.exists(legacy_lv_path):
            if _log:
                _log(f'Using legacy {self.LEGACY_STORAGE_LV} LV (no thin pool)')
            return 'legacy'

        # Thick state LV BEFORE the pool: the pool grabs 100%FREE, so
        # the guaranteed-space state LV must be carved out first.
        self._ensure_state_lv(_log)

        # Fresh VG or upgrade from empty VG — create thin pool + default LV.
        pool_path = f'{self.STORAGE_VG}/{self.STORAGE_POOL}'
        pool_exists = subprocess.run(
            ['lvs', pool_path], capture_output=True).returncode == 0
        if not pool_exists:
            # chunksize 512K + -Zn (no zero-on-allocate). A dm-thin
            # chunk returns to the pool only when the whole chunk is
            # discarded, so chunk size IS the reclaim granularity.
            # Under fragmented small-file deletes (NVR clips), 512K
            # reclaims ~10x more freed space than 4M (73% vs 7% of
            # deleted bytes, XFS, measured). Throughput, IOPS and CPU
            # are flat across chunk sizes on this LUKS+thin+NVMe stack:
            # LUKS encryption dominates CPU and the device dominates
            # bandwidth, so the large-chunk "fewer metadata ops"
            # advantage never surfaces. See
            # docs/storage-chunk-size-study.md. -Zn is safe under LUKS:
            # the entire pool is encrypted, so deallocated chunks are
            # encrypted noise without the passphrase. Affects
            # newly-provisioned devices only; existing pools keep their
            # chunk size (set at pool creation, immutable in place).
            result = subprocess.run(
                ['lvcreate', '--type', 'thin-pool', '-l', '100%FREE',
                 '--chunksize', '512K', '-Zn',
                 '-n', self.STORAGE_POOL, self.STORAGE_VG],
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError(f'thin-pool create failed: {result.stderr}')
            if _log:
                _log(f'Created thin pool {self.STORAGE_POOL}')

        # virtualsize = pool size; thin overcommit semantics mean the LV
        # only uses physical blocks it actually writes to.
        pool_size = subprocess.run(
            ['lvs', '--noheadings', '--nosuffix', '--units', 'b',
             '-o', 'lv_size', pool_path],
            capture_output=True, text=True, timeout=10).stdout.strip()
        result = subprocess.run(
            ['lvcreate', '--thin', '--virtualsize', f'{pool_size}B',
             '-n', self.STORAGE_LV, pool_path],
            capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f'default LV create failed: {result.stderr}')
        # XFS for the main data LV too (not just per-app volumes): it
        # holds docker's overlay2 (many small layer files) and any
        # uncapped media - the exact dynamic-inode case XFS was chosen
        # for (reefy_default + media was the ~58 GiB inode-tax victim in
        # the coral incident). Existing ext4 devices are never reformatted
        # (we only mkfs on create); the mount path is fs-type-aware so
        # both XFS (new) and ext4 (legacy) mount correctly.
        result = subprocess.run(
            ['mkfs.xfs', '-q', '-L', 'reefy-data', default_lv_path],
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f'mkfs.xfs failed: {result.stderr}')
        if _log:
            _log(f'Created thin LV {self.STORAGE_LV} (xfs)')
        return 'new'

    def _ensure_state_lv(self, _log=None):
        """Create the thick state LV (reefy_state), sized
        min(4 GiB, 10% of VG), floored at 256 MiB. Real blocks outside
        the thin pool so /mnt/reefy-data/state is never starved by a full
        app pool. Idempotent; must run BEFORE the pool's 100%FREE.
        State's footprint is tiny+bounded, so a fixed-but-generous size
        is correct (unlike unbounded app data); growable later via
        lvextend + xfs_growfs if a disk is added."""
        lv_path = f'/dev/{self.STORAGE_VG}/{self.STATE_LV}'
        if os.path.exists(lv_path):
            return
        vg_raw = subprocess.run(
            ['vgs', '--noheadings', '--nosuffix', '--units', 'b',
             '-o', 'vg_size', self.STORAGE_VG],
            capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            vg_bytes = int(float(vg_raw))
        except (ValueError, TypeError):
            vg_bytes = 0
        gib = 1024 ** 3
        size = min(4 * gib, vg_bytes // 10) if vg_bytes else gib
        size = max(size, 256 * 1024 * 1024)  # floor for tiny media
        size_mib = size // (1024 * 1024)
        r = subprocess.run(
            ['lvcreate', '-L', f'{size_mib}M', '-n', self.STATE_LV,
             self.STORAGE_VG],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            if _log:
                _log(f'state LV create failed: {r.stderr}')
            return
        r = subprocess.run(
            ['mkfs.xfs', '-q', lv_path],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            if _log:
                _log(f'state LV mkfs.xfs failed: {r.stderr}')
            return
        if _log:
            _log(f'Created thick state LV {self.STATE_LV} ({size_mib} MiB)')

    def _mount_state_lv(self, _log=None):
        """Mount the thick state LV at /mnt/reefy-data/state (Python port
        of boot-reefy-storage.sh's mount_state_lv). The redesign provisions
        in-place with no reboot, so the boot script won't mount the freshly
        created state LV until the next boot - mount it here so control
        state lands on the thick LV immediately. Seeds the empty LV from
        any state already on reefy_default; fs-aware opts; no-op if absent
        (legacy device) or already mounted (idempotent)."""
        slv = f'/dev/{self.STORAGE_VG}/{self.STATE_LV}'
        if not os.path.exists(slv):
            return
        sdir = os.path.join(shared.REEFY_DATA_MNT, 'state')
        if subprocess.run(['mountpoint', '-q', sdir],
                          capture_output=True).returncode == 0:
            return
        os.makedirs(sdir, exist_ok=True)
        opts = self._fs_mount_opts(slv)
        # Seed: if the LV is empty but reefy_default's state dir already
        # has content, copy it into the LV before mounting over it.
        tmp = tempfile.mkdtemp()
        try:
            if subprocess.run(['mount', '-o', opts, slv, tmp],
                              capture_output=True).returncode == 0:
                try:
                    if (not os.listdir(tmp) and os.path.isdir(sdir)
                            and os.listdir(sdir)):
                        subprocess.run(['cp', '-a', f'{sdir}/.', f'{tmp}/'],
                                       capture_output=True, timeout=60)
                finally:
                    subprocess.run(['umount', tmp], capture_output=True, timeout=10)
        finally:
            try:
                os.rmdir(tmp)
            except OSError:
                pass
        r = subprocess.run(['mount', '-o', opts, slv, sdir],
                           capture_output=True, text=True, timeout=10)
        if _log:
            _log(f'Mounted {self.STATE_LV} at {sdir}' if r.returncode == 0
                 else f'state LV mount failed: {r.stderr}')

    def _finalize_data_mount(self, lv_path, _log=None, label=''):
        """Common provision tail once the reefy_default LV exists: preserve
        the current (tmpfs/bootstrap) state, mount reefy_default (fs-aware),
        restore the state, mount the thick state LV, and create the standard
        data dirs. Shared by every provision path so the on-disk layout is
        identical and the state LV is mounted at provision (in-place
        provision never reboots). Returns True if reefy_default mounted."""
        subprocess.run(['cp', '-a', '/mnt/reefy-data/state', '/tmp/reefy-state'],
                       capture_output=True, timeout=10)
        subprocess.run(['umount', '/mnt/reefy-data'], capture_output=True, timeout=5)
        r = subprocess.run(
            ['mount', '-o', self._fs_mount_opts(lv_path), lv_path,
             '/mnt/reefy-data'], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            if _log:
                _log(f'reefy_default mount failed: {r.stderr}')
            return False
        subprocess.run(['cp', '-a', '/tmp/reefy-state', '/mnt/reefy-data/state'],
                       capture_output=True, timeout=10)
        subprocess.run(['rm', '-rf', '/tmp/reefy-state'], capture_output=True, timeout=5)
        self._mount_state_lv(_log)
        os.makedirs('/mnt/reefy-data/state/lan', exist_ok=True)
        os.makedirs('/mnt/reefy-data/apps', exist_ok=True)
        os.makedirs('/mnt/reefy-data/docker', exist_ok=True)
        if _log and label:
            _log(f'Persistent storage ready ({label})')
        return True

    def _active_reefy_lv_path(self):
        """Return the path to mount as /mnt/reefy-data: reefy_default
        if present, else legacy `data`, else None."""
        for lv in (self.STORAGE_LV, self.LEGACY_STORAGE_LV):
            p = f'/dev/{self.STORAGE_VG}/{lv}'
            if os.path.exists(p):
                return p
        return None

    def _has_thin_pool(self):
        """True if the LVM thin pool is available (i.e. this device is
        on the new storage layout, not legacy flat-LV). Backup snapshot
        path requires this; legacy devices skip backups gracefully."""
        return subprocess.run(
            ['lvs', f'{self.STORAGE_VG}/{self.STORAGE_POOL}'],
            capture_output=True
        ).returncode == 0

    def _volume_lv_name(self, path):
        """Stable per-volume thin LV name. Hash of the absolute path
        keeps it short, dns-safe-ish, and unique per device. Underscore
        separator avoids dm-mapper's `--` escape in /dev/mapper paths."""
        import hashlib
        h = hashlib.sha1(path.encode()).hexdigest()[:12]
        return f'reefy_backup_{h}'

    @contextlib.contextmanager
    def _volume_lock(self):
        """Serialize per-app volume create/mount across processes. Both
        the reefy-app-volumes boot oneshot (reefy-mount-volumes) and the
        running reconciler call _ensure_volume_lv on the same paths;
        without a lock they can race the findmnt-then-mount window and
        double-mount. flock on a /run lockfile (tmpfs, auto-cleared each
        boot)."""
        os.makedirs('/run/reefy', exist_ok=True)
        lock = open('/run/reefy/volume-mount.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _fs_mount_opts(self, dev):
        """Mount options for an LV, by detected filesystem. ext4 wants
        `commit=60`; XFS *rejects* it (mount fails), so XFS must not get
        it. Both keep noatime + discard (the FS half of the TRIM
        passthrough chain). Unknown/blank type -> ext4 opts, since our
        existing per-app LVs are ext4."""
        fstype = subprocess.run(
            ['blkid', '-o', 'value', '-s', 'TYPE', dev],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if fstype == 'xfs':
            return 'noatime,discard'
        return self.REEFY_DATA_MOUNT_OPTS

    def _ensure_volume_lv(self, path):
        """Provision + mount a per-volume thin LV at `path` if not
        already in place. No-op if:
          - the device has no thin pool (legacy storage), OR
          - the path is already a mount point (idempotent), OR
          - the path already exists with files on the default LV
            (legacy install — leave as plain dir to avoid hiding data).

        Concurrency-safe + idempotent: the boot oneshot and the running
        reconciler may both call this; the flock serializes them and the
        mountpoint re-check inside the lock makes a lost race a no-op.
        """
        if not self._has_thin_pool():
            return  # legacy storage; backups are disabled anyway

        with self._volume_lock():
            # Re-check INSIDE the lock: another process may have mounted
            # it while we waited on the flock.
            r = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE', path],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return

            # Existing dir with files on the default LV → legacy install,
            # don't mount over the data. (Migration path: factory-reset.)
            if os.path.isdir(path) and os.listdir(path):
                log('mqtt', f'Legacy data at {path}, skipping per-volume LV mount')
                return

            lv_name = self._volume_lv_name(path)
            lv_path = f'/dev/{self.STORAGE_VG}/{lv_name}'

            if not os.path.exists(lv_path):
                pool_size = subprocess.run(
                    ['lvs', '--noheadings', '--nosuffix', '--units', 'b',
                     '-o', 'lv_size', f'{self.STORAGE_VG}/{self.STORAGE_POOL}'],
                    capture_output=True, text=True, timeout=10).stdout.strip()
                # Fair-share containment: a capped volume's thin LV gets a
                # virtualsize of pct% of the pool. A thin LV can't map more
                # physical blocks than its virtualsize, so this guarantees a
                # (100-pct)% pool margin and ENOSPC lands only on this
                # volume - no quota machinery needed. Uncapped -> full pool.
                vsize = pool_size
                cap_pct = self._volume_caps.get(path)
                if cap_pct:
                    try:
                        vsize = str(int(int(pool_size) * float(cap_pct) / 100))
                    except (ValueError, TypeError):
                        vsize = pool_size
                r = subprocess.run(
                    ['lvcreate', '--thin', '--virtualsize', f'{vsize}B',
                     '-n', lv_name, f'{self.STORAGE_VG}/{self.STORAGE_POOL}'],
                    capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    log('mqtt', f'per-volume LV create failed for {path}: {r.stderr}')
                    return
                # XFS for new volumes: dynamic inode allocation -> no
                # ~58 GiB upfront inode-table tax that ext4 paid on these
                # full-pool-size LVs. (No -L: the LV name exceeds XFS's
                # 12-char label limit; the dm path identifies it anyway.)
                # Existing ext4 LVs are untouched - we only mkfs on create.
                r = subprocess.run(
                    ['mkfs.xfs', '-q', lv_path],
                    capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    log('mqtt', f'mkfs.xfs failed on {lv_path}: {r.stderr}')
                    return
                log('mqtt', f'Created per-volume LV {lv_name} (xfs) for {path}')

            os.makedirs(path, mode=0o755, exist_ok=True)
            # mount can stall on thin-pool metadata ops when the pool's
            # under load (Frigate-class write pressure, restore, big
            # docker pull). 60s gives the kernel enough headroom; the
            # whole state-apply retries on failure anyway.
            r = subprocess.run(
                ['mount', '-o', self._fs_mount_opts(lv_path), lv_path, path],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                log('mqtt', f'mount {lv_path} -> {path} failed: {r.stderr}')
                return
            log('mqtt', f'Mounted {lv_name} at {path}')

    def boot_mount(self):
        """Mount all per-app volumes from persisted desired-state, before
        docker starts. Run by reefy-app-volumes.service (oneshot,
        Before=docker.service) so containers never bind-mount an empty
        pre-mount directory (the boot mount-race that shadowed Frigate's
        config). No MQTT, no network: reads the cached desired-state.json
        and reuses the shared flock'd mount primitive. Idempotent - the
        running reconciler re-applies the same paths for runtime
        add/remove."""
        if not os.path.exists(self.DESIRED_STATE_PATH):
            log('mqtt', '[boot-mount] no desired-state.json; nothing to mount')
            return
        try:
            with open(self.DESIRED_STATE_PATH) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log('mqtt', f'[boot-mount] cannot read desired-state: {e}')
            return
        self._volume_caps = state.get('volume_caps') or {}
        backup = state.get('backup') or {}
        # Mount backup-flagged paths AND fair-share-capped paths (e.g.
        # Frigate media) - both get a per-volume LV that must be mounted
        # before docker.
        paths = list(self._volume_caps.keys())
        for inst in backup.get('instances', []):
            paths.extend(inst.get('paths', []))
        seen = set()
        for p in paths:
            if not p or p in seen:
                continue
            seen.add(p)
            try:
                self._ensure_volume_lv(p)
            except Exception as e:
                log('mqtt', f'[boot-mount] {p} failed: {e}')
        log('mqtt', f'[boot-mount] processed {len(seen)} volume(s) before docker')

    def _reclaim_deleted_instance_lvs(self, old_state, new_state,
                                       new_backup_paths):
        """lvremove per-volume LVs whose owning app instance is gone from
        desired-state, so a deleted app frees its pool space instead of
        leaking an orphaned LV.

        Conservative by design (this deletes data): derive the set of
        'live' instance uuids from EVERY apps/<uuid>/ path anywhere in the
        new state (backup paths + app_volumes + instances, scanned
        recursively, schema-agnostic). Only reclaim a removed backup path
        whose uuid is in NONE of them - i.e. the whole instance is gone.
        A volume that merely un-backup-flags keeps its uuid live elsewhere
        in the state, so it is never reclaimed."""
        if not self._has_thin_pool():
            return
        old_paths = set()
        for inst in (old_state.get('backup') or {}).get('instances', []):
            old_paths.update(inst.get('paths', []))
        removed = old_paths - new_backup_paths
        if not removed:
            return

        def _collect_uuids(obj, acc):
            if isinstance(obj, str):
                m = re.search(r'/apps/([^/]+)/', obj)
                if m:
                    acc.add(m.group(1))
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect_uuids(v, acc)
            elif isinstance(obj, (list, tuple, set)):
                for v in obj:
                    _collect_uuids(v, acc)

        live = set()
        for p in new_backup_paths:
            _collect_uuids(p, live)
        _collect_uuids(new_state.get('app_volumes', []), live)
        _collect_uuids(new_state.get('instances', []), live)

        with self._volume_lock():
            for path in removed:
                m = re.search(r'/apps/([^/]+)/', path)
                uuid = m.group(1) if m else None
                if not uuid or uuid in live:
                    continue  # instance still present (or unparseable) - keep
                lv_name = self._volume_lv_name(path)
                if subprocess.run(
                        ['lvs', f'{self.STORAGE_VG}/{lv_name}'],
                        capture_output=True).returncode != 0:
                    continue  # LV already gone
                subprocess.run(['umount', path],
                               capture_output=True, text=True, timeout=30)
                r = subprocess.run(
                    ['lvremove', '-f', f'{self.STORAGE_VG}/{lv_name}'],
                    capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    log('mqtt', f'Reclaimed LV {lv_name} (instance {uuid} deleted)')
                else:
                    log('mqtt', f'lvremove {lv_name} failed: {r.stderr}')

    def _find_new_storage_disks(self, desired_devices):
        """Compare desired device list against current VG PVs to find new disks."""
        try:
            result = subprocess.run(
                ['pvs', '--noheadings', '-o', 'pv_name',
                 '-S', f'vg_name={self.STORAGE_VG}'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return []
            # Parse PV names like "/dev/mapper/reefy-sda" → "sda"
            current_devs = set()
            for line in result.stdout.strip().splitlines():
                pv = line.strip()
                # Extract device name from /dev/mapper/reefy-<name>
                prefix = '/dev/mapper/reefy-'
                if pv.startswith(prefix):
                    current_devs.add(pv[len(prefix):])
            return [d for d in desired_devices if d not in current_devs]
        except Exception:
            return []

    def _extend_storage(self, new_disks):
        """Add new disks to existing VG and extend the LV + filesystem online."""
        key_part = self._find_reefy_key_partition()
        if not key_part:
            log('reconciler', f'{'Cannot find reefy LUKS key partition'}')
            return

        luks_key_size = 44
        new_pvs = []

        for dev_name in new_disks:
            target = f'/dev/{dev_name}'
            if not os.path.exists(target):
                log('reconciler', f'{f'Storage device {target} not found'}')
                continue

            luks_name = f'reefy-{dev_name}'

            # Wipe, LUKS format, open
            self._force_wipe_device(target)
            log('mqtt', f'LUKS formatting {target} (extending VG)')
            result = subprocess.run(
                ['cryptsetup', 'luksFormat', target,
                 '--key-file', key_part, '--keyfile-size', str(luks_key_size),
                 '--batch-mode'],
                capture_output=True, text=True, timeout=600
            )
            if result.returncode != 0:
                log('reconciler', f'{f'LUKS format failed on {target}: {result.stderr.strip()}'}')
                continue

            result = subprocess.run(
                ['cryptsetup', 'luksOpen', target, luks_name,
                 '--key-file', key_part, '--keyfile-size', str(luks_key_size)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                log('reconciler', f'{f'LUKS open failed on {target}: {result.stderr.strip()}'}')
                continue

            pv_path = f'/dev/mapper/{luks_name}'
            subprocess.run(['pvcreate', '-ff', '-y', pv_path],
                           capture_output=True, text=True, timeout=15)
            new_pvs.append(pv_path)

        if not new_pvs:
            log('reconciler', f'{'No new storage devices could be prepared'}')
            return

        # Extend VG with new PVs
        result = subprocess.run(
            ['vgextend', self.STORAGE_VG] + new_pvs,
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            log('mqtt', f'VG extend failed: {result.stderr}')
            return

        # Extend LV to use all free space
        lv_path = f'/dev/{self.STORAGE_VG}/{self.STORAGE_LV}'
        result = subprocess.run(
            ['lvextend', '-l', '+100%FREE', lv_path],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            log('mqtt', f'LV extend failed: {result.stderr}')
            return

        # Grow filesystem online
        result = subprocess.run(
            ['resize2fs', lv_path],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            dev_list = ', '.join(new_disks)
            log('mqtt', f'Extended storage with {dev_list}')
        else:
            log('mqtt', f'resize2fs failed: {result.stderr}')

    def _get_dm_tree(self, target):
        """Build list of all dm device names that depend on target (any depth).

        Walks the dependency tree: target → LUKS → LVM LV, etc.
        Returns dm names in removal order (leaves/top-level first).
        """
        try:
            stat = os.stat(target)
            target_majmin = (os.major(stat.st_rdev), os.minor(stat.st_rdev))
            log('mqtt', f'dm-tree: target {target} = ({target_majmin[0]}, {target_majmin[1]})')
        except (OSError, AttributeError) as e:
            log('mqtt', f'dm-tree: cannot stat {target}: {e}')
            return []

        result = subprocess.run(['dmsetup', 'ls'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            log('mqtt', f'dm-tree: dmsetup ls failed')
            return []

        # Build name → (major, minor) and name → dep_string mappings
        dm_devices = {}
        dm_deps = {}
        for line in result.stdout.strip().split('\n'):
            if not line or line.startswith('No devices'):
                continue
            parts = line.split()
            name = parts[0]
            # Parse (major, minor) from dmsetup ls output like "name (253, 0)"
            if len(parts) >= 2:
                try:
                    majmin_str = ' '.join(parts[1:]).strip('()')
                    maj, mn = majmin_str.split(',')
                    dm_devices[name] = (int(maj.strip()), int(mn.strip()))
                except (ValueError, IndexError):
                    pass
            deps = subprocess.run(['dmsetup', 'deps', name],
                                  capture_output=True, text=True, timeout=5)
            if deps.returncode == 0:
                dm_deps[name] = deps.stdout.strip()
                log('mqtt', f'dm-tree: {name} majmin={dm_devices.get(name)} deps={deps.stdout.strip()}')

        # Walk tree: find all dm devices reachable from target's major:minor
        to_remove = []
        search_majmins = {target_majmin}
        changed = True
        while changed:
            changed = False
            for name, dep_str in dm_deps.items():
                if name in to_remove:
                    continue
                for majmin in search_majmins:
                    if f'({majmin[0]}, {majmin[1]})' in dep_str:
                        to_remove.append(name)
                        log('mqtt', f'dm-tree: {name} depends on ({majmin[0]}, {majmin[1]})')
                        # Add this dm device's own major:minor to find layers above it
                        if name in dm_devices:
                            search_majmins.add(dm_devices[name])
                        changed = True
                        break

        # Reverse: remove top-level (LVM LVs) before bottom-level (LUKS)
        result = list(reversed(to_remove))
        log('mqtt', f'dm-tree: removal order: {result}')
        return result

    def _force_wipe_device(self, target):
        """Force-wipe a block device: tear down full dm tree, wipe signatures, zero header.

        Handles any pre-existing configuration: LVM, LUKS, RAID, btrfs, ZFS, etc.
        Walks the full dependency tree (e.g., device → LUKS → LVM) and removes
        all layers top-down before wiping.
        """
        log('mqtt', f'Force-wiping {target}')

        # Step 1: Tear down full dm tree (LVM LVs → LUKS → raw device)
        dm_tree = self._get_dm_tree(target)
        for dm_name in dm_tree:
            dm_path = f'/dev/mapper/{dm_name}'
            # Try LVM deactivation first (lvchange needs the LV to be inactive)
            result = subprocess.run(
                ['dmsetup', 'info', '-c', '--noheadings', '-o', 'uuid', dm_name],
                capture_output=True, text=True, timeout=5)
            dm_uuid = result.stdout.strip() if result.returncode == 0 else ''
            if dm_uuid.startswith('LVM-'):
                log('mqtt', f'Deactivating LVM device: {dm_name}')
                subprocess.run(['lvchange', '-an', dm_path],
                               capture_output=True, timeout=10)
            elif dm_uuid.startswith('CRYPT-'):
                log('mqtt', f'Closing LUKS: {dm_name}')
                subprocess.run(['cryptsetup', 'close', dm_name],
                               capture_output=True, timeout=15)
            else:
                log('mqtt', f'Removing dm device: {dm_name} (uuid={dm_uuid})')
                subprocess.run(['dmsetup', 'remove', '-f', dm_name],
                               capture_output=True, timeout=10)

        # Verify all dm layers are gone
        remaining = self._get_dm_tree(target)
        if remaining:
            log('mqtt', f'Warning: dm devices still active after cleanup: {remaining}')
            for dm_name in remaining:
                log('mqtt', f'Force-removing: {dm_name}')
                subprocess.run(['dmsetup', 'remove', '-f', dm_name],
                               capture_output=True, timeout=10)

        # Step 2: Wipe all filesystem/volume-manager signatures
        log('mqtt', f'Wiping signatures on {target}')
        subprocess.run(['wipefs', '-af', target], capture_output=True, timeout=15)

        # Step 3: Zero first 4MB (GPT headers, superblocks, partition tables)
        log('mqtt', f'Zeroing first 4MB of {target}')
        subprocess.run(
            ['dd', 'if=/dev/zero', f'of={target}', 'bs=1M', 'count=4', 'conv=fsync'],
            capture_output=True, timeout=15
        )

        subprocess.run(['blockdev', '--flushbufs', target], capture_output=True, timeout=5)
        log('mqtt', f'Device {target} wiped successfully')

    def _find_reefy_disk(self):
        """Find the reefy/sbnb USB disk and return (disk, partition_dev) tuple.
        Supports A/B layout with both reefy-a/reefy-b and legacy sbnb-a/sbnb-b labels."""
        for label in ('reefy-a', 'reefy-b', 'sbnb-a', 'sbnb-b'):
            try:
                dev = subprocess.run(
                    ['blkid', '-L', label],
                    capture_output=True, text=True, timeout=5
                ).stdout.strip()
                if dev:
                    parent = subprocess.run(
                        ['lsblk', '-no', 'PKNAME', dev],
                        capture_output=True, text=True, timeout=5
                    ).stdout.strip()
                    if parent:
                        return f'/dev/{parent}', dev
            except Exception:
                continue
        return None, None

    def _find_reefy_key_partition(self):
        """Find the LUKS key partition on the reefy USB drive (partition 3)."""
        disk, _ = self._find_reefy_disk()
        if disk:
            key_part = _part_dev(disk, 3)
            if os.path.exists(key_part):
                return key_part
        return None

    def _prepare_app_dirs(self, app_volumes, backup_paths=None):
        """Create host-mount directories for app volumes with correct ownership,
        then write seed files that don't already exist.

        app_volumes is a list of:
        {"path": "/mnt/reefy-data/apps/oc/data", "uid": 1000,
         "seed_files": {"openclaw.json": {...}}}

        backup_paths (set): paths that should be backed by their own
        thin LV (one LV per backup-true volume) so reefy-backup can
        snapshot them. If a path is in backup_paths and the thin pool
        is available, this provisions and mounts a per-volume LV at
        the path before the seed step. Falls back to plain dir if the
        thin pool is absent (legacy storage layout) or if the path
        already has data on the default LV (legacy install). See
        PLAN-backup.md §"Storage Layout".
        """
        backup_paths = backup_paths or set()
        for vol in app_volumes:
            path = vol.get('path', '')
            uid = vol.get('uid', 0)
            if not path:
                continue
            # Per-volume thin LV: for backup paths (so reefy-backup can
            # snapshot them) AND for fair-share-capped paths (so a cap
            # can be enforced via the LV's virtualsize - e.g. Frigate
            # media, which isn't backed up but must not eat the pool).
            if path in backup_paths or path in self._volume_caps:
                self._ensure_volume_lv(path)
            if not os.path.exists(path):
                os.makedirs(path, mode=0o755, exist_ok=True)
                log('mqtt', f'Created app dir: {path} (uid={uid})')

            # Write seed files (only if they don't already exist)
            seed_files = vol.get('seed_files', {})
            for filename, content_b64 in seed_files.items():
                file_path = os.path.join(path, filename)
                if os.path.exists(file_path):
                    log('mqtt', f'Seed file exists, skipping: {file_path}')
                    continue
                data = base64.b64decode(content_b64)
                with open(file_path, 'wb') as f:
                    f.write(data)
                log('mqtt', f'Wrote seed file: {file_path}')

            # Download files from URLs (only if they don't already exist)
            for f in vol.get('files', []):
                file_path = os.path.join(path, f['name'])
                if os.path.exists(file_path):
                    log('mqtt', f'File exists, skipping: {file_path}')
                    continue
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                try:
                    log('mqtt', f'Downloading {f['url']} -> {file_path}')
                    subprocess.run(
                        ['curl', '-fSL', '-o', file_path, f['url']],
                        capture_output=True, timeout=600, check=True
                    )
                    log('mqtt', f'Downloaded: {file_path}')
                except Exception as e:
                    log('mqtt', f'Download failed for {f['url']}: {e}')

            # Ensure correct ownership (only chown top-level dir to avoid
            # timeout on large directories like frigate media)
            try:
                st = os.stat(path)
                if st.st_uid != uid or st.st_gid != uid:
                    subprocess.run(['chown', f'{uid}:{uid}', path],
                                   capture_output=True, timeout=10)
            except Exception:
                subprocess.run(['chown', f'{uid}:{uid}', path],
                               capture_output=True, timeout=10)


def main_boot_mount():
    """Boot-mount executable entrypoint (reefy-mount-volumes): mount
    per-app volumes from persisted desired-state, then exit, so docker
    (ordered after it) starts with volumes already in place."""
    try:
        Storage().boot_mount()
    except Exception as e:
        log('mqtt', f'[boot-mount] fatal: {e}')
    sys.exit(0)
