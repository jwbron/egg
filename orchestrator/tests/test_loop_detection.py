"""Tests for the agent livelock / repetition-loop detector (#3665)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from health_checks.detection_plane import EventStreamSnapshot, RunningAgent
from health_checks.tier1.loop_detection import (
    FINDING_AGENT_LIVELOCK,
    detect_agent_livelock,
)


def _make_snapshot(
    running_agents: list[RunningAgent] | None = None,
    phase: str = "implement",
    status: str = "RUNNING",
    pipeline_id: str = "issue-99",
) -> EventStreamSnapshot:
    return EventStreamSnapshot(
        snapshot_id=f"{pipeline_id}:{phase}",
        pipeline_id=pipeline_id,
        phase=phase,
        running_agents=tuple(running_agents or []),
        phase_state={"status": status},
    )


class TestExtractToolSignatures:
    """Tests for the tool-signature extraction helper."""

    def test_extracts_claude_code_tool_calls(self) -> None:
        from health_checks.tier1.loop_detection import _extract_tool_signatures

        logs = """
> Bash: ls -la /tmp
> Read: /home/egg/repos/egg/README.md
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
> Bash: ls -la /tmp
"""
        sigs = _extract_tool_signatures(logs)
        assert len(sigs) == 12
        # All are the same tool+args → 1 unique
        assert len(set(sigs)) == 2  # Bash:ls -la /tmp and Read:/home/...

    def test_extracts_json_tool_calls(self) -> None:
        from health_checks.tier1.loop_detection import _extract_tool_signatures

        logs = '{"tool": "Bash", "arguments": {"command": "ls"}}\n' * 10
        sigs = _extract_tool_signatures(logs)
        assert len(sigs) == 10
        assert len(set(sigs)) == 1

    def test_empty_logs(self) -> None:
        from health_checks.tier1.loop_detection import _extract_tool_signatures

        assert _extract_tool_signatures("") == []

    def test_no_tool_calls(self) -> None:
        from health_checks.tier1.loop_detection import _extract_tool_signatures

        logs = "Some log text without tool calls\nMore text\n"
        assert _extract_tool_signatures(logs) == []


class TestDetectAgentLivelock:
    """Tests for the detect_agent_livelock detector."""

    def test_no_finding_when_not_running(self) -> None:
        snapshot = _make_snapshot(status="COMPLETE")
        assert detect_agent_livelock(snapshot) is None

    def test_no_finding_when_no_running_agents(self) -> None:
        snapshot = _make_snapshot(running_agents=[])
        assert detect_agent_livelock(snapshot) is None

    def test_no_finding_when_logs_unavailable(self) -> None:
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=None,
        ):
            assert detect_agent_livelock(snapshot) is None

    def test_finding_when_all_tool_calls_identical(self) -> None:
        """A single-input loop: same tool call repeated 12 times."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join(["> Bash: ls -la /tmp"] * 12)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.severity.value == "high"
        assert finding.requires_adjudication is False
        assert finding.evidence["role"] == "coder"
        assert finding.evidence["total_tool_calls"] == 12
        assert finding.evidence["unique_tool_calls"] == 1

    def test_no_finding_when_tool_calls_are_unique(self) -> None:
        """A working agent: all tool calls are different."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join([f"> Bash: command_{i}" for i in range(20)])
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is None

    def test_no_finding_when_too_few_tool_calls(self) -> None:
        """Below the minimum threshold, don't fire."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join(["> Bash: ls -la /tmp"] * 5)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is None

    def test_finding_for_multi_cycle_loop(self) -> None:
        """A 3-cycle loop: ABC repeated 20 times = 60 calls, 3 unique (ratio 0.05)."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join(["> Bash: a", "> Bash: b", "> Bash: c"] * 20)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.evidence["total_tool_calls"] == 60
        assert finding.evidence["unique_tool_calls"] == 3
        assert finding.evidence["unique_ratio"] == 0.05

    def test_no_finding_when_ratio_above_threshold(self) -> None:
        """If unique ratio is above 0.1, don't fire even with repeats."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        # 10 unique calls + 2 repeats = 12 calls, 10 unique, ratio = 0.83
        logs = "\n".join(
            [f"> Bash: cmd_{i}" for i in range(10)] + ["> Bash: cmd_0", "> Bash: cmd_1"]
        )
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is None


class TestAgentLivelockCheck:
    """Tests for the AgentLivelockCheck class wrapper."""

    def test_check_returns_healthy_when_no_finding(self) -> None:
        from health_checks.tier1.loop_detection import AgentLivelockCheck

        check = AgentLivelockCheck()
        context = MagicMock()
        context.pipeline_id = "issue-99"
        context.pipeline = MagicMock()
        context.pipeline.status = MagicMock()
        context.pipeline.status.value = "COMPLETE"
        context.pipeline.current_phase = MagicMock()
        context.pipeline.current_phase.value = "implement"
        context.current_phase = context.pipeline.current_phase

        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=None,
        ):
            result = check.run(context)
        assert result.status.value == "healthy"
        assert result.check_name == "agent_livelock"

    def test_check_returns_degraded_when_finding(self) -> None:
        from health_checks.tier1.loop_detection import AgentLivelockCheck

        check = AgentLivelockCheck()
        agent = RunningAgent(role="coder", state="running")
        logs = "\n".join(["> Bash: ls -la /tmp"] * 12)

        with (
            patch("health_checks.detection_plane.snapshot_from_health_context") as mock_snapshot,
            patch(
                "health_checks.tier1.loop_detection._get_agent_logs",
                return_value=logs,
            ),
        ):
            mock_snapshot.return_value = _make_snapshot(running_agents=[agent])
            context = MagicMock()
            context.pipeline_id = "issue-99"
            result = check.run(context)
        assert result.status.value == "degraded"
        assert result.action.value == "alert"
