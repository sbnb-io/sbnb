"""Unit tests for the Docker-independent Reefy artifact manager."""

import hashlib
import json
import os
import signal
import tempfile
import threading
import time
from unittest import mock

import _bootstrap  # noqa: F401
from reefy import artifacts


def _blob(root, value):
    digest = hashlib.sha256(value).hexdigest()
    path = os.path.join(root, 'blobs', 'sha256', digest)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as stream:
        stream.write(value)
    return f'sha256:{digest}'


def _fixture(kind='app', name='sample-data', version='1', layer=None):
    root = tempfile.mkdtemp()
    config = {
        'artifact_schema': 1,
        'kind': kind,
        'name': name,
        'version': version,
        'architecture': 'x86_64',
        'publisher': 'reefyai',
    }
    if kind == 'host-extension':
        config.update({
            'reefy_build_id': 'synthetic-build',
            'kernel_abi_digest': 'sha256:synthetic-abi',
            'activation_hook': 'usr/lib/reefy/activate',
        })
    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_digest = _blob(root, config_bytes)
    layer = layer or b'synthetic-squashfs-layer'
    layer_digest = _blob(root, layer)
    manifest = {
        'schemaVersion': 2,
        'config': {
            'mediaType': 'application/vnd.reefy.artifact.config.v1+json',
            'digest': config_digest,
            'size': len(config_bytes),
        },
        'layers': [{
            'mediaType': 'application/vnd.reefy.squashfs.v1',
            'digest': layer_digest,
            'size': len(layer),
        }],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    manifest_digest = _blob(root, manifest_bytes)
    return root, f'oci://{root}@{manifest_digest}', config


def _manager():
    root = tempfile.mkdtemp()
    os_release = os.path.join(root, 'os-release')
    with open(os_release, 'w') as stream:
        stream.write(
            'REEFY_BUILD_ID=synthetic-build\n'
            'REEFY_KERNEL_ABI_SHA256=synthetic-abi\n')
    return artifacts.ArtifactManager(
        store=os.path.join(root, 'store'),
        run_dir=os.path.join(root, 'run'),
        os_release_path=os_release)


def test_local_oci_artifact_is_digest_verified_admitted_and_leased():
    _, reference, _ = _fixture()
    manager = _manager()
    with mock.patch.object(
            manager, '_mount', return_value=['/synthetic/mount']):
        result = manager.prepare(reference)

    assert result['status'] == 'ready'
    status = manager.status()
    assert len(status['manifests']) == 1
    manifest = status['manifests'][0]
    assert manifest['ref'] == reference
    digest_hex = manifest['digest'].split(':', 1)[1]
    assert os.path.exists(os.path.join(
        manager.store, 'leases', f'{digest_hex}.json'))


def test_manifest_digest_mismatch_is_rejected_before_mount():
    root, reference, _ = _fixture()
    digest = reference.rsplit('@sha256:', 1)[1]
    path = os.path.join(root, 'blobs', 'sha256', digest)
    with open(path, 'ab') as stream:
        stream.write(b'tampered')
    manager = _manager()
    with mock.patch.object(manager, '_mount') as mount:
        try:
            manager.prepare(reference)
        except artifacts.ArtifactError as exception:
            assert 'manifest digest mismatch' in str(exception)
        else:
            raise AssertionError('tampered manifest was admitted')
    mount.assert_not_called()


def test_failed_blob_download_never_leaves_final_or_partial_content():
    source = artifacts.OciSource(
        'registry.example/fixtures@sha256:' + ('a' * 64))
    destination = os.path.join(tempfile.mkdtemp(), 'partial')

    class BrokenResponse:
        calls = 0

        def read(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b'incomplete'
            raise OSError('synthetic transfer interruption')

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    with mock.patch.object(source, '_request', return_value=BrokenResponse()):
        try:
            source.download_blob(
                'sha256:' + ('b' * 64), destination, expected_size=20)
        except OSError as exception:
            assert 'synthetic transfer interruption' in str(exception)
        else:
            raise AssertionError('interrupted transfer unexpectedly passed')
    assert not os.path.exists(destination)


def test_blob_digest_failure_removes_partial_content():
    root, reference, _ = _fixture()
    source = artifacts.OciSource(reference)
    destination = os.path.join(tempfile.mkdtemp(), 'partial')
    layer_digest = _blob(root, b'wrong bytes')
    try:
        source.download_blob(
            layer_digest, destination, expected_size=len(b'wrong bytes') + 1)
    except artifacts.ArtifactError as exception:
        assert 'size mismatch' in str(exception)
    else:
        raise AssertionError('wrong blob size unexpectedly passed')
    assert not os.path.exists(destination)


def test_host_extension_requires_exact_build_and_abi():
    root, reference, config = _fixture(
        kind='host-extension', name='nvidia-driver')
    config['reefy_build_id'] = 'wrong-build'
    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_digest = _blob(root, config_bytes)
    manifest_digest = reference.rsplit('@', 1)[1]
    manifest_path = os.path.join(
        root, 'blobs', 'sha256', manifest_digest.split(':', 1)[1])
    manifest = json.load(open(manifest_path))
    manifest['config']['digest'] = config_digest
    manifest['config']['size'] = len(config_bytes)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    new_manifest_digest = _blob(root, manifest_bytes)
    reference = f'oci://{root}@{new_manifest_digest}'

    manager = _manager()
    try:
        manager.prepare(reference, kind='host-extension')
    except artifacts.ArtifactError as exception:
        assert 'build identity mismatch' in str(exception)
    else:
        raise AssertionError('wrong-build host extension was admitted')


def test_provider_hook_uses_the_fixed_payload_path():
    manager = _manager()
    admitted = {
        'digest': 'sha256:' + ('a' * 64),
        'config': {
            'name': 'nvidia-driver',
            'version': '1',
            'activation_hook': 'usr/lib/reefy/activate',
        },
    }
    result = mock.Mock(returncode=0)
    with mock.patch.object(artifacts.os.path, 'isfile', return_value=True), \
            mock.patch.object(artifacts.os, 'access', return_value=True), \
            mock.patch.object(
                artifacts.subprocess, 'run', return_value=result) as run:
        manager._activate(admitted, ['/admitted/layer'])

    assert run.call_args.args[0] == [
        '/admitted/layer/usr/lib/reefy/activate']


def test_host_extension_rejects_nonstandard_activation_hook():
    root, reference, _config = _fixture(
        kind='host-extension', name='nvidia-driver')
    digest = reference.rsplit('@', 1)[1]
    manifest_path = os.path.join(
        root, 'blobs', 'sha256', digest.split(':', 1)[1])
    manifest = json.load(open(manifest_path))
    config_path = os.path.join(
        root, 'blobs', 'sha256',
        manifest['config']['digest'].split(':', 1)[1])
    config = json.load(open(config_path))
    config['activation_hook'] = 'arbitrary/root-command'
    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_digest = _blob(root, config_bytes)
    manifest['config']['digest'] = config_digest
    manifest['config']['size'] = len(config_bytes)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    reference = f'oci://{root}@{_blob(root, manifest_bytes)}'

    manager = _manager()
    try:
        manager.prepare(reference, kind='host-extension')
    except artifacts.ArtifactError as exception:
        assert 'activation hook is invalid' in str(exception)
    else:
        raise AssertionError('nonstandard activation hook was admitted')


def test_equivalent_host_extension_manifest_needs_no_reactivation():
    manager = _manager()
    config = {
        'kind': 'host-extension',
        'name': 'nvidia-driver',
        'version': '595.84',
    }
    layer = {
        'digest': 'sha256:' + ('c' * 64),
        'size': 1234,
        'mediaType': 'application/vnd.reefy.squashfs.v1',
    }
    active_admitted = {
        'digest': 'sha256:' + ('a' * 64),
        'config': config,
        'layers': [layer],
        'ref': 'synthetic-active',
    }
    requested = {
        'digest': 'sha256:' + ('b' * 64),
        'config': dict(config),
        'layers': [{**layer, 'annotations': {'rebuilt': 'true'}}],
        'ref': 'synthetic-requested',
    }
    artifacts._atomic_json(
        manager._manifest_path('a' * 64), active_admitted)
    artifacts._atomic_json(
        os.path.join(manager.run_dir, 'active', 'nvidia-driver.json'), {
            'digest': active_admitted['digest'],
            'name': 'nvidia-driver',
            'mounts': ['/active/layer'],
        })

    with mock.patch.object(artifacts.subprocess, 'run') as run:
        manager._activate(requested, ['/requested/layer'])

    run.assert_not_called()


def test_changed_host_extension_layer_still_requires_reboot():
    manager = _manager()
    config = {'kind': 'host-extension', 'name': 'nvidia-driver'}
    active_admitted = {
        'digest': 'sha256:' + ('a' * 64),
        'config': config,
        'layers': [{
            'digest': 'sha256:' + ('c' * 64),
            'size': 1234,
            'mediaType': 'application/vnd.reefy.squashfs.v1',
        }],
    }
    requested = {
        'digest': 'sha256:' + ('b' * 64),
        'config': dict(config),
        'layers': [{
            'digest': 'sha256:' + ('d' * 64),
            'size': 1234,
            'mediaType': 'application/vnd.reefy.squashfs.v1',
        }],
    }
    artifacts._atomic_json(
        manager._manifest_path('a' * 64), active_admitted)
    artifacts._atomic_json(
        os.path.join(manager.run_dir, 'active', 'nvidia-driver.json'), {
            'digest': active_admitted['digest'],
            'name': 'nvidia-driver',
            'mounts': ['/active/layer'],
        })

    with mock.patch.object(artifacts.subprocess, 'run') as run:
        try:
            manager._activate(requested, ['/requested/layer'])
        except artifacts.ArtifactError as exception:
            assert 'reboot required' in str(exception)
        else:
            raise AssertionError('changed host payload was accepted live')

    run.assert_not_called()


def test_same_digest_concurrent_requests_share_one_admission():
    _, reference, _ = _fixture()
    manager = _manager()
    original = manager._admit
    admissions = []

    def admit(*args):
        admissions.append(1)
        return original(*args)

    results = []
    with mock.patch.object(manager, '_admit', side_effect=admit), \
            mock.patch.object(manager, '_mount', return_value=['/mount']):
        threads = [threading.Thread(
            target=lambda: results.append(manager.prepare(reference)))
            for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert len(results) == 2
    assert admissions == [1]


def test_different_artifact_admissions_are_limited_to_two():
    references = [_fixture(name=f'sample-{index}')[1] for index in range(3)]
    manager = _manager()
    original = manager._admit
    guard = threading.Lock()
    release = threading.Event()
    active = 0
    maximum = 0
    results = []

    def admit(*args):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        release.wait(timeout=5)
        try:
            return original(*args)
        finally:
            with guard:
                active -= 1

    with mock.patch.object(manager, '_admit', side_effect=admit), \
            mock.patch.object(manager, '_mount', return_value=['/mount']):
        threads = [threading.Thread(
            target=lambda ref=reference: results.append(
                manager.prepare(ref))) for reference in references]
        for thread in threads:
            thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            with guard:
                if active == 2:
                    break
            time.sleep(0.01)
        try:
            with guard:
                assert active == 2
                assert maximum == 2
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=5)

    assert len(results) == 3
    assert maximum == 2


def test_artifact_flock_is_released_when_holder_is_killed():
    manager = _manager()
    lock_path = os.path.join(
        manager.run_dir, 'locks', 'manifests', 'a' * 64)
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        with artifacts._flock(lock_path):
            os.write(ready_write, b'1')
            time.sleep(60)
        os._exit(0)

    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b'1'
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            artifacts.fcntl.flock(
                descriptor, artifacts.fcntl.LOCK_EX
                | artifacts.fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
    finally:
        os.close(ready_read)


def test_host_extension_mount_is_executable_but_app_mount_is_noexec():
    manager = _manager()
    layer_digest = 'sha256:' + ('b' * 64)

    def command_for(kind):
        admitted = {
            'digest': 'sha256:' + ('a' * 64),
            'config': {'kind': kind},
            'layers': [{'digest': layer_digest}],
        }
        result = mock.Mock(returncode=0)
        with mock.patch.object(manager, '_is_mountpoint', return_value=False), \
                mock.patch.object(
                    artifacts.subprocess, 'run', return_value=result) as run:
            manager._mount(admitted)
        return run.call_args.args[0]

    assert 'noexec' not in command_for('host-extension')[4]
    assert 'noexec' in command_for('app')[4]


def test_gc_retains_three_versions_per_logical_identity():
    references = [
        _fixture(
            name='sample-data', version=str(index),
            layer=f'synthetic-layer-{index}'.encode())[1]
        for index in range(4)
    ]
    manager = _manager()
    with mock.patch.object(manager, '_mount', return_value=['/mount']):
        for reference in references:
            manager.prepare(reference)
    manager.gc(state_path=os.path.join(manager.store, 'missing-state.json'))

    status = manager.status()
    assert len(status['manifests']) == 3
    assert len(os.listdir(os.path.join(
        manager.store, 'blobs', 'sha256'))) == 3
