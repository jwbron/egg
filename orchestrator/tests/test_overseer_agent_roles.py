"""
Tests for overseer agent role definition.

The overseer role should be registered in shared/egg_contracts/agent_roles.py
with no file access (no repo mounted) and no phase assignment.
"""

import sys
from pathlib import Path

# Ensure shared is on the path
_shared_path = Path(__file__).parent.parent.parent / "shared"
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts.agent_roles import (
    AGENT_ROLES,
    AgentRole,
    AgentRoleDefinition,
    get_role_definition,
    get_roles_for_phase,
)


class TestOverseerRoleDefinition:
    """Tests for the OVERSEER agent role definition."""

    def test_overseer_in_agent_roles_enum(self):
        """AgentRole enum in shared/egg_contracts must include OVERSEER."""
        assert hasattr(AgentRole, "OVERSEER")
        assert AgentRole.OVERSEER == "overseer"

    def test_overseer_string_conversion(self):
        """OVERSEER value round-trips through string conversion."""
        assert AgentRole("overseer") == AgentRole.OVERSEER

    def test_overseer_is_distinct(self):
        """OVERSEER does not collide with other roles."""
        other_roles = [r for r in AgentRole if r != AgentRole.OVERSEER]
        assert "overseer" not in [r.value for r in other_roles]

    def test_overseer_registered_in_agent_roles_dict(self):
        """OVERSEER must be registered in the AGENT_ROLES dict."""
        assert AgentRole.OVERSEER in AGENT_ROLES

    def test_overseer_role_definition_structure(self):
        """OVERSEER role definition must have correct structure."""
        role_def = get_role_definition(AgentRole.OVERSEER)
        assert isinstance(role_def, AgentRoleDefinition)
        assert role_def.role == AgentRole.OVERSEER
        assert role_def.role.value == "overseer"
        assert len(role_def.description) > 0
        assert len(role_def.responsibilities) > 0

    def test_overseer_has_no_file_write_access(self):
        """OVERSEER must have empty allowed_write (no repo access)."""
        role_def = get_role_definition(AgentRole.OVERSEER)
        assert role_def.file_access.allowed_write == []

    def test_overseer_has_no_dependencies(self):
        """OVERSEER role should have no dependencies (infrastructure role)."""
        role_def = get_role_definition(AgentRole.OVERSEER)
        assert role_def.dependencies == []

    def test_overseer_can_run_in_parallel(self):
        """OVERSEER should be able to run alongside other agents."""
        role_def = get_role_definition(AgentRole.OVERSEER)
        assert role_def.can_run_in_parallel is True

    def test_overseer_produces_health_outputs(self):
        """OVERSEER should produce health_summary and oversight_log."""
        role_def = get_role_definition(AgentRole.OVERSEER)
        assert "health_summary" in role_def.produces_outputs
        assert "oversight_log" in role_def.produces_outputs

    def test_overseer_requires_no_inputs(self):
        """OVERSEER should require no inputs."""
        role_def = get_role_definition(AgentRole.OVERSEER)
        assert role_def.requires_inputs == []

    def test_overseer_not_in_phase_roles(self):
        """OVERSEER must NOT appear in phase-specific role mappings.

        The overseer is a cross-phase infrastructure role, not tied to
        any specific SDLC phase.
        """
        for phase in ("implement", "plan", "refine"):
            phase_roles = get_roles_for_phase(phase)
            assert AgentRole.OVERSEER not in phase_roles, (
                f"OVERSEER should not be in {phase} phase roles"
            )
