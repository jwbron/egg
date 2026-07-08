"""Slice-aware helpers for consensus-message fallbacks (#3542).

The tier-1 consensus checks fall back to scanning the pipeline's message
bus for ``CONSENSUS_CONFIRMED`` when no in-memory tracker is available.
That scan is pipeline-wide, but during a slice-DAG implement phase every
slice's agents write their consensus traffic to the same bus, so the
moment one slice fully confirms, an unscoped scan reports "all roles
confirmed" for the rest of the pipeline's life. That false positive is
what let the aggressive stall recovery mark the implement phase COMPLETE
between slices (the issue-3523 slice-7 to slice-8 transition, and again
60s after an operator phase restart cleared the slice trackers).

Per-slice agents tag every ``CONSENSUS_*`` message with ``slice_id`` in
``metadata`` (the ``_slice_meta`` spread in ``routes/signals``);
pipeline-level messages omit it. The message fallback reconstructs
*pipeline-level* consensus, so it must only count untagged messages,
mirroring ``peer_consensus._message_slice_id`` and the executor-side
skip of the pipeline-wide fallback for slice-scoped trackers (#2535).

The scan must also be phase-scoped: refine, plan, and implement share
the default review graph, so an earlier phase's untagged confirmations
satisfy the current phase's role set verbatim: the same
consensus-complete false positive, hours later, from a phase that
finished legitimately. Every confirm emitter sets ``Message.phase``
(``routes/signals``), so the fallback filters on it, treating a null
phase as matching any phase, the same conservative-match convention
as ``peer_consensus.reconstruct_tracker_from_messages``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def message_slice_id(message: Any) -> str | None:
    """Return the ``slice_id`` a message was tagged with, or ``None``."""
    metadata = getattr(message, "metadata", None) or {}
    if not hasattr(metadata, "get"):
        return None
    return metadata.get("slice_id")


def message_phase(message: Any) -> str | None:
    """Return the pipeline phase a message was sent in, or ``None``."""
    phase = getattr(message, "phase", None)
    if phase is None:
        return None
    return getattr(phase, "value", phase)


def pipeline_level_confirmed_roles(
    messages: Iterable[Any],
    phase: str | None = None,
) -> set[str]:
    """Roles with a pipeline-level (slice-untagged) CONSENSUS_CONFIRMED.

    Slice-tagged confirmations belong to a per-slice tracker's round and
    say nothing about pipeline-level consensus; counting them is how a
    completed early slice masquerades as whole-phase consensus.

    When ``phase`` is given, only confirmations from that phase count;
    a confirmation whose own phase is ``None`` is conservatively treated
    as matching (every emitter sets it, but if one doesn't, include
    rather than drop).
    """
    return {
        m.from_role
        for m in messages
        if m.message_type == "CONSENSUS_CONFIRMED"
        and message_slice_id(m) is None
        and (phase is None or message_phase(m) in (None, phase))
    }
