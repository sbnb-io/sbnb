#!/bin/sh
# Apply MAC-derived hostname. Fired by udev on physical NIC add + by a
# 180s OnBootSec timer backstop, and run synchronously as part of
# reefy-hostname.service (which reefy-control depends on — so boot blocks
# here until a hostname is set OR until we time out). Always exits 0:
# downstream services must still start even if we can't find a NIC —
# they'll just register with whatever hostname we managed to set (or
# "buildroot" as last resort).
#
# Blocks up to NIC_WAIT_S waiting for a physical NIC to appear. On
# QEMU we've seen virtio_net probes take 55s, so 120s buys comfortable
# headroom before falling back to a machine-id-derived hostname (still
# better than "buildroot" because it's unique per-device).

NIC_WAIT_S=120
POLL_INTERVAL_S=1

# Use /proc/uptime (monotonic) rather than wall clock — boot time
# commonly jumps backwards when chronyd/ntpd first sync, which would
# make a wall-clock deadline fire prematurely or never.
uptime_s() {
    awk '{printf "%d\n", $1}' /proc/uptime
}
deadline=$(( $(uptime_s) + NIC_WAIT_S ))

NAME=""
while :; do
    OUT=$(reefy-derive-hostname 2>/dev/null || true)
    case "${OUT}" in
        reefy-mid-*|'')
            # No physical NIC yet (or /etc/machine-id missing). Keep waiting
            # unless we've run out of budget; then accept whatever we got.
            if [ "$(uptime_s)" -ge "${deadline}" ]; then
                NAME="${OUT}"
                break
            fi
            sleep "${POLL_INTERVAL_S}"
            continue
            ;;
        reefy-*)
            NAME="${OUT}"
            break
            ;;
        *)
            # reefy-derive-hostname is constrained to emit `reefy-…` or
            # nothing, so reaching this branch means something upstream
            # broke. Log + wait — maybe it resolves on retry.
            echo "[reefy] hostname: unexpected '${OUT}' from reefy-derive-hostname, ignoring" >&2
            if [ "$(uptime_s)" -ge "${deadline}" ]; then
                break
            fi
            sleep "${POLL_INTERVAL_S}"
            continue
            ;;
    esac
done

if [ -z "${NAME}" ]; then
    # No NIC and no /etc/machine-id — leave hostname as kernel default.
    # reefy-control will still register, just with "buildroot". Log loudly.
    echo "[reefy] hostname: no NIC and no machine-id; leaving as $(hostname)" >&2
    exit 0
fi

CURRENT=$(hostname)
if [ "${CURRENT}" = "${NAME}" ]; then
    exit 0
fi

# Transient only — rootfs is A/B firmware and hostname is re-derived
# from the NIC on every boot; persisting would just go stale after a
# NIC replacement.
hostname "${NAME}"
echo "[reefy] hostname set to ${NAME} (was ${CURRENT})"
