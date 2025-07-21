#!/bin/bash

# Common utility functions for automation scripts
#
# This file provides functions for managing parameters in AWS SSM Parameter Store
# and for use in automation scripts (e.g., Ansible playbook runners).
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

parameters_show_all() {
  local SCOPE="$1"
  local PARAMETER_PATH="/sbnb/${SCOPE}/"
  PARAMS=$(aws ssm get-parameters-by-path --path "$PARAMETER_PATH" --with-decryption --recursive --query 'Parameters[*].[Name,Value]' --output text)
  if [ $? -ne 0 ]; then
    echo "AWS CLI command failed: aws ssm get-parameters-by-path --path $PARAMETER_PATH ..." >&2
    exit 1
  fi
  if [ -z "$PARAMS" ]; then
    echo "No parameters found at path $PARAMETER_PATH" >&2
    exit 1
  fi
  while IFS=$'\t' read -r NAME VALUE; do
    VAR_NAME=$(basename "$NAME")
    echo "$VAR_NAME=$VALUE"
  done <<< "$PARAMS"
}

parameters_push_all() {
  local PARAMETERS_JSON="$1"
  if [ -z "$PARAMETERS_JSON" ]; then
    echo "Error: Path to parameters.json must be provided as the argument to parameters_push_all." >&2
    exit 1
  fi
  if [ ! -f "$PARAMETERS_JSON" ]; then
    echo "$PARAMETERS_JSON not found!" >&2
    exit 1
  fi
  local SCOPE
  SCOPE=$(jq -r '.scope' "$PARAMETERS_JSON")
  if [ -z "$SCOPE" ] || [ "$SCOPE" == "null" ]; then
    echo "Error: 'scope' field is missing in $PARAMETERS_JSON" >&2
    exit 1
  fi
  local PARAMETER_PATH="/sbnb/${SCOPE}/"
  AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  if [ $? -ne 0 ] || [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "Could not determine AWS account. Is AWS CLI configured?"
    exit 1
  fi
  echo "Pushing all parameters from $PARAMETERS_JSON (scope: $SCOPE) to AWS SSM Parameter Store (account: $AWS_ACCOUNT_ID)..."
  for key in $(jq -r '.parameters | keys[]' "$PARAMETERS_JSON"); do
    value=$(jq -r --arg k "$key" '.parameters[$k]' "$PARAMETERS_JSON")
    param_name="${PARAMETER_PATH}${key}"
    aws ssm put-parameter --name "$param_name" --value "$value" --type SecureString --overwrite
    if [ $? -ne 0 ]; then
      echo "AWS CLI command failed: aws ssm put-parameter --name $param_name ..." >&2
      exit 1
    fi
    echo "Pushed $key to $param_name"
  done
} 