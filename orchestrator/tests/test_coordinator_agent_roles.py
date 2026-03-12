"""
Tests for coordinator agent role definition (Phase 1, TASK-1-2).

The coordinator role should be registered in shared/egg_contracts/agent_roles.py
with restricted file access: write only to .egg-state/agent-outputs/,
blocked from source code and contracts.
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
)


class TestCoordinatorRoleDefinition:
    """Tests for the COORDINATOR agent role definition."""

    def test_coordinator_in_agent_roles_enum(self):
        """AgentRole enum in shared/egg_contracts must include COORDINATOR.

        Gap: Currently missing — the enum has no COORDINATOR value.
        """
        # This will fail until the coder adds COORDINATOR to the shared enum
        assert hasattr(AgentRole, "COORDINATOR") or "coordinator" in [
            r.value for r in AgentRole
        ]

    def test_coordinator_registered_in_agent_roles_dict(self):
        """COORDINATOR must be registered in the AGENT_ROLES dict.

        Gap: Currently not registered.
        """
        # Check if coordinator is registered (may be under string key or enum key)
        coordinator_key = None
        for key in AGENT_ROLES:
            if key.value == "coordinator" if hasattr(key, "value") else key == "coordinator":
                coordinator_key = key
                break

        if coordinator_key is None:
            # Try the enum directly if it exists
            if hasattr(AgentRole, "COORDINATOR"):
                coordinator_key = AgentRole.COORDINATOR

        assert coordinator_key is not None, (
            "COORDINATOR role not registered in AGENT_ROLES. "
            "Add COORDINATOR_ROLE to AGENT_ROLES dict."
        )

    def test_coordinator_role_definition_structure(self):
        """COORDINATOR role definition must have correct structure."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        assert isinstance(role_def, AgentRoleDefinition)
        assert role_def.role.value == "coordinator"
        assert len(role_def.description) > 0
        assert len(role_def.responsibilities) > 0

    def test_coordinator_can_write_agent_outputs(self):
        """COORDINATOR must be able to write to .egg-state/agent-outputs/."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        assert role_def.file_access.can_write(".egg-state/agent-outputs/some-output.json")

    def test_coordinator_cannot_write_source_code(self):
        """COORDINATOR must NOT be able to write source code files."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        # Should be blocked from writing any source files
        assert not role_def.file_access.can_write("orchestrator/models.py")
        assert not role_def.file_access.can_write("gateway/phase_filter.py")
        assert not role_def.file_access.can_write("shared/egg_contracts/agent_roles.py")

    def test_coordinator_cannot_write_contracts(self):
        """COORDINATOR must NOT be able to write to .egg-state/contracts/."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        assert not role_def.file_access.can_write(".egg-state/contracts/1028.json")

    def test_coordinator_cannot_write_tests(self):
        """COORDINATOR must NOT be able to write test files."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        assert not role_def.file_access.can_write("tests/test_something.py")
        assert not role_def.file_access.can_write("orchestrator/tests/test_models.py")

    def test_coordinator_cannot_write_docs(self):
        """COORDINATOR must NOT be able to write documentation files."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        assert not role_def.file_access.can_write("docs/guides/coordinator.md")

    def test_coordinator_can_read_all_files(self):
        """COORDINATOR must be able to read all files (empty allowed_read = all)."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        # Empty allowed_read means all files readable
        assert role_def.file_access.can_read("orchestrator/models.py")
        assert role_def.file_access.can_read("gateway/phase_filter.py")
        assert role_def.file_access.can_read(".egg-state/contracts/1028.json")

    def test_coordinator_has_no_dependencies(self):
        """COORDINATOR role should have no dependencies (runs first)."""
        if not hasattr(AgentRole, "COORDINATOR"):
            import pytest

            pytest.skip("COORDINATOR not yet added to shared AgentRole enum")

        role_def = get_role_definition(AgentRole.COORDINATOR)
        assert role_def.dependencies == []
