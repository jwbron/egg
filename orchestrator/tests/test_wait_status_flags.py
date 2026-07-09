"""Tests for the ``--exclude-types`` / ``--quiet`` client-side print filters
added to ``skills/sdlc/bin/wait-status`` for PR B (issue #3364, task-1-1).

These flags gate ONLY the printed JSON line — cursor threading and terminal
exit codes are decided independently upstream — so the headline regression
guard (AC-B2) is that with no flags set the filter is a pure pass-through and
the default JSON-lines output is byte-for-byte unchanged.

The launcher is an extension-less, pure-stdlib vendored script, so (as the
standalone-parity drift guard in ``tests/sandbox/`` does) we import it via a
``SourceFileLoader`` rather than a normal ``import``.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# repo-root/orchestrator/tests/<this file> → parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_SCRIPT = _REPO_ROOT / "skills" / "sdlc" / "bin" / "wait-status"


def _load_skill_module() -> Any:
    """Import the extension-less standalone launcher as a module."""
    loader = importlib.machinery.SourceFileLoader("skill_wait_status_flags", str(_SKILL_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ws = _load_skill_module()


# ---------------------------------------------------------------------------
# _parse_exclude_types — comma-split, whitespace-strip, empty-drop
# ---------------------------------------------------------------------------


class TestParseExcludeTypes:
    def test_empty_yields_empty_set(self) -> None:
        assert ws._parse_exclude_types("") == frozenset()

    def test_single_type(self) -> None:
        assert ws._parse_exclude_types("phase.started") == frozenset({"phase.started"})

    def test_comma_split_strips_whitespace_and_drops_empties(self) -> None:
        # Trailing comma + interior whitespace must not leak empty entries.
        assert ws._parse_exclude_types("phase.started, phase.completed,") == frozenset(
            {"phase.started", "phase.completed"}
        )


# ---------------------------------------------------------------------------
# _line_is_essential — what survives --quiet
# ---------------------------------------------------------------------------


class TestLineIsEssential:
    @pytest.mark.parametrize(
        "event_type",
        ["pipeline.completed", "pipeline.failed", "pipeline.cancelled"],
    )
    def test_terminal_events_are_essential(self, event_type: str) -> None:
        assert ws._line_is_essential({"event_type": event_type}) is True

    @pytest.mark.parametrize("event_type", ["decision.created", "slice.closed"])
    def test_allowlisted_events_are_essential(self, event_type: str) -> None:
        assert ws._line_is_essential({"event_type": event_type}) is True

    @pytest.mark.parametrize("trigger", ["synthetic-terminal", "message"])
    def test_trigger_lines_are_essential(self, trigger: str) -> None:
        assert ws._line_is_essential({"trigger": trigger}) is True

    @pytest.mark.parametrize(
        "event_type",
        ["phase.started", "phase.completed", "agent.started", "decision.resolved"],
    )
    def test_non_essential_events(self, event_type: str) -> None:
        assert ws._line_is_essential({"trigger": "event", "event_type": event_type}) is False


# ---------------------------------------------------------------------------
# _should_emit_line — the print gate itself
# ---------------------------------------------------------------------------


class TestShouldEmitLine:
    def test_no_flags_is_pure_passthrough(self) -> None:
        """AC-B2 unit-level guard: empty excludes + quiet False never filters,
        even for a line that --quiet would otherwise drop."""
        line = {"trigger": "event", "event_type": "phase.started"}
        assert ws._should_emit_line(line, frozenset(), False) is True

    def test_exclude_types_drops_matching_line(self) -> None:
        line = {"trigger": "event", "event_type": "phase.started"}
        assert ws._should_emit_line(line, frozenset({"phase.started"}), False) is False

    def test_exclude_types_keeps_unmatched_line(self) -> None:
        line = {"trigger": "event", "event_type": "phase.completed"}
        assert ws._should_emit_line(line, frozenset({"phase.started"}), False) is True

    def test_quiet_drops_non_essential(self) -> None:
        line = {"trigger": "event", "event_type": "phase.started"}
        assert ws._should_emit_line(line, frozenset(), True) is False

    def test_quiet_keeps_slice_closed(self) -> None:
        line = {"trigger": "event", "event_type": "slice.closed"}
        assert ws._should_emit_line(line, frozenset(), True) is True

    def test_quiet_keeps_terminal(self) -> None:
        line = {"trigger": "event", "event_type": "pipeline.completed"}
        assert ws._should_emit_line(line, frozenset(), True) is True

    def test_exclude_types_can_drop_an_essential_type_even_under_quiet(self) -> None:
        # --exclude-types is checked first and unconditionally, so an operator
        # can drop even an otherwise-essential type explicitly.
        line = {"trigger": "event", "event_type": "slice.closed"}
        assert ws._should_emit_line(line, frozenset({"slice.closed"}), True) is False


# ---------------------------------------------------------------------------
# End-to-end through cmd_pipeline_wait_status — the observable stdout contract
# ---------------------------------------------------------------------------


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
        },
    }


def _terminal_event(cursor: str) -> dict[str, Any]:
    body = _phase_event(cursor, event_type="pipeline.completed")
    body["data"]["status"] = "complete"
    return body


def _slice_closed_event(cursor: str, outcome: str = "complete") -> dict[str, Any]:
    body = _phase_event(cursor, event_type="slice.closed")
    body["data"]["outcome"] = outcome
    return body


def _run(
    responses: list[Any], capsys: pytest.CaptureFixture[str], **ns_over: Any
) -> tuple[int, str]:
    ns = argparse.Namespace(
        pipeline_id="issue-3364",
        since="",
        inner_timeout=1,
        max_iterations=5,
    )
    for key, value in ns_over.items():
        setattr(ns, key, value)
    with patch.object(ws, "api_request", side_effect=list(responses)), patch("time.sleep"):
        rc = ws.cmd_pipeline_wait_status(ns)
    return rc, capsys.readouterr().out


def _event_types(out: str) -> list[str]:
    return [json.loads(line).get("event_type") for line in out.strip().splitlines() if line]


class TestEndToEndFilters:
    def test_default_output_unchanged_when_no_flags(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC-B2 regression guard: without the flags every line still prints."""
        responses = [_phase_event("msg:|evt:1"), _terminal_event("msg:|evt:2")]
        rc, out = _run(responses, capsys)
        assert rc == 0
        assert _event_types(out) == ["phase.started", "pipeline.completed"]

    def test_exclude_types_drops_targeted_type_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        responses = [
            _phase_event("msg:|evt:1", event_type="phase.started"),
            _phase_event("msg:|evt:2", event_type="agent.started"),
            _terminal_event("msg:|evt:3"),
        ]
        rc, out = _run(responses, capsys, exclude_types="phase.started")
        assert rc == 0
        # phase.started dropped; the untargeted agent.started + terminal remain.
        assert _event_types(out) == ["agent.started", "pipeline.completed"]

    def test_exclude_types_does_not_affect_terminal_exit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Filtering the terminal line still returns the terminal exit code —
        the gate is print-only, not control-flow."""
        responses = [_terminal_event("msg:|evt:1")]
        rc, out = _run(responses, capsys, exclude_types="pipeline.completed")
        assert rc == 0
        assert out.strip() == ""

    def test_quiet_keeps_slice_closed_and_terminal_drops_phase(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        responses = [
            _phase_event("msg:|evt:1", event_type="phase.started"),
            _slice_closed_event("msg:|evt:2", outcome="complete"),
            _terminal_event("msg:|evt:3"),
        ]
        rc, out = _run(responses, capsys, quiet=True)
        assert rc == 0
        # phase.started suppressed; slice.closed + terminal survive --quiet.
        assert _event_types(out) == ["slice.closed", "pipeline.completed"]
