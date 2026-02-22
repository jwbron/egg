"""
Tests for integration_test_enabled flag propagation in MultiAgentExecutor.

Verifies that when integration_test_enabled=True, tester agents receive
EGG_INTEGRATION_TEST_ENABLED=true in their environment, while non-tester
agents do not.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import (
    AgentRole,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from multi_agent import MultiAgentExecutor


def _make_pipeline() -> Pipeline:
    """Create a minimal RUNNING pipeline."""
    return Pipeline(
        id="issue-99",
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )


class TestIntegrationTestEnabled:
    """Tests for integration_test_enabled flag on MultiAgentExecutor."""

    def test_flag_stored_on_executor(self):
        """integration_test_enabled is stored as an instance attribute."""
        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=lambda r, p, e: (0, "ok"),
            integration_test_enabled=True,
        )

        assert executor.integration_test_enabled is True

    def test_flag_defaults_to_false(self):
        """integration_test_enabled defaults to False."""
        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=lambda r, p, e: (0, "ok"),
        )

        assert executor.integration_test_enabled is False


class TestIntegrationTestEnvPropagation:
    """Tests for EGG_INTEGRATION_TEST_ENABLED env var propagation via spawn_fn."""

    def _make_dispatcher(self, agents_sequence):
        """Create a mock dispatcher with serializable handoff data."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()
        mock_dispatcher.get_agents_to_run.side_effect = agents_sequence
        mock_dispatcher.get_next_dispatch.return_value = MagicMock(wave_number=1)
        # Return a plain dict so json.dumps works in _run_wave_docker
        mock_dispatcher.get_handoff_data.return_value = {"source": "test"}
        return mock_dispatcher

    def test_tester_gets_env_var_when_enabled(self):
        """TESTER agent receives EGG_INTEGRATION_TEST_ENABLED=true when enabled."""
        pipeline = _make_pipeline()
        mock_dispatcher = self._make_dispatcher([[AgentRole.TESTER], []])

        captured_env = {}

        def spy_spawn(role, prompt, extra_env):
            captured_env.update(extra_env or {})
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=spy_spawn,
            integration_test_enabled=True,
        )

        executor.execute_all_waves(
            agent_prompts={AgentRole.TESTER: "run tests"},
            max_waves=1,
        )

        assert captured_env.get("EGG_INTEGRATION_TEST_ENABLED") == "true"

    def test_coder_does_not_get_env_var_when_enabled(self):
        """CODER agent does NOT receive EGG_INTEGRATION_TEST_ENABLED."""
        pipeline = _make_pipeline()
        mock_dispatcher = self._make_dispatcher([[AgentRole.CODER], []])

        captured_env = {}

        def spy_spawn(role, prompt, extra_env):
            captured_env.update(extra_env or {})
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=spy_spawn,
            integration_test_enabled=True,
        )

        executor.execute_all_waves(
            agent_prompts={AgentRole.CODER: "write code"},
            max_waves=1,
        )

        assert "EGG_INTEGRATION_TEST_ENABLED" not in captured_env

    def test_tester_does_not_get_env_var_when_disabled(self):
        """TESTER agent does NOT receive EGG_INTEGRATION_TEST_ENABLED when disabled."""
        pipeline = _make_pipeline()
        mock_dispatcher = self._make_dispatcher([[AgentRole.TESTER], []])

        captured_env = {}

        def spy_spawn(role, prompt, extra_env):
            captured_env.update(extra_env or {})
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=spy_spawn,
            integration_test_enabled=False,
        )

        executor.execute_all_waves(
            agent_prompts={AgentRole.TESTER: "run tests"},
            max_waves=1,
        )

        assert "EGG_INTEGRATION_TEST_ENABLED" not in captured_env

    def test_mixed_wave_only_tester_gets_env(self):
        """In a wave with both CODER and TESTER, only TESTER gets the env var."""
        pipeline = _make_pipeline()
        mock_dispatcher = self._make_dispatcher([[AgentRole.CODER, AgentRole.TESTER], []])

        envs_by_role = {}

        def spy_spawn(role, prompt, extra_env):
            envs_by_role[role] = dict(extra_env or {})
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=spy_spawn,
            integration_test_enabled=True,
        )

        executor.execute_all_waves(
            agent_prompts={
                AgentRole.CODER: "write code",
                AgentRole.TESTER: "run tests",
            },
            max_waves=1,
        )

        assert envs_by_role[AgentRole.TESTER].get("EGG_INTEGRATION_TEST_ENABLED") == "true"
        assert "EGG_INTEGRATION_TEST_ENABLED" not in envs_by_role.get(AgentRole.CODER, {})
