"""
Confluence space-allowlist loader.

Reads the ``confluence:`` section of ``config/context-filters.yaml`` (or
whatever ``EGG_CONTEXT_FILTERS_PATH`` points at) and exposes helpers the
Confluence routes compose:

- ``allowed_spaces()`` — current ``frozenset[str]`` of allowlisted space keys.
- ``is_space_allowed(key)`` — simple membership test.
- ``reload_confluence_policy()`` — force a re-read from disk on the next call.

Expected YAML shape::

    confluence:
      spaces: ["ENG", "DOCS"]   # Atlassian space keys allowed for read access

Fail-closed semantics:

- Missing file → empty set (no space allowed).
- Missing ``confluence:`` section → empty set.
- Missing ``spaces:`` key → empty set.
- Malformed YAML → empty set, and the parse error is logged once per load
  cycle (not re-raised — a bad config file must not crash the gateway).

Cache invalidation mirrors ``jira_policy.py``: an ``st_mtime`` check fires
on every access, and ``reload_confluence_policy()`` forces a clear so
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
    raise RuntimeError("PyYAML is required by gateway.confluence_policy") from _exc

logger = get_logger("gateway.confluence-policy")


# Default path to the context-filters YAML file.  The gateway runs from the
# repository root (or from the in-container /app path, where the config dir
# is mirrored under /app/config).  Operators can override via env var.
_DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "EGG_CONTEXT_FILTERS_PATH",
        str(Path(__file__).parent.parent / "config" / "context-filters.yaml"),
    )
)

# Atlassian space keys are conventionally uppercase but the API accepts mixed
# case.  Anchor on a leading letter and accept letters/digits/underscore.
_SPACE_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


class ConfluencePolicy:
    """Thread-safe, mtime-caching loader for the Confluence space allowlist."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._spaces: frozenset[str] = frozenset()
        self._cached_mtime: float = 0
        self._lock = threading.Lock()
        self._loaded: bool = False
        self._logged_first_load: bool = False

    def allowed_spaces(self) -> frozenset[str]:
        """Return the current allowlist, reloading if the file changed."""
        try:
            current_mtime = self._config_path.stat().st_mtime
        except OSError:
            with self._lock:
                if self._spaces:
                    logger.warning(
                        "context-filters.yaml disappeared — clearing allowlist",
                        path=str(self._config_path),
                    )
                self._spaces = frozenset()
                self._cached_mtime = 0
                self._loaded = True
            return self._spaces

        with self._lock:
            if (not self._loaded) or current_mtime != self._cached_mtime:
                self._load()
                self._cached_mtime = current_mtime
                self._loaded = True
            return self._spaces

    def is_space_allowed(self, space_key: str) -> bool:
        """Return True iff ``space_key`` is in the allowlist."""
        if not space_key:
            return False
        return space_key in self.allowed_spaces()

    def reload(self) -> None:
        """Force the next ``allowed_spaces()`` to re-read from disk."""
        with self._lock:
            self._cached_mtime = 0
            self._loaded = False
            self._spaces = frozenset()
            self._logged_first_load = False

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
            self._spaces = frozenset()
            return

        try:
            parsed = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            logger.error(
                "Malformed context-filters.yaml — failing closed",
                path=str(self._config_path),
                error=str(exc),
            )
            self._spaces = frozenset()
            return

        if not isinstance(parsed, dict):
            logger.error(
                "context-filters.yaml top-level must be a mapping — failing closed",
                path=str(self._config_path),
                type=type(parsed).__name__,
            )
            self._spaces = frozenset()
            return

        confluence_section = parsed.get("confluence")
        if not isinstance(confluence_section, dict):
            self._spaces = frozenset()
            return

        spaces_raw = confluence_section.get("spaces")
        if spaces_raw is None:
            self._spaces = frozenset()
            return
        if not isinstance(spaces_raw, list):
            logger.error(
                "confluence.spaces must be a list — failing closed",
                path=str(self._config_path),
                type=type(spaces_raw).__name__,
            )
            self._spaces = frozenset()
            return

        cleaned: set[str] = set()
        for entry in spaces_raw:
            if not isinstance(entry, str):
                logger.warning(
                    "Ignoring non-string entry in confluence.spaces",
                    entry=repr(entry),
                )
                continue
            key = entry.strip()
            if not _SPACE_KEY_RE.fullmatch(key):
                logger.warning(
                    "Ignoring invalid Confluence space key in confluence.spaces",
                    entry=repr(entry),
                )
                continue
            cleaned.add(key)

        self._spaces = frozenset(cleaned)
        if not self._logged_first_load:
            logger.info(
                "Confluence space allowlist loaded",
                path=str(self._config_path),
                count=len(self._spaces),
            )
            self._logged_first_load = True


# -----------------------------------------------------------------------------
# Module-level singleton — matches ``jira_policy`` pattern.
# -----------------------------------------------------------------------------

_confluence_policy: ConfluencePolicy | None = None
_confluence_policy_lock = threading.Lock()


def get_confluence_policy() -> ConfluencePolicy:
    """Return the process-wide ``ConfluencePolicy`` singleton."""
    global _confluence_policy
    with _confluence_policy_lock:
        if _confluence_policy is None:
            _confluence_policy = ConfluencePolicy()
        return _confluence_policy


def allowed_spaces() -> frozenset[str]:
    """Convenience accessor — ``ConfluencePolicy.allowed_spaces()`` via singleton."""
    return get_confluence_policy().allowed_spaces()


def is_space_allowed(space_key: str) -> bool:
    """Convenience accessor — ``ConfluencePolicy.is_space_allowed()`` via singleton."""
    return get_confluence_policy().is_space_allowed(space_key)


def reload_confluence_policy() -> None:
    """Force the next allowlist access to re-read from disk.

    Invoked by the gateway's ``_reload_all_config()`` hook so ``POST
    /api/v1/config/reload`` picks up operator edits without a restart.
    """
    get_confluence_policy().reload()


def reset_confluence_policy() -> None:
    """Drop the module-level singleton (test helper)."""
    global _confluence_policy
    with _confluence_policy_lock:
        _confluence_policy = None


__all__ = [
    "ConfluencePolicy",
    "allowed_spaces",
    "get_confluence_policy",
    "is_space_allowed",
    "reload_confluence_policy",
    "reset_confluence_policy",
]
