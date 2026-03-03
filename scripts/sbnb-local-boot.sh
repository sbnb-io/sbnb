#!/bin/bash
#
# Boot Sbnb Linux in QEMU for local testing
#
# Usage: boot_in_qemu.sh [OPTIONS]
#
# Options:
#   -h, --help          Show this help message
#   -m, --memory SIZE   Memory size (default: 4G)
#   -c, --cpus NUM      Number of CPUs (default: 2)
#   -r, --readonly      Boot image read-only (no copy, use snapshot)
#   -n, --network MODE  Network mode: user (default), bridge, tap, none
#   -g, --graphics      Enable VNC graphics on :0
#   -d, --disk FILE     Attach additional disk
#   -v, --verbose       Verbose output (set -x)
#   --                  Pass remaining args to QEMU
#
# Examples:
#   boot_in_qemu.sh                          # Quick test (4G RAM, 2 CPUs)
#   boot_in_qemu.sh -m 16G -c 4              # More resources
#   boot_in_qemu.sh -r                       # Read-only (fast, no disk changes)
#   boot_in_qemu.sh -d data.qcow2            # Attach data disk
#   boot_in_qemu.sh -- -serial mon:stdio     # Custom QEMU args

set -euo pipefail

# Defaults
MEMORY="4G"
CPUS="2"
READONLY=false
NETWORK="user"
GRAPHICS=false
VERBOSE=false
EXTRA_DISKS=()
QEMU_EXTRA_ARGS=()

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        -m|--memory)
            MEMORY="$2"
            shift 2
            ;;
        -c|--cpus)
            CPUS="$2"
            shift 2
            ;;
        -r|--readonly)
            READONLY=true
            shift
            ;;
        -n|--network)
            NETWORK="$2"
            shift 2
            ;;
        -g|--graphics)
            GRAPHICS=true
            shift
            ;;
        -d|--disk)
            EXTRA_DISKS+=("$2")
            shift 2
            ;;
        -v|--verbose)
            VERBOSE=true
            set -x
            shift
            ;;
        --)
            shift
            QEMU_EXTRA_ARGS=("$@")
            break
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# Locate script and image directories
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
IMG_DIR="${SCRIPT_DIR}/../buildroot/output/images"
IMG_FILE_SRC="${IMG_DIR}/sbnb.raw"

# Check if source image exists
if [[ ! -f "${IMG_FILE_SRC}" ]]; then
    echo "Error: Image not found: ${IMG_FILE_SRC}" >&2
    echo "Build the image first: cd buildroot && make" >&2
    exit 1
fi

# Locate OVMF BIOS (try common locations)
OVMF_LOCATIONS=(
    "/usr/share/ovmf/OVMF.fd"
    "/usr/share/OVMF/OVMF_CODE.fd"
    "/usr/share/edk2/ovmf/OVMF_CODE.fd"
    "/usr/share/qemu/OVMF.fd"
)

OVMF_BIOS=""
for location in "${OVMF_LOCATIONS[@]}"; do
    if [[ -f "$location" ]]; then
        OVMF_BIOS="$location"
        break
    fi
done

if [[ -z "$OVMF_BIOS" ]]; then
    echo "Error: OVMF BIOS not found in common locations:" >&2
    printf "  %s\n" "${OVMF_LOCATIONS[@]}" >&2
    echo "" >&2
    echo "Install OVMF package:" >&2
    echo "  Ubuntu/Debian: sudo apt-get install ovmf" >&2
    echo "  Fedora/RHEL:   sudo dnf install edk2-ovmf" >&2
    echo "  Arch:          sudo pacman -S edk2-ovmf" >&2
    exit 1
fi

# Prepare boot disk
if $READONLY; then
    # Use snapshot mode - no disk changes, no copy needed
    IMG_FILE="${IMG_FILE_SRC}"
    DISK_ARGS="-drive file=${IMG_FILE},if=virtio,format=raw,snapshot=on"
    echo "Booting in READ-ONLY mode (snapshot, no disk changes)"
else
    # Copy image for read-write access
    IMG_FILE="${IMG_DIR}/sbnb-qemu.raw"
    echo "Copying image for read-write access..."
    cp "${IMG_FILE_SRC}" "${IMG_FILE}"
    DISK_ARGS="-drive file=${IMG_FILE},if=virtio,format=raw"
fi

# Configure network
case "$NETWORK" in
    user)
        # User-mode networking (NAT, no root required)
        NETWORK_ARGS="-netdev user,id=n1 -device virtio-net-pci,netdev=n1"
        ;;
    bridge)
        # Bridge networking (requires setup and often root)
        NETWORK_ARGS="-netdev bridge,id=n1,br=br0 -device virtio-net-pci,netdev=n1"
        ;;
    tap)
        # TAP networking (requires setup)
        NETWORK_ARGS="-netdev tap,id=n1 -device virtio-net-pci,netdev=n1"
        ;;
    none)
        # No networking
        NETWORK_ARGS="-nic none"
        ;;
    *)
        echo "Error: Unknown network mode: $NETWORK" >&2
        exit 1
        ;;
esac

# Configure graphics
if $GRAPHICS; then
    GRAPHICS_ARGS="-vnc :0"
else
    GRAPHICS_ARGS=""
fi

# Attach additional disks
DISK_NUM=1
for disk in "${EXTRA_DISKS[@]}"; do
    if [[ ! -f "$disk" ]]; then
        echo "Warning: Disk not found, skipping: $disk" >&2
        continue
    fi
    DISK_ARGS="${DISK_ARGS} -drive file=${disk},if=virtio,format=qcow2,index=${DISK_NUM}"
    ((DISK_NUM++))
done

# Display configuration
echo "=== QEMU Configuration ==="
echo "Image:    ${IMG_FILE}"
echo "OVMF:     ${OVMF_BIOS}"
echo "Memory:   ${MEMORY}"
echo "CPUs:     ${CPUS}"
echo "Network:  ${NETWORK}"
echo "Graphics: $(${GRAPHICS} && echo 'VNC :0' || echo 'none (serial console only)')"
[[ ${#EXTRA_DISKS[@]} -gt 0 ]] && echo "Disks:    ${EXTRA_DISKS[*]}"
echo "=========================="
echo ""

# Check KVM availability
if [[ ! -w /dev/kvm ]]; then
    echo "Warning: /dev/kvm not accessible, falling back to TCG (slow)" >&2
    echo "For better performance, ensure KVM is enabled and you're in the 'kvm' group" >&2
    ACCEL_ARGS="-accel tcg"
else
    ACCEL_ARGS="-accel kvm"
fi

# Start QEMU
echo "Starting QEMU... (Ctrl+A then X to exit)"
echo ""

exec qemu-system-x86_64 \
    -nographic \
    -machine q35 \
    -cpu host \
    ${ACCEL_ARGS} \
    -m "${MEMORY}" \
    -smp "${CPUS}" \
    -bios "${OVMF_BIOS}" \
    ${NETWORK_ARGS} \
    ${GRAPHICS_ARGS} \
    ${DISK_ARGS} \
    "${QEMU_EXTRA_ARGS[@]}"
