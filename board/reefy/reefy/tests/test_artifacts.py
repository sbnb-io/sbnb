"""Unit tests for the Docker-independent Reefy artifact manager."""

import hashlib
import json
import os
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


def test_artifact_supplied_hook_is_never_executed():
    manager = _manager()
    admitted = {
        'digest': 'sha256:' + ('a' * 64),
        'config': {
            'name': 'nvidia-driver',
            'version': '1',
            'activation_hook': 'malicious/from-artifact',
        },
    }
    result = mock.Mock(returncode=0)
    with mock.patch.object(artifacts.os.path, 'isfile', return_value=True), \
            mock.patch.object(
                artifacts.subprocess, 'run', return_value=result) as run:
        manager._activate(admitted, ['/admitted/layer'])

    assert run.call_args.args[0] == [
        '/usr/lib/reefy/activators/nvidia-driver']


def test_nvidia_activator_replaces_the_runtime_cdi_definition():
    path = os.path.join(
        os.path.dirname(__file__), '..', 'rootfs-overlay', 'usr', 'lib',
        'reefy', 'activators', 'nvidia-driver')
    with open(path, encoding='utf-8') as stream:
        script = stream.read()

    assert '--output=/run/cdi/nvidia.tmp.yaml' in script
    assert '--driver-root="$DRIVER_ROOT" --dev-root=/' in script
    assert 'DRIVER_ROOT=/run/reefy-artifacts/providers/nvidia-driver' in script
    assert ('mount -o remount,bind,ro,nodev,nosuid '
            '"$source" "$target"') in script
    assert ('mount -o remount,bind,ro,nodev,nosuid "$target"'
            not in script)
    assert '"$DRIVER_ROOT/usr/share/vulkan/implicit_layer.d"' in script
    assert '"$DRIVER_ROOT/usr/share/egl/egl_external_platform.d"' in script
    assert '"$DRIVER_ROOT/usr/share/nvidia"' in script
    assert 'mknod -m 666 /dev/nvidia-modeset c "$NVIDIA_MAJOR" 254' in script
    assert 'mv -f /run/cdi/nvidia.tmp.yaml /run/cdi/nvidia.yaml' in script
    assert 'rm -f /etc/cdi/nvidia.yaml' in script
    assert '--output=/etc/cdi/nvidia.yaml' not in script


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
