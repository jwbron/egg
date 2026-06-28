"""OverseerMonitor alerting/messaging primitives + infra-error dedup (#1489).

Decomposed from the pre-split ``overseer/monitor.py`` (#3312, slice-8).
"""

from __future__ import annotations

import hashlib
import time

from . import logger


def _infra_error_hash(self, error_msg: str) -> str:
    """Compute a hash for an infrastructure error message for dedup."""
    return hashlib.sha256(error_msg.encode()).hexdigest()[:16]


def _is_infra_error_deduped(self, agent_id: str, error_msg: str) -> bool:
    """Check if an infrastructure error for this agent is within the dedup window.

    Returns True if the same error (by hash) for the same agent was
    already escalated within the configurable dedup window.
    """
    error_hash = self._infra_error_hash(error_msg)
    key = (agent_id, error_hash)
    if key not in self._infra_error_dedup:
        return False

    window = getattr(self.config, "overseer_infra_error_dedup_window_seconds", 300)
    elapsed = time.time() - self._infra_error_dedup[key]
    if elapsed < window:
        return True

    # Outside window — allow re-escalation
    del self._infra_error_dedup[key]
    return False


def _record_infra_error_escalation(self, agent_id: str, error_msg: str) -> None:
    """Record an infrastructure error escalation for dedup tracking."""
    error_hash = self._infra_error_hash(error_msg)
    key = (agent_id, error_hash)
    self._infra_error_dedup[key] = time.time()


def _cleanup_infra_error_dedup(self) -> None:
    """Remove expired entries from the infra error dedup dict."""
    window = getattr(self.config, "overseer_infra_error_dedup_window_seconds", 300)
    now = time.time()
    expired = [k for k, ts in self._infra_error_dedup.items() if now - ts >= window]
    for k in expired:
        del self._infra_error_dedup[k]


async def _broadcast_alert(
    self, anomaly_type: str, agent_role: str, message: str, priority: str = "medium"
) -> None:
    """Broadcast an anomaly alert to all agents and the operator.

    Sends an ``OVERSEER_ALERT`` message to the ``all`` target so that
    any listener (including the ``/sdlc`` monitoring session) can pick
    it up via ``recent_messages``.

    Args:
        anomaly_type: Short label for the anomaly (e.g. "post_consensus_stall").
        agent_role: The agent (or component) the anomaly relates to.
        message: Human-readable description of the anomaly.
        priority: Alert priority (low/medium/high/critical).
    """
    subject = f"{anomaly_type}: {agent_role} [{priority}]"
    try:
        await self._run_cli(
            "egg-orch",
            "message",
            "send",
            self.pipeline_id,
            "--role",
            "overseer",
            "--to",
            "all",
            "--type",
            "OVERSEER_ALERT",
            "--subject",
            subject,
            "--body",
            message,
        )
    except Exception:
        logger.debug("Failed to broadcast alert for %s", agent_role, exc_info=True)


async def _send_message(self, agent_role: str, message: str) -> None:
    """Send a message to an agent via the orchestrator."""
    try:
        await self._run_cli(
            "egg-orch",
            "message",
            "send",
            self.pipeline_id,
            "--role",
            "overseer",
            "--to",
            agent_role,
            "--type",
            "STATUS",
            "--subject",
            "Overseer health check",
            "--body",
            message,
        )
    except Exception:
        logger.debug("Failed to send message to %s", agent_role, exc_info=True)


async def _resolve_alert(self, agent_id: str, alert_type: str) -> None:
    """Resolve a health alert so it does not accumulate."""
    try:
        await self._run_cli(
            "egg-orch",
            "health",
            "resolve",
            self.pipeline_id,
            "--agent-id",
            agent_id,
            "--alert-type",
            alert_type,
        )
    except Exception:
        logger.debug("Failed to resolve alert %s for %s", alert_type, agent_id, exc_info=True)


async def _create_hitl_decision(self, agent_role: str, message: str) -> None:
    """Create a HITL decision for an agent issue."""
    try:
        await self._run_cli(
            "egg-orch",
            "decision",
            "create",
            self.pipeline_id,
            "--question",
            f"Agent {agent_role} issue: {message}",
            "--options",
            "Restart agent",
            "Continue monitoring",
            "Cancel pipeline",
        )
    except Exception:
        logger.debug("Failed to create HITL decision for %s", agent_role, exc_info=True)


async def _create_phase_restart_decision(self, agent_role: str, message: str) -> None:
    """Create a HITL decision for a phase restart request.

    Unlike agent-level restarts (which the overseer executes
    automatically), phase restarts surface a decision so a human can
    review and then **manually call the phase restart API endpoint**.
    The HITL decision options are advisory — no automated execution
    occurs after the human selects an option.
    """
    try:
        await self._run_cli(
            "egg-orch",
            "decision",
            "create",
            self.pipeline_id,
            "--question",
            f"Phase restart decision: {message}",
            "--options",
            "Restart phase",
            "Restart individual agent",
            "Continue monitoring",
            "Cancel pipeline",
        )
    except Exception:
        logger.debug(
            "Failed to create phase restart HITL decision for %s",
            agent_role,
            exc_info=True,
        )


async def _send_slack_notification(self, agent_role: str, message: str) -> None:
    """Send a Slack notification about an agent issue."""
    try:
        import datetime
        import pathlib

        ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
        path = pathlib.Path.home() / "sharing" / "notifications" / f"{ts}-overseer.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Pipeline Health Alert\n\n"
            f"**Pipeline**: {self.pipeline_id}\n"
            f"**Agent**: {agent_role}\n\n"
            f"{message}\n"
        )
    except Exception:
        logger.debug("Failed to send Slack notification", exc_info=True)
