"""Emit-only per-event measurement surfaces for BRC context discipline (#3249).

After each one-shot ``python3 -m egg_agent`` event the agent process holds
everything the six context-discipline metrics derive from: the slice-1
window-occupancy capture (:attr:`AgentResult.window_occupancy` /
:attr:`AgentResult.token_usage`) and the slice-8 resume-vs-reseed verdict
(:class:`egg_agent.reseed.ResumeDecision`). The orchestrator event loop never
reads any of that back — under the #3164 orchestrator-owned loop it sees only
the pod's k8s exit-code classification, never the ``AgentResult`` / logs /
token usage. So the measurement MUST emit **agent-side, in-pod, after the SDK
call**.

This module is the single **Option-D adapter seam** (#3200 phase 10 / the
folded #3258 surfaces): it BUILDS a snapshot of the six metrics from the
slice-1 / slice-8 fields — degrading to ``None`` / ``0`` when a field is absent
(e.g. non-Claude / sub-200K LiteLLM routes whose SDK ``usage`` is partial) —
then EMITS it through the two surfaces a pod CAN write: the structured
**progress** event and the **heartbeat** ping, via the existing ``egg-orch``
CLI (the same surfaces the wrapper's ``emit_heartbeat`` uses). The third
"metrics" surface is an orchestrator in-process registry the pod cannot write
to directly, which is exactly why progress + heartbeat are the chosen channels.

The six metrics:

1. **window occupancy** — ``cache_read + cache_creation + input`` of the final
   turn (:attr:`AgentResult.window_occupancy`).
2. **peak utilization under resume** — occupancy as a fraction of the real
   backend window; the per-event value plus the ``resumed`` flag feed the
   offline peak aggregation.
3. **single-event working set vs real backend window** — the event's resident
   working set (occupancy) against ``real_backend_window`` (the
   recursion-escalation signal, #3200 §6).

Metrics 2 and 3 are window-relative, so they depend on resolving the model's
real backend window. ``orchestrator`` is off ``PYTHONPATH`` in the production
pod (``sandbox/Dockerfile``), so the in-pod resolution comes from the
``EGG_REAL_BACKEND_WINDOW`` cross-boundary env channel (see
:data:`REAL_BACKEND_WINDOW_ENV`), mirroring how the reseed threshold crosses the
boundary via ``EGG_RESEED_THRESHOLD``. When neither the env override nor the
(dev/CI-only) orchestrator import yields a value, both metrics degrade to
``None`` rather than raising.
4. **reseed frequency per phase** — the per-event ``reseeded`` verdict +
   ``reseed_reason`` from the slice-8 gate; frequency is aggregated offline.
5. **root-cache hit rate** — ``cache_read / occupancy`` (share of the window
   served from the warm root cache).
6. **tokens per event** — total tokens processed this event (usage components
   including output).

**Emit-only — nothing is gated on the emitted values.** No runtime control-flow
branch reads a metric, and the emit never changes the agent's exit code; the
metrics are consumed *offline* (#3249 items 2-3 tune the route-aware reseed cap
from them). Every emit failure is swallowed: a measurement emit must never
break — or even perturb the result of — an agent run.

**Default OFF behind** ``EGG_CONTEXT_MEASUREMENT`` so the standalone / legacy
path stays byte-identical until an operator opts a measurement run in (matching
the staged-rollout posture of ``EGG_CONTEXT_DISCIPLINE`` /
``EGG_SESSION_RESUME``).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from egg_agent.reseed import ResumeDecision
    from egg_agent.result import AgentResult

__all__ = [
    "MEASUREMENT_ENV",
    "MEASUREMENT_METRIC_FIELDS",
    "REAL_BACKEND_WINDOW_ENV",
    "MeasurementSnapshot",
    "build_snapshot",
    "emit_snapshot",
    "measurement_enabled",
    "record_measurement",
]

# The opt-in switch for the measurement emit. Default OFF — an unset / blank /
# unrecognised value keeps the legacy path byte-identical (no spurious progress
# events on non-measurement runs). Truthy spellings match
# ``context_discipline._TRUTHY`` so the flags share one mental model.
MEASUREMENT_ENV = "EGG_CONTEXT_MEASUREMENT"
_TRUTHY = {"1", "true", "yes", "on"}

# Cross-boundary real-backend-window override. The sandbox runs with
# ``orchestrator`` off ``PYTHONPATH`` (``sandbox/Dockerfile`` sets
# ``PYTHONPATH=.../sandbox:.../shared``), so an in-pod
# ``from orchestrator.agent_model_resolution import real_backend_window`` always
# fails — which would null ``real_backend_window`` / ``window_utilization`` on
# *every* production event. This env channel is the fix, mirroring
# ``reseed.RESEED_THRESHOLD_ENV``: the orchestrator side (which CAN compute the
# window) may export the resolved integer at spawn time so ``_resolve_real_window``
# populates the metric in-pod instead of degrading to a null window.
REAL_BACKEND_WINDOW_ENV = "EGG_REAL_BACKEND_WINDOW"

# A type alias for the injectable subprocess runner (overridden in tests).
Runner = Callable[[list[str]], object]


@dataclass(frozen=True)
class MeasurementSnapshot:
    """The six context-discipline metrics for one BRC event, plus identity.

    Built by :func:`build_snapshot` from the slice-1 occupancy capture and the
    slice-8 resume-vs-reseed verdict. Every metric is ``None`` / ``0`` when its
    source field is absent — the snapshot never raises and never assumes a
    Claude-shaped ``usage`` block.
    """

    # --- identity (route segmentation for the offline analysis) ---
    model: str
    agent_role: str | None
    phase: str | None
    slice_id: str | None
    num_turns: int | None

    # --- (1) window occupancy = cache_read + cache_creation + input (final turn) ---
    window_occupancy: int | None

    # --- slice-1 token component breakout (informational; from token_usage) ---
    input_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    output_tokens: int | None

    # --- (3) single-event working set vs real backend window ---
    real_backend_window: int | None
    reseed_threshold: int | None

    # --- (2) utilization for peak-under-resume aggregation ---
    window_utilization: float | None

    # --- (5) root-cache hit rate ---
    root_cache_hit_rate: float | None

    # --- (6) tokens per event (total processed, including output) ---
    tokens_per_event: int | None

    # --- (4) reseed-frequency signal (slice-8 verdict) ---
    resumed: bool
    reseeded: bool
    reseed_reason: str
    prior_occupancy: int | None


# Metric-bearing snapshot fields — the values the emit-only invariant forbids
# any control-flow branch from reading. Exported so the structural test pins the
# canonical list at one source of truth (no drift). Pure identity fields
# (``model`` / ``agent_role`` / ``phase`` / ``slice_id``) are deliberately
# excluded: gating an emit on "which role am I" is not gating on a metric.
MEASUREMENT_METRIC_FIELDS: tuple[str, ...] = (
    "window_occupancy",
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "real_backend_window",
    "reseed_threshold",
    "window_utilization",
    "root_cache_hit_rate",
    "tokens_per_event",
    "resumed",
    "reseeded",
    "prior_occupancy",
    "num_turns",
)


def measurement_enabled() -> bool:
    """Return whether the measurement emit is enabled (opt-in, default OFF).

    Reads ``$EGG_CONTEXT_MEASUREMENT``; accepts ``1`` / ``true`` / ``yes`` /
    ``on`` (case-insensitive). Never raises.
    """
    return os.environ.get(MEASUREMENT_ENV, "").strip().lower() in _TRUTHY


def _safe_ratio(numerator: int | None, denominator: int | None) -> float | None:
    """``numerator / denominator`` as a float, or ``None`` when undefined.

    Returns ``None`` for a missing operand or a non-positive denominator so a
    partial-``usage`` route (no occupancy) yields a null ratio rather than a
    ``ZeroDivisionError`` or a misleading ``0.0``.
    """
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _positive_int(value: object) -> int | None:
    """Return ``value`` as a positive int, else ``None`` (bools are not ints here).

    Mirrors :func:`egg_agent.reseed._positive_int` so the two cross-boundary
    overrides validate identically.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _resolve_real_window(model: str) -> int | None:
    """Resolve the model's real backend window, or ``None`` — never raising.

    Resolution order, mirroring :func:`egg_agent.reseed.resolve_reseed_threshold`:

    1. ``$EGG_REAL_BACKEND_WINDOW`` — an explicit positive-int override, the
       **only** cross-boundary channel that can populate this field in the
       sandbox: the agent process runs with ``orchestrator`` off ``PYTHONPATH``
       (``sandbox/Dockerfile``), so the import below always fails in-pod. The
       orchestrator does not yet export this variable at spawn time, so today
       the override is unset in-pod and resolution falls through to ``None`` —
       ``real_backend_window`` / ``window_utilization`` are ``None`` on every
       production event until that spawn-time export lands. Once it does, this
       is the path that populates the field in production.
    2. :func:`orchestrator.agent_model_resolution.real_backend_window` when that
       module is importable (tests + orchestrator runtime).

    Returns ``None`` when neither yields a value (e.g. the sandbox with no
    override set), degrading the two window-relative metrics to null rather than
    raising.
    """
    raw = os.environ.get(REAL_BACKEND_WINDOW_ENV, "").strip()
    if raw:
        try:
            override = int(raw)
        except ValueError:
            override = None
        positive = _positive_int(override)
        if positive is not None:
            return positive

    try:
        from orchestrator.agent_model_resolution import real_backend_window
    except Exception:  # pragma: no cover - sandbox lacks orchestrator on PYTHONPATH
        return None
    try:
        return _positive_int(real_backend_window(model))
    except Exception:  # pragma: no cover - defensive: resolution never raises here
        return None


def _resolve_threshold(model: str) -> int | None:
    """Resolve the reseed threshold for *model* via the slice-8 resolver.

    Reuses :func:`egg_agent.reseed.resolve_reseed_threshold`, which reads the
    ``$EGG_RESEED_THRESHOLD`` override first (the cross-boundary channel the
    orchestrator side *may* export into the sandbox — like
    ``$EGG_REAL_BACKEND_WINDOW``, no producer wires it yet, so today this falls
    through to the import path) and falls back to the orchestrator computation
    when importable. Returns ``None`` when unresolved.
    """
    try:
        from egg_agent.reseed import resolve_reseed_threshold

        return resolve_reseed_threshold(model)
    except Exception:  # pragma: no cover - defensive: never let resolution raise
        return None


def _component(token_usage: dict[str, int] | None, key: str) -> int | None:
    """Read a single ``token_usage`` component as an int, else ``None``."""
    if not isinstance(token_usage, dict):
        return None
    value = token_usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_snapshot(
    *,
    result: AgentResult,
    resume_decision: ResumeDecision,
    model: str,
) -> MeasurementSnapshot:
    """Build the six-metric snapshot from the slice-1 / slice-8 fields.

    Binds to ``result.window_occupancy`` / ``result.token_usage`` (slice-1) and
    ``resume_decision`` (slice-8), degrading every derived metric to ``None`` /
    ``0`` when its source is absent. The only environment reads are the identity
    fields (``EGG_AGENT_ROLE`` / ``EGG_PHASE`` / ``EGG_SLICE_ID``) and the
    best-effort window/threshold resolution helpers (all of which return
    ``None`` rather than raising) — not a pure function, but free of any
    side effect, so it is unit-testable in isolation.
    """
    occupancy = result.window_occupancy
    usage = result.token_usage

    input_tokens = _component(usage, "input_tokens")
    cache_read = _component(usage, "cache_read_input_tokens")
    cache_creation = _component(usage, "cache_creation_input_tokens")
    output_tokens = _component(usage, "output_tokens")

    real_window = _resolve_real_window(model)
    threshold = _resolve_threshold(model)

    # tokens/event: total processed this event (resident window + new output),
    # or None when no usage was reported at all.
    components = [c for c in (occupancy, output_tokens) if c is not None]
    tokens_per_event = sum(components) if components else None

    return MeasurementSnapshot(
        model=model,
        agent_role=os.environ.get("EGG_AGENT_ROLE") or None,
        phase=os.environ.get("EGG_PHASE") or None,
        slice_id=os.environ.get("EGG_SLICE_ID") or None,
        num_turns=result.num_turns,
        window_occupancy=occupancy,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        output_tokens=output_tokens,
        real_backend_window=real_window,
        reseed_threshold=threshold,
        window_utilization=_safe_ratio(occupancy, real_window),
        root_cache_hit_rate=_safe_ratio(cache_read, occupancy),
        tokens_per_event=tokens_per_event,
        resumed=bool(resume_decision.resume),
        reseeded=not resume_decision.resume,
        reseed_reason=resume_decision.reason,
        prior_occupancy=resume_decision.occupancy,
    )


def _summary(snapshot: MeasurementSnapshot) -> str:
    """A compact one-line heartbeat body — human-skimmable, not machine-parsed.

    Carries the slice-8 ``reseed_reason`` verbatim rather than branching on the
    ``reseeded`` metric to pick a word — the emit path must not read a metric
    value to make a decision, even a cosmetic one (the emit-only invariant the
    structural test pins). The only conditionals here are ``None`` presence
    checks on local copies, for null-formatting the log line.
    """
    occ = snapshot.window_occupancy
    util = snapshot.window_utilization
    hit = snapshot.root_cache_hit_rate
    occ_s = "n/a" if occ is None else str(occ)
    util_s = "n/a" if util is None else f"{util:.2f}"
    hit_s = "n/a" if hit is None else f"{hit:.2f}"
    return (
        f"context-measure occ={occ_s} util={util_s} "
        f"cache_hit={hit_s} decision={snapshot.reseed_reason}"
    )


def _default_run(cmd: list[str]) -> None:
    """Run an ``egg-orch`` emit command, best-effort (never raises, 5s cap).

    List form (no shell) so the JSON ``--detail`` payload cannot inject. A
    missing binary, timeout, or non-zero exit is swallowed: the emit is purely
    observational.
    """
    try:
        subprocess.run(cmd, timeout=5, capture_output=True, check=False)
    except Exception:  # pragma: no cover - emit is best-effort; never propagate
        pass


def emit_snapshot(snapshot: MeasurementSnapshot, *, run: Runner = _default_run) -> None:
    """Emit *snapshot* through the progress + heartbeat surfaces (best-effort).

    Routes the full metric payload as the ``detail`` of a structured progress
    event, and a compact summary as the body of a ``WORKING`` heartbeat — the
    two surfaces an in-pod agent can write. ``pipeline_id`` / ``role`` are read
    by the CLI from the pod env (``EGG_PIPELINE_ID`` / ``EGG_AGENT_ROLE``).
    Never raises; each surface emits independently so one failing does not
    suppress the other.

    The two emits are sequential 5s-capped subprocesses, so a fully stalled
    orchestrator adds up to ~10s per event before the pod returns. That worst
    case is bounded by the default-OFF gate (only opted-in measurement runs pay
    it) and the per-call ``timeout`` cap; the offline consumers tolerate a
    dropped emit, so the cap is preferred over an unbounded wait.
    """
    detail = json.dumps(asdict(snapshot), separators=(",", ":"), sort_keys=True)
    run(
        [
            "egg-orch",
            "progress",
            "emit",
            "--step",
            "context-measurement",
            "--state",
            "working",
            "--detail",
            detail,
        ]
    )
    run(
        [
            "egg-orch",
            "message",
            "heartbeat",
            "--state",
            "WORKING",
            "--body",
            _summary(snapshot),
        ]
    )


def record_measurement(
    *,
    result: AgentResult,
    resume_decision: ResumeDecision,
    model: str,
    run: Runner = _default_run,
) -> None:
    """Build and emit the per-event measurement snapshot (the in-pod entry point).

    Called once from ``python3 -m egg_agent`` after the SDK call. A no-op unless
    the measurement is opted in (``EGG_CONTEXT_MEASUREMENT``) AND the process is
    running inside a pipeline pod (``EGG_PIPELINE_ID`` set) — so standalone /
    legacy invocations stay byte-identical. Emit-only: returns ``None``, gates
    nothing on the metric values, and swallows every failure so a measurement
    problem can never change the agent's outcome.
    """
    if not measurement_enabled():
        return
    if not os.environ.get("EGG_PIPELINE_ID"):
        return
    try:
        snapshot = build_snapshot(result=result, resume_decision=resume_decision, model=model)
        emit_snapshot(snapshot, run=run)
    except Exception:  # pragma: no cover - emit is best-effort; never propagate
        pass
