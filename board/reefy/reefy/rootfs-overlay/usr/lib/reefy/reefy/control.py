#!/usr/bin/env python3
"""
Reefy MQTT Reconciler - Pull-based configuration management via MQTT

Open source implementation for self-hosted deployments.
Credentials are provided by user on USB flash or injected by console.
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
import threading
import traceback
import tarfile
import tempfile
import time
import ssl
import uuid as uuid_mod
from io import BytesIO

# Shared, dependency-free helpers (no paho) used across roles.
from reefy import shared
from reefy.shared import _part_dev, log
from reefy.storage import Storage

# Check if paho-mqtt is available
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("[mqtt] ERROR: paho-mqtt not installed")
    print("[mqtt] Add BR2_PACKAGE_PYTHON_PAHO_MQTT=y to defconfig")
    sys.exit(1)


class ControlPlane:
    # Config file search paths (highest priority first)
    CONFIG_PATHS = ['/mnt/reefy-data/state/mqtt.conf', '/mnt/reefy/mqtt/mqtt.conf']
    POLL_INTERVAL = 30  # seconds between config checks

    # Control <-> data-plane Varlink IPC. The control process (MQTT) calls
    # these over a unix socket; the data-plane process (reefy-reconciler)
    # executes the storage/container work, so a crash/OOM/hang there can't
    # take down control. Both keep the threaded command model.
    VARLINK_ADDRESS = 'unix:/run/reefy/reconciler.sock'
    VARLINK_INTERFACE_DIR = '/usr/share/varlink'

    # Control SERVES this interface to device-side sidecars (reefy-app-api)
    # so they can publish a cloud MQTT event through control's single
    # persistent connection - keeping the device mTLS key out of those
    # containers. Lives in its own dir (NOT /run/reefy, which holds the
    # reconciler socket) so the dir mounted into a sidecar exposes only
    # this socket, never the reconciler's.
    CONTROL_VARLINK_ADDRESS = 'unix:/run/reefy-sidecar/control.sock'
    # Topic suffixes a sidecar may publish through control. Allowlisted so
    # a compromised sidecar can't forge status/health/command-response/etc.
    CONTROL_PUBLISH_ALLOWED = ('notify',)

    def __init__(self):
        self.config = {}
        self.client = None
        self._apply_lock = threading.Lock()
        self._pending_state = None  # queued apply_state payload when lock is held
        self._subscribe_confirmed = threading.Event()
        # path -> fair-share cap (% of pool) for contained volumes. Set
        # from desired-state on each apply / boot-mount; consumed by
        # _ensure_volume_lv to size the thin LV's virtualsize so one
        # volume (e.g. Frigate media) can't consume the whole pool.
        self._volume_caps = {}
        # All on-disk state work (LUKS/LVM/XFS/volumes/mount/reclaim) lives
        # in reefy.storage.Storage; this process composes it and delegates.
        self._storage = Storage(self._volume_caps)

    def wait_for_config(self):
        """Block until MQTT configuration becomes available."""
        while True:
            for path in self.CONFIG_PATHS:
                if os.path.exists(path):
                    log('mqtt', f'Config found: {path}')
                    return
            log('mqtt', f'Waiting for config ({', '.join(self.CONFIG_PATHS)})...')
            time.sleep(self.POLL_INTERVAL)

    def setup(self):
        """Load config, find certs, initialize MQTT client."""
        self.config = self._load_config()

        if not self.config.get('MQTT_BROKER'):
            print("[mqtt] No MQTT_BROKER in config, will retry")
            return False

        self.broker = self.config.get('MQTT_BROKER')
        self.port = int(self.config.get('MQTT_PORT', '443'))
        self.hostname = os.uname().nodename
        self.topic_prefix = self.config.get('MQTT_TOPIC_PREFIX', 'reefy')

        # Determine certificate mode
        device_cert = self.config.get('MQTT_DEVICE_CERT', '/mnt/reefy-data/state/device.crt')
        device_key = self.config.get('MQTT_DEVICE_KEY', '/mnt/reefy-data/state/device.key')
        bootstrap_cert = self.config.get('MQTT_CLIENT_CERT', '/mnt/reefy/mqtt/bootstrap.crt')
        bootstrap_key = self.config.get('MQTT_CLIENT_KEY', '/mnt/reefy/mqtt/bootstrap.key')

        if os.path.exists(device_cert) and os.path.exists(device_key):
            self.mode = 'device'
            self.device_uuid = self._read_uuid()
            self.current_uuid = self.device_uuid
            self.client_cert = device_cert
            self.client_key = device_key
            log('mqtt', f'Device mode: UUID={self.device_uuid}')
        elif os.path.exists(bootstrap_cert) and os.path.exists(bootstrap_key):
            self.mode = 'bootstrap'
            self.bootstrap_uuid = self._get_or_create_bootstrap_uuid()
            self.current_uuid = self.bootstrap_uuid
            self.client_cert = bootstrap_cert
            self.client_key = bootstrap_key
            log('mqtt', f'Bootstrap mode: hostname={self.hostname} uuid={self.bootstrap_uuid}')
        else:
            print("[mqtt] No certificates found, will retry")
            log('mqtt', f'Checked device: {device_cert}')
            log('mqtt', f'Checked bootstrap: {bootstrap_cert}')
            return False

        self.ca_cert = self.config.get('MQTT_CA_CERT', '/mnt/reefy/mqtt/ca.crt')
        if not os.path.exists(self.ca_cert):
            log('mqtt', f'CA certificate not found: {self.ca_cert}, will retry')
            return False

        # Transport: websockets or tcp
        self.transport = self.config.get('MQTT_TRANSPORT', 'websockets')
        if self.transport == 'websockets':
            self.ws_path = self.config.get('MQTT_WS_PATH', '/mqtt')

        # Initialize MQTT client with stable client ID.
        # In device mode: UUID from provisioning. In bootstrap mode: bootstrap UUID.
        # Stable client ID ensures EMQX takes over the old session on reconnect
        # (discarding stale LWT) instead of firing LWT for the old connection.
        client_id = self.device_uuid if self.mode == 'device' else self.bootstrap_uuid
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                transport=self.transport,
            )
        except (AttributeError, TypeError):
            self.client = mqtt.Client(client_id=client_id, transport=self.transport)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._last_connect_time = 0
        self._last_disconnect_ts = 0

        # Set LWT — EMQX publishes this automatically on unexpected disconnect
        lwt_topic = self._get_status_topic()
        lwt_payload = json.dumps({"status": "offline", "hostname": self.hostname})
        self.client.will_set(lwt_topic, lwt_payload, qos=1, retain=True)

        return True

    def _get_status_topic(self):
        return f"{self.topic_prefix}/devices/{self.current_uuid}/status"

    def _load_config(self):
        """Load configuration from file or environment"""
        config = {}

        # Try persistent config first, then USB
        config_file = '/mnt/reefy-data/state/mqtt.conf'
        if not os.path.exists(config_file):
            config_file = '/mnt/reefy/mqtt/mqtt.conf'

        if os.path.exists(config_file):
            log('mqtt', f'Loading config from {config_file}')
            with open(config_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if '=' in line:
                            key, value = line.split('=', 1)
                            config[key.strip()] = value.strip()

        # Override with environment variables
        for key in ['MQTT_BROKER', 'MQTT_PORT', 'MQTT_TRANSPORT', 'MQTT_WS_PATH',
                    'MQTT_CA_CERT', 'MQTT_CLIENT_CERT', 'MQTT_CLIENT_KEY',
                    'MQTT_DEVICE_CERT', 'MQTT_DEVICE_KEY']:
            if key in os.environ:
                config[key] = os.environ[key]

        return config

    def _read_uuid(self):
        """Read device UUID from persistent storage"""
        uuid_file = '/mnt/reefy-data/state/device-uuid'
        if os.path.exists(uuid_file):
            with open(uuid_file, 'r') as f:
                return f.read().strip()
        return None

    BOOTSTRAP_UUID_PATH = '/tmp/bootstrap-uuid'

    def _get_or_create_bootstrap_uuid(self):
        """Get or create a bootstrap UUID for this boot session.

        Stored in /tmp so it persists across reconnects within a single
        boot (preventing duplicate DB entries) but is regenerated on
        each reboot (fresh identity). Server cleans up stale entries.
        """
        if os.path.exists(self.BOOTSTRAP_UUID_PATH):
            with open(self.BOOTSTRAP_UUID_PATH, 'r') as f:
                return f.read().strip()
        new_uuid = str(uuid_mod.uuid4())
        with open(self.BOOTSTRAP_UUID_PATH, 'w') as f:
            f.write(new_uuid)
        log('mqtt', f'Generated bootstrap UUID: {new_uuid}')
        return new_uuid

    def _get_mac(self):
        """Get MAC address of first physical NIC (same logic as boot-reefy.sh)"""
        try:
            import glob
            # Priority: wired (eth*, en*) first, then wireless (wl*)
            for pattern in ['eth*', 'en*', 'wl*']:
                ifaces = sorted(glob.glob(f'/sys/class/net/{pattern}'))
                for iface in ifaces:
                    mac_file = f'{iface}/address'
                    if os.path.exists(mac_file):
                        with open(mac_file, 'r') as f:
                            mac = f.read().strip()
                            if mac and mac != '00:00:00:00:00:00':
                                return mac
        except:
            pass
        return "unknown"

    def _generate_keypair_and_csr(self):
        """Generate RSA keypair and CSR on device.

        Private key is saved to persistent storage immediately.
        CSR is returned as PEM string for inclusion in registration message.
        """
        state_dir = '/mnt/reefy-data/state'
        key_path = os.path.join(state_dir, 'device.key')

        # Generate RSA-2048 private key
        subprocess.run([
            'openssl', 'genrsa', '-out', key_path, '2048'
        ], check=True, capture_output=True)
        os.chmod(key_path, 0o600)
        log('mqtt', f'Generated device key: {key_path}')

        # Generate CSR with hostname as CN (server will override CN with UUID)
        csr_path = '/tmp/device.csr'
        subprocess.run([
            'openssl', 'req', '-new',
            '-key', key_path,
            '-out', csr_path,
            '-subj', f'/O=Reefy/OU=Devices/CN={self.hostname}'
        ], check=True, capture_output=True)

        with open(csr_path, 'r') as f:
            csr_pem = f.read()
        os.unlink(csr_path)

        print("[mqtt] Generated CSR for registration")
        return csr_pem

    def on_connect(self, client, userdata, flags, rc, *args):
        # *args handles paho-mqtt v2 extra 'properties' parameter
        if rc != 0:
            log('mqtt', f'Connection failed: rc={rc}')
            return

        log('mqtt', f'Connected to {self.broker}:{self.port}')
        self._last_connect_time = time.time()

        try:
            if self.mode == 'bootstrap':
                # Publish online status (clears retained LWT offline message)
                status_topic = self._get_status_topic()
                client.publish(status_topic, json.dumps({
                    "status": "online", "hostname": self.hostname
                }), qos=1, retain=True)
                self._handle_bootstrap_connect(client)
            else:
                # Subscribe FIRST — before any publishes. This ensures the
                # SUBSCRIBE packet is the first thing queued after CONNACK,
                # avoiding interference from QoS 1 PUBLISH/PUBACK flows
                # during reconnect storms.
                self._subscribe_confirmed.clear()
                topic = f"{self.topic_prefix}/devices/{self.current_uuid}/commands"
                result, mid = client.subscribe(topic)
                log('mqtt', f'Subscribe sent: topic={topic}, result={result}, mid={mid}')

                # Publish online status after subscribe is queued. Include
                # /etc/os-release so the dashboard can extract IMAGE_VERSION
                # immediately — otherwise fw_version only lands ~30s later
                # when _apply_and_publish finishes and emits a full status.
                status_topic = self._get_status_topic()
                try:
                    with open('/etc/os-release') as f:
                        os_release = f.read()
                except OSError:
                    os_release = ''
                client.publish(status_topic, json.dumps({
                    "status": "online",
                    "hostname": self.hostname,
                    "hw": {"os_release": os_release},
                }), qos=1, retain=True)

                self._run_in_background(
                    self._handle_device_connect, args=(client,),
                    skip_msg="apply already running, skipping on-connect apply"
                )

                # Start subscribe watchdog — retry if SUBACK doesn't arrive
                threading.Thread(
                    target=self._subscribe_watchdog,
                    args=(client, topic),
                    daemon=True,
                ).start()
        except (BrokenPipeError, OSError) as e:
            # paho-mqtt issue #894: _sockpairW can break after network disruption,
            # causing subscribe/publish in on_connect to raise BrokenPipeError.
            # Force disconnect to trigger a clean reconnect cycle.
            log('mqtt', f'on_connect error (broken sockpair?): {e}, forcing disconnect')
            try:
                client.disconnect()
            except Exception:
                pass

    def _subscribe_watchdog(self, client, topic, timeout=5, max_retries=5):
        """Retry subscribe if SUBACK doesn't arrive within timeout.
        Runs as a daemon thread started from on_connect."""
        for attempt in range(1, max_retries + 1):
            if self._subscribe_confirmed.wait(timeout=timeout):
                return  # SUBACK received, done
            if not client.is_connected():
                return  # disconnected, on_connect will re-subscribe
            result, mid = client.subscribe(topic)
            log('mqtt', f'Subscribe retry {attempt}/{max_retries}: result={result}, mid={mid}')
        log('mqtt', f'WARNING: Subscribe not confirmed after {max_retries} retries')

    def on_disconnect(self, client, userdata, rc, *args):
        self._last_disconnect_ts = time.time()
        log('mqtt', f'Disconnected: rc={rc}, threads={threading.active_count()}')

    def on_subscribe(self, client, userdata, mid, granted_qos, *args):
        log('mqtt', f'Subscribe confirmed: mid={mid}, granted_qos={granted_qos}')
        self._subscribe_confirmed.set()

    def _handle_bootstrap_connect(self, client):
        """Bootstrap mode: Generate CSR, register, and wait for provisioning"""
        state_dir = '/mnt/reefy-data/state'
        os.makedirs(state_dir, exist_ok=True)
        os.makedirs(os.path.join(state_dir, 'lan'), exist_ok=True)

        # Subscribe to commands (unified — same topic for bootstrap and device mode)
        cmd_topic = f"{self.topic_prefix}/devices/{self.current_uuid}/commands"
        result, mid = client.subscribe(cmd_topic)
        log('mqtt', f'Subscribe sent: topic={cmd_topic}, result={result}, mid={mid}')

        # Publish registration with CSR
        self._register_device(client)
        print("[mqtt] Waiting for provisioning response...")

    def _get_hw_info(self):
        """Collect raw hardware information for server-side parsing."""
        hw = {}
        for name, path in [('cpuinfo', '/proc/cpuinfo'), ('meminfo', '/proc/meminfo'),
                          ('os_release', '/etc/os-release')]:
            try:
                with open(path) as f:
                    hw[name] = f.read()
            except OSError:
                pass
        for name, cmd in [('lspci', ['lspci', '-nn']), ('lsblk', ['lsblk', '-J']),
                          ('ip_addr', ['ip', '-j', 'addr']), ('ip_route', ['ip', '-j', 'route']),
                          ('dmidecode_mem', ['dmidecode', '-t', '17']),
                          ('efibootmgr', ['efibootmgr', '-v'])]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    hw[name] = result.stdout
            except Exception:
                pass
        # Platform identity from the kernel's DMI/SMBIOS export. Plain
        # sysfs reads (no dmidecode call); control runs as root so all
        # fields are readable. Combined into one block so it lands as a
        # single hardware-report section and parses server-side like the
        # other raw blobs. Boards that don't populate a field just omit
        # it (the file reads empty/absent).
        dmi = []
        for fname, label in (
                ('sys_vendor', 'System vendor'), ('product_name', 'Product'),
                ('board_vendor', 'Board vendor'), ('board_name', 'Board'),
                ('board_version', 'Board version'),
                ('bios_vendor', 'BIOS vendor'), ('bios_version', 'BIOS version'),
                ('bios_date', 'BIOS date'), ('chassis_type', 'Chassis type')):
            try:
                with open(f'/sys/class/dmi/id/{fname}') as f:
                    val = f.read().strip()
                if val:
                    dmi.append(f'{label}: {val}')
            except OSError:
                pass
        if dmi:
            hw['dmi_id'] = '\n'.join(dmi)
        # Thin-pool fill percentage. Backend renders an alarm badge
        # at >=80% so the user can free space before backups start
        # failing. None when this device has no thin pool (legacy
        # storage); backend treats absence as "not applicable".
        try:
            r = subprocess.run(
                ['lvs', '--noheadings', '--nosuffix', '-o', 'data_percent',
                 f'{self.STORAGE_VG}/{self.STORAGE_POOL}'],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                hw['pool_pct'] = int(float(r.stdout.strip()))
        except Exception:
            pass
        return hw

    def _read_device_password(self):
        """Read device password from persistent state."""
        try:
            with open('/mnt/reefy-data/state/device_password') as f:
                return f.read().strip()
        except OSError:
            return None

    def _register_device(self, client):
        """Generate keypair + CSR and publish registration with hardware info"""
        csr = self._generate_keypair_and_csr()

        reg_data = {
            "uuid": self.bootstrap_uuid,
            "hostname": self.hostname,
            "mac": self._get_mac(),
            "timestamp": time.time(),
            "csr": csr,
            "hw": self._get_hw_info(),
        }
        device_pw = self._read_device_password()
        if device_pw:
            reg_data["device_password"] = device_pw
        payload = json.dumps(reg_data)
        # Per-device registration topic (retained so admin can discover pending devices)
        topic = f"{self.topic_prefix}/devices/{self.current_uuid}/register"
        client.publish(topic, payload, retain=True)
        log('mqtt', f'Published registration with CSR: {self.hostname}')

    # ── Compose path ──
    COMPOSE_PATH = shared.COMPOSE_PATH

    def _handle_device_connect(self, client):
        """Device mode (subscribe already done in on_connect): ask the data
        plane to re-apply its own saved state (re-sync). The data plane
        owns desired-state.json - control never reads it. Publish ready
        when state was applied; if there's none yet (first adoption, before
        the backend's first apply_state) just go online."""
        res = self._varlink_call('Reconcile')
        if not res.get('ok'):
            log('reconciler', f"reconcile on connect failed: {res.get('error')}")
            self._publish_status('online', 'Device connected')
            return
        self._publish_status('online', 'Device connected')
        if res.get('applied'):
            self._publish_state_hash()
            shared.wait_for_tunnel_health()
            self._publish_stage('ready', 'Device ready')

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            action = payload.get('action', 'unknown')
            log('mqtt', f'Received command: {action} on {msg.topic}')
            self._handle_command(payload)
        except Exception as e:
            log('mqtt', f'Error handling message: {e}')
            import traceback
            traceback.print_exc()

    def _handle_provision(self, payload, cmd_id=None):
        """Provision device: save identity, set up storage, restart in device mode."""
        uuid = payload.get('uuid')
        certificate = payload.get('device_cert')
        if not uuid or not certificate:
            raise ValueError("Missing uuid or device_cert")

        log('mqtt', f'Received provisioning: UUID={uuid}')

        def _log_prov(msg):
            log('reconciler', msg)

        def _fail(error_msg):
            """Publish error stage so dashboard shows failure instead of spinning."""
            log('mqtt', f'Provision failed: {error_msg}')
            _log_prov(f'ERROR: {error_msg}')
            topic = f"{self.topic_prefix}/devices/{uuid}/stage"
            self.client.publish(topic, json.dumps({
                "stage": "error", "message": error_msg, "timestamp": time.time()
            }))

        try:
            return self._do_provision(payload, uuid, certificate, _log_prov)
        except Exception as e:
            _fail(str(e))
            raise

    def _do_provision(self, payload, uuid, certificate, _log_prov):
        """Internal provisioning logic — called by _handle_provision with error handling."""
        _log_prov(f"Provisioning received for UUID={uuid}")

        # Save bootstrap state before storage setup — _ensure_persistent_storage
        # may mount a real drive over /mnt/reefy-data, shadowing files generated
        # during bootstrap (device.key, device-uuid, etc.)
        bootstrap_state = '/tmp/reefy-bootstrap-state'
        state_dir_path = '/mnt/reefy-data/state'
        if os.path.isdir(state_dir_path):
            subprocess.run(['cp', '-a', state_dir_path, bootstrap_state],
                           capture_output=True, timeout=10)

        # Set up persistent storage (internal drives or USB p4)
        storage_config = payload.get('storage_config')
        self._storage._ensure_persistent_storage(storage_config, _log_prov)

        # Restore bootstrap state onto the now-mounted real storage
        if os.path.isdir(bootstrap_state):
            os.makedirs(state_dir_path, exist_ok=True)
            subprocess.run(['cp', '-a', f'{bootstrap_state}/.', state_dir_path],
                           capture_output=True, timeout=10)
            subprocess.run(['rm', '-rf', bootstrap_state],
                           capture_output=True, timeout=5)

        # Restart Docker so it sees the real /mnt/reefy-data
        # (Docker may have started on tmpfs before real storage was mounted)
        _log_prov('Restarting Docker for persistent storage...')
        subprocess.run(['systemctl', 'restart', 'docker'],
                       capture_output=True, timeout=60)

        # Remove old desired state (may have stale auth_secret from previous device)
        for stale in ['desired-state.json', 'docker-compose.json']:
            stale_path = os.path.join('/mnt/reefy-data/state', stale)
            if os.path.exists(stale_path):
                os.remove(stale_path)
                if _log_prov:
                    _log_prov(f'Removed stale {stale}')

        # Save certificate and UUID
        state_dir = '/mnt/reefy-data/state'
        cert_data = certificate.replace('\\n', '\n')

        with open(os.path.join(state_dir, 'device-uuid'), 'w') as f:
            f.write(uuid)

        cert_path = os.path.join(state_dir, 'device.crt')
        with open(cert_path, 'w') as f:
            f.write(cert_data)
        os.chmod(cert_path, 0o600)

        _log_prov('Device certificate saved, restarting in device mode')
        log('mqtt', f'Device certificate saved to {state_dir}')

        # Refresh the console banner so it reflects the adopted identity
        # immediately. The reefy-banner.path watch can miss device-uuid
        # appearing on the freshly-mounted state volume (the inotify watch
        # predates the mount), so trigger the render deterministically from
        # the code that just wrote it - otherwise the console keeps showing
        # "bootstrap (awaiting adoption)" until the next reboot.
        subprocess.run(['reefy-banner'], capture_output=True, timeout=10)

        topic = f"{self.topic_prefix}/devices/{uuid}/stage"
        self.client.publish(topic, json.dumps({
            "stage": "adopting",
            "message": "Certificate received, switching to device mode",
            "timestamp": time.time()
        }))

        # Mark the bootstrap UUID offline before we tear the session
        # down. Without this, the bootstrap record stays online forever
        # in the cloud: clean disconnect doesn't fire LWT, and we never
        # speak to that topic again under the bootstrap UUID. Retained
        # so future broker reconnects see the final state.
        bootstrap_status_topic = (
            f"{self.topic_prefix}/devices/{self.bootstrap_uuid}/status"
        )
        self.client.publish(
            bootstrap_status_topic,
            json.dumps({"status": "offline", "hostname": self.hostname}),
            qos=1, retain=True,
        )
        time.sleep(0.5)

        self.client.disconnect()
        subprocess.run(['systemctl', 'restart', 'reefy-control.service'])
        return "Provisioned"





    def _handle_wifi_scan(self, payload=None, cmd_id=None):
        """Run WiFi scan and return results via command response RPC."""
        print("[mqtt] WiFi scan requested")
        try:
            iface = shared.find_wireless_iface()
            if not iface:
                self._send_command_response(cmd_id, message=json.dumps({'networks': []}))
                return

            subprocess.run(['ip', 'link', 'set', iface, 'up'],
                           capture_output=True, timeout=5)
            time.sleep(1)

            result = subprocess.run(
                ['iw', 'dev', iface, 'scan'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                log('mqtt', f'WiFi scan failed: {result.stderr}')
                self._send_command_response(cmd_id, message=json.dumps({'networks': []}))
                return

            networks = self._parse_iw_scan(result.stdout)
            log('mqtt', f'WiFi scan found {len(networks)} networks')
            self._send_command_response(cmd_id, message=json.dumps({
                'networks': networks, 'timestamp': time.time()
            }))
        except Exception as e:
            log('mqtt', f'WiFi scan error: {e}')
            self._send_command_response(cmd_id, status='error', error=str(e))


    def _parse_iw_scan(self, output):
        """Parse iw dev scan output into structured list."""
        networks = []
        current = {}
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('BSS ') and '(' in line:
                if current.get('ssid'):
                    networks.append(current)
                current = {}
            elif line.startswith('SSID: '):
                current['ssid'] = line[6:]
            elif line.startswith('signal: '):
                try:
                    current['signal'] = float(line.split()[1])
                except (ValueError, IndexError):
                    pass
            elif line.startswith('freq: '):
                try:
                    current['freq'] = int(line.split()[1])
                except (ValueError, IndexError):
                    pass
            elif 'WPA' in line or 'RSN' in line:
                current['security'] = 'WPA'
            elif 'WEP' in line:
                current.setdefault('security', 'WEP')
        if current.get('ssid'):
            networks.append(current)
        return networks


    def _handle_wifi_status(self, payload=None, cmd_id=None):
        """Get WiFi status and publish via command response."""
        try:
            iface = shared.find_wireless_iface()
            status = {'interface': iface, 'connected': False}
            if not iface:
                self._send_command_response(cmd_id, status='success',
                    message=json.dumps(status))
                return

            # Connection info
            result = subprocess.run(['iw', 'dev', iface, 'link'],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and 'Not connected' not in result.stdout:
                status['connected'] = True
                for line in result.stdout.split('\n'):
                    line = line.strip()
                    if line.startswith('SSID:'):
                        status['ssid'] = line.split(':', 1)[1].strip()
                    elif line.startswith('signal:'):
                        try:
                            status['signal'] = int(line.split(':')[1].strip().split()[0])
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith('freq:'):
                        try:
                            status['freq'] = int(line.split(':')[1].strip())
                        except (ValueError, IndexError):
                            pass

            # IP address
            result = subprocess.run(['ip', '-4', 'addr', 'show', iface],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                if 'inet ' in line:
                    status['ip'] = line.strip().split()[1]
                    break

            # Service status
            result = subprocess.run(
                ['systemctl', 'is-active', f'wpa_supplicant@{iface}'],
                capture_output=True, text=True, timeout=5)
            status['service'] = result.stdout.strip()

            self._send_command_response(cmd_id, status='success',
                message=json.dumps(status))
        except Exception as e:
            self._send_command_response(cmd_id, status='error', error=str(e))

    def _download_and_run_bootstrap(self, bundle_url, uuid):
        """Download and execute bootstrap bundle"""
        log('mqtt', f'Downloading bootstrap from {bundle_url}')

        try:
            subprocess.run([
                'curl', '-f', '-o', '/tmp/bootstrap.tar.gz',
                '-H', f'X-Device-UUID: {uuid}',
                bundle_url
            ], check=True)

            os.makedirs('/tmp/bootstrap', exist_ok=True)
            subprocess.run([
                'tar', 'xzf', '/tmp/bootstrap.tar.gz',
                '-C', '/tmp/bootstrap'
            ], check=True)

            print("[mqtt] Running bootstrap playbook")
            result = subprocess.run(
                ['ansible-playbook', 'bootstrap.yml'],
                cwd='/tmp/bootstrap'
            )

            if result.returncode == 0:
                print("[mqtt] Bootstrap complete, restarting with device cert")
                subprocess.run(['systemctl', 'restart', 'reefy-control.service'])
            else:
                log('mqtt', f'Bootstrap failed: {result.returncode}')
        except Exception as e:
            log('mqtt', f'Bootstrap error: {e}')

    # Command dispatch table — add new commands here
    # Unified command dispatch — works in both bootstrap and device modes.
    # All commands go through _handle_command → dispatch dict → threaded handler.
    COMMAND_HANDLERS = {
        # Works in both bootstrap and device mode
        'wifi_scan': '_handle_wifi_scan',
        'wifi_status': '_handle_wifi_status',
        'update_firmware': '_update_firmware',
        'reboot': '_reboot',
        # Bootstrap → device transition
        'provision': '_handle_provision',
        # Device mode commands
        'apply_state': '_apply_state_command',
        'apply_config': '_apply_config',
        'run_playbook': '_run_playbook',
        'deploy_mcl': '_deploy_mcl',
        'update_collection': '_update_collection',
        'reset': '_reset_to_bootstrap',
        'reset_identity': '_reset_identity',
        'backup_now': '_backup_now',
        'restart_instance': '_restart_instance',
        'rotate_device_password': '_rotate_device_password',
    }

    def _send_command_response(self, cmd_id, status='success', message=None, error=None):
        """Send response to a command back to the server."""
        if cmd_id is None:
            return  # old server, no response expected
        response = {'id': cmd_id, 'status': status}
        if message:
            response['message'] = message
        if error:
            response['error'] = error
        topic = f"{self.topic_prefix}/devices/{self.current_uuid}/commands/response"
        self.client.publish(topic, json.dumps(response), qos=1)

    def _run_command(self, handler_name, payload, cmd_id):
        """Run a command handler in a thread with response reporting."""
        handler = getattr(self, handler_name)
        try:
            result = handler(payload, cmd_id=cmd_id)
            if result:
                self._send_command_response(cmd_id, status='success', message=str(result))
            else:
                self._send_command_response(cmd_id, status='success')
        except Exception as e:
            log('mqtt', f'Command {payload.get("action")} failed: {e}')
            self._send_command_response(cmd_id, status='error', error=str(e))

    def _handle_command(self, payload):
        """Unified command handler for both bootstrap and device modes."""
        action = payload.get('action')
        cmd_id = payload.get('id')

        if not action:
            log('mqtt', f'No action in message, ignoring')
            return

        handler_name = self.COMMAND_HANDLERS.get(action)
        if not handler_name:
            log('mqtt', f'Unknown action: {action}')
            self._send_command_response(cmd_id, status='error', error=f'Unknown action: {action}')
            return

        # Run in separate thread for parallel execution
        threading.Thread(
            target=self._run_command,
            args=(handler_name, payload, cmd_id),
            daemon=True
        ).start()

    def _apply_state_command(self, payload, cmd_id=None):
        """Apply desired state with serialization (only one at a time).
        Ignored in bootstrap mode — device will apply state after restart.

        Control vs data plane is handled one level down in
        _apply_desired_state: in control it forwards to the data plane
        over Varlink; in the data plane it does the mount/compose work.
        _apply_and_publish (which runs here, in control, for command
        applies) still publishes the applying/ready stages."""
        if self.mode == 'bootstrap':
            print("[mqtt] apply_state ignored in bootstrap mode (will apply after restart)")
            return "Ignored (bootstrap mode)"
        if not self._apply_lock.acquire(blocking=False):
            self._pending_state = payload
            print("[mqtt] apply_state already running, queued pending state")
            return "Queued"
        try:
            self._apply_state(payload)
            while self._pending_state is not None:
                pending = self._pending_state
                self._pending_state = None
                print("[mqtt] Applying queued pending state")
                self._apply_state(pending)
            return "State applied"
        finally:
            self._apply_lock.release()

    def _backup_now(self, payload, cmd_id=None):
        """Trigger immediate backup for an instance; forwards to the data
        plane over Varlink (which runs reefy-backup)."""
        res = self._varlink_call(
            'BackupNow', instance_uuid=payload.get('instance_uuid', ''))
        if not res.get('ok'):
            raise RuntimeError(res.get('error', 'backup failed'))
        return res.get('message', 'backup started')


    def _restart_instance(self, payload, cmd_id=None):
        """Recreate an app instance container; forwards to the data plane
        over Varlink (which runs docker compose up --force-recreate)."""
        res = self._varlink_call(
            'RestartInstance', instance_uuid=payload.get('instance_uuid', ''))
        if not res.get('ok'):
            raise RuntimeError(res.get('error', 'restart failed'))
        return f"Instance {payload.get('instance_uuid')} recreated"

    def _reset_identity(self, payload=None, cmd_id=None):
        """Remove device identity files and restart in bootstrap mode.
        Unlike factory reset, this preserves data (LUKS key, apps, volumes).
        Used by server to roll back a failed adoption."""
        print("[mqtt] Reset identity — removing device certs, restarting in bootstrap mode")
        state_dir = '/mnt/reefy-data/state'
        for f in ['device-uuid', 'device.crt', 'device.key',
                   'mqtt.conf', 'desired-state.json', 'docker-compose.json']:
            path = os.path.join(state_dir, f)
            if os.path.exists(path):
                os.remove(path)
        log('mqtt', 'Identity reset — restarting in bootstrap mode')
        self.client.disconnect()
        subprocess.run(['systemctl', 'restart', 'reefy-control.service'])
        return "Identity reset"



    def _hard_reboot(self):
        """Hard reboot via sysrq-b (bypasses init/systemd)."""
        self.client.disconnect()
        time.sleep(1)
        try:
            with open('/proc/sysrq-trigger', 'w') as f:
                f.write('b')
        except Exception:
            subprocess.run(['reboot', '-f'])

    def _reset_to_bootstrap(self, payload=None, cmd_id=None):
        """Factory reset: optional USB re-flash + secure wipe + hard reboot.

        If payload includes reflash=true and image_url, the device downloads
        a fresh personalized image and dd's it onto the USB dongle before
        wiping. This handles partition layout changes and firmware updates.

        Without reflash, performs the existing secure wipe:
        1. Overwrite LUKS key partition (p3) with random data
        2. Delete data (p4) and key (p3) partitions
        3. Hard reboot via sysrq-b

        On next boot, device has no persistent storage → tmpfs bootstrap.
        """
        reflash = payload.get('reflash', False) if payload else False
        image_url = payload.get('image_url', '') if payload else ''
        expected_size = payload.get('size', 0) if payload else 0

        log('mqtt', f'Factory reset starting (reflash={reflash})')

        usb_disk = self._storage._find_usb_disk()
        if not usb_disk:
            log('mqtt', 'WARNING: Could not find USB dongle')

        # Re-flash USB if requested. Any failure inside this block must
        # `return` instead of falling through — otherwise we'd wipe the
        # data partition for a user who asked for re-image, leaving them
        # with no data AND no new image.
        if reflash and image_url and usb_disk:
            data_dir = self._storage._find_data_dir()
            if not data_dir:
                log('mqtt', 'No persistent storage for reflash download, aborting reflash')
                self._publish_stage('error', 'No persistent storage for reflash download')
                return
            # Check free space
            stat = os.statvfs(data_dir)
            free = stat.f_bavail * stat.f_frsize
            if expected_size and free < expected_size * 1.1:
                log('mqtt', f'Not enough space for reflash: {free} free, need {expected_size}')
                self._publish_stage('error', 'Not enough space for reflash download')
                return
            download_path = os.path.join(data_dir, 'reefy-reflash.raw')

            # Download. `--retry-all-errors` is critical: bare `--retry`
            # ignores mid-stream errors (e.g. TLS bad-record-mac) which
            # we hit in prod after ~86% of a 2 GB download.
            self._publish_stage('downloading', f'Downloading firmware ({expected_size // 1048576} MB)')
            log('mqtt', f'Downloading image to {download_path}')
            result = subprocess.run(
                ['curl', '-fSL',
                 '--retry', '5',
                 '--retry-all-errors',
                 '--retry-delay', '5',
                 '-o', download_path, image_url],
                capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                log('mqtt', f'Reflash download failed: {result.stderr}')
                self._publish_stage('error', 'Reflash download failed')
                return
            if expected_size and os.path.getsize(download_path) != expected_size:
                log('mqtt', f'Size mismatch: got {os.path.getsize(download_path)}, expected {expected_size}')
                self._publish_stage('error', 'Reflash size mismatch')
                return
            # Flash USB
            self._publish_stage('flashing', 'Writing to USB — DO NOT POWER OFF')
            log('mqtt', f'Flashing {usb_disk} from {download_path}')
            result = subprocess.run(
                ['dd', f'if={download_path}', f'of={usb_disk}', 'bs=1M', 'conv=fsync'],
                capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                log('mqtt', f'dd failed: {result.stderr}')
                self._publish_stage('error', 'Reflash USB write failed')
                return
            log('mqtt', f'USB re-flashed successfully')
            # Skip partition wipe — USB is completely overwritten
            self._publish_stage('rebooting', 'USB re-flashed, rebooting')
            self._hard_reboot()
            return

        # Regular factory reset: secure wipe
        if usb_disk:
            key_part = _part_dev(usb_disk, 3)
            if os.path.exists(key_part):
                try:
                    subprocess.run(
                        ['dd', 'if=/dev/urandom', f'of={key_part}', 'bs=1M', 'count=1'],
                        capture_output=True, timeout=10,
                    )
                    log('mqtt', f'Overwritten key partition {key_part} with random data')
                except Exception as e:
                    log('mqtt', f'Key overwrite failed: {e}')

            # Delete partitions 4 and 3 (data + key)
            for part_num in [4, 3]:
                try:
                    subprocess.run(
                        ['parted', '-s', usb_disk, 'rm', str(part_num)],
                        capture_output=True, timeout=10,
                    )
                    log('mqtt', f'Deleted partition {part_num} from {usb_disk}')
                except Exception as e:
                    log('mqtt', f'Partition {part_num} delete failed: {e}')
        else:
            log('mqtt', 'WARNING: Could not find USB dongle — skipping partition wipe')

        # Remove bootstrap UUID so a fresh one is generated on next boot
        if os.path.exists(self.BOOTSTRAP_UUID_PATH):
            os.remove(self.BOOTSTRAP_UUID_PATH)
            log('mqtt', f'Removed {self.BOOTSTRAP_UUID_PATH}')

        log('mqtt', 'Factory reset complete — hard rebooting')
        self._hard_reboot()

    def _apply_config(self, payload, cmd_id=None):
        """Download and apply configuration bundle"""
        bundle_url = payload.get('bundle_url')
        version = payload.get('version', 'unknown')

        log('mqtt', f'Applying config version {version}')
        self._publish_status('applying', f'Downloading {version}')

        try:
            bundle_path = f'/mnt/reefy-data/cache/customer-{version}.tar.gz'
            os.makedirs('/mnt/reefy-data/cache', exist_ok=True)

            subprocess.run([
                'curl', '-f', '-o', bundle_path,
                '-H', f'X-Device-UUID: {self.device_uuid}',
                bundle_url
            ], check=True)

            extract_dir = f'/mnt/reefy-data/cache/customer-{version}'
            os.makedirs(extract_dir, exist_ok=True)
            subprocess.run(['tar', 'xzf', bundle_path, '-C', extract_dir], check=True)

            self._publish_status('applying', f'Running playbook {version}')

            result = subprocess.run(
                ['ansible-playbook', '-i', 'inventory.yml', 'provision.yml'],
                cwd=extract_dir,
                capture_output=True
            )

            if result.returncode == 0:
                self._publish_status('applied', f'Successfully applied {version}')
            else:
                error = result.stderr.decode()
                self._publish_status('error', f'Failed: {error}')
        except Exception as e:
            self._publish_status('error', str(e))

    def _run_playbook(self, payload, cmd_id=None):
        """Run ansible playbook delivered inline via MQTT message"""
        playbook_b64 = payload.get('playbook_b64')
        version = payload.get('version', 'inline')
        playbook_name = payload.get('playbook')

        if not playbook_b64:
            print("[mqtt] ERROR: Missing playbook_b64 in run_playbook command")
            self._publish_status('error', 'Missing playbook_b64')
            return

        log('mqtt', f'Running inline playbook version={version}')
        self._publish_status('applying', f'Running playbook {version}')

        work_dir = None
        try:
            # Decode and extract
            bundle_data = base64.b64decode(playbook_b64)
            work_dir = tempfile.mkdtemp(prefix=f'playbook-{version}-')

            tar_buf = BytesIO(bundle_data)
            with tarfile.open(fileobj=tar_buf, mode='r:gz') as tar:
                tar.extractall(path=work_dir, filter='data')

            # Find playbook file
            if not playbook_name:
                for name in ['provision.yml', 'playbook.yml', 'site.yml']:
                    if os.path.exists(os.path.join(work_dir, name)):
                        playbook_name = name
                        break

            if not playbook_name:
                # Use first .yml file found
                for f in os.listdir(work_dir):
                    if f.endswith('.yml') or f.endswith('.yaml'):
                        playbook_name = f
                        break

            if not playbook_name:
                raise FileNotFoundError("No playbook .yml file found in bundle")

            playbook_path = os.path.join(work_dir, playbook_name)
            if not os.path.exists(playbook_path):
                raise FileNotFoundError(f"Playbook not found: {playbook_name}")

            log('mqtt', f'Executing: ansible-playbook {playbook_name}')

            result = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', '-c', 'local', playbook_name],
                cwd=work_dir,
                capture_output=True,
                timeout=600
            )

            stdout = result.stdout.decode()
            stderr = result.stderr.decode()

            if result.returncode == 0:
                log('mqtt', f'Playbook completed successfully')
                # Include last few lines of output in status
                output_lines = stdout.strip().split('\n')
                summary = '\n'.join(output_lines[-5:]) if len(output_lines) > 5 else stdout.strip()
                self._publish_status('applied', f'Playbook {version} OK\n{summary}')
            else:
                log('mqtt', f'Playbook failed: rc={result.returncode}')
                error_msg = stderr.strip() or stdout.strip()
                # Truncate to fit in MQTT message
                if len(error_msg) > 500:
                    error_msg = error_msg[:500] + '...'
                self._publish_status('error', f'Playbook {version} failed: {error_msg}')

        except Exception as e:
            log('mqtt', f'Playbook error: {e}')
            self._publish_status('error', f'Playbook {version}: {e}')
        finally:
            if work_dir and os.path.exists(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)

    def _deploy_mcl(self, payload, cmd_id=None):
        """Deploy mcl config and (re)start mgmt daemon for continuous drift monitoring"""
        config_b64 = payload.get('config_b64')
        version = payload.get('version', 'inline')
        entry_point = payload.get('entry_point', 'main.mcl')

        if not config_b64:
            print("[mqtt] ERROR: Missing config_b64 in deploy_mcl command")
            self._publish_status('error', 'Missing config_b64')
            return

        log('mqtt', f'Deploying mcl config version={version}')
        self._publish_status('deploying', f'Deploying mcl {version}')

        mcl_dir = '/etc/reefy/mgmt'
        try:
            # Decode bundle
            bundle_data = base64.b64decode(config_b64)

            # Clear and recreate mcl directory
            if os.path.exists(mcl_dir):
                for item in os.listdir(mcl_dir):
                    item_path = os.path.join(mcl_dir, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.unlink(item_path)
            else:
                os.makedirs(mcl_dir, mode=0o755)

            # Extract mcl files
            tar_buf = BytesIO(bundle_data)
            with tarfile.open(fileobj=tar_buf, mode='r:gz') as tar:
                tar.extractall(path=mcl_dir, filter='data')

            # Verify entry point exists
            entry_path = os.path.join(mcl_dir, entry_point)
            if not os.path.exists(entry_path):
                # Try to find any .mcl file
                mcl_files = [f for f in os.listdir(mcl_dir) if f.endswith('.mcl')]
                if mcl_files:
                    # Rename single mcl file to main.mcl if needed
                    if len(mcl_files) == 1 and entry_point == 'main.mcl':
                        os.rename(
                            os.path.join(mcl_dir, mcl_files[0]),
                            entry_path
                        )
                        log('mqtt', f'Renamed {mcl_files[0]} -> main.mcl')
                    else:
                        raise FileNotFoundError(
                            f"Entry point '{entry_point}' not found. "
                            f"Available: {', '.join(mcl_files)}"
                        )
                else:
                    raise FileNotFoundError("No .mcl files found in bundle")

            log('mqtt', f'mcl config extracted to {mcl_dir}')

            # Check if mgmt is already running
            is_active = subprocess.run(
                ['systemctl', 'is-active', '--quiet', 'reefy-mgmt.service'],
                capture_output=True,
                timeout=10
            )

            if is_active.returncode == 0:
                # mgmt is running — use 'mgmt deploy' to push new config via
                # etcd. This preserves the running graph and triggers
                # Meta:reverse cleanup for removed resources.
                result = subprocess.run(
                    ['mgmt', 'deploy', '--seeds', 'http://127.0.0.1:2379',
                     '--force', '--no-git', 'lang', entry_path],
                    capture_output=True,
                    timeout=30
                )

                if result.returncode == 0:
                    log('mqtt', f'mgmt deploy pushed config version={version}')
                    self._publish_status('deployed', f'mcl {version} deployed via mgmt deploy')
                else:
                    stderr = result.stderr.decode().strip()
                    log('mqtt', f'mgmt deploy failed: {stderr}, falling back to restart')
                    # Fallback: restart if deploy fails
                    subprocess.run(
                        ['systemctl', 'restart', 'reefy-mgmt.service'],
                        capture_output=True,
                        timeout=30
                    )
                    self._publish_status('deployed', f'mcl {version} deployed, mgmt restarted')
            else:
                # mgmt not running — start it (first deploy or after failure)
                result = subprocess.run(
                    ['systemctl', 'start', 'reefy-mgmt.service'],
                    capture_output=True,
                    timeout=30
                )

                if result.returncode == 0:
                    log('mqtt', f'mgmt daemon started with config version={version}')
                    self._publish_status('deployed', f'mcl {version} deployed, mgmt daemon started')
                else:
                    stderr = result.stderr.decode().strip()
                    log('mqtt', f'mgmt start failed: {stderr}')
                    self._publish_status('error', f'mgmt start failed: {stderr}')

        except Exception as e:
            log('mqtt', f'mcl deploy error: {e}')
            self._publish_status('error', f'mcl deploy {version}: {e}')

    DEVICE_USERS = ['root', 'reefy']

    def _rotate_device_password(self, payload=None, cmd_id=None):
        """Generate new device password, apply to system users, save to file."""
        chars = 'abcdefghjkmnpqrstuvwxyz23456789'
        password = ''.join(chars[b % len(chars)] for b in os.urandom(12))

        state_dir = '/mnt/reefy-data/state'
        os.makedirs(state_dir, exist_ok=True)
        pw_file = os.path.join(state_dir, 'device_password')
        with open(pw_file, 'w') as f:
            f.write(password)
        os.chmod(pw_file, 0o600)

        # Apply to system users
        result = subprocess.run(
            ['mkpasswd', password], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            pw_hash = result.stdout.strip()
            for user in self.DEVICE_USERS:
                subprocess.run(
                    ['sed', '-i', f's|^{user}:[^:]*:|{user}:{pw_hash}:|', '/etc/shadow'],
                    capture_output=True, timeout=5)

        # Publish new password to server
        self._publish_status('online', 'Password rotated')
        log('mqtt', 'Device password rotated')
        return 'Password rotated'

    def _reboot(self, payload=None, cmd_id=None):
        """Reboot system"""
        print("[mqtt] Reboot requested")
        self._publish_status('rebooting', 'System reboot initiated')
        subprocess.run(['systemctl', 'reboot'])

    def _update_firmware(self, payload, cmd_id=None):
        """Download firmware EFI from URL and run reefy-update."""
        url = payload.get('url')
        version = payload.get('version', 'unknown')
        if not url:
            print("[mqtt] ERROR: No URL in update_firmware command")
            return

        download_path = '/mnt/reefy-data/cache/reefy-update.efi'
        os.makedirs('/mnt/reefy-data/cache', exist_ok=True)
        try:
            self._publish_stage('updating', f'Downloading firmware {version}')
            log('mqtt', f'Downloading firmware {version} from {url}')

            # Drop any stale partial from a previous update attempt so
            # `-C -` doesn't try to append-after a different-URL's bytes.
            if os.path.exists(download_path):
                os.remove(download_path)
            # --retry-all-errors covers TLS mid-stream failures (curl
            # exit 56) that the default --retry set skips. Real devices
            # behind flaky wifi hit the same class of failures.
            # -C - + --retry means each retry resumes from the current
            # local size instead of re-downloading from byte 0.
            result = subprocess.run(
                ['curl', '-f', '-L', '-C', '-',
                 '--retry', '5', '--retry-delay', '5',
                 '--retry-all-errors', '--retry-max-time', '300',
                 '-o', download_path, url],
                capture_output=True, text=True, timeout=900,
            )
            if result.returncode != 0:
                msg = f'Firmware download failed: {result.stderr.strip()}'
                self._publish_stage('error', msg)
                log('mqtt', f'{msg}')
                return

            log('reconciler', f'{'Download complete, applying update...'}')
            self._publish_stage('updating', 'Applying firmware update')
            print("[mqtt] Firmware downloaded, running reefy-update")

            result = subprocess.run(
                ['reefy-update', download_path],
                capture_output=True, text=True, timeout=600,
            )
            # Log reefy-update output for visibility
            for line in (result.stdout or '').strip().split('\n'):
                if line.strip():
                    log('mqtt', f'[reefy-update] {line}')
            # Always surface the real exit code. reefy-update writes
            # harmless warnings to stderr (e.g. mkfs.fat's "codepage 850"
            # fallback notice), so reporting only stderr made a spurious
            # non-zero exit look like an mkfs failure. The exit code is
            # the source of truth.
            log('mqtt', f'[reefy-update] exit code {result.returncode}')
            if result.returncode != 0:
                msg = (f'reefy-update failed (exit {result.returncode}): '
                       f'{result.stderr.strip()}')
                self._publish_stage('error', msg)
                log('mqtt', f'{msg}')
                return

            log('reconciler', f'{f'Firmware updated to {version}, rebooting...'}')
            self._publish_status('rebooting', f'Firmware updated to {version}')
            log('mqtt', f'Firmware updated to {version}, rebooting')
            os.remove(download_path)
            subprocess.run(['sync'])
            time.sleep(2)
            subprocess.run(['reboot'])

        except subprocess.TimeoutExpired as e:
            cmd_name = ' '.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
            msg = f'Firmware update timed out ({e.timeout}s) during: {cmd_name}'
            self._publish_stage('error', msg)
            log('mqtt', msg)
        except Exception as e:
            msg = f'Firmware update error: {e}'
            self._publish_stage('error', msg)
            log('mqtt', f'{msg}')
        finally:
            if os.path.exists(download_path):
                os.remove(download_path)

    def _update_collection(self, payload, cmd_id=None):
        """Update Ansible collection"""
        version = payload.get('version')
        log('mqtt', f'Collection update to {version} - not implemented yet')

    # ── Desired state management ──

    DESIRED_STATE_PATH = shared.DESIRED_STATE_PATH



    def _apply_state(self, payload):
        """Handle apply_state command: forward the new desired state to the
        data plane, which owns desired-state.json (read-old / write-new)
        and the apply. Control must NOT write that file here - it would
        clobber the data plane's old_state read before the data plane
        diffs, making diff-based cleanup (LV reclaim, static-IP removal) a
        no-op because old_state would equal new_state."""
        state = payload.get('state', {})
        if not state:
            print("[mqtt] ERROR: Empty state in apply_state")
            return
        self._apply_and_publish('Applying desired state', state=state)

    def _apply_desired_state(self, state):
        """Forward an MQTT command apply to the data plane (it owns
        mount/compose/restore and persists desired-state.json, reading the
        prior file as old_state for diff-based cleanup - reclaim, static-IP
        removal). Control never touches that file. For a re-sync of the
        saved state (on connect/boot) use Reconcile, not this. Returns True
        on success, False on failure."""
        res = self._varlink_call('ApplyState', state=json.dumps(state))
        if not res.get('ok'):
            log('reconciler', f"apply failed: {res.get('error')}")
        return bool(res.get('ok'))

    # Allow-list of host-path roots the `files` primitive is allowed
    # to write to. The backend is the trust boundary, but if a bug
    # ever has it emit /etc/passwd or /root/.ssh/authorized_keys we
    # want the device to refuse rather than silently obey.
    # Roots:
    #   /mnt/reefy-data/apps/   - per-app credential templates (the
    #                             original credential-vault-mvp use)
    #   /mnt/reefy-data/state/  - system-container config (e.g.
    #                             reefy-llm-proxy credentials.json)
    _FILES_ALLOWED_ROOTS = (
        '/mnt/reefy-data/apps/',
        '/mnt/reefy-data/state/',
    )




    # Storage / data-path constants live in reefy.shared (single source);
    # re-bound here as class attrs so existing self.<C> refs keep working.
    STORAGE_VG = shared.STORAGE_VG
    STORAGE_LV = shared.STORAGE_LV
    STORAGE_POOL = shared.STORAGE_POOL
    STATE_LV = shared.STATE_LV
    LEGACY_STORAGE_LV = shared.LEGACY_STORAGE_LV
    REEFY_DATA_MNT = shared.REEFY_DATA_MNT
    REEFY_DATA_MOUNT_OPTS = shared.REEFY_DATA_MOUNT_OPTS











    # --- Control <-> data-plane Varlink IPC ---

    def _varlink_call(self, method, retries=3, startup_grace_s=30, **kwargs):
        """Control-side: invoke a data-plane Varlink method over the unix
        socket. Returns the result dict, or {'ok': False, 'error': ...}
        on failure. Retries briefly so a just-restarting data plane
        doesn't fail the command outright. Runs in the command thread, so
        a slow data op never blocks the MQTT loop.

        Boot race: reefy-control and reefy-reconciler start in parallel,
        and control connects to MQTT (firing reconcile-on-connect) a few
        seconds before the reconciler binds the Varlink socket. That
        window showed up as a spurious 'data plane unreachable: [Errno 2]
        No such file or directory' on every boot. Treat "socket not there
        / not accepting yet" (ENOENT / connection refused) as a transient
        startup condition and keep retrying up to startup_grace_s - long
        enough to outlast the reconciler's startup - instead of burning
        the short `retries` budget. Other errors keep the short budget so
        a genuine data-plane fault still surfaces quickly."""
        import varlink
        last = 'unknown error'
        start = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                with varlink.Client.new_with_address(self.VARLINK_ADDRESS) as c, \
                        c.open('io.reefy.Reconciler') as con:
                    return getattr(con, method)(**kwargs)
            except Exception as e:
                last = str(e)
                not_ready = (
                    isinstance(e, (FileNotFoundError, ConnectionError))
                    or 'No such file' in last
                    or 'refused' in last.lower())
                if not_ready and (time.monotonic() - start) < startup_grace_s:
                    time.sleep(2)
                    continue
                if attempt >= max(1, retries):
                    break
                time.sleep(2)
        return {'ok': False, 'error': f'data plane unreachable: {last}'}





    # Dev/e2e ESP-injected key path. boot-reefy-init.sh copies this
    # file into the reefy user's authorized_keys on every boot so e2e
    # tests have a stable login key. Our cloud-key reconcile must keep
    # it present alongside the user-registered keys, otherwise the
    # first state apply would lock the e2e harness out.
    _DEV_INJECTED_KEY_PATH = '/mnt/reefy/reefy/dev/authorized_keys'











    BACKUP_DIR = '/mnt/reefy-data/state/backup'
    BACKUP_CONFIG_PATH = '/mnt/reefy-data/state/backup/config.json'
    BACKUP_SERVICE = 'reefy-backup'







    def _get_state_hash(self):
        """Compute hash of the saved desired state file.
        Must match server-side compute_state_hash()."""
        if not os.path.exists(self.DESIRED_STATE_PATH):
            return None
        try:
            with open(self.DESIRED_STATE_PATH) as f:
                state = json.load(f)
            return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
        except (OSError, json.JSONDecodeError):
            return None

    def _publish_state_hash(self):
        """Publish current state hash on a dedicated retained topic.
        Server compares this to decide whether to push desired state."""
        if self.mode != 'device' or not self.client:
            return
        state_hash = self._get_state_hash() or ''
        topic = f"{self.topic_prefix}/devices/{self.device_uuid}/state_hash"
        self.client.publish(topic, state_hash, retain=True)
        if state_hash:
            log('mqtt', f'Published state_hash: {state_hash}')

    def _start_control_varlink(self):
        """Spawn the io.reefy.Control Varlink server (device mode only) so
        sidecars can publish through control's persistent MQTT client.
        Daemon thread; control's main thread owns the paho loop and
        paho publish() is thread-safe under loop_forever()."""
        if self.mode != 'device':
            return
        threading.Thread(target=self._serve_control_varlink, daemon=True,
                         name='control-varlink').start()

    def _serve_control_varlink(self):
        import varlink
        service = varlink.Service(
            vendor='Reefy', product='control', version='1',
            url='io.reefy.Control',
            interface_dir=self.VARLINK_INTERFACE_DIR)
        ctl = self

        @service.interface('io.reefy.Control')
        class _Control:
            def PublishEvent(self, suffix, payload, _more=False):
                return ctl._ctl_publish_event(suffix, payload)

        # Bind the service after class definition (a class body can't see
        # this function's local `service`) - same idiom as the reconciler.
        class _Handler(varlink.RequestHandler):
            pass
        _Handler.service = service

        sock_path = self.CONTROL_VARLINK_ADDRESS.split(':', 1)[1]
        try:
            os.makedirs(os.path.dirname(sock_path), exist_ok=True)
            if os.path.exists(sock_path):
                os.unlink(sock_path)
        except OSError:
            pass

        log('mqtt', f'serving Control Varlink at {self.CONTROL_VARLINK_ADDRESS}')
        try:
            with varlink.ThreadingServer(
                    self.CONTROL_VARLINK_ADDRESS, _Handler) as server:
                # Let the mounting sidecar (its own uid) connect.
                try:
                    os.chmod(sock_path, 0o660)
                except OSError:
                    pass
                server.serve_forever()
        except Exception as e:
            log('mqtt', f'Control Varlink server died: {e}')

    def _ctl_publish_event(self, suffix, payload):
        """Publish a sidecar event to the cloud via control's persistent
        MQTT client. `suffix` is allowlisted; `payload` is a JSON string.
        Returns the Varlink reply dict (ok/error)."""
        try:
            if suffix not in self.CONTROL_PUBLISH_ALLOWED:
                return {'ok': False, 'error': f'suffix not allowed: {suffix}'}
            if self.mode != 'device' or not self.device_uuid:
                return {'ok': False, 'error': 'not in device mode'}
            if not self.client or not self.client.is_connected():
                return {'ok': False, 'error': 'mqtt not connected'}
            try:
                json.loads(payload)
            except (TypeError, ValueError) as e:
                return {'ok': False, 'error': f'invalid json payload: {e}'}
            topic = f"{self.topic_prefix}/devices/{self.device_uuid}/{suffix}"
            info = self.client.publish(topic, payload, qos=1)
            info.wait_for_publish(timeout=10)
            if not info.is_published():
                return {'ok': False, 'error': 'publish not acked'}
            log('mqtt', f'sidecar publish -> {suffix}')
            return {'ok': True, 'error': ''}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _publish_status(self, status, message=''):
        """Publish device connectivity status. Includes hw_info on 'online' (boot)."""
        if self.mode != 'device':
            return

        topic = f"{self.topic_prefix}/devices/{self.device_uuid}/status"
        data = {
            "status": status,
            "message": message,
            "timestamp": time.time()
        }
        # Send hardware info and device password on every boot so server stays up to date
        if status == 'online':
            data["hw"] = self._get_hw_info()
            device_pw = self._read_device_password()
            if device_pw:
                data["device_password"] = device_pw
        self.client.publish(topic, json.dumps(data), retain=True)
        log('mqtt', f'Status: {status} - {message}')

    def _publish_stage(self, stage, message=''):
        """Publish adoption/setup stage on a separate topic."""
        if self.mode != 'device':
            return

        topic = f"{self.topic_prefix}/devices/{self.device_uuid}/stage"
        data = {
            "stage": stage,
            "message": message,
            "timestamp": time.time()
        }
        self.client.publish(topic, json.dumps(data))
        log('mqtt', f'Stage: {stage} - {message}')

    def _publish_instance_event(self, iuuid, action, status, extra=None):
        """Publish a per-instance lifecycle event on the unified
        instance/status channel. action categorizes the subsystem
        ('backup' | 'restore' | 'health') and status varies by action
        - see backend's _handle_instance_event for the accepted shapes."""
        if self.mode != 'device' or not self.client:
            return
        topic = f"{self.topic_prefix}/devices/{self.device_uuid}/instance/status"
        payload = {
            'instance_uuid': iuuid,
            'action': action,
            'status': status,
        }
        if extra:
            payload.update(extra)
        self.client.publish(topic, json.dumps(payload), qos=1)

    def _publish_restore_status(self, iuuid, status, archive, error=None):
        """Restore lifecycle event - status is 'started' | 'success' |
        'error'. Backend clears restore_from on success, surfaces
        progress on the dashboard activity strip."""
        extra = {'archive': archive}
        if error:
            extra['error'] = (error or '')[:500]
        self._publish_instance_event(iuuid, 'restore', status, extra=extra)

    def _publish_health_status(self, iuuid, status, message=None):
        """Container health status from the device's compose runs.
        status is 'starting' | 'running' | 'failed'. Backend persists
        'failed' to device_instances.failure_status so the warning
        badge survives page reloads, and clears it on the next
        'starting' or 'running' for the same instance.

        Frontend renders nothing on 'running' - the green-light case
        is the absence of a badge, to avoid stale "all good" hints if
        the device later goes offline. 'starting' shows a transient
        spinner, 'failed' shows a sticky orange badge."""
        extra = {}
        if message:
            extra['message'] = (message or '')[:500]
        self._publish_instance_event(iuuid, 'health', status, extra=extra)


    # Log publishing handled by reefy-log-publisher (journald → MQTT)
    # All logging goes through log() helper → print() → journald

    def _apply_and_publish(self, label='Applying desired state', state=None):
        """Unified apply: publish stages, apply state, publish ready.
        on-connect apply passes state=None (re-apply the saved file);
        an MQTT command apply passes the new desired state to forward."""
        self._publish_stage('applying', label)
        log('reconciler', f'{label}')
        if not self._apply_desired_state(state=state):
            return
        self._publish_status('online', 'Device connected')
        self._publish_state_hash()
        shared.wait_for_tunnel_health()
        self._publish_stage('ready', 'Device ready')
        log('reconciler', f'{'Device ready'}')

    def _run_in_background(self, target, args=(), skip_msg="apply already running"):
        """Run target in a background thread with _apply_lock.
        Used by offline apply, on-connect apply, and MQTT command apply.

        Drains `_pending_state` before releasing the lock so an
        apply_state command that arrived while `target` held the lock
        still runs. Without this the first adoption race strands the
        queued payload (on_connect wins the lock, server's apply_state
        queues, on_connect finishes and nobody drains -> stage stuck
        on 'applying')."""
        def _wrapper():
            if not self._apply_lock.acquire(blocking=False):
                log('mqtt', f'{skip_msg}')
                return
            try:
                target(*args)
                while self._pending_state is not None:
                    pending = self._pending_state
                    self._pending_state = None
                    log('mqtt', 'Applying queued pending state')
                    self._apply_state(pending)
            except Exception as e:
                log('mqtt', f'ERROR in background task: {e}')
                traceback.print_exc()
                log('reconciler', f'{f'ERROR: {e}'}')
            finally:
                self._apply_lock.release()
        threading.Thread(target=_wrapper, daemon=True).start()

    def _start_connection_watchdog(self):
        """Force loop_forever() exit if disconnected too long (paho bug #894 workaround).

        Paho's loop_forever() can get stuck after prolonged network outages —
        the internal sockpair breaks and the reconnect loop hangs silently.
        This watchdog detects the stuck state and forces a clean exit so the
        outer reconnect loop in run() can recreate the client from scratch.

        Spawned ONCE at startup. Runs for the process's lifetime. Must not
        `break` on a normal reconnect — that was causing a thread leak (one
        leaked daemon per outer-loop iteration, accumulating thousands over
        days because the break only fired under sustained 120s+ outages).
        `self.client` is read dynamically so client recreation in the outer
        loop is transparent to this watchdog.
        """
        def _watchdog():
            while True:
                time.sleep(30)
                if self._last_disconnect_ts > 0 and not self.client.is_connected():
                    elapsed = time.time() - self._last_disconnect_ts
                    if elapsed > 120:
                        log('mqtt', f'Watchdog: disconnected for {int(elapsed)}s, forcing loop exit')
                        try:
                            self.client.loop_stop()
                            self.client.disconnect()
                        except Exception:
                            pass
                        # Clear the ts so we don't force-exit twice before
                        # on_disconnect fires again in the new client cycle.
                        self._last_disconnect_ts = 0
        threading.Thread(target=_watchdog, daemon=True).start()

    def _start_network_monitor(self):
        """Watch for IP address changes and trigger immediate MQTT reconnect.
        Complements the existing backoff loop (1s-60s) — if ip monitor fails
        or hangs, the backoff loop still reconnects."""
        def _monitor():
            while True:
                try:
                    proc = subprocess.Popen(
                        ['ip', 'monitor', 'address'],
                        stdout=subprocess.PIPE, text=True
                    )
                    for line in proc.stdout:
                        if not self.client.is_connected():
                            try:
                                log('mqtt', f'Network change detected, triggering reconnect')
                                self.client.reconnect()
                            except Exception:
                                pass
                except Exception as e:
                    log('mqtt', f'ip monitor error: {e}')
                    time.sleep(10)
        threading.Thread(target=_monitor, daemon=True).start()

    def run(self):
        """Start MQTT client loop"""
        # Configure TLS/mTLS
        try:
            self.client.tls_set(
                ca_certs=self.ca_cert,
                certfile=self.client_cert,
                keyfile=self.client_key,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLSv1_2
            )
        except Exception as e:
            log('mqtt', f'TLS configuration failed: {e}')
            log('mqtt', f'CA: {self.ca_cert}')
            log('mqtt', f'Cert: {self.client_cert}')
            log('mqtt', f'Key: {self.client_key}')
            sys.exit(1)

        if self.transport == 'websockets':
            self.client.ws_set_options(path=self.ws_path)

        # No boot-time apply here: the data plane owns boot reconcile (it
        # applies saved desired state on its own startup, offline-capable,
        # no socket needed). Control's job at boot is to call home fast.
        # On connect, on_connect forwards the current state over Varlink.
        # (A control-side offline apply would race the data-plane socket,
        # which isn't up until the data plane finishes its own boot apply.)

        # Start network monitor thread for instant MQTT reconnect
        self._start_network_monitor()

        # Device metrics collection lives in the reefy-metrics-publisher
        # service (sibling of reefy-log-publisher) - this reconciler only
        # handles state reconciliation now.

        # Connection watchdog — single lifetime thread. Previously spawned
        # inside the reconnect loop below, which leaked one daemon per
        # iteration (thousands over days).
        self._start_connection_watchdog()

        # Serve the Control Varlink interface so device-side sidecars
        # (reefy-app-api) can publish cloud events through this single
        # persistent MQTT connection - device mTLS key never leaves here.
        self._start_control_varlink()

        # Reconnect loop: if loop_forever() exits (paho bug #894: broken sockpair
        # after network disruption), recreate the client and reconnect.
        while True:
            log('mqtt', f'Connecting to {self.broker}:{self.port} (transport={self.transport})')
            self._last_disconnect_ts = 0
            try:
                self.client.connect(self.broker, self.port, keepalive=30)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as e:
                log('mqtt', f'Connection error: {e}')

            # loop_forever() exited — this should not happen normally.
            # Recreate the client to get fresh socket pairs.
            log('mqtt', f'loop_forever() exited, recreating client in 5s')
            time.sleep(5)
            if not self.setup():
                print("[mqtt] Client setup failed, exiting")
                sys.exit(1)
            try:
                self.client.tls_set(
                    ca_certs=self.ca_cert,
                    certfile=self.client_cert,
                    keyfile=self.client_key,
                    cert_reqs=ssl.CERT_REQUIRED,
                    tls_version=ssl.PROTOCOL_TLSv1_2
                )
            except Exception as e:
                log('mqtt', f'TLS reconfiguration failed: {e}')
                sys.exit(1)
            if self.transport == 'websockets':
                self.client.ws_set_options(path=self.ws_path)


# Role entrypoints. There is no runtime role-branching here: each role
# has its own executable in /usr/bin (reefy-control /
# reefy-reconciler / reefy-mount-volumes) that imports this module and
# calls exactly one of these. The process is unambiguously its role -
# the file it ran IS the role - and `ps`/systemd show it directly.


def main_control():
    """Control plane (reefy-control): the MQTT loop. Delegates
    storage/container work to the data plane over Varlink; never does it
    locally, so a crash/OOM/hang there can't take down comms."""
    try:
        reconciler = ControlPlane()
        reconciler.wait_for_config()
        while not reconciler.setup():
            log('mqtt', f'Setup incomplete, retrying in {MQTTReconciler.POLL_INTERVAL}s...')
            time.sleep(MQTTReconciler.POLL_INTERVAL)
        reconciler.run()
    except KeyboardInterrupt:
        print("\n[mqtt] Shutting down")
        sys.exit(0)
    except Exception as e:
        log('mqtt', f'Fatal error: {e}')
        traceback.print_exc()
        sys.exit(1)






if __name__ == '__main__':
    sys.stderr.write(
        'reefy_reconciler is a module; run one of the role executables: '
        'reefy-control, reefy-reconciler, reefy-mount-volumes\n')
    sys.exit(2)
