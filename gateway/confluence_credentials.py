"""
Confluence Credentials Manager for Gateway Sidecar.

Manages Atlassian Confluence API credentials for gateway-side credential
injection.  Credentials are read from ``~/.config/egg/secrets.env`` on the
host (or the path pointed to by ``EGG_SECRETS_PATH``), mirroring the pattern
used by ``jira_credentials.py`` — mtime-based cache refresh, thread-safe
access, never exported to the sandbox.

Credential precedence (decision F1):

For each of base URL, username, and API token, the loader checks
``ATLASSIAN_*`` first and falls back to ``CONFLUENCE_*`` per-key.  This lets
operators run a single shared Atlassian principal that covers both Jira and
Confluence reads while preserving back-compatibility with deployments that
still set the legacy per-service triple.

Per-key precedence — i.e. ``ATLASSIAN_USERNAME`` + ``CONFLUENCE_BASE_URL``
+ ``CONFLUENCE_API_TOKEN`` is a valid combination — because Atlassian
accounts are tenant-wide and operators may stage the migration in steps.

Base-URL derivation:

- If ``CONFLUENCE_BASE_URL`` is set, it is used verbatim (operators have
  already added the ``/wiki`` suffix).
- Otherwise, if ``ATLASSIAN_BASE_URL`` is set, the loader appends ``/wiki``
  because Confluence Cloud lives at ``<tenant>/wiki/...`` while Jira lives
  at the bare origin.

When any of the three resolved values is missing/blank, the loader raises
``ConfluenceCredentialsUnavailable``; the route layer translates that to
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

logger = get_logger("gateway.confluence-credentials")

# Default secrets path - can be overridden via environment variable.
SECRETS_PATH = Path(
    os.environ.get("EGG_SECRETS_PATH", Path.home() / ".config" / "egg" / "secrets.env")
)


class ConfluenceCredentialsUnavailable(RuntimeError):
    """Raised when Confluence credentials cannot be loaded.

    Route handlers should translate this to an HTTP 503 response.
    """


@dataclass(frozen=True)
class ConfluenceCredentials:
    """Container for Atlassian Cloud Basic-auth credentials for Confluence.

    ``base_url`` is the Confluence root — Atlassian Cloud lives at
    ``https://<tenant>.atlassian.net/wiki``.  The client appends the REST
    path (``/api/v2/...`` or ``/rest/api/...``) at request time.  No
    trailing slash.
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


class ConfluenceCredentialsManager:
    """Thread-safe, mtime-caching loader for Confluence credentials.

    Mirrors ``JiraCredentialsManager`` so reload semantics are identical
    — the cache is invalidated whenever the secrets file's ``st_mtime``
    changes, and concurrent readers never observe a torn credential.
    """

    def __init__(self, secrets_path: Path | None = None):
        self._secrets_path = secrets_path or SECRETS_PATH
        self._credentials: ConfluenceCredentials | None = None
        self._cached_mtime: float = 0
        self._lock = threading.Lock()
        self._logged_first_load: bool = False

    def get_credentials(self) -> ConfluenceCredentials:
        """Return currently-loaded credentials, raising if unavailable.

        Checks the file's mtime first; reloads if it has changed (or if the
        file has disappeared).  Raises ``ConfluenceCredentialsUnavailable``
        when any of the three required values resolves to blank/missing.
        """
        try:
            current_mtime = self._secrets_path.stat().st_mtime
        except OSError:
            with self._lock:
                self._credentials = None
                self._cached_mtime = 0
            raise ConfluenceCredentialsUnavailable(
                f"Secrets file not found: {self._secrets_path}"
            ) from None

        with self._lock:
            if current_mtime != self._cached_mtime:
                self._load_credentials()
                self._cached_mtime = current_mtime
            creds = self._credentials

        if creds is None:
            raise ConfluenceCredentialsUnavailable(
                "Confluence credentials missing — set ATLASSIAN_BASE_URL "
                "(or CONFLUENCE_BASE_URL), ATLASSIAN_USERNAME (or "
                "CONFLUENCE_USERNAME), ATLASSIAN_API_TOKEN (or "
                f"CONFLUENCE_API_TOKEN) in {self._secrets_path}"
            )
        return creds

    def _load_credentials(self) -> None:
        """Load credentials from secrets.env file (called under lock)."""
        if not self._secrets_path.exists():
            logger.warning("Secrets file not found", path=str(self._secrets_path))
            self._credentials = None
            return

        secrets = parse_env_file(self._secrets_path)

        # Per-key ATLASSIAN_* → CONFLUENCE_* fallback (decision F1).
        atlassian_base = (secrets.get("ATLASSIAN_BASE_URL") or "").strip().rstrip("/")
        confluence_base = (secrets.get("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
        username = (
            (secrets.get("ATLASSIAN_USERNAME") or "").strip()
            or (secrets.get("CONFLUENCE_USERNAME") or "").strip()
        )
        api_token = (
            (secrets.get("ATLASSIAN_API_TOKEN") or "").strip()
            or (secrets.get("CONFLUENCE_API_TOKEN") or "").strip()
        )

        # Base URL derivation — CONFLUENCE_BASE_URL wins when set (operators
        # have already added /wiki).  Otherwise derive from ATLASSIAN_BASE_URL
        # by appending /wiki.
        base_source: str
        if confluence_base:
            base_url = confluence_base
            base_source = "CONFLUENCE_BASE_URL"
        elif atlassian_base:
            base_url = f"{atlassian_base}/wiki"
            base_source = "ATLASSIAN_BASE_URL+/wiki"
        else:
            base_url = ""
            base_source = ""

        if not (base_url and username and api_token):
            missing = [
                name
                for name, value in (
                    ("BASE_URL (ATLASSIAN_BASE_URL or CONFLUENCE_BASE_URL)", base_url),
                    ("USERNAME (ATLASSIAN_USERNAME or CONFLUENCE_USERNAME)", username),
                    ("API_TOKEN (ATLASSIAN_API_TOKEN or CONFLUENCE_API_TOKEN)", api_token),
                )
                if not value
            ]
            logger.warning(
                "Confluence credentials incomplete",
                path=str(self._secrets_path),
                missing=missing,
            )
            self._credentials = None
            return

        username_source = (
            "ATLASSIAN_USERNAME"
            if (secrets.get("ATLASSIAN_USERNAME") or "").strip()
            else "CONFLUENCE_USERNAME"
        )
        token_source = (
            "ATLASSIAN_API_TOKEN"
            if (secrets.get("ATLASSIAN_API_TOKEN") or "").strip()
            else "CONFLUENCE_API_TOKEN"
        )

        self._credentials = ConfluenceCredentials(
            base_url=base_url,
            username=username,
            api_token=api_token,
        )
        if not self._logged_first_load:
            # Boot-time observability (risk R12) — log the resolved precedence
            # ONCE so operators can see which env-var triple won without
            # spamming every request.
            logger.info(
                "Confluence credentials loaded",
                base_url=base_url,
                base_source=base_source,
                username_source=username_source,
                token_source=token_source,
                username=username,
                token_prefix=api_token[:4] + "...",
            )
            self._logged_first_load = True

    def reload(self) -> None:
        """Force a reload on the next ``get_credentials()`` call."""
        with self._lock:
            self._cached_mtime = 0
            self._credentials = None
            self._logged_first_load = False


# Global singleton — resolved lazily so that tests can reset it.
_credentials_manager: ConfluenceCredentialsManager | None = None
_credentials_manager_lock = threading.Lock()


def get_confluence_credentials_manager() -> ConfluenceCredentialsManager:
    """Get or create the process-wide Confluence credentials manager."""
    global _credentials_manager
    with _credentials_manager_lock:
        if _credentials_manager is None:
            _credentials_manager = ConfluenceCredentialsManager()
        return _credentials_manager


def get_confluence_credentials() -> ConfluenceCredentials:
    """Return the current Confluence credentials.

    Raises ``ConfluenceCredentialsUnavailable`` when any of the three values
    is missing.  Routes call this per-request — the mtime check keeps the
    overhead to a single ``stat()`` syscall on the hot path.
    """
    return get_confluence_credentials_manager().get_credentials()


def reload_confluence_credentials() -> None:
    """Clear the credentials cache so the next call re-reads from disk.

    Invoked by the gateway's ``_reload_all_config()`` hook (triggered by
    ``POST /api/v1/config/reload`` and SIGHUP) so operators can rotate
    Atlassian tokens without restarting the gateway.
    """
    get_confluence_credentials_manager().reload()


def reset_confluence_credentials_manager() -> None:
    """Drop the module-level singleton (test helper)."""
    global _credentials_manager
    with _credentials_manager_lock:
        _credentials_manager = None


__all__ = [
    "ConfluenceCredentials",
    "ConfluenceCredentialsManager",
    "ConfluenceCredentialsUnavailable",
    "get_confluence_credentials",
    "get_confluence_credentials_manager",
    "reload_confluence_credentials",
    "reset_confluence_credentials_manager",
]
