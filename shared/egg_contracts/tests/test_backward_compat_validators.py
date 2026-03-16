"""Tests for backward-compatibility validators on models with removed agent roles.

Verifies that persisted data containing removed role values (integrator, checker,
reviewer_unified) deserializes gracefully instead of raising ValidationError.
"""

from egg_contracts.checkpoints import AgentType, CheckpointSummaryV2, CheckpointV2
from egg_contracts.models import (
    AgentRoleType,
    Contract,
    MultiAgentConfig,
    PhaseAgentConfig,
)


class TestContractFilterRemovedAgentRoles:
    """Contract._filter_removed_agent_roles drops executions with removed roles."""

    def test_integrator_execution_silently_dropped(self):
        """Old contract with integrator execution deserializes without error."""
        data = {
            "pipeline_id": "test-pipeline",
            "agent_executions": [
                {"role": "coder", "status": "complete"},
                {"role": "integrator", "status": "complete"},
                {"role": "tester", "status": "pending"},
            ],
        }
        contract = Contract(**data)
        roles = [e.role for e in contract.agent_executions]
        assert AgentRoleType.CODER in roles
        assert AgentRoleType.TESTER in roles
        assert len(contract.agent_executions) == 2

    def test_checker_execution_silently_dropped(self):
        """Old contract with checker execution deserializes without error."""
        data = {
            "pipeline_id": "test-pipeline",
            "agent_executions": [
                {"role": "checker", "status": "complete"},
                {"role": "coder", "status": "complete"},
            ],
        }
        contract = Contract(**data)
        assert len(contract.agent_executions) == 1
        assert contract.agent_executions[0].role == AgentRoleType.CODER

    def test_reviewer_unified_execution_silently_dropped(self):
        """Old contract with reviewer_unified execution deserializes without error."""
        data = {
            "pipeline_id": "test-pipeline",
            "agent_executions": [
                {"role": "reviewer_unified", "status": "complete"},
            ],
        }
        contract = Contract(**data)
        assert len(contract.agent_executions) == 0

    def test_valid_roles_preserved(self):
        """Valid role executions are not affected by the filter."""
        data = {
            "pipeline_id": "test-pipeline",
            "agent_executions": [
                {"role": "coder", "status": "complete"},
                {"role": "tester", "status": "running"},
                {"role": "documenter", "status": "pending"},
            ],
        }
        contract = Contract(**data)
        assert len(contract.agent_executions) == 3


class TestPhaseAgentConfigFilterRemovedRoles:
    """PhaseAgentConfig._filter_removed_roles filters out removed role strings."""

    def test_removed_roles_filtered_from_list(self):
        config = PhaseAgentConfig(roles=["coder", "integrator", "tester", "checker"])
        assert len(config.roles) == 2
        assert AgentRoleType.CODER in config.roles
        assert AgentRoleType.TESTER in config.roles

    def test_none_roles_preserved(self):
        config = PhaseAgentConfig(roles=None)
        assert config.roles is None


class TestMultiAgentConfigFilterRemovedRoles:
    """MultiAgentConfig._filter_removed_roles filters out removed role strings."""

    def test_removed_roles_filtered(self):
        config = MultiAgentConfig(
            roles_enabled=["coder", "tester", "integrator", "reviewer_unified"]
        )
        assert len(config.roles_enabled) == 2
        assert AgentRoleType.CODER in config.roles_enabled
        assert AgentRoleType.TESTER in config.roles_enabled


class TestCheckpointV2CoerceRemovedAgentTypes:
    """CheckpointV2._coerce_removed_agent_types maps removed types to UNKNOWN."""

    def test_integrator_mapped_to_unknown(self):
        """Old checkpoint with agent_type='integrator' maps to UNKNOWN."""
        data = _minimal_checkpoint(agent_type="integrator")
        checkpoint = CheckpointV2(**data)
        assert checkpoint.agent_type == AgentType.UNKNOWN

    def test_checker_mapped_to_unknown(self):
        """Old checkpoint with agent_type='checker' maps to UNKNOWN."""
        data = _minimal_checkpoint(agent_type="checker")
        checkpoint = CheckpointV2(**data)
        assert checkpoint.agent_type == AgentType.UNKNOWN

    def test_valid_type_preserved(self):
        """Valid agent_type values are not affected."""
        data = _minimal_checkpoint(agent_type="coder")
        checkpoint = CheckpointV2(**data)
        assert checkpoint.agent_type == AgentType.CODER


class TestCheckpointSummaryV2CoerceRemovedAgentTypes:
    """CheckpointSummaryV2._coerce_removed_agent_types maps removed types to UNKNOWN."""

    def test_integrator_mapped_to_unknown(self):
        data = _minimal_summary(agent_type="integrator")
        summary = CheckpointSummaryV2(**data)
        assert summary.agent_type == AgentType.UNKNOWN

    def test_checker_mapped_to_unknown(self):
        data = _minimal_summary(agent_type="checker")
        summary = CheckpointSummaryV2(**data)
        assert summary.agent_type == AgentType.UNKNOWN


def _minimal_checkpoint(agent_type: str = "unknown") -> dict:
    """Return minimal valid CheckpointV2 data with overridable agent_type."""
    return {
        "id": "ckpt-abcdef01",
        "trigger_type": "commit",
        "session_id": "test-session",
        "agent_type": agent_type,
        "created_at": "2026-01-01T00:00:00Z",
        "session_started_at": "2026-01-01T00:00:00Z",
        "session": {
            "session_id": "test-session",
            "started_at": "2026-01-01T00:00:00Z",
        },
        "transcript": {"messages": []},
        "token_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _minimal_summary(agent_type: str = "unknown") -> dict:
    """Return minimal valid CheckpointSummaryV2 data with overridable agent_type."""
    return {
        "id": "ckpt-abcdef01",
        "trigger_type": "commit",
        "session_id": "test-session",
        "agent_type": agent_type,
        "created_at": "2026-01-01T00:00:00Z",
    }
