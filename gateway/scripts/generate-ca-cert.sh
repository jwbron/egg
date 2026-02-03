#!/bin/bash
# Generate CA certificate for SSL bump (credential injection)
#
# Called by entrypoint.sh on gateway startup.
# Creates a short-lived CA certificate used for MITM on api.anthropic.com.
#
# Security notes:
# - CA key never leaves gateway container
# - Certificate is daily-rotated (not per-restart to avoid breaking in-flight requests)
# - Key permissions: 0600, owned by proxy user

set -euo pipefail

CA_CERT_DIR="/etc/squid/certs"
CA_CERT="${CA_CERT_DIR}/gateway-ca.pem"
CA_KEY="${CA_CERT_DIR}/gateway-ca.key"
CA_VALIDITY_DAYS=1  # Daily rotation

mkdir -p "$CA_CERT_DIR"

# Check if cert exists and is still valid (expires within 2 hours)
if [[ -f "$CA_CERT" && -f "$CA_KEY" ]]; then
    if openssl x509 -checkend 7200 -noout -in "$CA_CERT" 2>/dev/null; then
        echo "CA certificate still valid, skipping generation"
        exit 0
    fi
    echo "CA certificate expiring soon, regenerating..."
fi

echo "Generating new CA certificate for SSL bump..."

# Generate CA private key (ECDSA for performance)
openssl ecparam -genkey -name prime256v1 -out "$CA_KEY" 2>/dev/null

# Generate self-signed CA certificate
openssl req -new -x509 -sha256 \
    -key "$CA_KEY" \
    -out "$CA_CERT" \
    -days "$CA_VALIDITY_DAYS" \
    -subj "/CN=egg-gateway-ca/O=egg/OU=credential-injection" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    2>/dev/null

# Set restrictive permissions on private key
chmod 600 "$CA_KEY"
chmod 644 "$CA_CERT"

# Export public cert for container trust store (separate file with .crt extension)
cp "$CA_CERT" "${CA_CERT_DIR}/gateway-ca.crt"
chmod 644 "${CA_CERT_DIR}/gateway-ca.crt"

echo "CA certificate generated: $CA_CERT"
echo "Valid for $CA_VALIDITY_DAYS day(s)"
