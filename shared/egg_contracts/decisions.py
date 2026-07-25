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

import hashlib
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


# Loose in-prose form of the ``cq-N`` id, for scanning draft text. Unlike
# :data:`CQ_ID_PATTERN` (full-string match for validating a candidate id)
# this matches anywhere a draft cites a decision — a bare ``cq-3``, the
# ``<!-- egg-hitl-decision id=cq-3 -->`` marker emitted by
# ``egg-contract add-decision --format markdown``, or prose like
# "(see cq-3)".
CQ_CITATION_PATTERN = re.compile(r"\bcq-[0-9]+\b")


def extract_cq_citations(text: Any) -> set[str]:
    """Return every ``cq-N`` id cited anywhere in ``text``.

    Used by the decision-ledger citation check (#3390): a draft section
    that raises or commits to an operator-grade decision must cite the
    registered ``cq-N`` backing it, and the ``--format markdown`` output
    of ``egg-contract add-decision`` embeds the id, so a producer that
    followed the registration flow passes this for free. Non-string
    input yields the empty set.
    """
    if not isinstance(text, str):
        return set()
    return set(CQ_CITATION_PATTERN.findall(text))


# Valid dispositions for a considered-but-not-registered decision
# candidate (#3526). ``not_operator_grade``: a design call the
# planner/implementer makes on its own; ``deferred_to_plan``: potentially
# operator-grade, but better asked once the plan phase has made the design
# concrete (the orchestrator carries these into the plan prompt as
# pre-seeded candidates the planner must register or disposition).
CANDIDATE_DISPOSITIONS = frozenset({"not_operator_grade", "deferred_to_plan"})


def candidate_considered_errors(candidates_considered: Any) -> list[str]:
    """Validate the ``candidates_considered`` attestation field (#3526).

    Each entry must be a mapping with a non-empty ``question``, a
    ``disposition`` in :data:`CANDIDATE_DISPOSITIONS`, and a non-empty
    ``why``. Returns human-readable error strings; empty means valid.
    ``None`` (field absent) is valid; presence requirements are the
    caller's policy (see :func:`decision_attestation_errors`).
    """
    if candidates_considered is None:
        return []
    if not isinstance(candidates_considered, list):
        return [
            "candidates_considered must be a list of "
            "{question, disposition, why} entries "
            f"(got {type(candidates_considered).__name__})"
        ]
    errors: list[str] = []
    for i, raw in enumerate(candidates_considered):
        if isinstance(raw, dict):
            question = raw.get("question")
            disposition = raw.get("disposition")
            why = raw.get("why")
        else:
            question = getattr(raw, "question", None)
            disposition = getattr(raw, "disposition", None)
            why = getattr(raw, "why", None)
        if not isinstance(question, str) or not question.strip():
            errors.append(f"candidates_considered[{i}] is missing a non-empty question")
        disposition_value = getattr(disposition, "value", disposition)
        if disposition_value not in CANDIDATE_DISPOSITIONS:
            errors.append(
                f"candidates_considered[{i}] disposition {disposition_value!r} is not one of "
                f"{sorted(CANDIDATE_DISPOSITIONS)}"
            )
        if not isinstance(why, str) or not why.strip():
            errors.append(
                f"candidates_considered[{i}] is missing a non-empty why "
                "(one sentence on why this is not an operator decision)"
            )
    return errors


# Stable identity for a refine-deferred question (#3564). The plan phase
# receives refine's ``deferred_to_plan`` candidates with a ``dq-<hash>`` id
# derived from the normalized question text; the plan producer echoes each
# id in its attestation's ``deferred_resolutions``, and the propose-time
# gate recomputes the same ids to cross-check coverage. Content-derived
# (not ordinal) so the id is stable no matter how the candidate list is
# recovered, and exact id matching stays safe while the planner freely
# reframes the question text.
DQ_ID_PATTERN = re.compile(r"^dq-[0-9a-f]{8}$")


def deferred_question_id(question: Any) -> str:
    """Return the stable ``dq-<hash>`` id for a deferred question (#3564).

    First 8 hex chars of the SHA-256 of the :func:`normalize_question`
    form, ``dq-`` prefixed. Single source of truth for the identity the
    plan prompt renders and the propose-time coverage gate recomputes —
    both sides must call this so a whitespace/case reflow of the refine
    attestation cannot break the match.
    """
    digest = hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()
    return f"dq-{digest[:8]}"


# Valid resolutions for a refine-deferred question echoed in a plan
# attestation (#3564). ``registered``: the planner registered it (possibly
# reframed) as a ``cq-N``; ``not_operator_grade``: the plan's design
# dissolved it into a call the planner/implementer owns.
DEFERRED_RESOLUTION_KINDS = frozenset({"registered", "not_operator_grade"})


def deferred_resolution_errors(deferred_resolutions: Any) -> list[str]:
    """Validate the ``deferred_resolutions`` attestation field shape (#3564).

    Each entry must be a mapping with a ``deferred_id`` matching
    :data:`DQ_ID_PATTERN`, a ``resolution`` in
    :data:`DEFERRED_RESOLUTION_KINDS`, and the resolution's payload: a
    ``cq`` id (``cq-N``) when ``registered``, a non-empty ``why`` when
    ``not_operator_grade``. Returns human-readable error strings; empty
    means valid. ``None`` (field absent) is valid — whether coverage is
    *required* is the propose-time gate's policy, not a shape concern.
    """
    if deferred_resolutions is None:
        return []
    if not isinstance(deferred_resolutions, list):
        return [
            "deferred_resolutions must be a list of "
            "{deferred_id, resolution, cq|why} entries "
            f"(got {type(deferred_resolutions).__name__})"
        ]
    errors: list[str] = []
    for i, raw in enumerate(deferred_resolutions):
        if isinstance(raw, dict):
            deferred_id = raw.get("deferred_id")
            resolution = raw.get("resolution")
            cq = raw.get("cq")
            why = raw.get("why")
        else:
            deferred_id = getattr(raw, "deferred_id", None)
            resolution = getattr(raw, "resolution", None)
            cq = getattr(raw, "cq", None)
            why = getattr(raw, "why", None)
        if not isinstance(deferred_id, str) or not DQ_ID_PATTERN.match(deferred_id):
            errors.append(
                f"deferred_resolutions[{i}] deferred_id {deferred_id!r} is not a "
                "valid dq-<hash> id (copy it verbatim from the 'Deferred from "
                "refine' section of your prompt)"
            )
        if resolution not in DEFERRED_RESOLUTION_KINDS:
            errors.append(
                f"deferred_resolutions[{i}] resolution {resolution!r} is not one of "
                f"{sorted(DEFERRED_RESOLUTION_KINDS)}"
            )
        if resolution == "registered" and (not isinstance(cq, str) or not CQ_ID_PATTERN.match(cq)):
            errors.append(
                f"deferred_resolutions[{i}] is resolution 'registered' but its cq "
                f"{cq!r} is not a valid cq-N id (the id returned by "
                "`egg-contract add-decision`)"
            )
        if resolution == "not_operator_grade" and (not isinstance(why, str) or not why.strip()):
            errors.append(
                f"deferred_resolutions[{i}] is resolution 'not_operator_grade' but "
                "is missing a non-empty why (one sentence on how the design "
                "dissolved the question)"
            )
    return errors


def decision_attestation_errors(
    decisions_registered: Any,
    no_decisions_rationale: Any,
    candidates_considered: Any = None,
    deferred_resolutions: Any = None,
) -> list[str]:
    """Validate the decision-ledger attestation fields (#3390, #3526, #3564).

    A refine/plan producer's proposal attestation must carry exactly one
    of:

    - ``decisions_registered``: a non-empty list of ``cq-N`` ids — every
      HITL decision the producer registered this phase, or
    - ``no_decisions_rationale``: a non-empty string recording *why* the
      phase deliberately raises no operator decisions.

    The explicit-none form additionally requires ``candidates_considered``
    (#3526): at least one {question, disposition, why} entry enumerating
    the decision candidates the producer weighed and dispositioned away.
    A single free-form rationale paragraph proved trivially satisfiable;
    agents learned to fold every open choice into prose and attest
    "explicitly none"; so the empty ledger must now name what was
    considered, a form that is harder to satisfy vacuously.
    ``candidates_considered`` may also accompany ``decisions_registered``
    (some choices registered, others dispositioned away).

    A plan producer additionally echoes refine's deferred questions in
    ``deferred_resolutions`` (#3564) — shape-validated here via
    :func:`deferred_resolution_errors`; whether every deferred question is
    *covered* is the propose-time gate's cross-check (it needs the message
    store, which this pure helper must not touch).

    This is the single source of truth for that shape, shared by the
    orchestrator's Pydantic attestation model and the propose-time
    signal validator so the two layers cannot drift. Returns a list of
    human-readable error strings; empty means valid.
    """
    errors: list[str] = []

    ids: list[Any] = []
    if decisions_registered is None:
        pass
    elif isinstance(decisions_registered, list):
        ids = decisions_registered
    else:
        errors.append(
            "decisions_registered must be a list of cq-N id strings "
            f"(got {type(decisions_registered).__name__})"
        )
        return errors

    rationale = no_decisions_rationale if isinstance(no_decisions_rationale, str) else ""
    if no_decisions_rationale is not None and not isinstance(no_decisions_rationale, str):
        errors.append(
            f"no_decisions_rationale must be a string (got {type(no_decisions_rationale).__name__})"
        )
        return errors

    candidate_errors = candidate_considered_errors(candidates_considered)
    deferred_errors = deferred_resolution_errors(deferred_resolutions)
    if candidate_errors or deferred_errors:
        errors.extend(candidate_errors)
        errors.extend(deferred_errors)
        return errors

    has_ids = bool(ids)
    has_rationale = bool(rationale.strip())
    has_candidates = isinstance(candidates_considered, list) and bool(candidates_considered)
    if has_ids and has_rationale:
        errors.append(
            "attestation carries both decisions_registered and "
            "no_decisions_rationale — these are mutually exclusive. If you "
            "registered decisions, list them and drop the rationale; if the "
            "phase deliberately raises none, keep only the rationale."
        )
    if not has_ids and not has_rationale:
        errors.append(
            "attestation must carry either decisions_registered (the cq-N ids "
            "you registered via `egg-contract add-decision` / "
            "`mcp__sdlc__register_open_question`) or a non-empty "
            "no_decisions_rationale explaining why this phase deliberately "
            "raises no operator decisions."
        )
    if has_rationale and not has_ids and not has_candidates:
        errors.append(
            "an explicit-none ledger must enumerate the decision candidates "
            "it considered (#3526): pass candidates_considered, one "
            "{question, disposition, why} entry per open choice you weighed "
            "and dispositioned away (dispositions: 'not_operator_grade' for "
            "design calls the planner/implementer owns, 'deferred_to_plan' "
            "for choices better asked once the plan is concrete; via the "
            'CLI: repeated `--considered "<disposition> :: <question> :: '
            '<why>"`). A rationale with no named candidates is '
            "indistinguishable from not having looked."
        )
    for raw in ids:
        if not isinstance(raw, str) or not CQ_ID_PATTERN.match(raw):
            errors.append(
                f"decisions_registered entry {raw!r} is not a valid cq-N id "
                "(expected e.g. 'cq-1' — the id returned by "
                "`egg-contract add-decision`)"
            )
    return errors


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
