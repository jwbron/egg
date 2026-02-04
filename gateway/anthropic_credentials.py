"""
Anthropic Credentials Manager for Gateway Sidecar.

Manages Anthropic API credentials (API key or OAuth token) for proxy injection.
Credentials are read from ~/.config/egg/secrets.env on the host.

Supported credential types:
- ANTHROPIC_API_KEY: Standard Anthropic API key (x-api-key header)
- ANTHROPIC_OAUTH_TOKEN: OAuth token from Claude Max subscription (Authorization: Bearer header)

OAuth token takes precedence if both are configured.
"""

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# Add shared directory to path for jib_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.anthropic-credentials")

# Default secrets path - can be overridden via environment variable
# Gateway runs on host, so this is the host user's config directory
SECRETS_PATH = Path(
    os.environ.get("JIB_SECRETS_PATH", Path.home() / ".config" / "egg" / "secrets.env")
)


@dataclass
class AnthropicCredential:
    """Container for Anthropic API credential."""

    header_name: str  # "x-api-key" or "Authorization"
    header_value: str  # The credential value (includes "Bearer " prefix for OAuth)

    @property
    def is_api_key(self) -> bool:
        return self.header_name == "x-api-key"

    @property
    def is_oauth(self) -> bool:
        return self.header_name == "Authorization"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dictionary.

    Handles:
    - KEY=value
    - KEY="quoted value"
    - KEY='quoted value'
    - # comments
    - Empty lines

    Args:
        path: Path to the .env file

    Returns:
        Dictionary of environment variables
    """
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                # Parse KEY=value
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                if key:
                    result[key] = value
    except OSError as e:
        logger.error("Failed to read secrets file", path=str(path), error=str(e))
    return result


class AnthropicCredentialsManager:
    """
    Manages Anthropic credentials for gateway proxy injection.

    Credentials are loaded from secrets.env on first access and cached.
    The cache is invalidated when the file's mtime changes.
    Thread-safe for concurrent request handling.
    """

    def __init__(self, secrets_path: Path | None = None):
        """
        Initialize the credentials manager.

        Args:
            secrets_path: Path to secrets.env file (default: ~/.config/egg/secrets.env)
        """
        self._secrets_path = secrets_path or SECRETS_PATH
        self._credential: AnthropicCredential | None = None
        self._cached_mtime: float = 0
        self._lock = threading.Lock()

    def get_credential(self) -> AnthropicCredential | None:
        """
        Get the Anthropic credential for API requests.

        Checks if the secrets file has changed and reloads if necessary.
        Thread-safe with mtime-based cache invalidation.

        Returns:
            AnthropicCredential if available, None if not configured.
        """
        try:
            current_mtime = self._secrets_path.stat().st_mtime
        except OSError:
            # File doesn't exist or can't be accessed
            with self._lock:
                self._credential = None
                self._cached_mtime = 0
            return None

        with self._lock:
            if current_mtime != self._cached_mtime:
                self._load_credential()
                self._cached_mtime = current_mtime
            return self._credential

    def _load_credential(self) -> None:
        """Load credential from secrets.env file."""
        if not self._secrets_path.exists():
            logger.warning("Secrets file not found", path=str(self._secrets_path))
            self._credential = None
            return

        secrets = parse_env_file(self._secrets_path)
        if not secrets:
            logger.warning("Secrets file is empty or could not be parsed")
            self._credential = None
            return

        # Check for OAuth token first (takes precedence)
        oauth_token = secrets.get("ANTHROPIC_OAUTH_TOKEN", "").strip()
        if oauth_token:
            # Validate format
            if len(oauth_token) < 20:
                logger.error("OAuth token appears too short (expected 20+ characters)")
                self._credential = None
                return

            self._credential = AnthropicCredential(
                header_name="Authorization",
                header_value=f"Bearer {oauth_token}",
            )
            logger.info(
                "Anthropic OAuth token loaded from secrets",
                token_prefix=oauth_token[:10] + "...",
            )
            return

        # Fall back to API key
        api_key = secrets.get("ANTHROPIC_API_KEY", "").strip()
        if api_key:
            # Validate format
            if not api_key.startswith("sk-ant-"):
                logger.warning("API key doesn't start with 'sk-ant-', may be invalid")
            if len(api_key) < 50:
                logger.error("API key appears too short (expected 50+ characters)")
                self._credential = None
                return

            self._credential = AnthropicCredential(
                header_name="x-api-key",
                header_value=api_key,
            )
            logger.info(
                "Anthropic API key loaded from secrets",
                key_prefix=api_key[:10] + "...",
            )
            return

        logger.warning(
            "No Anthropic credentials found in secrets",
            path=str(self._secrets_path),
            hint="Add ANTHROPIC_API_KEY or ANTHROPIC_OAUTH_TOKEN to secrets.env",
        )
        self._credential = None

    def reload(self) -> None:
        """Force reload of credentials (for testing or config updates)."""
        with self._lock:
            self._cached_mtime = 0
            self._credential = None


# Global credentials manager instance
_credentials_manager: AnthropicCredentialsManager | None = None


def get_credentials_manager() -> AnthropicCredentialsManager:
    """Get or create the global credentials manager."""
    global _credentials_manager
    if _credentials_manager is None:
        _credentials_manager = AnthropicCredentialsManager()
    return _credentials_manager


def reset_credentials_manager() -> None:
    """Reset the global credentials manager (for testing)."""
    global _credentials_manager
    _credentials_manager = None
