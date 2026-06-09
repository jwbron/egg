"""Unit tests for the ``get_service_logs`` server-side filter (#3032)."""

from __future__ import annotations

import json
import re

from log_filter import filter_log_lines, known_severities, severity_rank


def _line(severity: str, message: str, task_id: str | None = None) -> str:
    obj: dict = {"severity": severity, "message": message}
    if task_id is not None:
        obj["context"] = {"task_id": task_id}
    return json.dumps(obj)


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

    def test_unrecognised_min_level_is_inert(self):
        """A bad ``min_level`` is treated as no severity filter (route validates it)."""
        raw = "\n".join([_line("INFO", "keep-this"), _line("ERROR", "drop-this")])
        # Only the pattern is genuinely active here; the bogus level is ignored,
        # so the INFO line is not filtered out by severity.
        out = filter_log_lines(raw, min_level="LOUD", pattern=re.compile("keep-this"))
        assert out == _line("INFO", "keep-this")
