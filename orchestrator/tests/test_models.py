"""
Tests for orchestrator models.
"""

from datetime import datetime

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    DecisionStatus,
    HITLDecision,
    PhaseExecution,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)


class TestContainerInfo:
    """Tests for ContainerInfo model."""

    def test_create_minimal(self):
        """Test creating ContainerInfo with minimal fields."""
        info = ContainerInfo(
            container_id="abc123",
            container_name="egg-sandbox-test",
        )
        assert info.container_id == "abc123"
        assert info.container_name == "egg-sandbox-test"
        assert info.status == ContainerStatus.PENDING
        assert info.started_at is None

    def test_create_full(self):
        """Test creating ContainerInfo with all fields."""
        now = datetime.utcnow()
        info = ContainerInfo(
            container_id="abc123",
            container_name="egg-sandbox-coder",
            status=ContainerStatus.RUNNING,
            started_at=now,
            agent_role=AgentRole.CODER,
            session_token="token123",
        )
        assert info.status == ContainerStatus.RUNNING
        assert info.agent_role == AgentRole.CODER
        assert info.session_token == "token123"


class TestAgentExecution:
    """Tests for AgentExecution model."""

    def test_create_pending(self):
        """Test creating pending agent execution."""
        agent = AgentExecution(role=AgentRole.CODER)
        assert agent.role == AgentRole.CODER
        assert agent.status == AgentExecutionStatus.PENDING
        assert agent.container_id is None
        assert agent.outputs == {}

    def test_agent_with_outputs(self):
        """Test agent execution with handoff data."""
        agent = AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.COMPLETE,
            commit="abc1234",
            outputs={"files_changed": ["src/main.py"]},
        )
        assert agent.commit == "abc1234"
        assert agent.outputs["files_changed"] == ["src/main.py"]


class TestHITLDecision:
    """Tests for HITLDecision model."""

    def test_create_decision(self):
        """Test creating a HITL decision."""
        decision = HITLDecision(
            id="decision-1",
            question="Which approach should we use?",
            options=["Option A", "Option B"],
        )
        assert decision.id == "decision-1"
        assert decision.status == DecisionStatus.PENDING
        assert len(decision.options) == 2
        assert decision.resolution is None

    def test_decision_timeout(self):
        """Test decision timeout configuration."""
        decision = HITLDecision(
            id="decision-1",
            question="Proceed?",
            timeout_seconds=7200,
        )
        assert decision.timeout_seconds == 7200


class TestPhaseExecution:
    """Tests for PhaseExecution model."""

    def test_create_phase(self):
        """Test creating phase execution."""
        phase = PhaseExecution(phase=PipelinePhase.REFINE)
        assert phase.phase == PipelinePhase.REFINE
        assert phase.status == PipelineStatus.PENDING
        assert phase.work_started_at is None
        assert phase.containers == []
        assert phase.agents == []

    def test_hitl_review_cycles_defaults_to_zero(self):
        """Test that hitl_review_cycles defaults to 0 and is independent of review_cycles."""
        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        assert phase.review_cycles == 0
        assert phase.hitl_review_cycles == 0

    def test_hitl_review_cycles_independent_of_review_cycles(self):
        """Test that hitl_review_cycles and review_cycles are tracked independently."""
        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        phase.review_cycles = 2
        phase.hitl_review_cycles = 1
        assert phase.review_cycles == 2
        assert phase.hitl_review_cycles == 1

    def test_phase_with_agents(self):
        """Test phase with agent executions."""
        phase = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[
                AgentExecution(role=AgentRole.CODER),
                AgentExecution(role=AgentRole.TESTER),
            ],
        )
        assert len(phase.agents) == 2
        assert phase.agents[0].role == AgentRole.CODER


class TestPipelineConfig:
    """Tests for PipelineConfig model."""

    def test_defaults(self):
        """Test default configuration values."""
        config = PipelineConfig()
        assert config.auto_create_pr is True
        assert config.multi_agent is True
        assert config.parallel_agents is True
        assert config.max_review_cycles == 3
        assert config.decision_timeout == 3600
        assert config.hitl_gates is True

    def test_custom_config(self):
        """Test custom configuration."""
        config = PipelineConfig(
            auto_create_pr=False,
            multi_agent=False,
            max_review_cycles=5,
        )
        assert config.auto_create_pr is False
        assert config.multi_agent is False
        assert config.max_review_cycles == 5


class TestPipeline:
    """Tests for Pipeline model."""

    def test_create_pipeline(self):
        """Test creating a pipeline."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        assert pipeline.id == "issue-496"
        assert pipeline.issue_number == 496
        assert pipeline.status == PipelineStatus.PENDING
        assert pipeline.current_phase == PipelinePhase.REFINE
        assert pipeline.phases == {}

    def test_get_phase_execution_creates(self):
        """Test get_phase_execution creates phase if not exists."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        phase = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase.phase == PipelinePhase.REFINE
        assert "refine" in pipeline.phases

    def test_get_phase_execution_returns_existing(self):
        """Test get_phase_execution returns existing phase."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
            phases={
                "refine": PhaseExecution(
                    phase=PipelinePhase.REFINE,
                    status=PipelineStatus.RUNNING,
                )
            },
        )
        phase = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase.status == PipelineStatus.RUNNING

    def test_add_decision(self):
        """Test adding a HITL decision."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        decision = pipeline.add_decision(
            question="Proceed with implementation?",
            options=["Yes", "No"],
        )
        assert decision.id == "decision-1"
        assert len(pipeline.decisions) == 1
        assert decision.status == DecisionStatus.PENDING

    def test_resolve_decision(self):
        """Test resolving a HITL decision."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        pipeline.add_decision("Proceed?", ["Yes", "No"])
        resolved = pipeline.resolve_decision("decision-1", "Yes")
        assert resolved is not None
        assert resolved.status == DecisionStatus.RESOLVED
        assert resolved.resolution == "Yes"
        assert resolved.resolved_at is not None

    def test_resolve_nonexistent_decision(self):
        """Test resolving a non-existent decision returns None."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        resolved = pipeline.resolve_decision("decision-999", "Yes")
        assert resolved is None

    def test_get_pending_decisions(self):
        """Test getting pending decisions."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        pipeline.add_decision("Question 1")
        pipeline.add_decision("Question 2")
        pipeline.resolve_decision("decision-1", "Answer 1")

        pending = pipeline.get_pending_decisions()
        assert len(pending) == 1
        assert pending[0].question == "Question 2"

    def test_pipeline_serialization(self):
        """Test pipeline can be serialized to JSON."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        pipeline.add_decision("Test?")
        json_data = pipeline.model_dump_json()
        assert "issue-496" in json_data

    def test_pipeline_deserialization(self):
        """Test pipeline can be deserialized from JSON."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        json_data = pipeline.model_dump_json()
        restored = Pipeline.model_validate_json(json_data)
        assert restored.id == pipeline.id
        assert restored.issue_number == pipeline.issue_number

    def test_network_mode_default_none(self):
        """Test that network_mode defaults to None."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        assert pipeline.network_mode is None

    def test_network_mode_private(self):
        """Test creating a pipeline with network_mode='private'."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
            network_mode="private",
        )
        assert pipeline.network_mode == "private"

    def test_network_mode_roundtrip(self):
        """Test that network_mode survives serialization/deserialization."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
            network_mode="private",
        )
        json_data = pipeline.model_dump_json()
        restored = Pipeline.model_validate_json(json_data)
        assert restored.network_mode == "private"

    def test_network_mode_backward_compat(self):
        """Test that missing network_mode in JSON defaults to None (backward compat)."""
        import json

        raw = json.dumps(
            {
                "id": "issue-496",
                "issue_number": 496,
                "repo": "owner/repo",
                "branch": "egg/issue-496",
                "mode": "issue",
                "status": "pending",
                "current_phase": "refine",
            }
        )
        restored = Pipeline.model_validate_json(raw)
        assert restored.network_mode is None


class TestAgentRole:
    """Tests for AgentRole enum."""

    def test_all_roles(self):
        """Test all agent roles are defined."""
        roles = list(AgentRole)
        assert AgentRole.CODER in roles
        assert AgentRole.REVIEWER in roles
        assert AgentRole.CHECKER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        assert AgentRole.INTEGRATOR in roles
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles
        assert AgentRole.REFINER in roles
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CONTRACT in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert AgentRole.REVIEWER_REFINE in roles
        assert AgentRole.REVIEWER_PLAN in roles
        assert len(roles) == 15


class TestPipelinePhase:
    """Tests for PipelinePhase enum."""

    def test_phase_order(self):
        """Test phases are defined in SDLC order."""
        phases = list(PipelinePhase)
        assert phases[0] == PipelinePhase.REFINE
        assert phases[1] == PipelinePhase.PLAN
        assert phases[2] == PipelinePhase.IMPLEMENT
        assert phases[3] == PipelinePhase.PR
