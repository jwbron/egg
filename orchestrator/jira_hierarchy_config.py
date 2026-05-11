"""
Jira hierarchy-field configuration loader (issue #1557, TASK-1-4).

Maps each Atlassian Jira project key to the hierarchy mechanism used to link
child tickets to their parent epic.  The choice between ``parent`` and
``epic_link`` depends on the project's Jira flavour (team-managed /
Next-gen projects use ``parent``; company-managed projects with legacy
custom-field-10014 hierarchies use ``epic_link``).

Per #1557 decision-2 the operator declares the mapping per-project in
``~/.config/egg/jira-hierarchy.yaml``:

    projects:
      ENG: parent
      KORE: epic_link

The :func:`resolve_hierarchy_field` helper raises
:class:`JiraHierarchyUnmappedError` when the requested project isn't in
the YAML — the plan apply step (TASK-1-13) refuses to create children
for unmapped projects to avoid silently creating broken hierarchies.

Loader semantics:

- mtime-cached (mirrors the pattern in ``gateway/jira_credentials.py``).
- thread-safe via a ``threading.Lock``.
- malformed YAML / unknown values raise :class:`JiraHierarchyConfigError`.
- a missing config file is treated as an empty mapping — every project
  resolves to :class:`JiraHierarchyUnmappedError`; the operator's setup
  step is to author the YAML before running the epic flow.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
import yaml

logger = structlog.get_logger(__name__)


HierarchyField = Literal["parent", "epic_link"]

VALID_HIERARCHY_FIELDS: frozenset[str] = frozenset({"parent", "epic_link"})

CONFIG_PATH = Path(
    os.environ.get(
        "EGG_JIRA_HIERARCHY_PATH",
        Path.home() / ".config" / "egg" / "jira-hierarchy.yaml",
    )
)


class JiraHierarchyConfigError(Exception):
    """Raised when the hierarchy config YAML is malformed."""


class JiraHierarchyUnmappedError(Exception):
    """Raised when a project key has no entry in the hierarchy config."""

    def __init__(self, project_key: str, config_path: Path):
        super().__init__(
            f"Jira project '{project_key}' has no hierarchy mapping in "
            f"{config_path}. Add an entry under 'projects:' mapping "
            f"'{project_key}' to either 'parent' or 'epic_link'."
        )
        self.project_key = project_key
        self.config_path = config_path


@dataclass(frozen=True)
class JiraHierarchyConfig:
    """In-memory representation of the hierarchy YAML.

    Schema (validated at load time):

    .. code-block:: yaml

        projects:
          <PROJECT_KEY>: parent | epic_link
    """

    projects: dict[str, HierarchyField]

    def field_for(self, project_key: str) -> HierarchyField:
        """Return the hierarchy field for ``project_key``.

        Raises :class:`JiraHierarchyUnmappedError` when the project has no
        mapping — callers must NOT silently fall back to a default because
        a wrong field choice produces unreachable child tickets.
        """
        try:
            return self.projects[project_key]
        except KeyError as exc:  # noqa: PERF203
            raise JiraHierarchyUnmappedError(
                project_key, _config_path_for_error()
            ) from exc


def _config_path_for_error() -> Path:
    """Return the path the active manager is watching.

    Indirection lets the error message reference the same path the
    manager actually loaded from (test-overridable via env var).
    """
    return _MANAGER.config_path


def _parse_config(raw: dict[str, object], path: Path) -> JiraHierarchyConfig:
    """Validate the parsed YAML body and return a :class:`JiraHierarchyConfig`."""
    if not isinstance(raw, dict):
        raise JiraHierarchyConfigError(
            f"Hierarchy config at {path} must be a YAML mapping at the top "
            f"level (got {type(raw).__name__})"
        )

    projects_raw = raw.get("projects")
    if projects_raw is None:
        # An empty file (or one with only comments) is permissible and is
        # equivalent to no projects mapped — every resolve fails with
        # JiraHierarchyUnmappedError.
        return JiraHierarchyConfig(projects={})

    if not isinstance(projects_raw, dict):
        raise JiraHierarchyConfigError(
            f"Hierarchy config at {path} 'projects:' must be a mapping "
            f"(got {type(projects_raw).__name__})"
        )

    projects: dict[str, HierarchyField] = {}
    for key, value in projects_raw.items():
        if not isinstance(key, str) or not key:
            raise JiraHierarchyConfigError(
                f"Hierarchy config at {path}: project key must be a "
                f"non-empty string (got {key!r})"
            )
        if not isinstance(value, str) or value not in VALID_HIERARCHY_FIELDS:
            raise JiraHierarchyConfigError(
                f"Hierarchy config at {path}: project '{key}' value must be "
                f"one of {sorted(VALID_HIERARCHY_FIELDS)} (got {value!r})"
            )
        projects[key] = value  # type: ignore[assignment]

    return JiraHierarchyConfig(projects=projects)


class JiraHierarchyConfigManager:
    """Thread-safe mtime-caching loader for the hierarchy config."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or CONFIG_PATH
        self._config: JiraHierarchyConfig | None = None
        self._cached_mtime: float = -1.0
        self._lock = threading.Lock()

    def get_config(self) -> JiraHierarchyConfig:
        """Return the current hierarchy config (reloading on mtime change).

        A missing file is treated as an empty mapping so the orchestrator
        can boot even before the operator has authored
        ``~/.config/egg/jira-hierarchy.yaml`` — subsequent
        :func:`resolve_hierarchy_field` calls then raise
        :class:`JiraHierarchyUnmappedError`, which the apply step
        surfaces to the operator as a HITL gate.
        """
        try:
            current_mtime = self.config_path.stat().st_mtime
        except OSError:
            current_mtime = -1.0

        with self._lock:
            if current_mtime != self._cached_mtime:
                self._reload(current_mtime)
            config = self._config

        # _reload always populates _config (either with parsed YAML or
        # with an empty mapping when the file is missing).
        assert config is not None
        return config

    def _reload(self, mtime: float) -> None:
        """Load (or reload) the YAML file (called under lock)."""
        if mtime < 0 or not self.config_path.exists():
            logger.info(
                "jira_hierarchy_config_missing",
                path=str(self.config_path),
            )
            self._config = JiraHierarchyConfig(projects={})
            self._cached_mtime = mtime
            return

        try:
            raw = yaml.safe_load(self.config_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise JiraHierarchyConfigError(
                f"Hierarchy config at {self.config_path} is not valid YAML: {exc}"
            ) from exc

        self._config = _parse_config(raw, self.config_path)
        self._cached_mtime = mtime
        logger.info(
            "jira_hierarchy_config_loaded",
            path=str(self.config_path),
            project_count=len(self._config.projects),
        )


_MANAGER = JiraHierarchyConfigManager()


def get_hierarchy_config() -> JiraHierarchyConfig:
    """Return the singleton hierarchy config."""
    return _MANAGER.get_config()


def resolve_hierarchy_field(project_key: str) -> HierarchyField:
    """Return the hierarchy field (``parent`` or ``epic_link``) for ``project_key``.

    Raises :class:`JiraHierarchyUnmappedError` when the project has no
    mapping in the YAML.  Callers must surface this to the operator (the
    apply step at TASK-1-13 opens a HITL gate) rather than picking a
    default — a wrong field choice produces unreachable child tickets
    that the operator has to clean up by hand.
    """
    config = get_hierarchy_config()
    return config.field_for(project_key)


def reset_for_tests(config_path: Path | None = None) -> JiraHierarchyConfigManager:
    """Replace the module-singleton manager (test-only).

    Returns the new manager so tests can inspect / mutate it.  Production
    callers MUST NOT use this — it is not thread-safe with respect to a
    concurrent :func:`get_hierarchy_config` call.
    """
    global _MANAGER
    _MANAGER = JiraHierarchyConfigManager(config_path=config_path)
    return _MANAGER
