"""Liveness checks and bounded recovery for Reefy infrastructure containers.

The systemd timer runs this module once per minute. A single failed probe is
not actionable because devices can briefly lose network or CPU time. Recovery
starts only after consecutive failures and is rate-limited per service.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request


FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 3
RESTART_TIMEOUT_SECONDS = 15
STATE_DIR = '/run/reefy-watchdog'
COMPOSE_PROJECTS = ('reefy-system', 'state')

CHECKS = (
    {
        'service': 'cloudflared',
        'url': 'http://127.0.0.1:20241/ready',
        'kind': 'cloudflared',
    },
    {
        'service': 'reefy-proxy',
        'url': 'http://127.0.0.1:8080/',
        'kind': 'http-response',
    },
)


def log(message):
    print(f'[reefy-watchdog] {message}', flush=True)


def probe(url, kind, timeout=PROBE_TIMEOUT_SECONDS):
    """Return True when the endpoint proves its event loop is responsive."""
    try:
        request = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if kind != 'cloudflared':
                return True
            payload = json.loads(response.read(4096))
            return (payload.get('status') == 200
                    and payload.get('readyConnections', 0) > 0)
    except urllib.error.HTTPError:
        # reefy-proxy normally rejects an unauthenticated root request with
        # 403. Any complete HTTP response proves its event loop is alive.
        return kind == 'http-response'
    except (json.JSONDecodeError, urllib.error.URLError, OSError, TimeoutError):
        return False


def find_container(service):
    """Find a system Compose service, preferring the Apps v2 project."""
    for project in COMPOSE_PROJECTS:
        try:
            result = subprocess.run(
                [
                    'docker', 'ps', '-aq',
                    '--filter',
                    f'label=com.docker.compose.project={project}',
                    '--filter', f'label=com.docker.compose.service={service}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        container = next(
            (line for line in result.stdout.splitlines() if line), None)
        if container:
            return container
    return None


def _read_int(path, default=0):
    try:
        with open(path) as stream:
            return int(stream.read().strip())
    except (OSError, ValueError):
        return default


def _write_int(path, value):
    with open(path, 'w') as stream:
        stream.write(str(value))


def _clear(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def check_service(check, state_dir=STATE_DIR, now=None):
    """Probe one service and perform bounded recovery when it stays wedged."""
    service = check['service']
    failure_file = os.path.join(state_dir, f'{service}-failures')
    cooldown_file = os.path.join(state_dir, f'{service}-cooldown')
    container = find_container(service)

    if not container:
        # The service is optional and may not be present in desired state.
        _clear(failure_file)
        return True

    if probe(check['url'], check['kind']):
        _clear(failure_file)
        return True

    failures = _read_int(failure_file) + 1
    _write_int(failure_file, failures)
    log(f'{service}: liveness probe failed ({failures}/{FAILURE_THRESHOLD})')

    if failures < FAILURE_THRESHOLD:
        return False

    now = int(time.time()) if now is None else int(now)
    cooldown_until = _read_int(cooldown_file)
    if now < cooldown_until:
        log(f'{service}: recovery suppressed by cooldown')
        return False

    log(f'{service}: restarting unresponsive container {container[:12]}')
    try:
        result = subprocess.run(
            ['docker', 'restart', '--time', '5', container],
            capture_output=True,
            text=True,
            timeout=RESTART_TIMEOUT_SECONDS,
        )
        restarted = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        restarted = False

    _write_int(cooldown_file, now + COOLDOWN_SECONDS)
    if restarted:
        _clear(failure_file)
        log(f'{service}: restart completed')
    else:
        log(f'{service}: restart failed or timed out')
    return False


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    for check in CHECKS:
        check_service(check)


if __name__ == '__main__':
    main()
