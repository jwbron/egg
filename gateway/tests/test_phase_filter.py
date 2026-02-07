"""
Tests for Phase Filter module.

Tests cover:
- Operation matching
- Phase permission loading
- Blocked/allowed operation filtering
- Exit requirements
"""

import json
import tempfile
from pathlib import Path

import pytest

import phase_filter
from phase_filter import (
    FilterResult,
    Operation,
    OperationType,
    PhaseFilter,
    PhasePermissions,
    PipelinePhase,
    filter_operation,
    get_phase_filter,
    is_operation_blocked,
)


class TestOperation:
    """Tests for Operation class."""

    def test_matches_exact(self):
        """Exact pattern matches exact command."""
        op = Operation(OperationType.GIT, "push origin main")
        assert op.matches("push origin main") is True
        assert op.matches("push origin develop") is False

    def test_matches_wildcard(self):
        """Wildcard pattern matches multiple commands."""
        op = Operation(OperationType.GIT, "push *")
        assert op.matches("push origin main") is True
        assert op.matches("push upstream develop") is True
        assert op.matches("pull origin main") is False

    def test_matches_multiple_wildcards(self):
        """Multiple wildcards work correctly."""
        op = Operation(OperationType.GH, "issue * *")
        assert op.matches("issue comment 123") is True
        assert op.matches("issue edit 456") is True
        assert op.matches("pr comment 123") is False


class TestPhasePermissions:
    """Tests for PhasePermissions class."""

    def test_from_dict_basic(self):
        """Create PhasePermissions from dictionary."""
        data = {
            "allowed_operations": [
                {"type": "git", "pattern": "push *", "description": "Push code"}
            ],
            "blocked_operations": [
                {"type": "gh", "pattern": "pr create *", "description": "No PRs"}
            ],
            "exit_requires": "reviewer",
        }
        permissions = PhasePermissions.from_dict(data)

        assert len(permissions.allowed_operations) == 1
        assert len(permissions.blocked_operations) == 1
        assert permissions.exit_requires == "reviewer"
        assert permissions.allowed_operations[0].type == OperationType.GIT
        assert permissions.blocked_operations[0].pattern == "pr create *"

    def test_from_dict_empty_lists(self):
        """Handle empty operation lists."""
        data = {
            "allowed_operations": [],
            "blocked_operations": [],
            "exit_requires": "human",
        }
        permissions = PhasePermissions.from_dict(data)

        assert len(permissions.allowed_operations) == 0
        assert len(permissions.blocked_operations) == 0


class TestPhaseFilter:
    """Tests for PhaseFilter class."""

    @pytest.fixture
    def custom_permissions_file(self) -> Path:
        """Create a temporary permissions file."""
        permissions = {
            "schemaVersion": "1.0",
            "phases": {
                "refine": {
                    "allowed_operations": [
                        {"type": "gh", "pattern": "issue comment *", "description": "Comment"},
                    ],
                    "blocked_operations": [
                        {"type": "git", "pattern": "push *", "description": "No push"},
                    ],
                    "exit_requires": "human",
                },
                "implement": {
                    "allowed_operations": [
                        {"type": "git", "pattern": "push *", "description": "Push code"},
                    ],
                    "blocked_operations": [
                        {"type": "gh", "pattern": "pr create *", "description": "No PR yet"},
                    ],
                    "exit_requires": "reviewer",
                },
            },
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(permissions, f)
            return Path(f.name)

    def test_load_from_file(self, custom_permissions_file: Path):
        """Load permissions from a file."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        permissions = pf.get_permissions(PipelinePhase.REFINE)

        assert permissions is not None
        assert permissions.exit_requires == "human"
        assert len(permissions.blocked_operations) == 1

    def test_default_permissions_when_no_file(self):
        """Use default permissions when file doesn't exist."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent/path.json"))
        permissions = pf.get_permissions(PipelinePhase.IMPLEMENT)

        assert permissions is not None
        assert permissions.exit_requires == "reviewer"

    def test_filter_blocked_operation(self, custom_permissions_file: Path):
        """Blocked operations are correctly identified."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        result = pf.filter_operation(
            PipelinePhase.REFINE,
            OperationType.GIT,
            "push origin main",
        )

        assert result.allowed is False
        assert result.phase == PipelinePhase.REFINE
        assert result.operation_type == OperationType.GIT
        assert "push" in result.message.lower()

    def test_filter_allowed_operation(self, custom_permissions_file: Path):
        """Allowed operations are correctly identified."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        result = pf.filter_operation(
            PipelinePhase.REFINE,
            OperationType.GH,
            "issue comment 123",
        )

        assert result.allowed is True

    def test_filter_not_explicitly_blocked(self, custom_permissions_file: Path):
        """Operations not explicitly blocked are allowed."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        result = pf.filter_operation(
            PipelinePhase.REFINE,
            OperationType.EGG_CONTRACT,
            "show",
        )

        # Not in blocked list, so allowed
        assert result.allowed is True

    def test_is_operation_blocked_helper(self, custom_permissions_file: Path):
        """is_operation_blocked helper works correctly."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)

        assert pf.is_operation_blocked(
            PipelinePhase.REFINE, OperationType.GIT, "push origin main"
        ) is True
        assert pf.is_operation_blocked(
            PipelinePhase.IMPLEMENT, OperationType.GIT, "push origin main"
        ) is False

    def test_get_exit_requirement(self, custom_permissions_file: Path):
        """Get exit requirement for a phase."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)

        assert pf.get_exit_requirement(PipelinePhase.REFINE) == "human"
        assert pf.get_exit_requirement(PipelinePhase.IMPLEMENT) == "reviewer"


class TestFilterOperationFunction:
    """Tests for the convenience filter_operation function."""

    def test_filter_with_strings(self):
        """filter_operation accepts strings."""
        # Reset global filter to use defaults
        phase_filter._filter = None

        result = filter_operation("implement", "gh", "pr create")

        assert result.allowed is False
        assert "pr create" in result.message.lower() or "pr" in str(result.blocked_reason).lower()

    def test_filter_with_enums(self):
        """filter_operation accepts enums."""
        phase_filter._filter = None

        result = filter_operation(
            PipelinePhase.IMPLEMENT,
            OperationType.GIT,
            "push origin main",
        )

        assert result.allowed is True


class TestIsOperationBlockedFunction:
    """Tests for the convenience is_operation_blocked function."""

    def test_blocked_during_refine(self):
        """Git push is blocked during refine phase."""
        phase_filter._filter = None

        assert is_operation_blocked("refine", "git", "push origin main") is True

    def test_allowed_during_implement(self):
        """Git push is allowed during implement phase."""
        phase_filter._filter = None

        assert is_operation_blocked("implement", "git", "push origin main") is False

    def test_pr_create_blocked_until_pr_phase(self):
        """PR create is blocked until PR phase."""
        phase_filter._filter = None

        assert is_operation_blocked("refine", "gh", "pr create") is True
        assert is_operation_blocked("plan", "gh", "pr create") is True
        assert is_operation_blocked("implement", "gh", "pr create") is True
        assert is_operation_blocked("pr", "gh", "pr create") is False


class TestDefaultPermissions:
    """Tests for default permission configuration."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_refine_phase_blocks_push(self):
        """Refine phase blocks git push."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.REFINE, OperationType.GIT, "push origin main"
        )
        assert result.allowed is False

    def test_refine_phase_blocks_pr_create(self):
        """Refine phase blocks PR creation."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.REFINE, OperationType.GH, "pr create"
        )
        assert result.allowed is False

    def test_plan_phase_blocks_push(self):
        """Plan phase blocks git push."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.PLAN, OperationType.GIT, "push origin main"
        )
        assert result.allowed is False

    def test_implement_phase_allows_push(self):
        """Implement phase allows git push."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.IMPLEMENT, OperationType.GIT, "push origin main"
        )
        assert result.allowed is True

    def test_implement_phase_blocks_pr_create(self):
        """Implement phase blocks PR creation."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.IMPLEMENT, OperationType.GH, "pr create"
        )
        assert result.allowed is False

    def test_pr_phase_allows_pr_create(self):
        """PR phase allows PR creation."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.PR, OperationType.GH, "pr create"
        )
        assert result.allowed is True

    def test_pr_phase_allows_push(self):
        """PR phase allows git push."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.PR, OperationType.GIT, "push origin main"
        )
        assert result.allowed is True
