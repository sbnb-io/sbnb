#!/bin/sh
set -euxo pipefail

# Sbnb Linux boot script executed by systemd at startup to perform the following tasks:
# 1. Set a unique hostname using the platform's serial number or a random value if no serial number found
# 2. Mount sbnb USB flash identified by PARTLABEL="sbnb" or LABEL="sbnb"
# 3. Mount VMware shared folder shared by the host and named "sbnb" if we started in a VMware VM
# 4. Find Tailscale key file "sbnb-tskey.txt"
# 5. Start Tailscale tunnel
# 6. Execute sbnb-cmds.sh if found
# 7. Display ASCII banner and hostname/interface IP summary

# Function to set unique hostname using platform serial number
set_hostname() {
    SERIAL=$(dmidecode -s system-serial-number || echo "Not Specified")
    if [ "${SERIAL}" = "Not Specified" ]; then
        # Set random hostname if no platform serial number found
        SERIAL=$(xxd -l6 -p /dev/random)
    fi
    SERIAL=$(echo "${SERIAL}" | tr ' ' '-') # Replace spaces with dashes
    hostname "sbnb-${SERIAL}"
}

# Function to mount sbnb USB flash identified by PARTLABEL="sbnb" or LABEL="sbnb" (case insensitive)
mount_sbnb_usb() {
    SBNB_DEV=$(blkid | grep -i 'LABEL="sbnb"\|PARTLABEL="sbnb"' | awk -F: '{print $1}' | head -n 1) || true
    if [ -n "${SBNB_DEV}" ]; then
        SBNB_MNT="/mnt/sbnb"
        mkdir -p "${SBNB_MNT}" || true
        mount -o ro "${SBNB_DEV}" "${SBNB_MNT}" || true
    else
        echo "No device with PARTLABEL or LABEL 'sbnb' found."
    fi
}

# Function to mount VMware shared folder
mount_vmware_shared_folder() {
    VMWARE_MNT="/mnt/vmware"
    mkdir -p "${VMWARE_MNT}"
    if ! vmhgfs-fuse .host:/sbnb "${VMWARE_MNT}" -o allow_other; then
        echo "[sbnb] Failed to mount VMware shared folder"
    fi
}

# Function to find Tailscale key file
find_ts_key() {
    local key_file=""
    local search_paths="/mnt/sbnb/sbnb-tskey.txt /mnt/vmware/sbnb-tskey.txt"

    for path in ${search_paths}; do
        if [ -f "${path}" ]; then
            key_file="${path}"
            break
        fi
    done

    echo "${key_file}"
}

# Function to start Tailscale
start_tailscale() {
    local ts_key_file
    ts_key_file=$(find_ts_key)

    if [ -z "${ts_key_file}" ]; then
        echo "[sbnb] No Tailscale key found!"
        return 0
    fi

    echo "[sbnb] Starting Tailscale"
    tailscale up --ssh --auth-key "file:${ts_key_file}"
}

# Function to display ASCII banner and hostname/interface IP summary
display_banner() {
    {
        echo "   ____  _           _       _     _"
        echo "  / ___|| |__  _ __ | |__   | |   (_)_ __  _   ___  __"
        echo "  \___ \| '_ \| '_ \| '_ \  | |   | | '_ \| | | \ \/ /"
        echo "   ___) | |_) | | | | |_) | | |___| | | | | |_| |>  <"
        echo "  |____/|_.__/|_| |_|_.__/  |_____|_|_| |_|\__,_/_/\_\\"
        echo ""
        echo "  Welcome to Sbnb Linux!"
        echo "  Version:" $(. /etc/os-release; echo ${IMAGE_VERSION})
        echo ""
        echo "  Just an ASCII banner for now."
        echo "  Animations will arrive right after Linux is rewritten in JavaScript."
        echo ""
        echo "Hostname: $(hostname)"
        echo "Interface IPs:"
        ip -o -4 addr list | awk '{print $2, $4}'
    } > /dev/kmsg
}

# Function to find and execute sbnb-cmds.sh
execute_sbnb_cmds() {
    local cmd_file=""
    local search_paths="/mnt/sbnb/sbnb-cmds.sh /mnt/vmware/sbnb-cmds.sh"

    for path in ${search_paths}; do
        if [ -f "${path}" ]; then
            cmd_file="${path}"
            break
        fi
    done

    if [ -n "${cmd_file}" ]; then
        echo "[sbnb] Executing commands from ${cmd_file}"
        sh "${cmd_file}"
    else
        echo "[sbnb] No sbnb-cmds.sh file found!"
    fi
}

# Main script execution
set_hostname
mount_sbnb_usb
mount_vmware_shared_folder
start_tailscale
execute_sbnb_cmds
display_banner
