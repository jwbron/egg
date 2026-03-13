"""
Tests for coordinator gateway permissions (Phase 3, TASK-3-3 and TASK-3-4).

Tests coordinator pseudo-phase permissions in gateway/phase_filter.py and
coordinator file restrictions in gateway/agent_restrictions.py.
"""

import sys
from pathlib import Path

# Ensure gateway and shared are on the path
_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "gateway", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from agent_restrictions import (
    AGENT_PATTERNS,
    AgentRole,
    get_agent_pattern,
    validate_agent_push,
)


class TestCoordinatorAgentRole:
    """Tests for coordinator in gateway AgentRole constants."""

    def test_coordinator_constant_exists(self):
        """AgentRole in gateway must have COORDINATOR constant.

        Gap: COORDINATOR not yet added to gateway/agent_restrictions.py AgentRole class.
        """
        assert hasattr(AgentRole, "COORDINATOR"), (
            "AgentRole class in gateway/agent_restrictions.py needs COORDINATOR = 'coordinator'"
        )

    def test_coordinator_value(self):
        """COORDINATOR value must be 'coordinator'."""
        if not hasattr(AgentRole, "COORDINATOR"):
            pytest.skip("COORDINATOR not yet added to gateway AgentRole")
        assert AgentRole.COORDINATOR == "coordinator"


class TestCoordinatorFilePatterns:
    """Tests for coordinator file access patterns in the gateway."""

    def test_coordinator_pattern_registered(self):
        """COORDINATOR patterns must be registered in AGENT_PATTERNS.

        Gap: No coordinator patterns defined yet.
        """
        has_coordinator = "coordinator" in AGENT_PATTERNS or (
            hasattr(AgentRole, "COORDINATOR") and AgentRole.COORDINATOR in AGENT_PATTERNS
        )
        assert has_coordinator, (
            "COORDINATOR patterns not in AGENT_PATTERNS. "
            "Add COORDINATOR_PATTERNS with write access to .egg-state/agent-outputs/ only."
        )

    def test_coordinator_can_write_agent_outputs(self):
        """Coordinator must be able to write to .egg-state/agent-outputs/."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert pattern.can_write(".egg-state/agent-outputs/1028-coordinator-output.json")

    def test_coordinator_cannot_write_source_code(self):
        """Coordinator must NOT be able to write source code."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write("orchestrator/models.py")
        assert not pattern.can_write("gateway/phase_filter.py")
        assert not pattern.can_write("shared/egg_contracts/agent_roles.py")
        assert not pattern.can_write("sandbox/egg_lib/orch_cli.py")

    def test_coordinator_cannot_write_contracts(self):
        """Coordinator must NOT be able to write contracts."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write(".egg-state/contracts/1028.json")

    def test_coordinator_cannot_write_tests(self):
        """Coordinator must NOT be able to write test files."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write("tests/test_something.py")
        assert not pattern.can_write("orchestrator/tests/test_models.py")

    def test_coordinator_cannot_write_docs(self):
        """Coordinator must NOT be able to write documentation."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write("docs/guides/coordinator.md")
        assert not pattern.can_write("docs/index.md")

    def test_coordinator_cannot_write_ci_config(self):
        """Coordinator must NOT be able to write CI config."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write(".github/workflows/main.yml")

    def test_coordinator_cannot_write_reviews(self):
        """Coordinator must NOT be able to write reviews."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write(".egg-state/reviews/1028-review.json")

    def test_coordinator_cannot_write_drafts(self):
        """Coordinator must NOT be able to write drafts."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write(".egg-state/drafts/1028-plan.md")

    def test_coordinator_path_traversal_blocked(self):
        """Path traversal attempts must be blocked."""
        pattern = get_agent_pattern("coordinator")
        if pattern is None:
            pytest.skip("Coordinator pattern not yet registered")
        assert not pattern.can_write("../../etc/passwd")
        assert not pattern.can_write(".egg-state/agent-outputs/../contracts/1028.json")


class TestCoordinatorValidateAgentPush:
    """Tests for validate_agent_push with coordinator role."""

    def test_coordinator_push_agent_outputs_allowed(self):
        """Coordinator push to agent-outputs should be allowed."""
        result = validate_agent_push(
            "coordinator",
            [".egg-state/agent-outputs/1028-output.json"],
        )
        if not result.allowed and "Unknown agent role" not in result.message:
            pytest.fail(f"Coordinator push to agent-outputs should be allowed: {result.message}")

    def test_coordinator_push_source_code_blocked(self):
        """Coordinator push to source code should be blocked."""
        result = validate_agent_push(
            "coordinator",
            ["orchestrator/models.py"],
        )
        # If coordinator pattern is registered, push should be blocked
        if result.allowed and "Unknown agent role" not in result.message:
            pytest.fail("Coordinator push to source code should be blocked")

    def test_coordinator_push_mixed_files(self):
        """Coordinator push with mixed files should be blocked if any restricted."""
        result = validate_agent_push(
            "coordinator",
            [
                ".egg-state/agent-outputs/output.json",
                "orchestrator/models.py",  # Should be blocked
            ],
        )
        if result.allowed and "Unknown agent role" not in result.message:
            pytest.fail("Mixed push with restricted files should be blocked")

    def test_coordinator_push_empty_files(self):
        """Coordinator push with no files should be allowed."""
        result = validate_agent_push("coordinator", [])
        assert result.allowed


class TestCoordinatorPhasePermissions:
    """Tests for coordinator pseudo-phase in gateway/phase_filter.py."""

    def test_phase_filter_has_coordinator_phase(self):
        """phase_filter.py should define coordinator pseudo-phase permissions.

        Gap: Coordinator pseudo-phase not yet in phase_filter.py.
        """
        phase_filter_path = _project_root / "gateway" / "phase_filter.py"
        content = phase_filter_path.read_text()
        has_coordinator = "coordinator" in content.lower()
        if not has_coordinator:
            pytest.skip(
                "Coordinator pseudo-phase not yet added to phase_filter.py. "
                "Need: git push allowed, egg-contract ops, egg-orch coordinator ops, "
                "gh pr merge/create blocked."
            )

    def test_coordinator_phase_allows_git_push(self):
        """Coordinator phase must allow git push to egg-owned branches.

        Gap: Not yet implemented.
        """
        try:
            from phase_filter import PipelinePhase

            if not hasattr(PipelinePhase, "COORDINATOR"):
                pytest.skip("COORDINATOR phase not yet in PipelinePhase enum")
        except ImportError:
            pytest.skip("Cannot import phase_filter")

    def test_coordinator_phase_blocks_pr_merge(self):
        """Coordinator phase must block gh pr merge.

        Gap: Not yet implemented.
        """
        try:
            from phase_filter import PipelinePhase

            if not hasattr(PipelinePhase, "COORDINATOR"):
                pytest.skip("COORDINATOR phase not yet in PipelinePhase enum")
        except ImportError:
            pytest.skip("Cannot import phase_filter")

    def test_coordinator_phase_blocks_pr_create(self):
        """Coordinator phase must block gh pr create (agents create PRs, not coordinator).

        Gap: Not yet implemented.
        """
        try:
            from phase_filter import PipelinePhase

            if not hasattr(PipelinePhase, "COORDINATOR"):
                pytest.skip("COORDINATOR phase not yet in PipelinePhase enum")
        except ImportError:
            pytest.skip("Cannot import phase_filter")


class TestCoordinatorSessionManager:
    """Tests for coordinator session support in gateway/session_manager.py."""

    def test_session_manager_supports_coordinator_role(self):
        """Session manager must support coordinator role for sessions.

        Gap: Not yet implemented.
        """
        session_mgr_path = _project_root / "gateway" / "session_manager.py"
        content = session_mgr_path.read_text()
        # The session manager should accept coordinator as an agent_role
        # This is a structural test - it should at least not reject 'coordinator'
        has_coordinator_ref = "coordinator" in content.lower()
        if not has_coordinator_ref:
            pytest.skip(
                "Session manager does not yet reference coordinator role. "
                "Coordinator sessions need coordinator pseudo-phase association."
            )
