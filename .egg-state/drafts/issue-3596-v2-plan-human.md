# Making Agent Pipelines Observable: The Implementation Plan

## What We're Building

Operators need to be able to tell whether an agent is actually working or
stuck — at a glance, from the status check. Right now, every agent reports
"WORKING" regardless of whether it's grinding through a large task, doing
nothing, or spinning in circles.

The codebase already has 27 built-in detectors that could catch these
problems automatically, but **none of them can fire** because:
1. The detection system is never actually called during normal operation
2. The data it needs is never populated — only 5 of 13 data fields are
   filled in, and only 3 of 7 agent-status fields

This plan fixes those gaps in order of impact.

## What Already Exists (Don't Rebuild)

The following infrastructure already exists and was verified against the
codebase:
- Pod log capture (Redis-backed, 24-hour retention)
- Health alerts endpoint (`GET /pipelines/{id}/health/alerts`)
- Heartbeat emitters (for tool calls and driver liveness)
- HealthMonitor (active in production, 12+ call sites)
- ProgressStore (tracks progress events)
- Commit enumeration tools
- Exit info snapshots (frozen at container exit)
- Commit authorship registry
- Evidence rescue (patch-id matching for unreachable commits)

## What's Missing (The 7 Gaps)

1. **Detection plane is never invoked** — The code exists to run all 27
   detectors, but nothing actually calls it during normal operation. It's
   like having smoke detectors that are never connected to power.

2. **Snapshot builder is sparse** — The function that prepares data for the
   detectors only fills in 5 of 13 fields. The other 8 fields (container
   transitions, git state, decision state, cost counters, gateway errors,
   midturn messages, raw runtime data, raw LLM data) are left empty.

3. **Role field is wrong** — The snapshot puts a container ID (a UUID) in
   the "role" field instead of the agent's actual role name (like "coder"
   or "task_planner"). Any detector that checks which agent it's looking
   at is matching against the wrong value.

4. **Status doesn't show progress** — The `get_status` endpoint only shows
   basic agent info (role, status, start time). It doesn't show commit
   counts, how long since the last heartbeat, or how many progress events
   an agent has recorded. Active alerts require a separate API call.

5. **Peer-progress gate is too broad** — The system that prevents false
   alerts about slow agents is too generous: it defers to ANY peer's
   activity, including the overseer's own heartbeat. This means a wedged
   agent can hide behind the overseer's activity.

6. **No forward-progress detector** — There's no detector that catches the
   specific case of an agent running for a long time with zero commits,
   zero progress events, and zero file changes — the "exited successfully
   but did nothing" scenario.

7. **README is misleading** — The documentation claims the detection plane
   is wired into production, but it isn't.

## What We're Going to Build

### Slice 1: Wire the detection plane + enrich the snapshot (foundation)

This is the critical first step. Without it, nothing else works.

**1a. Wire the detection plane into the runtime tick**
- Call the detection plane evaluator from the runtime tick handler
- Make it idempotent (won't double-fire from two call sites)
- Emit findings as events on the event bus
- Route findings that need human judgment to the adjudicator
- Let routine findings trigger automatic corrective actions

**1b-g. Enrich the snapshot builder** (7 sub-tasks)
Each sub-task populates one group of data fields that the 27 detectors need:
- **1b**: Container transitions (container death, OOM, restarts)
- **1c**: Git state (commit count, last commit, branch, divergence, corruption)
- **1d**: Decision state (pending HITL decisions, replay queue)
- **1e**: RunningAgent liveness fields (heartbeat age, tool-call age, exit info) + fix the role=str(cid) defect
- **1f**: Phase state and raw runtime data (thread liveness, restart propagation)
- **1g**: Correct the misleading README

### Slice 2: Add a forward-progress detector

A new detector that fires when an agent has been running for more than
600 seconds (configurable) with:
- Zero new commits
- Zero progress events
- Zero file modifications

This directly addresses the issue's key diagnostic: "a hand-rolled loop
counting commits on the agent's worktree" that could tell a working agent
from one that "exited rc=0 doing nothing against an empty worktree mount."

### Slice 3: Fix the peer-progress gate

Fix `_has_recent_peer_progress` to only defer alerts based on peers that
the agent actually depends on (from the BRC review graph's dependency edges),
not any peer's heartbeat. This prevents the overseer's own activity from
suppressing alerts about agents it watches.

### Slice 4: Enrich get_status with progress signals

Add a "progress" section to each agent in the status response:
- How long since the last heartbeat
- How long since the last progress event
- How many commits the agent has made
- When the last commit was, its SHA, and its subject line
- How many progress events have been recorded

Add an "alerts" section to the top-level status (capped at 10 entries)
so operators don't need a separate API call to see what's wrong.

Add phase timing (when the phase started, how long it's been running).

All fields are null when unmeasurable — never 0 (per operator constraint:
distinguish null from zero).

### Slice 5: Record sampling params (deferred)

Extend the cost logging to record sampling parameters (temperature, top_p,
etc.) per call. Pin explicit values per model. This is fully independent
and deferred — can be done as a follow-up.

## What We're NOT Building (Deferred)

- **Consumption breaker (task-5)**: No cost counter store exists. The cost
  logging currently goes to stdout — there's no queryable store. This task
  is deferred until a store is created.
- **Session transcript capture on pod exit**: Agent session transcripts are
  only saved when a pod exits. An agent that never exits (stuck) has no
  transcript at all. This requires changes to the agent session lifecycle
  and is tracked as a separate follow-up.
- **Repetition-triggered context surgery**: Requires Claude Code session
  history rewriting support — needs investigation first.
- **Ground-truth verifier role**: A reviewer-graph topology change, not a
  visibility improvement.

## How We'll Build It

**Execution order:**
1. Slice 1 first (the foundation — everything depends on it)
2. Slices 2, 3, and 4 in parallel (they all depend on slice 1 but not on
   each other)
3. Slice 5 anytime (it's independent and deferred)

**Testing approach:**
- Each sub-task has a dedicated test file
- Tests verify the detector receives populated data fields
- Tests verify best-effort degradation (failures don't crash the system)
- Tests verify the role=str(cid) defect is fixed
- Manual verification: check that `/status` shows progress signals and
  alerts, and that detection findings appear in `/health/alerts`

**Key constraints (from operator):**
- Null is not zero — missing measurements must be null, never 0
- Don't rebuild existing infrastructure (agent_log_store, health alerts
  endpoint) — they already exist and work
- Session transcripts are a real gap, distinct from pod logs — scope it
  explicitly if touching post-mortem durability
