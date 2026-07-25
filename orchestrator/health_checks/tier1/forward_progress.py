"""Forward-progress detector (#3596).

A deterministic detection-plane detector that detects when an agent is not
making forward progress.

The detector is **stateless** in the calibration-corpus model: it evaluates a
single :class:`EventStreamSnapshot` and returns a finding or None. In
production, the snapshot builder (``snapshot_from_health_context``) enriches
``git_state`` with per-agent commit counts and the age of the last commit,
so the detector can reason about progress from a single snapshot.

Three firing modes:

1. **Stall** (medium): ``git_state.agent_last_commit_age_s`` exceeds
   ``forward_progress_stall_seconds`` (default 600s) for an agent in the
   running-agent set while the phase is RUNNING — the agent is alive but
   not producing commits.

2. **Reset** (high): ``git_state.agent_prev_commit_counts`` is present and an
   agent's current commit count is *less than* its previous count — work was
   silently discarded (the #3506 scenario).

3. **No commits at completion** (medium): an agent marked COMPLETE in the
   pipeline model has zero commits — it exited cleanly doing nothing.

Deterministic → ``requires_adjudication=False``. Routine findings are handled
by the bounded corrective vocabulary (slice-6) without an LLM.

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

# Default stall threshold: 10 minutes since the agent's last commit while RUNNING.
_DEFAULT_STALL_SECONDS = 600.0


def _git_state(snapshot: Any) -> dict[str, Any]:
    state = getattr(snapshot, "git_state", None)
    return state if isinstance(state, dict) else {}


def _phase_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "phase_state", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_forward_progress(
    snapshot: Any,
    *,
    stall_seconds: float = _DEFAULT_STALL_SECONDS,
) -> Finding | None:
    """Fire when an agent is not making forward progress.

    Fires in three modes (highest severity first):

    1. **Reset** (high): ``git_state.agent_prev_commit_counts`` is present and
       an agent's current commit count is less than its previous count — work
       was silently discarded.

    2. **Stall** (medium): ``git_state.agent_last_commit_age_s`` is present and
       exceeds ``stall_seconds`` for an agent in the running-agent set while
       the phase is RUNNING — the agent is alive but not producing commits.

    3. **No commits at completion** (medium): an agent marked COMPLETE in the
       pipeline model has zero commits — it exited cleanly doing nothing.

    All three modes read from the snapshot itself (stateless). The snapshot
    builder populates the git_state fields from git subprocess calls and the
    progress store.

    Deterministic → ``requires_adjudication=False``.
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

    findings: list[Finding] = []

    # Mode 1: Reset detection — commit count decreased
    findings.extend(_detect_commit_reset(git_state, pipeline_id))

    # Mode 2: Stall detection — no commits for too long
    findings.extend(_detect_commit_stall(git_state, pipeline_id, stall_seconds))

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
                requires_adjudication=False,
                detector_key="forward_progress",
            ))
    return findings


def _detect_commit_stall(
    git_state: dict[str, Any], pipeline_id: str, stall_seconds: float
) -> list[Finding]:
    """Detect when an agent has not committed for too long."""
    last_commit_ages = git_state.get("agent_last_commit_age_s", {})
    if not isinstance(last_commit_ages, dict):
        return []

    findings: list[Finding] = []
    for role, age in last_commit_ages.items():
        age_s = _as_float(age)
        if age_s is None:
            continue
        if age_s >= stall_seconds:
            findings.append(Finding(
                finding_class=FINDING_FORWARD_PROGRESS_STALL,
                severity=Severity.MEDIUM,
                evidence={
                    "pipeline_id": pipeline_id,
                    "agent_role": role,
                    "last_commit_age_s": age_s,
                    "stall_threshold_seconds": stall_seconds,
                },
                recommended_action=(
                    f"Agent '{role}' has not produced new commits for "
                    f"{int(age_s)}s (threshold {int(stall_seconds)}s). "
                    f"The agent is RUNNING but making no forward progress. "
                    f"Check container logs and agent activity."
                ),
                requires_adjudication=False,
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
                            requires_adjudication=False,
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
]
