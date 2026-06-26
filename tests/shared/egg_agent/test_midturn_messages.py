"""Tests for mid-turn operator message delivery (#3123).

The poller's contract: throttled bus polls via an ``egg-orch message
poll`` subprocess, a cursor file that persists across one-shot
invocations, seed-at-tip on first poll (no history flood), operator-only
injection, and fail-soft on every failure mode.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

from egg_agent.midturn_messages import (
    DEFAULT_POLL_INTERVAL_SECS,
    MidturnMessagePoller,
    is_midturn_messages_disabled,
)


def _poll_response(messages: list[dict[str, Any]], *, stale: bool = False) -> str:
    data: dict[str, Any] = {"messages": messages, "count": len(messages)}
    if stale:
        data["since_id_stale"] = True
    return json.dumps({"success": True, "data": data})


def _completed(stdout: str, rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


def _message(
    msg_id: str,
    from_role: str = "overseer",
    body: str = "Adopt the prior branch.",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "from_role": from_role,
        "to_role": "coder",
        "message_type": "GUIDANCE",
        "subject": "course correction",
        "timestamp": "2026-06-11T09:00:00",
        "body": body,
        **extra,
    }


class _Clock:
    """Deterministic monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _make_poller(tmp_path, clock: _Clock | None = None) -> MidturnMessagePoller:
    return MidturnMessagePoller(
        "pipeline-test",
        "coder",
        interval_secs=60.0,
        cursor_dir=str(tmp_path),
        now=clock or _Clock(),
    )


class TestFirstPollSeedsCursor:
    def test_first_poll_seeds_at_tip_without_injecting(self, tmp_path):
        """No cursor file → record the tip, inject nothing (history is not new)."""
        poller = _make_poller(tmp_path)
        history = [_message("msg-1"), _message("msg-2", from_role="coder")]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(history)),
            ) as mock_run,
        ):
            assert poller.poll() is None

        # Cursor seeded at the last message id; no --since on the seed poll.
        assert poller._cursor_path.read_text() == "msg-2"
        cmd = mock_run.call_args.args[0]
        assert "--since" not in cmd
        assert "pipeline-test" in cmd
        assert "--role" in cmd

    def test_first_poll_with_overflow_history_snaps_to_tip(self, tmp_path):
        """Pre-existing history past the bus limit doesn't re-inject as new.

        ``message_store`` returns ``matches[-limit:]`` with no ``since_id``
        — the LAST ``limit`` messages, not the oldest — so the seed-at-tip
        guarantee in the docstring is load-bearing on that semantic.
        This test pins the poller's behavior: when the store returns the
        last ``limit`` messages and an operator-authored directive is
        among them (i.e. could plausibly be \"new\"), the first poll
        still treats the whole batch as history, advances the cursor to
        the very tip, and injects nothing. A second poll with no further
        messages then asserts the cursor stayed at the tip (no replay).
        """
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)

        # Simulate the store returning the last 100 (tail of 110+ stream).
        # Include an operator-authored message in the batch so we can
        # confirm seed-at-tip suppresses it (a regression would render
        # it because operator filtering would otherwise kick in).
        tail_count = 100
        tail = [
            _message(
                f"msg-{i}",
                from_role=("overseer" if i == 50 else "coder"),
                body=("historical operator directive" if i == 50 else "peer chatter"),
            )
            for i in range(11, 11 + tail_count)
        ]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(tail)),
            ) as mock_run,
        ):
            assert poller.poll() is None  # seed-at-tip — historical operator msg suppressed

        assert poller._cursor_path.read_text() == f"msg-{11 + tail_count - 1}"  # tip
        cmd = mock_run.call_args.args[0]
        assert "--since" not in cmd

        # Second poll, interval elapsed, store returns nothing new.
        clock.value += 61.0
        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response([])),
            ) as mock_run,
        ):
            assert poller.poll() is None

        # Cursor still at the tip; no historical operator message was injected.
        assert poller._cursor_path.read_text() == f"msg-{11 + tail_count - 1}"
        cmd = mock_run.call_args.args[0]
        assert cmd[cmd.index("--since") + 1] == f"msg-{11 + tail_count - 1}"

    def test_second_poll_injects_new_operator_message(self, tmp_path):
        """A message arriving after the seed is rendered for injection."""
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response([_message("msg-1")])),
            ),
        ):
            assert poller.poll() is None  # seed

        clock.value += 61.0
        # An operator-authored message renders under the BINDING header; the
        # #2270 §2 intent-discriminator only treats human/operator/user senders
        # as binding directives (overseer/orchestrator broadcasts are now
        # informational), so this test uses an operator sender to assert the
        # binding operator-message rendering.
        new_message = _message("msg-2", from_role="operator", body="ADOPT, DO NOT REIMPLEMENT")
        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response([new_message])),
            ) as mock_run,
        ):
            block = poller.poll()

        cmd = mock_run.call_args.args[0]
        assert cmd[cmd.index("--since") + 1] == "msg-1"
        assert block is not None
        assert "ADOPT, DO NOT REIMPLEMENT" in block
        assert "Operator directive(s) received mid-turn" in block
        assert "BINDING" in block
        assert poller._cursor_path.read_text() == "msg-2"


class TestThrottle:
    def test_polls_are_interval_gated(self, tmp_path):
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response([])),
            ) as mock_run,
        ):
            poller.poll()
            clock.value += 30.0  # under the 60s interval
            poller.poll()
            poller.poll()

        assert mock_run.call_count == 1

    def test_concurrent_polls_share_the_throttle_slot(self, tmp_path):
        """Two callers entering ``poll`` simultaneously fetch at most once.

        ``poll()`` runs under ``asyncio.to_thread``; if PostToolUse ever
        delivers callbacks concurrently, the throttle check-and-set must
        be atomic — otherwise both callers observe a stale ``_last_poll``,
        both pass the gate, and both spawn a subprocess (and risk a
        duplicate-injection window). The lock around the check-and-set
        in ``poll`` closes the race; here we drive two threads through
        the gate simultaneously and assert only one fetch ran.
        """
        import threading as _threading

        clock = _Clock()
        poller = _make_poller(tmp_path, clock)
        # Seed cursor so the first eligible poll is treated as new
        # (covers both throttle paths: seed and steady-state).
        poller._cursor_path.write_text("msg-0", encoding="utf-8")

        gate = _threading.Event()
        fetch_started = _threading.Event()

        def slow_run(*_a, **_kw):
            fetch_started.set()
            gate.wait(timeout=2.0)
            return _completed(_poll_response([_message("msg-1", body="op directive")]))

        results: list[Any] = []
        errors: list[BaseException] = []

        def runner():
            try:
                results.append(poller.poll())
            except BaseException as exc:  # pragma: no cover - propagate
                errors.append(exc)

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch("egg_agent.midturn_messages.subprocess.run", side_effect=slow_run) as mock_run,
        ):
            t1 = _threading.Thread(target=runner)
            t2 = _threading.Thread(target=runner)
            t1.start()
            # Wait until t1 is inside the fetch (so t2 hits the lock /
            # post-set throttle and must see ``_last_poll`` advanced).
            assert fetch_started.wait(timeout=2.0)
            t2.start()
            gate.set()
            t1.join(timeout=3.0)
            t2.join(timeout=3.0)

        assert not errors, errors
        assert mock_run.call_count == 1
        # Exactly one caller gets the block; the other is throttled out.
        assert sum(r is not None for r in results) == 1
        assert sum(r is None for r in results) == 1

    def test_is_due_to_poll_predicate(self, tmp_path):
        """Lockless fast-path predicate exposes the throttle window.

        Lets ``client.py``'s PostToolUse hook skip ``asyncio.to_thread``
        on every tool call once the interval gate is closed. The
        predicate is intentionally lockless; the authoritative gate is
        in ``poll`` under ``_poll_lock`` (covered by
        ``test_concurrent_polls_share_the_throttle_slot``).
        """
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)

        # Never polled yet → due.
        assert poller.is_due_to_poll() is True

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response([])),
            ),
        ):
            poller.poll()

        # Just polled → not due.
        assert poller.is_due_to_poll() is False
        clock.value += 30.0
        assert poller.is_due_to_poll() is False
        clock.value += 31.0  # past 60s interval
        assert poller.is_due_to_poll() is True

    def test_default_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("EGG_MIDTURN_MESSAGES_INTERVAL_SECS", "120")
        poller = MidturnMessagePoller("p", "r")
        assert poller.interval_secs == 120.0

        monkeypatch.setenv("EGG_MIDTURN_MESSAGES_INTERVAL_SECS", "not-a-number")
        poller = MidturnMessagePoller("p", "r")
        assert poller.interval_secs == DEFAULT_POLL_INTERVAL_SECS

        monkeypatch.setenv("EGG_MIDTURN_MESSAGES_INTERVAL_SECS", "-5")
        poller = MidturnMessagePoller("p", "r")
        assert poller.interval_secs == DEFAULT_POLL_INTERVAL_SECS


class TestFiltering:
    def _seeded_poller(self, tmp_path, clock: _Clock) -> MidturnMessagePoller:
        poller = _make_poller(tmp_path, clock)
        poller._cursor_path.write_text("msg-0", encoding="utf-8")
        return poller

    def test_peer_and_protocol_traffic_not_injected_but_advances_cursor(self, tmp_path):
        """Non-operator messages stay on the between-invocation path."""
        clock = _Clock()
        poller = self._seeded_poller(tmp_path, clock)
        peer_traffic = [
            _message("msg-1", from_role="coder", message_type="CONSENSUS_PROPOSE"),
            _message("msg-2", from_role="tester", message_type="STATUS"),
        ]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(peer_traffic)),
            ),
        ):
            assert poller.poll() is None

        # Cursor still advances so the chatter is not re-fetched forever.
        assert poller._cursor_path.read_text() == "msg-2"

    def test_mixed_batch_injects_only_operator_messages(self, tmp_path):
        clock = _Clock()
        poller = self._seeded_poller(tmp_path, clock)
        batch = [
            _message("msg-1", from_role="coder", body="peer chatter"),
            _message("msg-2", from_role="overseer", body="operator directive"),
            _message("msg-3", from_role="human", body="human follow-up"),
        ]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(batch)),
            ),
        ):
            block = poller.poll()

        assert block is not None
        assert "operator directive" in block
        assert "human follow-up" in block
        assert "peer chatter" not in block

    def test_orchestrator_role_is_injected(self, tmp_path):
        """Orchestrator-issued nudges surface mid-turn (e.g. the
        ``brc_confirmation_timeout`` directed wake from
        ``orchestrator/routes/pipelines.py::_send_brc_confirmation_nudge``,
        whose whole point is to nudge a producer that has stalled
        post-ACK)."""
        clock = _Clock()
        poller = self._seeded_poller(tmp_path, clock)
        batch = [
            _message(
                "msg-1",
                from_role="orchestrator",
                message_type="OVERSEER_ALERT",
                subject="BRC confirmation timeout — call mcp__brc__confirm",
                body="You are PROPOSED and fully ACKed but have not confirmed in 300s.",
            )
        ]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(batch)),
            ),
        ):
            block = poller.poll()

        assert block is not None
        assert "mcp__brc__confirm" in block
        assert "from orchestrator" in block

    def test_since_id_stale_resnaps_without_injecting(self, tmp_path):
        """Store-side cursor invalidation (#2464) re-snaps to tip silently."""
        clock = _Clock()
        poller = self._seeded_poller(tmp_path, clock)
        replay = [_message("msg-7"), _message("msg-8")]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(replay, stale=True)),
            ),
        ):
            assert poller.poll() is None

        assert poller._cursor_path.read_text() == "msg-8"


class TestFailSoft:
    def test_missing_binary_is_silent(self, tmp_path):
        poller = _make_poller(tmp_path)
        with patch("egg_agent.midturn_messages.shutil.which", return_value=None):
            assert poller.poll() is None

    def test_nonzero_rc_timeout_and_bad_json_are_silent(self, tmp_path):
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)
        failure_modes = [
            _completed("", rc=1),
            subprocess.TimeoutExpired(cmd="egg-orch", timeout=16),
            _completed("not json"),
            _completed(json.dumps({"success": False, "message": "boom"})),
        ]
        with patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"):
            for mode in failure_modes:
                clock.value += 61.0
                kwargs = (
                    {"side_effect": mode} if isinstance(mode, Exception) else {"return_value": mode}
                )
                with patch("egg_agent.midturn_messages.subprocess.run", **kwargs):
                    assert poller.poll() is None
        # No cursor was ever written on failures.
        assert not poller._cursor_path.exists()

    def test_failed_fetch_does_not_advance_cursor(self, tmp_path):
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)
        poller._cursor_path.write_text("msg-5", encoding="utf-8")

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed("", rc=2),
            ),
        ):
            assert poller.poll() is None

        assert poller._cursor_path.read_text() == "msg-5"


class TestRendering:
    def test_block_truncated_at_cap(self, tmp_path):
        from egg_agent.midturn_messages import _RENDERED_BLOCK_MAX_CHARS

        clock = _Clock()
        poller = _make_poller(tmp_path, clock)
        poller._cursor_path.write_text("msg-0", encoding="utf-8")
        huge = [_message("msg-1", body="directive " * 2000)]

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response(huge)),
            ),
        ):
            block = poller.poll()

        assert block is not None
        assert len(block) < _RENDERED_BLOCK_MAX_CHARS + 200
        assert "messages truncated" in block

    def test_empty_body_renders_placeholder(self, tmp_path):
        clock = _Clock()
        poller = _make_poller(tmp_path, clock)
        poller._cursor_path.write_text("msg-0", encoding="utf-8")

        with (
            patch("egg_agent.midturn_messages.shutil.which", return_value="/usr/bin/egg-orch"),
            patch(
                "egg_agent.midturn_messages.subprocess.run",
                return_value=_completed(_poll_response([_message("msg-1", body="")])),
            ),
        ):
            block = poller.poll()

        assert block is not None
        assert "(no body)" in block


class TestDisableSwitch:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("EGG_MIDTURN_MESSAGES", raising=False)
        assert is_midturn_messages_disabled() is False

    def test_disabled_values(self, monkeypatch):
        for value in ("false", "0", "off", "disabled", "FALSE", " Off "):
            monkeypatch.setenv("EGG_MIDTURN_MESSAGES", value)
            assert is_midturn_messages_disabled() is True

    def test_enabled_values(self, monkeypatch):
        for value in ("true", "1", "on", "anything-else"):
            monkeypatch.setenv("EGG_MIDTURN_MESSAGES", value)
            assert is_midturn_messages_disabled() is False
