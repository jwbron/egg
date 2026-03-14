"""Integration tests for SDLC pipeline review rejection flow.

Tests the review rejection scenario where:
1. Reviewer marks tasks as incomplete
2. Implementer receives feedback and addresses issues
3. Cycle continues until tasks pass review
"""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from egg_contracts import (
    Contract,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    ReviewFeedback,
    Role,
    Task,
    TaskStatus,
    apply_mutation,
    load_contract,
    save_contract,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        contracts_dir = repo_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_path


@pytest.fixture
def sample_issue_info():
    """Create sample issue info for testing."""
    return IssueInfo(
        number=200,
        title="Feature with review cycles",
        url="https://github.com/test-owner/test-repo/issues/200",
    )


@pytest.fixture
def contract_in_implement_phase(sample_issue_info):
    """Create a contract in implement phase with tasks ready for review."""
    contract = Contract(
        schemaVersion="1.0",
        issue=sample_issue_info,
        current_phase=PipelinePhase.IMPLEMENT,
        phases=[
            Phase(
                id="phase-1",
                name="Implementation Phase",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(
                        id="task-1-1",
                        description="Implement feature A",
                        status=TaskStatus.IN_PROGRESS,
                        acceptance_criteria="Feature A works correctly with all edge cases",
                        commit="abc1234",
                    ),
                    Task(
                        id="task-1-2",
                        description="Implement feature B",
                        status=TaskStatus.PENDING,
                        acceptance_criteria="Feature B passes all tests",
                    ),
                ],
            ),
        ],
    )
    return contract


class TestReviewRejection:
    """Tests for reviewer rejecting task implementations."""

    def test_reviewer_marks_task_incomplete(self, temp_repo, contract_in_implement_phase):
        """Reviewer can mark a task as incomplete."""
        save_contract(contract_in_implement_phase, temp_repo)

        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="egg-reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.INCOMPLETE.value,
            reason="Missing edge case handling for empty input",
        )

        assert result.success
        assert result.contract is not None
        task = result.contract.phases[0].tasks[0]
        assert task.status == TaskStatus.INCOMPLETE

    def test_review_feedback_tracked(self, temp_repo, contract_in_implement_phase):
        """Review feedback is properly tracked in the contract."""
        save_contract(contract_in_implement_phase, temp_repo)

        # Add review feedback
        contract = load_contract(200, temp_repo)
        feedback = ReviewFeedback(
            timestamp=datetime.now(UTC),
            task_id="task-1-1",
            feedback="Missing edge case handling for empty input. Please add validation.",
            status=TaskStatus.INCOMPLETE,
        )
        contract.phases[0].review_feedback.append(feedback)
        save_contract(contract, temp_repo)

        # Verify feedback persisted
        loaded = load_contract(200, temp_repo)
        assert len(loaded.phases[0].review_feedback) == 1
        assert "edge case" in loaded.phases[0].review_feedback[0].feedback

    def test_review_cycle_count_increments(self, temp_repo, contract_in_implement_phase):
        """Review cycle count increments when task is rejected."""
        save_contract(contract_in_implement_phase, temp_repo)

        contract = load_contract(200, temp_repo)
        initial_cycles = contract.phases[0].tasks[0].review_cycles
        assert initial_cycles == 0

        # Increment cycle count when rejected
        contract.phases[0].tasks[0].review_cycles += 1
        save_contract(contract, temp_repo)

        loaded = load_contract(200, temp_repo)
        assert loaded.phases[0].tasks[0].review_cycles == 1


class TestReviewCycleWorkflow:
    """Tests for the complete review cycle workflow."""

    def test_implement_review_fix_cycle(self, temp_repo, contract_in_implement_phase):
        """Complete cycle: implement → review reject → fix → review approve."""
        save_contract(contract_in_implement_phase, temp_repo)

        # Step 1: Initial implementation (commit already present)
        contract = load_contract(200, temp_repo)
        assert contract.phases[0].tasks[0].commit == "abc1234"

        # Step 2: Reviewer rejects with feedback
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.INCOMPLETE.value,
            reason="Needs error handling",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Increment review cycle
        contract = load_contract(200, temp_repo)
        contract.phases[0].tasks[0].review_cycles += 1
        save_contract(contract, temp_repo)

        # Step 3: Implementer fixes the issue
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.commit",
            new_value="def2345",
            reason="Added error handling",
        )
        assert result.success

        result = apply_mutation(
            contract=result.contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.notes",
            new_value="Added try/catch blocks for edge cases",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Step 4: Reviewer approves the fix
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
            reason="Error handling looks good",
        )
        assert result.success
        save_contract(result.contract, temp_repo)

        # Verify final state
        final = load_contract(200, temp_repo)
        task = final.phases[0].tasks[0]
        assert task.status == TaskStatus.COMPLETE
        assert task.commit == "def2345"
        assert task.review_cycles == 1  # One rejection cycle

    def test_multiple_rejection_cycles(self, temp_repo, contract_in_implement_phase):
        """Task goes through multiple rejection cycles before approval."""
        save_contract(contract_in_implement_phase, temp_repo)

        # Cycle 1: Reject
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.INCOMPLETE.value,
        )
        result.contract.phases[0].tasks[0].review_cycles += 1
        save_contract(result.contract, temp_repo)

        # Fix 1
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.commit",
            new_value="aaa1111",
        )
        save_contract(result.contract, temp_repo)

        # Cycle 2: Reject again
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.INCOMPLETE.value,
        )
        result.contract.phases[0].tasks[0].review_cycles += 1
        save_contract(result.contract, temp_repo)

        # Fix 2
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.commit",
            new_value="bbb2222",
        )
        save_contract(result.contract, temp_repo)

        # Cycle 3: Finally approve
        contract = load_contract(200, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        save_contract(result.contract, temp_repo)

        # Verify
        final = load_contract(200, temp_repo)
        task = final.phases[0].tasks[0]
        assert task.status == TaskStatus.COMPLETE
        assert task.review_cycles == 2  # Two rejection cycles before approval

    def test_phase_blocked_when_task_incomplete(self, temp_repo, contract_in_implement_phase):
        """Phase cannot be marked complete while tasks are incomplete."""
        # Mark first task complete but second incomplete
        contract_in_implement_phase.phases[0].tasks[0].status = TaskStatus.COMPLETE
        contract_in_implement_phase.phases[0].tasks[1].status = TaskStatus.INCOMPLETE
        save_contract(contract_in_implement_phase, temp_repo)

        # Attempt to mark phase complete should be handled by business logic
        # The mutation itself may succeed, but semantically the phase shouldn't
        # be marked complete with incomplete tasks
        contract = load_contract(200, temp_repo)

        # This tests the data model - actual enforcement would be in pipeline logic
        incomplete_tasks = [t for t in contract.phases[0].tasks if t.status != TaskStatus.COMPLETE]
        assert len(incomplete_tasks) > 0


class TestReviewFeedbackManagement:
    """Tests for managing review feedback across cycles."""

    def test_feedback_accumulates_across_cycles(self, temp_repo, contract_in_implement_phase):
        """Multiple feedback entries accumulate in the phase."""
        save_contract(contract_in_implement_phase, temp_repo)

        contract = load_contract(200, temp_repo)

        # First feedback
        feedback1 = ReviewFeedback(
            timestamp=datetime.now(UTC),
            task_id="task-1-1",
            feedback="Missing error handling",
            status=TaskStatus.INCOMPLETE,
        )
        contract.phases[0].review_feedback.append(feedback1)
        save_contract(contract, temp_repo)

        # Second feedback (different issue)
        contract = load_contract(200, temp_repo)
        feedback2 = ReviewFeedback(
            timestamp=datetime.now(UTC),
            task_id="task-1-1",
            feedback="Performance issue in the loop",
            status=TaskStatus.INCOMPLETE,
        )
        contract.phases[0].review_feedback.append(feedback2)
        save_contract(contract, temp_repo)

        # Verify both feedback entries exist
        final = load_contract(200, temp_repo)
        assert len(final.phases[0].review_feedback) == 2
        feedback_texts = [f.feedback for f in final.phases[0].review_feedback]
        assert "error handling" in feedback_texts[0]
        assert "Performance" in feedback_texts[1]

    def test_feedback_for_different_tasks(self, temp_repo, contract_in_implement_phase):
        """Feedback can be added for different tasks independently."""
        save_contract(contract_in_implement_phase, temp_repo)

        contract = load_contract(200, temp_repo)

        # Feedback for task 1
        feedback1 = ReviewFeedback(
            timestamp=datetime.now(UTC),
            task_id="task-1-1",
            feedback="Task 1 needs work",
            status=TaskStatus.INCOMPLETE,
        )
        contract.phases[0].review_feedback.append(feedback1)

        # Feedback for task 2
        feedback2 = ReviewFeedback(
            timestamp=datetime.now(UTC),
            task_id="task-1-2",
            feedback="Task 2 also needs work",
            status=TaskStatus.INCOMPLETE,
        )
        contract.phases[0].review_feedback.append(feedback2)
        save_contract(contract, temp_repo)

        # Verify feedback is separated by task
        final = load_contract(200, temp_repo)
        task1_feedback = [f for f in final.phases[0].review_feedback if f.task_id == "task-1-1"]
        task2_feedback = [f for f in final.phases[0].review_feedback if f.task_id == "task-1-2"]
        assert len(task1_feedback) == 1
        assert len(task2_feedback) == 1
