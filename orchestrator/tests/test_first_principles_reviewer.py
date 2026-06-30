"""Tests for the ``first_principles_reviewer`` role and its accept-path.

Covers the additive wiring (role registry, refine review graph, gateway
patterns, the broadened ``decisions.*`` capability), the prompt single-source
invariant (the criteria interpolate the exact accept-path option labels), and
the resolution-hook dispatch logic (proceed / phase-guard / adopt-no-artifact).

The integration the hook drives end-to-end — the durable seed rewrite and the
refine re-run — is validated by a live proving run, not here; these tests pin
the pure, unit-testable surface.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestRoleWiring:
    def test_role_in_refine_reviewers_all_repos(self) -> None:
        from egg_contracts.agent_roles import AgentRole, get_roles_for_phase

        egg = get_roles_for_phase("refine", repo="jwbron/egg")
        other = get_roles_for_phase("refine", repo="acme/widgets")
        assert AgentRole.FIRST_PRINCIPLES_REVIEWER in egg
        # Not egg-only — questioning the premise is universal.
        assert AgentRole.FIRST_PRINCIPLES_REVIEWER in other

    def test_contract_role_is_reviewer(self) -> None:
        from egg_contracts.agent_roles import AgentRole, get_contract_role
        from egg_contracts.roles import Role

        assert get_contract_role(AgentRole.FIRST_PRINCIPLES_REVIEWER) == Role.REVIEWER

    def test_role_definition_is_review_no_source_writes(self) -> None:
        from egg_contracts.agent_roles import AgentCategory, AgentRole, get_role_definition

        defn = get_role_definition(AgentRole.FIRST_PRINCIPLES_REVIEWER)
        assert defn.category == AgentCategory.REVIEW
        assert ".egg-state/agent-outputs/" in defn.file_access.allowed_write
        # It must never touch source or the contract directly.
        assert not defn.file_access.can_write("orchestrator/foo.py")
        assert not defn.file_access.can_write(".egg-state/contracts/x.json")

    def test_gateway_patterns_registered(self) -> None:
        from egg_contracts.agent_roles import AgentRole
        from egg_restrictions.patterns import get_agent_patterns_for_repo

        pats = get_agent_patterns_for_repo()
        assert AgentRole.FIRST_PRINCIPLES_REVIEWER in pats


class TestReviewGraph:
    def test_critical_reviewer_of_refiner(self) -> None:
        from review_graph import get_review_graph_for_phase

        g = get_review_graph_for_phase("refine", repo="jwbron/egg")
        assert "first_principles_reviewer" in g.critical_reviewers_for("refiner")
        # It reviews; it does not produce.
        assert g.is_reviewer("first_principles_reviewer")
        assert not g.is_producer("first_principles_reviewer")

    def test_present_for_non_egg_repos(self) -> None:
        from review_graph import get_review_graph_for_phase

        g = get_review_graph_for_phase("refine", repo="acme/widgets")
        assert "first_principles_reviewer" in g.critical_reviewers_for("refiner")


class TestDecisionCreateCapability:
    """The (B) capability grant: reviewers may CREATE decisions, not resolve."""

    def test_reviewer_can_create_but_not_resolve_decisions(self) -> None:
        from egg_contracts.roles import Role, can_modify

        assert can_modify(Role.REVIEWER, "decisions.0") is True
        assert can_modify(Role.REVIEWER, "decisions.0.resolved") is False
        assert can_modify(Role.REVIEWER, "decisions.0.resolution") is False
        # Implementer create-access is unaffected.
        assert can_modify(Role.IMPLEMENTER, "decisions.0") is True


class TestPromptSingleSource:
    """The criteria must interpolate the EXACT accept-path option labels."""

    def test_criteria_interpolate_the_sentinel_labels(self) -> None:
        from routes.decisions import (
            FIRST_PRINCIPLES_ADOPT_OPTION,
            FIRST_PRINCIPLES_CANCEL_OPTION,
            FIRST_PRINCIPLES_PROCEED_OPTION,
        )
        from routes.pipelines import _get_first_principles_review_criteria

        criteria = _get_first_principles_review_criteria()
        # Drift between the label the agent writes and the label the hook
        # matches would silently break the accept-path — pin them together.
        assert FIRST_PRINCIPLES_ADOPT_OPTION in criteria
        assert FIRST_PRINCIPLES_PROCEED_OPTION in criteria
        assert FIRST_PRINCIPLES_CANCEL_OPTION in criteria
        assert "-first-principles.json" in criteria
        assert "Never NACK" in criteria

    def test_reviewer_type_dispatch(self) -> None:
        from routes.pipelines import _get_review_criteria_for_type

        # The role-name → reviewer-type one-liner yields this verbatim.
        rt = "first_principles_reviewer".replace("reviewer_", "", 1).replace("_", "-")
        assert rt == "first-principles-reviewer"
        assert "first-principles" in _get_review_criteria_for_type(rt, "refine")


class TestAcceptPathHookDispatch:
    def _decision(self, phase_value: str | None = "refine", decision_id: str = "cq-1"):
        phase = SimpleNamespace(value=phase_value) if phase_value is not None else None
        return SimpleNamespace(id=decision_id, phase=phase)

    def test_non_matching_label_returns_none(self) -> None:
        from routes.decisions import _maybe_apply_first_principles_redirect

        out = _maybe_apply_first_principles_redirect(
            "p1", self._decision(), "Some unrelated resolution", SimpleNamespace(issue_number=1)
        )
        assert out is None

    def test_proceed_is_a_noop_action(self) -> None:
        from routes.decisions import (
            FIRST_PRINCIPLES_PROCEED_OPTION,
            _maybe_apply_first_principles_redirect,
        )

        out = _maybe_apply_first_principles_redirect(
            "p1",
            self._decision(),
            FIRST_PRINCIPLES_PROCEED_OPTION,
            SimpleNamespace(issue_number=1),
        )
        assert out == {
            "action": "first_principles_redirect",
            "outcome": "proceed",
            "success": True,
        }

    def test_phase_guard_skips_non_refine(self) -> None:
        from routes.decisions import (
            FIRST_PRINCIPLES_ADOPT_OPTION,
            _maybe_apply_first_principles_redirect,
        )

        out = _maybe_apply_first_principles_redirect(
            "p1",
            self._decision(phase_value="plan"),
            FIRST_PRINCIPLES_ADOPT_OPTION,
            SimpleNamespace(issue_number=1),
        )
        assert out is None

    def test_adopt_without_artifact_fails_loudly(self, tmp_path) -> None:
        from routes.decisions import (
            FIRST_PRINCIPLES_ADOPT_OPTION,
            _maybe_apply_first_principles_redirect,
        )

        # No first-principles artifact on the (empty) worktree → adopt cannot
        # recover the proposed seed and must surface a failure, never silently
        # succeed.
        with patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path):
            out = _maybe_apply_first_principles_redirect(
                "p1",
                self._decision(),
                FIRST_PRINCIPLES_ADOPT_OPTION,
                SimpleNamespace(issue_number=1),
            )
        assert out is not None
        assert out["outcome"] == "adopt"
        assert out["success"] is False
        assert "no proposed redirect" in out["error"]

    def test_adopt_reads_artifact_and_calls_redirect(self, tmp_path) -> None:
        import json

        from routes.decisions import (
            FIRST_PRINCIPLES_ADOPT_OPTION,
            _maybe_apply_first_principles_redirect,
        )

        outputs = tmp_path / ".egg-state" / "agent-outputs"
        outputs.mkdir(parents=True)
        (outputs / "7-first-principles.json").write_text(
            json.dumps({"concern": "c", "proposed_task_description": "Do the simpler thing"})
        )

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path),
            patch("routes.pipelines._pipeline_identifier", return_value=7),
            patch(
                "routes.pipelines.apply_first_principles_redirect",
                return_value=["refiner", "reviewer_refine"],
            ) as mock_redirect,
        ):
            out = _maybe_apply_first_principles_redirect(
                "p1",
                self._decision(),
                FIRST_PRINCIPLES_ADOPT_OPTION,
                SimpleNamespace(issue_number=7),
            )

        mock_redirect.assert_called_once()
        assert mock_redirect.call_args.args[1] == "Do the simpler thing"
        assert out["outcome"] == "adopted"
        assert out["success"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
