"""
Tests for coordinator-related models (Phase 1 of coordinator feature).

Tests the COORDINATOR agent role, CoordinatorState, and related data models
introduced for the conversational coordinator feature (issue #1028).
"""

from datetime import datetime

import pytest
from models import (
    AgentRole,
    AgentSpawnRecord,
    CoordinatorState,
    Escalation,
    GuardrailCounters,
    PhaseDecision,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)


class TestCoordinatorRole:
    """Tests for the COORDINATOR agent role in the AgentRole enum."""

    def test_coordinator_role_exists(self):
        """COORDINATOR must be a valid AgentRole value."""
        assert AgentRole.COORDINATOR == "coordinator"

    def test_coordinator_in_all_roles(self):
        """COORDINATOR must appear in the list of all roles."""
        roles = list(AgentRole)
        assert AgentRole.COORDINATOR in roles

    def test_coordinator_string_conversion(self):
        """COORDINATOR value round-trips through string conversion."""
        assert AgentRole("coordinator") == AgentRole.COORDINATOR

    def test_coordinator_role_is_distinct(self):
        """COORDINATOR does not collide with other roles."""
        other_roles = [r for r in AgentRole if r != AgentRole.COORDINATOR]
        assert "coordinator" not in [r.value for r in other_roles]


class TestAgentSpawnRecord:
    """Tests for the AgentSpawnRecord model."""

    def test_create_minimal(self):
        """Create a spawn record with only required fields."""
        record = AgentSpawnRecord(role=AgentRole.CODER)
        assert record.role == AgentRole.CODER
        assert record.status == "running"
        assert record.retry_number == 0
        assert record.completed_at is None
        assert record.container_id is None
        assert record.task_context == ""

    def test_create_full(self):
        """Create a spawn record with all fields."""
        now = datetime.utcnow()
        record = AgentSpawnRecord(
            role=AgentRole.TESTER,
            spawned_at=now,
            completed_at=now,
            status="complete",
            container_id="container-abc123",
            task_context="Write tests for auth module",
            retry_number=1,
        )
        assert record.role == AgentRole.TESTER
        assert record.status == "complete"
        assert record.container_id == "container-abc123"
        assert record.retry_number == 1

    def test_serialization_roundtrip(self):
        """AgentSpawnRecord survives JSON serialization."""
        record = AgentSpawnRecord(
            role=AgentRole.CODER,
            status="complete",
            container_id="c-123",
            task_context="Implement feature X",
            retry_number=2,
        )
        data = record.model_dump()
        restored = AgentSpawnRecord(**data)
        assert restored.role == record.role
        assert restored.status == record.status
        assert restored.retry_number == record.retry_number

    def test_retry_number_non_negative(self):
        """retry_number must be >= 0."""
        with pytest.raises(ValueError):
            AgentSpawnRecord(role=AgentRole.CODER, retry_number=-1)


class TestPhaseDecision:
    """Tests for the PhaseDecision model."""

    def test_create_minimal(self):
        """Create a phase decision with required fields."""
        decision = PhaseDecision(phase="implement", action="advance")
        assert decision.phase == "implement"
        assert decision.action == "advance"
        assert decision.reason == ""
        assert isinstance(decision.decided_at, datetime)

    def test_create_with_reason(self):
        """Create a phase decision with a reason."""
        decision = PhaseDecision(
            phase="plan",
            action="skip",
            reason="Simple bug fix, no plan needed",
        )
        assert decision.action == "skip"
        assert "bug fix" in decision.reason

    def test_loopback_action(self):
        """Support loopback phase action."""
        decision = PhaseDecision(
            phase="implement",
            action="loopback",
            reason="Tester found edge case, coder needs to fix",
        )
        assert decision.action == "loopback"

    def test_serialization_roundtrip(self):
        """PhaseDecision survives JSON serialization."""
        decision = PhaseDecision(
            phase="implement",
            action="advance",
            reason="All tests pass",
        )
        data = decision.model_dump()
        restored = PhaseDecision(**data)
        assert restored.phase == decision.phase
        assert restored.action == decision.action


class TestEscalation:
    """Tests for the Escalation model."""

    def test_create_unresolved(self):
        """Create an unresolved escalation."""
        escalation = Escalation(question="Which database should we use?")
        assert escalation.question == "Which database should we use?"
        assert escalation.escalation_type == "choice"
        assert escalation.resolved_at is None
        assert escalation.resolution is None

    def test_create_feedback_type(self):
        """Create a feedback escalation."""
        escalation = Escalation(
            question="What are the performance requirements?",
            escalation_type="feedback",
        )
        assert escalation.escalation_type == "feedback"

    def test_resolved_escalation(self):
        """Escalation can be marked as resolved."""
        escalation = Escalation(
            question="Proceed?",
            resolved_at=datetime.utcnow(),
            resolution="Yes, proceed with option A",
        )
        assert escalation.resolved_at is not None
        assert escalation.resolution == "Yes, proceed with option A"


class TestGuardrailCounters:
    """Tests for the GuardrailCounters model."""

    def test_defaults(self):
        """Default counters are zero."""
        counters = GuardrailCounters()
        assert counters.total_agents_spawned == 0
        assert counters.retries_by_role == {}
        assert counters.coordinator_respawns == 0
        assert isinstance(counters.started_at, datetime)

    def test_track_spawns(self):
        """Counters track total agent spawns."""
        counters = GuardrailCounters(total_agents_spawned=5)
        assert counters.total_agents_spawned == 5

    def test_track_retries_by_role(self):
        """Counters track retries per role."""
        counters = GuardrailCounters(retries_by_role={"coder": 2, "tester": 1})
        assert counters.retries_by_role["coder"] == 2
        assert counters.retries_by_role["tester"] == 1

    def test_coordinator_respawns(self):
        """Counters track coordinator respawns."""
        counters = GuardrailCounters(coordinator_respawns=2)
        assert counters.coordinator_respawns == 2

    def test_non_negative_agents_spawned(self):
        """total_agents_spawned must be >= 0."""
        with pytest.raises(ValueError):
            GuardrailCounters(total_agents_spawned=-1)

    def test_non_negative_coordinator_respawns(self):
        """coordinator_respawns must be >= 0."""
        with pytest.raises(ValueError):
            GuardrailCounters(coordinator_respawns=-1)


class TestCoordinatorState:
    """Tests for the CoordinatorState model."""

    def test_default_state(self):
        """Default state is empty."""
        state = CoordinatorState()
        assert state.workflow_type == ""
        assert state.agents_spawned == []
        assert state.phase_decisions == []
        assert state.escalations == []
        assert isinstance(state.guardrail_counters, GuardrailCounters)

    def test_workflow_type(self):
        """Can set workflow type."""
        state = CoordinatorState(workflow_type="bug_fix")
        assert state.workflow_type == "bug_fix"

    def test_with_agents_spawned(self):
        """State tracks spawned agents."""
        state = CoordinatorState(
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, task_context="Fix auth"),
                AgentSpawnRecord(role=AgentRole.TESTER, task_context="Test auth fix"),
            ]
        )
        assert len(state.agents_spawned) == 2
        assert state.agents_spawned[0].role == AgentRole.CODER

    def test_with_phase_decisions(self):
        """State tracks phase decisions."""
        state = CoordinatorState(
            phase_decisions=[
                PhaseDecision(phase="refine", action="skip", reason="Simple fix"),
                PhaseDecision(phase="implement", action="advance", reason="Done"),
            ]
        )
        assert len(state.phase_decisions) == 2

    def test_with_escalations(self):
        """State tracks escalations."""
        state = CoordinatorState(
            escalations=[
                Escalation(question="Which approach?"),
            ]
        )
        assert len(state.escalations) == 1

    def test_full_serialization_roundtrip(self):
        """Full CoordinatorState survives JSON round-trip."""
        state = CoordinatorState(
            workflow_type="feature",
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
            ],
            phase_decisions=[
                PhaseDecision(phase="plan", action="advance"),
            ],
            escalations=[
                Escalation(question="Proceed?", resolution="Yes"),
            ],
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=3,
                retries_by_role={"coder": 1},
                coordinator_respawns=0,
            ),
        )
        json_data = state.model_dump_json()
        restored = CoordinatorState.model_validate_json(json_data)
        assert restored.workflow_type == "feature"
        assert len(restored.agents_spawned) == 1
        assert restored.agents_spawned[0].role == AgentRole.CODER
        assert restored.guardrail_counters.total_agents_spawned == 3


class TestPipelineWithCoordinatorState:
    """Tests for Pipeline model with coordinator_state integration."""

    def test_pipeline_without_coordinator_state(self):
        """Existing pipelines work without coordinator state (backward compat)."""
        pipeline = Pipeline(
            id="issue-100",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
        )
        # Pipeline should not break if coordinator_state is not set
        json_data = pipeline.model_dump_json()
        restored = Pipeline.model_validate_json(json_data)
        assert restored.id == "issue-100"

    def test_pipeline_serialization_roundtrip(self):
        """Pipeline with standard fields serializes correctly."""
        pipeline = Pipeline(
            id="issue-200",
            issue_number=200,
            repo="owner/repo",
            branch="egg/issue-200",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        json_data = pipeline.model_dump_json()
        restored = Pipeline.model_validate_json(json_data)
        assert restored.status == PipelineStatus.RUNNING

    def test_backward_compat_no_coordinator_fields(self):
        """Pipeline deserialized from old JSON without coordinator fields works."""
        import json

        raw = json.dumps(
            {
                "id": "issue-300",
                "issue_number": 300,
                "repo": "owner/repo",
                "branch": "egg/issue-300",
                "mode": "issue",
                "status": "pending",
                "current_phase": "refine",
            }
        )
        pipeline = Pipeline.model_validate_json(raw)
        assert pipeline.id == "issue-300"


class TestPipelineConfigCoordinatorEnabled:
    """Tests for coordinator_enabled flag in PipelineConfig.

    PipelineConfig has a coordinator_enabled boolean (default false) to
    opt-in to coordinator-driven pipelines, plus guardrail config fields.
    """

    def test_default_config_coordinator_disabled(self):
        """Default PipelineConfig should have coordinator disabled."""
        config = PipelineConfig()
        assert config.coordinator_enabled is False

    def test_coordinator_enabled_true(self):
        """PipelineConfig can enable coordinator mode."""
        config = PipelineConfig(coordinator_enabled=True)
        assert config.coordinator_enabled is True

    def test_coordinator_max_agents_default(self):
        """Default max agents for coordinator should be 10."""
        config = PipelineConfig()
        assert config.coordinator_max_agents == 10

    def test_coordinator_max_retries_per_role_default(self):
        """Default max retries per role should be 2."""
        config = PipelineConfig()
        assert config.coordinator_max_retries_per_role == 2

    def test_coordinator_max_respawns_default(self):
        """Default max coordinator respawns should be 2."""
        config = PipelineConfig()
        assert config.coordinator_max_respawns == 2

    def test_coordinator_config_custom_values(self):
        """PipelineConfig accepts custom coordinator guardrail values."""
        config = PipelineConfig(
            coordinator_enabled=True,
            coordinator_max_agents=5,
            coordinator_max_retries_per_role=1,
            coordinator_max_respawns=3,
        )
        assert config.coordinator_max_agents == 5
        assert config.coordinator_max_retries_per_role == 1
        assert config.coordinator_max_respawns == 3

    def test_coordinator_max_agents_minimum(self):
        """coordinator_max_agents must be >= 1."""
        with pytest.raises(ValueError):
            PipelineConfig(coordinator_max_agents=0)

    def test_coordinator_config_serialization_roundtrip(self):
        """PipelineConfig with coordinator fields survives serialization."""
        config = PipelineConfig(
            coordinator_enabled=True,
            coordinator_max_agents=8,
        )
        data = config.model_dump()
        restored = PipelineConfig(**data)
        assert restored.coordinator_enabled is True
        assert restored.coordinator_max_agents == 8

    def test_config_backward_compat_without_coordinator_fields(self):
        """Old config JSON without coordinator fields deserializes correctly."""
        import json

        raw = json.dumps({"multi_agent": True, "parallel_agents": True})
        config = PipelineConfig.model_validate_json(raw)
        assert config.coordinator_enabled is False  # Default
        assert config.coordinator_max_agents == 10  # Default


class TestPipelineCoordinatorState:
    """Tests for coordinator_state field on Pipeline model."""

    def test_pipeline_coordinator_state_default_none(self):
        """Pipeline coordinator_state defaults to None."""
        pipeline = Pipeline(
            id="issue-500",
            issue_number=500,
            repo="owner/repo",
            branch="egg/issue-500",
        )
        assert pipeline.coordinator_state is None

    def test_pipeline_with_coordinator_state(self):
        """Pipeline can store CoordinatorState."""
        state = CoordinatorState(
            workflow_type="bug_fix",
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER),
            ],
        )
        pipeline = Pipeline(
            id="issue-500",
            issue_number=500,
            repo="owner/repo",
            branch="egg/issue-500",
            coordinator_state=state,
        )
        assert pipeline.coordinator_state is not None
        assert pipeline.coordinator_state.workflow_type == "bug_fix"

    def test_pipeline_coordinator_state_roundtrip(self):
        """Pipeline with coordinator_state survives JSON round-trip."""
        state = CoordinatorState(
            workflow_type="feature",
            guardrail_counters=GuardrailCounters(total_agents_spawned=3),
        )
        pipeline = Pipeline(
            id="issue-500",
            issue_number=500,
            repo="owner/repo",
            branch="egg/issue-500",
            coordinator_state=state,
        )
        json_data = pipeline.model_dump_json()
        restored = Pipeline.model_validate_json(json_data)
        assert restored.coordinator_state is not None
        assert restored.coordinator_state.workflow_type == "feature"
        assert restored.coordinator_state.guardrail_counters.total_agents_spawned == 3

    def test_pipeline_backward_compat_no_coordinator_state(self):
        """Old pipeline JSON without coordinator_state deserializes correctly."""
        import json

        raw = json.dumps(
            {
                "id": "issue-300",
                "issue_number": 300,
                "repo": "owner/repo",
                "branch": "egg/issue-300",
                "mode": "issue",
                "status": "pending",
                "current_phase": "refine",
            }
        )
        pipeline = Pipeline.model_validate_json(raw)
        assert pipeline.coordinator_state is None


class TestCoordinatorStateEdgeCases:
    """Edge case tests for coordinator state models."""

    def test_empty_agents_spawned_list(self):
        """Empty agents_spawned list is valid."""
        state = CoordinatorState(agents_spawned=[])
        assert state.agents_spawned == []

    def test_multiple_retries_same_role(self):
        """Multiple spawn records for the same role with different retry numbers."""
        state = CoordinatorState(
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, retry_number=0, status="failed"),
                AgentSpawnRecord(role=AgentRole.CODER, retry_number=1, status="complete"),
            ]
        )
        assert len(state.agents_spawned) == 2
        assert state.agents_spawned[0].status == "failed"
        assert state.agents_spawned[1].status == "complete"

    def test_guardrail_counters_at_limits(self):
        """Guardrail counters at high values."""
        counters = GuardrailCounters(
            total_agents_spawned=10,
            retries_by_role={"coder": 2, "tester": 2},
            coordinator_respawns=2,
        )
        assert counters.total_agents_spawned == 10

    def test_escalation_without_resolution(self):
        """Unresolved escalation has None resolution."""
        escalation = Escalation(question="What approach?")
        assert escalation.resolution is None
        assert escalation.resolved_at is None
