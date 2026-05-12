"""
Shared Jira credentials loader.

Single source of truth for Atlassian Cloud Basic-auth credentials. Lives in
``shared/`` so both the gateway sidecar and the orchestrator-side
:mod:`orchestrator.jira_transitions` client can load credentials without
duplicating the parsing logic (#1557 TASK-1-5, risk_analyst R1 mitigation).

The legacy ``gateway/jira_credentials.py`` module re-exports
:class:`JiraCredentials`, :class:`JiraCredentialsUnavailable`,
:func:`get_jira_credentials`, :func:`get_jira_credentials_manager`, and
:func:`parse_env_file` so existing
``from gateway.jira_credentials import …`` imports continue to resolve
without change.

Credential precedence (decision F1, issue #1931):

For each of base URL, username, and API token, the loader checks
``ATLASSIAN_*`` first and falls back to ``JIRA_*`` per-key.  This lets
operators run a single shared Atlassian principal that covers both Jira and
Confluence reads while preserving back-compatibility with deployments that
still set the legacy per-service triple.
"""

from __future__ import annotations

import base64
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover — exercised when egg_logging missing
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("shared.egg_jira_credentials")


# Default secrets path - can be overridden via environment variable.
SECRETS_PATH = Path(
    os.environ.get("EGG_SECRETS_PATH", Path.home() / ".config" / "egg" / "secrets.env")
)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dictionary.

    Mirrors :func:`gateway.anthropic_credentials.parse_env_file` exactly so
    the gateway-side import (``from gateway.anthropic_credentials import
    parse_env_file``) keeps working after we move the canonical source
    here.  We don't import from the gateway-side module because that would
    re-introduce the orchestrator → gateway coupling that this refactor
    deliberately removes.

    Handles ``KEY=value``, quoted values (``KEY="..."`` / ``KEY='...'``),
    ``#`` comments, and empty lines.
    """
    result: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                if key:
                    result[key] = value
    except OSError as e:
        logger.error("Failed to read secrets file", path=str(path), error=str(e))
    return result


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
    """Thread-safe, mtime-caching loader for Jira credentials."""

    def __init__(self, secrets_path: Path | None = None):
        self._secrets_path = secrets_path or SECRETS_PATH
        self._credentials: JiraCredentials | None = None
        self._cached_mtime: float = 0
        self._lock = threading.Lock()

    def get_credentials(self) -> JiraCredentials:
        """Return currently-loaded credentials, raising if unavailable.

        Checks the file's mtime first; reloads if it has changed (or if the
        file has disappeared).  Raises :class:`JiraCredentialsUnavailable`
        when any of the three required keys is blank/missing.
        """
        try:
            current_mtime = self._secrets_path.stat().st_mtime
        except OSError:
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
        keys; ``JIRA_*`` is used as a fallback per-key (decision F1).
        """
        if not self._secrets_path.exists():
            logger.warning("Secrets file not found", path=str(self._secrets_path))
            self._credentials = None
            return

        secrets = parse_env_file(self._secrets_path)

        atlassian_base = (secrets.get("ATLASSIAN_BASE_URL") or "").strip().rstrip("/")
        jira_base = (secrets.get("JIRA_BASE_URL") or "").strip().rstrip("/")
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
            token_prefix=api_token[:4] + "..." if api_token else "",
        )

    def reload(self) -> None:
        """Force a reload on the next :meth:`get_credentials` call."""
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
    """Return the current Jira credentials or raise :class:`JiraCredentialsUnavailable`.

    Routes call this per-request — the mtime check keeps the overhead to a
    single ``stat()`` syscall on the hot path.
    """
    return get_jira_credentials_manager().get_credentials()


def reset_manager_for_tests(secrets_path: Path | None = None) -> JiraCredentialsManager:
    """Replace the singleton manager (test-only).

    Returns the new manager so tests can inspect / mutate it.
    """
    global _credentials_manager
    with _credentials_manager_lock:
        _credentials_manager = JiraCredentialsManager(secrets_path=secrets_path)
        return _credentials_manager


__all__ = [
    "JiraCredentials",
    "JiraCredentialsManager",
    "JiraCredentialsUnavailable",
    "SECRETS_PATH",
    "get_jira_credentials",
    "get_jira_credentials_manager",
    "parse_env_file",
    "reset_manager_for_tests",
]
