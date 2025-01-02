#!/bin/sh
set -euxo pipefail

# Set unique hostname using platform serial number
SERIAL=$(dmidecode -s system-serial-number)
if [ "${SERIAL}" = "Not Specified" ];then
    # Set random hostname if no platform serial number found
    SERIAL=$(xxd -l6 -p /dev/random);
fi

hostname sbnb-${SERIAL}

# Mount sbnb USB flash identified by PARTLABEL="sbnb"
SBNB_DEV=$(blkid -t PARTLABEL="sbnb" -o device -l)
SBNB_MNT="/mnt/sbnb"
mkdir -p "${SBNB_MNT}"
mount -o ro "${SBNB_DEV}" "${SBNB_MNT}"

# Read tailscale key from USB flash
TS_KEY=$(cat "${SBNB_MNT}"/sbnb-tskey.txt)

# Start tunnel
if [ -z ${TS_KEY} ];then
    echo "[sbnb] No tailscale key found!"
else
    echo "[sbnb] Starting tailscale"
    tailscale up --ssh --auth-key ${TS_KEY}
fi
