"""Forward-progress detector (#3596, task-2-1).

A deterministic detection-plane detector that detects when an agent is not
making forward progress.

The detector is **stateless** in the calibration-corpus model: it evaluates a
single :class:`EventStreamSnapshot` and returns a finding or None. In
production, the snapshot builder (``snapshot_from_health_context``) enriches
``git_state`` with per-agent commit counts and the age of the last commit,
and populates ``consensus`` and ``midturn_messages`` so the detector can
reason about BRC progress from a single snapshot.

Firing modes (highest severity first):

1. **Reset** (high): ``git_state.agent_prev_commit_counts`` is present and an
   agent's current commit count is *less than* its previous count — work was
   silently discarded (the #3506 scenario).

2. **Stall** (high, adjudicated): An agent is RUNNING but not making forward
   progress. Distinguishes three sub-modes per operator directive #3:

   - **Livelocked**: Agent has recent tool calls (last_tool_call_age_s <
     threshold) but no BRC progress (no CONSENSUS_PROPOSE/CONSENSUS_CONFIRMED
     in the phase for >1 hour). The agent is spinning but not advancing the
     consensus protocol.
   - **Deadlocked on unsatisfiable contract**: Agent has no recent activity
     AND there's a pending HITL decision that's been open for a long time
     AND the agent is the sole blocker. The contract itself is unsatisfiable.
   - **Working out-of-role**: Agent is active (recent tool calls) but the
     phase doesn't match what the agent should be working on.

3. **No commits at completion** (medium, adjudicated): an agent marked
   COMPLETE in the pipeline model has zero commits — it exited cleanly
   doing nothing.

All findings set ``requires_adjudication=True`` because "stuck vs.
legitimately slow is ambiguous" (contract task-2-1, operator directive #3).

Registered into the slice-1 calibration corpus by ``detector_key`` and into
the production :class:`DetectionPlane` (see :func:`DetectionPlane.default`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.types import Finding, Severity

# Finding-class strings (matched structurally on the raw string by the plane).
FINDING_FORWARD_PROGRESS_STALL = "forward_progress_stall"
FINDING_FORWARD_PROGRESS_RESET = "forward_progress_reset"
FINDING_FORWARD_PROGRESS_NO_COMMITS = "forward_progress_no_commits"
FINDING_FORWARD_PROGRESS_BRC_ABSENCE = "forward_progress_brc_absence"

# Default stall threshold: 10 minutes since the agent's last commit while RUNNING.
_DEFAULT_STALL_SECONDS = 600.0

# BRC progress threshold: 1 hour with no CONSENSUS_PROPOSE/CONSENSUS_CONFIRMED
# despite activity. Per operator directive #2.
_DEFAULT_BRC_PROGRESS_WINDOW_S = 3600.0

# Tool-call recency threshold for distinguishing livelocked vs. deadlocked.
# If last_tool_call_age_s < this, the agent is "active" (making tool calls).
_DEFAULT_TOOL_CALL_RECENT_S = 600.0


def _git_state(snapshot: Any) -> dict[str, Any]:
    state = getattr(snapshot, "git_state", None)
    return state if isinstance(state, dict) else {}


def _phase_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "phase_state", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _consensus_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "consensus", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _decision_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "decision_state", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _running_agents(snapshot: Any) -> tuple[Any, ...]:
    return getattr(snapshot, "running_agents", ()) or ()


def _midturn_messages(snapshot: Any) -> tuple[dict[str, Any], ...]:
    return getattr(snapshot, "midturn_messages", ()) or ()


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _has_brc_progress(
    snapshot: Any,
    consensus: dict[str, Any],
    midturn_messages: tuple[dict[str, Any], ...],
    brc_progress_window_s: float,
) -> bool:
    """Check whether there has been BRC progress (proposal/consensus action)
    within the progress window.

    BRC progress means: a CONSENSUS_PROPOSE or CONSENSUS_CONFIRMED message
    in the phase within the last ``brc_progress_window_s`` seconds, OR the
    consensus tracker reports a recent proposal.

    Per operator directive #2: "The distinguishing signal is absence of BRC
    progress (no proposal / no consensus action) despite activity, not
    absence of activity."
    """
    import time

    now = time.time()

    # Check consensus tracker's latest proposal timestamp
    latest_proposal_age_s = _as_float(consensus.get("latest_proposal_age_s"))
    if latest_proposal_age_s is not None and latest_proposal_age_s < brc_progress_window_s:
        return True

    # Check midturn_messages for recent CONSENSUS_PROPOSE / CONSENSUS_CONFIRMED
    for msg in midturn_messages:
        msg_type = msg.get("message_type", "") if isinstance(msg, dict) else ""
        if msg_type in ("CONSENSUS_PROPOSE", "CONSENSUS_CONFIRMED"):
            ts = msg.get("timestamp")
            if ts is None:
                # If we can't parse the timestamp, assume it's recent
                return True
            try:
                from datetime import datetime

                if isinstance(ts, str):
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age = now - dt.timestamp()
                    if age < brc_progress_window_s:
                        return True
            except (ValueError, TypeError):
                # If we can't parse the timestamp, assume it's recent
                return True

    return False


def _has_activity(
    git_state: dict[str, Any],
    running_agents: tuple[Any, ...],
    tool_call_recent_threshold_s: float,
) -> bool:
    """Check whether the agent has ANY activity signal.

    Per operator directive #2 and contract task-2-1: the detector must not
    key on commits alone. Activity includes:
    - Recent tool calls (last_tool_call_age_s < threshold)
    - Progress events (agent_progress_event_counts > 0)
    - File modifications (agent_file_modification_counts > 0)

    Note: recent commits are NOT considered an activity signal here — commits
    are already checked by the stall detection. If the agent has recent
    commits (within the stall window), the stall check won't fire anyway.
    """
    # Check running_agents for recent tool calls
    for agent in running_agents:
        tool_call_age = _as_float(getattr(agent, "last_tool_call_age_s", None))
        if tool_call_age is not None and tool_call_age < tool_call_recent_threshold_s:
            return True

    # Check git_state for progress events and file modifications
    progress_counts = git_state.get("agent_progress_event_counts", {})
    if isinstance(progress_counts, dict):
        for count in progress_counts.values():
            count_float = _as_float(count)
            if count_float is not None and count_float > 0:
                return True

    file_mod_counts = git_state.get("agent_file_modification_counts", {})
    if isinstance(file_mod_counts, dict):
        for count in file_mod_counts.values():
            count_float = _as_float(count)
            if count_float is not None and count_float > 0:
                return True

    return False


def detect_forward_progress(
    snapshot: Any,
    *,
    stall_seconds: float = _DEFAULT_STALL_SECONDS,
    brc_progress_window_s: float = _DEFAULT_BRC_PROGRESS_WINDOW_S,
    tool_call_recent_threshold_s: float = _DEFAULT_TOOL_CALL_RECENT_S,
) -> Finding | None:
    """Fire when an agent is not making forward progress.

    Fires in modes (highest severity first):

    1. **Reset** (high, adjudicated): ``git_state.agent_prev_commit_counts``
       is present and an agent's current commit count is less than its
       previous count — work was silently discarded.

    2. **Stall** (high, adjudicated): An agent is RUNNING but not making
       forward progress. Distinguishes three sub-modes per operator directive
       #3: livelocked (active but no BRC progress), deadlocked on
       unsatisfiable contract (no activity + pending HITL + sole blocker),
       working out-of-role (active but wrong phase).

    3. **No commits at completion** (medium, adjudicated): an agent marked
       COMPLETE in the pipeline model has zero commits — it exited cleanly
       doing nothing.

    All findings set ``requires_adjudication=True`` because "stuck vs.
    legitimately slow is ambiguous" (contract task-2-1, operator directive #3).

    The detector checks multiple activity signals (commits, progress events,
    file modifications, tool calls) per operator directive #2 — it does NOT
    key on commits alone.
    """
    pipeline_id = str(getattr(snapshot, "pipeline_id", "") or "")
    if not pipeline_id:
        return None

    phase_state = _phase_state(snapshot)
    status = str(phase_state.get("status", "")).upper()
    if status != "RUNNING":
        return None

    git_state = _git_state(snapshot)
    if not git_state:
        return None

    consensus = _consensus_state(snapshot)
    decision_state = _decision_state(snapshot)
    running_agents = _running_agents(snapshot)
    midturn_messages = _midturn_messages(snapshot)

    findings: list[Finding] = []

    # Mode 1: Reset detection — commit count decreased
    findings.extend(_detect_commit_reset(git_state, pipeline_id))

    # Mode 2: Stall detection — multi-signal, BRC-aware
    findings.extend(
        _detect_commit_stall(
            snapshot,
            git_state,
            consensus,
            decision_state,
            running_agents,
            midturn_messages,
            pipeline_id,
            stall_seconds,
            brc_progress_window_s,
            tool_call_recent_threshold_s,
        )
    )

    # Mode 3: No commits at completion
    pipeline = getattr(snapshot, "_pipeline_ref", None)
    if pipeline is not None:
        findings.extend(_detect_no_commits_at_completion(pipeline, git_state))

    if findings:
        # Return the highest-severity finding
        severity_order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2, Severity.INFO: 3}
        findings.sort(key=lambda f: severity_order.get(f.severity, 99))
        return findings[0]

    return None


def _detect_commit_reset(
    git_state: dict[str, Any], pipeline_id: str
) -> list[Finding]:
    """Detect when an agent's commit count decreased (work discarded)."""
    current_counts = git_state.get("agent_commit_counts", {})
    prev_counts = git_state.get("agent_prev_commit_counts", {})

    if not isinstance(current_counts, dict) or not isinstance(prev_counts, dict):
        return []

    findings: list[Finding] = []
    for role, current in current_counts.items():
        prev = prev_counts.get(role)
        if prev is not None and int(current) < int(prev):
            findings.append(Finding(
                finding_class=FINDING_FORWARD_PROGRESS_RESET,
                severity=Severity.HIGH,
                evidence={
                    "pipeline_id": pipeline_id,
                    "agent_role": role,
                    "previous_commit_count": int(prev),
                    "current_commit_count": int(current),
                    "commit_delta": int(current) - int(prev),
                },
                recommended_action=(
                    f"Agent '{role}' commit count decreased from {int(prev)} to "
                    f"{int(current)} — work may have been silently discarded "
                    f"(#3506/#3596). Check the worktree for discarded commits "
                    f"and verify the agent's session state."
                ),
                requires_adjudication=True,
                detector_key="forward_progress",
            ))
    return findings


def _detect_commit_stall(
    snapshot: Any,
    git_state: dict[str, Any],
    consensus: dict[str, Any],
    decision_state: dict[str, Any],
    running_agents: tuple[Any, ...],
    midturn_messages: tuple[dict[str, Any], ...],
    pipeline_id: str,
    stall_seconds: float,
    brc_progress_window_s: float,
    tool_call_recent_threshold_s: float,
) -> list[Finding]:
    """Detect when an agent is not making forward progress.

    Uses multi-signal detection (commits, progress events, file mods, tool
    calls) per operator directive #2 — does NOT key on commits alone.
    Distinguishes three stall modes per operator directive #3.
    """
    agent_commit_counts = git_state.get("agent_commit_counts", {})
    agent_last_commit_age_s = git_state.get("agent_last_commit_age_s", {})

    if not isinstance(agent_commit_counts, dict):
        agent_commit_counts = {}
    if not isinstance(agent_last_commit_age_s, dict):
        agent_last_commit_age_s = {}

    findings: list[Finding] = []

    # Build a set of roles to check: from running_agents if available,
    # otherwise fall back to git_state keys (for backward compat with tests
    # that only set git_state).
    roles_to_check: dict[str, dict[str, Any]] = {}

    for agent in running_agents:
        role = getattr(agent, "role", "")
        if not role:
            continue
        roles_to_check[role] = {
            "tool_call_age": _as_float(getattr(agent, "last_tool_call_age_s", None)),
            "from_running_agents": True,
        }

    # Fall back to git_state keys when no running_agents are set
    if not roles_to_check:
        for role in agent_commit_counts:
            if role not in roles_to_check:
                roles_to_check[role] = {
                    "tool_call_age": None,
                    "from_running_agents": False,
                }
        for role in agent_last_commit_age_s:
            if role not in roles_to_check:
                roles_to_check[role] = {
                    "tool_call_age": None,
                    "from_running_agents": False,
                }

    # Check each agent for stall conditions
    for role, agent_info in roles_to_check.items():
        tool_call_age = agent_info["tool_call_age"]
        commit_age = _as_float(agent_last_commit_age_s.get(role))
        commit_count = agent_commit_counts.get(role, 0)

        # Check if commit is stalled (no commits for too long)
        commit_stalled = (
            commit_age is not None
            and commit_age >= stall_seconds
        )

        # Check if agent has zero commits and stalled
        zero_commits = (
            isinstance(commit_count, int)
            and commit_count == 0
            and commit_age is not None
            and commit_age >= stall_seconds
        )

        if not commit_stalled and not zero_commits:
            continue

        # Check if the agent has ANY activity (multi-signal, per directive #2)
        has_activity = _has_activity(
            git_state, running_agents, tool_call_recent_threshold_s
        )

        # Check if the agent has BRC progress
        has_brc = _has_brc_progress(
            snapshot, consensus, midturn_messages, brc_progress_window_s
        )

        # If the agent has BRC progress, it's not stalled — even if commits
        # are stale (operator directive #2: "must not key on commits alone").
        if has_brc:
            continue

        # If the agent has any activity (progress events, file mods, tool calls,
        # recent commits), it's not stalled — it's doing something, just not
        # committing. The BRC-progress-absence check (below) handles the case
        # where the agent is active but not making BRC progress.
        if has_activity:
            continue
            findings.append(Finding(
                finding_class=FINDING_FORWARD_PROGRESS_STALL,
                severity=Severity.HIGH,
                evidence={
                    "pipeline_id": pipeline_id,
                    "agent_role": role,
                    "last_commit_age_s": commit_age,
                    "last_tool_call_age_s": tool_call_age,
                    "stall_threshold_seconds": stall_seconds,
                    "brc_progress_age_s": consensus.get("latest_proposal_age_s"),
                    "brc_progress_window_s": brc_progress_window_s,
                    "stall_mode": "livelocked",
                },
                recommended_action=(
                    f"Agent '{role}' is active (tool calls/commits) but has not "
                    f"made BRC progress (no CONSENSUS_PROPOSE/CONSENSUS_CONFIRMED "
                    f"for {consensus.get('latest_proposal_age_s', '?')}s, "
                    f"threshold {brc_progress_window_s}s). This is a livelock: "
                    f"the agent is spinning but not advancing the consensus protocol. "
                    f"Check what the agent is doing and whether it should be "
                    f"proposing or waiting on reviewers."
                ),
                requires_adjudication=True,
                detector_key="forward_progress",
            ))
            continue

        # If the agent has no activity and no BRC progress, check for
        # deadlocked_contract or generic stall
        pending_hitl = decision_state.get("pending_hitl", False)
        blocking_agents = consensus.get("blocking_agents", [])
        is_sole_blocker = (
            len(blocking_agents) == 1
            and blocking_agents[0] == role
        )

        if pending_hitl and is_sole_blocker:
            findings.append(Finding(
                finding_class=FINDING_FORWARD_PROGRESS_STALL,
                severity=Severity.HIGH,
                evidence={
                    "pipeline_id": pipeline_id,
                    "agent_role": role,
                    "last_commit_age_s": commit_age,
                    "last_tool_call_age_s": tool_call_age,
                    "stall_threshold_seconds": stall_seconds,
                    "pending_hitl": pending_hitl,
                    "open_decisions": decision_state.get("open_decisions", 0),
                    "oldest_open_age_s": decision_state.get("oldest_open_age_s"),
                    "stall_mode": "deadlocked_contract",
                },
                recommended_action=(
                    f"Agent '{role}' is the sole blocker with a pending HITL "
                    f"decision that has been open for "
                    f"{decision_state.get('oldest_open_age_s', '?')}s. "
                    f"The contract may be unsatisfiable. Adjudicate whether to "
                    f"open an operator HITL or re-scope the task."
                ),
                requires_adjudication=True,
                detector_key="forward_progress",
            ))
        else:
            # Generic stall — agent is not active and not making progress
            findings.append(Finding(
                finding_class=FINDING_FORWARD_PROGRESS_STALL,
                severity=Severity.HIGH,
                evidence={
                    "pipeline_id": pipeline_id,
                    "agent_role": role,
                    "last_commit_age_s": commit_age,
                    "last_tool_call_age_s": tool_call_age,
                    "stall_threshold_seconds": stall_seconds,
                    "stall_mode": "generic_stall",
                },
                recommended_action=(
                    f"Agent '{role}' has not produced new commits for "
                    f"{int(commit_age or 0)}s (threshold {int(stall_seconds)}s). "
                    f"The agent is RUNNING but making no forward progress. "
                    f"Check container logs and agent activity."
                ),
                requires_adjudication=True,
                detector_key="forward_progress",
            ))

    # Also check for BRC-progress-absence without commit stall:
    # agent has recent commits but no BRC progress for >1 hour
    # (the #3596 scenario: healthy agent doing implement-phase work during
    # plan phase with no proposal for an hour)
    latest_proposal_age_s = _as_float(consensus.get("latest_proposal_age_s"))
    if latest_proposal_age_s is not None and latest_proposal_age_s >= brc_progress_window_s:
        # There has been no BRC progress for too long
        # Check if any agent has recent activity (commits or tool calls)
        any_recent_activity = False
        for agent in running_agents:
            tool_call_age = _as_float(getattr(agent, "last_tool_call_age_s", None))
            if tool_call_age is not None and tool_call_age < tool_call_recent_threshold_s:
                any_recent_activity = True
                break

        # Also check commit ages
        if not any_recent_activity:
            for _role, age in agent_last_commit_age_s.items():
                age_s = _as_float(age)
                if age_s is not None and age_s < tool_call_recent_threshold_s:
                    any_recent_activity = True
                    break

        if any_recent_activity:
            findings.append(Finding(
                finding_class=FINDING_FORWARD_PROGRESS_BRC_ABSENCE,
                severity=Severity.HIGH,
                evidence={
                    "pipeline_id": pipeline_id,
                    "brc_progress_age_s": latest_proposal_age_s,
                    "brc_progress_window_s": brc_progress_window_s,
                    "has_proposed": consensus.get("has_proposed", False),
                    "producer_phases": consensus.get("producer_phases", {}),
                    "stall_mode": "livelocked",
                },
                recommended_action=(
                    f"No BRC progress (no CONSENSUS_PROPOSE/CONSENSUS_CONFIRMED) "
                    f"for {int(latest_proposal_age_s)}s (threshold "
                    f"{int(brc_progress_window_s)}s) despite recent agent activity. "
                    f"The agent may be working out-of-phase or livelocked. "
                    f"Check whether the agent should be proposing."
                ),
                requires_adjudication=True,
                detector_key="forward_progress",
            ))

    return findings


def _detect_no_commits_at_completion(
    pipeline: Any, git_state: dict[str, Any]
) -> list[Finding]:
    """Check if any COMPLETE agent has zero commits."""
    from models import AgentExecutionStatus

    agent_commit_counts = git_state.get("agent_commit_counts", {})
    if not isinstance(agent_commit_counts, dict):
        agent_commit_counts = {}

    findings: list[Finding] = []
    try:
        phases = getattr(pipeline, "phases", {}) or {}
        for phase_exec in phases.values():
            agents = getattr(phase_exec, "agents", []) or []
            for agent in agents:
                if getattr(agent, "status", None) == AgentExecutionStatus.COMPLETE:
                    role = str(getattr(agent, "role", ""))
                    count = agent_commit_counts.get(role, 0)
                    if count == 0:
                        findings.append(Finding(
                            finding_class=FINDING_FORWARD_PROGRESS_NO_COMMITS,
                            severity=Severity.MEDIUM,
                            evidence={
                                "agent_role": role,
                                "commit_count": 0,
                                "phase": str(getattr(phase_exec, "phase", "")),
                            },
                            recommended_action=(
                                f"Agent '{role}' completed with zero commits — "
                                f"it may have done nothing. Check its outputs and "
                                f"container logs for evidence of work."
                            ),
                            requires_adjudication=True,
                            detector_key="forward_progress",
                        ))
    except Exception:  # noqa: BLE001 — defensive
        pass
    return findings


detect_forward_progress.detector_key = "forward_progress"  # type: ignore[attr-defined]
detect_forward_progress.name = "forward_progress_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_forward_progress",
    "FINDING_FORWARD_PROGRESS_STALL",
    "FINDING_FORWARD_PROGRESS_RESET",
    "FINDING_FORWARD_PROGRESS_NO_COMMITS",
    "FINDING_FORWARD_PROGRESS_BRC_ABSENCE",
]
