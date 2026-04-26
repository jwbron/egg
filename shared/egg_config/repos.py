"""Centralized layered repo-config loader (issue #2073).

This is the single import point for the merged view of
``<repo>/.egg/repositories.yaml`` plus ``~/.config/egg/repositories.yaml``.
:mod:`config.repo_config` and :mod:`sandbox.egg_lib.docker` both consume
the merged view through this module so every host-side caller sees the
same layered dict.

Merge semantics (HITL decisions 5/9/10):

* The user file is read first, providing operator-scoped fields.
* If a checkout path is supplied, ``<checkout>/.egg/repositories.yaml``
  is auto-discovered (silent-skip if absent — decision-10).
* For each repo, the repo-defaults block is laid down first, then the
  user-file ``repo_settings[<repo>]`` block overrides it leaf-by-leaf.
* List-valued fields (``persist``, ``watch_files``, ``checks``,
  ``extra_packages.*``, ``local_repos.paths``) replace by default.  No
  ``extends:`` keyword in v1 (decision-9).
* Operator-scoped top-level keys come exclusively from the user file
  (decision-5 rationale).

Repo-file persist denylist (NACK-5 / risk-3 mitigation):

* ``persist:`` entries declared in the repo-defaults file may NOT
  point under ``/etc``, ``/root``, ``/var``, ``/home/``, ``/proc``,
  ``/sys``, ``/dev``, ``/.ssh`` and must lie inside ``/usr/local/``,
  ``/opt/``, or be repo-relative.
* Operator-side overrides remain unrestricted — operators may persist
  whatever paths they want from their own machine.

Performance:

* :func:`load_merged_repo_config` is wrapped in an mtime-keyed
  ``functools.lru_cache`` so repeated calls within a process don't
  re-scan the filesystem (NACK non-blocking, risk-13).
* :func:`reload_config` clears the cache (called by the gateway's
  SIGHUP handler).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .repos_schema import (
    OPERATOR_SCOPED_PER_REPO_KEYS,
    OPERATOR_SCOPED_TOP_LEVEL_KEYS,
    ConfigError,
    RepoDefaultsFile,
    UserConfigFile,
    classify_persist_entry,
)

# ---------------------------------------------------------------------------
# Persist-path denylist (NACK-5)
# ---------------------------------------------------------------------------

# Absolute prefixes that a repo-file persist entry must NEVER touch.
# The intent is to prevent a malicious feature branch from shipping a
# ``persist: [/etc/passwd]`` entry that egg silently honours during a
# sandbox image build.
_DENYLIST_ABS_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/root",
    "/var",
    "/home/",
    "/proc",
    "/sys",
    "/dev",
)

# Substring marker — refuse anything with ``/.ssh`` in the path so a
# user-relative ``.ssh`` doesn't slip through.
_DENYLIST_SUBSTRINGS: tuple[str, ...] = ("/.ssh",)

# When an entry IS absolute and survives the denylist, it must lie
# under one of these allowed prefixes.
_ALLOWED_ABS_PREFIXES: tuple[str, ...] = (
    "/usr/local/",
    "/opt/",
)

# Per-repo blocks that the merge engine threads through.  Used both for
# layering and to pinpoint list-replacement vs deep-merge behavior.
_LIST_REPLACE_KEYS: frozenset[str] = frozenset(
    {"persist", "watch_files", "checks"}
)


def _is_denylisted_abs_path(entry: str) -> bool:
    """Return True if ``entry`` is an absolute path the repo file may NOT persist.

    Mirrors the security gate documented at the top of this module.
    """
    # Substring check first — catches /.ssh in any guise.
    for marker in _DENYLIST_SUBSTRINGS:
        if marker in entry:
            return True
    # Hard prefix denylist for absolute paths.
    for prefix in _DENYLIST_ABS_PREFIXES:
        if entry == prefix or entry.startswith(prefix + "/") or entry.startswith(
            prefix + "\0"  # never matches; documents intent
        ):
            return True
        # /var, /etc, /root, /proc, /sys, /dev (no trailing slash variants)
        if prefix.endswith("/"):
            continue
        if entry == prefix or entry.startswith(prefix + "/"):
            return True
    # Outside the safe set?
    if entry.startswith("/") and not any(
        entry.startswith(p) for p in _ALLOWED_ABS_PREFIXES
    ):
        # Not in /usr/local/ or /opt/ → denied.
        return True
    return False


def _enforce_repo_persist_denylist(
    persist_entries: list[str], *, repo_label: str
) -> None:
    """Reject repo-file ``persist:`` entries that fall in the denylist.

    Repo-relative entries always pass — they're rooted at the checkout
    and can never escape the repo without symlink games (a separate
    concern handled at copy time in :mod:`sandbox.egg_lib.docker`).
    """
    for entry in persist_entries:
        # Repo-relative entries are always safe at this layer.
        if classify_persist_entry(entry) != "system":
            continue
        if _is_denylisted_abs_path(entry):
            raise ConfigError(
                f"{repo_label}: persist entry {entry!r} is not allowed "
                "in a checked-in repo-defaults file. Repo-side persist "
                "entries must be repo-relative or rooted under "
                "/usr/local/ or /opt/. Move this entry to your user "
                "file at ~/.config/egg/repositories.yaml if you really "
                "need to persist it."
            )


# ---------------------------------------------------------------------------
# Merged view dataclass
# ---------------------------------------------------------------------------


@dataclass
class MergedRepoConfig:
    """The merged view returned by :func:`load_merged_repo_config`.

    ``user_file`` is the raw user-file dict (post-validation), so callers
    can still reach operator-scoped fields like ``writable_repos`` /
    ``local_repos`` / ``github_username`` directly.

    ``repo_blocks`` maps ``"owner/name"`` (case-preserved as it appears
    in either file) to the merged per-repo dict that consumers should
    use. The dict shape mirrors the user-facing per-repo block — i.e. it
    contains ``persist`` (a single list), not the legacy two-list shape.
    """

    user_file: dict[str, Any] = field(default_factory=dict)
    repo_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_repo(self, repo: str) -> dict[str, Any]:
        """Look up the merged per-repo block, case-insensitive."""
        repo_lower = repo.lower()
        for key, value in self.repo_blocks.items():
            if key.lower() == repo_lower:
                return value
        return {}


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def _user_config_path(user_path: Path | None) -> Path | None:
    """Resolve which user-file path to read.

    Search order (matches :mod:`config.repo_config._get_config_path` so
    the legacy callers don't shift behaviour):

    1. The explicit ``user_path`` argument.
    2. The ``EGG_REPO_CONFIG`` environment variable.
    3. ``~/.config/egg/repositories.yaml``.
    4. ``~/repos/egg/config/repositories.yaml`` (container mount).

    Returns ``None`` if no candidate exists — callers may still get a
    useful merged view from a repo-defaults file alone.
    """
    if user_path is not None:
        return user_path if user_path.exists() else None

    env_path = os.environ.get("EGG_REPO_CONFIG")
    if env_path:
        env_config = Path(env_path)
        if env_config.exists():
            return env_config

    host_config = Path.home() / ".config" / "egg" / "repositories.yaml"
    if host_config.exists():
        return host_config

    container_config = Path.home() / "repos" / "egg" / "config" / "repositories.yaml"
    if container_config.exists():
        return container_config

    return None


def _repo_config_path(checkout: Path | None) -> Path | None:
    """Auto-discover ``<checkout>/.egg/repositories.yaml`` (decision-10)."""
    if checkout is None:
        return None
    candidate = Path(checkout) / ".egg" / "repositories.yaml"
    return candidate if candidate.exists() else None


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file as a dict; raise :class:`ConfigError` with both
    file paths in the message if it fails to parse.
    """
    try:
        with path.open() as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: top-level YAML must be a mapping; got "
            f"{type(data).__name__}."
        )
    return data


def _merge_repo_block(
    *, base: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """Merge ``override`` onto ``base`` per the rules at the top of this module.

    * List-valued fields (``persist``, ``watch_files``, ``checks``)
      replace outright if present in ``override``.
    * Dict fields (``build_commands``) deep-merge.
    * Scalars (``auth_mode``, ``checkpoint_repo``, ``template``) are
      replaced if the override carries a non-None value.
    """
    out: dict[str, Any] = {}
    keys = set(base) | set(override)
    for key in keys:
        if key in _LIST_REPLACE_KEYS:
            if key in override:
                out[key] = list(override[key] or [])
            elif key in base:
                out[key] = list(base[key] or [])
            continue
        if key == "build_commands":
            base_block = base.get("build_commands") or {}
            override_block = override.get("build_commands") or {}
            if not base_block and not override_block:
                continue
            out["build_commands"] = _merge_build_commands(base_block, override_block)
            continue
        # Scalars (auth_mode, checkpoint_repo, template, schemaVersion,
        # restrict_to_configured_users, disable_auto_fix, …) — override
        # wins when present, falling back to base.
        if key in override and override[key] is not None:
            out[key] = override[key]
        elif key in base and base[key] is not None:
            out[key] = base[key]
    return out


def _merge_build_commands(
    base: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge ``build_commands`` blocks.

    The block carries scalar fields (``commands``, ``watch_files``,
    ``persist``) that are *all* list-valued in the new schema, plus
    legacy ``persist_dirs`` / ``persist_system_dirs`` which are rejected
    by the schema layer before they reach us. We treat every list as
    replace-by-default.
    """
    keys = set(base) | set(override)
    out: dict[str, Any] = {}
    for key in keys:
        if key in override:
            out[key] = override[key]
        else:
            out[key] = base[key]
    return out


def _classify_persist_for_manifest(
    persist: list[str],
) -> tuple[list[str], list[str]]:
    """Split a unified ``persist:`` list into the legacy two-list manifest shape.

    Used by :mod:`sandbox.egg_lib.docker` so the host-side manifest
    classifier produces ``persist_dirs`` + ``persist_system_dirs`` from
    the unified list (architect Component C3 — keeps the sandbox image
    cross-version stable).
    """
    repo_dirs: list[str] = []
    system_dirs: list[str] = []
    for entry in persist:
        if classify_persist_entry(entry) == "system":
            system_dirs.append(entry)
        else:
            repo_dirs.append(entry)
    return repo_dirs, system_dirs


def load_merged_repo_config(
    checkout: Path | None = None,
    user_path: Path | None = None,
) -> MergedRepoConfig:
    """Load and merge the repo-defaults + user files into a single view.

    Args:
        checkout: Path to the working checkout whose
            ``.egg/repositories.yaml`` should be auto-discovered. Pass
            ``None`` to skip auto-discovery (e.g. when only the user
            file is needed).
        user_path: Override the user-file location. Falls back to the
            ``EGG_REPO_CONFIG`` env var and the historical search
            paths.

    Returns:
        A :class:`MergedRepoConfig` carrying the validated user-file
        dict and the merged per-repo blocks.

    Raises:
        ConfigError: If either file fails schema validation, the repo
            file declares operator-scoped keys, or a repo-file persist
            entry hits the denylist.
    """
    user_resolved = _user_config_path(user_path)
    repo_resolved = _repo_config_path(checkout)

    user_mtime = (
        user_resolved.stat().st_mtime_ns if user_resolved is not None else 0
    )
    repo_mtime = (
        repo_resolved.stat().st_mtime_ns if repo_resolved is not None else 0
    )

    return _load_cached(
        str(user_resolved) if user_resolved is not None else "",
        user_mtime,
        str(repo_resolved) if repo_resolved is not None else "",
        repo_mtime,
    )


@lru_cache(maxsize=64)
def _load_cached(
    user_path_str: str,
    _user_mtime: int,
    repo_path_str: str,
    _repo_mtime: int,
) -> MergedRepoConfig:
    """Mtime-keyed cache wrapper around the actual loader.

    The mtime arguments aren't used inside the function — they're part
    of the cache key so the LRU re-reads when either file changes.
    """
    user_path = Path(user_path_str) if user_path_str else None
    repo_path = Path(repo_path_str) if repo_path_str else None

    user_dict: dict[str, Any] = {}
    if user_path is not None:
        user_dict = _read_yaml(user_path)

    repo_dict: dict[str, Any] = {}
    if repo_path is not None:
        repo_dict = _read_yaml(repo_path)

    user_file = UserConfigFile.from_dict(
        user_dict, file_label=str(user_path) if user_path else "<user-file>"
    )

    repo_defaults: RepoDefaultsFile | None = None
    if repo_path is not None:
        repo_defaults = RepoDefaultsFile.from_dict(
            repo_dict, file_label=str(repo_path)
        )
        # Repo-side persist denylist enforcement (NACK-5).
        _enforce_repo_persist_denylist(
            repo_defaults.persist,
            repo_label=str(repo_path),
        )

    # Build merged per-repo blocks. The repo-defaults block (if any)
    # represents *the current checkout* — keyed under the user-file
    # ``repo_settings`` slot whose name matches it. Without a clear
    # owner/name signal we attribute the repo-defaults block to every
    # entry in ``repo_settings`` and to a synthetic key so callers can
    # find it regardless.
    merged: dict[str, dict[str, Any]] = {}

    repo_block_dict: dict[str, Any] = (
        repo_defaults.to_dict() if repo_defaults is not None else {}
    )

    user_repo_settings = user_file.repo_settings
    for repo_name, override_block in user_repo_settings.items():
        merged[repo_name] = _merge_repo_block(
            base=repo_block_dict, override=override_block
        )

    # Surface the repo-defaults block under a synthetic ``__checkout__``
    # key when no explicit repo_settings entry exists — useful for the
    # validator and the onboard skill.
    if repo_defaults is not None and not merged:
        merged["__checkout__"] = repo_block_dict

    return MergedRepoConfig(user_file=user_file.raw, repo_blocks=merged)


def reload_config() -> None:
    """Clear the loader's mtime cache so the next call re-reads from disk.

    Compatible with :func:`config.repo_config.reload_config` — the
    gateway's SIGHUP handler calls both.
    """
    _load_cached.cache_clear()


__all__ = [
    "MergedRepoConfig",
    "OPERATOR_SCOPED_PER_REPO_KEYS",
    "OPERATOR_SCOPED_TOP_LEVEL_KEYS",
    "_classify_persist_for_manifest",
    "_enforce_repo_persist_denylist",
    "load_merged_repo_config",
    "reload_config",
]
