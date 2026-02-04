"""
Context management for egg_logging.

Provides correlation IDs and context propagation for structured logging.
Aligns with OpenTelemetry trace context for distributed tracing compatibility.
"""

import os
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# Thread-local and async-safe context storage
_current_context: ContextVar["LogContext | None"] = ContextVar("egg_log_context", default=None)


@dataclass
class LogContext:
    """Context for log correlation and tracing.

    Attributes:
        trace_id: W3C Trace Context trace-id (32 hex chars)
        span_id: W3C Trace Context span-id (16 hex chars)
        trace_flags: W3C Trace Context trace-flags (2 hex chars, usually "01")
        task_id: Beads task ID for task correlation
        repository: GitHub repository (owner/repo format)
        pr_number: Pull request number if applicable
        workflow_id: Unique identifier for the workflow/job that initiated this work
        workflow_type: Type of workflow (e.g., 'check_failure', 'comment', 'slack_task')
        extra: Additional context fields to include in logs
    """

    trace_id: str | None = None
    span_id: str | None = None
    trace_flags: str = "01"
    task_id: str | None = None
    repository: str | None = None
    pr_number: int | None = None
    workflow_id: str | None = None
    workflow_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Generate trace_id and span_id if not provided."""
        if self.trace_id is None:
            self.trace_id = secrets.token_hex(16)
        if self.span_id is None:
            self.span_id = secrets.token_hex(8)

    def new_span(self) -> "LogContext":
        """Create a new context with a new span_id but same trace_id."""
        return LogContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            trace_flags=self.trace_flags,
            task_id=self.task_id,
            repository=self.repository,
            pr_number=self.pr_number,
            workflow_id=self.workflow_id,
            workflow_type=self.workflow_type,
            extra=dict(self.extra),
        )

    def with_extra(self, **kwargs: Any) -> "LogContext":
        """Create a new context with additional extra fields."""
        new_extra = dict(self.extra)
        new_extra.update(kwargs)
        return LogContext(
            trace_id=self.trace_id,
            span_id=self.span_id,
            trace_flags=self.trace_flags,
            task_id=self.task_id,
            repository=self.repository,
            pr_number=self.pr_number,
            workflow_id=self.workflow_id,
            workflow_type=self.workflow_type,
            extra=new_extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert context to dict for log inclusion."""
        result: dict[str, Any] = {}

        if self.trace_id:
            result["traceId"] = self.trace_id
        if self.span_id:
            result["spanId"] = self.span_id
        if self.trace_flags:
            result["traceFlags"] = self.trace_flags
        if self.task_id:
            result["task_id"] = self.task_id
        if self.repository:
            result["repository"] = self.repository
        if self.pr_number:
            result["pr_number"] = self.pr_number
        if self.workflow_id:
            result["workflow_id"] = self.workflow_id
        if self.workflow_type:
            result["workflow_type"] = self.workflow_type

        return result


def get_current_context() -> LogContext | None:
    """Get the current logging context."""
    return _current_context.get()


def set_current_context(ctx: LogContext | None) -> None:
    """Set the current logging context."""
    _current_context.set(ctx)


def get_or_create_context() -> LogContext:
    """Get the current context or create a new one."""
    ctx = get_current_context()
    if ctx is None:
        ctx = LogContext()
        set_current_context(ctx)
    return ctx


class ContextScope:
    """Context manager for scoped logging context.

    Usage:
        with ContextScope(task_id="bd-abc123", repository="owner/repo"):
            logger.info("Processing task")
            # All logs in this scope include the context
    """

    def __init__(
        self,
        trace_id: str | None = None,
        span_id: str | None = None,
        task_id: str | None = None,
        repository: str | None = None,
        pr_number: int | None = None,
        workflow_id: str | None = None,
        workflow_type: str | None = None,
        **extra: Any,
    ):
        # Store the provided values - we'll create the context in __enter__
        # so we can inherit from parent context if needed
        self._trace_id = trace_id
        self._span_id = span_id
        self._task_id = task_id
        self._repository = repository
        self._pr_number = pr_number
        self._workflow_id = workflow_id
        self._workflow_type = workflow_type
        self._extra = extra
        self._new_context: LogContext | None = None
        self._previous_context: LogContext | None = None
        self._token: Any = None

    def __enter__(self) -> LogContext:
        self._previous_context = get_current_context()

        # Inherit trace_id from parent context if not explicitly provided
        trace_id = self._trace_id
        if trace_id is None and self._previous_context:
            trace_id = self._previous_context.trace_id

        self._new_context = LogContext(
            trace_id=trace_id,
            span_id=self._span_id,
            task_id=self._task_id,
            repository=self._repository,
            pr_number=self._pr_number,
            workflow_id=self._workflow_id,
            workflow_type=self._workflow_type,
            extra=self._extra,
        )

        self._token = _current_context.set(self._new_context)
        return self._new_context

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        _current_context.reset(self._token)


def context_from_env() -> LogContext:
    """Create a context from environment variables.

    Looks for:
        - EGG_TRACE_ID / OTEL_TRACE_ID: Trace ID
        - EGG_SPAN_ID / OTEL_SPAN_ID: Span ID
        - EGG_TASK_ID: Task ID
        - EGG_REPOSITORY: Repository name
        - EGG_PR_NUMBER: PR number
        - EGG_WORKFLOW_ID: Workflow/job identifier
        - EGG_WORKFLOW_TYPE: Workflow type
    """
    pr_number_str = os.environ.get("EGG_PR_NUMBER")
    return LogContext(
        trace_id=os.environ.get("EGG_TRACE_ID") or os.environ.get("OTEL_TRACE_ID"),
        span_id=os.environ.get("EGG_SPAN_ID") or os.environ.get("OTEL_SPAN_ID"),
        task_id=os.environ.get("EGG_TASK_ID"),
        repository=os.environ.get("EGG_REPOSITORY"),
        pr_number=int(pr_number_str) if pr_number_str else None,
        workflow_id=os.environ.get("EGG_WORKFLOW_ID"),
        workflow_type=os.environ.get("EGG_WORKFLOW_TYPE"),
    )
