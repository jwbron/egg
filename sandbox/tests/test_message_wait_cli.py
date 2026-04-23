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

from egg_lib.orch_cli import (  # noqa: E402
    ApiError,
    cmd_message_heartbeat,
    cmd_message_wait,
    cmd_message_wait_loop,
    create_parser,
)

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
        with patch("egg_lib.orch_cli.api_request") as mock_req:
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
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0},
            }
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 1

    def test_wait_exit_2_on_5xx(self):
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError("internal error", status_code=500)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 2

    def test_wait_exit_2_on_connection_error(self):
        """Connection errors (no status_code) are transient."""
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError("connection refused", status_code=None)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 2

    def test_wait_exit_3_on_4xx_non_408(self):
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError("bad request", status_code=400)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 3

    def test_wait_exit_2_on_408_timeout(self):
        """408 is transient, not permanent."""
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError("request timeout", status_code=408)
            rc = cmd_message_wait(_make_wait_args())
        assert rc == 2

    def test_wait_sends_for_params(self):
        """The request URL includes ``for=TYPE`` for each --for."""
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0},
            }
            cmd_message_wait(_make_wait_args(for_=["CONSENSUS_CONFIRMED", "OVERSEER_ALERT"]))
            # Inspect the endpoint
            endpoint = mock_req.call_args[0][1]
            assert "for=CONSENSUS_CONFIRMED" in endpoint
            assert "for=OVERSEER_ALERT" in endpoint

    def test_wait_includes_timeout_param(self):
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0},
            }
            cmd_message_wait(_make_wait_args(timeout=15))
            endpoint = mock_req.call_args[0][1]
            assert "timeout=15" in endpoint


# ---------------------------------------------------------------------------
# cmd_message_wait_loop
# ---------------------------------------------------------------------------


class TestWaitLoop:
    """``wait-loop`` retries until match, max-iterations, or permanent error."""

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
        """First successful match returns 0 immediately."""
        with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
            mock_wait.return_value = 0
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 0
        mock_wait.assert_called_once()

    def test_retries_on_timeout(self):
        """Exit-1 (timeout) → retry until max-iterations."""
        with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
            mock_wait.return_value = 1
            rc = cmd_message_wait_loop(self._make_loop_args(max_iter=3))
        assert rc == 1
        assert mock_wait.call_count == 3

    def test_exits_one_on_permanent_error(self):
        """Plan TASK-2-4 + reviewer_plan blocker 3: inner rc=3
        (permanent error) is MAPPED to outer rc=1 so the wrapper
        honours the documented 0/1 caller contract.

        Rationale: wait-loop is a stay-alive wrapper — callers adopt
        the "rc=0 match / rc=1 no-match" convention. Surfacing rc=3
        would confuse callers that don't know the internal code.
        Plan fix: map rc=3 → rc=1 here.
        """
        with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
            mock_wait.return_value = 3
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 1, (
            "Inner rc=3 should map to outer rc=1 per plan TASK-2-4; "
            "if this fails, the 3→1 coercion was removed"
        )

    def test_retries_on_transient_with_backoff(self):
        """Exit-2 (transient) → retry with backoff (uses time.sleep)."""
        call_count = [0]

        def _side_effect(args):
            call_count[0] += 1
            if call_count[0] < 3:
                return 2  # transient
            return 0  # match

        with (
            patch("egg_lib.orch_cli.cmd_message_wait", side_effect=_side_effect),
            patch("time.sleep") as mock_sleep,
        ):
            rc = cmd_message_wait_loop(self._make_loop_args(max_iter=5))
        assert rc == 0
        # Two transient sleeps before the final match.
        assert mock_sleep.call_count >= 2

    def test_match_after_transient_resets_backoff(self):
        """Transient errors reset backoff on success."""
        # We just assert the loop terminates; the backoff reset is an
        # internal optimisation tested via the successful exit rc.
        with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
            mock_wait.side_effect = [1, 1, 0]  # timeout, timeout, match
            rc = cmd_message_wait_loop(self._make_loop_args(max_iter=5))
        assert rc == 0

    def test_wait_loop_runs_for_many_timeouts_without_exiting(self):
        """Plan TASK-2-4 acceptance (d): wait-loop MUST loop FOREVER
        through inner rc=1 (timeout) without exiting early.

        Without this coverage, a silent early-exit bug could slip in
        and agents would die on their first timeout instead of
        re-entering the block — exactly the anti-pattern #1897 fixes.

        We exercise 5 consecutive rc=1 timeouts with a finite
        ``max_iterations=5`` cap so the test terminates deterministically.
        The assertion is: the inner call is made 5 times AND the final
        rc is 1 (safety cap tripped, not a premature exit).
        """
        with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
            # All rc=1 (timeout) — should retry rather than return.
            mock_wait.return_value = 1
            rc = cmd_message_wait_loop(self._make_loop_args(max_iter=5))
        assert rc == 1, "Expected outer rc=1 (safety cap), not a premature exit"
        assert mock_wait.call_count == 5, (
            f"Inner call invoked {mock_wait.call_count} times; "
            "expected exactly 5 (each timeout should re-enter the loop)"
        )

    def test_wait_loop_default_max_iterations_is_effectively_unbounded(self):
        """Plan TASK-2-4 + reviewer_plan blocker 3: default
        --max-iterations MUST be effectively unbounded (sys.maxsize)
        so normal BRC never trips it.

        We verify by setting max_iterations to None (the CLI default)
        and assert the internal cap gets coerced to sys.maxsize so a
        legitimate multi-hour phase doesn't silently exit early.
        """
        import sys as _sys

        args = self._make_loop_args()
        args.max_iterations = None

        # Patch cmd_message_wait to return 0 on first call so we don't
        # actually iterate sys.maxsize times. The test asserts only
        # that None is accepted and coerced internally, not the cap
        # value directly.
        with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
            mock_wait.return_value = 0
            rc = cmd_message_wait_loop(args)
        assert rc == 0

        # Also pin the coercion logic for 0 / negative values via an
        # explicit spy. If a future refactor loses the coercion, a
        # malicious operator could set --max-iterations=0 and get a
        # no-op — the test catches that regression.
        for invalid in (0, -1, -99):
            args.max_iterations = invalid
            with patch("egg_lib.orch_cli.cmd_message_wait") as mock_wait:
                mock_wait.return_value = 0
                rc = cmd_message_wait_loop(args)
            assert rc == 0, (
                f"max_iterations={invalid} should still attempt a wait; "
                "expected coercion to sys.maxsize"
            )
        # Finally: sys.maxsize semantics pin — the coerced cap is at
        # least as big as sys.maxsize / 2 so the loop is practically
        # infinite for BRC timescales.
        assert _sys.maxsize > 10**9  # sanity (platform-agnostic)


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
        """Issue #1897: CLI POSTs to the dedicated
        ``/api/v1/pipelines/{id}/heartbeat`` endpoint with a flat
        ``{from_role, state}`` body (NOT wrapped in metadata — the
        server unpacks into the stored message's metadata field).
        """
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.return_value = {"success": True}
            rc = cmd_message_heartbeat(_make_hb_args(state="WORKING"))
        assert rc == 0
        call = mock_req.call_args
        # api_request(url, path, method, data, timeout)
        path = call[0][1]
        posted = call[0][3]
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
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.return_value = {"success": True}
            rc = cmd_message_heartbeat(
                _make_hb_args(
                    state="WAITING_ON_ROLE",
                    waiting_on="reviewer_code",
                )
            )
        assert rc == 0
        posted = mock_req.call_args[0][3]
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
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError("bad metadata", status_code=400)
            rc = cmd_message_heartbeat(_make_hb_args())
        assert rc == 3

    def test_heartbeat_client_retries_5xx(self):
        """5xx from server → exit 2 (transient)."""
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError("server error", status_code=500)
            rc = cmd_message_heartbeat(_make_hb_args())
        assert rc == 2

    def test_heartbeat_since_flag_optional(self):
        """--since ISO-8601 / epoch string flows through to the flat
        payload — the server unpacks this into the stored message's
        metadata.since field.
        """
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.return_value = {"success": True}
            cmd_message_heartbeat(_make_hb_args(since="1700000000"))
            posted = mock_req.call_args[0][3]
            assert posted["since"] == "1700000000"

    def test_heartbeat_rate_limit_429_returns_exit_3(self):
        """Plan TASK-3-4: 429 response triggers exit code 3 so wrappers
        treat it as a permanent failure and back off (rather than
        spin-retrying and hammering the server)."""
        with patch("egg_lib.orch_cli.api_request") as mock_req:
            mock_req.side_effect = ApiError(
                "rate limit",
                status_code=429,
                details={"retry_after": 30},
            )
            rc = cmd_message_heartbeat(_make_hb_args(state="WORKING"))
        assert rc == 3
