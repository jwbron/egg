# Making Agent Pipelines Observable: A Plain-Language Summary

## The Problem

A pipeline ran for about 48 hours and hit four different failures — all
completely invisible to the usual status checks. Every time, the system
reported the same thing: "running," with every agent marked as "working."
There were no error messages, no pending decisions, no red flags.

An operator had to build a custom script just to count commits on each
agent's workspace to tell the difference between:

- An agent genuinely working through a large task (good)
- An agent that exited immediately without doing anything (bad)
- An agent whose work was silently discarded every cycle (bad)
- An agent re-deriving days of already-finished work because it lost its
  memory (bad)

Finding out what was actually happening took hours of manual detective work:
git history, log greps, and racing to capture pod logs before they vanished.
Once the state was visible, recovery was cheap. The expensive part was
always discovering the state.

## What We Need to See

Four areas of visibility are needed:

1. **Forward progress** — Is an agent actually getting somewhere? Would we
   notice if it started going backwards?
2. **Events and alerts** — Destructive or exceptional things happen that
   never reach anyone watching. Right now, alert volume is just a number
   with no readable detail.
3. **Clocks and per-invocation accounting** — Important deadlines are
   invisible until they pass. A cycle that did nothing looks identical to
   one that did real work.
4. **Post-mortem durability** — Evidence dies with the pod. By the time
   you know you need it, the state needed for diagnosis is gone.

## What Already Exists

The infrastructure for most of this already exists in the codebase. The
problem is that the data is not surfaced where operators can see it at a
glance.

**Forward progress tracking** already exists:
- A health monitor tracks when each agent last sent a heartbeat, last made
  progress, and last had any activity
- The system records commit SHAs when agents make changes
- Per-cycle timing records are stored with start/completion timestamps
- Retry counts are tracked per agent
- Tools exist to enumerate unpushed commits and check for uncommitted
  changes

**Events and alerts** already exist:
- A dedicated alert message type is visible to human-facing tools
- The health monitor can return structured alerts with type, message,
  severity, and timestamp
- A REST endpoint serves active health alerts
- Pod logs are captured to a Redis-backed store with a 24-hour retention
- 25+ deterministic detectors cover container death, restart loops,
  consensus stalls, disk pressure, and more

**Clocks and per-invocation accounting** already exist:
- Phase and agent timing (start/completion) is recorded
- Configurable thresholds for stall detection, silent agents, long-running
  phases, and more
- An idle budget mechanism for the consensus event loop
- Per-agent timing state with anomaly tracking
- Branch tip tracking at phase start

**Post-mortem durability** already exists:
- Pod logs are captured to Redis before deletion (24h TTL)
- Frozen exit snapshots record role, exit code, and last log lines
- Patch-id rescue can recover unreachable commits
- Commit authorship is durably registered
- Unpushed commits are salvaged before worktree deletion
- Diagnostic issues are logged to an append-only file

## The Gap: Data Exists But Isn't Visible

The core complaint is that the status check reports "running" and "working"
for every agent, with no way to tell what's actually happening. Here is what
is missing from the status output:

**Forward progress is not visible:**
- The status only shows each agent's role, running state, container ID,
  start time, and elapsed time
- It does NOT show: how many commits the agent has made, when the last
  commit was, how long since the last heartbeat or progress event, the
  retry count, or how many files have changed

**Active alerts are not in the status:**
- The status shows a count of pending decisions but NOT active health
  alerts
- An operator must call a separate endpoint or run a separate command to
  see alerts
- Alert volume is just a number, not something you can read

**Per-invocation accounting is not visible:**
- No per-agent "time in current state" or "time since last progress"
- The idle budget is documented but not visible in the status
- Phase timing exists but isn't surfaced beyond elapsed time on agents

**Post-mortem evidence is not consolidated:**
- Logs are captured to Redis but there's no way to see "what happened to
  this agent" from the status
- Exit information exists but is only populated when the container monitor
  detects an exit — if the pod is killed first, the evidence is lost

**A key detector is dead code:**
- A heartbeat-stall detector exists that should fire when an agent hasn't
  sent a heartbeat or made tool calls in a long time
- But the function that populates agent data never fills in the fields the
  detector reads — so it can never actually fire in production

## Proposed Approach

### 1. Add progress signals to the status output

Add a "progress" section to each agent in the status, showing:
- How long since the last heartbeat and last progress event
- How many commits the agent has made
- When the last commit was, its SHA, and its subject line
- How many progress events have been recorded
- How many files have changed

### 2. Surface active alerts in the status

Add an "alerts" section to the top-level status, showing the most recent
active health alerts (capped at 10) so operators don't need a separate
command to see what's wrong.

### 3. Add per-invocation accounting to the status

Show phase start time, phase elapsed time, and idle budget status so
operators can see how long things have been running and how much buffer
remains.

### 4. Consolidate post-mortem evidence

Show exit information for completed or failed agents directly in the status,
and link to recent log store records so operators can see what happened
without hunting across tools.

### 5. Fix the dead-code detector

Populate the liveness fields that the heartbeat-stall detector reads, so
that detector actually works in production.

### 6. Add a forward-progress detector

Add a new detector that fires when an agent has been running for a while
with zero commits and zero progress events — catching the "agent exited
successfully but did nothing" case.

## Risks

- **Payload size**: Adding commit counts, file changes, and alerts to every
  status call could make responses larger. Mitigate by capping alerts,
  making new fields best-effort, and caching where possible.
- **Git cost**: Counting commits and file changes requires git operations on
  each agent's workspace. Mitigate by capping counts, using lightweight
  formats, and running with timeouts.
- **Coupling**: Reading from the health monitor requires importing its
  singleton. Mitigate with defensive imports and fallback to empty values.
- **Redis dependency**: Reading from the log store adds a Redis dependency.
  Mitigate by making it best-effort — degrade gracefully on failure.
