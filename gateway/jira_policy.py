"""
Jira project-allowlist + write-verb policy loader.

Reads the ``jira:`` section of ``config/context-filters.yaml`` (or whatever
``EGG_CONTEXT_FILTERS_PATH`` points at) and exposes the helpers the Jira
routes compose:

- ``allowed_projects()`` — current ``frozenset[str]`` of allowlisted keys.
- ``is_project_allowed(key)`` — simple membership test.
- ``extract_project_key(ticket_key)`` — ``"FOO-123" -> "FOO"``.
- ``link_types()`` — current allowlist of ``createIssueLink`` type names.
- ``link_type_allowed(name)`` — membership test for the link-type allowlist.
- ``epic_link_field()`` — Atlassian field name to use for the epic-link
  shorthand (``"parent"`` for next-gen / company-managed projects;
  ``"customfield_10014"`` for classic / team-managed projects).

Expected YAML shape:

    jira:
      projects: ["ENG", "DEVOPS"]
      # Optional — default is ["Blocks", "Relates"].  Atlassian recognises
      # many other names ("Cloners", "Duplicate", ...) but the v1 default
      # set keeps the operator surface tight.
      link_types: ["Blocks", "Relates"]
      # Optional — default is "parent".  Set to "customfield_10014" for
      # classic / team-managed Jira projects whose "Epic Link" still uses
      # the legacy custom field.
      epic_link_field: parent

Fail-closed semantics:

- Missing file → empty set (no project allowed).
- Missing ``jira:`` section → empty set + defaults for link_types /
  epic_link_field.
- Malformed YAML → empty set, and the parse error is logged once per load
  cycle (not re-raised — a bad config file must not crash the gateway).
- Malformed ``link_types`` (not a list-of-strings) → defaults; warning
  logged.
- Malformed ``epic_link_field`` (not in the allowlist) → defaults; warning
  logged.

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

# Default link-type allowlist (refine decision-4).  Atlassian's stock link
# types include many more names ("Cloners", "Duplicate", "Tests"...), but
# the v1 default keeps the operator surface tight and is what the
# orchestrator's epic dispatcher actually uses.
_DEFAULT_LINK_TYPES: frozenset[str] = frozenset({"Blocks", "Relates"})

# Allowlisted values for ``epic_link_field`` (refine decision-2).  Anything
# else is treated as malformed and falls back to the default.
_VALID_EPIC_LINK_FIELDS: frozenset[str] = frozenset({"parent", "customfield_10014"})

# Default Atlassian field used by the ``epicLink`` shorthand.  ``parent`` is
# correct for next-gen / company-managed projects (the modern default).
_DEFAULT_EPIC_LINK_FIELD: str = "parent"


class JiraPolicy:
    """Thread-safe, mtime-caching loader for the Jira project allowlist."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._projects: frozenset[str] = frozenset()
        self._link_types: frozenset[str] = _DEFAULT_LINK_TYPES
        self._epic_link_field: str = _DEFAULT_EPIC_LINK_FIELD
        self._cached_mtime: float = 0
        self._lock = threading.Lock()
        self._loaded: bool = False

    def _refresh_if_needed(self) -> None:
        """Re-read the YAML if the file has changed since last load."""
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
                self._link_types = _DEFAULT_LINK_TYPES
                self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
                self._cached_mtime = 0
                self._loaded = True
            return

        with self._lock:
            if (not self._loaded) or current_mtime != self._cached_mtime:
                self._load()
                self._cached_mtime = current_mtime
                self._loaded = True

    def allowed_projects(self) -> frozenset[str]:
        """Return the current allowlist, reloading if the file changed."""
        self._refresh_if_needed()
        return self._projects

    def is_project_allowed(self, project_key: str) -> bool:
        """Return True iff ``project_key`` is in the allowlist."""
        if not project_key:
            return False
        return project_key in self.allowed_projects()

    def link_types(self) -> frozenset[str]:
        """Return the configured ``createIssueLink`` type allowlist.

        Defaults to ``frozenset({"Blocks", "Relates"})`` per refine
        decision-4.  Lookups are case-sensitive (Atlassian's link-type
        names are case-sensitive too — ``"blocks"`` is not the same as
        ``"Blocks"``).
        """
        self._refresh_if_needed()
        return self._link_types

    def link_type_allowed(self, name: str) -> bool:
        """Return True iff ``name`` is in the link-type allowlist."""
        if not isinstance(name, str) or not name:
            return False
        return name in self.link_types()

    def epic_link_field(self) -> str:
        """Return the Atlassian field name to use for the ``epicLink`` shorthand.

        Defaults to ``"parent"``; operators on classic / team-managed
        projects can set it to ``"customfield_10014"``.
        """
        self._refresh_if_needed()
        return self._epic_link_field

    def reload(self) -> None:
        """Force the next ``allowed_projects()`` to re-read from disk."""
        with self._lock:
            self._cached_mtime = 0
            self._loaded = False
            self._projects = frozenset()
            self._link_types = _DEFAULT_LINK_TYPES
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD

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
            self._link_types = _DEFAULT_LINK_TYPES
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
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
            self._link_types = _DEFAULT_LINK_TYPES
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
            return

        if not isinstance(parsed, dict):
            logger.error(
                "context-filters.yaml top-level must be a mapping — failing closed",
                path=str(self._config_path),
                type=type(parsed).__name__,
            )
            self._projects = frozenset()
            self._link_types = _DEFAULT_LINK_TYPES
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
            return

        jira_section = parsed.get("jira")
        if not isinstance(jira_section, dict):
            self._projects = frozenset()
            self._link_types = _DEFAULT_LINK_TYPES
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
            return

        # ---- projects allowlist (read-verb gate, also used by writes) ----
        projects_raw = jira_section.get("projects")
        if projects_raw is None:
            self._projects = frozenset()
        elif not isinstance(projects_raw, list):
            logger.error(
                "jira.projects must be a list — failing closed",
                path=str(self._config_path),
                type=type(projects_raw).__name__,
            )
            self._projects = frozenset()
        else:
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

        # ---- link-type allowlist (createIssueLink) ----
        link_types_raw = jira_section.get("link_types")
        if link_types_raw is None:
            self._link_types = _DEFAULT_LINK_TYPES
        elif not isinstance(link_types_raw, list):
            logger.error(
                "jira.link_types must be a list — falling back to defaults",
                path=str(self._config_path),
                type=type(link_types_raw).__name__,
            )
            self._link_types = _DEFAULT_LINK_TYPES
        else:
            link_cleaned: set[str] = set()
            for entry in link_types_raw:
                if not isinstance(entry, str):
                    logger.warning(
                        "Ignoring non-string entry in jira.link_types",
                        entry=repr(entry),
                    )
                    continue
                name = entry.strip()
                if not name:
                    continue
                link_cleaned.add(name)
            self._link_types = frozenset(link_cleaned) if link_cleaned else _DEFAULT_LINK_TYPES

        # ---- epic-link field dispatch (createJiraIssue with epicLink) ----
        epic_link_field_raw = jira_section.get("epic_link_field")
        if epic_link_field_raw is None:
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
        elif (
            not isinstance(epic_link_field_raw, str)
            or epic_link_field_raw not in _VALID_EPIC_LINK_FIELDS
        ):
            logger.warning(
                "jira.epic_link_field invalid — falling back to default",
                path=str(self._config_path),
                value=repr(epic_link_field_raw),
                valid=sorted(_VALID_EPIC_LINK_FIELDS),
            )
            self._epic_link_field = _DEFAULT_EPIC_LINK_FIELD
        else:
            self._epic_link_field = epic_link_field_raw

        logger.info(
            "Jira project allowlist loaded",
            path=str(self._config_path),
            project_count=len(self._projects),
            link_types=sorted(self._link_types),
            epic_link_field=self._epic_link_field,
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


def link_types() -> frozenset[str]:
    """Convenience accessor — ``JiraPolicy.link_types()`` via singleton."""
    return get_jira_policy().link_types()


def link_type_allowed(name: str) -> bool:
    """Convenience accessor — ``JiraPolicy.link_type_allowed()`` via singleton."""
    return get_jira_policy().link_type_allowed(name)


def epic_link_field() -> str:
    """Convenience accessor — ``JiraPolicy.epic_link_field()`` via singleton."""
    return get_jira_policy().epic_link_field()


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
    "epic_link_field",
    "extract_project_key",
    "get_jira_policy",
    "is_project_allowed",
    "link_type_allowed",
    "link_types",
    "reload_jira_policy",
    "reset_jira_policy",
]
