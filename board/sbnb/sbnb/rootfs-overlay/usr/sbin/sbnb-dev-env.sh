#!/bin/sh
set -euxo pipefail

# Starting a privileged development environment here. We are using the latest
# debian:sid because it includes qemu-9.2.0, which supports AMD SEV-SNP
# confidential computing technology.
#
# If confidential computing is not required, you can switch to ubuntu:24.04 as
# an alternative.
#
# TODO: Implement a selector to choose between Debian, Ubuntu, Fedora, and
# other distributions.
IMAGE="debian:sid"
NAME="sbnb-dev-env"

# Attach to the dev container if it's already running
if docker ps | grep -q ${NAME};then
    docker exec -it ${NAME} tmux new-session -A -s sbnb-dev-env
    exit 0
fi

# Create a new dev container
docker run -it -d --privileged \
        -v /root:/root \
        -v /dev:/dev \
        -v /:/host \
        -v /var/run/docker.sock:/var/run/docker.sock \
        --net=host \
        --name ${NAME} --rm \
        --pull=always \
        --ulimit nofile=262144:262144 \
        ${IMAGE}

docker exec -it ${NAME} bash -c /host/usr/sbin/_sbnb-dev-env-container.sh
