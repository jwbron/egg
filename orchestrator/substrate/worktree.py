"""Worktree-manager protocol for the substrate-swap spike (#2623).

See ``docs/architecture/claude-code-substrate.md`` for the broader
substrate model. This module defines the ``WorktreeManager`` protocol
every substrate implementation must satisfy.

HITL decision cq-5 pins the worktree model to **port egg's
``WORKTREE_BASE_DIR``** (``gateway/worktree_manager.py:49``): the
Claude Code substrate keeps the per-pipeline / per-role layout the
gateway already uses, so agents continue to run in isolated worktrees
without changing what egg's filesystem looks like.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from egg_contracts.agent_roles import AgentRole


@runtime_checkable
class WorktreeManager(Protocol):
    """Protocol every substrate worktree manager must satisfy."""

    def create(self, pipeline_id: str, role: AgentRole) -> Path:
        """Create a per-agent worktree.

        Args:
            pipeline_id: Pipeline identifier (e.g. ``"issue-2623"``).
            role: Agent role; used to name the worktree branch.

        Returns:
            Absolute path to the worktree the spawner will run the
            agent inside.
        """
        ...

    def tear_down(self, pipeline_id: str) -> None:
        """Remove all worktrees for the named pipeline.

        Implementations MUST use a path-escape guard equivalent to the
        ``child.resolve().is_relative_to(base.resolve())`` defense in
        ``gateway/worktree_manager.py:1711`` so a malicious pipeline
        id cannot delete files outside the configured base.
        """
        ...
