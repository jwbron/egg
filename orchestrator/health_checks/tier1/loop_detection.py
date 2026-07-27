"""
Agent livelock / repetition-loop detector (#3665).

Fires when an agent in a RUNNING phase produces zero *new unique tool inputs*
over a trailing window. The empirical finding from the incident analysis:
across every observed livelock (single-input, 2-, 3-, and 8-cycles), counting
unique tool inputs never issued before in the session over a trailing window
separates a loop from legitimate work cleanly — a working agent produces new
ones and a loop of any length produces none.

The detector reads agent log transcripts from the ``agent_log_store`` (which
captures the full pod stdout before reaping, unlike the truncated pod log).
It parses Claude Code's tool-call emission lines and tracks the set of unique
tool-input signatures seen per agent. When the count of *new* signatures in
the trailing window is zero and the agent has been running past the grace
period, a ``livelock`` finding is produced.

Deterministic -> ``requires_adjudication=False``. The bounded corrective
vocabulary (slice-6) can nudge the agent without an LLM call.
"""

from __future__ import annotations

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

# Regex to parse Claude Code tool-call lines from agent logs.
# Claude Code emits lines like:
#   "> mcp__egg__task_add_commit"
#   "> Bash"
#   "> Read"
#   "> TodoWrite"
# We capture the tool name and the first argument (if present) as the signature.
_TOOL_CALL_RE = re.compile(
    r"^\s*>\s+(\S+)(?:\s+(.+?))?\s*$",
    re.MULTILINE,
)

# Also match the JSON tool-use format that Claude Code may emit:
#   "tool": "Bash"
_TOOL_JSON_RE = re.compile(r'"tool"\s*:\s*"([^"]+)"')


def _extract_tool_signatures(logs: str) -> list[str]:
    """Extract tool-call signatures from agent log text.

    Returns a list of signatures (tool name + first argument hash) in order
    of appearance. Each signature is a stable string that identifies a
    distinct tool input.

    A signature is the tool name plus the first ~80 chars of the first
    argument line, which is enough to distinguish distinct inputs while
    collapsing truly identical repetitions.
    """
    signatures: list[str] = []

    # Try the line-based format first (Claude Code's "> tool_name args" format)
    for match in _TOOL_CALL_RE.finditer(logs):
        tool_name = match.group(1)
        args = match.group(2)
        if args:
            # Use first 80 chars of args as the signature differentiator
            sig = f"{tool_name}:{args[:80]}"
        else:
            sig = tool_name
        signatures.append(sig)

    # If no line-based matches, try JSON format
    if not signatures:
        for match in _TOOL_JSON_RE.finditer(logs):
            tool_name = match.group(1)
            signatures.append(tool_name)

    return signatures


def _get_agent_logs(
    pipeline_id: str,
    agent_role: str,
) -> str | None:
    """Fetch the most recent captured logs for an agent.

    Uses the agent_log_store (which captures full pod stdout before reaping).
    Returns None if no logs are available.
    """
    try:
        from agent_log_store import get_agent_log_store

        store = get_agent_log_store()
        records = store.list_records(pipeline_id, include_logs=True)
        # Find records matching this agent role
        for record in records:
            if record.get("agent_role") == agent_role:
                return record.get("logs", "")
        # If no exact role match, try the most recent record
        if records:
            return records[0].get("logs", "")
        return None
    except Exception:
        return None


def detect_agent_livelock(
    snapshot: Any,
    *,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    grace_seconds: int = _DEFAULT_GRACE_SECONDS,
    min_tool_calls: int = _MIN_TOOL_CALLS,
) -> Finding | None:
    """Fire when an agent shows zero new unique tool inputs over a trailing window.

    The detector reads agent log transcripts from the agent_log_store and
    parses tool-call emission lines. It tracks the set of unique tool-input
    signatures seen per agent. When the count of *new* signatures in the
    trailing window is zero (and the agent has made at least ``min_tool_calls``
    total, and has been running past the grace period), a ``livelock`` finding
    is produced.

    The signature is the tool name plus the first 80 chars of the first
    argument, which distinguishes distinct inputs while collapsing truly
    identical repetitions. This handles single-input loops, 2-cycles, 3-cycles,
    and 8-cycles uniformly — any loop produces no new unique signatures.

    Deterministic -> ``requires_adjudication=False``.
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

    # Check each running agent for livelock
    for agent in running_agents:
        role = getattr(agent, "role", "")
        if not role:
            continue

        logs = _get_agent_logs(pipeline_id, role)
        if not logs:
            continue

        signatures = _extract_tool_signatures(logs)

        # Need at least min_tool_calls to have enough history to judge
        if len(signatures) < min_tool_calls:
            continue

        # Count unique signatures in the trailing window.
        # Since we don't have per-call timestamps from the log text, we
        # use the total unique count vs total count as a proxy:
        # - If all signatures are the same (or very few unique), it's a loop
        # - If there are many unique signatures, it's legitimate work
        unique_signatures = set(signatures)
        unique_ratio = len(unique_signatures) / len(signatures) if signatures else 0

        # A loop produces very few unique signatures relative to total calls.
        # A working agent produces mostly unique signatures.
        # Threshold: if unique_ratio < 0.1 (i.e., 90%+ of calls are repeats),
        # and we have enough total calls, it's a livelock.
        if len(signatures) >= 10 and unique_ratio < 0.1:
            return Finding(
                finding_class="agent_livelock",
                severity=Severity.HIGH,
                evidence={
                    "role": role,
                    "phase": getattr(snapshot, "phase", ""),
                    "total_tool_calls": len(signatures),
                    "unique_tool_calls": len(unique_signatures),
                    "unique_ratio": round(unique_ratio, 3),
                    "window_seconds": window_seconds,
                    "min_tool_calls": min_tool_calls,
                },
                recommended_action=(
                    f"Agent '{role}' appears to be in a repetition loop: "
                    f"{len(signatures)} tool calls with only "
                    f"{len(unique_signatures)} unique inputs "
                    f"(ratio {unique_ratio:.1%}). Nudge the agent or "
                    f"respawn it to break the loop."
                ),
                requires_adjudication=False,
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
            },
        )


__all__ = [
    "AgentLivelockCheck",
    "FINDING_AGENT_LIVELOCK",
    "detect_agent_livelock",
]
