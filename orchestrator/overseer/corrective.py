"""Orchestrator-side corrective-action executor (#2270 slice-6, §4 — Authority).

The overseer **advises**; the control plane **executes**. A detection-plane
``Finding`` that ``requires_adjudication`` spawns an on-demand overseer agent
which returns an :class:`~overseer.decision_maker.AdjudicationVerdict`
recommending one of a CLOSED corrective vocabulary. This module is the
*authority plane*: it consumes that recommendation and, running under the
orchestrator identity (never an agent), executes exactly three bounded actions.

Design invariants (the slice-6 contract):

* **Closed vocabulary** — exactly ``open_operator_hitl`` / ``nudge_agent`` /
  ``respawn_cohort``. No agent — not even the overseer — may invoke these
  directly; the shared RBAC gate
  (:func:`egg_restrictions.corrective.corrective_action_authorized`, the same
  predicate the gateway's ``agent_restrictions`` re-exports) denies every agent
  role. :meth:`CorrectiveExecutor.execute` re-checks authorization before acting,
  so an unauthorized caller is denied at the executor too.
* **Bounded** — each action is rate-limited per ``(action, target)`` within a
  sliding window, so a flapping detector cannot fire a storm of respawns.
* **Idempotent** — an identical ``(action, target, dedupe_key)`` request inside
  the idempotency window is a no-op that returns the prior outcome, so a re-fired
  verdict cannot double-open a HITL decision or double-respawn a cohort.
* **Audited** — every attempt — executed, denied, rate-limited, deduped, barred,
  or failed — is logged with structured fields.
* **Barred during zero-agent HITL parks** — when no agents are running there is
  nothing to correct, so every action is refused (the §3 invariant: corrective
  churn must not fire during a multi-hour zero-agent park).

The three side effects are injected as callables (``open_hitl`` / ``nudge`` /
``respawn``) so the authority logic is unit-testable without a live
orchestrator; ``routes/pipelines`` wires the production seams.
"""

from __future__ import annotations

import logging
import time as _time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class CorrectiveAction(StrEnum):
    """The CLOSED corrective vocabulary. Exactly three members."""

    OPEN_OPERATOR_HITL = "open_operator_hitl"
    NUDGE_AGENT = "nudge_agent"
    RESPAWN_COHORT = "respawn_cohort"


# Mirror of :data:`egg_restrictions.corrective.CORRECTIVE_ACTIONS`, derived from
# the enum so the two can never silently drift.
CORRECTIVE_ACTIONS: frozenset[str] = frozenset(a.value for a in CorrectiveAction)


class CorrectiveStatus(StrEnum):
    """Outcome status for a single :meth:`CorrectiveExecutor.execute` call."""

    EXECUTED = "executed"  # side effect ran successfully
    DENIED = "denied"  # RBAC: caller not authorized
    BARRED = "barred"  # zero-agent HITL park — nothing to correct
    RATE_LIMITED = "rate_limited"  # too many of this (action, target) in window
    DEDUPED = "deduped"  # idempotent no-op (identical request in window)
    FAILED = "failed"  # side effect raised
    UNKNOWN_ACTION = "unknown_action"  # outside the closed vocabulary
    NOOP = "noop"  # verdict recommended "none" / no action


# Statuses that mean "the action did not run" — used by callers that branch on
# whether a real side effect fired.
_NON_EXECUTED_STATUSES: frozenset[str] = frozenset(
    {
        CorrectiveStatus.DENIED,
        CorrectiveStatus.BARRED,
        CorrectiveStatus.RATE_LIMITED,
        CorrectiveStatus.DEDUPED,
        CorrectiveStatus.FAILED,
        CorrectiveStatus.UNKNOWN_ACTION,
        CorrectiveStatus.NOOP,
    }
)

DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 600
DEFAULT_RATE_LIMIT_MAX_PER_WINDOW = 1
DEFAULT_IDEMPOTENCY_WINDOW_SECONDS = 600


@dataclass
class CorrectiveContext:
    """Inputs for one corrective action.

    Attributes:
        pipeline_id: Pipeline the action targets.
        running_agent_count: Number of agents currently running. ``<= 0`` bars
            every action (zero-agent HITL park invariant).
        caller_identity: Identity invoking the executor. Authorized only when it
            is the orchestrator control plane; defaults to ``"orchestrator"``
            because the executor is control-plane code, but is checked so a
            mis-wired agent caller is denied.
        target: The action target — an agent role for ``nudge_agent`` /
            ``respawn_cohort``, or a cohort label. Part of the rate-limit and
            idempotency keys.
        phase: Optional pipeline phase, for the nudge/audit trail.
        finding_class: The detection-plane finding class that produced this
            action (default dedupe key + audit context).
        reason: Human-readable justification (verdict reasoning), surfaced in the
            HITL question / audit log.
        idempotency_key: Explicit idempotency key. When empty, falls back to
            ``finding_class`` → ``target`` → ``pipeline_id`` so a re-fired verdict
            on the same finding dedupes.
        payload: Action-specific extra data (e.g. the escalation dict for the
            nudge, or the finding/verdict objects for the HITL decision builder).
    """

    pipeline_id: str
    running_agent_count: int
    caller_identity: str = "orchestrator"
    target: str = ""
    phase: str | None = None
    finding_class: str = ""
    reason: str = ""
    idempotency_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> str:
        """Resolve the idempotency discriminator (never empty)."""
        return (
            self.idempotency_key
            or self.finding_class
            or self.target
            or self.pipeline_id
            or "default"
        )


@dataclass(frozen=True)
class CorrectiveOutcome:
    """Result of a single :meth:`CorrectiveExecutor.execute` call."""

    action: str
    status: str
    detail: str = ""
    target: str = ""
    idempotency_key: str = ""
    executed: bool = False
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "detail": self.detail,
            "target": self.target,
            "idempotency_key": self.idempotency_key,
            "executed": self.executed,
            "ts": self.ts,
        }


# A side-effect seam: receives the context, performs the action, returns a short
# human-readable detail string (or ``None``). Raising signals failure.
CorrectiveHandler = Callable[[CorrectiveContext], "str | None"]
Authorizer = Callable[[str, str], "tuple[bool, str]"]


def _default_authorize(identity: str, action: str) -> tuple[bool, str]:
    """Default authorizer — the shared RBAC predicate (deny-by-default).

    Imports lazily so the executor module has no import-time dependency on the
    restrictions package; an import failure denies (fail-closed) rather than
    silently allowing an unauthorized action.
    """
    try:
        from egg_restrictions.corrective import corrective_action_authorized
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        return False, f"RBAC predicate unavailable ({exc}); denying by default"
    return corrective_action_authorized(identity, action)


class CorrectiveExecutor:
    """Bounded, audited, idempotent executor for the closed corrective vocabulary.

    The three handlers are injected so the authority logic is testable in
    isolation. :meth:`actions` exposes exactly the three vocabulary members.
    """

    def __init__(
        self,
        *,
        open_hitl: CorrectiveHandler,
        nudge: CorrectiveHandler,
        respawn: CorrectiveHandler,
        authorize: Authorizer | None = None,
        rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        rate_limit_max_per_window: int = DEFAULT_RATE_LIMIT_MAX_PER_WINDOW,
        idempotency_window_seconds: int = DEFAULT_IDEMPOTENCY_WINDOW_SECONDS,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._handlers: dict[CorrectiveAction, CorrectiveHandler] = {
            CorrectiveAction.OPEN_OPERATOR_HITL: open_hitl,
            CorrectiveAction.NUDGE_AGENT: nudge,
            CorrectiveAction.RESPAWN_COHORT: respawn,
        }
        self._authorize = authorize or _default_authorize
        self._rl_window = max(0, int(rate_limit_window_seconds))
        self._rl_max = max(1, int(rate_limit_max_per_window))
        self._idem_window = max(0, int(idempotency_window_seconds))
        self._time = time_fn or _time.time
        # (action, target) -> recent successful-execution timestamps
        self._recent: dict[tuple[str, str], deque[float]] = {}
        # (action, target, dedupe_key) -> last successful outcome
        self._idempotency: dict[tuple[str, str, str], CorrectiveOutcome] = {}

    @property
    def actions(self) -> frozenset[str]:
        """The exact set of executable actions (exactly three)."""
        return frozenset(a.value for a in self._handlers)

    # -- internals ---------------------------------------------------------

    def _audit(self, outcome: CorrectiveOutcome, context: CorrectiveContext) -> None:
        log = logger.info if outcome.status == CorrectiveStatus.EXECUTED else logger.warning
        log(
            "corrective_action",
            extra={
                "pipeline_id": context.pipeline_id,
                "action": outcome.action,
                "status": outcome.status,
                "target": outcome.target,
                "finding_class": context.finding_class,
                "caller_identity": context.caller_identity,
                "running_agent_count": context.running_agent_count,
                "idempotency_key": outcome.idempotency_key,
                "executed": outcome.executed,
                "detail": outcome.detail,
            },
        )

    def _make(
        self,
        action: str,
        status: str,
        context: CorrectiveContext,
        detail: str,
        *,
        executed: bool = False,
        ts: float | None = None,
    ) -> CorrectiveOutcome:
        outcome = CorrectiveOutcome(
            action=action,
            status=status,
            detail=detail,
            target=context.target,
            idempotency_key=context.dedupe_key(),
            executed=executed,
            ts=ts if ts is not None else self._time(),
        )
        self._audit(outcome, context)
        return outcome

    # -- public API --------------------------------------------------------

    def execute(
        self, action: CorrectiveAction | str, context: CorrectiveContext
    ) -> CorrectiveOutcome:
        """Execute (or refuse) one corrective action under the slice-6 guarantees."""
        now = self._time()
        action_value = action.value if isinstance(action, CorrectiveAction) else str(action)

        # 1. Closed vocabulary — reject anything outside the three actions.
        try:
            action_enum = CorrectiveAction(action_value)
        except ValueError:
            return self._make(
                action_value,
                CorrectiveStatus.UNKNOWN_ACTION,
                context,
                f"action {action_value!r} is outside the closed vocabulary {sorted(self.actions)}",
                ts=now,
            )

        # 2. RBAC — only the orchestrator control plane may execute. Agents
        #    (including the overseer, which advises) are denied here too, not
        #    only at the gateway, so a mis-wired caller cannot slip through.
        allowed, why = self._authorize(context.caller_identity, action_value)
        if not allowed:
            return self._make(action_value, CorrectiveStatus.DENIED, context, why, ts=now)

        # 3. Zero-agent HITL park bar — nothing running means nothing to correct.
        if context.running_agent_count <= 0:
            return self._make(
                action_value,
                CorrectiveStatus.BARRED,
                context,
                "barred: zero agents running (HITL park) — no corrective action fires",
                ts=now,
            )

        # 4. Idempotency — an identical request inside the window is a no-op
        #    that returns the prior successful outcome.
        idem_key = (action_value, context.target, context.dedupe_key())
        cached = self._idempotency.get(idem_key)
        if cached is not None:
            if self._idem_window and (now - cached.ts) < self._idem_window:
                return self._make(
                    action_value,
                    CorrectiveStatus.DEDUPED,
                    context,
                    f"idempotent no-op: identical action executed {now - cached.ts:.0f}s ago",
                    ts=now,
                )
            # expired — drop so a genuine re-occurrence can fire again
            del self._idempotency[idem_key]

        # 5. Rate limit — bound the number of (action, target) firings per window.
        bucket = self._recent.setdefault((action_value, context.target), deque())
        if self._rl_window:
            while bucket and (now - bucket[0]) >= self._rl_window:
                bucket.popleft()
        if len(bucket) >= self._rl_max:
            return self._make(
                action_value,
                CorrectiveStatus.RATE_LIMITED,
                context,
                f"rate-limited: {len(bucket)} firing(s) of {action_value} on "
                f"{context.target!r} within {self._rl_window}s (max {self._rl_max})",
                ts=now,
            )

        # 6. Dispatch the side effect.
        handler = self._handlers[action_enum]
        try:
            detail = handler(context) or "executed"
        except Exception as exc:  # noqa: BLE001 - all side-effect failures audited
            return self._make(
                action_value,
                CorrectiveStatus.FAILED,
                context,
                f"side effect raised: {exc}",
                ts=now,
            )

        # Record for rate-limit + idempotency only on a successful execution so a
        # transient failure does not block a legitimate retry.
        bucket.append(now)
        outcome = CorrectiveOutcome(
            action=action_value,
            status=CorrectiveStatus.EXECUTED,
            detail=str(detail),
            target=context.target,
            idempotency_key=context.dedupe_key(),
            executed=True,
            ts=now,
        )
        self._idempotency[idem_key] = outcome
        self._audit(outcome, context)
        return outcome

    def execute_verdict(self, verdict: Any, context: CorrectiveContext) -> CorrectiveOutcome:
        """Map an :class:`AdjudicationVerdict` recommendation onto an action.

        ``recommended_action == "none"`` (or empty) is a NOOP — the adjudicator
        judged the finding a false alarm or chose to take no action. Any other
        value is routed through :meth:`execute`, which rejects out-of-vocabulary
        recommendations.
        """
        action = str(getattr(verdict, "recommended_action", "") or "").strip()
        if action in ("", "none"):
            return self._make(
                action or "none",
                CorrectiveStatus.NOOP,
                context,
                "adjudicator recommended no action",
            )
        return self.execute(action, context)


__all__ = [
    "CORRECTIVE_ACTIONS",
    "CorrectiveAction",
    "CorrectiveContext",
    "CorrectiveExecutor",
    "CorrectiveHandler",
    "CorrectiveOutcome",
    "CorrectiveStatus",
]
