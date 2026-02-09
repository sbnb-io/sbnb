#!/bin/bash
set -euxo pipefail

# Prepare dev env within container

export DEBIAN_FRONTEND=noninteractive

# Install some useful packages
apt-get update -y \
        && apt-get install -y \
        build-essential vim tmux file \
        cpio unzip rsync wget bc \
        dosfstools git apt-file systemd net-tools \
        sudo pciutils parted fio nvme-cli smartmontools \
        ipmitool yamllint jq sysstat strace curl libelf-dev ncurses-dev \
        exuberant-ctags iputils-ping bind9-dnsutils mc docker.io qemu-kvm ovmf \
        genisoimage

# Finally start tmux for fellow developers
tmux new-session -A -s sbnb-dev-env
