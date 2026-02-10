"""Configuration validation for gateway startup.

Validates all required configuration at startup to fail fast with clear errors.
This validates the network lockdown implementation.

Reference: ADR-Internet-Tool-Access-Lockdown.md

Security Model:
- Gateway always runs with locked-down Squid configuration
- Per-container mode (private/public) is determined by session, not gateway config
- Private containers route through proxy, public containers bypass it
"""

import sys
from pathlib import Path


class ConfigError(Exception):
    """Raised when configuration validation fails."""


def validate_config() -> None:
    """Validate all gateway configuration at startup.

    Checks:
    - Required secrets exist
    - Squid configuration is valid
    - Allowed domains file exists and has content (in private mode)

    Raises:
        ConfigError: If any validation fails
    """
    errors: list[str] = []

    # Check for required secrets
    secrets_dir = Path("/secrets")
    if secrets_dir.is_dir():
        # Note: GitHub tokens are now managed in-memory by token_refresher.py
        # We don't check for .github-token file here anymore

        # Launcher secret (for session management authentication)
        launcher_secret_file = secrets_dir / "launcher-secret"
        if not launcher_secret_file.is_file():
            errors.append(
                "Launcher secret not found: /secrets/launcher-secret\n"
                "  Run `egg --setup` to generate launcher secret"
            )
    else:
        errors.append(
            "Secrets directory not mounted: /secrets\n  Ensure ~/.config/egg is mounted at /secrets"
        )

    # Validate Squid configuration
    # In private mode, we use squid.conf (locked down)
    # In public mode, we use squid-allow-all.conf (full internet)
    squid_conf = Path("/etc/squid/squid.conf")
    if not squid_conf.is_file():
        errors.append(
            "Squid configuration not found: /etc/squid/squid.conf\n"
            "  This file is required for network lockdown"
        )

    squid_allow_all_conf = Path("/etc/squid/squid-allow-all.conf")
    if not squid_allow_all_conf.is_file():
        errors.append(
            "Squid allow-all configuration not found: /etc/squid/squid-allow-all.conf\n"
            "  This file is required for public mode"
        )

    # Allowed domains file (only required for private mode, but should exist)
    domains_file = Path("/etc/squid/allowed_domains.txt")
    if not domains_file.is_file():
        errors.append(
            "Allowed domains file not found: /etc/squid/allowed_domains.txt\n"
            "  This file must be present for private mode"
        )
    else:
        # Check file has actual domains (not just comments)
        try:
            with open(domains_file) as f:
                domains = [
                    line.strip() for line in f if line.strip() and not line.strip().startswith("#")
                ]
            if not domains:
                errors.append(
                    "Allowed domains file is empty (no domains configured)\n"
                    "  At minimum, api.anthropic.com is required for private mode"
                )
        except Exception as e:
            errors.append(f"Failed to read allowed domains file: {e}")

    # Check Squid CA certificate
    squid_cert = Path("/etc/squid/squid-ca.pem")
    if not squid_cert.is_file():
        errors.append(
            "Squid CA certificate not found: /etc/squid/squid-ca.pem\n"
            "  This certificate is required for SNI inspection"
        )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise ConfigError(f"{len(errors)} configuration error(s) found")


def validate_network_lockdown_mode() -> bool:
    """Check if network lockdown mode components are properly configured.

    Returns:
        True if all lockdown components are present
    """
    # Verify all required components
    squid_conf = Path("/etc/squid/squid.conf").is_file()
    domains_file = Path("/etc/squid/allowed_domains.txt").is_file()
    squid_cert = Path("/etc/squid/squid-ca.pem").is_file()

    return squid_conf and domains_file and squid_cert


if __name__ == "__main__":
    try:
        validate_config()
        print("Configuration validation passed")

        if validate_network_lockdown_mode():
            print("  Network lockdown components: READY")
        else:
            print("  WARNING: Network lockdown components missing")

        sys.exit(0)
    except ConfigError:
        sys.exit(1)
