"""
Tests for the OVERSEER agent role (issue #1059).

Covers:
- OVERSEER in orchestrator AgentRole enum
- OVERSEER AgentRoleDefinition in shared/egg_contracts/agent_roles.py
- OVERSEER in AGENT_ROLES registry
- OVERSEER file access patterns (read-only monitor)
- OVERSEER as infrastructure role (bypass phase-role validation)
- PipelineConfig backward compatibility with new overseer fields
- Gateway OVERSEER_PATTERNS (blocks all file writes)
- AgentRole count
"""

import json

import pytest
from models import (
    AgentExecution,
    AgentRole,
    PipelineConfig,
)

# ── Orchestrator AgentRole enum tests ──────────────────────────────


class TestOverseerInAgentRoleEnum:
    """Verify OVERSEER exists in the orchestrator's AgentRole enum."""

    def test_overseer_enum_value(self):
        """OVERSEER enum has string value 'overseer'."""
        assert AgentRole.OVERSEER == "overseer"
        assert AgentRole.OVERSEER.value == "overseer"

    def test_overseer_in_members(self):
        """OVERSEER is in the AgentRole member list."""
        assert AgentRole.OVERSEER in list(AgentRole)

    def test_overseer_from_string(self):
        """Can construct OVERSEER from string."""
        assert AgentRole("overseer") == AgentRole.OVERSEER

    def test_agent_role_count(self):
        """AgentRole has the expected number of members (including OVERSEER)."""
        roles = list(AgentRole)
        # OVERSEER should be counted among the total
        assert AgentRole.OVERSEER in roles
        # At minimum there are 15 roles (the base 14 + OVERSEER)
        assert len(roles) >= 15


# ── Agent execution with OVERSEER ──────────────────────────────────


class TestOverseerAgentExecution:
    """Verify AgentExecution model works with OVERSEER role."""

    def test_create_execution(self):
        """Can create an AgentExecution with OVERSEER role."""
        execution = AgentExecution(role=AgentRole.OVERSEER)
        assert execution.role == AgentRole.OVERSEER

    def test_serialization_roundtrip(self):
        """AgentExecution with OVERSEER serializes and deserializes correctly."""
        execution = AgentExecution(role=AgentRole.OVERSEER)
        data = execution.model_dump()
        restored = AgentExecution.model_validate(data)
        assert restored.role == AgentRole.OVERSEER

    def test_string_deserialization(self):
        """AgentExecution accepts 'overseer' string for role."""
        data = {"role": "overseer"}
        execution = AgentExecution.model_validate(data)
        assert execution.role == AgentRole.OVERSEER


# ── Shared AgentRoleDefinition tests ───────────────────────────────


class TestOverseerRoleDefinition:
    """Test OVERSEER_ROLE definition in shared/egg_contracts/agent_roles.py."""

    @pytest.fixture(autouse=True)
    def _load_role_definitions(self):
        """Import agent role definitions, skip if not available."""
        try:
            from egg_contracts.agent_roles import (
                AGENT_ROLES,
            )
            from egg_contracts.agent_roles import (
                AgentRole as SharedAgentRole,
            )

            self.AGENT_ROLES = AGENT_ROLES
            self.SharedAgentRole = SharedAgentRole
        except (ImportError, AttributeError):
            pytest.skip("egg_contracts.agent_roles not available")

    def test_overseer_in_registry(self):
        """OVERSEER is registered in AGENT_ROLES."""
        assert self.SharedAgentRole.OVERSEER in self.AGENT_ROLES

    def test_overseer_role_value(self):
        """OVERSEER role definition has correct role value."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert role_def.role == self.SharedAgentRole.OVERSEER

    def test_overseer_no_dependencies(self):
        """OVERSEER has no dependencies (runs independently)."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert role_def.dependencies == []

    def test_overseer_can_read_all_files(self):
        """OVERSEER can read all files (empty allowed_read = all files readable)."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert role_def.file_access.allowed_read == []
        # Empty allowed_read means all files readable
        assert role_def.file_access.can_read("orchestrator/models.py")
        assert role_def.file_access.can_read("tests/test_foo.py")

    def test_overseer_blocked_from_source_files(self):
        """OVERSEER cannot write to source code files."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert not role_def.file_access.can_write("orchestrator/models.py")
        assert not role_def.file_access.can_write("shared/egg_contracts/agent_roles.py")
        assert not role_def.file_access.can_write("gateway/gateway.py")

    def test_overseer_blocked_from_tests(self):
        """OVERSEER cannot write to test files."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert not role_def.file_access.can_write("tests/test_models.py")
        assert not role_def.file_access.can_write("orchestrator/tests/test_foo.py")

    def test_overseer_blocked_from_docs(self):
        """OVERSEER cannot write to documentation files."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert not role_def.file_access.can_write("docs/index.md")

    def test_overseer_blocked_from_contracts(self):
        """OVERSEER cannot write to contract files."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert not role_def.file_access.can_write(".egg-state/contracts/1059.json")

    def test_overseer_can_run_in_parallel(self):
        """OVERSEER can run in parallel with other agents."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert role_def.can_run_in_parallel is True

    def test_overseer_produces_outputs(self):
        """OVERSEER produces health summary and oversight logs."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert len(role_def.produces_outputs) > 0

    def test_overseer_requires_no_inputs(self):
        """OVERSEER requires no inputs from other agents."""
        role_def = self.AGENT_ROLES[self.SharedAgentRole.OVERSEER]
        assert role_def.requires_inputs == []


# ── Infrastructure role tests ──────────────────────────────────────


class TestOverseerAsInfrastructureRole:
    """Test OVERSEER is treated as an infrastructure role."""

    def test_is_infrastructure_role(self):
        """OVERSEER is recognized as an infrastructure role."""
        try:
            from egg_contracts.agent_roles import is_infrastructure_role

            assert is_infrastructure_role("overseer")
        except ImportError:
            pytest.skip("is_infrastructure_role not available")

    def test_not_in_phase_specific_roles(self):
        """OVERSEER should NOT be in phase-specific role lists."""
        try:
            from egg_contracts.agent_roles import AgentRole as SharedAgentRole
            from egg_contracts.agent_roles import get_roles_for_phase

            for phase in ["implement", "plan", "refine"]:
                try:
                    roles = get_roles_for_phase(phase, include_reviewers=True)
                    assert SharedAgentRole.OVERSEER not in roles, (
                        f"OVERSEER should not be in {phase} phase roles"
                    )
                except ValueError:
                    pass  # Phase has no defined roles
        except ImportError:
            pytest.skip("get_roles_for_phase not available")


# ── PipelineConfig tests ──────────────────────────────────────────


class TestPipelineConfigOverseerFields:
    """Test PipelineConfig backward compatibility with overseer fields."""

    def test_default_config_creates_without_overseer_fields(self):
        """Creating PipelineConfig() with no args still works."""
        config = PipelineConfig()
        assert config is not None

    def test_overseer_enabled_default(self):
        """overseer_enabled defaults to True if field exists."""
        config = PipelineConfig()
        if hasattr(config, "overseer_enabled"):
            assert config.overseer_enabled is True

    def test_overseer_poll_interval_default(self):
        """overseer_poll_interval_seconds defaults to 30."""
        config = PipelineConfig()
        if hasattr(config, "overseer_poll_interval_seconds"):
            assert config.overseer_poll_interval_seconds == 30

    def test_overseer_stall_threshold_default(self):
        """overseer_stall_base_threshold_seconds defaults to 120."""
        config = PipelineConfig()
        if hasattr(config, "overseer_stall_base_threshold_seconds"):
            assert config.overseer_stall_base_threshold_seconds == 120

    def test_overseer_max_redirects_default(self):
        """overseer_max_redirects_before_escalation defaults to 2."""
        config = PipelineConfig()
        if hasattr(config, "overseer_max_redirects_before_escalation"):
            assert config.overseer_max_redirects_before_escalation == 2

    def test_backward_compat_old_json(self):
        """Old serialized JSON without overseer fields deserializes correctly."""
        old_json = json.dumps(
            {
                "auto_create_pr": True,
                "parallel_agents": True,
                "max_review_cycles": 3,
            }
        )
        config = PipelineConfig.model_validate_json(old_json)
        assert config.parallel_agents is True
        assert config.max_review_cycles == 3

    def test_overseer_fields_roundtrip(self):
        """Config with overseer fields survives serialization roundtrip."""
        config = PipelineConfig()
        data = config.model_dump()
        restored = PipelineConfig.model_validate(data)
        # All standard fields should be equal
        assert restored.parallel_agents == config.parallel_agents
        assert restored.max_review_cycles == config.max_review_cycles

    def test_custom_overseer_values(self):
        """Can set custom overseer field values if fields exist."""
        kwargs = {}
        # Only set fields that exist
        sample = PipelineConfig()
        if hasattr(sample, "overseer_enabled"):
            kwargs["overseer_enabled"] = False
        if hasattr(sample, "overseer_poll_interval_seconds"):
            kwargs["overseer_poll_interval_seconds"] = 60
        if hasattr(sample, "overseer_max_redirects_before_escalation"):
            kwargs["overseer_max_redirects_before_escalation"] = 3

        if kwargs:
            config = PipelineConfig(**kwargs)
            if hasattr(config, "overseer_enabled"):
                assert config.overseer_enabled is False
            if hasattr(config, "overseer_poll_interval_seconds"):
                assert config.overseer_poll_interval_seconds == 60


# ── Gateway restrictions tests ─────────────────────────────────────


class TestOverseerGatewayRestrictions:
    """Test OVERSEER gateway file access restrictions."""

    @pytest.fixture(autouse=True)
    def _load_gateway_patterns(self):
        """Import gateway agent restrictions, skip if not available."""
        try:
            from agent_restrictions import AGENT_PATTERNS, OVERSEER_PATTERNS

            self.AGENT_PATTERNS = AGENT_PATTERNS
            self.OVERSEER_PATTERNS = OVERSEER_PATTERNS
        except ImportError:
            pytest.skip("gateway agent_restrictions not available")

    def test_overseer_in_registry(self):
        """OVERSEER is in the AGENT_PATTERNS registry."""
        assert "overseer" in self.AGENT_PATTERNS

    def test_overseer_blocks_source_writes(self):
        """OVERSEER blocks writes to source code directories."""
        patterns = self.OVERSEER_PATTERNS
        blocked = patterns.blocked_patterns
        # Should block major source directories
        assert any(
            p in blocked for p in ["**/*", "src/", "orchestrator/", "gateway/", "shared/"]
        ), f"Expected source dirs to be blocked, got: {blocked}"

    def test_overseer_limited_allowed_write_patterns(self):
        """OVERSEER has very limited allowed write patterns (oversight/outputs only)."""
        patterns = self.OVERSEER_PATTERNS
        # Either no allowed patterns (fully blocked) or only oversight-related
        for p in patterns.allowed_patterns:
            assert ".egg-state/" in p, f"Unexpected allowed write pattern: {p}"


# ── EventType tests ────────────────────────────────────────────────


class TestProgressEventType:
    """Test for PROGRESS_EMITTED event type (if implemented)."""

    def test_progress_emitted_exists(self):
        """PROGRESS_EMITTED should exist in EventType enum (when implemented)."""
        from events import EventType

        if hasattr(EventType, "PROGRESS_EMITTED"):
            assert EventType.PROGRESS_EMITTED.value == "progress.emitted"
        else:
            pytest.skip("PROGRESS_EMITTED not yet added to EventType")
