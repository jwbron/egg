"""
Tests for PipelineDispatcher.
"""

from pathlib import Path
from unittest.mock import patch

from dispatch import PipelineDispatcher, map_agent_role_to_contract_role
from models import AgentRole, Pipeline


class TestContractKey:
    """Tests for PipelineDispatcher.contract_key."""

    def test_issue_mode_returns_issue_number(self):
        """Issue-mode pipelines use the issue number as contract key."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert dispatcher.contract_key == 496

    def test_local_mode_returns_pipeline_id(self):
        """Local-mode pipelines use the pipeline ID as contract key."""
        pipeline = Pipeline(
            id="local-47601d1d",
            repo="owner/repo",
            branch="egg/local-47601d1d",
        )
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert dispatcher.contract_key == "local-47601d1d"

    def test_issue_mode_returns_int(self):
        """Issue-mode contract key is an int."""
        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
        )
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert isinstance(dispatcher.contract_key, int)

    def test_local_mode_returns_str(self):
        """Local-mode contract key is a str."""
        pipeline = Pipeline(id="local-abc123")
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert isinstance(dispatcher.contract_key, str)


class TestSaveContract:
    """Tests for PipelineDispatcher.save_contract."""

    def test_noop_when_orchestrator_not_loaded(self):
        """save_contract is a no-op when _contract_orchestrator is None.

        Non-contract roles (e.g. REFINER) never access contract_orchestrator,
        so _contract_orchestrator stays None.  save_contract should return
        without attempting to persist anything.
        """
        pipeline = Pipeline(id="issue-42", issue_number=42, repo="owner/repo")
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))

        assert dispatcher._contract_orchestrator is None

        # Should not raise or attempt to load/save.
        with patch("dispatch.save_contract") as mock_save:
            dispatcher.save_contract()
            mock_save.assert_not_called()

    def test_saves_when_orchestrator_loaded(self):
        """save_contract persists the contract when orchestrator is loaded."""
        pipeline = Pipeline(id="issue-42", issue_number=42, repo="owner/repo")
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))

        # Simulate a loaded orchestrator by setting _contract_orchestrator.
        from unittest.mock import MagicMock

        mock_orch = MagicMock()
        mock_contract = MagicMock()
        mock_orch.apply_to_contract.return_value = mock_contract
        dispatcher._contract_orchestrator = mock_orch

        with patch("dispatch.save_contract") as mock_save:
            dispatcher.save_contract()
            mock_orch.apply_to_contract.assert_called_once()
            mock_save.assert_called_once_with(mock_contract, Path("/tmp/repo"))


class TestMapAgentRoleToContractRole:
    """Tests for map_agent_role_to_contract_role."""

    def test_contract_roles_return_mapping(self):
        """Contract-participating roles return a ContractAgentRole."""
        contract_roles = [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.INTEGRATOR,
            AgentRole.ARCHITECT,
            AgentRole.TASK_PLANNER,
            AgentRole.RISK_ANALYST,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
        ]
        for role in contract_roles:
            result = map_agent_role_to_contract_role(role)
            assert result is not None, f"{role} should map to a contract role"

    def test_non_contract_roles_return_none(self):
        """Non-contract roles (REFINER, REVIEWER, etc.) return None."""
        non_contract_roles = [
            AgentRole.REFINER,
            AgentRole.REVIEWER,
            AgentRole.CHECKER,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ]
        for role in non_contract_roles:
            result = map_agent_role_to_contract_role(role)
            assert result is None, f"{role} should not map to a contract role"
