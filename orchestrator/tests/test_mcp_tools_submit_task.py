"""Tests for the ``submit_task`` MCP tool's ``mode`` parameter (#1557 TASK-1-1).

The ``mode`` argument is the epic-detection knob added in #1557:

  * ``auto`` (default) — let the orchestrator auto-detect ``reassess`` vs
    ``fresh`` based on the epic's current children.
  * ``reassess`` — force the reassess flow (degrades to ``fresh`` when the
    epic has no children).
  * ``fresh`` — force the fresh flow even when children exist.

These tests pin the schema entry and the validation parity with the
existing ``qualifier`` rejection at ``mcp_tools.py:1281`` (rejected values
return ``{"error": ...}`` so the MCP framework surfaces them to the caller
as an HTTP-400-equivalent error).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from mcp_tools import PIPELINE_TOOLS, PipelineToolHandler


@pytest.fixture
def handler() -> PipelineToolHandler:
    return PipelineToolHandler(
        orchestrator_url="http://localhost:9849",
        gateway_url=f"http://test-gateway:{TEST_GATEWAY_PORT}",
    )


def _mock_pipeline_create_response() -> MagicMock:
    """Return a urllib-style context-managed mock that matches the success
    payload returned by ``POST /api/v1/pipelines``."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(
        {"data": {"pipeline": {"id": "KORE-1234"}}}
    ).encode()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


# -----------------------------------------------------------------------------
# Schema entry — ``mode`` documented as enum on the JSON schema
# -----------------------------------------------------------------------------


class TestSubmitTaskModeSchema:
    """Pin the JSON-schema shape of the ``mode`` property."""

    def test_submit_task_present(self) -> None:
        tools_by_name = {t["name"]: t for t in PIPELINE_TOOLS}
        assert "submit_task" in tools_by_name

    def test_mode_property_exists(self) -> None:
        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "submit_task")
        props = tool["inputSchema"]["properties"]
        assert "mode" in props, "submit_task schema is missing 'mode'"

    def test_mode_is_string_enum(self) -> None:
        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "submit_task")
        mode = tool["inputSchema"]["properties"]["mode"]
        assert mode["type"] == "string"
        assert set(mode["enum"]) == {"auto", "reassess", "fresh"}

    def test_mode_default_is_auto(self) -> None:
        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "submit_task")
        mode = tool["inputSchema"]["properties"]["mode"]
        assert mode["default"] == "auto"

    def test_mode_is_not_required(self) -> None:
        """``mode`` defaults to ``auto`` — it must remain optional so today's
        single-ticket callers stay byte-identical."""
        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "submit_task")
        required = tool["inputSchema"].get("required", [])
        assert "mode" not in required

    def test_mode_description_mentions_epic(self) -> None:
        """Lightweight regression — ``mode`` is only meaningful for Jira
        epics; the description should call that out."""
        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "submit_task")
        mode = tool["inputSchema"]["properties"]["mode"]
        assert "epic" in mode["description"].lower()


# -----------------------------------------------------------------------------
# Handler validation — reject unknown modes; parity with qualifier rejection
# -----------------------------------------------------------------------------


class TestSubmitTaskModeValidation:
    """``_handle_submit_task`` validates ``mode`` before any HTTP traffic.

    The parity reference is ``qualifier`` rejection at ``mcp_tools.py:1281``
    — both return a ``{"error": ...}`` dict that the MCP layer surfaces as
    an HTTP-400 to the caller.
    """

    def test_unknown_mode_returns_error(self, handler: PipelineToolHandler) -> None:
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "jira_ticket": "KORE-1234",
                "mode": "bogus",
            },
        )
        assert "error" in result
        # Parity with qualifier rejection — message names the bad value and
        # lists the accepted enum members.
        assert "bogus" in result["error"]
        assert "auto" in result["error"]
        assert "reassess" in result["error"]
        assert "fresh" in result["error"]

    def test_non_string_mode_returns_error(self, handler: PipelineToolHandler) -> None:
        """Numeric / bool / list values are rejected before reaching the
        orchestrator (defence in depth — the JSON schema layer also rejects)."""
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "jira_ticket": "KORE-1234",
                "mode": 1,
            },
        )
        assert "error" in result

    def test_empty_string_mode_returns_error(self, handler: PipelineToolHandler) -> None:
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "jira_ticket": "KORE-1234",
                "mode": "",
            },
        )
        assert "error" in result

    @pytest.mark.parametrize("mode_value", ["AUTO", "Reassess", "FRESH"])
    def test_case_sensitive_rejection(self, handler: PipelineToolHandler, mode_value: str) -> None:
        """``mode`` is case-sensitive (mirrors qualifier strictness) — the
        enum lists lowercase values, so uppercased synonyms are rejected."""
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "jira_ticket": "KORE-1234",
                "mode": mode_value,
            },
        )
        assert "error" in result

    def test_validation_runs_before_http(self, handler: PipelineToolHandler) -> None:
        """An invalid ``mode`` must short-circuit BEFORE any HTTP traffic is
        attempted — otherwise a misconfigured caller would create a pipeline
        and only then discover the error."""
        with patch("urllib.request.build_opener") as mock_build_opener:
            result = handler.handle_tool_call(
                "submit_task",
                {
                    "description": "Test task",
                    "repo": "owner/repo",
                    "jira_ticket": "KORE-1234",
                    "mode": "invalid",
                },
            )
        assert "error" in result
        mock_build_opener.assert_not_called()


# -----------------------------------------------------------------------------
# Handler forwarding — valid modes reach the orchestrator
# -----------------------------------------------------------------------------


class TestSubmitTaskModeForwarding:
    """Valid ``mode`` values are forwarded on the POST body as
    ``jira_epic_mode``.  Omitted → defaults to ``auto``.  Forwarding only
    happens when ``jira_ticket`` is supplied (issue / GitHub-only pipelines
    don't carry the field).
    """

    @pytest.mark.parametrize("mode_value", ["auto", "reassess", "fresh"])
    def test_valid_modes_accepted(self, handler: PipelineToolHandler, mode_value: str) -> None:
        with patch("urllib.request.build_opener") as mock_build_opener:
            mock_opener = MagicMock()
            mock_opener.open.return_value = _mock_pipeline_create_response()
            mock_build_opener.return_value = mock_opener

            result = handler.handle_tool_call(
                "submit_task",
                {
                    "description": "Test task",
                    "repo": "owner/repo",
                    "jira_ticket": "KORE-1234",
                    "mode": mode_value,
                },
            )
        # No error key — the call succeeded.
        assert "error" not in result
        # The orchestrator receives the resolved mode under ``jira_epic_mode``.
        call_args = mock_opener.open.call_args_list[0]
        request_obj = call_args[0][0]
        body = json.loads(request_obj.data)
        assert body.get("jira_epic_mode") == mode_value

    def test_omitted_mode_defaults_to_auto(self, handler: PipelineToolHandler) -> None:
        """Omitting ``mode`` against a Jira-ticket call lands as
        ``jira_epic_mode: auto`` so existing single-ticket callers see the
        new field on the wire — but with a default that preserves today's
        behaviour."""
        with patch("urllib.request.build_opener") as mock_build_opener:
            mock_opener = MagicMock()
            mock_opener.open.return_value = _mock_pipeline_create_response()
            mock_build_opener.return_value = mock_opener

            handler.handle_tool_call(
                "submit_task",
                {
                    "description": "Test task",
                    "repo": "owner/repo",
                    "jira_ticket": "KORE-1234",
                },
            )
        call_args = mock_opener.open.call_args_list[0]
        body = json.loads(call_args[0][0].data)
        assert body.get("jira_epic_mode") == "auto"

    def test_explicit_auto_round_trips(self, handler: PipelineToolHandler) -> None:
        """Explicitly passing ``mode: auto`` is wire-identical to omitting
        it — both materialise as ``jira_epic_mode: auto``."""
        with patch("urllib.request.build_opener") as mock_build_opener:
            mock_opener = MagicMock()
            mock_opener.open.return_value = _mock_pipeline_create_response()
            mock_build_opener.return_value = mock_opener

            handler.handle_tool_call(
                "submit_task",
                {
                    "description": "Test task",
                    "repo": "owner/repo",
                    "jira_ticket": "KORE-1234",
                    "mode": "auto",
                },
            )
        body = json.loads(mock_opener.open.call_args_list[0][0][0].data)
        assert body["jira_epic_mode"] == "auto"

    def test_mode_omitted_when_no_jira_ticket(self, handler: PipelineToolHandler) -> None:
        """Calls without ``jira_ticket`` (GitHub-issue / freeform calls) do
        NOT include ``jira_epic_mode`` on the body — the field is meaningless
        outside the Jira flow."""
        with patch("urllib.request.build_opener") as mock_build_opener:
            mock_opener = MagicMock()
            mock_opener.open.return_value = _mock_pipeline_create_response()
            mock_build_opener.return_value = mock_opener

            handler.handle_tool_call(
                "submit_task",
                {
                    "description": "Test task",
                    "repo": "owner/repo",
                    "mode": "reassess",  # ignored — no jira_ticket
                },
            )
        body = json.loads(mock_opener.open.call_args_list[0][0][0].data)
        assert "jira_epic_mode" not in body

    def test_jira_ticket_forwarded_alongside_mode(self, handler: PipelineToolHandler) -> None:
        """``jira_ticket`` is forwarded as an upper-cased canonical key in
        the request body so the orchestrator can probe its issuetype.
        ``mode`` rides alongside it."""
        with patch("urllib.request.build_opener") as mock_build_opener:
            mock_opener = MagicMock()
            mock_opener.open.return_value = _mock_pipeline_create_response()
            mock_build_opener.return_value = mock_opener

            handler.handle_tool_call(
                "submit_task",
                {
                    "description": "Test task",
                    "repo": "owner/repo",
                    "jira_ticket": "kore-1234",
                    "mode": "reassess",
                },
            )
        body = json.loads(mock_opener.open.call_args_list[0][0][0].data)
        # Ticket key forwarded upper-cased.
        assert body.get("jira_ticket") == "KORE-1234"
        # Pipeline id mirrors the upper-cased ticket.
        assert body.get("pipeline_id") == "KORE-1234"
        # Mode comes along for the ride.
        assert body.get("jira_epic_mode") == "reassess"

    def test_invalid_jira_ticket_rejected_before_mode_check(
        self, handler: PipelineToolHandler
    ) -> None:
        """Invalid ticket shape is rejected first — but the mode validator
        still must not raise when reached.  Pin both orderings work."""
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Test task",
                "repo": "owner/repo",
                "jira_ticket": "not a ticket",
                "mode": "reassess",
            },
        )
        assert "error" in result
        assert "Invalid JIRA ticket format" in result["error"]
