"""Resume-vs-reseed decision gate for the BRC event-pump (#3200, slice-8, task-8-1).

The event-pump invokes a one-shot ``python3 -m egg_agent`` process per BRC event.
Slice-6 added the *substrate* that makes a warm resume possible (persist the
prior run's ``session_id`` + cumulative ``window_occupancy`` to a state file,
read it back, gate it behind ``EGG_SESSION_RESUME``). This module adds the
*decision* on top of that substrate: at the start of each re-invocation, compare
the resumed session's occupancy against a deterministic threshold and choose:

- **occupancy known and < threshold -> RESUME** the cached session (#3186), so
  the warm context (and its >90% root cache) is reused.
- **occupancy >= threshold -> RESEED**: do NOT resume; start a fresh session
  seeded only from the protected root (slice-4) and let the bulk be re-pulled
  just-in-time (slice-5). This discards accumulated history *before* Claude
  Code's ~95% lossy auto-compaction would fire, which is the whole point — the
  reseed bounds the window deterministically; the JIT pull does not (a pulled
  slice stays resident until the next reseed/compaction).

**Bias to RESEED on any uncertainty.** A reseed is cheap and safe (it only
forfeits recency, never the anchors in the protected root); a wrong *resume* can
carry a near-full window into a lossy compaction that drops exactly the BRC
anchors (reviewed SHAs, NACK obligations). So every ambiguous case collapses to
a reseed, never to a "resume below threshold":

- no warm session (first event, expired session, consensus reset, pod death),
- unknown / ``None`` occupancy (non-Claude / sub-200K LiteLLM routes whose SDK
  usage may be partial or absent),
- no resolvable threshold,
- resume disabled (``EGG_SESSION_RESUME`` off — the staged-rollout default).

**Threshold uses the REAL backend window, never the ``[1m]`` alias.** The
threshold is ``min(400_000, 0.80 * real_backend_window)`` (slice-2,
``orchestrator.agent_model_resolution.reseed_threshold``). Computing 80% of the
``[1m]``-implied 1M for a model whose real backend is, e.g., Qwen-128K is the
mis-trigger bug; resolving against the real window avoids it.

**Threshold resolution across the sandbox boundary.** The sandbox runs with
``PYTHONPATH=.../sandbox:.../shared`` — ``orchestrator`` is *not* importable in
the agent process. So this module never hard-depends on it: it reads an
``EGG_RESEED_THRESHOLD`` integer override first (the orchestrator side, which
*can* compute the threshold, may export it into the sandbox env), and only falls
back to importing :func:`orchestrator.agent_model_resolution.reseed_threshold`
when that package happens to be importable (tests + orchestrator runtime). When
neither yields a value the gate gets ``None`` and biases to a safe reseed.

**Within-event growth is out of scope.** This gate fires only at re-invocation
(cross-event drift). A single event whose working set is large is bounded by the
tool-output caps (``tool_output_cap.py``) and the gated recursion escalation, not
by this threshold.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from egg_agent.session import read_session_state, session_resume_enabled

try:
    from egg_logging import get_logger

    logger: Any = get_logger("egg-agent")
except ImportError:  # pragma: no cover - stdlib fallback outside the sandbox
    import logging

    logger = logging.getLogger(__name__)

__all__ = [
    "RESEED_THRESHOLD_ENV",
    "ResumeDecision",
    "decide_resume_session",
    "decide_session_action",
    "resolve_reseed_threshold",
]

# Cross-boundary threshold override. The orchestrator side (which can import
# ``agent_model_resolution``) may compute ``reseed_threshold(model)`` at spawn
# time and export the integer here so the sandbox agent — where ``orchestrator``
# is off PYTHONPATH — can still decide against the real backend window.
RESEED_THRESHOLD_ENV = "EGG_RESEED_THRESHOLD"


@dataclass(frozen=True)
class ResumeDecision:
    """The outcome of the resume-vs-reseed gate for one re-invocation.

    ``resume`` is the load-bearing field: ``True`` means re-enter
    ``session_id``; ``False`` means reseed (the caller passes ``resume=None`` to
    the agent so it cold-starts from the protected root). ``reason`` names which
    branch fired (for logs/metrics); ``occupancy`` and ``threshold`` carry the
    compared values (either may be ``None`` when unresolved).
    """

    resume: bool
    session_id: str | None
    reason: str
    occupancy: int | None
    threshold: int | None


def _positive_int(value: Any) -> int | None:
    """Return ``value`` as a positive int, else ``None`` (bools are not ints here)."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    return value if value > 0 else None


def resolve_reseed_threshold(model: str) -> int | None:
    """Resolve the reseed threshold (tokens) for *model*, or ``None`` — never raising.

    Resolution order:

    1. ``$EGG_RESEED_THRESHOLD`` — an explicit positive-int override, the
       cross-boundary channel for the sandbox (see module docstring).
    2. :func:`orchestrator.agent_model_resolution.reseed_threshold` when that
       module is importable (tests + orchestrator runtime). This is the
       authoritative ``min(400_000, 0.80 * real_backend_window)`` computed
       against the REAL backend window, not the ``[1m]`` alias.

    Returns ``None`` when neither yields a usable value (e.g. the sandbox with no
    override set), which makes the gate bias to a safe reseed.
    """
    raw = os.environ.get(RESEED_THRESHOLD_ENV, "").strip()
    if raw:
        try:
            override = int(raw)
        except ValueError:
            override = None
        positive = _positive_int(override)
        if positive is not None:
            return positive

    try:
        from orchestrator.agent_model_resolution import reseed_threshold
    except Exception:  # pragma: no cover - sandbox lacks orchestrator on PYTHONPATH
        return None
    try:
        return _positive_int(reseed_threshold(model))
    except Exception:  # pragma: no cover - defensive: never let resolution raise
        return None


def decide_session_action(
    *,
    session_id: str | None,
    occupancy: int | None,
    threshold: int | None,
) -> ResumeDecision:
    """Pure resume-vs-reseed decision over (session_id, occupancy, threshold).

    Rules, in order — every non-resume branch is a safe reseed:

    - falsy ``session_id`` -> reseed (``no_warm_session``)
    - ``threshold`` unresolved -> reseed (``no_threshold``)
    - ``occupancy`` unknown (``None`` / non-int) -> reseed (``unknown_occupancy``)
    - ``occupancy >= threshold`` -> reseed (``at_or_above_threshold``)
    - otherwise -> RESUME (``below_threshold``)

    No I/O, no imports — this is the unit-testable core (task-8-2's decision
    boundary). The occupancy is the slice-1 window occupancy
    (``cache_read + cache_creation + input``), never billed input.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return ResumeDecision(False, None, "no_warm_session", occupancy, threshold)

    safe_threshold = _positive_int(threshold)
    if safe_threshold is None:
        return ResumeDecision(False, None, "no_threshold", occupancy, threshold)

    # Occupancy must be a real int; bools and None both mean "unknown" -> reseed.
    if isinstance(occupancy, bool) or not isinstance(occupancy, int):
        return ResumeDecision(False, None, "unknown_occupancy", occupancy, safe_threshold)

    if occupancy >= safe_threshold:
        return ResumeDecision(False, None, "at_or_above_threshold", occupancy, safe_threshold)

    return ResumeDecision(True, sid, "below_threshold", occupancy, safe_threshold)


def decide_resume_session(
    *,
    model: str,
    explicit_resume: str | None = None,
    session_state_path: str | os.PathLike[str] | None = None,
) -> ResumeDecision:
    """Read the persisted session state and decide resume-vs-reseed for this event.

    Reads the prior run's ``session_id`` + ``window_occupancy`` from the
    slice-6 state file (``session_state_path``, else ``$EGG_SESSION_STATE_FILE``),
    resolves the threshold from *model*, and applies :func:`decide_session_action`.
    ``explicit_resume`` (the CLI ``--resume`` id) overrides which session to
    re-enter, but occupancy always comes from the persisted record — we never
    resume without a known occupancy.

    Short-circuits to a reseed when ``EGG_SESSION_RESUME`` is off (the
    staged-rollout default), so the gate's output matches what the client would
    actually do and no state file is read needlessly. Never raises: every read
    failure in the substrate already collapses to ``None`` (cold-start).
    """
    if not session_resume_enabled():
        return ResumeDecision(False, None, "resume_disabled", None, None)

    state = read_session_state(session_state_path)
    occupancy = state.window_occupancy if state is not None else None
    session_id = explicit_resume or (state.session_id if state is not None else None)

    if not (session_id and session_id.strip()):
        decision = ResumeDecision(False, None, "no_warm_session", occupancy, None)
    else:
        threshold = resolve_reseed_threshold(model)
        decision = decide_session_action(
            session_id=session_id,
            occupancy=occupancy,
            threshold=threshold,
        )

    logger.info(
        "BRC event-pump resume-vs-reseed decision: %s",
        "resume" if decision.resume else "reseed",
        event_type="system",
        event_subtype="resume_gate_decision",
        resume=decision.resume,
        reason=decision.reason,
        occupancy=decision.occupancy,
        threshold=decision.threshold,
        model=model,
    )
    return decision
