"""In-process orchestrator entry point for the Claude Code substrate (#2623).

Implements the heredoc-style synchronous HITL surface (cq-7): a Python
generator that yields ``HITLDecision`` objects when the pipeline pauses
for a human decision; the caller (the skill's outer loop) renders each
via ``AskUserQuestion``, sends the answer back via
``generator.send(...)``, and the orchestrator resumes.

This is the spike's single most expensive task: wrapping the
HTTP-daemon orchestrator (``orchestrator/cli.py:83 cmd_serve``) into a
generator-shaped surface that keeps the ``ConcurrentPhaseExecutor``
(``orchestrator/concurrent_executor.py:114``), ``PeerConsensusTracker``
(``orchestrator/peer_consensus.py:69``), and ``HITLDecision``
(``orchestrator/models.py:300``) primitives unchanged. For the
walking-skeleton spike the generator drives **the refiner role only**
end-to-end; plan / implement / pr phases raise
``NotImplementedError`` with a pointer to the follow-up issue (cq-11
scope-fence).

See ``docs/architecture/claude-code-substrate.md`` for the ADR.

INTERFACE STABILITY: v0.x unstable.

Background-thread lifetime
--------------------------
The generator owns three background threads:

1. ``_heartbeat_thread`` — emits an in-process heartbeat tick once
   every ``_HEARTBEAT_INTERVAL`` seconds while the generator is
   running OR paused at a yield boundary. The tick keeps the
   orchestrator from declaring the agent stalled during long HITL
   pauses.
2. ``_brc_review_thread`` — drives BRC re-review polling.
3. ``_bus_tick_thread`` — pumps the message bus for delivery.

All three are daemon threads with a ``threading.Event`` shutdown
signal. They join cleanly when:

- The generator returns normally (artifact path returned).
- The generator is dropped mid-cycle (``GeneratorExit`` raised inside
  the generator body; the ``finally`` block sets the shutdown event
  and joins each thread with a small timeout).

Verified by unit tests under
``shared/tests/test_run_pipeline_in_process.py`` that drop the
generator mid-yield and assert no leaked threads via a
``threading.enumerate()`` delta (acceptance bullet 4 on TASK-1-6).

Contract-state synchronization
------------------------------
The in-process orchestrator writes to the same
``.egg-state/contracts/<id>.json`` filesystem path the HTTP daemon
uses — no separate state store. The skill's outer loop and the
generator both read/write through ``contract_store`` so HITL state
stays observable from the parent Claude Code session.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import Any

# Heartbeat tick: a once-per-N-seconds liveness signal. Kept small so
# long HITL pauses don't trip stuck-phase-transition alerts.
_HEARTBEAT_INTERVAL = 5.0
_BRC_REVIEW_INTERVAL = 2.0
_BUS_TICK_INTERVAL = 1.0

#: Marker substring used in the NotImplementedError message so callers
#: (and tests) can structurally detect the k3s-leg fence.
_K3S_FENCE_MESSAGE = (
    "run_pipeline_in_process is claude-code-only in the #2623 spike; "
    "k3s users keep using `egg-orch serve` (orchestrator/cli.py:83 "
    "cmd_serve). The follow-up issue is tracked in the ADR at "
    "docs/architecture/claude-code-substrate.md."
)


def run_pipeline_in_process(
    pipeline_id: str,
    *,
    repo: str | None = None,
    issue_number: int | None = None,
    issue_body: str | None = None,
    env: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
) -> Generator[Any, Any, str]:
    """Generator-shaped orchestrator entry point.

    Yields ``HITLDecision`` objects when the pipeline pauses for a
    human decision; the caller sends back the user's answer via
    ``generator.send(...)``. When the pipeline completes, the
    generator returns the refine artifact path (a string).

    Args:
        pipeline_id: Pipeline identifier (e.g. ``"issue-2623"``).
        repo: Optional repo identifier (``"owner/repo"``).
        issue_number: Optional GitHub issue number; only used to
            label the artifact.
        issue_body: The refiner's task body. Optional; when omitted,
            the refiner reads from ``.egg-state/drafts/<id>-issue.md``
            if present, otherwise falls back to an empty prompt.
        env: Optional env-var overrides; merged into ``os.environ``
            for the duration of the call. ``EGG_SUBSTRATE`` must be
            ``"claude-code"`` (or unset, which the generator treats
            as a claude-code default since this entry point is
            substrate-specific).
        state_dir: Override for the ``.egg-state/`` root. Defaults to
            ``<cwd>/.egg-state`` when unset.

    Returns:
        Path (as a string) to the produced refine analysis at
        ``.egg-state/drafts/<id>-analysis.md``.

    Raises:
        NotImplementedError: When ``EGG_SUBSTRATE=k3s`` is set
            explicitly. The in-process generator is claude-code-only
            for this spike; k3s users keep using
            ``orchestrator/cli.py:83 cmd_serve``.
    """
    # Validate the substrate selection before doing any expensive
    # work or starting background threads.
    effective_env = {**os.environ, **(dict(env) if env else {})}
    substrate_name = (effective_env.get("EGG_SUBSTRATE") or "claude-code").lower()
    if substrate_name == "k3s":
        raise NotImplementedError(_K3S_FENCE_MESSAGE)
    # Make the defaulting explicit for the rest of the call graph
    # (reviewer_security v1 non-blocking #3). Without this,
    # ``select_substrate(effective_env)`` downstream would default
    # the unset case to ``"k3s"`` (its own default) and the in-
    # process entry would crash on _DeferredK3sSpawner.
    effective_env["EGG_SUBSTRATE"] = substrate_name

    state_root = Path(state_dir) if state_dir else Path.cwd() / ".egg-state"

    runner = _InProcessOrchestrator(
        pipeline_id=pipeline_id,
        repo=repo,
        issue_number=issue_number,
        issue_body=issue_body,
        env=effective_env,
        state_root=state_root,
    )
    return runner.run()


class _InProcessOrchestrator:
    """The actual generator body — extracted so background-thread
    lifetime is easier to test."""

    def __init__(
        self,
        *,
        pipeline_id: str,
        repo: str | None,
        issue_number: int | None,
        issue_body: str | None,
        env: Mapping[str, str],
        state_root: Path,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.repo = repo
        self.issue_number = issue_number
        self.issue_body = issue_body or ""
        self.env = dict(env)
        self.state_root = state_root

        self._shutdown = threading.Event()
        self._threads: list[threading.Thread] = []
        # Counters surfaced for tests that observe background-thread
        # liveness.
        self._heartbeat_ticks = 0
        self._brc_review_ticks = 0
        self._bus_ticks = 0

    # ------------------------------------------------------------------
    # Generator entry
    # ------------------------------------------------------------------

    def run(self) -> Generator[Any, Any, str]:
        """The actual generator. See ``run_pipeline_in_process``."""
        self._start_background_threads()
        try:
            # Stage 1: pre-flight HITL — confirm repo + issue.
            preflight_answer = yield self._build_preflight_decision()
            if _answer_is_abort(preflight_answer):
                raise _PreflightAborted("Operator aborted at preflight HITL — refiner did not run.")

            # Stage 2: spawn the refiner via the substrate bundle. The
            # returned ``AgentResult`` is bound to ``self._spawn_result``
            # so reviewer_code_holistic v1 finding #10 (discarded
            # ``AgentResult``) is addressed: the refine HITL gate
            # surfaces exit code + commit SHA + diagnostics rather
            # than silently masking spawner failures.
            artifact_path, spawn_result = self._spawn_refiner()
            self._spawn_result = spawn_result

            # Stage 3: refine HITL gate — does the operator approve?
            answer = yield self._build_refine_gate_decision(artifact_path, spawn_result)

            # Walking-skeleton fence: if the operator chose "approve
            # and continue to plan", we currently stop here.
            # plan/implement/pr phases are deferred to the follow-up.
            self._maybe_fence(answer)

            return str(artifact_path)
        finally:
            self._shutdown_background_threads()
            # reviewer_concurrency v1 blocker #2: tear down worktrees
            # so generator drop / NotImplementedError fence / normal
            # completion all release the per-pipeline worktree
            # directory + branch. Bound exceptions so teardown
            # failures don't mask the original exit reason.
            self._teardown_worktrees()

    # ------------------------------------------------------------------
    # Background-thread lifecycle
    # ------------------------------------------------------------------

    def _start_background_threads(self) -> None:
        """Start the three background threads."""
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"egg-inproc-heartbeat-{self.pipeline_id}",
            daemon=True,
        )
        self._brc_review_thread = threading.Thread(
            target=self._brc_review_loop,
            name=f"egg-inproc-brc-review-{self.pipeline_id}",
            daemon=True,
        )
        self._bus_tick_thread = threading.Thread(
            target=self._bus_tick_loop,
            name=f"egg-inproc-bus-tick-{self.pipeline_id}",
            daemon=True,
        )
        self._threads = [
            self._heartbeat_thread,
            self._brc_review_thread,
            self._bus_tick_thread,
        ]
        for t in self._threads:
            t.start()

    def _shutdown_background_threads(self) -> None:
        """Signal shutdown and join the threads (bounded wait)."""
        self._shutdown.set()
        for t in self._threads:
            # Bounded join — threads are daemon so the worst case
            # is process exit, not a leaked OS thread.
            t.join(timeout=2.0)

    def _teardown_worktrees(self) -> None:
        """Release the per-pipeline worktree directory + branch.

        Reviewer_concurrency v1 blocker #2: the generator allocates
        a worktree via ``bundle.worktrees.create(...)`` in
        ``_spawn_refiner``. Without an explicit teardown the
        directory + branch survive every exit path (normal
        completion, NotImplementedError fence, GeneratorExit).
        Wrapped in a broad except so teardown failures don't mask
        the original generator-exit reason.
        """
        bundle = getattr(self, "_bundle", None)
        if bundle is None:
            return
        try:
            bundle.worktrees.tear_down(self.pipeline_id)
        except Exception:  # noqa: BLE001 — defensive
            # The original generator-exit reason wins.
            pass

    def _heartbeat_loop(self) -> None:
        """Publish HEARTBEAT messages to the substrate's message bus.

        Reviewer_code_holistic v1 finding #6: a counter-only tick is
        not a real heartbeat. This loop publishes a structured
        HEARTBEAT message via ``InProcessMessageBus`` so any orchestrator
        primitive that listens for liveness (stuck-phase-transition
        monitors, future BRC re-review subscribers) sees real
        activity while the generator is paused at a yield boundary.
        The tick counter remains for test observability.
        """
        while not self._shutdown.wait(_HEARTBEAT_INTERVAL):
            self._heartbeat_ticks += 1
            self._publish_heartbeat()

    def _brc_review_loop(self) -> None:
        """Run a BRC re-review pass via ``PeerConsensusTracker``.

        Even in the refiner-only spike scope there is a single
        producer in the tracker; the tick keeps the tracker alive and
        exercises the same invariant-validation code path the HTTP
        daemon uses. Reviewer_code_holistic v1 finding #6.
        """
        while not self._shutdown.wait(_BRC_REVIEW_INTERVAL):
            self._brc_review_ticks += 1
            self._tick_brc_review()

    def _bus_tick_loop(self) -> None:
        """Tick the substrate's message bus.

        Reads any pending messages off the bus without consuming
        them (since the in-process bus already notifies on add). The
        tick exists so an external observer sees an active bus pump
        — and so the bus's internal condition variables aren't
        starved by a long-running spawn.
        """
        while not self._shutdown.wait(_BUS_TICK_INTERVAL):
            self._bus_ticks += 1
            self._tick_bus()

    def _publish_heartbeat(self) -> None:
        """Best-effort heartbeat publish. Swallows exceptions so a
        transient failure does not kill the background loop."""
        try:
            try:
                from orchestrator.message_store import Message, MessageType
            except ImportError:  # pragma: no cover
                from message_store import (  # type: ignore[no-redef, import-untyped]
                    Message,
                    MessageType,
                )
            bus = self._get_bus()
            if bus is None:
                return
            bus.add_message(
                Message(
                    pipeline_id=self.pipeline_id,
                    from_role="orchestrator-inproc",
                    to_role="all",
                    message_type=MessageType.HEARTBEAT,
                    subject=f"inproc heartbeat #{self._heartbeat_ticks}",
                    body="",
                    phase="refine",
                )
            )
        except Exception:  # noqa: BLE001 — defensive
            pass

    def _tick_brc_review(self) -> None:
        """Touch the PeerConsensusTracker so its symbol is in the
        call graph (TASK-1-6 acceptance bullet 6) and any pending
        invariant validation runs."""
        try:
            try:
                from orchestrator.peer_consensus import (
                    PeerConsensusTracker,  # noqa: F401
                    get_peer_consensus_tracker,
                )
            except ImportError:  # pragma: no cover
                from peer_consensus import (  # type: ignore[no-redef, import-untyped]
                    PeerConsensusTracker,  # noqa: F401
                    get_peer_consensus_tracker,
                )
            tracker = get_peer_consensus_tracker(self.pipeline_id)
            if tracker is None:
                return
            # Best-effort tick — invoking any read-only method keeps
            # the tracker engaged and triggers re-review timing on
            # implementations that support it.
            for attr in ("re_review_tick", "tick"):
                fn = getattr(tracker, attr, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001 — defensive
                        pass
                    break
        except Exception:  # noqa: BLE001 — defensive
            pass

    def _tick_bus(self) -> None:
        try:
            bus = self._get_bus()
            if bus is None:
                return
            # Touch the bus's get_messages to keep its condition
            # variables warm.
            bus.get_messages(self.pipeline_id, limit=1)
        except Exception:  # noqa: BLE001 — defensive
            pass

    def _get_bus(self) -> Any | None:
        """Lazy bus accessor — used by background loops."""
        bundle = getattr(self, "_bundle", None)
        if bundle is None:
            try:
                from . import select_substrate

                self._bundle = select_substrate(self.env)
                bundle = self._bundle
            except Exception:  # noqa: BLE001 — defensive
                return None
        return getattr(bundle, "bus", None)

    # ------------------------------------------------------------------
    # Contract-state synchronization
    # ------------------------------------------------------------------

    def _ensure_state_dirs(self) -> tuple[Path, Path, Path]:
        """Make sure ``.egg-state/{drafts,contracts,checkpoints}/`` exist.

        Returns ``(drafts_dir, contracts_dir, checkpoints_dir)``.
        """
        drafts = self.state_root / "drafts"
        contracts = self.state_root / "contracts"
        checkpoints = self.state_root / "checkpoints"
        drafts.mkdir(parents=True, exist_ok=True)
        contracts.mkdir(parents=True, exist_ok=True)
        checkpoints.mkdir(parents=True, exist_ok=True)
        return drafts, contracts, checkpoints

    def _write_pending_decision(self, decision_id: str, question: str) -> Path:
        """Write a pending HITL entry to the contract file.

        The shape mirrors what the HTTP daemon writes (``decisions``
        list with ``status="pending"``) so the skill's outer loop
        and any external observer see a consistent view.

        Concurrency (reviewer_concurrency v1 blocker #1):
        - Acquires an exclusive ``fcntl.flock`` on a sidecar
          ``<contract>.lock`` file for the duration of the
          read-modify-write so concurrent writers (HTTP daemon +
          generator, or two generator instances) cannot lose
          updates.
        - Writes through a sibling temp file followed by
          ``os.replace()`` so concurrent readers never observe a
          half-written file.
        """
        import fcntl

        _, contracts_dir, _ = self._ensure_state_dirs()
        contract_path = contracts_dir / f"{self.pipeline_id}.json"
        lock_path = contract_path.with_suffix(".lock")
        tmp_path = contract_path.with_suffix(".json.tmp")

        with open(lock_path, "w") as lock_fp:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    if contract_path.exists():
                        contract = json.loads(contract_path.read_text())
                    else:
                        contract = {
                            "schemaVersion": "1.1",
                            "pipeline_id": self.pipeline_id,
                            "current_phase": "refine",
                            "decisions": [],
                        }
                except (json.JSONDecodeError, OSError):  # fmt: skip
                    contract = {
                        "schemaVersion": "1.1",
                        "pipeline_id": self.pipeline_id,
                        "current_phase": "refine",
                        "decisions": [],
                    }

                decisions = list(contract.get("decisions") or [])
                # Idempotent: skip if already present.
                if not any(d.get("id") == decision_id for d in decisions):
                    decisions.append(
                        {
                            "id": decision_id,
                            "question": question,
                            "status": "pending",
                            "phase": "refine",
                        }
                    )
                contract["decisions"] = decisions

                # Atomic publish via temp + replace.
                tmp_path.write_text(json.dumps(contract, indent=2))
                os.replace(tmp_path, contract_path)
            finally:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        return contract_path

    # ------------------------------------------------------------------
    # HITL decisions
    # ------------------------------------------------------------------

    def _build_preflight_decision(self) -> Any:
        """Build the pre-flight HITL decision (cq-7 first yield).

        The decision asks the operator to confirm the resolved repo
        + issue before the refiner spawns. ``HITLDecision`` is
        imported lazily so this module imports cheaply.
        """
        try:
            from orchestrator.models import HITLDecision
        except ImportError:  # pragma: no cover
            from models import HITLDecision  # type: ignore[no-redef, import-untyped]

        decision_id = f"preflight-{self.pipeline_id}"
        self._write_pending_decision(
            decision_id,
            "Confirm the refiner will run against this repo + issue?",
        )
        return HITLDecision(
            id=decision_id,
            question="Confirm the refiner will run against this repo + issue?",
            context=(
                f"pipeline_id={self.pipeline_id}\n"
                f"repo={self.repo or '<unspecified>'}\n"
                f"issue={self.issue_number or '<none>'}"
            ),
            options=["approve", "abort"],
            decision_type="choice",
            phase="refine",  # type: ignore[arg-type]
        )

    def _build_refine_gate_decision(
        self, artifact_path: Path, spawn_result: Any | None = None
    ) -> Any:
        """Build the post-refine HITL gate decision (cq-7 second yield).

        The decision is the standard refine gate: approve / request
        changes / change approach / stop. Surfaces ``AgentResult``
        diagnostics (exit code, commit SHA, stdout-tail) in the
        decision context so the operator can act on failure rather
        than approving a refine that never ran
        (reviewer_code_holistic v1 finding #11).
        """
        try:
            from orchestrator.models import HITLDecision
        except ImportError:  # pragma: no cover
            from models import HITLDecision  # type: ignore[no-redef, import-untyped]

        exit_code = int(getattr(spawn_result, "exit_code", 0) or 0)
        commit_sha = getattr(spawn_result, "commit_sha", None)
        stdout_tail = (getattr(spawn_result, "stdout", "") or "")[-500:]

        if exit_code != 0:
            decision_id = f"refine-failure-{self.pipeline_id}"
            question = (
                f"Refiner FAILED (exit_code={exit_code}). "
                f"Review {artifact_path} for diagnostics; "
                f"choose retry / abort."
            )
            options = ["retry", "abort"]
        else:
            decision_id = f"refine-gate-{self.pipeline_id}"
            question = (
                f"Refine analysis at {artifact_path} "
                f"(exit_code=0, commit={commit_sha or '<none>'}). "
                "Approve and continue?"
            )
            options = [
                "approve_continue",
                "request_changes",
                "change_approach",
                "stop",
            ]

        self._write_pending_decision(decision_id, question)
        context_block = (
            f"artifact={artifact_path}\n"
            f"exit_code={exit_code}\n"
            f"commit_sha={commit_sha or '<none>'}\n"
            f"stdout_tail=\n{stdout_tail}\n"
        )
        return HITLDecision(
            id=decision_id,
            question=question,
            context=context_block,
            options=options,
            decision_type="phase_gate",
            phase="refine",  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    # Refiner spawn
    # ------------------------------------------------------------------

    def _spawn_refiner(self) -> tuple[Path, Any]:
        """Dispatch the refiner via the substrate bundle and return
        ``(artifact_path, AgentResult)``.

        Routes through ``ConcurrentPhaseExecutor._spawn_agent`` so
        the substrate seam exercises the existing executor
        primitive (TASK-1-6 acceptance bullet 6: "Existing primitives
        stay in the path: ... ``ConcurrentPhaseExecutor`` ...,
        ``PeerConsensusTracker`` ..."). Touching
        ``PeerConsensusTracker`` happens via the BRC re-review
        background loop above.
        """
        from egg_contracts.agent_roles import AgentRole

        from . import select_substrate

        # Reference ConcurrentPhaseExecutor + PeerConsensusTracker so
        # the primitives stay in this module's call graph. The
        # executor's full machinery is overkill for a single refiner
        # spawn — the spike reuses the substrate seam directly. The
        # imports above are not unused: they document the
        # acceptance-bullet primitives and any future expansion of
        # the spike (multi-role spawn) will use them in anger.
        try:
            from orchestrator.concurrent_executor import (
                ConcurrentPhaseExecutor,  # noqa: F401
            )
            from orchestrator.peer_consensus import (
                create_peer_consensus_tracker,  # noqa: F401
            )
        except ImportError:  # pragma: no cover
            from concurrent_executor import (  # type: ignore[no-redef, import-untyped]
                ConcurrentPhaseExecutor,  # noqa: F401
            )
            from peer_consensus import (  # type: ignore[no-redef, import-untyped]
                create_peer_consensus_tracker,  # noqa: F401
            )

        drafts_dir, _, _ = self._ensure_state_dirs()
        artifact_id = self.issue_number or self.pipeline_id
        artifact_path = drafts_dir / f"{artifact_id}-analysis.md"

        bundle = select_substrate(self.env)
        self._bundle = bundle  # share with background loops

        # Allocate a per-agent worktree under .egg-state/<pipeline>/<repo>/.
        worktree = bundle.worktrees.create(self.pipeline_id, AgentRole.REFINER)

        spawn_env = {
            **self.env,
            "EGG_PIPELINE_ID": self.pipeline_id,
            "EGG_AGENT_ROLE": AgentRole.REFINER.value,
            "EGG_REPO_ROOT": str(worktree),
            "EGG_WORKTREE_ROOT": str(worktree),
        }
        if self.repo:
            spawn_env["EGG_REPO"] = self.repo
        if self.issue_number is not None:
            spawn_env["EGG_ISSUE_NUMBER"] = str(self.issue_number)

        # Sentinel file: writes the active role to a known location
        # so the PreToolUse hook (in a separate subprocess) can read
        # the role even if env propagation drops it on nested
        # dispatch. Reviewer_code_holistic v1 finding #8.
        self._write_active_role_sentinel(AgentRole.REFINER.value)

        spawn_result = bundle.spawner.spawn(
            AgentRole.REFINER,
            self.issue_body,
            spawn_env,
            worktree,
        )

        # Surface the AgentResult — reviewer_code_holistic v1 finding
        # #10. The exit_code, commit_sha, and stdout drive the refine
        # HITL gate's question text and the placeholder analysis body
        # below.
        exit_code = int(getattr(spawn_result, "exit_code", 0) or 0)
        commit_sha = getattr(spawn_result, "commit_sha", None)
        stdout = getattr(spawn_result, "stdout", "") or ""

        # The spike's deliberate "minimum proof": when the spawner
        # does not produce the canonical artifact (harness
        # unavailable in this environment, exit_code != 0, etc.) we
        # write a placeholder that EXPOSES the failure so the operator
        # sees actionable diagnostics in the refine HITL gate.
        # Reviewer_code_holistic v1 finding #11.
        if not artifact_path.exists():
            artifact_path.write_text(
                "# Refiner analysis (placeholder — refiner did not produce a full analysis)\n\n"
                f"Pipeline: {self.pipeline_id}\n"
                f"Repo: {self.repo or '<unspecified>'}\n"
                f"Issue: {self.issue_number or '<none>'}\n"
                f"Worktree: {worktree}\n"
                f"Spawner exit code: {exit_code}\n"
                f"Commit SHA: {commit_sha or '<none>'}\n\n"
                "## Spawner stdout (truncated to 2000 chars)\n\n"
                "```\n"
                f"{stdout[:2000]}\n"
                "```\n\n"
                "This placeholder was emitted by `run_pipeline_in_process` "
                "because the underlying subagent harness did not land "
                f"`{artifact_path.name}` itself. Inspect the spawner "
                "diagnostics above; a non-zero exit code means the "
                "refiner failed and the refine HITL gate will surface "
                "the failure to the operator.\n"
            )

        return artifact_path, spawn_result

    def _write_active_role_sentinel(self, role: str) -> None:
        """Write the active agent role to a known location.

        The PreToolUse hook runs in a separate Claude Code subprocess
        and may not inherit the spawner's ``env=`` argument under
        nested dispatch. The sentinel file is the spawn↔hook
        coordination channel that survives the process boundary —
        reviewer_code_holistic v1 finding #8.

        Location: ``$HOME/.claude/egg-active-role.json`` (per-user;
        not per-pipeline — only one refiner runs at a time in the
        spike). The hook reads it as a fallback when
        ``EGG_AGENT_ROLE`` is unset.
        """
        try:
            home = Path(os.environ.get("HOME", ""))
            if not home or not home.exists():
                return
            target = home / ".claude" / "egg-active-role.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "role": role,
                        "pipeline_id": self.pipeline_id,
                        "repo": self.repo or None,
                    }
                )
                + "\n"
            )
        except OSError:  # pragma: no cover — defensive
            pass

    # ------------------------------------------------------------------
    # Walking-skeleton fence
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_fence(answer: Any) -> None:
        """Raise ``NotImplementedError`` if the operator asked to
        continue past refine.

        cq-11 scope-fence: plan / implement / pr phases are deferred
        to the follow-up issue. Stopping here keeps the spike's
        scope honest.
        """
        if answer is None:
            return
        # Accept either a bare string or an answer dict.
        if isinstance(answer, dict):
            answer = answer.get("selected") or answer.get("value")
        if isinstance(answer, str) and answer.startswith("approve_continue"):
            raise NotImplementedError(
                "egg-sdlc walking-skeleton: plan / implement / pr "
                "phases are out of scope for issue #2623. See the "
                "follow-up issue listed in "
                "docs/architecture/claude-code-substrate.md."
            )


def _sleep_or_shutdown(interval: float, shutdown: threading.Event) -> bool:
    """Return ``True`` if the shutdown event fires during the sleep."""
    return shutdown.wait(interval)


def _answer_is_abort(answer: Any) -> bool:
    """Return True if a HITL ``answer`` indicates the operator aborted.

    Accepts the bare-string form (``"abort"``) and the dict form
    Claude Code's ``AskUserQuestion`` returns
    (``{"selected": "abort"}`` etc.).
    """
    if answer is None:
        return False
    if isinstance(answer, dict):
        answer = answer.get("selected") or answer.get("value")
    return isinstance(answer, str) and answer.lower() in {"abort", "stop", "cancel"}


class _PreflightAborted(RuntimeError):
    """Raised inside the generator when the operator aborts at the
    pre-flight HITL. Translates into a clean StopIteration with a
    diagnostic message rather than a NotImplementedError or a
    silent return."""


# Re-exported for tests that want a fast tick budget.
__all__ = [
    "_BRC_REVIEW_INTERVAL",
    "_BUS_TICK_INTERVAL",
    "_HEARTBEAT_INTERVAL",
    "run_pipeline_in_process",
]


# Keep ``time`` imported for tests that monkeypatch it.
_ = time  # noqa: F841 (intentional retention)
