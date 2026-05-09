#!/bin/sh
set -uxo pipefail
# NOTE: no set -e — errors are logged but don't stop execution.

# Phase 2: Non-critical init — runs in parallel with Docker/MQTT/GPU.
# See docs/boot-architecture.md for full boot flow.

REEFY_DATA_MNT="/mnt/reefy-data"

# Generate device password on first boot, create reefy user,
# and set password for both root and reefy.
# Runs every boot since rootfs is in RAM (not persistent).
setup_device_credentials() {
    PASSWORD_FILE="${REEFY_DATA_MNT}/state/device_password"

    # Generate password on first boot only
    # Uses easy-to-type chars (no ambiguous 0O1lI): 12 chars ≈ 59 bits entropy
    # Always generate even without persistent storage (overlay dir is fine —
    # bootstrap state copy during adoption will carry it to real storage)
    if [ ! -f "${PASSWORD_FILE}" ]; then
        mkdir -p "$(dirname "${PASSWORD_FILE}")"
        PASSWORD=$(tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' < /dev/urandom | head -c 12)
        echo "${PASSWORD}" > "${PASSWORD_FILE}"
        chmod 600 "${PASSWORD_FILE}"
        echo "[reefy] Generated device password"
    fi

    # Dev-mode: print the password to kmsg/serial console if the kernel cmdline
    # requests it. Opt-in via `reefy.dev_shell=1` — production builds default to
    # `quiet` without this flag, so the password is never exposed.
    if grep -q 'reefy.dev_shell=1' /proc/cmdline 2>/dev/null; then
        DEV_PASSWORD=$(cat "${PASSWORD_FILE}" 2>/dev/null)
        echo "[reefy] DEV: root/reefy password=${DEV_PASSWORD}" > /dev/kmsg
    fi

    # Read password
    PASSWORD=$(cat "${PASSWORD_FILE}" 2>/dev/null) || return 0
    [ -z "${PASSWORD}" ] && return 0

    # Users to configure — keep in sync with DEVICE_USERS in reefy-mqtt-reconciler
    DEVICE_USERS="root reefy"

    # Create non-root users if not exists (every boot — rootfs is in RAM).
    # BusyBox adduser leaves /home/<user> as root:root on some builds, so
    # chown after creation to guarantee the user can write to their own home.
    for user in ${DEVICE_USERS}; do
        [ "${user}" = "root" ] && continue
        id "${user}" >/dev/null 2>&1 || adduser -D -s /bin/sh "${user}" 2>/dev/null || true
        [ -d "/home/${user}" ] && chown "${user}:${user}" "/home/${user}"
    done

    # Set passwords using mkpasswd hash + sed (BusyBox passwd doesn't accept stdin)
    HASH=$(mkpasswd "${PASSWORD}")
    for user in ${DEVICE_USERS}; do
        sed -i "s|^${user}:[^:]*:|${user}:${HASH}:|" /etc/shadow
    done

    echo "[reefy] Device credentials applied"

    # If a key is staged on the ESP, install it as the canonical
    # reefy/app-* shared authorized_keys (root:root 0644 so sshd can
    # read it after dropping privs to any user - see
    # sshd_config.d/reefy-apps.conf for the AuthorizedKeysFile path).
    # Independent of dev_shell - that flag only gates the kmsg password
    # leak above.
    if [ -f /mnt/reefy/reefy/dev/authorized_keys ]; then
        mkdir -p /etc/ssh/authorized_keys.d
        cp /mnt/reefy/reefy/dev/authorized_keys /etc/ssh/authorized_keys.d/reefy
        chmod 644 /etc/ssh/authorized_keys.d/reefy
    fi
}

# Display ASCII banner and hostname/interface IP summary
display_banner() {
    {
        echo " ____            __       "
        echo "|  _ \ ___  ___ / _|_   _ "
        echo "| |_) / _ \/ _ \ |_| | | |"
        echo "|  _ <  __/  __/  _| |_| |"
        echo "|_| \_\___|\___|_|  \__, |"
        echo "                    |___/ "
        echo ""
        echo "  Welcome to Reefy!"
        echo "  Version:" $(. /etc/os-release; echo ${IMAGE_VERSION})
        echo ""
        echo "Hostname: $(hostname)"
        echo "Active slot: $(reefy-efi status 2>/dev/null | awk '/^Active:/ {print $2}')"
        echo "Interface IPs:"
        ip -o -4 addr list | awk '{print $2, $4}'
    } > /dev/kmsg
}

# Main execution
setup_device_credentials
display_banner
