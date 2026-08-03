"""Resolve the live container that owns an app terminal.

Apps v2 desired state can arrive before a guarded v1-to-v2 migration
finishes. During that interval the desired v2 container name exists only on
paper while the legacy container deliberately remains running. Terminal and
SSH access must follow the live container, not just the newest name.
"""

from __future__ import annotations

import subprocess


def container_running(name: str) -> bool:
    if not name:
        return False
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Running}}', name],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == 'true'


def resolve_app_container(
        state: dict, instance_uuid: str, requested: str | None = None,
        is_running=container_running) -> str:
    """Prefer a live v2 primary container, then a live legacy fallback."""
    legacy = f'state-{instance_uuid}-1'
    if state.get('schema_version') != 2:
        return requested or legacy

    app = next((
        entry for entry in state.get('apps', [])
        if entry.get('instance_uuid') == instance_uuid
    ), {})
    project = app.get('project_name')
    service = app.get('primary_service') or 'app'
    preferred = f'{project}-{service}-1' if project else ''

    candidates = []
    for candidate in (preferred, requested, legacy):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if is_running(candidate):
            return candidate

    # Preserve the v2 target as the useful failure when neither generation is
    # live. Callers then report the desired container as missing rather than
    # silently attaching to an unrelated or stale name.
    return preferred or requested or legacy
