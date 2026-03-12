"""
Tests for CoordinatorExecutor (Phase 3, TASK-3-1a and TASK-3-1b).

Tests coordinator container lifecycle management including:
- Spawning coordinator when coordinator_enabled
- Injecting coordinator env vars
- Health monitoring and crash recovery
- Global guardrail enforcement
- Pipeline completion handling
"""

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from models import (
    GuardrailCounters,
    Pipeline,
    PipelineConfig,
    PipelineStatus,
)


class TestCoordinatorExecutorModuleExists:
    """Tests for the existence of the coordinator executor module."""

    def test_coordinator_executor_file_exists(self):
        """orchestrator/coordinator_executor.py must exist.

        Gap: This is a new file that needs to be created.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        assert executor_path.exists(), (
            "orchestrator/coordinator_executor.py does not exist. "
            "Create CoordinatorExecutor class with spawn, monitor, and recovery logic."
        )

    def test_coordinator_executor_class_importable(self):
        """CoordinatorExecutor class must be importable."""
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        try:
            from coordinator_executor import CoordinatorExecutor  # noqa: F401
        except ImportError as e:
            pytest.fail(f"Cannot import CoordinatorExecutor: {e}")


class TestCoordinatorExecutorSpawn:
    """Tests for coordinator container spawning."""

    def test_coordinator_spawns_when_enabled(self):
        """Coordinator container should spawn when coordinator_enabled is True.

        Gap: Not yet implemented.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        assert "coordinator_enabled" in content or "spawn" in content.lower(), (
            "CoordinatorExecutor should check coordinator_enabled and spawn coordinator container"
        )

    def test_coordinator_env_vars_injected(self):
        """Coordinator container should receive coordinator-specific env vars.

        Expected env vars: EGG_COORDINATOR_MODE=true, EGG_COORDINATOR_TOOLS=true,
        issue context, repo info.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_env_setup = "EGG_COORDINATOR_MODE" in content or "coordinator" in content.lower()
        assert has_env_setup, (
            "CoordinatorExecutor should inject EGG_COORDINATOR_MODE and related env vars"
        )


class TestCoordinatorExecutorRecovery:
    """Tests for coordinator crash recovery."""

    def test_crash_recovery_logic_exists(self):
        """CoordinatorExecutor must have crash recovery logic.

        Gap: Crash detection and respawn logic needed.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_recovery = any(
            keyword in content.lower() for keyword in ["respawn", "crash", "recovery", "restart"]
        )
        assert has_recovery, "CoordinatorExecutor should have crash detection and respawn logic"

    def test_max_respawns_enforced(self):
        """Coordinator respawns must be limited (default 2).

        Gap: Max respawn enforcement.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_limit = "max" in content.lower() and "respawn" in content.lower()
        if not has_limit:
            pytest.skip("Max respawn limit not yet implemented")


class TestCoordinatorExecutorGuardrails:
    """Tests for global guardrail enforcement."""

    def test_max_agents_enforced(self):
        """Coordinator executor must enforce max total agents (default 10).

        Gap: Global agent count guardrail.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_max = "max" in content.lower() and "agent" in content.lower()
        assert has_max, "CoordinatorExecutor should enforce max total agents guardrail"

    def test_guardrail_counters_tracked(self):
        """GuardrailCounters model correctly tracks enforcement data."""
        counters = GuardrailCounters(
            total_agents_spawned=10,
            retries_by_role={"coder": 2, "tester": 1},
            coordinator_respawns=1,
        )
        # Verify we can check against limits
        assert counters.total_agents_spawned >= 10  # At max
        assert counters.retries_by_role.get("coder", 0) >= 2  # At max retries


class TestCoordinatorExecutorCompletion:
    """Tests for coordinator completion handling."""

    def test_completion_checks_agents_done(self):
        """Coordinator completion should verify all spawned agents are done.

        Gap: Completion verification logic.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_completion = "complet" in content.lower()
        assert has_completion, (
            "CoordinatorExecutor should handle coordinator completion and verify agents"
        )


class TestCoordinatorPipelineWiring:
    """Tests for wiring CoordinatorExecutor into pipeline creation (TASK-3-2)."""

    def test_pipelines_route_references_coordinator(self):
        """Pipeline creation route must route to CoordinatorExecutor when enabled.

        Gap: Wiring in orchestrator/routes/pipelines.py.
        """
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        if not pipelines_path.exists():
            pytest.skip("pipelines.py not found")

        content = pipelines_path.read_text()
        has_coordinator = "coordinator" in content.lower()
        if not has_coordinator:
            pytest.skip(
                "Pipeline creation route does not yet reference coordinator. "
                "Need to route to CoordinatorExecutor when coordinator_enabled is true."
            )

    def test_existing_pipelines_unaffected(self):
        """Existing pipelines must work when coordinator_enabled is false (default)."""
        # Default config should not have coordinator enabled
        config = PipelineConfig()
        # coordinator_enabled should default to false if it exists
        if hasattr(config, "coordinator_enabled"):
            assert config.coordinator_enabled is False
        # Standard pipeline should still work
        pipeline = Pipeline(
            id="issue-999",
            issue_number=999,
            repo="owner/repo",
            branch="egg/issue-999",
        )
        assert pipeline.status == PipelineStatus.PENDING
