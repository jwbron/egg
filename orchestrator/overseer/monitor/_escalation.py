"""OverseerMonitor escalation handling + corrective-action execution (#3312, slice-8).

The escalation safety-net keywords (``_HUMAN_WORDS`` / ``_ACTION_WORDS``) and
``file_diagnostic_issue`` are reached through the package barrel (``_pkg``) so
``patch("overseer.monitor.file_diagnostic_issue")`` keeps intercepting the call.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque

import overseer.monitor as _pkg

from . import logger


async def handle_escalation(
    self,
    escalation: dict,
    consensus: dict | None = None,
    container_logs_cache: dict[str, str] | None = None,
) -> None:
    """Handle an escalation from the orchestrator's tripwire processor.

    Implements a hallucination guard: the Sonnet decision tier only
    acts on data that has been classified by the Haiku tier first.

    Args:
        escalation: Dict with escalation details from the orchestrator.
        consensus: Optional current BRC consensus status.
        container_logs_cache: Optional per-cycle cache to avoid redundant fetches.
    """
    agent_role = escalation.get("agent_role", escalation.get("agent_id", "unknown"))

    # Fetch container logs for the escalated agent (best-effort, cached per cycle)
    if container_logs_cache is None:
        container_logs_cache = {}
    if agent_role not in container_logs_cache:
        container_logs_cache[agent_role] = await self._query_container_logs(agent_role)
    container_logs = container_logs_cache[agent_role]

    # Hallucination guard: always classify first, then decide
    classification = await self._classify_stall(
        logs=escalation.get("logs", []),
        progress=escalation.get("progress", []),
        consensus=consensus,
        container_logs=container_logs or None,
    )

    # Check redirect history for this agent. Filter to the current
    # generation (#2270 slice-5) so escalations stamped before an
    # orchestrator recycle can never inflate this run's redirect_count.
    # Records predating generation stamping default to the current
    # generation (backwards compatible).
    history = [
        h
        for h in self._escalation_history.get(agent_role, [])
        if h.get("generation", self.generation) == self.generation
    ]
    max_redirects = getattr(self.config, "overseer_max_redirects_before_escalation", 2)

    redirect_count = sum(1 for h in history if h.get("action") == "redirect")

    # Include container logs in context for the decision maker
    action_context: dict = {
        "escalation": escalation,
        "pipeline_id": self.pipeline_id,
    }
    if container_logs:
        action_context["container_logs"] = container_logs[-4000:]

    if redirect_count >= max_redirects:
        # Too many redirects -- escalate
        escalation_decision = await self._decide_escalation_level(
            classification, history, context=action_context
        )
        decision = {
            "action": escalation_decision.get("level", "hitl"),
            "message": escalation_decision.get("reasoning", ""),
            "priority": "high",
        }
    else:
        decision = await self._decide_corrective_action(
            classification,
            action_context,
            redirect_history=history,
        )

    await self._execute_action(decision, agent_role, container_logs=container_logs)

    # Record in escalation history (bounded per agent)
    if agent_role not in self._escalation_history:
        self._escalation_history[agent_role] = deque(maxlen=50)
    self._escalation_history[agent_role].append(
        {
            "action": decision.get("action"),
            "classification": classification,
            "timestamp": time.time(),
            "generation": self.generation,
        }
    )


async def _execute_action(
    self, decision: dict, agent_role: str, container_logs: str | None = None
) -> None:
    """Execute a corrective action based on a decision.

    Includes a safety net: if the decision message indicates human
    intervention is required but the action is only ``nudge`` or
    ``redirect``, the action is upgraded to ``hitl``.

    Args:
        decision: Output from decide_corrective_action.
        agent_role: The target agent role.
        container_logs: Optional container logs to include in diagnostic issues.
    """
    action = decision.get("action", "nudge")
    message = decision.get("message", "")

    # Safety net: upgrade to hitl if message indicates human intervention
    # but action is too weak.  Match common LLM phrasings — the message
    # comes from a classifier so we check for "human" or "manual" paired
    # with action-oriented words.
    if action in ("nudge", "redirect"):
        msg_lower = message.lower()
        if any(hw in msg_lower for hw in _pkg._HUMAN_WORDS) and any(
            aw in msg_lower for aw in _pkg._ACTION_WORDS
        ):
            logger.info(
                "Upgrading action from %s to hitl for %s: message indicates "
                "human intervention required",
                action,
                agent_role,
            )
            action = "hitl"

    logger.info(
        "Executing %s action for %s in pipeline %s: %s",
        action,
        agent_role,
        self.pipeline_id,
        message[:100],
    )

    self._log_oversight_event(
        {
            "event": "action_executed",
            "action": action,
            "agent_role": agent_role,
            "message": message[:500],
            "priority": decision.get("priority", "medium"),
        }
    )

    # Broadcast all non-trivial actions so the /sdlc monitoring session
    # (and any other listener) can surface overseer findings.
    await self._broadcast_alert(
        anomaly_type=f"action:{action}",
        agent_role=agent_role,
        message=message,
        priority=decision.get("priority", "medium"),
    )

    if action in ("nudge", "redirect"):
        await self._send_message(agent_role, message)
        self.self_monitor.record_message_sent()

    elif action == "hitl":
        await self._create_hitl_decision(agent_role, message)
        self.self_monitor.record_message_sent()

    elif action == "issue":
        # Guarded shadow->enforce gate (#2270 §6): the alert broadcast above
        # already surfaces the finding; the gh filing (through the two-tier
        # dedup ledger) only fires in "live" mode, so the default "shadow"
        # mode can never auto-spam the tracker on a mis-calibrated detector.
        if getattr(self.config, "overseer_auto_file_issues_mode", "shadow") == "live":
            issue_context: dict = {"pipeline_id": self.pipeline_id}
            if container_logs:
                issue_context["container_logs"] = container_logs[-4000:]
            await _pkg.file_diagnostic_issue(
                pipeline_id=self.pipeline_id,
                agent_role=agent_role,
                anomaly={"type": "escalation", "description": message},
                context=issue_context,
                dedup_ledger=self._issue_dedup_ledger,
            )
        else:
            logger.info(
                "auto-file-issues in shadow mode; surfacing diagnostic for %s "
                "via alert instead of filing (pipeline %s)",
                agent_role,
                self.pipeline_id,
            )

    elif action == "slack":
        await self._send_slack_notification(agent_role, message)

    elif action == "restart_agent":
        await self._execute_restart_agent(agent_role, message)
        self.self_monitor.record_message_sent()

    elif action == "restart_phase":
        # Phase restarts require HITL approval — the human must
        # manually call the phase restart API after reviewing.
        from urllib.parse import quote

        current_phase = os.environ.get("EGG_CURRENT_PHASE", "implement")
        orchestrator_url = os.environ.get("EGG_ORCHESTRATOR_URL", "http://localhost:9849")
        restart_api = (
            f"POST {orchestrator_url}/api/v1/pipelines/"
            f"{quote(self.pipeline_id, safe='')}/phases/{quote(current_phase, safe='')}/restart"
        )
        await self._create_phase_restart_decision(
            agent_role,
            f"Phase restart requested: {message}. To approve, call: {restart_api}",
        )
        self.self_monitor.record_message_sent()


async def _execute_restart_agent(self, agent_role: str, message: str) -> None:
    """Execute an agent restart via the orchestrator REST API.

    The spawner is the single source of truth for restart counts and
    limit enforcement.  This method simply POSTs to the restart endpoint
    and interprets the response.  If the spawner returns an error (HTTP 500
    for restart-limit-exceeded or spawn failure), we parse the response body
    from the HTTPError and escalate appropriately.

    Args:
        agent_role: The agent role to restart.
        message: Reason for the restart.
    """
    # Call the restart REST API endpoint directly (no CLI subcommand exists)
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    try:
        orchestrator_url = os.environ.get("EGG_ORCHESTRATOR_URL", "http://localhost:9849")
        restart_url = (
            f"{orchestrator_url}/api/v1/pipelines/"
            f"{quote(self.pipeline_id, safe='')}/agents/{quote(agent_role, safe='')}/restart"
        )

        req_data = json.dumps({"reason": message[:500]}).encode()
        req = urllib.request.Request(
            restart_url,
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())

        if result.get("success"):
            restart_count = result.get("data", {}).get("restart_count", "?")
            logger.info(
                "Agent %s restarted successfully (count: %s)",
                agent_role,
                restart_count,
            )
            self._log_oversight_event(
                {
                    "event": "agent_restarted",
                    "agent_role": agent_role,
                    "restart_count": restart_count,
                    "reason": message[:500],
                }
            )
            # A successful restart means the agent is no longer exhausted
            # (e.g. budget was manually reset by an operator).
            self._agents_restart_exhausted.discard(agent_role)
        else:
            # 2xx response with success=false (shouldn't happen in practice
            # but handle defensively)
            error_msg = result.get("message", "Unknown error")
            logger.error(
                "Failed to restart agent %s: %s",
                agent_role,
                error_msg,
            )
            await self._handle_restart_failure(agent_role, error_msg, message)
    except urllib.error.HTTPError as e:
        # The restart endpoint returns HTTP 500 on ContainerSpawnError
        # (including restart-limit-exceeded).  Parse the JSON body to
        # determine whether this is a limit-exceeded case.
        try:
            body = json.loads(e.read().decode())
            error_msg = body.get("message", str(e))
        except json.JSONDecodeError, UnicodeDecodeError:
            error_msg = str(e)
        logger.error(
            "Failed to restart agent %s (HTTP %s): %s",
            agent_role,
            e.code,
            error_msg,
        )
        await self._handle_restart_failure(agent_role, error_msg, message)
    except Exception as e:
        logger.error("Exception restarting agent %s: %s", agent_role, e)
        await self._create_hitl_decision(
            agent_role,
            f"Exception restarting agent {agent_role}: {e}. Original issue: {message}",
        )


async def _handle_restart_failure(self, agent_role: str, error_msg: str, message: str) -> None:
    """Handle a failed restart attempt — track exhaustion and escalate.

    Only marks an agent as "exhausted" when the error indicates the restart
    budget has been used up (not transient Docker/network failures).  When
    2+ agents have exhausted their restart limits, escalate to a phase
    restart HITL decision.
    """
    is_limit_exceeded = "restart limit" in error_msg.lower() and "exceeded" in error_msg.lower()
    if is_limit_exceeded:
        self._agents_restart_exhausted.add(agent_role)
    if len(self._agents_restart_exhausted) >= 2:
        exhausted_list = sorted(self._agents_restart_exhausted)
        logger.warning(
            "Multiple agents exhausted restart limits (%s) — escalating to phase restart",
            exhausted_list,
        )
        await self._create_hitl_decision(
            "orchestrator",
            f"Multiple agents have exhausted restart limits "
            f"({', '.join(exhausted_list)}). Consider restarting "
            f"the entire phase. Original issue: {message}",
        )
    else:
        await self._create_hitl_decision(
            agent_role,
            f"Attempted to restart agent {agent_role} but failed: "
            f"{error_msg}. Original issue: {message}",
        )
