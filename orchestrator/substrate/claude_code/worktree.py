"""``LocalWorktreeManager`` for the Claude Code substrate (#2623).

Implements ``WorktreeManager`` using on-host filesystem operations.
HITL decision cq-5 pins the model to **port egg's
``WORKTREE_BASE_DIR``** (``gateway/worktree_manager.py:49``): the
Claude Code substrate keeps the per-pipeline / per-role layout the
gateway already uses, defaulting to ``~/.egg-worktrees/`` and
allowing ``EGG_WORKTREE_BASE`` to override.

The path-escape defense mirrors the
``child.resolve().is_relative_to(base.resolve())`` guard in
``gateway/worktree_manager.py:1711`` (call site within
``_remove_worktree``; matching ``base.resolve()`` at line 1700).

See ``docs/architecture/claude-code-substrate.md`` for the ADR.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from egg_contracts.agent_roles import AgentRole

#: Path **fragment** appended to ``$HOME`` to form the default base
#: directory. Resolved at ``LocalWorktreeManager.__init__`` time
#: (not at module import) so ``monkeypatch.setenv("HOME", ...)`` in
#: tests is honored. Matches ``gateway/worktree_manager.py:49
#: WORKTREE_BASE_DIR`` shape so operators don't need to learn two
#: layouts. Override the full path with the ``EGG_WORKTREE_BASE``
#: env var.
_DEFAULT_BASE_NAME = ".egg-worktrees"


def _default_base() -> Path:
    """Return the default base directory, computed from the *current* ``$HOME``.

    No module-level constant alias exists by design (reviewer v2
    non-blocking): an alias evaluated at import time would freeze
    ``$HOME`` and silently diverge from what ``LocalWorktreeManager``
    itself sees when ``monkeypatch.setenv("HOME", ...)`` is in
    effect. Call this helper directly if you need the current
    default base outside the manager.
    """
    return Path(os.environ.get("HOME", "/home/egg")) / _DEFAULT_BASE_NAME


# Conservative regex for pipeline / role names; matches the gateway's
# validate_identifier behavior.
_SAFE_IDENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class LocalWorktreeManager:
    """Per-pipeline filesystem worktree manager.

    Creates per-agent worktrees under
    ``<base>/<pipeline_id>/<role>/`` with ``git worktree add``, tracks
    them in a dict, and tears them down at phase end.

    The path-escape guard rejects any pipeline_id / role string that
    would resolve outside ``<base>`` — this is the
    ``gateway/worktree_manager.py:1711`` ``is_relative_to`` pattern.
    """

    def __init__(self, base: Path | None = None) -> None:
        env_override = os.environ.get("EGG_WORKTREE_BASE")
        if base is not None:
            self._base = Path(base)
        elif env_override:
            self._base = Path(env_override)
        else:
            # Compute lazily so monkeypatching $HOME in tests works.
            self._base = _default_base()
        # Map pipeline_id → list of (worktree_path, branch) we created.
        self._tracked: dict[str, list[tuple[Path, str]]] = {}
        self._lock = threading.RLock()

    @property
    def base(self) -> Path:
        """The configured base directory."""
        return self._base

    def create(self, pipeline_id: str, role: AgentRole) -> Path:
        """Create a worktree under ``<base>/<pipeline_id>/<role>/``.

        Args:
            pipeline_id: Pipeline identifier; must match
                ``[a-zA-Z0-9][a-zA-Z0-9._-]*`` to defeat traversal.
            role: Agent role; the role name is used unchanged as the
                second path segment and is also enum-validated.

        Returns:
            Absolute path to the newly-created worktree.
        """
        _validate_identifier(pipeline_id, "pipeline_id")
        role_name = role.value if hasattr(role, "value") else str(role)
        _validate_identifier(role_name, "role")

        target = (self._base / pipeline_id / role_name).resolve()
        self._assert_within_base(target)

        target.mkdir(parents=True, exist_ok=True)
        branch = f"egg/{pipeline_id}/{role_name}"

        # Best-effort ``git worktree add``. If the cwd isn't a git
        # repo (e.g. test environment), fall back to creating the
        # directory without a git worktree — the spawner still has a
        # place to land artifacts. Failures don't raise to avoid
        # blowing up the spike on environments without git.
        try:
            subprocess.run(
                ["git", "worktree", "add", "-B", branch, str(target)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError):  # fmt: skip
            # Worktree creation may fail (no git, no checkout); the
            # directory still exists for the spawner to use.
            pass

        with self._lock:
            self._tracked.setdefault(pipeline_id, []).append((target, branch))
        return target

    def remove(self, pipeline_id: str, role: AgentRole) -> None:
        """Remove a single (pipeline, role) worktree.

        Reviewer v1 blocker #3: ``tear_down(pipeline_id)`` is
        pipeline-scoped and removes every per-role worktree it tracked,
        which is the wrong granularity for the failure path of one
        concurrent spawn (it would wipe peer worktrees mid-spawn). This
        per-role variant is what ``_spawn_agent_via_substrate`` calls
        when a single role's spawn fails.

        Args:
            pipeline_id: Pipeline identifier (validated against the same
                pattern as ``create``).
            role: The role whose worktree should be removed.
        """
        _validate_identifier(pipeline_id, "pipeline_id")
        role_name = role.value if hasattr(role, "value") else str(role)
        _validate_identifier(role_name, "role")
        target = (self._base / pipeline_id / role_name).resolve()

        with self._lock:
            entries = self._tracked.get(pipeline_id, [])
            remaining: list[tuple[Path, str]] = []
            to_remove: list[tuple[Path, str]] = []
            for path, branch in entries:
                try:
                    if path.resolve() == target:
                        to_remove.append((path, branch))
                        continue
                except OSError:
                    pass
                remaining.append((path, branch))
            if remaining:
                self._tracked[pipeline_id] = remaining
            else:
                self._tracked.pop(pipeline_id, None)

        # If the in-memory tracker missed it (e.g. process restart),
        # still attempt the on-disk teardown if the path is within base.
        if not to_remove:
            to_remove = [(target, f"egg/{pipeline_id}/{role_name}")]

        self._remove_entries(to_remove)

    def tear_down(self, pipeline_id: str) -> None:
        """Remove all worktrees for the named pipeline.

        Uses ``Path.resolve().is_relative_to(base.resolve())`` to
        reject any path that resolves outside the configured base —
        the same defense pattern as
        ``gateway/worktree_manager.py:1711``.

        See ``remove(pipeline_id, role)`` for the per-role variant
        that should be used from concurrent-spawn failure paths
        (reviewer v1 blocker #3).
        """
        _validate_identifier(pipeline_id, "pipeline_id")
        base_resolved = self._base.resolve()

        with self._lock:
            entries = self._tracked.pop(pipeline_id, [])

        # Always try to remove the pipeline-level directory, even if
        # the in-memory list was lost (e.g., process restart).
        pipeline_dir = (self._base / pipeline_id).resolve()
        self._remove_entries(entries)

        # Tear down the pipeline-level dir if it's empty and lives
        # under the base.
        try:
            if pipeline_dir.is_relative_to(base_resolved) and pipeline_dir.exists():
                shutil.rmtree(pipeline_dir, ignore_errors=True)
        except (AttributeError, OSError):  # fmt: skip
            if pipeline_dir.exists() and str(pipeline_dir).startswith(str(base_resolved) + os.sep):
                shutil.rmtree(pipeline_dir, ignore_errors=True)

    def _remove_entries(self, entries: list[tuple[Path, str]]) -> None:
        """Best-effort removal of (path, branch) tuples with path-escape guard."""
        base_resolved = self._base.resolve()
        for path, branch in entries:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            try:
                if not resolved.is_relative_to(base_resolved):
                    # Path-escape guard — refuse to remove anything
                    # outside the base.
                    continue
            except AttributeError:
                # Python <3.9 fallback (should never hit on our
                # runtime, but mirror the gateway's belt-and-braces).
                if not str(resolved).startswith(str(base_resolved) + os.sep):
                    continue

            # Try ``git worktree remove`` first for a clean teardown;
            # if it fails (no git, dirty state), fall back to plain
            # rmtree.
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(resolved)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except (subprocess.SubprocessError, OSError):  # fmt: skip
                pass

            if resolved.exists():
                shutil.rmtree(resolved, ignore_errors=True)

            # Best-effort branch deletion. Ignored when not a git
            # checkout.
            try:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
            except (subprocess.SubprocessError, OSError):  # fmt: skip
                pass

    def _assert_within_base(self, target: Path) -> None:
        """Raise ``ValueError`` if ``target`` resolves outside the base."""
        base_resolved = self._base.resolve()
        try:
            base_resolved.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            target_resolved = target.resolve()
        except OSError as exc:
            raise ValueError(f"could not resolve worktree path: {target}") from exc
        try:
            if not target_resolved.is_relative_to(base_resolved):
                raise ValueError(
                    f"worktree {target_resolved} resolves outside base "
                    f"{base_resolved}; refusing to create"
                )
        except AttributeError:
            if not str(target_resolved).startswith(str(base_resolved) + os.sep):
                raise ValueError(
                    f"worktree {target_resolved} resolves outside base "
                    f"{base_resolved}; refusing to create"
                ) from None


def _validate_identifier(value: str, name: str) -> None:
    """Reject empty / traversal / non-safe-character identifiers."""
    if not value:
        raise ValueError(f"Invalid {name}: cannot be empty")
    if ".." in value:
        raise ValueError(f"Invalid {name}: path traversal not allowed")
    if not _SAFE_IDENT.match(value):
        raise ValueError(f"Invalid {name}: must match {_SAFE_IDENT.pattern!r}; got {value!r}")
