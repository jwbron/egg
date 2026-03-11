"""Tests for inter-agent message capture in checkpoint_handler.

Covers _fetch_inter_agent_messages: concurrent mode gating, orchestrator API
interaction, error handling, and edge cases.
"""

import json
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from checkpoint_handler import _fetch_inter_agent_messages
from egg_contracts.checkpoints import InterAgentMessage


class TestFetchInterAgentMessagesGating:
    """Tests for early-return conditions in _fetch_inter_agent_messages."""

    def test_returns_empty_when_pipeline_id_is_none(self):
        """Should return empty list when pipeline_id is None."""
        result = _fetch_inter_agent_messages(None, "coder")
        assert result == []

    def test_returns_empty_when_agent_role_is_none(self):
        """Should return empty list when agent_role is None."""
        result = _fetch_inter_agent_messages("issue-999", None)

        assert result == []

    def test_returns_empty_when_both_none(self):
        """Should return empty list when both params are None."""
        result = _fetch_inter_agent_messages(None, None)
        assert result == []

    def test_returns_empty_when_pipeline_id_is_empty_string(self):
        """Should return empty list when pipeline_id is empty string."""
        result = _fetch_inter_agent_messages("", "coder")
        assert result == []

    def test_returns_empty_when_agent_role_is_empty_string(self):
        """Should return empty list when agent_role is empty string."""
        result = _fetch_inter_agent_messages("issue-999", "")
        assert result == []

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "false"}, clear=False)
    def test_returns_empty_when_concurrent_mode_false(self):
        """Should return empty list when EGG_CONCURRENT_MODE is false."""
        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(os.environ, {}, clear=False)
    def test_returns_empty_when_concurrent_mode_not_set(self):
        """Should return empty list when EGG_CONCURRENT_MODE is not set."""
        os.environ.pop("EGG_CONCURRENT_MODE", None)
        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "FALSE"}, clear=False)
    def test_returns_empty_when_concurrent_mode_uppercase_false(self):
        """Case-insensitive check: FALSE should be treated as false."""
        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []


class TestFetchInterAgentMessagesSuccess:
    """Tests for successful message fetching from orchestrator."""

    def _make_mock_response(self, messages: list[dict]) -> MagicMock:
        """Create a mock urllib response with the given messages."""
        body = json.dumps({"data": {"messages": messages}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_fetches_messages_and_classifies_direction(self, mock_urlopen):
        """Should fetch messages and classify sent vs received."""
        messages = [
            {
                "id": "msg-1",
                "from_role": "coder",
                "to_role": "all",
                "message_type": "PROGRESS",
                "subject": "API done",
                "body": "Finished endpoints",
                "timestamp": "2026-03-11T10:00:00+00:00",
            },
            {
                "id": "msg-2",
                "from_role": "tester",
                "to_role": "coder",
                "message_type": "QUESTION",
                "subject": "Expected status?",
                "body": "What HTTP code?",
                "timestamp": "2026-03-11T10:05:00+00:00",
            },
        ]
        mock_urlopen.return_value = self._make_mock_response(messages)

        result = _fetch_inter_agent_messages("issue-999", "coder")

        assert len(result) == 2
        # First message: sent by coder
        assert result[0].direction == "sent"
        assert result[0].from_role == "coder"
        assert result[0].message_type == "PROGRESS"
        # Second message: received by coder
        assert result[1].direction == "received"
        assert result[1].from_role == "tester"
        assert result[1].message_type == "QUESTION"

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_returns_inter_agent_message_instances(self, mock_urlopen):
        """Should return list of InterAgentMessage model instances."""
        messages = [
            {
                "id": "msg-1",
                "from_role": "coder",
                "to_role": "tester",
                "message_type": "RESPONSE",
                "subject": "Answer",
                "body": "Use 400",
                "timestamp": "2026-03-11T10:00:00+00:00",
            },
        ]
        mock_urlopen.return_value = self._make_mock_response(messages)

        result = _fetch_inter_agent_messages("issue-999", "coder")

        assert len(result) == 1
        assert isinstance(result[0], InterAgentMessage)
        assert result[0].pipeline_id == "issue-999"
        assert result[0].subject == "Answer"

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_empty_messages_returns_empty_list(self, mock_urlopen):
        """Should return empty list when orchestrator returns no messages."""
        mock_urlopen.return_value = self._make_mock_response([])

        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(
        os.environ,
        {
            "EGG_CONCURRENT_MODE": "true",
            "EGG_ORCHESTRATOR_URL": "http://custom-orch:1234",
        },
        clear=False,
    )
    @patch("urllib.request.urlopen")
    def test_uses_custom_orchestrator_url(self, mock_urlopen):
        """Should use EGG_ORCHESTRATOR_URL for the API call."""
        mock_urlopen.return_value = self._make_mock_response([])

        _fetch_inter_agent_messages("issue-999", "coder")

        # Verify the URL used in the request
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "custom-orch:1234" in request_obj.full_url

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_message_without_timestamp_uses_utc_now(self, mock_urlopen):
        """Should use datetime.now(UTC) when message has no timestamp."""
        messages = [
            {
                "id": "msg-1",
                "from_role": "coder",
                "to_role": "all",
                "message_type": "STATUS",
                "subject": "Working",
                # no timestamp field
            },
        ]
        mock_urlopen.return_value = self._make_mock_response(messages)

        before = datetime.now(UTC)
        result = _fetch_inter_agent_messages("issue-999", "coder")
        after = datetime.now(UTC)

        assert len(result) == 1
        assert before <= result[0].timestamp <= after

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_message_missing_optional_fields_uses_defaults(self, mock_urlopen):
        """Should handle missing optional fields with defaults."""
        messages = [
            {
                "id": "msg-1",
                "from_role": "coder",
                # no to_role, no message_type, no subject, no body
                "timestamp": "2026-03-11T10:00:00+00:00",
            },
        ]
        mock_urlopen.return_value = self._make_mock_response(messages)

        result = _fetch_inter_agent_messages("issue-999", "coder")

        assert len(result) == 1
        assert result[0].to_role == "all"
        assert result[0].message_type == ""
        assert result[0].subject == ""
        assert result[0].body == ""


class TestFetchInterAgentMessagesErrors:
    """Tests for error handling in _fetch_inter_agent_messages."""

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_connection_error_returns_empty_list(self, mock_urlopen):
        """Should return empty list on network error, not raise."""
        mock_urlopen.side_effect = ConnectionError("Connection refused")

        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_timeout_returns_empty_list(self, mock_urlopen):
        """Should return empty list on timeout, not raise."""
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("timeout")

        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_malformed_json_returns_empty_list(self, mock_urlopen):
        """Should return empty list on malformed JSON response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_missing_data_key_returns_empty_list(self, mock_urlopen):
        """Should return empty list when response lacks 'data' key."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"error": "not found"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _fetch_inter_agent_messages("issue-999", "coder")
        assert result == []

    @patch.dict(os.environ, {"EGG_CONCURRENT_MODE": "true"}, clear=False)
    @patch("urllib.request.urlopen")
    def test_invalid_timestamp_format_returns_empty_list(self, mock_urlopen):
        """Should return empty list when a message has an unparseable timestamp."""
        messages = [
            {
                "id": "msg-1",
                "from_role": "coder",
                "to_role": "all",
                "message_type": "STATUS",
                "subject": "Working",
                "timestamp": "not-a-date",
            },
        ]
        body = json.dumps({"data": {"messages": messages}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # The exception from datetime.fromisoformat is caught by the broad except
        result = _fetch_inter_agent_messages("issue-999", "coder")
        # Returns empty because the exception aborts all message processing
        assert result == []
