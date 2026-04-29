"""Main overseer monitoring loop.

Implements the continuous poll-classify-decide-act cycle that runs for
the lifetime of a pipeline.  The monitor queries the orchestrator for
progress events and health alerts, routes anomalies through the
classifier tier, and executes corrective actions via the decision tier.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from overseer.classifier import (
    check_alignment,
    check_decision_consistency,
    classify_error,
    classify_stall,
    detect_loop,
)
from overseer.decision_maker import (
    compose_redirect_message,
    decide_corrective_action,
    decide_escalation_level,
)
from overseer.issue_filer import file_diagnostic_issue
from overseer.self_monitor import OverseerSelfMonitor

try:
    from state_store import get_state_store as _get_state_store
except ImportError:
    _get_state_store = None

logger = logging.getLogger(__name__)

# Keywords for the escalation safety net — match common LLM phrasings
# indicating human intervention is needed.
_HUMAN_WORDS = ("human", "manual", "operator")
_ACTION_WORDS = ("intervention", "attention", "review", "required", "needed", "escalat")


class _DefaultConfig:
    """Fallback config when no PipelineConfig is provided."""

    overseer_poll_interval_seconds: int = 30
    overseer_max_redirects_before_escalation: int = 2
    overseer_decision_maker_model: str = "sonnet"
    overseer_rerun_min_work_seconds: int = 60
    overseer_hitl_propagation_timeout_seconds: int = 300
    overseer_infra_error_dedup_window_seconds: int = 300


class OverseerMonitor:
    """Main overseer monitoring loop.

    Polls the orchestrator for progress events and health alerts,
    classifies anomalies, decides on corrective actions, and executes
    them.

    Args:
        pipeline_id: The pipeline to monitor.
        config: Pipeline configuration (uses defaults if ``None``).
        classifier: Optional override for the classifier module (for testing).
        decision_maker: Optional override for the decision_maker module (for testing).
    """

    def __init__(
        self,
        pipeline_id: str,
        config: Any = None,
        classifier: Any = None,
        decision_maker: Any = None,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.config = config or _DefaultConfig()
        self.self_monitor = OverseerSelfMonitor()
        self._running = False
        # agent_role -> bounded deque of escalations (keep last 50 per agent)
        self._escalation_history: dict[str, deque] = {}

        # Allow dependency injection for testing
        self._classifier = classifier
        self._decision_maker = decision_maker

        # Post-consensus stall deduplication
        self._post_consensus_stall_reported = False
        self._post_consensus_stall_first_seen: float | None = None

        # Incomplete consensus stall tracking (#1471)
        self._incomplete_consensus_first_seen: float | None = None
        self._incomplete_consensus_blocking: frozenset[str] | None = None
        self._incomplete_consensus_nudged = False
        self._incomplete_consensus_hitl_created = False
        # Track the absolute start time for activity deferral cap (#1609)
        self._incomplete_consensus_absolute_start: float | None = None

        # Re-run anomaly deduplication (decision IDs already flagged)
        self._rerun_anomaly_reported: set[str] = set()

        # Status inconsistency deduplication
        self._status_inconsistency_reported = False
        self._status_inconsistency_first_seen: float | None = None

        # HITL resolution propagation tracking
        self._hitl_resolution_pending: dict[str, float] = {}  # decision_id -> first-seen ts
        self._hitl_resolution_verified: set[str] = set()
        self._hitl_resolution_alerted: set[str] = set()  # failures already alerted

        # Orchestrator unreachability tracking
        self._consecutive_orch_failures: int = 0
        self._orch_unreachable_threshold: int = 3  # escalate after N consecutive failures

        # Infrastructure error deduplication between Tier 1 and Tier 2 (#1489)
        # Maps (agent_id, error_hash) -> timestamp of first escalation
        self._infra_error_dedup: dict[tuple[str, str], float] = {}

        # Agent restart tracking: the spawner (via the REST API) is the single
        # source of truth for restart counts and limit enforcement.  We track
        # which agents have been reported as exhausted (limit exceeded) so we
        # can escalate to a phase restart HITL when 2+ agents are exhausted.
        # See issue #1695 items 2 & 3.
        self._agents_restart_exhausted: set[str] = set()

        # Cross-phase consistency: track phase transitions and deduplication
        self._last_phase_name: str | None = None
        self._cross_phase_checked: set[tuple[str, str]] = set()

        # Oversight logging to .egg-state/oversight/
        self._oversight_dir = self._resolve_oversight_dir()
        self._jsonl_path: Path | None = None
        if self._oversight_dir:
            try:
                self._oversight_dir.mkdir(parents=True, exist_ok=True)
                self._jsonl_path = self._oversight_dir / f"{pipeline_id}-oversight.jsonl"
            except OSError:
                # Non-critical: oversight logging is optional
                logger.debug("Cannot create oversight dir %s", self._oversight_dir)
                self._oversight_dir = None

    # -----------------------------------------------------------------
    # Oversight logging
    # -----------------------------------------------------------------

    @staticmethod
    def _resolve_oversight_dir() -> Path | None:
        """Resolve the .egg-state/oversight/ directory path."""
        repo_path = os.environ.get("EGG_REPO_PATH")
        if repo_path:
            return Path(repo_path) / ".egg-state" / "oversight"
        return None

    def _log_oversight_event(self, event: dict) -> None:
        """Append an oversight event as a JSONL line."""
        if not self._jsonl_path:
            return
        try:
            import datetime

            record = {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "pipeline_id": self.pipeline_id,
                **event,
            }
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            logger.debug("Failed to write oversight event to JSONL", exc_info=True)

    def write_health_summary(self) -> None:
        """Write the pipeline health summary to .egg-state/oversight/."""
        if not self._oversight_dir:
            return
        try:
            summary = self.generate_health_summary()
            summary_path = self._oversight_dir / f"{self.pipeline_id}-health-summary.md"
            summary_path.write_text(summary)
        except Exception:
            logger.debug("Failed to write health summary", exc_info=True)

    # -----------------------------------------------------------------
    # Classifier / decision maker accessors
    # -----------------------------------------------------------------

    async def _classify_stall(
        self,
        logs: list[dict],
        progress: list[dict],
        consensus: dict | None = None,
        container_logs: str | None = None,
    ) -> dict:
        if self._classifier and hasattr(self._classifier, "classify_stall"):
            return await self._classifier.classify_stall(
                logs, progress, consensus=consensus, container_logs=container_logs
            )
        return await classify_stall(
            logs, progress, consensus=consensus, container_logs=container_logs
        )

    async def _classify_error(self, error_context: dict, container_logs: str | None = None) -> dict:
        if self._classifier and hasattr(self._classifier, "classify_error"):
            return await self._classifier.classify_error(
                error_context, container_logs=container_logs
            )
        return await classify_error(error_context, container_logs=container_logs)

    async def _detect_loop(self, recent_actions: list[dict]) -> dict:
        if self._classifier and hasattr(self._classifier, "detect_loop"):
            return await self._classifier.detect_loop(recent_actions)
        return await detect_loop(recent_actions)

    async def _check_alignment(self, activity: list[dict], contract: dict) -> dict:
        if self._classifier and hasattr(self._classifier, "check_alignment"):
            return await self._classifier.check_alignment(activity, contract)
        return await check_alignment(activity, contract)

    async def _check_decision_consistency_cls(
        self, phase_output: dict, prior_decisions: list[dict]
    ) -> dict:
        if self._classifier and hasattr(self._classifier, "check_decision_consistency"):
            return await self._classifier.check_decision_consistency(phase_output, prior_decisions)
        return await check_decision_consistency(phase_output, prior_decisions)

    async def _decide_corrective_action(self, classification: dict, context: dict) -> dict:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(self._decision_maker, "decide_corrective_action"):
            return await self._decision_maker.decide_corrective_action(
                classification, context, model=model
            )
        return await decide_corrective_action(classification, context, model=model)

    async def _compose_redirect_message(self, agent_role: str, issue: str, context: dict) -> str:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(self._decision_maker, "compose_redirect_message"):
            return await self._decision_maker.compose_redirect_message(
                agent_role, issue, context, model=model
            )
        return await compose_redirect_message(agent_role, issue, context, model=model)

    async def _decide_escalation_level(
        self, classification: dict, redirect_history: list[dict], context: dict | None = None
    ) -> dict:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(self._decision_maker, "decide_escalation_level"):
            return await self._decision_maker.decide_escalation_level(
                classification, redirect_history, context=context, model=model
            )
        return await decide_escalation_level(
            classification, redirect_history, context=context, model=model
        )

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def start(self) -> None:
        """Start the monitoring loop.

        Runs until :meth:`stop` is called or the pipeline reaches a
        terminal state (``complete``, ``failed``, or ``cancelled``).
        """
        self._running = True
        logger.info("Overseer monitor started for pipeline %s", self.pipeline_id)

        while self._running:
            await self._poll_cycle()
            poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)
            await asyncio.sleep(poll_interval)

    async def stop(self) -> None:
        """Stop the monitoring loop and write final health summary."""
        self._running = False
        self.write_health_summary()
        logger.info("Overseer monitor stopped for pipeline %s", self.pipeline_id)

    # -----------------------------------------------------------------
    # Core poll cycle
    # -----------------------------------------------------------------

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
            if status in ("complete", "failed", "cancelled"):
                # Validate PR phase outcome before shutting down
                if status == "complete":
                    await self._check_pr_phase_outcome(pipeline_data)

                self._log_oversight_event(
                    {
                        "event": "pipeline_terminal",
                        "status": status,
                    }
                )
                self._running = False
                self.write_health_summary()

            # 9. Cross-phase consistency (LLM-based, only on phase transitions)
            if status not in ("complete", "failed", "cancelled"):
                await self._check_cross_phase_consistency(
                    current_phase, decisions, contract_data=None
                )

            # 10. Check for post-consensus stall
            await self._check_post_consensus_stall(consensus, status)

            # 11. Check for incomplete consensus stall (#1471)
            await self._check_incomplete_consensus_stall(consensus, status)

        except Exception:
            logger.exception("Error in overseer poll cycle")

        # 12. Update self-monitoring
        duration = time.time() - cycle_start
        self.self_monitor.record_poll_cycle(duration)

    # -----------------------------------------------------------------
    # Escalation handling
    # -----------------------------------------------------------------

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

        # Check redirect history for this agent
        history = list(self._escalation_history.get(agent_role, []))
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
            }
        )

    # -----------------------------------------------------------------
    # Action execution
    # -----------------------------------------------------------------

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
            if any(hw in msg_lower for hw in _HUMAN_WORDS) and any(
                aw in msg_lower for aw in _ACTION_WORDS
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
            issue_context: dict = {"pipeline_id": self.pipeline_id}
            if container_logs:
                issue_context["container_logs"] = container_logs[-4000:]
            await file_diagnostic_issue(
                pipeline_id=self.pipeline_id,
                agent_role=agent_role,
                anomaly={"type": "escalation", "description": message},
                context=issue_context,
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

    # -----------------------------------------------------------------
    # CLI wrappers
    # -----------------------------------------------------------------

    async def _run_cli(self, *args: str, timeout: float = 15) -> tuple[int, str, str]:
        """Run a CLI command asynchronously.

        Returns:
            Tuple of (returncode, stdout, stderr).
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", "timeout"
        return proc.returncode or 0, (stdout_bytes or b"").decode(), (stderr_bytes or b"").decode()

    async def _query_progress(self) -> list[dict]:
        """Query progress events from the orchestrator."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "progress",
                "query",
                "--pipeline",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                return data if isinstance(data, list) else [data]
        except Exception:
            logger.debug("Failed to query progress events", exc_info=True)
        return []

    async def _query_health_alerts(self) -> list[dict]:
        """Query active health alerts from the orchestrator."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "health",
                "alerts",
                "--pipeline",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                return data if isinstance(data, list) else [data]
        except Exception:
            logger.debug("Failed to query health alerts", exc_info=True)
        return []

    async def _poll_escalation_messages(self) -> list[dict]:
        """Poll for escalation messages directed to the overseer."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "message",
                "poll",
                "--role",
                "overseer",
                "--wait",
                "5",
                "--json",
                timeout=20,
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and data.get("messages"):
                    return data["messages"]
        except Exception:
            logger.debug("Failed to poll escalation messages", exc_info=True)
        return []

    async def _query_pipeline_data(self) -> dict:
        """Query the full pipeline status data.

        Returns:
            Pipeline status dict, or empty dict on failure.
        """
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "pipeline",
                "status",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.debug("Failed to query pipeline status", exc_info=True)
        return {}

    async def _query_consensus_status(self) -> dict:
        """Query current BRC consensus state from the orchestrator."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "consensus",
                "status",
                "--pipeline",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.debug("Failed to query consensus status", exc_info=True)
        return {}

    async def _query_current_phase(self) -> dict:
        """Query current phase name and status from the orchestrator."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "phase",
                "get",
                "--pipeline",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.debug("Failed to query current phase", exc_info=True)
        return {}

    async def _query_decisions(self) -> list[dict]:
        """Query all decisions (including resolved) for the pipeline."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "decision",
                "list",
                "--pipeline",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and data.get("decisions"):
                    return data["decisions"]
        except Exception:
            logger.debug("Failed to query decisions", exc_info=True)
        return []

    async def _query_contract_data(self) -> dict:
        """Query SDLC contract state via egg-contract show."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-contract",
                "show",
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.debug("Failed to query contract data", exc_info=True)
        return {}

    async def _query_container_list(self) -> list[dict]:
        """List containers for the pipeline via ``egg-orch container list``."""
        try:
            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "container",
                "list",
                self.pipeline_id,
                "--json",
            )
            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return data.get("data", {}).get("containers", [])
                if isinstance(data, list):
                    return data
        except Exception:
            logger.debug("Failed to list containers", exc_info=True)
        return []

    async def _query_container_logs(self, agent_role: str, tail: int = 200) -> str:
        """Fetch recent container logs for an agent role.

        Auto-selects the best container for the role: prefers running
        containers, then falls back to the most recently started one.

        Args:
            agent_role: The agent role whose container logs to fetch.
            tail: Number of log lines from the end (default 200).

        Returns:
            Log output as a string, or empty string on failure.
        """
        try:
            containers = await self._query_container_list()
            if not containers:
                return ""

            # Filter to containers matching the target agent role
            role_containers = [c for c in containers if c.get("agent_role") == agent_role]
            if not role_containers:
                return ""

            # Prefer running containers (newest first), then most recently started
            running = [c for c in role_containers if c.get("status") == "running"]
            if running:
                running.sort(key=lambda c: c.get("started_at", ""), reverse=True)
                selected = running[0]
            else:
                role_containers.sort(key=lambda c: c.get("started_at", ""), reverse=True)
                selected = role_containers[0]

            container_id = selected.get("container_id", "")
            if not container_id:
                return ""

            rc, stdout, _ = await self._run_cli(
                "egg-orch",
                "container",
                "logs",
                self.pipeline_id,
                container_id,
                "--lines",
                str(tail),
                "--json",
                timeout=30,
            )
            if rc == 0 and stdout.strip():
                try:
                    data = json.loads(stdout)
                except json.JSONDecodeError:
                    return stdout.strip()
                if isinstance(data, dict):
                    return data.get("data", {}).get("logs", data.get("logs", ""))
                return stdout.strip()
        except Exception:
            logger.debug(
                "Failed to fetch container logs for %s",
                agent_role,
                exc_info=True,
            )
        return ""

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

    def _load_pipeline_for_transition_check(self):
        """Load the current Pipeline model for the post-consensus-stall
        short-circuit check (#1911).

        Uses ``state_store.get_state_store`` keyed on ``EGG_REPO_PATH``
        so a missing env var or missing state-store module degrades
        gracefully to ``None`` — the detector then falls through to the
        existing grace-period logic (fail open).  Tests patch
        ``overseer.monitor._get_state_store`` to inject a fake store.
        """
        try:
            if _get_state_store is None:
                return None
            repo_path = os.environ.get("EGG_REPO_PATH")
            if not repo_path:
                return None
            store = _get_state_store(repo_path)
            return store.load_pipeline(self.pipeline_id)
        except Exception:
            # Fail open — see the caller's docstring.  Any failure to
            # load the pipeline must not suppress a real stall alert.
            logger.debug(
                "Post-consensus stall: failed to load pipeline for short-circuit check",
                exc_info=True,
            )
            return None

    async def _check_post_consensus_stall(self, consensus: dict, pipeline_status_str: str) -> None:
        """Detect and escalate when consensus is complete but phase hasn't transitioned.

        Includes deduplication (only fires once) and a grace period of 3 poll
        cycles to avoid false positives during normal phase transitions.

        Args:
            consensus: Current consensus status dict.
            pipeline_status_str: Current pipeline status string (e.g. "running").
        """
        if not consensus.get("is_complete"):
            # Consensus not complete — reset tracking state
            self._post_consensus_stall_reported = False
            self._post_consensus_stall_first_seen = None
            return
        if pipeline_status_str != "running":
            return

        # Short-circuit on any transition-completion evidence (#1911).
        # The "post-consensus-push-stall" detector fires during the
        # ~90s implement→pr handoff window and previously mis-classified
        # a legitimate transition as a stall.  If any of the following
        # hold, the transition is clearly underway or complete, so the
        # detector must stay silent:
        #   (a) current_phase has already advanced past "implement"
        #   (b) pipeline.pr_number has been populated (PR was created)
        #   (c) phases["pr"].artifacts["pr_url"] is set
        # We fall open on *any* exception so we never mask a genuine
        # stall on a bug in the short-circuit.
        pipeline = self._load_pipeline_for_transition_check()
        if pipeline is not None:
            try:
                current_phase_value = getattr(
                    getattr(pipeline, "current_phase", None), "value", None
                )
                pr_number = getattr(pipeline, "pr_number", None)
                phases = getattr(pipeline, "phases", None) or {}
                pr_phase = phases.get("pr") if hasattr(phases, "get") else None
                pr_artifacts = getattr(pr_phase, "artifacts", None) if pr_phase else None
                pr_url_artifact = (
                    pr_artifacts.get("pr_url") if isinstance(pr_artifacts, dict) else None
                )
                if (
                    (current_phase_value and current_phase_value != "implement")
                    or pr_number is not None
                    or pr_url_artifact
                ):
                    # Reset first-seen so a genuinely subsequent stall
                    # still gets its own grace period.
                    self._post_consensus_stall_first_seen = None
                    return
            except Exception:
                # Fail open — don't suppress alerts on a bug in the short-circuit.
                logger.debug(
                    "Post-consensus stall short-circuit check raised; falling through",
                    exc_info=True,
                )

        # Already escalated — don't spam
        if self._post_consensus_stall_reported:
            return

        # Grace period: wait 3 poll cycles before escalating to allow
        # normal phase transition to complete
        poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)
        grace_seconds = poll_interval * 3
        now = time.time()

        if self._post_consensus_stall_first_seen is None:
            self._post_consensus_stall_first_seen = now
            return

        if (now - self._post_consensus_stall_first_seen) < grace_seconds:
            return

        logger.warning(
            "Post-consensus stall detected for pipeline %s: "
            "consensus complete but pipeline still running",
            self.pipeline_id,
        )

        message = (
            "All agents confirmed consensus but the pipeline phase has not "
            "transitioned. Possible orchestrator transition failure. "
            f"Pipeline: {self.pipeline_id}"
        )

        # Broadcast alert so /sdlc monitoring session can surface it
        await self._broadcast_alert("post_consensus_stall", "orchestrator", message, "high")

        # Create HITL decision for human attention
        await self._create_hitl_decision("orchestrator", message)

        # Also send Slack notification for visibility
        await self._send_slack_notification("orchestrator", message)

        self._post_consensus_stall_reported = True

        self._log_oversight_event(
            {
                "event": "post_consensus_stall",
                "consensus": consensus,
                "pipeline_status": pipeline_status_str,
            }
        )

    # -----------------------------------------------------------------
    # Activity-aware helpers (#1609)
    # -----------------------------------------------------------------

    def _blocking_agents_are_active(self, blocking_agents: list[str]) -> bool:
        """Check if any blocking agents have recent progress events."""
        try:
            from progress_store import get_progress_store

            store = get_progress_store()
            window = getattr(self.config, "active_agent_stall_extension_seconds", 120)
            cutoff = datetime.now(UTC) - timedelta(seconds=window)
            for agent in blocking_agents:
                events = store.get_events(self.pipeline_id, agent_role=agent, since=cutoff)
                if events:
                    return True
        except Exception:
            logger.debug("Failed to check agent activity", exc_info=True)
        return False

    def _get_recent_proposal_age(self) -> float | None:
        """Return seconds since the most recent CONSENSUS_PROPOSE, or None."""
        try:
            from peer_consensus import get_peer_consensus_tracker

            tracker = get_peer_consensus_tracker(self.pipeline_id)
            if tracker is not None:
                latest = tracker.get_latest_proposal_timestamp()
                if isinstance(latest, datetime):
                    return (datetime.now(UTC) - latest).total_seconds()
        except Exception:
            logger.debug("Failed to query proposal age", exc_info=True)
        return None

    # -----------------------------------------------------------------
    # Incomplete consensus stall detection (issue #1471)
    # -----------------------------------------------------------------

    async def _check_incomplete_consensus_stall(
        self, consensus: dict, pipeline_status_str: str
    ) -> None:
        """Detect and nudge when consensus is incomplete with stuck blocking agents.

        When one or more agents have not confirmed for an extended period while
        other agents have, this method sends a targeted nudge to the blocking
        agents. If the nudge doesn't resolve the stall, escalates to HITL.

        Args:
            consensus: Current consensus status dict from ``_query_consensus_status()``.
            pipeline_status_str: Current pipeline status string (e.g. "running").
        """
        if pipeline_status_str != "running":
            return

        # If consensus is complete or empty, reset and skip
        if not consensus or consensus.get("is_complete"):
            self._reset_incomplete_consensus_tracking()
            return

        blocking_agents = consensus.get("blocking_agents", [])
        if not blocking_agents:
            self._reset_incomplete_consensus_tracking()
            return

        current_blocking = frozenset(blocking_agents)
        now = time.time()

        # If blocking set changed, reset tracking
        if current_blocking != self._incomplete_consensus_blocking:
            self._reset_incomplete_consensus_tracking()
            self._incomplete_consensus_blocking = current_blocking
            self._incomplete_consensus_first_seen = now
            self._incomplete_consensus_absolute_start = now
            return

        if self._incomplete_consensus_first_seen is None:
            self._incomplete_consensus_first_seen = now
            self._incomplete_consensus_absolute_start = now
            return

        # Post-proposal grace: reset if a recent proposal arrived (#1609)
        post_proposal_grace = getattr(self.config, "post_proposal_grace_seconds", 300)
        proposal_age = self._get_recent_proposal_age()
        if proposal_age is not None and proposal_age < post_proposal_grace:
            logger.debug(
                "Recent CONSENSUS_PROPOSE (%.0fs ago) — deferring incomplete consensus check",
                proposal_age,
            )
            self._reset_incomplete_consensus_tracking()
            self._incomplete_consensus_blocking = current_blocking
            self._incomplete_consensus_first_seen = now
            self._incomplete_consensus_absolute_start = now  # restart deferral cap
            return

        elapsed = now - self._incomplete_consensus_first_seen
        poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)

        # Nudge threshold: 10 poll cycles (~5 min at 30s interval)
        nudge_threshold = poll_interval * 10
        # HITL threshold: 20 poll cycles (~10 min at 30s interval)
        hitl_threshold = poll_interval * 20

        if elapsed >= hitl_threshold and not self._incomplete_consensus_hitl_created:
            # Activity check: skip HITL escalation if agents are active (#1609)
            # Cap total deferral at 2x HITL threshold to prevent indefinite suppression
            absolute_elapsed = now - (self._incomplete_consensus_absolute_start or now)
            max_deferral = hitl_threshold * 2
            if absolute_elapsed < max_deferral and self._blocking_agents_are_active(
                sorted(blocking_agents)
            ):
                logger.info(
                    "Incomplete consensus: blocking agents still active, deferring HITL"
                    " (pipeline=%s, blocking=%s, absolute_elapsed=%ds, max_deferral=%ds)",
                    self.pipeline_id,
                    sorted(blocking_agents),
                    round(absolute_elapsed),
                    round(max_deferral),
                )
                return

            # Nudge didn't resolve — escalate to HITL
            logger.warning(
                "Incomplete consensus stall persists after nudge — escalating to HITL"
                " (pipeline=%s, blocking=%s, elapsed=%ds)",
                self.pipeline_id,
                sorted(blocking_agents),
                round(elapsed),
            )
            message = (
                f"Consensus incomplete for {round(elapsed)}s. "
                f"Blocking agents: {', '.join(sorted(blocking_agents))}. "
                f"These agents were nudged but have not re-confirmed. "
                f"Pipeline: {self.pipeline_id}"
            )
            await self._create_hitl_decision("orchestrator", message)
            await self._send_slack_notification("orchestrator", message)
            self._incomplete_consensus_hitl_created = True

            self._log_oversight_event(
                {
                    "event": "incomplete_consensus_hitl",
                    "blocking_agents": sorted(blocking_agents),
                    "elapsed_seconds": round(elapsed),
                }
            )

        elif elapsed >= nudge_threshold and not self._incomplete_consensus_nudged:
            # Activity check: defer nudge if agents are active (#1609)
            # Cap nudge deferrals: don't defer past HITL threshold from absolute start
            absolute_elapsed = now - (self._incomplete_consensus_absolute_start or now)
            if absolute_elapsed < hitl_threshold and self._blocking_agents_are_active(
                sorted(blocking_agents)
            ):
                logger.info(
                    "Incomplete consensus: blocking agents are active, deferring nudge"
                    " (pipeline=%s, blocking=%s, absolute_elapsed=%ds)",
                    self.pipeline_id,
                    sorted(blocking_agents),
                    round(absolute_elapsed),
                )
                # Reset first_seen to extend the window
                self._incomplete_consensus_first_seen = now
                self._log_oversight_event(
                    {
                        "event": "incomplete_consensus_activity_extension",
                        "blocking_agents": sorted(blocking_agents),
                    }
                )
                return

            # Send targeted nudge to each blocking agent
            logger.info(
                "Incomplete consensus stall detected — nudging blocking agents"
                " (pipeline=%s, blocking=%s, elapsed=%ds)",
                self.pipeline_id,
                sorted(blocking_agents),
                round(elapsed),
            )
            for agent_role in sorted(blocking_agents):
                nudge_message = (
                    f"You are blocking consensus for pipeline {self.pipeline_id}. "
                    f"Your confirmed status may have been cleared after a re-review "
                    f"cycle. Please re-confirm via `egg-orch consensus confirmed`, "
                    f"or if you are a reviewer of a re-proposing producer, re-review "
                    f"and ACK/NACK the latest proposal then confirm."
                )
                await self._send_message(agent_role, nudge_message)
                self.self_monitor.record_message_sent()

            await self._broadcast_alert(
                "incomplete_consensus_stall",
                ", ".join(sorted(blocking_agents)),
                f"Consensus incomplete for {round(elapsed)}s. "
                f"Blocking agents: {', '.join(sorted(blocking_agents))}. "
                f"Nudge sent.",
                "medium",
            )
            self._incomplete_consensus_nudged = True

            self._log_oversight_event(
                {
                    "event": "incomplete_consensus_nudge",
                    "blocking_agents": sorted(blocking_agents),
                    "elapsed_seconds": round(elapsed),
                }
            )

    def _reset_incomplete_consensus_tracking(self) -> None:
        """Reset all incomplete-consensus stall tracking state."""
        self._incomplete_consensus_first_seen = None
        self._incomplete_consensus_blocking = None
        self._incomplete_consensus_nudged = False
        self._incomplete_consensus_hitl_created = False
        self._incomplete_consensus_absolute_start = None

    # -----------------------------------------------------------------
    # Orchestrator reachability (issue #1371)
    # -----------------------------------------------------------------

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
            await self._broadcast_alert(
                "orchestrator_unreachable", "orchestrator", message, "critical"
            )
            await self._send_slack_notification("orchestrator", message)

    # -----------------------------------------------------------------
    # Health checks (issue #1297)
    # -----------------------------------------------------------------

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

    async def _check_pr_phase_outcome(self, pipeline_data: dict) -> None:
        """Safety-net check: detect pipeline completing without a PR.

        This is defense-in-depth for edge cases that escape the primary failure
        handling in ``_auto_create_pr`` (which sets the pipeline to FAILED when
        PR creation returns no URL).  If a pipeline somehow reaches ``complete``
        status with ``current_phase=pr`` but no ``pr_url`` in phase artifacts,
        this surfaces the issue via a HITL decision and Slack notification so
        that stranded work on the branch is not silently lost.
        """
        current_phase = pipeline_data.get("current_phase", "")
        if current_phase != "pr":
            return

        phases = pipeline_data.get("phases", {})
        pr_phase = phases.get("pr", {})
        artifacts = pr_phase.get("artifacts") or {}
        pr_url = artifacts.get("pr_url")

        if pr_url:
            return

        message = (
            f"PR phase completed without creating a PR: no pr_url in phase artifacts. "
            f"Work may be stranded on the branch. "
            f"Pipeline: {self.pipeline_id}"
        )
        logger.error(
            "PR phase completed without PR for pipeline %s",
            self.pipeline_id,
        )
        self._log_oversight_event(
            {"event": "pr_phase_no_pr", "current_phase": current_phase, "artifacts": artifacts}
        )
        await self._broadcast_alert("pr_phase_no_pr", "orchestrator", message, "critical")
        await self._create_hitl_decision("orchestrator", message)
        await self._send_slack_notification("orchestrator", message)

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

    # -----------------------------------------------------------------
    # Infrastructure error deduplication (#1489)
    # -----------------------------------------------------------------

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

    # -----------------------------------------------------------------
    # Health summary
    # -----------------------------------------------------------------

    def generate_health_summary(self) -> str:
        """Generate a pipeline health summary at completion.

        Returns:
            A markdown-formatted health summary string.
        """
        self_health = self.self_monitor.check_health()
        metrics = self_health["metrics"]

        escalation_summary_lines: list[str] = []
        for agent_role, history in self._escalation_history.items():
            actions = [h.get("action", "?") for h in history]
            escalation_summary_lines.append(
                f"- **{agent_role}**: {len(history)} escalation(s) -- actions: {', '.join(actions)}"
            )

        escalation_text = (
            "\n".join(escalation_summary_lines)
            if escalation_summary_lines
            else "- No escalations during pipeline run"
        )

        self_health_text = ""
        if self_health["concerns"]:
            concerns = "\n".join(f"  - {c}" for c in self_health["concerns"])
            self_health_text = f"\n### Overseer Self-Health Concerns\n{concerns}\n"

        return f"""## Pipeline Health Summary

**Pipeline**: `{self.pipeline_id}`
**Monitor cycles**: {metrics["cycle_count"]}
**Total messages sent**: {metrics["total_messages"]}
**LLM calls**: {metrics["total_llm_calls"]} (${metrics["total_llm_cost_usd"]:.4f})

### Escalation History
{escalation_text}
{self_health_text}
### Metrics
- Avg poll duration: {metrics["avg_poll_duration_seconds"]:.2f}s
- Max poll duration: {metrics["max_poll_duration_seconds"]:.2f}s
- Hourly LLM cost: ${metrics["hourly_llm_cost_usd"]:.4f}
"""
