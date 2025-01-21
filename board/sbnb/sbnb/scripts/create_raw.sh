#!/bin/bash

set -euxo pipefail

EFI=sbnb.efi
IMG_FILE=sbnb.raw
VHD_FILE=sbnb.vhd
TMP_DIR=$(mktemp -d)
FS_SIZE="512" # in MB
SBNB_TSKEY="${BR2_EXTERNAL_SBNB_PATH}"/sbnb-tskey.txt

# check if called under root
if [ "$EUID" -ne 0 ];then
    echo "Please run as root"
    exit 1
fi

# Create vfat image and copy efi binary into it
dd if=/dev/zero of=${IMG_FILE} bs=1M count=${FS_SIZE}
LOOP=$(losetup --show -f ${IMG_FILE})
parted -s ${LOOP} mklabel gpt
parted -s ${LOOP} mkpart sbnb 0% 100%
parted -s ${LOOP} set 1 boot on

partprobe ${LOOP}

mkfs.vfat -F 32 ${LOOP}p1
mount -o loop ${LOOP}p1 ${TMP_DIR}
mkdir -p ${TMP_DIR}/EFI/Boot/
cp ${EFI} ${TMP_DIR}/EFI/Boot/bootx64.efi

# Copy sbnb config with tskey.
if [ -e ${SBNB_TSKEY} ];then
  cp ${SBNB_TSKEY} ${TMP_DIR}/
fi

# Cleanup tmp dir
umount ${TMP_DIR}
rm -rf ${TMP_DIR}
losetup -d ${LOOP}

# Prepare compressed raw image
gzip -f -k ${IMG_FILE}

# Prepare vhd
qemu-img convert -f raw -O vpc ${IMG_FILE} ${VHD_FILE}

echo Raw sbnb image for bare metal is ${IMG_FILE}
echo VHD sbnb image is ${VHD_FILE}
