"""Tests for the event-driven ``egg-orch message wait`` family (issue #1897).

Covers the three new commands added in Phase 2:

- ``egg-orch message wait``      → event-driven blocking primitive.
- ``egg-orch message wait-loop`` → canonical stay-alive idiom.
- ``egg-orch message heartbeat`` → structured HEARTBEAT emitter.

Exit-code contract (from the plan):

    0 = matched, 1 = timeout, 2 = transient, 3 = permanent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Path setup for egg_lib import.
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402
from egg_lib.orch_cli import (  # noqa: E402
    cmd_message_heartbeat,
    cmd_message_wait,
    cmd_message_wait_loop,
    create_parser,
)

# Mock path for the handler HTTP helper used by the refactored cmd_message_*
# shims.  cmd_* now delegates to egg_agent_tools.handlers.message.* which in
# turn calls orchestrator_request from the handler gateway module.
_ORCH_MOCK_PATH = "egg_agent_tools.handlers.message.orchestrator_request"

# ---------------------------------------------------------------------------
# Parser: argparse surface
# ---------------------------------------------------------------------------


class TestWaitParser:
    """The ``message wait`` subparser accepts the documented flags."""

    def test_wait_requires_for(self):
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "wait", "issue-42"])

    def test_wait_accepts_multiple_for(self):
        parser = create_parser()
        args = parser.parse_args(
            [
                "message",
                "wait",
                "issue-42",
                "--for",
                "CONSENSUS_CONFIRMED",
                "--for",
                "CONSENSUS_RE_REVIEW",
                "--timeout",
                "30",
            ]
        )
        assert args.for_ == ["CONSENSUS_CONFIRMED", "CONSENSUS_RE_REVIEW"]
        assert args.timeout == 30

    def test_wait_accepts_from_filter(self):
        parser = create_parser()
        args = parser.parse_args(
            [
                "message",
                "wait",
                "issue-42",
                "--for",
                "HANDOFF",
                "--from",
                "coder",
                "--timeout",
                "5",
            ]
        )
        assert args.from_ == "coder"

    def test_wait_loop_accepts_max_iterations(self):
        parser = create_parser()
        args = parser.parse_args(
            [
                "message",
                "wait-loop",
                "issue-42",
                "--for",
                "CONSENSUS_CONFIRMED",
                "--max-iterations",
                "5",
            ]
        )
        assert args.max_iterations == 5

    def test_heartbeat_requires_state(self):
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "heartbeat", "issue-42"])

    def test_heartbeat_accepts_valid_states(self):
        parser = create_parser()
        for state in ("WORKING", "WAITING_ON_ROLE", "PROPOSED", "IDLE"):
            args = parser.parse_args(
                [
                    "message",
                    "heartbeat",
                    "issue-42",
                    "--state",
                    state,
                    "--waiting-on",
                    "reviewer_code" if state == "WAITING_ON_ROLE" else "",
                ]
            )
            assert args.state == state

    def test_heartbeat_rejects_invalid_state(self):
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "message",
                    "heartbeat",
                    "issue-42",
                    "--state",
                    "BOGUS",
                ]
            )


# ---------------------------------------------------------------------------
# cmd_message_wait: exit-code contract
# ---------------------------------------------------------------------------


def _make_wait_args(
    pipeline_id: str = "issue-42",
    for_: list[str] | None = None,
    timeout: int = 5,
    json_: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        pipeline_id=pipeline_id,
        for_=for_ or ["CONSENSUS_CONFIRMED"],
        role=None,
        from_=None,
        since=None,
        limit=None,
        timeout=timeout,
        json=json_,
    )


class TestWaitExitCodes:
    """Exit-code contract: 0 = matched, 1 = timeout, 2 = transient,
    3 = permanent."""

    def test_wait_exit_0_on_match(self):
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {
                    "messages": [
                        {
                            "timestamp": "2026-04-23T06:00:00",
                            "from_role": "coder",
                            "to_role": "all",
                            "message_type": "CONSENSUS_CONFIRMED",
                            "subject": "done",
                            "body": "",
                        }
                    ],
                    "matched": True,
                    "count": 1,
                },
            }
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 0

    def test_wait_exit_1_on_timeout(self):
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0},
            }
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 1

    def test_wait_exit_2_on_5xx(self):
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("internal error", status_code=500)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 2

    def test_wait_exit_2_on_connection_error(self):
        """Connection errors (no status_code) are transient."""
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("connection refused", status_code=None)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 2

    def test_wait_exit_3_on_4xx_non_408(self):
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("bad request", status_code=400)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 3

    def test_wait_exit_2_on_408_timeout(self):
        """408 is transient, not permanent."""
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("request timeout", status_code=408)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 2

    def test_wait_sends_for_params(self):
        """The request URL includes ``for=TYPE`` for each --for."""
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0},
            }
            cmd_message_wait(_make_wait_args(for_=["CONSENSUS_CONFIRMED", "OVERSEER_ALERT"]))
            endpoint = mock_req.call_args.args[0]
            assert "for=CONSENSUS_CONFIRMED" in endpoint
            assert "for=OVERSEER_ALERT" in endpoint

    def test_wait_includes_timeout_param(self):
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0},
            }
            cmd_message_wait(_make_wait_args(timeout=15))
            endpoint = mock_req.call_args.args[0]
            assert "timeout=15" in endpoint


# ---------------------------------------------------------------------------
# cmd_message_wait_loop
# ---------------------------------------------------------------------------


class TestWaitLoop:
    """``wait-loop`` retries until match, max-iterations, or permanent error.

    Loop semantics (retry on timeout / backoff on transient / propagate
    permanent / safety cap) are exercised in
    ``tests/sandbox/egg_agent_tools/test_handlers_message.py``.  These
    CLI-level tests focus on the rc mapping the shim performs around the
    handler.
    """

    def _make_loop_args(
        self,
        max_iter: int = 3,
        timeout: int = 1,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            pipeline_id="issue-42",
            for_=["CONSENSUS_CONFIRMED"],
            role=None,
            from_=None,
            since=None,
            limit=None,
            timeout=timeout,
            max_iterations=max_iter,
            json=False,
        )

    def test_exits_zero_on_match(self):
        """Handler returning matched=True → rc=0."""
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            return_value={
                "ok": True,
                "matched": True,
                "messages": [
                    {
                        "timestamp": "2026-04-23T06:00:00",
                        "from_role": "coder",
                        "to_role": "all",
                        "message_type": "CONSENSUS_CONFIRMED",
                        "subject": "done",
                        "body": "",
                    }
                ],
                "iterations": 1,
            },
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 0

    def test_exits_one_on_permanent_error(self):
        """Handler raising a permanent GatewayError collapses to rc=1 —
        the wait-loop wrapper owns the 0/1 outward contract per plan
        TASK-2-4 (callers who need 0/1/2/3 call ``message wait`` directly).
        """
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=GatewayError("forbidden", status_code=403),
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 1, "Handler permanent failure should map to outer rc=1 per plan TASK-2-4"

    def test_exits_one_on_handler_error(self):
        """Bad args from the user (HandlerError) also collapse to rc=1."""
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=HandlerError("missing for_types"),
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 1

    def test_safety_cap_without_match_returns_one(self):
        """If the handler's safety cap trips without a match, the shim
        returns rc=1 (no-match), preserving the legacy contract."""
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            return_value={
                "ok": True,
                "matched": False,
                "messages": [],
                "iterations": 3,
            },
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 1

    def test_wait_loop_accepts_none_max_iterations(self):
        """Plan TASK-2-4: None / non-positive ``--max-iterations`` must
        not crash the shim — the handler coerces it internally to a
        practically-unbounded cap.
        """
        args = self._make_loop_args()
        args.max_iterations = None

        for invalid in (None, 0, -1, -99):
            args.max_iterations = invalid
            with patch(
                "egg_agent_tools.handlers.message.message_wait_loop",
                return_value={"ok": True, "matched": True, "messages": []},
            ):
                rc = cmd_message_wait_loop(args)
            assert rc == 0, (
                f"max_iterations={invalid} should still attempt a wait; "
                "shim must forward non-positive caps without raising"
            )


# ---------------------------------------------------------------------------
# cmd_message_heartbeat
# ---------------------------------------------------------------------------


def _make_hb_args(
    state: str = "WORKING",
    waiting_on: str | None = None,
    since: str | None = None,
    body: str | None = None,
    role: str | None = "coder",
    pipeline_id: str = "issue-42",
    json_: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        pipeline_id=pipeline_id,
        role=role,
        state=state,
        waiting_on=waiting_on,
        since=since,
        body=body,
        json=json_,
    )


class TestHeartbeat:
    """``message heartbeat`` wraps message send with validation."""

    def test_heartbeat_working_sends_state_metadata(self):
        """Issue #1897: handler POSTs to the dedicated
        ``/api/v1/pipelines/{id}/heartbeat`` endpoint with a flat
        ``{from_role, state}`` body (NOT wrapped in metadata — the
        server unpacks into the stored message's metadata field).
        """
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {"success": True, "data": {"deduped": False}}
            rc = cmd_message_heartbeat(_make_hb_args(state="WORKING"))
        assert rc == 0
        call = mock_req.call_args
        # orchestrator_request(endpoint, method=..., data=...)
        path = call.args[0]
        posted = call.kwargs["data"]
        # Dedicated heartbeat route (plan TASK-3-2).
        assert path.endswith("/heartbeat"), f"Expected /heartbeat route, got {path!r}"
        # Flat payload shape.
        assert posted["state"] == "WORKING"
        assert posted["from_role"] == "coder"

    def test_heartbeat_waiting_on_role_requires_waiting_on(self):
        """WAITING_ON_ROLE without --waiting-on returns exit 3."""
        rc = cmd_message_heartbeat(_make_hb_args(state="WAITING_ON_ROLE", waiting_on=None))
        assert rc == 3

    def test_heartbeat_waiting_on_role_with_target(self):
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {"success": True, "data": {"deduped": False}}
            rc = cmd_message_heartbeat(
                _make_hb_args(
                    state="WAITING_ON_ROLE",
                    waiting_on="reviewer_code",
                )
            )
        assert rc == 0
        posted = mock_req.call_args.kwargs["data"]
        assert posted["state"] == "WAITING_ON_ROLE"
        assert posted["waiting_on"] == "reviewer_code"

    def test_heartbeat_rejects_invalid_state(self):
        """argparse already screens this in the CLI, but the handler also
        double-checks defensively — invalid state → exit 3."""
        rc = cmd_message_heartbeat(_make_hb_args(state="BOGUS"))
        assert rc == 3

    def test_heartbeat_missing_role_returns_3(self):
        with patch("egg_lib.orch_cli.get_agent_role_from_env", return_value=None):
            rc = cmd_message_heartbeat(_make_hb_args(role=None))
        assert rc == 3

    def test_heartbeat_client_rejects_4xx(self):
        """A 4xx response from the server (e.g., pydantic validation) should
        surface as exit 3."""
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("bad metadata", status_code=400)
            rc = cmd_message_heartbeat(_make_hb_args())
        assert rc == 3

    def test_heartbeat_client_retries_5xx(self):
        """5xx from server → exit 2 (transient)."""
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("server error", status_code=500)
            rc = cmd_message_heartbeat(_make_hb_args())
        assert rc == 2

    def test_heartbeat_since_flag_optional(self):
        """--since ISO-8601 / epoch string flows through to the flat
        payload — the server unpacks this into the stored message's
        metadata.since field.
        """
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {"success": True, "data": {"deduped": False}}
            cmd_message_heartbeat(_make_hb_args(since="1700000000"))
            posted = mock_req.call_args.kwargs["data"]
            assert posted["since"] == "1700000000"

    def test_heartbeat_rate_limit_429_returns_exit_3(self):
        """Plan TASK-3-4: 429 response triggers exit code 3 so wrappers
        treat it as a permanent failure and back off (rather than
        spin-retrying and hammering the server)."""
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError(
                "rate limit",
                status_code=429,
                details={"retry_after": 30},
            )
            rc = cmd_message_heartbeat(_make_hb_args(state="WORKING"))
        assert rc == 3
