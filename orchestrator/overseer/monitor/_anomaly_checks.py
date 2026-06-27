"""OverseerMonitor deterministic health checks + current-phase alert filtering.

Covers the orchestrator-reachability (#1371), re-run / status-consistency /
HITL-propagation / cross-phase health checks (#1297). Decomposed from the
pre-split ``overseer/monitor.py`` (#3312, slice-8).
"""

from __future__ import annotations

import time

from . import logger


def _filter_current_phase_agents(self, alerts: list[dict], pipeline_status: dict) -> list[dict]:
    """Filter alerts to only include agents in the current phase.

    Args:
        alerts: Health alerts from the orchestrator.
        pipeline_status: Full pipeline status including concurrent agent info.

    Returns:
        Alerts filtered to only current-phase agents.
    """
    concurrent = pipeline_status.get("concurrent", {})
    current_agents = concurrent.get("agents", [])
    if not current_agents:
        # No agent list available — return all alerts unfiltered
        return alerts

    # Build set of current phase agent roles/ids
    current_agent_ids: set[str] = set()
    for agent in current_agents:
        if isinstance(agent, dict):
            current_agent_ids.add(agent.get("role", ""))
            current_agent_ids.add(agent.get("agent_id", ""))
        elif isinstance(agent, str):
            current_agent_ids.add(agent)
    current_agent_ids.discard("")

    return [
        alert
        for alert in alerts
        if alert.get("agent_role", alert.get("agent_id", "")) in current_agent_ids
    ]


async def _check_orchestrator_reachability(self, pipeline_data: dict, phase_data: dict) -> None:
    """Track consecutive orchestrator query failures and escalate.

    When both pipeline status and phase queries return empty results,
    the orchestrator is likely unreachable. After
    ``_orch_unreachable_threshold`` consecutive failures, escalate via
    Slack and log an oversight event.

    Resets the counter on any successful response.
    """
    orch_reachable = bool(pipeline_data) or bool(phase_data)

    if orch_reachable:
        if self._consecutive_orch_failures > 0:
            logger.info(
                "Orchestrator reachable again after %d consecutive failures",
                self._consecutive_orch_failures,
            )
            self._log_oversight_event(
                {
                    "event": "orchestrator_recovered",
                    "consecutive_failures": self._consecutive_orch_failures,
                }
            )
        self._consecutive_orch_failures = 0
        return

    self._consecutive_orch_failures += 1
    logger.warning(
        "Orchestrator unreachable (consecutive failures: %d/%d)",
        self._consecutive_orch_failures,
        self._orch_unreachable_threshold,
    )

    # Alert at threshold, then re-alert every threshold cycles
    should_alert = (
        self._consecutive_orch_failures >= self._orch_unreachable_threshold
        and self._consecutive_orch_failures % self._orch_unreachable_threshold == 0
    )

    if should_alert:
        message = (
            f"Orchestrator has been unreachable for "
            f"{self._consecutive_orch_failures} consecutive poll cycles "
            f"(~{self._consecutive_orch_failures * getattr(self.config, 'overseer_poll_interval_seconds', 30)}s). "
            f"Pipeline {self.pipeline_id} may be orphaned. "
            f"Check orchestrator container health and logs."
        )
        self._log_oversight_event(
            {
                "event": "orchestrator_unreachable",
                "consecutive_failures": self._consecutive_orch_failures,
            }
        )
        await self._broadcast_alert("orchestrator_unreachable", "orchestrator", message, "critical")
        await self._send_slack_notification("orchestrator", message)


async def _check_rerun_anomaly(self, decisions: list[dict], phase_data: dict) -> None:
    """Detect agents that completed suspiciously fast after request_changes.

    Flags phase_gate decisions where the resolution contains
    ``request_changes``, ``content_changed`` is ``False``, and the
    subsequent work cycle lasted less than ``overseer_rerun_min_work_seconds``.
    """
    import datetime as _dt

    cycle_timings = phase_data.get("phase_execution", {}).get("cycle_timings", [])

    min_work = getattr(self.config, "overseer_rerun_min_work_seconds", 60)

    for d in decisions:
        did = d.get("id", "")
        if did in self._rerun_anomaly_reported:
            continue
        if d.get("decision_type") != "phase_gate":
            continue
        resolution = d.get("resolution") or ""
        if "request_changes" not in resolution.lower():
            continue
        if d.get("content_changed") is not False:
            continue

        # Find the cycle that started after this decision was resolved
        resolved_at = d.get("resolved_at")
        if not resolved_at:
            continue

        work_duration: float | None = None
        try:
            r = _dt.datetime.fromisoformat(resolved_at)
        except ValueError, TypeError:
            continue

        for ct in cycle_timings:
            started = ct.get("started_at")
            completed = ct.get("completed_at")
            if not started or not completed:
                continue
            try:
                s = _dt.datetime.fromisoformat(started)
                c = _dt.datetime.fromisoformat(completed)
                if s >= r:
                    work_duration = (c - s).total_seconds()
                    break
            except ValueError, TypeError:
                continue

        if work_duration is not None and work_duration < min_work:
            message = (
                f"Re-run anomaly: decision {did} requested changes but agent "
                f"completed in {work_duration:.0f}s (< {min_work}s) with "
                f"content_changed=False. Possible no-op re-run. "
                f"Pipeline: {self.pipeline_id}"
            )
            logger.warning("Re-run anomaly detected: %s", did)
            self._log_oversight_event(
                {"event": "rerun_anomaly", "decision_id": did, "work_seconds": work_duration}
            )
            await self._broadcast_alert("rerun_anomaly", "overseer", message, "high")
            await self._create_hitl_decision("overseer", message)
            await self._send_slack_notification("overseer", message)
            self._rerun_anomaly_reported.add(did)


async def _check_status_consistency(self, pipeline_data: dict) -> None:
    """Detect pipeline status=failed when all agents show status=complete."""
    status = pipeline_data.get("status", "")
    if status != "failed":
        # Not in failed state — reset tracking so alert can re-fire
        # if the pipeline re-enters failed state later.
        self._status_inconsistency_first_seen = None
        self._status_inconsistency_reported = False
        return

    if self._status_inconsistency_reported:
        return

    agents = pipeline_data.get("concurrent", {}).get("agents", [])
    if not agents:
        return

    all_complete = all(
        (a.get("status") == "complete" if isinstance(a, dict) else False) for a in agents
    )
    if not all_complete:
        self._status_inconsistency_first_seen = None
        return

    # Grace period: 1 poll cycle
    poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)
    now = time.time()

    if self._status_inconsistency_first_seen is None:
        self._status_inconsistency_first_seen = now
        return

    if (now - self._status_inconsistency_first_seen) < poll_interval:
        return

    message = (
        "Status inconsistency: pipeline status is 'failed' but all agents "
        f"show status 'complete'. Possible transient failure state. "
        f"Pipeline: {self.pipeline_id}"
    )
    logger.warning("Status inconsistency detected for pipeline %s", self.pipeline_id)
    self._log_oversight_event(
        {"event": "status_inconsistency", "pipeline_status": status, "agents": agents}
    )
    await self._broadcast_alert("status_inconsistency", "orchestrator", message, "high")
    await self._create_hitl_decision("orchestrator", message)
    await self._send_slack_notification("orchestrator", message)
    self._status_inconsistency_reported = True


async def _check_hitl_resolution_propagation(self, decisions: list[dict]) -> None:
    """Detect resolved phase_gate decisions not propagated to the contract."""
    timeout = getattr(self.config, "overseer_hitl_propagation_timeout_seconds", 300)
    now = time.time()

    # First pass: identify which decisions need a contract check
    timed_out: list[tuple[str, float]] = []
    for d in decisions:
        did = d.get("id", "")
        if did in self._hitl_resolution_verified or did in self._hitl_resolution_alerted:
            continue
        if d.get("decision_type") != "phase_gate":
            continue
        if d.get("status") != "resolved":
            continue

        if did not in self._hitl_resolution_pending:
            self._hitl_resolution_pending[did] = now
            continue

        elapsed = now - self._hitl_resolution_pending[did]
        if elapsed >= timeout:
            timed_out.append((did, elapsed))

    if not timed_out:
        return

    # Single contract query for all timed-out decisions
    contract = await self._query_contract_data()
    if not contract:
        return  # contract unavailable — retry next cycle
    contract_decisions = contract.get("decisions", [])

    for did, elapsed in timed_out:
        propagated = any(
            cd.get("id") == did and cd.get("status") == "resolved" for cd in contract_decisions
        )

        if propagated:
            self._hitl_resolution_verified.add(did)
            self._hitl_resolution_pending.pop(did, None)
            continue

        message = (
            f"HITL propagation failure: decision {did} was resolved "
            f"{elapsed:.0f}s ago but is not reflected in the SDLC contract. "
            f"Pipeline: {self.pipeline_id}"
        )
        logger.warning("HITL propagation failure detected: %s", did)
        self._log_oversight_event(
            {
                "event": "hitl_propagation_failure",
                "decision_id": did,
                "elapsed_seconds": elapsed,
            }
        )
        await self._broadcast_alert("hitl_propagation_failure", "orchestrator", message, "high")
        await self._create_hitl_decision("orchestrator", message)
        await self._send_slack_notification("orchestrator", message)
        self._hitl_resolution_alerted.add(did)
        self._hitl_resolution_pending.pop(did, None)


async def _check_cross_phase_consistency(
    self,
    phase_data: dict,
    decisions: list[dict],
    contract_data: dict | None = None,
) -> None:
    """On phase transition, check that phase output respects prior HITL decisions."""
    current_phase_name = phase_data.get("current_phase") or phase_data.get("name")
    if not current_phase_name:
        return

    # Detect phase transition
    if self._last_phase_name is None:
        self._last_phase_name = current_phase_name
        return

    if current_phase_name == self._last_phase_name:
        return

    # Phase changed — reset per-phase tracking state and run consistency check
    previous_phase = self._last_phase_name
    self._last_phase_name = current_phase_name
    self._agents_restart_exhausted.clear()

    if (previous_phase, current_phase_name) in self._cross_phase_checked:
        return
    self._cross_phase_checked.add((previous_phase, current_phase_name))

    # Collect resolved decisions from prior phases
    prior_decisions = [
        d
        for d in decisions
        if d.get("status") == "resolved" and d.get("phase") != current_phase_name
    ]
    if not prior_decisions:
        return

    # Fetch contract data lazily if not provided
    if contract_data is None:
        contract_data = await self._query_contract_data()

    if not contract_data:
        return

    result = await self._check_decision_consistency_cls(contract_data, prior_decisions)
    # TODO: Propagate actual token count and cost from the classifier.
    # Currently _call_classifier does not return usage metadata, so we
    # record the call for tracking purposes with zero tokens/cost.
    self.self_monitor.record_llm_call("haiku", 0, 0.0)

    if not result.get("consistent", True) and result.get("confidence", 0) > 0.7:
        concerns = result.get("concerns", [])
        concerns_text = "; ".join(concerns) if concerns else "No specific concerns listed"
        message = (
            f"Cross-phase consistency issue: phase '{current_phase_name}' output "
            f"may not respect prior HITL decisions from '{previous_phase}'. "
            f"Concerns: {concerns_text}. "
            f"Pipeline: {self.pipeline_id}"
        )
        logger.warning(
            "Cross-phase consistency issue for pipeline %s: %s -> %s",
            self.pipeline_id,
            previous_phase,
            current_phase_name,
        )
        self._log_oversight_event(
            {
                "event": "cross_phase_inconsistency",
                "from_phase": previous_phase,
                "to_phase": current_phase_name,
                "concerns": concerns,
                "confidence": result.get("confidence"),
            }
        )
        await self._broadcast_alert("cross_phase_inconsistency", "overseer", message, "high")
        await self._create_hitl_decision("overseer", message)
        await self._send_slack_notification("overseer", message)
