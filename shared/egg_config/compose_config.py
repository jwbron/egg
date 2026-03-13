#!/usr/bin/env python3
"""Read ~/.config/egg/config.yaml and emit KEY=VALUE lines for docker-compose.

This module bridges the config.yaml settings to environment variables that
docker-compose.yml expects. It also reads secrets from secrets.env and
dedicated secret files (launcher-secret, github-token).

Usage from shell:
    eval "$(python3 shared/egg_config/compose_config.py [config_dir])"

The config_dir defaults to ~/.config/egg if not provided.
"""

import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    from egg_config.constants import (
        GATEWAY_PORT,
        GATEWAY_PROXY_PORT,
        MCP_SERVER_PORT,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    # Standalone script execution: add shared/ to path
    _shared_dir = Path(__file__).parent.parent
    if str(_shared_dir) not in sys.path:
        sys.path.insert(0, str(_shared_dir))
    from egg_config.constants import (
        GATEWAY_PORT,
        GATEWAY_PROXY_PORT,
        MCP_SERVER_PORT,
        ORCHESTRATOR_PORT,
    )


# Mapping from config.yaml keys to environment variable names.
# Each entry is (yaml_key, env_var_name, default_value).
CONFIG_KEY_MAP: list[tuple[str, str, str | None]] = [
    ("host_home", "HOST_HOME", None),
    ("host_uid", "HOST_UID", "1000"),
    ("host_gid", "HOST_GID", "1000"),
    ("git_name", "EGG_USER_GIT_NAME", "egg"),
    ("git_email", "EGG_USER_GIT_EMAIL", "egg@localhost"),
    ("compose_project_name", "COMPOSE_PROJECT_NAME", "egg"),
    ("gateway_api_port", "GATEWAY_API_PORT", str(GATEWAY_PORT)),
    ("gateway_proxy_port", "GATEWAY_PROXY_PORT", str(GATEWAY_PROXY_PORT)),
    ("orchestrator_api_port", "ORCHESTRATOR_API_PORT", str(ORCHESTRATOR_PORT)),
    ("mcp_server_port", "EGG_MCP_SERVER_PORT", str(MCP_SERVER_PORT)),
    ("mcp_rate_limit", "EGG_MCP_RATE_LIMIT", "30"),
    # Image overrides (optional)
    ("gateway_image", "EGG_GATEWAY_IMAGE", None),
    ("orchestrator_image", "EGG_ORCHESTRATOR_IMAGE", None),
    ("sandbox_image", "EGG_SANDBOX_IMAGE", None),
    # Bot mode (optional)
    ("gateway_bot_name", "GATEWAY_BOT_NAME", None),
    ("gateway_bot_branch_prefix", "GATEWAY_BOT_BRANCH_PREFIX", None),
    ("gateway_trusted_users", "GATEWAY_TRUSTED_USERS", None),
]

# Secret keys read from secrets.env (never stored in config.yaml)
SECRET_KEYS = [
    "EGG_LAUNCHER_SECRET",
    "GITHUB_USER_TOKEN",
    "BOT_GITHUB_TOKEN",
]


def _read_secrets_env(config_dir: Path) -> dict[str, str]:
    """Read secrets from secrets.env file."""
    secrets: dict[str, str] = {}
    secrets_file = config_dir / "secrets.env"
    if not secrets_file.exists():
        return secrets

    with open(secrets_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                secrets[key] = value

    return secrets


def _read_secret_file(config_dir: Path, filename: str) -> str | None:
    """Read a single-value secret from a dedicated file."""
    path = config_dir / filename
    if path.exists():
        content = path.read_text().strip()
        if content:
            return content
    return None


def load_compose_env(config_dir: Path | None = None) -> dict[str, str]:
    """Read config.yaml + secrets and return env vars for docker-compose.

    Priority (highest to lowest):
    1. Existing environment variables (not overridden)
    2. Secrets from secrets.env and dedicated files
    3. Values from config.yaml
    4. Built-in defaults

    Args:
        config_dir: Path to config directory. Defaults to ~/.config/egg.

    Returns:
        Dictionary of environment variable name -> value.
    """
    if config_dir is None:
        config_dir = Path(os.environ.get("EGG_CONFIG_DIR", Path.home() / ".config" / "egg"))

    config_dir = Path(config_dir)
    result: dict[str, str] = {}

    # 1. Read config.yaml
    config_file = config_dir / "config.yaml"
    config: dict[str, Any] = {}
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Failed to read {config_file}: {e}", file=sys.stderr)

    # 2. Map config.yaml keys to env vars
    for yaml_key, env_var, default in CONFIG_KEY_MAP:
        value = config.get(yaml_key)
        if value is not None:
            result[env_var] = str(value)
        elif default is not None:
            result[env_var] = default

    # 3. Always set EGG_CONFIG_DIR
    result["EGG_CONFIG_DIR"] = str(config_dir)

    # 4. Read secrets from secrets.env
    secrets = _read_secrets_env(config_dir)
    for key in SECRET_KEYS:
        if key in secrets and secrets[key]:
            result[key] = secrets[key]

    # 5. Read dedicated secret files (override secrets.env values)
    launcher_secret = _read_secret_file(config_dir, "launcher-secret")
    if launcher_secret:
        result["EGG_LAUNCHER_SECRET"] = launcher_secret

    github_token = _read_secret_file(config_dir, "github-token")
    if github_token:
        result["GITHUB_USER_TOKEN"] = github_token

    return result


def main() -> None:
    """CLI entrypoint: print conditional export statements for shell eval.

    Emits ``[ -z "${KEY+x}" ] && export KEY='value'`` so that pre-existing
    environment variables are not overridden, matching the priority documented
    in :func:`load_compose_env`.
    """
    config_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    env_vars = load_compose_env(config_dir)

    for key, value in sorted(env_vars.items()):
        # Shell-safe quoting: single quotes with escaped single quotes
        safe_value = value.replace("'", "'\\''")
        # Only set if not already in the environment (honour existing vars)
        print(f"[ -z \"${{{key}+x}}\" ] && export {key}='{safe_value}'")


if __name__ == "__main__":
    main()
