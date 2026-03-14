#!/bin/bash
# Generate DH parameters for SSL bump (one-time, at build time)
#
# Using 2048-bit DH params as a balance between security and build time:
# - 2048-bit: ~30 seconds to generate, NIST-approved through 2030
# - 4096-bit: ~5-10 minutes to generate, marginally more secure
#
# For ephemeral key exchange in a local proxy between sandbox and gateway
# on a private network, 2048-bit provides adequate security.

set -euo pipefail

DH_FILE="/etc/squid/certs/dhparam.pem"

if [[ -f "$DH_FILE" ]]; then
    echo "DH parameters already exist"
    exit 0
fi

mkdir -p "$(dirname "$DH_FILE")"

echo "Generating DH parameters (2048-bit, ~30 seconds)..."
openssl dhparam -out "$DH_FILE" 2048 2>/dev/null

chmod 644 "$DH_FILE"
echo "DH parameters generated: $DH_FILE"
