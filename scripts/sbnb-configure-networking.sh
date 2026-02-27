#!/bin/bash

# This script is used to configure the host networking for the VM.
# It creates a bridge interface (br0) and attaches the main interface to it.
# This allows the VM to be attached to the host and obtain an IP address from the same DHCP server as the host itself.
# This also allows the VM to be directly accessible from the host and other devices on the LAN.
# The script is idempotent and can be run multiple times without causing issues.
#
# Note: Bridge setup is skipped if the main interface is WiFi (802.11 cannot be bridged).
# Warning: This script will remove bridge-conflicting network configs on the host.
#
# Below is an example of the network configuration after running this script and starting two VMs:
#
# +---------------------+
# |        LAN          |
# +---------------------+
#           |
#           |
# +---------------------+
# |        Host         |
# | +-----------------+ |
# | |     br0         | |
# | +-----------------+ |
# |   |           |     |
# | +-----+     +-----+ |
# | | tap0|     | tap1| |
# | +-----+     +-----+ |
# |   |           |     |
# | +-----+     +-----+ |
# | | VM1 |     | VM2 | |
# | +-----+     +-----+ |
# +---------------------+

set -euxo pipefail

# Configure host networking
configure_host_networking() {
  if ip link show br0 &>/dev/null; then
    echo "Host network already configured."
    return
  fi

  MAIN_INTERFACE=$(ip route | grep default | awk '{print $5}')
  if [ -z "${MAIN_INTERFACE}" ]; then
    echo "Warning: No main interface found."
    return
  fi

  # WiFi interfaces cannot be bridged (802.11 3-address frame limitation)
  if [ -d "/sys/class/net/${MAIN_INTERFACE}/wireless" ]; then
    echo "WiFi interface ${MAIN_INTERFACE} detected, skipping bridge setup."
    return
  fi

  # Remove only bridge-conflicting configs (ethernet/wildcard), preserve WiFi
  rm -f /etc/systemd/network/*-br0.* /etc/systemd/network/99-wildcard.network /etc/systemd/network/30-*.network

  cat > /etc/systemd/network/25-br0.netdev << EOF
[NetDev]
Name=br0
Kind=bridge
MACAddress=none
EOF

  cat > /etc/systemd/network/25-br.network << EOF
[Match]
Name=br0

[Network]
DHCP=yes
EOF

  cat > /etc/systemd/network/30-en-br0.network << EOF
[Match]
Name=${MAIN_INTERFACE}

[Network]
Bridge=br0
EOF

  cat > /etc/systemd/network/25-br0.link << EOF
[Match]
OriginalName=br0

[Link]
MACAddressPolicy=none
EOF

  systemctl restart systemd-networkd
}

configure_host_networking