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
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid as uuid_mod
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from io import BytesIO

from reefy import shared
from reefy.shared import _part_dev, log


class ExistingVolumeUnavailableError(RuntimeError):
    """An owned data LV exists but cannot safely back its app path."""


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
    MANAGED_VOLUME_TAG = 'reefy_managed_app_volume'
    OWNER_TAG_PREFIX = 'reefy_owner_'
    MANAGED_VOLUME_RE = re.compile(r'^reefy_backup_[0-9a-f]{12}$')
    LUKS_SECTOR_SIZES = (4096, 2048, 1024, 512)

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
        result = subprocess.run(
            ['dd', 'if=/dev/urandom', f'of={key_part}', 'bs=1M', 'count=1'],
            capture_output=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(
                f'Failed to initialize LUKS key partition {key_part}: '
                f'{self._process_error(result)}')
        result = subprocess.run(
            ['openssl', 'rand', '-base64', '32'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                f'Failed to generate LUKS key: {self._process_error(result)}')
        passphrase = result.stdout.strip()
        result = subprocess.run(
            ['dd', f'of={key_part}', 'conv=notrunc'],
            input=passphrase.encode(), capture_output=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(
                f'Failed to write LUKS key partition {key_part}: '
                f'{self._process_error(result)}')

    @staticmethod
    def _process_error(result):
        """Return a readable stderr/stdout string for CompletedProcess."""
        value = getattr(result, 'stderr', None) or getattr(result, 'stdout', None)
        if isinstance(value, bytes):
            value = value.decode(errors='replace')
        value = (value or '').strip()
        return value or '(no output)'

    @staticmethod
    def _is_power_of_two(value):
        return isinstance(value, int) and value > 0 and not value & (value - 1)

    def _read_blockdev_int(self, target, option):
        result = subprocess.run(
            ['blockdev', option, target],
            capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            raise RuntimeError(
                f'Cannot read {option} for {target}: '
                f'{self._process_error(result)}')
        try:
            return int(result.stdout.strip())
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f'Invalid {option} value for {target}: '
                f'{result.stdout!r}') from e

    def _read_block_geometry(self, target):
        """Return raw block geometry needed for safe LUKS formatting."""
        geometry = {
            'logical': self._read_blockdev_int(target, '--getss'),
            'physical': self._read_blockdev_int(target, '--getpbsz'),
            'size': self._read_blockdev_int(target, '--getsize64'),
        }
        logical = geometry['logical']
        physical = geometry['physical']
        if (not self._is_power_of_two(logical)
                or not self._is_power_of_two(physical)
                or logical < 512
                or physical < logical
                or geometry['size'] <= 0):
            raise RuntimeError(
                f'Invalid block geometry for {target}: '
                f'logical={logical}, physical={physical}, '
                f'size={geometry["size"]}')
        return geometry

    @classmethod
    def _choose_luks_sector_size(cls, geometries, required_sector_size=None):
        """Choose the largest safe LUKS sector shared by every raw disk."""
        if not geometries:
            raise RuntimeError('No storage devices selected for LUKS provisioning')
        candidates = (
            (required_sector_size,)
            if required_sector_size is not None
            else cls.LUKS_SECTOR_SIZES
        )
        for candidate in candidates:
            if candidate not in cls.LUKS_SECTOR_SIZES:
                continue
            if all(
                    geometry['logical'] <= candidate <= geometry['physical']
                    and candidate % geometry['logical'] == 0
                    and geometry['physical'] % candidate == 0
                    and geometry['size'] % candidate == 0
                    for geometry in geometries.values()):
                return candidate
        details = '; '.join(
            f'{target} logical={geometry["logical"]} '
            f'physical={geometry["physical"]} size={geometry["size"]}'
            for target, geometry in geometries.items())
        requirement = (f' required={required_sector_size}'
                       if required_sector_size is not None else '')
        raise RuntimeError(
            f'No safe common LUKS sector size for selected devices'
            f'{requirement}: {details}')

    def _preflight_luks_targets(self, targets, required_sector_size=None,
                                _log=None):
        """Validate a complete target batch before key rotation or wiping."""
        if not targets:
            raise RuntimeError('No storage devices selected for LUKS provisioning')
        device_paths = [target for target, _ in targets]
        mapper_names = [mapper_name for _, mapper_name in targets]
        if len(set(device_paths)) != len(device_paths):
            raise RuntimeError(f'Duplicate LUKS target selected: {device_paths}')
        if len(set(mapper_names)) != len(mapper_names):
            raise RuntimeError(f'Duplicate LUKS mapper selected: {mapper_names}')
        missing = [target for target in device_paths if not os.path.exists(target)]
        if missing:
            raise RuntimeError(
                f'Selected storage device(s) not found: {", ".join(missing)}')

        geometries = {
            target: self._read_block_geometry(target)
            for target in device_paths
        }
        sector_size = self._choose_luks_sector_size(
            geometries, required_sector_size=required_sector_size)
        if _log:
            details = ', '.join(
                f'{target}={geometry["logical"]}/{geometry["physical"]}'
                for target, geometry in geometries.items())
            _log(f'Using common LUKS sector size {sector_size} ({details})')
        return sector_size

    def _require_common_mapper_sector_size(self, mapper_paths,
                                           required_sector_size=None):
        if not mapper_paths:
            raise RuntimeError('No LUKS mapper devices were prepared')
        sizes = {
            mapper_path: self._read_blockdev_int(mapper_path, '--getss')
            for mapper_path in mapper_paths
        }
        unique = set(sizes.values())
        if len(unique) != 1:
            rendered = ', '.join(
                f'{path}={size}' for path, size in sizes.items())
            raise RuntimeError(
                f'LUKS mapper logical sector sizes do not match: {rendered}')
        sector_size = next(iter(unique))
        if sector_size not in self.LUKS_SECTOR_SIZES:
            raise RuntimeError(
                f'Unsupported LUKS mapper sector size {sector_size}: '
                f'{", ".join(mapper_paths)}')
        if (required_sector_size is not None
                and sector_size != required_sector_size):
            raise RuntimeError(
                f'LUKS mapper sector size {sector_size} does not match '
                f'required size {required_sector_size}: '
                f'{", ".join(mapper_paths)}')
        return sector_size

    def _provision_luks_stack(self, targets, key_part, _log=None,
                              sector_size=None, write_keyfile=True):
        """Provision every target with one explicit LUKS sector size.

        Returns all /dev/mapper/<name> paths for the next LVM layer, or
        raises after closing newly opened mappers. Fresh adoption writes one
        new shared key before formatting; extension and recovery callers set
        write_keyfile=False to preserve the existing shared key.

        `targets` is a list of (device_path, mapper_name) tuples; the
        USB-p4 fallback passes `[('/dev/sda4', 'reefy-data')]`, the
        internal-drive path passes one tuple per device in
        storage_config.devices with mapper names `reefy-<dev>`.

        For each target: tear down any prior dm/LUKS layers
        (`_force_wipe_device`), luksFormat with a 600s budget that
        captures stdout/stderr/dmesg on timeout (bare TimeoutExpired
        drops the captured output, leaving only the command line and
        budget - unactionable upstream), then luksOpen.

        When requested, the fresh keyfile is written once up front. All
        targets in this call share the same key, matching the existing
        single-key-unlocks-all-reefy-volumes invariant used at boot.
        """
        sector_size = self._preflight_luks_targets(
            targets, required_sector_size=sector_size, _log=_log)
        if write_keyfile:
            self._write_fresh_keyfile(key_part, _log)

        luks_pvs = []
        opened_mapper_names = []
        try:
            for target, mapper_name in targets:
                if _log:
                    _log(f'LUKS formatting {target}...')
                self._force_wipe_device(target)

                try:
                    result = subprocess.run(
                        ['cryptsetup', 'luksFormat', '--type', 'luks2',
                         '--sector-size', str(sector_size), target,
                         '--key-file', key_part, '--keyfile-size', '44',
                         '--batch-mode'],
                        capture_output=True, text=True, timeout=600,
                        check=False)
                except subprocess.TimeoutExpired as e:
                    out = e.stdout or ''
                    err = e.stderr or ''
                    if isinstance(out, bytes):
                        out = out.decode(errors='replace')
                    if isinstance(err, bytes):
                        err = err.decode(errors='replace')
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
                        f'{e.timeout}s.\nstdout: {out.strip() or "(empty)"}\n'
                        f'stderr: {err.strip() or "(empty)"}\n'
                        f'dmesg tail:\n{dmesg_tail}') from e

                if result.returncode != 0:
                    raise RuntimeError(
                        f'LUKS format failed on {target}: '
                        f'{self._process_error(result)}')

                # --allow-discards lets dm-crypt pass TRIM down to the
                # drive; --persistent stores the flags in the LUKS header
                # so subsequent opens inherit them. The performance flag
                # avoids a single post-encryption submission bottleneck.
                result = subprocess.run(
                    ['cryptsetup', 'luksOpen', target, mapper_name,
                     '--allow-discards', '--perf-submit_from_crypt_cpus',
                     '--persistent',
                     '--key-file', key_part, '--keyfile-size', '44'],
                    capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    raise RuntimeError(
                        f'LUKS open failed on {target}: '
                        f'{self._process_error(result)}')

                mapper_path = f'/dev/mapper/{mapper_name}'
                opened_mapper_names.append(mapper_name)
                actual_sector_size = self._read_blockdev_int(
                    mapper_path, '--getss')
                if actual_sector_size != sector_size:
                    raise RuntimeError(
                        f'LUKS mapper {mapper_path} has logical sector size '
                        f'{actual_sector_size}, expected {sector_size}')
                luks_pvs.append(mapper_path)
        except Exception:
            for mapper_name in reversed(opened_mapper_names):
                try:
                    subprocess.run(
                        ['cryptsetup', 'luksClose', mapper_name],
                        capture_output=True, timeout=30)
                except Exception:
                    pass
            raise

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
            raise RuntimeError('Cannot find Reefy boot disk for storage key')

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
                raise RuntimeError(
                    f'LUKS key partition was not created: {key_part}')

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
                 '--perf-submit_from_crypt_cpus',
                 '--key-file', key_part, '--keyfile-size', '44'],
                capture_output=True, timeout=120)
            if result.returncode == 0:
                if _log:
                    _log(f'Opened existing LUKS on {dev}')

        # Check if we can activate LVM from opened drives
        subprocess.run(['vgscan'], capture_output=True, timeout=10)
        if subprocess.run(['vgs', 'reefy'], capture_output=True,
                          timeout=5).returncode == 0:
            result = subprocess.run(
                ['vgchange', '-ay', 'reefy'], capture_output=True,
                text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError(
                    f'Existing storage VG activation failed: '
                    f'{self._process_error(result)}')
            # Prefer the new thin LV (reefy_default) over the legacy
            # flat `data` LV from pre-thin-pool installs.
            lv_path = self._active_reefy_lv_path()
            if lv_path:
                if _log:
                    _log('Mounting existing internal storage...')
                if self._finalize_data_mount(lv_path, _log, 'existing internal'):
                    return
                raise RuntimeError(
                    f'Existing internal storage mount failed for {lv_path}; '
                    f'refusing destructive reprovisioning')

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
            raise RuntimeError(
                'Requested internal storage was not mounted after '
                'provisioning; refusing USB fallback')

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
            raise RuntimeError(
                f'USB data partition was not created: {data_part}')

        # Fresh LUKS on the USB data partition via shared provision
        # helper (handles wipe + luksFormat + luksOpen + timeout
        # capture + fresh-key write). Mapper name `reefy-data` matches
        # what boot-reefy-storage.sh's setup_data_partition expects on
        # subsequent boots.
        luks_pvs = self._provision_luks_stack(
            [(data_part, 'reefy-data')], key_part, _log)
        if len(luks_pvs) != 1:
            raise RuntimeError(
                'USB data partition LUKS provision did not return one mapper')

        # Build VG + thin pool + reefy_default LV on top of the LUKS-
        # opened mapper. Helper handles the fresh-install path here.
        self._ensure_lvm_stack(luks_pvs, _log)
        lv_path = self._active_reefy_lv_path()
        if not lv_path:
            raise RuntimeError('No Reefy LV after USB LVM setup')

        # Migrate state from tmpfs, mount persistent (+ state LV + dirs).
        if not self._finalize_data_mount(lv_path, _log, 'USB, LVM thin'):
            raise RuntimeError('USB persistent storage mount failed')
        print("[mqtt] Persistent storage created on USB (LVM thin)")

    def _setup_internal_persistent(self, storage_config, key_part, _log=None):
        """Set up internal drives as /mnt/reefy-data during adoption."""
        devices = storage_config.get('devices', [])
        if not devices:
            return

        subprocess.run(['modprobe', 'dm_crypt'], capture_output=True, timeout=5)
        subprocess.run(['modprobe', 'dm_mod'], capture_output=True, timeout=5)

        targets = [
            (f'/dev/{dev_name}', f'reefy-{dev_name}')
            for dev_name in devices
        ]
        missing = [target for target, _ in targets
                   if not os.path.exists(target)]
        if missing:
            raise RuntimeError(
                f'Selected internal storage device(s) not found: '
                f'{", ".join(missing)}')

        luks_pvs = self._provision_luks_stack(targets, key_part, _log)
        if len(luks_pvs) != len(targets):
            raise RuntimeError(
                f'Prepared {len(luks_pvs)} of {len(targets)} selected '
                f'internal storage devices')

        # Build VG + thin pool + reefy_default LV (helper handles
        # existing-VG extend / legacy-LV mount paths too).
        self._ensure_lvm_stack(luks_pvs, _log)
        lv_path = self._active_reefy_lv_path()
        if not lv_path:
            raise RuntimeError(
                'No reefy_default or legacy LV after internal LVM setup')

        # Migrate state from tmpfs, mount persistent (+ state LV + dirs).
        dev_list = ', '.join(devices)
        if not self._finalize_data_mount(
                lv_path, _log, f'internal: {dev_list}'):
            raise RuntimeError(
                f'Internal storage mount failed for selected devices: '
                f'{dev_list}')
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

    def _pv_vg_name(self, pv):
        result = subprocess.run(
            ['pvs', '--noheadings', '-o', 'vg_name',
             '-S', f'pv_name={pv}'],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(
                f'Cannot inspect PV {pv}: {self._process_error(result)}')
        return result.stdout.strip() or None

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
        self._require_common_mapper_sector_size(luks_pvs)
        pv_memberships = {}
        for pv in luks_pvs:
            pv_memberships[pv] = self._pv_vg_name(pv)
            if (pv_memberships[pv]
                    and pv_memberships[pv] != self.STORAGE_VG):
                raise RuntimeError(
                    f'PV {pv} already belongs to VG {pv_memberships[pv]}')
            if pv_memberships[pv]:
                continue
            result = subprocess.run(
                ['pvcreate', '-ff', '-y', pv],
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(
                    f'PV create failed on {pv}: '
                    f'{self._process_error(result)}')

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
            # Extend with the complete new PV set in one LVM transaction.
            new_pvs = [pv for pv in luks_pvs
                       if pv_memberships[pv] != self.STORAGE_VG]
            if new_pvs:
                result = subprocess.run(
                    ['vgextend', self.STORAGE_VG] + new_pvs,
                    capture_output=True, text=True, timeout=15)
                if result.returncode != 0:
                    raise RuntimeError(
                        f'VG extend failed for {", ".join(new_pvs)}: '
                        f'{self._process_error(result)}')
            result = subprocess.run(
                ['vgchange', '-ay', self.STORAGE_VG],
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(
                    f'VG activation failed: {self._process_error(result)}')

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
        try:
            result = subprocess.run(
                ['lvs', f'{self.STORAGE_VG}/{self.STORAGE_POOL}'],
                capture_output=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return False
        return result.returncode == 0

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

    @staticmethod
    def _valid_owned_volume_path(path):
        if not isinstance(path, str):
            return False
        normalized = os.path.normpath(path)
        prefix = f'{shared.REEFY_DATA_MNT}/apps/'
        if normalized != path or not normalized.startswith(prefix):
            return False
        # Registry entries are volume roots, never arbitrary descendants:
        # /mnt/reefy-data/apps/<instance>/<volume>.
        relative = normalized[len(prefix):]
        return len(relative.split('/')) == 2 and all(
            segment not in ('', '.', '..')
            for segment in relative.split('/'))

    @classmethod
    def _owner_tag_for_instance(cls, instance_id):
        if not isinstance(instance_id, str) or not instance_id:
            return None
        token = hashlib.sha256(instance_id.encode()).hexdigest()[:20]
        return f'{cls.OWNER_TAG_PREFIX}{token}'

    @classmethod
    def _owner_tag_for_path(cls, path):
        """Return a stable, non-identifying LVM owner tag for an app path."""
        if not cls._valid_owned_volume_path(path):
            return None
        prefix = f'{shared.REEFY_DATA_MNT}/apps/'
        instance_id = path[len(prefix):].split('/', 1)[0]
        return cls._owner_tag_for_instance(instance_id)

    @classmethod
    def _cap_warning_for_path(cls, path):
        """Return a path-free warning that identifies the affected volume."""
        if not cls._valid_owned_volume_path(path):
            return None
        prefix = f'{shared.REEFY_DATA_MNT}/apps/'
        instance_uuid, volume = path[len(prefix):].split('/', 1)
        return {
            'code': 'storage.cap_not_enforced',
            'instance_uuid': instance_uuid,
            'volume': volume,
        }

    def _volume_tags(self, lv_name):
        """Return an LV's tags, or None when LVM cannot be inspected."""
        try:
            result = subprocess.run(
                ['lvs', '--noheadings', '-o', 'lv_tags',
                 f'{self.STORAGE_VG}/{lv_name}'],
                capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        return {tag.strip() for tag in result.stdout.strip().split(',')
                if tag.strip()}

    def _remember_owned_volume(self, path):
        """Attach ownership to the LV itself instead of a sidecar registry.

        Existing provisioned devices are adopted lazily when their current or
        previous desired-state path resolves to the deterministic LV name.
        Unknown untagged legacy LVs are never reclaimed.
        """
        owner_tag = self._owner_tag_for_path(path)
        if owner_tag is None:
            return False
        lv_name = self._volume_lv_name(path)
        tags = self._volume_tags(lv_name)
        if tags is None:
            return False
        missing = [tag for tag in (self.MANAGED_VOLUME_TAG, owner_tag)
                   if tag not in tags]
        if not missing:
            return True
        command = ['lvchange']
        for tag in missing:
            command.extend(['--addtag', tag])
        command.append(f'{self.STORAGE_VG}/{lv_name}')
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=15)
        except (subprocess.SubprocessError, OSError):
            return False
        if result.returncode != 0:
            log('mqtt', 'Cannot attach ownership to dedicated app volume')
            return False
        return True

    @staticmethod
    def _capped_virtual_size(pool_size, cap_pct, sector_size,
                             allocation_size=None):
        """Return cap bytes rounded down to mapper and LVM alignment."""
        try:
            pool_bytes = int(Decimal(str(pool_size)))
            pct = Decimal(str(cap_pct))
            sector_bytes = int(sector_size)
            allocation_bytes = int(
                sector_bytes if allocation_size is None else allocation_size)
        except (InvalidOperation, TypeError, ValueError, OverflowError) as e:
            raise ValueError('invalid volume cap inputs') from e
        if (pool_bytes <= 0 or sector_bytes <= 0 or allocation_bytes <= 0
                or not pct.is_finite() or pct <= 0 or pct > 100):
            raise ValueError('volume cap inputs are outside safe bounds')
        alignment = math.lcm(sector_bytes, allocation_bytes)
        raw_bytes = int(
            (Decimal(pool_bytes) * pct / Decimal(100)).to_integral_value(
                rounding=ROUND_FLOOR))
        aligned_bytes = raw_bytes - raw_bytes % alignment
        if aligned_bytes <= 0 or aligned_bytes > pool_bytes:
            raise ValueError('volume cap is too small for LVM alignment')
        return aligned_bytes

    def _lv_virtual_size(self, lv_ref):
        """Return an LV's virtual size in bytes, or None if unconfirmed."""
        try:
            result = subprocess.run(
                ['lvs', '--noheadings', '--nosuffix', '--units', 'b',
                 '-o', 'lv_size', lv_ref],
                capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        raw_size = result.stdout.strip().lstrip('<>')
        if result.returncode != 0 or not raw_size:
            return None
        try:
            return int(Decimal(raw_size))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return None

    def _lv_metadata_names(self):
        """Return all VG LV names, or None on inspection error."""
        try:
            result = subprocess.run(
                ['lvs', '--noheadings', '-o', 'lv_name', self.STORAGE_VG],
                capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        return {line.strip() for line in result.stdout.splitlines()
                if line.strip()}

    def _lv_metadata_with_tags(self):
        """Return {lv_name: {tags}}, or None on inspection failure."""
        try:
            result = subprocess.run(
                ['lvs', '--noheadings', '--separator', '|',
                 '-o', 'lv_name,lv_tags', self.STORAGE_VG],
                capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        records = {}
        for line in result.stdout.splitlines():
            fields = line.strip().split('|', 1)
            if not fields or not fields[0].strip():
                continue
            records[fields[0].strip()] = {
                tag.strip()
                for tag in (fields[1] if len(fields) > 1 else '').split(',')
                if tag.strip()
            }
        return records

    def _lv_metadata_exists(self, lv_name):
        """Return LV existence from LVM metadata, or None on inspection error."""
        names = self._lv_metadata_names()
        return None if names is None else lv_name in names

    def _desired_volume_size(self, path):
        """Return the requested virtual size for a new or capped LV."""
        pool_ref = f'{self.STORAGE_VG}/{self.STORAGE_POOL}'
        pool_size = self._lv_virtual_size(pool_ref)
        if pool_size is None:
            log('mqtt',
                'Cannot read thin-pool size for per-volume storage')
            return None
        if path not in self._volume_caps:
            return pool_size
        try:
            return self._capped_virtual_size(
                pool_size, self._volume_caps[path],
                self._vg_mapper_sector_size(), self._vg_extent_size())
        except (RuntimeError, ValueError,
                subprocess.SubprocessError, OSError) as e:
            log('mqtt', f'Cannot calculate safe capped volume size: {e}')
            return None

    def _remove_new_volume_lv(self, lv_ref):
        """Best-effort cleanup after this invocation created a bad LV."""
        lv_name = lv_ref.rsplit('/', 1)[-1]
        present = self._lv_metadata_exists(lv_name)
        if present is False:
            return True
        if present is None:
            log('mqtt',
                'Cannot verify newly-created per-volume LV for cleanup; '
                'manual inspection is required')
            return False
        try:
            subprocess.run(
                ['lvremove', '-f', lv_ref], capture_output=True,
                text=True, timeout=15)
        except (subprocess.SubprocessError, OSError):
            # The command may have committed metadata before its client
            # timed out. The authoritative post-state below decides.
            pass
        remaining = self._lv_metadata_exists(lv_name)
        if remaining is False:
            return True
        log('mqtt',
            'Unable to clean up newly-created per-volume LV; '
            'manual inspection is required')
        return False

    def _require_new_volume_cleanup(self, lv_ref):
        """Confirm a failed fresh LV is absent before default fallback."""
        if not self._remove_new_volume_lv(lv_ref):
            raise ExistingVolumeUnavailableError(
                'New app volume could not be safely removed')

    @staticmethod
    def _same_block_device(first, second):
        """Return True when two paths name the same block device.

        Device-mapper aliases are not consistently represented as symlinks.
        A valid /dev/mapper node may be a direct block-device node while the
        corresponding /dev/<vg>/<lv> path resolves through /dev/dm-N. Path
        string comparison therefore produces false mismatches. st_rdev is
        the kernel device identity and is stable across both forms.
        """
        if not first or not second:
            return False
        try:
            first_stat = os.stat(first)
            second_stat = os.stat(second)
        except (OSError, TypeError, ValueError):
            return False
        return (
            stat.S_ISBLK(first_stat.st_mode)
            and stat.S_ISBLK(second_stat.st_mode)
            and first_stat.st_rdev == second_stat.st_rdev)

    @classmethod
    def _unexpected_mount_error(cls, path):
        """Return a path-free, actionable error for an unsafe app mount."""
        identity = cls._cap_warning_for_path(path)
        label = (
            f'{identity["instance_uuid"]}/{identity["volume"]}'
            if identity else 'an app volume')
        return ExistingVolumeUnavailableError(
            f'Storage mapping conflict for {label}: the Reefy volume assigned '
            'to this app is not mounted at its expected path. Setup stopped '
            'to protect existing data. Do not format or delete storage. '
            'Inspect the volume mapping, then Resync.')

    def _remove_mounted_new_volume(self, path, lv_name):
        """Unmount and remove a fresh LV before allowing default fallback."""
        lv_path = f'/dev/{self.STORAGE_VG}/{lv_name}'
        try:
            mounted = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE', path],
                capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError) as e:
            raise ExistingVolumeUnavailableError(
                'Cannot inspect a new app volume mount') from e
        if mounted.returncode == 0:
            source = mounted.stdout.strip()
            if not self._same_block_device(source, lv_path):
                raise self._unexpected_mount_error(path)
            try:
                unmounted = subprocess.run(
                    ['umount', path], capture_output=True,
                    text=True, timeout=30)
            except (subprocess.SubprocessError, OSError) as e:
                raise ExistingVolumeUnavailableError(
                    'New app volume could not be unmounted') from e
            if unmounted.returncode != 0:
                raise ExistingVolumeUnavailableError(
                    'New app volume could not be unmounted')
        elif mounted.returncode != 1:
            raise ExistingVolumeUnavailableError(
                'Cannot determine whether a new app volume is mounted')
        self._require_new_volume_cleanup(
            f'{self.STORAGE_VG}/{lv_name}')

    def _resolve_new_volume_mount_failure(self, path, lv_name):
        """Return True if mount actually succeeded, False if LV was removed."""
        lv_path = f'/dev/{self.STORAGE_VG}/{lv_name}'
        try:
            mounted = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE', path],
                capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError) as e:
            raise ExistingVolumeUnavailableError(
                'Cannot determine whether a new app volume was mounted') from e
        if mounted.returncode == 0:
            source = mounted.stdout.strip()
            if self._same_block_device(source, lv_path):
                if self._remember_owned_volume(path):
                    return True
                self._remove_mounted_new_volume(path, lv_name)
                return False
            raise self._unexpected_mount_error(path)
        if mounted.returncode != 1:
            raise ExistingVolumeUnavailableError(
                'Cannot determine whether a new app volume was mounted')
        self._require_new_volume_cleanup(
            f'{self.STORAGE_VG}/{lv_name}')
        return False

    def _dedicated_volume_mount_status(self, path):
        """Return True for the expected LV, False if absent, else None."""
        lv_path = (
            f'/dev/{self.STORAGE_VG}/{self._volume_lv_name(path)}')
        try:
            mounted = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE', path],
                capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return None
        if mounted.returncode == 1:
            return False
        if mounted.returncode != 0:
            return None
        source = mounted.stdout.strip()
        if not source:
            return None
        if not self._same_block_device(source, lv_path):
            return None
        return True

    def _ensure_volume_lv(self, path, allow_create=True,
                          expect_existing=False):
        """Provision + mount a per-volume thin LV at `path` if not
        already in place. No-op if:
          - the device has no thin pool (legacy storage), OR
          - the path is already a mount point (idempotent), OR
          - the path already exists with files on the default LV
            (legacy install - leave as plain dir to avoid hiding data).

        Concurrency-safe + idempotent: the boot oneshot and the running
        reconciler may both call this; the flock serializes them and the
        mountpoint re-check inside the lock makes a lost race a no-op.
        Returns True when the path is on its dedicated LV and any requested
        cap is confirmed. Returns False when callers must use default app
        storage, or when an existing data-bearing LV must be preserved but
        its requested cap cannot be confirmed.
        """
        if not self._has_thin_pool():
            if expect_existing:
                raise ExistingVolumeUnavailableError(
                    'Cannot access the thin pool for an owned app volume')
            return False  # legacy storage; backups are disabled anyway

        with self._volume_lock():
            lv_name = self._volume_lv_name(path)
            lv_path = f'/dev/{self.STORAGE_VG}/{lv_name}'
            capped = path in self._volume_caps

            # Re-check INSIDE the lock: another process may have mounted
            # it while we waited on the flock.
            try:
                r = subprocess.run(
                    ['findmnt', '-n', '-o', 'SOURCE', path],
                    capture_output=True, text=True, timeout=5)
            except (subprocess.SubprocessError, OSError) as e:
                if expect_existing:
                    raise ExistingVolumeUnavailableError(
                        'Cannot inspect an owned app volume mount') from e
                raise
            if r.returncode == 0 and r.stdout.strip():
                expected_source = self._same_block_device(
                    r.stdout.strip(), lv_path)
                if not expected_source:
                    if expect_existing:
                        raise self._unexpected_mount_error(path)
                    if not capped and not allow_create:
                        return True
                    log('mqtt',
                        'Dedicated-volume path is mounted from an unexpected '
                        'device; leaving it mounted without claiming success')
                    return False
                if not self._remember_owned_volume(path):
                    # Compatibility for already-provisioned devices: an
                    # untagged deterministic LV remains usable and is never
                    # eligible for automatic reclaim. A later reconcile can
                    # retry attaching the tags.
                    log('mqtt',
                        'Cannot attach ownership metadata to existing app '
                        'volume; preserving it without automatic reclaim')
                if not capped:
                    return True
                desired_size = self._desired_volume_size(path)
                actual_size = self._lv_virtual_size(
                    f'{self.STORAGE_VG}/{lv_name}')
                if (desired_size is None or actual_size is None
                        or actual_size > desired_size):
                    log('mqtt',
                        'Existing volume exceeds or cannot confirm its cap; '
                        'leaving it mounted without claiming enforcement')
                    return False
                return True
            if r.returncode != 1:
                if expect_existing:
                    raise ExistingVolumeUnavailableError(
                        'Cannot inspect an owned app volume mount')
                log('mqtt',
                    'Cannot inspect per-volume mount; '
                    'using default app storage')
                return False

            # The /dev symlink is not authoritative: an inactive LV can be
            # present in LVM metadata without a device node. Authoritative
            # pre-create state is also what makes ambiguous timeout cleanup
            # safe - we only remove an LV proven absent before our command.
            lv_exists = self._lv_metadata_exists(lv_name)
            if lv_exists is None:
                if expect_existing:
                    raise ExistingVolumeUnavailableError(
                        'Cannot inspect an owned app volume')
                log('mqtt',
                    'Cannot inspect per-volume LV metadata; '
                    'using default app storage')
                return False

            if expect_existing and not lv_exists:
                raise ExistingVolumeUnavailableError(
                    'Owned app volume is missing from LVM metadata')

            # Existing dir with files on the default LV -> legacy install.
            # Never hide divergent default-LV data with an owned LV.
            if os.path.isdir(path) and os.listdir(path):
                if lv_exists:
                    raise ExistingVolumeUnavailableError(
                        'Owned app volume conflicts with data at its mount path')
                log('mqtt',
                    f'Legacy data at {path}, skipping per-volume LV mount')
                return False

            if not lv_exists and not allow_create:
                return False

            desired_size = None
            cap_enforced = True
            if not lv_exists:
                desired_size = self._desired_volume_size(path)
                if desired_size is None:
                    return False
            elif capped:
                desired_size = self._desired_volume_size(path)
                actual_size = (
                    self._lv_virtual_size(f'{self.STORAGE_VG}/{lv_name}')
                    if desired_size is not None else None)
                if (desired_size is None or actual_size is None
                        or actual_size > desired_size):
                    cap_enforced = False
                    log('mqtt',
                        'Existing volume exceeds or cannot confirm its cap; '
                        'preserving its data and reporting a warning')

            created_here = False
            if not lv_exists:
                # Fair-share containment: a capped volume's thin LV gets a
                # virtualsize of pct% of the pool. A thin LV can't map more
                # physical blocks than its virtualsize, so this guarantees a
                # (100-pct)% pool margin and ENOSPC lands only on this
                # volume - no quota machinery needed. Uncapped -> full pool.
                lv_ref = f'{self.STORAGE_VG}/{lv_name}'
                try:
                    r = subprocess.run(
                        ['lvcreate', '--thin', '--virtualsize',
                         f'{desired_size}B', '-n', lv_name,
                         f'{self.STORAGE_VG}/{self.STORAGE_POOL}'],
                        capture_output=True, text=True, timeout=15)
                except (subprocess.SubprocessError, OSError):
                    # A timed-out lvcreate may have completed in LVM before
                    # its client was killed. This LV was confirmed absent
                    # immediately above, so it is safe to attempt removal.
                    self._require_new_volume_cleanup(lv_ref)
                    raise
                if r.returncode != 0:
                    log('mqtt', f'per-volume LV create failed for {path}: {r.stderr}')
                    self._require_new_volume_cleanup(lv_ref)
                    return False
                if capped:
                    actual_size = self._lv_virtual_size(lv_ref)
                    if actual_size is None or actual_size > desired_size:
                        log('mqtt',
                            'Created volume size cannot safely enforce cap; '
                            'removing it and using default app storage')
                        self._require_new_volume_cleanup(lv_ref)
                        return False
                # XFS for new volumes: dynamic inode allocation -> no
                # ~58 GiB upfront inode-table tax that ext4 paid on these
                # full-pool-size LVs. (No -L: the LV name exceeds XFS's
                # 12-char label limit; the dm path identifies it anyway.)
                # Existing ext4 LVs are untouched - we only mkfs on create.
                try:
                    r = subprocess.run(
                        ['mkfs.xfs', '-q', lv_path],
                        capture_output=True, text=True, timeout=60)
                except (subprocess.SubprocessError, OSError):
                    self._require_new_volume_cleanup(lv_ref)
                    raise
                if r.returncode != 0:
                    log('mqtt', f'mkfs.xfs failed on {lv_path}: {r.stderr}')
                    # Do not strand an unformatted LV. Removing only the LV
                    # created in this invocation makes the next reconcile a
                    # clean retry without ever formatting a pre-existing LV.
                    self._require_new_volume_cleanup(lv_ref)
                    return False
                created_here = True
                log('mqtt', f'Created per-volume LV {lv_name} (xfs) for {path}')

            try:
                os.makedirs(path, mode=0o755, exist_ok=True)
                # Mount can stall on thin-pool metadata ops when the pool is
                # under load. 60s gives the kernel enough headroom.
                mount_opts = self._fs_mount_opts(lv_path)
                r = subprocess.run(
                    ['mount', '-o', mount_opts, lv_path, path],
                    capture_output=True, text=True, timeout=60)
            except (subprocess.SubprocessError, OSError) as e:
                if created_here:
                    mounted = self._resolve_new_volume_mount_failure(
                        path, lv_name)
                    if mounted:
                        return cap_enforced
                    return False
                raise ExistingVolumeUnavailableError(
                    'Owned app volume mount preparation failed') from e
            if r.returncode != 0:
                log('mqtt', f'mount {lv_path} -> {path} failed: {r.stderr}')
                if created_here:
                    mounted = self._resolve_new_volume_mount_failure(
                        path, lv_name)
                    if mounted:
                        return cap_enforced
                    return False
                raise ExistingVolumeUnavailableError(
                    'Owned app volume could not be mounted')
            log('mqtt', f'Mounted {lv_name} at {path}')
            if not self._remember_owned_volume(path):
                if created_here:
                    self._remove_mounted_new_volume(path, lv_name)
                    return False
                # Never make an existing device unavailable just because an
                # upgrade could not attach new metadata. Untagged LVs are
                # deliberately excluded from reclaim, so this fails safe.
                log('mqtt',
                    'Cannot attach ownership metadata to existing app '
                    'volume; preserving it without automatic reclaim')
            return cap_enforced

    def boot_mount(self):
        """Mount all per-app volumes from persisted desired-state, before
        docker starts. Run by reefy-app-volumes.service (oneshot,
        Before=docker.service) so containers never bind-mount an empty
        pre-mount directory (the boot mount-race that shadowed Frigate's
        config). No MQTT, no network: reads the cached desired-state.json
        and reuses the shared flock'd mount primitive. Idempotent - the
        running reconciler re-applies the same paths for runtime
        add/remove.

        Keep this fast boot path limited to mounting live volumes. Reclaim,
        pruning, migration, and other housekeeping belong to normal runtime
        reconciliation and must not delay container startup.
        """
        state_path = shared.desired_state_path()
        if not os.path.exists(state_path):
            log('mqtt', '[boot-mount] no desired state; nothing to mount')
            return
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log('mqtt', f'[boot-mount] cannot read desired-state: {e}')
            return
        if state.get('schema_version') == 2:
            app_volumes = []
            volume_caps = {}
            backup_instances = []
            for app in state.get('apps') or []:
                app_volumes.extend(app.get('volumes') or [])
                volume_caps.update(app.get('volume_caps') or {})
                if app.get('backup'):
                    backup_instances.append(app['backup'])
            state = {
                'app_volumes': app_volumes,
                'volume_caps': volume_caps,
                'backup': {'instances': backup_instances},
            }
        self._volume_caps = state.get('volume_caps') or {}
        backup = state.get('backup') or {}
        # Mount backup-flagged paths AND fair-share-capped paths (e.g.
        # Frigate media) - both get a per-volume LV that must be mounted
        # before docker.
        backup_paths = set()
        for inst in backup.get('instances', []):
            backup_paths.update(inst.get('paths', []))
        managed_paths = set(self._volume_caps.keys()) | backup_paths
        app_paths = {
            volume.get('path')
            for volume in state.get('app_volumes', [])
            if isinstance(volume, dict) and volume.get('path')
        }
        metadata_names = self._lv_metadata_names()
        metadata_available = metadata_names is not None
        if not metadata_available:
            log('mqtt',
                '[boot-mount] cannot inventory dedicated app volumes; '
                'attempting existing volumes independently')
        thin_pool_available = (
            metadata_available and self.STORAGE_POOL in metadata_names)
        paths = managed_paths | app_paths
        seen = set()
        for p in sorted(paths):
            if not p or p in seen:
                continue
            seen.add(p)
            managed = p in managed_paths
            existing_lv = (
                self._volume_lv_name(p) in metadata_names
                if metadata_available else False)
            if metadata_available and not managed and not existing_lv:
                continue
            try:
                # A successful inventory authorizes normal boot preparation,
                # including creation for currently managed paths. If the
                # inventory failed, only attempt to discover and mount an
                # existing LV. Never create or delete storage from uncertain
                # metadata, and isolate each failure to its own app path.
                prepared = self._ensure_volume_lv(
                    p, allow_create=managed and metadata_available,
                    expect_existing=(
                        existing_lv if metadata_available else False))
                if (not prepared and p in backup_paths
                        and thin_pool_available
                        and self._dedicated_volume_mount_status(p) is not True):
                    raise ExistingVolumeUnavailableError(
                        'Dedicated backup volume is unavailable at boot')
                if not prepared and p in self._volume_caps:
                    log('mqtt',
                        '[boot-mount] storage cap not enforced; '
                        'using default app storage')
            except Exception as e:
                log('mqtt', f'[boot-mount] {p} failed: {e}')
        log('mqtt', f'[boot-mount] processed {len(seen)} volume(s) before docker')

    def _reclaim_deleted_instance_lvs(self, old_state, new_state,
                                       new_backup_paths):
        """Retry removal of Reefy-tagged LVs after their whole app is gone.

        Active and previous desired-state paths lazily tag deterministic LVs,
        which adopts already-provisioned devices without a sidecar registry.
        Unknown or untagged legacy LVs are always preserved. A failed removal
        retains its LVM tags and is naturally retried on the next reconcile.
        """
        if not self._has_thin_pool():
            return
        old_state = old_state or {}
        new_state = new_state or {}

        old_paths = set()
        for inst in (old_state.get('backup') or {}).get('instances', []):
            old_paths.update(inst.get('paths', []))
        old_paths.update((old_state.get('volume_caps') or {}).keys())
        new_managed_paths = (
            set(new_backup_paths or set())
            | set((new_state.get('volume_caps') or {}).keys()))

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
        for p in new_managed_paths:
            _collect_uuids(p, live)
        _collect_uuids(new_state.get('app_volumes', []), live)
        for instance in new_state.get('instances', []):
            if isinstance(instance, dict):
                for key in ('instance_uuid', 'uuid', 'instance_name'):
                    identifier = instance.get(key)
                    if identifier:
                        live.add(str(identifier))
            _collect_uuids(instance, live)

        with self._volume_lock():
            metadata = self._lv_metadata_with_tags()
            if metadata is None:
                log('mqtt',
                    'Skipping volume reclaim while LVM metadata is '
                    'unavailable')
                return

            # Backward compatibility: adopt deterministic LVs referenced by
            # either side of the desired-state transition. An unmatched
            # untagged LV from an older device is preserved indefinitely.
            seed_paths = old_paths | new_managed_paths
            for volume in old_state.get('app_volumes', []):
                if isinstance(volume, dict) and volume.get('path'):
                    seed_paths.add(volume['path'])
            for volume in new_state.get('app_volumes', []):
                if isinstance(volume, dict) and volume.get('path'):
                    seed_paths.add(volume['path'])
            for path in sorted(seed_paths):
                lv_name = self._volume_lv_name(path)
                if (lv_name in metadata
                        and self._valid_owned_volume_path(path)):
                    self._remember_owned_volume(path)

            metadata = self._lv_metadata_with_tags()
            if metadata is None:
                return
            live_owner_tags = {
                tag for tag in (
                    self._owner_tag_for_instance(identifier)
                    for identifier in live)
                if tag
            }

            for lv_name, tags in sorted(metadata.items()):
                if (not self.MANAGED_VOLUME_RE.fullmatch(lv_name)
                        or self.MANAGED_VOLUME_TAG not in tags):
                    continue
                owner_tags = {
                    tag for tag in tags
                    if tag.startswith(self.OWNER_TAG_PREFIX)
                }
                if not owner_tags or owner_tags & live_owner_tags:
                    continue
                lv_path = f'/dev/{self.STORAGE_VG}/{lv_name}'
                try:
                    find_mount = subprocess.run(
                        ['findmnt', '-n', '-o', 'TARGET', '--source', lv_path],
                        capture_output=True, text=True, timeout=5)
                except (subprocess.SubprocessError, OSError):
                    log('mqtt',
                        f'Cannot reclaim {lv_name}: mount inspection failed')
                    continue
                if find_mount.returncode == 0:
                    targets = [target.strip()
                               for target in find_mount.stdout.splitlines()
                               if target.strip()]
                    failed = False
                    for target in targets:
                        try:
                            unmount = subprocess.run(
                                ['umount', target], capture_output=True,
                                text=True, timeout=30)
                        except (subprocess.SubprocessError, OSError):
                            failed = True
                            break
                        if unmount.returncode != 0:
                            failed = True
                            break
                    if failed:
                        log('mqtt',
                            f'Cannot reclaim {lv_name}: unmount failed')
                        continue
                elif find_mount.returncode != 1:
                    log('mqtt',
                        f'Cannot reclaim {lv_name}: mount inspection failed')
                    continue
                try:
                    subprocess.run(
                        ['lvremove', '-f', f'{self.STORAGE_VG}/{lv_name}'],
                        capture_output=True, text=True, timeout=30)
                except (subprocess.SubprocessError, OSError):
                    pass
                present = self._lv_metadata_exists(lv_name)
                if present is False:
                    log('mqtt', f'Reclaimed orphaned app LV {lv_name}')
                else:
                    log('mqtt', f'lvremove {lv_name} did not remove metadata')

    def _find_new_storage_disks(self, desired_devices):
        """Compare desired device list against current VG PVs to find new disks."""
        result = subprocess.run(
            ['pvs', '--noheadings', '-o', 'pv_name',
             '-S', f'vg_name={self.STORAGE_VG}'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            raise RuntimeError(
                f'Cannot inspect VG {self.STORAGE_VG} storage devices: '
                f'{self._process_error(result)}')
        # Parse PV names like "/dev/mapper/reefy-sda" -> "sda".
        current_devs = set()
        for line in result.stdout.strip().splitlines():
            pv = line.strip()
            prefix = '/dev/mapper/reefy-'
            if pv.startswith(prefix):
                current_devs.add(pv[len(prefix):])
        return [d for d in desired_devices if d not in current_devs]

    def _vg_mapper_sector_size(self):
        result = subprocess.run(
            ['pvs', '--noheadings', '-o', 'pv_name',
             '-S', f'vg_name={self.STORAGE_VG}'],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(
                f'Cannot inspect VG {self.STORAGE_VG} PVs: '
                f'{self._process_error(result)}')
        mapper_paths = [line.strip() for line in result.stdout.splitlines()
                        if line.strip()]
        if not mapper_paths:
            raise RuntimeError(
                f'VG {self.STORAGE_VG} has no PVs to establish LUKS '
                f'sector size')
        return self._require_common_mapper_sector_size(mapper_paths)

    def _vg_extent_size(self):
        """Return the VG allocation extent in bytes."""
        result = subprocess.run(
            ['vgs', '--noheadings', '--nosuffix', '--units', 'b',
             '-o', 'vg_extent_size', self.STORAGE_VG],
            capture_output=True, text=True, timeout=10)
        raw_size = result.stdout.strip().lstrip('<>')
        if result.returncode != 0 or not raw_size:
            raise RuntimeError(
                f'Cannot inspect VG {self.STORAGE_VG} extent size')
        try:
            extent_size = int(Decimal(raw_size))
        except (InvalidOperation, TypeError, ValueError, OverflowError) as e:
            raise RuntimeError(
                f'Invalid VG {self.STORAGE_VG} extent size') from e
        if extent_size <= 0:
            raise RuntimeError(
                f'Invalid VG {self.STORAGE_VG} extent size')
        return extent_size

    def _extend_storage(self, new_disks):
        """Add new disks to existing VG and extend the LV + filesystem online."""
        key_part = self._find_reefy_key_partition()
        if not key_part:
            raise RuntimeError('Cannot find reefy LUKS key partition')

        targets = [(f'/dev/{dev_name}', f'reefy-{dev_name}')
                   for dev_name in new_disks]
        required_sector_size = self._vg_mapper_sector_size()
        new_pvs = self._provision_luks_stack(
            targets,
            key_part,
            _log=lambda message: log('reconciler', message),
            sector_size=required_sector_size,
            write_keyfile=False,
        )
        if len(new_pvs) != len(targets):
            raise RuntimeError(
                f'Prepared {len(new_pvs)} of {len(targets)} selected '
                f'extension devices')

        for pv_path in new_pvs:
            result = subprocess.run(
                ['pvcreate', '-ff', '-y', pv_path],
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(
                    f'PV create failed on {pv_path}: '
                    f'{self._process_error(result)}')

        # Extend VG with new PVs
        result = subprocess.run(
            ['vgextend', self.STORAGE_VG] + new_pvs,
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            raise RuntimeError(
                f'VG extend failed: {self._process_error(result)}')

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

        Returns path-free warning details for capped paths that fell back to
        default storage.
        """
        backup_paths = backup_paths or set()
        cap_warnings = {}
        metadata_names = self._lv_metadata_names()
        if metadata_names is None:
            raise ExistingVolumeUnavailableError(
                'Cannot inspect dedicated app volumes')
        thin_pool_available = self.STORAGE_POOL in metadata_names
        for vol in app_volumes:
            path = vol.get('path', '')
            uid = vol.get('uid', 0)
            if not path:
                continue
            # Per-volume thin LV: for backup paths (so reefy-backup can
            # snapshot them) AND for fair-share-capped paths (so a cap
            # can be enforced via the LV's virtualsize - e.g. Frigate
            # media, which isn't backed up but must not eat the pool).
            managed = path in backup_paths or path in self._volume_caps
            capped = path in self._volume_caps
            existing_lv = self._volume_lv_name(path) in metadata_names
            prepared = False
            expected = existing_lv
            if managed or existing_lv:
                try:
                    prepared = self._ensure_volume_lv(
                        path, allow_create=managed,
                        expect_existing=expected)
                except ExistingVolumeUnavailableError:
                    raise
                except (RuntimeError, ValueError,
                        subprocess.SubprocessError, OSError) as e:
                    if expected:
                        raise ExistingVolumeUnavailableError(
                            'Owned app volume preparation failed') from e
                    if not capped or path in backup_paths:
                        raise
                    prepared = False
                    log('mqtt',
                        'Capped volume preparation failed; '
                        'using default app storage')
            if (not prepared and path in backup_paths
                    and thin_pool_available
                    and self._dedicated_volume_mount_status(path) is not True):
                raise RuntimeError(
                    'Dedicated backup volume preparation failed')
            if capped and not prepared:
                warning = self._cap_warning_for_path(path)
                if warning:
                    cap_warnings[(warning['instance_uuid'], warning['volume'])] = (
                        warning)
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
                    log('mqtt', f'Downloading app file -> {file_path}')
                    subprocess.run(
                        ['curl', '-fSL', '-o', file_path, f['url']],
                        capture_output=True, timeout=600, check=True
                    )
                    log('mqtt', f'Downloaded: {file_path}')
                except subprocess.CalledProcessError as e:
                    log('mqtt',
                        f'Download failed for {file_path} '
                        f'(curl exit {e.returncode})')
                except subprocess.TimeoutExpired:
                    log('mqtt', f'Download timed out for {file_path}')
                except Exception as e:
                    log('mqtt',
                        f'Download failed for {file_path} '
                        f'({type(e).__name__})')

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

        return [cap_warnings[key] for key in sorted(cap_warnings)]


def main_boot_mount():
    """Boot-mount executable entrypoint (reefy-mount-volumes): mount
    per-app volumes from persisted desired-state, then exit, so docker
    (ordered after it) starts with volumes already in place."""
    try:
        Storage().boot_mount()
    except Exception as e:
        log('mqtt', f'[boot-mount] fatal: {e}')
    sys.exit(0)
