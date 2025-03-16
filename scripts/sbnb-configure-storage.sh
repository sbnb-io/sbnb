#!/bin/bash

# This script is used to configure the host storage for the VM.
# It finds all drives that don't have any partitions on them,
# allocates all 100%FREE space to LVM, creates one flat space from all available drives (similar to RAID0),
# creates PV, VG, LV, formats LV to ext4 with "-m 0" and mounts it to /mnt/sbnb-data.
# The script is idempotent and can be run multiple times without causing issues.
#
# Warning: This script will overwrite the existing storage configuration on the host.
#
# Example representation of the final state with two 1TB nvme drives
#
# Initial state:
# /dev/nvme0n1 (1TB)
# /dev/nvme1n1 (1TB)
#
# After running the script:
# /dev/nvme0n1 (1TB)  /dev/nvme1n1 (1TB)
#      |                   |
#      v                   v
#   pvcreate            pvcreate
#      |                   |
#      v                   v
#   sbnb-vg (Volume Group, 2TB)
#      |
#      v
#   sbnb-lv (Logical Volume, 2TB)
#      |
#      v
#   ext4 filesystem
#      |
#      v
#   /mnt/sbnb-data (Mount Point, 2TB)

set -euxo pipefail

# Constants
MOUNT_POINT="/mnt/sbnb-data"
VG_NAME="sbnb-vg"
LV_NAME="sbnb-lv"
LV_PATH="/dev/${VG_NAME}/${LV_NAME}"

# Check if /mnt/sbnb-data is already mounted
is_mounted() {
    mountpoint -q "${MOUNT_POINT}"
}

# Find all drives without partitions and not part of any LVM
find_drives() {
    lsblk -dn -o NAME | grep -E '^(sd|hd|nvme)' | while read -r disk; do
        if [ -z "$(lsblk -n -o NAME /dev/$disk | grep -v "^$disk$")" ] && [ -z "$(pvs --noheadings -o pv_name | grep /dev/$disk)" ]; then
            echo "/dev/$disk"
        fi
    done
}

# Create physical volumes
create_pvs() {
    for drive in $1; do
        pvcreate "$drive"
    done
}

# Extend volume group
extend_vg() {
    vgextend "${VG_NAME}" $1
}

# Extend logical volume
extend_lv() {
    lvextend -l +100%FREE "${LV_PATH}"
    resize2fs "${LV_PATH}"
}

# Check if the logical volume already exists
lv_exists() {
    lvdisplay "${LV_PATH}" &> /dev/null
}

# Create volume group if it doesn't exist
create_vg() {
    vgcreate "${VG_NAME}" $1
}

# Create logical volume
create_lv() {
    lvcreate -l 100%FREE -Zy -Wy --yes -n "${LV_NAME}" "${VG_NAME}"
}

# Format logical volume to ext4 with "-m 0"
format_lv() {
    mkfs.ext4 -m 0 "${LV_PATH}"
}

# Create mount point and mount logical volume
mount_lv() {
    if lv_exists; then
        if ! is_mounted; then
            mkdir -p "${MOUNT_POINT}"
            mount "${LV_PATH}" "${MOUNT_POINT}" || echo "Failed to mount ${LV_PATH}."
        else
            echo "${MOUNT_POINT} is already mounted."
        fi
    else
        echo "Logical volume ${LV_PATH} does not exist. Nothing to mount."
    fi
}

# Unmount and destroy LVM
destroy_lvm() {
    if is_mounted; then
        umount "${MOUNT_POINT}"
    fi

    if lv_exists; then
        lvremove -y "${LV_PATH}"
    fi

    if vgdisplay "${VG_NAME}" &> /dev/null; then
        vgremove -y "${VG_NAME}"
    fi

    pvs --noheadings -o pv_name | grep -E "/dev/(sd|hd|nvme)" | while read -r pv; do
        pvremove -y "$pv"
    done
}

# Configure host storage
configure_host_storage() {
    DRIVES=$(find_drives)

    if [ -z "${DRIVES}" ]; then
        echo "No drives without partitions found. Proceeding to mount existing LVM if available."
    fi

    if [ -n "${DRIVES}" ]; then
        if vgdisplay "${VG_NAME}" &> /dev/null; then
            echo "Volume group ${VG_NAME} exists. Extending it with new drives."
            create_pvs "${DRIVES}"
            extend_vg "${DRIVES}"
            extend_lv
        else
            echo "Volume group ${VG_NAME} does not exist. Creating it with new drives."
            create_pvs "${DRIVES}"
            create_vg "${DRIVES}"
            create_lv
            format_lv
        fi
    else
        echo "No new drives detected. Skipping configuration."
    fi

    mount_lv
}

# Main script logic
usage() {
    echo "Usage: $0 [-c create|destroy]"
    exit 1
}

warning() {
    echo "Warning: This will destroy the LVM configuration and all data will be lost."
    echo "Continuing to destroy in 5 seconds if not cancelled... Press Ctrl+C to cancel."
    sleep 5
}

parse_arguments() {
    ACTION="create"  # Default action

    while getopts ":c:" opt; do
        case ${opt} in
            c)
                ACTION=$OPTARG
                ;;
            \?)
                usage
                ;;
            :)
                usage
                ;;
        esac
    done
}

main() {
    parse_arguments "$@"

    case "$ACTION" in
        create)
            configure_host_storage
            ;;
        destroy)
            warning
            destroy_lvm
            ;;
        *)
            usage
            ;;
    esac
}

main "$@"
