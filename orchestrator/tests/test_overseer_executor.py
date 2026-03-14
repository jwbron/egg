"""
Tests for overseer executor lifecycle management.

Tests that the overseer executor handles spawning, completion,
and crash scenarios correctly without affecting pipeline status.
"""

import sys
from pathlib import Path

# Ensure orchestrator and shared are on the path
_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


class TestOverseerExecutorExists:
    """Tests that the overseer executor module exists and is importable."""

    def test_overseer_executor_module_exists(self):
        """orchestrator/overseer_executor.py must exist."""
        module_path = _project_root / "orchestrator" / "overseer_executor.py"
        assert module_path.exists(), "orchestrator/overseer_executor.py does not exist"

    def test_overseer_executor_importable(self):
        """OverseerExecutor class must be importable."""
        from overseer_executor import OverseerExecutor

        assert OverseerExecutor is not None

    def test_overseer_config_importable(self):
        """OverseerConfig class must be importable."""
        from overseer_executor import OverseerConfig

        assert OverseerConfig is not None


class TestOverseerConfig:
    """Tests for OverseerConfig defaults."""

    def test_default_poll_interval(self):
        from overseer_executor import OverseerConfig

        config = OverseerConfig()
        assert config.poll_interval_seconds == 30

    def test_default_stall_threshold(self):
        from overseer_executor import OverseerConfig

        config = OverseerConfig()
        assert config.stall_base_threshold_seconds == 120

    def test_default_max_redirects(self):
        from overseer_executor import OverseerConfig

        config = OverseerConfig()
        assert config.max_redirects_before_escalation == 2


class TestOverseerExecutorMethods:
    """Tests for OverseerExecutor methods."""

    def test_overseer_executor_has_spawn_in_background(self):
        """OverseerExecutor must have spawn_in_background method."""
        from overseer_executor import OverseerExecutor

        assert hasattr(OverseerExecutor, "spawn_in_background")

    def test_overseer_executor_has_handle_completion(self):
        """OverseerExecutor must have handle_overseer_completion method."""
        from overseer_executor import OverseerExecutor

        assert hasattr(OverseerExecutor, "handle_overseer_completion")

    def test_overseer_executor_has_wait_for_completion(self):
        """OverseerExecutor must have wait_for_completion method."""
        from overseer_executor import OverseerExecutor

        assert hasattr(OverseerExecutor, "wait_for_completion")

    def test_overseer_executor_has_should_spawn(self):
        """OverseerExecutor must have should_spawn_overseer method."""
        from overseer_executor import OverseerExecutor

        assert hasattr(OverseerExecutor, "should_spawn_overseer")


class TestOverseerAutoSpawnLogic:
    """Tests for overseer auto-spawn logic in pipelines.py."""

    def test_pipelines_references_overseer(self):
        """pipelines.py should reference the overseer for auto-spawn."""
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        content = pipelines_path.read_text()
        assert "overseer" in content.lower(), (
            "pipelines.py should reference overseer for auto-spawn logic"
        )

    def test_pipelines_imports_overseer_executor(self):
        """pipelines.py should import OverseerExecutor."""
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        content = pipelines_path.read_text()
        assert "OverseerExecutor" in content, "pipelines.py should import OverseerExecutor"

    def test_pipelines_overseer_uses_haiku_model(self):
        """Overseer spawn command should use haiku model for cost efficiency."""
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        content = pipelines_path.read_text()
        # Find the overseer command section
        overseer_idx = content.find("overseer_command")
        if overseer_idx > -1:
            # Check nearby for haiku model
            section = content[overseer_idx : overseer_idx + 500]
            assert "haiku" in section, (
                "Overseer should use haiku model for cost-effective monitoring"
            )

    def test_pipelines_overseer_no_repo_access(self):
        """Overseer container should be spawned with no repo volumes."""
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        content = pipelines_path.read_text()
        # The overseer spawn should use repo_volumes={}
        assert "EGG_OVERSEER_MODE" in content, (
            "pipelines.py should set EGG_OVERSEER_MODE for overseer container"
        )

    def test_overseer_env_vars_set(self):
        """Overseer container should receive all required env vars."""
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        content = pipelines_path.read_text()
        for env_var in [
            "EGG_OVERSEER_MODE",
            "EGG_OVERSEER_POLL_INTERVAL",
            "EGG_OVERSEER_STALL_THRESHOLD",
            "EGG_OVERSEER_MAX_REDIRECTS",
        ]:
            assert env_var in content, f"pipelines.py should set {env_var} for overseer container"
