"""Tests for multi_agent.MultiAgentExecutor.execute_all_waves() max_waves cap."""

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
    AgentExecution,
    AgentExecutionStatus,
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


class TestExecuteAllWavesMaxCap:
    """Tests for the max_waves safety cap on execute_all_waves()."""

    def test_stops_after_max_waves(self):
        """execute_all_waves stops after max_waves even if dispatcher keeps returning agents."""
        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()

        # get_agents_to_run always returns something -> infinite loop without cap
        mock_dispatcher.get_agents_to_run.return_value = [AgentRole.CODER]
        mock_dispatcher.get_next_dispatch.return_value = MagicMock(wave_number=1)

        # Use a spawn_fn to avoid needing Docker
        call_count = 0

        def fake_spawn(role, prompt, extra_env):
            nonlocal call_count
            call_count += 1
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=fake_spawn,
        )

        max_waves = 5
        waves = executor.execute_all_waves(
            agent_prompts={AgentRole.CODER: "do work"},
            max_waves=max_waves,
        )

        assert len(waves) == max_waves
        mock_dispatcher.save_contract.assert_called_once()

    def test_default_max_waves_is_5(self):
        """Default max_waves is 5 (verify signature default)."""
        import inspect

        sig = inspect.signature(MultiAgentExecutor.execute_all_waves)
        default = sig.parameters["max_waves"].default
        assert default == 5

    def test_stops_before_max_when_no_more_waves(self):
        """Stops normally when dispatcher returns no more agents before max_waves."""
        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()

        # Return agents for 2 waves, then None
        mock_dispatcher.get_agents_to_run.side_effect = [
            [AgentRole.CODER],
            [AgentRole.CODER],
            [],  # No more agents
        ]
        mock_dispatcher.get_next_dispatch.return_value = MagicMock(wave_number=1)

        def fake_spawn(role, prompt, extra_env):
            return (0, "ok")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=fake_spawn,
        )

        waves = executor.execute_all_waves(
            agent_prompts={AgentRole.CODER: "do work"},
            max_waves=10,
        )

        assert len(waves) == 2

    def test_stops_on_failure_before_max_waves(self):
        """Stops on wave failure before reaching max_waves."""
        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.save_contract = MagicMock()

        mock_dispatcher.get_agents_to_run.return_value = [AgentRole.CODER]
        mock_dispatcher.get_next_dispatch.return_value = MagicMock(wave_number=1)

        # fail_agent returns a FAILED execution
        mock_dispatcher.fail_agent.return_value = AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.FAILED,
            error="Exit code: 1",
        )

        def fail_spawn(role, prompt, extra_env):
            return (1, "error")

        executor = MultiAgentExecutor(
            pipeline=pipeline,
            repo_path=Path("/repo"),
            dispatcher=mock_dispatcher,
            spawn_fn=fail_spawn,
        )

        waves = executor.execute_all_waves(
            agent_prompts={AgentRole.CODER: "do work"},
            max_waves=10,
        )

        # Should stop after first wave due to failure
        assert len(waves) == 1
