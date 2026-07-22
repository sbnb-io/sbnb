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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from io import BytesIO

from reefy import shared
from reefy.shared import _part_dev, log
from reefy.storage import Storage


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


class DataPlane:
    # Shared constants (single source in reefy.shared).
    DESIRED_STATE_PATH = shared.DESIRED_STATE_PATH
    COMPOSE_PATH = shared.COMPOSE_PATH
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
    # Sticky terminal-failure guard: persisted signature of the last
    # compose that failed non-recoverably, so neither the reconcile loop
    # nor a reconciler restart / reboot re-pulls a doomed (multi-GB) image
    # again. Cleared on a changed compose or a successful apply.
    _FAILED_SIG_PATH = '/mnt/reefy-data/state/.failed-compose-sig'
    _DEV_INJECTED_KEY_PATH = '/mnt/reefy/reefy/dev/authorized_keys'
    _FILES_ALLOWED_ROOTS = (
        '/mnt/reefy-data/apps/',
        '/mnt/reefy-data/state/',
    )

    def __init__(self, storage):
        self._storage = storage
        # Same dict object Storage reads in _ensure_volume_lv / _prepare_app_dirs.
        self._volume_caps = storage._volume_caps
        self._apply_lock = threading.Lock()
        self._pending_state = None
        # The data plane never runs control's setup(); load the MQTT
        # identity the backup config/timer need from the same files
        # reefy-mqtt-pub reads.
        cfg = shared.load_mqtt_config()
        self.broker = cfg.get('MQTT_BROKER')
        self.port = int(cfg.get('MQTT_PORT', '443'))
        self.topic_prefix = cfg.get('MQTT_TOPIC_PREFIX', 'reefy')
        self.device_uuid = shared.read_device_uuid()

    # --- Event publishing (no MQTT client; shell out to reefy-mqtt-pub) ---

    def _publish_event(self, topic_suffix, payload):
        """Publish one MQTT event via reefy-mqtt-pub (device certs).
        Non-fatal: a publish failure must never abort apply/restore work."""
        try:
            subprocess.run(['reefy-mqtt-pub', topic_suffix, json.dumps(payload)],
                           capture_output=True, timeout=20)
        except Exception as e:
            log('mqtt', f'[data-plane] event publish failed ({topic_suffix}): {e}')

    def _publish_stage(self, stage, message=''):
        self._publish_event('stage', {'stage': stage, 'message': message,
                                      'timestamp': time.time()})

    def _publish_status(self, status, message=''):
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
            extra['error'] = (error or '')[:500]
        self._publish_instance_event(iuuid, 'restore', status, extra=extra)

    def _publish_health_status(self, iuuid, status, message=None, image=None):
        extra = {}
        if message:
            extra['message'] = (message or '')[:500]
        if image:
            # Running image, reported on 'running' so the server can show
            # the version actually on the device (vs the desired one).
            extra['image'] = image
        self._publish_instance_event(iuuid, 'health', status, extra=extra)

    def _send_command_response(self, cmd_id, status=None, message=None, error=None):
        # Data-plane work is invoked over Varlink (cmd_id is always None);
        # command responses are published by the control process.
        return

    def _apply_state_command(self, payload, cmd_id=None):
        """Apply desired state with serialization (only one at a time).
        Data plane: does the real mount/compose work directly. The
        control process owns bootstrap-mode gating and publishes the
        applying/ready stages around its Varlink call."""
        if not self._apply_lock.acquire(blocking=False):
            self._pending_state = payload
            print("[mqtt] apply_state already running, queued pending state")
            return "Queued"
        try:
            applied = self._apply_state(payload)
            while self._pending_state is not None:
                pending = self._pending_state
                self._pending_state = None
                print("[mqtt] Applying queued pending state")
                applied = self._apply_state(pending)
            return bool(applied)
        finally:
            self._apply_lock.release()

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

    def _restart_instance(self, payload, cmd_id=None):
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

        compose_file = '/mnt/reefy-data/state/docker-compose.json'
        if not os.path.exists(compose_file):
            raise RuntimeError('No docker-compose.json found')

        svc_id = instance_uuid
        self._send_command_response(cmd_id, status='running',
                                    message=f'Restarting {svc_id}...')
        log('mqtt', f'Recreating instance {svc_id} with current compose config')

        result = subprocess.run(
            ['docker', 'compose', '-f', compose_file, '-p', 'state',
             'up', '-d', '--force-recreate', '--no-deps', svc_id],
            capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f'docker compose up --force-recreate failed (exit {result.returncode})')

        log('mqtt', f'Instance {svc_id} recreated')
        return f'Instance {svc_id} restarted'

    def _apply_state(self, payload):
        """Handle apply_state command — save and apply desired state."""
        state = payload.get('state', {})
        if not state:
            print("[mqtt] ERROR: Empty state in apply_state")
            return False

        # Read old state before overwriting (for diff-based cleanup)
        old_state = None
        try:
            if os.path.exists(self.DESIRED_STATE_PATH):
                with open(self.DESIRED_STATE_PATH) as f:
                    old_state = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

        # Save to persistent storage
        try:
            with open(self.DESIRED_STATE_PATH, 'w') as f:
                json.dump(state, f)
            log('mqtt', f'Saved desired state: {state}')
        except OSError as e:
            log('mqtt', f'ERROR: Failed to save desired state: {e}')
            return False

        # Data plane applies directly; the control process publishes the
        # applying/ready stages around its Varlink call.
        return self._apply_desired_state(old_state=old_state)

    def _apply_desired_state(self, old_state=None):
        """Load saved desired state and apply it (hostname, compose, proxy).
        If no desired state exists, reset hostname to MAC-based default.
        old_state: previous desired state for diff-based cleanup (None on boot).
        Returns True on success, False on failure."""
        if not os.path.exists(self.DESIRED_STATE_PATH):
            shared.set_hostname(shared.get_default_hostname())
            return True

        try:
            with open(self.DESIRED_STATE_PATH) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log('mqtt', f'ERROR: Failed to read desired state: {e}')
            return False

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
            self._storage._prepare_app_dirs(app_volumes, backup_paths=backup_paths)

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

        # Write compose file and run docker compose up
        compose = state.get('compose')
        if compose:
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
        if old_state is not None:
            self._storage._reclaim_deleted_instance_lvs(old_state, state, backup_paths)

        return True

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
                        log('mqtt', f'WiFi setup failed: {result.stdout}')
                except Exception as e:
                    log('mqtt', f'WiFi setup error: {e}')
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
        code path as the on-reconnect Reconcile so the two never diverge.
        Running under the apply lock means a state control forwards while
        the data plane is still booting serializes behind this."""
        self._dp_reconcile()

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
            def ApplyState(self, state, _more=False):
                return recon._dp_apply_state(state)

            def Reconcile(self, _more=False):
                return recon._dp_reconcile()

            def BackupNow(self, instance_uuid, _more=False):
                return recon._dp_backup_now(instance_uuid)

            def RestartInstance(self, instance_uuid, _more=False):
                return recon._dp_restart_instance(instance_uuid)

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
        # unreachable"). _boot_apply holds the apply lock, so a forwarded
        # apply serializes behind it rather than running concurrently.
        threading.Thread(target=self._boot_apply, daemon=True).start()

        log('mqtt', f'[data-plane] serving Varlink at {self.VARLINK_ADDRESS}')
        with varlink.ThreadingServer(self.VARLINK_ADDRESS, _Handler) as server:
            server.serve_forever()

    def _dp_apply_state(self, state):
        try:
            result = self._apply_state_command({'state': json.loads(state)})
            if result is False:
                return {
                    'ok': False,
                    'error': 'desired-state apply failed',
                }
            return {'ok': True, 'error': ''}
        except Exception as e:
            log('mqtt', f'[data-plane] ApplyState failed: {e}')
            return {'ok': False, 'error': str(e)[:500]}

    def _dp_reconcile(self):
        """Re-apply the data plane's own saved desired state (re-sync).
        The data plane owns desired-state.json; control calls this on
        reconnect instead of reading the file. Runs under the apply lock
        (serializes with command applies); drains any state queued while
        held. `applied` is False when there was no saved state - the apply
        then just resets the hostname to its default."""
        had_state = os.path.exists(self.DESIRED_STATE_PATH)
        if not self._apply_lock.acquire(blocking=False):
            # A command apply is already running; it covers current state.
            return {'ok': True, 'applied': had_state, 'error': ''}
        try:
            applied_ok = self._apply_desired_state()
            # No saved state resets the hostname and returns True.
            while self._pending_state is not None:
                pending = self._pending_state
                self._pending_state = None
                applied_ok = self._apply_state(pending)
            if applied_ok is False:
                return {
                    'ok': False,
                    'applied': had_state,
                    'error': 'desired-state reconcile failed',
                }
        except Exception as e:
            log('mqtt', f'[data-plane] reconcile failed: {e}')
            return {'ok': False, 'applied': had_state, 'error': str(e)[:500]}
        finally:
            self._apply_lock.release()
        return {'ok': True, 'applied': had_state, 'error': ''}

    def _dp_backup_now(self, instance_uuid):
        try:
            self._backup_now({'instance_uuid': instance_uuid}, cmd_id=None)
            return {'ok': True, 'message': 'backup started', 'error': ''}
        except Exception as e:
            log('mqtt', f'[data-plane] BackupNow failed: {e}')
            return {'ok': False, 'message': '', 'error': str(e)[:500]}

    def _dp_restart_instance(self, instance_uuid):
        try:
            self._restart_instance({'instance_uuid': instance_uuid}, cmd_id=None)
            return {'ok': True, 'error': ''}
        except Exception as e:
            log('mqtt', f'[data-plane] RestartInstance failed: {e}')
            return {'ok': False, 'error': str(e)[:500]}

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
            if reefy_data_src in acceptable_sources:
                new_disks = self._storage._find_new_storage_disks(devices)
                if not new_disks:
                    log('mqtt', f'Internal storage already at {self.REEFY_DATA_MNT}')
                    return
                self._storage._extend_storage(new_disks)
                return
        except Exception:
            pass

        key_part = self._storage._find_reefy_key_partition()
        if not key_part:
            raise RuntimeError('Cannot find reefy LUKS key partition')

        luks_key_size = 44

        subprocess.run(['modprobe', 'dm_crypt'], capture_output=True)
        subprocess.run(['modprobe', 'dm_mod'], capture_output=True)

        vg_exists = subprocess.run(
            ['vgs', self.STORAGE_VG], capture_output=True
        ).returncode == 0

        # Open existing LUKS on each storage device, or format if fresh.
        luks_pvs = []
        for dev_name in devices:
            target = f'/dev/{dev_name}'
            if not os.path.exists(target):
                if vg_exists:
                    continue  # offline disk on a restore boot — skip
                raise RuntimeError(f'Storage device {target} not found')
            luks_name = f'reefy-{dev_name}'
            mapper_path = f'/dev/mapper/{luks_name}'
            if os.path.exists(mapper_path):
                luks_pvs.append(mapper_path)
                continue

            is_luks = subprocess.run(
                ['cryptsetup', 'isLuks', target],
                capture_output=True, timeout=5).returncode == 0
            if not is_luks:
                # Fresh disk — wipe + format.
                self._storage._force_wipe_device(target)
                log('mqtt', f'LUKS formatting {target}')
                r = subprocess.run(
                    ['cryptsetup', 'luksFormat', target,
                     '--key-file', key_part, '--keyfile-size',
                     str(luks_key_size), '--batch-mode'],
                    capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    raise RuntimeError(
                        f'LUKS format failed on {target}: {r.stderr}')
            r = subprocess.run(
                ['cryptsetup', 'luksOpen', target, luks_name,
                 '--perf-submit_from_crypt_cpus',
                 '--key-file', key_part, '--keyfile-size', str(luks_key_size)],
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(
                    f'LUKS open failed on {target}: {r.stderr}')
            luks_pvs.append(mapper_path)

        if not luks_pvs:
            raise RuntimeError('No storage devices could be prepared')

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
        """Write compose JSON and run docker compose up, streaming output to logs.
        Returns True on success, False on failure."""
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
        """Bucket `docker compose up` output into a retry policy class.

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
            'pull access denied',
            'repository does not exist',
            'requested access to the resource is denied',
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
    def _failure_reason(cls, output):
        return {
            'no_space': 'out of disk space',
            'image_missing': 'image not found or access denied',
            'storage_corruption': 'docker storage error',
        }.get(cls, 'docker compose up failed')

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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
        log('mqtt', f'[data-plane] fatal: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
