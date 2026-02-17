"""
Configuration utilities for egg.

Provides configuration loading from ~/.config/egg/ for both the egg launcher
and the gateway sidecar setup script.
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


class Config:
    """Configuration paths for egg."""

    # User configuration directory
    USER_CONFIG_DIR = Path.home() / ".config" / "egg"

    # Repository configuration file
    REPOS_CONFIG_FILE = USER_CONFIG_DIR / "repositories.yaml"


def get_repos_config_file() -> Path:
    """Return the path to the repositories configuration file."""
    return Config.REPOS_CONFIG_FILE


def get_local_repos(config_file: Path | None = None) -> list[Path]:
    """Load local repository paths from configuration.

    Reads from ~/.config/egg/repositories.yaml and returns the list of
    configured local repository paths.

    Args:
        config_file: Optional path to config file. If not provided, uses
                    the default at ~/.config/egg/repositories.yaml

    Returns:
        List of Path objects for configured local repositories
    """
    config_path = config_file or Config.REPOS_CONFIG_FILE

    if not config_path.exists():
        return []

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        local_repos_config = config.get("local_repos", {})
        paths = local_repos_config.get("paths", []) if isinstance(local_repos_config, dict) else []

        # Convert to Path objects and filter existing directories
        result = []
        for path_str in paths:
            path = Path(path_str).expanduser().resolve()
            if path.exists() and path.is_dir():
                result.append(path)
        return result
    except Exception as e:
        print(f"Failed to load repository config: {e}", file=sys.stderr)
        return []


def main() -> None:
    """CLI entrypoint to get configured repo paths.

    When called from shell, prints one repo path per line.

    Usage:
        python -m egg_config.config  # Print all configured repo paths
    """
    for repo_path in get_local_repos():
        print(repo_path)


if __name__ == "__main__":
    main()
