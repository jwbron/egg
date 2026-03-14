"""
Tests for overseer exclusion from coordinator guardrails.

The overseer is infrastructure and must NOT count against the
coordinator_max_agents guardrail.
"""

import sys
from pathlib import Path

# Ensure orchestrator and shared are on the path
_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


class TestOverseerGuardrailExclusion:
    """Tests that the overseer is excluded from coordinator guardrail counts."""

    def test_guardrail_check_function_exists(self):
        """_check_spawn_guardrails function should exist in coordinator routes."""
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        assert "_check_spawn_guardrails" in content

    def test_guardrail_excludes_overseer_from_count(self):
        """The guardrail check must filter out overseer from agent counts.

        The code should reference 'overseer' in the context of excluding
        infrastructure roles from the max_agents count.
        """
        routes_path = _project_root / "orchestrator" / "routes" / "coordinator.py"
        content = routes_path.read_text()
        # Verify the guardrail code references overseer exclusion
        assert "overseer" in content, (
            "coordinator.py should reference 'overseer' for guardrail exclusion"
        )
        assert "infra_roles" in content or "infrastructure" in content.lower(), (
            "Guardrail check should have infrastructure role exclusion logic"
        )

    def test_guardrail_with_overseer_in_agents_spawned(self):
        """Overseer in agents_spawned should not count toward max_agents."""
        from models import (
            AgentRole,
            AgentSpawnRecord,
            CoordinatorState,
            GuardrailCounters,
            Pipeline,
            PipelineConfig,
        )

        # Create a pipeline at the max_agents limit with overseer
        config = PipelineConfig(
            coordinator_enabled=True,
            coordinator_max_agents=2,
        )
        state = CoordinatorState(
            agents_spawned=[
                AgentSpawnRecord(role="coder", status="complete"),
                AgentSpawnRecord(role="overseer", status="running"),
            ],
            guardrail_counters=GuardrailCounters(total_agents_spawned=2),
        )
        pipeline = Pipeline(
            id="test-guardrail",
            config=config,
            coordinator_state=state,
        )

        # Import and test the actual guardrail function
        from routes.coordinator import _check_spawn_guardrails

        # With overseer excluded, only 1 task agent (coder) — should allow spawning
        allowed, reason = _check_spawn_guardrails(pipeline, "tester")
        assert allowed, (
            f"Should allow spawn when overseer is excluded from count. Reason: {reason}"
        )

    def test_guardrail_without_overseer_still_limits(self):
        """Guardrail should still enforce limits for non-infrastructure agents."""
        from models import (
            AgentSpawnRecord,
            CoordinatorState,
            GuardrailCounters,
            Pipeline,
            PipelineConfig,
        )

        config = PipelineConfig(
            coordinator_enabled=True,
            coordinator_max_agents=2,
        )
        state = CoordinatorState(
            agents_spawned=[
                AgentSpawnRecord(role="coder", status="complete"),
                AgentSpawnRecord(role="tester", status="running"),
            ],
            guardrail_counters=GuardrailCounters(total_agents_spawned=2),
        )
        pipeline = Pipeline(
            id="test-guardrail",
            config=config,
            coordinator_state=state,
        )

        from routes.coordinator import _check_spawn_guardrails

        # With 2 task agents at max_agents=2, should reject
        allowed, reason = _check_spawn_guardrails(pipeline, "documenter")
        assert not allowed, "Should reject spawn when task agent limit is reached"
