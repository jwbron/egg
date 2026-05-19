"""k3s adapter satisfying the ``AgentSpawner`` protocol (#2623).

See ``docs/architecture/claude-code-substrate.md`` for the broader
substrate model. The Claude Code substrate is exercised end-to-end by
the walking-skeleton spike (cq-11); the k3s substrate is a co-equal
substrate (cq-1 + cq-9) and must continue to work after the
``concurrent_executor._spawn_agent`` seam is patched in TASK-1-2 to
dispatch through ``select_substrate(...).spawner``.

This adapter is the thin wrapper that lets the existing
``KubernetesSpawner.create_concurrent_spawn_fn`` factory satisfy the
new ``AgentSpawner`` protocol from day one — without re-implementing
spawn lifecycle, image selection, or session registration on the k3s
side.

INTERFACE STABILITY: v0.x unstable.

The k3s adapter is intentionally a re-wire shim: it imports from
``orchestrator.kubernetes_spawner`` but does NOT patch the call site
that's a TASK-1-2 concern. The adapter is purely additive in this
file; the existing
``orchestrator/concurrent_executor.py:504 _spawn_agent`` call site
remains untouched until TASK-1-2.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from egg_contracts.agent_roles import AgentRole

from .spawner import AgentResult


class K3sSpawnerAdapter:
    """Adapter wrapping ``KubernetesSpawner.create_concurrent_spawn_fn``.

    The wrapped factory returns a callable with the legacy spawn
    signature ``(role, branch, extra_env, command) -> SpawnedContainer``.
    This adapter exposes the new
    ``spawn(role, prompt, env, worktree) -> AgentResult`` signature
    from cq-4 and bridges the two by:

    1. Calling the legacy factory with empty ``command`` (production
       paths build the prompt-wrapping command inside the orchestrator
       via ``build_consensus_wrapped_command`` already; the wrapper
       passes through to the spawner unchanged).
    2. Capturing the resulting container's commit SHA via
       ``git -C <worktree> rev-parse HEAD`` so reviewers can attach
       commit-bound ACKs (INV-6,
       ``orchestrator/action_guards.py:631``, body at line 757).
    3. Translating the legacy ``SpawnedContainer`` shape into the new
       ``AgentResult`` shape.

    The adapter does not block on the underlying job — the legacy
    factory was always fire-and-monitor; the synchronous-spawn
    contract for the k3s leg is satisfied because the orchestrator's
    existing ``ConcurrentPhaseExecutor`` waits on container
    completion via ``container_id`` after the legacy spawn returns.
    Callers that need true blocking should run inside the
    ConcurrentPhaseExecutor's monitor loop or layer their own wait on
    top of the returned ``container_id``.

    Args:
        legacy_spawn_fn: The callable returned by
            ``KubernetesSpawner.create_concurrent_spawn_fn(...)``.
        worktree_resolver: Optional callable
            ``(role: AgentRole, branch: str | None) -> Path | None``
            that returns the on-disk worktree path so the adapter can
            run ``git rev-parse HEAD``. When ``None``, the adapter
            skips commit-sha capture and returns ``AgentResult``
            with ``commit_sha=None`` — callers (or downstream
            reviewers) can fall back to a separate commit-attestation
            channel.
    """

    def __init__(
        self,
        legacy_spawn_fn: Any,
        *,
        worktree_resolver: Any | None = None,
    ) -> None:
        self._legacy = legacy_spawn_fn
        self._resolve_worktree = worktree_resolver

    def spawn(
        self,
        role: AgentRole,
        prompt: str,
        env: Mapping[str, str],
        worktree: Path,
    ) -> AgentResult:
        """Spawn an agent via the wrapped k3s factory.

        Args:
            role: The role to spawn.
            prompt: Task prompt (forwarded as the ``command`` arg to
                the legacy factory when non-empty; the orchestrator's
                production paths normally pre-build this via
                ``build_consensus_wrapped_command``).
            env: Extra env vars to inject into the agent container.
            worktree: Path used for the commit-sha capture. The k3s
                spawner manages its own gateway-side worktree
                independently; this argument is only consulted at the
                end to capture ``commit_sha``.
        """
        start = time.monotonic()
        # ``prompt`` here is forwarded as the optional ``command``
        # arg only when callers want to inject a non-default command;
        # most production paths construct the command outside the
        # spawner and pass it via ``extra_env`` already.
        command: list[str] | None = None
        if prompt:
            command = ["claude", "--print", prompt]
        spawned = self._legacy(
            role=role,
            branch=None,
            extra_env=dict(env),
            command=command,
        )
        duration = time.monotonic() - start

        # Resolve commit SHA from the on-disk worktree for INV-6.
        commit_sha: str | None = None
        target_worktree = worktree
        if self._resolve_worktree is not None:
            resolved = self._resolve_worktree(role, None)
            if resolved is not None:
                target_worktree = resolved
        if target_worktree is not None and target_worktree.exists():
            try:
                proc = subprocess.run(
                    ["git", "-C", str(target_worktree), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if proc.returncode == 0:
                    commit_sha = proc.stdout.strip() or None
            except subprocess.SubprocessError, OSError:
                # Fall through with commit_sha=None — the gateway-side
                # k3s path may not have a worktree at this exact path
                # yet. The legacy attestation flow remains the source
                # of truth for k3s commit SHAs.
                commit_sha = None

        # Reviewer_code_holistic v1 finding #12: surface a structured
        # warning when commit_sha is None so INV-6 attach-time failures
        # are correlated with the silent capture failure here rather
        # than showing up first at review-time.
        if commit_sha is None:
            import sys

            print(
                "[K3sSpawnerAdapter] WARNING: commit_sha not captured "
                f"for role={getattr(role, 'value', role)} worktree="
                f"{target_worktree}. INV-6 reviewers will not have a "
                "commit-bound ACK target from this AgentResult. "
                "The legacy gateway-side attestation flow may still "
                "carry the SHA out-of-band; the follow-up issue "
                "covers wiring that into AgentResult.commit_sha "
                "directly.",
                file=sys.stderr,
            )

        # The legacy ``SpawnedContainer`` carries no stdout/exit_code
        # directly — that data lands on ``container_info`` and is
        # consumed by the orchestrator's monitor loop. We surface a
        # minimal ``AgentResult`` here so the new protocol contract
        # is satisfied; callers that need the full container payload
        # continue to read it via the orchestrator's container store.
        return AgentResult(
            stdout=getattr(spawned, "stdout", ""),
            exit_code=getattr(spawned, "exit_code", 0),
            duration_seconds=duration,
            worktree=target_worktree,
            commit_sha=commit_sha,
            artifacts=[],
        )
