"""
Decision queue and polling mechanism for HITL integration.

Manages human-in-the-loop decisions with queueing, polling,
and resolution.
"""

import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

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


from models import DecisionStatus, HITLDecision, Pipeline
from state_store import get_pipeline_state_lock, get_state_store

logger = get_logger("orchestrator.decision_queue")


class DecisionNotFoundError(Exception):
    """Raised when a decision is not found."""

    pass


class DecisionAlreadyResolvedError(Exception):
    """Raised when trying to resolve an already resolved decision."""

    pass


DecisionHandler = Callable[[HITLDecision], None]


class DecisionQueue:
    """Queue for managing HITL decisions.

    Provides thread-safe queueing, polling, and timeout handling
    for human-in-the-loop decisions.
    """

    def __init__(
        self,
        pipeline_id: str,
        repo_path: Path,
    ):
        """Initialize decision queue.

        Args:
            pipeline_id: Pipeline ID
            repo_path: Path to repository
        """
        self.pipeline_id = pipeline_id
        self.repo_path = repo_path

        self._handlers: list[DecisionHandler] = []
        # Use the shared per-pipeline lock so that decision state changes
        # are atomic with respect to other pipeline state writers
        # (e.g. StateStore.update_pipeline).
        self._lock = get_pipeline_state_lock(pipeline_id)

    def _load_pipeline(self) -> Pipeline:
        """Load pipeline from state store."""
        store = get_state_store(self.repo_path)
        return store.load_pipeline(self.pipeline_id)

    def _save_pipeline(self, pipeline: Pipeline) -> None:
        """Save pipeline to state store."""
        store = get_state_store(self.repo_path)
        store.save_pipeline(pipeline)

    def add_handler(self, handler: DecisionHandler) -> None:
        """Add a handler for new decisions.

        Args:
            handler: Function called when new decision is queued
        """
        with self._lock:
            self._handlers.append(handler)

    def remove_handler(self, handler: DecisionHandler) -> None:
        """Remove a decision handler.

        Args:
            handler: Handler to remove
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _notify_handlers(self, decision: HITLDecision) -> None:
        """Notify all handlers of a new decision.

        Args:
            decision: New decision
        """
        with self._lock:
            handlers = self._handlers.copy()

        for handler in handlers:
            try:
                handler(decision)
            except Exception as e:
                logger.error(
                    "Decision handler error",
                    decision_id=decision.id,
                    error=str(e),
                )

    def queue_decision(
        self,
        question: str,
        context: str = "",
        options: list[str] | None = None,
    ) -> HITLDecision:
        """Queue a new decision for human review.

        Args:
            question: Decision question
            context: Additional context
            options: Available options (empty for free-form)

        Returns:
            Created HITLDecision
        """
        with self._lock:
            pipeline = self._load_pipeline()

            # Create decision using pipeline method
            decision = pipeline.add_decision(
                question=question,
                options=options or [],
            )

            # Update with additional fields
            decision.context = context

            self._save_pipeline(pipeline)

            logger.info(
                "Decision queued",
                pipeline_id=self.pipeline_id,
                decision_id=decision.id,
                question=question[:50],
            )

        # Notify handlers outside lock
        self._notify_handlers(decision)

        return decision

    def get_pending_decisions(self) -> list[HITLDecision]:
        """Get all pending decisions.

        Returns:
            List of pending decisions
        """
        with self._lock:
            pipeline = self._load_pipeline()
            return [d for d in pipeline.decisions if d.status == DecisionStatus.PENDING]

    def get_decision(self, decision_id: str) -> HITLDecision:
        """Get a specific decision.

        Args:
            decision_id: Decision ID

        Returns:
            HITLDecision

        Raises:
            DecisionNotFoundError: If decision not found
        """
        with self._lock:
            pipeline = self._load_pipeline()
            for decision in pipeline.decisions:
                if decision.id == decision_id:
                    return decision
        raise DecisionNotFoundError(f"Decision {decision_id} not found")

    def resolve_decision(
        self,
        decision_id: str,
        resolution: str,
    ) -> HITLDecision:
        """Resolve a pending decision.

        Args:
            decision_id: Decision ID
            resolution: Human's response

        Returns:
            Updated HITLDecision

        Raises:
            DecisionNotFoundError: If decision not found
            DecisionAlreadyResolvedError: If already resolved
        """
        with self._lock:
            pipeline = self._load_pipeline()

            for decision in pipeline.decisions:
                if decision.id == decision_id:
                    if decision.status != DecisionStatus.PENDING:
                        raise DecisionAlreadyResolvedError(
                            f"Decision {decision_id} is already {decision.status.value}"
                        )

                    decision.status = DecisionStatus.RESOLVED
                    decision.resolution = resolution
                    decision.resolved_at = datetime.utcnow()
                    pipeline.updated_at = datetime.utcnow()

                    self._save_pipeline(pipeline)

                    logger.info(
                        "Decision resolved",
                        pipeline_id=self.pipeline_id,
                        decision_id=decision_id,
                        resolution=resolution[:50],
                    )

                    return decision

            raise DecisionNotFoundError(f"Decision {decision_id} not found")

    def cancel_decision(self, decision_id: str) -> HITLDecision:
        """Cancel a pending decision.

        Args:
            decision_id: Decision ID

        Returns:
            Updated HITLDecision

        Raises:
            DecisionNotFoundError: If decision not found
        """
        with self._lock:
            pipeline = self._load_pipeline()

            for decision in pipeline.decisions:
                if decision.id == decision_id:
                    if decision.status != DecisionStatus.PENDING:
                        return decision

                    decision.status = DecisionStatus.CANCELLED
                    decision.resolved_at = datetime.utcnow()
                    pipeline.updated_at = datetime.utcnow()

                    self._save_pipeline(pipeline)

                    logger.info(
                        "Decision cancelled",
                        pipeline_id=self.pipeline_id,
                        decision_id=decision_id,
                    )

                    return decision

            raise DecisionNotFoundError(f"Decision {decision_id} not found")

    def wait_for_decision(
        self,
        decision_id: str,
        poll_interval: int = 5,
    ) -> HITLDecision:
        """Wait for a decision to be resolved.

        Polls indefinitely until the decision is no longer PENDING.
        The caller should inspect the returned decision's status
        (RESOLVED, CANCELLED, etc.) to determine the outcome.

        Args:
            decision_id: Decision ID
            poll_interval: Seconds between polls

        Returns:
            HITLDecision once it is no longer PENDING

        Raises:
            DecisionNotFoundError: If decision not found
        """
        while True:
            decision = self.get_decision(decision_id)

            if decision.status != DecisionStatus.PENDING:
                return decision

            time.sleep(poll_interval)

    def get_queue_status(self) -> dict[str, Any]:
        """Get queue status summary.

        Returns:
            Status dictionary
        """
        pipeline = self._load_pipeline()
        decisions = pipeline.decisions

        pending = [d for d in decisions if d.status == DecisionStatus.PENDING]
        resolved = [d for d in decisions if d.status == DecisionStatus.RESOLVED]

        return {
            "pipeline_id": self.pipeline_id,
            "total_decisions": len(decisions),
            "pending": len(pending),
            "resolved": len(resolved),
            "pending_decisions": [
                {
                    "id": d.id,
                    "question": d.question,
                    "created_at": d.created_at.isoformat(),
                }
                for d in pending
            ],
        }


# Cache of decision queues by pipeline
_decision_queues: dict[str, DecisionQueue] = {}
_queues_lock = threading.Lock()


def get_decision_queue(pipeline_id: str, repo_path: Path | str) -> DecisionQueue:
    """Get or create a decision queue for a pipeline.

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository

    Returns:
        DecisionQueue instance
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)

    cache_key = f"{pipeline_id}:{repo_path}"

    with _queues_lock:
        if cache_key not in _decision_queues:
            _decision_queues[cache_key] = DecisionQueue(pipeline_id, repo_path)
        return _decision_queues[cache_key]
