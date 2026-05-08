"""Unit tests for egg_agent_tools.handlers.brc.

Covers brc_propose/ack/nack/confirm/get_state/list_blocking.  Tests patch
:func:`orchestrator_request` so no HTTP traffic occurs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))
# Orchestrator path is needed for the cross-check tests that import
# ``attestation_schemas.validate_attestation`` and assert pre-flight
# verdicts agree with the strict-mode orchestrator validator.
sys.path.insert(0, str(ROOT / "orchestrator"))

from egg_agent_tools.handlers import brc  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


def _ok_response(**extra):
    data = {"consensus": {"agents": {"coder": {"phase": "implement"}}}}
    data.update(extra)
    return {"success": True, "data": data}


class TestBrcPropose:
    def test_happy_path(self):
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value=_ok_response(),
            ) as req,
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="abc1234"),
        ):
            resp = brc.brc_propose(
                {
                    "pipeline_id": "pipe-1",
                    "role": "coder",
                    "summary": "x" * 60,
                    "artifacts": ["f.py"],
                    "tasks": ["task-1-1"],
                }
            )
        assert resp["ok"] is True
        assert resp["role"] == "coder"
        assert resp["phase"] == "implement"
        assert req.call_count == 1
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_propose"
        assert data["payload"]["summary"] == "x" * 60
        assert data["payload"]["tasks_satisfied"] == ["task-1-1"]
        assert data["payload"]["commit_sha"] == "abc1234"

    def test_missing_summary(self):
        with pytest.raises(HandlerError):
            brc.brc_propose({"pipeline_id": "p", "role": "coder"})

    def test_missing_pipeline_id(self):
        with (
            patch.dict("os.environ", {}, clear=False),
            patch("egg_agent_tools.handlers.brc.get_pipeline_id", return_value=None),
        ):
            with pytest.raises(HandlerError):
                brc.brc_propose({"role": "coder", "summary": "x" * 60})

    def test_missing_role(self):
        with patch("egg_agent_tools.handlers.brc.get_agent_role", return_value=None):
            with pytest.raises(HandlerError):
                brc.brc_propose({"pipeline_id": "p", "summary": "x" * 60})

    def test_invalid_commit_sha_raises(self):
        with pytest.raises(HandlerError, match="Invalid commit SHA"):
            brc.brc_propose(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "summary": "x" * 60,
                    "commit_sha": "not-a-hex-sha",
                }
            )

    def test_gateway_500_raises_gateway_error(self):
        def boom(*a, **kw):
            raise GatewayError("upstream down", status_code=500)

        with (
            patch("egg_agent_tools.handlers.brc.orchestrator_request", side_effect=boom),
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="a" * 40),
        ):
            with pytest.raises(GatewayError):
                brc.brc_propose({"pipeline_id": "p", "role": "coder", "summary": "x" * 60})

    def test_unsuccessful_response_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value={"success": False, "message": "nope"},
            ),
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="a" * 40),
        ):
            with pytest.raises(GatewayError):
                brc.brc_propose({"pipeline_id": "p", "role": "coder", "summary": "x" * 60})


class TestBrcProposeTesterAttestationPreFlight:
    """Pre-flight tester-attestation validation in ``brc_propose`` (#2338).

    Mirrors the orchestrator's strict-mode tester checks but runs at the
    handler boundary so misconfigurations fail locally with an actionable
    HandlerError rather than as a 400 bouncing off the orchestrator.
    """

    def _propose_tester(self, attestation: dict):
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value=_ok_response(),
            ),
            patch(
                "egg_agent_tools.handlers.brc._resolve_head_sha",
                return_value="abc1234",
            ),
        ):
            return brc.brc_propose(
                {
                    "pipeline_id": "pipe-1",
                    "role": "tester",
                    "summary": "x" * 60,
                    "attestation": attestation,
                }
            )

    def test_missing_tests_run_rejected_pre_flight(self):
        """Empty / missing tests_run is rejected at handler boundary."""
        with pytest.raises(HandlerError, match="tests_run > 0"):
            self._propose_tester({})

    def test_zero_tests_run_rejected(self):
        with pytest.raises(HandlerError, match="tests_run > 0"):
            self._propose_tester({"tests_run": 0, "checks_passed": ["test"]})

    def test_non_integer_tests_run_rejected(self):
        with pytest.raises(HandlerError, match="must be an integer"):
            self._propose_tester({"tests_run": "many", "checks_passed": ["test"]})

    def test_missing_checks_passed_rejected(self):
        with pytest.raises(HandlerError, match="checks_passed"):
            self._propose_tester({"tests_run": 5})

    def test_empty_checks_passed_rejected(self):
        with pytest.raises(HandlerError, match="checks_passed"):
            self._propose_tester({"tests_run": 5, "checks_passed": []})

    def test_happy_path_passes_pre_flight(self):
        resp = self._propose_tester({"tests_run": 42, "checks_passed": ["lint", "test"]})
        assert resp["ok"] is True

    def test_blocked_with_reason_passes(self):
        """tests_execution_blocked=true with a reason is accepted —
        no tests_run / checks_passed required for blocked pipelines."""
        resp = self._propose_tester(
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": (
                    "private-network mode blocked package downloads"
                ),
            }
        )
        assert resp["ok"] is True

    def test_blocked_without_reason_rejected(self):
        with pytest.raises(HandlerError, match="tests_execution_blocked_reason"):
            self._propose_tester({"tests_execution_blocked": True})

    def test_blocked_with_tests_run_conflict(self):
        """blocked=true + tests_run > 0 is contradictory."""
        with pytest.raises(HandlerError, match="conflicts with tests_run"):
            self._propose_tester(
                {
                    "tests_execution_blocked": True,
                    "tests_execution_blocked_reason": "x",
                    "tests_run": 5,
                }
            )

    def test_other_roles_skip_validator(self):
        """Coder / documenter etc. should not be subject to tester
        attestation requirements — pre-flight only fires on role=tester."""
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value=_ok_response(),
            ),
            patch(
                "egg_agent_tools.handlers.brc._resolve_head_sha",
                return_value="abc",
            ),
        ):
            resp = brc.brc_propose(
                {
                    "pipeline_id": "pipe-1",
                    "role": "coder",
                    "summary": "x" * 60,
                    "attestation": {},  # empty — would fail tester pre-flight
                }
            )
        assert resp["ok"] is True

    def test_omitted_attestation_treated_as_empty_and_rejected(self):
        """Caller that omits ``attestation`` entirely defaults to ``{}``,
        which fails strict-mode pre-flight — the canonical "tester
        forgot to populate" scenario from #2338. The handler surfaces
        the same actionable error the orchestrator would have returned
        as a 400."""
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value=_ok_response(),
            ),
            patch(
                "egg_agent_tools.handlers.brc._resolve_head_sha",
                return_value="abc",
            ),
        ):
            with pytest.raises(HandlerError, match="tests_run > 0"):
                brc.brc_propose(
                    {
                        "pipeline_id": "pipe-1",
                        "role": "tester",
                        "summary": "x" * 60,
                        # No attestation — defaults to {} and fails pre-flight.
                    }
                )

    # --- Coverage-gap cases flagged in the PR review --------------------

    def test_non_list_checks_passed_rejected(self):
        """``checks_passed`` as a non-list (string) trips the
        ``isinstance(..., list)`` guard. The orchestrator's Pydantic
        parse step also rejects this, so pre-flight catches it first
        with a friendlier message."""
        with pytest.raises(HandlerError, match="checks_passed"):
            self._propose_tester({"tests_run": 5, "checks_passed": "lint"})

    def test_negative_tests_run_passes_pre_flight(self):
        """The orchestrator's ``_validate_strict`` only rejects
        ``tests_run == 0`` (negative ints slip past Pydantic's plain
        ``int`` field). Pre-flight intentionally mirrors that — see the
        cross-check test below."""
        resp = self._propose_tester({"tests_run": -1, "checks_passed": ["test"]})
        assert resp["ok"] is True

    def test_string_false_tests_execution_blocked_passes_through(self):
        """Pydantic v2 coerces the string ``"false"`` to ``False``, so
        the orchestrator routes a payload like
        ``{"tests_execution_blocked": "false", "tests_run": 5,
        "checks_passed": ["t"]}`` into the non-blocked branch and
        accepts it. Pre-flight uses ``_coerce_attestation_bool`` to
        match — without it, ``bool("false")`` is truthy and pre-flight
        would wrongly demand a reason."""
        resp = self._propose_tester(
            {
                "tests_execution_blocked": "false",
                "tests_run": 5,
                "checks_passed": ["test"],
            }
        )
        assert resp["ok"] is True

    def test_string_true_tests_execution_blocked_requires_reason(self):
        """Symmetric to the previous: string ``"true"`` is coerced to
        ``True``, so pre-flight enters the blocked branch and demands
        a reason — same verdict the orchestrator would reach."""
        with pytest.raises(HandlerError, match="tests_execution_blocked_reason"):
            self._propose_tester({"tests_execution_blocked": "true"})

    def test_unparseable_tests_execution_blocked_rejected(self):
        """Values neither bool-like nor recognized strings (e.g. a
        list) are rejected pre-flight with a coercion error. Pydantic
        would also reject these — pre-flight just surfaces the message
        before the request hits the wire."""
        with pytest.raises(HandlerError, match="must be a bool"):
            self._propose_tester({"tests_execution_blocked": ["maybe"]})

    def test_non_string_checks_passed_items_rejected(self):
        """``checks_passed=[1, 2, 3]`` is the same #2338 footgun shape
        as the canonical empty-attestation case — the agent forgot to
        stringify check identifiers. Pydantic's ``list[str]`` field
        rejects this; pre-flight catches it first with a message that
        names the bad items."""
        with pytest.raises(HandlerError, match="must be strings"):
            self._propose_tester({"tests_run": 5, "checks_passed": [1, 2, 3]})

    # --- No-op propose path for refactor / doc-only slices (#2431) ----

    def test_no_test_changes_needed_with_reason_passes(self):
        """no_test_changes_needed=true + reason + checks_passed is
        accepted, even with tests_run=0. This is the no-op path that
        unblocks BRC consensus for refactor / doc-only slices."""
        resp = self._propose_tester(
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": (
                    "slice-3 is a pure decomposition: symbol moves between "
                    "submodules, no behavior change; existing test suite covers."
                ),
                "checks_passed": ["lint", "test"],
            }
        )
        assert resp["ok"] is True

    def test_no_test_changes_needed_without_reason_rejected(self):
        with pytest.raises(HandlerError, match="no_test_changes_reason"):
            self._propose_tester(
                {
                    "no_test_changes_needed": True,
                    "checks_passed": ["lint", "test"],
                }
            )

    def test_no_test_changes_needed_without_checks_passed_rejected(self):
        """The no-op path still requires checks_passed — the tester
        must have actually run the configured checks against the diff."""
        with pytest.raises(HandlerError, match="checks_passed"):
            self._propose_tester(
                {
                    "no_test_changes_needed": True,
                    "no_test_changes_reason": "pure refactor; existing tests cover",
                }
            )

    def test_no_test_changes_needed_blank_reason_rejected(self):
        """Whitespace-only reason is treated as missing."""
        with pytest.raises(HandlerError, match="no_test_changes_reason"):
            self._propose_tester(
                {
                    "no_test_changes_needed": True,
                    "no_test_changes_reason": "   ",
                    "checks_passed": ["lint", "test"],
                }
            )

    def test_no_test_changes_and_blocked_mutually_exclusive(self):
        with pytest.raises(HandlerError, match="mutually exclusive"):
            self._propose_tester(
                {
                    "tests_execution_blocked": True,
                    "tests_execution_blocked_reason": "private network blocks deps",
                    "no_test_changes_needed": True,
                    "no_test_changes_reason": "pure refactor",
                    "checks_passed": ["lint"],
                }
            )

    def test_no_test_changes_with_tests_run_is_allowed(self):
        """The no-op path lets a tester report running existing tests
        (tests_run > 0) while still flagging no new tests authored.
        Useful when the tester ran `make test` against the coder's
        diff to verify the refactor preserved behavior."""
        resp = self._propose_tester(
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": "decomposition only; no behavior change",
                "tests_run": 1432,
                "checks_passed": ["lint", "test"],
            }
        )
        assert resp["ok"] is True


class TestPreFlightMirrorsOrchestrator:
    """Cross-check: pre-flight and orchestrator's strict validator agree.

    The pre-flight docstring claims it "mirrors the orchestrator's
    strict-mode tester checks." This class enforces that as an
    invariant rather than a docstring claim — every payload runs
    through both validators, and the verdicts (accept / reject) must
    match. If someone tightens one side and not the other, one of these
    parametrized cases fails. See PR #2344 review feedback.
    """

    @staticmethod
    def _orchestrator_verdict(attestation: dict) -> bool:
        """Return True if the orchestrator's strict validator would
        accept the payload, False if it raises. Catches ``ValueError``
        — pydantic.ValidationError inherits from ValueError in v2, and
        ``_validate_strict`` raises ``ValueError`` directly."""
        from attestation_schemas import AttestationStrictness, validate_attestation

        try:
            validate_attestation(
                "tester",
                attestation,
                AttestationStrictness.STRICT,
                is_producer=True,
            )
        except ValueError:
            return False
        return True

    @staticmethod
    def _pre_flight_verdict(attestation: dict) -> bool:
        """Return True if pre-flight accepts (passes silently), False
        if it raises ``HandlerError``."""
        try:
            brc._validate_tester_attestation_pre_flight(attestation)
        except HandlerError:
            return False
        return True

    @pytest.mark.parametrize(
        "attestation",
        [
            # Empty payload — both reject.
            {},
            # Missing checks_passed — both reject.
            {"tests_run": 5},
            # Missing tests_run — both reject.
            {"checks_passed": ["test"]},
            # Zero tests_run — both reject.
            {"tests_run": 0, "checks_passed": ["test"]},
            # Empty checks_passed list — both reject.
            {"tests_run": 5, "checks_passed": []},
            # Blocked without reason — both reject.
            {"tests_execution_blocked": True},
            # Blocked + tests_run > 0 — both reject (mutual exclusion).
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "x",
                "tests_run": 5,
            },
            # Happy path — both accept.
            {"tests_run": 42, "checks_passed": ["lint", "test"]},
            # Blocked with reason — both accept.
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "private-network mode",
            },
            # Negative tests_run — both accept (Pydantic has no constraint,
            # _validate_strict only rejects == 0; pre-flight mirrors).
            {"tests_run": -3, "checks_passed": ["test"]},
            # String "false" for tests_execution_blocked — both accept.
            {
                "tests_execution_blocked": "false",
                "tests_run": 1,
                "checks_passed": ["test"],
            },
            # String "true" without reason — both reject.
            {"tests_execution_blocked": "true"},
            # --- Pydantic parse-step type checks (PR #2344 re-review) ---
            # Bad tests_run type (unparseable string) — Pydantic rejects in
            # both modes; pre-flight now mirrors via _coerce_attestation_int.
            {"tests_run": "abc", "checks_passed": ["test"]},
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "x",
                "tests_run": "abc",
            },
            # Bad tests_run type (list) — same.
            {"tests_run": ["t1", "t2"], "checks_passed": ["test"]},
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "x",
                "tests_run": ["t1", "t2"],
            },
            # Non-integer-valued float — Pydantic rejects (only int-like
            # floats coerce to int); pre-flight mirrors.
            {"tests_run": 0.5, "checks_passed": ["test"]},
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "x",
                "tests_run": 0.5,
            },
            # Integer-valued float — Pydantic accepts (2.0 → 2); pre-flight
            # mirrors via float.is_integer() in the coercion helper.
            {"tests_run": 2.0, "checks_passed": ["test"]},
            # Parseable integer string — Pydantic accepts; pre-flight mirrors.
            {"tests_run": "42", "checks_passed": ["test"]},
            # Non-list checks_passed — Pydantic rejects (list[str] field) in
            # both blocked and non-blocked modes; pre-flight mirrors with
            # an unconditional isinstance(..., list) check above the
            # branch split.
            {"tests_run": 5, "checks_passed": "lint"},
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "x",
                "checks_passed": "lint",
            },
            # Non-string items in checks_passed — Pydantic's list[str] field
            # rejects this (the operationally-relevant #2338 footgun shape
            # flagged in the second re-review). Pre-flight now mirrors with
            # an explicit item-type check.
            {"tests_run": 5, "checks_passed": [1, 2, 3]},
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "x",
                "checks_passed": [1, 2, 3],
            },
            # Mixed string + non-string items — pre-flight should still
            # reject (Pydantic does too).
            {"tests_run": 5, "checks_passed": ["lint", 42, "test"]},
            # --- No-op propose path (#2431) ---------------------------
            # Happy path: no_test_changes_needed=true + reason +
            # checks_passed, tests_run can be 0.
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": "pure refactor: symbol moves only",
                "checks_passed": ["lint", "test"],
            },
            # Same with tests_run > 0 (tester ran existing suite) — both accept.
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": "decomposition; existing tests cover",
                "tests_run": 142,
                "checks_passed": ["lint", "test"],
            },
            # Missing reason — both reject.
            {
                "no_test_changes_needed": True,
                "checks_passed": ["lint", "test"],
            },
            # Whitespace-only reason — both reject.
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": "   ",
                "checks_passed": ["lint", "test"],
            },
            # Missing checks_passed — both reject (still required on no-op path).
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": "pure refactor",
            },
            # Empty checks_passed — both reject.
            {
                "no_test_changes_needed": True,
                "no_test_changes_reason": "pure refactor",
                "checks_passed": [],
            },
            # Mutual exclusion with tests_execution_blocked — both reject.
            {
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "private net blocks deps",
                "no_test_changes_needed": True,
                "no_test_changes_reason": "pure refactor",
                "checks_passed": ["lint"],
            },
            # String "true" coercion — both accept the no-op happy shape.
            {
                "no_test_changes_needed": "true",
                "no_test_changes_reason": "pure refactor",
                "checks_passed": ["lint", "test"],
            },
            # String "false" — falls through to normal-path validation,
            # which then rejects on missing tests_run.
            {
                "no_test_changes_needed": "false",
                "checks_passed": ["lint", "test"],
            },
        ],
    )
    def test_pre_flight_matches_orchestrator(self, attestation: dict):
        pre_flight = self._pre_flight_verdict(attestation)
        orchestrator = self._orchestrator_verdict(attestation)
        assert pre_flight == orchestrator, (
            f"Verdict divergence for {attestation!r}: "
            f"pre-flight={'accept' if pre_flight else 'reject'}, "
            f"orchestrator={'accept' if orchestrator else 'reject'}. "
            "Pre-flight must mirror the orchestrator's strict validator."
        )


class TestBrcAck:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value=_ok_response(),
        ) as req:
            resp = brc.brc_ack(
                {
                    "pipeline_id": "p",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "x" * 60,
                    "files_reviewed": ["a.py"],
                    "ack_version": 1,
                }
            )
        assert resp["ok"] is True
        assert resp["producer_role"] == "coder"
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_ack"
        assert data["payload"]["reason"] == "x" * 60
        assert data["payload"]["artifact_references"] == ["a.py"]

    def test_missing_producer_role(self):
        with pytest.raises(HandlerError):
            brc.brc_ack({"pipeline_id": "p", "role": "r", "reason": "y"})

    def test_missing_reason(self):
        with pytest.raises(HandlerError):
            brc.brc_ack({"pipeline_id": "p", "role": "r", "producer_role": "coder"})

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_ack(
                    {
                        "pipeline_id": "p",
                        "role": "r",
                        "producer_role": "coder",
                        "reason": "y",
                        "ack_version": 1,
                    }
                )


class TestBrcNack:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value=_ok_response(),
        ) as req:
            resp = brc.brc_nack(
                {
                    "pipeline_id": "p",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "blocking",
                    "nack_version": 1,
                }
            )
        assert resp["ok"] is True
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_nack"

    def test_missing_reason(self):
        with pytest.raises(HandlerError):
            brc.brc_nack({"pipeline_id": "p", "role": "r", "producer_role": "coder"})

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_nack(
                    {
                        "pipeline_id": "p",
                        "role": "r",
                        "producer_role": "coder",
                        "reason": "why",
                        "nack_version": 1,
                    }
                )


class TestBrcConfirm:
    def test_happy_confirmed(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "success": True,
                "data": {"status": "confirmed", "consensus_reached": True},
            },
        ):
            resp = brc.brc_confirm({"pipeline_id": "p", "role": "coder"})
        assert resp["ok"] is True
        assert resp["status"] == "confirmed"
        assert resp["consensus_reached"] is True

    def test_pending_acks(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "success": True,
                "data": {"status": "pending_acks", "consensus_reached": False},
            },
        ):
            resp = brc.brc_confirm({"pipeline_id": "p", "role": "coder"})
        assert resp["ok"] is False
        assert resp["status"] == "pending_acks"
        assert resp["consensus_reached"] is False

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_confirm({"pipeline_id": "p", "role": "r"})


class TestBrcGetState:
    def test_default_shape(self):
        payload = {
            "data": {
                "concurrent": {
                    "consensus": {
                        "is_complete": False,
                        "blocking_agents": ["coder"],
                        "agents": {},
                    }
                }
            }
        }
        with patch("egg_agent_tools.handlers.brc.orchestrator_request", return_value=payload):
            resp = brc.brc_get_state({"pipeline_id": "p"})
        assert resp["ok"] is True
        assert resp["is_complete"] is False
        assert resp["blocking_agents"] == ["coder"]
        assert "raw" not in resp

    def test_verbose_includes_raw(self):
        payload = {
            "data": {"concurrent": {"consensus": {"is_complete": True, "blocking_agents": []}}}
        }
        with patch("egg_agent_tools.handlers.brc.orchestrator_request", return_value=payload):
            resp = brc.brc_get_state({"pipeline_id": "p", "verbose": True})
        assert resp["raw"] == payload["data"]


class TestBrcListBlocking:
    def test_returns_blocking_list(self):
        payload = {"data": {"concurrent": {"consensus": {"blocking_agents": ["coder", "tester"]}}}}
        with patch("egg_agent_tools.handlers.brc.orchestrator_request", return_value=payload):
            resp = brc.brc_list_blocking({"pipeline_id": "p"})
        assert resp["blocking_agents"] == ["coder", "tester"]

    def test_empty_when_missing(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {}},
        ):
            resp = brc.brc_list_blocking({"pipeline_id": "p"})
        assert resp["blocking_agents"] == []

    def test_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("server error", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_list_blocking({"pipeline_id": "p"})


# ---------------------------------------------------------------------------
# Iter-2 (#1917): brc_read_peer_artifact — local brc-history with pagination
# ---------------------------------------------------------------------------

import base64  # noqa: E402
import json  # noqa: E402
from unittest.mock import patch  # noqa: E402,F811


def _make_history_file(root, identifier: str, phase: str, records: list[dict]):
    """Write a brc-history file mirroring _write_brc_history's format."""
    dir_ = root / ".egg-state" / "brc-history"
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{identifier}-{phase}.json"
    path.write_text(json.dumps(records))
    return path


def _records(*specs):
    """Build minimal BRC records (from_role, message_type)."""
    return [
        {
            "id": f"id-{i}",
            "from_role": role,
            "message_type": mt,
            "body": f"b-{i}",
            "timestamp": f"2026-04-24T00:00:{i:02d}Z",
        }
        for i, (role, mt) in enumerate(specs, start=1)
    ]


class TestBrcReadPeerArtifact:
    def _set_env(self, monkeypatch, tmp_path, identifier="1917"):
        monkeypatch.setenv("EGG_ISSUE_NUMBER", identifier)
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        # Ensure pipeline id doesn't shadow issue number.
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)

    def test_happy_path_returns_records(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(
            tmp_path,
            "1917",
            "plan",
            _records(("coder", "CONSENSUS_PROPOSE"), ("reviewer_code", "CONSENSUS_ACK")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "plan"})
        assert resp["ok"] is True
        assert len(resp["items"]) == 2
        assert resp["total_available"] == 2
        assert resp["next_cursor"] is None
        # Security (reviewer_code NACK #1): ``path`` is NOT echoed —
        # information-disclosure hardening.
        assert "path" not in resp
        assert resp["skipped_malformed"] == 0

    def test_missing_history_file_returns_empty(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        resp = brc.brc_read_peer_artifact({"phase": "plan"})
        assert resp["items"] == []
        assert resp["total_available"] == 0
        assert resp["next_cursor"] is None
        # Again: no ``path`` echo in the empty-result branch.
        assert "path" not in resp

    def test_missing_phase_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({})

    def test_invalid_phase_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "bogus"})

    def test_filter_by_peer_role(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(
            tmp_path,
            "1917",
            "plan",
            _records(
                ("coder", "CONSENSUS_PROPOSE"),
                ("reviewer_code", "CONSENSUS_ACK"),
                ("coder", "CONSENSUS_PROPOSE"),
            ),
        )
        resp = brc.brc_read_peer_artifact({"phase": "plan", "peer_role": "coder"})
        assert len(resp["items"]) == 2
        assert {r["from_role"] for r in resp["items"]} == {"coder"}

    def test_producer_role_alias(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(
            tmp_path,
            "1917",
            "plan",
            _records(
                ("coder", "CONSENSUS_PROPOSE"),
                ("reviewer_code", "CONSENSUS_ACK"),
            ),
        )
        # Using the alias keyword should behave like peer_role.
        resp = brc.brc_read_peer_artifact({"phase": "plan", "producer_role": "reviewer_code"})
        assert len(resp["items"]) == 1
        assert resp["items"][0]["from_role"] == "reviewer_code"

    def test_filter_by_message_type_string(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(
            tmp_path,
            "1917",
            "plan",
            _records(
                ("coder", "CONSENSUS_PROPOSE"),
                ("reviewer_code", "CONSENSUS_ACK"),
                ("coder", "CONSENSUS_NACK"),
            ),
        )
        resp = brc.brc_read_peer_artifact({"phase": "plan", "message_type": "CONSENSUS_ACK"})
        assert [r["message_type"] for r in resp["items"]] == ["CONSENSUS_ACK"]

    def test_filter_by_message_type_list(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(
            tmp_path,
            "1917",
            "plan",
            _records(
                ("coder", "CONSENSUS_PROPOSE"),
                ("reviewer_code", "CONSENSUS_ACK"),
                ("coder", "CONSENSUS_NACK"),
            ),
        )
        resp = brc.brc_read_peer_artifact(
            {
                "phase": "plan",
                "message_type": ["CONSENSUS_ACK", "CONSENSUS_NACK"],
            }
        )
        assert len(resp["items"]) == 2

    def test_unknown_message_type_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "message_type": "CONSENSUS_OOPS"})

    def test_invalid_message_type_shape_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "message_type": 42})

    def test_pagination_exact_limit(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        records = _records(*[("coder", "CONSENSUS_PROPOSE")] * 50)
        _make_history_file(tmp_path, "1917", "plan", records)
        resp = brc.brc_read_peer_artifact({"phase": "plan", "limit": 50})
        assert len(resp["items"]) == 50
        # Exact-limit page: next_cursor is None because no more rows.
        assert resp["next_cursor"] is None

    def test_pagination_beyond_limit(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        records = _records(*[("coder", "CONSENSUS_PROPOSE")] * 120)
        _make_history_file(tmp_path, "1917", "plan", records)
        resp = brc.brc_read_peer_artifact({"phase": "plan", "limit": 50})
        assert len(resp["items"]) == 50
        assert resp["next_cursor"] is not None
        # Second page.
        resp2 = brc.brc_read_peer_artifact(
            {"phase": "plan", "limit": 50, "cursor": resp["next_cursor"]}
        )
        assert len(resp2["items"]) == 50
        # Third (partial) page.
        resp3 = brc.brc_read_peer_artifact(
            {"phase": "plan", "limit": 50, "cursor": resp2["next_cursor"]}
        )
        assert len(resp3["items"]) == 20
        assert resp3["next_cursor"] is None

    def test_pagination_offset_beyond_total_returns_empty(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        records = _records(*[("coder", "CONSENSUS_PROPOSE")] * 3)
        _make_history_file(tmp_path, "1917", "plan", records)
        far = base64.urlsafe_b64encode(b'{"offset": 999}').decode().rstrip("=")
        resp = brc.brc_read_peer_artifact({"phase": "plan", "cursor": far})
        assert resp["items"] == []
        assert resp["next_cursor"] is None

    def test_bad_cursor_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(tmp_path, "1917", "plan", _records(("coder", "CONSENSUS_PROPOSE")))
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "cursor": "$$not-b64$$"})

    def test_negative_cursor_offset_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        _make_history_file(tmp_path, "1917", "plan", _records(("coder", "CONSENSUS_PROPOSE")))
        neg = base64.urlsafe_b64encode(b'{"offset": -1}').decode().rstrip("=")
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "cursor": neg})

    def test_non_string_cursor_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "cursor": 42})

    def test_limit_zero_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "limit": 0})

    def test_limit_cap_enforced(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "limit": 10_000})

    def test_non_integer_limit_rejected(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "limit": "ten"})

    def test_corrupt_records_skipped(self, tmp_path, monkeypatch):
        """Non-dict array entries are skipped silently — the handler
        treats only mapping objects as BRC records."""
        self._set_env(monkeypatch, tmp_path)
        dir_ = tmp_path / ".egg-state" / "brc-history"
        dir_.mkdir(parents=True, exist_ok=True)
        (dir_ / "1917-plan.json").write_text(
            json.dumps(
                [
                    "not-an-object",
                    {"from_role": "coder", "message_type": "CONSENSUS_PROPOSE"},
                    42,
                ]
            )
        )
        resp = brc.brc_read_peer_artifact({"phase": "plan"})
        # Only the one dict survives; non-dict entries are ignored.
        assert len(resp["items"]) == 1
        assert resp["items"][0]["from_role"] == "coder"

    def test_malformed_json_raises_handler_error(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        dir_ = tmp_path / ".egg-state" / "brc-history"
        dir_.mkdir(parents=True, exist_ok=True)
        (dir_ / "1917-plan.json").write_text("not-json")
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan"})

    def test_non_array_json_raises_handler_error(self, tmp_path, monkeypatch):
        self._set_env(monkeypatch, tmp_path)
        dir_ = tmp_path / ".egg-state" / "brc-history"
        dir_.mkdir(parents=True, exist_ok=True)
        (dir_ / "1917-plan.json").write_text('{"not": "an array"}')
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan"})

    def test_caller_issue_override_is_ignored(self, tmp_path, monkeypatch):
        """Security (reviewer_code NACK #1a + risk_analyst R2): a caller
        cannot override the env-resolved identifier to read another
        pipeline's history.  The override must be silently ignored;
        the handler resolves strictly from the env."""
        self._set_env(monkeypatch, tmp_path, identifier="1917")
        # History files for two pipelines; the caller tries to read
        # 1911's but the env-bound handler only ever looks at 1917.
        _make_history_file(tmp_path, "1911", "implement", _records(("coder", "CONSENSUS_PROPOSE")))
        _make_history_file(
            tmp_path,
            "1917",
            "implement",
            _records(("coder", "CONSENSUS_ACK")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "implement", "issue": 1911})
        assert resp["total_available"] == 1
        # The returned record comes from the 1917 file (env identifier),
        # not 1911 (caller override).
        assert resp["items"][0]["message_type"] == "CONSENSUS_ACK"

    def test_invalid_peer_role_rejected(self, tmp_path, monkeypatch):
        """reviewer_code NACK #1: peer_role must match [a-z0-9_-] — any
        special characters (path-traversal style, shell metacharacters)
        are rejected before filename construction."""
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError) as exc:
            brc.brc_read_peer_artifact({"phase": "plan", "peer_role": "../etc/passwd"})
        assert "peer_role" in str(exc.value).lower()

    def test_skipped_malformed_tracked_in_response(self, tmp_path, monkeypatch):
        """Non-dict entries are counted in ``skipped_malformed`` so
        paginated reads remain deterministic (reviewer_code NACK #1b)."""
        self._set_env(monkeypatch, tmp_path)
        dir_ = tmp_path / ".egg-state" / "brc-history"
        dir_.mkdir(parents=True, exist_ok=True)
        (dir_ / "1917-plan.json").write_text(
            json.dumps(
                [
                    "not-an-object",
                    {"from_role": "coder", "message_type": "CONSENSUS_PROPOSE"},
                    42,
                    None,
                ]
            )
        )
        resp = brc.brc_read_peer_artifact({"phase": "plan"})
        assert len(resp["items"]) == 1
        # Three non-dict entries were skipped.
        assert resp["skipped_malformed"] == 3

    def test_docstring_mentions_no_cli_rationale(self):
        """decision-13 — brc_read_peer_artifact is cli_command=None so its
        handler docstring must explain why."""
        doc = brc.brc_read_peer_artifact.__doc__ or ""
        lower = doc.lower()
        assert "no cli" in lower or "no-cli" in lower

    # ---- Slice-aware implement-phase reads (#2548 follow-up) -----------

    def _make_slice_history_file(self, root, identifier: str, slice_id: str, records: list[dict]):
        """Write a per-slice implement-phase brc-history file."""
        dir_ = root / ".egg-state" / "brc-history"
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{identifier}-implement-{slice_id}.json"
        path.write_text(json.dumps(records))
        return path

    def _make_unattributed_history_file(self, root, identifier: str, records: list[dict]):
        """Write the cross-cutting `unattributed` sibling file."""
        dir_ = root / ".egg-state" / "brc-history"
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{identifier}-implement-unattributed.json"
        path.write_text(json.dumps(records))
        return path

    def test_slice_scoped_implement_reads_per_slice_file(self, tmp_path, monkeypatch):
        """When EGG_SLICE_ID is set and phase=='implement', the handler
        reads {identifier}-implement-{slice_id}.json — not the legacy
        aggregate file (which the writer no longer produces in slice mode)."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        # Slice-1 has its own transcript; no aggregate file exists.
        self._make_slice_history_file(
            tmp_path,
            "1917",
            "slice-1",
            _records(("coder", "CONSENSUS_PROPOSE"), ("reviewer_code", "CONSENSUS_ACK")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "implement"})
        assert resp["ok"] is True
        assert len(resp["items"]) == 2
        assert resp["total_available"] == 2

    def test_slice_scoped_implement_no_aggregate_fallback(self, tmp_path, monkeypatch):
        """A slice-scoped agent must NOT silently dead-end into the
        aggregate file even if a stale {identifier}-implement.json
        happens to be on disk — the slice file is the canonical path."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-2")
        # Aggregate file from a previous run (or another tool) — must
        # be ignored when slice-scoped.
        _make_history_file(
            tmp_path,
            "1917",
            "implement",
            _records(("coder", "CONSENSUS_PROPOSE")),
        )
        # Slice-2's per-slice file does not exist.
        resp = brc.brc_read_peer_artifact({"phase": "implement"})
        assert resp["items"] == []
        assert resp["total_available"] == 0

    def test_slice_scoped_implement_merges_unattributed_sibling(self, tmp_path, monkeypatch):
        """By default the slice transcript is merged with the cross-
        cutting `unattributed` sibling so reviewers see OVERSEER_ALERT,
        AGENT_FAILED, etc. interleaved with their slice's CONSENSUS_*."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        # Slice-1 records and unattributed records have distinct
        # timestamps so we can assert chronological interleave.
        slice_recs = [
            {
                "id": "s1",
                "from_role": "coder",
                "message_type": "CONSENSUS_PROPOSE",
                "body": "b1",
                "timestamp": "2026-04-24T00:00:01Z",
            },
            {
                "id": "s2",
                "from_role": "reviewer_code",
                "message_type": "CONSENSUS_ACK",
                "body": "b2",
                "timestamp": "2026-04-24T00:00:03Z",
            },
        ]
        unattr_recs = [
            {
                "id": "u1",
                "from_role": "overseer",
                "message_type": "OVERSEER_ALERT",
                "body": "u1",
                "timestamp": "2026-04-24T00:00:02Z",
            },
        ]
        self._make_slice_history_file(tmp_path, "1917", "slice-1", slice_recs)
        self._make_unattributed_history_file(tmp_path, "1917", unattr_recs)
        resp = brc.brc_read_peer_artifact({"phase": "implement"})
        assert len(resp["items"]) == 3
        # Re-sorted by timestamp: s1 → u1 → s2.
        assert [r["id"] for r in resp["items"]] == ["s1", "u1", "s2"]

    def test_slice_scoped_implement_skip_unattributed_on_request(self, tmp_path, monkeypatch):
        """``include_unattributed=False`` reads only the per-slice file."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        self._make_slice_history_file(
            tmp_path,
            "1917",
            "slice-1",
            _records(("coder", "CONSENSUS_PROPOSE")),
        )
        self._make_unattributed_history_file(
            tmp_path,
            "1917",
            _records(("overseer", "OVERSEER_ALERT")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "implement", "include_unattributed": False})
        assert len(resp["items"]) == 1
        assert resp["items"][0]["from_role"] == "coder"

    def test_slice_scoped_implement_unattributed_only_present(self, tmp_path, monkeypatch):
        """If a slice never produced any CONSENSUS_* but unattributed
        traffic exists, the handler still returns the unattributed
        records rather than an empty response."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        self._make_unattributed_history_file(
            tmp_path,
            "1917",
            _records(("overseer", "OVERSEER_ALERT")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "implement"})
        assert len(resp["items"]) == 1
        assert resp["items"][0]["from_role"] == "overseer"

    def test_pipeline_level_implement_reads_aggregate(self, tmp_path, monkeypatch):
        """Without EGG_SLICE_ID the handler reads the aggregate
        ``{identifier}-implement.json`` file (babysit_pr / non-slice
        runs are unaffected by the slice-aware switch)."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        _make_history_file(
            tmp_path,
            "1917",
            "implement",
            _records(("coder", "CONSENSUS_PROPOSE")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "implement"})
        assert len(resp["items"]) == 1

    def test_invalid_slice_id_env_rejected(self, tmp_path, monkeypatch):
        """Defense-in-depth: a malformed EGG_SLICE_ID must not be
        interpolated into the filename. Same regex the writer enforces
        at the orchestrator seam."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "../etc/passwd")
        with pytest.raises(HandlerError) as exc:
            brc.brc_read_peer_artifact({"phase": "implement"})
        assert "slice" in str(exc.value).lower()

    def test_invalid_include_unattributed_rejected(self, tmp_path, monkeypatch):
        """``include_unattributed`` must be a bool when supplied."""
        self._set_env(monkeypatch, tmp_path)
        with pytest.raises(HandlerError):
            brc.brc_read_peer_artifact({"phase": "plan", "include_unattributed": "yes"})

    def test_slice_scoped_non_implement_reads_aggregate(self, tmp_path, monkeypatch):
        """EGG_SLICE_ID only switches the implement phase. Refine/plan/pr
        always read the aggregate file — slice-aware writers never
        partition those phases."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        _make_history_file(
            tmp_path,
            "1917",
            "plan",
            _records(("coder", "CONSENSUS_PROPOSE")),
        )
        resp = brc.brc_read_peer_artifact({"phase": "plan"})
        assert len(resp["items"]) == 1

    def test_filter_by_message_type_overseer_alert_in_unattributed(self, tmp_path, monkeypatch):
        """Reviewers can scan cross-cutting alerts in their slice's
        transcript by filtering on a non-CONSENSUS_* type. Regression
        guard: the handler's ``_BRC_HISTORY_TYPES`` whitelist must include
        the same non-CONSENSUS_* types the writer emits to the
        ``unattributed`` sibling, otherwise this raises ``Unknown
        message_type(s)`` even though matching records exist on disk."""
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("EGG_SLICE_ID", "slice-1")
        self._make_slice_history_file(
            tmp_path,
            "1917",
            "slice-1",
            _records(("coder", "CONSENSUS_PROPOSE")),
        )
        self._make_unattributed_history_file(
            tmp_path,
            "1917",
            _records(
                ("overseer", "OVERSEER_ALERT"),
                ("system", "HEARTBEAT"),
            ),
        )
        resp = brc.brc_read_peer_artifact({"phase": "implement", "message_type": "OVERSEER_ALERT"})
        assert len(resp["items"]) == 1
        assert resp["items"][0]["message_type"] == "OVERSEER_ALERT"
        assert resp["items"][0]["from_role"] == "overseer"


class TestBrcPipelineIdValidation:
    """Pipeline IDs are interpolated into URL paths — format validation
    prevents path traversal.  Mirrors TestPipelineIdValidation in
    test_handlers_progress.py."""

    def test_traversal_pipeline_id_rejected(self):
        with pytest.raises(HandlerError, match="Invalid pipeline_id"):
            brc.brc_propose(
                {
                    "pipeline_id": "../other",
                    "role": "coder",
                    "summary": "x" * 60,
                }
            )

    def test_pipeline_id_with_slashes_rejected(self):
        with pytest.raises(HandlerError, match="Invalid pipeline_id"):
            brc.brc_ack(
                {
                    "pipeline_id": "a/b/c",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "x" * 60,
                }
            )

    def test_valid_pipeline_id_passes_validation(self):
        """Sanity check: a well-formed ID must not be rejected by the
        format regex."""
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value=_ok_response(),
            ),
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="a" * 40),
        ):
            resp = brc.brc_propose(
                {
                    "pipeline_id": "issue-1917",
                    "role": "coder",
                    "summary": "x" * 60,
                }
            )
        assert resp["ok"] is True


class TestBrcResolveObligation:
    """Handler smoke tests for ``mcp__brc__resolve_obligation`` (#2338).

    The handler is a thin wrapper around the orchestrator's signal endpoint
    — these tests cover request shaping, validation, and response shape.
    """

    def test_happy_path_threads_args_into_signal(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"status": "resolved"}},
        ) as req:
            resp = brc.brc_resolve_obligation(
                {
                    "pipeline_id": "pipe-1",
                    "role": "tester",
                    "reviewer_role": "reviewer_contract",
                    "producer_role": "coder",
                    "commit_sha": "abc1234",
                    "note": "cherry-picked",
                }
            )
        assert resp["ok"] is True
        assert resp["role"] == "tester"
        assert resp["reviewer_role"] == "reviewer_contract"
        assert resp["producer_role"] == "coder"
        assert req.call_count == 1
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_resolve_obligation"
        assert data["agent_role"] == "tester"
        assert data["reviewer_role"] == "reviewer_contract"
        assert data["producer_role"] == "coder"
        assert data["commit_sha"] == "abc1234"
        assert data["note"] == "cherry-picked"

    def test_omits_optional_fields_when_blank(self):
        """commit_sha and note are not threaded through when blank — keeps
        the wire payload tidy and lets the handler default to its own
        fallback values."""
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": True, "data": {"status": "resolved"}},
        ) as req:
            brc.brc_resolve_obligation(
                {
                    "pipeline_id": "pipe-1",
                    "role": "tester",
                    "reviewer_role": "reviewer_contract",
                    "producer_role": "coder",
                }
            )
        data = req.call_args.kwargs["data"]
        assert "commit_sha" not in data
        assert "note" not in data

    def test_missing_reviewer_role(self):
        with pytest.raises(HandlerError, match="reviewer_role"):
            brc.brc_resolve_obligation(
                {
                    "pipeline_id": "pipe-1",
                    "role": "tester",
                    "producer_role": "coder",
                }
            )

    def test_missing_producer_role(self):
        with pytest.raises(HandlerError, match="producer_role"):
            brc.brc_resolve_obligation(
                {
                    "pipeline_id": "pipe-1",
                    "role": "tester",
                    "reviewer_role": "reviewer_contract",
                }
            )

    def test_invalid_commit_sha_rejected(self):
        with pytest.raises(HandlerError, match="Invalid commit SHA"):
            brc.brc_resolve_obligation(
                {
                    "pipeline_id": "pipe-1",
                    "role": "tester",
                    "reviewer_role": "reviewer_contract",
                    "producer_role": "coder",
                    "commit_sha": "not-a-sha",
                }
            )

    def test_orchestrator_failure_surfaces(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"success": False, "message": "no tracker"},
        ):
            with pytest.raises(GatewayError, match="no tracker"):
                brc.brc_resolve_obligation(
                    {
                        "pipeline_id": "pipe-1",
                        "role": "tester",
                        "reviewer_role": "reviewer_contract",
                        "producer_role": "coder",
                    }
                )


class TestBrcHistoryTypesDriftGuard:
    """Regression guard locking writer/reader symmetry for the *full* BRC
    history type set.

    The writer (``orchestrator.routes.pipelines.BRC_HISTORY_TYPES``) and
    the reader-side filter whitelist
    (``egg_agent_tools.handlers.brc._BRC_HISTORY_TYPES``) must list the
    same types: any type the writer emits must be filterable by the
    reader, and any type the reader accepts must be one the writer
    actually produces. The single-type regression test added in #2548
    locks ``OVERSEER_ALERT`` only — this test parses the writer-side
    literal out of the orchestrator source and asserts membership
    equality, so future drift on either side surfaces as a test failure.

    The handler module deliberately does *not* import the orchestrator
    package (which pulls fastapi); the regex-extraction approach
    preserves that boundary while still locking the contract.
    """

    def test_handler_whitelist_matches_writer_set(self):
        import re

        pipelines_path = ROOT / "orchestrator" / "routes" / "pipelines.py"
        source = pipelines_path.read_text()
        match = re.search(
            r"^BRC_HISTORY_TYPES\s*=\s*frozenset\s*\(\s*\{(?P<body>.*?)\}\s*\)",
            source,
            re.MULTILINE | re.DOTALL,
        )
        assert match is not None, (
            "Could not locate ``BRC_HISTORY_TYPES = frozenset({...})`` "
            f"literal in {pipelines_path}; the drift-guard regex needs "
            "updating to track the new shape."
        )
        writer_types = frozenset(re.findall(r'"([A-Z_]+)"', match.group("body")))
        assert writer_types, (
            "Parsed ``BRC_HISTORY_TYPES`` literal is empty — the regex "
            "did not capture any type names; check the literal shape."
        )
        handler_types = brc._BRC_HISTORY_TYPES
        assert handler_types == writer_types, (
            "Sandbox handler whitelist drifted from orchestrator writer "
            "set. "
            f"Handler-only (reader accepts but writer never emits): "
            f"{sorted(handler_types - writer_types)}; "
            f"Writer-only (writer emits but reader rejects): "
            f"{sorted(writer_types - handler_types)}. "
            "Update one or both to keep the partition symmetric."
        )
