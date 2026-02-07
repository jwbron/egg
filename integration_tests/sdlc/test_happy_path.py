"""Integration tests for SDLC pipeline happy path.

Tests the full pipeline success scenario where:
1. Contract is created from issue
2. Phases progress from refine → plan → implement → pr
3. Tasks are completed by implementer
4. Tasks are approved by reviewer
5. Pipeline completes successfully
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

# Add shared directory to path
_shared_path = Path(__file__).parent.parent.parent / "shared"
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts import (
    AuditRole,
    Contract,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    Role,
    Task,
    TaskStatus,
    apply_mutation,
    create_contract,
    load_contract,
    save_contract,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # Create .egg-state/contracts directory
        contracts_dir = repo_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_path


@pytest.fixture
def sample_issue_info():
    """Create sample issue info for testing."""
    return IssueInfo(
        number=133,
        title="Add structurally enforced checkpoints",
        url="https://github.com/test-owner/test-repo/issues/133",
    )


@pytest.fixture
def sample_contract(sample_issue_info):
    """Create a sample contract with phases and tasks."""
    contract = Contract(
        schemaVersion="1.0",
        issue=sample_issue_info,
        current_phase=PipelinePhase.IMPLEMENT,
        phases=[
            Phase(
                id="phase-1",
                name="Core Library",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(
                        id="task-1-1",
                        description="Create contract schema",
                        status=TaskStatus.PENDING,
                        acceptance_criteria="Schema validates sample contracts",
                    ),
                    Task(
                        id="task-1-2",
                        description="Implement loader",
                        status=TaskStatus.PENDING,
                        acceptance_criteria="Loader reads/writes contracts correctly",
                    ),
                ],
            ),
            Phase(
                id="phase-2",
                name="Gateway Integration",
                status=PhaseStatus.PENDING,
                tasks=[
                    Task(
                        id="task-2-1",
                        description="Add contract API endpoints",
                        status=TaskStatus.PENDING,
                        acceptance_criteria="API endpoints respond correctly",
                    ),
                ],
            ),
        ],
    )
    return contract


class TestHappyPathContractLifecycle:
    """Tests for the complete contract lifecycle in a successful run."""

    def test_contract_creation_and_persistence(self, temp_repo, sample_issue_info):
        """Contract can be created and persisted to disk."""
        # Create contract
        contract = create_contract(
            issue_number=sample_issue_info.number,
            title=sample_issue_info.title,
            url=sample_issue_info.url,
            repo_root=temp_repo,
        )
        assert contract.issue.number == 133
        assert contract.current_phase == PipelinePhase.REFINE

        # Save to disk
        save_contract(contract, temp_repo)

        # Load back
        loaded = load_contract(133, temp_repo)
        assert loaded.issue.number == 133
        assert loaded.issue.title == sample_issue_info.title

    def test_phase_progression_refine_to_plan(self, temp_repo, sample_contract):
        """Phase can progress from refine to plan with human approval."""
        sample_contract.current_phase = PipelinePhase.REFINE
        save_contract(sample_contract, temp_repo)

        # Human approves transition to plan phase
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="human-reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.PLAN.value,
            reason="Analysis approved, proceed to planning",
        )

        assert result.success
        assert result.contract is not None
        assert result.contract.current_phase == PipelinePhase.PLAN

        # Verify audit log
        assert len(result.contract.audit_log) > 0
        last_entry = result.contract.audit_log[-1]
        assert last_entry.field_path == "current_phase"
        assert last_entry.new_value == "plan"

    def test_phase_progression_plan_to_implement(self, temp_repo, sample_contract):
        """Phase can progress from plan to implement with human approval."""
        sample_contract.current_phase = PipelinePhase.PLAN
        save_contract(sample_contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="human-reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.IMPLEMENT.value,
            reason="Plan approved, proceed to implementation",
        )

        assert result.success
        assert result.contract is not None
        assert result.contract.current_phase == PipelinePhase.IMPLEMENT

    def test_phase_progression_implement_to_pr(self, temp_repo, sample_contract):
        """Phase can progress from implement to PR with reviewer approval."""
        sample_contract.current_phase = PipelinePhase.IMPLEMENT
        # Mark all tasks complete first
        for phase in sample_contract.phases:
            phase.status = PhaseStatus.COMPLETE
            for task in phase.tasks:
                task.status = TaskStatus.COMPLETE
        save_contract(sample_contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.PR.value,
            reason="All tasks complete, creating PR",
        )

        assert result.success
        assert result.contract is not None
        assert result.contract.current_phase == PipelinePhase.PR


class TestHappyPathTaskExecution:
    """Tests for task execution in a successful run."""

    def test_implementer_adds_commit_to_task(self, temp_repo, sample_contract):
        """Implementer can add commit reference to a task."""
        save_contract(sample_contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
            reason="Implementation commit",
        )

        assert result.success
        assert result.contract is not None
        task = result.contract.phases[0].tasks[0]
        assert task.commit == "abc1234"

    def test_implementer_adds_notes_to_task(self, temp_repo, sample_contract):
        """Implementer can add notes to a task."""
        save_contract(sample_contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.notes",
            new_value="Created schema with Pydantic models",
            reason="Implementation notes",
        )

        assert result.success
        assert result.contract is not None
        task = result.contract.phases[0].tasks[0]
        assert "Pydantic" in task.notes

    def test_reviewer_marks_task_complete(self, temp_repo, sample_contract):
        """Reviewer can mark a task as complete."""
        # First, implementer adds commit
        sample_contract.phases[0].tasks[0].commit = "abc1234"
        save_contract(sample_contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
            reason="Task implementation verified",
        )

        assert result.success
        assert result.contract is not None
        task = result.contract.phases[0].tasks[0]
        assert task.status == TaskStatus.COMPLETE

    def test_reviewer_marks_phase_complete(self, temp_repo, sample_contract):
        """Reviewer can mark a phase as complete when all tasks are done."""
        # Mark all tasks in phase-1 as complete
        for task in sample_contract.phases[0].tasks:
            task.status = TaskStatus.COMPLETE
            task.commit = "abc1234"
        save_contract(sample_contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.0.status",
            new_value=PhaseStatus.COMPLETE.value,
            reason="All tasks verified",
        )

        assert result.success
        assert result.contract is not None
        phase = result.contract.phases[0]
        assert phase.status == PhaseStatus.COMPLETE


class TestHappyPathFullPipeline:
    """End-to-end test for complete pipeline success."""

    def test_complete_pipeline_success(self, temp_repo, sample_contract):
        """Full pipeline completes successfully with proper state transitions."""
        # Start in implement phase with tasks pending
        sample_contract.current_phase = PipelinePhase.IMPLEMENT
        save_contract(sample_contract, temp_repo)

        # Step 1: Implementer completes task 1-1
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 2: Reviewer approves task 1-1
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 3: Implementer completes task 1-2
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.1.commit",
            new_value="def2345",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 4: Reviewer approves task 1-2
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.0.tasks.1.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 5: Reviewer marks phase-1 complete
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.0.status",
            new_value=PhaseStatus.COMPLETE.value,
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 6: Implementer completes task 2-1
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.1.tasks.0.commit",
            new_value="fab3456",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 7: Reviewer approves task 2-1
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.1.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 8: Reviewer marks phase-2 complete
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.1.status",
            new_value=PhaseStatus.COMPLETE.value,
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 9: Transition to PR phase
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.PR.value,
            reason="All phases complete, creating PR",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Verify final state
        final_contract = load_contract(133, temp_repo)
        assert final_contract.current_phase == PipelinePhase.PR

        # All tasks should be complete
        for phase in final_contract.phases:
            assert phase.status == PhaseStatus.COMPLETE
            for task in phase.tasks:
                assert task.status == TaskStatus.COMPLETE
                assert task.commit is not None

        # Audit log should have all transitions
        assert len(final_contract.audit_log) >= 9

    def test_audit_log_tracks_all_changes(self, temp_repo, sample_contract):
        """Audit log properly tracks all mutations."""
        save_contract(sample_contract, temp_repo)

        # Make several changes
        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        save_contract(result.contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.notes",
            new_value="Implementation notes",
        )
        save_contract(result.contract, temp_repo)

        contract = load_contract(133, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        save_contract(result.contract, temp_repo)

        # Verify audit log
        final = load_contract(133, temp_repo)
        assert len(final.audit_log) >= 3

        # Check that entries have correct structure
        for entry in final.audit_log:
            assert entry.actor is not None
            assert entry.role is not None
            assert entry.field_path is not None
            assert entry.timestamp is not None
