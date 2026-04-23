"""Unit tests for egg_agent_tools.handlers.task.

Covers task_complete (with and without commit SHA).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import task  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


class TestTaskComplete:
    def test_happy_path_without_commit(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1765),
        ):
            resp = task.task_complete({"task": "task-1-1"})
        assert resp["ok"] is True
        assert resp["task"] == "task-1-1"
        assert resp["commit"] is None
        assert req.call_count == 1
        data = req.call_args.kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.0.status"
        assert data["new_value"] == "complete"

    def test_happy_path_with_commit(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1765),
        ):
            resp = task.task_complete({"task": "task-2-3", "commit": "abcdef1234"})
        assert resp["ok"] is True
        assert resp["commit"] == "abcdef1234"
        # Two calls: status + commit link.
        assert req.call_count == 2
        commit_call = req.call_args_list[1].kwargs["data"]
        assert commit_call["field_path"] == "phases.1.tasks.2.commit"
        assert commit_call["new_value"] == "abcdef1234"

    def test_parses_single_segment_task_id(self):
        """'task-5' interpreted as phases.0.tasks.4 per parity with CLI."""
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": True, "data": {}},
            ) as req,
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            task.task_complete({"task": "task-5"})
        data = req.call_args.kwargs["data"]
        assert data["field_path"] == "phases.0.tasks.4.status"

    def test_missing_task_id(self):
        with pytest.raises(HandlerError):
            task.task_complete({})

    @pytest.mark.parametrize(
        "bad", ["", "x1", "task-", "task-0", "task-1-a", "task-a-b", "task-1-2-3"]
    )
    def test_invalid_task_id(self, bad):
        with pytest.raises(HandlerError):
            task.task_complete({"task": bad})

    def test_invalid_commit_sha(self):
        with pytest.raises(HandlerError):
            task.task_complete({"task": "task-1-1", "commit": "nothex!!"})

    def test_missing_identifier(self):
        with patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=None):
            with pytest.raises(HandlerError):
                task.task_complete({"task": "task-1-1"})

    def test_gateway_500_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=GatewayError("fail", status_code=500),
            ),
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                task.task_complete({"task": "task-1-1"})

    def test_commit_link_failure_raises_gateway_error(self):
        """First call succeeds, second (commit-link) returns failure."""
        responses = [
            {"success": True, "data": {}},
            {"success": False, "message": "oops"},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError) as exc:
                task.task_complete({"task": "task-1-1", "commit": "a" * 40})
        assert "failed to link commit" in str(exc.value).lower()

    def test_unsuccessful_status_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.task.gateway_request",
                return_value={"success": False, "message": "denied"},
            ),
            patch("egg_agent_tools.handlers.task.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                task.task_complete({"task": "task-1-1"})
