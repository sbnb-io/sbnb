#!/bin/sh
set -euxo pipefail

# Start privileged dev env
IMAGE="ubuntu:24.04"
NAME="sbnb"

# Attach to the dev container if it's already running
if docker ps | grep -q ${NAME};then
    docker exec -it ${NAME} tmux new-session -A -s sbnb
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
