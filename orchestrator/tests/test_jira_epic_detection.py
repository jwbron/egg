"""Tests for ``orchestrator.jira_epic_detect`` (issue #1557, TASK-1-2).

Covers:

* :func:`_extract_issuetype_name` — gateway-envelope handling.
* :func:`_project_key_from_jira_key` — splitting ``<PROJECT>-<NUM>``.
* :func:`detect_jira_issuetype` — Epic vs non-Epic dispatch, case
  insensitivity, error wrapping for HTTP / network failures, and
  malformed response handling.

The module's public surface accepts a ``gateway_invoker`` callable
whose signature mirrors
``orchestrator.gateway_client.GatewayClient._make_request``. Every
test below uses a :class:`MagicMock` for that callable so the suite
never touches the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from jira_epic_detect import (
    IssuetypeProbeResult,
    JiraEpicDetectionError,
    _extract_issuetype_name,
    _project_key_from_jira_key,
    detect_jira_issuetype,
)

# ---------------------------------------------------------------------------
# _project_key_from_jira_key
# ---------------------------------------------------------------------------


class TestProjectKeyFromJiraKey:
    def test_basic_eng_key(self):
        assert _project_key_from_jira_key("ENG-123") == "ENG"

    def test_short_proj_key(self):
        assert _project_key_from_jira_key("PROJ-1") == "PROJ"

    def test_key_with_dash_in_numeric_suffix_rejected_by_jql_guard(self):
        # v5 added _validate_jira_key (JQL injection guard) — keys with extra
        # dashes are rejected, not split on the first dash.
        with pytest.raises(JiraEpicDetectionError) as exc:
            _project_key_from_jira_key("ABC-1-2")
        assert "ABC-1-2" in str(exc.value)

    def test_missing_dash_raises(self):
        with pytest.raises(JiraEpicDetectionError) as exc:
            _project_key_from_jira_key("NOTAKEY")
        assert "NOTAKEY" in str(exc.value)
        assert "PROJECT" in str(exc.value)

    def test_empty_string_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            _project_key_from_jira_key("")


# ---------------------------------------------------------------------------
# _extract_issuetype_name
# ---------------------------------------------------------------------------


class TestExtractIssuetypeName:
    def test_direct_fields_shape(self):
        body = {"fields": {"issuetype": {"name": "Epic"}}}
        assert _extract_issuetype_name(body) == "Epic"

    def test_envelope_data_fields_shape(self):
        body = {"data": {"fields": {"issuetype": {"name": "Task"}}}}
        assert _extract_issuetype_name(body) == "Task"

    def test_missing_fields_raises(self):
        with pytest.raises(JiraEpicDetectionError) as exc:
            _extract_issuetype_name({})
        assert "fields.issuetype.name" in str(exc.value)

    def test_missing_issuetype_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            _extract_issuetype_name({"fields": {}})

    def test_missing_name_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            _extract_issuetype_name({"fields": {"issuetype": {}}})

    def test_empty_name_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            _extract_issuetype_name({"fields": {"issuetype": {"name": ""}}})

    def test_non_string_name_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            _extract_issuetype_name({"fields": {"issuetype": {"name": 42}}})

    def test_envelope_with_empty_data_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            _extract_issuetype_name({"data": {}})


# ---------------------------------------------------------------------------
# detect_jira_issuetype — happy paths
# ---------------------------------------------------------------------------


class TestDetectJiraIssuetypeHappy:
    def _make_invoker(self, response):
        invoker = MagicMock()
        invoker.return_value = response
        return invoker

    def test_epic_returns_is_epic_true(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": "Epic"}}})
        result = detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert isinstance(result, IssuetypeProbeResult)
        assert result.is_epic is True
        assert result.issuetype == "Epic"
        assert result.project_key == "ENG"

    def test_task_returns_is_epic_false(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": "Task"}}})
        result = detect_jira_issuetype("PROJ-42", gateway_invoker=invoker)
        assert result.is_epic is False
        assert result.issuetype == "Task"
        assert result.project_key == "PROJ"

    def test_invoker_called_with_expected_endpoint_and_payload(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": "Bug"}}})
        detect_jira_issuetype("ABC-7", gateway_invoker=invoker)
        invoker.assert_called_once()
        call_args = invoker.call_args
        assert call_args.args[0] == "/api/v1/jira/ticket/get"
        assert call_args.kwargs["method"] == "POST"
        assert call_args.kwargs["data"] == {
            "ticket": "ABC-7",
            "fields": ["issuetype"],
        }

    def test_lowercase_epic_detected(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": "epic"}}})
        result = detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert result.is_epic is True
        # Original name is preserved (only the comparison is normalised).
        assert result.issuetype == "epic"

    def test_uppercase_epic_detected(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": "EPIC"}}})
        result = detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert result.is_epic is True
        assert result.issuetype == "EPIC"

    def test_whitespace_padded_epic_detected(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": " Epic "}}})
        result = detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert result.is_epic is True

    def test_envelope_response_unwrapped(self):
        invoker = self._make_invoker({"data": {"fields": {"issuetype": {"name": "Epic"}}}})
        result = detect_jira_issuetype("ENG-9", gateway_invoker=invoker)
        assert result.is_epic is True
        assert result.issuetype == "Epic"

    def test_story_not_epic(self):
        invoker = self._make_invoker({"fields": {"issuetype": {"name": "Story"}}})
        result = detect_jira_issuetype("PROJ-3", gateway_invoker=invoker)
        assert result.is_epic is False
        assert result.issuetype == "Story"


# ---------------------------------------------------------------------------
# detect_jira_issuetype — error paths
# ---------------------------------------------------------------------------


class TestDetectJiraIssuetypeErrors:
    def test_invalid_jira_key_raises_before_invocation(self):
        invoker = MagicMock()
        with pytest.raises(JiraEpicDetectionError):
            detect_jira_issuetype("NOTAKEY", gateway_invoker=invoker)
        # The project-key parse happens BEFORE the gateway call.
        invoker.assert_not_called()

    def test_missing_issuetype_in_response_raises(self):
        invoker = MagicMock()
        invoker.return_value = {"fields": {}}
        with pytest.raises(JiraEpicDetectionError) as exc:
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert "fields.issuetype.name" in str(exc.value)

    def test_connection_error_wrapped(self):
        invoker = MagicMock()
        invoker.side_effect = ConnectionError("connection refused")
        with pytest.raises(JiraEpicDetectionError) as exc:
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert "Network failure" in str(exc.value)
        assert "ENG-1" in str(exc.value)

    def test_timeout_error_wrapped(self):
        invoker = MagicMock()
        invoker.side_effect = TimeoutError("request timed out")
        with pytest.raises(JiraEpicDetectionError) as exc:
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert "Network failure" in str(exc.value)

    def test_oserror_wrapped(self):
        invoker = MagicMock()
        invoker.side_effect = OSError("dns failure")
        with pytest.raises(JiraEpicDetectionError):
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)

    def test_gateway_http_error_with_status_code_wrapped(self):
        class GatewayError(Exception):
            status_code = 403

        invoker = MagicMock()
        invoker.side_effect = GatewayError("forbidden")
        with pytest.raises(JiraEpicDetectionError) as exc:
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert "HTTP 403" in str(exc.value)

    def test_gateway_http_error_500_wrapped(self):
        class GatewayError(Exception):
            status_code = 500

        invoker = MagicMock()
        invoker.side_effect = GatewayError("internal server error")
        with pytest.raises(JiraEpicDetectionError) as exc:
            detect_jira_issuetype("PROJ-1", gateway_invoker=invoker)
        assert "HTTP 500" in str(exc.value)
        assert "PROJ-1" in str(exc.value)

    def test_unknown_exception_without_status_code_propagates(self):
        # Programming-error exceptions (no ``status_code`` attribute) are
        # allowed to propagate unchanged so test failures aren't disguised.
        invoker = MagicMock()
        invoker.side_effect = RuntimeError("unexpected")
        with pytest.raises(RuntimeError) as exc:
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
        assert "unexpected" in str(exc.value)

    def test_key_error_propagates(self):
        # KeyError lacks ``status_code`` so should propagate, not be wrapped.
        invoker = MagicMock()
        invoker.side_effect = KeyError("missing")
        with pytest.raises(KeyError):
            detect_jira_issuetype("ENG-1", gateway_invoker=invoker)
