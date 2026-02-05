"""Configuration and constants for egg.

This module contains the Config class, Colors, gateway constants,
and platform detection utilities.
"""

import os
from pathlib import Path


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


# Gateway container constants (containerized gateway sidecar)
GATEWAY_CONTAINER_NAME = "egg-gateway"
GATEWAY_IMAGE_NAME = "egg-gateway"
GATEWAY_PORT = 9848
GATEWAY_PROXY_PORT = 3129

# Network lockdown configuration
# Dual-network architecture: egg-isolated (internal) + egg-external (for gateway)
# egg container connects only to egg-isolated and routes all traffic through gateway proxy
EGG_ISOLATED_NETWORK = "egg-isolated"
EGG_EXTERNAL_NETWORK = "egg-external"
EGG_ISOLATED_SUBNET = "172.32.0.0/24"  # Subnet for egg-isolated network
EGG_EXTERNAL_SUBNET = "172.33.0.0/24"  # Subnet for egg-external network
EGG_CONTAINER_IP = "172.32.0.10"  # Fixed IP for egg container in isolated network
GATEWAY_ISOLATED_IP = "172.32.0.2"  # Gateway IP in isolated network
GATEWAY_EXTERNAL_IP = "172.33.0.2"  # Gateway IP in external network


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
def get_local_repos() -> list[Path]:
    """Load local repository paths from configuration.

    Uses the shared egg_config module for consistent config parsing.
    Falls back to local implementation if module not available.
    """
    try:
        # Try to import from shared module
        import sys

        _script_dir = Path(__file__).parent.parent.resolve()
        if str(_script_dir) not in sys.path:
            sys.path.insert(0, str(_script_dir))
        from egg_config import get_local_repos as _get_local_repos

        return _get_local_repos()
    except ImportError:
        pass

    # Fallback implementation if shared module not available
    if not Config.REPOS_CONFIG_FILE.exists():
        return []
    try:
        import yaml

        with open(Config.REPOS_CONFIG_FILE) as f:
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
