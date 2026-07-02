"""Cross-repo merge-sequencing gate for multi-repo pipelines (#3393, slice-5).

This module implements the cq-1 two-tier merge-sequencing hold. It is a
pure-logic consumer of injected callables (gateway reads/writes + HITL
registration), mirroring :mod:`stacked_pr_reconciler`'s decoupled design so
it can be unit-tested without a live gateway, and it is driven on the
**existing** stacked-PR reconciler cadence (``_start_stacked_pr_reconciler``
in ``routes/pipelines.py``) rather than inventing a new scheduler subsystem.

Model
-----
A dependency edge ``B → A`` (slice ``B`` ``dependencies`` names ``A``) is
**cross-repo** iff ``resolve_slice_repo(A) != resolve_slice_repo(B)`` — the
1:1 slice↔repo mapping from #3393. Cross-repo edges cannot stack via a shared
integration branch, so instead the dependent slice ``B`` is developed in
parallel and its PR is opened as a **draft**; only its *ready* transition
waits on the upstream. Same-repo edges keep the existing stacked-PR
integration-branch behaviour and are ignored here.

Two tiers (operator ruling cq-1):

* **Tier A — automated merge-state hold (default).** Poll the upstream
  slice PR's merge state; when it merges, auto-mark ``B`` ready via
  ``mark_pr_ready``. Merge detection keys off the PR ``mergedAt`` / ``state``
  (NOT head-SHA equality — a squash/rebase merge yields a merge-commit SHA
  ≠ the PR head). Two failure terminals fall through to a HITL hold rather
  than hanging: an upstream that reaches **CLOSED-not-merged**, and a poll
  that exceeds the **attempt bound** (a never-merging upstream).

* **Tier B — HITL beyond-merge-state hold (opt-in).** For an edge the plan
  declares as a beyond-merge-state condition (release/publish of the
  upstream repo, a version-pin choice, or a genuine cannot-continue dev
  block), ``B``'s ready transition is held and released ONLY by a human
  decision — never programmatic detection. The plan opts an edge in via the
  :data:`BEYOND_MERGE_STATE_MARKER` token in the dependent slice's ``goal``
  (or a task description); absent ⇒ the default Tier-A hold.

All HITL holds (Tier B, plus the two Tier-A failure terminals) share a
single release path: once the registered decision is resolved by a human,
``B`` is marked ready on the next tick. Tier A's happy path (auto-ready on
merge) is the second, distinct release path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Token the planner embeds in a dependent slice's ``goal`` (or a task
# description) to opt its cross-repo dependency into the Tier-B HITL
# beyond-merge-state hold. Absent ⇒ the default Tier-A automated
# merge-state hold. A distinctive bracketed sentinel so ordinary prose
# ("this lands after the upstream merges") never trips it (architect
# arch-q1: "optional per-slice marker; default absent ⇒ auto").
BEYOND_MERGE_STATE_MARKER = "[hold:beyond-merge-state]"

# Default poll-attempt budget before a never-merging Tier-A upstream
# escalates to a HITL hold. At the 30 s reconciler cadence this is ~2 h
# of waiting; the operator can override via the reconciler wiring.
DEFAULT_MAX_POLL_ATTEMPTS = 240

HoldKind = Literal["auto", "hitl"]
HoldReason = Literal["closed_unmerged", "timeout", "beyond_merge_state"]


@dataclass(frozen=True)
class CrossRepoUpstream:
    """One cross-repo upstream dependency of a dependent slice."""

    slice_id: str
    repo: str
    pr_number: int | None


@dataclass(frozen=True)
class CrossRepoGate:
    """A dependent slice ``B`` whose PR ready-state is gated on upstreams.

    ``upstreams`` are ONLY the cross-repo dependencies (same-repo deps
    stack via the integration branch and are excluded). ``B``'s PR is
    ready only when EVERY cross-repo upstream has merged (Tier A) or a
    human releases the hold (Tier B / Tier-A failure terminal).
    """

    slice_id: str
    repo: str
    pr_number: int
    hold_kind: HoldKind
    upstreams: tuple[CrossRepoUpstream, ...]


@dataclass
class GateProgress:
    """Mutable per-gate bookkeeping that persists across poll ticks.

    Lives in a dict owned by the reconciler daemon closure, so it is
    per-run state (reset on orchestrator restart — the poll re-derives
    gates from the contract each tick and re-converges idempotently).
    """

    attempts: int = 0
    decision_registered: bool = False
    resolved: bool = False


@dataclass
class PollResult:
    """Snapshot of a single cross-repo merge-gate poll pass (for logs/tests)."""

    gates_detected: int = 0
    readied: int = 0
    holds_registered: int = 0
    pending: int = 0
    reasons: list[HoldReason] = field(default_factory=list)


def _slice_repo_of(slice_obj: Any, resolve_repo: Callable[[Any], str | None]) -> str | None:
    try:
        return resolve_repo(slice_obj)
    except Exception:  # noqa: BLE001
        return None


def classify_hold_kind(slice_obj: Any) -> HoldKind:
    """Return ``"hitl"`` if the slice opts into Tier B, else ``"auto"``.

    The declaration surface is the :data:`BEYOND_MERGE_STATE_MARKER`
    token in the slice ``goal`` or any task ``description``. Default
    (absent) ⇒ Tier-A automated merge-state hold.
    """
    goal = getattr(slice_obj, "goal", "") or ""
    if BEYOND_MERGE_STATE_MARKER in goal:
        return "hitl"
    for task in getattr(slice_obj, "tasks", None) or []:
        if BEYOND_MERGE_STATE_MARKER in (getattr(task, "description", "") or ""):
            return "hitl"
    return "auto"


def find_cross_repo_gates(
    contract: Any,
    resolve_repo: Callable[[Any], str | None],
) -> list[CrossRepoGate]:
    """Return one :class:`CrossRepoGate` per dependent slice with an open PR.

    A gate is emitted for slice ``B`` iff ``B`` has an open PR
    (``pr_number`` set), a resolvable repo, and ≥1 dependency ``A`` whose
    resolved repo differs from ``B``'s. Same-repo dependencies are
    excluded (they stack via the integration branch). Slices without a
    PR yet, without deps, or with only same-repo deps produce no gate —
    so an N=1 pipeline (every slice resolves to the one repo) yields an
    empty list and the poll is a no-op.
    """
    slices = list(getattr(contract, "slices", None) or [])
    by_id = {s.id: s for s in slices}
    gates: list[CrossRepoGate] = []

    for s in slices:
        deps = getattr(s, "dependencies", None) or []
        if not deps:
            continue
        s_repo = _slice_repo_of(s, resolve_repo)
        s_pr = getattr(s, "pr_number", None)
        if not s_repo or not isinstance(s_pr, int) or isinstance(s_pr, bool) or s_pr < 1:
            continue  # ``B``'s PR must exist to gate its ready-state

        upstreams: list[CrossRepoUpstream] = []
        for up_id in deps:
            up = by_id.get(up_id)
            if up is None:
                continue
            up_repo = _slice_repo_of(up, resolve_repo)
            if not up_repo or up_repo == s_repo:
                continue  # same-repo dep — stacks via integration branch
            up_pr = getattr(up, "pr_number", None)
            upstreams.append(
                CrossRepoUpstream(
                    slice_id=up.id,
                    repo=up_repo,
                    pr_number=(
                        int(up_pr)
                        if isinstance(up_pr, int) and not isinstance(up_pr, bool) and up_pr >= 1
                        else None
                    ),
                )
            )

        if not upstreams:
            continue  # no cross-repo dependency — nothing to gate

        gates.append(
            CrossRepoGate(
                slice_id=s.id,
                repo=s_repo,
                pr_number=int(s_pr),
                hold_kind=classify_hold_kind(s),
                upstreams=tuple(upstreams),
            )
        )
    return gates


def _is_merged(state: dict[str, Any] | None) -> bool:
    """Merged iff ``mergedAt``/``merged_at`` is set OR state == MERGED.

    Keys off merge-state, never head-SHA equality (#3393 task-5-1 pin a).
    """
    if not state:
        return False
    if state.get("merged_at") or state.get("mergedAt"):
        return True
    return str(state.get("state") or "").strip().upper() == "MERGED"


def _is_closed_unmerged(state: dict[str, Any] | None) -> bool:
    """Closed-not-merged terminal: state == CLOSED with no merge timestamp."""
    if not state:
        return False
    if state.get("merged_at") or state.get("mergedAt"):
        return False
    return str(state.get("state") or "").strip().upper() == "CLOSED"


def poll_once(
    contract: Any,
    *,
    resolve_repo: Callable[[Any], str | None],
    get_merge_state: Callable[[str, int], dict[str, Any] | None],
    mark_ready: Callable[[str, int], bool],
    register_hold: Callable[[CrossRepoGate, HoldReason], bool],
    hold_is_resolved: Callable[[CrossRepoGate], bool],
    state: dict[str, GateProgress],
    max_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS,
) -> PollResult:
    """Run one cross-repo merge-gate pass over the contract's slices.

    Injected callables keep this pure/testable:

    * ``resolve_repo(slice) -> repo`` — the #3393 runtime repo resolver.
    * ``get_merge_state(repo, pr_number) -> {"state","merged_at"} | None`` —
      upstream PR merge-state read (``None`` ⇒ unknown this tick).
    * ``mark_ready(repo, pr_number) -> bool`` — draft→ready transition.
    * ``register_hold(gate, reason) -> bool`` — register/ensure a HITL hold
      decision on the contract (idempotent per gate; surfaced on status).
    * ``hold_is_resolved(gate) -> bool`` — has the human resolved the gate's
      hold decision on the (freshly-loaded) contract?
    * ``state`` — mutable per-gate progress that persists across ticks.

    Returns a :class:`PollResult` for logging/tests. Never raises for a
    single gate — a callable failure degrades that gate to "pending this
    tick" and the next tick retries.
    """
    result = PollResult()
    for gate in find_cross_repo_gates(contract, resolve_repo):
        result.gates_detected += 1
        prog = state.setdefault(gate.slice_id, GateProgress())
        if prog.resolved:
            continue
        try:
            _poll_one_gate(
                gate,
                prog,
                get_merge_state=get_merge_state,
                mark_ready=mark_ready,
                register_hold=register_hold,
                hold_is_resolved=hold_is_resolved,
                max_attempts=max_attempts,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "cross_repo_merge_gate: gate poll raised — pending this tick",
                extra={"slice_id": gate.slice_id, "error": str(exc)},
            )
            result.pending += 1
    return result


def _poll_one_gate(
    gate: CrossRepoGate,
    prog: GateProgress,
    *,
    get_merge_state: Callable[[str, int], dict[str, Any] | None],
    mark_ready: Callable[[str, int], bool],
    register_hold: Callable[[CrossRepoGate, HoldReason], bool],
    hold_is_resolved: Callable[[CrossRepoGate], bool],
    max_attempts: int,
    result: PollResult,
) -> None:
    # A hold decision is already registered (Tier B up-front, or a Tier-A
    # failure terminal). The ONLY release path now is human resolution.
    if prog.decision_registered:
        if hold_is_resolved(gate):
            if mark_ready(gate.repo, gate.pr_number):
                prog.resolved = True
                result.readied += 1
            else:
                result.pending += 1
        else:
            result.pending += 1
        return

    # Tier B: register the HITL hold up front; never auto-detect release.
    if gate.hold_kind == "hitl":
        if register_hold(gate, "beyond_merge_state"):
            prog.decision_registered = True
            result.holds_registered += 1
            result.reasons.append("beyond_merge_state")
        result.pending += 1
        return

    # Tier A: poll every cross-repo upstream's merge state.
    states = [
        get_merge_state(up.repo, up.pr_number) if up.pr_number is not None else None
        for up in gate.upstreams
    ]

    # Any upstream CLOSED-not-merged ⇒ Tier-A failure terminal: do NOT
    # auto-ready; fall through to a HITL hold (distinct from Tier B).
    if any(_is_closed_unmerged(ms) for ms in states):
        if register_hold(gate, "closed_unmerged"):
            prog.decision_registered = True
            result.holds_registered += 1
            result.reasons.append("closed_unmerged")
        result.pending += 1
        return

    # All upstreams merged ⇒ auto-ready (Tier-A happy path).
    if states and all(_is_merged(ms) for ms in states):
        if mark_ready(gate.repo, gate.pr_number):
            prog.resolved = True
            result.readied += 1
        else:
            result.pending += 1
        return

    # Some upstream still open / unknown ⇒ keep waiting, but bound the
    # poll: a never-merging upstream escalates to a HITL hold rather than
    # leaving the dependent PR draft indefinitely.
    prog.attempts += 1
    if prog.attempts > max_attempts:
        if register_hold(gate, "timeout"):
            prog.decision_registered = True
            result.holds_registered += 1
            result.reasons.append("timeout")
    result.pending += 1
