"""Supervision policy constants shared by the orchestrator event loop and
the consensus wrapper (#3138, #3064 slice-3).

The extract here prevents a fork between the loop (orchestrator side)
and the pod wrapper (bash template), so the values are always in sync
and a single-line edit here changes both::
    orchestrator.event_loop    — reads constants for supervision,
                                 backoff, streak tracking
    orchestrator.consensus_wrapper — embeds the same constants in the bash
                                 event-pump template
"""

# ---------------------------------------------------------------------------
# Agent-invocation failure supervision (#3138)
# ---------------------------------------------------------------------------
# Linear backoff: ``streak * _BACKOFF_FACTOR`` seconds,
# capped at ``_BACKOFF_CAP_SECONDS``.
SUPERVISION_BACKOFF_FACTOR = 2
SUPERVISION_BACKOFF_CAP_SECONDS = 30

# Warn-level log / sticky message when the per-dupe-key streak
# reaches this threshold.
SUPERVISION_FAILURE_STREAK_WARN = 5

# Sticky alert (anomaly ``agent-invocation-fail-streak``) when the
# per-dupe-key streak reaches this threshold.
SUPERVISION_FAILURE_STREAK_ALERT = 10
