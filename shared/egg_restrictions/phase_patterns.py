"""Phase-scoped file-write patterns — the *second* of egg's two push-gate layers.

Every git push through the gateway is checked against two independent filters,
both of which must allow every changed file or the push is rejected:

1. **Role layer** — :mod:`egg_restrictions.patterns` (:data:`AGENT_PATTERNS`):
   *can role R ever write path P?*
2. **Phase layer** — this module (:data:`PHASE_FILE_PATTERNS`): *is path P
   writable during pipeline phase φ, regardless of role?*

The gateway's authoritative phase enforcement lives in
``gateway/phase_filter.py`` (``PhaseFileRestriction.is_file_allowed`` /
``PhaseFilter.check_phase_file_restrictions``), configured by
``.egg/phase-permissions.json``. The ``check_file_restriction`` MCP tool runs
in the sandbox — nowhere near the gateway — and historically consulted only the
role layer, so it reported ``can_write: true`` for paths the phase gate rejects
at push time (e.g. ``.egg-state/drafts/*-plan.md`` during the *refine* phase,
which is reserved to the *plan* phase). That phase-blind false positive drove a
NACK loop in #2968: a reviewer trusted the tool and NACKed a producer for a
"false gateway claim" that was in fact a true phase-gate block.

This module mirrors the gateway's phase data and matching logic so phase-blind
callers can predict push acceptance. The mirror is kept honest by a parity test
(``gateway/tests/test_phase_filter_restrictions.py``) that compares this module
against the real :class:`PhaseFilter` for every phase. A future consolidation —
parallel to #1903, which made ``patterns.py`` the single source of truth for the
role layer — could collapse the two by having ``gateway/phase_filter.py`` derive
from here and dropping the ``phase_file_restrictions`` key from the JSON; that
touches the security-critical push path and is intentionally out of scope here.

Phases with no configured restriction (and ``apply``, whose live gateway config
carries no row) are unrestricted at this layer. The legacy ``pr`` phase was
hard-removed in #2777 and is not a :class:`PipelinePhase`.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from egg_contracts.models import PipelinePhase

from .matchers import match_pattern

__all__ = [
    "PHASE_FILE_PATTERNS",
    "PhaseFilePattern",
    "phase_file_verdict",
]


@dataclass(frozen=True)
class PhaseFilePattern:
    """File-write restriction for a single pipeline phase.

    Mirrors ``gateway/phase_filter.py``'s ``PhaseFileRestriction``:

    - ``allowed_patterns``: if non-empty, a file must match one of these (the
      sentinel ``"*"`` short-circuits to allow everything).
    - ``blocked_patterns``: files matching these are always rejected, and are
      checked first so a block beats an allow.
    """

    allowed_patterns: tuple[str, ...] = ()
    blocked_patterns: tuple[str, ...] = ()
    description: str = ""

    def is_file_allowed(self, file_path: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for ``file_path`` under this phase.

        Logic is a 1:1 mirror of ``PhaseFileRestriction.is_file_allowed`` in
        ``gateway/phase_filter.py`` — keep them in lockstep (the parity test
        enforces it).
        """
        try:
            normalized = _normalize_path(file_path)
        except ValueError as exc:
            # Paths that escape the repository are never allowed.
            return False, str(exc)

        # Blocked patterns first — an explicit block beats any allow.
        for pattern in self.blocked_patterns:
            if match_pattern(normalized, pattern):
                return False, f"File '{file_path}' matches blocked pattern '{pattern}'"

        # A non-empty allow list is a strict whitelist.
        if self.allowed_patterns:
            if "*" in self.allowed_patterns:
                return True, "All files allowed"
            for pattern in self.allowed_patterns:
                if match_pattern(normalized, pattern):
                    return True, f"File '{file_path}' matches allowed pattern '{pattern}'"
            return False, f"File '{file_path}' does not match any allowed pattern"

        # No allow list = allow by default (only blocked patterns matter).
        return True, "No explicit restrictions"


def _normalize_path(file_path: str) -> str:
    """Mirror of ``PhaseFileRestriction._normalize_path`` in the gateway.

    Resolves ``.``/``..`` and rejects paths that escape the repository.
    """
    normalized = posixpath.normpath(file_path)
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"Invalid path escapes repository: {file_path}")
    return normalized


# Single source of truth for the phase layer as consumed by phase-blind callers.
# These rows MUST stay equivalent to the live gateway config
# (``.egg/phase-permissions.json`` → ``PhaseFilter``); the parity test in
# ``gateway/tests/test_phase_filter_restrictions.py`` fails CI on drift.
#
# Only phases with real restrictions appear here. ``apply`` is intentionally
# absent: the deployed JSON carries no ``apply`` row, so the live gateway leaves
# it unrestricted at the phase layer. (The Python *fallback* in
# ``phase_filter.py`` does restrict ``apply`` per #1557, but that branch only
# runs when the JSON is absent, which is never the case in production — so the
# fallback's apply rule is currently dead. Resolving that divergence belongs to
# the consolidation follow-up, not here.)
PHASE_FILE_PATTERNS: dict[str, PhaseFilePattern] = {
    "refine": PhaseFilePattern(
        allowed_patterns=(
            ".egg-state/drafts/*analysis*",
            ".egg-state/checkpoints/*",
            ".egg-state/agent-outputs/*",
            ".egg-state/reviews/*",
            ".egg-state/agent-anchors/*",
        ),
        description=(
            "Refine phase can only push analysis drafts, checkpoints, "
            "agent outputs, reviews, and agent anchors "
            "(contracts go through the contract API, not git — #2979)"
        ),
    ),
    "plan": PhaseFilePattern(
        allowed_patterns=(
            ".egg-state/drafts/*plan*",
            ".egg-state/checkpoints/*",
            ".egg-state/agent-outputs/*",
            ".egg-state/reviews/*",
            ".egg-state/agent-anchors/*",
        ),
        description=(
            "Plan phase can only push plan drafts, checkpoints, "
            "agent outputs, reviews, and agent anchors "
            "(contracts go through the contract API, not git — #2979)"
        ),
    ),
    "implement": PhaseFilePattern(
        blocked_patterns=(
            ".egg-state/contracts/*",
            ".egg-state/drafts/*",
            ".egg-state/pipelines/*",
            ".egg-state/reviews/*",
        ),
        description=(
            "Implement phase can push code but not .egg-state/ "
            "(except checkpoints, agent-outputs, and agent-anchors)"
        ),
    ),
}


def phase_file_verdict(phase: str | None, file_path: str) -> tuple[bool, str | None]:
    """Return ``(allowed, block_reason)`` for ``file_path`` under ``phase``.

    ``block_reason`` is a human-readable string only when ``allowed`` is
    ``False``; it is ``None`` when the file is allowed (or when no phase-layer
    restriction applies).

    Behaviour mirrors ``PhaseFilter.check_phase_file_restrictions`` in
    ``gateway/phase_filter.py``:

    - ``None`` / empty string ⇒ no phase context, no-op ``(True, None)``. This
      keeps a phase-less caller (no ``EGG_PHASE``) behaving exactly as the
      role-only check did before #2968. Note that the gateway itself would
      fail closed on an explicit ``""`` (``PipelinePhase("")`` raises), so the
      empty-string branch is a small intentional divergence. It is unreachable
      in practice because every live caller normalises ``""`` to ``None``
      before it reaches this function (``restrictions.py`` does
      ``req.get("phase") or get_phase()``, and ``get_phase()`` returns
      ``None`` for an empty ``EGG_PHASE``); if a future caller ever exposes
      ``phase_file_verdict`` to a path that doesn't pre-normalise, drop the
      ``or empty`` branch so ``""`` falls through to the ``ValueError``
      handler below and fails closed like the gateway.
    - A string the canonical :class:`PipelinePhase` enum doesn't recognise
      (e.g. ``"IMPLEMENT"``, ``"unknown"``, the dead ``"pr"`` from #2777) ⇒
      **fail closed** ``(False, reason)`` — the gateway would reject the push
      with ``"Unknown phase ... blocking by default"``, and the mirror does
      the same so an off-canonical caller can't slip a false ``can_write:
      true`` through. In production the orchestrator always exports the
      canonical lowercase ``EGG_PHASE`` (``kubernetes_spawner.py``), so this
      path only fires on a manual / test caller passing a bad string.
    - A canonical phase with no configured restriction (currently ``apply``,
      whose deployed JSON carries no row) ⇒ ``(True, None)`` — matches the
      gateway's "no phase file restrictions for phase" fall-through.
    - Otherwise the mirror's :class:`PhaseFilePattern` is evaluated and its
      verdict returned.
    """
    if not phase:
        return True, None

    try:
        canonical = PipelinePhase(phase)
    except ValueError:
        # Match the gateway's security stance for off-canonical phase strings.
        return False, (
            f"Unknown phase {phase!r}: phase-layer gate fails closed "
            "(matches gateway/phase_filter.py)"
        )

    pattern = PHASE_FILE_PATTERNS.get(canonical.value)
    if pattern is None:
        return True, None
    allowed, reason = pattern.is_file_allowed(file_path)
    return allowed, (None if allowed else reason)
