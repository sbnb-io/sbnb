#!/usr/bin/env python3
"""Build a digest-pinned local OCI layout from immutable payload files.

The resulting layout is suitable for Reefy device E2E tests and can also be
uploaded by ORAS in provider CI. Payload construction, including SquashFS
creation, intentionally remains provider-specific.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


CONFIG_MEDIA_TYPE = 'application/vnd.reefy.artifact.config.v1+json'
LAYER_MEDIA_TYPE = 'application/vnd.reefy.squashfs.v1'
MANIFEST_MEDIA_TYPE = 'application/vnd.oci.image.manifest.v1+json'


def _json_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(',', ':')).encode()


def _put_bytes(root, value):
    digest = hashlib.sha256(value).hexdigest()
    target = root / 'blobs' / 'sha256' / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value)
    return f'sha256:{digest}', len(value)


def _put_file(root, source):
    hasher = hashlib.sha256()
    size = 0
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
            size += len(chunk)
    digest = hasher.hexdigest()
    target = root / 'blobs' / 'sha256' / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(source, target)
    return f'sha256:{digest}', size


def build(arguments):
    root = Path(arguments.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'oci-layout').write_text(
        json.dumps({'imageLayoutVersion': '1.0.0'}) + '\n')

    config = {
        'artifact_schema': 1,
        'kind': arguments.kind,
        'name': arguments.name,
        'version': arguments.version,
        'architecture': arguments.architecture,
        'publisher': arguments.publisher,
    }
    if arguments.kind == 'host-extension':
        if not arguments.reefy_build_id or not arguments.kernel_abi_digest:
            raise SystemExit(
                'host-extension requires build ID and kernel ABI digest')
        config.update({
            'reefy_build_id': arguments.reefy_build_id,
            'kernel_abi_digest': arguments.kernel_abi_digest,
        })
    config_digest, config_size = _put_bytes(root, _json_bytes(config))

    layers = []
    for raw_path in arguments.layer:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise SystemExit(f'layer is not a file: {path}')
        digest, size = _put_file(root, path)
        layers.append({
            'mediaType': LAYER_MEDIA_TYPE,
            'digest': digest,
            'size': size,
            'annotations': {
                'org.opencontainers.image.title': path.name,
            },
        })

    manifest = {
        'schemaVersion': 2,
        'mediaType': MANIFEST_MEDIA_TYPE,
        'artifactType': 'application/vnd.reefy.artifact.v1',
        'config': {
            'mediaType': CONFIG_MEDIA_TYPE,
            'digest': config_digest,
            'size': config_size,
        },
        'layers': layers,
        'annotations': {
            'org.opencontainers.image.title': arguments.name,
            'org.opencontainers.image.version': arguments.version,
        },
    }
    manifest_bytes = _json_bytes(manifest)
    manifest_digest, manifest_size = _put_bytes(root, manifest_bytes)
    index = {
        'schemaVersion': 2,
        'mediaType': 'application/vnd.oci.image.index.v1+json',
        'manifests': [{
            'mediaType': MANIFEST_MEDIA_TYPE,
            'digest': manifest_digest,
            'size': manifest_size,
            'artifactType': manifest['artifactType'],
            'annotations': manifest['annotations'],
        }],
    }
    (root / 'index.json').write_bytes(_json_bytes(index))
    print(f'oci://{root}@{manifest_digest}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument(
        '--kind', choices=('app', 'host-extension'), required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--architecture', default='x86_64')
    parser.add_argument('--publisher', default='reefyai')
    parser.add_argument('--reefy-build-id')
    parser.add_argument('--kernel-abi-digest')
    parser.add_argument('--layer', action='append', required=True)
    build(parser.parse_args())


if __name__ == '__main__':
    main()
