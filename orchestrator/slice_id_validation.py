"""Canonical ``slice_id`` shape and request-payload extractor.

The orchestrator pins slice ids to the canonical ``slice-<N>`` shape
at every gateway-facing seam where an external caller could supply
one — signal handlers (#2403), the operator-triggered restart route
(#2410), and the gateway-bound branch builders in
``concurrent_executor``. The pattern lives here, in a small shared
module, so each seam validates against the same regex rather than
re-deriving it inline.

The contract-side ``Slice.id`` field
(``shared/egg_contracts/models.py``) accepts the broader pattern
``^(?:slice|phase)-[0-9]+$`` for backward compatibility with
pre-#2137 contracts. The pattern below is intentionally narrower —
only the canonical ``slice-<N>`` shape. The canonicalisation is
performed by the parent ``Contract`` model validator
``Contract._migrate_phases_to_slices`` (mode="wrap" — runs before
per-Slice field validation), which rewrites legacy ``phase-<N>`` ids
to ``slice-<N>`` whenever a contract is loaded via
``Contract.model_validate(json_dict)``. Slices reaching the spawn /
signal / restart path through the contract-loader path are therefore
already canonical. The guarantee does NOT extend to direct ``Slice``
construction (e.g. ``Slice(id="phase-2", ...)``) — pydantic field
validation alone is permissive — so the regex below is doing real
work for any code path that constructs Slices outside Contract
loading (e.g. a future migration tool, a hand-built fixture, or
direct ``model_validate`` on a Slice dict). Such a caller's
``phase-<N>`` slice will be rejected here as it should — the
registry key MUST be canonical so the per-slice tracker can be
looked up, and Job names / worktree ids that embed it MUST be
RFC-1123 safe.
"""

from __future__ import annotations

import re
from typing import Any

SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")


def extract_slice_id(data: dict[str, Any]) -> str | None:
    """Return ``slice_id`` from a request payload, validated.

    Returns ``None`` when no slice scope was supplied (the agent is
    pipeline-level, not slice-scoped). Raises ``ValueError`` when the
    caller supplied a non-empty value that does not match the
    canonical ``slice-<N>`` shape.

    Defense-in-depth: validate at every gateway-facing seam even
    though the spawn-side is already canonical — a future caller
    that forgets the upstream regex must not be able to smuggle path
    separators or shell metacharacters into a tracker registry key,
    a Job name, or a worktree id.
    """
    raw = data.get("slice_id")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not SLICE_ID_PATTERN.fullmatch(raw):
        raise ValueError(f"Invalid slice_id {raw!r}: must match 'slice-<N>'")
    return raw
