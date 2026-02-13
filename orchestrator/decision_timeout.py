"""
Timeout and escalation handling for stale decisions.

Provides background monitoring for decision timeouts and
escalation to human notification channels.
"""

import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from decision_queue import get_decision_queue
from models import DecisionStatus, HITLDecision

logger = get_logger("orchestrator.decision_timeout")


EscalationHandler = Callable[[str, HITLDecision], None]


class DecisionTimeoutMonitor:
    """Monitors decisions for timeouts and triggers escalations.

    Runs a background thread that periodically checks for stale
    decisions and invokes escalation handlers.
    """

    def __init__(
        self,
        check_interval: int = 60,
        warning_threshold_percent: float = 0.75,
    ):
        """Initialize timeout monitor.

        Args:
            check_interval: Seconds between checks
            warning_threshold_percent: Fraction of timeout before warning (0-1)
        """
        self.check_interval = check_interval
        self.warning_threshold_percent = warning_threshold_percent

        self._pipelines: dict[str, Path] = {}
        self._handlers: list[EscalationHandler] = []
        self._warned_decisions: set[str] = set()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def register_pipeline(self, pipeline_id: str, repo_path: Path) -> None:
        """Register a pipeline for timeout monitoring.

        Args:
            pipeline_id: Pipeline ID
            repo_path: Path to repository
        """
        with self._lock:
            self._pipelines[pipeline_id] = repo_path

    def unregister_pipeline(self, pipeline_id: str) -> None:
        """Unregister a pipeline from monitoring.

        Args:
            pipeline_id: Pipeline ID
        """
        with self._lock:
            if pipeline_id in self._pipelines:
                del self._pipelines[pipeline_id]

    def add_escalation_handler(self, handler: EscalationHandler) -> None:
        """Add an escalation handler.

        Args:
            handler: Function called on timeout/warning
        """
        with self._lock:
            self._handlers.append(handler)

    def remove_escalation_handler(self, handler: EscalationHandler) -> None:
        """Remove an escalation handler.

        Args:
            handler: Handler to remove
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _escalate(self, pipeline_id: str, decision: HITLDecision) -> None:
        """Escalate a decision to all handlers.

        Args:
            pipeline_id: Pipeline ID
            decision: Decision to escalate
        """
        with self._lock:
            handlers = self._handlers.copy()

        for handler in handlers:
            try:
                handler(pipeline_id, decision)
            except Exception as e:
                logger.error(
                    "Escalation handler error",
                    pipeline_id=pipeline_id,
                    decision_id=decision.id,
                    error=str(e),
                )

    def _check_decision_status(
        self,
        decision: HITLDecision,
    ) -> tuple[bool, bool]:
        """Check if decision should warn or timeout.

        Args:
            decision: Decision to check

        Returns:
            Tuple of (should_warn, should_timeout)
        """
        if decision.status != DecisionStatus.PENDING:
            return False, False

        now = datetime.utcnow()
        elapsed = (now - decision.created_at).total_seconds()
        timeout = decision.timeout_seconds

        should_timeout = elapsed >= timeout
        should_warn = (
            not should_timeout
            and elapsed >= timeout * self.warning_threshold_percent
        )

        return should_warn, should_timeout

    def _check_pipeline(self, pipeline_id: str, repo_path: Path) -> None:
        """Check a single pipeline for timeouts.

        Args:
            pipeline_id: Pipeline ID
            repo_path: Path to repository
        """
        try:
            queue = get_decision_queue(pipeline_id, repo_path)
            pending = queue.get_pending_decisions()

            for decision in pending:
                decision_key = f"{pipeline_id}:{decision.id}"
                should_warn, should_timeout = self._check_decision_status(decision)

                if should_timeout:
                    # Timeout the decision
                    queue.check_timeouts()

                    logger.warning(
                        "Decision timed out",
                        pipeline_id=pipeline_id,
                        decision_id=decision.id,
                    )

                    self._escalate(pipeline_id, decision)

                elif should_warn and decision_key not in self._warned_decisions:
                    # Issue warning (only once per decision)
                    self._warned_decisions.add(decision_key)

                    remaining = decision.timeout_seconds - (
                        datetime.utcnow() - decision.created_at
                    ).total_seconds()

                    logger.warning(
                        "Decision approaching timeout",
                        pipeline_id=pipeline_id,
                        decision_id=decision.id,
                        remaining_seconds=int(remaining),
                    )

                    # Could trigger a warning notification here

        except Exception as e:
            logger.error(
                "Failed to check pipeline timeouts",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            with self._lock:
                pipelines = self._pipelines.copy()

            for pipeline_id, repo_path in pipelines.items():
                self._check_pipeline(pipeline_id, repo_path)

            time.sleep(self.check_interval)

    def start(self) -> None:
        """Start the timeout monitor."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

        logger.info(
            "Decision timeout monitor started",
            check_interval=self.check_interval,
        )

    def stop(self) -> None:
        """Stop the timeout monitor."""
        if not self._running:
            return

        self._running = False
        if self._thread:
            self._thread.join(timeout=self.check_interval + 1)
            self._thread = None

        logger.info("Decision timeout monitor stopped")

    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running


def create_notification_handler(
    notification_url: str | None = None,
) -> EscalationHandler:
    """Create an escalation handler that sends notifications.

    Args:
        notification_url: URL to POST notifications to

    Returns:
        Escalation handler function
    """
    def handler(pipeline_id: str, decision: HITLDecision) -> None:
        message = (
            f"Decision timeout in pipeline {pipeline_id}:\n"
            f"Question: {decision.question}\n"
            f"Decision ID: {decision.id}"
        )

        logger.warning(message)

        if notification_url:
            try:
                import requests

                requests.post(
                    notification_url,
                    json={
                        "type": "decision_timeout",
                        "pipeline_id": pipeline_id,
                        "decision_id": decision.id,
                        "question": decision.question,
                    },
                    timeout=10,
                )
            except Exception as e:
                logger.error(
                    "Failed to send notification",
                    error=str(e),
                )

    return handler


def create_slack_handler(
    webhook_url: str,
) -> EscalationHandler:
    """Create an escalation handler that sends Slack notifications.

    Args:
        webhook_url: Slack webhook URL

    Returns:
        Escalation handler function
    """
    def handler(pipeline_id: str, decision: HITLDecision) -> None:
        try:
            import requests

            payload = {
                "text": f":warning: Decision timeout in pipeline `{pipeline_id}`",
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "Decision Timeout",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Pipeline:*\n{pipeline_id}",
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Decision ID:*\n{decision.id}",
                            },
                        ],
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Question:*\n{decision.question}",
                        },
                    },
                ],
            }

            requests.post(webhook_url, json=payload, timeout=10)

        except Exception as e:
            logger.error("Failed to send Slack notification", error=str(e))

    return handler


# Singleton monitor instance
_timeout_monitor: DecisionTimeoutMonitor | None = None


def get_timeout_monitor() -> DecisionTimeoutMonitor:
    """Get the singleton timeout monitor.

    Returns:
        DecisionTimeoutMonitor instance
    """
    global _timeout_monitor
    if _timeout_monitor is None:
        _timeout_monitor = DecisionTimeoutMonitor()
    return _timeout_monitor
