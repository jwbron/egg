"""
Utility functions for configuration loading.

This module provides common utilities used by service configs:
- load_env_file: Parse .env style files
- load_yaml_file: Parse YAML config files
- safe_int: Parse integers with fallback
"""

from pathlib import Path
from typing import Any


def load_env_file(path: Path) -> dict[str, str]:
    """Load a .env style file into a dictionary.

    Parses files with KEY=VALUE format, supporting:
    - Comments starting with #
    - Quoted values (single or double quotes)
    - Empty lines

    Args:
        path: Path to the .env file

    Returns:
        Dictionary of key-value pairs
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    result[key] = value
    except Exception:
        pass

    return result


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file into a dictionary.

    Args:
        path: Path to the YAML file

    Returns:
        Dictionary from YAML content, or empty dict on error
    """
    if not path.exists():
        return {}

    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def safe_int(value: str | None, default: int = 0) -> int:
    """Safely parse an integer from a string.

    Args:
        value: String to parse (can be None)
        default: Default value if parsing fails

    Returns:
        Parsed integer or default value
    """
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_bool(value: str | None, default: bool = False) -> bool:
    """Safely parse a boolean from a string.

    Recognizes: true, false, yes, no, 1, 0 (case-insensitive)

    Args:
        value: String to parse (can be None)
        default: Default value if parsing fails

    Returns:
        Parsed boolean or default value
    """
    if value is None:
        return default

    value_lower = value.lower().strip()
    if value_lower in ("true", "yes", "1", "on"):
        return True
    if value_lower in ("false", "no", "0", "off"):
        return False
    return default
