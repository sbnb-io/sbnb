#!/bin/bash
# Build the sbnb/qemu-standard Docker image
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="sbnb/qemu-standard"

echo "Building ${IMAGE_NAME}..."
docker build -t "${IMAGE_NAME}" "${SCRIPT_DIR}"

echo "Done. Image: ${IMAGE_NAME}"
