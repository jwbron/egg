"""Orchestrator-side corrective-action executor (#2270 slice-6, §4 — Authority).

The overseer **advises**; the control plane **executes**. A detection-plane
``Finding`` that ``requires_adjudication`` spawns an on-demand overseer agent
which returns an :class:`~overseer.decision_maker.AdjudicationVerdict`
recommending one of a CLOSED corrective vocabulary (plus the non-executable
``none``). This module is the *authority plane*: the only thing that acts on
that recommendation, running under the orchestrator identity (never an agent).

Design invariants (pinned by ``orchestrator/tests/test_corrective_executor.py``):

* **Closed vocabulary** — :data:`CORRECTIVE_ACTIONS` is exactly
  ``{nudge_agent, respawn_cohort, open_operator_hitl}``. ``none`` is the
  adjudicator's "false alarm / no action" and is deliberately NOT executable.
* **Dependency-injected** — the three side effects are passed in, so the
  authority logic is unit-testable with spies and the real wiring lives in
  ``routes/pipelines``.
* **Bounded** — a sliding-window rate limit caps actions per window.
* **Idempotent** — a repeated ``idempotency_key`` runs the side effect
  at-most-once (``deduplicated``).
* **Barred during zero-agent HITL parks** — with no agents running there is
  nothing to correct, so every action is ``barred`` (the §3 invariant).
* **Audited** — every attempt (executed / denied / barred / deduplicated /
  rate_limited) is recorded to the ``audit_sink``.

Decision precedence inside :meth:`CorrectiveExecutor.execute` (each gate is
checked in order; the first that trips wins):

  1. action ∉ vocabulary        → ``denied``
  2. zero-agent HITL park        → ``barred``
  3. duplicate idempotency key   → ``deduplicated``
  4. rate-limit window exceeded  → ``rate_limited``
  5. otherwise                   → ``executed``
"""

from __future__ import annotations

import logging
import time as _time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The CLOSED corrective vocabulary — exactly three executable actions. Mirrors
# ``overseer.decision_maker.ADJUDICATION_ACTIONS`` minus the non-executable
# ``none``.
CORRECTIVE_ACTIONS: frozenset[str] = frozenset(
    {"nudge_agent", "respawn_cohort", "open_operator_hitl"}
)

# Outcome statuses. ``executed`` is the only success; the rest are refusals.
STATUS_EXECUTED = "executed"
STATUS_DENIED = "denied"
STATUS_BARRED = "barred"
STATUS_DEDUPLICATED = "deduplicated"
STATUS_RATE_LIMITED = "rate_limited"

DEFAULT_MAX_ACTIONS_PER_WINDOW = 100
DEFAULT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class CorrectiveOutcome:
    """Immutable result of one :meth:`CorrectiveExecutor.execute` call."""

    action: str
    status: str
    executed: bool
    pipeline_id: str = ""
    target_role: str | None = None
    idempotency_key: str | None = None
    detail: str = ""
    # The injected side effect's return value (decision_id for the HITL writer,
    # bool for nudge/respawn). ``None`` for any refused attempt.
    result: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "executed": self.executed,
            "pipeline_id": self.pipeline_id,
            "target_role": self.target_role,
            "idempotency_key": self.idempotency_key,
            "detail": self.detail,
        }


# A side-effect seam: invoked with keyword arguments (at least ``pipeline_id``)
# and returns a decision id (HITL) or a truthiness flag (nudge / respawn).
CorrectiveHandler = Callable[..., Any]
AuditSink = Callable[[dict], None]


class CorrectiveExecutor:
    """Bounded, audited, idempotent executor for the closed corrective vocabulary.

    The three handlers are injected so the authority logic is testable in
    isolation. :attr:`ACTIONS` advertises exactly the three vocabulary members.
    """

    ACTIONS: frozenset[str] = CORRECTIVE_ACTIONS

    def __init__(
        self,
        *,
        open_operator_hitl: CorrectiveHandler,
        nudge_agent: CorrectiveHandler,
        respawn_cohort: CorrectiveHandler,
        audit_sink: AuditSink | None = None,
        max_actions_per_window: int = DEFAULT_MAX_ACTIONS_PER_WINDOW,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._handlers: dict[str, CorrectiveHandler] = {
            "open_operator_hitl": open_operator_hitl,
            "nudge_agent": nudge_agent,
            "respawn_cohort": respawn_cohort,
        }
        self._audit_sink = audit_sink
        self._max_actions = max(1, int(max_actions_per_window))
        self._window = max(0.0, float(window_seconds))
        self._clock = clock or _time.monotonic
        # Sliding window of executed-action timestamps (global rate limit).
        self._recent: deque[float] = deque()
        # Idempotency keys already executed (at-most-once).
        self._seen_keys: set[str] = set()

    @property
    def actions(self) -> frozenset[str]:
        """The exact set of executable actions (exactly three)."""
        return frozenset(self._handlers)

    # -- internals ---------------------------------------------------------

    def _emit_audit(self, outcome: CorrectiveOutcome) -> None:
        record = outcome.to_dict()
        log = logger.info if outcome.status == STATUS_EXECUTED else logger.warning
        log("corrective_action", extra=record)
        if self._audit_sink is not None:
            try:
                self._audit_sink(record)
            except Exception:  # noqa: BLE001 - audit must never break execution
                logger.debug("corrective audit_sink raised", exc_info=True)

    def _finish(
        self,
        action: str,
        status: str,
        *,
        pipeline_id: str,
        target_role: str | None,
        idempotency_key: str | None,
        executed: bool = False,
        detail: str = "",
        result: Any = None,
    ) -> CorrectiveOutcome:
        outcome = CorrectiveOutcome(
            action=action,
            status=status,
            executed=executed,
            pipeline_id=pipeline_id,
            target_role=target_role,
            idempotency_key=idempotency_key,
            detail=detail,
            result=result,
        )
        self._emit_audit(outcome)
        return outcome

    def _prune(self, now: float) -> None:
        if not self._window:
            return
        while self._recent and (now - self._recent[0]) >= self._window:
            self._recent.popleft()

    def _forward_kwargs(
        self,
        action: str,
        *,
        pipeline_id: str,
        phase: str | None,
        target_role: str | None,
        finding: Any,
        question: str | None,
        options: Any,
    ) -> dict[str, Any]:
        """Curate the keyword arguments forwarded to each injected handler."""
        base: dict[str, Any] = {"pipeline_id": pipeline_id, "phase": phase, "finding": finding}
        if action == "open_operator_hitl":
            base["question"] = question
            base["options"] = options
        else:  # nudge_agent / respawn_cohort
            base["target_role"] = target_role
        return base

    # -- public API --------------------------------------------------------

    def execute(
        self,
        action: str,
        *,
        pipeline_id: str,
        running_agent_count: int = 1,
        phase: str | None = None,
        target_role: str | None = None,
        finding: Any = None,
        idempotency_key: str | None = None,
        question: str | None = None,
        options: Any = None,
    ) -> CorrectiveOutcome:
        """Execute (or refuse) one corrective action under the slice-6 guarantees."""
        now = self._clock()

        # 1. Closed vocabulary — anything outside the three actions is denied.
        if action not in self._handlers:
            return self._finish(
                action,
                STATUS_DENIED,
                pipeline_id=pipeline_id,
                target_role=target_role,
                idempotency_key=idempotency_key,
                detail=f"action {action!r} is outside the closed vocabulary {sorted(self.actions)}",
            )

        # 2. Zero-agent HITL park bar — nothing running, nothing to correct.
        if running_agent_count <= 0:
            return self._finish(
                action,
                STATUS_BARRED,
                pipeline_id=pipeline_id,
                target_role=target_role,
                idempotency_key=idempotency_key,
                detail="barred: zero agents running (HITL park)",
            )

        # 3. Idempotency — a repeated key runs the side effect at most once.
        if idempotency_key is not None and idempotency_key in self._seen_keys:
            return self._finish(
                action,
                STATUS_DEDUPLICATED,
                pipeline_id=pipeline_id,
                target_role=target_role,
                idempotency_key=idempotency_key,
                detail=f"idempotent no-op: key {idempotency_key!r} already executed",
            )

        # 4. Rate limit — bound executed actions per sliding window.
        self._prune(now)
        if len(self._recent) >= self._max_actions:
            return self._finish(
                action,
                STATUS_RATE_LIMITED,
                pipeline_id=pipeline_id,
                target_role=target_role,
                idempotency_key=idempotency_key,
                detail=(
                    f"rate-limited: {len(self._recent)} action(s) within "
                    f"{self._window}s (max {self._max_actions})"
                ),
            )

        # 5. Execute the injected side effect.
        handler = self._handlers[action]
        kwargs = self._forward_kwargs(
            action,
            pipeline_id=pipeline_id,
            phase=phase,
            target_role=target_role,
            finding=finding,
            question=question,
            options=options,
        )
        result = handler(**kwargs)

        # Record for rate-limit + idempotency only on a real execution.
        self._recent.append(now)
        if idempotency_key is not None:
            self._seen_keys.add(idempotency_key)

        return self._finish(
            action,
            STATUS_EXECUTED,
            pipeline_id=pipeline_id,
            target_role=target_role,
            idempotency_key=idempotency_key,
            executed=True,
            detail="executed",
            result=result,
        )


__all__ = [
    "CORRECTIVE_ACTIONS",
    "CorrectiveExecutor",
    "CorrectiveHandler",
    "CorrectiveOutcome",
]
