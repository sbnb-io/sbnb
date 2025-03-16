#!/bin/bash

# This script configures the bare metal for Sbnb Linux.
# It includes configuration for storage and networking, and reconfigures the Docker daemon
# to use a specified storage mount point.
# For storage and network configuration details see corresponding scripts.
# This script is idempotent and can be called multiple times.

# Include other scripts
source "$(dirname "$0")/sbnb-configure-storage.sh"
source "$(dirname "$0")/sbnb-configure-networking.sh"

configure_docker() {
    # Reconfigure Docker daemon to use storage
    DOCKER_CONFIG_FILE="/etc/docker/daemon.json"
    MOUNT_POINT=${MOUNT_POINT:-"/var/lib/docker"}

    # Create Docker config directory if it doesn't exist
    mkdir -p "$(dirname "$DOCKER_CONFIG_FILE")"

    # Check if Docker is already configured
    if grep -q "\"data-root\": \"$MOUNT_POINT/docker\"" "$DOCKER_CONFIG_FILE"; then
        echo "Docker is already configured to use storage at $MOUNT_POINT/docker"
    else
        # Create or override Docker config file
        echo "{\"data-root\": \"$MOUNT_POINT/docker\"}" > "$DOCKER_CONFIG_FILE"

        # Restart Docker daemon to apply changes
        systemctl restart docker

        echo "Docker daemon reconfigured to use storage at $MOUNT_POINT/docker"
    fi
}

# Call the configure_docker function
configure_docker
