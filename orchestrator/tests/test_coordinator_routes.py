"""
Tests for coordinator orchestrator API routes (Phase 2).

Tests the REST endpoints for agent spawning, phase management,
escalation, state queries, and role validation.
"""

import sys
from pathlib import Path

# Ensure orchestrator and shared are on the path
_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


class TestCoordinatorRoutesExist:
    """Tests that the coordinator routes module exists and is structured correctly."""

    def test_coordinator_routes_module_exists(self):
        """orchestrator/routes/coordinator.py must exist.

        Gap: This is a new file that needs to be created.
        """
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        assert routes_path.exists(), (
            "orchestrator/routes/coordinator.py does not exist. "
            "Create it with coordinator spawn, cancel, state, phase, and escalation endpoints."
        )

    def test_coordinator_blueprint_registered(self):
        """Coordinator blueprint must be registered in orchestrator/api.py.

        Gap: The blueprint needs to be imported and registered.
        """
        api_path = _project_root / "orchestrator" / "api.py"
        if api_path.exists():
            content = api_path.read_text()
            assert "coordinator" in content.lower(), (
                "Coordinator blueprint not registered in api.py. "
                "Import and register the coordinator blueprint."
            )


class TestCoordinatorSpawnEndpoint:
    """Tests for POST /api/v1/pipelines/{id}/coordinator/spawn endpoint."""

    def test_spawn_has_role_validation(self):
        """Spawn endpoint must validate roles."""
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        assert "coordinator" in content.lower() and (
            "role" in content.lower() or "auth" in content.lower()
        ), "Coordinator routes should include role validation"

    def test_spawn_endpoint_validates_guardrails(self):
        """Spawn should reject when max agents or max retries exceeded."""
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        assert "guardrail" in content.lower() or "max" in content.lower(), (
            "Spawn endpoint should enforce guardrails (max agents, max retries)"
        )


class TestCoordinatorStateEndpoint:
    """Tests for GET /api/v1/pipelines/{id}/coordinator/state endpoint."""

    def test_state_returns_comprehensive_data(self):
        """State endpoint must return phase, agents, decisions, and guardrails."""
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        has_state = "state" in content.lower()
        assert has_state, "State endpoint should be defined in coordinator routes"


class TestCoordinatorPhaseEndpoint:
    """Tests for POST /api/v1/pipelines/{id}/coordinator/phase endpoint."""

    def test_phase_advance_requires_reason(self):
        """Phase endpoint must require a reason string for decisions."""
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        assert "reason" in content.lower(), (
            "Phase endpoint should accept and record a reason for phase decisions"
        )


class TestCoordinatorEscalateEndpoint:
    """Tests for POST /api/v1/pipelines/{id}/coordinator/escalate endpoint."""

    def test_escalate_supports_choice_and_feedback(self):
        """Escalation endpoint must support both choice and feedback types."""
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        assert "escalat" in content.lower(), (
            "Escalation endpoint should be defined in coordinator routes"
        )


class TestCoordinatorClientMethods:
    """Tests for coordinator client methods in shared/egg_orchestrator/client.py."""

    def test_client_has_coordinator_spawn_method(self):
        """Client must have coordinator_spawn_agent method."""
        client_path = _project_root / "shared" / "egg_orchestrator" / "client.py"
        content = client_path.read_text()
        assert "coordinator_spawn" in content, "Client should have coordinator_spawn_agent method"

    def test_client_has_coordinator_state_method(self):
        """Client must have coordinator_get_state method."""
        client_path = _project_root / "shared" / "egg_orchestrator" / "client.py"
        content = client_path.read_text()
        assert "coordinator_get_state" in content or "coordinator_state" in content, (
            "Client should have coordinator_get_state method"
        )


class TestCoordinatorCLI:
    """Tests for coordinator CLI commands in sandbox/egg_lib/orch_cli.py."""

    def test_cli_has_coordinator_subcommand(self):
        """orch_cli.py must have a coordinator subcommand group."""
        cli_path = _project_root / "sandbox" / "egg_lib" / "orch_cli.py"
        content = cli_path.read_text()
        assert "coordinator" in content.lower(), (
            "orch_cli.py should have coordinator subcommand group "
            "with spawn, state, phase, escalate, cancel commands."
        )
