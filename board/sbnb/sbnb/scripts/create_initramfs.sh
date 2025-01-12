#!/bin/bash

set -euxo pipefail

INITRAMFS="${BINARIES_DIR}/rootfs-sbnb.cpio"
INITRAMFS_DIR="${BINARIES_DIR}/rootfs-sbnb"

# Create initramfs structure
rm -rf "${INITRAMFS_DIR}"
mkdir -p "${INITRAMFS_DIR}"
mkdir -p "${INITRAMFS_DIR}/bin"
mkdir -p "${INITRAMFS_DIR}/sbin"
mkdir -p "${INITRAMFS_DIR}/usr/bin"
mkdir -p "${INITRAMFS_DIR}/usr/sbin"

# Copy statically linked busybox into initramfs
cp "${TARGET_DIR}/bin/busybox" "${INITRAMFS_DIR}/bin"

# Copy squashfs rootfs
cp "${BINARIES_DIR}/rootfs.squashfs" "${INITRAMFS_DIR}"

# Place init script
cp "${BR2_EXTERNAL_SBNB_PATH}/board/sbnb/sbnb/scripts/init" "${INITRAMFS_DIR}"

# cpio initramfs
pushd "${INITRAMFS_DIR}"
find . -print0 | cpio --null -ov --format=newc > "${INITRAMFS}"
popd

echo Output: "${INITRAMFS}"
