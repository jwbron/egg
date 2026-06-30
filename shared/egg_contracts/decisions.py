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

# Single source of truth for how long a HITL question may be before a
# status/gate surface truncates it. Shared by the ``get_status``
# surfacing (``_pending_contract_decisions``) and the phase_gate guard
# (``_outstanding_contract_hitl``) so the same ``cq-N`` renders at one
# consistent length on both surfaces (#3374 review).
CONTRACT_QUESTION_MAX_CHARS = 4_000
CONTRACT_QUESTION_TRUNCATION_SUFFIX = "… (truncated)"


def truncate_question(question: str, max_chars: int = CONTRACT_QUESTION_MAX_CHARS) -> str:
    """Return ``question`` truncated to ``max_chars`` with a suffix marker.

    Single source of truth for the length cap applied when a HITL
    question is echoed onto a status/gate surface, so both surfaces
    truncate identically.
    """
    if len(question) > max_chars:
        return question[:max_chars] + CONTRACT_QUESTION_TRUNCATION_SUFFIX
    return question


# Collapse runs of whitespace so trivially-reformatted re-registrations
# of the same question (extra spaces, a wrapped newline) normalize to the
# same key.
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_question(question: Any) -> str:
    """Return the dedupe-normalized form of a decision question.

    Lower-cased, leading/trailing-stripped, and internal whitespace runs
    collapsed to a single space. Non-string input normalizes to ``""``.
    This is the single source of truth for the equivalence used by
    :func:`find_duplicate_open_question`.
    """
    if not isinstance(question, str):
        return ""
    return _WHITESPACE_RUN.sub(" ", question).strip().lower()


def _field(d: Any, name: str) -> Any:
    """Read ``name`` from a Decision instance or a plain contract dict."""
    if isinstance(d, dict):
        return d.get(name)
    return getattr(d, name, None)


def _find_equivalent_question(
    existing: Iterable[Any],
    question: str,
    phase: Any,
    *,
    resolved: bool,
) -> Any | None:
    """Return the first HITL decision equivalent to ``question`` in the
    requested resolution state.

    Shared scan behind :func:`find_duplicate_open_question` (``resolved``
    is :data:`False`) and :func:`find_resolved_question` (``resolved`` is
    :data:`True`). Matches a decision that is

    - a HITL decision (``type == "hitl"``),
    - in the requested ``resolved`` state, and
    - whose :func:`normalize_question` form equals that of ``question``
      under the same ``phase``.

    ``existing`` may hold :class:`Decision` instances or plain dicts
    (the gateway's JSON contract payload), mirroring :func:`next_cq_id`.
    ``phase`` is compared on its string value so a ``PipelinePhase`` enum,
    its ``.value``, or ``None`` all match consistently.
    """
    target = normalize_question(question)
    if not target:
        return None
    target_phase = getattr(phase, "value", phase)

    for d in existing:
        d_type = _field(d, "type")
        d_type = getattr(d_type, "value", d_type)
        if d_type != "hitl":
            continue
        if bool(_field(d, "resolved")) != resolved:
            continue
        d_phase = _field(d, "phase")
        d_phase = getattr(d_phase, "value", d_phase)
        if d_phase != target_phase:
            continue
        if normalize_question(_field(d, "question")) == target:
            return d
    return None


def find_duplicate_open_question(
    existing: Iterable[Any],
    question: str,
    phase: Any,
) -> Any | None:
    """Return an existing unresolved HITL decision equivalent to ``question``.

    A later phase or a re-run agent that registers a question already
    posed (and not yet answered) should adopt the prior ``cq-N`` rather
    than mint a duplicate (the operator who answers ``cq-1`` should not
    then face an identical ``cq-4``). This scans ``existing`` for the
    first decision that is

    - a HITL decision (``type == "hitl"``),
    - unresolved (``resolved`` is falsy), and
    - whose :func:`normalize_question` form equals that of ``question``
      under the same ``phase``.

    ``existing`` may hold :class:`Decision` instances or plain dicts
    (the gateway's JSON contract payload), mirroring :func:`next_cq_id`.
    ``phase`` is compared on its string value so a ``PipelinePhase`` enum,
    its ``.value``, or ``None`` all match consistently. Returns the
    matching entry (so callers can reuse its id) or ``None``.
    """
    return _find_equivalent_question(existing, question, phase, resolved=False)


def find_resolved_question(
    existing: Iterable[Any],
    question: str,
    phase: Any,
) -> Any | None:
    """Return an existing **resolved** HITL decision equivalent to ``question``.

    The convergence counterpart to :func:`find_duplicate_open_question`.
    When a refine/plan phase re-runs to fold operator resolutions into its
    documents (the converge-before-advance loop, #3392), its agents may
    re-register a question that was already *answered* in a prior round.
    Minting a fresh ``cq-N`` would re-surface an answered decision, so the
    loop would never reach a fixpoint. Adopting the resolved decision
    makes re-registration idempotent and carries the prior answer forward,
    so each round's open-decision set shrinks toward zero (modulo
    genuinely-new questions).

    Same matching rules as :func:`find_duplicate_open_question` except the
    decision must be **resolved** (``resolved`` is truthy). Returns the
    matching entry (so callers can reuse its id and resolution) or
    ``None``.
    """
    return _find_equivalent_question(existing, question, phase, resolved=True)


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
