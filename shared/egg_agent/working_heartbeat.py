"""In-tool-loop WORKING heartbeat emission (#3341).

One ``propose`` invocation under the BRC event-pump can run 30+ minutes (a
slice coder implements its whole task list in a single turn). The
consensus wrapper emits exactly one ``WORKING`` heartbeat *before* handing
control to the agent, then there is no further bus traffic until the
invocation returns — the "no background emitter" design (see
``orchestrator/consensus_wrapper.py``). So a genuinely-busy agent making
continuous tool calls looks bus-silent for the whole turn, and any single
step longer than the heartbeat-silence threshold
(``orchestrator_implement_heartbeat_timeout_seconds``, 600s) trips the
health monitor's ``check_heartbeats`` tripwire regardless of how busy the
pod is. Acting on that false stall by restarting can orphan in-flight
commits and cascade-fail a multi-slice run (#3339), so the false positive
has a real, expensive failure mode.

This module restores the missing liveness signal at its source: a
throttled emitter that ``client.py`` wires into the SDK session as a
PostToolUse hook. Every tool call gives it a chance to fire; a monotonic
interval gate keeps the actual emission cheap (one ``egg-orch message
heartbeat`` subprocess per ``EGG_WORKING_HEARTBEAT_INTERVAL_SECS``), so a
busy agent stays visibly alive on the bus and the heartbeat tripwire only
fires on genuine silence.

It is the exact sibling of :class:`midturn_messages.MidturnMessagePoller`
(#3123): same 30+-minute-turn problem, same hook shape (lockless fast-path
predicate → ``asyncio.to_thread`` → fail-soft subprocess), and it reuses
the same ``WORKING`` heartbeat the wrapper already emits — the
schema-validated, per-role-deduped, rate-limited ``/heartbeat`` endpoint
(#1897), which is exactly the signal ``check_heartbeats`` consumes.

Design notes:

* Event-driven, not a background timer: the heartbeat fires only when the
  agent is doing tool work, which *is* the liveness semantics we want, and
  it honours the wrapper's deliberate "no background emitter" stance.
* The interval (default 120s) sits well under the 300s Tier-1 and 600s
  implement-phase silence thresholds, so several ticks land before any
  tripwire could fire, and well under the 20/min ``EGG_HEARTBEAT_RATE_LIMIT``
  cap.
* Limitation: PostToolUse fires *after* a tool call returns, so a single
  uninterrupted tool call longer than the interval (e.g. one 10-minute
  ``make test-all`` Bash call) still does not tick mid-call. The dominant
  repro — dozens of edits/tests/commits across a long turn — is covered.
* Fail-soft everywhere: a missing ``egg-orch`` binary, an unreachable
  orchestrator, a 429 rate-limit, or a subprocess timeout all mean "no
  heartbeat this tick" — never an agent failure.
* Gated on pipeline context (``EGG_PIPELINE_ID`` + ``EGG_AGENT_ROLE``, read
  from the pod env by the CLI handler) so non-pipeline ``egg_agent``
  callers are untouched; ``EGG_WORKING_HEARTBEAT=false`` is the rollback
  escape hatch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

# Default seconds between heartbeat emissions. Tool calls arrive much more
# often than this; the interval gate makes the hook effectively free
# between emissions. 120s leaves ≥2 ticks before the 300s Tier-1 stall
# window and ~5 before the 600s implement-phase window, while staying far
# under the 20/min heartbeat rate-limit cap.
DEFAULT_HEARTBEAT_INTERVAL_SECS = 120.0

# Subprocess budget for one emission. Mirrors the consensus wrapper's
# ``timeout 5 egg-orch message heartbeat`` — the heartbeat is best-effort
# liveness, not something worth blocking the tool loop on.
_EMIT_SUBPROCESS_TIMEOUT_SECS = 5

# Body text echoed into the heartbeat so a snapshot/log reader can tell an
# in-tool-loop liveness ping apart from the wrapper's once-per-event ping.
_HEARTBEAT_BODY = "in-tool-loop liveness"


def is_working_heartbeat_disabled() -> bool:
    """Check the ``EGG_WORKING_HEARTBEAT`` escape hatch (default: enabled)."""
    return (os.environ.get("EGG_WORKING_HEARTBEAT") or "").strip().lower() in (
        "false",
        "0",
        "off",
        "disabled",
    )


def _interval_secs() -> float:
    raw = (os.environ.get("EGG_WORKING_HEARTBEAT_INTERVAL_SECS") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_HEARTBEAT_INTERVAL_SECS


class WorkingHeartbeatEmitter:
    """Throttled in-tool-loop ``WORKING`` heartbeat emitter.

    One instance per SDK session; :meth:`emit` is called from the
    PostToolUse hook (via ``asyncio.to_thread`` — it runs a subprocess)
    and returns ``True`` when it actually emitted, ``False`` otherwise.
    """

    def __init__(
        self,
        pipeline_id: str,
        role: str,
        *,
        interval_secs: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.role = role
        self.interval_secs = interval_secs if interval_secs is not None else _interval_secs()
        self._now = now
        self._last_emit: float | None = None
        # Guards the throttle check-and-set so two concurrent PostToolUse
        # callbacks (today serial, but the SDK may move to concurrent
        # delivery) cannot both observe a stale ``_last_emit`` and both
        # spawn a heartbeat subprocess. ``is_due_to_emit`` outside the lock
        # is intentionally lockless — a fast-path hint for hook callers;
        # the authoritative gate is in ``emit`` under this lock.
        self._emit_lock = threading.Lock()

    # -- bus access -------------------------------------------------------

    def _send(self) -> bool:
        """Run one ``egg-orch message heartbeat`` subprocess (fail-soft).

        ``pipeline_id`` / ``role`` are read from the pod env
        (``EGG_PIPELINE_ID`` / ``EGG_AGENT_ROLE``) by the CLI handler, the
        same way the consensus wrapper's ``emit_heartbeat`` invokes it.
        Returns ``True`` on a clean exit, ``False`` on any failure (binary
        missing, non-zero rc — including a 429 rate-limit surfaced as
        rc=3 — timeout, or OS error).
        """
        binary = shutil.which("egg-orch")
        if not binary:
            return False
        slice_tag = (os.environ.get("EGG_SLICE_ID") or "none").strip() or "none"
        cmd = [
            binary,
            "message",
            "heartbeat",
            "--state",
            "WORKING",
            "--body",
            f"{_HEARTBEAT_BODY} (slice={slice_tag})",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_EMIT_SUBPROCESS_TIMEOUT_SECS,
                check=False,
            )
        except subprocess.SubprocessError, OSError:
            return False
        return proc.returncode == 0

    # -- public API -------------------------------------------------------

    def is_due_to_emit(self) -> bool:
        """Lockless predicate — does the throttle window allow an emit now?

        Intended as a fast-path hint for hook callers that want to avoid
        crossing the ``asyncio.to_thread`` boundary on every tool call
        when the emit is clearly throttled. False positives are possible
        under concurrent calls (two callers can both observe the same
        stale ``_last_emit``) — the authoritative gate is in :meth:`emit`
        under ``_emit_lock``, which re-checks atomically and ensures only
        one subprocess runs per interval.
        """
        last = self._last_emit
        if last is None:
            return True
        return (self._now() - last) >= self.interval_secs

    def emit(self) -> bool:
        """Emit a ``WORKING`` heartbeat if the interval has elapsed.

        Returns ``True`` when a heartbeat was actually sent, ``False`` when
        throttled or on any send failure. The throttle window advances on
        every *attempt* (not just clean sends) so a transiently-unreachable
        orchestrator does not turn the hook into a per-tool-call subprocess
        storm.
        """
        # Atomic check-and-set: closes the race where two concurrent
        # callers both observe a stale ``_last_emit`` and both pass the
        # gate, spawning duplicate subprocesses.
        with self._emit_lock:
            now = self._now()
            if self._last_emit is not None and (now - self._last_emit) < self.interval_secs:
                return False
            self._last_emit = now

        return self._send()
