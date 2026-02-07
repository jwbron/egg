"""Tests for phase transition logic."""

import sys
import tempfile
from pathlib import Path

import pytest

# Add gateway and shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "gateway"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts import save_contract
from egg_contracts.models import (
    Contract,
    Issue,
    Phase,
    PhaseStatus,
    PipelinePhase,
    Task,
    TaskStatus,
)
from phase_transition import (
    TransitionResult,
    can_transition,
    check_implementation_complete,
    execute_transition,
    get_current_phase,
    get_exit_requirement,
    get_next_phase,
    validate_transition,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository with contract."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        contracts_dir = repo_root / ".egg" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_root


@pytest.fixture
def contract_in_refine(temp_repo):
    """Create a contract in refine phase."""
    contract = Contract(
        issue=Issue(number=123, title="Test", url="https://example.com/123"),
        currentPhase=PipelinePhase.REFINE,
        branch="egg/issue-123",
        phases=[],
    )
    save_contract(contract, temp_repo)
    return contract, temp_repo


@pytest.fixture
def contract_in_implement(temp_repo):
    """Create a contract in implement phase with tasks."""
    contract = Contract(
        issue=Issue(number=124, title="Test", url="https://example.com/124"),
        currentPhase=PipelinePhase.IMPLEMENT,
        branch="egg/issue-124",
        phases=[
            Phase(
                id="phase-1",
                name="Implementation",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(id="task-1", description="Task 1", status=TaskStatus.COMPLETE),
                    Task(id="task-2", description="Task 2", status=TaskStatus.PENDING),
                ],
            )
        ],
    )
    save_contract(contract, temp_repo)
    return contract, temp_repo


class TestTransitionResult:
    """Tests for TransitionResult dataclass."""

    def test_success_result(self):
        result = TransitionResult(
            success=True,
            message="Transitioned",
            from_phase="refine",
            to_phase="plan",
        )
        assert result.success is True

    def test_to_dict(self):
        result = TransitionResult(
            success=False,
            message="Cannot transition",
            from_phase="refine",
            to_phase="implement",
            details={"reason": "Must go to plan first"},
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["from_phase"] == "refine"
        assert "details" in d


class TestCanTransition:
    """Tests for transition validation."""

    def test_refine_to_plan(self):
        assert can_transition("refine", "plan") is True

    def test_plan_to_implement(self):
        assert can_transition("plan", "implement") is True

    def test_implement_to_pr(self):
        assert can_transition("implement", "pr") is True

    def test_refine_to_implement_invalid(self):
        assert can_transition("refine", "implement") is False

    def test_pr_to_refine_invalid(self):
        assert can_transition("pr", "refine") is False

    def test_unknown_phase(self):
        assert can_transition("unknown", "plan") is False


class TestGetExitRequirement:
    """Tests for exit requirement lookup."""

    def test_refine_requires_human(self):
        assert get_exit_requirement("refine") == "human"

    def test_plan_requires_human(self):
        assert get_exit_requirement("plan") == "human"

    def test_implement_requires_reviewer(self):
        assert get_exit_requirement("implement") == "reviewer"

    def test_pr_requires_human(self):
        assert get_exit_requirement("pr") == "human"

    def test_unknown_phase(self):
        assert get_exit_requirement("unknown") is None


class TestGetNextPhase:
    """Tests for next phase lookup."""

    def test_refine_next_is_plan(self):
        assert get_next_phase("refine") == "plan"

    def test_plan_next_is_implement(self):
        assert get_next_phase("plan") == "implement"

    def test_implement_next_is_pr(self):
        assert get_next_phase("implement") == "pr"

    def test_pr_next_is_none(self):
        assert get_next_phase("pr") is None


class TestCheckImplementationComplete:
    """Tests for implementation completion check."""

    def test_all_complete(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(
                    id="phase-1",
                    name="Test",
                    tasks=[
                        Task(id="task-1", description="A", status=TaskStatus.COMPLETE),
                        Task(id="task-2", description="B", status=TaskStatus.COMPLETE),
                    ],
                )
            ],
        )
        complete, incomplete = check_implementation_complete(contract)
        assert complete is True
        assert len(incomplete) == 0

    def test_some_incomplete(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(
                    id="phase-1",
                    name="Test",
                    tasks=[
                        Task(id="task-1", description="A", status=TaskStatus.COMPLETE),
                        Task(id="task-2", description="B", status=TaskStatus.PENDING),
                    ],
                )
            ],
        )
        complete, incomplete = check_implementation_complete(contract)
        assert complete is False
        assert "task-2" in incomplete

    def test_failed_counts_as_complete(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(
                    id="phase-1",
                    name="Test",
                    tasks=[
                        Task(id="task-1", description="A", status=TaskStatus.FAILED),
                    ],
                )
            ],
        )
        complete, incomplete = check_implementation_complete(contract)
        assert complete is True


class TestValidateTransition:
    """Tests for transition validation."""

    def test_valid_transition_with_human(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.REFINE,
        )
        result = validate_transition(contract, "plan", "human")
        assert result.success is True

    def test_refine_to_plan_requires_human(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.REFINE,
        )
        result = validate_transition(contract, "plan", "implementer")
        assert result.success is False
        assert "human" in result.message.lower()

    def test_implement_to_pr_requires_reviewer(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.IMPLEMENT,
            phases=[
                Phase(
                    id="phase-1",
                    name="Test",
                    tasks=[
                        Task(id="task-1", description="A", status=TaskStatus.COMPLETE),
                    ],
                )
            ],
        )
        result = validate_transition(contract, "pr", "implementer")
        assert result.success is False
        assert "reviewer" in result.message.lower()

    def test_implement_to_pr_with_reviewer(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.IMPLEMENT,
            phases=[
                Phase(
                    id="phase-1",
                    name="Test",
                    tasks=[
                        Task(id="task-1", description="A", status=TaskStatus.COMPLETE),
                    ],
                )
            ],
        )
        result = validate_transition(contract, "pr", "reviewer")
        assert result.success is True

    def test_implement_to_pr_with_incomplete_tasks(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.IMPLEMENT,
            phases=[
                Phase(
                    id="phase-1",
                    name="Test",
                    tasks=[
                        Task(id="task-1", description="A", status=TaskStatus.PENDING),
                    ],
                )
            ],
        )
        result = validate_transition(contract, "pr", "reviewer")
        assert result.success is False
        assert "incomplete" in result.message.lower() or "complete" in result.message.lower()

    def test_invalid_target_phase(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.REFINE,
        )
        result = validate_transition(contract, "invalid", "human")
        assert result.success is False
        assert "invalid" in result.message.lower()

    def test_invalid_transition_order(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            currentPhase=PipelinePhase.REFINE,
        )
        result = validate_transition(contract, "implement", "human")
        assert result.success is False
        assert "cannot transition" in result.message.lower()


class TestExecuteTransition:
    """Tests for executing transitions."""

    def test_execute_valid_transition(self, contract_in_refine):
        contract, repo_root = contract_in_refine
        result = execute_transition(
            repo_root,
            123,
            "plan",
            "test-user",
            "human",
        )
        assert result.success is True
        assert result.from_phase == "refine"
        assert result.to_phase == "plan"

    def test_execute_blocked_transition(self, contract_in_refine):
        contract, repo_root = contract_in_refine
        result = execute_transition(
            repo_root,
            123,
            "plan",
            "test-agent",
            "implementer",
        )
        assert result.success is False

    def test_contract_not_found(self, temp_repo):
        result = execute_transition(
            temp_repo,
            999,
            "plan",
            "test",
            "human",
        )
        assert result.success is False
        assert "not found" in result.message.lower()


class TestGetCurrentPhase:
    """Tests for current phase lookup."""

    def test_get_current_phase(self, contract_in_refine):
        contract, repo_root = contract_in_refine
        phase = get_current_phase(repo_root, 123)
        assert phase == "refine"

    def test_get_current_phase_not_found(self, temp_repo):
        phase = get_current_phase(temp_repo, 999)
        assert phase is None
