"""Tests for the ``has_contract`` parameter of ``get_roles_for_phase()``.

CUSTOM+PR pipelines (#1762) run without an upstream SDLC contract. In that
mode the ``reviewer_contract`` reviewer has no artifacts to verify, so it must
be filtered out of the phase roster so BRC does not wait on an agent that
cannot ACK. These tests lock in that behavior.
"""

from __future__ import annotations

import pytest
from egg_contracts.agent_roles import EGG_REPO, AgentRole, get_roles_for_phase


class TestImplementPhaseHasContractFalse:
    def test_excludes_reviewer_contract(self):
        roles = get_roles_for_phase("implement", has_contract=False)
        assert AgentRole.REVIEWER_CONTRACT not in roles

    def test_still_includes_reviewer_code(self):
        # Only reviewer_contract is filtered by has_contract=False; other
        # implement-phase reviewers must remain.
        roles = get_roles_for_phase("implement", has_contract=False)
        assert AgentRole.REVIEWER_CODE in roles

    def test_still_includes_producers(self):
        # Producers (coder/tester/documenter) are unaffected by has_contract.
        roles = get_roles_for_phase("implement", has_contract=False)
        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles


class TestImplementPhaseHasContractTrue:
    def test_includes_reviewer_contract_with_egg_repo(self):
        roles = get_roles_for_phase("implement", has_contract=True, repo=EGG_REPO)
        assert AgentRole.REVIEWER_CONTRACT in roles

    def test_includes_reviewer_contract_with_none_repo(self):
        # repo=None means no repo filtering; reviewer_contract must appear.
        roles = get_roles_for_phase("implement", has_contract=True, repo=None)
        assert AgentRole.REVIEWER_CONTRACT in roles

    def test_default_has_contract_is_true(self):
        # Backward compatibility: default should yield the same roster as
        # explicitly passing has_contract=True.
        default_roles = get_roles_for_phase("implement")
        explicit_roles = get_roles_for_phase("implement", has_contract=True)
        assert default_roles == explicit_roles
        assert AgentRole.REVIEWER_CONTRACT in default_roles


class TestImplementPhaseRegressionLocks:
    def test_reviewer_agent_design_not_in_implement_roster_true_egg(self):
        # REVIEWER_AGENT_DESIGN is a refine-phase reviewer, not implement.
        # Regardless of has_contract or repo, it must not appear in the
        # implement-phase roster.
        roles = get_roles_for_phase("implement", has_contract=True, repo=EGG_REPO)
        assert AgentRole.REVIEWER_AGENT_DESIGN not in roles

    def test_reviewer_agent_design_not_in_implement_roster_false_egg(self):
        roles = get_roles_for_phase("implement", has_contract=False, repo=EGG_REPO)
        assert AgentRole.REVIEWER_AGENT_DESIGN not in roles

    def test_reviewer_agent_design_not_in_implement_roster_none_repo(self):
        roles_true = get_roles_for_phase("implement", has_contract=True, repo=None)
        roles_false = get_roles_for_phase("implement", has_contract=False, repo=None)
        assert AgentRole.REVIEWER_AGENT_DESIGN not in roles_true
        assert AgentRole.REVIEWER_AGENT_DESIGN not in roles_false


class TestOtherPhasesUnaffected:
    def test_plan_phase_identical_regardless_of_has_contract(self):
        # reviewer_contract is only present in the implement phase, so toggling
        # has_contract should not change the plan-phase roster at all.
        plan_true = get_roles_for_phase("plan", has_contract=True)
        plan_false = get_roles_for_phase("plan", has_contract=False)
        assert plan_true == plan_false

    def test_refine_phase_reviewer_contract_never_present(self):
        # Sanity: reviewer_contract also isn't in refine-phase reviewers, so
        # has_contract=False leaves that roster unchanged as well.
        refine_true = get_roles_for_phase("refine", has_contract=True, repo=EGG_REPO)
        refine_false = get_roles_for_phase("refine", has_contract=False, repo=EGG_REPO)
        assert refine_true == refine_false
        assert AgentRole.REVIEWER_CONTRACT not in refine_true
        assert AgentRole.REVIEWER_CONTRACT not in refine_false


class TestErrorHandling:
    def test_unknown_phase_still_raises_value_error(self):
        # has_contract=False must not swallow the ValueError for unknown
        # phases — that error path is independent of reviewer filtering.
        with pytest.raises(ValueError, match="nonexistent"):
            get_roles_for_phase("nonexistent", has_contract=False)


class TestIncludeReviewersFalseMakesFilterMoot:
    def test_include_reviewers_false_with_has_contract_false(self):
        # When reviewers aren't added at all, has_contract has nothing to
        # filter — the result must equal the producers-only roster.
        roles = get_roles_for_phase("implement", include_reviewers=False, has_contract=False)
        assert roles == [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
        ]
        assert AgentRole.REVIEWER_CONTRACT not in roles
        assert AgentRole.REVIEWER_CODE not in roles

    def test_include_reviewers_false_true_vs_false_identical(self):
        # With include_reviewers=False, has_contract should be a no-op.
        roles_true = get_roles_for_phase("implement", include_reviewers=False, has_contract=True)
        roles_false = get_roles_for_phase("implement", include_reviewers=False, has_contract=False)
        assert roles_true == roles_false
