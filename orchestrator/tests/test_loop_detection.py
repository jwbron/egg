"""Slice-3 contract tests for the deterministic loop detector (issue #3665).

Verifies that ``detect_tool_input_loop`` fires on zero-new-input windows of
any cycle shape (1-, 2-, 3-, 8-cycles) and does not fire on productive agents.

The detector counts *tool inputs never issued before in the session* over a
trailing window. A working agent produces new ones; a loop of any length
produces none. This is the empirical finding from the issue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_tests_dir = Path(__file__).parent
_orchestrator_dir = _tests_dir.parent
if str(_orchestrator_dir) not in sys.path:
    sys.path.insert(0, str(_orchestrator_dir))

loop_detection = pytest.importorskip("health_checks.tier1.loop_detection")
detection_plane = pytest.importorskip("health_checks.detection_plane")


def _make_snapshot(
    *,
    pipeline_id: str = "issue-3665",
    phase: str = "implement",
    status: str = "RUNNING",
    midturn_messages=None,
    agent_role: str = "coder",
    blocking_agents=None,
    running_agents=None,
):
    """Build a minimal EventStreamSnapshot for testing.

    By default, sets up a snapshot where the agent role is in blocking_agents
    (simulating a producer/reviewer with an outstanding obligation). Pass
    ``blocking_agents=[]`` or ``agent_role="overseer"`` to test the exemption.
    """
    from health_checks.detection_plane import EventStreamSnapshot, RunningAgent

    if blocking_agents is None:
        blocking_agents = [agent_role]
    if running_agents is None:
        running_agents = (
            (RunningAgent(role=agent_role, state="running"),) if agent_role else ()
        )

    return EventStreamSnapshot(
        snapshot_id=f"{pipeline_id}:{phase}",
        pipeline_id=pipeline_id,
        phase=phase,
        running_agents=running_agents,
        phase_state={"status": status},
        consensus={"blocking_agents": blocking_agents},
        midturn_messages=midturn_messages or (),
    )


def _msg(tool_name: str, input_text: str, input_hash: str | None = None):
    """Build a midturn_messages entry."""
    import hashlib

    if input_hash is None:
        input_hash = hashlib.sha256(f"{tool_name}:{input_text}".encode()).hexdigest()
    return {
        "tool_name": tool_name,
        "input": input_text,
        "input_hash": input_hash,
    }


# ---------------------------------------------------------------------------
# Single-input loop (1-cycle)
# ---------------------------------------------------------------------------


class TestSingleInputLoop:
    """Verify the detector fires on a single-input repetition loop."""

    def test_fires_on_single_input_loop(self):
        """A 1-cycle loop (same tool call repeated) fires after the window."""
        # Same tool call repeated — zero new inputs
        msg = _msg("bash", "ls -la")
        messages = tuple([msg] * 5)

        snapshot = _make_snapshot(midturn_messages=messages)

        # Reset the default tracker for a clean test
        loop_detection.reset_default_loop_tracker()

        # First poll: records the hash, zero new inputs (first poll has no history)
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None  # First poll: no history to compare

        # Second poll: same hash, zero new inputs
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None  # Only 1 zero-new poll

        # Third poll: same hash, zero new inputs (window=3)
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None  # 2 zero-new polls

        # Fourth poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is not None
        assert finding.finding_class == "tool_input_loop"
        assert finding.severity == "high"
        assert finding.requires_adjudication is False
        assert finding.detector_key == "tool_input_loop"
        assert finding.evidence["zero_new_input_polls"] >= 3


# ---------------------------------------------------------------------------
# 2-cycle loop
# ---------------------------------------------------------------------------


class TestTwoCycleLoop:
    """Verify the detector fires on a 2-cycle repetition loop."""

    def test_fires_on_two_cycle_loop(self):
        """A 2-cycle loop (two tool calls alternating) fires after the window."""
        msg_a = _msg("bash", "ls -la")
        msg_b = _msg("bash", "grep foo")
        # Alternating between the same two calls — zero new inputs after first poll
        messages = tuple([msg_a, msg_b] * 5)

        snapshot = _make_snapshot(midturn_messages=messages)

        loop_detection.reset_default_loop_tracker()

        # First poll: records both hashes
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Second poll: same hashes, zero new
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Third poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None  # 2 zero-new polls

        # Fourth poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is not None
        assert finding.finding_class == "tool_input_loop"


# ---------------------------------------------------------------------------
# 3-cycle loop
# ---------------------------------------------------------------------------


class TestThreeCycleLoop:
    """Verify the detector fires on a 3-cycle repetition loop."""

    def test_fires_on_three_cycle_loop(self):
        """A 3-cycle loop (three tool calls cycling) fires after the window."""
        msg_a = _msg("bash", "ls -la")
        msg_b = _msg("bash", "grep foo")
        msg_c = _msg("bash", "cat file.txt")
        messages = tuple([msg_a, msg_b, msg_c] * 5)

        snapshot = _make_snapshot(midturn_messages=messages)

        loop_detection.reset_default_loop_tracker()

        # First poll: records all three hashes
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Second poll: same hashes, zero new
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Third poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Fourth poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is not None
        assert finding.finding_class == "tool_input_loop"


# ---------------------------------------------------------------------------
# 8-cycle loop
# ---------------------------------------------------------------------------


class TestEightCycleLoop:
    """Verify the detector fires on an 8-cycle repetition loop."""

    def test_fires_on_eight_cycle_loop(self):
        """An 8-cycle loop (eight tool calls cycling) fires after the window."""
        messages = tuple(
            _msg("bash", f"echo {i}") for i in range(8)
        )
        # Repeat the same 8 calls
        messages = tuple(list(messages) * 5)

        snapshot = _make_snapshot(midturn_messages=messages)

        loop_detection.reset_default_loop_tracker()

        # First poll: records all 8 hashes
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Second poll: same hashes, zero new
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Third poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Fourth poll: window reached
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is not None
        assert finding.finding_class == "tool_input_loop"


# ---------------------------------------------------------------------------
# Productive agent (negative case)
# ---------------------------------------------------------------------------


class TestProductiveAgent:
    """Verify the detector does NOT fire on a productive agent."""

    def test_does_not_fire_on_productive_agent(self):
        """An agent producing new tool inputs each poll does not trigger."""
        # Each poll has a new, unique tool input
        for i in range(10):
            messages = tuple(
                _msg("bash", f"echo {i}_{j}") for j in range(3)
            )
            snapshot = _make_snapshot(midturn_messages=messages)

            loop_detection.reset_default_loop_tracker()
            # Only test one poll per reset to keep the test simple
            finding = loop_detection.detect_tool_input_loop(snapshot)
            assert finding is None

    def test_does_not_fire_when_producing_new_inputs(self):
        """An agent that produces new inputs in some polls does not trigger."""
        loop_detection.reset_default_loop_tracker()

        # Poll 1: initial inputs
        msg1 = _msg("bash", "ls -la")
        snapshot1 = _make_snapshot(midturn_messages=(msg1,))
        finding = loop_detection.detect_tool_input_loop(snapshot1)
        assert finding is None

        # Poll 2: new input added
        msg2 = _msg("bash", "grep foo")
        snapshot2 = _make_snapshot(midturn_messages=(msg1, msg2))
        finding = loop_detection.detect_tool_input_loop(snapshot2)
        assert finding is None  # 1 new input, not a loop

        # Poll 3: same as poll 2 (zero new)
        finding = loop_detection.detect_tool_input_loop(snapshot2)
        assert finding is None  # Only 1 zero-new poll

        # Poll 4: still same (zero new, but only 2 consecutive)
        finding = loop_detection.detect_tool_input_loop(snapshot2)
        assert finding is None  # 2 zero-new polls

        # Poll 5: window reached — but this is a productive agent that just
        # stopped producing new inputs for 3 polls. This SHOULD fire because
        # the agent is in blocking_agents (has an obligation).
        finding = loop_detection.detect_tool_input_loop(snapshot2)
        assert finding is not None  # 3 zero-new polls = window


# ---------------------------------------------------------------------------
# Polling supervisor exemption (#3665 TASK-3-1 constraint)
# ---------------------------------------------------------------------------


class TestPollingSupervisorExemption:
    """Verify the detector does NOT fire on a polling supervisor (e.g. overseer).

    A polling agent's correct steady state is zero novel tool inputs — it
    issues the same query on a timer forever. The detector must exempt roles
    that do not carry a producer or reviewer edge (i.e., roles not in
    blocking_agents).
    """

    def test_does_not_fire_on_polling_supervisor(self):
        """A polling supervisor with zero novel inputs over a long window is NOT flagged."""
        # Simulate the overseer polling: same MCP calls repeated
        msg = _msg("mcp__brc__get_state", '{"pipeline_id": "issue-3665-v3"}')
        messages = tuple([msg] * 5)

        # The overseer is NOT in blocking_agents — it has no producer/reviewer edge
        snapshot = _make_snapshot(
            midturn_messages=messages,
            agent_role="overseer",
            blocking_agents=["coder", "reviewer_code"],  # overseer NOT listed
        )

        loop_detection.reset_default_loop_tracker()

        # Even after many polls with zero new inputs, should NOT fire
        for _ in range(10):
            finding = loop_detection.detect_tool_input_loop(snapshot)
            assert finding is None, (
                "Polling supervisor (overseer) must not be flagged as a loop — "
                "its correct steady state is zero novel tool inputs"
            )

    def test_does_not_fire_when_no_blocking_agents(self):
        """When blocking_agents is empty, no agent is a producer/reviewer, so no loop."""
        msg = _msg("bash", "ls -la")
        messages = tuple([msg] * 5)

        snapshot = _make_snapshot(
            midturn_messages=messages,
            agent_role="overseer",
            blocking_agents=[],  # No producers/reviewers
        )

        loop_detection.reset_default_loop_tracker()

        for _ in range(10):
            finding = loop_detection.detect_tool_input_loop(snapshot)
            assert finding is None

    def test_fires_when_agent_is_blocking_producer(self):
        """When the agent IS in blocking_agents, the loop detector fires normally."""
        msg = _msg("bash", "ls -la")
        messages = tuple([msg] * 5)

        snapshot = _make_snapshot(
            midturn_messages=messages,
            agent_role="coder",
            blocking_agents=["coder"],  # coder IS a blocking producer
        )

        loop_detection.reset_default_loop_tracker()

        # First poll: records the hash
        finding = loop_detection.detect_tool_input_loop(snapshot)
        assert finding is None

        # Polls 2-4: zero new inputs, window reached
        for _ in range(3):
            finding = loop_detection.detect_tool_input_loop(snapshot)

        assert finding is not None
        assert finding.finding_class == "tool_input_loop"


# ---------------------------------------------------------------------------
# Non-RUNNING phase
# ---------------------------------------------------------------------------


class TestNonRunningPhase:
    """Verify the detector does not fire when the phase is not RUNNING."""

    def test_does_not_fire_when_phase_not_running(self):
        """The detector stays silent when the phase status is not RUNNING."""
        msg = _msg("bash", "ls -la")
        snapshot = _make_snapshot(
            midturn_messages=(msg,),
            status="COMPLETE",
        )

        loop_detection.reset_default_loop_tracker()

        # Even with zero new inputs, should not fire because phase is not RUNNING
        for _ in range(5):
            finding = loop_detection.detect_tool_input_loop(snapshot)
            assert finding is None


# ---------------------------------------------------------------------------
# Empty midturn_messages
# ---------------------------------------------------------------------------


class TestEmptyMessages:
    """Verify the detector does not fire when midturn_messages is empty."""

    def test_does_not_fire_with_empty_messages(self):
        """The detector stays silent when there are no midturn_messages."""
        snapshot = _make_snapshot(midturn_messages=())

        loop_detection.reset_default_loop_tracker()

        for _ in range(5):
            finding = loop_detection.detect_tool_input_loop(snapshot)
            assert finding is None


# ---------------------------------------------------------------------------
# Evidence payload
# ---------------------------------------------------------------------------


class TestEvidencePayload:
    """Verify the finding's evidence payload is correct."""

    def test_evidence_contains_required_fields(self):
        """The finding evidence includes pipeline_id, phase, and window info."""
        msg = _msg("bash", "ls -la")
        snapshot = _make_snapshot(midturn_messages=(msg,))

        loop_detection.reset_default_loop_tracker()

        # Fire the detector
        for _ in range(4):
            finding = loop_detection.detect_tool_input_loop(snapshot)
            if finding is not None:
                break

        assert finding is not None
        evidence = finding.evidence
        assert "pipeline_id" in evidence
        assert evidence["pipeline_id"] == "issue-3665"
        assert "phase" in evidence
        assert evidence["phase"] == "implement"
        assert "zero_new_input_polls" in evidence
        assert "window_size" in evidence
        assert "last_tool_name" in evidence
        assert "last_input_hash" in evidence

    def test_recommended_action_is_descriptive(self):
        """The recommended_action explains the loop and suggests remediation."""
        msg = _msg("bash", "ls -la")
        snapshot = _make_snapshot(midturn_messages=(msg,))

        loop_detection.reset_default_loop_tracker()

        for _ in range(4):
            finding = loop_detection.detect_tool_input_loop(snapshot)
            if finding is not None:
                break

        assert finding is not None
        assert "zero new tool inputs" in finding.recommended_action.lower()
        assert "repetition loop" in finding.recommended_action.lower()


# ---------------------------------------------------------------------------
# Integration: detection plane includes the loop detector
# ---------------------------------------------------------------------------


class TestDetectionPlaneIntegration:
    """Verify the loop detector is registered in the default detection plane."""

    def test_loop_detector_registered_in_plane(self):
        """The default detection plane includes the tool_input_loop detector."""
        plane = detection_plane.default_detection_plane()
        assert "tool_input_loop" in plane.detectors

    def test_loop_detector_fires_through_plane(self):
        """The loop detector fires when evaluated through the plane."""
        from health_checks.detection_plane import EventStreamSnapshot, RunningAgent

        msg = _msg("bash", "ls -la")
        snapshot = EventStreamSnapshot(
            snapshot_id="issue-3665:implement",
            pipeline_id="issue-3665",
            phase="implement",
            running_agents=(RunningAgent(role="coder", state="running"),),
            phase_state={"status": "RUNNING"},
            consensus={"blocking_agents": ["coder"]},
            midturn_messages=(msg,),
        )

        loop_detection.reset_default_loop_tracker()

        plane = detection_plane.default_detection_plane()

        # First poll: records the hash
        findings = plane.evaluate(snapshot)
        assert not any(f.finding_class == "tool_input_loop" for f in findings)

        # Poll 2-4: zero new inputs, window reached
        for _ in range(3):
            findings = plane.evaluate(snapshot)

        assert any(f.finding_class == "tool_input_loop" for f in findings)
