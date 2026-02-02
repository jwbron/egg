"""Configuration validation for gateway startup.

Validates all required configuration at startup to fail fast with clear errors.
This validates the network lockdown implementation.

Security Model (PRIVATE_MODE):
- PRIVATE_MODE=true: Network locked down (Anthropic API only) + private repos only
- PRIVATE_MODE=false: Full internet access + public repos only (default)

This single flag ensures you can't accidentally combine open network with
private repo access (a security anti-pattern that could lead to data exfiltration).
"""

import os
import sys
from pathlib import Path


class ConfigError(Exception):
    """Raised when configuration validation fails."""


def validate_config(
    secrets_dir: Path | None = None,
    squid_conf_path: Path | None = None,
) -> None:
    """Validate all gateway configuration at startup.

    Checks:
    - Required secrets exist
    - Squid configuration is valid
    - Allowed domains file exists and has content (in private mode)

    Raises:
        ConfigError: If any validation fails
    """
    errors: list[str] = []

    secrets_dir = secrets_dir or Path("/secrets")

    # Check for required secrets
    if secrets_dir.is_dir():
        launcher_secret_file = secrets_dir / "launcher-secret"
        if not launcher_secret_file.is_file():
            errors.append(
                f"Launcher secret not found: {launcher_secret_file}\n"
                "  Run setup.sh to generate launcher secret"
            )
    else:
        errors.append(
            f"Secrets directory not mounted: {secrets_dir}\n"
            "  Ensure secrets directory is mounted"
        )

    # Validate Squid configuration (optional - only if using proxy)
    squid_conf = squid_conf_path or Path("/etc/squid/squid.conf")
    if squid_conf.parent.exists():
        if not squid_conf.is_file():
            errors.append(
                f"Squid configuration not found: {squid_conf}\n"
                "  This file is required for network lockdown"
            )

        squid_allow_all_conf = squid_conf.parent / "squid-allow-all.conf"
        if not squid_allow_all_conf.is_file():
            errors.append(
                f"Squid allow-all configuration not found: {squid_allow_all_conf}\n"
                "  This file is required for public mode"
            )

        domains_file = squid_conf.parent / "allowed_domains.txt"
        if not domains_file.is_file():
            errors.append(
                f"Allowed domains file not found: {domains_file}\n"
                "  This file must be present for private mode"
            )
        else:
            try:
                with open(domains_file) as f:
                    domains = [
                        line.strip()
                        for line in f
                        if line.strip() and not line.strip().startswith("#")
                    ]
                if not domains:
                    errors.append(
                        "Allowed domains file is empty (no domains configured)\n"
                        "  At minimum, api.anthropic.com is required for private mode"
                    )
            except Exception as e:
                errors.append(f"Failed to read allowed domains file: {e}")

        squid_cert = squid_conf.parent / "squid-ca.pem"
        if not squid_cert.is_file():
            errors.append(
                f"Squid CA certificate not found: {squid_cert}\n"
                "  This certificate is required for SNI inspection"
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise ConfigError(f"{len(errors)} configuration error(s) found")


def validate_network_lockdown_mode(squid_conf_dir: Path | None = None) -> bool:
    """Check if network lockdown mode components are properly configured.

    Returns:
        True if all lockdown components are present
    """
    squid_dir = squid_conf_dir or Path("/etc/squid")

    squid_conf = (squid_dir / "squid.conf").is_file()
    domains_file = (squid_dir / "allowed_domains.txt").is_file()
    squid_cert = (squid_dir / "squid-ca.pem").is_file()

    return squid_conf and domains_file and squid_cert


def is_private_mode_enabled() -> bool:
    """Check if private mode is enabled.

    PRIVATE_MODE controls BOTH network access AND repository visibility:
    - true: Private repos only + network locked down (Anthropic API only)
    - false: Public repos only + full internet access (default)
    """
    value = os.environ.get("PRIVATE_MODE", "false").lower().strip()
    return value in ("true", "1", "yes")


if __name__ == "__main__":
    try:
        validate_config()
        print("Configuration validation passed")

        if is_private_mode_enabled():
            print("Mode: PRIVATE (locked network + private repos only)")
            if validate_network_lockdown_mode():
                print("  Network lockdown components: READY")
            else:
                print("  WARNING: Network lockdown components missing")
        else:
            print("Mode: PUBLIC (full internet + public repos only)")
            if Path("/etc/squid/squid-allow-all.conf").is_file():
                print("  Allow-all configuration: READY")
            else:
                print("  WARNING: squid-allow-all.conf not found")

        sys.exit(0)
    except ConfigError:
        sys.exit(1)
