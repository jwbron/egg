"""Tests for the deterministic protected-root renderer (slice-4, task-4-2).

#3200 / slice-4 ("Protected root: deterministic, byte-stable, cacheable").
The coder (task-4-1) lands a renderer that assembles the small, resident,
role-parameterized protected root in a FIXED four-section order:

  (a) role contract;
  (b) task anchor (``compose_task_description``, #3163);
  (c) the four #3189 deterministic anchors derived in slice-3
      (:class:`egg_anchor.models.BRCDerivedAnchors`: last-reviewed SHA per
      producer, latest verdict per edge, open NACKs, conditional-ACK
      obligations);
  (d) non-negotiable directives.

The renderer emits STABLE BYTES: sorted keys, bounded sections, hard
per-section caps, and NO timestamps / sequence numbers / nondeterministic
ordering — so an identical anchor input yields identical bytes across runs and
the root is safely cacheable + resident.

This file asserts the four task-4-2 acceptance properties:

  1. byte-stability     — identical input -> identical bytes;
  2. per-section caps   — oversized sections truncated at the documented cap;
  3. sort-stability     — key/element ordering stable regardless of input order;
  4. role-parameterized — two roles render distinct-but-each-stable roots.

The renderer signature (``render_protected_root(*, role, role_contract,
task_description, derived, directives, caps)``) is merged, so these tests call
it directly with keyword arguments — a renderer regression fails loudly here
rather than being masked by a skip.
"""

from __future__ import annotations

from typing import Any

from egg_anchor.models import (
    BRCDerivedAnchors,
    ConditionalAckObligation,
    OpenNack,
    ReviewEdgeVerdict,
    ReviewVerdict,
)
from egg_anchor.protected_root import RootCaps, render_protected_root

# ---------------------------------------------------------------------------
# Canonical fixture inputs. Sentinels are unique so each of the four sections
# can be located in the rendered root.
# ---------------------------------------------------------------------------

ROLE_A = "reviewer_code"
ROLE_B = "coder"

ROLE_CONTRACT_SENTINEL = "ZZ-ROLE-CONTRACT-SECTION-A-ZZ"
TASK_ANCHOR_SENTINEL = "ZZ-TASK-ANCHOR-SECTION-B-ZZ"
DIRECTIVE_SENTINEL = "ZZ-NON-NEGOTIABLE-DIRECTIVE-SECTION-D-ZZ"

SHA_CODER = "abc1230000000000000000000000000000000def"
SHA_TESTER = "fed3210000000000000000000000000000000cba"


def _task_anchor() -> str:
    """A faithful task anchor (compose_task_description, #3163) when importable."""
    try:
        from egg_contracts.loader import compose_task_description
    except ImportError:
        return f"This pipeline's task is GitHub issue #3200. {TASK_ANCHOR_SENTINEL}"
    composed = compose_task_description(
        description=f"{TASK_ANCHOR_SENTINEL} Protected root context discipline.",
        issue_number=3200,
        issue_url="https://github.com/jwbron/egg/issues/3200",
    )
    return composed or f"{TASK_ANCHOR_SENTINEL}"


def _anchors() -> BRCDerivedAnchors:
    """A realistic, fully-populated set of #3189 anchors (section c)."""
    return BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER, "tester": SHA_TESTER},
        latest_verdicts=[
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="coder",
                verdict=ReviewVerdict.NACK,
                version=2,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_security",
                producer="coder",
                verdict=ReviewVerdict.CONDITIONAL_ACK,
                version=2,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="tester",
                verdict=ReviewVerdict.ACK,
                version=1,
                reviewed_sha=SHA_TESTER,
            ),
        ],
        open_nacks=[
            OpenNack(
                reviewer="reviewer_code",
                producer="coder",
                version=2,
                reason="missing guard on the ResultMessage branch",
            ),
        ],
        conditional_ack_obligations=[
            ConditionalAckObligation(
                reviewer="reviewer_security",
                producer="coder",
                version=2,
                condition="git mv old/path new/path before merge",
                resolved=False,
            ),
        ],
    )


def _canonical_inputs() -> dict[str, Any]:
    return {
        "role": ROLE_A,
        "role_contract": (
            f"{ROLE_CONTRACT_SENTINEL} {ROLE_A}: review the diff for correctness "
            "and never drop an open NACK obligation."
        ),
        "task_description": _task_anchor(),
        "derived": _anchors(),
        "directives": [
            f"{DIRECTIVE_SENTINEL} The deterministic anchor layer is authoritative.",
            "Re-derive anchors on reseed; never re-review a settled SHA.",
        ],
    }


def _render(**overrides: Any) -> str:
    """Render the protected root from the canonical inputs, with overrides."""
    inputs = _canonical_inputs()
    inputs.update(overrides)
    return render_protected_root(**inputs)


# ---------------------------------------------------------------------------
# 1. Byte-stability — identical input -> identical bytes, all four sections.
# ---------------------------------------------------------------------------


def test_render_is_byte_stable_across_identical_renders() -> None:
    """Two renders of identical input produce byte-for-byte identical output."""
    first = _render()
    second = _render()
    assert first == second
    assert isinstance(first, str)
    assert first, "protected root rendered empty"


def test_render_contains_all_four_sections_in_fixed_order() -> None:
    """All four sections render; the deterministic anchor layer is present."""
    text = _render()
    # (a) role contract, (b) task anchor, (c) #3189 anchors, (d) directives.
    assert ROLE_CONTRACT_SENTINEL in text
    assert TASK_ANCHOR_SENTINEL in text
    assert SHA_CODER in text, "section (c): last-reviewed SHA not rendered"
    assert "missing guard" in text, "section (c): open NACK reason not rendered"
    assert DIRECTIVE_SENTINEL in text

    # Fixed order a -> b -> c -> d.
    markers = [
        ROLE_CONTRACT_SENTINEL,
        TASK_ANCHOR_SENTINEL,
        SHA_CODER,
        DIRECTIVE_SENTINEL,
    ]
    positions = [text.find(m) for m in markers]
    assert all(p >= 0 for p in positions), f"a section sentinel is missing: {positions}"
    assert positions == sorted(positions), (
        f"protected-root sections are not in the fixed a->b->c->d order: {positions}"
    )


# ---------------------------------------------------------------------------
# 2. Per-section caps — oversized sections truncated at a hard cap.
# ---------------------------------------------------------------------------

_OVERSIZE = 4_000_000  # ~4 MB of filler — far past any reasonable section cap.


def test_oversized_freetext_section_is_truncated() -> None:
    """A multi-megabyte role-contract section is hard-capped, not inlined whole."""
    head = "OVERSIZE-HEAD-SENTINEL"
    giant = f"{head} " + ("x" * _OVERSIZE)
    text = _render(role_contract=giant)
    assert giant not in text, "oversized section was inlined verbatim (no cap)"
    # A hard cap keeps the whole root far smaller than the raw oversized input.
    assert len(text) < _OVERSIZE / 20, (
        f"rendered root len {len(text)} not bounded below a hard per-section cap"
    )
    # Truncated, not omitted: the head of the section survives.
    assert head in text, "capped section dropped entirely instead of truncating"


def test_oversized_anchor_section_is_truncated() -> None:
    """A flood of open NACKs (section c) is bounded by the per-section cap."""
    flood = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER},
        open_nacks=[
            OpenNack(
                reviewer=f"reviewer_{i}",
                producer="coder",
                version=2,
                reason="y" * 1000,
            )
            for i in range(5000)
        ],
    )
    text = _render(derived=flood)
    # 5000 * ~1KB reasons ~= 5MB of raw anchor content; the cap must bound it.
    assert len(text) < 5_000_000 / 20, (
        f"anchor section len {len(text)} not bounded by a hard per-section cap"
    )


# ---------------------------------------------------------------------------
# 3. Sort-stability — element/key ordering does not depend on input order.
# ---------------------------------------------------------------------------


def test_render_is_stable_regardless_of_input_ordering() -> None:
    """Permuting dict-key and list-element order yields identical bytes.

    A byte-stable protected root must sort its own content rather than trust
    callers to pre-sort — so the same logical anchors in a different order
    render identically.
    """
    forward = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER, "tester": SHA_TESTER},
        latest_verdicts=[
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="coder",
                verdict=ReviewVerdict.NACK,
                version=2,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="tester",
                verdict=ReviewVerdict.ACK,
                version=1,
                reviewed_sha=SHA_TESTER,
            ),
        ],
        open_nacks=[
            OpenNack(reviewer="reviewer_a", producer="coder", version=2, reason="alpha"),
            OpenNack(reviewer="reviewer_b", producer="coder", version=2, reason="beta"),
        ],
    )
    # Same logical content, reversed dict-insertion and list order.
    reversed_anchors = BRCDerivedAnchors(
        last_reviewed_sha={"tester": SHA_TESTER, "coder": SHA_CODER},
        latest_verdicts=list(reversed(forward.latest_verdicts)),
        open_nacks=list(reversed(forward.open_nacks)),
    )

    assert _render(derived=forward) == _render(derived=reversed_anchors)


# ---------------------------------------------------------------------------
# 4. Role-parameterization — distinct roots per role, each individually stable.
# ---------------------------------------------------------------------------


def test_two_roles_render_distinct_but_each_stable_roots() -> None:
    """Distinct roles -> distinct roots; each role's root is byte-stable."""
    role_a_contract = f"{ROLE_CONTRACT_SENTINEL} {ROLE_A} contract."
    role_b_contract = f"{ROLE_CONTRACT_SENTINEL} {ROLE_B} contract."

    a_first = _render(role=ROLE_A, role_contract=role_a_contract)
    a_second = _render(role=ROLE_A, role_contract=role_a_contract)
    b_first = _render(role=ROLE_B, role_contract=role_b_contract)
    b_second = _render(role=ROLE_B, role_contract=role_b_contract)

    # Each role's root is individually byte-stable.
    assert a_first == a_second
    assert b_first == b_second
    # The two roles render distinct roots (role-parameterized).
    assert a_first != b_first

    assert ROLE_A in a_first
    assert ROLE_B in b_first


# ---------------------------------------------------------------------------
# 5. Robustness guards — exercise the tiebreaker keys and the sub-marker
#    `_truncate` clamp directly, so the determinism they protect is locked in
#    even though the upstream deriver currently makes both cases unreachable.
# ---------------------------------------------------------------------------


def test_duplicate_edge_entries_are_byte_stable_via_tiebreakers() -> None:
    """>1 entry per (producer, reviewer) edge renders identically regardless of order.

    The primary sort key is ``(producer, reviewer)``; if the derived layer ever
    emits multiple entries for the same edge, only the ``version``/``reason``/
    ``condition``/``resolved`` tiebreakers keep the render byte-stable. Permuting
    the input order of such duplicate-edge entries must not change the output —
    this fails loudly if a tiebreaker is dropped from a sort key.
    """
    forward = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER},
        latest_verdicts=[
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="coder",
                verdict=ReviewVerdict.NACK,
                version=1,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="coder",
                verdict=ReviewVerdict.ACK,
                version=2,
                reviewed_sha=SHA_TESTER,
            ),
        ],
        open_nacks=[
            OpenNack(reviewer="reviewer_code", producer="coder", version=1, reason="alpha"),
            OpenNack(reviewer="reviewer_code", producer="coder", version=2, reason="beta"),
        ],
        conditional_ack_obligations=[
            ConditionalAckObligation(
                reviewer="reviewer_security",
                producer="coder",
                version=1,
                condition="first condition",
                resolved=False,
            ),
            ConditionalAckObligation(
                reviewer="reviewer_security",
                producer="coder",
                version=2,
                condition="second condition",
                resolved=True,
            ),
        ],
    )
    # Same logical content, every duplicate-edge list reversed.
    reversed_anchors = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER},
        latest_verdicts=list(reversed(forward.latest_verdicts)),
        open_nacks=list(reversed(forward.open_nacks)),
        conditional_ack_obligations=list(reversed(forward.conditional_ack_obligations)),
    )

    assert _render(derived=forward) == _render(derived=reversed_anchors)


def test_sub_marker_cap_hard_trims_without_marker() -> None:
    """A cap smaller than the truncation marker hard-trims to the cap, never longer.

    With ``reason_chars`` below the marker length, ``_truncate`` cannot fit its
    marker, so it hard-trims the value to the cap rather than returning the
    (longer) marker alone — keeping the documented "hard ceiling including the
    marker" guarantee for any custom :class:`RootCaps`.
    """
    flood = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER},
        open_nacks=[
            OpenNack(
                reviewer="reviewer_code",
                producer="coder",
                version=2,
                reason="abcdefghij" * 10,  # 100 chars, far past the 5-char cap.
            ),
        ],
    )
    text = _render(derived=flood, caps=RootCaps(reason_chars=5))
    # The reason is hard-trimmed to exactly 5 chars, with no marker appended
    # (the cap is smaller than the marker, so the marker cannot be added).
    assert "abcde" in text
    assert "abcdef" not in text
    assert "…[truncated]" not in text
