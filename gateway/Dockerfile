FROM python:3.11-slim

# Install git, gh CLI, and Squid proxy for Phase 2 network lockdown
RUN apt-get update && apt-get install -y \
    git curl gnupg \
    # Squid HTTP proxy with SSL support for SNI inspection
    squid-openssl \
    # OpenSSL for certificate generation
    openssl \
    # gosu for dropping privileges (Squid needs root start, gateway needs non-root)
    gosu \
    && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
    https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Create directories for SSL bump certificates and Squid operation
# Note: CA certificate is generated at runtime by entrypoint.sh (daily rotation)
# DH parameters are generated at build time (slow, one-time operation)
# Note: /var/lib/squid must exist with proper permissions for security_file_certgen
RUN mkdir -p /etc/squid/certs /etc/squid/ssl /var/log/squid /var/spool/squid /var/lib/squid && \
    chown -R proxy:proxy /etc/squid/certs /etc/squid/ssl /var/log/squid /var/spool/squid /var/lib/squid

# Copy certificate generation scripts
COPY gateway/scripts/generate-ca-cert.sh /usr/local/bin/
COPY gateway/scripts/generate-dhparam.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/generate-*.sh

# Generate DH parameters at build time (slow operation, ~30 seconds)
RUN /usr/local/bin/generate-dhparam.sh

# Initialize SSL certificate database for dynamically generated certificates
# Required for generate-host-certificates=on in squid.conf
RUN /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 16MB && \
    chown -R proxy:proxy /var/lib/squid/ssl_db

# Initialize Squid cache directories
RUN /usr/sbin/squid -z -N 2>/dev/null || true

# Create egg user with UID/GID 1000 so gosu can resolve HOME correctly
# Without this, gosu sets HOME=/ when dropping to UID 1000, breaking Path.home()
# This matches the sandbox user setup for consistency
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg

WORKDIR /app

# Copy gateway code
COPY gateway/*.py ./

# Copy shared modules (egg_logging for logging, egg_config for config parsing)
COPY shared/egg_logging/ ./egg_logging/
COPY shared/egg_config/ ./egg_config/

# Copy config module (repo_config needed by github_client for user mode)
COPY config/repo_config.py /config/

# Copy Squid configuration for network lockdown
# squid.conf is the restrictive allowlist-based config (used in private mode)
# squid-allow-all.conf allows full internet access (used in public mode)
COPY gateway/squid.conf /etc/squid/squid.conf
COPY gateway/squid-allow-all.conf /etc/squid/squid-allow-all.conf
COPY gateway/allowed_domains.txt /etc/squid/allowed_domains.txt
RUN chmod 644 /etc/squid/squid.conf /etc/squid/squid-allow-all.conf /etc/squid/allowed_domains.txt

# Copy config validator for startup checks
COPY gateway/config_validator.py ./config_validator.py

# Install dependencies
# httpx: HTTP client with streaming support for Anthropic API proxy
# pre-commit: Required to run pre-commit hooks when gateway executes git commit on repos
RUN pip install --no-cache-dir flask waitress pyyaml requests PyJWT cryptography httpx pre-commit

ENV PYTHONPATH="/app"
# Expose both gateway API port and Squid proxy port
EXPOSE 9847 3128

COPY gateway/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
