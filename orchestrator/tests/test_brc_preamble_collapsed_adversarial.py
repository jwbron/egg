"""Adversarial hardening tests for the collapsed BRC preamble (#2908 slice-3).

Authored by the slice-3 tester (per #2936 coder-owns-tests, dual-role
review-and-harden). Probes regressions the coder's snapshot tests
(``test_brc_preamble_collapsed.py``) do not yet pin:

* The pre-seeded-empty-producer branch (#2581) must NOT smuggle
  STAY-ALIVE / wait-loop / cursor-threading text back into the preamble
  for pure producers (coder, documenter) whose slice plan has no tasks.
* No deleted-helper imports or references survive in the post-collapse
  module — the coder summary claims ``_brc_preconfirm_wait_line`` and
  ``_brc_stay_alive_wait_line`` are gone with zero callers. Verify the
  zero-callers claim is durable against accidental re-introduction.
* The event-pump model is explicitly named — ``event-pump`` must appear
  at least once in every implement-phase preamble so the agent knows
  the new model is in effect (silent-fallback hunt: a regression that
  drops the closing "Event-handler contract" block would still pass the
  other absent-strings asserts but leave the agent without the new
  framing).
* The event-handler contract names ``egg-orch brc next-action`` (the
  wrapper's polling endpoint) — pins the slice-2 hand-off mention so a
  refactor doesn't accidentally drop it.
* The plan-phase preamble (much smaller — the plan graph has fewer
  active producers/reviewers) must also be free of the collapsed-out
  lifecycle plumbing, so a future plan-phase preamble that grows back
  past its current ~1.6 KB doesn't accidentally reintroduce STAY-ALIVE.
* Optional-arg call sites: the preamble must render without crashing
  when ``repo`` / ``branch`` / ``base_branch`` are all ``None`` (the
  caller at ``pipelines.py:13502`` reaches the generic path with default
  args; a future regression to a stricter signature must fail loudly).
* Closing contract — the negative ``wait-loop`` mention must remain
  inside the event-handler contract; pin it so a refactor that drops
  the negative qualifier (without dropping the mention) fails this
  assertion rather than passing the no-positive-instructions test
  silently.
"""

from __future__ import annotations

import importlib

import pytest

from orchestrator.routes.pipelines import _build_brc_preamble

# Roles that participate in the implement-phase BRC graph.
_IMPLEMENT_ROLES = (
    "coder",
    "tester",
    "documenter",
    "reviewer_code",
    "reviewer_code_holistic",
    "reviewer_contract",
    "reviewer_security",
    "reviewer_concurrency",
)


def _render(role: str, *, phase: str = "implement", **kwargs: object) -> str:
    return _build_brc_preamble(role, phase, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pre-seeded empty-producer shortcut (#2581) must not re-introduce STAY-ALIVE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "documenter"])
def test_pre_seeded_empty_producer_branch_omits_stay_alive(role: str) -> None:
    """``is_pre_seeded_empty_producer=True`` keeps the STAY-ALIVE prose out.

    The empty-producer shortcut already replaces the propose flow with a
    "confirm and exit" pattern; a regression that left STAY-ALIVE prose
    in the seed branch would confuse a pure-producer agent into blocking
    on a wait it should never enter.
    """
    text = _render(role, is_pre_seeded_empty_producer=True)
    assert "STAY-ALIVE" not in text
    assert "STAY ALIVE" not in text
    # The negative-qualifier guard from the coder's test suite applies
    # here too — any wait-loop mention must carry a "do NOT" qualifier.
    for line in text.splitlines():
        if "wait-loop" in line:
            assert (
                "do NOT call" in line
                or "Do NOT call" in line
                or "do NOT block on" in line
                or "Do NOT block on" in line
                or "do NOT issue" in line
                or "Do NOT issue" in line
            ), f"unexpected positive wait-loop instruction in seeded {role}: {line!r}"


@pytest.mark.parametrize("role", ["coder", "documenter"])
def test_pre_seeded_empty_producer_branch_keeps_skeleton(role: str) -> None:
    """The empty-producer shortcut still surfaces the roster + lifecycle skeleton."""
    text = _render(role, is_pre_seeded_empty_producer=True)
    assert "### Active Agents in This Phase" in text
    assert "### Producer Lifecycle" in text
    # The shortcut block itself is present.
    assert "Pre-seeded empty-producer shortcut" in text


# ---------------------------------------------------------------------------
# Deleted-helper integrity
# ---------------------------------------------------------------------------


def test_deleted_helpers_have_no_module_attribute() -> None:
    """``_brc_preconfirm_wait_line`` / ``_brc_stay_alive_wait_line`` are gone.

    The coder summary claims these helpers are deleted with zero callers.
    A regression that re-imported them (e.g. via a stale ``__all__`` or
    a copy-paste) would surface here so a later collapse pass doesn't
    have to fix the same regression twice.
    """
    pipelines_mod = importlib.import_module("orchestrator.routes.pipelines")
    assert not hasattr(pipelines_mod, "_brc_preconfirm_wait_line")
    assert not hasattr(pipelines_mod, "_brc_stay_alive_wait_line")


# ---------------------------------------------------------------------------
# Event-pump model: must be explicitly named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_event_pump_model_named_in_preamble(role: str) -> None:
    """The phrase ``event-pump`` appears at least once in implement.

    A silent regression that drops the closing "Event-handler contract"
    block would still pass the absent-strings tests but leave the agent
    without the new framing. This assertion ensures the new model is
    surfaced explicitly.
    """
    text = _render(role)
    assert "event-pump" in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_next_action_endpoint_named_in_event_handler_contract(role: str) -> None:
    """``egg-orch brc next-action`` is named in the event-handler contract.

    Pins the slice-2 hand-off mention so a refactor that drops it
    doesn't silently leave the wrapper's polling endpoint undocumented
    in the agent's prompt.
    """
    text = _render(role)
    assert "egg-orch brc next-action" in text


# ---------------------------------------------------------------------------
# Plan-phase preamble — collapsed-out plumbing must stay out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    ["architect", "task_planner", "risk_analyst", "reviewer_plan", "reviewer_refine"],
)
def test_plan_phase_preamble_has_no_stay_alive_or_cursor_plumbing(role: str) -> None:
    """The plan-phase preamble inherits the collapsed shape.

    The plan-phase preamble is much smaller (no reviewer roster of 6),
    but it still uses ``_build_brc_preamble``. A regression that
    re-introduces STAY-ALIVE prose in the plan-phase code path would
    silently re-leak the foot-gun for plan agents.
    """
    text = _render(role, phase="plan")
    assert "STAY-ALIVE" not in text
    assert "STAY ALIVE" not in text
    assert "egg-wait-cursor" not in text
    assert "issue #2323" not in text


# ---------------------------------------------------------------------------
# Optional-arg robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _IMPLEMENT_ROLES)
def test_preamble_renders_without_optional_args(role: str) -> None:
    """``_build_brc_preamble(role, phase)`` works without optional args.

    All optional args (``repo`` / ``branch`` / ``base_branch``) default
    to ``None``; the call sites at ``pipelines.py:13441/:13474/:13502``
    are guaranteed to pass them in, but a defensive caller (or a future
    test) might omit them. The function must not crash and must produce
    a non-empty preamble.
    """
    text = _render(role)
    assert text
    assert "### Active Agents in This Phase" in text


@pytest.mark.parametrize("role", _IMPLEMENT_ROLES)
def test_preamble_renders_with_all_optional_args(role: str) -> None:
    """``_build_brc_preamble`` with all optional args mirrors caller usage."""
    text = _render(
        role,
        repo="egg",
        branch="egg/issue-2908-impl2/work",
        base_branch="main",
    )
    assert text
    # The branch / base resolution affects the SYNC step text for
    # reviewers — ``origin/egg/issue-2908-impl2/work`` should appear in
    # at least the reviewer variants (where the SYNC step is rendered).
    if role.startswith("reviewer_") or role == "tester":
        assert "origin/" in text


# ---------------------------------------------------------------------------
# Negative wait-loop mention must remain inside the event-handler contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_negative_wait_loop_mention_is_in_event_handler_contract(role: str) -> None:
    """The single remaining ``wait-loop`` mention sits inside the contract block.

    The closing event-handler contract carries the negative instruction
    ``You do NOT block on egg-orch message wait-loop yourself``. A
    refactor that strips this sentence without dropping the rest of the
    contract block would leave the agent without the explicit "wrapper
    owns the wait" framing — surfacing the regression here makes it
    visible quickly.
    """
    text = _render(role)
    contract_idx = text.find("Event-handler contract (#2908)")
    assert contract_idx != -1, "event-handler contract section is missing"
    contract_text = text[contract_idx:]
    # The negative wait-loop mention lives inside the contract block.
    assert "do NOT block on `egg-orch message wait-loop`" in contract_text


# ---------------------------------------------------------------------------
# Roster sanity — implement-phase preamble lists every active role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_implement_roster_lists_every_active_role(role: str) -> None:
    """The roster mentions each active role in the implement graph."""
    text = _render(role)
    for active_role in (
        "coder",
        "tester",
        "documenter",
        "reviewer_code",
        "reviewer_code_holistic",
        "reviewer_contract",
        "reviewer_security",
        "reviewer_concurrency",
    ):
        assert active_role in text, f"roster missing {active_role}"


# ---------------------------------------------------------------------------
# "is_pre_seeded_empty_producer" parameter is keyword-only
# ---------------------------------------------------------------------------


def test_is_pre_seeded_empty_producer_is_keyword_only() -> None:
    """The flag is keyword-only — call-site stability against positional drift.

    A future signature regression that moved ``is_pre_seeded_empty_producer``
    to positional would silently change call-site semantics; this test
    asserts the keyword-only marker is durable.
    """
    with pytest.raises(TypeError):
        # Positional passing should fail (the ``*`` makes it kw-only).
        _build_brc_preamble("coder", "implement", "egg", "branch", "main", True)


# ---------------------------------------------------------------------------
# Byte-size assertion — verify the snapshot test's relative-drop math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    ["coder", "reviewer_code", "tester"],
)
def test_post_collapse_preamble_is_smaller_than_baseline_constant(role: str) -> None:
    """Each post-collapse preamble must be strictly smaller than its baseline.

    Independent assertion of the coder's drop-ratio test in
    ``test_brc_preamble_collapsed.py``; this version asserts the
    direction (drop must be strictly positive) so a degenerate
    "no change at all" regression fails loudly without depending on
    the ≥ 25% margin staying intact.
    """
    from orchestrator.tests.test_brc_preamble_collapsed import (  # local import — symmetric
        PRE_COLLAPSE_BASELINE_BYTES,
    )

    baseline = PRE_COLLAPSE_BASELINE_BYTES[role]
    current = len(
        _render(
            role,
            repo="egg",
            branch="egg/issue-2908-impl2/work",
            base_branch="main",
        ).encode("utf-8")
    )
    assert current < baseline, (
        f"{role}: post-collapse preamble ({current}) is not smaller than baseline ({baseline})"
    )


# ---------------------------------------------------------------------------
# Concurrent-mode default vs sequential-mode default
# ---------------------------------------------------------------------------


def test_preamble_unchanged_by_repeated_invocations() -> None:
    """``_build_brc_preamble`` is a pure function — same inputs, same output."""
    a = _render("coder", repo="egg", branch="b", base_branch="main")
    b = _render("coder", repo="egg", branch="b", base_branch="main")
    assert a == b


# ---------------------------------------------------------------------------
# Closing contract anchor — the event-handler block is the last block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_event_handler_contract_appears_after_lifecycles(role: str) -> None:
    """The closing event-handler contract sits AFTER the lifecycle blocks.

    A refactor that hoisted the contract block above the lifecycles
    would re-introduce the "STAY-ALIVE last" foot-gun visually (the
    closing block teaches "wrapper owns the wait" — it belongs after
    the lifecycle so the agent reads it last).
    """
    text = _render(role)
    contract_idx = text.find("Event-handler contract (#2908)")
    assert contract_idx != -1
    # At least one lifecycle marker must precede the contract block.
    lifecycle_markers = []
    for marker in ("### Producer Lifecycle", "### Reviewer Lifecycle"):
        idx = text.find(marker)
        if idx != -1:
            lifecycle_markers.append(idx)
    assert lifecycle_markers, "no lifecycle marker found"
    assert min(lifecycle_markers) < contract_idx, (
        f"event-handler contract must follow the lifecycle blocks for {role}"
    )
