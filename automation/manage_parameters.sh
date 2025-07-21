#!/bin/bash
set -e

# manage_parameters.sh
#
# This script provides a CLI for managing parameters in AWS SSM Parameter Store for automation workflows.
# It can show all parameters for a given scope (for export as environment variables) or push parameters from a JSON file.
#
# ---
# AWS SSM Parameter Store format:
#   Each parameter is stored under a path like /sbnb/<scope>/<key>
#   Example:
#     /sbnb/so/GRAFANA_URL = https://example.com/grafana
#     /sbnb/so/TELEGRAM_BOT_TOKEN = 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
#
# Example parameters.json content (one scope per file):
# {
#   "scope": "so",
#   "parameters": {
#     "GRAFANA_URL": "https://example.com/grafana",
#     "GRAFANA_USERNAME": "admin",
#     "GRAFANA_PASSWORD": "password123",
#     "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
#     "CHAT_ID": "987654321",
#     "HF_TOKEN": "hf_stubtoken"
#   }
# }
#
# Usage:
#   ./manage_parameters.sh --show-all --scope so
#   ./manage_parameters.sh --push-all --parameters path/to/parameters.json

# Source common functions
source "$(dirname "$0")/common.sh"

parameters_usage() {
  echo "Usage: $0 [--show-all --scope <scope> | --push-all --parameters <parameters.json>]"
  exit 1
}

ACTION=""
SCOPE=""
PARAMETERS_JSON=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --show-all)
      ACTION="$1"
      shift
      ;;
    --push-all)
      ACTION="$1"
      shift
      ;;
    --scope)
      if [[ -n "$2" ]]; then
        SCOPE="$2"
        shift 2
      else
        echo "Error: --scope requires a value"
        parameters_usage
      fi
      ;;
    --parameters)
      if [[ -n "$2" ]]; then
        PARAMETERS_JSON="$2"
        shift 2
      else
        echo "Error: --parameters requires a value"
        parameters_usage
      fi
      ;;
    *)
      echo "Unknown argument: $1"
      parameters_usage
      ;;
  esac
done

if [[ "$ACTION" == "--show-all" ]]; then
  if [[ -z "$SCOPE" ]]; then
    echo "Error: --show-all requires --scope <scope>."
    parameters_usage
  fi
  parameters_show_all "$SCOPE"
  exit 0
fi

if [[ "$ACTION" == "--push-all" ]]; then
  if [[ -z "$PARAMETERS_JSON" ]]; then
    echo "Error: --push-all requires --parameters <parameters.json>."
    parameters_usage
  fi
  parameters_push_all "$PARAMETERS_JSON"
  exit 0
fi

parameters_usage 
