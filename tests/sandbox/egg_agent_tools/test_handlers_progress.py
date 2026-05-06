"""Unit tests for egg_agent_tools.handlers.progress.

Covers progress_emit, progress_signal_error, progress_heartbeat.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import progress  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


class TestProgressEmit:
    """progress_emit hits the structured-event endpoint (step/state)."""

    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {"event": {"id": "ev-1"}}},
        ) as req:
            resp = progress.progress_emit(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "step": "refactor handlers",
                    "state": "working",
                    "detail": "halfway",
                }
            )
        assert resp["ok"] is True
        assert resp["event_id"] == "ev-1"
        data = req.call_args.kwargs["data"]
        assert data["step"] == "refactor handlers"
        assert data["state"] == "working"
        assert data["detail"] == "halfway"

    def test_missing_step(self):
        with pytest.raises(HandlerError):
            progress.progress_emit({"pipeline_id": "p", "role": "coder", "state": "working"})

    def test_missing_state(self):
        with pytest.raises(HandlerError):
            progress.progress_emit({"pipeline_id": "p", "role": "coder", "step": "x"})

    def test_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("orch down", status_code=503),
        ):
            with pytest.raises(GatewayError):
                progress.progress_emit(
                    {
                        "pipeline_id": "p",
                        "role": "coder",
                        "step": "x",
                        "state": "working",
                    }
                )

    def test_unsuccessful_response_raises(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": False, "message": "denied"},
        ):
            with pytest.raises(GatewayError):
                progress.progress_emit(
                    {
                        "pipeline_id": "p",
                        "role": "coder",
                        "step": "x",
                        "state": "working",
                    }
                )


class TestProgressSignalError:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as req:
            resp = progress.progress_signal_error(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "error": "oh no",
                    "recoverable": True,
                }
            )
        assert resp["ok"] is True
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "error"
        assert data["error"] == "oh no"
        assert data["recoverable"] is True

    def test_missing_error(self):
        with pytest.raises(HandlerError):
            progress.progress_signal_error({"pipeline_id": "p", "role": "r"})

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                progress.progress_signal_error({"pipeline_id": "p", "role": "r", "error": "x"})


class TestProgressHeartbeat:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as req:
            resp = progress.progress_heartbeat({"pipeline_id": "p", "role": "coder"})
        assert resp["ok"] is True
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "heartbeat"

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                progress.progress_heartbeat({"pipeline_id": "p", "role": "r"})

    def test_attaches_slice_id_from_env(self, monkeypatch):
        # #2473: heartbeat must mirror progress_signal_error's slice_id
        # forwarding so a future slice-scoped consumer can route correctly.
        monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as req:
            progress.progress_heartbeat({"pipeline_id": "p", "role": "coder"})
        assert req.call_args.kwargs["data"]["slice_id"] == "slice-3"

    def test_omits_slice_id_when_unset(self, monkeypatch):
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as req:
            progress.progress_heartbeat({"pipeline_id": "p", "role": "coder"})
        assert "slice_id" not in req.call_args.kwargs["data"]

    def test_invalid_slice_id_rejected(self, monkeypatch):
        monkeypatch.setenv("EGG_SLICE_ID", "slice-2/../etc")
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ):
            with pytest.raises(HandlerError, match="slice_id"):
                progress.progress_heartbeat({"pipeline_id": "p", "role": "coder"})


# ---------------------------------------------------------------------------
# Iter-2 (#1917): progress_overseer_alert + progress_query_status
# ---------------------------------------------------------------------------


class TestProgressOverseerAlert:
    """overseer_alert posts to ``/api/v1/pipelines/<pid>/messages`` with
    message_type=OVERSEER_ALERT and to_role='all' hard-coded."""

    def test_happy_path_builds_message(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={
                "success": True,
                "data": {"message": {"id": "m-1", "message_type": "OVERSEER_ALERT"}},
            },
        ) as req:
            resp = progress.progress_overseer_alert(
                {
                    "pipeline_id": "issue-1",
                    "role": "overseer",
                    "anomaly": "agent-loop",
                    "priority": "high",
                    "summary": "loop detected",
                    "detail": "repeats every 5s",
                    "recommend": "kill it",
                }
            )
        assert resp["ok"] is True
        assert resp["alert"]["id"] == "m-1"
        data = req.call_args.kwargs["data"]
        assert data["message_type"] == "OVERSEER_ALERT"
        assert data["to_role"] == "all"  # hard-coded per handler contract
        assert data["subject"] == "agent-loop [high]"
        assert "loop detected" in data["body"]
        assert "repeats every 5s" in data["body"]
        assert "kill it" in data["body"]
        assert req.call_args.args[0] == "/api/v1/pipelines/issue-1/messages"

    def test_defaults_role_to_overseer_when_env_missing(self):
        """Handler must fall back to 'overseer' when neither req.role
        nor EGG_AGENT_ROLE is set — the whole point of this tool is the
        overseer role."""
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": {"message": {}}},
            ),
            patch("egg_agent_tools.handlers.progress.get_agent_role", return_value=None),
        ):
            resp = progress.progress_overseer_alert(
                {
                    "pipeline_id": "p",
                    "anomaly": "foo",
                    "priority": "low",
                    "summary": "s",
                }
            )
        assert resp["role"] == "overseer"

    @pytest.mark.parametrize("missing", ["anomaly", "priority", "summary"])
    def test_required_fields(self, missing):
        base = {
            "pipeline_id": "p",
            "role": "overseer",
            "anomaly": "x",
            "priority": "low",
            "summary": "s",
        }
        del base[missing]
        with pytest.raises(HandlerError):
            progress.progress_overseer_alert(base)

    def test_invalid_priority_rejected(self):
        with pytest.raises(HandlerError) as exc:
            progress.progress_overseer_alert(
                {
                    "pipeline_id": "p",
                    "role": "overseer",
                    "anomaly": "foo",
                    "priority": "urgent",  # not in enum
                    "summary": "s",
                }
            )
        assert "priority" in str(exc.value).lower()

    def test_detail_type_enforced(self):
        with pytest.raises(HandlerError):
            progress.progress_overseer_alert(
                {
                    "pipeline_id": "p",
                    "role": "overseer",
                    "anomaly": "foo",
                    "priority": "low",
                    "summary": "s",
                    "detail": 123,
                }
            )

    def test_recommend_type_enforced(self):
        with pytest.raises(HandlerError):
            progress.progress_overseer_alert(
                {
                    "pipeline_id": "p",
                    "role": "overseer",
                    "anomaly": "foo",
                    "priority": "low",
                    "summary": "s",
                    "recommend": ["a", "b"],
                }
            )

    def test_missing_pipeline_id_raises(self):
        with patch("egg_agent_tools.handlers.progress.get_pipeline_id", return_value=None):
            with pytest.raises(HandlerError):
                progress.progress_overseer_alert(
                    {
                        "anomaly": "foo",
                        "priority": "low",
                        "summary": "s",
                    }
                )

    def test_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("boom", status_code=503),
        ):
            with pytest.raises(GatewayError):
                progress.progress_overseer_alert(
                    {
                        "pipeline_id": "p",
                        "role": "overseer",
                        "anomaly": "foo",
                        "priority": "low",
                        "summary": "s",
                    }
                )

    def test_unsuccessful_response_raises(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": False, "message": "refused"},
        ):
            with pytest.raises(GatewayError):
                progress.progress_overseer_alert(
                    {
                        "pipeline_id": "p",
                        "role": "overseer",
                        "anomaly": "foo",
                        "priority": "low",
                        "summary": "s",
                    }
                )


class TestProgressQueryStatus:
    """query_status hits GET /api/v1/pipelines/<pid>/status.

    Security (reviewer_code NACK #2 + risk_analyst R2): a
    caller-supplied ``pipeline_id`` must match ``EGG_PIPELINE_ID``
    when the env var is set.  When env is unset (operator-shell use)
    the caller value is accepted as a fallback.
    """

    def _env_pid(self, value="issue-7"):
        return patch("egg_agent_tools.handlers.progress.get_pipeline_id", return_value=value)

    def test_happy_path_env_pipeline_id(self):
        status_payload = {
            "status": "in_progress",
            "current_phase": "implement",
            "pending_decisions": 2,
            "updated_at": "2026-04-24T00:00:00Z",
        }
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": status_payload},
            ) as req,
            self._env_pid("issue-7"),
        ):
            resp = progress.progress_query_status({})
        assert resp["ok"] is True
        assert resp["status"] == "in_progress"
        assert resp["current_phase"] == "implement"
        assert resp["pending_decisions"] == 2
        assert resp["updated_at"] == "2026-04-24T00:00:00Z"
        assert req.call_args.args[0] == "/api/v1/pipelines/issue-7/status"

    def test_caller_pipeline_id_matching_env_is_accepted(self):
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": {"status": "idle"}},
            ),
            self._env_pid("issue-7"),
        ):
            resp = progress.progress_query_status({"pipeline_id": "issue-7"})
        assert resp["ok"] is True

    def test_caller_pipeline_id_disagreeing_with_env_rejected(self):
        """Cross-pipeline-read hardening: agent cannot query a
        different pipeline than the one it's bound to."""
        with self._env_pid("issue-7"):
            with pytest.raises(HandlerError) as exc:
                progress.progress_query_status({"pipeline_id": "issue-8"})
        assert "must match" in str(exc.value).lower()

    def test_caller_pipeline_id_accepted_when_env_missing(self):
        """Operator-shell fallback: if EGG_PIPELINE_ID is unset the
        caller may name a pipeline directly."""
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": {"status": "idle"}},
            ),
            self._env_pid(None),
        ):
            resp = progress.progress_query_status({"pipeline_id": "issue-99"})
        assert resp["pipeline_id"] == "issue-99"

    def test_include_raw_returns_full_payload(self):
        status_payload = {"status": "idle", "extra": {"more": "stuff"}}
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": status_payload},
            ),
            self._env_pid("p"),
        ):
            resp = progress.progress_query_status({"include_raw": True})
        assert resp["raw"] == status_payload

    def test_missing_pipeline_id_raises(self):
        with self._env_pid(None):
            with pytest.raises(HandlerError):
                progress.progress_query_status({})

    def test_orchestrator_success_false_surfaces_as_gateway_error(self):
        """Missing pipelines come back as {success: False} — handler
        must translate to GatewayError so the MCP client gets an
        is_error result instead of a successful empty-data read."""
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": False, "message": "no such pipeline"},
            ),
            self._env_pid("issue-7"),
        ):
            with pytest.raises(GatewayError):
                progress.progress_query_status({})

    def test_gateway_exception_propagates(self):
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                side_effect=GatewayError("500", status_code=500),
            ),
            self._env_pid("issue-7"),
        ):
            with pytest.raises(GatewayError):
                progress.progress_query_status({})

    def test_defaults_pending_decisions_to_zero(self):
        """When the payload omits pending_decisions, the handler must
        default it to 0 rather than surface None."""
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": {"status": "idle"}},
            ),
            self._env_pid("issue-7"),
        ):
            resp = progress.progress_query_status({})
        assert resp["pending_decisions"] == 0

    def test_null_data_returns_empty_status(self):
        """When gateway returns {success: true, data: null}, the handler
        must not raise AttributeError."""
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": None},
            ),
            self._env_pid("issue-7"),
        ):
            resp = progress.progress_query_status({})
        assert resp["ok"] is True
        assert resp["status"] is None

    def test_absent_success_key_raises_gateway_error(self):
        """A malformed response without the 'success' key must raise
        GatewayError (consistent with other handlers)."""
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"data": {"status": "idle"}},
            ),
            self._env_pid("issue-7"),
        ):
            with pytest.raises(GatewayError):
                progress.progress_query_status({})


class TestPipelineIdValidation:
    """Pipeline IDs are interpolated into URL paths — format validation
    prevents path traversal."""

    def test_valid_pipeline_id_accepted(self):
        with (
            patch(
                "egg_agent_tools.handlers.progress.orchestrator_request",
                return_value={"success": True, "data": {"event": {"id": "e"}}},
            ),
        ):
            resp = progress.progress_emit(
                {"pipeline_id": "issue-7", "role": "coder", "step": "x", "state": "working"}
            )
        assert resp["ok"] is True

    def test_pipeline_id_with_traversal_rejected(self):
        with pytest.raises(HandlerError) as exc:
            progress.progress_emit(
                {"pipeline_id": "../other", "role": "coder", "step": "x", "state": "working"}
            )
        assert "Invalid pipeline_id" in str(exc.value)

    def test_pipeline_id_with_slashes_rejected(self):
        with pytest.raises(HandlerError):
            progress.progress_emit(
                {"pipeline_id": "a/b/c", "role": "coder", "step": "x", "state": "working"}
            )


class TestProgressQueryStatusPipelineIdValidation:
    """The inline format check at progress.py:240-241 bypasses
    _require_pipeline_id — verify it directly."""

    def test_traversal_pipeline_id_rejected(self):
        with patch("egg_agent_tools.handlers.progress.get_pipeline_id", return_value=None):
            with pytest.raises(HandlerError, match="Invalid pipeline_id"):
                progress.progress_query_status({"pipeline_id": "../x"})

    def test_pipeline_id_with_slashes_rejected(self):
        with patch("egg_agent_tools.handlers.progress.get_pipeline_id", return_value=None):
            with pytest.raises(HandlerError, match="Invalid pipeline_id"):
                progress.progress_query_status({"pipeline_id": "a/b/c"})


class TestProgressEmitNullData:
    """progress_emit must handle null data from orchestrator gracefully."""

    def test_null_data_no_attribute_error(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": None},
        ):
            resp = progress.progress_emit(
                {"pipeline_id": "p", "role": "coder", "step": "x", "state": "working"}
            )
        assert resp["ok"] is True
        assert resp["event_id"] is None
