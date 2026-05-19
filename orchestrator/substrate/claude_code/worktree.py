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

#: Default base directory — matches
#: ``gateway/worktree_manager.py:49 WORKTREE_BASE_DIR`` shape so
#: operators don't need to learn two layouts. Override with the
#: ``EGG_WORKTREE_BASE`` env var.
DEFAULT_BASE = Path(os.environ.get("HOME", "/home/egg")) / ".egg-worktrees"

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
            self._base = DEFAULT_BASE
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
        except subprocess.SubprocessError, OSError:
            # Worktree creation may fail (no git, no checkout); the
            # directory still exists for the spawner to use.
            pass

        with self._lock:
            self._tracked.setdefault(pipeline_id, []).append((target, branch))
        return target

    def tear_down(self, pipeline_id: str) -> None:
        """Remove all worktrees for the named pipeline.

        Uses ``Path.resolve().is_relative_to(base.resolve())`` to
        reject any path that resolves outside the configured base —
        the same defense pattern as
        ``gateway/worktree_manager.py:1711``.
        """
        _validate_identifier(pipeline_id, "pipeline_id")
        base_resolved = self._base.resolve()

        with self._lock:
            entries = self._tracked.pop(pipeline_id, [])

        # Always try to remove the pipeline-level directory, even if
        # the in-memory list was lost (e.g., process restart).
        pipeline_dir = (self._base / pipeline_id).resolve()
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
            except subprocess.SubprocessError, OSError:
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
            except subprocess.SubprocessError, OSError:
                pass

        # Tear down the pipeline-level dir if it's empty and lives
        # under the base.
        try:
            if pipeline_dir.is_relative_to(base_resolved) and pipeline_dir.exists():
                shutil.rmtree(pipeline_dir, ignore_errors=True)
        except AttributeError, OSError:
            if pipeline_dir.exists() and str(pipeline_dir).startswith(str(base_resolved) + os.sep):
                shutil.rmtree(pipeline_dir, ignore_errors=True)

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
