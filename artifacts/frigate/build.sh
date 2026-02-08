#!/bin/bash
set -e

VERSION=${1:-latest}
IMAGE="sbnb/frigate-models:${VERSION}"

echo "Building ${IMAGE}..."
docker build -t "${IMAGE}" .

echo "Pushing ${IMAGE}..."
docker push "${IMAGE}"

echo "Done: ${IMAGE}"
