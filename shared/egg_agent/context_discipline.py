"""Master feature flag for the #3200 BRC context discipline (slice-9, task-9-1).

A SINGLE switch gating the whole context discipline end-to-end:

* the protected-root / queryable-environment split + JIT pull (slice-4/5), and
* the threshold reseed warm-resume substrate (slice-6/8).

``ON`` drives every event-pump role — producers (``coder`` / ``architect`` /
``task_planner`` / ``risk_analyst`` …) and reviewers (``reviewer_code`` /
``reviewer_plan`` / ``reviewer_refine`` …) alike — through the new path: the
per-event composer (``orchestrator.routes.event_prompt.compose_event_prompt``)
renders the role-parameterized protected root with bulk moved to JIT pull, and
the warm-resume gate (``egg_agent.session.session_resume_enabled``) activates so
the slice-8 occupancy-vs-threshold gate can resume-or-reseed. ``OFF`` — the
rollout default — preserves today's full-context INLINE path byte-for-byte.

**Read in ONE place.** This module owns the only read of
``$EGG_CONTEXT_DISCIPLINE``. Every other call site
(``session.session_resume_enabled``, the event-prompt composer's ``jit_pull``
decision) calls :func:`context_discipline_enabled` rather than re-reading the
env var, so the discipline has a single authoritative on/off and no role
hard-codes the new path.

**Subsumes the narrower staging knobs.** Earlier slices shipped each component
behind its own staging switch (``EGG_SESSION_RESUME`` for the warm-resume
substrate). Those remain as finer-grained overrides for staged rollout, but the
master flag subsumes them: turning :func:`context_discipline_enabled` on turns
the whole discipline on regardless of the narrower switches.

**Default OFF, fail-safe.** An unset / blank / unrecognised value reads as OFF
so production stays on the legacy path until an operator opts in. Accepts the
usual truthy spellings (``1`` / ``true`` / ``yes`` / ``on``, case-insensitive),
matching ``session_resume_enabled`` so the two flags share one mental model.
"""

from __future__ import annotations

import os

__all__ = [
    "CONTEXT_DISCIPLINE_ENV",
    "context_discipline_enabled",
]

# The single master switch for the #3200 context discipline. Read ONLY by
# :func:`context_discipline_enabled` — every other consumer goes through that
# function so the env var has exactly one authoritative reader.
CONTEXT_DISCIPLINE_ENV = "EGG_CONTEXT_DISCIPLINE"

# Truthy spellings, matching ``session._TRUTHY`` so the master flag and the
# narrower warm-resume knob parse identically.
_TRUTHY = {"1", "true", "yes", "on"}


def context_discipline_enabled() -> bool:
    """Return whether the #3200 context discipline is enabled (opt-in, default OFF).

    The master switch over ``$EGG_CONTEXT_DISCIPLINE``: ``True`` routes every
    event-pump role through the queryable-environment split + threshold reseed;
    ``False`` (unset / blank / unrecognised — the rollout default) keeps the
    legacy full-context inline path. Accepts ``1`` / ``true`` / ``yes`` / ``on``
    (case-insensitive). Never raises.
    """
    return os.environ.get(CONTEXT_DISCIPLINE_ENV, "").strip().lower() in _TRUTHY
