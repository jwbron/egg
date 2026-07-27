"""Tests for the agent livelock / repetition-loop detector (#3665).

CORRECTED per operator feedback (cq-1, cq-3):

* **cq-1**: The detector reads the live session transcript from
  ``session_state_store`` (not ``agent_log_store``) and keys on the
  **full untruncated** ``(tool_name, input)`` pair (no 80-char
  truncation). Tests verify that distinct inputs sharing a prefix are
  NOT collapsed.

* **cq-3**: ``requires_adjudication=True`` — the detector escalates to
  HITL with the looping input quoted verbatim, not a nudge.

* **Metric correction**: The detector uses novelty counting (fire at
  zero new inputs in the trailing window), not a ratio threshold.

Tests use the ``tool_calls_by_role`` path (populated in the snapshot's
``raw`` field) to exercise the production code path without mocking
``_get_agent_logs``.
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
    tool_calls_by_role: dict[str, list[str]] | None = None,
) -> EventStreamSnapshot:
    """Build a snapshot with optional tool_calls_by_role in the raw field.

    The ``tool_calls_by_role`` field is what the detector reads in production
    (populated by ``snapshot_from_health_context`` via
    ``_extract_tool_calls_by_role``). Tests provide it directly here so the
    production code path is exercised without mocking ``_get_agent_logs``.
    """
    raw: dict = {}
    if tool_calls_by_role:
        raw["tool_calls_by_role"] = tool_calls_by_role
    return EventStreamSnapshot(
        snapshot_id=f"{pipeline_id}:{phase}",
        pipeline_id=pipeline_id,
        phase=phase,
        running_agents=tuple(running_agents or []),
        phase_state={"status": status},
        raw=raw,
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

        # Two inputs that share an 80-char prefix but differ after — must be distinct
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

    def test_no_finding_when_no_tool_calls(self) -> None:
        """No tool_calls_by_role in snapshot and no transcript available."""
        agent = RunningAgent(role="coder", state="running")
        snapshot = _make_snapshot(running_agents=[agent])
        with patch(
            "health_checks.tier1.loop_detection._get_agent_logs",
            return_value=None,
        ):
            assert detect_agent_livelock(snapshot) is None

    def test_finding_when_all_tool_calls_identical(self) -> None:
        """A single-input loop: same tool call repeated 12 times.

        Novelty counting: the trailing window (last half = 6 calls) contains
        only repeats of the same signature, which was already seen in the
        first half. Novelty = 0 → livelock.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = ["Bash:ls -la /tmp"] * 12
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
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
        """A working agent: all tool calls are different (high novelty).

        20 unique calls. The trailing window (last half = 10 calls) contains
        signatures that were NOT seen in the first half → novelty > 0.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = [f"Bash:command_{i}" for i in range(20)]
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is None

    def test_finding_when_too_few_tool_calls_but_all_identical(self) -> None:
        """5 identical calls: 5 >= min_tool_calls (3), and all are identical.

        The trailing window (last half = 2 calls) contains only repeats →
        novelty = 0 → livelock.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = ["Bash:ls -la /tmp"] * 5
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["novel_in_window"] == 0

    def test_finding_for_multi_cycle_loop(self) -> None:
        """A 3-cycle loop: ABC repeated 20 times = 60 calls, 3 unique.

        Novelty counting: the trailing window (last half = 30 calls) contains
        only A, B, C, all of which were seen in the first half → novelty = 0.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = ["Bash:a", "Bash:b", "Bash:c"] * 20
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["total_tool_calls"] == 60
        assert finding.evidence["unique_tool_calls"] == 3
        assert finding.evidence["novel_in_window"] == 0

    def test_no_finding_when_agent_is_making_progress(self) -> None:
        """If the agent is producing new inputs, don't fire.

        10 unique calls followed by 2 repeats = 12 calls. The trailing window
        (last half = 6 calls) contains cmd_6, cmd_7, cmd_8, cmd_9, cmd_0, cmd_1.
        cmd_0 and cmd_1 were seen in the first half, but cmd_6, cmd_7, cmd_8,
        cmd_9 were NOT → novelty = 4 > 0 → no livelock.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = [f"Bash:cmd_{i}" for i in range(10)] + ["Bash:cmd_0", "Bash:cmd_1"]
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is None

    def test_finding_for_8_cycle_loop(self) -> None:
        """An 8-cycle loop: ABCDEFGH repeated 5 times = 40 calls, 8 unique.

        Novelty counting: the trailing window (last half = 20 calls) contains
        only A-H, all of which were seen in the first half → novelty = 0.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = [f"Bash:{c}" for c in "ABCDEFGH"] * 5
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["total_tool_calls"] == 40
        assert finding.evidence["unique_tool_calls"] == 8
        assert finding.evidence["novel_in_window"] == 0

    def test_finding_quotes_looping_input_verbatim(self) -> None:
        """cq-3: the looping input must be quoted verbatim in the finding."""
        agent = RunningAgent(role="coder", state="running")
        looping_input = "Bash:grep -rn 'convergence_stall' orchestrator/event_loop/_loop.py | head -20"
        tool_calls = [looping_input] * 15
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is not None
        # The looping input must be in the evidence
        assert "grep -rn 'convergence_stall'" in finding.evidence["looping_input"]
        # And in the recommended action
        assert "grep -rn 'convergence_stall'" in finding.recommended_action

    def test_finding_for_2_cycle_loop(self) -> None:
        """A 2-cycle loop: AB repeated 15 times = 30 calls, 2 unique.

        Novelty counting: the trailing window (last half = 15 calls) contains
        only A and B, both of which were seen in the first half → novelty = 0.
        """
        agent = RunningAgent(role="coder", state="running")
        tool_calls = ["Bash:a", "Bash:b"] * 15
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        assert finding.evidence["total_tool_calls"] == 30
        assert finding.evidence["unique_tool_calls"] == 2
        assert finding.evidence["novel_in_window"] == 0

    def test_finding_for_livelock_with_distinct_prefixes(self) -> None:
        """cq-1: distinct inputs sharing a prefix must NOT be collapsed.

        Two inputs that share an 80-char prefix but differ after must be
        treated as distinct. If they were truncated (as the fix commit did),
        they would collapse and the detector might miss the loop.
        """
        agent = RunningAgent(role="coder", state="running")
        prefix = "x" * 80
        # 15 calls: alternating between two distinct inputs that share a prefix
        tool_calls = [f"Bash:{prefix}aaa", f"Bash:{prefix}bbb"] * 7 + [f"Bash:{prefix}aaa"]
        snapshot = _make_snapshot(
            running_agents=[agent],
            tool_calls_by_role={"coder": tool_calls},
        )
        finding = detect_agent_livelock(snapshot)
        assert finding is not None
        assert finding.finding_class == FINDING_AGENT_LIVELOCK
        # The two distinct inputs are correctly identified as unique
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
        tool_calls = ["Bash:ls -la /tmp"] * 12

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context"
        ) as mock_snapshot:
            mock_snapshot.return_value = _make_snapshot(
                running_agents=[agent],
                tool_calls_by_role={"coder": tool_calls},
            )
            context = MagicMock()
            context.pipeline_id = "issue-99"
            result = check.run(context)
        assert result.status.value == "degraded"
        assert result.action.value == "alert"
        # cq-3: requires_adjudication must be True in the details
        assert result.details["requires_adjudication"] is True
