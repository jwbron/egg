"""
Tests for orchestrator models.
"""

from datetime import UTC, datetime

import pytest
from models import (
    PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN,
    AgentExecution,
    AgentExecutionStatus,
    AgentExitInfo,
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
    resolve_consensus_timeout_minutes,
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
        now = datetime.now(UTC)
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

    def test_slice_id_none_allowed(self):
        """``slice_id=None`` (the default) is the pipeline-level scope."""
        agent = AgentExecution(role=AgentRole.CODER)
        assert agent.slice_id is None

    def test_slice_id_canonical_accepted(self):
        """Canonical ``slice-<N>`` ids pass the validator."""
        agent = AgentExecution(role=AgentRole.CODER, slice_id="slice-2")
        assert agent.slice_id == "slice-2"

    @pytest.mark.parametrize(
        "bad_value",
        ["phase-2", "slice-", "slice-2a", "Slice-2", " slice-2", "slice-2 ", ""],
    )
    def test_slice_id_non_canonical_rejected(self, bad_value):
        """Non-canonical ``slice_id`` values are rejected at construction.

        Defense-in-depth (#2422 review): production write paths use
        ``extract_slice_id`` / ``concurrent_executor._slice_id`` which
        already enforce ``SLICE_ID_PATTERN``, but a hand-built fixture
        or migration tool must not be able to smuggle a non-canonical
        value through ``AgentExecution(...)``.
        """
        with pytest.raises(ValueError, match="Invalid slice_id"):
            AgentExecution(role=AgentRole.CODER, slice_id=bad_value)


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

    def test_decision_type_default(self):
        """Test that decision_type defaults to 'choice'."""
        decision = HITLDecision(
            id="decision-1",
            question="Pick one?",
        )
        assert decision.decision_type == "choice"

    def test_decision_type_phase_gate(self):
        """Test creating a phase_gate decision."""
        decision = HITLDecision(
            id="decision-1",
            question="Approve the plan?",
            decision_type="phase_gate",
            options=["approve", "request changes"],
        )
        assert decision.decision_type == "phase_gate"

    def test_content_changed_default_none(self):
        """Test that content_changed defaults to None."""
        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
        )
        assert decision.content_changed is None

    def test_content_changed_set(self):
        """Test creating a decision with content_changed set."""
        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
            decision_type="phase_gate",
            content_changed=True,
        )
        assert decision.content_changed is True

    def test_questions_default_empty(self):
        """Test that questions defaults to empty list."""
        decision = HITLDecision(
            id="decision-1",
            question="Pick one?",
        )
        assert decision.questions == []

    def test_questions_with_feedback(self):
        """Test creating a feedback decision with structured questions."""
        questions = [
            {"id": "q-1", "question": "What is the expected volume?", "answer": ""},
            {"id": "q-2", "question": "Any performance requirements?", "answer": ""},
        ]
        decision = HITLDecision(
            id="decision-1",
            question="Please provide feedback",
            decision_type="feedback",
            questions=questions,
        )
        assert decision.decision_type == "feedback"
        assert len(decision.questions) == 2
        assert decision.questions[0]["id"] == "q-1"

    def test_dict_resolution_serialized_to_json_string(self):
        """Dict resolution is auto-serialized to a JSON string (#1635)."""
        import json

        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
            resolution={"action": "select", "selected": "approve"},
        )
        assert isinstance(decision.resolution, str)
        assert json.loads(decision.resolution) == {
            "action": "select",
            "selected": "approve",
        }

    def test_list_resolution_serialized_to_json_string(self):
        """List resolution is auto-serialized to a JSON string (#1635)."""
        import json

        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
            resolution=["option-a", "option-b"],
        )
        assert isinstance(decision.resolution, str)
        assert json.loads(decision.resolution) == ["option-a", "option-b"]

    def test_string_resolution_unchanged(self):
        """String resolution passes through unchanged (#1635)."""
        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
            resolution='{"action": "approve"}',
        )
        assert decision.resolution == '{"action": "approve"}'

    def test_none_resolution_unchanged(self):
        """None resolution passes through unchanged (#1635)."""
        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
            resolution=None,
        )
        assert decision.resolution is None

    def test_dict_resolution_serialized_on_assignment(self):
        """Dict resolution is serialized when assigned to an existing instance (#1635)."""
        import json

        decision = HITLDecision(
            id="decision-1",
            question="Approve?",
            resolution=None,
        )
        # Direct attribute assignment — requires validate_assignment=True
        decision.resolution = {"action": "select", "selected": "approve"}
        assert isinstance(decision.resolution, str)
        assert json.loads(decision.resolution) == {"action": "select", "selected": "approve"}


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

    def test_phase_start_sha_defaults_to_none(self):
        """phase_start_sha defaults to None."""
        phase = PhaseExecution(phase=PipelinePhase.IMPLEMENT)
        assert phase.phase_start_sha is None

    def test_phase_start_sha_can_be_set(self):
        """phase_start_sha can be set to a commit SHA."""
        phase = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            phase_start_sha="abc123def456",
        )
        assert phase.phase_start_sha == "abc123def456"

    def test_phase_start_sha_serializes(self):
        """phase_start_sha round-trips through model_dump."""
        phase = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            phase_start_sha="abc123def456",
        )
        data = phase.model_dump()
        assert data["phase_start_sha"] == "abc123def456"
        restored = PhaseExecution(**data)
        assert restored.phase_start_sha == "abc123def456"

    def test_phase_start_sha_none_serializes(self):
        """phase_start_sha=None round-trips through model_dump."""
        phase = PhaseExecution(phase=PipelinePhase.IMPLEMENT)
        data = phase.model_dump()
        assert data["phase_start_sha"] is None
        restored = PhaseExecution(**data)
        assert restored.phase_start_sha is None

    def test_agent_exits_defaults_to_empty_list(self):
        """agent_exits defaults to an empty list (issue #2205)."""
        phase = PhaseExecution(phase=PipelinePhase.IMPLEMENT)
        assert phase.agent_exits == []

    def test_agent_exits_appends_records(self):
        """agent_exits accepts AgentExitInfo records, preserving order."""
        now = datetime.now(UTC)
        phase = PhaseExecution(phase=PipelinePhase.IMPLEMENT)
        phase.agent_exits.append(
            AgentExitInfo(
                role=AgentRole.CODER,
                exit_code=0,
                terminated_at=now,
            )
        )
        phase.agent_exits.append(
            AgentExitInfo(
                role=AgentRole.TESTER,
                exit_code=1,
                last_lines=["traceback line 1", "traceback line 2"],
                terminated_at=now,
                container_id="abc123",
            )
        )
        assert [ae.role for ae in phase.agent_exits] == [AgentRole.CODER, AgentRole.TESTER]
        assert phase.agent_exits[1].exit_code == 1
        assert phase.agent_exits[1].last_lines == ["traceback line 1", "traceback line 2"]
        assert phase.agent_exits[1].container_id == "abc123"

    def test_agent_exits_round_trip(self):
        """agent_exits serializes and rehydrates through model_dump."""
        now = datetime.now(UTC)
        phase = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            agent_exits=[
                AgentExitInfo(
                    role=AgentRole.CODER,
                    exit_code=1,
                    last_lines=["line a", "line b"],
                    terminated_at=now,
                    container_id="cid-1",
                ),
            ],
        )
        data = phase.model_dump(mode="json")
        assert len(data["agent_exits"]) == 1
        assert data["agent_exits"][0]["role"] == AgentRole.CODER.value
        assert data["agent_exits"][0]["exit_code"] == 1
        assert data["agent_exits"][0]["last_lines"] == ["line a", "line b"]
        assert data["agent_exits"][0]["container_id"] == "cid-1"

        restored = PhaseExecution(**phase.model_dump())
        assert len(restored.agent_exits) == 1
        assert restored.agent_exits[0].role == AgentRole.CODER
        assert restored.agent_exits[0].exit_code == 1

    def test_legacy_hitl_feedback_migrates_to_operator_directive(self):
        """A persisted hitl_feedback string is translated into operator_directives.

        Pre-#2795 ``PhaseExecution`` carried a single ``hitl_feedback: str``
        that the inline HITL handler and recovery path wrote and consumed.
        #2795 replaced it with ``operator_directives``; without the migration,
        Pydantic's default ``extra='ignore'`` would silently drop the
        surviving value on the first load after deploy, losing the operator's
        directive for any pipeline paused at a HITL gate.
        """
        raw = {
            "phase": PipelinePhase.REFINE.value,
            "hitl_feedback": "Drop the planner-scope sections.",
            "hitl_review_cycles": 2,
        }
        restored = PhaseExecution.model_validate(raw)
        assert len(restored.operator_directives) == 1
        assert restored.operator_directives[0].feedback_text == "Drop the planner-scope sections."
        # iteration_n is hitl_review_cycles - 1, matching the inline path.
        assert restored.operator_directives[0].iteration_n == 1
        # The legacy attribute is gone on the migrated model.
        assert not hasattr(restored, "hitl_feedback")

    def test_legacy_hitl_feedback_absent_or_empty_is_noop(self):
        """No legacy field and an empty legacy field both leave directives unchanged."""
        # Missing field — load is a plain construction.
        restored = PhaseExecution.model_validate({"phase": PipelinePhase.REFINE.value})
        assert restored.operator_directives == []

        # Explicit empty string — treated as no-op, no synthetic directive.
        restored = PhaseExecution.model_validate(
            {"phase": PipelinePhase.REFINE.value, "hitl_feedback": ""}
        )
        assert restored.operator_directives == []

    def test_legacy_hitl_feedback_appends_to_existing_directives(self):
        """If operator_directives already has entries, migrated entry appends.

        Guards against losing both pre-#2795 hitl_feedback and any
        post-#2795 directives if a write path raced the migration.
        """
        raw = {
            "phase": PipelinePhase.REFINE.value,
            "hitl_review_cycles": 2,
            "hitl_feedback": "Legacy directive",
            "operator_directives": [
                {"iteration_n": 0, "feedback_text": "Already-structured directive"},
            ],
        }
        restored = PhaseExecution.model_validate(raw)
        assert len(restored.operator_directives) == 2
        assert restored.operator_directives[0].feedback_text == "Already-structured directive"
        assert restored.operator_directives[1].feedback_text == "Legacy directive"
        # Collides on iteration_n=1 because hitl_review_cycles=2 → 1, which
        # doesn't conflict with the existing entry at iteration 0.
        assert restored.operator_directives[1].iteration_n == 1

    def test_legacy_hitl_feedback_null_operator_directives_is_safe(self):
        """An explicit ``null`` for ``operator_directives`` does not break the migration.

        Pydantic's ``default_factory=list`` does not prevent a writer from
        emitting a literal ``null`` on the JSON record. The validator
        normalises ``operator_directives: None`` to ``[]`` before any
        further work, so neither the migration branch nor the field
        validation downstream raises.
        """
        raw = {
            "phase": PipelinePhase.REFINE.value,
            "hitl_feedback": "Drop the planner-scope sections.",
            "hitl_review_cycles": 1,
            "operator_directives": None,
        }
        restored = PhaseExecution.model_validate(raw)
        assert len(restored.operator_directives) == 1
        assert restored.operator_directives[0].feedback_text == "Drop the planner-scope sections."

    def test_null_operator_directives_without_legacy_feedback_is_safe(self):
        """``operator_directives: null`` is normalised even when no migration runs.

        Without ``hitl_feedback`` the validator returns early; before the
        normalisation moved above that early return, the non-Optional
        ``list[OperatorDirective]`` field validation would reject the
        record. This case guards that path.
        """
        raw = {
            "phase": PipelinePhase.REFINE.value,
            "operator_directives": None,
        }
        restored = PhaseExecution.model_validate(raw)
        assert restored.operator_directives == []

    def test_legacy_hitl_feedback_sparse_indices_collision_floor(self):
        """Collision fallback picks one past the maximum, not ``len(directives)``.

        With existing indices ``[1, 2]`` and ``hitl_review_cycles=2`` the
        primary candidate ``hitl_review_cycles - 1 = 1`` collides with
        the existing entry. The old ``len(directives)`` fallback would
        have landed on ``2`` and collided again; the ``max(...) + 1``
        floor lands on ``3``.
        """
        raw = {
            "phase": PipelinePhase.REFINE.value,
            "hitl_review_cycles": 2,
            "hitl_feedback": "Legacy directive",
            "operator_directives": [
                {"iteration_n": 1, "feedback_text": "First"},
                {"iteration_n": 2, "feedback_text": "Sparse"},
            ],
        }
        restored = PhaseExecution.model_validate(raw)
        assert len(restored.operator_directives) == 3
        # Primary candidate hitl_review_cycles-1 = 1 collides with existing
        # entry at iteration_n=1 → fallback to max([1, 2]) + 1 = 3.
        assert restored.operator_directives[2].iteration_n == 3


class TestAgentExitInfo:
    """Tests for AgentExitInfo (issue #2205)."""

    def test_minimal(self):
        """exit_code and role are required, last_lines defaults to []."""
        now = datetime.now(UTC)
        info = AgentExitInfo(
            role=AgentRole.TESTER,
            exit_code=1,
            terminated_at=now,
        )
        assert info.role == AgentRole.TESTER
        assert info.exit_code == 1
        assert info.last_lines == []
        assert info.container_id is None

    def test_full(self):
        """All fields populated round-trip."""
        now = datetime.now(UTC)
        info = AgentExitInfo(
            role=AgentRole.CODER,
            exit_code=137,
            last_lines=["a", "b", "c"],
            terminated_at=now,
            container_id="cid-9",
        )
        data = info.model_dump(mode="json")
        restored = AgentExitInfo(**info.model_dump())
        assert data["exit_code"] == 137
        assert restored.role == AgentRole.CODER
        assert restored.last_lines == ["a", "b", "c"]
        assert restored.container_id == "cid-9"

    def test_exit_code_none_accepted(self):
        """exit_code=None is valid (matches ContainerInfo.exit_code).

        The k8s path leaves exit_code=None during pod-phase races where
        status is FAILED but container_statuses[0].state.terminated hasn't
        populated yet. AgentExitInfo must not reject this.
        """
        now = datetime.now(UTC)
        info = AgentExitInfo(
            role=AgentRole.CODER,
            exit_code=None,
            terminated_at=now,
        )
        assert info.exit_code is None
        restored = AgentExitInfo(**info.model_dump())
        assert restored.exit_code is None


class TestPipelineConfig:
    """Tests for PipelineConfig model."""

    def test_defaults(self):
        """Test default configuration values."""
        config = PipelineConfig()
        assert config.parallel_agents is True
        assert config.max_review_cycles == 3
        assert config.hitl_gates is True
        assert config.concurrent_phases == ["refine", "plan", "implement"]
        assert config.auto_repropose_debounce_seconds == 60
        assert config.max_auto_repropose == 5

    def test_custom_config(self):
        """Test custom configuration."""
        config = PipelineConfig(
            max_review_cycles=5,
        )
        assert config.max_review_cycles == 5


class TestResolveConsensusTimeoutMinutes:
    """Tests for resolve_consensus_timeout_minutes (issue #2263).

    The resolver picks a per-phase timeout in this order:
      1. Per-phase override field if explicitly set.
      2. Legacy global ``consensus_timeout_minutes`` if explicitly set
         (preserves the back-compat clause: pipelines that pass only the
         global behave identically across all three phases).
      3. Phase-aware default from PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN.
    """

    def test_phase_aware_defaults_when_nothing_set(self):
        config = PipelineConfig()
        assert resolve_consensus_timeout_minutes(config, "refine") == 30
        assert resolve_consensus_timeout_minutes(config, "plan") == 60
        assert resolve_consensus_timeout_minutes(config, "implement") == 90

    def test_phase_defaults_match_constant(self):
        config = PipelineConfig()
        for phase, expected in PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN.items():
            assert resolve_consensus_timeout_minutes(config, phase) == expected

    def test_legacy_global_applies_to_all_phases(self):
        config = PipelineConfig(consensus_timeout_minutes=45)
        assert resolve_consensus_timeout_minutes(config, "refine") == 45
        assert resolve_consensus_timeout_minutes(config, "plan") == 45
        assert resolve_consensus_timeout_minutes(config, "implement") == 45

    def test_per_phase_override_wins_over_legacy_global(self):
        config = PipelineConfig(
            consensus_timeout_minutes=45,
            consensus_timeout_minutes_implement=120,
        )
        assert resolve_consensus_timeout_minutes(config, "refine") == 45
        assert resolve_consensus_timeout_minutes(config, "plan") == 45
        assert resolve_consensus_timeout_minutes(config, "implement") == 120

    def test_per_phase_override_alone_uses_phase_defaults_for_others(self):
        config = PipelineConfig(consensus_timeout_minutes_plan=15)
        assert resolve_consensus_timeout_minutes(config, "refine") == 30
        assert resolve_consensus_timeout_minutes(config, "plan") == 15
        assert resolve_consensus_timeout_minutes(config, "implement") == 90

    def test_unknown_phase_falls_back_to_refine_default(self):
        config = PipelineConfig()
        expected = PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["refine"]
        assert resolve_consensus_timeout_minutes(config, "totally-unknown") == expected

    def test_unknown_phase_still_honors_legacy_global(self):
        config = PipelineConfig(consensus_timeout_minutes=45)
        assert resolve_consensus_timeout_minutes(config, "totally-unknown") == 45


class TestStartPhaseValidator:
    """Tests for PipelineConfig.start_phase validation."""

    def test_start_phase_plan_accepted(self):
        config = PipelineConfig(start_phase="plan")
        assert config.start_phase == "plan"

    def test_start_phase_implement_accepted(self):
        config = PipelineConfig(start_phase="implement")
        assert config.start_phase == "implement"

    def test_start_phase_none_accepted(self):
        config = PipelineConfig(start_phase=None)
        assert config.start_phase is None

    def test_start_phase_pr_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid start_phase"):
            PipelineConfig(start_phase="pr")

    def test_start_phase_refine_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid start_phase"):
            PipelineConfig(start_phase="refine")

    def test_start_phase_invalid_string_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid start_phase"):
            PipelineConfig(start_phase="nonexistent")


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

    def test_add_decision_with_decision_type(self):
        """Test adding a decision with decision_type parameter."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        decision = pipeline.add_decision(
            question="Approve the plan?",
            options=["approve", "request changes"],
            decision_type="phase_gate",
        )
        assert decision.decision_type == "phase_gate"

    def test_add_decision_with_questions(self):
        """Test adding a decision with questions parameter."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        questions = [{"id": "q-1", "question": "Why?", "answer": ""}]
        decision = pipeline.add_decision(
            question="Feedback needed",
            decision_type="feedback",
            questions=questions,
        )
        assert decision.decision_type == "feedback"
        assert len(decision.questions) == 1

    def test_add_decision_default_type(self):
        """Test that add_decision defaults to decision_type='choice'."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        decision = pipeline.add_decision(question="Pick one?", options=["A", "B"])
        assert decision.decision_type == "choice"
        assert decision.questions == []

    def test_add_decision_with_content_changed(self):
        """Test that add_decision passes content_changed to the decision."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        decision = pipeline.add_decision(
            question="Approve?",
            decision_type="phase_gate",
            content_changed=True,
        )
        assert decision.content_changed is True

    def test_add_decision_content_changed_default_none(self):
        """Test that add_decision defaults content_changed to None."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        decision = pipeline.add_decision(question="Pick one?")
        assert decision.content_changed is None

    def test_backward_compat_old_format_dict(self):
        """Test that old-format dict without decision_type/questions parses correctly."""
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
                "decisions": [
                    {
                        "id": "decision-1",
                        "question": "Approve?",
                        "context": "",
                        "options": ["approve"],
                        "status": "pending",
                        "created_at": "2024-01-01T00:00:00",
                        "resolved_at": None,
                        "resolution": None,
                    }
                ],
            }
        )
        restored = Pipeline.model_validate_json(raw)
        assert len(restored.decisions) == 1
        assert restored.decisions[0].decision_type == "choice"
        assert restored.decisions[0].questions == []


class TestPipelineEpicFields:
    """Tests for the Jira-epic SDLC fields on ``Pipeline`` (issue #1557).

    Covers:
    - ``Pipeline.is_epic`` default + roundtrip.
    - ``Pipeline.pipeline_mode`` default + roundtrip.
    - ``Pipeline.pr_url`` default, validator (None / empty trim /
      http / https / non-http rejection), roundtrip.

    Acceptance criteria reference (slice-2 task-2-2):
    "Pipeline.pr_url round-trips through state_store" — the model layer
    is exercised here; the state_store layer is exercised in
    ``test_state_store.py::TestPipelinesForJiraTicket``.
    """

    def _base_pipeline_kwargs(self) -> dict:
        return {
            "id": "issue-1557",
            "issue_number": 1557,
            "repo": "owner/repo",
            "branch": "egg/issue-1557",
        }

    def test_is_epic_default_false(self):
        """Default Pipeline.is_epic is False (non-epic pipelines)."""
        pipeline = Pipeline(**self._base_pipeline_kwargs())
        assert pipeline.is_epic is False

    def test_pipeline_mode_default_none(self):
        """Default Pipeline.pipeline_mode is None (only set for epic)."""
        pipeline = Pipeline(**self._base_pipeline_kwargs())
        assert pipeline.pipeline_mode is None

    def test_pr_url_default_none(self):
        """Default Pipeline.pr_url is None until PR is opened."""
        pipeline = Pipeline(**self._base_pipeline_kwargs())
        assert pipeline.pr_url is None

    def test_is_epic_true_persists(self):
        """``is_epic=True`` is persisted on the model."""
        pipeline = Pipeline(**self._base_pipeline_kwargs(), is_epic=True)
        assert pipeline.is_epic is True

    def test_pipeline_mode_fresh_persists(self):
        """``pipeline_mode='fresh'`` round-trips through model_dump."""
        pipeline = Pipeline(
            **self._base_pipeline_kwargs(),
            is_epic=True,
            pipeline_mode="fresh",
        )
        assert pipeline.pipeline_mode == "fresh"
        roundtrip = Pipeline.model_validate(pipeline.model_dump())
        assert roundtrip.pipeline_mode == "fresh"

    def test_pipeline_mode_reassess_persists(self):
        """``pipeline_mode='reassess'`` round-trips through model_dump."""
        pipeline = Pipeline(
            **self._base_pipeline_kwargs(),
            is_epic=True,
            pipeline_mode="reassess",
        )
        assert pipeline.pipeline_mode == "reassess"
        roundtrip = Pipeline.model_validate(pipeline.model_dump())
        assert roundtrip.pipeline_mode == "reassess"

    def test_pipeline_mode_invalid_rejected(self):
        """Non-Literal pipeline_mode raises a pydantic ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Pipeline(
                **self._base_pipeline_kwargs(),
                is_epic=True,
                pipeline_mode="bogus-mode",  # type: ignore[arg-type]
            )

    def test_pr_url_https_accepted(self):
        """Valid https:// URL is preserved."""
        pipeline = Pipeline(
            **self._base_pipeline_kwargs(),
            pr_url="https://github.com/owner/repo/pull/123",
        )
        assert pipeline.pr_url == "https://github.com/owner/repo/pull/123"

    def test_pr_url_http_accepted(self):
        """Plain http:// URL accepted (docstring: deliberately permissive)."""
        pipeline = Pipeline(
            **self._base_pipeline_kwargs(),
            pr_url="http://example.com/pull/9",
        )
        assert pipeline.pr_url == "http://example.com/pull/9"

    def test_pr_url_empty_string_normalised_to_none(self):
        """Empty / whitespace-only pr_url normalises to None."""
        pipeline = Pipeline(**self._base_pipeline_kwargs(), pr_url="   ")
        assert pipeline.pr_url is None

    def test_pr_url_non_http_rejected(self):
        """Non-http(s) URL (e.g. ftp://, file://) is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Pipeline(
                **self._base_pipeline_kwargs(),
                pr_url="ftp://example.com/x",
            )

    def test_pr_url_non_string_rejected(self):
        """Non-string pr_url raises a validation error."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Pipeline(
                **self._base_pipeline_kwargs(),
                pr_url=12345,  # type: ignore[arg-type]
            )

    def test_pipeline_full_epic_roundtrip(self):
        """Full epic pipeline (is_epic + pipeline_mode + pr_url) roundtrip."""
        pipeline = Pipeline(
            **self._base_pipeline_kwargs(),
            is_epic=True,
            pipeline_mode="reassess",
            pr_url="https://github.com/owner/repo/pull/456",
        )
        roundtrip = Pipeline.model_validate(pipeline.model_dump())
        assert roundtrip.is_epic is True
        assert roundtrip.pipeline_mode == "reassess"
        assert roundtrip.pr_url == "https://github.com/owner/repo/pull/456"


class TestAgentRole:
    """Tests for AgentRole enum."""

    def test_all_roles(self):
        """Test all agent roles are defined."""
        roles = list(AgentRole)
        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        # Issue #1557 — APPLIER joined the registry for Jira-epic
        # SDLC support (drives gateway Jira mutations after HITL
        # approval on epic-mode pipelines).
        assert AgentRole.APPLIER in roles
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles
        assert AgentRole.REFINER in roles
        assert AgentRole.INSPECTOR in roles
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CODE_HOLISTIC in roles
        assert AgentRole.REVIEWER_CONTRACT in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert AgentRole.REVIEWER_REFINE in roles
        assert AgentRole.REVIEWER_PLAN in roles
        assert AgentRole.REVIEWER_SECURITY in roles
        assert AgentRole.REVIEWER_CONCURRENCY in roles
        assert AgentRole.OVERSEER in roles
        assert AgentRole.AUTOFIXER in roles
        assert AgentRole.CONFLICT_RESOLVER in roles
        # Issue #1557: APPLIER asserted above next to the other execution
        # roles (CODER / TESTER / DOCUMENTER); count assertion below
        # pins the registry size including APPLIER.
        assert len(roles) == 20


class TestBackwardCompatibility:
    """Tests for backward compatibility with removed enum values."""

    def test_reviewer_unified_no_longer_in_enum(self):
        """reviewer_unified has been removed from AgentRole enum."""
        assert not hasattr(AgentRole, "REVIEWER_UNIFIED"), (
            "AgentRole.REVIEWER_UNIFIED should be removed"
        )

    def test_checker_no_longer_in_enum(self):
        """checker has been removed from AgentRole enum."""
        assert not hasattr(AgentRole, "CHECKER"), "AgentRole.CHECKER should be removed"

    def test_checker_deserializes_as_tester(self):
        """Persisted pipeline state with role='checker' migrates to tester."""
        agent = AgentExecution.model_validate({"role": "checker"})
        assert agent.role == AgentRole.TESTER

    def test_reviewer_unified_deserializes_as_reviewer_code(self):
        """Persisted pipeline state with role='reviewer_unified' migrates to reviewer_code."""
        agent = AgentExecution.model_validate({"role": "reviewer_unified"})
        assert agent.role == AgentRole.REVIEWER_CODE

    def test_checker_in_container_info_deserializes(self):
        """ContainerInfo with agent_role='checker' migrates to tester."""
        info = ContainerInfo.model_validate(
            {"container_id": "c1", "container_name": "test", "agent_role": "checker"}
        )
        assert info.agent_role == AgentRole.TESTER

    def test_reviewer_unified_in_container_info_deserializes(self):
        """ContainerInfo with agent_role='reviewer_unified' migrates to reviewer_code."""
        info = ContainerInfo.model_validate(
            {"container_id": "c1", "container_name": "test", "agent_role": "reviewer_unified"}
        )
        assert info.agent_role == AgentRole.REVIEWER_CODE

    def test_generic_reviewer_no_longer_in_enum(self):
        """Generic REVIEWER has been removed from AgentRole enum."""
        assert not hasattr(AgentRole, "REVIEWER"), "AgentRole.REVIEWER should be removed"

    def test_generic_reviewer_deserializes_as_reviewer_code(self):
        """Persisted pipeline state with role='reviewer' migrates to reviewer_code."""
        agent = AgentExecution.model_validate({"role": "reviewer"})
        assert agent.role == AgentRole.REVIEWER_CODE

    def test_valid_roles_unaffected_by_migration(self):
        """Existing valid roles are not changed by the migration validator."""
        agent = AgentExecution.model_validate({"role": "coder"})
        assert agent.role == AgentRole.CODER

    def test_decision_timeout_still_valid(self):
        """Ensure the existing vestigial DecisionStatus.TIMEOUT still works."""
        assert DecisionStatus.TIMEOUT == "timeout"


class TestPipelinePhase:
    """Tests for PipelinePhase enum."""

    def test_phase_order(self):
        """Test phases are defined in SDLC order.

        Issue #1557: the APPLY phase is conditional — inserted between
        PLAN and IMPLEMENT only when ``Pipeline.is_epic`` is True. The
        enum order reflects the canonical sequence so iteration matches
        execution order for epic pipelines; non-epic pipelines skip
        APPLY via the orchestrator scheduler.
        """
        phases = list(PipelinePhase)
        assert phases[0] == PipelinePhase.REFINE
        assert phases[1] == PipelinePhase.PLAN
        assert phases[2] == PipelinePhase.APPLY
        assert phases[3] == PipelinePhase.IMPLEMENT
        assert phases[4] == PipelinePhase.PR

    def test_apply_phase_exists(self):
        """Issue #1557: APPLY phase enum is present and round-trips."""
        assert PipelinePhase.APPLY == "apply"
        # StrEnum: value-equal to string for serialisation symmetry.
        assert PipelinePhase("apply") == PipelinePhase.APPLY
