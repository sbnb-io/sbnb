#!/bin/bash
# SBNB Integration Test Suite
#
# Usage:
#   ./run-tests.sh --host=bare-metal-host --tskey=tskey-auth-xxx
#   ./run-tests.sh --host=bare-metal-host --tskey=tskey-auth-xxx --skip-gpu
#   ./run-tests.sh --host=bare-metal-host --tskey=tskey-auth-xxx -vv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../../../.." && pwd)"

# Defaults
HOST=""
TSKEY=""
SKIP_GPU=false
HF_TOKEN=""
VM_PASSWORD="sbnb-test"
VERBOSITY=""
RUNCMD_ARGS=()

usage() {
  cat <<EOF
SBNB Integration Test Suite

Usage:
  $(basename "$0") --host=<host> --tskey=<key> [options]

Required:
  --host=<host>         Bare metal host (IP or Tailscale hostname)
  --tskey=<key>         Tailscale auth key for VM creation

Options:
  --skip-gpu            Skip GPU tests (for CPU-only hosts)
  --hf-token=<token>    HuggingFace token for gated models
  --vm-password=<pw>    VM root password (default: sbnb-test)
  --runcmd=<cmd>        Command to run in VM on first boot (repeatable)
  -v, -vv, -vvv        Ansible verbosity level
  -h, --help            Show this help
EOF
  exit 1
}

# Parse arguments
for arg in "$@"; do
  case "$arg" in
    --host=*)       HOST="${arg#*=}" ;;
    --tskey=*)      TSKEY="${arg#*=}" ;;
    --skip-gpu)     SKIP_GPU=true ;;
    --hf-token=*)   HF_TOKEN="${arg#*=}" ;;
    --vm-password=*) VM_PASSWORD="${arg#*=}" ;;
    --runcmd=*)     RUNCMD_ARGS+=("${arg#*=}") ;;
    -v|-vv|-vvv)    VERBOSITY="$arg" ;;
    -h|--help)      usage ;;
    *) echo "Unknown argument: $arg"; usage ;;
  esac
done

# Validate required args
if [ -z "$HOST" ]; then
  echo "ERROR: --host is required"
  usage
fi
if [ -z "$TSKEY" ]; then
  echo "ERROR: --tskey is required"
  usage
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="/tmp/sbnb-integration-test-${TIMESTAMP}.log"

# Generate unique test suffix so we know the VM names for cleanup
TEST_SUFFIX=$(python3 -c "import random,string; print(''.join(random.choices(string.ascii_lowercase,k=4)))")
PLAYBOOKS_DIR="$PROJECT_ROOT/collections/ansible_collections/sbnb/compute/playbooks"

# All possible VM names (each service gets its own VM)
ALL_VMS=(
  "sbnb-test-cpu-${TEST_SUFFIX}"
  "sbnb-test-gpu-${TEST_SUFFIX}"
  "sbnb-test-fryer-${TEST_SUFFIX}"
  "sbnb-test-vllm-${TEST_SUFFIX}"
  "sbnb-test-sglang-${TEST_SUFFIX}"
  "sbnb-test-frigate-${TEST_SUFFIX}"
  "sbnb-test-ollama-${TEST_SUFFIX}"
  "sbnb-test-openclaw-${TEST_SUFFIX}"
)

# Cleanup VMs on interrupt
cleanup_on_interrupt() {
  echo ""
  echo "=========================================="
  echo "  Interrupted! Cleaning up test VMs..."
  echo "=========================================="
  cd "$PROJECT_ROOT"
  for vm in "${ALL_VMS[@]}"; do
    echo "  Stopping $vm ..."
    ansible-playbook -i "$HOST," "$PLAYBOOKS_DIR/stop-vm.yml" \
      -e sbnb_vm_name="$vm" \
      -e sbnb_vm_remove=true \
      -e sbnb_vm_persist_boot_image=false 2>/dev/null || true
  done
  echo "  Cleanup done."
  exit 130
}
trap cleanup_on_interrupt INT TERM

echo "=========================================="
echo "  SBNB Integration Test Suite"
echo "=========================================="
echo "  Host:          $HOST"
echo "  Suffix:        $TEST_SUFFIX"
echo "  Skip GPU:      $SKIP_GPU"
echo "  Log file:      $LOG_FILE"
echo "  VMs:           sbnb-test-{cpu,gpu,fryer,vllm,sglang,frigate,ollama,openclaw}-${TEST_SUFFIX}"
echo "=========================================="
echo ""

cd "$PROJECT_ROOT"

# Build runcmd JSON array
RUNCMD_JSON="[]"
if [ ${#RUNCMD_ARGS[@]} -gt 0 ]; then
  RUNCMD_JSON=$(printf '%s\n' "${RUNCMD_ARGS[@]}" | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))")
fi

EXTRA_VARS=$(cat <<EOF
{
  "sbnb_vm_tskey": "$TSKEY",
  "test_skip_gpu": $SKIP_GPU,
  "test_vm_password": "$VM_PASSWORD",
  "test_hf_token": "$HF_TOKEN",
  "test_project_root": "$PROJECT_ROOT",
  "test_suffix": "$TEST_SUFFIX",
  "sbnb_vm_runcmd": $RUNCMD_JSON
}
EOF
)

ansible-playbook \
  -i "$HOST," \
  "$SCRIPT_DIR/test-integration.yml" \
  -e "$EXTRA_VARS" \
  ${VERBOSITY:+"$VERBOSITY"} \
  2>&1 | tee "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

echo ""
if [ $EXIT_CODE -eq 0 ]; then
  echo "=========================================="
  echo "  ALL TESTS PASSED"
  echo "=========================================="
else
  echo "=========================================="
  echo "  TESTS FAILED (exit code: $EXIT_CODE)"
  echo "  See log: $LOG_FILE"
  echo "=========================================="
fi

exit $EXIT_CODE
