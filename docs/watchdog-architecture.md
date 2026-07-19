# Watchdog Architecture

## Overview

Three layers of watchdog protection ensure devices recover from failures automatically:

| Layer | What it watches | Trigger | Recovery |
|-------|----------------|---------|----------|
| **Hardware watchdog** (systemd) | systemd itself, kernel | Kernel panic, deadlock, freeze | Hardware reboot in ~60-120s |
| **Boot watchdog** (reefy-boot-watchdog) | A/B firmware update health | Critical services not starting after update | sysrq reboot in 360s, falls back to old firmware |
| **App watchdog** (reefy-watchdog.timer) | Docker, MQTT, network | Service failures, connectivity loss | Restart services or reboot |

## Layer 1: Hardware Watchdog (systemd RuntimeWatchdogSec)

**Config**: `/etc/systemd/system.conf.d/watchdog.conf`

```ini
[Manager]
RuntimeWatchdogSec=120
RebootWatchdogSec=2min
```

**How it works**:
- systemd opens `/dev/watchdog0` at boot
- Pings hardware watchdog every 60s (half of 120s timeout)
- If systemd stops pinging (kernel panic, deadlock, OOM) → hardware forces reboot after 120s
- `RebootWatchdogSec=2min` — if shutdown hangs, hardware forces reboot after 2 minutes

**Verification**:
```bash
# Check watchdog is active
systemctl show | grep -i watchdog
# Expected: RuntimeWatchdogUSec=2min, WatchdogLastPingTimestamp=<recent>

# Check systemd has watchdog fd open
lsof | grep watchdog
# Expected: systemd PID 1 has /dev/watchdog0 open
```

**Testing** (lab devices only):
```bash
# Disable kernel auto-reboot on panic, then trigger panic
echo 0 > /proc/sys/kernel/panic
echo c > /proc/sysrq-trigger
# Kernel panics. Watchdog fires after ~60-120s (hardware reboot).
# With panic=10 (production), kernel auto-reboots in 10s before watchdog.
```

**Test result**: Device rebooted in ~80 seconds after kernel panic with `panic=0`. This is expected — last watchdog ping was at most 60s before the panic, plus 120s timeout = 60-120s total.

## Layer 2: Boot Watchdog (A/B Firmware Updates)

**Script**: `/usr/bin/reefy-boot-watchdog`
**Service**: `reefy-boot-watchdog.service`

**How it works**:
- Only activates when `BootCurrent != BootOrder[0]` (pending A/B firmware update)
- Starts a 360-second timer
- If `reefy-boot-confirm` doesn't stop the service within 360s → sysrq reboot
- UEFI falls back to old firmware slot (BootNext was consumed)

**No hardware watchdog manipulation** — Layer 1 (systemd) owns `/dev/watchdog0` exclusively. Boot watchdog uses sysrq as a software fallback.

**Boot confirmation flow**:
1. Device boots new firmware via BootNext
2. `reefy-boot-confirm` waits for critical services (reefy-storage, reefy-mqtt) to be active
3. If all healthy → `reefy-efi confirm` commits new slot as default
4. Stops `reefy-boot-watchdog` → timer cancelled
5. If services fail → watchdog fires → sysrq reboot → old slot

## Layer 3: App Watchdog (reefy-watchdog.timer)

**Timer**: `reefy-watchdog.timer` (every 60s, starts 2min after boot)
**Script**: `/usr/bin/reefy-watchdog.sh`

Checks app-level health every minute. In addition to the legacy Tailscale
recovery, it probes:

- `cloudflared` at `http://127.0.0.1:20241/ready`. The response must report at
  least one ready tunnel connection.
- `reefy-proxy` at `http://127.0.0.1:8080/`. Any complete HTTP response,
  including the normal unauthenticated 403, proves the proxy event loop is
  responsive.

After three consecutive failures, the watchdog restarts only the failed
container. A five-minute per-service cooldown prevents restart storms during
extended network or system pressure. Each restart is bounded so the oneshot
watchdog service cannot hang behind Docker.

## Design Decisions

### Why systemd owns the hardware watchdog

Previous design: `reefy-boot-watchdog` opened `/dev/watchdog0` directly with `exec 3>/dev/watchdog0`. This caused issues:
- Watchdog was only armed during A/B updates, not during normal operation
- No watchdog pinging during the timeout → hardware rebooted prematurely (~60s instead of 360s)
- Closing the watchdog fd without writing magic close char `V` left the watchdog ticking

New design: systemd owns the watchdog for the entire system lifetime. Boot watchdog uses sysrq (software) as a separate safety net.

### Watchdog timing

```
Boot ──────────────────────────────────────────────────────────────
  │
  ├─ t=0    systemd starts, opens /dev/watchdog0, pings every 60s
  │
  ├─ t=3s   reefy-storage.service completes
  │           ├─ reefy-boot-watchdog starts (360s timer, sysrq only)
  │           └─ reefy-boot-confirm starts (waits for services)
  │
  ├─ t=8s   reefy-mqtt connects, boot-confirm health checks pass
  │           ├─ reefy-efi confirm → commits new boot slot
  │           └─ stops reefy-boot-watchdog (timer cancelled)
  │
  ├─ t=∞    systemd keeps pinging hardware watchdog forever
  │           Any kernel hang → hardware reboot in ~60-120s
  │
  └─ Normal operation: Layer 1 always active, Layer 2 inactive,
     Layer 3 checks every 60s
```

## Files

| File | Purpose |
|------|---------|
| `etc/systemd/system.conf.d/watchdog.conf` | RuntimeWatchdogSec + RebootWatchdogSec |
| `usr/bin/reefy-boot-watchdog` | A/B update sysrq safety net |
| `usr/lib/systemd/system/reefy-boot-watchdog.service` | Systemd unit for boot watchdog |
| `usr/bin/reefy-boot-confirm` | Health check + boot slot confirmation |
| `usr/lib/systemd/system/reefy-boot-confirm.service` | Systemd unit for boot confirm |
| `usr/bin/reefy-efi` | EFI operations (fix/update/confirm/status) |
