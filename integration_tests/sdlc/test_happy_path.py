"""Integration test for SDLC pipeline happy path."""

import json
import pytest
import sys
import tempfile
from pathlib import Path

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "gateway"))

from egg_contracts import (
    Contract,
    Issue,
    Phase,
    Task,
    load_contract,
    save_contract,
)
from egg_contracts.models import PhaseStatus, PipelinePhase, TaskStatus
from phase_transition import execute_transition, validate_transition


@pytest.fixture
def temp_repo():
    """Create a temporary repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / ".egg" / "contracts").mkdir(parents=True)
        (repo_root / "docs" / "issues").mkdir(parents=True)
        yield repo_root


@pytest.fixture
def full_contract(temp_repo):
    """Create a contract with all phases and tasks."""
    contract = Contract(
        schemaVersion="1.0",
        issue=Issue(
            number=100,
            title="Test Issue",
            url="https://github.com/test/repo/issues/100",
        ),
        currentPhase=PipelinePhase.REFINE,
        branch="egg/issue-100",
        phases=[
            Phase(
                id="phase-1",
                name="Schema",
                status=PhaseStatus.PENDING,
                tasks=[
                    Task(id="task-1", description="Create schema", status=TaskStatus.PENDING),
                    Task(id="task-2", description="Add validation", status=TaskStatus.PENDING),
                ],
            ),
            Phase(
                id="phase-2",
                name="Implementation",
                status=PhaseStatus.PENDING,
                tasks=[
                    Task(id="task-3", description="Implement models", status=TaskStatus.PENDING),
                ],
            ),
        ],
        decisions=[],
        audit_log=[],
    )
    save_contract(contract, temp_repo)
    return contract


class TestHappyPath:
    """Test the happy path through the SDLC pipeline."""

    def test_refine_to_plan_transition(self, temp_repo, full_contract):
        """Test transitioning from refine to plan phase."""
        # Validate transition
        result = validate_transition(full_contract, "plan", "human")
        assert result.success is True

        # Execute transition
        result = execute_transition(temp_repo, 100, "plan", "test-user", "human")
        assert result.success is True
        assert result.from_phase == "refine"
        assert result.to_phase == "plan"

        # Verify contract updated
        contract = load_contract(temp_repo, 100)
        assert contract.currentPhase == PipelinePhase.PLAN

    def test_plan_to_implement_transition(self, temp_repo, full_contract):
        """Test transitioning from plan to implement phase."""
        # First transition to plan
        execute_transition(temp_repo, 100, "plan", "test-user", "human")

        # Then transition to implement
        result = execute_transition(temp_repo, 100, "implement", "test-user", "human")
        assert result.success is True
        assert result.to_phase == "implement"

    def test_implement_to_pr_requires_complete_tasks(self, temp_repo, full_contract):
        """Test that implement->pr requires all tasks complete."""
        # Transition to implement phase
        execute_transition(temp_repo, 100, "plan", "test-user", "human")
        execute_transition(temp_repo, 100, "implement", "test-user", "human")

        # Try to transition to PR with incomplete tasks
        result = execute_transition(temp_repo, 100, "pr", "test-user", "reviewer")
        assert result.success is False
        assert "complete" in result.message.lower() or "incomplete" in result.message.lower()

    def test_implement_to_pr_with_complete_tasks(self, temp_repo, full_contract):
        """Test implement->pr succeeds when all tasks complete."""
        # Setup: transition to implement and complete all tasks
        execute_transition(temp_repo, 100, "plan", "test-user", "human")
        execute_transition(temp_repo, 100, "implement", "test-user", "human")

        contract = load_contract(temp_repo, 100)
        for phase in contract.phases:
            for task in phase.tasks:
                task.status = TaskStatus.COMPLETE
        save_contract(contract, temp_repo)

        # Now transition should succeed
        result = execute_transition(temp_repo, 100, "pr", "test-user", "reviewer")
        assert result.success is True
        assert result.to_phase == "pr"

    def test_full_pipeline_flow(self, temp_repo, full_contract):
        """Test complete pipeline from refine to PR."""
        # Step 1: Refine -> Plan (human approval)
        result = execute_transition(temp_repo, 100, "plan", "human-user", "human")
        assert result.success is True

        # Step 2: Plan -> Implement (human approval)
        result = execute_transition(temp_repo, 100, "implement", "human-user", "human")
        assert result.success is True

        # Step 3: Complete all tasks (simulating implementer work)
        contract = load_contract(temp_repo, 100)
        for phase in contract.phases:
            for task in phase.tasks:
                task.status = TaskStatus.COMPLETE
                task.commit = "abc1234"
        save_contract(contract, temp_repo)

        # Step 4: Implement -> PR (reviewer approval)
        result = execute_transition(temp_repo, 100, "pr", "reviewer-bot", "reviewer")
        assert result.success is True

        # Verify final state
        contract = load_contract(temp_repo, 100)
        assert contract.currentPhase == PipelinePhase.PR
        assert all(
            task.status == TaskStatus.COMPLETE
            for phase in contract.phases
            for task in phase.tasks
        )


class TestRoleEnforcement:
    """Test role-based access control in pipeline."""

    def test_implementer_cannot_advance_phase(self, temp_repo, full_contract):
        """Test that implementer cannot advance phases."""
        result = execute_transition(temp_repo, 100, "plan", "agent", "implementer")
        assert result.success is False
        assert "human" in result.message.lower()

    def test_reviewer_cannot_advance_to_plan(self, temp_repo, full_contract):
        """Test that reviewer cannot advance refine->plan."""
        result = execute_transition(temp_repo, 100, "plan", "reviewer", "reviewer")
        assert result.success is False

    def test_reviewer_can_advance_implement_to_pr(self, temp_repo, full_contract):
        """Test that reviewer can advance implement->pr."""
        # Setup
        execute_transition(temp_repo, 100, "plan", "human", "human")
        execute_transition(temp_repo, 100, "implement", "human", "human")

        contract = load_contract(temp_repo, 100)
        for phase in contract.phases:
            for task in phase.tasks:
                task.status = TaskStatus.COMPLETE
        save_contract(contract, temp_repo)

        # Reviewer should be able to advance
        result = execute_transition(temp_repo, 100, "pr", "reviewer", "reviewer")
        assert result.success is True

    def test_human_can_advance_any_phase(self, temp_repo, full_contract):
        """Test that human can advance any phase."""
        # Human can do refine->plan
        result = execute_transition(temp_repo, 100, "plan", "human", "human")
        assert result.success is True

        # Human can do plan->implement
        result = execute_transition(temp_repo, 100, "implement", "human", "human")
        assert result.success is True
