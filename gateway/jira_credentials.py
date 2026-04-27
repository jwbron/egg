"""
Jira Credentials Manager for Gateway Sidecar.

Manages Atlassian Jira API credentials for gateway-side credential injection.
Credentials are read from ~/.config/egg/secrets.env on the host (or the path
pointed to by ``EGG_SECRETS_PATH``), mirroring the pattern used by
``anthropic_credentials.py`` — mtime-based cache refresh, thread-safe access,
never exported to the sandbox.

Credential precedence (decision F1, issue #1931):

For each of base URL, username, and API token, the loader checks
``ATLASSIAN_*`` first and falls back to ``JIRA_*`` per-key.  This lets
operators run a single shared Atlassian principal that covers both Jira and
Confluence reads while preserving back-compatibility with deployments that
still set the legacy per-service triple.  Per-key precedence — i.e.
``ATLASSIAN_USERNAME`` + ``JIRA_BASE_URL`` + ``JIRA_API_TOKEN`` is a valid
combination — because Atlassian accounts are tenant-wide and operators may
stage the migration in steps.

Note: unlike Confluence, Jira lives at the bare Atlassian origin (no ``/wiki``
suffix), so ``ATLASSIAN_BASE_URL`` is used verbatim when ``JIRA_BASE_URL``
is unset.

When any of the three resolved values is missing, ``get_jira_credentials()``
raises ``JiraCredentialsUnavailable``; the route layer translates that to
HTTP 503.
"""

from __future__ import annotations

import base64
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

# Import parse_env_file from the sibling credentials module so we share the
# exact same parsing rules (comments, quoting, empty lines).  The module is
# loaded via either a relative import (production, package form) or a flat
# import (tests / standalone container mode), matching how gateway.py does it.
try:
    from .anthropic_credentials import parse_env_file
except ImportError:
    from anthropic_credentials import parse_env_file  # type: ignore[no-redef]

logger = get_logger("gateway.jira-credentials")

# Default secrets path - can be overridden via environment variable.
SECRETS_PATH = Path(
    os.environ.get("EGG_SECRETS_PATH", Path.home() / ".config" / "egg" / "secrets.env")
)


class JiraCredentialsUnavailable(RuntimeError):
    """Raised when Jira credentials cannot be loaded.

    Route handlers should translate this to an HTTP 503 response.
    """


@dataclass(frozen=True)
class JiraCredentials:
    """Container for Atlassian Cloud Basic-auth credentials.

    ``base_url`` must be the bare origin (e.g. ``https://foo.atlassian.net``)
    with no trailing slash and no ``/rest/api/...`` suffix.  The client adds
    the REST path at request time.
    """

    base_url: str
    username: str
    api_token: str

    def basic_auth_header(self) -> str:
        """Return the value of an ``Authorization: Basic ...`` header.

        Atlassian Basic auth is ``base64(email:api_token)``.
        """
        raw = f"{self.username}:{self.api_token}".encode()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"Basic {encoded}"


class JiraCredentialsManager:
    """Thread-safe, mtime-caching loader for Jira credentials.

    Mirrors ``AnthropicCredentialsManager`` so reload semantics are identical
    — the cache is invalidated whenever the secrets file's ``st_mtime``
    changes, and concurrent readers never observe a torn credential.
    """

    def __init__(self, secrets_path: Path | None = None):
        self._secrets_path = secrets_path or SECRETS_PATH
        self._credentials: JiraCredentials | None = None
        self._cached_mtime: float = 0
        self._lock = threading.Lock()

    def get_credentials(self) -> JiraCredentials:
        """Return currently-loaded credentials, raising if unavailable.

        Checks the file's mtime first; reloads if it has changed (or if the
        file has disappeared).  Raises ``JiraCredentialsUnavailable`` when any
        of the three required keys is blank/missing.
        """
        try:
            current_mtime = self._secrets_path.stat().st_mtime
        except OSError:
            # File doesn't exist or can't be accessed — clear cache and fail.
            with self._lock:
                self._credentials = None
                self._cached_mtime = 0
            raise JiraCredentialsUnavailable(
                f"Secrets file not found: {self._secrets_path}"
            ) from None

        with self._lock:
            if current_mtime != self._cached_mtime:
                self._load_credentials()
                self._cached_mtime = current_mtime
            creds = self._credentials

        if creds is None:
            raise JiraCredentialsUnavailable(
                "Jira credentials missing — set ATLASSIAN_BASE_URL (or "
                "JIRA_BASE_URL), ATLASSIAN_USERNAME (or JIRA_USERNAME), "
                "ATLASSIAN_API_TOKEN (or JIRA_API_TOKEN) in "
                f"{self._secrets_path}"
            )
        return creds

    def _load_credentials(self) -> None:
        """Load credentials from secrets.env file (called under lock).

        Per-key precedence: ``ATLASSIAN_*`` is preferred for each of the three
        keys; ``JIRA_*`` is used as a fallback per-key (decision F1).  Mixed
        combinations are valid (e.g. ``ATLASSIAN_USERNAME`` +
        ``JIRA_API_TOKEN``) because Atlassian accounts are tenant-wide.
        """
        if not self._secrets_path.exists():
            logger.warning("Secrets file not found", path=str(self._secrets_path))
            self._credentials = None
            return

        secrets = parse_env_file(self._secrets_path)

        atlassian_base = (secrets.get("ATLASSIAN_BASE_URL") or "").strip().rstrip("/")
        jira_base = (secrets.get("JIRA_BASE_URL") or "").strip().rstrip("/")
        # Jira lives at the bare Atlassian origin — no /wiki suffix needed.
        if atlassian_base:
            base_url = atlassian_base
            base_source = "ATLASSIAN_BASE_URL"
        elif jira_base:
            base_url = jira_base
            base_source = "JIRA_BASE_URL"
        else:
            base_url = ""
            base_source = ""

        username = (secrets.get("ATLASSIAN_USERNAME") or "").strip()
        username_source = "ATLASSIAN_USERNAME" if username else ""
        if not username:
            username = (secrets.get("JIRA_USERNAME") or "").strip()
            username_source = "JIRA_USERNAME" if username else ""

        api_token = (secrets.get("ATLASSIAN_API_TOKEN") or "").strip()
        token_source = "ATLASSIAN_API_TOKEN" if api_token else ""
        if not api_token:
            api_token = (secrets.get("JIRA_API_TOKEN") or "").strip()
            token_source = "JIRA_API_TOKEN" if api_token else ""

        if not (base_url and username and api_token):
            missing = [
                name
                for name, value in (
                    ("BASE_URL (ATLASSIAN_BASE_URL or JIRA_BASE_URL)", base_url),
                    ("USERNAME (ATLASSIAN_USERNAME or JIRA_USERNAME)", username),
                    ("API_TOKEN (ATLASSIAN_API_TOKEN or JIRA_API_TOKEN)", api_token),
                )
                if not value
            ]
            logger.warning(
                "Jira credentials incomplete",
                path=str(self._secrets_path),
                missing=missing,
            )
            self._credentials = None
            return

        self._credentials = JiraCredentials(
            base_url=base_url,
            username=username,
            api_token=api_token,
        )
        logger.info(
            "Jira credentials loaded",
            base_url=base_url,
            base_source=base_source,
            username_source=username_source,
            token_source=token_source,
            username=username,
            token_prefix=api_token[:4] + "...",
        )

    def reload(self) -> None:
        """Force a reload on the next ``get_credentials()`` call."""
        with self._lock:
            self._cached_mtime = 0
            self._credentials = None


# Global singleton — resolved lazily so that tests can reset it.
_credentials_manager: JiraCredentialsManager | None = None
_credentials_manager_lock = threading.Lock()


def get_jira_credentials_manager() -> JiraCredentialsManager:
    """Get or create the process-wide Jira credentials manager."""
    global _credentials_manager
    with _credentials_manager_lock:
        if _credentials_manager is None:
            _credentials_manager = JiraCredentialsManager()
        return _credentials_manager


def get_jira_credentials() -> JiraCredentials:
    """Return the current Jira credentials or raise ``JiraCredentialsUnavailable``.

    Routes call this per-request — the mtime check keeps the overhead to a
    single ``stat()`` syscall on the hot path.
    """
    return get_jira_credentials_manager().get_credentials()


def reload_jira_credentials() -> None:
    """Clear the credentials cache so the next call re-reads from disk.

    Invoked by the gateway's ``_reload_all_config()`` hook (triggered by
    ``POST /api/v1/config/reload`` and SIGHUP) so operators can rotate Jira
    tokens without restarting the gateway.
    """
    get_jira_credentials_manager().reload()


def reset_jira_credentials_manager() -> None:
    """Drop the module-level singleton (test helper)."""
    global _credentials_manager
    with _credentials_manager_lock:
        _credentials_manager = None
