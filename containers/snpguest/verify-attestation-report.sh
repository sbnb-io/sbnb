#!/bin/bash

set -euxo pipefail

# Verify that the "Measurement" value in the AMD SEV-SNP attestation report
# matches the expected golden measurement. A mismatch in this value may
# indicate that the confidential computing VM has been tampered with.

# The golden measurement is updated whenever a new release of the CC VM
# firmware (such as SVSM or OVMF) is made.
EXPECTED_REPORT_HASH="4b0b323cf8bb3dc227a838978a6d6d60070dcffea67712472204cabd46ad0eba"

# Pull certs from CA + VCEK
snpguest fetch ca der milan -e vcek ./certs-kds
snpguest fetch vcek der milan ./certs-kds /host/attestation-report.bin

# Verify certs
snpguest verify certs certs-kds

# Verify attestation report
snpguest verify attestation certs-kds/ /host/attestation-report.bin

# Verify that the "Measurement" value in the AMD SEV-SNP attestation report
# matches the expected golden measurement.
snpguest  display report /host/attestation-report.bin | grep -A 4 Measurement: \
    | sha256sum  | grep -q ${EXPECTED_REPORT_HASH}
