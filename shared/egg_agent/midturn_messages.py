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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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

# ``from_role`` values whose messages are operator-authored or
# orchestrator-issued course corrections worth surfacing mid-turn.
# ``overseer`` covers both the MCP ``send_message`` tool (sent as the
# overseer role) and overseer monitor alerts; ``orchestrator`` covers
# deterministic nudges that originate in the orchestrator itself —
# notably the ``brc_confirmation_timeout`` directed wake to a stuck
# producer (``orchestrator/routes/pipelines.py::_send_brc_confirmation_nudge``).
# Without ``orchestrator`` in this set the brc-confirmation-timeout nudge
# would be silently dropped — the producer's poll fetches it, advances
# the cursor past it, and injects nothing, which is exactly the
# silent-drop failure mode #3123 was meant to close. The remaining
# entries cover direct human/operator senders.
_INJECT_FROM_ROLES = frozenset({"overseer", "orchestrator", "human", "operator", "user"})

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
        # Guards the throttle check-and-set so two concurrent PostToolUse
        # callbacks (today serial, but the SDK may move to concurrent
        # delivery) cannot both observe a stale ``_last_poll`` and both
        # spawn a poll subprocess. ``is_due_to_poll`` outside the lock is
        # intentionally lockless — it is a fast-path hint for hook
        # callers; the authoritative gate is in ``poll`` under this lock.
        self._poll_lock = threading.Lock()

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
        except (subprocess.SubprocessError, OSError):  # fmt: skip
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

    def is_due_to_poll(self) -> bool:
        """Lockless predicate — does the throttle window allow a poll now?

        Intended as a fast-path hint for hook callers that want to avoid
        crossing the ``asyncio.to_thread`` boundary on every tool call
        when the poll is clearly throttled. False positives are
        possible under concurrent calls (two callers can both observe
        the same stale ``_last_poll``) — the authoritative gate is in
        ``poll`` under ``_poll_lock``, which re-checks atomically and
        ensures only one fetch runs per interval.
        """
        last = self._last_poll
        if last is None:
            return True
        return (self._now() - last) >= self.interval_secs

    def poll(self) -> str | None:
        """Poll the bus if the interval has elapsed; return a block to inject.

        Returns ``None`` when throttled, on any fetch failure, when no
        new messages arrived, or when the new messages are all
        non-operator traffic (their ids still advance the cursor).
        """
        # Atomic check-and-set: closes the race where two concurrent
        # callers both observe a stale ``_last_poll`` and both pass the
        # gate, spawning duplicate subprocesses.
        with self._poll_lock:
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


# ---------------------------------------------------------------------------
# Intent discrimination (#2270 §2, task-7-2).
#
# Membership in ``_INJECT_FROM_ROLES`` decides whether a message is *surfaced*
# mid-turn; INTENT decides whether it is surfaced as a **binding operator
# directive** or merely as an informational notice. Gating bindingness on
# ``from_role`` alone is the alert-reflection defect: an informational
# ``OVERSEER_ALERT`` (``overseer_restart [info]``, ``stuck-phase-transition``,
# a "Ready to confirm" ``STATUS``) from the overseer/orchestrator gets reflected
# back into a producer's stream and rendered as a BINDING course correction,
# which the producer then (wrongly) treats as an operator order. The fix keys on
# intent:
#
# * ``operator_directive`` — a genuine human/operator message, an explicit
#   directive message_type, or the one orchestrator-issued OVERSEER_ALERT that
#   *is* a directive: the #3123 ``brc_confirmation_timeout`` directed nudge
#   (marked via ``metadata.alert_type``). These RENDER AS BINDING.
# * ``informational`` — everything else from overseer/orchestrator (status,
#   restart/health/anomaly alerts). Surfaced for awareness, NEVER binding.
INTENT_OPERATOR_DIRECTIVE = "operator_directive"
INTENT_INFORMATIONAL = "informational"

# from_role values that are always genuine operator directives.
_OPERATOR_DIRECTIVE_ROLES = frozenset({"human", "operator", "user"})
# message_type values that are directives regardless of sender.
_DIRECTIVE_MESSAGE_TYPES = frozenset({"OPERATOR_DIRECTIVE", "NUDGE"})
# metadata.alert_type values that ride on OVERSEER_ALERT but ARE directives and
# must stay binding — the #3123 brc-confirmation-timeout directed wake.
_DIRECTIVE_ALERT_TYPES = frozenset({"brc_confirmation_timeout"})

# Detector wiring for the slice-1 calibration corpus (alert_reflection rows).
ALERT_REFLECTION_DETECTOR_KEY = "alert_reflection"


def classify_message_intent(message: dict[str, Any]) -> str:
    """Classify a bus message as ``operator_directive`` or ``informational``.

    Prefers an explicit ``intent`` field when the producer of the message set
    one (the calibration corpus does); otherwise infers intent from
    ``from_role`` / ``message_type`` / ``metadata.alert_type``. Unknown shapes
    fall back to ``informational`` — the safe default, since the failure we are
    closing is treating a non-directive as binding.
    """
    explicit = str(message.get("intent") or "").strip().lower()
    if explicit in (INTENT_OPERATOR_DIRECTIVE, INTENT_INFORMATIONAL):
        return explicit

    from_role = str(message.get("from_role") or "").strip().lower()
    if from_role in _OPERATOR_DIRECTIVE_ROLES:
        return INTENT_OPERATOR_DIRECTIVE

    message_type = str(message.get("message_type") or "").strip().upper()
    if message_type in _DIRECTIVE_MESSAGE_TYPES:
        return INTENT_OPERATOR_DIRECTIVE

    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        alert_type = str(metadata.get("alert_type") or "").strip().lower()
        if alert_type in _DIRECTIVE_ALERT_TYPES:
            return INTENT_OPERATOR_DIRECTIVE

    return INTENT_INFORMATIONAL


def _render_message(message: dict[str, Any]) -> list[str]:
    """Render one message into header + body lines."""
    timestamp = str(message.get("timestamp") or "")[:19]
    from_role = str(message.get("from_role") or "?")
    message_type = str(message.get("message_type") or "?")
    subject = str(message.get("subject") or "").strip()
    header = f"### [{timestamp}] from {from_role} ({message_type})"
    if subject:
        header += f": {subject}"
    body = str(message.get("body") or "").strip()
    return [header, "", body if body else "(no body)", ""]


def _render_block(messages: list[dict[str, Any]]) -> str:
    """Render surfaced messages, segmented by intent (#2270 §2, task-7-2).

    Operator directives render under a BINDING header; informational
    overseer/orchestrator broadcasts render under a clearly non-binding header
    so they reach the agent for awareness without being mistaken for an operator
    order. The #3123 brc-confirmation-timeout nudge classifies as a directive
    and therefore stays in the binding section.
    """
    directives: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    for message in messages:
        if classify_message_intent(message) == INTENT_OPERATOR_DIRECTIVE:
            directives.append(message)
        else:
            informational.append(message)

    lines: list[str] = []
    if directives:
        lines += [
            "## Operator directive(s) received mid-turn",
            "",
            "The operator sent the following while you were working. They are "
            "BINDING course corrections — apply them to your remaining work "
            "NOW. If a directive contradicts work you have already done this "
            "turn, stop and reconcile (rework, drop, or adopt as directed) "
            "before proposing; do not finish the contradicted approach first.",
            "",
        ]
        for message in directives:
            lines += _render_message(message)

    if informational:
        lines += [
            "## Informational notices (mid-turn — NOT binding)",
            "",
            "The following are informational overseer/orchestrator broadcasts "
            "(status, restart, health/anomaly alerts) surfaced for your "
            "awareness. They are NOT operator directives: do not treat them as "
            "binding course corrections and do not change your committed "
            "approach on their account. Act only if an actual operator "
            "directive (above) or your own task contract calls for it.",
            "",
        ]
        for message in informational:
            lines += _render_message(message)

    block = "\n".join(lines)
    if len(block) > _RENDERED_BLOCK_MAX_CHARS:
        block = block[:_RENDERED_BLOCK_MAX_CHARS] + _BLOCK_TRUNCATION_SENTINEL
    return block


# ---------------------------------------------------------------------------
# Calibration detector (#2270 slice-1 corpus, ``alert_reflection`` rows).
#
# Lives here, co-located with the intent logic it guards, and is registered into
# the overseer-calibration corpus by the slice-7 test. A frozen structural
# finding (duck-typed on the corpus ``Finding`` protocol) keeps this shared
# module free of an orchestrator import.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AlertReflectionFinding:
    """Structural finding for the alert-reflection corpus rows.

    Exposes exactly the attributes the calibration harness matches on
    (``finding_class`` / ``severity`` / ``requires_adjudication`` plus
    ``evidence`` / ``recommended_action``), so it satisfies the corpus
    ``Finding`` protocol without importing ``health_checks``.
    """

    finding_class: str
    severity: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""
    requires_adjudication: bool = False


def detect_alert_reflection(snapshot: Any) -> _AlertReflectionFinding | None:
    """Fire when an informational alert was rendered as a binding directive.

    The alert-reflection defect: a message whose intent is NOT
    ``operator_directive`` nonetheless got ``rendered_as_binding`` in an agent's
    mid-turn stream. A genuine operator directive rendered as binding, or an
    informational alert correctly left non-binding, are both clean (``None``).
    Deterministic and cheap — ``requires_adjudication=False``.
    """
    messages = getattr(snapshot, "midturn_messages", ()) or ()
    offenders: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if not message.get("rendered_as_binding"):
            continue
        if classify_message_intent(message) == INTENT_OPERATOR_DIRECTIVE:
            continue
        offenders.append(
            {
                "from_role": message.get("from_role"),
                "message_type": message.get("message_type"),
                "subject": message.get("subject"),
                "intent": classify_message_intent(message),
            }
        )
    if not offenders:
        return None
    return _AlertReflectionFinding(
        finding_class="alert_reflection",
        severity="medium",
        evidence={"reflected": offenders, "count": len(offenders)},
        recommended_action=(
            "An informational overseer/orchestrator alert was rendered as a "
            "binding operator directive in an agent's mid-turn stream. Gate "
            "mid-turn injection on message intent, not from_role, so only "
            "genuine operator directives render as binding."
        ),
        requires_adjudication=False,
    )
