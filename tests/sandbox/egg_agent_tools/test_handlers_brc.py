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
