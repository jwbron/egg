"""Tests for ``PipelineToolHandler._handle_run_agent_task`` (#1762).

The handler validates the caller's arguments locally before forwarding
to ``POST /api/v1/pipelines`` with ``mode="custom"``. Local validation
gives the user a fast, clear error before any network round-trip.

This module covers the client-side input checks, the pipeline-ID
derivation rules, and the request-body construction. The server-side
400 responses (route-level validation) are covered by
``test_pipelines_routes_custom_mode.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from mcp_tools import PipelineToolHandler


@pytest.fixture
def handler():
    return PipelineToolHandler(
        orchestrator_url="http://localhost:9849",
        gateway_url=f"http://test-gateway:{TEST_GATEWAY_PORT}",
    )


def _mock_success_response(pipeline_id: str):
    """Return a helper that mocks ``_make_request`` so the handler
    thinks the orchestrator accepted the pipeline. Two calls are
    expected: one for create, one for start."""
    side_effects = [
        {"data": {"pipeline": {"id": pipeline_id}}},
        {"data": {"started": True}},
    ]
    return side_effects


# ---------------------------------------------------------------------------
# Required-field validation (client-side)
# ---------------------------------------------------------------------------


class TestRequiredFieldValidation:
    def test_missing_phase(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {"repo": "owner/repo", "description": "do something"},
        )
        assert "error" in result
        assert "phase" in result["error"].lower()

    def test_invalid_phase(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {"phase": "pr", "repo": "owner/repo", "description": "do something"},
        )
        assert "error" in result
        assert "refine" in result["error"] and "implement" in result["error"]

    def test_missing_repo(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {"phase": "implement", "description": "do something"},
        )
        assert "error" in result
        assert "repo" in result["error"].lower()

    def test_invalid_repo_format(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "no-slash",
                "description": "do something",
            },
        )
        assert "error" in result
        assert "owner/name" in result["error"]

    def test_repo_with_shell_metacharacter_rejected(self, handler):
        """risk_analyst R9 — the owner/name regex must reject values
        that could be re-interpreted as git flags or shell args."""
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo;rm",
                "description": "do something",
            },
        )
        assert "error" in result
        assert "owner/name" in result["error"]

    def test_missing_description(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {"phase": "implement", "repo": "owner/repo"},
        )
        assert "error" in result
        assert "description" in result["error"].lower()

    def test_empty_description(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {"phase": "implement", "repo": "owner/repo", "description": ""},
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Optional-field type validation
# ---------------------------------------------------------------------------


class TestOptionalFieldValidation:
    def test_roles_non_list_rejected(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "roles": "coder",
            },
        )
        assert "error" in result
        assert "roles" in result["error"].lower()

    def test_qualifier_invalid_chars_rejected(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "qualifier": "BadUpper",
            },
        )
        assert "error" in result
        assert "qualifier" in result["error"].lower()

    def test_qualifier_trailing_hyphen_rejected(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "qualifier": "bad-",
            },
        )
        assert "error" in result

    def test_qualifier_valid_segments_accepted(self, handler):
        side_effects = _mock_success_response("issue-42-v2-hotfix")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            result = handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "issue_number": 42,
                    "qualifier": "v2-hotfix",
                },
            )
        assert "error" not in result
        create_call = mock_req.call_args_list[0]
        body = create_call.kwargs.get("data") or create_call.args[2]
        assert body["pipeline_id"] == "issue-42-v2-hotfix"

    def test_issue_number_zero_rejected(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "issue_number": 0,
            },
        )
        assert "error" in result
        assert "issue_number" in result["error"].lower()

    def test_issue_number_negative_rejected(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "issue_number": -5,
            },
        )
        assert "error" in result

    def test_pr_number_zero_rejected(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "pr_number": 0,
            },
        )
        assert "error" in result
        assert "pr_number" in result["error"].lower()


# ---------------------------------------------------------------------------
# Pipeline-ID derivation rules
# ---------------------------------------------------------------------------


class TestPipelineIdDerivation:
    def test_issue_and_qualifier(self, handler):
        side_effects = _mock_success_response("issue-100-backend")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "issue_number": 100,
                    "qualifier": "backend",
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["pipeline_id"] == "issue-100-backend"

    def test_issue_only(self, handler):
        side_effects = _mock_success_response("issue-100-custom")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "issue_number": 100,
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["pipeline_id"] == "issue-100-custom"

    def test_pr_only_is_babysit_compatible(self, handler):
        """The plan says pr-only pipelines must use the BABYSIT ID
        (``pr-<N>``) to subsume BABYSIT internally (decision-2)."""
        side_effects = _mock_success_response("pr-42")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "pr_number": 42,
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["pipeline_id"] == "pr-42"

    def test_pr_plus_qualifier(self, handler):
        side_effects = _mock_success_response("pr-42-followup")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "pr_number": 42,
                    "qualifier": "followup",
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["pipeline_id"] == "pr-42-followup"

    def test_synthetic_when_no_identifier(self, handler):
        side_effects = _mock_success_response("custom-12345678")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["pipeline_id"].startswith("custom-")
        # UUID4 hex [:8] = 8-char hex suffix.
        suffix = body["pipeline_id"].split("-", 1)[1]
        assert len(suffix) == 8
        int(suffix, 16)  # raises if non-hex


# ---------------------------------------------------------------------------
# Request body construction (the bits forwarded to the route)
# ---------------------------------------------------------------------------


class TestRequestBodyConstruction:
    def test_basic_fields_forwarded(self, handler):
        side_effects = _mock_success_response("custom-aabbccdd")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "refine",
                    "repo": "owner/repo",
                    "description": "research the feature",
                    "roles": ["refiner"],
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["mode"] == "custom"
        assert body["phase"] == "refine"
        assert body["repo"] == "owner/repo"
        assert body["prompt"] == "research the feature"
        assert body["roles"] == ["refiner"]

    def test_roles_omitted_when_null(self, handler):
        """``None`` roles means "use the default roster for the phase".
        The forwarding body should NOT include the key so the route
        sees the default path."""
        side_effects = _mock_success_response("custom-aabbccdd")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "roles": None,
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert "roles" not in body

    def test_analysis_and_plan_forwarded(self, handler):
        side_effects = _mock_success_response("custom-aabbccdd")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "plan",
                    "repo": "owner/repo",
                    "description": "x",
                    "analysis": "## Analysis\n\nfoo",
                    "plan": "## Plan\n\nbar",
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["analysis"] == "## Analysis\n\nfoo"
        assert body["plan"] == "## Plan\n\nbar"

    def test_pr_number_forwarded(self, handler):
        side_effects = _mock_success_response("pr-42")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "pr_number": 42,
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["pr_number"] == 42

    def test_config_string_parsed_as_json(self, handler):
        side_effects = _mock_success_response("custom-aabbccdd")
        with patch.object(handler, "_make_request", side_effect=side_effects) as mock_req:
            handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "config": '{"hitl_gates": false}',
                },
            )
        body = mock_req.call_args_list[0].kwargs.get("data") or mock_req.call_args_list[0].args[2]
        assert body["config"] == {"hitl_gates": False}

    def test_config_invalid_json_returns_error(self, handler):
        result = handler.handle_tool_call(
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "x",
                "config": "{not json",
            },
        )
        assert "error" in result
        assert "config" in result["error"].lower()


# ---------------------------------------------------------------------------
# Error response handling (server-side 400s)
# ---------------------------------------------------------------------------


class TestServerErrorHandling:
    def test_400_reviewer_only_roster_surfaces_reason(self, handler):
        from urllib.error import HTTPError

        http_err = HTTPError(
            "http://orchestrator/api/v1/pipelines",
            400,
            "Bad Request",
            {},
            MagicMock(),
        )

        def _raise(*a, **kw):
            http_err.read = MagicMock(
                return_value=(
                    b'{"message": "Invalid roles", "details": {"reason": "reviewer_only_roster"}}'
                )
            )
            raise http_err

        with patch.object(handler, "_make_request", side_effect=_raise):
            result = handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "roles": ["reviewer_code"],
                },
            )
        assert "error" in result
        assert result.get("reason") == "reviewer_only_roster"

    def test_409_conflict_surfaces_reason(self, handler):
        from urllib.error import HTTPError

        http_err = HTTPError(
            "http://orchestrator/api/v1/pipelines",
            409,
            "Conflict",
            {},
            MagicMock(),
        )

        def _raise(*a, **kw):
            http_err.read = MagicMock(
                return_value=(
                    b'{"message": "Pipeline exists", '
                    b'"details": {"reason": "duplicate_pipeline", '
                    b'"existing_pipeline_id": "pr-42", '
                    b'"existing_status": "RUNNING", '
                    b'"existing_phase": "implement"}}'
                )
            )
            raise http_err

        with patch.object(handler, "_make_request", side_effect=_raise):
            result = handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                    "pr_number": 42,
                },
            )
        assert result.get("reason") == "duplicate_pipeline"
        assert result.get("existing_pipeline_id") == "pr-42"


# ---------------------------------------------------------------------------
# Success path: task_id + status
# ---------------------------------------------------------------------------


class TestSuccessShape:
    def test_started_status_on_happy_path(self, handler):
        side_effects = _mock_success_response("custom-11223344")
        with patch.object(handler, "_make_request", side_effect=side_effects):
            result = handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                },
            )
        assert result["task_id"] == "custom-11223344"
        assert result["status"] == "started"

    def test_created_not_started_when_start_fails(self, handler):
        """If POST /start raises after the pipeline was created, the
        handler must still return a task_id so the caller can retry."""
        from urllib.error import HTTPError

        def _side(path, method=None, data=None, timeout=None):
            if "/start" in path:
                raise HTTPError(path, 500, "Internal Error", {}, MagicMock())
            return {"data": {"pipeline": {"id": "custom-11223344"}}}

        with patch.object(handler, "_make_request", side_effect=_side):
            result = handler.handle_tool_call(
                "run_agent_task",
                {
                    "phase": "implement",
                    "repo": "owner/repo",
                    "description": "x",
                },
            )
        assert result["task_id"] == "custom-11223344"
        assert result["status"] == "created_not_started"
