"""Unit tests for the ``get_service_logs`` server-side filter (#3032)."""

from __future__ import annotations

import json
import logging
import re

import pytest
from log_filter import filter_log_lines, known_severities, severity_rank


def _line(severity: str, message: str, task_id: str | None = None) -> str:
    obj: dict = {"severity": severity, "message": message}
    if task_id is not None:
        obj["context"] = {"task_id": task_id}
    return json.dumps(obj)


def _line_extra_pipeline(severity: str, message: str, pipeline_id: str) -> str:
    """Production-shape line: the kwarg landed in ``extra`` (not ``context``).

    The ``JsonFormatter`` only allowlists ``task_id``/``repository``/``pr_number``
    into ``context``; ``pipeline_id=`` kwargs (what the orchestrator actually
    uses) land in ``extra`` instead.
    """
    return json.dumps(
        {
            "severity": severity,
            "message": message,
            "extra": {"pipeline_id": pipeline_id},
        }
    )


class TestSeverityRank:
    def test_known_levels_ordered(self):
        ranks = [severity_rank(s) for s in known_severities()]
        assert ranks == sorted(ranks)
        assert severity_rank("WARNING") > severity_rank("INFO")

    def test_case_insensitive(self):
        assert severity_rank("warning") == severity_rank("WARNING")

    def test_unknown_is_none(self):
        assert severity_rank("LOUD") is None
        assert severity_rank(None) is None
        assert severity_rank("") is None


class TestFilterLogLines:
    def test_no_filter_returns_input_with_tail_limit(self):
        raw = "\n".join(f"line {i}" for i in range(5))
        assert filter_log_lines(raw) == raw
        assert filter_log_lines(raw, limit=2) == "line 3\nline 4"

    def test_level_floor_drops_lower_and_unstructured(self):
        raw = "\n".join(
            [
                _line("INFO", "info"),
                _line("WARNING", "warn"),
                _line("ERROR", "err"),
                "plain non-json line",
            ]
        )
        out = filter_log_lines(raw, min_level="WARNING")
        assert "warn" in out
        assert "err" in out
        assert "info" not in out
        assert "plain non-json" not in out

    def test_pipeline_id_scopes_to_task(self):
        raw = "\n".join(
            [
                _line("INFO", "mine", task_id="p-1"),
                _line("INFO", "theirs", task_id="p-2"),
                _line("INFO", "untagged"),  # no context.task_id
            ]
        )
        out = filter_log_lines(raw, pipeline_id="p-1")
        # Only the p-1 line survives; the others (wrong / missing task_id) drop.
        assert out == _line("INFO", "mine", task_id="p-1")

    def test_pipeline_id_matches_extra_pipeline_id(self):
        """Production lines use ``extra.pipeline_id`` (not ``context.task_id``)."""
        raw = "\n".join(
            [
                _line_extra_pipeline("WARNING", "mine via extra", "p-1"),
                _line_extra_pipeline("WARNING", "theirs", "p-2"),
            ]
        )
        out = filter_log_lines(raw, pipeline_id="p-1")
        assert "mine via extra" in out
        assert "theirs" not in out

    def test_pipeline_id_matches_extra_task_id_fallback(self):
        """A caller spelling the kwarg ``task_id`` (not on the formatter's
        allowlist) lands at ``extra.task_id``; the filter still matches."""
        raw = json.dumps(
            {
                "severity": "ERROR",
                "message": "via extra.task_id",
                "extra": {"task_id": "p-1"},
            }
        )
        assert "via extra.task_id" in filter_log_lines(raw, pipeline_id="p-1")
        assert filter_log_lines(raw, pipeline_id="p-2") == ""

    def test_pipeline_id_end_to_end_via_json_formatter(self):
        """Producer/consumer parity: lines produced by the real
        ``JsonFormatter`` (the thing every orchestrator log call goes through)
        must be matchable by the filter when callers use ``pipeline_id=``.

        This is the test that would have caught the bug — the hand-built
        ``_line()`` fixture nests ``context.task_id`` correctly, but
        ``logger.warning("...", pipeline_id=...)`` actually lands the id in
        ``extra.pipeline_id``.
        """
        from egg_logging.formatters import JsonFormatter

        formatter = JsonFormatter(service="orchestrator")

        def _emit(level: int, message: str, **kwargs: object) -> str:
            record = logging.LogRecord(
                name="orchestrator.deployment",
                level=level,
                pathname=__file__,
                lineno=1,
                msg=message,
                args=(),
                exc_info=None,
            )
            for key, value in kwargs.items():
                setattr(record, key, value)
            return formatter.format(record)

        raw = "\n".join(
            [
                _emit(
                    logging.WARNING,
                    "Context PR opener failed at advance_phase",
                    pipeline_id="issue-123",
                    reason="upstream",
                ),
                _emit(logging.INFO, "routine poll", pipeline_id="issue-123"),
                _emit(logging.ERROR, "boom", pipeline_id="other-456"),
                _emit(logging.WARNING, "global warn"),  # no pipeline_id
            ]
        )

        # The motivating example from the PR description: WARNING+ for a
        # specific pipeline. Pre-fix this returned "".
        out = filter_log_lines(raw, pipeline_id="issue-123", min_level="WARNING")
        assert "Context PR opener" in out
        assert "routine poll" not in out  # below floor
        assert "boom" not in out  # wrong pipeline
        assert "global warn" not in out  # no pipeline_id

        # pipeline_id alone (no level floor) keeps INFO too.
        out = filter_log_lines(raw, pipeline_id="issue-123")
        assert "Context PR opener" in out
        assert "routine poll" in out
        assert "boom" not in out
        assert "global warn" not in out

    def test_pattern_is_regex_search(self):
        raw = "\n".join(
            [
                _line("INFO", "Context PR opener failed reason=x"),
                _line("INFO", "routine poll"),
            ]
        )
        out = filter_log_lines(raw, pattern=re.compile(r"opener failed reason="))
        assert "Context PR opener" in out
        assert "routine poll" not in out

    def test_pattern_matches_plain_substring(self):
        raw = "\n".join([_line("INFO", "alpha"), _line("INFO", "beta")])
        out = filter_log_lines(raw, pattern=re.compile("alpha"))
        assert "alpha" in out
        assert "beta" not in out

    def test_filters_are_anded(self):
        raw = "\n".join(
            [
                _line("WARNING", "keep me", task_id="p-1"),
                _line("WARNING", "wrong pipeline", task_id="p-2"),
                _line("INFO", "too quiet", task_id="p-1"),
            ]
        )
        out = filter_log_lines(
            raw,
            pipeline_id="p-1",
            min_level="WARNING",
            pattern=re.compile("keep"),
        )
        assert out == _line("WARNING", "keep me", task_id="p-1")

    def test_limit_keeps_most_recent_matches(self):
        raw = "\n".join(_line("ERROR", f"e{i}", task_id="p-1") for i in range(5))
        out = filter_log_lines(raw, min_level="ERROR", limit=2)
        assert out.splitlines() == [
            _line("ERROR", "e3", task_id="p-1"),
            _line("ERROR", "e4", task_id="p-1"),
        ]

    def test_unrecognised_min_level_raises(self):
        """A bad ``min_level`` raises so the footgun isn't silent.

        Pre-fix this filter silently dropped on an unknown level — a
        deliberately-set parameter producing no signal is the exact failure
        mode the review rules guard against.
        """
        raw = _line("INFO", "anything")
        with pytest.raises(ValueError, match="min_level must be one of"):
            filter_log_lines(raw, min_level="LOUD")

    def test_limit_zero_returns_empty(self):
        """``lines[-0:]`` is ``lines[:]`` — guard against the slice gotcha."""
        raw = "\n".join(_line("INFO", f"m{i}") for i in range(3))
        assert filter_log_lines(raw, limit=0) == ""
        assert filter_log_lines(raw, pattern=re.compile("m"), limit=0) == ""

    def test_limit_negative_returns_empty(self):
        raw = "\n".join(_line("INFO", f"m{i}") for i in range(3))
        assert filter_log_lines(raw, limit=-5) == ""
