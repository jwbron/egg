"""Tests for BRC consensus CLI commands in egg-orch.

Covers:
- ``egg-orch consensus ack`` requires ``--reason`` (issue #1716)
- ``egg-orch consensus ack`` threads reason into payload
- ``egg-orch consensus propose`` accepts new structured args
  (``--commit``, ``--files-changed``, ``--tests-run``, ``--tasks``)
  and includes them in the payload
- Backward compatibility: existing args still work as expected
"""

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add sandbox/ to sys.path so egg-orch module is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

# egg-orch is a script without .py extension; import it via importlib
_egg_orch_path = Path(__file__).parent.parent / "bin" / "egg-orch"


def _import_egg_orch():
    """Import the egg-orch CLI script as a module."""
    spec = importlib.util.spec_from_file_location("egg_orch", _egg_orch_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def egg_orch():
    """Import egg-orch once per module."""
    return _import_egg_orch()


@pytest.fixture
def parser(egg_orch):
    """Get a fresh parser."""
    return egg_orch.create_parser()


# ---------------------------------------------------------------------------
# ACK: --reason required
# ---------------------------------------------------------------------------


class TestConsensusAckReasonRequired:
    """egg-orch consensus ack must require --reason (issue #1716)."""

    def test_ack_without_reason_exits_nonzero(self, parser):
        """ack without --reason should fail at argparse."""
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "consensus",
                    "ack",
                    "coder",
                    "--files-reviewed",
                    "src/auth.py",
                ]
            )
        assert exc_info.value.code != 0

    def test_ack_with_reason_parses(self, parser):
        """ack with --reason should parse successfully."""
        args = parser.parse_args(
            [
                "consensus",
                "ack",
                "coder",
                "--files-reviewed",
                "src/auth.py",
                "--reason",
                "Reviewed auth module: input validation covers edge cases, "
                "error handling is robust, tests are comprehensive",
            ]
        )
        assert args.producer_role == "coder"
        assert args.files_reviewed == ["src/auth.py"]
        assert "Reviewed auth module" in args.reason


class TestConsensusAckPayload:
    """Verify cmd_consensus_ack threads reason into the signal payload."""

    @patch.dict(os.environ, {"EGG_PIPELINE_ID": "issue-42", "EGG_AGENT_ROLE": "reviewer_code"})
    def test_reason_appears_in_payload(self, egg_orch):
        """The reason argument must appear in the payload sent to the orchestrator."""
        captured_data = {}

        def mock_orch_request(url, method="GET", data=None):
            captured_data.update(data or {})
            return {"success": True}

        with patch.object(egg_orch, "orch_request", side_effect=mock_orch_request):
            args = egg_orch.create_parser().parse_args(
                [
                    "consensus",
                    "ack",
                    "coder",
                    "--files-reviewed",
                    "src/auth.py",
                    "tests/test_auth.py",
                    "--reason",
                    "Thoroughly reviewed auth module: validation logic is correct, "
                    "edge cases handled, error messages are actionable",
                ]
            )
            result = egg_orch.cmd_consensus_ack(args)

        assert result == 0
        assert captured_data["signal_type"] == "consensus_ack"
        assert captured_data["agent_role"] == "reviewer_code"
        assert captured_data["producer_role"] == "coder"
        assert "reason" in captured_data["payload"]
        assert "Thoroughly reviewed auth module" in captured_data["payload"]["reason"]
        assert captured_data["payload"]["artifact_references"] == [
            "src/auth.py",
            "tests/test_auth.py",
        ]

    @patch.dict(os.environ, {"EGG_PIPELINE_ID": "issue-42", "EGG_AGENT_ROLE": "reviewer_code"})
    def test_ack_still_sends_artifact_references(self, egg_orch):
        """Existing --files-reviewed arg must still be sent alongside --reason."""
        captured_data = {}

        def mock_orch_request(url, method="GET", data=None):
            captured_data.update(data or {})
            return {"success": True}

        with patch.object(egg_orch, "orch_request", side_effect=mock_orch_request):
            args = egg_orch.create_parser().parse_args(
                [
                    "consensus",
                    "ack",
                    "coder",
                    "--files-reviewed",
                    "f1.py",
                    "f2.py",
                    "f3.py",
                    "--reason",
                    "All three files reviewed: f1 handles auth, f2 handles "
                    "validation, f3 handles serialization. No issues found.",
                ]
            )
            egg_orch.cmd_consensus_ack(args)

        assert captured_data["payload"]["artifact_references"] == [
            "f1.py",
            "f2.py",
            "f3.py",
        ]


# ---------------------------------------------------------------------------
# PROPOSE: new structured args
# ---------------------------------------------------------------------------


class TestConsensusProposeStructuredArgs:
    """egg-orch consensus propose accepts --commit, --files-changed, --tests-run, --tasks."""

    def test_propose_accepts_commit_arg(self, parser):
        """--commit should be accepted by the propose parser."""
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "--summary",
                "Implemented authentication module with full test coverage "
                "and comprehensive error handling for all edge cases",
                "--commit",
                "abc123def456",
            ]
        )
        assert args.commit == "abc123def456"

    def test_propose_accepts_files_changed(self, parser):
        """--files-changed should accept a list of files."""
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "--summary",
                "Implemented authentication module with full test coverage "
                "and comprehensive error handling for all edge cases",
                "--files-changed",
                "src/auth.py",
                "src/models.py",
            ]
        )
        assert args.files_changed == ["src/auth.py", "src/models.py"]

    def test_propose_accepts_tests_run(self, parser):
        """--tests-run should accept a list of test identifiers."""
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "--summary",
                "Implemented authentication module with full test coverage "
                "and comprehensive error handling for all edge cases",
                "--tests-run",
                "test_auth.py",
                "test_models.py",
            ]
        )
        assert args.tests_run == ["test_auth.py", "test_models.py"]

    def test_propose_accepts_tasks(self, parser):
        """--tasks should accept a list of task identifiers."""
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "--summary",
                "Implemented authentication module with full test coverage "
                "and comprehensive error handling for all edge cases",
                "--tasks",
                "task-1-1",
                "task-1-2",
            ]
        )
        assert args.tasks == ["task-1-1", "task-1-2"]

    def test_propose_all_new_args_together(self, parser):
        """All new structured args can be used together."""
        args = parser.parse_args(
            [
                "consensus",
                "propose",
                "--summary",
                "Full implementation of auth module with tests and docs "
                "covering all acceptance criteria from the contract",
                "--commit",
                "abc123",
                "--files-changed",
                "src/auth.py",
                "--tests-run",
                "test_auth.py",
                "--tasks",
                "task-1-1",
                "--artifacts",
                "src/auth.py",
            ]
        )
        assert args.commit == "abc123"
        assert args.files_changed == ["src/auth.py"]
        assert args.tests_run == ["test_auth.py"]
        assert args.tasks == ["task-1-1"]
        assert args.artifacts == ["src/auth.py"]


class TestConsensusProposePayload:
    """Verify cmd_consensus_propose includes new structured args in the payload."""

    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "issue-42",
            "EGG_AGENT_ROLE": "coder",
            "EGG_REPO_PATH": "/tmp/test-repo",
        },
    )
    def test_structured_args_in_payload(self, egg_orch):
        """New structured args must appear in the signal payload."""
        captured_data = {}

        def mock_orch_request(url, method="GET", data=None):
            captured_data.update(data or {})
            return {"success": True, "data": {"consensus": {"agents": {}}}}

        with patch.object(egg_orch, "orch_request", side_effect=mock_orch_request):
            args = egg_orch.create_parser().parse_args(
                [
                    "consensus",
                    "propose",
                    "--summary",
                    "Implemented auth module covering login, logout, and "
                    "session management with full test coverage",
                    "--commit",
                    "abc123def456",
                    "--files-changed",
                    "src/auth.py",
                    "src/session.py",
                    "--tests-run",
                    "test_auth.py",
                    "--tasks",
                    "task-1-1",
                    "task-1-2",
                    "--commit-sha",
                    "abc123def456",
                ]
            )
            result = egg_orch.cmd_consensus_propose(args)

        assert result == 0
        payload = captured_data["payload"]
        assert payload["commit"] == "abc123def456"
        assert payload["files_changed"] == ["src/auth.py", "src/session.py"]
        assert payload["tests_run"] == ["test_auth.py"]
        assert payload["tasks_satisfied"] == ["task-1-1", "task-1-2"]

    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "issue-42",
            "EGG_AGENT_ROLE": "coder",
            "EGG_REPO_PATH": "/tmp/test-repo",
        },
    )
    def test_propose_without_new_args_still_works(self, egg_orch):
        """Proposal without new structured args should still work (backward compat)."""
        captured_data = {}

        def mock_orch_request(url, method="GET", data=None):
            captured_data.update(data or {})
            return {"success": True, "data": {"consensus": {"agents": {}}}}

        with patch.object(egg_orch, "orch_request", side_effect=mock_orch_request):
            args = egg_orch.create_parser().parse_args(
                [
                    "consensus",
                    "propose",
                    "--summary",
                    "Implemented auth module covering login, logout, and "
                    "session management with full test coverage for the team",
                    "--commit-sha",
                    "abc123def456",
                ]
            )
            result = egg_orch.cmd_consensus_propose(args)

        assert result == 0
        payload = captured_data["payload"]
        assert "summary" in payload
        assert payload["commit_sha"] == "abc123def456"

    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "issue-42",
            "EGG_AGENT_ROLE": "coder",
            "EGG_REPO_PATH": "/tmp/test-repo",
        },
    )
    def test_propose_file_payload_bypasses_structured_args(self, egg_orch, tmp_path):
        """When --file is used, structured args should be ignored (file takes precedence)."""
        payload_file = tmp_path / "proposal.json"
        payload_file.write_text(
            json.dumps(
                {
                    "summary": "Auth module from JSON file with all the details "
                    "about what was implemented and why it matters",
                    "attestation": {},
                    "artifacts": [],
                    "commit_sha": "fromfile",
                }
            )
        )
        captured_data = {}

        def mock_orch_request(url, method="GET", data=None):
            captured_data.update(data or {})
            return {"success": True, "data": {"consensus": {"agents": {}}}}

        with patch.object(egg_orch, "orch_request", side_effect=mock_orch_request):
            args = egg_orch.create_parser().parse_args(
                [
                    "consensus",
                    "propose",
                    "--file",
                    str(payload_file),
                ]
            )
            result = egg_orch.cmd_consensus_propose(args)

        assert result == 0
        # File payload should be used as-is
        assert captured_data["payload"]["commit_sha"] == "fromfile"


# ---------------------------------------------------------------------------
# NACK: --reason still required (regression guard)
# ---------------------------------------------------------------------------


class TestConsensusNackReasonRequired:
    """Regression: nack --reason must remain required."""

    def test_nack_without_reason_exits_nonzero(self, parser):
        """nack without --reason should fail at argparse."""
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                [
                    "consensus",
                    "nack",
                    "coder",
                    "--files-reviewed",
                    "src/auth.py",
                ]
            )
        assert exc_info.value.code != 0

    def test_nack_with_reason_parses(self, parser):
        """nack with --reason should parse successfully."""
        args = parser.parse_args(
            [
                "consensus",
                "nack",
                "coder",
                "--files-reviewed",
                "src/auth.py",
                "--reason",
                "Auth module missing input validation: the login function "
                "does not sanitize user input before database query",
            ]
        )
        assert args.producer_role == "coder"
        assert "Auth module missing" in args.reason


# ---------------------------------------------------------------------------
# WITHDRAW: --reason still required (regression guard)
# ---------------------------------------------------------------------------


class TestConsensusWithdrawReasonRequired:
    """Regression: withdraw --reason must remain required."""

    def test_withdraw_without_reason_exits_nonzero(self, parser):
        """withdraw without --reason should fail at argparse."""
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["consensus", "withdraw"])
        assert exc_info.value.code != 0

    def test_withdraw_with_reason_parses(self, parser):
        """withdraw with --reason should parse successfully."""
        args = parser.parse_args(
            [
                "consensus",
                "withdraw",
                "--reason",
                "Withdrawing proposal due to fundamental design issue found "
                "during review: needs architectural redesign of the auth flow",
            ]
        )
        assert "Withdrawing proposal" in args.reason
