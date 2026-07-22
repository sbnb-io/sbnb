# Watchdog architecture

## Overview

Reefy uses three independent recovery layers:

| Layer | Watches | Trigger | Recovery |
|---|---|---|---|
| Hardware watchdog | PID 1, kernel progress, and shutdown | systemd stops petting the hardware watchdog | Hardware reset. |
| A/B boot watchdog | Trial firmware reaching control-plane health | A non-default boot is not confirmed within 360 seconds | Immediate sysrq reboot so the prior default slot can boot. |
| Infrastructure watchdog | Tailscale authentication and selected tunnel containers | Repeated service-specific failures | Restart only the affected tunnel unit or container, with cooldown. |

The layers do not share a timer or watchdog file descriptor. systemd owns the
hardware watchdog for the full system lifetime. The A/B watchdog uses sysrq,
and the infrastructure watchdog uses a systemd timer plus bounded service
restarts.

## Layer 1: systemd hardware watchdog

Configuration:

```ini
[Manager]
RuntimeWatchdogSec=120
RebootWatchdogSec=2min
```

When the hardware exposes `/dev/watchdog0`, systemd opens it and pets it at
half the runtime timeout. A kernel deadlock, PID 1 stall, or other failure that
stops those pings lets the device reset the machine. `RebootWatchdogSec` also
bounds a shutdown that never completes.

The kernel has its own production panic timeout, so a normal kernel panic can
reboot before the hardware watchdog. The hardware timer remains the fallback
for failures that prevent the kernel's recovery path from progressing.

Useful checks on a device:

```bash
systemctl show --property=RuntimeWatchdogUSec \
  --property=RebootWatchdogUSec \
  --property=WatchdogLastPingTimestamp
ls -l /dev/watchdog0
```

Hardware-watchdog panic tests can interrupt storage and active workloads and
should be run only on disposable lab devices.

## Layer 2: A/B boot watchdog

`reefy-boot-watchdog.service` activates its timer only when standard UEFI
`BootCurrent` differs from the first `BootOrder` entry. That state means Reefy
is testing a one-shot `BootNext` slot rather than booting the persistent
default.

The confirmation flow is:

1. Firmware consumes `BootNext` and boots the inactive slot.
2. `reefy-boot-confirm` waits up to 300 seconds.
3. It refuses confirmation immediately if `reefy-storage` or `reefy-control`
   enters the failed state.
4. When both are active, it runs `reefy-efi confirm`.
5. Confirmation commits the current slot with a fresh pair of standard UEFI
   boot entries.
6. Boot confirmation stops `reefy-boot-watchdog.service`.

If confirmation does not stop the watchdog within 360 seconds, it writes `b`
to `/proc/sysrq-trigger`. That performs an immediate software reboot. Because
`BootNext` was one-shot and has already been consumed, firmware uses the prior
persistent default on the next boot.

The A/B watchdog does not open, close, or pet `/dev/watchdog0`. This avoids a
second owner fighting systemd and avoids accidentally leaving a hardware timer
armed after a file descriptor closes.

Docker and individual apps are not boot-confirmation requirements. Storage and
the MQTT control plane are the minimum recovery surface needed to diagnose and
repair application failures.

## Layer 3: infrastructure watchdog

`reefy-watchdog.timer` starts two minutes after boot and runs every minute. It
launches a bounded oneshot service with a 50-second systemd timeout.

The shell entry point performs the Tailscale-specific log check and then runs
the Python liveness checks. State and cooldown files live in
`/run/reefy-watchdog`, so they reset on reboot.

### Tailscale authentication recovery

Some expired or deleted ephemeral Tailscale nodes can leave `tailscaled`
reporting a running backend while repeatedly logging `node not found`.

The watchdog counts those messages over the previous two minutes. After at
least three errors, it restarts `reefy-tunnel.service`, which reruns the Reefy
tunnel setup and authentication. It deliberately does not restart
`tailscaled`, because killing that daemon would drop active Tailscale SSH and
tmux sessions. A three-minute cooldown prevents repeated reauthentication.

### Tunnel-container liveness

The Python watchdog checks only optional containers that actually exist in the
Compose project:

| Service | Probe | Healthy result |
|---|---|---|
| `cloudflared` | `http://127.0.0.1:20241/ready` | JSON status 200 with at least one ready connection. |
| `reefy-proxy` | `http://127.0.0.1:8080/` | Any complete HTTP response, including the expected unauthenticated 403. |

An absent optional service is skipped and its failure count is cleared. One
failed probe increments a per-service counter; a healthy probe clears it.
After three consecutive failures, the watchdog restarts only the matching
container using:

```text
docker restart --time 5 <container>
```

The restart call is bounded to 15 seconds. Whether it succeeds or fails, a
five-minute per-service cooldown prevents a restart storm. A successful
restart clears the failure counter.

The infrastructure layer does not currently claim to monitor every app,
Docker as a whole, MQTT reachability, or general Internet connectivity. Its
scope is the known tunnel failure modes above.

## Timing

```text
boot
 |-- systemd arms and continuously pets the hardware watchdog
 |-- normal default-slot boot: A/B services exit without action
 |
 |-- trial slot boot
 |     |-- boot-confirm waits up to 300 seconds for storage + control
 |     |-- success: commit current slot and stop boot watchdog
 |     `-- no confirmation by 360 seconds: sysrq reboot
 |
 `-- 2 minutes after boot
       `-- infrastructure watchdog every minute
             |-- Tailscale log recovery, 3-minute cooldown
             `-- cloudflared/proxy probes, 3 failures, 5-minute cooldown
```

## Failure containment

- A container liveness failure cannot manipulate the hardware watchdog.
- A missing optional tunnel container does not trigger recovery.
- A transient failed HTTP probe must repeat across three timer runs.
- Container recovery does not restart unrelated user apps.
- Tailscale recovery preserves the long-running `tailscaled` process.
- The A/B timeout remains longer than the boot-confirm health window.
- All infrastructure restart subprocesses and the oneshot service are time
  bounded.

## Implementation files

| File | Responsibility |
|---|---|
| `board/reefy/reefy/rootfs-overlay/etc/systemd/system.conf.d/watchdog.conf` | systemd hardware watchdog configuration. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-boot-watchdog` | 360-second A/B trial watchdog and sysrq reboot. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-boot-confirm` | Storage and control health check plus slot confirmation. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-watchdog.sh` | Timer entry point and Tailscale log recovery. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/watchdog.py` | cloudflared and proxy probes, failure counts, and bounded restart. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/reefy-watchdog.timer` | Two-minute delay and one-minute interval. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/reefy-watchdog.service` | Bounded oneshot wrapper. |
