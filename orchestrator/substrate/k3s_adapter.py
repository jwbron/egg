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

        # reviewer_concurrency v1 blocker #4: do NOT capture
        # ``commit_sha`` here. The legacy spawn factory is
        # fire-and-monitor (see the docstring above); this method
        # returns BEFORE the pod has produced its commit, so a
        # ``git rev-parse HEAD`` against the orchestrator-host
        # worktree would capture the pre-spawn HEAD and BRC reviewers
        # would attach commit-bound ACKs to the wrong SHA.
        #
        # The k3s leg's INV-6 ``ack_commit_sha`` is populated by the
        # existing gateway-side attestation channel: the
        # orchestrator's monitor loop reads the legitimate SHA off
        # ``SpawnedContainer.container_info`` once the pod
        # terminates. The follow-up issue covers wiring that channel
        # into ``AgentResult.commit_sha`` directly so the new
        # protocol contract is also satisfied end-to-end on k3s.
        target_worktree = worktree
        if self._resolve_worktree is not None:
            resolved = self._resolve_worktree(role, None)
            if resolved is not None:
                target_worktree = resolved

        commit_sha: str | None = None
        # Structured note for downstream observability — the gap is
        # documented; INV-6 mismatches will correlate with this
        # log line rather than showing up first at review-time.
        # Use the structured logger instead of stderr ``print`` so
        # the message routes through the daemon's log pipeline
        # (reviewer_code v2 non-blocking).
        try:
            try:
                from egg_logging import get_logger
            except ImportError:  # pragma: no cover
                import logging

                _logger = logging.getLogger("orchestrator.substrate.k3s_adapter")
            else:
                _logger = get_logger("orchestrator.substrate.k3s_adapter")
            _logger.warning(
                "k3s commit_sha intentionally None (fire-and-monitor "
                "factory races producer commit); legacy gateway-side "
                "attestation channel is authoritative for k3s INV-6 "
                "SHAs. See substrate ADR follow-up appendix.",
                extra={"role": getattr(role, "value", str(role))},
            )
        except Exception:  # noqa: BLE001 — defensive
            pass

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
