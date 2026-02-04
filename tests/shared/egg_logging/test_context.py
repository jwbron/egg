"""Tests for jib_logging context management."""

import os

from egg_logging import (
    ContextScope,
    LogContext,
    context_from_env,
    get_current_context,
    get_or_create_context,
    set_current_context,
)


class TestLogContext:
    """Tests for LogContext dataclass."""

    def test_auto_generates_trace_id(self):
        """Test that trace_id is auto-generated if not provided."""
        ctx = LogContext()
        assert ctx.trace_id is not None
        assert len(ctx.trace_id) == 32  # 16 bytes as hex

    def test_auto_generates_span_id(self):
        """Test that span_id is auto-generated if not provided."""
        ctx = LogContext()
        assert ctx.span_id is not None
        assert len(ctx.span_id) == 16  # 8 bytes as hex

    def test_accepts_provided_ids(self):
        """Test that provided IDs are used."""
        ctx = LogContext(trace_id="abc123", span_id="def456")
        assert ctx.trace_id == "abc123"
        assert ctx.span_id == "def456"

    def test_default_trace_flags(self):
        """Test default trace flags."""
        ctx = LogContext()
        assert ctx.trace_flags == "01"

    def test_new_span_preserves_trace_id(self):
        """Test that new_span creates a new span with same trace_id."""
        ctx = LogContext(trace_id="original_trace")
        new_ctx = ctx.new_span()

        assert new_ctx.trace_id == "original_trace"
        assert new_ctx.span_id != ctx.span_id

    def test_new_span_preserves_task_context(self):
        """Test that new_span preserves task context."""
        ctx = LogContext(
            task_id="bd-abc123",
            repository="owner/repo",
            pr_number=42,
        )
        new_ctx = ctx.new_span()

        assert new_ctx.task_id == "bd-abc123"
        assert new_ctx.repository == "owner/repo"
        assert new_ctx.pr_number == 42

    def test_with_extra_adds_fields(self):
        """Test that with_extra adds new fields."""
        ctx = LogContext(extra={"key1": "value1"})
        new_ctx = ctx.with_extra(key2="value2")

        assert new_ctx.extra["key1"] == "value1"
        assert new_ctx.extra["key2"] == "value2"

    def test_with_extra_does_not_modify_original(self):
        """Test that with_extra doesn't modify the original context."""
        ctx = LogContext(extra={"key1": "value1"})
        ctx.with_extra(key2="value2")

        assert "key2" not in ctx.extra

    def test_to_dict_includes_all_fields(self):
        """Test that to_dict includes all set fields."""
        ctx = LogContext(
            trace_id="trace123",
            span_id="span456",
            trace_flags="01",
            task_id="bd-abc",
            repository="owner/repo",
            pr_number=123,
        )
        d = ctx.to_dict()

        assert d["traceId"] == "trace123"
        assert d["spanId"] == "span456"
        assert d["traceFlags"] == "01"
        assert d["task_id"] == "bd-abc"
        assert d["repository"] == "owner/repo"
        assert d["pr_number"] == 123

    def test_to_dict_excludes_none_fields(self):
        """Test that to_dict excludes None fields."""
        ctx = LogContext(trace_id="trace123", span_id="span456")
        d = ctx.to_dict()

        assert "task_id" not in d
        assert "repository" not in d
        assert "pr_number" not in d


class TestContextFunctions:
    """Tests for context getter/setter functions."""

    def teardown_method(self):
        """Reset context after each test."""
        set_current_context(None)

    def test_get_current_context_returns_none_initially(self):
        """Test that get_current_context returns None when no context is set."""
        set_current_context(None)
        assert get_current_context() is None

    def test_set_and_get_context(self):
        """Test setting and getting context."""
        ctx = LogContext(task_id="test-task")
        set_current_context(ctx)

        retrieved = get_current_context()
        assert retrieved is ctx
        assert retrieved.task_id == "test-task"

    def test_get_or_create_returns_existing(self):
        """Test that get_or_create returns existing context if set."""
        ctx = LogContext(task_id="existing")
        set_current_context(ctx)

        result = get_or_create_context()
        assert result.task_id == "existing"

    def test_get_or_create_creates_new(self):
        """Test that get_or_create creates new context if none exists."""
        set_current_context(None)
        result = get_or_create_context()

        assert result is not None
        assert result.trace_id is not None


class TestContextScope:
    """Tests for ContextScope context manager."""

    def teardown_method(self):
        """Reset context after each test."""
        set_current_context(None)

    def test_context_scope_sets_context(self):
        """Test that ContextScope sets the context within the scope."""
        with ContextScope(task_id="scoped-task") as ctx:
            current = get_current_context()
            assert current is ctx
            assert current.task_id == "scoped-task"

    def test_context_scope_restores_context(self):
        """Test that ContextScope restores previous context after scope."""
        original = LogContext(task_id="original")
        set_current_context(original)

        with ContextScope(task_id="scoped"):
            assert get_current_context().task_id == "scoped"

        assert get_current_context().task_id == "original"

    def test_context_scope_restores_none(self):
        """Test that ContextScope restores None if no previous context."""
        set_current_context(None)

        with ContextScope(task_id="scoped"):
            assert get_current_context().task_id == "scoped"

        assert get_current_context() is None

    def test_nested_context_scopes(self):
        """Test nested context scopes."""
        with ContextScope(task_id="outer"):
            assert get_current_context().task_id == "outer"

            with ContextScope(task_id="inner"):
                assert get_current_context().task_id == "inner"

            assert get_current_context().task_id == "outer"

    def test_context_scope_inherits_trace_id(self):
        """Test that nested scope inherits trace_id from parent."""
        with ContextScope(trace_id="parent_trace", task_id="parent"):
            with ContextScope(task_id="child") as child:
                # Child should inherit trace_id from parent
                assert child.trace_id == "parent_trace"


class TestContextFromEnv:
    """Tests for context_from_env function."""

    def teardown_method(self):
        """Clean up environment variables after each test."""
        for var in [
            "EGG_TRACE_ID",
            "EGG_SPAN_ID",
            "EGG_TASK_ID",
            "EGG_REPOSITORY",
            "EGG_PR_NUMBER",
            "EGG_WORKFLOW_ID",
            "EGG_WORKFLOW_TYPE",
            "OTEL_TRACE_ID",
            "OTEL_SPAN_ID",
        ]:
            os.environ.pop(var, None)

    def test_reads_egg_variables(self):
        """Test that it reads EGG_* environment variables."""
        os.environ["EGG_TRACE_ID"] = "env_trace"
        os.environ["EGG_SPAN_ID"] = "env_span"
        os.environ["EGG_TASK_ID"] = "env_task"
        os.environ["EGG_REPOSITORY"] = "owner/repo"
        os.environ["EGG_PR_NUMBER"] = "42"

        ctx = context_from_env()

        assert ctx.trace_id == "env_trace"
        assert ctx.span_id == "env_span"
        assert ctx.task_id == "env_task"
        assert ctx.repository == "owner/repo"
        assert ctx.pr_number == 42

    def test_falls_back_to_otel_variables(self):
        """Test fallback to OTEL_* variables for trace context."""
        os.environ["OTEL_TRACE_ID"] = "otel_trace"
        os.environ["OTEL_SPAN_ID"] = "otel_span"

        ctx = context_from_env()

        assert ctx.trace_id == "otel_trace"
        assert ctx.span_id == "otel_span"

    def test_egg_takes_precedence_over_otel(self):
        """Test that EGG_* variables take precedence over OTEL_*."""
        os.environ["EGG_TRACE_ID"] = "egg_trace"
        os.environ["OTEL_TRACE_ID"] = "otel_trace"

        ctx = context_from_env()

        assert ctx.trace_id == "egg_trace"

    def test_handles_missing_variables(self):
        """Test graceful handling of missing variables."""
        ctx = context_from_env()

        # Should auto-generate trace_id and span_id
        assert ctx.trace_id is not None
        assert ctx.span_id is not None
        # Other fields should be None
        assert ctx.task_id is None
        assert ctx.repository is None
        assert ctx.pr_number is None
