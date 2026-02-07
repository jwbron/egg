"""Tests for phase-based operation filtering."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Add gateway and shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "gateway"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from phase_filter import (
    PhaseFilterResult,
    PhasePermissions,
)


@pytest.fixture
def temp_permissions():
    """Create temporary phase permissions file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        permissions = {
            "schemaVersion": "1.0",
            "phases": {
                "refine": {
                    "allowed": [
                        {"type": "gh", "pattern": "issue *"},
                        {"type": "git", "pattern": "fetch *"},
                    ],
                    "blocked": [
                        {"type": "git", "pattern": "push *", "description": "No push in refine"},
                        {"type": "gh", "pattern": "pr create *"},
                    ],
                    "exit_requires": "human",
                },
                "implement": {
                    "allowed": [
                        {"type": "git", "pattern": "push *"},
                        {"type": "git", "pattern": "commit *"},
                        {"type": "egg-contract", "pattern": "add-commit *"},
                    ],
                    "blocked": [
                        {"type": "gh", "pattern": "pr create *"},
                    ],
                    "exit_requires": "reviewer",
                },
            },
        }
        path = Path(tmpdir) / "permissions.json"
        with open(path, "w") as f:
            json.dump(permissions, f)
        yield path


class TestPhaseFilterResult:
    """Tests for PhaseFilterResult dataclass."""

    def test_allowed_result(self):
        result = PhaseFilterResult(
            allowed=True,
            reason="Operation allowed",
            phase="implement",
            operation="push origin main",
        )
        assert result.allowed is True
        assert result.phase == "implement"

    def test_blocked_result(self):
        result = PhaseFilterResult(
            allowed=False,
            reason="Operation blocked",
            phase="refine",
            operation="push origin main",
            hint="Wait for plan approval",
        )
        assert result.allowed is False
        assert result.hint is not None

    def test_to_dict(self):
        result = PhaseFilterResult(
            allowed=False,
            reason="Blocked",
            phase="refine",
            operation="push",
            hint="Hint",
        )
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["reason"] == "Blocked"
        assert d["phase"] == "refine"
        assert d["hint"] == "Hint"


class TestPhasePermissions:
    """Tests for PhasePermissions class."""

    def test_load_permissions(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        config = permissions.get_phase_config("refine")
        assert config is not None
        assert "allowed" in config
        assert "blocked" in config

    def test_get_unknown_phase(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        config = permissions.get_phase_config("unknown")
        assert config is None

    def test_get_exit_requirement(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        assert permissions.get_exit_requirement("refine") == "human"
        assert permissions.get_exit_requirement("implement") == "reviewer"


class TestCheckOperation:
    """Tests for operation checking."""

    def test_allowed_operation(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        result = permissions.check_operation("refine", "gh", "issue view 123")
        assert result.allowed is True

    def test_blocked_operation(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        result = permissions.check_operation("refine", "git", "push origin main")
        assert result.allowed is False
        assert "push" in result.reason.lower() or "blocked" in result.reason.lower()

    def test_wildcard_pattern_match(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        # "fetch *" should match "fetch origin"
        result = permissions.check_operation("refine", "git", "fetch origin")
        assert result.allowed is True

    def test_unlisted_operation_blocked(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        # Operation not in allowed list should be blocked
        result = permissions.check_operation("refine", "git", "checkout branch")
        assert result.allowed is False

    def test_unconfigured_phase_allowed(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        # Phase not in config should allow operations
        result = permissions.check_operation("unknown", "git", "anything")
        assert result.allowed is True


class TestPatternMatching:
    """Tests for pattern matching logic."""

    def test_exact_match(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        # Should match exact pattern
        assert permissions._matches_pattern("fetch origin", "fetch *") is True

    def test_wildcard_suffix(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        assert permissions._matches_pattern("push origin main", "push *") is True
        assert permissions._matches_pattern("push", "push *") is True

    def test_no_match(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        assert permissions._matches_pattern("commit -m test", "push *") is False

    def test_exact_command_match(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        assert permissions._matches_pattern("status", "status") is True
        assert permissions._matches_pattern("status -s", "status") is True


class TestImplementPhase:
    """Tests for implement phase permissions."""

    def test_push_allowed_in_implement(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        result = permissions.check_operation("implement", "git", "push origin main")
        assert result.allowed is True

    def test_commit_allowed_in_implement(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        result = permissions.check_operation("implement", "git", "commit -m 'test'")
        assert result.allowed is True

    def test_pr_create_blocked_in_implement(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        result = permissions.check_operation("implement", "gh", "pr create --title test")
        assert result.allowed is False

    def test_contract_command_allowed(self, temp_permissions):
        permissions = PhasePermissions(temp_permissions)
        result = permissions.check_operation(
            "implement", "egg-contract", "add-commit --task task-1 --commit abc123"
        )
        assert result.allowed is True


class TestDefaultPermissions:
    """Tests for default behavior when permissions file not found."""

    def test_missing_file_uses_defaults(self):
        permissions = PhasePermissions(Path("/nonexistent/path.json"))
        # Should load without error
        config = permissions.get_phase_config("refine")
        # With empty defaults, should return None
        assert config is None

    def test_unconfigured_allows_all(self):
        permissions = PhasePermissions(Path("/nonexistent/path.json"))
        result = permissions.check_operation("refine", "git", "push origin")
        # Unconfigured phase should allow
        assert result.allowed is True
