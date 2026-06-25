"""Tests for the slice-8 resume-vs-reseed decision gate (#3200, AC-3, task-8-2).

Slice-8 / task-8-1 lands the *resume-vs-reseed decision* in the event-pump
path (``shared/egg_agent`` + the ``python3 -m egg_agent`` entrypoint in
``sandbox``). At re-invocation the gate reads the prior session's cumulative
window **occupancy** (``cache_read + cache_creation + input``, slice-1 — NOT
billed input) round-tripped through :class:`egg_agent.session.SessionState`
(slice-6), compares it to the model's reseed threshold computed against the
model's REAL backend window (slice-2), and decides:

* occupancy is a **known** value **< threshold** -> **resume** the cached
  session (warm continue via the slice-6 substrate);
* occupancy **>= threshold** -> **reseed**: a FRESH session seeded only from
  the protected root (slice-4), relying on JIT re-pull (slice-5);
* occupancy is **None/unknown** -> **reseed** (cheap, safe bias) — covers the
  non-Claude / sub-200K LiteLLM routes whose ``usage`` may be partial/absent,
  exactly where the trigger matters most;
* **no warm session** (first invocation / expired / consensus-reset / pod
  death) -> **reseed** from the protected root, never a hard failure.

The threshold MUST be computed against the model's real window, never the
``[1m]`` alias: ``[1m]`` is Claude Code's compaction opt-in, not a window size,
so for a sub-1M backend the trigger has to fire well below the 400k floor that
``[1m]`` would imply. That mis-trigger (deferring reseed to 400k and
overflowing a 128K/262K backend) is the central regression slice-2 guarded and
this gate must not reintroduce.

These tests pin the decision *boundary* and the four bias rules. Boundary
occupancy values are derived from the live ``reseed_threshold`` helper (slice-2,
already merged) rather than hard-coded, so they track the floor/margin knobs
without edits.

Parallel-BRC-producer convention (see ``test_reseed_threshold.py`` /
``test_client_resume.py`` / ``test_protected_root.py``): tester and coder run
on separate branches, so the decision symbol may be ABSENT when this file is
collected on the tester branch. The locator ``pytest.skip``s until the coder's
``task-8-1`` implementation merges; the assertions then run at PR assembly. The
invoker is signature-introspecting (maps occupancy / model / session_id /
state by parameter-name aliases) and the verdict normaliser accepts the common
shapes a "resume id or None" / bool / decision-object API might take, so it
converges on the coder's surface without presupposing one exact spelling.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest
from egg_agent.session import SessionState

# The slice-2 threshold helpers live in ``orchestrator/agent_model_resolution.py``.
# The shared test conftest adds ``shared`` / ``sandbox`` to sys.path but not
# ``orchestrator``; add it so ``_threshold_for`` can derive ground-truth boundary
# values (mirrors the sys.path setup in ``orchestrator/tests/test_reseed_threshold.py``).
_ORCH_PATH = Path(__file__).resolve().parents[3] / "orchestrator"
if _ORCH_PATH.is_dir() and str(_ORCH_PATH) not in sys.path:
    sys.path.insert(0, str(_ORCH_PATH))

# --------------------------------------------------------------------------- #
# Locators (parallel-BRC skip-guards)
# --------------------------------------------------------------------------- #

# Modules the coder might land the decision in. ``shared/egg_agent`` is the
# slice's home; the entrypoint module is included because the gate wires into
# ``python3 -m egg_agent``.
_CANDIDATE_MODULES = (
    "egg_agent.reseed",
    "egg_agent.resume",
    "egg_agent.session",
    "egg_agent.client",
    "egg_agent.threshold",
    "egg_agent.__main__",
)

# Names a "decide whether to resume or reseed" function might take.
_CANDIDATE_FN_NAMES = (
    "decide_resume_or_reseed",
    "resume_or_reseed",
    "decide_session_action",
    "decide_resume",
    "decide_reseed",
    "resolve_resume_decision",
    "resume_decision",
    "choose_resume_or_reseed",
    "resume_vs_reseed",
    "should_resume",
    "plan_session_resume",
    "decide_warm_resume",
)


def _decision_fn():
    """Return the slice-8 decision callable, or skip until the coder lands it."""
    for mod_name in _CANDIDATE_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for fn_name in _CANDIDATE_FN_NAMES:
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                return fn
    pytest.skip(
        "resume-vs-reseed decision fn not yet implemented (waiting on coder, slice-8 task-8-1)"
    )


def _threshold_for(model: str) -> int:
    """Ground-truth reseed threshold for ``model`` via the merged slice-2 helper.

    Prefer an egg_agent re-export if the coder added one; fall back to the
    canonical ``orchestrator.agent_model_resolution`` home. Used only to DERIVE
    boundary occupancy values for the tests — the decision under test is
    expected to compute the same threshold internally.
    """
    candidates = (
        "egg_agent.reseed",
        "egg_agent.threshold",
        "egg_agent.session",
        "agent_model_resolution",
        "orchestrator.agent_model_resolution",
    )
    for mod_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for fn_name in ("reseed_threshold", "threshold", "compute_reseed_threshold"):
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                try:
                    return int(fn(model))
                except Exception:  # pragma: no cover - wrong candidate signature
                    continue
    pytest.skip("reseed_threshold helper not importable (slice-2 prerequisite)")


# --------------------------------------------------------------------------- #
# Signature-introspecting invoker + verdict normaliser
# --------------------------------------------------------------------------- #

_MODEL_PARAMS = {"model", "model_alias", "model_name", "alias", "model_id"}
_OCCUPANCY_PARAMS = {
    "occupancy",
    "window_occupancy",
    "occ",
    "tokens",
    "used",
    "current_occupancy",
    "resumed_occupancy",
}
_SESSION_ID_PARAMS = {"session_id", "sid", "resume", "resume_id", "prior_session_id"}
_STATE_PARAMS = {
    "state",
    "session_state",
    "prior",
    "prior_state",
    "record",
    "warm",
    "warm_session",
    "resume_state",
}
_ENABLED_PARAMS = {"enabled", "resume_enabled", "session_resume_enabled", "allow_resume"}
_THRESHOLD_PARAMS = {"threshold", "reseed_threshold", "limit", "max_occupancy"}


def _invoke(
    fn,
    *,
    model: str,
    occupancy: int | None,
    session_id: str,
    enabled: bool = True,
) -> Any:
    """Call ``fn`` mapping our scenario inputs onto its params by name alias.

    Tolerant of the plausible signature shapes: ``(state, model)``,
    ``(occupancy, model)``, ``(session_id, occupancy, model)``,
    ``(state, model, *, enabled=...)``, ``(occupancy, threshold)``, etc.
    Unmatched params fall back to their defaults; an unmatched *required* param
    is filled with the most likely value (a ``SessionState``) or skips.
    """
    state = SessionState(session_id=session_id, window_occupancy=occupancy)
    threshold = _threshold_for(model)
    try:
        sig = inspect.signature(fn)
    except TypeError, ValueError:  # pragma: no cover - builtins etc.
        return fn(state, model)

    kwargs: dict[str, Any] = {}
    used_positional: list[Any] = []
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        lname = name.lower()
        if lname in _MODEL_PARAMS:
            value: Any = model
        elif lname in _OCCUPANCY_PARAMS:
            value = occupancy
        elif lname in _SESSION_ID_PARAMS:
            value = session_id
        elif lname in _STATE_PARAMS:
            value = state if session_id else None
        elif lname in _ENABLED_PARAMS:
            value = enabled
        elif lname in _THRESHOLD_PARAMS:
            value = threshold
        elif param.default is not inspect.Parameter.empty:
            continue  # leave optional unknown params at their default
        else:
            # Unknown required param: best-effort guess (a state object), and
            # remember we are uncertain.
            value = state
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = value
        else:
            used_positional.append(value)

    return fn(*used_positional, **kwargs)


_RESUME = "resume"
_RESEED = "reseed"


def _verdict(result: Any, *, session_id: str) -> str:
    """Normalise the decision's return value to ``"resume"`` or ``"reseed"``.

    Accepts the shapes a resume-vs-reseed gate plausibly returns:

    * ``str | None`` — the resume session id to pass to ``run_agent`` (the most
      natural shape given ``__main__`` threads ``resume=...``): non-empty -> resume.
    * ``bool`` — ``should_resume``: ``True`` -> resume.
    * a decision object/enum — read ``.resume`` / ``.should_resume`` / ``.reseed``
      / ``.action`` / ``.mode`` / ``.decision`` / a resume-id attribute.
    * a ``(action, session_id)`` tuple.
    """
    # None -> always reseed.
    if result is None:
        return _RESEED
    # Bare bool.
    if isinstance(result, bool):
        return _RESUME if result else _RESEED
    # Bare string: either a session id (resume) or an action word.
    if isinstance(result, str):
        low = result.strip().lower()
        if low in (_RESUME, _RESEED):
            return low
        return _RESUME if result.strip() else _RESEED
    # SessionState or similar object carrying a session id.
    # Tuple/list: scan elements for an action word or a truthy id.
    if isinstance(result, (tuple, list)):
        for elem in result:
            if isinstance(elem, str) and elem.strip().lower() in (_RESUME, _RESEED):
                return elem.strip().lower()
        for elem in result:
            if isinstance(elem, bool):
                return _RESUME if elem else _RESEED
        for elem in result:
            if isinstance(elem, str) and elem.strip():
                return _RESUME
        return _RESEED

    # Object with descriptive attributes.
    for attr in ("should_resume", "resume", "is_resume", "warm"):
        val = getattr(result, attr, None)
        if isinstance(val, bool):
            return _RESUME if val else _RESEED
    for attr in ("reseed", "should_reseed", "is_reseed", "cold"):
        val = getattr(result, attr, None)
        if isinstance(val, bool):
            return _RESEED if val else _RESUME
    for attr in ("action", "mode", "decision", "kind", "verdict"):
        val = getattr(result, attr, None)
        if val is not None:
            low = str(getattr(val, "value", val)).strip().lower()
            if _RESUME in low:
                return _RESUME
            if _RESEED in low or "cold" in low or "fresh" in low:
                return _RESEED
    for attr in ("resume_session_id", "session_id", "resume_id", "resume"):
        val = getattr(result, attr, None)
        if isinstance(val, str):
            return _RESUME if val.strip() else _RESEED

    raise AssertionError(
        f"Could not interpret decision result {result!r} (type {type(result)!r}) "
        "as resume/reseed — the verdict normaliser needs an alias for the "
        "coder's chosen return shape (parallel-BRC re-alignment)."
    )


def _decide(*, model: str, occupancy: int | None, session_id: str, enabled: bool = True) -> str:
    fn = _decision_fn()
    return _verdict(
        _invoke(fn, model=model, occupancy=occupancy, session_id=session_id, enabled=enabled),
        session_id=session_id,
    )


# Models used across the boundary tests.
_CLAUDE = "opus[1m]"  # real window 1M -> threshold 400_000 (floor caps 0.80*1M)
_SUB_1M = "kimi-k2.7-code[1m]"  # real window 262_144 -> threshold ~209_715 (< 400k floor)
_WARM_SID = "sess-warm-0001"


# --------------------------------------------------------------------------- #
# Decision boundary: under / at / over (AC-3)
# --------------------------------------------------------------------------- #


def test_occupancy_just_under_threshold_resumes():
    """Known occupancy one token below threshold -> resume the warm session."""
    t = _threshold_for(_CLAUDE)
    assert _decide(model=_CLAUDE, occupancy=t - 1, session_id=_WARM_SID) == _RESUME


def test_occupancy_at_threshold_reseeds():
    """At the threshold (boundary is exclusive for resume) -> reseed from root."""
    t = _threshold_for(_CLAUDE)
    assert _decide(model=_CLAUDE, occupancy=t, session_id=_WARM_SID) == _RESEED


def test_occupancy_over_threshold_reseeds():
    """Well over threshold -> reseed, discarding accumulated history."""
    t = _threshold_for(_CLAUDE)
    assert _decide(model=_CLAUDE, occupancy=t + 100_000, session_id=_WARM_SID) == _RESEED


# --------------------------------------------------------------------------- #
# Bias rules: None/unknown occupancy and no-warm-session both reseed (never a
# lossy resume), even with a valid session id and resume enabled.
# --------------------------------------------------------------------------- #


def test_none_occupancy_biases_to_reseed():
    """Unknown occupancy (partial/absent SDK usage) -> reseed, NOT resume."""
    assert _decide(model=_CLAUDE, occupancy=None, session_id=_WARM_SID) == _RESEED


def test_no_warm_session_reseeds():
    """No resumable session (empty id) -> reseed from the protected root, no raise."""
    assert _decide(model=_CLAUDE, occupancy=None, session_id="") == _RESEED


def test_no_warm_session_with_known_low_occupancy_still_reseeds():
    """Even a 'low' occupancy cannot force a resume when there is no warm session."""
    t = _threshold_for(_CLAUDE)
    assert _decide(model=_CLAUDE, occupancy=t - 1, session_id="") == _RESEED


# --------------------------------------------------------------------------- #
# Real-window threshold: no [1m] mis-trigger (the slice-2 regression, carried
# into the decision). For a sub-1M backend, an occupancy above the REAL
# threshold but below the 400k floor the [1m] alias would imply MUST reseed.
# --------------------------------------------------------------------------- #


def test_sub_1m_backend_uses_real_window_not_1m_alias():
    """kimi[1m]: occupancy > real threshold but < 400k floor -> reseed (no mis-trigger)."""
    real_t = _threshold_for(_SUB_1M)
    # Sanity: the sub-1M threshold is genuinely below the 400k [1m]-implied floor,
    # otherwise this regression has nothing to catch.
    assert real_t < 400_000
    occ = (real_t + 400_000) // 2  # strictly between real threshold and the floor
    assert real_t < occ < 400_000
    # The mis-trigger bug (threshold off the [1m]-implied 1M window) would see
    # occ < 400_000 and RESUME; the correct real-window gate reseeds.
    assert _decide(model=_SUB_1M, occupancy=occ, session_id=_WARM_SID) == _RESEED


def test_sub_1m_backend_below_real_threshold_resumes():
    """Below the sub-1M real threshold the same backend resumes — proves the gate
    keys on the real window symmetrically, not a blanket 'always reseed sub-1M'."""
    real_t = _threshold_for(_SUB_1M)
    assert _decide(model=_SUB_1M, occupancy=real_t - 1, session_id=_WARM_SID) == _RESUME


# --------------------------------------------------------------------------- #
# Reseed routes to the fresh / protected-root path (no resume id leaks through).
# --------------------------------------------------------------------------- #


def test_reseed_does_not_yield_a_resume_session_id():
    """On a reseed verdict the gate must not hand a session id to ``run_agent``.

    A reseed seeds a FRESH session from the protected root (slice-4) and
    re-pulls bulk JIT (slice-5); leaking the prior id would warm-resume the
    history the reseed exists to discard. We assert the raw return carries no
    truthy resume id whenever the verdict is reseed.
    """
    fn = _decision_fn()
    t = _threshold_for(_CLAUDE)
    result = _invoke(fn, model=_CLAUDE, occupancy=t + 50_000, session_id=_WARM_SID)
    assert _verdict(result, session_id=_WARM_SID) == _RESEED

    # No resume id should survive a reseed, in any of the shapes the gate uses.
    leaked: str | None = None
    if isinstance(result, str):
        leaked = result if result.strip().lower() not in (_RESUME, _RESEED) else None
    else:
        for attr in ("resume_session_id", "session_id", "resume_id", "resume"):
            val = getattr(result, attr, None)
            if isinstance(val, str) and val.strip():
                leaked = val
                break
    assert not leaked, f"reseed verdict leaked a resume session id: {leaked!r}"
