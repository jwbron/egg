"""Agent-spawner protocol for the substrate-swap walking skeleton (#2623).

See the architecture decision record at
``docs/architecture/claude-code-substrate.md`` for the four-interface
substrate model. This module defines the ``AgentSpawner`` protocol and
the ``AgentResult`` dataclass that every substrate implementation must
satisfy.

The protocol shape is pinned by HITL decision cq-4 (synchronous spawn):
``spawn(role, prompt, env, worktree) -> AgentResult`` blocks the caller
until the agent completes. Internal concurrency (thread pool, etc.) is
the spawner's responsibility — callers can issue concurrent ``spawn()``
calls from a ``ThreadPoolExecutor`` if they need fan-out.

INTERFACE STABILITY: v0.x unstable.

This abstraction is part of a walking-skeleton spike (cq-11). The shape
may change in incompatible ways in the follow-up rollout; downstream
consumers should not assume API stability until the follow-up issue
formally promotes the protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from egg_contracts.agent_roles import AgentRole


@dataclass
class AgentResult:
    """Result of a synchronous spawn invocation.

    Fields:
        stdout: Captured stdout from the agent (may be empty when the
            agent writes only via the filesystem).
        exit_code: Process exit code. 0 means success.
        duration_seconds: Wall-clock seconds the spawn took, end-to-end.
        worktree: Path to the worktree the agent ran in. The caller
            typically reads artifacts out of this directory after the
            spawn returns.
        commit_sha: SHA of the worktree's HEAD commit **after** the
            agent ran. This is captured by the spawner via
            ``git -C <worktree> rev-parse HEAD`` immediately before the
            ``AgentResult`` is returned. ``None`` when the worktree
            does not contain a git checkout. Required to satisfy
            invariant INV-6 (``ack_commit_sha`` consistency) in
            ``orchestrator/action_guards.py:631`` (body at line 757):
            reviewers attach commit-bound ACKs to the producer's
            recorded ``commit_sha``, so the spawner must return it.
        artifacts: Optional list of artifact paths the agent produced.
            Substrate-implementation-defined; empty by default.
    """

    stdout: str = ""
    exit_code: int = 0
    duration_seconds: float = 0.0
    worktree: Path | None = None
    commit_sha: str | None = None
    artifacts: list[str] = field(default_factory=list)


@runtime_checkable
class AgentSpawner(Protocol):
    """Protocol every substrate spawner must satisfy.

    Method signature pinned by HITL decision cq-4 (synchronous spawn):
    the caller blocks until the agent finishes. Substrate
    implementations own their internal concurrency strategy; the
    ``ConcurrentPhaseExecutor`` fans out by submitting concurrent
    ``spawn()`` calls to a ``ThreadPoolExecutor``.
    """

    def spawn(
        self,
        role: AgentRole,
        prompt: str,
        env: Mapping[str, str],
        worktree: Path,
    ) -> AgentResult:
        """Spawn an agent of ``role`` and block until it completes.

        Args:
            role: The role to spawn (drives system-prompt assembly and
                file-restriction enforcement).
            prompt: The role-specific task prompt to inject. The
                spawner is responsible for prepending the canonical
                role rubric via ``build_system_prompt(...)``.
            env: Environment variables to set on the agent. The
                spawner may add or override entries (e.g.
                ``EGG_AGENT_ROLE``, ``EGG_PIPELINE_ID``).
            worktree: Path to the per-agent worktree the spawner has
                already created via ``WorktreeManager.create(...)``.

        Returns:
            An ``AgentResult`` with the spawn's outputs. The
            ``commit_sha`` field is REQUIRED to be populated when the
            agent produced a commit; reviewers attach commit-bound
            ACKs to it (INV-6).
        """
        ...
