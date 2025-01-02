#!/bin/bash
set -euxo pipefail

# Place efi and raw images into buildroot/output/images dir
pushd ${BINARIES_DIR}

# TODO: avoid calling sudo
echo Building sbnb.efi uefi uki image
sudo -E "${BR2_EXTERNAL_SBNB_PATH}"/board/sbnb/sbnb/scripts/create_efi.sh

echo Building sbnb.raw bootable image
sudo -E "${BR2_EXTERNAL_SBNB_PATH}"/board/sbnb/sbnb/scripts/create_raw.sh

popd
