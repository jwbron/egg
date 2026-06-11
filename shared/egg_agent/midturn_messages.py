"""Mid-turn operator message delivery (#3123).

A long producer turn under the BRC event-pump can run 30+ minutes (one
``propose`` invocation implements a whole slice), and the message bus is
only consulted between invocations — so an operator correction sent via
``send_message`` lands after the contradicting work is already done, and
the only remaining lever is a full review cycle. Restarting the agent is
not a safe nudge either (#2806: the consensus wrapper treats producer
restart as permanent death).

This module provides a throttled poller that ``client.py`` wires into the
SDK session as a PostToolUse hook: every tool call gives it a chance to
fire, an interval gate keeps the actual polling cheap (one ``egg-orch
message poll`` subprocess per ``EGG_MIDTURN_MESSAGES_INTERVAL_SECS``),
and any *new* operator-authored messages are rendered as
``additionalContext`` so the directive reaches the model mid-turn.

Scope decisions:

* Only operator-authored traffic is injected (``from_role`` in
  ``_INJECT_FROM_ROLES``). Peer-agent and protocol messages
  (``CONSENSUS_*``, heartbeats) stay on the between-invocation path —
  injecting them would thrash producers with chatter the BRC wrapper
  already sequences.
* The cursor persists across one-shot invocations (file in
  ``EGG_WAIT_CURSOR_DIR``, same back-channel pattern as the ``wait``
  CLI's cursor file, #2323), so a message that lands *between*
  invocations is injected at the start of the next one rather than
  dropped, and nothing is double-delivered.
* First poll ever (no cursor file) seeds the cursor at the stream tip
  WITHOUT injecting: pre-existing history was already visible to the
  agent through the normal channels; this hook is for corrections that
  arrive after work started.
* Fail-soft everywhere: a missing ``egg-orch`` binary, an unreachable
  orchestrator, malformed JSON, or an unwritable cursor file mean "no
  injection this turn" — never an agent failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Default seconds between actual bus polls. Tool calls arrive much more
# often than this; the interval gate makes the hook effectively free
# between polls. 60 s keeps worst-case correction latency around a
# minute — versus the length of the whole remaining turn today.
DEFAULT_POLL_INTERVAL_SECS = 60.0

# Subprocess budget for one poll. The CLI's own non-waiting timeout is
# 15 s; one extra second of headroom avoids racing it.
_POLL_SUBPROCESS_TIMEOUT_SECS = 16

# ``from_role`` values whose messages are operator-authored course
# corrections worth surfacing mid-turn. ``overseer`` covers both the
# MCP ``send_message`` tool (sent as the overseer role) and overseer
# monitor alerts; the rest cover direct human/operator senders.
_INJECT_FROM_ROLES = frozenset({"overseer", "human", "operator", "user"})

# Cap on the rendered injection block. Operator messages are normally
# short directives; a pathological body should not blow up the turn.
_RENDERED_BLOCK_MAX_CHARS = 8000

_BLOCK_TRUNCATION_SENTINEL = (
    "\n…(messages truncated — run `egg-orch message poll` to read the "
    "full backlog before continuing)\n"
)


def is_midturn_messages_disabled() -> bool:
    """Check the ``EGG_MIDTURN_MESSAGES`` escape hatch (default: enabled)."""
    return (os.environ.get("EGG_MIDTURN_MESSAGES") or "").strip().lower() in (
        "false",
        "0",
        "off",
        "disabled",
    )


def _poll_interval_secs() -> float:
    raw = (os.environ.get("EGG_MIDTURN_MESSAGES_INTERVAL_SECS") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_POLL_INTERVAL_SECS


class MidturnMessagePoller:
    """Throttled message-bus poller for mid-turn operator delivery.

    One instance per SDK session; ``poll()`` is called from the
    PostToolUse hook (via ``asyncio.to_thread`` — it runs a subprocess)
    and returns either a rendered markdown block to inject or ``None``.
    """

    def __init__(
        self,
        pipeline_id: str,
        role: str,
        *,
        interval_secs: float | None = None,
        cursor_dir: str | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.role = role
        self.interval_secs = interval_secs if interval_secs is not None else _poll_interval_secs()
        self._now = now
        self._last_poll: float | None = None

        base_dir = cursor_dir or os.environ.get("EGG_WAIT_CURSOR_DIR") or tempfile.gettempdir()
        # Hash the identifiers into the filename so arbitrary pipeline
        # ids / roles can't produce path separators (same defensive
        # stance as the wait CLI's cursor path, #2323).
        digest = hashlib.md5(
            f"{pipeline_id}\x00{role}".encode(), usedforsecurity=False
        ).hexdigest()[:12]
        self._cursor_path = Path(base_dir) / f"egg-midturn-msg-cursor-{digest}"

    # -- cursor -----------------------------------------------------------

    def _read_cursor(self) -> str | None:
        try:
            value = self._cursor_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def _write_cursor(self, message_id: str) -> None:
        try:
            self._cursor_path.write_text(message_id, encoding="utf-8")
        except OSError:
            # Unwritable cursor degrades to "seed again next session" —
            # worst case is a re-injection, not a failure.
            pass

    # -- bus access -------------------------------------------------------

    def _fetch(self, since_id: str | None) -> tuple[list[dict[str, Any]], bool] | None:
        """Run one ``egg-orch message poll`` subprocess.

        Returns ``(messages, since_id_stale)`` or ``None`` on any
        failure (binary missing, non-zero rc, timeout, bad JSON).
        """
        binary = shutil.which("egg-orch")
        if not binary:
            return None
        cmd = [
            binary,
            "message",
            "poll",
            self.pipeline_id,
            "--role",
            self.role,
            "--limit",
            "100",
            "--json",
        ]
        if since_id:
            cmd += ["--since", since_id]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_POLL_SUBPROCESS_TIMEOUT_SECS,
                check=False,
            )
        except subprocess.SubprocessError, OSError:
            return None
        if proc.returncode != 0:
            return None
        try:
            result = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            return None
        if not isinstance(result, dict) or not result.get("success", False):
            return None
        data = result.get("data") or {}
        if not isinstance(data, dict):
            return None
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            raw_messages = []
        messages = [m for m in raw_messages if isinstance(m, dict)]
        return messages, bool(data.get("since_id_stale"))

    # -- public API -------------------------------------------------------

    def poll(self) -> str | None:
        """Poll the bus if the interval has elapsed; return a block to inject.

        Returns ``None`` when throttled, on any fetch failure, when no
        new messages arrived, or when the new messages are all
        non-operator traffic (their ids still advance the cursor).
        """
        now = self._now()
        if self._last_poll is not None and (now - self._last_poll) < self.interval_secs:
            return None
        self._last_poll = now

        cursor = self._read_cursor()
        fetched = self._fetch(since_id=cursor)
        if fetched is None:
            return None
        messages, since_id_stale = fetched

        last_id = ""
        for message in messages:
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                last_id = message_id
        if last_id:
            self._write_cursor(last_id)

        if cursor is None or since_id_stale:
            # First poll (seed at tip) or store-side cursor invalidation
            # (#2464: re-snap rather than replaying an unbounded
            # backlog). Either way: advance, inject nothing.
            return None

        operator_messages = [
            m
            for m in messages
            if str(m.get("from_role") or "").strip().lower() in _INJECT_FROM_ROLES
        ]
        if not operator_messages:
            return None
        return _render_block(operator_messages)


def _render_block(messages: list[dict[str, Any]]) -> str:
    """Render operator messages as the injected context block."""
    lines: list[str] = [
        "## Operator message(s) received mid-turn",
        "",
        "The operator sent the following while you were working. They are "
        "BINDING course corrections — apply them to your remaining work "
        "NOW. If a directive contradicts work you have already done this "
        "turn, stop and reconcile (rework, drop, or adopt as directed) "
        "before proposing; do not finish the contradicted approach first.",
        "",
    ]
    for message in messages:
        timestamp = str(message.get("timestamp") or "")[:19]
        from_role = str(message.get("from_role") or "?")
        message_type = str(message.get("message_type") or "?")
        subject = str(message.get("subject") or "").strip()
        header = f"### [{timestamp}] from {from_role} ({message_type})"
        if subject:
            header += f": {subject}"
        lines.append(header)
        lines.append("")
        body = str(message.get("body") or "").strip()
        lines.append(body if body else "(no body)")
        lines.append("")
    block = "\n".join(lines)
    if len(block) > _RENDERED_BLOCK_MAX_CHARS:
        block = block[:_RENDERED_BLOCK_MAX_CHARS] + _BLOCK_TRUNCATION_SENTINEL
    return block
