# Health Watchdog

Sbnb Linux includes a health watchdog that periodically checks system health on bare metal hosts and auto-recovers from known failure modes.

## How It Works

The watchdog uses a **systemd timer + oneshot service**:

- `sbnb-watchdog.timer` fires every 60 seconds
- `sbnb-watchdog.service` runs the health check script once and exits
- If the script gets stuck, `TimeoutStartSec=50s` kills it
- If a previous run is still active, systemd skips the new activation (no overlap)

## Current Checks

### Tailscale Control Plane Recovery

**Problem:** Tailscale automatically removes ephemeral nodes that have been offline for an extended period. When the node comes back online, `tailscaled` gets stuck in a `"node not found"` loop where the control plane returns 404 errors indefinitely. The same happens if a node is manually removed from the Tailscale admin console. The daemon reports `BackendState: Running` so standard status checks don't detect it. This is a known upstream bug ([tailscale#12032](https://github.com/tailscale/tailscale/issues/12032)).

**Detection:** The watchdog scans `tailscaled` journal logs for `"node not found"` errors in the last 2 minutes. If 3 or more are found, it restarts `tailscaled` and `sbnb-tunnel`.

**Cooldown:** After a restart, a 3-minute cooldown prevents restart storms. Cooldown state is stored in `/run/sbnb-watchdog/` (tmpfs, cleared on reboot).

## Logs

```bash
journalctl -u sbnb-watchdog
```

When a restart is triggered, you'll see:
```
sbnb-watchdog: tailscale: 5 'node not found' errors in last 2min, restarting
```

## Manual Trigger

```bash
systemctl start sbnb-watchdog.service
```

## Timer Status

```bash
systemctl list-timers sbnb-watchdog.timer
```

## Adding New Checks

Edit `/usr/bin/sbnb-watchdog.sh`:

1. Add a `check_xxx()` function
2. Call it from the main section

```sh
check_xxx() {
    # Detect the problem
    # Take corrective action
    # Log via: logger -t sbnb-watchdog "xxx: description"
}

# Main
mkdir -p "$COOLDOWN_DIR"
check_tailscale
check_xxx
```
