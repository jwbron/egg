"""Inter-agent message-bus protocol for the substrate-swap spike (#2623).

See ``docs/architecture/claude-code-substrate.md`` for the broader
substrate model. This module defines the ``MessageBus`` protocol every
substrate implementation must satisfy.

The shape mirrors the existing
``orchestrator/message_store.py:200 MessageStore`` semantics so the
in-process Claude Code implementation can subclass or delegate to it
and the k3s/Redis-backed implementation continues to work unchanged.

The BRC concurrency invariants (INV-1..INV-6) from
``orchestrator/action_guards.py:631 validate_invariants`` are bus-
implementation-independent — the in-process bus must preserve them
just as the Redis-backed bus does. ``orchestrator/peer_consensus.py``
holds the invariant logic; the bus only ferries messages.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class MessageBus(Protocol):
    """Protocol every substrate message bus must satisfy.

    The shape matches the public surface of
    ``orchestrator.message_store.MessageStore`` for drop-in
    interchangeability.
    """

    def add_message(self, message: object) -> object:
        """Append a message to the bus.

        Args:
            message: A ``Message`` instance (see
                ``orchestrator/message_store.py``). Typed as ``object``
                in the protocol to avoid a circular import; concrete
                implementations should accept and return
                ``Message``.

        Returns:
            The stored message (typically the same instance, possibly
            with a generated ``id`` populated).
        """
        ...

    def get_messages(
        self,
        pipeline_id: str,
        *,
        role: str | None = None,
        since_id: str | None = None,
        limit: int = 100,
        wait: int = 0,
        wait_for_types: Sequence[str] | None = None,
        from_role: str | None = None,
        from_tip: bool = False,
    ) -> list[object]:
        """Return messages for ``pipeline_id``, optionally filtered.

        See ``orchestrator.message_store.MessageStore.get_messages``
        for the full semantics. When ``wait > 0``, the implementation
        is expected to block on a condition variable (in-process) or
        equivalent (e.g. Redis ``XREAD BLOCK``) until a matching
        message arrives or the timeout expires.
        """
        ...
