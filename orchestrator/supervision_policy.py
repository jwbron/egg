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

# ---------------------------------------------------------------------------
# Successful-no-op park (#3425)
# ---------------------------------------------------------------------------
# A one-shot invocation that exits cleanly WITHOUT changing BRC state is a
# "successful no-op": the loop re-derives the identical dedupe key next poll
# and would re-spawn indefinitely (observed as ~50 pod spawns against a slice
# wedged on an unresolved operator HITL decision). After this many consecutive
# clean completions of the SAME dedupe key the arm is parked. A productive
# success changes BRC state — its key is never re-derived — so it can never
# accumulate a streak past 1.
SUPERVISION_NOOP_STREAK_PARK = 3

# While parked, a probe spawn is still allowed this often. This is the
# liveness backstop for a wedge whose unblock the orchestrator cannot observe
# through the contract-decision fingerprint; it bounds the burn to ~2 pods/h
# instead of deadlocking the arm.
SUPERVISION_NOOP_PARK_RETRY_SECONDS = 1800

# A parked role whose latest HEARTBEAT self-reports ``WAITING_ON_ROLE`` is
# normal BRC choreography (a consumer waiting for its upstream producer's
# first proposal), PROVIDED the waited-on role is live — so the park alert is
# downgraded to low priority in that shape (#3520). "Live" means the waited-on
# role emitted any bus message within this window: under the orchestrator-owned
# event loop a working producer's pod emits WORKING heartbeats (dedup-exempt,
# see routes/messages.py) plus progress/consensus traffic, so a healthy
# producer is visible well inside it. Matches the health monitor's default
# non-implement heartbeat timeout (600s).
SUPERVISION_WAITING_ROLE_LIVE_SECONDS = 600
