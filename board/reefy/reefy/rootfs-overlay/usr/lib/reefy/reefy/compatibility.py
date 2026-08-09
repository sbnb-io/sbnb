"""Validation and loading for the Reefy firmware compatibility report."""

import json


COMPATIBILITY_PATH = '/usr/share/reefy/compatibility.json'


def _positive_revision(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_manifest(manifest):
    """Return the complete manifest when it satisfies envelope v1."""
    if not isinstance(manifest, dict) or manifest.get('manifest_version') != 1:
        raise ValueError('unsupported compatibility manifest_version')
    protocols = manifest.get('protocols')
    if not isinstance(protocols, dict):
        raise ValueError('compatibility protocols must be an object')

    desired_state = protocols.get('desired_state')
    if desired_state is not None:
        if not isinstance(desired_state, dict):
            raise ValueError('desired_state capability must be an object')
        versions = desired_state.get('versions')
        if (not isinstance(versions, list)
                or not versions
                or any(not _positive_revision(value) for value in versions)
                or len(set(versions)) != len(versions)):
            raise ValueError('desired_state versions must be unique positive integers')
        features = desired_state.get('features', {})
        if (not isinstance(features, dict)
                or any(not isinstance(name, str) or not name
                       or not _positive_revision(revision)
                       for name, revision in features.items())):
            raise ValueError('desired_state features must be positive revisions')

    for family in ('commands', 'events'):
        capabilities = protocols.get(family)
        if capabilities is None:
            continue
        if (not isinstance(capabilities, dict)
                or any(not isinstance(name, str) or not name
                       or not _positive_revision(revision)
                       for name, revision in capabilities.items())):
            raise ValueError(f'{family} must map names to positive revisions')
    return manifest


def load_manifest(path=COMPATIBILITY_PATH):
    """Load one immutable, complete compatibility snapshot from the image."""
    with open(path, encoding='utf-8') as stream:
        return validate_manifest(json.load(stream))
