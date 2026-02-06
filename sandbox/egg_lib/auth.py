"""Authentication and API key management for egg.

This module handles Anthropic API keys, GitHub tokens,
and related authentication utilities.
"""

import os
import sys
from pathlib import Path

from .config import Config
from .output import warn


def get_claude_oauth_token() -> str | None:
    """
    Get Claude OAuth token from environment or secrets.env.

    Returns:
        OAuth token string if found, None otherwise.
    """
    # Check environment variable first
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        token = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        # Skip placeholder tokens (used internally by gateway)
        if "PROXY-INJECTED" in token:
            return None
        return token

    # Check secrets.env
    secrets_file = Config.USER_CONFIG_DIR / "secrets.env"
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                return line.split("=", 1)[1].strip()

    return None


def get_anthropic_api_key() -> str | None:
    """
    Get Anthropic API key from environment, secrets.env, or legacy config file.

    Returns:
        API key string if found, None otherwise.
    """
    # Check environment variable first
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]

    # Check secrets.env
    secrets_file = Config.USER_CONFIG_DIR / "secrets.env"
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()

    # Legacy: check dedicated file
    api_key_file = Config.USER_CONFIG_DIR / "anthropic-api-key"
    if api_key_file.exists():
        return api_key_file.read_text().strip()

    return None


def get_anthropic_auth_method() -> str:
    """
    Get the Anthropic authentication method from environment, config, or secrets.

    Determines auth method in order of precedence:
    1. ANTHROPIC_AUTH_METHOD environment variable
    2. anthropic_auth_method in config.yaml
    3. Presence of CLAUDE_CODE_OAUTH_TOKEN in secrets.env -> 'oauth'
    4. Presence of ANTHROPIC_API_KEY in secrets.env -> 'api_key'
    5. Default to 'oauth' (recommended method)

    Returns:
        Auth method: 'api_key' or 'oauth'
    """
    import yaml

    # Check environment variable first
    method = os.environ.get("ANTHROPIC_AUTH_METHOD", "").lower()
    if method in ("api_key", "oauth"):
        return method

    # Check config.yaml
    config_file = Config.USER_CONFIG_DIR / "config.yaml"
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
                method = str(config.get("anthropic_auth_method", "")).lower()
                if method in ("api_key", "oauth"):
                    return method
        except Exception:
            pass

    # Infer from available credentials in secrets.env
    if get_claude_oauth_token():
        return "oauth"
    if get_anthropic_api_key():
        return "api_key"

    # Default to oauth (recommended for most users)
    return "oauth"


def get_github_token() -> str | None:
    """Get GitHub PAT using the unified HostConfig system.

    Uses HostConfig to load the token from (in order of precedence):
    - Environment variable GITHUB_TOKEN (highest priority)
    - ~/.config/egg/secrets.env (GITHUB_TOKEN=...)
    - ~/.config/egg/github-token (dedicated file)

    This follows the same configuration pattern as other egg secrets
    (Slack tokens, Confluence tokens, etc.) via config/host_config.py.

    Returns:
        Token string if found and valid, None otherwise
    """
    try:
        # Import HostConfig from project root
        script_dir = Path(__file__).resolve().parent.parent
        project_root = script_dir.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from config.host_config import HostConfig

        config = HostConfig()
        token: str | None = config.github_token

        if token and token.startswith(("ghp_", "github_pat_")):
            return token
    except ImportError as e:
        warn(f"Could not import HostConfig: {e}")
    except Exception as e:
        warn(f"Error loading GitHub token from config: {e}")
    return None


def get_github_readonly_token() -> str | None:
    """Get read-only GitHub token for external repositories.

    This token is used for repos outside the primary token's scope,
    such as external-org/repo.

    Returns:
        Token string if found, None otherwise
    """
    try:
        script_dir = Path(__file__).resolve().parent.parent
        project_root = script_dir.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from config.host_config import HostConfig

        config = HostConfig()
        # Note: github_readonly_token falls back to github_token if not set
        token: str | None = config.get_secret("GITHUB_READONLY_TOKEN")
        if token:
            return token
    except ImportError:
        pass
    except Exception:
        pass
    return None
