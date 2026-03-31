#!/usr/bin/env python3
"""Pre-built overseer monitoring script.

Runs a continuous poll cycle that queries the orchestrator API for pipeline
status, health alerts, progress events, and escalation messages.  Outputs
structured JSON lines to stdout so the overseer agent can read and act on
anomalies without writing its own monitoring loop.

Exits cleanly when the pipeline reaches a terminal state or on SIGTERM.

Usage:
    python3 /opt/egg-runtime/sandbox/overseer_monitor.py

Environment:
    EGG_PIPELINE_ID          Required — pipeline to monitor.
    EGG_AGENT_ROLE           Agent role (default: "overseer").
    EGG_ORCHESTRATOR_URL     Orchestrator base URL.
    EGG_OVERSEER_POLL_INTERVAL  Poll interval in seconds (default: 15).
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import time
from typing import Any
from urllib.parse import urlencode

from egg_lib.orch_cli import (
    ApiError,
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
    except (ApiError, Exception):
        return {}


def _orch_post(
    base_url: str, endpoint: str, data: dict[str, Any], timeout: int = 10
) -> dict[str, Any]:
    """POST request to the orchestrator, returning {} on failure."""
    try:
        return api_request(base_url, endpoint, method="POST", data=data, timeout=timeout)
    except (ApiError, Exception):
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
    """Send an inter-agent message."""
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


def run_monitor(
    pipeline_id: str,
    role: str = "overseer",
    poll_interval: int = 15,
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
        phase_name = phase_info.get("name", "unknown") if isinstance(phase_info, dict) else "unknown"
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
        if status in TERMINAL_STATES:
            report["terminal"] = True
            emit_cycle_report(report)
            return 0 if status == "complete" else 1

        emit_cycle_report(report)

        # Sleep with early exit on shutdown
        sleep_end = time.monotonic() + poll_interval
        while time.monotonic() < sleep_end and not shutdown_requested:
            time.sleep(min(1.0, sleep_end - time.monotonic()))

    # SIGTERM path — emit shutdown report
    emit_cycle_report({
        "cycle": cycle,
        "ts": _now_iso(),
        "shutdown": True,
        "reason": "signal",
    })
    return 0


def main() -> None:
    pipeline_id = get_pipeline_id_from_env()
    if not pipeline_id:
        print("Error: EGG_PIPELINE_ID is required", file=sys.stderr)
        sys.exit(1)

    role = os.environ.get("EGG_AGENT_ROLE", "overseer")
    poll_interval = int(os.environ.get("EGG_OVERSEER_POLL_INTERVAL", "15"))

    exit_code = run_monitor(pipeline_id, role=role, poll_interval=poll_interval)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
