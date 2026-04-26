#!/usr/bin/env python3
"""Pre-built overseer monitoring script.

Queries the orchestrator API for pipeline status, health alerts, progress
events, and escalation messages.  Outputs structured JSON lines to stdout
so the overseer agent can read and act on anomalies without writing its own
monitoring loop.

Two modes of operation:

  --once   Run a single poll cycle and exit.  Exit code 0 means the pipeline
           is healthy (running or complete); exit code 1 means failed,
           cancelled, or unknown.  This is the **recommended mode** — the
           overseer agent calls it repeatedly, processing each cycle's
           output between calls.

  (default) Run a continuous poll loop until the pipeline reaches a terminal
            state or SIGTERM is received.  Preserved for backward compatibility.

Usage:
    python3 /opt/egg-runtime/sandbox/overseer_monitor.py --once
    python3 /opt/egg-runtime/sandbox/overseer_monitor.py

Environment:
    EGG_PIPELINE_ID          Required — pipeline to monitor.
    EGG_AGENT_ROLE           Agent role (default: "overseer").
    EGG_ORCHESTRATOR_URL     Orchestrator base URL.
    EGG_OVERSEER_POLL_INTERVAL  Poll interval in seconds (default: 30, continuous mode only).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import sys
import time
from typing import Any
from urllib.parse import urlencode

from egg_lib.orch_cli import (
    api_request,
    get_orchestrator_url,
    get_pipeline_id_from_env,
)

TERMINAL_STATES = frozenset({"complete", "failed", "cancelled"})


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _orch_get(base_url: str, endpoint: str, timeout: int = 10) -> dict[str, Any]:
    """GET request to the orchestrator, returning {} on failure."""
    try:
        return api_request(base_url, endpoint, timeout=timeout)
    except Exception:
        return {}


def _orch_post(
    base_url: str, endpoint: str, data: dict[str, Any], timeout: int = 10
) -> dict[str, Any]:
    """POST request to the orchestrator, returning {} on failure."""
    try:
        return api_request(base_url, endpoint, method="POST", data=data, timeout=timeout)
    except Exception:
        return {}


def query_pipeline_status(base_url: str, pipeline_id: str) -> dict[str, Any]:
    """Query full pipeline status including consensus info."""
    result = _orch_get(base_url, f"/api/v1/pipelines/{pipeline_id}/status")
    return result.get("data", result) if result else {}


def query_health_alerts(base_url: str, pipeline_id: str) -> list[dict[str, Any]]:
    """Query active health alerts."""
    result = _orch_get(base_url, f"/api/v1/pipelines/{pipeline_id}/health/alerts")
    alerts = result.get("alerts", [])
    return alerts if isinstance(alerts, list) else []


def query_progress(base_url: str, pipeline_id: str) -> list[dict[str, Any]]:
    """Query progress events from all agents."""
    result = _orch_get(base_url, f"/api/v1/pipelines/{pipeline_id}/progress")
    events = result.get("data", {}).get("events", [])
    return events if isinstance(events, list) else []


def poll_messages(
    base_url: str, pipeline_id: str, role: str, wait: int = 5
) -> list[dict[str, Any]]:
    """Poll for escalation messages directed to the overseer."""
    params = {"role": role, "wait": str(wait)}
    endpoint = f"/api/v1/pipelines/{pipeline_id}/messages?{urlencode(params)}"
    timeout = wait + 10
    result = _orch_get(base_url, endpoint, timeout=timeout)
    messages = result.get("data", {}).get("messages", [])
    return messages if isinstance(messages, list) else []


def send_heartbeat(base_url: str, pipeline_id: str, role: str) -> bool:
    """Send a heartbeat signal."""
    data = {"signal_type": "heartbeat", "agent_role": role}
    result = _orch_post(base_url, f"/api/v1/pipelines/{pipeline_id}/signal", data)
    return bool(result.get("success"))


def send_message(
    base_url: str,
    pipeline_id: str,
    from_role: str,
    to_role: str,
    subject: str,
    body: str,
    message_type: str = "OVERSEER_ALERT",
) -> bool:
    """Send an inter-agent message.

    Not called by run_monitor() directly — provided as a library utility
    for the overseer agent to import when it needs to send alerts.
    """
    data = {
        "from_role": from_role,
        "to_role": to_role,
        "message_type": message_type,
        "subject": subject,
        "body": body,
    }
    result = _orch_post(base_url, f"/api/v1/pipelines/{pipeline_id}/messages", data)
    return bool(result.get("success"))


def emit_cycle_report(report: dict[str, Any]) -> None:
    """Write a JSON-line cycle report to stdout."""
    print(json.dumps(report, default=str), flush=True)


# ---------------------------------------------------------------------------
# Migrated detectors (issue #1962, TASK-6-1)
# ---------------------------------------------------------------------------

# Default agent-timing state file path. Overridable for tests via the
# AGENT_TIMING_PATH env var.
_AGENT_TIMING_PATH_DEFAULT = ".egg-state/oversight/agent-timing.json"

# Per-anomaly suppression window multiplier — a detector skips emitting
# if the same (role, anomaly) fired within ``2 * threshold`` seconds.
_SUPPRESSION_FACTOR = 2


def _agent_timing_path() -> str:
    return os.environ.get("AGENT_TIMING_PATH", _AGENT_TIMING_PATH_DEFAULT)


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_overseer_owns_host(config: dict[str, Any]) -> bool:
    return bool(config.get("overseer_owns_host_detection", False))


def _suppress(
    entry: Any,  # AgentTimingEntry from egg_overseer.state
    anomaly: str,
    threshold_seconds: int,
    now: datetime.datetime,
) -> bool:
    """Return True if the same anomaly fired recently for this role+phase."""
    last = entry.alerted_anomalies.get(anomaly)
    if last is None:
        return False
    delta = (now - last).total_seconds()
    return bool(delta < (_SUPPRESSION_FACTOR * threshold_seconds))


def run_migrated_detectors(
    *,
    base_url: str,
    pipeline_id: str,
    phase_name: str,
    config_subset: dict[str, Any],
    progress_events: list[dict[str, Any]],
    consensus: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run the four migrated detectors for the current cycle.

    Detectors honour the ``overseer_owns_host_detection`` flag — when
    False (calibration-window default), they short-circuit so /sdlc's
    host detectors stay the active source. When True, the overseer is
    the sole source of these alerts.

    Args:
        base_url: Orchestrator base URL.
        pipeline_id: Pipeline id (also used for the agent-timing state file).
        phase_name: Current phase name.
        config_subset: ``PipelineConfig`` block from the
            pipelines-status payload.
        progress_events: Recent progress events (used to update
            ``has_any_messages`` / ``last_seen``).
        consensus: Current consensus dict.

    Returns:
        List of alert dicts the agent should consider emitting via
        ``egg-orch overseer alert``. Each dict carries the keys
        ``anomaly``, ``priority``, ``summary``, ``detail``,
        ``role`` and a ``calibration_only`` boolean. During the
        calibration window (``overseer_owns_host_detection=False``,
        the default) the detectors run but emit alerts marked
        ``calibration_only=true`` so the host (/sdlc) and downstream
        consumers know to treat them as observational rather than
        authoritative — the host detectors keep firing as the
        authoritative source. When the flag is True the alerts are
        ``calibration_only=false`` and the host detectors are
        expected to be silent (the cleanup PR follow-up makes that
        assumption real by deleting the dormant /sdlc code blocks).

    The reviewer_contract NACK on the original "host-only-or-overseer-
    only" implementation correctly pointed out that the plan calls for
    *side-by-side* calibration (`feedback-1.Q6` lists "net reduction in
    /sdlc HITL prompts per pipeline" as a success signal that requires
    comparable data from both sides). This function therefore runs the
    detectors unconditionally and uses the flag only to mark whether
    the alerts are calibration-only (observational) or authoritative.
    """
    is_authoritative = _is_overseer_owns_host(config_subset)
    calibration_only = not is_authoritative

    try:
        from egg_overseer.state import (
            AgentTimingEntry,
            load_agent_timing,
            save_agent_timing,
        )
    except ImportError as exc:
        # Production: a missing egg_overseer package is a packaging
        # bug — the sandbox image build skipped copying
        # shared/egg_overseer/ into the runtime. Silently returning []
        # would mask the problem (zero overseer alerts forever, no
        # operator-visible signal); fail loud instead.
        # Only swallow when EGG_OVERSEER_TEST_MODE=1 (lightweight
        # unit tests that mock the cycle).
        if os.environ.get("EGG_OVERSEER_TEST_MODE") == "1":
            return []
        # Emit a structured stderr line operators can grep for, then
        # re-raise so the cycle visibly fails (the monitor wrapper
        # logs it).
        print(
            json.dumps(
                {
                    "_overseer_error": "egg_overseer_packaging_missing",
                    "error": str(exc),
                    "fix": (
                        "Sandbox image build skipped shared/egg_overseer/. "
                        "Rebuild with the package included."
                    ),
                }
            ),
            file=sys.stderr,
        )
        raise

    timing_path = _agent_timing_path()
    state = load_agent_timing(timing_path, pipeline_id=pipeline_id)
    now = datetime.datetime.now(datetime.UTC)

    # Update has_any_messages from the progress feed. Each progress event
    # carries a `role` field. We treat any progress event as evidence
    # that the agent is alive.
    seen_roles: set[str] = set()
    for ev in progress_events:
        role_name = ev.get("role") or ev.get("agent_role")
        if role_name:
            seen_roles.add(role_name)
    for role_name in seen_roles:
        entry = state.entries.get(role_name)
        if entry is None:
            entry = AgentTimingEntry(
                role=role_name,
                phase=phase_name,
                phase_entered_at=now,
                first_seen_at=now,
                has_any_messages=True,
            )
            state.entries[role_name] = entry
        elif entry.phase != phase_name:
            # Phase transition: reset the per-phase anchors so the
            # stall detector compares against the time the role
            # entered THIS phase, not a stale anchor from the prior
            # one. Also clear alerted_anomalies so suppression
            # bookkeeping starts fresh in the new phase.
            entry.phase = phase_name
            entry.phase_entered_at = now
            entry.alerted_anomalies = {}
        entry.has_any_messages = True

    stall_threshold = _config_int(config_subset, "overseer_agent_stall_seconds", 180)
    silent_threshold = _config_int(config_subset, "overseer_silent_agent_threshold_seconds", 600)
    nack_threshold = _config_int(config_subset, "overseer_nack_unresolved_seconds", 180)
    long_run_threshold = _config_int(config_subset, "overseer_long_running_phase_seconds", 3600)

    alerts: list[dict[str, Any]] = []

    for role_name, entry in list(state.entries.items()):
        # Skip entries left over from a prior phase. The phase-transition
        # reset above handles roles that emit a progress event in the
        # new phase, but a role that has been silent across the
        # transition would otherwise fire an immediate agent-stall on
        # every cycle (its phase_entered_at would still anchor the
        # earlier phase).
        if entry.phase != phase_name:
            continue
        # detect_agent_stall — phase_entered_at older than threshold.
        elapsed = (now - entry.phase_entered_at).total_seconds()
        if elapsed > stall_threshold and not _suppress(entry, "agent-stall", stall_threshold, now):
            alerts.append(
                {
                    "anomaly": "agent-stall",
                    "priority": "medium",
                    "role": role_name,
                    "summary": (
                        f"agent {role_name} has been in {phase_name} for "
                        f"{int(elapsed)}s without progress"
                    ),
                    "detail": (
                        f"phase_entered_at={entry.phase_entered_at.isoformat()}; "
                        f"recommended next step: check agent logs via "
                        f"`egg-checkpoint show`."
                    ),
                    "calibration_only": calibration_only,
                }
            )
            entry.alerted_anomalies["agent-stall"] = now

        # detect_agent_silent — first_seen_at old AND no messages.
        if (
            not entry.has_any_messages
            and (now - entry.first_seen_at).total_seconds() > silent_threshold
            and not _suppress(entry, "agent-silent", silent_threshold, now)
        ):
            alerts.append(
                {
                    "anomaly": "agent-silent",
                    "priority": "medium",
                    "role": role_name,
                    "summary": (
                        f"agent {role_name} has produced no messages for "
                        f"{int((now - entry.first_seen_at).total_seconds())}s"
                    ),
                    "detail": (
                        f"first_seen_at={entry.first_seen_at.isoformat()}; "
                        f"silent threshold {silent_threshold}s exceeded."
                    ),
                    "calibration_only": calibration_only,
                }
            )
            entry.alerted_anomalies["agent-silent"] = now

    # detect_nack_unresolved — consensus state with an outstanding NACK.
    nacks = consensus.get("nacks") or consensus.get("blocking_nacks") or []
    for nack in nacks if isinstance(nacks, list) else []:
        if not isinstance(nack, dict):
            continue
        nack_role = nack.get("from_role") or nack.get("reviewer") or "unknown"
        ts = nack.get("timestamp") or nack.get("ts")
        try:
            nack_dt = datetime.datetime.fromisoformat(ts) if ts else None
        except (TypeError, ValueError):
            nack_dt = None
        if nack_dt is None:
            continue
        elapsed_n = (now - nack_dt).total_seconds()
        if elapsed_n > nack_threshold:
            entry = state.entries.setdefault(
                nack_role,
                AgentTimingEntry(
                    role=nack_role,
                    phase=phase_name,
                    phase_entered_at=now,
                    first_seen_at=now,
                ),
            )
            if _suppress(entry, "agent-nack-unresolved", nack_threshold, now):
                continue
            alerts.append(
                {
                    "anomaly": "agent-nack-unresolved",
                    "priority": "high",
                    "role": nack_role,
                    "summary": (f"NACK from {nack_role} unresolved for {int(elapsed_n)}s"),
                    "detail": (
                        f"NACK timestamp {ts}; threshold {nack_threshold}s. "
                        f"Producer should re-propose or the human should rule."
                    ),
                    "calibration_only": calibration_only,
                }
            )
            entry.alerted_anomalies["agent-nack-unresolved"] = now

    # detect_phase_long_running — implement phase elapsed beyond long_run.
    if phase_name == "implement":
        # Find the producer entry's phase_entered_at as a proxy for
        # phase start. Filter to entries belonging to the current
        # phase so an entry left over from a prior phase doesn't
        # make the current phase appear "long-running" within
        # milliseconds of starting (reviewer_code blocker: state.entries
        # is not cleared on phase transition).
        starts = [
            e.phase_entered_at
            for k, e in state.entries.items()
            if e.phase == phase_name and not k.startswith("_")
        ]
        if starts:
            phase_started = min(starts)
            elapsed_p = (now - phase_started).total_seconds()
            if elapsed_p > long_run_threshold:
                # Use a synthetic 'phase' role for suppression bookkeeping.
                synth = state.entries.setdefault(
                    "_phase_long_running",
                    AgentTimingEntry(
                        role="_phase_long_running",
                        phase=phase_name,
                        phase_entered_at=phase_started,
                        first_seen_at=phase_started,
                    ),
                )
                if not _suppress(synth, "phase-long-running", long_run_threshold, now):
                    alerts.append(
                        {
                            "anomaly": "phase-long-running",
                            "priority": "medium",
                            "role": "implement-phase",
                            "summary": (
                                f"implement phase has been running for "
                                f"{int(elapsed_p)}s "
                                f"(> {long_run_threshold}s threshold)"
                            ),
                            "detail": (f"earliest phase_entered_at {phase_started.isoformat()}."),
                            "calibration_only": calibration_only,
                        }
                    )
                    synth.alerted_anomalies["phase-long-running"] = now

    # Persist updated alert bookkeeping. Best-effort — a write failure
    # logs but does not abort the cycle.
    try:
        save_agent_timing(state, timing_path)
    except OSError as exc:
        # Don't fail the cycle on a write error; the next tick will retry.
        print(
            json.dumps(
                {
                    "_overseer_warning": "save_agent_timing failed",
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
        )

    return alerts


def run_once(
    pipeline_id: str,
    role: str = "overseer",
    base_url: str | None = None,
) -> dict[str, Any]:
    """Run a single poll cycle and return the report.

    This is the ``--once`` mode entry point.  It queries all data sources,
    sends a heartbeat, and returns the cycle report dict (also emitted to
    stdout as a JSON line).

    Returns:
        The cycle report dictionary.
    """
    if base_url is None:
        base_url = get_orchestrator_url()

    cycle_start = time.monotonic()

    pipeline_data = query_pipeline_status(base_url, pipeline_id)
    status = pipeline_data.get("status", "unknown")
    phase_info = pipeline_data.get("phase", {})
    phase_name = phase_info.get("name", "unknown") if isinstance(phase_info, dict) else "unknown"
    consensus = pipeline_data.get("concurrent", {}).get("consensus", {})

    alerts = query_health_alerts(base_url, pipeline_id)
    progress = query_progress(base_url, pipeline_id)
    escalations = poll_messages(base_url, pipeline_id, role, wait=3)
    heartbeat_ok = send_heartbeat(base_url, pipeline_id, role)

    # Issue #1962: run the migrated detectors (host → overseer migration).
    # The detectors emit alerts only when overseer_owns_host_detection=True
    # (the calibration-window default is False so /sdlc keeps owning these
    # detectors during the first release; the cleanup-PR follow-up flips
    # the default and deletes the dormant /sdlc code blocks).
    config_subset = pipeline_data.get("config", {}) or {}
    detector_alerts = run_migrated_detectors(
        base_url=base_url,
        pipeline_id=pipeline_id,
        phase_name=phase_name,
        config_subset=config_subset,
        progress_events=progress,
        consensus=consensus,
    )

    # Tier-1 intersection gate (decision-18). The advisor is invoked
    # only when Haiku flags an anomaly AND a Tier-1 health alert is
    # present. We surface the gate state in the cycle report so the
    # overseer agent (Claude) reads it before calling the advisor MCP
    # tool. The agent's Haiku-classify pass owns the
    # `tier1_alerts_present == True` AND classification.confidence ≥ 0.8
    # decision; we expose helper `maybe_consult_advisor` (below) that
    # encodes the gate and forwards to the MCP tool when conditions
    # are met.
    advisor_gate = {
        "tier1_alerts_present": bool(alerts),
        "tier1_alert_types": sorted({a.get("type", "") for a in alerts if a}),
        "gate_open": bool(alerts),  # Haiku-flag check is agent-side
    }

    report: dict[str, Any] = {
        "cycle": 1,
        "ts": _now_iso(),
        "status": status,
        "phase": phase_name,
        "alerts": len(alerts),
        "alerts_detail": alerts,
        "progress_events": len(progress),
        "escalations": escalations,
        "consensus": consensus,
        "heartbeat_ok": heartbeat_ok,
        "cycle_duration_s": round(time.monotonic() - cycle_start, 2),
        "terminal": status in TERMINAL_STATES,
        "detector_alerts": detector_alerts,
        "advisor_gate": advisor_gate,
    }

    emit_cycle_report(report)
    return report


# Tier-1 intersection: the Haiku classifier confidence threshold above
# which the gate is considered tripped. Matches the precedent shipped
# in #2012 (Tier-1 intersection gate for agent-heartbeat-stall) and
# documented in sandbox/agent-config/rules/overseer.md.
HAIKU_CONFIDENCE_THRESHOLD = 0.8


def should_consult_advisor(
    classification: dict[str, Any],
    cycle_report: dict[str, Any],
) -> bool:
    """Return True when the Tier-1 intersection gate is open.

    Pure predicate (no side effects). Used by the overseer agent to
    decide whether to invoke the ``mcp__overseer__consult_advisor``
    MCP tool through its own MCP client surface — the actual tool
    invocation happens at the agent layer via the SDK's MCP client,
    NOT via a REST endpoint (the orchestrator's MCP server is
    exposed only over the FastMCP streamable-HTTP transport at
    ``/mcp``; there is no ``/api/v1/mcp/tools/...`` REST surface).

    Issue #1962 TASK-4-2 gate spec:
        confidence ≥ HAIKU_CONFIDENCE_THRESHOLD (0.8)
        AND any Tier-1 health alert present.

    Args:
        classification: Haiku's classification dict; must contain a
            numeric ``confidence`` field.
        cycle_report: Output of ``run_once``; we read
            ``advisor_gate.tier1_alerts_present``.

    Returns:
        True when both halves of the gate are satisfied; False
        otherwise.
    """
    confidence = float(classification.get("confidence") or 0.0)
    advisor_gate = cycle_report.get("advisor_gate", {})
    tier1 = bool(advisor_gate.get("tier1_alerts_present"))
    return confidence >= HAIKU_CONFIDENCE_THRESHOLD and tier1


def run_monitor(
    pipeline_id: str,
    role: str = "overseer",
    poll_interval: int = 30,
    base_url: str | None = None,
) -> int:
    """Run the monitoring loop until terminal state or SIGTERM.

    Args:
        pipeline_id: Pipeline to monitor.
        role: Agent role for heartbeats and message polling.
        poll_interval: Seconds between poll cycles.
        base_url: Orchestrator URL override.

    Returns:
        Exit code: 0 for complete, 1 for failed/cancelled.
    """
    if base_url is None:
        base_url = get_orchestrator_url()

    shutdown_requested = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cycle = 0

    while not shutdown_requested:
        cycle += 1
        cycle_start = time.monotonic()

        # 1. Pipeline status (includes consensus)
        pipeline_data = query_pipeline_status(base_url, pipeline_id)
        status = pipeline_data.get("status", "unknown")
        phase_info = pipeline_data.get("phase", {})
        phase_name = (
            phase_info.get("name", "unknown") if isinstance(phase_info, dict) else "unknown"
        )
        consensus = pipeline_data.get("concurrent", {}).get("consensus", {})

        # 2. Health alerts
        alerts = query_health_alerts(base_url, pipeline_id)

        # 3. Progress events
        progress = query_progress(base_url, pipeline_id)

        # 4. Escalation messages
        escalations = poll_messages(base_url, pipeline_id, role, wait=3)

        # 5. Heartbeat
        heartbeat_ok = send_heartbeat(base_url, pipeline_id, role)

        # Build cycle report
        report: dict[str, Any] = {
            "cycle": cycle,
            "ts": _now_iso(),
            "status": status,
            "phase": phase_name,
            "alerts": len(alerts),
            "alerts_detail": alerts,
            "progress_events": len(progress),
            "escalations": escalations,
            "consensus": consensus,
            "heartbeat_ok": heartbeat_ok,
            "cycle_duration_s": round(time.monotonic() - cycle_start, 2),
        }

        # 6. Terminal state check
        report["terminal"] = status in TERMINAL_STATES
        if report["terminal"]:
            emit_cycle_report(report)
            return 0 if status == "complete" else 1

        emit_cycle_report(report)

        # Sleep with early exit on shutdown
        sleep_end = time.monotonic() + poll_interval
        while time.monotonic() < sleep_end and not shutdown_requested:
            time.sleep(max(0, min(1.0, sleep_end - time.monotonic())))

    # SIGTERM path — emit shutdown report
    emit_cycle_report(
        {
            "cycle": cycle,
            "ts": _now_iso(),
            "shutdown": True,
            "reason": "signal",
        }
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Overseer monitoring script")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit (exit code 0=healthy, 1=failed/cancelled/unknown)",
    )
    args = parser.parse_args()

    pipeline_id = get_pipeline_id_from_env()
    if not pipeline_id:
        print("Error: EGG_PIPELINE_ID is required", file=sys.stderr)
        sys.exit(1)

    role = os.environ.get("EGG_AGENT_ROLE", "overseer")

    if args.once:
        report = run_once(pipeline_id, role=role)
        sys.exit(0 if report["status"] in ("running", "complete") else 1)

    poll_interval = int(os.environ.get("EGG_OVERSEER_POLL_INTERVAL", "30"))
    exit_code = run_monitor(pipeline_id, role=role, poll_interval=poll_interval)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
