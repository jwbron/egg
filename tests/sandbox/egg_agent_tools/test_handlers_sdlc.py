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
        assert resp["id"] == "cq-1"
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

    def test_toctou_retry_on_index_conflict(self):
        """Two concurrent agents may compute the same decision index;
        the loser's write should retry after re-reading the contract."""
        first_contract = _fake_contract(decisions=[])
        second_contract = _fake_contract(decisions=[{"id": "cq-1", "question": "other agent's"}])
        responses = [
            {"success": True, "data": first_contract},  # attempt 1 read
            {"success": False, "message": "Array index 0 out of range"},
            {"success": True, "data": second_contract},  # attempt 2 read
            {"success": True, "data": {}},  # attempt 2 mutate
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=42),
        ):
            resp = sdlc.register_open_question({"question": "q?"})
        assert resp["ok"] is True
        # Retried: cq counter is now 2 (one cq-1 already present),
        # array index is now 1 (one entry already present).
        assert resp["id"] == "cq-2"
        mutate_data = gr.call_args_list[3].kwargs["data"]
        assert mutate_data["field_path"] == "decisions.1"

    def test_cq_prefix_skips_legacy_decision_ids_in_namespace(self):
        """Regression for #2616: when the contract is pre-populated with
        legacy ``decision-N`` entries (mirrors of the orchestrator's
        bridged refine decisions and the phase_gate), a fresh
        registration must allocate ``cq-1`` rather than re-using a
        ``decision-N`` that already exists on the pipeline side and
        triggers HTTP 409 on ``provide_input``."""
        legacy = _fake_contract(
            current_phase="implement",
            decisions=[{"id": f"decision-{i}", "question": f"q{i}"} for i in range(1, 14)],
        )
        responses = [
            {"success": True, "data": legacy},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1557),
        ):
            resp = sdlc.register_open_question({"question": "scope dispute?"})
        # Cq counter ignores ``decision-N`` entries, so the first new
        # contract question is ``cq-1`` — no collision with the
        # phase_gate that the orchestrator allocated as ``decision-14``.
        assert resp["id"] == "cq-1"
        # Appended at the end of the existing array.
        mutate_data = gr.call_args_list[1].kwargs["data"]
        assert mutate_data["field_path"] == "decisions.13"

    def test_toctou_non_retryable_error_bails_immediately(self):
        """A non-TOCTOU gateway error must not be retried."""
        fake_contract = _fake_contract()
        responses = [
            {"success": True, "data": fake_contract},
            {"success": False, "message": "role not authorized"},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            with pytest.raises(GatewayError):
                sdlc.register_open_question({"question": "q?"})
        # Exactly two calls — read + mutate; no retry.
        assert gr.call_count == 2


class TestFetchContractNullData:
    """_fetch_contract must return {} when the gateway response has
    data=null (key exists with null value — the {} default is unused)."""

    def test_null_data_returns_empty_dict(self):
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": None},
            ),
            patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=1),
        ):
            resp = sdlc.show_contract({})
        assert resp["ok"] is True
        assert resp["contract"] == {}


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


# ---------------------------------------------------------------------------
# Iter-2 (#1917): show_contract + verify_criterion
# ---------------------------------------------------------------------------


class TestShowContract:
    def _mock(self, contract_data: dict):
        return patch(
            "egg_agent_tools.handlers.sdlc.gateway_request",
            return_value={"success": True, "data": contract_data},
        )

    def _id(self, value=42):
        return patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=value)

    def test_happy_path_returns_full_contract(self):
        contract = {"current_phase": "plan", "decisions": [], "phases": []}
        with self._mock(contract), self._id():
            resp = sdlc.show_contract({})
        assert resp["ok"] is True
        assert resp["contract"]["current_phase"] == "plan"

    def test_fields_projection_returns_only_named_fields(self):
        contract = {
            "current_phase": "plan",
            "decisions": [{"id": "d1"}],
            "phases": [{"id": "p1"}],
        }
        with self._mock(contract), self._id():
            resp = sdlc.show_contract({"fields": ["current_phase", "decisions"]})
        assert set(resp["contract"].keys()) == {"current_phase", "decisions"}
        # Untouched field must be stripped.
        assert "phases" not in resp["contract"]

    def test_empty_fields_list_returns_empty_contract(self):
        """Edge case: explicit [] projects to zero keys — the handler
        should honour that rather than treating it as 'no projection'."""
        contract = {"current_phase": "plan", "decisions": []}
        with self._mock(contract), self._id():
            resp = sdlc.show_contract({"fields": []})
        assert resp["contract"] == {}

    def test_unknown_field_raises_handler_error(self):
        """decision-4 requirement: unknown names must raise, not silently
        skip — agents learn the contract shape."""
        contract = {"current_phase": "plan"}
        with self._mock(contract), self._id():
            with pytest.raises(HandlerError) as exc:
                sdlc.show_contract({"fields": ["not_a_field"]})
        assert "Unknown field" in str(exc.value)

    def test_non_list_fields_rejected(self):
        contract = {"current_phase": "plan"}
        with self._mock(contract), self._id():
            with pytest.raises(HandlerError):
                sdlc.show_contract({"fields": "current_phase"})

    def test_non_string_field_entry_rejected(self):
        contract = {"current_phase": "plan"}
        with self._mock(contract), self._id():
            with pytest.raises(HandlerError):
                sdlc.show_contract({"fields": [123]})

    def test_audit_flag_passes_through_to_gateway_params(self):
        contract = {"current_phase": "plan", "audit_log": [{"timestamp": "t"}]}
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": contract},
            ) as gr,
            self._id(),
        ):
            sdlc.show_contract({"audit": True})
        params = gr.call_args.kwargs.get("params") or {}
        assert params.get("include_audit_log") == "true"

    def test_gateway_error_propagates(self):
        def boom(*a, **kw):
            raise GatewayError("boom", status_code=503)

        with (
            patch("egg_agent_tools.handlers.sdlc.gateway_request", side_effect=boom),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                sdlc.show_contract({})

    def test_unsuccessful_response_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": False, "message": "no such contract"},
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                sdlc.show_contract({})

    def test_missing_identifier_raises_handler_error(self):
        with patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=None):
            with pytest.raises(HandlerError):
                sdlc.show_contract({})


class TestVerifyCriterion:
    @staticmethod
    def _contract_with_criteria(count: int = 5):
        return {
            "acceptance_criteria": [
                {"id": f"ac-{i + 1}", "description": f"criterion {i + 1}", "verified": False}
                for i in range(count)
            ]
        }

    def _id(self, value=42):
        return patch("egg_agent_tools.handlers.sdlc.get_contract_identifier", return_value=value)

    def test_happy_path_mutates_correct_field_path(self):
        contract = self._contract_with_criteria(5)
        responses = [
            {"success": True, "data": contract},  # pre-flight read
            {"success": True, "data": {}},  # mutate
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
        ):
            resp = sdlc.verify_criterion({"criterion": "ac-3"})
        assert resp == {"ok": True, "criterion": "ac-3"}
        data = gr.call_args_list[1].kwargs["data"]
        # 1-based ac-3 → 0-based index 2.
        assert data["field_path"] == "acceptance_criteria.2.verified"
        assert data["new_value"] is True

    def test_missing_criterion_raises_handler_error(self):
        with pytest.raises(HandlerError):
            sdlc.verify_criterion({})

    @pytest.mark.parametrize("bad", ["", "1", "ac-", "ac-0", "ac-a", "AC-", "criterion-1"])
    def test_invalid_criterion_id(self, bad):
        with pytest.raises(HandlerError):
            sdlc.verify_criterion({"criterion": bad})

    def test_case_insensitive_prefix(self):
        """`AC-5` should resolve just like `ac-5` — CLI parity."""
        contract = self._contract_with_criteria(5)
        responses = [
            {"success": True, "data": contract},
            {"success": True, "data": {}},
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ) as gr,
            self._id(),
        ):
            sdlc.verify_criterion({"criterion": "AC-5"})
        data = gr.call_args_list[1].kwargs["data"]
        assert data["field_path"] == "acceptance_criteria.4.verified"

    def test_criterion_out_of_range_raises_handler_error(self):
        """Pre-flight bounds check: ac-999 on a contract with 2 criteria
        must raise HandlerError before the mutate call."""
        contract = self._contract_with_criteria(2)
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                return_value={"success": True, "data": contract},
            ) as gr,
            self._id(),
        ):
            with pytest.raises(HandlerError) as exc:
                sdlc.verify_criterion({"criterion": "ac-999"})
        assert "out of range" in str(exc.value).lower()
        # Only one call — the pre-flight read; mutate was never attempted.
        assert gr.call_count == 1

    def test_gateway_unauthorized_surfaces_as_gateway_error(self):
        """decision-7: the gateway enforces REVIEWER — the handler is a
        thin forward. A role-denial failure on the mutate must surface
        as GatewayError (not silently return success)."""
        contract = self._contract_with_criteria(3)
        responses = [
            {"success": True, "data": contract},  # pre-flight read succeeds
            {
                "success": False,
                "message": "Role 'implementer' not authorized to modify this field",
            },
        ]
        with (
            patch(
                "egg_agent_tools.handlers.sdlc.gateway_request",
                side_effect=lambda *a, **kw: responses.pop(0),
            ),
            self._id(),
        ):
            with pytest.raises(GatewayError) as exc:
                sdlc.verify_criterion({"criterion": "ac-1"})
        assert "not authorized" in str(exc.value).lower()

    def test_gateway_exception_propagates(self):
        def boom(*a, **kw):
            raise GatewayError("net down")

        with (
            patch("egg_agent_tools.handlers.sdlc.gateway_request", side_effect=boom),
            self._id(),
        ):
            with pytest.raises(GatewayError):
                sdlc.verify_criterion({"criterion": "ac-2"})

    def test_docstring_mentions_reviewer_role(self):
        """Agents self-select on the REVIEWER-role requirement from the
        docstring — decision-7."""
        assert sdlc.verify_criterion.__doc__ is not None
        assert "REVIEWER" in sdlc.verify_criterion.__doc__
