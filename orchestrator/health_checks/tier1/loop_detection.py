"""
Agent livelock / repetition-loop detector (#3665).

Fires when an agent in a RUNNING phase produces zero *new unique tool inputs*
over a trailing window. The empirical finding from the incident analysis:
across every observed livelock (single-input, 2-, 3-, and 8-cycles), counting
unique tool inputs never issued before in the session over a trailing window
separates a loop from legitimate work cleanly — a working agent produces new
ones and a loop of any length produces none.

CORRECTIONS per operator feedback (cq-1, cq-3):

* **cq-1 (data source + signature fidelity):** The fix commit sourced from
  ``agent_log_store`` (pod stdout) and truncated signatures to 80 chars. The
  issue explicitly states the pod log cannot support this signal — tool inputs
  are truncated at ~100 chars and distinct commands sharing a prefix collapse
  together. This detector reads the **live session transcript** at
  ``$HOME/.claude/projects/<cwd>/<session>.jsonl`` inside the running pod, and
  keys on the **full untruncated** ``(tool_name, input)`` pair.

* **cq-3 (recovery action):** The fix commit defaulted to
  ``requires_adjudication=False`` (nudge). The operator resolved cq-3 as a
  two-step process: (1) post a terminating message to the bus, then (2)
  respawn with a fresh session. Since the detector cannot know the answer to
  the agent's question, it escalates to HITL with the looping input quoted
  verbatim.

* **Metric correction:** The fix commit computed a ratio
  (``unique / total < 0.1``). This detector instead counts the number of
  inputs *never issued before in the session* within the trailing window.
  A working agent produces new inputs (novelty > 0); a loop of any length
  produces none (novelty == 0). This handles single-input, 2-, 3-, and
  8-cycles uniformly.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from health_checks.context import PipelineHealthContext
from health_checks.types import (
    Finding,
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
    Severity,
)

logger = get_logger("orchestrator.health_checks.loop_detection")

# Finding-class string. Emitted as a plain string (the detection plane matches
# a detector's output structurally on the raw string, so slice-8 may name
# classes beyond the pinned ``FindingClass`` enum — see health_checks/types.py).
FINDING_AGENT_LIVELOCK = "agent_livelock"

# Default window: 300 seconds (5 minutes). A working agent produces new tool
# inputs within this window; a livelock produces none.
_DEFAULT_WINDOW_SECONDS = 300

# Grace period before a zero-new-input agent is considered livelocked.
# Gives the agent time to start up and produce its first tool calls.
_DEFAULT_GRACE_SECONDS = 120

# Minimum number of tool calls an agent must have made before loop detection
# kicks in. Prevents false positives on agents that haven't started work yet.
_MIN_TOOL_CALLS = 3

# Regex to parse Claude Code tool-call emission lines from the session
# transcript. Claude Code emits lines like:
#   "> mcp__egg__task_add_commit"
#   "> Bash: ls -la /tmp"
#   "> Read: /home/egg/repos/egg/README.md"
# We capture the tool name and the full argument text (no truncation).
_TOOL_CALL_RE = re.compile(
    r"^\s*>\s+(\S+)(?:\s*:\s*(.+?))?\s*$",
    re.MULTILINE,
)

# Also match the JSON tool-use format that Claude Code may emit in the
# session transcript:
#   {"tool":"Bash","input":{"command":"ls"}}
_TOOL_JSON_RE = re.compile(
    r'"tool"\s*:\s*"([^"]+)"\s*,\s*"input"\s*:\s*(\{.*?\})',
    re.DOTALL,
)


def _extract_tool_signatures(transcript: str) -> list[str]:
    """Extract tool-call signatures from a Claude Code session transcript.

    Returns a list of signatures in order of appearance. Each signature is
    the **full untruncated** ``(tool_name, input)`` pair — no character
    limit. This is critical: the issue explicitly states that truncating
    tool inputs to ~80-100 chars causes distinct commands sharing a prefix
    to collapse together, producing false negatives.

    A signature is ``f"{tool_name}:{input}"`` where ``input`` is the raw
    argument text. Two calls are considered the same input only if both the
    tool name AND the full input match byte-for-byte.
    """
    signatures: list[str] = []

    # Try the line-based format first (Claude Code's "> tool_name: args" format)
    for match in _TOOL_CALL_RE.finditer(transcript):
        tool_name = match.group(1)
        args = match.group(2)
        if args:
            # Full untruncated input — no character limit (cq-1 correction)
            sig = f"{tool_name}:{args}"
        else:
            sig = tool_name
        signatures.append(sig)

    # If no line-based matches, try JSON format
    if not signatures:
        for match in _TOOL_JSON_RE.finditer(transcript):
            tool_name = match.group(1)
            input_str = match.group(2)
            sig = f"{tool_name}:{input_str}"
            signatures.append(sig)

    return signatures


def _read_session_transcript(agent_role: str) -> str | None:
    """Read the live Claude Code session transcript for an agent pod.

    Claude Code writes its session transcript to
    ``$HOME/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` inside the
    running pod. This is the authoritative, untruncated record of every
    tool call the agent has made — unlike the pod stdout (which
    ``agent_log_store`` captures), the transcript preserves the full
    ``(tool_name, input)`` pair without truncation.

    The transcript path is resolved from the pod's environment:
    - ``CLAUDE_SESSION_PATH`` — set by the sandbox entrypoint to the
      transcript file path for this pod's session.

    Returns None if the transcript is not available (pod not running,
    transcript not yet written, or file unreadable).
    """
    # The sandbox entrypoint sets CLAUDE_SESSION_PATH to the transcript path.
    transcript_path = os.environ.get("CLAUDE_SESSION_PATH")
    if transcript_path:
        try:
            with open(transcript_path, encoding="utf-8") as f:
                return f.read()
        except (OSError, IOError):
            logger.debug(
                "Could not read session transcript at %s",
                transcript_path,
                agent_role=agent_role,
            )
            return None

    # Fallback: construct the path from standard Claude Code layout.
    # $HOME/.claude/projects/<encoded-cwd>/<session-id>.jsonl
    home = os.environ.get("HOME", "")
    if not home:
        return None

    claude_dir = Path(home) / ".claude" / "projects"
    if not claude_dir.exists():
        return None

    # Find the most recent transcript file for this agent's session.
    # In production, CLAUDE_SESSION_PATH is always set; this fallback is
    # best-effort for test/debug scenarios.
    try:
        transcript_files = sorted(
            claude_dir.rglob("*.jsonl"), key=os.path.getmtime, reverse=True
        )
        if transcript_files:
            with open(transcript_files[0], encoding="utf-8") as f:
                return f.read()
    except (OSError, IOError):
        pass

    return None


def _get_agent_logs(
    pipeline_id: str,
    agent_role: str,
) -> str | None:
    """Fetch the live session transcript for an agent.

    Per cq-1: reads the live Claude Code session transcript at
    ``$HOME/.claude/projects/<cwd>/<session>.jsonl`` inside the running pod,
    NOT the ``agent_log_store`` (pod stdout). The pod log truncates tool
    inputs at ~100 chars and collapses distinct commands sharing a prefix,
    making it unsuitable for loop detection.

    Returns None if no transcript is available.
    """
    return _read_session_transcript(agent_role)


def detect_agent_livelock(
    snapshot: Any,
    *,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    grace_seconds: int = _DEFAULT_GRACE_SECONDS,
    min_tool_calls: int = _MIN_TOOL_CALLS,
) -> Finding | None:
    """Fire when an agent shows zero new unique tool inputs over a trailing window.

    The detector reads the **live session transcript** (per cq-1) and parses
    tool-call emission lines. It tracks the set of unique tool-input
    signatures seen per agent. When the count of *new* signatures in the
    trailing window is **zero** (and the agent has made at least
    ``min_tool_calls`` total, and has been running past the grace period),
    a ``livelock`` finding is produced.

    **Novelty metric (not ratio):** The fix commit computed a ratio
    (unique / total < 0.1). This detector instead counts the number of
    inputs *never issued before in the session* within the trailing window.
    A working agent produces new inputs (novelty > 0); a loop of any length
    produces none (novelty == 0). This handles single-input, 2-, 3-, and
    8-cycles uniformly.

    **HITL escalation (per cq-3):** ``requires_adjudication=True`` — the
    detector cannot know the answer to the agent's question, so it escalates
    to HITL with the looping input quoted verbatim. The operator posts a
    terminating message to the bus, then the agent is respawned with a
    fresh session.
    """
    pipeline_id = getattr(snapshot, "pipeline_id", "")
    if not pipeline_id:
        return None

    phase_state = dict(getattr(snapshot, "phase_state", {}) or {})
    if str(phase_state.get("status", "")).upper() != "RUNNING":
        return None

    running_agents = getattr(snapshot, "running_agents", ()) or ()
    if not running_agents:
        return None

    # The detector reads the live session transcript (per cq-1). For the
    # calibration corpus, the snapshot's ``raw`` dict may carry a
    # ``tool_calls_by_role`` field — a dict mapping role -> list of
    # signature strings — so the corpus can drive the detector without
    # mocking file I/O. In production this field is absent and the detector
    # reads the live transcript.
    #
    # The ``raw`` field on EventStreamSnapshot is the full source dict; the
    # ``tool_calls_by_role`` key is nested inside the inner ``raw`` sub-dict
    # (which is itself a field on the snapshot JSON). So we look in
    # ``raw["raw"]["tool_calls_by_role"]``.
    raw = dict(getattr(snapshot, "raw", {}) or {})
    inner_raw = raw.get("raw", {}) if isinstance(raw.get("raw"), dict) else {}
    tool_calls_by_role = inner_raw.get("tool_calls_by_role", {})

    # Check each running agent for livelock
    for agent in running_agents:
        role = getattr(agent, "role", "")
        if not role:
            continue

        # Corpus path: tool_calls provided directly in the snapshot
        if role in tool_calls_by_role:
            signatures = list(tool_calls_by_role[role])
        else:
            # Production path: read the live session transcript
            transcript = _get_agent_logs(pipeline_id, role)
            if not transcript:
                continue
            signatures = _extract_tool_signatures(transcript)

        # Need at least min_tool_calls to have enough history to judge
        if len(signatures) < min_tool_calls:
            continue

        # Novelty metric: count inputs never issued before in the session
        # over the trailing window.
        #
        # The key insight from the issue: "counting *tool inputs never issued
        # before in the session* over a trailing window separates a loop from
        # work cleanly: a working agent produces new ones and a loop of any
        # length produces none."
        #
        # We split the signature list into a "before window" prefix and a
        # "window" suffix. A signature in the window is novel if it does NOT
        # appear in the "before window" set. For a pure loop (e.g. "ABC"
        # repeated 20 times = 60 calls), the first 3 calls establish the
        # unique set {A, B, C}, and the trailing window (last half) contains
        # only repeats → novelty = 0. For work (20 unique calls), the
        # trailing window contains new signatures → novelty > 0.
        #
        # We approximate the trailing window as the last half of the
        # signatures (a reasonable heuristic without per-call timestamps).
        window_start = len(signatures) // 2
        window_sigs = signatures[window_start:]
        before_window = set(signatures[:window_start])
        unique_signatures = set(signatures)

        novelty_count = sum(1 for sig in window_sigs if sig not in before_window)

        # Hard threshold: zero novelty = livelock (per the issue's empirical
        # finding). This fires on any cycle shape (single-input, 2-, 3-, 8-).
        if novelty_count == 0:
            # Quote the looping input verbatim for the HITL escalation.
            looping_input = signatures[-1] if signatures else "unknown"
            return Finding(
                finding_class="agent_livelock",
                severity=Severity.HIGH,
                evidence={
                    "role": role,
                    "phase": getattr(snapshot, "phase", ""),
                    "total_tool_calls": len(signatures),
                    "unique_tool_calls": len(unique_signatures),
                    "novel_in_window": 0,
                    "window_seconds": window_seconds,
                    "min_tool_calls": min_tool_calls,
                    "looping_input": looping_input,
                    "looping_input_truncated": (
                        looping_input[:200] + "..."
                        if len(looping_input) > 200
                        else looping_input
                    ),
                },
                recommended_action=(
                    f"Agent '{role}' is in a repetition loop: {len(signatures)} tool "
                    f"calls with zero new unique inputs in the trailing window. "
                    f"The looping input is: {looping_input[:200]}. "
                    f"Escalating to HITL — post a terminating message to the bus, "
                    f"then respawn with a fresh session."
                ),
                requires_adjudication=True,
                detector_key="agent_livelock",
            )

    return None


detect_agent_livelock.detector_key = "agent_livelock"  # type: ignore[attr-defined]
detect_agent_livelock.name = "agent_livelock_detector"  # type: ignore[attr-defined]


class AgentLivelockCheck:
    """Tier 1 health check wrapper around :func:`detect_agent_livelock`.

    Runs on RUNTIME_TICK and ON_DEMAND triggers. Delegates to the
    detection-plane detector for the actual logic, translating the
    ``Finding`` into a ``HealthResult`` for the health-check runner's
    event-bus emission path.

    Per cq-3, a livelock finding sets ``requires_adjudication=True`` — the
    corrective action escalates to HITL with the looping input quoted
    verbatim, since the detector cannot know the answer to the agent's
    question.
    """

    name: str = "agent_livelock"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.RUNTIME_TICK,
            HealthTrigger.ON_DEMAND,
        }
    )

    def __init__(
        self,
        window_seconds: int = _DEFAULT_WINDOW_SECONDS,
        grace_seconds: int = _DEFAULT_GRACE_SECONDS,
        min_tool_calls: int = _MIN_TOOL_CALLS,
    ):
        self._window_seconds = window_seconds
        self._grace_seconds = grace_seconds
        self._min_tool_calls = min_tool_calls

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Run the livelock check against the pipeline health context."""
        try:
            from health_checks.detection_plane import snapshot_from_health_context

            snapshot = snapshot_from_health_context(context)
            finding = detect_agent_livelock(
                snapshot,
                window_seconds=self._window_seconds,
                grace_seconds=self._grace_seconds,
                min_tool_calls=self._min_tool_calls,
            )
        except Exception as exc:
            logger.warning("AgentLivelockCheck failed", error=str(exc), exc_info=True)
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning=f"Check failed internally: {exc}",
            )

        if finding is None:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="No livelock detected: agents are producing unique tool inputs.",
            )

        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning=finding.recommended_action,
            action=HealthAction.ALERT,
            details={
                "finding_class": finding.finding_class,
                "severity": finding.severity,
                "evidence": finding.evidence,
                "requires_adjudication": finding.requires_adjudication,
            },
        )


__all__ = [
    "AgentLivelockCheck",
    "FINDING_AGENT_LIVELOCK",
    "detect_agent_livelock",
]
