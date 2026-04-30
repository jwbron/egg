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
from typing import Any
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
    since: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        pipeline_id=pipeline_id,
        for_=for_ or ["CONSENSUS_CONFIRMED"],
        role=None,
        from_=None,
        since=since,
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
        since: str | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            pipeline_id="issue-42",
            for_=["CONSENSUS_CONFIRMED"],
            role=None,
            from_=None,
            since=since,
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


# ---------------------------------------------------------------------------
# Auto cursor file (issue #2323): close the wait→process→wait race that
# stalls multi-producer reviewer phases. The cursor file is derived
# automatically from EGG_AGENT_ROLE + a hash of the wait's --for set,
# threading the response cursor across successive CLI invocations
# without callers having to opt in.
# ---------------------------------------------------------------------------


from egg_lib.orch_cli import _wait_cursor_path  # noqa: E402


class TestWaitCursorPath:
    """``_wait_cursor_path`` derives a per-(pipeline_id, role,
    for_types, from_role) file path."""

    def test_returns_none_without_role(self):
        assert _wait_cursor_path("issue-42", None, ["X"]) is None
        assert _wait_cursor_path("issue-42", "", ["X"]) is None

    def test_returns_none_without_for_types(self):
        assert _wait_cursor_path("issue-42", "reviewer_plan", []) is None
        assert (
            _wait_cursor_path("issue-42", "reviewer_plan", None)  # type: ignore[arg-type]
            is None
        )

    def test_path_is_stable_across_for_types_order(self, monkeypatch, tmp_path):
        """``--for X --for Y`` and ``--for Y --for X`` must share a
        cursor — the type set is what matters, not the order."""
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        a = _wait_cursor_path("issue-42", "reviewer_plan", ["X", "Y"])
        b = _wait_cursor_path("issue-42", "reviewer_plan", ["Y", "X"])
        assert a == b

    def test_distinct_for_types_yield_distinct_paths(self, monkeypatch, tmp_path):
        """POLL (``--for CONSENSUS_PROPOSE``) and STAY ALIVE
        (``--for ... 4 types ...``) must hash to different files so
        cross-purpose cursor leakage is impossible."""
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        poll = _wait_cursor_path("issue-42", "reviewer_plan", ["CONSENSUS_PROPOSE"])
        stay = _wait_cursor_path(
            "issue-42",
            "reviewer_plan",
            ["CONSENSUS_PROPOSE", "CONSENSUS_RE_REVIEW", "CONSENSUS_CONFIRMED", "OVERSEER_ALERT"],
        )
        assert poll != stay

    def test_distinct_roles_yield_distinct_paths(self, monkeypatch, tmp_path):
        """Two roles in the same container (unusual but possible)
        must not stomp on each other's cursors."""
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        a = _wait_cursor_path("issue-42", "reviewer_plan", ["X"])
        b = _wait_cursor_path("issue-42", "reviewer_code", ["X"])
        assert a != b

    def test_distinct_pipelines_yield_distinct_paths(self, monkeypatch, tmp_path):
        """Two pipelines sharing a ``/tmp`` mount (debug shells,
        integration test reuse) must NOT share a cursor — pipeline B's
        first wait would otherwise read pipeline A's cursor and either
        re-receive A's stream from time zero (when B's stream is
        unrelated) or skip part of B's stream."""
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        a = _wait_cursor_path("issue-42", "reviewer_plan", ["X"])
        b = _wait_cursor_path("issue-99", "reviewer_plan", ["X"])
        assert a != b

    def test_distinct_from_roles_yield_distinct_paths(self, monkeypatch, tmp_path):
        """Two waits with the same ``--for`` set but different
        ``--from`` filters must NOT share a cursor — a wait advancing
        past a message its filter dropped would cause a sibling wait
        with a different filter to miss it."""
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        a = _wait_cursor_path("issue-42", "reviewer_plan", ["X"], from_role="architect")
        b = _wait_cursor_path("issue-42", "reviewer_plan", ["X"], from_role="coder")
        no_filter = _wait_cursor_path("issue-42", "reviewer_plan", ["X"])
        assert a != b
        assert a != no_filter
        assert b != no_filter

    def test_path_lives_under_egg_wait_cursor_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        path = _wait_cursor_path("issue-42", "reviewer_plan", ["X"])
        assert path is not None
        assert path.startswith(str(tmp_path))
        assert "egg-wait-cursor-issue-42-reviewer_plan-" in path


class TestAutoCursorWait:
    """``cmd_message_wait`` auto-threads a per-(role, for_types) cursor."""

    def _setenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str = "reviewer_plan"
    ) -> None:
        monkeypatch.setenv("EGG_AGENT_ROLE", role)
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))

    def _expected_path(
        self,
        tmp_path: Path,
        for_types: list[str],
        role: str = "reviewer_plan",
        pipeline_id: str = "issue-42",
        from_role: str | None = None,
    ) -> str:
        path = _wait_cursor_path(pipeline_id, role, for_types, from_role)
        assert path is not None
        return path

    def test_no_role_skips_cursor_handling(self, monkeypatch, tmp_path):
        """Debug shells without ``EGG_AGENT_ROLE`` get the legacy
        from-tip behavior — no file-system side effects."""
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-x"},
            }
            cmd_message_wait(_make_wait_args())
        endpoint = mock_req.call_args.args[0]
        assert "since_id" not in endpoint
        assert list(tmp_path.iterdir()) == []

    def test_first_call_omits_since(self, monkeypatch, tmp_path):
        """A fresh container (no cursor file yet) must NOT pass
        ``since_id`` — the server's from_tip semantics protect against
        re-matching ancient stream entries (issue #1925)."""
        self._setenv(monkeypatch, tmp_path)
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-aaa"},
            }
            cmd_message_wait(_make_wait_args())
        endpoint = mock_req.call_args.args[0]
        assert "since_id" not in endpoint

    def test_writes_cursor_on_timeout_then_threads_on_next_call(self, monkeypatch, tmp_path):
        """Wait→wait race regression: a timeout on call 1 advances the
        cursor file, and call 2 picks it up as ``since_id`` so any
        event that landed in the gap is delivered."""
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]

        # Call 1: server returns "no match, here's the tip".
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {
                    "messages": [],
                    "matched": False,
                    "count": 0,
                    "cursor": "01-tip-at-timeout",
                },
            }
            cmd_message_wait(_make_wait_args(for_=for_types))

        cursor_path = self._expected_path(tmp_path, for_types)
        assert cursor_path is not None
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-tip-at-timeout"

        # Call 2: server should now see the threaded cursor.
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-newer"},
            }
            cmd_message_wait(_make_wait_args(for_=for_types))
            endpoint = mock_req.call_args.args[0]
        assert "since_id=01-tip-at-timeout" in endpoint

    def test_writes_cursor_on_match(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {
                    "messages": [
                        {
                            "id": "01-msg-id",
                            "timestamp": "2026-04-30T05:00:00",
                            "from_role": "task_planner",
                            "to_role": "reviewer_plan",
                            "message_type": "CONSENSUS_PROPOSE",
                            "subject": "plan v1",
                            "body": "",
                        }
                    ],
                    "matched": True,
                    "count": 1,
                    "cursor": "01-msg-id",
                },
            }
            rc = cmd_message_wait(_make_wait_args(for_=for_types))
        assert rc == 0
        cursor_path = self._expected_path(tmp_path, for_types)
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-msg-id"

    def test_does_not_write_on_permanent_error(self, monkeypatch, tmp_path):
        """rc=3 leaves any prior cursor file alone — the wait did not
        advance, so the cursor must not move."""
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = self._expected_path(tmp_path, for_types)
        with open(cursor_path, "w") as fh:
            fh.write("01-keep-me")
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("bad request", status_code=400)
            rc = cmd_message_wait(_make_wait_args(for_=for_types))
        assert rc == 3
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-keep-me"

    def test_does_not_write_on_transient_error(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = self._expected_path(tmp_path, for_types)
        with open(cursor_path, "w") as fh:
            fh.write("01-keep-me")
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.side_effect = GatewayError("server error", status_code=500)
            rc = cmd_message_wait(_make_wait_args(for_=for_types))
        assert rc == 2
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-keep-me"

    def test_explicit_since_overrides_cursor(self, monkeypatch, tmp_path):
        """An explicit ``--since`` wins over the auto-derived cursor —
        callers being deliberate about resume position must not be
        overridden by a stale file."""
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = self._expected_path(tmp_path, for_types)
        with open(cursor_path, "w") as fh:
            fh.write("01-stale")
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-tip"},
            }
            cmd_message_wait(_make_wait_args(for_=for_types, since="01-explicit"))
        endpoint = mock_req.call_args.args[0]
        assert "since_id=01-explicit" in endpoint
        assert "since_id=01-stale" not in endpoint


class TestAutoCursorWaitLoop:
    """``cmd_message_wait_loop`` mirrors the auto-cursor semantics."""

    def _setenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str = "reviewer_plan"
    ) -> None:
        monkeypatch.setenv("EGG_AGENT_ROLE", role)
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))

    def _make_loop_args(self, since: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            pipeline_id="issue-42",
            for_=["CONSENSUS_PROPOSE"],
            role=None,
            from_=None,
            since=since,
            limit=None,
            timeout=1,
            max_iterations=3,
            json=False,
        )

    def test_no_role_skips_cursor_handling(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        captured: dict[str, Any] = {}

        def _capture_handler(req):
            captured.update(req)
            return {"ok": True, "matched": True, "messages": [], "cursor": "01-x"}

        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=_capture_handler,
        ):
            cmd_message_wait_loop(self._make_loop_args())
        assert "since" not in captured
        assert list(tmp_path.iterdir()) == []

    def test_threads_stored_cursor_to_handler(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", for_types)
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-prior-tip")
        captured: dict[str, Any] = {}

        def _capture_handler(req):
            captured.update(req)
            return {"ok": True, "matched": True, "messages": [], "cursor": "01-new"}

        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=_capture_handler,
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 0
        assert captured.get("since") == "01-prior-tip"

    def test_writes_cursor_on_match(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            return_value={
                "ok": True,
                "matched": True,
                "messages": [
                    {
                        "timestamp": "2026-04-30T05:00:00",
                        "from_role": "architect",
                        "to_role": "reviewer_plan",
                        "message_type": "CONSENSUS_PROPOSE",
                        "subject": "arch v1",
                        "body": "",
                    }
                ],
                "cursor": "01-architect-id",
                "iterations": 1,
            },
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 0
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", ["CONSENSUS_PROPOSE"])
        assert cursor_path is not None
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-architect-id"

    def test_writes_cursor_on_safety_cap(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", ["CONSENSUS_PROPOSE"])
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-old")
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            return_value={
                "ok": True,
                "matched": False,
                "messages": [],
                "cursor": "01-advanced",
                "iterations": 3,
            },
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 1
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-advanced"

    def test_does_not_write_on_permanent_error(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", ["CONSENSUS_PROPOSE"])
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-keep-me")
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=GatewayError("forbidden", status_code=403),
        ):
            rc = cmd_message_wait_loop(self._make_loop_args())
        assert rc == 1
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-keep-me"

    def test_explicit_since_overrides_cursor(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", ["CONSENSUS_PROPOSE"])
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-stale")
        captured: dict[str, Any] = {}

        def _capture_handler(req):
            captured.update(req)
            return {"ok": True, "matched": True, "messages": [], "cursor": "01-tip"}

        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            side_effect=_capture_handler,
        ):
            cmd_message_wait_loop(self._make_loop_args(since="01-explicit"))
        assert captured.get("since") == "01-explicit"


class TestCursorNullResponsePreservesPriorCursor:
    """A ``cursor=None`` response (empty stream, pathological safety
    cap with no observed events) must NOT clear a previously-stored
    cursor — the invariant "the cursor file never moves backward"
    holds unconditionally. Pinned per the previous review's finding #5.
    """

    def _setenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str = "reviewer_plan"
    ) -> None:
        monkeypatch.setenv("EGG_AGENT_ROLE", role)
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))

    def test_wait_preserves_prior_cursor_when_response_cursor_is_none(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", for_types)
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-prior-tip")
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": None},
            }
            cmd_message_wait(_make_wait_args(for_=for_types))
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-prior-tip"

    def test_wait_loop_preserves_prior_cursor_when_response_cursor_is_none(
        self, monkeypatch, tmp_path
    ):
        self._setenv(monkeypatch, tmp_path)
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", ["CONSENSUS_PROPOSE"])
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-prior-tip")
        with patch(
            "egg_agent_tools.handlers.message.message_wait_loop",
            return_value={
                "ok": True,
                "matched": False,
                "messages": [],
                "cursor": None,
                "iterations": 3,
            },
        ):
            args = argparse.Namespace(
                pipeline_id="issue-42",
                for_=["CONSENSUS_PROPOSE"],
                role=None,
                from_=None,
                since=None,
                limit=None,
                timeout=1,
                max_iterations=3,
                json=False,
            )
            cmd_message_wait_loop(args)
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-prior-tip"

    def test_wait_preserves_prior_cursor_when_response_cursor_is_empty(self, monkeypatch, tmp_path):
        """Whitespace-only and empty-string cursor responses must
        also preserve."""
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", for_types)
        assert cursor_path is not None
        with open(cursor_path, "w") as fh:
            fh.write("01-prior-tip")
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "   "},
            }
            cmd_message_wait(_make_wait_args(for_=for_types))
        with open(cursor_path) as fh:
            assert fh.read().strip() == "01-prior-tip"


class TestCursorReadDefenses:
    """``_read_cursor_file`` must not crash on malformed file
    contents — a corrupt cursor file is best-effort I/O, not a hard
    failure. Pinned per the previous review's finding #3.
    """

    def test_non_utf8_cursor_file_is_treated_as_empty(self, monkeypatch, tmp_path):
        """``UnicodeDecodeError`` (a ``ValueError`` subclass) must be
        caught — a wait that hits a corrupted cursor file should fall
        back to from-tip semantics, not abort with a traceback."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "reviewer_plan")
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))
        for_types = ["CONSENSUS_PROPOSE"]
        cursor_path = _wait_cursor_path("issue-42", "reviewer_plan", for_types)
        assert cursor_path is not None
        # Write raw non-UTF-8 bytes (0xff is invalid as the lead byte
        # of any UTF-8 sequence).
        with open(cursor_path, "wb") as fh:
            fh.write(b"\xff\xfe\xfd")
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-tip"},
            }
            rc = cmd_message_wait(_make_wait_args(for_=for_types))
        # No traceback: returns the timeout exit code (1).
        assert rc == 1
        endpoint = mock_req.call_args.args[0]
        # No since_id propagated — corrupt cursor was ignored.
        assert "since_id" not in endpoint


class TestCursorPathPipelineAndFromIsolation:
    """End-to-end checks that two waits with the same role + for-set
    but different ``pipeline_id`` or ``--from`` filters do NOT share
    a cursor file. Pinned per the previous review's findings #1 and
    #2.
    """

    def _setenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str = "reviewer_plan"
    ) -> None:
        monkeypatch.setenv("EGG_AGENT_ROLE", role)
        monkeypatch.setenv("EGG_WAIT_CURSOR_DIR", str(tmp_path))

    def test_pipeline_isolation(self, monkeypatch, tmp_path):
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        # Pipeline A advances the cursor.
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-A-tip"},
            }
            cmd_message_wait(_make_wait_args(pipeline_id="issue-42", for_=for_types))
        # Pipeline B's first call must NOT pick up A's cursor.
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-B-tip"},
            }
            cmd_message_wait(_make_wait_args(pipeline_id="issue-99", for_=for_types))
            endpoint = mock_req.call_args.args[0]
        assert "since_id=01-A-tip" not in endpoint

    def test_from_role_isolation(self, monkeypatch, tmp_path):
        """A wait with ``--from architect`` advancing past a message
        its filter dropped must not cause a sibling wait with
        ``--from coder`` to skip a smaller message ID from coder."""
        self._setenv(monkeypatch, tmp_path)
        for_types = ["CONSENSUS_PROPOSE"]
        # Wait scoped to architect — advances cursor to 01-A-tip.
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-A-tip"},
            }
            args_arch = _make_wait_args(for_=for_types)
            args_arch.from_ = "architect"
            cmd_message_wait(args_arch)
        # Wait scoped to coder must NOT see 01-A-tip on its first call.
        with patch(_ORCH_MOCK_PATH) as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"messages": [], "matched": False, "count": 0, "cursor": "01-C-tip"},
            }
            args_coder = _make_wait_args(for_=for_types)
            args_coder.from_ = "coder"
            cmd_message_wait(args_coder)
            endpoint = mock_req.call_args.args[0]
        assert "since_id=01-A-tip" not in endpoint
