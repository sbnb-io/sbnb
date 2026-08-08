"""Digest-pinned OCI artifact cache and read-only SquashFS mounting.

This client is independent of Docker. Host extensions come only from
allow-listed Reefy repositories and carry a fixed-path activation hook inside
their digest-verified, read-only payload.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_STORE = '/mnt/reefy-data/artifacts'
DEFAULT_RUN_DIR = '/run/reefy-artifacts'
MAX_METADATA = 4 * 1024 * 1024
MAX_BLOB = 16 * 1024 * 1024 * 1024
BLOB_DOWNLOAD_ATTEMPTS = 4
BLOB_RETRY_DELAYS = (1, 2, 4)
HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
HOST_EXTENSION_REPOSITORIES = {
    'ghcr.io/reefyai/reefy-nvidia',
    'ghcr.io/reefyai/reefy-amd',
    'ghcr.io/reefyai/reefy-intel',
    'ghcr.io/reefyai/reefy-artifact-fixtures',
}
HOST_EXTENSION_NAMES = {
    'nvidia-driver',
    'amd-driver',
    'intel-accelerator',
    'e2e-host-extension',
}
HOST_ACTIVATION_HOOK = 'usr/lib/reefy/activate'
MANIFEST_ACCEPT = ', '.join((
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.oci.artifact.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
))


class ArtifactError(RuntimeError):
    pass


def _split_digest(value):
    algorithm, separator, digest = value.partition(':')
    if algorithm != 'sha256' or not separator or not HEX_DIGEST.fullmatch(digest):
        raise ArtifactError('only exact sha256 digests are accepted')
    return digest


def _digest_bytes(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f'{path}.tmp.{os.getpid()}'
    with open(temporary, 'w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path):
    try:
        with open(path) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def _transient_download_error(exception):
    current = exception
    while current is not None:
        if isinstance(current, urllib.error.HTTPError):
            return current.code in (408, 429) or current.code >= 500
        if isinstance(current, (ssl.SSLError, TimeoutError, ConnectionError)):
            return True
        if isinstance(current, urllib.error.URLError):
            return True
        if isinstance(current, OSError) and getattr(current, 'errno', None):
            return True
        current = current.__cause__ or current.__context__
    return False


def _os_release(path='/etc/os-release'):
    result = {}
    try:
        with open(path) as stream:
            for raw in stream:
                key, separator, value = raw.strip().partition('=')
                if separator:
                    result[key] = value.strip().strip('"')
    except OSError:
        pass
    return result


@contextlib.contextmanager
def _flock(path, operation=fcntl.LOCK_EX):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, operation)
        yield
    finally:
        os.close(descriptor)


class OciSource:
    def __init__(self, reference):
        source, separator, digest = reference.rpartition('@')
        if not separator:
            raise ArtifactError('artifact reference must be digest-pinned')
        self.reference = reference
        self.digest = digest
        self.digest_hex = _split_digest(digest)
        self.token = None
        if source.startswith('oci://'):
            self.local_root = source[len('oci://'):]
            if not self.local_root.startswith('/'):
                raise ArtifactError('local OCI layout must be absolute')
            self.registry = ''
            self.repository = ''
        else:
            self.local_root = ''
            self.registry, separator, self.repository = source.partition('/')
            if not separator or not self.registry or not self.repository:
                raise ArtifactError('invalid OCI registry reference')

    @property
    def repository_identity(self):
        return (f'{self.registry}/{self.repository}'
                if self.registry else 'local-e2e')

    def manifest(self):
        if self.local_root:
            return self._local_bytes(self.digest, MAX_METADATA)
        return self._request_bytes(
            f'/v2/{self.repository}/manifests/{self.digest}',
            MAX_METADATA, accept=MANIFEST_ACCEPT)

    def metadata_blob(self, digest):
        if self.local_root:
            return self._local_bytes(digest, MAX_METADATA)
        return self._request_bytes(
            f'/v2/{self.repository}/blobs/{digest}', MAX_METADATA)

    def download_blob(self, digest, destination, expected_size):
        if expected_size is not None and not 0 <= expected_size <= MAX_BLOB:
            raise ArtifactError('blob exceeds size policy')
        attempts = 1 if self.local_root else BLOB_DOWNLOAD_ATTEMPTS
        for attempt in range(1, attempts + 1):
            hasher = hashlib.sha256()
            count = 0
            try:
                if self.local_root:
                    response = open(self._local_path(digest), 'rb')
                else:
                    response = self._request(
                        f'/v2/{self.repository}/blobs/{digest}')
                with response, open(destination, 'wb') as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > MAX_BLOB:
                            raise ArtifactError('blob exceeds size policy')
                        hasher.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception as exception:
                try:
                    os.remove(destination)
                except OSError:
                    pass
                if (attempt >= attempts
                        or not _transient_download_error(exception)):
                    raise
                delay = BLOB_RETRY_DELAYS[attempt - 1]
                print(
                    'transient artifact blob download failure; '
                    f'retry {attempt + 1}/{attempts} in {delay}s: '
                    f'{exception}', file=sys.stderr, flush=True)
                time.sleep(delay)
                continue
            break
        if expected_size is not None and count != expected_size:
            try:
                os.remove(destination)
            except OSError:
                pass
            raise ArtifactError('blob size mismatch')
        if f'sha256:{hasher.hexdigest()}' != digest:
            try:
                os.remove(destination)
            except OSError:
                pass
            raise ArtifactError('blob digest mismatch')

    def _local_path(self, digest):
        return os.path.join(
            self.local_root, 'blobs', 'sha256', _split_digest(digest))

    def _local_bytes(self, digest, limit):
        try:
            with open(self._local_path(digest), 'rb') as stream:
                value = stream.read(limit + 1)
        except OSError as exception:
            raise ArtifactError('cannot read local OCI content') from exception
        if len(value) > limit:
            raise ArtifactError('OCI metadata exceeds size policy')
        return value

    def _request_bytes(self, path, limit, accept=None):
        with self._request(path, accept=accept) as response:
            value = response.read(limit + 1)
        if len(value) > limit:
            raise ArtifactError('OCI metadata exceeds size policy')
        return value

    def _request(self, path, accept=None):
        headers = {'User-Agent': 'reefy-artifacts/1'}
        if accept:
            headers['Accept'] = accept
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        request = urllib.request.Request(
            f'https://{self.registry}{path}', headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exception:
            if exception.code != 401 or self.token:
                raise ArtifactError(
                    f'registry request failed with HTTP {exception.code}') \
                    from exception
            self.token = self._anonymous_token(
                exception.headers.get('WWW-Authenticate', ''))
            return self._request(path, accept=accept)
        except OSError as exception:
            raise ArtifactError('registry request failed') from exception

    def _anonymous_token(self, challenge):
        if not challenge.lower().startswith('bearer '):
            raise ArtifactError('registry does not permit anonymous pulls')
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not fields.get('realm'):
            raise ArtifactError('invalid registry authentication challenge')
        query = urllib.parse.urlencode({
            'service': fields.get('service', self.registry),
            'scope': fields.get(
                'scope', f'repository:{self.repository}:pull'),
        })
        try:
            with urllib.request.urlopen(
                    f'{fields["realm"]}?{query}', timeout=30) as response:
                payload = json.loads(response.read(MAX_METADATA))
        except (OSError, ValueError) as exception:
            raise ArtifactError('anonymous registry token request failed') \
                from exception
        token = payload.get('token') or payload.get('access_token')
        if not token:
            raise ArtifactError('registry returned no anonymous token')
        return token


class ArtifactManager:
    def __init__(self, store=DEFAULT_STORE, run_dir=DEFAULT_RUN_DIR,
                 os_release_path='/etc/os-release'):
        self.store = store
        self.run_dir = run_dir
        self.os_release_path = os_release_path

    def prepare(self, reference, kind='app', cached_only=False):
        source = OciSource(reference)
        if (kind == 'host-extension' and not source.local_root
                and source.repository_identity not in HOST_EXTENSION_REPOSITORIES):
            raise ArtifactError('host-extension repository is not allowed')
        gc_lock = os.path.join(self.run_dir, 'locks', 'garbage-collection')
        lock_path = os.path.join(
            self.run_dir, 'locks', 'manifests', source.digest_hex)
        with _flock(gc_lock, fcntl.LOCK_SH), _flock(lock_path):
            admitted = _read_json(self._manifest_path(source.digest_hex))
            if not admitted:
                if cached_only:
                    raise ArtifactError('artifact is not admitted in cache')
                with self._preparation_slot():
                    admitted = _read_json(
                        self._manifest_path(source.digest_hex))
                    if not admitted:
                        admitted = self._admit(source, kind)
            # Admission is persistent but compatibility is boot-specific.
            # Revalidate cached host extensions against the running slot so
            # an A/B update cannot activate the previous slot's modules before
            # the cloud supplies this build's provider digest.
            self._validate_config(admitted.get('config') or {}, kind, source)
            mounts = self._mount(admitted)
            if kind == 'host-extension':
                self._activate(admitted, mounts)
            self._lease(admitted, reference)
            result = {'status': 'ready', 'digest': source.digest,
                      'mounts': mounts}
        # Retention is opportunistic. Preparation has already succeeded, so a
        # diagnostic/cleanup failure must not hold a dependent app offline.
        try:
            self.gc()
        except (ArtifactError, OSError):
            pass
        return result

    @contextlib.contextmanager
    def _preparation_slot(self):
        """Allow at most two concurrent transfer/admission transactions."""
        directory = os.path.join(self.run_dir, 'locks', 'preparation-slots')
        os.makedirs(directory, exist_ok=True)
        descriptor = None
        while descriptor is None:
            for number in range(2):
                candidate = os.open(
                    os.path.join(directory, str(number)),
                    os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
                try:
                    fcntl.flock(
                        candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    descriptor = candidate
                    break
                except BlockingIOError:
                    os.close(candidate)
            if descriptor is None:
                time.sleep(0.1)
        try:
            yield
        finally:
            os.close(descriptor)

    def _admit(self, source, kind):
        manifest_bytes = source.manifest()
        if _digest_bytes(manifest_bytes) != source.digest:
            raise ArtifactError('manifest digest mismatch')
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as exception:
            raise ArtifactError('manifest is not valid JSON') from exception
        config_descriptor = manifest.get('config') or {}
        config_digest = config_descriptor.get('digest') or ''
        config_bytes = source.metadata_blob(config_digest)
        if _digest_bytes(config_bytes) != config_digest:
            raise ArtifactError('config digest mismatch')
        try:
            config = json.loads(config_bytes)
        except ValueError as exception:
            raise ArtifactError('artifact config is not valid JSON') from exception
        self._validate_config(config, kind, source)
        layers = []
        for descriptor in manifest.get('layers') or []:
            digest = descriptor.get('digest') or ''
            digest_hex = _split_digest(digest)
            final_path = self._blob_path(digest_hex)
            blob_lock = os.path.join(
                self.run_dir, 'locks', 'blobs', digest_hex)
            with _flock(blob_lock):
                if not self._valid_blob(
                        final_path, digest, descriptor.get('size')):
                    os.makedirs(os.path.dirname(final_path), exist_ok=True)
                    partial = f'{final_path}.partial.{os.getpid()}'
                    source.download_blob(
                        digest, partial, descriptor.get('size'))
                    os.replace(partial, final_path)
            layers.append({
                'digest': digest,
                'size': descriptor.get('size'),
                'mediaType': descriptor.get('mediaType'),
                'annotations': descriptor.get('annotations') or {},
            })
        if not layers:
            raise ArtifactError('artifact has no layers')
        admitted = {
            'ref': source.reference,
            'digest': source.digest,
            'repository': source.repository_identity,
            'config': config,
            'layers': layers,
            'admitted_at': int(time.time()),
        }
        _atomic_json(self._manifest_path(source.digest_hex), admitted)
        return admitted

    def _validate_config(self, config, kind, source):
        if config.get('artifact_schema') != 1 or config.get('kind') != kind:
            raise ArtifactError('artifact config contract mismatch')
        if config.get('architecture') not in ('x86_64', 'amd64'):
            raise ArtifactError('artifact architecture mismatch')
        if kind != 'host-extension':
            return
        if config.get('name') not in HOST_EXTENSION_NAMES:
            raise ArtifactError('host-extension name is not allowed')
        if config.get('activation_hook') != HOST_ACTIVATION_HOOK:
            raise ArtifactError('host-extension activation hook is invalid')
        running = _os_release(self.os_release_path)
        if config.get('reefy_build_id') != running.get('REEFY_BUILD_ID'):
            raise ArtifactError('Reefy build identity mismatch')
        expected_abi = (config.get('kernel_abi_digest') or '').removeprefix(
            'sha256:')
        if expected_abi != running.get('REEFY_KERNEL_ABI_SHA256'):
            raise ArtifactError('kernel ABI evidence mismatch')
        if not source.local_root and config.get('publisher') != 'reefyai':
            raise ArtifactError('host-extension publisher mismatch')

    def _mount(self, admitted):
        digest_hex = _split_digest(admitted['digest'])
        root = os.path.join(self.run_dir, 'mounts', digest_hex)
        mount_options = 'loop,ro,nodev,nosuid'
        if admitted.get('config', {}).get('kind') != 'host-extension':
            mount_options += ',noexec'
        mounts = []
        for index, layer in enumerate(admitted['layers']):
            target = os.path.join(root, str(index))
            os.makedirs(target, exist_ok=True)
            if not self._is_mountpoint(target):
                source = self._blob_path(_split_digest(layer['digest']))
                result = subprocess.run(
                    ['mount', '-t', 'squashfs', '-o', mount_options,
                     source, target],
                    capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    raise ArtifactError('cannot mount artifact layer')
            mounts.append(target)
        return mounts

    def _activate(self, admitted, mounts):
        config = admitted['config']
        name = config['name']
        provider_lock = os.path.join(
            self.run_dir, 'locks', 'providers', name)
        with _flock(provider_lock):
            active_path = os.path.join(
                self.run_dir, 'active', f'{name}.json')
            active = _read_json(active_path)
            if active.get('digest') == admitted['digest']:
                return
            if active.get('digest'):
                active_admitted = _read_json(self._manifest_path(
                    _split_digest(active['digest'])))
                if self._same_payload(active_admitted, admitted):
                    return
                raise ArtifactError('different provider is active; reboot required')
            activators = [
                os.path.join(mount, HOST_ACTIVATION_HOOK)
                for mount in mounts
                if os.path.isfile(os.path.join(mount, HOST_ACTIVATION_HOOK))
            ]
            if len(activators) != 1 or not os.access(activators[0], os.X_OK):
                raise ArtifactError('provider activation hook is unavailable')
            activator = activators[0]
            environment = {
                'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                'LANG': 'C.UTF-8',
                'REEFY_ARTIFACT_DIGEST': admitted['digest'],
                'REEFY_ARTIFACT_CONFIG': json.dumps(config, sort_keys=True),
                'REEFY_ARTIFACT_MOUNTS': ':'.join(mounts),
            }
            # Downloads and mounts remain parallel, but host activation is
            # global. Provider hooks may both run depmod, load modules, and
            # publish CDI specifications. Running different vendors' hooks
            # concurrently can corrupt the shared module dependency update or
            # make modprobe observe an incomplete index.
            activation_lock = os.path.join(
                self.run_dir, 'locks', 'host-activation')
            try:
                with _flock(activation_lock):
                    # Inherit reefy-artifacts stdout and stderr. The
                    # reconciler forwards both streams through the normal
                    # Reefy log, while the boot-time one-shot service writes
                    # them to journald.
                    result = subprocess.run(
                        [activator], env=environment, timeout=300,
                        close_fds=True)
            except (OSError, subprocess.TimeoutExpired) as exception:
                raise ArtifactError(
                    f'provider activation hook failed '
                    f'({type(exception).__name__})') from exception
            if result.returncode != 0:
                raise ArtifactError(
                    f'provider activation hook failed with exit '
                    f'{result.returncode}')
            _atomic_json(active_path, {
                'name': name, 'version': config.get('version'),
                'digest': admitted['digest'], 'mounts': mounts,
                'activated_at': int(time.time()),
            })

    @staticmethod
    def _same_payload(left, right):
        """Whether two admitted manifests activate identical host bytes.

        OCI manifests may differ only in outer metadata while referring to
        the same validated config and ordered SquashFS layers. Re-running a
        host activator is unnecessary in that case and can be unsafe after
        kernel modules are loaded. Config and every mounted layer descriptor
        remain exact; only non-runtime manifest metadata is ignored.
        """
        if not left or left.get('config') != right.get('config'):
            return False

        def runtime_layers(value):
            return [
                (
                    layer.get('digest'),
                    layer.get('size'),
                    layer.get('mediaType'),
                )
                for layer in value.get('layers') or []
            ]

        return runtime_layers(left) == runtime_layers(right)

    def activate_cached_state(self, state_path):
        state = _read_json(state_path)
        results = []
        for app in state.get('apps') or []:
            for artifact in app.get('artifacts') or []:
                try:
                    results.append(self.prepare(
                        artifact['ref'], artifact.get('kind') or 'app',
                        cached_only=True))
                except ArtifactError as exception:
                    print(f'[artifacts] cached activation skipped: {exception}')
        return results

    def status(self):
        manifests = []
        directory = os.path.join(self.store, 'manifests')
        if os.path.isdir(directory):
            for entry in sorted(os.scandir(directory), key=lambda item: item.name):
                if entry.is_file() and entry.name.endswith('.json'):
                    value = _read_json(entry.path)
                    if value:
                        manifests.append(value)
        blob_bytes = 0
        blob_directory = os.path.join(self.store, 'blobs', 'sha256')
        if os.path.isdir(blob_directory):
            for entry in os.scandir(blob_directory):
                try:
                    if entry.is_file():
                        blob_bytes += entry.stat().st_size
                except OSError:
                    pass
        return {'manifests': manifests, 'blob_bytes': blob_bytes}

    @staticmethod
    def _identity(admitted):
        config = admitted.get('config') or {}
        return '/'.join((
            config.get('kind') or 'unknown',
            config.get('name') or 'unknown',
            config.get('architecture') or 'unknown',
        ))

    def _protected_digests(self, state_path):
        protected = set()
        state = _read_json(state_path)
        for app in state.get('apps') or []:
            for artifact in app.get('artifacts') or []:
                _source, separator, digest = (
                    artifact.get('ref') or '').rpartition('@')
                if separator:
                    try:
                        protected.add(_split_digest(digest))
                    except ArtifactError:
                        pass
        active_directory = os.path.join(self.run_dir, 'active')
        if os.path.isdir(active_directory):
            for entry in os.scandir(active_directory):
                active = _read_json(entry.path) if entry.is_file() else {}
                try:
                    protected.add(_split_digest(active.get('digest') or ''))
                except ArtifactError:
                    pass
        pins_directory = os.path.join(self.store, 'pins')
        if os.path.isdir(pins_directory):
            for entry in os.scandir(pins_directory):
                if entry.is_file() and HEX_DIGEST.fullmatch(entry.name):
                    protected.add(entry.name)
        return protected

    def gc(self, state_path='/mnt/reefy-data/state/desired-state-v2.json'):
        """Retain three recent versions per kind/name/architecture identity."""
        gc_lock = os.path.join(self.run_dir, 'locks', 'garbage-collection')
        with _flock(gc_lock):
            protected = self._protected_digests(state_path)
            manifests = []
            directory = os.path.join(self.store, 'manifests')
            if os.path.isdir(directory):
                for entry in os.scandir(directory):
                    if not entry.is_file() or not entry.name.endswith('.json'):
                        continue
                    value = _read_json(entry.path)
                    if not value:
                        continue
                    digest_hex = entry.name[:-5]
                    lease = _read_json(os.path.join(
                        self.store, 'leases', f'{digest_hex}.json'))
                    manifests.append({
                        'digest_hex': digest_hex,
                        'path': entry.path,
                        'value': value,
                        'identity': self._identity(value),
                        'last_used_at': lease.get(
                            'last_used_at', value.get('admitted_at', 0)),
                    })

            retained = set(protected)
            identities = {}
            for manifest in manifests:
                identities.setdefault(manifest['identity'], []).append(manifest)
            for values in identities.values():
                values.sort(
                    key=lambda item: item['last_used_at'], reverse=True)
                retained.update(
                    item['digest_hex'] for item in values[:3])

            removed_manifests = []
            for manifest in manifests:
                digest_hex = manifest['digest_hex']
                if digest_hex in retained:
                    continue
                mount_root = os.path.join(
                    self.run_dir, 'mounts', digest_hex)
                for index in reversed(range(len(
                        manifest['value'].get('layers') or []))):
                    target = os.path.join(mount_root, str(index))
                    if self._is_mountpoint(target):
                        result = subprocess.run(
                            ['umount', target], capture_output=True, timeout=60)
                        if result.returncode != 0:
                            raise ArtifactError(
                                'cannot unmount reclaimable artifact')
                shutil.rmtree(mount_root, ignore_errors=True)
                os.remove(manifest['path'])
                try:
                    os.remove(os.path.join(
                        self.store, 'leases', f'{digest_hex}.json'))
                except FileNotFoundError:
                    pass
                removed_manifests.append(digest_hex)

            referenced_blobs = set()
            for manifest in manifests:
                if manifest['digest_hex'] not in retained:
                    continue
                referenced_blobs.update(
                    _split_digest(layer.get('digest') or '')
                    for layer in manifest['value'].get('layers') or [])
            removed_bytes = 0
            blob_directory = os.path.join(self.store, 'blobs', 'sha256')
            if os.path.isdir(blob_directory):
                for entry in os.scandir(blob_directory):
                    if not entry.is_file() or entry.name in referenced_blobs:
                        continue
                    try:
                        removed_bytes += entry.stat().st_size
                        os.remove(entry.path)
                    except OSError:
                        pass
            return {
                'removed_manifests': removed_manifests,
                'removed_bytes': removed_bytes,
                'retained_manifests': len(manifests) - len(removed_manifests),
            }

    def _lease(self, admitted, reference):
        digest_hex = _split_digest(admitted['digest'])
        _atomic_json(os.path.join(
            self.store, 'leases', f'{digest_hex}.json'), {
                'digest': admitted['digest'], 'ref': reference,
                # Multiple local or cached admissions can finish within one
                # second. Preserve their real order so retention consistently
                # keeps the three newest versions.
                'last_used_at': time.time_ns(),
            })

    def _manifest_path(self, digest_hex):
        return os.path.join(self.store, 'manifests', f'{digest_hex}.json')

    def _blob_path(self, digest_hex):
        return os.path.join(self.store, 'blobs', 'sha256', digest_hex)

    @staticmethod
    def _valid_blob(path, digest, size):
        try:
            if size is not None and os.path.getsize(path) != size:
                return False
            hasher = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(chunk)
            return f'sha256:{hasher.hexdigest()}' == digest
        except OSError:
            return False

    @staticmethod
    def _is_mountpoint(path):
        try:
            result = subprocess.run(
                ['mountpoint', '-q', path], capture_output=True, timeout=10)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog='reefy-artifacts')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--ref', required=True)
    prepare.add_argument('--kind', default='app')
    cached = commands.add_parser('activate-cached')
    cached.add_argument(
        '--state', default='/mnt/reefy-data/state/desired-state-v2.json')
    commands.add_parser('status')
    gc = commands.add_parser('gc')
    gc.add_argument(
        '--state', default='/mnt/reefy-data/state/desired-state-v2.json')
    arguments = parser.parse_args(argv)
    manager = ArtifactManager()
    try:
        if arguments.command == 'prepare':
            result = manager.prepare(arguments.ref, arguments.kind)
        elif arguments.command == 'activate-cached':
            result = manager.activate_cached_state(arguments.state)
        elif arguments.command == 'status':
            result = manager.status()
        else:
            result = manager.gc(arguments.state)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ArtifactError as exception:
        print(str(exception), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
