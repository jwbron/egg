"""Unit tests for egg_agent_tools.handlers.sdlc.

Covers register_open_question, request_feedback, and check_hitl_answers.

The handlers talk to the gateway via :func:`gateway_request`.  Tests patch
that function directly so no real HTTP traffic is generated.  This also
lets us assert exactly what payload was sent to the gateway.

Each handler has three shaped tests: happy-path, missing-arg, and
gateway-error.  The gateway-error scenarios assert ``GatewayError`` is
*raised* — never turned into a silent success dict, never ``sys.exit``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure sandbox / shared are importable.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import sdlc  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


def _fake_contract(**overrides):
    base = {
        "current_phase": "plan",
        "decisions": [],
        "feedback": None,
    }
    base.update(overrides)
    return base


class TestRegisterOpenQuestion:
    def test_happy_path_with_options(self):
        fake_contract = _fake_contract()
        responses = [
            {"success": True, "data": fake_contract},
            {"success": True, "data": {}},
        ]

        def fake_gateway(endpoint, **kwargs):
            return responses.pop(0)

        with (
            patch("egg_agent_tools.handlers.sdlc.gateway_request", side_effect=fake_gateway) as gr,
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=42),
        ):
            resp = sdlc.register_open_question(
                {"question": "A or B?", "options": ["A", "B"], "repo_path": "/repo"}
            )

        assert resp["ok"] is True
        assert resp["id"] == "decision-1"
        # Options get an "Other" appended automatically.
        labels = [o["label"] for o in resp["decision"]["options"]]
        assert labels == ["A", "B", "Other (explain in reply)"]

        # Second call is the mutate; verify the payload shape.
        mutate_kwargs = gr.call_args_list[1].kwargs
        data = mutate_kwargs["data"]
        assert data["field_path"] == "decisions.0"
        assert data["new_value"]["phase"] == "plan"
        assert data["actor"] == "egg"

    def test_no_options_keeps_list_empty(self):
        fake_contract = _fake_contract()
        responses = [
            {"success": True, "data": fake_contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=42),
        ):
            resp = sdlc.register_open_question({"question": "q?"})
        # When no options are given the handler leaves the list empty
        # (parity with the CLI — the "Other" suffix only appears when
        # the caller supplied a non-empty list).
        assert resp["decision"]["options"] == []

    def test_phase_override_respected(self):
        fake_contract = _fake_contract(current_phase="plan")
        responses = [
            {"success": True, "data": fake_contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.register_open_question({"question": "q?", "phase": "implement"})
        assert resp["decision"]["phase"] == "implement"

    def test_missing_question_raises_handler_error(self):
        with pytest.raises(HandlerError):
            sdlc.register_open_question({})

    def test_invalid_phase_rejected(self):
        with pytest.raises(HandlerError):
            sdlc.register_open_question({"question": "q?", "phase": "bogus"})

    def test_missing_identifier_raises_handler_error(self):
        with patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=None):
            with pytest.raises(HandlerError):
                sdlc.register_open_question({"question": "q?"})

    def test_gateway_500_raises_gateway_error(self):
        """Handler propagates GatewayError — must NOT return a success dict
        and must NOT call sys.exit."""

        def boom(*a, **kw):
            raise GatewayError("Internal Server Error", status_code=500)

        with (
            patch("egg_agent_tools.handlers.sdlc.gateway_request", side_effect=boom),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=7),
        ):
            with pytest.raises(GatewayError) as exc:
                sdlc.register_open_question({"question": "q?"})
        assert "Internal Server Error" in str(exc.value)

    def test_unsuccessful_response_raises_gateway_error(self):
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": False, "message": "bad"},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                sdlc.register_open_question({"question": "q?"})


class TestRequestFeedback:
    def test_happy_path_multiple_questions(self):
        fake_contract = _fake_contract()
        responses = [
            {"success": True, "data": fake_contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=42),
        ):
            resp = sdlc.request_feedback({"questions": ["what?", "why?"], "repo_path": "/repo"})
        assert resp["ok"] is True
        assert len(resp["questions"]) == 2
        assert resp["questions"][0]["id"] == "Q1"
        assert "markdown" in resp

    def test_accepts_question_alias(self):
        """Parity with CLI which uses repeated --question."""
        fake_contract = _fake_contract()
        responses = [
            {"success": True, "data": fake_contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.request_feedback({"question": ["only one"]})
        assert len(resp["questions"]) == 1

    def test_existing_pending_feedback_surfaces_warning(self):
        fake_contract = _fake_contract(feedback={"id": "feedback-1", "submitted": False})
        responses = [
            {"success": True, "data": fake_contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.request_feedback({"questions": ["hi"]})
        assert "warning" in resp

    def test_empty_questions_raises(self):
        with pytest.raises(HandlerError):
            sdlc.request_feedback({"questions": []})

    def test_gateway_error_propagates(self):
        def boom(*a, **kw):
            raise GatewayError("timeout")

        with (
            patch("egg_agent_tools.handlers.sdlc.gateway_request", side_effect=boom),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                sdlc.request_feedback({"questions": ["x"]})


class TestCheckHitlAnswers:
    def test_returns_only_resolved_by_default(self):
        decisions = [
            {"id": "d1", "phase": "plan", "resolved": True, "resolution": {"id": "opt-1"}},
            {"id": "d2", "phase": "plan", "resolved": False, "resolution": None},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": {"decisions": decisions}},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.check_hitl_answers({"phase": "plan"})
        assert [d["id"] for d in resp["decisions"]] == ["d1"]

    def test_include_unresolved_flag(self):
        decisions = [
            {"id": "d1", "phase": "plan", "resolved": True},
            {"id": "d2", "phase": "plan", "resolved": False},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": {"decisions": decisions}},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.check_hitl_answers({"phase": "plan", "include_unresolved": True})
        assert {d["id"] for d in resp["decisions"]} == {"d1", "d2"}

    def test_no_phase_returns_all_phases_resolved(self):
        """With no ``phase`` argument, resolved decisions from every phase
        are returned — including prior phases the operator already closed
        out. Regression test for #1959."""
        decisions = [
            {"id": "d1", "phase": "refine", "resolved": True, "resolution": {"id": "opt-1"}},
            {"id": "d2", "phase": "refine", "resolved": True, "resolution": {"id": "opt-2"}},
            {"id": "d3", "phase": "plan", "resolved": False, "resolution": None},
            {"id": "d4", "phase": "plan", "resolved": True, "resolution": {"id": "opt-4"}},
        ]
        feedback = {"id": "feedback-1", "phase": "refine", "submitted": True}
        data = {"decisions": decisions, "feedback": feedback}
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": data},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
            # EGG_PHASE must NOT affect the default — prove it by setting one.
            patch.dict("os.environ", {"EGG_PHASE": "plan"}, clear=False),
        ):
            resp = sdlc.check_hitl_answers({})
        assert {d["id"] for d in resp["decisions"]} == {"d1", "d2", "d4"}
        assert resp["feedback"] == feedback

    def test_phase_filter_applied_to_feedback(self):
        data = {
            "decisions": [],
            "feedback": {"id": "feedback-1", "phase": "refine"},
        }
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": data},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.check_hitl_answers({"phase": "plan"})
        # Feedback belongs to another phase — filtered out.
        assert resp["feedback"] is None

    def test_gateway_error_propagates(self):
        def boom(*a, **kw):
            raise GatewayError("server error", status_code=500)

        with (
            patch("egg_agent_tools.handlers.sdlc.gateway_request", side_effect=boom),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                sdlc.check_hitl_answers({})

    def test_invalid_phase_rejected(self):
        with pytest.raises(HandlerError):
            sdlc.check_hitl_answers({"phase": "bogus"})

    def test_response_shape_matches_declared_schema(self):
        """Assert the response dict has the documented keys."""
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": {"decisions": [], "feedback": None}},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.check_hitl_answers({})
        assert set(resp.keys()) >= {"ok", "decisions", "feedback"}
