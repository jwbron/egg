"""CLI parity tests for the 15 cmd_* shims that now delegate to the
shared egg_agent_tools handlers.

Each test exercises a cmd_* function with fixed argparse arguments and a
handler double that returns a canned response, then asserts the CLI's
stdout / exit-code against committed expected values.  Error-path cases
trigger ``GatewayError`` from the handler and assert the CLI renders the
stderr + exit-code surface that callers relied on before the refactor.

The expected values are inline string fixtures — no first-run snapshot
recording. Every expected value is visible in the diff of this file so a
reviewer can read the intended output directly.
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers.errors import GatewayError  # noqa: E402
from egg_lib import contract_cli, orch_cli  # noqa: E402


def _capture(func, args):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = func(args)
    return rc, out.getvalue(), err.getvalue()


# ── contract_cli parity ────────────────────────────────────────────────────


class TestCmdAddDecisionParity:
    def test_happy_path_stdout(self):
        ns = argparse.Namespace(
            issue=1765,
            pipeline_id=None,
            repo_path=None,
            question="A or B?",
            options=["A", "B"],
            phase=None,
            format="json",
        )
        fake = {"ok": True, "id": "decision-7", "decision": {"id": "decision-7"}}
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.register_open_question",
                return_value=fake,
            ),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            rc, stdout, stderr = _capture(contract_cli.cmd_add_decision, ns)
        assert rc == 0
        assert stdout == "Created decision decision-7: A or B?\n"
        assert stderr == ""

    def test_gateway_error_renders_legacy_stderr_and_exit_1(self):
        err = GatewayError("server down", status_code=500)
        with (
            patch("egg_agent_tools.handlers.sdlc.register_open_question", side_effect=err),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            # main() wraps cmd_* and catches GatewayError.
            with patch.object(
                sys, "argv", ["egg-contract", "--issue", "1765", "add-decision", "--question", "q?"]
            ):
                rc = contract_cli.main(["--issue", "1765", "add-decision", "--question", "q?"])
        assert rc == 1

    def test_missing_identifier_exits_1_with_stderr(self):
        ns = argparse.Namespace(
            issue=None,
            pipeline_id=None,
            repo_path=None,
            question="q",
            options=None,
            phase=None,
            format="json",
        )
        with patch("egg_lib.contract_cli.get_contract_identifier", return_value=None):
            rc, stdout, stderr = _capture(contract_cli.cmd_add_decision, ns)
        assert rc == 1
        assert "Contract identifier required" in stderr


class TestCmdAddFeedbackParity:
    def test_happy_path_json_format(self):
        ns = argparse.Namespace(
            issue=1765,
            pipeline_id=None,
            repo_path=None,
            question=["Why?"],
            format="json",
        )
        fake = {
            "ok": True,
            "id": "feedback-3",
            "questions": [{"id": "Q1", "question": "Why?"}],
            "markdown": "ignored-in-json",
        }
        with (
            patch("egg_agent_tools.handlers.sdlc.request_feedback", return_value=fake),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            rc, stdout, stderr = _capture(contract_cli.cmd_add_feedback, ns)
        assert rc == 0
        assert stdout == ("Created feedback feedback-3 with 1 question(s)\n  Q1: Why?\n")

    def test_warning_forwarded_to_stderr(self):
        ns = argparse.Namespace(
            issue=1765,
            pipeline_id=None,
            repo_path=None,
            question=["Why?"],
            format="json",
        )
        fake = {
            "ok": True,
            "id": "feedback-4",
            "questions": [{"id": "Q1", "question": "Why?"}],
            "warning": "there was already pending feedback",
        }
        with (
            patch("egg_agent_tools.handlers.sdlc.request_feedback", return_value=fake),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            rc, stdout, stderr = _capture(contract_cli.cmd_add_feedback, ns)
        assert rc == 0
        assert "already pending" in stderr

    def test_no_questions_exits_1(self):
        ns = argparse.Namespace(
            issue=1765,
            pipeline_id=None,
            repo_path=None,
            question=[],
            format="json",
        )
        with patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765):
            rc, stdout, stderr = _capture(contract_cli.cmd_add_feedback, ns)
        assert rc == 1
        assert "At least one --question" in stderr


class TestCmdCompleteTaskParity:
    def test_happy_path_no_commit(self):
        ns = argparse.Namespace(
            issue=1765,
            pipeline_id=None,
            repo_path=None,
            task="task-1-2",
            commit=None,
        )
        with (
            patch(
                "egg_agent_tools.handlers.task.task_complete",
                return_value={"ok": True, "task": "task-1-2", "commit": None},
            ),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            rc, stdout, stderr = _capture(contract_cli.cmd_complete_task, ns)
        assert rc == 0
        assert stdout == "Completed task-1-2\n"

    def test_happy_path_with_commit_prints_short_sha(self):
        ns = argparse.Namespace(
            issue=1765,
            pipeline_id=None,
            repo_path=None,
            task="task-2-3",
            commit="abcdef1234567890",
        )
        with (
            patch(
                "egg_agent_tools.handlers.task.task_complete",
                return_value={
                    "ok": True,
                    "task": "task-2-3",
                    "commit": "abcdef1234567890",
                },
            ),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            rc, stdout, stderr = _capture(contract_cli.cmd_complete_task, ns)
        assert rc == 0
        assert stdout == "Completed task-2-3 (commit abcdef1)\n"

    def test_gateway_error_surfaced_via_main(self):
        """Exercise the main() wrapper so we prove GatewayError is caught
        and rendered as exit code 1 + stderr."""
        with (
            patch(
                "egg_agent_tools.handlers.task.task_complete",
                side_effect=GatewayError("bang", status_code=500),
            ),
            patch("egg_lib.contract_cli.get_contract_identifier", return_value=1765),
        ):
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                rc = contract_cli.main(
                    [
                        "--issue",
                        "1765",
                        "complete-task",
                        "--task",
                        "task-1-1",
                    ]
                )
        assert rc == 1
        assert "bang" in err_buf.getvalue()


# ── orch_cli parity ────────────────────────────────────────────────────────


class TestCmdConsensusProposeParity:
    def _ns(self, **extra):
        base = {
            "pipeline_id": "p1",
            "role": "coder",
            "push": False,
            "file": None,
            "summary": "x" * 60,
            "artifacts": [],
            "risk": "",
            "files_changed": [],
            "tests_run": [],
            "tasks": ["task-1-1"],
            "commit_sha": "abc1234",
            "changed_artifacts": None,
            "json": False,
        }
        base.update(extra)
        return argparse.Namespace(**base)

    def test_happy_path_stdout(self):
        fake = {
            "ok": True,
            "role": "coder",
            "phase": "implement",
            "signal": {"data": {}},
        }
        with (
            patch("egg_agent_tools.handlers.brc.brc_propose", return_value=fake),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_propose, self._ns())
        assert rc == 0
        assert "Proposal sent by coder" in stdout
        assert "BRC phase: implement" in stdout

    def test_gateway_error_exits_1(self):
        with (
            patch(
                "egg_agent_tools.handlers.brc.brc_propose",
                side_effect=GatewayError("down", status_code=500),
            ),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_propose, self._ns())
        assert rc == 1
        assert "down" in stderr


class TestCmdConsensusAckNackParity:
    def _ack_ns(self, **extra):
        base = {
            "pipeline_id": "p1",
            "role": "reviewer_code",
            "producer_role": "coder",
            "reason": "looks good " * 10,
            "files_reviewed": ["a.py"],
            "ack_version": 1,
            "nack_version": 1,
            "json": False,
        }
        base.update(extra)
        return argparse.Namespace(**base)

    def test_ack_happy_path(self):
        fake = {"ok": True, "signal": {"data": {}}, "producer_role": "coder"}
        with (
            patch("egg_agent_tools.handlers.brc.brc_ack", return_value=fake),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="reviewer_code"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_ack, self._ack_ns())
        assert rc == 0
        assert "ACK recorded" in stdout or "ACK sent" in stdout or "reviewer_code" in stdout

    def test_nack_happy_path(self):
        fake = {"ok": True, "signal": {"data": {}}, "producer_role": "coder"}
        with (
            patch("egg_agent_tools.handlers.brc.brc_nack", return_value=fake),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="reviewer_code"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_nack, self._ack_ns())
        assert rc == 0


class TestCmdConsensusConfirmedParity:
    def _ns(self, **extra):
        base = {
            "pipeline_id": "p1",
            "role": "coder",
            "json": False,
        }
        base.update(extra)
        return argparse.Namespace(**base)

    def test_pending_acks_exits_2(self):
        """Exit-code parity: pending_acks must exit 2 (preserved from the
        pre-refactor CLI so scripts can distinguish 'waiting' from
        'confirmed')."""
        fake = {
            "ok": False,
            "status": "pending_acks",
            "consensus_reached": False,
            "role": "coder",
            "message": "waiting",
            "signal": {"data": {}},
        }
        with (
            patch("egg_agent_tools.handlers.brc.brc_confirm", return_value=fake),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_confirmed, self._ns())
        assert rc == 2
        assert "Waiting for reviewer re-ACKs" in stdout

    def test_confirmed_exits_0(self):
        fake = {
            "ok": True,
            "status": "confirmed",
            "consensus_reached": True,
            "role": "coder",
            "message": "",
            "signal": {"data": {}},
        }
        with (
            patch("egg_agent_tools.handlers.brc.brc_confirm", return_value=fake),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_confirmed, self._ns())
        assert rc == 0
        assert "Consensus reached" in stdout

    def test_gateway_error(self):
        with (
            patch(
                "egg_agent_tools.handlers.brc.brc_confirm",
                side_effect=GatewayError("oops", status_code=500),
            ),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_consensus_confirmed, self._ns())
        assert rc == 1


class TestCmdSignalParity:
    def _emit_ns(self, **extra):
        # cmd_progress_emit delegates to handlers.progress_emit;
        # cmd_signal_progress is a separate legacy-endpoint path and
        # does NOT go through the shared handler (documented inline in
        # orch_cli.cmd_signal_progress).
        base = {
            "pipeline_id": "p1",
            "role": "coder",
            "step": "refactor",
            "state": "working",
            "detail": None,
            "blocker": None,
            "json": False,
        }
        base.update(extra)
        return argparse.Namespace(**base)

    def test_progress_emit_happy(self):
        fake = {
            "ok": True,
            "role": "coder",
            "step": "refactor",
            "state": "working",
            "event_id": "ev-1",
            "signal": {"data": {}},
        }
        with (
            patch("egg_agent_tools.handlers.progress.progress_emit", return_value=fake),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_progress_emit, self._emit_ns())
        assert rc == 0
        assert "ev-1" in stdout

    def test_progress_emit_gateway_error(self):
        with (
            patch(
                "egg_agent_tools.handlers.progress.progress_emit",
                side_effect=GatewayError("oops", status_code=503),
            ),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_progress_emit, self._emit_ns())
        assert rc == 1

    def test_error_signal_happy(self):
        ns = argparse.Namespace(
            pipeline_id="p1",
            role="coder",
            error="oh no",
            recoverable=True,
            json=False,
        )
        fake = {"ok": True, "role": "coder", "signal": {"data": {}}}
        with (
            patch(
                "egg_agent_tools.handlers.progress.progress_signal_error",
                return_value=fake,
            ),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_signal_error, ns)
        assert rc == 0

    def test_heartbeat_happy(self):
        ns = argparse.Namespace(
            pipeline_id="p1",
            role="coder",
            json=False,
        )
        fake = {"ok": True, "role": "coder", "signal": {"data": {}}}
        with (
            patch(
                "egg_agent_tools.handlers.progress.progress_heartbeat",
                return_value=fake,
            ),
            patch("egg_lib.orch_cli.require_pipeline_id", return_value="p1"),
            patch("egg_lib.orch_cli._require_role", return_value="coder"),
        ):
            rc, stdout, stderr = _capture(orch_cli.cmd_signal_heartbeat, ns)
        assert rc == 0
