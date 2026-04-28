"""Tests for ``plan_parser.validate_forest`` (#2137 TASK-2-5).

The slice DAG must be a forest — every slice has at most one DAG
parent — so the stacked-PR machinery has a single base per child PR.
``validate_forest`` is wired into ``_populate_contract_from_plan`` to
reject offending plans at ingestion (refine-phase decision-18).

These tests cover:

* Valid forests (zero parents, single-parent chain, sibling fan-out)
  return an empty error list.
* Multi-parent slices are rejected with a structured error that names
  the offender, its parents, and points at the
  ``serialized_chain_order`` remediation.
* A diamond DAG (slice with two real parents) surfaces ONE error per
  offending node — not duplicate errors per parent.
* Duplicate slice ids are flagged independently of the parent count.
* Unknown dependencies are silently dropped (they're a different
  ingestion error and would otherwise drown the forest signal).
* Empty input returns empty errors (idempotent on edge cases).
"""

from __future__ import annotations

import pytest

from egg_contracts.models import Slice
from egg_contracts.plan_parser import validate_forest


def _slice(id_: str, deps: list[str] | None = None) -> Slice:
    return Slice(id=id_, name=f"slice {id_}", dependencies=deps or [])


class TestValidForests:
    """Forests that should pass validation cleanly."""

    def test_empty_input(self) -> None:
        assert validate_forest([]) == []

    def test_single_root(self) -> None:
        assert validate_forest([_slice("slice-1")]) == []

    def test_two_disjoint_roots(self) -> None:
        # Two independent root slices form a forest of two trees.
        slices = [_slice("slice-1"), _slice("slice-2")]
        assert validate_forest(slices) == []

    def test_linear_chain(self) -> None:
        # slice-1 → slice-2 → slice-3 (each child has exactly one parent).
        slices = [
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-2"]),
        ]
        assert validate_forest(slices) == []

    def test_root_with_two_children_is_a_tree_not_a_diamond(self) -> None:
        # Both children point to the same root, but each child has
        # exactly one parent — that's a tree, which IS a forest.
        slices = [
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-1"]),
        ]
        assert validate_forest(slices) == []


class TestMultiParentRejection:
    """Multi-parent slices break the forest invariant."""

    def test_diamond_surfaces_single_error(self) -> None:
        # slice-1 → slice-2, slice-1 → slice-3, slice-2 + slice-3 → slice-4
        slices = [
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-1"]),
            _slice("slice-4", ["slice-2", "slice-3"]),
        ]
        errors = validate_forest(slices)
        assert len(errors) == 1
        msg = errors[0]
        assert "slice-4" in msg
        # Parents must be named in the message so the planner can act
        # on the structured error without grepping the full diff.
        assert "slice-2" in msg
        assert "slice-3" in msg
        # Remediation pointer must reference the refine-phase decision.
        assert "serialized_chain_order" in msg

    def test_three_parents_count_appears_in_error(self) -> None:
        slices = [
            _slice("slice-1"),
            _slice("slice-2"),
            _slice("slice-3"),
            _slice("slice-4", ["slice-1", "slice-2", "slice-3"]),
        ]
        errors = validate_forest(slices)
        assert len(errors) == 1
        # Refiner shouldn't have to count parents themselves; the
        # structured error states the count explicitly.
        assert "3" in errors[0]

    def test_each_offender_gets_its_own_error(self) -> None:
        # Two independent diamond offenders → two errors. The validator
        # must surface them all in one pass so the planner can fix
        # everything in one re-proposal.
        slices = [
            _slice("slice-1"),
            _slice("slice-2"),
            _slice("slice-3", ["slice-1", "slice-2"]),  # offender A
            _slice("slice-4", ["slice-1", "slice-2"]),  # offender B
        ]
        errors = validate_forest(slices)
        assert len(errors) == 2
        offenders = [e for e in errors if "slice-3" in e or "slice-4" in e]
        assert len(offenders) == 2


class TestUnknownDepsAreSilentlyDropped:
    """Unknown deps are a separate error — the forest validator only
    counts dependencies that resolve to real sibling slice ids."""

    def test_one_real_one_unknown_dep_is_not_a_diamond(self) -> None:
        slices = [
            _slice("slice-1"),
            _slice("slice-2", ["slice-1", "slice-99"]),
        ]
        # slice-99 is unknown — only slice-1 counts, so the parent
        # count is 1 and validate_forest returns clean.
        assert validate_forest(slices) == []


class TestDuplicateIds:
    """Duplicate slice ids are surfaced as a distinct error."""

    def test_duplicate_id_flagged(self) -> None:
        slices = [_slice("slice-1"), _slice("slice-1")]
        errors = validate_forest(slices)
        # At least one error mentions the duplicate id.
        assert any("slice-1" in e and "duplicate" in e.lower() for e in errors)


class TestCycleDetection:
    """Cyclic slice DAGs are rejected.

    Per reviewer_code's non-blocking observation on the tester v1 ACK
    (and the corresponding coder NACK), a cyclic plan would otherwise
    deadlock the orchestrator silently. The xfail markers below pin
    the post-fix invariants — they fail today (validate_forest does not
    yet call has_cycle) and turn into regression guards once the coder
    lands the fix. ``strict=True`` flags the XPASS as a signal to drop
    the marker.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Coder gap (reviewer_code non-blocking + coder NACK #6): "
            "validate_forest does not yet call has_cycle. A 2-cycle "
            "(slice-1 -> slice-2 -> slice-1) must be rejected at plan "
            "ingestion to prevent silent orchestrator deadlock. Test "
            "passes once the coder wires has_cycle() into validate_forest."
        ),
    )
    def test_two_cycle_rejected(self) -> None:
        slices = [
            _slice("slice-1", ["slice-2"]),
            _slice("slice-2", ["slice-1"]),
        ]
        errors = validate_forest(slices)
        assert errors, "Cyclic plan must produce at least one error"
        assert any("cycle" in e.lower() or "cyclic" in e.lower() for e in errors)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Coder gap (reviewer_code non-blocking + coder NACK #6): "
            "validate_forest does not yet detect self-loops "
            "(slice-1 depending on itself). Test passes once the coder "
            "wires has_cycle() into validate_forest."
        ),
    )
    def test_self_loop_rejected(self) -> None:
        slices = [_slice("slice-1", ["slice-1"])]
        errors = validate_forest(slices)
        assert errors, "Self-loop must produce at least one error"
        assert any("cycle" in e.lower() or "self" in e.lower() for e in errors)


class TestRealisticPlan:
    """End-to-end-ish: a sliced plan with serialized chain works."""

    def test_serialized_chain_resolves_a_diamond(self) -> None:
        # The fix for the diamond above is: chain slice-2 onto slice-3
        # so slice-4's only dependency is slice-3 (with slice-2 chained
        # before it). After the planner applies the
        # serialized_chain_order rule the structure becomes a forest.
        slices = [
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-2"]),
            _slice("slice-4", ["slice-3"]),
        ]
        assert validate_forest(slices) == []
