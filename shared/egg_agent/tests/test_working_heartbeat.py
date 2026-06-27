"""In-tool-loop WORKING heartbeat emitter (#3341).

The consensus wrapper emits one WORKING heartbeat per one-shot event, then nothing
until the (30+ minute) invocation returns — so a genuinely-busy producer looks
bus-silent for the whole turn and trips the health monitor's heartbeat-silence
tripwire (false stall → false restart → orphaned commit cascade, #3339). The
PostToolUse-driven :class:`WorkingHeartbeatEmitter` re-emits the WORKING heartbeat
during the turn, throttled by a monotonic interval gate so a chatty tool storm
stays cheap.

This module pins the contract:

* the throttle gate (first emit due; suppressed within the interval; due again
  after it elapses) and the atomic check-and-set;
* the throttle window advancing on *attempt* (failed sends do not become a
  per-tool-call subprocess storm);
* fail-soft ``_send`` (missing binary / non-zero rc / timeout → ``False``, never
  raises) and the exact ``egg-orch message heartbeat`` command shape; and
* the ``EGG_WORKING_HEARTBEAT`` / ``EGG_WORKING_HEARTBEAT_INTERVAL_SECS`` env knobs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — make ``shared/`` importable so ``egg_agent.working_heartbeat``
# resolves against the local tree (mirrors test_midturn_messages.py).
# ---------------------------------------------------------------------------

_shared_dir = Path(__file__).resolve().parents[2]
if str(_shared_dir) not in sys.path:
    sys.path.insert(0, str(_shared_dir))

from egg_agent.working_heartbeat import (  # noqa: E402
    DEFAULT_HEARTBEAT_INTERVAL_SECS,
    WorkingHeartbeatEmitter,
    _interval_secs,
    is_working_heartbeat_disabled,
)


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


def _make_emitter(interval_secs: float = 120.0):
    clock = _Clock()
    emitter = WorkingHeartbeatEmitter(
        "issue-3341",
        "coder",
        interval_secs=interval_secs,
        now=clock,
    )
    return emitter, clock


# ---------------------------------------------------------------------------
# Throttle gate
# ---------------------------------------------------------------------------


def test_first_emit_is_due_and_sends(monkeypatch) -> None:
    emitter, _clock = _make_emitter()
    sent: list[bool] = []
    monkeypatch.setattr(emitter, "_send", lambda: sent.append(True) or True)

    assert emitter.is_due_to_emit() is True
    assert emitter.emit() is True
    assert sent == [True]


def test_within_interval_is_throttled(monkeypatch) -> None:
    emitter, clock = _make_emitter(interval_secs=120.0)
    calls: list[int] = []
    monkeypatch.setattr(emitter, "_send", lambda: calls.append(1) or True)

    assert emitter.emit() is True  # first emit fires
    clock.advance(60.0)  # still inside the 120s window

    assert emitter.is_due_to_emit() is False
    assert emitter.emit() is False  # throttled — no second send
    assert calls == [1]


def test_due_again_after_interval_elapses(monkeypatch) -> None:
    emitter, clock = _make_emitter(interval_secs=120.0)
    calls: list[int] = []
    monkeypatch.setattr(emitter, "_send", lambda: calls.append(1) or True)

    assert emitter.emit() is True
    clock.advance(120.0)  # window fully elapsed

    assert emitter.is_due_to_emit() is True
    assert emitter.emit() is True
    assert calls == [1, 1]


def test_failed_send_still_advances_throttle(monkeypatch) -> None:
    """A transiently-unreachable orchestrator must not turn the hook into a
    per-tool-call subprocess storm: the window advances on every attempt."""
    emitter, clock = _make_emitter(interval_secs=120.0)
    calls: list[int] = []

    def _failing_send() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(emitter, "_send", _failing_send)

    assert emitter.emit() is False  # attempted, send failed
    assert calls == [1]

    # Immediately retrying inside the window does NOT re-spawn the subprocess.
    assert emitter.emit() is False
    assert calls == [1]

    clock.advance(120.0)
    assert emitter.emit() is False  # window elapsed → attempts again
    assert calls == [1, 1]


# ---------------------------------------------------------------------------
# _send fail-soft + command shape
# ---------------------------------------------------------------------------


def test_send_returns_false_when_binary_missing(monkeypatch) -> None:
    emitter, _clock = _make_emitter()
    monkeypatch.setattr("egg_agent.working_heartbeat.shutil.which", lambda _b: None)
    assert emitter._send() is False


def test_send_builds_expected_command_with_slice_tag(monkeypatch) -> None:
    emitter, _clock = _make_emitter()
    monkeypatch.setattr("egg_agent.working_heartbeat.shutil.which", lambda _b: "/usr/bin/egg-orch")
    monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("egg_agent.working_heartbeat.subprocess.run", _fake_run)

    assert emitter._send() is True
    assert captured["cmd"] == [
        "/usr/bin/egg-orch",
        "message",
        "heartbeat",
        "--state",
        "WORKING",
        "--body",
        "in-tool-loop liveness (slice=slice-3)",
    ]
    assert captured["timeout"] == 5


def test_send_defaults_slice_tag_to_none(monkeypatch) -> None:
    emitter, _clock = _make_emitter()
    monkeypatch.setattr("egg_agent.working_heartbeat.shutil.which", lambda _b: "/usr/bin/egg-orch")
    monkeypatch.delenv("EGG_SLICE_ID", raising=False)
    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("egg_agent.working_heartbeat.subprocess.run", _fake_run)
    emitter._send()
    assert captured["cmd"][-1] == "in-tool-loop liveness (slice=none)"


def test_send_non_zero_rc_is_false(monkeypatch) -> None:
    """A 429 rate-limit surfaces as rc=3 — treated as a soft miss, not a raise."""
    emitter, _clock = _make_emitter()
    monkeypatch.setattr("egg_agent.working_heartbeat.shutil.which", lambda _b: "/usr/bin/egg-orch")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="rate limited")

    monkeypatch.setattr("egg_agent.working_heartbeat.subprocess.run", _fake_run)
    assert emitter._send() is False


def test_send_timeout_is_fail_soft(monkeypatch) -> None:
    emitter, _clock = _make_emitter()
    monkeypatch.setattr("egg_agent.working_heartbeat.shutil.which", lambda _b: "/usr/bin/egg-orch")

    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr("egg_agent.working_heartbeat.subprocess.run", _raise_timeout)
    assert emitter._send() is False


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "0", "off", "disabled", "FALSE", " Off "])
def test_disabled_env_hatch(monkeypatch, value) -> None:
    monkeypatch.setenv("EGG_WORKING_HEARTBEAT", value)
    assert is_working_heartbeat_disabled() is True


@pytest.mark.parametrize("value", ["", "true", "1", "on", "enabled"])
def test_enabled_by_default(monkeypatch, value) -> None:
    if value:
        monkeypatch.setenv("EGG_WORKING_HEARTBEAT", value)
    else:
        monkeypatch.delenv("EGG_WORKING_HEARTBEAT", raising=False)
    assert is_working_heartbeat_disabled() is False


def test_interval_default(monkeypatch) -> None:
    monkeypatch.delenv("EGG_WORKING_HEARTBEAT_INTERVAL_SECS", raising=False)
    assert _interval_secs() == DEFAULT_HEARTBEAT_INTERVAL_SECS


def test_interval_override(monkeypatch) -> None:
    monkeypatch.setenv("EGG_WORKING_HEARTBEAT_INTERVAL_SECS", "45")
    assert _interval_secs() == 45.0


@pytest.mark.parametrize("value", ["notanumber", "0", "-5"])
def test_interval_invalid_falls_back_to_default(monkeypatch, value) -> None:
    monkeypatch.setenv("EGG_WORKING_HEARTBEAT_INTERVAL_SECS", value)
    assert _interval_secs() == DEFAULT_HEARTBEAT_INTERVAL_SECS
