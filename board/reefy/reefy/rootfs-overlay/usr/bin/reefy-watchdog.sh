#!/bin/sh
# reefy-watchdog: periodic health checks for bare metal host
# Runs as a oneshot service triggered by reefy-watchdog.timer.
# Add new check functions below and call them from main.

COOLDOWN_DIR="/run/reefy-watchdog"

check_tailscale() {
    cooldown_file="$COOLDOWN_DIR/tailscale-cooldown"

    # Skip if in cooldown after a recent restart
    if [ -f "$cooldown_file" ]; then
        cooldown_until=$(cat "$cooldown_file")
        now=$(date +%s)
        if [ "$now" -lt "$cooldown_until" ]; then
            return 0
        fi
        rm -f "$cooldown_file"
    fi

    # Count "node not found" errors in the last 2 minutes of tailscaled logs.
    # Tailscale removes ephemeral nodes after extended offline periods.
    # When the node comes back, tailscaled gets stuck returning 404 while
    # still reporting BackendState "Running" (known bug: tailscale#12032).
    errors=$(journalctl -u tailscaled --since "2 min ago" --no-pager -q 2>/dev/null \
        | grep -c "node not found") || true

    if [ "$errors" -ge 3 ]; then
        logger -t reefy-watchdog "tailscale: $errors 'node not found' errors in last 2min, re-authenticating"
        # Only restart reefy-tunnel (which re-runs tailscale up with auth key).
        # Do NOT restart tailscaled — that kills its cgroup, dropping all
        # Tailscale SSH sessions and tmux servers.
        systemctl restart reefy-tunnel
        # 3 minute cooldown
        echo $(( $(date +%s) + 180 )) > "$cooldown_file"
    fi
}

# Main
mkdir -p "$COOLDOWN_DIR"
check_tailscale
# Detect running-but-unresponsive tunnel infrastructure. The Python helper
# requires three consecutive probe failures and rate-limits recovery.
PYTHONPATH=/usr/lib/reefy python3 -m reefy.watchdog
