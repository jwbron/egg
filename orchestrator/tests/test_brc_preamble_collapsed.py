"""Tests for the collapsed BRC preamble (#2908 slice-3 TASK-3-3).

Authored by the slice-3 coder (per #2936 coder-owns-tests). The
tester reviews-and-hardens these in their own pass.

Coverage (plan TASK-3-7 acceptance):

* Three role-shape snapshots — coder / reviewer_code / tester (the
  dual-role agent in the implement graph) — exercising the three
  caller sites in ``orchestrator/routes/pipelines.py``.
* Absent-strings assertions: ``STAY-ALIVE`` / ``STAY ALIVE`` /
  positive ``wait-loop`` instructions / cursor-threading references
  must NOT appear in the post-collapse output.
* Kept-strings assertions: the dual-mandate phrase ``Both must pass
  to ACK`` MUST appear in the reviewer-bearing variants (reviewer_code
  and tester); the agent roster MUST be present.
* Byte-size assertion: the preamble drops ≥ 25% per the plan
  acceptance (softened from ≥ 40% in the architect's original draft).
  Baselines are captured against the pre-collapse commit before
  slice-3 (HEAD at the time of authorship); subsequent edits to the
  preamble may change the absolute size but the relative drop should
  remain at-or-better than 25%.
"""

from __future__ import annotations

import pytest

from orchestrator.routes.pipelines import _build_brc_preamble

# Pre-collapse baseline sizes captured from
# ``git stash --keep-index`` + render at slice-3 task-3-3 authorship.
# The architect plan: "byte size drop ≥ 25% (measured against
# pre-collapse snapshot…softened from ≥ 40%)". A future preamble
# edit that adds back lifecycle prose can re-raise the post-collapse
# numbers; the assertion uses a relative-drop ratio so it stays
# meaningful across edits.
PRE_COLLAPSE_BASELINE_BYTES: dict[str, int] = {
    "coder": 9664,
    "reviewer_code": 12606,
    "tester": 24139,
}

# Minimum acceptable byte-size drop ratio (plan TASK-3-3 acceptance).
MIN_DROP_RATIO = 0.25


def _render(role: str) -> str:
    return _build_brc_preamble(
        role,
        "implement",
        repo="egg",
        branch="egg/issue-2908-impl2/work",
        base_branch="main",
    )


# ---------------------------------------------------------------------------
# Absent-strings: wait-loop / STAY-ALIVE / cursor mechanics must be gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_stay_alive_strings_absent(role: str) -> None:
    """``STAY-ALIVE`` / ``STAY ALIVE`` are removed from the preamble."""
    text = _render(role)
    assert "STAY-ALIVE" not in text.upper().replace(" ALIVE", "-ALIVE")
    # Permissive check: the literal phrase "STAY ALIVE" must not appear
    # as an instructional heading (it could in principle appear inside
    # the future-roadmap prose; here we want the literal lifecycle
    # heading absent).
    assert "STAY ALIVE" not in text
    assert "**STAY-ALIVE**" not in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_positive_wait_loop_instructions(role: str) -> None:
    """No positive instructions to call ``wait-loop`` remain.

    The single remaining ``wait-loop`` mention in the closing
    event-handler contract is a NEGATIVE instruction (``You do NOT
    block on wait-loop yourself``); the assertion below distinguishes
    positive from negative occurrences by requiring a "do NOT"
    qualifier on every line that mentions ``wait-loop``.
    """
    text = _render(role)
    for line in text.splitlines():
        if "wait-loop" in line:
            assert "do NOT block on" in line or "do NOT issue" in line, (
                f"unexpected positive wait-loop instruction in {role}: {line!r}"
            )


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_cursor_threading_references(role: str) -> None:
    """The ``/tmp/egg-wait-cursor-…`` plumbing reference is removed."""
    text = _render(role)
    assert "egg-wait-cursor" not in text
    assert "issue #2323" not in text  # cross-link to cursor-threading


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_status_ready_to_confirm_foot_gun(role: str) -> None:
    """The ``ready_to_confirm`` directed-STATUS plumbing is removed.

    Under the event-pump model the wrapper's ``next-action`` call
    decides when the role is ready to confirm; the agent no longer
    needs to interpret a directed STATUS nudge to discover it.
    """
    text = _render(role)
    assert "ready_to_confirm: true" not in text
    assert "Ready to confirm — all confirm preconditions satisfied" not in text


# ---------------------------------------------------------------------------
# Kept-strings: dual-mandate banner + agent roster preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["reviewer_code", "tester"])
def test_dual_mandate_phrase_preserved(role: str) -> None:
    """``Both must pass to ACK`` survives the collapse (plan acceptance)."""
    text = _render(role)
    assert "Both must pass to ACK" in text
    # The two equal-weight mandates framing is also kept.
    assert "TWO equal-weight mandates" in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_agent_roster_preserved(role: str) -> None:
    """The ``### Active Agents in This Phase`` roster heading survives."""
    text = _render(role)
    assert "### Active Agents in This Phase" in text


@pytest.mark.parametrize("role", ["coder"])
def test_producer_lifecycle_skeleton_preserved(role: str) -> None:
    """Producer lifecycle keeps its key headings (ORIENT / WORK / PROPOSE)."""
    text = _render(role)
    assert "### Producer Lifecycle" in text
    assert "**ORIENT**" in text
    assert "**WORK**" in text
    assert "**PROPOSE**" in text
    assert "**CONFIRM**" in text


@pytest.mark.parametrize("role", ["reviewer_code"])
def test_reviewer_lifecycle_skeleton_preserved(role: str) -> None:
    """Reviewer lifecycle keeps its key headings (PREPARE / SYNC / REVIEW)."""
    text = _render(role)
    assert "### Reviewer Lifecycle" in text
    assert "**PREPARE**" in text
    assert "**SYNC**" in text
    assert "**REVIEW**" in text
    assert "**ACK/NACK**" in text
    assert "**CONFIRM**" in text


@pytest.mark.parametrize("role", ["tester"])
def test_dual_role_banner_preserved(role: str) -> None:
    """The ``Dual-Role Execution Order`` banner heading survives."""
    text = _render(role)
    assert "Dual-Role Execution Order" in text


# ---------------------------------------------------------------------------
# Event-pump contract — the new closing framing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_event_handler_contract_present(role: str) -> None:
    """The closing event-handler contract replaces the legacy warning."""
    text = _render(role)
    assert "Event-handler contract (#2908)" in text
    assert "event-pump wrapper drives your lifecycle" in text
    # And the old "you have FAILED your role" warning is gone.
    assert "you have FAILED your role" not in text


# ---------------------------------------------------------------------------
# Byte-size drop assertion (plan TASK-3-3 acceptance ≥ 25%)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "baseline_bytes"),
    sorted(PRE_COLLAPSE_BASELINE_BYTES.items()),
)
def test_preamble_byte_size_drops_by_at_least_25_percent(
    role: str, baseline_bytes: int
) -> None:
    """The collapsed preamble must shed ≥ 25% of the pre-collapse bytes."""
    text = _render(role)
    current_bytes = len(text.encode("utf-8"))
    drop_ratio = (baseline_bytes - current_bytes) / baseline_bytes
    assert drop_ratio >= MIN_DROP_RATIO, (
        f"{role}: drop ratio {drop_ratio:.3f} < {MIN_DROP_RATIO} "
        f"(baseline {baseline_bytes} → current {current_bytes})"
    )
