"""
Token Refresher - Manages GitHub App installation token refresh in-memory.

Tokens are refreshed automatically when they're within 15 minutes of expiry.
On failure, the last valid token is returned with a warning logged (up to 3
consecutive failures). After 3 failures, the cached token is cleared (fail closed).

This replaces the host-side github-token-refresher systemd service with an
in-memory solution that runs within the gateway sidecar.
"""

import os
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import requests

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.token-refresher")

# GitHub API constants
GITHUB_API_BASE = "https://api.github.com"

# Default paths for GitHub App credentials
# Can be overridden via EGG_CONFIG_DIR for containerized deployments
DEFAULT_CONFIG_DIR = Path(os.environ.get("EGG_CONFIG_DIR", Path.home() / ".config" / "egg"))


@dataclass
class TokenInfo:
    """Information about the current token."""

    token: str
    expires_at: datetime
    generated_at: datetime
    source: str  # "refresher"

    @property
    def is_expired(self) -> bool:
        """Check if token is expired."""
        return datetime.now(UTC) > self.expires_at

    @property
    def minutes_until_expiry(self) -> float:
        """Minutes until token expires."""
        return (self.expires_at - datetime.now(UTC)).total_seconds() / 60


class TokenRefresher:
    """
    Manages GitHub App installation token refresh in-memory.

    Tokens are refreshed automatically when they're within 15 minutes
    of expiry. On failure, the last valid token is returned with a
    warning logged.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: int,
        refresh_margin_minutes: int = 15,
        max_consecutive_failures: int = 3,
    ):
        """
        Initialize the token refresher.

        Args:
            app_id: GitHub App ID
            private_key: Private key PEM content (not path)
            installation_id: GitHub App installation ID
            refresh_margin_minutes: Refresh when this many minutes until expiry
            max_consecutive_failures: Clear cached token after this many failures
        """
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._refresh_margin = timedelta(minutes=refresh_margin_minutes)
        self._max_failures = max_consecutive_failures

        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._generated_at: datetime | None = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0

    def _ensure_valid_token(self) -> None:
        """
        Ensure we have a valid token, refreshing if needed.

        Must be called while holding self._lock.
        """
        if self._needs_refresh():
            try:
                self._refresh()
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                logger.error(
                    "Token refresh failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    consecutive_failures=self._consecutive_failures,
                    has_cached_token=self._token is not None,
                )

                # If we have a cached token, use it with warning
                if self._token and self._consecutive_failures < self._max_failures:
                    logger.warning(
                        "Using cached token after refresh failure",
                        expires_at=self._expires_at.isoformat() if self._expires_at else None,
                        consecutive_failures=self._consecutive_failures,
                    )
                elif self._consecutive_failures >= self._max_failures:
                    logger.error(
                        "Max refresh failures reached, clearing cached token",
                        max_failures=self._max_failures,
                        consecutive_failures=self._consecutive_failures,
                    )
                    self._token = None
                    self._expires_at = None
                    self._generated_at = None

    def get_token(self) -> str | None:
        """
        Get a valid token, refreshing if needed.

        Returns:
            Valid token or None if refresh fails and no cached token.
        """
        with self._lock:
            self._ensure_valid_token()
            return self._token

    def get_token_info(self) -> TokenInfo | None:
        """
        Get token with metadata.

        Returns:
            TokenInfo or None if no token available.
        """
        with self._lock:
            self._ensure_valid_token()

            if not self._token or not self._expires_at or not self._generated_at:
                return None

            return TokenInfo(
                token=self._token,
                expires_at=self._expires_at,
                generated_at=self._generated_at,
                source="refresher",
            )

    def _needs_refresh(self) -> bool:
        """Check if token needs refresh."""
        if not self._token or not self._expires_at:
            return True
        return datetime.now(UTC) > (self._expires_at - self._refresh_margin)

    def _refresh(self) -> None:
        """Generate new installation token via GitHub API."""
        # Create JWT for GitHub App authentication
        now = datetime.now(UTC)
        payload = {
            "iat": int(now.timestamp()) - 60,  # 1 min in past for clock skew
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "iss": self._app_id,
        }
        jwt_token = jwt.encode(payload, self._private_key, algorithm="RS256")

        # Request installation access token
        response = requests.post(
            f"{GITHUB_API_BASE}/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        self._token = data["token"]
        # Parse ISO 8601 timestamp from GitHub (e.g., "2024-01-01T12:00:00Z")
        self._expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        self._generated_at = datetime.now(UTC)

        # Calculate minutes until expiry directly (avoid calling get_token_info()
        # which would try to acquire the lock we already hold)
        minutes_until_expiry = (self._expires_at - datetime.now(UTC)).total_seconds() / 60
        logger.info(
            "Token refreshed successfully",
            expires_at=self._expires_at.isoformat(),
            minutes_until_expiry=f"{minutes_until_expiry:.1f}",
        )

    @property
    def consecutive_failures(self) -> int:
        """Get current consecutive failure count."""
        with self._lock:
            return self._consecutive_failures

    def reset_failure_count(self) -> None:
        """Reset the consecutive failure counter (for testing)."""
        with self._lock:
            self._consecutive_failures = 0


# Global token refresher instances (bot and reviewer)
_token_refresher: TokenRefresher | None = None
_reviewer_token_refresher: TokenRefresher | None = None
_refresher_initialization_attempted = False
_reviewer_refresher_initialization_attempted = False


def _read_secrets_env(secrets_file: Path) -> dict[str, str]:
    """Read secrets.env file into a dictionary."""
    secrets_dict: dict[str, str] = {}
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                secrets_dict[key.strip()] = value.strip()
    return secrets_dict


def initialize_token_refresher(
    config_dir: Path | None = None,
    app_id: str | None = None,
    private_key_path: Path | None = None,
    installation_id: int | None = None,
) -> TokenRefresher | None:
    """
    Initialize the global token refresher from config or environment.

    Config can be provided via:
    1. Explicit parameters (highest priority)
    2. Environment variables: GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH, GITHUB_INSTALLATION_ID
    3. secrets.env file in config_dir (default: ~/.config/egg/)

    Returns None if required config is missing.

    Args:
        config_dir: Directory containing config files (optional)
        app_id: GitHub App ID (optional, overrides env/file)
        private_key_path: Path to private key PEM file (optional)
        installation_id: GitHub App installation ID (optional)

    Returns:
        Initialized TokenRefresher or None if config missing
    """
    global _token_refresher, _refresher_initialization_attempted

    # Only attempt initialization once
    if _refresher_initialization_attempted:
        return _token_refresher
    _refresher_initialization_attempted = True

    config_dir = config_dir or DEFAULT_CONFIG_DIR

    # Read secrets.env for GitHub App credentials
    secrets = _read_secrets_env(config_dir / "secrets.env")

    # Resolve app_id (explicit > env > secrets.env)
    resolved_app_id = app_id or os.environ.get("GITHUB_APP_ID") or secrets.get("GITHUB_APP_ID")

    # Resolve installation_id (explicit > env > secrets.env)
    resolved_installation_id = installation_id
    if resolved_installation_id is None:
        env_installation_id = os.environ.get("GITHUB_INSTALLATION_ID") or os.environ.get(
            "GITHUB_APP_INSTALLATION_ID"
        )
        if not env_installation_id:
            env_installation_id = secrets.get("GITHUB_APP_INSTALLATION_ID")
        if env_installation_id:
            try:
                resolved_installation_id = int(env_installation_id)
            except ValueError:
                logger.warning(
                    "Invalid GITHUB_APP_INSTALLATION_ID",
                    value=env_installation_id,
                )

    # Resolve private key
    resolved_private_key_path = private_key_path
    if not resolved_private_key_path:
        env_key_path = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
        if env_key_path:
            resolved_private_key_path = Path(env_key_path)
        else:
            resolved_private_key_path = config_dir / "github-app.pem"

    # Validate all required config is present
    if not all([resolved_app_id, resolved_installation_id, resolved_private_key_path]):
        logger.error(
            "Token refresher not configured (missing credentials)",
            has_app_id=bool(resolved_app_id),
            has_installation_id=bool(resolved_installation_id),
            has_private_key_path=bool(resolved_private_key_path),
        )
        return None

    if not resolved_private_key_path.exists():
        logger.error(
            "Private key file not found",
            private_key_path=str(resolved_private_key_path),
        )
        return None

    try:
        private_key = resolved_private_key_path.read_text()
        if resolved_app_id is None or resolved_installation_id is None:
            raise ValueError("app_id and installation_id are required")
        refresher = TokenRefresher(
            app_id=resolved_app_id,
            private_key=private_key,
            installation_id=resolved_installation_id,
        )

        # Verify we can get a token on startup
        token = refresher.get_token()
        if token:
            logger.info(
                "Token refresher initialized successfully",
                app_id=resolved_app_id,
                installation_id=resolved_installation_id,
            )
            _token_refresher = refresher
            return refresher
        else:
            logger.error("Token refresher failed to get initial token")
            return None

    except Exception as e:
        logger.error(
            "Failed to initialize token refresher",
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def get_token_refresher() -> TokenRefresher | None:
    """
    Get the global token refresher instance (for bot/main app).

    Returns None if refresher was not initialized or initialization failed.
    Call initialize_token_refresher() first during startup.
    """
    return _token_refresher


def get_bot_token() -> tuple[str | None, str]:
    """
    Get the bot token from the token refresher.

    Returns:
        Tuple of (token, source) where source is "refresher" or "none"
    """
    refresher = get_token_refresher()
    if refresher:
        token = refresher.get_token()
        if token:
            return token, "refresher"

    return None, "none"


def initialize_reviewer_token_refresher(
    config_dir: Path | None = None,
    app_id: str | None = None,
    private_key: str | None = None,
    private_key_path: Path | None = None,
    installation_id: int | None = None,
) -> TokenRefresher | None:
    """
    Initialize the global reviewer token refresher from config or environment.

    The reviewer token is used for a separate GitHub App that posts code reviews.
    This allows reviews to use the full GitHub Reviews API (approve/request-changes)
    since the reviewer is not the same account as the PR author.

    Config can be provided via:
    1. Explicit parameters (highest priority)
    2. Environment variables: REVIEWER_APP_ID, REVIEWER_APP_PRIVATE_KEY, REVIEWER_APP_INSTALLATION_ID
    3. secrets.env file in config_dir (for app_id/installation_id)
    4. reviewer-app.pem file in config_dir (for private key)

    Returns None if required config is missing.

    Args:
        config_dir: Directory containing secrets.env and reviewer-app.pem (defaults to ~/.config/egg/)
        app_id: Reviewer GitHub App ID (optional, overrides env)
        private_key: Reviewer private key PEM content (optional)
        private_key_path: Path to reviewer private key PEM file (optional)
        installation_id: Reviewer GitHub App installation ID (optional)

    Returns:
        Initialized TokenRefresher or None if config missing
    """
    global _reviewer_token_refresher, _reviewer_refresher_initialization_attempted

    # Only attempt initialization once
    if _reviewer_refresher_initialization_attempted:
        return _reviewer_token_refresher
    _reviewer_refresher_initialization_attempted = True

    config_dir = config_dir or DEFAULT_CONFIG_DIR

    # Read secrets.env for reviewer credentials
    secrets = _read_secrets_env(config_dir / "secrets.env")

    # Resolve app_id (explicit > env > secrets.env)
    resolved_app_id = app_id or os.environ.get("REVIEWER_APP_ID") or secrets.get("REVIEWER_APP_ID")

    # Resolve installation_id (explicit > env > secrets.env)
    resolved_installation_id = installation_id
    if resolved_installation_id is None:
        env_installation_id = os.environ.get("REVIEWER_APP_INSTALLATION_ID")
        if not env_installation_id:
            env_installation_id = secrets.get("REVIEWER_APP_INSTALLATION_ID")
        if env_installation_id:
            try:
                resolved_installation_id = int(env_installation_id)
            except ValueError:
                logger.warning(
                    "Invalid REVIEWER_APP_INSTALLATION_ID",
                    value=env_installation_id,
                )

    # Resolve private key (explicit > env > file)
    # Note: PEM files are multiline, so we read from a file instead of secrets.env
    resolved_private_key = private_key or os.environ.get("REVIEWER_APP_PRIVATE_KEY")
    if not resolved_private_key:
        # Try to read from PEM file
        resolved_private_key_path = private_key_path
        if not resolved_private_key_path:
            resolved_private_key_path = config_dir / "reviewer-app.pem"
        if resolved_private_key_path.exists():
            try:
                resolved_private_key = resolved_private_key_path.read_text()
            except Exception as e:
                logger.warning(
                    "Failed to read reviewer private key file",
                    path=str(resolved_private_key_path),
                    error=str(e),
                )

    # Validate all required config is present
    if not all([resolved_app_id, resolved_installation_id, resolved_private_key]):
        # Reviewer is optional - just log debug and return None
        logger.debug(
            "Reviewer token refresher not configured (optional)",
            has_app_id=bool(resolved_app_id),
            has_installation_id=bool(resolved_installation_id),
            has_private_key=bool(resolved_private_key),
        )
        return None

    try:
        if (
            resolved_app_id is None
            or resolved_installation_id is None
            or resolved_private_key is None
        ):
            raise ValueError("app_id, installation_id, and private_key are required")

        refresher = TokenRefresher(
            app_id=resolved_app_id,
            private_key=resolved_private_key,
            installation_id=resolved_installation_id,
        )

        # Verify we can get a token on startup
        token = refresher.get_token()
        if token:
            logger.info(
                "Reviewer token refresher initialized successfully",
                app_id=resolved_app_id,
                installation_id=resolved_installation_id,
            )
            _reviewer_token_refresher = refresher
            return refresher
        else:
            logger.error("Reviewer token refresher failed to get initial token")
            return None

    except Exception as e:
        logger.error(
            "Failed to initialize reviewer token refresher",
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def get_reviewer_token_refresher() -> TokenRefresher | None:
    """
    Get the global reviewer token refresher instance.

    Returns None if refresher was not initialized or initialization failed.
    Call initialize_reviewer_token_refresher() first during startup.
    """
    return _reviewer_token_refresher


def get_reviewer_token() -> tuple[str | None, str]:
    """
    Get the reviewer token from the reviewer token refresher.

    Returns:
        Tuple of (token, source) where source is "reviewer_refresher" or "none"
    """
    refresher = get_reviewer_token_refresher()
    if refresher:
        token = refresher.get_token()
        if token:
            return token, "reviewer_refresher"

    return None, "none"


def is_reviewer_token_available() -> bool:
    """
    Check if a reviewer token is available.

    Returns:
        True if reviewer token refresher is initialized and has a valid token.
    """
    refresher = get_reviewer_token_refresher()
    if refresher:
        token = refresher.get_token()
        return token is not None
    return False


def reset_token_refresher() -> None:
    """
    Reset the global token refresher state (for testing).

    This clears both bot and reviewer instances and allows re-initialization.
    """
    global _token_refresher, _refresher_initialization_attempted
    global _reviewer_token_refresher, _reviewer_refresher_initialization_attempted
    _token_refresher = None
    _refresher_initialization_attempted = False
    _reviewer_token_refresher = None
    _reviewer_refresher_initialization_attempted = False
