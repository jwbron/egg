FROM python:3.13-slim

# Install git, gh CLI, and Squid proxy for Phase 2 network lockdown
RUN apt-get update && apt-get install -y \
    git curl gnupg \
    # Squid HTTP proxy with SSL support for SNI inspection
    squid-openssl \
    # OpenSSL for certificate generation
    openssl \
    # gosu for dropping privileges (Squid needs root start, gateway needs non-root)
    gosu \
    # procps provides pgrep/pkill for process management and health checks
    procps \
    # tini: minimal init system — reaps zombie processes and forwards signals.
    # Without tini, the Python gateway (PID 1) doesn't reap children, causing
    # zombie accumulation from Squid child processes and git/ssh subprocesses.
    tini \
    && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
    https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Create directories for SSL bump certificates and Squid operation
# Note: CA certificate is generated at runtime by entrypoint.sh (daily rotation)
# Note: /var/lib/squid must exist with proper permissions for security_file_certgen
RUN mkdir -p /etc/squid/certs /etc/squid/ssl /var/log/squid /var/spool/squid /var/lib/squid && \
    chown -R proxy:proxy /etc/squid/certs /etc/squid/ssl /var/log/squid /var/spool/squid /var/lib/squid

# Copy certificate generation scripts
COPY gateway/scripts/generate-ca-cert.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/generate-ca-cert.sh

# Note: DH parameter file generation removed. Squid's tls-dh directive now
# uses only the named curve (prime256v1) for ECDH key exchange. The previous
# dhparam.pem file caused OpenSSL error 0x1e08010c during Squid reconfigure
# when it tried to decode DH parameters as EC parameters. Modern TLS uses
# ECDH (not finite-field DH), so the named curve alone is sufficient.
# See: https://github.com/jwbron/egg/issues/1387

# Initialize SSL certificate database for dynamically generated certificates
# Required for generate-host-certificates=on in squid.conf
RUN /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 16MB && \
    chown -R proxy:proxy /var/lib/squid/ssl_db

# Initialize Squid cache directories
RUN /usr/sbin/squid -z -N 2>/dev/null || true

# Create egg user with UID/GID 1000 as default for local development.
# In CI (GitHub Actions), HOST_UID may differ (e.g. 1001); entrypoint.sh
# creates a passwd entry for the actual HOST_UID at runtime so gosu
# resolves HOME=/home/egg correctly instead of defaulting to HOME=/.
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg

WORKDIR /app

# Copy gateway code
COPY gateway/*.py ./

# Copy shared modules (egg_logging for logging, egg_config for config parsing, egg_contracts for contract API, egg_restrictions for agent role enforcement)
COPY shared/egg_logging/ ./egg_logging/
COPY shared/egg_config/ ./egg_config/
COPY shared/egg_contracts/ ./egg_contracts/
COPY shared/egg_restrictions/ ./egg_restrictions/

# Copy config module (repo_config needed by github_client for user mode)
# Copy to both /config/ (for package-style imports) and /app/ (for PYTHONPATH)
COPY config/repo_config.py /config/
COPY config/repo_config.py /app/

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
# pydantic: Required for egg_contracts module validation
RUN pip install --no-cache-dir flask waitress pyyaml requests PyJWT cryptography httpx pre-commit pydantic

ENV PYTHONPATH="/app"
# Expose gateway API port, Squid proxy port, and dedicated health check port
EXPOSE 9848 3129 9851

COPY gateway/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Use tini as PID 1 to reap zombie processes (git, ssh, squid children).
# Without this, the Python gateway doesn't call wait() on terminated children,
# leading to zombie process accumulation (50+ observed in production).
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
