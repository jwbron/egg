"""Tests for ``validate_roles_for_custom_phase`` (#1762).

The helper in ``shared/egg_contracts/agent_roles.py`` powers the
``run_agent_task`` MCP primitive. It validates and resolves a user-supplied
role subset against a phase's roster, rejecting degenerate rosters that
would deadlock BRC or pull in cross-phase roles.

This test module locks in the contract:

    (resolved_roles, None)      on success
    (None, error_reason)         on failure

Where ``error_reason`` is one of the fixed strings listed in the helper's
docstring. The strings are the wire contract for the route-level 400
responses planned for Phase 2, so we assert on the literal values.
"""

from __future__ import annotations

import pytest
from egg_contracts.agent_roles import (
    EGG_REPO,
    AgentRole,
    get_roles_for_phase,
    validate_roles_for_custom_phase,
)

# ---------------------------------------------------------------------------
# Default-roster fallback (None / empty list → full phase roster)
# ---------------------------------------------------------------------------


class TestDefaultRosterFallback:
    """When ``requested_roles`` is None or [] the helper must return the
    full default roster for that phase — identical to
    ``get_roles_for_phase`` after ``has_contract`` / repo filtering."""

    def test_none_returns_full_implement_roster_egg(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement", None, EGG_REPO, has_contract=True
        )
        assert err is None
        assert resolved == get_roles_for_phase(
            "implement", include_reviewers=True, repo=EGG_REPO, has_contract=True
        )

    def test_empty_list_returns_full_roster(self):
        """Empty list is treated the same as None — the helper docstring
        says callers normally normalise None↔[]."""
        resolved, err = validate_roles_for_custom_phase("plan", [], EGG_REPO, has_contract=True)
        assert err is None
        assert resolved == get_roles_for_phase(
            "plan", include_reviewers=True, repo=EGG_REPO, has_contract=True
        )

    def test_none_respects_has_contract_false_filter(self):
        """Default roster must drop ``reviewer_contract`` when
        has_contract=False."""
        resolved, err = validate_roles_for_custom_phase(
            "implement", None, EGG_REPO, has_contract=False
        )
        assert err is None
        assert AgentRole.REVIEWER_CONTRACT not in resolved
        assert AgentRole.REVIEWER_CODE in resolved

    def test_none_respects_non_egg_repo_filter(self):
        """Default roster must drop ``reviewer_agent_design`` for
        non-egg repos (egg-only reviewer)."""
        resolved, err = validate_roles_for_custom_phase(
            "refine", None, "other/repo", has_contract=True
        )
        assert err is None
        assert AgentRole.REVIEWER_AGENT_DESIGN not in resolved
        assert AgentRole.REFINER in resolved

    def test_none_for_refine_phase(self):
        resolved, err = validate_roles_for_custom_phase("refine", None, EGG_REPO, has_contract=True)
        assert err is None
        assert AgentRole.REFINER in resolved


# ---------------------------------------------------------------------------
# Invalid phase
# ---------------------------------------------------------------------------


class TestInvalidPhase:
    def test_unknown_phase_returns_invalid_phase_error(self):
        resolved, err = validate_roles_for_custom_phase(
            "pr", [AgentRole.CODER.value], EGG_REPO, has_contract=True
        )
        assert resolved is None
        assert err == "invalid_phase"

    def test_empty_phase_string_is_invalid(self):
        resolved, err = validate_roles_for_custom_phase(
            "", [AgentRole.CODER.value], EGG_REPO, has_contract=True
        )
        assert resolved is None
        assert err == "invalid_phase"

    def test_unknown_phase_with_none_roles_also_errors(self):
        """The ``not requested_roles`` branch still has to reject
        invalid phases — otherwise we'd leak a ValueError out of
        get_roles_for_phase."""
        resolved, err = validate_roles_for_custom_phase(
            "nonexistent", None, EGG_REPO, has_contract=True
        )
        assert resolved is None
        assert err == "invalid_phase"


# ---------------------------------------------------------------------------
# Happy-path subset validation
# ---------------------------------------------------------------------------


class TestValidSubset:
    def test_single_producer_implement(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement", [AgentRole.CODER.value], EGG_REPO, has_contract=True
        )
        assert err is None
        assert resolved == [AgentRole.CODER]

    def test_producer_plus_reviewer(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, AgentRole.REVIEWER_CODE.value],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        assert set(resolved) == {AgentRole.CODER, AgentRole.REVIEWER_CODE}

    def test_all_producers_no_reviewer(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [
                AgentRole.CODER.value,
                AgentRole.TESTER.value,
                AgentRole.DOCUMENTER.value,
            ],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        assert set(resolved) == {
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
        }

    def test_plan_phase_subset(self):
        resolved, err = validate_roles_for_custom_phase(
            "plan",
            [AgentRole.TASK_PLANNER.value, AgentRole.REVIEWER_PLAN.value],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        assert set(resolved) == {AgentRole.TASK_PLANNER, AgentRole.REVIEWER_PLAN}

    def test_refine_phase_subset_with_egg_reviewer(self):
        resolved, err = validate_roles_for_custom_phase(
            "refine",
            [AgentRole.REFINER.value, AgentRole.REVIEWER_AGENT_DESIGN.value],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        assert set(resolved) == {
            AgentRole.REFINER,
            AgentRole.REVIEWER_AGENT_DESIGN,
        }

    def test_result_is_list_of_agent_role_enum(self):
        """Return value must be strongly-typed ``AgentRole`` members so
        callers can use ``.value`` safely for persistence."""
        resolved, err = validate_roles_for_custom_phase(
            "implement", [AgentRole.CODER.value], EGG_REPO, has_contract=True
        )
        assert err is None
        assert all(isinstance(r, AgentRole) for r in resolved)

    def test_duplicates_are_deduplicated(self):
        """Doc: 'preserves the canonical ordering ... free of duplicates'."""
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, AgentRole.CODER.value],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        assert resolved == [AgentRole.CODER]

    def test_canonical_ordering_preserved(self):
        """Result follows ``get_roles_for_phase()`` canonical ordering
        so persisted rosters round-trip predictably."""
        # Pass reviewer before producer deliberately — helper should re-order.
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.REVIEWER_CODE.value, AgentRole.CODER.value],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        canonical = get_roles_for_phase(
            "implement", include_reviewers=True, repo=EGG_REPO, has_contract=True
        )
        # Check that resolved maintains canonical order among its members.
        resolved_indices = [canonical.index(r) for r in resolved]
        assert resolved_indices == sorted(resolved_indices)


# ---------------------------------------------------------------------------
# invalid_roles errors (unknown values, cross-phase reviewers, etc.)
# ---------------------------------------------------------------------------


class TestInvalidRoles:
    def test_unknown_role_value(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, "not_a_real_role"],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "invalid_roles"

    def test_cross_phase_reviewer_plan_in_implement(self):
        """``reviewer_plan`` is a plan-phase reviewer — not selectable
        when phase=implement."""
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, AgentRole.REVIEWER_PLAN.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "invalid_roles"

    def test_cross_phase_producer_refiner_in_implement(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.REFINER.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "invalid_roles"

    def test_reviewer_agent_design_non_egg_repo(self):
        """``reviewer_agent_design`` is egg-only — rejected for non-egg
        repos even when phase=refine."""
        resolved, err = validate_roles_for_custom_phase(
            "refine",
            [AgentRole.REFINER.value, AgentRole.REVIEWER_AGENT_DESIGN.value],
            "other/repo",
            has_contract=True,
        )
        assert resolved is None
        assert err == "invalid_roles"

    def test_producer_from_another_phase_rejected(self):
        """``architect`` is a plan-phase producer — not selectable for
        implement."""
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, AgentRole.ARCHITECT.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "invalid_roles"


# ---------------------------------------------------------------------------
# cross_phase_role error (overseer/autofixer/conflict_resolver/inspector)
# ---------------------------------------------------------------------------


class TestCrossPhaseRoles:
    """The utility/interface roles are never selectable via
    run_agent_task, regardless of phase."""

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.OVERSEER,
            AgentRole.AUTOFIXER,
            AgentRole.CONFLICT_RESOLVER,
            AgentRole.INSPECTOR,
        ],
    )
    def test_cross_phase_role_rejected(self, role):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, role.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "cross_phase_role"

    @pytest.mark.parametrize("phase", ["refine", "plan", "implement"])
    def test_cross_phase_role_rejected_in_all_phases(self, phase):
        # Use the phase's primary producer to isolate the cross-phase signal.
        producer_for_phase = {
            "refine": AgentRole.REFINER,
            "plan": AgentRole.TASK_PLANNER,
            "implement": AgentRole.CODER,
        }[phase]
        resolved, err = validate_roles_for_custom_phase(
            phase,
            [producer_for_phase.value, AgentRole.OVERSEER.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "cross_phase_role"

    def test_overseer_alone_also_rejected(self):
        """Overseer-only (no producer) still trips the cross-phase check
        — the more specific error must take precedence over
        reviewer_only_roster."""
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.OVERSEER.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "cross_phase_role"


# ---------------------------------------------------------------------------
# reviewer_only_roster error (BRC deadlock guard)
# ---------------------------------------------------------------------------


class TestReviewerOnlyRoster:
    """Reviewer-only rosters deadlock BRC because reviewers ACK/NACK
    producers, and there's no producer in the roster. The helper must
    return ``reviewer_only_roster`` for these cases (decision-6)."""

    def test_single_reviewer_implement(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.REVIEWER_CODE.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "reviewer_only_roster"

    def test_multiple_reviewers_no_producer(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.REVIEWER_CODE.value, AgentRole.REVIEWER_CONTRACT.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "reviewer_only_roster"

    def test_plan_reviewer_only(self):
        resolved, err = validate_roles_for_custom_phase(
            "plan",
            [AgentRole.REVIEWER_PLAN.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "reviewer_only_roster"

    def test_refine_reviewer_only(self):
        resolved, err = validate_roles_for_custom_phase(
            "refine",
            [AgentRole.REVIEWER_REFINE.value, AgentRole.REVIEWER_AGENT_DESIGN.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert err == "reviewer_only_roster"


# ---------------------------------------------------------------------------
# reviewer_contract_without_artifact
# ---------------------------------------------------------------------------


class TestReviewerContractWithoutArtifact:
    """``reviewer_contract`` verifies an upstream SDLC contract; when
    has_contract=False it has nothing to verify and is rejected."""

    def test_reviewer_contract_with_has_contract_false(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, AgentRole.REVIEWER_CONTRACT.value],
            EGG_REPO,
            has_contract=False,
        )
        assert resolved is None
        assert err == "reviewer_contract_without_artifact"

    def test_reviewer_contract_with_has_contract_true_allowed(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.CODER.value, AgentRole.REVIEWER_CONTRACT.value],
            EGG_REPO,
            has_contract=True,
        )
        assert err is None
        assert AgentRole.REVIEWER_CONTRACT in resolved


# ---------------------------------------------------------------------------
# Edge cases / regressions
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_repo_none_allows_egg_only_reviewer(self):
        """``repo=None`` disables the egg-only filter, mirroring
        get_roles_for_phase semantics."""
        resolved, err = validate_roles_for_custom_phase(
            "refine",
            [AgentRole.REFINER.value, AgentRole.REVIEWER_AGENT_DESIGN.value],
            None,
            has_contract=True,
        )
        assert err is None
        assert AgentRole.REVIEWER_AGENT_DESIGN in resolved

    def test_has_contract_false_plan_phase(self):
        """plan phase has no reviewer_contract; has_contract=False is
        a no-op there."""
        resolved, err = validate_roles_for_custom_phase(
            "plan",
            [AgentRole.TASK_PLANNER.value, AgentRole.REVIEWER_PLAN.value],
            EGG_REPO,
            has_contract=False,
        )
        assert err is None
        assert set(resolved) == {AgentRole.TASK_PLANNER, AgentRole.REVIEWER_PLAN}

    def test_case_sensitive_role_values(self):
        """``AgentRole("CODER")`` would raise — so the helper should
        reject upper-case strings as invalid."""
        resolved, err = validate_roles_for_custom_phase(
            "implement", ["CODER"], EGG_REPO, has_contract=True
        )
        assert resolved is None
        assert err == "invalid_roles"

    def test_whitespace_in_role_value_rejected(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement", [" coder "], EGG_REPO, has_contract=True
        )
        assert resolved is None
        assert err == "invalid_roles"

    def test_error_return_shape_on_success(self):
        """Tuple shape must always be consistent — the second element
        is ``None`` on success, a non-empty string on failure."""
        resolved, err = validate_roles_for_custom_phase(
            "implement", [AgentRole.CODER.value], EGG_REPO, has_contract=True
        )
        assert err is None
        assert isinstance(resolved, list)
        assert all(isinstance(r, AgentRole) for r in resolved)

    def test_error_return_shape_on_failure(self):
        resolved, err = validate_roles_for_custom_phase(
            "implement",
            [AgentRole.REVIEWER_CODE.value],
            EGG_REPO,
            has_contract=True,
        )
        assert resolved is None
        assert isinstance(err, str) and err
