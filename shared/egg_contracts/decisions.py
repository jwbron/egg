"""Helpers for allocating contract ``Decision.id`` values.

Two prefixes share the ``Decision.id`` namespace (validated by the
``^(decision|cq)-[0-9]+$`` pattern on ``Decision.id`` in :mod:`models`):

- ``decision-N`` — pipeline-side phase_gate writes mirrored into the
  contract by the orchestrator's bridge. Allocated by the orchestrator's
  ``HITLDecision`` queue (see ``orchestrator/routes/pipelines.py``).
- ``cq-N`` — agent-registered contract questions (``register_open_question``,
  ``_build_hitl_decision`` for impasse escalations). Allocated by
  :func:`next_cq_id`.

Splitting the prefixes prevents the collision in #2616 where both
allocators counted from ``len(decisions)+1`` and drifted after the
bridge mirrored contract decisions into the pipeline queue and the
phase_gate consumed the next pipeline ID.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Single source of truth for the ``cq-N`` regex. The Pydantic field
# pattern on ``Decision.id`` (``^(decision|cq)-[0-9]+$``) accepts both
# prefixes; this regex matches only ``cq-N`` so the counter stays
# stable as legacy ``decision-N`` entries come and go.
CQ_ID_PATTERN = re.compile(r"^cq-([0-9]+)$")


def next_cq_id(existing: Iterable[Any]) -> str:
    """Return the next ``cq-N`` id, ignoring non-``cq-`` ids in ``existing``.

    ``existing`` may be any iterable of :class:`Decision` instances,
    plain dicts (from the gateway's JSON contract payload), or any
    object exposing an ``id`` attribute. Entries whose id is missing,
    ``None``, or does not match ``cq-N`` are skipped — so legacy
    ``decision-N`` entries written by the pipeline-side bridge do not
    perturb the counter.
    """
    nums: list[int] = []
    for d in existing:
        if isinstance(d, dict):
            raw = d.get("id")
        else:
            raw = getattr(d, "id", None)
        if not isinstance(raw, str):
            continue
        m = CQ_ID_PATTERN.match(raw)
        if m:
            nums.append(int(m.group(1)))
    return f"cq-{max(nums, default=0) + 1}"
