#!/bin/bash
# Remote Host Attestation with AMD SEV-SNP and vTPM Measurements

set -euo pipefail

# This script performs remote host attestation using the AMD SEV-SNP
# attestation report and vTPM measurements. It compares the obtained values
# against predefined "golden" values that are known to be valid.

# The golden values are updated whenever there is a new release of the CC VM
# firmware (such as SVSM or OVMF) or any updates to the Linux environment
# (kernel, command-line arguments, or initramfs).
EXPECTED_SNP_REPORT_HASH="4b0b323cf8bb3dc227a838978a6d6d60070dcffea67712472204cabd46ad0eba"
EXPECTED_TPM_POLICY="1e2936b21eb61bffa6367dc539e8ec5f498f530593f77de6fe68c1c70b454040"

# A mismatch between the actual values obtained from the remote host and the
# expected golden values may indicate that the confidential computing VM has
# been tampered with.

if [ $# -eq 0 ];then
  echo "Usage: $0 user@host"
  exit 1
fi

SSH_ARGS="-o StrictHostKeyChecking=no"
SSH=$1

# Step 1:
# Verify that the "Measurement" value in the AMD SEV-SNP attestation
# report matches the expected golden measurement.

# Request attestation report from the remote host
ssh ${SSH_ARGS} ${SSH} snpguest report attestation-report.bin random-request-file.txt --random --vmpl 2
scp ${SSH}:~/attestation-report.bin ./

# Pull certs from CA + VCEK
snpguest fetch ca der milan -e vcek ./certs-kds
snpguest fetch vcek der milan ./certs-kds attestation-report.bin

# Verify certs
snpguest verify certs certs-kds

# Verify attestation report
snpguest verify attestation certs-kds/ attestation-report.bin

snpguest  display report attestation-report.bin | grep -A 4 Measurement: \
    | sha256sum  | grep -q ${EXPECTED_SNP_REPORT_HASH}

# Step 2:
# Verify the vTPM PCR policy by calculating it using all PCR banks.  Any
# unauthorized modification to system components will alter the PCR policy,
# which this script can detect.
# Note: A critical PCR bank is PCR bank 4, which measures the authenticode hash
# of the EFI PE/COFF binary. This binary contains the Linux kernel,
# command-line arguments, and initramfs combined into a single file, known as
# the Unified Kernel Image (UKI).
ssh ${SSH} tpm2_createpolicy --policy-pcr -l "sha256:all"  -L policy.file  | grep -q ${EXPECTED_TPM_POLICY}

figlet ATTESTED
echo The remote Confidential Computing VM has successfully passed AMD SEV-SNP and vTPM attestation.
