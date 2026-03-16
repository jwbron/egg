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
import subprocess
import time
from typing import Any

from overseer.classifier import (
    check_alignment,
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


class _DefaultConfig:
    """Fallback config when no PipelineConfig is provided."""

    overseer_poll_interval_seconds: int = 30
    overseer_max_redirects_before_escalation: int = 2
    overseer_decision_maker_model: str = "sonnet"


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
        self._escalation_history: dict[str, list] = {}  # agent_role -> list of escalations

        # Allow dependency injection for testing
        self._classifier = classifier
        self._decision_maker = decision_maker

    # -----------------------------------------------------------------
    # Classifier / decision maker accessors
    # -----------------------------------------------------------------

    async def _classify_stall(self, logs: list[dict], progress: list[dict]) -> dict:
        if self._classifier and hasattr(self._classifier, "classify_stall"):
            return await self._classifier.classify_stall(logs, progress)
        return await classify_stall(logs, progress)

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

    async def _decide_corrective_action(
        self, classification: dict, context: dict
    ) -> dict:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(
            self._decision_maker, "decide_corrective_action"
        ):
            return await self._decision_maker.decide_corrective_action(
                classification, context, model=model
            )
        return await decide_corrective_action(classification, context, model=model)

    async def _compose_redirect_message(
        self, agent_role: str, issue: str, context: dict
    ) -> str:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(
            self._decision_maker, "compose_redirect_message"
        ):
            return await self._decision_maker.compose_redirect_message(
                agent_role, issue, context, model=model
            )
        return await compose_redirect_message(agent_role, issue, context, model=model)

    async def _decide_escalation_level(
        self, classification: dict, redirect_history: list[dict]
    ) -> dict:
        model = getattr(self.config, "overseer_decision_maker_model", "sonnet")
        if self._decision_maker and hasattr(
            self._decision_maker, "decide_escalation_level"
        ):
            return await self._decision_maker.decide_escalation_level(
                classification, redirect_history, model=model
            )
        return await decide_escalation_level(
            classification, redirect_history, model=model
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
            poll_interval = getattr(
                self.config, "overseer_poll_interval_seconds", 30
            )
            await asyncio.sleep(poll_interval)

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        logger.info("Overseer monitor stopped for pipeline %s", self.pipeline_id)

    # -----------------------------------------------------------------
    # Core poll cycle
    # -----------------------------------------------------------------

    async def _poll_cycle(self) -> None:
        """Execute a single monitoring cycle.

        Steps:
            1. Query progress events
            2. Query health alerts
            3. Check for escalation messages from orchestrator
            4. Route anomalies through classifier
            5. Route classified results through decision maker
            6. Execute corrective actions
            7. Update self-monitoring
        """
        cycle_start = time.time()

        try:
            # 1. Query progress events
            progress_events = await self._query_progress()

            # 2. Query health alerts
            alerts = await self._query_health_alerts()

            # 3. Check for escalation messages
            escalations = await self._poll_escalation_messages()

            # 4 & 5. Process any anomalies
            for alert in alerts:
                agent_role = alert.get("agent_role", alert.get("agent_id", "unknown"))
                classification = await self._classify_stall(
                    logs=alert.get("logs", []),
                    progress=progress_events,
                )
                decision = await self._decide_corrective_action(
                    classification,
                    {"alert": alert, "pipeline_id": self.pipeline_id},
                )
                await self._execute_action(decision, agent_role)

            # Process escalation messages
            for escalation in escalations:
                await self.handle_escalation(escalation)

            # Check pipeline status for terminal state
            status = await self._query_pipeline_status()
            if status in ("complete", "failed", "cancelled"):
                self._running = False

        except Exception:
            logger.exception("Error in overseer poll cycle")

        # 7. Update self-monitoring
        duration = time.time() - cycle_start
        self.self_monitor.record_poll_cycle(duration)

    # -----------------------------------------------------------------
    # Escalation handling
    # -----------------------------------------------------------------

    async def handle_escalation(self, escalation: dict) -> None:
        """Handle an escalation from the orchestrator's tripwire processor.

        Implements a hallucination guard: the Sonnet decision tier only
        acts on data that has been classified by the Haiku tier first.

        Args:
            escalation: Dict with escalation details from the orchestrator.
        """
        agent_role = escalation.get("agent_role", escalation.get("agent_id", "unknown"))

        # Hallucination guard: always classify first, then decide
        classification = await self._classify_stall(
            logs=escalation.get("logs", []),
            progress=escalation.get("progress", []),
        )

        # Check redirect history for this agent
        history = self._escalation_history.get(agent_role, [])
        max_redirects = getattr(
            self.config, "overseer_max_redirects_before_escalation", 2
        )

        redirect_count = sum(
            1 for h in history if h.get("action") == "redirect"
        )

        if redirect_count >= max_redirects:
            # Too many redirects -- escalate
            escalation_decision = await self._decide_escalation_level(
                classification, history
            )
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

        # Record in escalation history
        self._escalation_history.setdefault(agent_role, []).append(
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

        Args:
            decision: Output from decide_corrective_action.
            agent_role: The target agent role.
        """
        action = decision.get("action", "nudge")
        message = decision.get("message", "")

        logger.info(
            "Executing %s action for %s in pipeline %s: %s",
            action,
            agent_role,
            self.pipeline_id,
            message[:100],
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

    async def _query_progress(self) -> list[dict]:
        """Query progress events from the orchestrator."""
        try:
            result = subprocess.run(
                [
                    "egg-orch", "progress", "query",
                    "--pipeline", self.pipeline_id,
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                return data if isinstance(data, list) else [data]
        except Exception:
            logger.debug("Failed to query progress events", exc_info=True)
        return []

    async def _query_health_alerts(self) -> list[dict]:
        """Query active health alerts from the orchestrator."""
        try:
            result = subprocess.run(
                [
                    "egg-orch", "health", "alerts",
                    "--pipeline", self.pipeline_id,
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                return data if isinstance(data, list) else [data]
        except Exception:
            logger.debug("Failed to query health alerts", exc_info=True)
        return []

    async def _poll_escalation_messages(self) -> list[dict]:
        """Poll for escalation messages directed to the overseer."""
        try:
            result = subprocess.run(
                [
                    "egg-orch", "message", "poll",
                    "--role", "overseer",
                    "--wait", "5",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and data.get("messages"):
                    return data["messages"]
        except Exception:
            logger.debug("Failed to poll escalation messages", exc_info=True)
        return []

    async def _query_pipeline_status(self) -> str:
        """Query the current pipeline status."""
        try:
            result = subprocess.run(
                [
                    "egg-orch", "pipeline", "status",
                    self.pipeline_id,
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    return data.get("status", "running")
        except Exception:
            logger.debug("Failed to query pipeline status", exc_info=True)
        return "running"

    async def _send_message(self, agent_role: str, message: str) -> None:
        """Send a message to an agent via the orchestrator."""
        try:
            subprocess.run(
                [
                    "egg-orch", "message", "send",
                    "--to", agent_role,
                    "--subject", "Overseer health check",
                    "--body", message,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            logger.debug("Failed to send message to %s", agent_role, exc_info=True)

    async def _create_hitl_decision(self, agent_role: str, message: str) -> None:
        """Create a HITL decision for an agent issue."""
        try:
            subprocess.run(
                [
                    "egg-orch", "decision", "create",
                    "--question",
                    f"Agent {agent_role} issue: {message}",
                    "--options", "Restart agent", "Continue monitoring", "Cancel pipeline",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            logger.debug(
                "Failed to create HITL decision for %s", agent_role, exc_info=True
            )

    async def _send_slack_notification(
        self, agent_role: str, message: str
    ) -> None:
        """Send a Slack notification about an agent issue."""
        try:
            import pathlib
            import datetime

            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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
                f"- **{agent_role}**: {len(history)} escalation(s) -- "
                f"actions: {', '.join(actions)}"
            )

        escalation_text = (
            "\n".join(escalation_summary_lines) if escalation_summary_lines
            else "- No escalations during pipeline run"
        )

        self_health_text = ""
        if self_health["concerns"]:
            concerns = "\n".join(f"  - {c}" for c in self_health["concerns"])
            self_health_text = f"\n### Overseer Self-Health Concerns\n{concerns}\n"

        return f"""## Pipeline Health Summary

**Pipeline**: `{self.pipeline_id}`
**Monitor cycles**: {metrics['cycle_count']}
**Total messages sent**: {metrics['total_messages']}
**LLM calls**: {metrics['total_llm_calls']} (${metrics['total_llm_cost_usd']:.4f})

### Escalation History
{escalation_text}
{self_health_text}
### Metrics
- Avg poll duration: {metrics['avg_poll_duration_seconds']:.2f}s
- Max poll duration: {metrics['max_poll_duration_seconds']:.2f}s
- Hourly LLM cost: ${metrics['hourly_llm_cost_usd']:.4f}
"""
