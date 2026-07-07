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


def pipeline_level_confirmed_roles(messages: Iterable[Any]) -> set[str]:
    """Roles with a pipeline-level (slice-untagged) CONSENSUS_CONFIRMED.

    Slice-tagged confirmations belong to a per-slice tracker's round and
    say nothing about pipeline-level consensus; counting them is how a
    completed early slice masquerades as whole-phase consensus.
    """
    return {
        m.from_role
        for m in messages
        if m.message_type == "CONSENSUS_CONFIRMED" and message_slice_id(m) is None
    }
