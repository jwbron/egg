"""
Tests for `egg-orch overseer alert` (issue #1784).

The overseer agent must escalate anomalies via OVERSEER_ALERT messages, not
HANDOFF/STATUS. This subcommand wraps the message-send endpoint so the
overseer never has to pick the message type by hand. These tests assert:

- The subcommand is registered and accepts the documented flags.
- Required flags (--anomaly, --priority, --summary) are enforced by argparse.
- The POST payload always carries message_type=OVERSEER_ALERT and to_role=all,
  regardless of any other flag the caller passed.
- --priority is restricted to the documented choices.
- The from_role is taken from --role, then EGG_AGENT_ROLE, then defaults to
  "overseer" so the alert is attributable even when the caller forgets.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_lib.orch_cli import cmd_overseer_alert, create_parser  # noqa: E402


def _parse(argv: list[str]) -> argparse.Namespace:
    return create_parser().parse_args(argv)


class TestParserRegistration:
    """`overseer alert` is wired into the parser with the documented flags."""

    def test_required_flags_parse(self):
        args = _parse(
            [
                "overseer",
                "alert",
                "p1",
                "--anomaly",
                "stuck-phase-transition",
                "--priority",
                "high",
                "--summary",
                "BRC complete but no transition for 90s",
            ]
        )
        assert args.command == "overseer"
        assert args.overseer_command == "alert"
        assert args.anomaly == "stuck-phase-transition"
        assert args.priority == "high"
        assert args.summary == "BRC complete but no transition for 90s"
        assert args.detail is None
        assert args.recommend is None
        assert args.func is cmd_overseer_alert

    def test_anomaly_required(self):
        with pytest.raises(SystemExit):
            _parse(
                [
                    "overseer",
                    "alert",
                    "p1",
                    "--priority",
                    "high",
                    "--summary",
                    "x",
                ]
            )

    def test_priority_required(self):
        with pytest.raises(SystemExit):
            _parse(
                [
                    "overseer",
                    "alert",
                    "p1",
                    "--anomaly",
                    "agent-stall",
                    "--summary",
                    "x",
                ]
            )

    def test_summary_required(self):
        with pytest.raises(SystemExit):
            _parse(
                [
                    "overseer",
                    "alert",
                    "p1",
                    "--anomaly",
                    "agent-stall",
                    "--priority",
                    "high",
                ]
            )

    def test_priority_choices_enforced(self):
        with pytest.raises(SystemExit):
            _parse(
                [
                    "overseer",
                    "alert",
                    "p1",
                    "--anomaly",
                    "x",
                    "--priority",
                    "critical",  # not a documented choice
                    "--summary",
                    "x",
                ]
            )


class TestPayloadShape:
    """Posted payload always carries the right type and routing."""

    def _run(self, argv: list[str], mock_request: MagicMock) -> tuple[str, dict[str, Any]]:
        mock_request.return_value = {
            "success": True,
            "data": {"message": {"id": "msg-1"}},
        }
        rc = cmd_overseer_alert(_parse(argv))
        assert rc == 0
        assert mock_request.called
        endpoint, kwargs = mock_request.call_args[0][0], mock_request.call_args[1]
        return endpoint, kwargs

    @patch("egg_lib.orch_cli.orch_request")
    def test_message_type_is_overseer_alert(self, mock_request):
        endpoint, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--anomaly",
                "stuck-phase-transition",
                "--priority",
                "high",
                "--summary",
                "BRC complete, no transition",
            ],
            mock_request,
        )
        assert endpoint == "/api/v1/pipelines/pipe-1/messages"
        assert kwargs["method"] == "POST"
        assert kwargs["data"]["message_type"] == "OVERSEER_ALERT"

    @patch("egg_lib.orch_cli.orch_request")
    def test_to_role_is_always_all(self, mock_request):
        _, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--anomaly",
                "stuck-phase-transition",
                "--priority",
                "high",
                "--summary",
                "x",
            ],
            mock_request,
        )
        assert kwargs["data"]["to_role"] == "all"

    @patch("egg_lib.orch_cli.orch_request")
    def test_subject_includes_anomaly_and_priority(self, mock_request):
        _, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--anomaly",
                "agent-loop",
                "--priority",
                "medium",
                "--summary",
                "coder is repeating the same edit",
            ],
            mock_request,
        )
        assert kwargs["data"]["subject"] == "agent-loop [medium]"

    @patch("egg_lib.orch_cli.orch_request")
    def test_body_combines_summary_detail_recommend(self, mock_request):
        _, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--anomaly",
                "stuck-phase-transition",
                "--priority",
                "high",
                "--summary",
                "BRC complete, no transition",
                "--detail",
                "consensus.state=confirmed at 17:44, still in refine at 17:46",
                "--recommend",
                "manually advance to plan",
            ],
            mock_request,
        )
        body = kwargs["data"]["body"]
        assert "BRC complete, no transition" in body
        assert "Detail:" in body
        assert "consensus.state=confirmed" in body
        assert "Recommended action:" in body
        assert "manually advance to plan" in body

    @patch("egg_lib.orch_cli.orch_request")
    def test_role_from_env(self, mock_request, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "overseer")
        _, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--anomaly",
                "x",
                "--priority",
                "low",
                "--summary",
                "x",
            ],
            mock_request,
        )
        assert kwargs["data"]["from_role"] == "overseer"

    @patch("egg_lib.orch_cli.orch_request")
    def test_role_defaults_to_overseer_when_env_missing(self, mock_request, monkeypatch):
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        _, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--anomaly",
                "x",
                "--priority",
                "low",
                "--summary",
                "x",
            ],
            mock_request,
        )
        assert kwargs["data"]["from_role"] == "overseer"

    @patch("egg_lib.orch_cli.orch_request")
    def test_explicit_role_flag_wins(self, mock_request, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
        _, kwargs = self._run(
            [
                "overseer",
                "alert",
                "pipe-1",
                "--role",
                "sentinel_agent",
                "--anomaly",
                "x",
                "--priority",
                "low",
                "--summary",
                "x",
            ],
            mock_request,
        )
        assert kwargs["data"]["from_role"] == "sentinel_agent"


class TestExitCodes:
    """Non-success responses surface as a non-zero exit code."""

    @patch("egg_lib.orch_cli.orch_request")
    def test_failure_returns_nonzero(self, mock_request, capsys):
        mock_request.return_value = {"success": False, "message": "boom"}
        rc = cmd_overseer_alert(
            _parse(
                [
                    "overseer",
                    "alert",
                    "pipe-1",
                    "--anomaly",
                    "x",
                    "--priority",
                    "low",
                    "--summary",
                    "x",
                ]
            )
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "boom" in err

    @patch("egg_lib.orch_cli.orch_request")
    def test_json_success_returns_zero(self, mock_request, capsys):
        mock_request.return_value = {
            "success": True,
            "data": {"message": {"id": "msg-42"}},
        }
        rc = cmd_overseer_alert(
            _parse(
                [
                    "overseer",
                    "alert",
                    "pipe-1",
                    "--anomaly",
                    "agent-loop",
                    "--priority",
                    "medium",
                    "--summary",
                    "coder looping",
                    "--json",
                ]
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        import json

        payload = json.loads(out)
        assert payload["success"] is True

    @patch("egg_lib.orch_cli.orch_request")
    def test_json_failure_returns_nonzero(self, mock_request, capsys):
        mock_request.return_value = {"success": False, "message": "auth denied"}
        rc = cmd_overseer_alert(
            _parse(
                [
                    "overseer",
                    "alert",
                    "pipe-1",
                    "--anomaly",
                    "x",
                    "--priority",
                    "low",
                    "--summary",
                    "x",
                    "--json",
                ]
            )
        )
        assert rc == 1
        out = capsys.readouterr().out
        import json

        payload = json.loads(out)
        assert payload["success"] is False
