# On-Demand Agent Lifecycle

> Event-loop ownership modes, the per-event spawn dedupe contract, failure
> supervision semantics, and the monitor matrix that supports both the legacy
> long-lived-pod path and the orchestrator's on-demand path. Introduced by
> [#3064](https://github.com/jwbron/egg/issues/3064).

## The #3023 Post-Mortem Constraint

The first attempt at on-demand agent spawning landed the `EGG_EVENT_LOOP_OWNER`
guard alone. The guard silenced the in-pod event loop, leaving nothing to
service BRC events — deadlock.

Since #2908 slice-4 deleted the legacy `EGG_BRC_EVENT_PUMP` rollback flag,
there is **no rollback path** to the pre-#3064 behavior.

The hard constraint:

- The flag **must default** to the in-pod loop (`pod`).
- The guard and spawner **must land together** (or spawner first) in a single PR.
- The follow-up default-flip is a **separate, gated** step that runs a live
  BRC proving run with `EGG_EVENT_LOOP_OWNER=orchestrator` BEFORE committing
  to retiring the in-pod path.

See [Live Proving-Run Procedure](#live-proving-run-procedure) and
[Prepared Follow-Up Issue Body](#prepared-follow-up-issue-body) below.

## Event-Loop Ownership: Two Modes

`EGG_EVENT_LOOP_OWNER` ∈ {`pod` (default), `orchestrator`} controls who
runs the consensus event loop and spawns agent pods.

### `pod` — Long-Lived Pods (Production Default)

This is the historical mode, unchanged by #3064. The orchestrator spawns the
full agent team up front at phase start. Each agent pod runs the BRC
consensus wrapper in a long-lived in-pod event pump:

- **Who runs the loop:** the pod's wrapper (`orchestrator/consensus_wrapper.py`).
- **Who spawns:** `concurrent_executor.spawn_all()` → `kubernetes_spawner.spawn_agent_job()` at phase start.
- **Verb→pod mapping:** every role gets a pod. Confirm and complete verbs are
  handled agent-free by the wrapper — no agent subprocess is invoked.
- **Lifecycle:** pod lives for the full phase; idle pods reserve CPU/memory
  and a gateway session for the duration.
- **Supervision:** in-pod background heartbeat at 30 s interval; idle-budget
  alert at `EGG_BRC_IDLE_BUDGET_MIN` (default 30 min) via `OVERSEER_ALERT`.
- **Worktree:** created once at spawn; persists on disk across the pod lifetime.

### `orchestrator` — On-Demand One-Shot Pods

Under orchestrator ownership the in-process event loop at the
`concurrent_executor` completion-poll site consumes `_derive_next_action`
per role and spawns an agent pod **only** when that role has an actionable
event (`propose` | `ack` | `nack`). Each pod handles a single event and
exits.

```
                    ┌─────────────────────────────────┐
                    │   orchestrator / event_loop.py  │
                    │                                 │
 _derive_next      ─│─► per-role verb→decision        │
 _action           ─│─► map:                          │
                    │                                 │
                    │   propose│ack│nack  ──► spawn   │
                    │   confirm│complete ──► orchest-  │
                    │                         rator-  │
                    │                         side    │
                    │   wait              ──► nothing │
                    │                                 │
                    └───┬────────────────────────────┘
                        │ k8s Job
                        ▼
        ┌───────────────────────────────┐
        │  one-shot agent pod           │
        │                             │
        │  EGG_EVENT_LOOP_OWNER        │
        │      = orchestrator          │
        │  EGG_EVENT_ACTION             │
        │      = propose|ack|nack      │
        │  EGG_EVENT_DEDUPE_KEY        │
        │                             │
        │  1. Re-check next-action    │
        │     (stale → exit 0)        │
        │  2. compose_event_prompt    │
        │  3. invoke_agent_for_event  │
        │  4. Exit w/ #2908-          │
        │     classified code          │
        └─────────────────────────────┘
```

**Verb → Pod mapping:**

| Verb | Action | Pod spawned? |
|------|--------|--------------|
| `propose` | Agent proposes work | ✅ One-shot pod |
| `ack` | Agent reviews → ACK | ✅ One-shot pod |
| `nack` | Agent reviews → NACK | ✅ One-shot pod |
| `confirm` | Bookkeeping | ❌ Orchestrator-side |
| `complete` | Bookkeeping | ❌ Orchestrator-side |
| `wait` | No peer action | ❌ Nothing |

**Key design decisions:**
- `confirm`/`complete` never reach a pod — the wrapper already handles them
  agent-free, and the orchestrator-side path mirrors that.
- The orchestrator's poll interval is env-tunable (default **5 s**).

## Dedupe-Key Contract

To prevent duplicate pods for the same event (the re-derivation window is
~10–30 s), every spawn carries a content-addressed dedupe key.

```
sha256(
  pipeline_id,
  slice_id | null,
  phase,
  role,
  action,                    // propose|ack|nack
  event_identity             // depends on verb
)
```

**Event identity per verb:**

- **Reviewer ACK/NACK:** `producer_role` + `proposal_commit_sha` (from `pending_reviews` payload,
  `routes/consensus.py:220-221`)
- **Producer propose:** target version + open NACK set being addressed
- **First propose:** literal `v1`

**Enforcement layers:**

| Layer | Mechanism |
|-------|-----------|
| **In-memory set** | Dedupe key set avoids re-spawning for an already-dispatched event within a session |
| **Job labels** | `egg.dev/event-key=<12-hex-prefix>` on the k8s Job; reconciled on orchestrator restart |
| **Stale-event check** (wrapper one-shot arm) | Before invoking the agent, re-fetches `next-action` — exits 0 without agent invocation if the event no longer matches the injected identity |
| **At-most-one-live-pod invariant** | Enforced by the dedupe-key check: same dedupe key ⇒ same Job adoption, never a duplicate |

**Orchestrator restart:** No persisted bookkeeping. On restart, `event_loop.py` re-derives
actions from the consensus tracker (rebuilt from the message store, #2761) and reconciles
against live k8s Jobs by label selector. Stateless, re-derived, idempotent.

## Failure Supervision (HITL cq-2)

Per the HITL-approved design, the orchestrator watches one-shot Job status with
per-`(role, action-arm)` streak counters. The constants are shared between the
wrapper and the event loop via `orchestrator/supervision_policy.py` (NEW) — one set of
values, no fork.

| Streak | Action | Description |
|--------|--------|-------------|
| 1–4 | Silent retry | Respawn the same event key after `streak × 2 s` backoff (capped 30 s) |
| 5 | `warn` log | Warning emitted; retries continue |
| ≥10 | **Sticky `OVERSEER_ALERT`** | Anomaly: `agent-invocation-fail-streak` (orchestrator-side origin marker). Alert latched per-`(role, arm)` cluster — exactly once, reset on new dedupe-key change |
| Success | Reset | Streak resets to zero |

**What does NOT trigger supervision:**

- NACK outcomes (consensus working as designed)
- Stale-event exits (exit 0, no invocation)
- `confirm`/`complete` (no pod spawned)
- Legitimate BRC protocol steps of any kind

**AGENT_FAILED engagement:** Producer propose-arm exhaustion engages the existing
`AGENT_FAILED` path (#2806) relocated for orchestrator mode — same phase-level
HITL escalation as pod mode. Wrapper-side #2806 code is untouched and will be
deleted in the follow-up cleanup PR.

## Worktree Re-attach & Session Reuse (Hot-Path Latency)

Under orchestrator ownership the worktree becomes a hot path.

**Worktree re-attach-first policy:**
1. Validate the existing worktree for `{pipeline_id}[-{slice_id}]-{role}`:
   expected branch checked out, `.git` integrity, no foreign lock.
2. **On pass:** discard uncommitted changes and untracked staging artifacts
   (`git reset --hard` + `git clean -fd`) and hard-sync to the role branch tip
   before agent invocation. (The #3023 post-mortem constraint means a
   predecessor pod killed mid-event must never leak unproposed residue
   into a successor's commit.)
3. **On any validation mismatch OR discard failure:** fall back to today's
   `create_with_retry`.

**Session reuse:**
- Re-register against the gateway only when no live session exists or the token
  has aged out.
- Session teardown moves to **phase end** or **streak exhaustion** in orchestrator
  mode.
- Pod-mode session lifecycle unchanged.

**Latency budget:** p50 spawn→invoke **< 60 s** (asserted in a simulated-clock test).

## Monitor Matrix (Tripwire × Ownership Mode)

| Tripwire | `pod` mode | `orchestrator` mode |
|----------|-----------|---------------------|
| "Role has no pod" | Trip (pod died) | **Normal** — never alerts |
| Heartbeat timeout (120 s default, 600 s implement) | Active | **Active only while a Job is running** |
| Container exit | Trip on unexpected exit | **Trips only if a Job was active** |
| Silent mid-event pod | — | **Trips** (one-shot pod that goes silent mid-invocation triggers) |
| Idle-budget alert (`EGG_BRC_IDLE_BUDGET_MIN`) | In-pod alert (wrapper-side) | **Convergence-stall judgment** from tracker timestamps — same knob, same anomaly name — orchestrator-side |

**HeartbeatCoordinator mode guard:** In orchestrator mode, session refresh happens
at spawn (not via a background heartbeat subprocess). Absent senders between events
trip nothing.

## Live Proving-Run Procedure

Before the production default can be flipped from `pod` to `orchestrator`,
a live BRC pipeline must run through all phases with
`EGG_EVENT_LOOP_OWNER=orchestrator` and pass this acceptance checklist:

1. **All phases converge** — BRC consensus completes in refine, plan,
   implement (all slices), and PR; no phase stalls or deadlocks.
2. **No duplicate pods** — for any single event, the orchestrator never
   spawns two pods simultaneously; at-most-one-live-pod holds across the
   full run.
3. **Supervision fires on induced failure** — a deliberately killed mid-event
   pod triggers the `agent-invocation-fail-streak` escalation path
   (backoff, warn at 5, sticky `OVERSEER_ALERT` at 10, exhaustion). The
   alert name matches the one emitted from the in-pod path.
4. **Latency budget held** — measured p50 spawn→invoke latency across all
   events ≤ 60 s.

## Prepared Follow-Up Issue Body

The follow-up issue is filed **immediately post-merge** (manual action
referenced from the PR description) and encodes the operator-mandated
sequence:

1. **Live BRC proving run** with `EGG_EVENT_LOOP_OWNER=orchestrator` to
   pass the [acceptance checklist](#live-proving-run-procedure).
2. **Flip the default** to `orchestrator`.
3. **One cleanup PR** deleting:
   - The in-pod wait arm (≈`consensus_wrapper.py:379` event-pump loop)
   - The background heartbeat subprocess (≈`209-230`)
   - Wrapper-side #3138 streak code (≈`897-901`)
   - Wrapper-side #2806 failure-code path (≈`134`+)
   - The `EGG_EVENT_LOOP_OWNER` flag itself
   - **End state: no dead/deprecated code.**

Filing the issue is an immediate post-merge manual step. The follow-up PR
body should be a copy-paste from this section. **Issue [#3164](https://github.com/jwbron/egg/issues/3164)
is reserved for this purpose** (the operator directed filing during the
plan phase gate review).

## References

- [#3064](https://github.com/jwbron/egg/issues/3064) — orchestrator-owned event loop
- [#3023](https://github.com/jwbron/egg/issues/3023) — scrapped first attempt (post-mortem)
- [#3164](https://github.com/jwbron/egg/issues/3164) — flip follow-up issue (post-merge filing)
- [#2908](https://github.com/jwbron/egg/issues/2908) — BRC consensus wrapper (foundation)
- [#3138](https://github.com/jwbron/egg/issues/3138) — streak/backoff supervision semantics
- [#2806](https://github.com/jwbron/egg/issues/2806) — AGENT_FAILED path
- [#2761](https://github.com/jwbron/egg/issues/2761) — tracker rebuild from message store
- `orchestrator/supervision_policy.py` — shared streak constants (NEW, slices 3–6)
- `orchestrator/event_loop.py` — orchestrator-side event loop + dedupe + supervision (NEW, slices 2–5)
- `orchestrator/consensus_wrapper.py` — wrapper template (one-shot arm, ~line 379 wait-loop)
- `orchestrator/kubernetes_spawner.py` — one-shot Job spawning (NEW entry, slices 2, 4)
- `orchestrator/health_monitor.py` — lifecycle-aware tripwires
- `orchestrator/heartbeat.py` — HeartbeatCoordinator mode guard
