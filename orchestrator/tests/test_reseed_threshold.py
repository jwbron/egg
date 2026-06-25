"""Tests for the slice-2 real-backend-window resolver and reseed threshold.

#3200 / slice-2 (AC-3 foundation, task-2-3). The coder lands two pure,
deterministic helpers in ``orchestrator/agent_model_resolution.py``:

  * ``real_backend_window(model)`` (task-2-1) — the REAL upstream context
    window for a model alias/id, keyed on the BARE name. The ``[1m]`` suffix
    is Claude Code's compaction opt-in, NOT a window size, so it is stripped
    before lookup and never used as the window. Claude aliases -> 1_000_000;
    ``_SUB_1M_CONTEXT_MODELS`` members -> their registered size
    (``kimi-k2.7-code`` -> 262_144); any other non-Claude / 200K-profile model
    -> 200_000.
  * ``reseed_threshold(model)`` (task-2-2) — ``min(FLOOR, 0.80 * real_window)``
    where ``FLOOR`` is a named, overridable constant (400_000). The 0.80 margin
    pre-empts Claude Code's ~95% lossy auto-compaction.

These tests assert the issue's worked examples:

    opus[1m]        -> 400_000   (min(400k, 0.80 * 1_000_000 = 800k))
    200K profile    -> 160_000   (min(400k, 0.80 *   200_000 = 160k))
    Qwen/128K-class -> 102_400   (min(400k, 0.80 *   128_000 = 102.4k))

plus the central mis-trigger regression (task-2-3 AC): a sub-1M model's
threshold is computed against its REAL window, NEVER the ``[1m]``-implied 1M —
the bug that would defer reseed to 400k and overflow a small backend.

Tester and coder run as parallel BRC producers on separate branches, so these
symbols may be absent when this file is collected on the tester branch. The
import helpers ``pytest.skip`` until the coder's implementation merges — the
established slice convention (see ``test_agent_model_resolution.py``), which
keeps the suite green pre-merge and runs the assertions at PR assembly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add orchestrator to sys.path the same way test_agent_model_resolution.py does.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


def _module():
    """Return the ``agent_model_resolution`` module (always importable today)."""
    try:
        import agent_model_resolution  # type: ignore[import-not-found]

        return agent_model_resolution
    except ImportError:  # pragma: no cover - module exists in-tree
        pytest.skip("agent_model_resolution not importable")


def _real_window():
    """Return the slice-2 real-backend-window resolver, or skip until it lands."""
    amr = _module()
    for name in ("real_backend_window", "resolve_real_backend_window", "real_window"):
        fn = getattr(amr, name, None)
        if callable(fn):
            return fn
    pytest.skip("real_backend_window not yet implemented (waiting on coder, task-2-1)")


def _threshold():
    """Return the slice-2 reseed-threshold helper, or skip until it lands."""
    amr = _module()
    for name in (
        "reseed_threshold",
        "threshold",
        "compute_reseed_threshold",
        "threshold_for_model",
    ):
        fn = getattr(amr, name, None)
        if callable(fn):
            return fn
    pytest.skip("reseed threshold fn not yet implemented (waiting on coder, task-2-2)")


def _floor_constant_name(amr):
    """Best-effort locate the module-level ``400_000`` floor constant by name."""
    matches = [
        name
        for name, value in vars(amr).items()
        if name.isupper() and isinstance(value, int) and value == 400_000
    ]
    return matches[0] if matches else None


# --------------------------------------------------------------------------- #
# real_backend_window — keyed on the BARE name, [1m] suffix is not a window
# --------------------------------------------------------------------------- #


def test_claude_alias_real_window_is_1m():
    """opus / opus[1m] both resolve to the real 1M Anthropic window (task-2-1 AC)."""
    real = _real_window()
    assert real("opus") == 1_000_000
    assert real("opus[1m]") == 1_000_000


def test_registered_sub_1m_model_returns_registered_size():
    """kimi-k2.7-code -> its registered 262_144 window, with or without [1m]."""
    real = _real_window()
    assert real("kimi-k2.7-code") == 262_144
    # The [1m] suffix is stripped before lookup; it must not bump the window to 1M.
    assert real("kimi-k2.7-code[1m]") == 262_144


def test_unregistered_non_claude_model_returns_200k_profile():
    """An unregistered non-Claude model takes the 200K compaction profile (task-2-1 AC)."""
    real = _real_window()
    assert real("acme-unregistered-model-v9") == 200_000


# --------------------------------------------------------------------------- #
# reseed_threshold — min(400_000, 0.80 * real_window), worked examples
# --------------------------------------------------------------------------- #


def test_threshold_opus_hits_400k_floor():
    """opus[1m] -> 400k: the floor caps 0.80*1M=800k (worked example)."""
    thr = _threshold()
    assert thr("opus") == 400_000
    assert thr("opus[1m]") == 400_000


def test_threshold_200k_profile_is_160k():
    """200K profile -> 160k: 0.80*200_000, under the floor (worked example)."""
    thr = _threshold()
    assert thr("acme-unregistered-model-v9") == 160_000


def test_threshold_kimi_262k():
    """kimi-k2.7-code -> 209_715: 0.80*262_144, under the floor."""
    thr = _threshold()
    assert thr("kimi-k2.7-code") == int(0.80 * 262_144)  # 209_715


# --------------------------------------------------------------------------- #
# Mis-trigger regression: sub-1M threshold uses the REAL window, not the alias
# --------------------------------------------------------------------------- #


def test_sub_1m_threshold_uses_real_window_not_1m_alias(monkeypatch):
    """Qwen/128K-class -> ~102k, and the [1m] suffix never lifts it to the 400k floor.

    This is the central mis-trigger regression (task-2-3 AC). A synthetic 128K
    backend is registered for the duration of the test so the assertion exercises
    the resolver LOGIC without coupling to whichever Qwen-class key the coder
    ultimately lands in ``_SUB_1M_CONTEXT_MODELS``.
    """
    amr = _module()
    real = _real_window()
    thr = _threshold()

    monkeypatch.setitem(amr._SUB_1M_CONTEXT_MODELS, "qwen3-128k-test", 128_000)

    # The real window is 128K regardless of the [1m] suffix ...
    assert real("qwen3-128k-test") == 128_000
    assert real("qwen3-128k-test[1m]") == 128_000  # NOT 1_000_000 — the mis-trigger bug

    # ... so the threshold stays ~102k and never reaches the 400k floor a 1M
    # mis-read would produce.
    expected = int(0.80 * 128_000)  # 102_400
    assert thr("qwen3-128k-test") == expected
    assert thr("qwen3-128k-test[1m]") == expected


# --------------------------------------------------------------------------- #
# The floor is a named, overridable constant (task-2-2 AC)
# --------------------------------------------------------------------------- #


def test_floor_is_named_overridable_constant(monkeypatch):
    """Lowering the named 400_000 floor lowers the capped threshold accordingly."""
    amr = _module()
    thr = _threshold()
    name = _floor_constant_name(amr)
    if name is None:
        pytest.skip("400_000 floor constant not locatable by name (coder naming TBD)")
    monkeypatch.setattr(amr, name, 50_000)
    # opus' 0.80*1M=800k is now capped by the lowered 50k floor.
    assert thr("opus") == 50_000
