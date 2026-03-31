"""Main overseer monitoring loop.

Implements the continuous poll-classify-decide-act cycle that runs for
the lifetime of a pipeline.  The monitor queries the orchestrator for
progress events and health alerts, routes anomalies through the
classifier tier, and executes corrective actions via the decision tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
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
    ) -> dict:
        if self._classifier and hasattr(self._classifier, "classify_stall"):
            return await self._classifier.classify_stall(logs, progress, consensus=consensus)
        return await classify_stall(logs, progress, consensus=consensus)

    async def _classify_error(self, error_context: dict) -> dict:
        if self._classifier and hasattr(self._classifier, "classify_error"):
            return await self._classifier.classify_error(error_context)
        return await classify_error(error_context)

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
        self, classification: dict, redirect_history: list[dict]
    ) -> dict:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(self._decision_maker, "decide_escalation_level"):
            return await self._decision_maker.decide_escalation_level(
                classification, redirect_history, model=model
            )
        return await decide_escalation_level(classification, redirect_history, model=model)

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
            11. Update self-monitoring
        """
        cycle_start = time.time()

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

            # 6 & 7. Process any anomalies (with consensus context)
            for alert in alerts:
                agent_role = alert.get("agent_role", alert.get("agent_id", "unknown"))
                classification = await self._classify_stall(
                    logs=alert.get("logs", []),
                    progress=progress_events,
                    consensus=consensus or None,
                )
                decision = await self._decide_corrective_action(
                    classification,
                    {"alert": alert, "pipeline_id": self.pipeline_id},
                )
                await self._execute_action(decision, agent_role)
                # Resolve the alert so it doesn't accumulate (#1428)
                await self._resolve_alert(
                    agent_id=alert.get("agent_id", agent_role),
                    alert_type=alert.get("alert_type", "unknown"),
                )

            # Process escalation messages (with consensus context)
            for escalation in escalations:
                await self.handle_escalation(escalation, consensus=consensus)

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

        except Exception:
            logger.exception("Error in overseer poll cycle")

        # 11. Update self-monitoring
        duration = time.time() - cycle_start
        self.self_monitor.record_poll_cycle(duration)

    # -----------------------------------------------------------------
    # Escalation handling
    # -----------------------------------------------------------------

    async def handle_escalation(self, escalation: dict, consensus: dict | None = None) -> None:
        """Handle an escalation from the orchestrator's tripwire processor.

        Implements a hallucination guard: the Sonnet decision tier only
        acts on data that has been classified by the Haiku tier first.

        Args:
            escalation: Dict with escalation details from the orchestrator.
            consensus: Optional current BRC consensus status.
        """
        agent_role = escalation.get("agent_role", escalation.get("agent_id", "unknown"))

        # Hallucination guard: always classify first, then decide
        classification = await self._classify_stall(
            logs=escalation.get("logs", []),
            progress=escalation.get("progress", []),
            consensus=consensus,
        )

        # Check redirect history for this agent
        history = list(self._escalation_history.get(agent_role, []))
        max_redirects = getattr(self.config, "overseer_max_redirects_before_escalation", 2)

        redirect_count = sum(1 for h in history if h.get("action") == "redirect")

        if redirect_count >= max_redirects:
            # Too many redirects -- escalate
            escalation_decision = await self._decide_escalation_level(classification, history)
            decision = {
                "action": escalation_decision.get("level", "hitl"),
                "message": escalation_decision.get("reasoning", ""),
                "priority": "high",
            }
        else:
            decision = await self._decide_corrective_action(
                classification,
                {"escalation": escalation, "pipeline_id": self.pipeline_id},
            )

        await self._execute_action(decision, agent_role)

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

    async def _execute_action(self, decision: dict, agent_role: str) -> None:
        """Execute a corrective action based on a decision.

        Includes a safety net: if the decision message indicates human
        intervention is required but the action is only ``nudge`` or
        ``redirect``, the action is upgraded to ``hitl``.

        Args:
            decision: Output from decide_corrective_action.
            agent_role: The target agent role.
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
            await file_diagnostic_issue(
                pipeline_id=self.pipeline_id,
                agent_role=agent_role,
                anomaly={"type": "escalation", "description": message},
                context={"pipeline_id": self.pipeline_id},
            )

        elif action == "slack":
            await self._send_slack_notification(agent_role, message)

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
            except (ValueError, TypeError):
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
                except (ValueError, TypeError):
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

        # Phase changed — run consistency check (deduplicate per transition pair)
        previous_phase = self._last_phase_name
        self._last_phase_name = current_phase_name

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
                "--to",
                agent_role,
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
                "--agent-id",
                agent_id,
                "--alert-type",
                alert_type,
            )
        except Exception:
            logger.debug(
                "Failed to resolve alert %s for %s", alert_type, agent_id, exc_info=True
            )

    async def _create_hitl_decision(self, agent_role: str, message: str) -> None:
        """Create a HITL decision for an agent issue."""
        try:
            await self._run_cli(
                "egg-orch",
                "decision",
                "create",
                "--question",
                f"Agent {agent_role} issue: {message}",
                "--options",
                "Restart agent",
                "Continue monitoring",
                "Cancel pipeline",
            )
        except Exception:
            logger.debug("Failed to create HITL decision for %s", agent_role, exc_info=True)

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
