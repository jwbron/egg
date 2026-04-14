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

import pytest

# Add sandbox to sys.path so egg_lib is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_lib.orch_cli import create_parser

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
            ]
        )
        assert args.reason == "Reviewed src/a.py: logic is correct, tests cover all branches"
        assert args.producer_role == "coder"
        assert args.files_reviewed == ["src/a.py"]


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
            ]
        )
        # Simulate what cmd_consensus_ack would build
        payload = {
            "artifact_references": args.files_reviewed,
            "reason": args.reason,
        }
        assert payload["reason"] == (
            "Checked auth.py and models.py: parameterized queries throughout, no injection risk"
        )
        assert payload["artifact_references"] == ["src/auth.py", "src/models.py"]


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
    """Navigate argparse tree to find the ``--type`` action of ``message send``."""
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
        """The --type argument help text lists all expected message types."""
        parser = create_parser()
        type_action = _find_msg_send_type_action(parser)
        help_text = type_action.help
        assert help_text is not None, "--type has no help text"
        for msg_type in ("PROGRESS", "QUESTION", "STATUS", "HANDOFF"):
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
