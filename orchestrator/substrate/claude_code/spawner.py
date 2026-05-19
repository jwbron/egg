"""``ClaudeCodeSpawner`` for the Claude Code substrate (#2623).

Implements ``AgentSpawner`` (HITL decision cq-4: synchronous spawn)
by driving a subagent through ``shared/egg_harness`` (``run_agent``)
inside the user's Claude Code session. The caller blocks until the
subagent completes; internal concurrency is the spawner's
responsibility per cq-4.

Substrate-swap framing (reviewer_code_holistic v1 finding #3)
-------------------------------------------------------------

The walking-skeleton spike runs egg's existing ``egg_harness`` loop
in-process to the user's Claude Code session — it does NOT, at the
spike level, dispatch via Claude Code's native ``Agent`` tool with
``subagent_type="general-purpose"``. The harness drives the
AnthropicProvider directly and exposes its own tool registry.

This is a deliberate scope decision for the spike: the harness
runner is what the existing k3s sandbox uses inside its pod, so
re-hosting it inside Claude Code is the minimum-viable substrate
swap. The follow-up issue (listed in the ADR's "Open work"
appendix) covers wiring an alternative
``ClaudeCodeAgentToolSpawner`` that emits an ``Agent`` tool
envelope for the parent session's outer loop to execute. Until
then this spawner is functionally a "re-host the harness in the
user's session" path, not a "swap to Claude Code primitives" path.
The R1 trust-context section of the ADR documents the security
delta this framing implies.

Key responsibilities (in the implemented scope):

1. Assemble the system prompt via ``build_system_prompt(...)`` from
   ``shared/egg_harness/prompt.py:24`` — this is the structural fix
   issue #2622 documents (the orchestrator wraps the per-role rubric
   alongside any prompt extras).
2. Run the subagent synchronously, capturing stdout / exit code /
   duration.
3. Capture ``commit_sha`` via ``git -C <worktree> rev-parse HEAD``
   *immediately* after the subagent returns. INV-6 in
   ``orchestrator/action_guards.py:631`` (body at line 757) requires
   this so reviewers can attach commit-bound ACKs to the producer's
   recorded SHA.

See ``docs/architecture/claude-code-substrate.md`` for the ADR.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from egg_contracts.agent_roles import AgentRole
from egg_harness.prompt import PromptSource, build_system_prompt

from ..spawner import AgentResult


class ClaudeCodeSpawner:
    """Synchronous subagent spawner for the Claude Code substrate.

    The spawner blocks the caller (cq-4) and returns an
    ``AgentResult`` once the subagent completes. Multiple concurrent
    ``spawn()`` calls are safe: the spawner is stateless beyond the
    optional injected ``run_agent_fn``.

    Args:
        run_agent_fn: Optional override for the subagent runner. The
            default delegates to ``shared.egg_harness.client.run_agent``,
            which drives the egg harness loop and returns its own
            ``AgentResult`` (we translate it into this module's
            ``AgentResult`` shape). The override lets tests substitute
            a deterministic fake without standing up the full harness.
        role_rubric_loader: Optional callable
            ``(role: AgentRole) -> str`` that returns the role-specific
            rubric markdown to prepend. When ``None``, the spawner
            falls back to a minimal "you are <role>" string. The
            production wiring injects a loader that reads from
            ``plugins/egg-sdlc/skills/egg-sdlc/agents/<role>.md`` —
            same layout as the existing refine-plan skill.
    """

    def __init__(
        self,
        *,
        run_agent_fn: Callable[..., Any] | None = None,
        role_rubric_loader: Callable[[AgentRole], str] | None = None,
    ) -> None:
        self._run_agent = run_agent_fn
        self._load_rubric = role_rubric_loader or _default_role_rubric

    def spawn(
        self,
        role: AgentRole,
        prompt: str,
        env: Mapping[str, str],
        worktree: Path,
    ) -> AgentResult:
        """Spawn the subagent and block until it completes.

        Args:
            role: Role to spawn (drives the rubric prepended via
                ``build_system_prompt``).
            prompt: Task-specific prompt body. The role rubric is
                prepended automatically; the caller passes only the
                task context.
            env: Extra env vars to set on the subagent. The spawner
                always sets ``EGG_AGENT_ROLE`` and ``EGG_WORKTREE_ROOT``
                in addition to whatever the caller supplies.
            worktree: Path the subagent runs in.

        Returns:
            ``AgentResult`` with ``commit_sha`` populated from
            ``git -C <worktree> rev-parse HEAD``.
        """
        start = time.monotonic()
        system_prompt = self._build_system_prompt(role)

        merged_env = {
            **dict(env),
            "EGG_AGENT_ROLE": role.value if hasattr(role, "value") else str(role),
            "EGG_WORKTREE_ROOT": str(worktree),
        }

        harness_result: Any | None = None
        runner_error: str | None = None
        if self._run_agent is None:
            # Lazy import: the production runner pulls in Anthropic
            # provider code that's expensive at import time.
            try:
                from egg_harness.client import run_agent as harness_run_agent
            except ImportError as exc:
                runner_error = f"egg_harness unavailable: {exc}"
                harness_run_agent = None  # type: ignore[assignment]
            runner: Callable[..., Any] | None = harness_run_agent
        else:
            runner = self._run_agent

        if runner is not None and runner_error is None:
            try:
                harness_result = runner(
                    prompt,
                    system_prompt=system_prompt,
                    cwd=str(worktree),
                    env=merged_env,
                )
            except Exception as exc:  # pragma: no cover - defensive
                runner_error = f"run_agent raised: {exc!r}"

        duration = time.monotonic() - start

        # Capture commit SHA — INV-6 required.
        commit_sha = _capture_head_sha(worktree)

        stdout = getattr(harness_result, "stdout", "") or ""
        exit_code = getattr(harness_result, "returncode", 0)
        if runner_error is not None and exit_code == 0:
            exit_code = 1
            stdout = stdout or runner_error

        return AgentResult(
            stdout=stdout,
            exit_code=int(exit_code or 0),
            duration_seconds=duration,
            worktree=worktree,
            commit_sha=commit_sha,
            artifacts=[],
        )

    def _build_system_prompt(self, role: AgentRole) -> str:
        """Assemble the system prompt for ``role``.

        Uses ``build_system_prompt(...)`` from
        ``shared/egg_harness/prompt.py:24`` — the structural depth
        fix from issue #2622 lives in this helper, so we route all
        prompt assembly through it.
        """
        rubric = self._load_rubric(role)
        sources: list[PromptSource] = [rubric]
        return build_system_prompt(sources)


def _default_role_rubric(role: AgentRole) -> str:
    """Fallback role rubric when no loader is injected.

    Production wiring overrides this with a loader that reads
    ``plugins/egg-sdlc/skills/egg-sdlc/agents/<role>.md`` (the
    canonical per-role markdown). The fallback exists so unit tests
    can construct a ``ClaudeCodeSpawner`` without instantiating the
    full plugin layout.
    """
    role_name = role.value if hasattr(role, "value") else str(role)
    return f"You are the egg `{role_name}` subagent. Run the assigned task."


def _capture_head_sha(worktree: Path) -> str | None:
    """Return the worktree HEAD commit SHA, or ``None`` if unavailable.

    Required by INV-6: reviewers attach commit-bound ACKs to the
    producer's recorded SHA, so the spawner must capture it
    immediately after the subagent completes (before any cleanup
    that might modify HEAD).
    """
    if worktree is None:
        return None
    path = Path(worktree)
    if not path.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={
                **os.environ,
                # Disable interactive credential helpers so the
                # capture stays non-blocking.
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except (subprocess.SubprocessError, OSError):  # fmt: skip
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    if not sha or len(sha) < 7:
        return None
    return sha
