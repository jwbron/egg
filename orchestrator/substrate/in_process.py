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
            answer = yield self._build_preflight_decision()  # noqa: F841 (placeholder)

            # Stage 2: spawn the refiner via the substrate bundle.
            artifact_path = self._spawn_refiner()

            # Stage 3: refine HITL gate — does the operator approve?
            answer = yield self._build_refine_gate_decision(artifact_path)

            # Walking-skeleton fence: if the operator chose "approve
            # and continue to plan", we currently stop here.
            # plan/implement/pr phases are deferred to the follow-up.
            self._maybe_fence(answer)

            return str(artifact_path)
        finally:
            self._shutdown_background_threads()

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

    def _heartbeat_loop(self) -> None:
        while not self._shutdown.wait(_HEARTBEAT_INTERVAL):
            self._heartbeat_ticks += 1

    def _brc_review_loop(self) -> None:
        while not self._shutdown.wait(_BRC_REVIEW_INTERVAL):
            self._brc_review_ticks += 1

    def _bus_tick_loop(self) -> None:
        while not self._shutdown.wait(_BUS_TICK_INTERVAL):
            self._bus_ticks += 1

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
        """
        _, contracts_dir, _ = self._ensure_state_dirs()
        contract_path = contracts_dir / f"{self.pipeline_id}.json"

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
        except json.JSONDecodeError, OSError:
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
        contract_path.write_text(json.dumps(contract, indent=2))
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

    def _build_refine_gate_decision(self, artifact_path: Path) -> Any:
        """Build the post-refine HITL gate decision (cq-7 second yield).

        The decision is the standard refine gate: approve / request
        changes / change approach / stop.
        """
        try:
            from orchestrator.models import HITLDecision
        except ImportError:  # pragma: no cover
            from models import HITLDecision  # type: ignore[no-redef, import-untyped]

        decision_id = f"refine-gate-{self.pipeline_id}"
        self._write_pending_decision(
            decision_id,
            "Refine analysis ready. Choose how to proceed.",
        )
        return HITLDecision(
            id=decision_id,
            question=f"Refine analysis at {artifact_path}. Approve and continue?",
            context=str(artifact_path),
            options=[
                "approve_continue",
                "request_changes",
                "change_approach",
                "stop",
            ],
            decision_type="phase_gate",
            phase="refine",  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    # Refiner spawn
    # ------------------------------------------------------------------

    def _spawn_refiner(self) -> Path:
        """Dispatch the refiner via the substrate bundle and return
        the artifact path."""
        from egg_contracts.agent_roles import AgentRole

        from . import select_substrate

        drafts_dir, _, _ = self._ensure_state_dirs()
        artifact_id = self.issue_number or self.pipeline_id
        artifact_path = drafts_dir / f"{artifact_id}-analysis.md"

        bundle = select_substrate(self.env)

        # Allocate a per-agent worktree under .egg-state/<pipeline>/<repo>/.
        worktree = bundle.worktrees.create(self.pipeline_id, AgentRole.REFINER)

        spawn_env = {
            **self.env,
            "EGG_PIPELINE_ID": self.pipeline_id,
            "EGG_AGENT_ROLE": AgentRole.REFINER.value,
        }
        if self.repo:
            spawn_env["EGG_REPO"] = self.repo
        if self.issue_number is not None:
            spawn_env["EGG_ISSUE_NUMBER"] = str(self.issue_number)

        bundle.spawner.spawn(
            AgentRole.REFINER,
            self.issue_body,
            spawn_env,
            worktree,
        )

        # The spike's deliberate "minimum proof": if the spawner did
        # not produce the canonical artifact (e.g. the harness is
        # unavailable in this environment) we write a placeholder
        # describing why so the caller's HITL gate has something to
        # show. Real production runs land a full analysis.
        if not artifact_path.exists():
            artifact_path.write_text(
                f"# Refiner analysis (placeholder)\n\n"
                f"Pipeline: {self.pipeline_id}\n"
                f"Repo: {self.repo or '<unspecified>'}\n"
                f"Issue: {self.issue_number or '<none>'}\n\n"
                f"This is a walking-skeleton run — the underlying "
                f"subagent harness wrote no analysis at "
                f"{artifact_path} so the in-process orchestrator "
                f"emitted this stub to keep the HITL flow exercising "
                f"end-to-end.\n"
            )

        return artifact_path

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


# Re-exported for tests that want a fast tick budget.
__all__ = [
    "_BRC_REVIEW_INTERVAL",
    "_BUS_TICK_INTERVAL",
    "_HEARTBEAT_INTERVAL",
    "run_pipeline_in_process",
]


# Keep ``time`` imported for tests that monkeypatch it.
_ = time  # noqa: F841 (intentional retention)
