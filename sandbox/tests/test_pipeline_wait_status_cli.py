"""Tests for the ``egg-orch pipeline wait-status`` CLI (issue #2211).

Covers parser surface, the loop body's classification logic, JSON-line
emission, cursor threading, and exit codes per the §3 contract in
``docs/reference/agent-wait-patterns.md``:

    0 = terminal pipeline state, 1 = max-iter, 2 = transient,
    3 = permanent (4xx).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_lib.orch_cli import (  # noqa: E402
    ApiError,
    cmd_pipeline_wait_status,
    create_parser,
)

_API_MOCK_PATH = "egg_lib.orch_cli.api_request"


# ---------------------------------------------------------------------------
# Parser: argparse surface
# ---------------------------------------------------------------------------


class TestParser:
    def test_wait_status_accepts_pipeline_id(self):
        parser = create_parser()
        args = parser.parse_args(["pipeline", "wait-status", "issue-42"])
        assert args.pipeline_id == "issue-42"
        assert args.since == ""
        assert args.inner_timeout == 25
        assert args.max_iterations is None

    def test_wait_status_accepts_since_and_timeout(self):
        parser = create_parser()
        args = parser.parse_args(
            [
                "pipeline",
                "wait-status",
                "issue-42",
                "--since",
                "msg:abc|evt:5",
                "--inner-timeout",
                "10",
                "--max-iterations",
                "3",
            ]
        )
        assert args.since == "msg:abc|evt:5"
        assert args.inner_timeout == 10
        assert args.max_iterations == 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ns(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "pipeline_id": "issue-42",
        "since": "",
        "inner_timeout": 1,
        "max_iterations": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _no_change(cursor: str = "msg:|evt:0") -> dict[str, Any]:
    return {
        "success": True,
        "data": {"changed": False, "no_change": True, "cursor": cursor},
    }


def _phase_event(cursor: str, event_type: str = "phase.started") -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "changed": True,
            "trigger": "event",
            "event_type": event_type,
            "cursor": cursor,
            "current_phase": "implement",
            "status": "running",
            "phase_elapsed_seconds": 5,
            "concurrent": {"consensus": {"is_complete": False}},
        },
    }


def _terminal_event(cursor: str, event_type: str = "pipeline.completed") -> dict[str, Any]:
    body = _phase_event(cursor, event_type=event_type)
    body["data"]["status"] = "complete"
    return body


def _message_event(cursor: str, message_type: str = "OVERSEER_ALERT") -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "changed": True,
            "trigger": "message",
            "messages": [{"message_type": message_type, "subject": "stall"}],
            "cursor": cursor,
            "current_phase": "implement",
            "status": "running",
        },
    }


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_terminal_status_returns_zero(self, capsys):
        with patch(_API_MOCK_PATH, side_effect=[_terminal_event("msg:|evt:1")]):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        line = json.loads(out[0])
        assert line["event_type"] == "pipeline.completed"
        assert line["cursor"] == "msg:|evt:1"

    def test_status_complete_via_status_field_returns_zero(self, capsys):
        body = _phase_event("msg:|evt:1")
        body["data"]["status"] = "complete"
        body["data"]["event_type"] = "phase.completed"  # not in terminal-event set
        with patch(_API_MOCK_PATH, side_effect=[body]):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))
        assert rc == 0

    def test_max_iterations_returns_one(self):
        # Three no-change responses; max_iterations=2 → exit 1
        with patch(
            _API_MOCK_PATH,
            side_effect=[
                _no_change("msg:|evt:1"),
                _no_change("msg:|evt:2"),
                _no_change("msg:|evt:3"),
            ],
        ):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=2))
        assert rc == 1

    def test_400_returns_three(self, capsys):
        err = ApiError("Invalid 'since' cursor", status_code=400)
        with patch(_API_MOCK_PATH, side_effect=err):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))
        assert rc == 3
        assert "400" in capsys.readouterr().err

    def test_404_returns_three(self):
        err = ApiError("Pipeline issue-42 not found", status_code=404)
        with patch(_API_MOCK_PATH, side_effect=err):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))
        assert rc == 3

    def test_other_4xx_returns_three(self):
        err = ApiError("forbidden", status_code=403)
        with patch(_API_MOCK_PATH, side_effect=err):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))
        assert rc == 3

    def test_5xx_then_terminal_recovers_to_zero(self):
        # 5xx burns budget; subsequent terminal still returns 0.
        err = ApiError("server error", status_code=503)
        with (
            patch(_API_MOCK_PATH, side_effect=[err, err, _terminal_event("msg:|evt:9")]),
            patch("time.sleep"),
        ):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=10))
        assert rc == 0

    def test_5xx_exhausts_budget_returns_two(self):
        err = ApiError("server error", status_code=500)
        # Many 5xx in a row; with sleep mocked the loop iterates until the
        # cumulative backoff sleep exceeds the 60 s budget.
        with (
            patch(_API_MOCK_PATH, side_effect=err),
            patch("time.sleep"),
        ):
            rc = cmd_pipeline_wait_status(_ns(max_iterations=200))
        assert rc == 2


class TestEmissionShape:
    def test_no_change_emits_nothing(self, capsys):
        with patch(
            _API_MOCK_PATH,
            side_effect=[_no_change("msg:|evt:1"), _terminal_event("msg:|evt:2")],
        ):
            cmd_pipeline_wait_status(_ns(max_iterations=5))
        out = capsys.readouterr().out.strip().splitlines()
        # Exactly one line — the terminal event. No-change is silent.
        assert len(out) == 1
        assert json.loads(out[0])["event_type"] == "pipeline.completed"

    def test_event_line_carries_dashboard_fields(self, capsys):
        with patch(
            _API_MOCK_PATH,
            side_effect=[_phase_event("msg:|evt:1"), _terminal_event("msg:|evt:2")],
        ):
            cmd_pipeline_wait_status(_ns(max_iterations=5))
        out = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert len(out) == 2
        first = out[0]
        assert first["trigger"] == "event"
        assert first["event_type"] == "phase.started"
        assert first["current_phase"] == "implement"
        assert first["status"] == "running"
        assert first["phase_elapsed_seconds"] == 5
        assert "concurrent" in first
        # Bulk snapshot fields are NOT on the JSON-line.
        assert "running_agents" not in first
        assert "completed_agents" not in first
        assert "recent_messages" not in first
        assert "pipeline" not in first

    def test_message_line_carries_messages_array(self, capsys):
        with patch(
            _API_MOCK_PATH,
            side_effect=[_message_event("msg:m-1|evt:1"), _terminal_event("msg:|evt:2")],
        ):
            cmd_pipeline_wait_status(_ns(max_iterations=5))
        out = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        first = out[0]
        assert first["trigger"] == "message"
        assert first["messages"][0]["message_type"] == "OVERSEER_ALERT"
        assert "event_type" not in first


class TestCursorThreading:
    def test_first_call_omits_since(self):
        with patch(_API_MOCK_PATH, side_effect=[_terminal_event("msg:|evt:1")]) as mock:
            cmd_pipeline_wait_status(_ns(since="", max_iterations=5))
        endpoint = mock.call_args_list[0][0][1]
        assert "since=" not in endpoint
        assert "wait=1" in endpoint

    def test_explicit_since_threaded_into_first_call(self):
        with patch(_API_MOCK_PATH, side_effect=[_terminal_event("msg:|evt:9")]) as mock:
            cmd_pipeline_wait_status(_ns(since="msg:abc|evt:5", max_iterations=5))
        endpoint = mock.call_args_list[0][0][1]
        assert "since=" in endpoint

    def test_response_cursor_threaded_into_next_call(self):
        with patch(
            _API_MOCK_PATH,
            side_effect=[
                _phase_event("msg:|evt:7"),
                _terminal_event("msg:|evt:8"),
            ],
        ) as mock:
            cmd_pipeline_wait_status(_ns(max_iterations=5))
        first_endpoint = mock.call_args_list[0][0][1]
        second_endpoint = mock.call_args_list[1][0][1]
        assert "since=" not in first_endpoint
        # Cursor from the first response shows up in the second call's URL,
        # URL-encoded (the ``|`` becomes %7C).
        assert "since=msg" in second_endpoint
        assert "%7C" in second_endpoint

    def test_path_b_cursor_threaded_into_next_call(self):
        """Pin: a Path-B (no_change) response's cursor is threaded into
        the next call's ``since`` exactly like a Path-A cursor.

        Without this, a regression that resets the cursor only on
        Path-B would silently re-start the wait at tip on every quiet
        stretch — events that fired during the no-change window would
        be missed by the next call's subscription.
        """
        with patch(
            _API_MOCK_PATH,
            side_effect=[
                _no_change("msg:abc|evt:5"),
                _terminal_event("msg:|evt:6"),
            ],
        ) as mock:
            cmd_pipeline_wait_status(_ns(max_iterations=5))
        first_endpoint = mock.call_args_list[0][0][1]
        second_endpoint = mock.call_args_list[1][0][1]
        assert "since=" not in first_endpoint
        # Cursor from the no_change response is URL-encoded
        # (``msg:abc|evt:5`` → ``msg%3Aabc%7Cevt%3A5``) and appears on
        # the second call's URL.
        assert "since=msg%3Aabc%7Cevt%3A5" in second_endpoint


def _no_change_terminal(
    cursor: str = "msg:|evt:0",
    status: str = "failed",
    current_phase: str = "implement",
) -> dict[str, Any]:
    """Path-B (no_change) envelope carrying a terminal ``status`` field.

    Mirrors the shape that ``_build_minimal_status_envelope`` would
    produce on a hypothetical late-subscriber path that wasn't caught
    by the server-side short-circuit (issue #2378). Used to exercise
    the CLI's defense-in-depth check.
    """
    return {
        "success": True,
        "data": {
            "changed": False,
            "no_change": True,
            "cursor": cursor,
            "current_phase": current_phase,
            "status": status,
        },
    }


class TestPathBTerminalShortCircuit:
    """Issue #2378 defense-in-depth: a Path-B no_change envelope whose
    ``status`` field carries a terminal value (``failed``/``complete``/
    ``cancelled``) must end the loop with exit 0 and one synthetic
    JSON line, instead of looping silently.
    """

    @pytest.mark.parametrize("status", ["failed", "complete", "cancelled"])
    def test_no_change_with_terminal_status_returns_zero(
        self, capsys: pytest.CaptureFixture[str], status: str
    ) -> None:
        with patch(_API_MOCK_PATH, side_effect=[_no_change_terminal(status=status)]) as mock:
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))

        assert rc == 0
        # Loop exited after one call — no second iteration.
        assert mock.call_count == 1
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        line = json.loads(out[0])
        assert line["trigger"] == "synthetic-terminal"
        assert line["status"] == status
        assert line["current_phase"] == "implement"
        assert line["cursor"] == "msg:|evt:0"
        # event_type mirrors the Path-A line shape so downstream
        # consumers can key off it uniformly (issue #2378 review).
        expected_event = {
            "failed": "pipeline.failed",
            "complete": "pipeline.completed",
            "cancelled": "pipeline.cancelled",
        }[status]
        assert line["event_type"] == expected_event

    def test_no_change_running_still_loops(self):
        """Regression guard: only terminal statuses short-circuit Path-B."""
        running = _no_change_terminal(status="running")
        with patch(
            _API_MOCK_PATH,
            side_effect=[running, running, _terminal_event("msg:|evt:9")],
        ) as mock:
            rc = cmd_pipeline_wait_status(_ns(max_iterations=5))

        assert rc == 0
        assert mock.call_count == 3


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Ensure the CLI hits a deterministic URL during tests."""
    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://test-orchestrator:9849")
