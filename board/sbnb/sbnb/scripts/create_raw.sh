#!/bin/bash

set -euxo pipefail

EFI=sbnb.efi
IMG_FILE=sbnb.raw
TMP_DIR=$(mktemp -d)
FS_SIZE="256M"
SBNB_TSKEY="${BR2_EXTERNAL_SBNB_PATH}"/sbnb-tskey.txt

# check if called under root
if [ "$EUID" -ne 0 ];then
    echo "Please run as root"
    exit 1
fi

# Create vfat image and copy efi binary into it
dd if=/dev/zero of=${IMG_FILE} bs=${FS_SIZE} count=1
LOOP=$(losetup --show -f ${IMG_FILE})
parted -s ${LOOP} mklabel gpt
parted -s ${LOOP} mkpart sbnb 0% 100%
parted -s ${LOOP} set 1 boot on

partprobe ${LOOP}

mkfs.vfat ${LOOP}p1
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

echo Raw sbnb image for bare metal is ${IMG_FILE}

# Prepare compressed raw image
gzip -f -k ${IMG_FILE}
