#!/bin/sh
# Create NVIDIA device nodes and generate CDI specification.
# Called by nvidia-cdi-generate.service after nvidia modules are loaded.

set -e

has_nvidia_pci_device() {
    for vendor in /sys/bus/pci/devices/*/vendor; do
        [ -e "$vendor" ] || continue
        if [ "$(cat "$vendor" 2>/dev/null)" = "0x10de" ]; then
            return 0
        fi
    done
    return 1
}

if ! has_nvidia_pci_device; then
    echo "No NVIDIA PCI device found, skipping CDI setup"
    exit 0
fi

# Load nvidia-uvm if not already loaded
modprobe nvidia-uvm 2>/dev/null || true

# Get nvidiactl major number
NVIDIA_MAJOR=$(awk '/^[0-9]+ nvidiactl$/{print $1}' /proc/devices 2>/dev/null)
if [ -z "$NVIDIA_MAJOR" ]; then
    echo "NVIDIA driver not loaded, skipping CDI setup"
    exit 0
fi

if [ ! -e /dev/nvidiactl ]; then
    mknod -m 666 /dev/nvidiactl c "$NVIDIA_MAJOR" 255
fi

# Create /dev/nvidia[0-N] for each GPU found in /proc/driver/nvidia/gpus/
GPU_IDX=0
for gpu_dir in /proc/driver/nvidia/gpus/*/; do
    [ -d "$gpu_dir" ] || break
    if [ ! -e "/dev/nvidia${GPU_IDX}" ]; then
        mknod -m 666 "/dev/nvidia${GPU_IDX}" c "$NVIDIA_MAJOR" "$GPU_IDX"
    fi
    GPU_IDX=$((GPU_IDX + 1))
done

# Fallback: if no GPUs found in /proc, create at least nvidia0
if [ "$GPU_IDX" -eq 0 ] && [ ! -e /dev/nvidia0 ]; then
    mknod -m 666 /dev/nvidia0 c "$NVIDIA_MAJOR" 0
fi

# Create /dev/nvidia-uvm
NVIDIA_UVM_MAJOR=$(awk '/^[0-9]+ nvidia-uvm$/{print $1}' /proc/devices 2>/dev/null)
if [ -n "$NVIDIA_UVM_MAJOR" ]; then
    [ -e /dev/nvidia-uvm ] || mknod -m 666 /dev/nvidia-uvm c "$NVIDIA_UVM_MAJOR" 0
    [ -e /dev/nvidia-uvm-tools ] || mknod -m 666 /dev/nvidia-uvm-tools c "$NVIDIA_UVM_MAJOR" 1
fi

# Generate CDI specification
mkdir -p /var/run/cdi
/usr/bin/nvidia-ctk cdi generate --disable-hook update-ldcache --output=/var/run/cdi/nvidia.yaml

echo "NVIDIA CDI setup complete"
nvidia-ctk cdi list
