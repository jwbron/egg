"""Configuration file loading for egg.

Loads configuration from YAML files with environment variable support.
"""

import os
from pathlib import Path
from typing import Any

import yaml


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand environment variables in config values."""
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    elif isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML configuration file.

    Args:
        path: Path to the YAML file

    Returns:
        Parsed configuration dictionary

    Raises:
        FileNotFoundError: If the file doesn't exist
        yaml.YAMLError: If the file is not valid YAML
    """
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    result: dict[str, Any] = _expand_env_vars(config)
    return result


def find_config_file(
    name: str = "egg.yaml",
    env_var: str = "EGG_CONFIG",
    search_paths: list[Path] | None = None,
) -> Path | None:
    """Find a configuration file.

    Search order:
    1. Environment variable (if set)
    2. Current directory
    3. ~/.config/egg/
    4. Additional search paths (if provided)

    Args:
        name: Configuration file name
        env_var: Environment variable to check first
        search_paths: Additional paths to search

    Returns:
        Path to config file, or None if not found
    """
    # Check environment variable first
    if env_var and (env_path := os.environ.get(env_var)):
        path = Path(env_path)
        if path.exists():
            return path

    # Build search path list
    paths_to_check = [
        Path.cwd() / name,
        Path.home() / ".config" / "egg" / name,
    ]
    if search_paths:
        paths_to_check.extend(search_paths)

    for path in paths_to_check:
        if path.exists():
            return path

    return None


def load_config(
    config_path: Path | None = None,
    secrets_path: Path | None = None,
) -> dict[str, Any]:
    """Load configuration and secrets.

    Args:
        config_path: Path to egg.yaml (auto-discovered if None)
        secrets_path: Path to secrets.yaml (auto-discovered if None)

    Returns:
        Merged configuration dictionary
    """
    config: dict[str, Any] = {}

    # Load main config
    if config_path is None:
        config_path = find_config_file("egg.yaml", "EGG_CONFIG")
    if config_path:
        config = load_yaml(config_path)

    # Load secrets
    if secrets_path is None:
        secrets_path = find_config_file("secrets.yaml", "EGG_SECRETS")
    if secrets_path:
        secrets = load_yaml(secrets_path)
        config["secrets"] = secrets.get("secrets", secrets)

    return config
