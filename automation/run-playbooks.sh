#!/bin/bash
set -e

# Usage: ./run-playbooks.sh --scope <scope> --playbooks <path/to/playbooks.json> --host <host>
#
# Example playbooks.json:
# [
#   "sbnb-mount-data-disk.yaml",
#   "install-docker.yaml",
#   "install-nvidia.yaml",
#   "install-nvidia-container-toolkit.yaml",
#   "nvidia-smi-exporter.yaml",
#   "grafana.yaml",
#   "run-sunny-osprey.yaml"
# ]

# Parse arguments
SCOPE=""
PLAYBOOKS_JSON=""
SBNB_HOSTS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)
      SCOPE="$2"
      shift 2
      ;;
    --playbooks)
      PLAYBOOKS_JSON="$2"
      shift 2
      ;;
    --host)
      SBNB_HOSTS="$2"
      shift 2
      ;;
    *)
      echo "Usage: $0 --scope <scope> --playbooks <path/to/playbooks.json> --host <host>"
      exit 1
      ;;
  esac
done

if [ -z "$SCOPE" ] || [ -z "$PLAYBOOKS_JSON" ] || [ -z "$SBNB_HOSTS" ]; then
  echo "Usage: $0 --scope <scope> --playbooks <path/to/playbooks.json> --host <host>"
  exit 1
fi

# Check for jq
if ! command -v jq &> /dev/null; then
    echo "❌ Error: jq is required but not installed. Please install jq."
    exit 1
fi

# Check for playbooks.json
if [ ! -f "$PLAYBOOKS_JSON" ]; then
    echo "❌ Error: $PLAYBOOKS_JSON not found."
    exit 1
fi

# Source common.sh using directory-agnostic path
source "$(dirname "$0")/common.sh"

# Export all required env variables from AWS SSM using parameters_show_all
PARAM_EXPORTS=$(parameters_show_all "$SCOPE")
if [ -z "$PARAM_EXPORTS" ]; then
    echo "❌ Error: No parameters found for scope '$SCOPE' in AWS SSM. Exiting."
    exit 1
fi
export $PARAM_EXPORTS

# Export SBNB_HOSTS for Ansible
export SBNB_HOSTS

# Read playbooks from playbooks.json
playbooks=($(jq -r '.[]' "$PLAYBOOKS_JSON"))

for playbook in "${playbooks[@]}"; do
    echo "==> Running playbook: $playbook"
    ansible-playbook -i ./ansible_tailscale_inventory.py "$playbook"
    echo "✅ Playbook $playbook completed successfully."
    sleep 1
done

echo "All playbooks completed successfully!" 
