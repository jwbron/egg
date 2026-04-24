"""
Jira project-allowlist loader.

Reads the ``jira:`` section of ``config/context-filters.yaml`` (or whatever
``EGG_CONTEXT_FILTERS_PATH`` points at) and exposes three helpers the Jira
routes compose:

- ``allowed_projects()`` — current ``frozenset[str]`` of allowlisted keys.
- ``is_project_allowed(key)`` — simple membership test.
- ``extract_project_key(ticket_key)`` — ``"FOO-123" -> "FOO"``.

Expected YAML shape:

    jira:
      projects: ["ENG", "DEVOPS"]

Fail-closed semantics:

- Missing file → empty set (no project allowed).
- Missing ``jira:`` section → empty set.
- Malformed YAML → empty set, and the parse error is logged once per load
  cycle (not re-raised — a bad config file must not crash the gateway).

Cache invalidation mirrors ``anthropic_credentials.py``: an ``st_mtime``
check fires on every access, and ``reload_jira_policy()`` forces a clear so
``POST /api/v1/config/reload`` picks up operator edits immediately.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

try:
    import yaml
except ImportError as _exc:  # pragma: no cover — yaml is a hard dependency
    raise RuntimeError("PyYAML is required by gateway.jira_policy") from _exc

logger = get_logger("gateway.jira-policy")


# Default path to the context-filters YAML file.  The gateway runs from the
# repository root (or from the in-container /app path, where the config dir
# is mirrored under /app/config).  Operators can override via env var.
_DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "EGG_CONTEXT_FILTERS_PATH",
        str(Path(__file__).parent.parent / "config" / "context-filters.yaml"),
    )
)

_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TICKET_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)-(\d+)$")


class JiraPolicy:
    """Thread-safe, mtime-caching loader for the Jira project allowlist."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._projects: frozenset[str] = frozenset()
        self._cached_mtime: float = 0
        self._lock = threading.Lock()
        self._loaded: bool = False

    def allowed_projects(self) -> frozenset[str]:
        """Return the current allowlist, reloading if the file changed."""
        try:
            current_mtime = self._config_path.stat().st_mtime
        except OSError:
            # File missing — clear cache and fail closed.
            with self._lock:
                if self._projects:
                    logger.warning(
                        "context-filters.yaml disappeared — clearing allowlist",
                        path=str(self._config_path),
                    )
                self._projects = frozenset()
                self._cached_mtime = 0
                self._loaded = True
            return self._projects

        with self._lock:
            if (not self._loaded) or current_mtime != self._cached_mtime:
                self._load()
                self._cached_mtime = current_mtime
                self._loaded = True
            return self._projects

    def is_project_allowed(self, project_key: str) -> bool:
        """Return True iff ``project_key`` is in the allowlist."""
        if not project_key:
            return False
        return project_key in self.allowed_projects()

    def reload(self) -> None:
        """Force the next ``allowed_projects()`` to re-read from disk."""
        with self._lock:
            self._cached_mtime = 0
            self._loaded = False
            self._projects = frozenset()

    def _load(self) -> None:
        """Load the allowlist from YAML (called under the lock)."""
        try:
            raw = self._config_path.read_text()
        except OSError as exc:
            logger.warning(
                "Failed to read context-filters.yaml",
                path=str(self._config_path),
                error=str(exc),
            )
            self._projects = frozenset()
            return

        try:
            parsed = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            logger.error(
                "Malformed context-filters.yaml — failing closed",
                path=str(self._config_path),
                error=str(exc),
            )
            self._projects = frozenset()
            return

        if not isinstance(parsed, dict):
            logger.error(
                "context-filters.yaml top-level must be a mapping — failing closed",
                path=str(self._config_path),
                type=type(parsed).__name__,
            )
            self._projects = frozenset()
            return

        jira_section = parsed.get("jira")
        if not isinstance(jira_section, dict):
            self._projects = frozenset()
            return

        projects_raw = jira_section.get("projects")
        if projects_raw is None:
            self._projects = frozenset()
            return
        if not isinstance(projects_raw, list):
            logger.error(
                "jira.projects must be a list — failing closed",
                path=str(self._config_path),
                type=type(projects_raw).__name__,
            )
            self._projects = frozenset()
            return

        cleaned: set[str] = set()
        for entry in projects_raw:
            if not isinstance(entry, str):
                logger.warning(
                    "Ignoring non-string entry in jira.projects",
                    entry=repr(entry),
                )
                continue
            key = entry.strip()
            if not _PROJECT_KEY_RE.fullmatch(key):
                logger.warning(
                    "Ignoring invalid Jira project key in jira.projects",
                    entry=repr(entry),
                )
                continue
            cleaned.add(key)

        self._projects = frozenset(cleaned)
        logger.info(
            "Jira project allowlist loaded",
            path=str(self._config_path),
            count=len(self._projects),
        )


def extract_project_key(ticket_key: str) -> str:
    """Extract the project portion of a Jira ticket key.

    ``extract_project_key("FOO-123")`` → ``"FOO"``.  Returns an empty string
    for inputs that don't match the Jira ticket shape — callers translate
    that into a 400 / 403 as appropriate.
    """
    if not isinstance(ticket_key, str):
        return ""
    match = _TICKET_KEY_RE.match(ticket_key.strip())
    return match.group(1) if match else ""


# -----------------------------------------------------------------------------
# Module-level singleton — matches ``anthropic_credentials`` pattern.
# -----------------------------------------------------------------------------

_jira_policy: JiraPolicy | None = None
_jira_policy_lock = threading.Lock()


def get_jira_policy() -> JiraPolicy:
    """Return the process-wide ``JiraPolicy`` singleton."""
    global _jira_policy
    with _jira_policy_lock:
        if _jira_policy is None:
            _jira_policy = JiraPolicy()
        return _jira_policy


def allowed_projects() -> frozenset[str]:
    """Convenience accessor — ``JiraPolicy.allowed_projects()`` via singleton."""
    return get_jira_policy().allowed_projects()


def is_project_allowed(project_key: str) -> bool:
    """Convenience accessor — ``JiraPolicy.is_project_allowed()`` via singleton."""
    return get_jira_policy().is_project_allowed(project_key)


def reload_jira_policy() -> None:
    """Force the next allowlist access to re-read from disk.

    Invoked by the gateway's ``_reload_all_config()`` hook so ``POST
    /api/v1/config/reload`` picks up operator edits without a restart.
    """
    get_jira_policy().reload()


def reset_jira_policy() -> None:
    """Drop the module-level singleton (test helper)."""
    global _jira_policy
    with _jira_policy_lock:
        _jira_policy = None


__all__ = [
    "JiraPolicy",
    "allowed_projects",
    "extract_project_key",
    "get_jira_policy",
    "is_project_allowed",
    "reload_jira_policy",
    "reset_jira_policy",
]
