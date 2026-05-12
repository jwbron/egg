"""Host-side helpers for the sandbox launcher.

The interactive ``egg --setup`` flow was removed along with the rest of
interactive mode (#1762). What remains is the lightweight host check
used by the GHA exec path and the standard mount helper.
"""

import os

from .config import Config
from .output import info, warn


def check_host_setup() -> bool:
    """Check if host setup is complete for standalone egg.

    Standalone egg requires minimal setup:
    - Gateway will be started on-demand (containerized)
    - Directories are auto-created if missing
    - Config file is optional
    """
    # Auto-create config directory
    Config.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Check if config exists (warning only, not critical)
    config_file = Config.USER_CONFIG_DIR / "repositories.yaml"
    if not config_file.exists():
        warn(f"Configuration file not found: {config_file}")
        info("Create one from config/repositories.yaml.example to configure repositories")
        print()

    return True


def add_standard_mounts(mount_args: list[str], quiet: bool = False) -> None:
    """Add standard mounts (shared-certs) to mount_args list.

    These mounts are always added dynamically rather than relying on config files,
    ensuring they're always available even if setup hasn't been run recently.
    """
    # Mount the shared certs Docker named volume for the gateway CA certificate.
    # The gateway (whether started via docker-compose or programmatically) writes
    # its CA cert to the '{project_name}-certs' named volume at /shared/certs.
    # Sandbox containers must mount the same volume (not a host path) to always
    # see the current cert. Using a host bind-mount (~/.egg-shared-certs) diverges
    # from the volume the gateway writes to and results in stale/expired certs.
    project_name = os.environ.get("COMPOSE_PROJECT_NAME", "egg")
    certs_volume = f"{project_name}-certs"
    mount_args.extend(["-v", f"{certs_volume}:/shared/certs:ro"])
    if not quiet:
        print("  - /shared/certs/ (gateway CA cert - read-only)")
