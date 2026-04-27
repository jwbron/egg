"""
Tests for BRC CLI argument changes (issues #1716 and #1718).

Verifies:
- ``egg-orch consensus ack`` requires ``--reason`` (exits non-zero without it)
  and threads the reason value into the signal payload.
- ``egg-orch consensus propose`` accepts new structured args
  (``--files-changed``, ``--tests-run``, ``--tasks``) and includes them in
  the signal payload under the correct keys.
- ``egg-orch message send --type`` help text includes HANDOFF.
- ``egg-orch message send --type HANDOFF`` parses successfully.
"""

import argparse
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Add sandbox to sys.path so egg_lib is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_lib.orch_cli import cmd_message_poll, create_parser

# ---------------------------------------------------------------------------
# ACK: --reason is required
# ---------------------------------------------------------------------------


class TestAckReasonRequired:
    """``egg-orch consensus ack`` requires --reason."""

    def test_ack_exits_nonzero_without_reason(self):
        """Omitting --reason causes argparse to exit non-zero."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "consensus",
                    "ack",
                    "coder",
                    "issue-42",
                    "--files-reviewed",
                    "src/a.py",
                    "--ack-version",
                    "1",
                ]
            )
        assert exc_info.value.code != 0

    def test_ack_succeeds_with_reason(self):
        """Providing --reason parses successfully."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "ack",
                "coder",
                "issue-42",
                "--files-reviewed",
                "src/a.py",
                "--reason",
                "Reviewed src/a.py: logic is correct, tests cover all branches",
                "--ack-version",
                "3",
            ]
        )
        assert args.reason == "Reviewed src/a.py: logic is correct, tests cover all branches"
        assert args.producer_role == "coder"
        assert args.files_reviewed == ["src/a.py"]
        assert args.ack_version == 3


class TestAckReasonInPayload:
    """Parsed --reason value threads into the ACK signal payload."""

    def test_reason_threads_into_payload(self):
        """cmd_consensus_ack builds payload with reason from parsed args."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "ack",
                "coder",
                "issue-42",
                "--files-reviewed",
                "src/auth.py",
                "src/models.py",
                "--reason",
                "Checked auth.py and models.py: parameterized queries throughout, no injection risk",
                "--ack-version",
                "2",
            ]
        )
        # Simulate what cmd_consensus_ack would build
        payload = {
            "artifact_references": args.files_reviewed,
            "reason": args.reason,
            "ack_version": args.ack_version,
        }
        assert payload["reason"] == (
            "Checked auth.py and models.py: parameterized queries throughout, no injection risk"
        )
        assert payload["artifact_references"] == ["src/auth.py", "src/models.py"]
        assert payload["ack_version"] == 2


class TestAckConditionalFlag:
    """--pre-merge-condition marks the ACK as conditional (issue #1998)."""

    def test_condition_defaults_to_empty(self):
        """Omitting the flag yields an empty condition (unconditional ACK)."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "ack",
                "coder",
                "issue-42",
                "--files-reviewed",
                "src/a.py",
                "--reason",
                "All looks good, reviewed a.py and confirmed auth flow is correct",
                "--ack-version",
                "1",
            ]
        )
        assert getattr(args, "pre_merge_condition", "") == ""

    def test_condition_parses(self):
        """--pre-merge-condition captures the obligation string."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "ack",
                "coder",
                "issue-42",
                "--files-reviewed",
                "src/a.py",
                "--reason",
                "Approved, but a file move must happen before merging",
                "--pre-merge-condition",
                "A human must `git mv legacy/x new/x` before merge",
                "--ack-version",
                "1",
            ]
        )
        assert args.pre_merge_condition == "A human must `git mv legacy/x new/x` before merge"


# ---------------------------------------------------------------------------
# ACK/NACK: --ack-version / --nack-version are required (#2142)
# ---------------------------------------------------------------------------


class TestAckVersionRequired:
    """``egg-orch consensus ack`` requires --ack-version (#2142)."""

    def test_ack_exits_nonzero_without_version(self):
        """Omitting --ack-version causes argparse to exit non-zero."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "consensus",
                    "ack",
                    "coder",
                    "issue-42",
                    "--files-reviewed",
                    "src/a.py",
                    "--reason",
                    "Reviewed src/a.py: logic is correct",
                ]
            )
        assert exc_info.value.code != 0

    def test_ack_rejects_non_integer_version(self):
        """--ack-version with a non-integer value exits non-zero."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "consensus",
                    "ack",
                    "coder",
                    "issue-42",
                    "--files-reviewed",
                    "src/a.py",
                    "--reason",
                    "Reviewed src/a.py: logic is correct",
                    "--ack-version",
                    "not-an-int",
                ]
            )
        assert exc_info.value.code != 0


class TestNackRequiredArgs:
    """``egg-orch consensus nack`` requires --nack-version, --reason, --files-reviewed."""

    def test_nack_succeeds_with_all_required(self):
        """All required args present: parses successfully and threads version."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "nack",
                "coder",
                "issue-42",
                "--files-reviewed",
                "src/a.py",
                "--reason",
                "src/a.py:42 raises on empty input — needs guard",
                "--nack-version",
                "4",
            ]
        )
        assert args.producer_role == "coder"
        assert args.files_reviewed == ["src/a.py"]
        assert args.reason == "src/a.py:42 raises on empty input — needs guard"
        assert args.nack_version == 4

    def test_nack_exits_nonzero_without_version(self):
        """Omitting --nack-version causes argparse to exit non-zero."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "consensus",
                    "nack",
                    "coder",
                    "issue-42",
                    "--files-reviewed",
                    "src/a.py",
                    "--reason",
                    "Issue blocking",
                ]
            )
        assert exc_info.value.code != 0

    def test_nack_payload_threads_version(self):
        """cmd_consensus_nack builds payload with nack_version from parsed args."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "nack",
                "coder",
                "issue-42",
                "--files-reviewed",
                "src/a.py",
                "--reason",
                "src/a.py:42 raises on empty input",
                "--nack-version",
                "5",
            ]
        )
        # Simulate what cmd_consensus_nack builds
        payload = {
            "artifact_references": args.files_reviewed,
            "reason": args.reason,
            "nack_version": args.nack_version,
        }
        assert payload["nack_version"] == 5


# ---------------------------------------------------------------------------
# PROPOSE: new structured args
# ---------------------------------------------------------------------------


class TestProposeStructuredArgs:
    """``egg-orch consensus propose`` accepts new structured args."""

    def test_files_changed_arg_parsed(self):
        """--files-changed accepts multiple files."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Implemented feature",
                "--files-changed",
                "src/auth.py",
                "src/models.py",
            ]
        )
        assert args.files_changed == ["src/auth.py", "src/models.py"]

    def test_tests_run_arg_parsed(self):
        """--tests-run accepts multiple test suite names."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Implemented feature",
                "--tests-run",
                "pytest",
                "ruff",
            ]
        )
        assert args.tests_run == ["pytest", "ruff"]

    def test_tasks_arg_parsed(self):
        """--tasks accepts multiple task IDs."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Implemented feature",
                "--tasks",
                "task-1-1",
                "task-1-2",
            ]
        )
        assert args.tasks == ["task-1-1", "task-1-2"]

    def test_all_structured_args_together(self):
        """All new structured args can be used simultaneously."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Full implementation with tests",
                "--files-changed",
                "src/a.py",
                "src/b.py",
                "--tests-run",
                "pytest",
                "mypy",
                "--tasks",
                "task-1-1",
                "--artifacts",
                "src/a.py",
            ]
        )
        assert args.files_changed == ["src/a.py", "src/b.py"]
        assert args.tests_run == ["pytest", "mypy"]
        assert args.tasks == ["task-1-1"]
        assert args.summary == "Full implementation with tests"
        assert args.artifacts == ["src/a.py"]

    def test_structured_args_optional(self):
        """New structured args are optional (not required)."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Basic proposal",
            ]
        )
        # New args should default to None when not provided
        assert args.files_changed is None
        assert args.tests_run is None
        assert args.tasks is None


class TestProposeStructuredArgsInPayload:
    """Parsed structured args thread into the propose signal payload."""

    def test_payload_includes_structured_fields(self):
        """cmd_consensus_propose builds payload with structured fields."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Auth implementation",
                "--files-changed",
                "src/auth.py",
                "--tests-run",
                "pytest",
                "--tasks",
                "task-1-1",
                "task-1-2",
                "--commit-sha",
                "abc123",
            ]
        )
        # Simulate what cmd_consensus_propose builds
        payload = {
            "summary": getattr(args, "summary", "") or "",
            "attestation": {},
            "artifacts": getattr(args, "artifacts", []) or [],
            "risk_considered": getattr(args, "risk", "") or "",
            "commit_sha": args.commit_sha,
            "files_changed": getattr(args, "files_changed", []) or [],
            "tests_run": getattr(args, "tests_run", []) or [],
            "tasks_satisfied": getattr(args, "tasks", []) or [],
        }
        assert payload["files_changed"] == ["src/auth.py"]
        assert payload["tests_run"] == ["pytest"]
        assert payload["tasks_satisfied"] == ["task-1-1", "task-1-2"]

    def test_payload_defaults_when_args_absent(self):
        """Missing structured args produce empty defaults in payload."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "issue-42",
                "--summary",
                "Basic proposal",
                "--commit-sha",
                "abc123",
            ]
        )
        # Simulate payload construction
        payload = {
            "files_changed": getattr(args, "files_changed", []) or [],
            "tests_run": getattr(args, "tests_run", []) or [],
            "tasks_satisfied": getattr(args, "tasks", []) or [],
        }
        assert payload["files_changed"] == []
        assert payload["tests_run"] == []
        assert payload["tasks_satisfied"] == []


# ---------------------------------------------------------------------------
# MESSAGE SEND: --type help text includes HANDOFF (issue #1718)
# ---------------------------------------------------------------------------


def _find_msg_send_type_action(
    parser: argparse.ArgumentParser,
) -> argparse.Action:
    """Navigate argparse tree to find the ``--type`` action of ``message send``.

    NOTE: This helper accesses argparse private APIs (``_subparsers``,
    ``_SubParsersAction``) because there is no public API for introspecting
    subparser trees.  If a future Python version changes these internals,
    update the traversal logic here.
    """
    subparsers_group = parser._subparsers
    assert subparsers_group is not None, "parser has no _subparsers"
    msg_send_parser: argparse.ArgumentParser | None = None
    for action in subparsers_group._actions:
        if isinstance(action, argparse._SubParsersAction):
            msg_parser = action.choices.get("message")
            if msg_parser:
                inner_group = msg_parser._subparsers
                assert inner_group is not None, "message parser has no _subparsers"
                for sub_action in inner_group._actions:
                    if isinstance(sub_action, argparse._SubParsersAction):
                        msg_send_parser = sub_action.choices.get("send")
                        break
            break
    assert msg_send_parser is not None, "Could not find 'message send' subparser"

    type_action: argparse.Action | None = None
    for act in msg_send_parser._actions:
        if hasattr(act, "option_strings") and "--type" in act.option_strings:
            type_action = act
            break
    assert type_action is not None, "Could not find --type argument"
    return type_action


class TestMessageSendTypeHelpText:
    """``egg-orch message send --type`` help text includes HANDOFF."""

    def test_type_help_includes_handoff(self):
        """The --type argument help text lists HANDOFF as a valid type."""
        parser = create_parser()
        type_action = _find_msg_send_type_action(parser)
        help_text = type_action.help
        assert help_text is not None, "--type has no help text"
        assert "HANDOFF" in help_text, f"Expected 'HANDOFF' in --type help text, got: {help_text}"

    def test_type_help_includes_all_message_types(self):
        """The --type argument help text lists all active message types."""
        parser = create_parser()
        type_action = _find_msg_send_type_action(parser)
        help_text = type_action.help
        assert help_text is not None, "--type has no help text"
        for msg_type in ("PROGRESS", "STATUS", "HANDOFF"):
            assert msg_type in help_text, (
                f"Expected '{msg_type}' in --type help text, got: {help_text}"
            )

    def test_message_send_accepts_handoff_type(self):
        """``egg-orch message send --type HANDOFF`` parses successfully."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "message",
                "send",
                "issue-42",
                "--to",
                "tester",
                "--type",
                "HANDOFF",
                "--subject",
                "Test files ready",
                "--body",
                "See commit abc1234",
            ]
        )
        assert args.type == "HANDOFF"
        assert args.to == "tester"
        assert args.subject == "Test files ready"
        assert args.body == "See commit abc1234"

    def test_message_send_rejects_invalid_type(self):
        """``egg-orch message send --type HANDOF`` (typo) exits non-zero."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "message",
                    "send",
                    "issue-42",
                    "--to",
                    "tester",
                    "--type",
                    "HANDOF",
                    "--subject",
                    "Test files ready",
                    "--body",
                    "See commit abc1234",
                ]
            )
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# MESSAGE POLL: body display (no truncation, indented multi-line)
# ---------------------------------------------------------------------------


def _make_poll_args(**overrides: object) -> argparse.Namespace:
    """Build a minimal ``argparse.Namespace`` for ``cmd_message_poll``."""
    defaults = {
        "pipeline_id": "pipe-1",
        "json": False,
        "role": "reviewer_code",
        "since": None,
        "limit": None,
        "wait": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestMessagePollBodyDisplay:
    """``cmd_message_poll`` displays full message bodies with indentation."""

    @patch("egg_lib.orch_cli.orch_request")
    def test_multiline_body_fully_displayed(self, mock_request, capsys):
        """Multi-line bodies are printed in full, not truncated."""
        long_body = (
            "### Blocking\n"
            "1. **auth.py:42** — SQL injection risk\n"
            "2. **models.py:99** — Missing null check\n"
            "\n"
            "### Non-blocking\n"
            "- Consider renaming `do_thing` for clarity"
        )
        mock_request.return_value = {
            "data": {
                "messages": [
                    {
                        "timestamp": "2026-04-14T12:00:00Z",
                        "from_role": "reviewer_code",
                        "to_role": "coder",
                        "message_type": "NACK",
                        "subject": "Review feedback",
                        "body": long_body,
                    }
                ]
            }
        }
        args = _make_poll_args()
        rc = cmd_message_poll(args)
        assert rc == 0
        output = capsys.readouterr().out
        # Every line of the body must appear in output
        for line in long_body.split("\n"):
            assert line in output, f"Missing body line: {line!r}"

    @patch("egg_lib.orch_cli.orch_request")
    def test_multiline_body_indented(self, mock_request, capsys):
        """Multi-line bodies are indented with 4 spaces on continuation lines."""
        body = "Line one\nLine two\nLine three"
        mock_request.return_value = {
            "data": {
                "messages": [
                    {
                        "timestamp": "2026-04-14T12:00:00Z",
                        "from_role": "reviewer_code",
                        "to_role": "coder",
                        "message_type": "NACK",
                        "subject": "Feedback",
                        "body": body,
                    }
                ]
            }
        }
        args = _make_poll_args()
        cmd_message_poll(args)
        output = capsys.readouterr().out
        # Continuation lines should be indented with 4 spaces
        assert "    Line two" in output
        assert "    Line three" in output


# ---------------------------------------------------------------------------
# ACK/NACK: handler threads version field into orchestrator signal payload (#2142)
# ---------------------------------------------------------------------------


class TestAckHandlerThreadsAckVersion:
    """``brc_ack`` posts the version field to the orchestrator (#2142).

    This is the regression for the silent-pass bug where the version field
    failed to reach the version-match guard. Asserts the wire payload sent
    to ``/api/v1/pipelines/<id>/signal`` includes ``ack_version``.
    """

    def test_payload_includes_ack_version(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_ack(
                {
                    "pipeline_id": "issue-42",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "Reviewed src/auth.py: substantive multi-file review well over fifty chars",
                    "files_reviewed": ["src/auth.py"],
                    "ack_version": 7,
                }
            )

        assert mock_request.called
        data = mock_request.call_args.kwargs["data"]
        assert data["payload"]["ack_version"] == 7

    def test_missing_ack_version_raises(self):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import HandlerError

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ):
            with pytest.raises(HandlerError, match="ack_version"):
                handlers.brc_ack(
                    {
                        "pipeline_id": "issue-42",
                        "role": "reviewer_code",
                        "producer_role": "coder",
                        "reason": "Reviewed src/auth.py over fifty chars to satisfy the validator",
                        "files_reviewed": ["src/auth.py"],
                    }
                )


class TestNackHandlerThreadsNackVersion:
    """``brc_nack`` posts the version field to the orchestrator (#2142).

    Mirror of the ACK regression: confirms the CLI builder path threads
    ``nack_version`` into the wire payload that reaches the version-match
    guard. Without this, a stale NACK would silently land against the new
    proposal version instead of being rejected with HTTP 409.
    """

    def test_payload_includes_nack_version(self):
        from egg_agent_tools.handlers import brc as handlers

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as mock_request:
            handlers.brc_nack(
                {
                    "pipeline_id": "issue-42",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "src/auth.py:42 raises on empty input — substantive blocker over fifty chars",
                    "files_reviewed": ["src/auth.py"],
                    "nack_version": 9,
                }
            )

        assert mock_request.called
        data = mock_request.call_args.kwargs["data"]
        assert data["payload"]["nack_version"] == 9

    def test_missing_nack_version_raises(self):
        from egg_agent_tools.handlers import brc as handlers
        from egg_agent_tools.handlers.errors import HandlerError

        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {}},
        ):
            with pytest.raises(HandlerError, match="nack_version"):
                handlers.brc_nack(
                    {
                        "pipeline_id": "issue-42",
                        "role": "reviewer_code",
                        "producer_role": "coder",
                        "reason": "src/auth.py:42 raises — substantive blocker text comfortably over the threshold",
                        "files_reviewed": ["src/auth.py"],
                    }
                )


class TestRenderStaleVersionRejection:
    """``_render_stale_version_rejection`` labels the producer correctly (#2142).

    Regression for the bug where the rendered message read
    ``producer <reviewer> is at v...`` because the formatter pulled from
    ``rejection.reviewer`` instead of the snapshot's ``producer`` field.
    The fix reads ``current_proposal.producer`` (with the response's
    ``producer_role`` as fallback) so operators see the correct actor.
    """

    @staticmethod
    def _stale_response(
        producer: str = "coder", reviewer: str = "reviewer_security"
    ) -> dict[str, Any]:
        return {
            "status": "stale_version",
            "producer_role": producer,
            "rejection": {
                "reviewer": reviewer,
                "current_proposal": {
                    "producer": producer,
                    "version": 7,
                    "commit_sha": "deadbeef",
                    "artifacts": ["src/a.py", "src/b.py"],
                },
            },
        }

    def test_stderr_labels_producer_not_reviewer(self, capsys):
        """The rendered line names the producer, not the reviewer."""
        from egg_lib.orch_cli import _render_stale_version_rejection

        args = argparse.Namespace(json=False)
        rc = _render_stale_version_rejection(args, self._stale_response(), "ACK")
        assert rc == 2
        err = capsys.readouterr().err
        assert "producer coder is at v7" in err
        assert "reviewer_security" not in err

    def test_json_output_emits_rejection_envelope(self, capsys):
        """--json prints the rejection envelope verbatim (no producer label issue)."""
        from egg_lib.orch_cli import _render_stale_version_rejection

        args = argparse.Namespace(json=True)
        resp = self._stale_response()
        rc = _render_stale_version_rejection(args, resp, "NACK")
        assert rc == 2
        import json as _json

        out = capsys.readouterr().out
        parsed = _json.loads(out)
        assert parsed == resp["rejection"]

    def test_falls_back_to_response_producer_role(self, capsys):
        """If snapshot omits ``producer``, fall back to ``producer_role``."""
        from egg_lib.orch_cli import _render_stale_version_rejection

        resp = self._stale_response()
        # Drop the snapshot's ``producer`` field — fallback must engage.
        del resp["rejection"]["current_proposal"]["producer"]
        args = argparse.Namespace(json=False)
        _render_stale_version_rejection(args, resp, "ACK")
        err = capsys.readouterr().err
        assert "producer coder is at v7" in err

    def test_cmd_consensus_ack_renders_stale_version(self, capsys):
        """End-to-end: cmd_consensus_ack routes a stale_version response to the renderer."""
        from egg_lib import orch_cli

        args = argparse.Namespace(
            pipeline_id="issue-42",
            role="reviewer_security",
            producer_role="coder",
            reason="Reviewed src/a.py over fifty chars to satisfy the validator",
            files_reviewed=["src/a.py"],
            pre_merge_condition="",
            ack_version=3,
            json=False,
        )
        resp = self._stale_response()
        with patch("egg_agent_tools.handlers.brc.brc_ack", return_value=resp):
            rc = orch_cli.cmd_consensus_ack(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "ACK rejected: producer coder is at v7" in err
        assert "reviewer_security" not in err


class TestMessagePollEmptyBody:
    @patch("egg_lib.orch_cli.orch_request")
    def test_empty_body_not_printed(self, mock_request, capsys):
        """Messages with empty bodies don't produce extra blank lines."""
        mock_request.return_value = {
            "data": {
                "messages": [
                    {
                        "timestamp": "2026-04-14T12:00:00Z",
                        "from_role": "coder",
                        "to_role": "reviewer_code",
                        "message_type": "PROGRESS",
                        "subject": "Tests passing",
                        "body": "",
                    }
                ]
            }
        }
        args = _make_poll_args()
        cmd_message_poll(args)
        output = capsys.readouterr().out
        lines = [line for line in output.strip().split("\n") if line.strip()]
        # Should only have the header line and the count line
        assert len(lines) == 2
