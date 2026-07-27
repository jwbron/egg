"""Tests for the agent livelock / repetition-loop detector (#3665).

CORRECTED per operator feedback (cq-1, cq-3):

* **cq-1**: The detector reads the live session transcript (not
  ``agent_log_store``) and keys on the **full untruncated**
  ``(tool_name, input)`` pair (no 80-char truncation). Tests verify
  that distinct inputs sharing a prefix are NOT collapsed.

* **cq-3**: ``requires_adjudication=True`` — the detector escalates to
  HITL with the looping input quoted verbatim, not a nudge.

* **Metric correction**: The detector uses novelty counting (fire at
  zero new inputs in the trailing window), not a ratio threshold.
  Tests verify that a 3-cycle loop (ABC repeated) fires, and that
  low-but-nonzero novelty produces a WARN-tier finding.
"""

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
        # All Bash calls are identical → 1 unique; Read is different → 2 unique
        assert len(set(sigs)) == 2

    def test_extracts_json_tool_calls(self) -> None:
        from health_checks.tier1.loop_detection import _extract_tool_signatures

        logs = '{"tool": "Bash", "input": {"command": "ls"}}\n' * 10
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

    def test_full_untruncated_signature_no_80_char_limit(self) -> None:
        """cq-1: signatures must NOT be truncated to 80 chars.

        Distinct inputs sharing a prefix must remain distinct. The fix commit
        truncated to 80 chars, causing false negatives when distinct commands
        share a prefix.
        """
        from health_checks.tier1.loop_detection import _extract_tool_signatures

        # Two inputs that share a 80-char prefix but differ after — must be distinct
        prefix = "x" * 80
        logs = f"> Bash: {prefix}aaa\n> Bash: {prefix}bbb\n"
        sigs = _extract_tool_signatures(logs)
        assert len(sigs) == 2
        assert len(set(sigs)) == 2  # Both are unique — NOT collapsed by truncation


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
        """A single-input loop: same tool call repeated 12 times.

        Novelty counting: only the first call is novel (never seen before),
        the remaining 11 are repeats. But the trailing window has zero NEW
        inputs — the agent is repeating the same call. The detector fires
        because novelty in the window is 0 (all calls in the window were
        already seen before the window started).
        """
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
        # cq-3: requires_adjudication=True (HITL escalation, not nudge)
        assert finding.requires_adjudication is True
        assert finding.evidence["role"] == "coder"
        assert finding.evidence["total_tool_calls"] == 12
        assert finding.evidence["unique_tool_calls"] == 1
        assert finding.evidence["novel_in_window"] == 0
        # The looping input must be quoted verbatim in the evidence
        assert "looping_input" in finding.evidence

    def test_no_finding_when_tool_calls_are_unique(self) -> None:
        """A working agent: all tool calls are different (high novelty)."""
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
        """A 3-cycle loop: ABC repeated 20 times = 60 calls, 3 unique.

        Novelty counting: the first 3 calls (A, B, C) are novel; the remaining
        57 are repeats. In the trailing window, zero new inputs are produced.
        The detector fires because novelty in the window is 0.
        """
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join(["> Bash: a", "> Bash: b", "> Bash: c"] * 20)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["total_tool_calls"] == 60
        assert finding.evidence["unique_tool_calls"] == 3
        assert finding.evidence["novel_in_window"] == 0

    def test_no_finding_when_ratio_above_threshold(self) -> None:
        """If the agent is producing new inputs, don't fire.

        This test verifies the novelty metric (not ratio): 10 unique calls
        + 2 repeats = 12 calls. The first 10 are novel, the last 2 are
        repeats. But since the trailing window contains novel inputs
        (the first 10), the detector does NOT fire.
        """
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        # 10 unique calls followed by 2 repeats
        logs = "\n".join(
            [f"> Bash: cmd_{i}" for i in range(10)] + ["> Bash: cmd_0", "> Bash: cmd_1"]
        )
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is None

    def test_finding_for_8_cycle_loop(self) -> None:
        """An 8-cycle loop: ABCDEFGH repeated 5 times = 40 calls, 8 unique.

        Novelty counting: the first 8 calls are novel; the remaining 32 are
        repeats. In the trailing window, zero new inputs are produced.
        """
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join([f"> Bash: {c}" for c in "ABCDEFGH"] * 5)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["total_tool_calls"] == 40
        assert finding.evidence["unique_tool_calls"] == 8
        assert finding.evidence["novel_in_window"] == 0

    def test_warn_finding_for_low_novelty(self) -> None:
        """Low but nonzero novelty produces a WARN-tier finding.

        10 unique calls + 90 repeats = 100 calls. Novelty = 10 (the first 10
        are new). Novelty fraction = 10/100 = 0.1, which is at the threshold.
        With 20 unique + 80 repeats = 100 calls, novelty = 20, fraction = 0.2,
        which is above the threshold — no finding.
        """
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        # 20 unique + 80 repeats = 100 calls, novelty fraction = 0.2 > 0.1
        logs = "\n".join(
            [f"> Bash: cmd_{i}" for i in range(20)] + ["> Bash: cmd_0"] * 80
        )
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        # novelty fraction = 20/100 = 0.2 > 0.1, so no finding
        assert finding is None

    def test_warn_finding_for_very_low_novelty(self) -> None:
        """Very low novelty (but nonzero) produces a WARN-tier finding.

        5 unique + 95 repeats = 100 calls. Novelty = 5, fraction = 0.05 < 0.1.
        This should produce a warning finding, not a hard livelock alert.
        """
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        # 5 unique + 95 repeats = 100 calls, novelty fraction = 0.05 < 0.1
        logs = "\n".join(
            [f"> Bash: cmd_{i}" for i in range(5)] + ["> Bash: cmd_0"] * 95
        )
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == "agent_livelock_warning"
        assert finding.severity.value == "medium"
        assert finding.requires_adjudication is False
        assert finding.evidence["novel_in_window"] == 5

    def test_finding_quotes_looping_input_verbatim(self) -> None:
        """cq-3: the looping input must be quoted verbatim in the finding."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        looping_input = "> Bash: grep -rn 'convergence_stall' orchestrator/event_loop/_loop.py | head -20"
        logs = "\n".join([looping_input] * 15)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        # The looping input must be in the evidence
        assert finding.evidence["looping_input"] == "Bash: grep -rn 'convergence_stall' orchestrator/event_loop/_loop.py | head -20"
        # And in the recommended action
        assert "grep -rn 'convergence_stall'" in finding.recommended_action

    def test_finding_for_2_cycle_loop(self) -> None:
        """A 2-cycle loop: AB repeated 15 times = 30 calls, 2 unique."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        logs = "\n".join(["> Bash: a", "> Bash: b"] * 15)
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=logs,
        ):
            finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["total_tool_calls"] == 30
        assert finding.evidence["unique_tool_calls"] == 2
        assert finding.evidence["novel_in_window"] == 0


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
        # cq-3: requires_adjudication must be True in the details
        assert result.details["requires_adjudication"] is True

    def test_check_returns_degraded_for_warn_finding(self) -> None:
        """WARN-tier findings should also produce a degraded result."""
        from health_checks.tier1.loop_detection import AgentLivelockCheck

        check = AgentLivelockCheck()
        agent = RunningAgent(role="coder", state="running")
        # 5 unique + 95 repeats = 100 calls, novelty fraction = 0.05 < 0.1
        logs = "\n".join(
            [f"> Bash: cmd_{i}" for i in range(5)] + ["> Bash: cmd_0"] * 95
        )

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
        assert result.details["finding_class"] == "agent_livelock_warning"
        assert result.details["requires_adjudication"] is False
