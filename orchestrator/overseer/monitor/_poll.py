"""OverseerMonitor core poll-classify-decide-act cycle (#3312, slice-8).

``_TERMINAL_STATUSES`` is reached through the package barrel (``_pkg``).
"""

from __future__ import annotations

import time

import overseer.monitor as _pkg

from . import logger


async def _poll_cycle(self) -> None:
    """Execute a single monitoring cycle.

    Steps:
        1. Query consensus status and current phase
        2. Query progress events
        3. Query health alerts
        4. Query pipeline status (single call for filtering + terminal check)
        4-orch. Check orchestrator reachability
        4a. Query decisions for deterministic health checks
        4b. Run deterministic health checks (rerun anomaly, status
            consistency, HITL resolution propagation)
        5. Check for escalation messages from orchestrator
        6. Route anomalies through classifier (with consensus context)
        7. Route classified results through decision maker / execute actions
        8. Check pipeline status for terminal state
        9. Cross-phase consistency (LLM-based, only on phase transitions)
        10. Check for post-consensus stalls
        11. Check for incomplete consensus stalls (#1471)
        12. Update self-monitoring
    """
    cycle_start = time.time()

    # Periodic cleanup of expired infra error dedup entries (#1489)
    self._cleanup_infra_error_dedup()

    try:
        # 1. Query consensus status and current phase
        consensus = await self._query_consensus_status()
        current_phase = await self._query_current_phase()

        # 2. Query progress events
        progress_events = await self._query_progress()

        # 3. Query health alerts
        alerts = await self._query_health_alerts()

        # 4. Query pipeline status (single call, used for filtering + terminal check)
        pipeline_data = await self._query_pipeline_data()
        status = pipeline_data.get("status", "running") if pipeline_data else "running"

        # 4-orch. Check orchestrator reachability
        await self._check_orchestrator_reachability(pipeline_data, current_phase)

        # 4a. Query decisions for health checks
        decisions = await self._query_decisions()

        # 4b. Deterministic health checks
        await self._check_rerun_anomaly(decisions, current_phase)
        await self._check_status_consistency(pipeline_data)
        await self._check_hitl_resolution_propagation(decisions)

        # Filter alerts to only current-phase agents
        if current_phase and pipeline_data:
            alerts = self._filter_current_phase_agents(alerts, pipeline_data)

        # 5. Check for escalation messages
        escalations = await self._poll_escalation_messages()

        # 6 & 7. Process any anomalies (with consensus context + container logs)
        container_logs_cache: dict[str, str] = {}
        for alert in alerts:
            agent_role = alert.get("agent_role", alert.get("agent_id", "unknown"))
            alert_type = alert.get("alert_type", "")

            # Fetch container logs for the alerted agent (best-effort, cached per cycle)
            if agent_role not in container_logs_cache:
                container_logs_cache[agent_role] = await self._query_container_logs(agent_role)
            container_logs = container_logs_cache[agent_role]

            if alert_type == "infrastructure_error":
                # Tier 1 infrastructure_error alerts: skip LLM classification,
                # route directly to decision maker with pre-set classification
                error_msg = alert.get("message", "Infrastructure error detected by Tier 1")

                # Dedup: skip if already escalated within the dedup window
                if self._is_infra_error_deduped(agent_role, error_msg):
                    logger.debug(
                        "Dedup: skipping duplicate infra error escalation for %s",
                        agent_role,
                    )
                    await self._resolve_alert(
                        agent_id=alert.get("agent_id", agent_role),
                        alert_type="infrastructure_error",
                    )
                    continue

                logger.info(
                    "Infrastructure error alert for %s — bypassing classifier",
                    agent_role,
                )
                classification = {
                    "classification": "infrastructure_error",
                    "confidence": 1.0,
                    "reasoning": error_msg,
                }
                self._record_infra_error_escalation(agent_role, error_msg)
            else:
                classification = await self._classify_stall(
                    logs=alert.get("logs", []),
                    progress=progress_events,
                    consensus=consensus or None,
                    container_logs=container_logs or None,
                )

                # Dedup: if classifier detected infra error, check dedup window.
                # Use the raw alert message (not LLM reasoning) so that Tier 1
                # and Tier 2 hash to the same value for the same underlying error.
                if classification.get("classification") == "infrastructure_error":
                    dedup_key = alert.get("message", classification.get("reasoning", ""))
                    if self._is_infra_error_deduped(agent_role, dedup_key):
                        logger.debug(
                            "Dedup: skipping classifier-detected infra error for %s",
                            agent_role,
                        )
                        await self._resolve_alert(
                            agent_id=alert.get("agent_id", agent_role),
                            alert_type=alert.get("alert_type", "unknown"),
                        )
                        continue
                    self._record_infra_error_escalation(agent_role, dedup_key)

            # Include container logs in context for the decision maker
            action_context: dict = {
                "alert": alert,
                "pipeline_id": self.pipeline_id,
            }
            if container_logs:
                action_context["container_logs"] = container_logs[-4000:]

            decision = await self._decide_corrective_action(
                classification,
                action_context,
                redirect_history=list(self._escalation_history.get(agent_role, [])),
            )
            await self._execute_action(decision, agent_role, container_logs=container_logs)
            await self._resolve_alert(
                agent_id=alert.get("agent_id", agent_role),
                alert_type=alert.get("alert_type", "unknown"),
            )

        # Process escalation messages (with consensus context, reusing log cache)
        for escalation in escalations:
            await self.handle_escalation(
                escalation, consensus=consensus, container_logs_cache=container_logs_cache
            )

        # 8. Check pipeline status for terminal state
        if status in _pkg._TERMINAL_STATUSES:
            # NOTE: the legacy ``_check_pr_phase_outcome`` safety-net
            # was removed in #2777 (cq-4); see the docstring on the
            # removed helper for why the condition is unreachable
            # under the new context-PR-up-front topology.
            self._log_oversight_event(
                {
                    "event": "pipeline_terminal",
                    "status": status,
                }
            )
            self._running = False
            self.write_health_summary()

        # 9. Cross-phase consistency (LLM-based, only on phase transitions)
        if status not in _pkg._TERMINAL_STATUSES:
            await self._check_cross_phase_consistency(current_phase, decisions, contract_data=None)

        # 10. Check for post-consensus stall
        await self._check_post_consensus_stall(consensus, status)

        # 11. Check for incomplete consensus stall (#1471)
        await self._check_incomplete_consensus_stall(consensus, status)

    except Exception:
        logger.exception("Error in overseer poll cycle")

    # 12. Update self-monitoring
    duration = time.time() - cycle_start
    self.self_monitor.record_poll_cycle(duration)
