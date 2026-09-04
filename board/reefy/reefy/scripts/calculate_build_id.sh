#!/bin/bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: calculate_build_id.sh BASE_BUILD_ID PROVIDER_PINS E2E_SALT" >&2
  exit 2
fi

BASE_REEFY_BUILD_ID=$1
PROVIDER_PINS=$2
BUILD_IDENTITY_SALT=$3

if [ -z "${BASE_REEFY_BUILD_ID}" ]; then
  echo "ERROR: empty base Reefy build identity" >&2
  exit 1
fi
if [ ! -f "${PROVIDER_PINS}" ]; then
  echo "ERROR: missing provider publisher pins" >&2
  exit 1
fi
if [ "$(wc -l < "${PROVIDER_PINS}")" -ne 3 ]; then
  echo "ERROR: provider publisher pins must contain exactly three entries" >&2
  exit 1
fi
for provider in nvidia intel amd; do
  if [ "$(grep -Ec "^${provider}=[0-9a-f]{40}$" "${PROVIDER_PINS}")" -ne 1 ]; then
    echo "ERROR: missing or invalid ${provider} publisher pin" >&2
    exit 1
  fi
done

PROVIDER_SET_SHA256=$(sha256sum "${PROVIDER_PINS}" | awk '{print $1}')
REEFY_BUILD_ID=$(printf '%s\0%s\0%s\0' \
  reefy-provider-build-id-v1 "${BASE_REEFY_BUILD_ID}" \
  "${PROVIDER_SET_SHA256}" | sha256sum | awk '{print $1}')

if [ -n "${BUILD_IDENTITY_SALT}" ]; then
  REEFY_BUILD_ID=$(printf '%s\0%s\0%s\0' \
    reefy-e2e-build-id-v2 "${REEFY_BUILD_ID}" \
    "${BUILD_IDENTITY_SALT}" | sha256sum | awk '{print $1}')
fi

printf '%s\n' "${REEFY_BUILD_ID}"
