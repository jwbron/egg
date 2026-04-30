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
