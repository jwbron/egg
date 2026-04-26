"""Always-on guards for the ``reviewer_code_holistic`` BRC role (issue #2126).

The holistic reviewer is the always-on generalist counterpart to
``reviewer_code``. It must:

1. Be registered alongside ``reviewer_code`` in the implement-phase
   review graph as a *distinct CRITICAL* role so its NACKs are not
   averaged with the fan-out reviewer's slice ACKs.
2. Run on every implement pipeline (no fan-out gate, no PR-size gate).
3. Use a holistic-lens prompt (not the fan-out / line-by-line code
   review criteria).

These asserts are deterministic — they do not run the LLM.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub Docker the same way the other prompt tests do.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from review_graph import (  # noqa: E402
    ReviewCriticality,
    get_default_implement_graph,
)
from routes.pipelines import _build_review_prompt  # noqa: E402

# ---------------------------------------------------------------------------
# Review-graph wiring.
# ---------------------------------------------------------------------------


class TestImplementGraphWiring:
    """Both holistic edges must exist as CRITICAL."""

    def setup_method(self) -> None:
        self.graph = get_default_implement_graph()

    def test_reviews_coder_critical(self) -> None:
        edge = self.graph.get_edge("reviewer_code_holistic", "coder")
        assert edge is not None, "reviewer_code_holistic → coder edge missing"
        assert edge.criticality is ReviewCriticality.CRITICAL, (
            "reviewer_code_holistic → coder must be CRITICAL so a holistic "
            "NACK gates consensus on its own (issue #2126)."
        )

    def test_reviews_tester_critical(self) -> None:
        edge = self.graph.get_edge("reviewer_code_holistic", "tester")
        assert edge is not None, "reviewer_code_holistic → tester edge missing"
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_distinct_from_reviewer_code(self) -> None:
        """Both reviewers exist as separate CRITICAL edges so NACKs don't average."""
        code_coder = self.graph.get_edge("reviewer_code", "coder")
        holistic_coder = self.graph.get_edge("reviewer_code_holistic", "coder")
        assert code_coder is not None
        assert holistic_coder is not None
        assert code_coder.reviewer_role != holistic_coder.reviewer_role, (
            "reviewer_code and reviewer_code_holistic must be distinct roles "
            "in the review graph — merging them defeats the issue #2126 goal."
        )

    def test_listed_in_critical_reviewers_for_coder(self) -> None:
        critical = self.graph.critical_reviewers_for("coder")
        assert "reviewer_code_holistic" in critical
        assert "reviewer_code" in critical

    def test_listed_in_critical_reviewers_for_tester(self) -> None:
        critical = self.graph.critical_reviewers_for("tester")
        assert "reviewer_code_holistic" in critical


# ---------------------------------------------------------------------------
# Phase roster + role registration.
# ---------------------------------------------------------------------------


class TestRoleRegistration:
    """The role must be in the canonical registry and the implement roster."""

    def test_in_agent_role_enum(self) -> None:
        from egg_contracts.agent_roles import AgentRole

        assert AgentRole("reviewer_code_holistic") is AgentRole.REVIEWER_CODE_HOLISTIC

    def test_in_agent_roles_registry(self) -> None:
        from egg_contracts.agent_roles import AGENT_ROLES, AgentRole

        assert AgentRole.REVIEWER_CODE_HOLISTIC in AGENT_ROLES

    def test_in_implement_phase_roster(self) -> None:
        from egg_contracts.agent_roles import AgentRole, get_roles_for_phase

        roles = get_roles_for_phase("implement")
        assert AgentRole.REVIEWER_CODE_HOLISTIC in roles

    def test_applies_to_non_egg_repos(self) -> None:
        """The holistic reviewer is not egg-only — every repo gets it."""
        from egg_contracts.agent_roles import AgentRole, get_roles_for_phase

        roles = get_roles_for_phase("implement", repo="some-org/some-repo")
        assert AgentRole.REVIEWER_CODE_HOLISTIC in roles

    def test_maps_to_reviewer_contract_role(self) -> None:
        from egg_contracts.agent_roles import get_contract_role
        from egg_contracts.roles import Role

        assert get_contract_role("reviewer_code_holistic") == Role.REVIEWER


# ---------------------------------------------------------------------------
# Prompt assembly: criteria differentiation + always-on (no PR-size gate).
# ---------------------------------------------------------------------------


class TestHolisticPrompt:
    def setup_method(self) -> None:
        self.prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code-holistic",
            issue_number=100,
        )

    def test_no_fan_out_block(self) -> None:
        """Holistic always single-passes — no fan-out section, ever."""
        assert "Subagent Fan-Out Strategy" not in self.prompt, (
            "reviewer_code_holistic must not include the fan-out block — "
            "it always reads the whole diff itself (issue #2126)."
        )

    def test_no_subagent_threshold_text(self) -> None:
        """The 10-files / 500-LOC gate is reviewer_code's, not holistic's."""
        # Be conservative: the holistic prompt may reference review
        # criteria that mention "10" or "500" for unrelated reasons, so
        # only assert on the gate phrase itself.
        assert "files_changed > 10" not in self.prompt
        assert "(loc_added + loc_removed) > 500" not in self.prompt

    def test_carries_holistic_scope_marker(self) -> None:
        """The scope preamble must identify this as the holistic lens."""
        prompt_lower = self.prompt.lower()
        assert "holistic" in prompt_lower, (
            "Holistic reviewer prompt must surface its lens identity."
        )

    def test_canonical_use_case_reference(self) -> None:
        """The PR #2105 ``__checkout__`` miss is the canonical example."""
        assert "__checkout__" in self.prompt or "PR #2105" in self.prompt, (
            "Holistic prompt should reference the canonical end-to-end "
            "use-case-dead-end miss (PR #2105 / __checkout__)."
        )

    def test_complementary_framing(self) -> None:
        """The preamble must tell the reviewer to defer line-by-line work."""
        prompt_lower = self.prompt.lower()
        assert "fan-out" in prompt_lower or "slice" in prompt_lower, (
            "Holistic preamble must frame its job as complementary to "
            "reviewer_code's fan-out / slice work."
        )
