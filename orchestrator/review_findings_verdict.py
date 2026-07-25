"""Server-side edge verdict computed from structured review findings (#3523, slice-3).

Slice-2 (``shared/egg_contracts/review_findings.py``) defined the versioned
**Finding** schema and its boundary validator but wired nothing into the
verdict path. This module is the S3 determinism-boundary move: the reviewer
emits findings (judgment — what to flag, severity, confidence, prose), and
this **pure, orchestrator-side code** computes the edge verdict (mechanics —
mechanism-level dedup, the ACK/NACK outcome, the convergence signal). The
producer-facing NACK *reason* is rendered from these findings by
``orchestrator/consensus_wrapper.py`` (the shared serial-spine resource), so
the two S3/S4 edits to that file serialise cleanly.

Three documented outcomes (issue #3523 item 1):

* **any blocking finding => NACK.** "Blocking" here means *blocking-eligible*
  — :meth:`Finding.effective_severity` already downgrades a ``blocking``
  finding with no ``failure_scenario`` to advisory, so the vibe-NACK cannot
  reach this function.
* **advisory-only => ACK with obligations.** The advisory findings' optional
  ``pre_merge_obligation`` texts route through the existing conditional-ACK
  path (``ApprovalEntry.pre_merge_condition`` / the ``obligation_*`` fields on
  ``orchestrator/approval_matrix.py``).
* **empty => ACK.** Nothing flagged is a clean ACK.

**Mechanism-level dedup + convergence-as-signal.** Findings from different
lenses that name the same causal mechanism merge into ONE finding carrying the
list of the >=2 producing lenses in ``converged_roles``; convergence raises the
merged finding's confidence one rung. Today that convergence information is
discarded — surfacing it (to the producer via the rendered reason, and to HITL
on escalation) is the point.

**Staged rollout.** The whole path is gated behind ``EGG_REVIEW_FINDINGS_MODE``,
resolved EXACTLY like ``slice_green_gate.green_gate_mode()`` (``off`` default,
unknown => ``off``, ``log`` records the computed-vs-legacy verdict into the BRC
artifacts without acting, ``on`` uses the computed verdict). Everything in this
module is a pure function of its inputs; it never reads/writes matrix state or
the environment except through :func:`review_findings_mode`. The caller (a
later wiring slice) decides — based on the mode — whether to *act* on the
computed verdict or merely *log* it, which is what keeps ``off``/``log``
outcomes byte-identical to the legacy prose-NACK path.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

from egg_contracts.review_findings import (
    Finding,
    FindingConfidence,
    FindingSeverity,
    FindingsPayload,
)

# --- verdict constants -------------------------------------------------------

VERDICT_ACK = "ACK"
VERDICT_NACK = "NACK"


# --- staged-flag resolution (mirrors slice_green_gate.green_gate_mode) --------

# Operator switch for the findings-computed verdict path. Three-state,
# default off during rollout (#3523 S3): "off"/unset => the legacy
# prose-NACK path is authoritative and this module is not consulted for
# real outcomes; "log" => the computed verdict is recorded alongside the
# legacy verdict into the BRC artifacts but never acted on; "on" => the
# computed verdict drives the edge ACK/NACK.
FINDINGS_MODE_ENV_VAR = "EGG_REVIEW_FINDINGS_MODE"

_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_LOG_ONLY_VALUES = frozenset({"log", "log-only", "log_only"})


def review_findings_mode() -> Literal["off", "log", "on"]:
    """Resolve the operator switch to one of ``off`` / ``log`` / ``on``.

    Resolved EXACTLY like ``slice_green_gate.green_gate_mode()``: unknown
    values resolve to ``off`` so an operator typo degrades to "legacy path
    unchanged", never to "computed verdict silently drives consensus".
    """
    raw = os.environ.get(FINDINGS_MODE_ENV_VAR, "off").strip().lower()
    if raw in _ENABLED_VALUES:
        return "on"
    if raw in _LOG_ONLY_VALUES:
        return "log"
    return "off"


# --- computed verdict result -------------------------------------------------


@dataclass(frozen=True)
class ComputedVerdict:
    """The edge verdict computed from a reviewer's findings payload.

    Pure data. ``findings`` is the merged/deduped set (mechanism-level dedup
    already applied); ``blocking_findings`` and ``advisory_findings`` partition
    it by :meth:`Finding.effective_severity`. ``obligations`` are the
    non-empty ``pre_merge_obligation`` texts carried by the advisory findings,
    which the caller attaches as conditional-ACK ``pre_merge_condition``.
    """

    verdict: str
    findings: list[Finding] = field(default_factory=list)
    blocking_findings: list[Finding] = field(default_factory=list)
    advisory_findings: list[Finding] = field(default_factory=list)
    obligations: list[str] = field(default_factory=list)

    @property
    def is_nack(self) -> bool:
        """True when the computed verdict blocks the producer."""
        return self.verdict == VERDICT_NACK

    @property
    def converged_findings(self) -> list[Finding]:
        """Merged findings backed by >=2 producing lenses (convergence signal)."""
        return [f for f in self.findings if len(f.converged_roles) >= 2]

    @property
    def obligation_text(self) -> str:
        """The advisory obligations joined into one conditional-ACK condition.

        Empty string when there are no obligations — an unconditional ACK.
        """
        return "\n".join(self.obligations)


# --- mechanism-level dedup ---------------------------------------------------

_CONFIDENCE_ORDER: tuple[FindingConfidence, ...] = (
    FindingConfidence.LOW,
    FindingConfidence.MEDIUM,
    FindingConfidence.HIGH,
)


def _raise_confidence(confidence: FindingConfidence) -> FindingConfidence:
    """Bump a confidence one rung, saturating at ``high``.

    Convergence (>=2 independent lenses naming the same mechanism) is
    corroborating evidence, so the merged finding is at least as confident as
    its most confident constituent and one rung higher when possible.
    """
    idx = _CONFIDENCE_ORDER.index(confidence)
    return _CONFIDENCE_ORDER[min(idx + 1, len(_CONFIDENCE_ORDER) - 1)]


def _normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace for a stable dedup key."""
    return " ".join(text.lower().split())


def _mechanism_key(finding: Finding) -> tuple[Any, ...]:
    """Deterministic key identifying the causal mechanism a finding names.

    Code owns *mechanics*, not judgment, so the key is derived only from
    concrete finding fields — never a semantic guess:

    1. An explicit ``mechanism`` tag (an additive extra field a reviewer may
       set, tolerated by the schema's ``extra="allow"``) is authoritative when
       present. This is the intended convergence hook: two lenses that agree
       on a mechanism id merge regardless of where they anchored.
    2. Otherwise a concrete file anchor (``path`` + exact line range): findings
       pinned to the same location are treated as the same mechanism.
    3. Otherwise (slice-level / unanchored) the normalized ``summary``.
    """
    mech = getattr(finding, "mechanism", None)
    if isinstance(mech, str) and mech.strip():
        return ("mechanism", mech.strip().lower())
    anchor = finding.anchor
    if anchor.path and not anchor.slice_level:
        return ("anchor", anchor.path, anchor.line_start, anchor.line_end)
    return ("summary", _normalize_text(finding.summary))


def _severity_rank(finding: Finding) -> int:
    """1 for a blocking-eligible finding, else 0 (drives representative pick)."""
    return 1 if finding.effective_severity() == FindingSeverity.BLOCKING else 0


def _confidence_rank(confidence: FindingConfidence) -> int:
    return _CONFIDENCE_ORDER.index(confidence)


def merge_findings_by_mechanism(findings: list[Finding]) -> list[Finding]:
    """Merge findings naming the same causal mechanism into one each.

    Deterministic and pure. Groups by :func:`_mechanism_key`, preserving
    first-seen group order for a stable output. Within a group the
    representative is the most severe (blocking-eligible outranks advisory),
    then most confident, then earliest — so a merged group is blocking iff any
    constituent was blocking-eligible. When the group draws on >=2 DISTINCT
    producing lenses, the representative records them in ``converged_roles``
    (sorted) and has its confidence raised one rung. A group from a single lens
    (a reviewer filing the same mechanism twice) still dedupes to one finding
    but records no convergence.
    """
    groups: OrderedDict[tuple[Any, ...], list[tuple[int, Finding]]] = OrderedDict()
    for idx, finding in enumerate(findings):
        groups.setdefault(_mechanism_key(finding), []).append((idx, finding))

    merged: list[Finding] = []
    for group in groups.values():
        # Representative: highest severity, then highest confidence, then
        # earliest index (negated so max() prefers the earliest on a tie).
        _, representative = max(
            group,
            key=lambda item: (
                _severity_rank(item[1]),
                _confidence_rank(item[1].confidence),
                -item[0],
            ),
        )
        roles = sorted({f.role for _, f in group})
        if len(roles) >= 2:
            merged.append(
                representative.model_copy(
                    update={
                        "converged_roles": roles,
                        "confidence": _raise_confidence(representative.confidence),
                    }
                )
            )
        else:
            merged.append(representative)
    return merged


# --- verdict computation -----------------------------------------------------


def compute_verdict(payload: FindingsPayload) -> ComputedVerdict:
    """Compute the edge verdict from a reviewer's findings payload (pure).

    The three documented outcomes (#3523 item 1):

    * any blocking-eligible finding => ``NACK``,
    * advisory-only => ``ACK`` (advisory ``pre_merge_obligation`` texts become
      conditional-ACK obligations),
    * empty => ``ACK``.

    Mechanism-level dedup runs first, so convergence raises confidence and
    duplicate mechanisms collapse before the verdict is decided.
    """
    merged = merge_findings_by_mechanism(list(payload.findings))
    blocking = [f for f in merged if f.effective_severity() == FindingSeverity.BLOCKING]
    advisory = [f for f in merged if f.effective_severity() == FindingSeverity.ADVISORY]
    obligations = [
        f.pre_merge_obligation.strip()
        for f in advisory
        if f.pre_merge_obligation and f.pre_merge_obligation.strip()
    ]
    verdict = VERDICT_NACK if blocking else VERDICT_ACK
    return ComputedVerdict(
        verdict=verdict,
        findings=merged,
        blocking_findings=blocking,
        advisory_findings=advisory,
        obligations=obligations,
    )


# --- log-mode comparison record ----------------------------------------------


def verdict_log_record(
    payload: FindingsPayload,
    computed: ComputedVerdict,
    *,
    legacy_verdict: str | None = None,
    legacy_reason: str | None = None,
) -> dict[str, Any]:
    """A JSON-serializable computed-vs-legacy record for ``log`` mode (pure).

    In ``log`` mode the caller records this into the BRC artifacts without
    acting on the computed verdict — so an operator can compare what the
    findings-computed path *would* have decided against the legacy prose-NACK
    outcome before flipping the flag to ``on``. ``agrees`` is ``None`` when no
    legacy verdict is supplied to compare against.
    """
    agrees: bool | None = None
    if legacy_verdict is not None:
        agrees = legacy_verdict.strip().upper() == computed.verdict

    return {
        "mode": "log",
        "role": payload.role,
        "computed_verdict": computed.verdict,
        "legacy_verdict": legacy_verdict,
        "verdicts_agree": agrees,
        "legacy_reason": legacy_reason,
        "blocking_count": len(computed.blocking_findings),
        "advisory_count": len(computed.advisory_findings),
        "obligation_count": len(computed.obligations),
        "converged": [
            {"id": f.id, "converged_roles": list(f.converged_roles)}
            for f in computed.converged_findings
        ],
        "findings": [f.to_dict() for f in computed.findings],
    }


__all__ = [
    "FINDINGS_MODE_ENV_VAR",
    "VERDICT_ACK",
    "VERDICT_NACK",
    "ComputedVerdict",
    "compute_verdict",
    "merge_findings_by_mechanism",
    "review_findings_mode",
    "verdict_log_record",
]
