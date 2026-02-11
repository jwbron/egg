"""Configuration and constants for egg.

This module contains the Config class, Colors, gateway constants,
and platform detection utilities.

Gateway and network constants are imported from the shared egg_config module
to ensure consistency across the codebase. See shared/egg_config/constants.py
for the authoritative definitions.
"""

import os
import sys
from pathlib import Path

# Import constants from shared module (installed as egg_config)
# Try installed package first, fall back to relative import for development
try:
    from egg_config.constants import (
        EGG_CONTAINER_IP,
        EGG_EXTERNAL_NETWORK,
        EGG_EXTERNAL_SUBNET,
        EGG_ISOLATED_NETWORK,
        EGG_ISOLATED_SUBNET,
        GATEWAY_CONTAINER_NAME,
        GATEWAY_EXTERNAL_IP,
        GATEWAY_IMAGE_NAME,
        GATEWAY_ISOLATED_IP,
        GATEWAY_PORT,
        GATEWAY_PROXY_PORT,
        ORCHESTRATOR_CONTAINER_NAME,
        ORCHESTRATOR_EXTERNAL_IP,
        ORCHESTRATOR_IMAGE_NAME,
        ORCHESTRATOR_ISOLATED_IP,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    # Development fallback: add shared/ to path
    _shared_dir = Path(__file__).parent.parent.parent / "shared"
    if str(_shared_dir) not in sys.path:
        sys.path.insert(0, str(_shared_dir))
    from egg_config.constants import (
        EGG_CONTAINER_IP,
        EGG_EXTERNAL_NETWORK,
        EGG_EXTERNAL_SUBNET,
        EGG_ISOLATED_NETWORK,
        EGG_ISOLATED_SUBNET,
        GATEWAY_CONTAINER_NAME,
        GATEWAY_EXTERNAL_IP,
        GATEWAY_IMAGE_NAME,
        GATEWAY_ISOLATED_IP,
        GATEWAY_PORT,
        GATEWAY_PROXY_PORT,
        ORCHESTRATOR_CONTAINER_NAME,
        ORCHESTRATOR_EXTERNAL_IP,
        ORCHESTRATOR_IMAGE_NAME,
        ORCHESTRATOR_ISOLATED_IP,
        ORCHESTRATOR_PORT,
    )


class Colors:
    """ANSI color codes for terminal output"""

    BLUE = "\033[0;34m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    BOLD = "\033[1m"
    NC = "\033[0m"


class Config:
    """Configuration paths and constants"""

    # Cache directory for Docker staging (XDG-compliant)
    # Respects XDG_CACHE_HOME if set
    _xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    CACHE_DIR = _xdg_cache / "egg"
    CONFIG_DIR = CACHE_DIR  # Alias for backward compatibility
    DOCKERFILE = CONFIG_DIR / "Dockerfile"
    USER_CONFIG_DIR = Path.home() / ".config" / "egg"  # User config (secrets, preferences)
    REPOS_CONFIG_FILE = USER_CONFIG_DIR / "repositories.yaml"
    GITHUB_TOKEN_FILE = USER_CONFIG_DIR / "github-token"
    IMAGE_NAME = "egg"
    CONTAINER_NAME = "egg"

    # Directories that are dangerous to mount (contain credentials)
    DANGEROUS_DIRS = [
        Path.home() / ".ssh",
        Path.home() / ".config" / "gcloud",
        Path.home() / ".gitconfig",
        Path.home() / ".netrc",
        Path.home() / ".aws",
        Path.home() / ".kube",
        Path.home() / ".gnupg",
        Path.home() / ".docker",
    ]


# Re-export gateway and network constants for backward compatibility.
# These are imported from egg_config.constants at the top of this file.
# See shared/egg_config/constants.py for the authoritative definitions.
__all__ = [
    "Colors",
    "Config",
    "EGG_CONTAINER_IP",
    "EGG_EXTERNAL_NETWORK",
    "EGG_EXTERNAL_SUBNET",
    "EGG_ISOLATED_NETWORK",
    "EGG_ISOLATED_SUBNET",
    "GATEWAY_CONTAINER_NAME",
    "GATEWAY_EXTERNAL_IP",
    "GATEWAY_IMAGE_NAME",
    "GATEWAY_ISOLATED_IP",
    "GATEWAY_PORT",
    "GATEWAY_PROXY_PORT",
    "ORCHESTRATOR_CONTAINER_NAME",
    "ORCHESTRATOR_EXTERNAL_IP",
    "ORCHESTRATOR_IMAGE_NAME",
    "ORCHESTRATOR_ISOLATED_IP",
    "ORCHESTRATOR_PORT",
    "get_local_repos",
    "get_platform",
]


def get_platform() -> str:
    """Detect platform: linux or macos"""
    import platform

    system = platform.system().lower()
    if system == "linux":
        return "linux"
    elif system == "darwin":
        return "macos"
    return "unknown"


# Import shared config module for get_local_repos
# This ensures egg and gateway use identical config parsing
def get_local_repos(config_file: Path | None = None) -> list[Path]:
    """Load local repository paths from configuration.

    Uses the shared egg_config module for consistent config parsing.
    Falls back to local implementation if module not available.

    Args:
        config_file: Optional path to config file. If not provided, uses
                    the default at ~/.config/egg/repositories.yaml
    """
    try:
        # Try to import from shared module
        import sys

        _script_dir = Path(__file__).parent.parent.resolve()
        if str(_script_dir) not in sys.path:
            sys.path.insert(0, str(_script_dir))
        from egg_config import get_local_repos as _get_local_repos

        result: list[Path] = _get_local_repos(config_file=config_file)
        return result
    except ImportError:
        pass

    # Fallback implementation if shared module not available
    config_path = config_file or Config.REPOS_CONFIG_FILE
    if not config_path.exists():
        return []
    try:
        import yaml

        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        local_repos_config = config.get("local_repos", {})
        paths = local_repos_config.get("paths", []) if isinstance(local_repos_config, dict) else []
        result = []
        for path_str in paths:
            path = Path(path_str).expanduser().resolve()
            if path.exists() and path.is_dir():
                result.append(path)
        return result
    except Exception:
        return []
