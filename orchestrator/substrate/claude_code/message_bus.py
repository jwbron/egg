"""In-process message bus for the Claude Code substrate (#2623).

Delegates to ``orchestrator/message_store.py:200 MessageStore`` — the
existing thread-safe in-memory bus that already powers
``EGG_MESSAGE_STORE_BACKEND=memory`` local-dev runs. Subclassing
rather than re-implementing keeps the bus behavior identical to the
production k3s code path when the latter is configured for the
memory backend, which is exactly the cq-1 contract: both substrates
share BRC mechanics; only the transport differs.

The BRC concurrency invariants (INV-3 stale-version rejection, INV-5
open-NACK barrier, INV-6 ack_commit_sha consistency) live in
``orchestrator/peer_consensus.py`` and
``orchestrator/action_guards.py:631 validate_invariants``. The bus
itself does not enforce them; it only ferries messages. The
behavioral oracle is the existing BRC test suite at
``orchestrator/tests/test_brc_*.py`` (7+ files including
``test_brc_open_nacks_barrier.py`` and
``test_brc_content_validation.py``); tests under TASK-1-8 exercise
the InProcessMessageBus surface against the same oracle.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

from orchestrator.message_store import MessageStore


class InProcessMessageBus(MessageStore):
    """Pure-Python in-process bus satisfying the ``MessageBus``
    protocol.

    Inherits ``add_message`` / ``get_messages`` / blocking-read /
    notify-on-add semantics from ``MessageStore``. This subclass
    exists primarily for type discrimination — callers can check
    ``isinstance(bus, InProcessMessageBus)`` to distinguish the
    Claude Code substrate from the Redis-backed k3s substrate
    without sniffing module names — and to provide a stable seam if
    the in-process bus later needs Claude-Code-specific tweaks
    (e.g., shorter heartbeat ticks, sandbox-aware filtering).

    Why subclass instead of compose: the
    ``orchestrator.message_store.MessageStore`` API is large
    (``get_messages_with_meta``, ``clear``, ``flush_pending_writes``
    and several others) and is consumed directly by
    ``PeerConsensusTracker`` and the orchestrator's wait-loop
    routes. Subclassing avoids re-exporting that surface manually
    and keeps a single source of truth for the message shape.
    """

    def __init__(self) -> None:
        super().__init__()
