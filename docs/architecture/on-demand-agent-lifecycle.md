# On-Demand Agent Lifecycle

> The orchestrator-owned BRC event loop, the per-event spawn dedupe contract,
> failure supervision semantics, and the monitor matrix for the on-demand
> one-shot-pod path. The mechanism was introduced by
> [#3064](https://github.com/jwbron/egg/issues/3064); the in-pod wait arm and
> the `EGG_EVENT_LOOP_OWNER` mode toggle were retired by
> [#3164](https://github.com/jwbron/egg/issues/3164) after the live proving
> run on issue #3200 passed. **Orchestrator ownership is now the only mode.**

## History & the #3023 Post-Mortem Lesson

> This section is **lineage**. The current state is single-mode (orchestrator
> ownership); the history below explains why the rollout was staged.

The first attempt at on-demand agent spawning ([#3023](https://github.com/jwbron/egg/issues/3023))
landed the `EGG_EVENT_LOOP_OWNER` guard alone. The guard silenced the in-pod
event loop, leaving nothing to service BRC events — deadlock. Since #2908
had already deleted the legacy `EGG_BRC_EVENT_PUMP` rollback flag,
there was no runtime fallback.

The lesson drove a staged rollout under #3064:

- The flag (`EGG_EVENT_LOOP_OWNER`) **defaulted** to the in-pod loop (`pod`)
  so the mechanism could land dark.
- The guard and spawner **landed together** in #3064.
- The default-flip was a **separate, gated** step: a live BRC proving run with
  `EGG_EVENT_LOOP_OWNER=orchestrator` on issue #3200 had to pass the
  acceptance checklist (see [Live Proving-Run Procedure](#live-proving-run-procedure),
  retained below as history) BEFORE retiring the in-pod path.

[#3164](https://github.com/jwbron/egg/issues/3164) completed that sequence:
the proving run passed, the default flipped, and the in-pod wait arm + the
`EGG_EVENT_LOOP_OWNER` flag were removed. There is **no rollback flag** — the
only regression path is `git revert` of the #3164 change.

## Event-Loop Ownership: Orchestrator-Only

The orchestrator owns the consensus event loop. The in-process event loop at
the `concurrent_executor` completion-poll site consumes `_derive_next_action`
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

The wrapper around each pod is **one-shot-only**: it requires `EGG_EVENT_ACTION`
to be set, handles exactly that single event, then exits. There is no in-pod
wait-loop between events and no 30 s background heartbeat subshell — both were
retired in #3164.

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
- `confirm`/`complete` never reach a pod — the orchestrator handles them
  agent-free.
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

## Failure Supervision

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
`AGENT_FAILED` path (#2806) on the orchestrator side — phase-level HITL
escalation. (The wrapper-side #2806 in-pod code was removed in #3164 when the
in-pod wait arm was retired.)

## Worktree Re-attach & Session Reuse (Hot-Path Latency)

Under orchestrator ownership the worktree becomes a hot path.

**Worktree re-attach-first policy:**
1. Validate the existing worktree for `{pipeline_id}[-{slice_id}]-{role}`:
   `.git` integrity, no foreign lock, and branch checked out is the assigned
   branch, the derived per-agent work branch `egg/{agent_worktree_id}/work`
   (the gateway materializes worktrees on this local branch, wired to push to
   the assigned branch — #3480), or detached `HEAD`.
2. **On pass:** discard uncommitted changes and untracked staging artifacts
   (`git reset --hard` + `git clean -fd`) and sync to the role branch tip
   before agent invocation. The sync is **fast-forward-aware** ([#3506](https://github.com/jwbron/egg/issues/3506)):
   when the pre-discard tree was clean and the local HEAD is a strict
   descendant of the origin tip, the local commits are the agent's own
   durable multi-session work and HEAD is kept; hard-reset to the tip
   happens only on divergence, a behind-tip HEAD, or a dirty pre-discard
   tree (the killed-mid-event signature; the #3023 post-mortem constraint
   means a predecessor pod killed mid-event must never leak unproposed
   residue into a successor's commit). A reset that discards commits ahead
   of the tip is auto-salvaged to an `egg/recovered/...` ref and durably
   recorded as a message-bus system message to the role before the reset
   runs ([#3509](https://github.com/jwbron/egg/issues/3509)), so a
   resuming agent with no session memory can find and resume its prior
   work instead of silently re-deriving it (falls back to log-only when
   `pipeline_id` context is unavailable, or record-only when the salvage
   push itself fails). **Uncommitted work is snapshotted first**
   ([#3639](https://github.com/jwbron/egg/issues/3639)): a dirty tree is
   committed (`git add -A` plus a `[salvage] pre-reset working-tree state`
   commit) *before* the `reset --hard`, so it becomes an ordinary orphan
   that the salvage + record path above recovers. Without that step a
   session that worked for hours without committing had nothing for the
   orphan detector to find and lost everything on a routine respawn. The
   snapshot does not relax the residue policy: the tree still hard-resets
   to the origin tip, so the successor inherits nothing uncommitted; it
   only makes the discarded state recoverable. Ignored files are excluded,
   and a failed snapshot logs at WARNING with the file count and proceeds
   with the reset rather than blocking reuse. The snapshot is skipped
   entirely when the re-attach carries no `branch`: with no origin tip to
   reset to and no salvage target, the commit would simply become the
   successor's HEAD — un-vetted residue promoted to committed state, which
   is what R6 exists to prevent. When the salvage push fails the bus
   record says the snapshot was *not* pushed and asks for escalation
   rather than reassuring the successor that nothing was lost. The ask
   also scales with *what* the snapshot captured: when every captured path
   is a **state file the next event regenerates and some other store
   durably holds**, the record softens to "read it if you need it" so a
   routine respawn does not train the #3509 message into background noise.
   The membership test is regeneration, not authorship — the dominant
   member is written by the *sandbox* on the agent's own tool call and
   holds agent-authored prose. The allowlist is
   `.egg-state/agent-outputs/*/brc-memory*.md` (rewritten by
   `sandbox/egg_agent_tools/handlers/brc_memory.py` on every
   `brc_ack`/`brc_nack`, with the orchestrator message history as the
   durable backstop — see
   [brc-memory.md](brc-memory.md)),
   `.egg-state/agent-outputs/consensus-confirmed`, and
   `.egg-state/agent-outputs/<pipeline-id>-apply-handoff.json`;
   matching is segment-wise so `*` does not cross `/`. Agent *output* in
   the same directory — `<pipeline>-wontdo.json`,
   `<identifier>-tester-output.json` — is deliberately excluded: nothing
   rewrites it on the next event and no other store holds it, so losing it
   warrants the imperative. Anything else —
   including an unrecognised or unknown file set — keeps the imperative
   "inspect it before starting work", as does a snapshot flagged partial
   (a truncated capture's path list omits whatever failed to stage, so it
   cannot establish that the snapshot holds nothing but state files). The
   bus record's metadata carries the inputs *and* the outcome as separate
   fields — `wip_paths` (capped), `wip_partial`,
   `wip_machine_state_only` (the path predicate alone) and `wip_softened`
   (whether the body actually softened) — so a triage consumer can
   reconstruct the decision instead of regexing the prose; the two derived
   fields diverge whenever a machine-state-only path set is disqualified
   by a commit stack, a truncated capture, or a failed salvage push. The
   threshold selects wording only; the snapshot itself is always taken —
   including when the path list cannot be read at all. The staged-path
   read uses `-z` so `wip_paths` carries real bytes rather than
   `core.quotePath` C-quoted tokens, which means a filename that is not
   valid UTF-8 would be undecodable under `subprocess`'s strict `text=True`
   decode; the read passes `errors="replace"`, so one bad name costs one
   name (a U+FFFD in `wip_paths`) rather than the whole path set. That
   replacement does not move the softening decision in either direction:
   every non-`*` character in the softening globs is ASCII and replacement
   only substitutes non-ASCII for non-ASCII, so a replaced path matches
   exactly the globs its raw bytes would. Anything that still defeats that read — a
   timeout on a large staged set, a non-zero `diff` against a locked index
   — logs a WARNING and commits blind (`wip_paths`/`wip_files` become
   `null`, so the record takes the imperative) rather than letting a
   metadata read cost the working tree. A
   snapshot whose `git add -A` did not complete cleanly is marked incomplete in
   both its commit message and the bus record, since a truncated snapshot
   is otherwise indistinguishable downstream from a complete one. The same
   marker rides the #2807 crash-salvage commit
   (`commit_working_tree`), which pushes to `egg/recovered/…` with no bus
   record at all — there the commit message is the only channel a triager
   ever sees.
3. **Before handing off to spawn:** translate the validated paths from
   orchestrator-local (under `WORKTREE_BASE_DIR`) to host paths, matching
   what the create path already gets from the gateway. An untranslated
   local path mounts as an empty kubelet-created `hostPath` dir on the
   node — the agent boots into an empty worktree and silently no-ops
   (#3502).
4. **On any validation mismatch OR discard failure:** fall back to today's
   `create_with_retry`.

**Session reuse:**
- Re-register against the gateway only when no live session exists or the token
  has aged out.
- Session teardown happens at **phase end** or **streak exhaustion**.
- The gateway session is refreshed **at spawn time**, not via a between-events
  heartbeat fan-out (the 30 s background heartbeat was retired in #3164).

**Latency budget:** p50 spawn→invoke **< 60 s** (asserted in a simulated-clock test).

## Monitor Matrix

Orchestrator ownership is the only mode (#3164). Tripwires are active **only
while a role's one-shot Job is live**; between dispatched events a role has no
running pod, and that silence is normal.

| Tripwire | Behaviour |
|----------|-----------|
| "Role has no pod" | **Normal** — never alerts (between-events silence is expected) |
| Heartbeat timeout (120 s default, 600 s implement) | **Active only while a Job is running** |
| Container exit | **Trips only if a Job was active** |
| Silent mid-event pod | **Trips** — a one-shot pod that goes silent mid-invocation (Job active, no heartbeat) alerts normally |
| Idle-budget alert (`EGG_BRC_IDLE_BUDGET_MIN`) | **Convergence-stall judgment** from tracker timestamps, orchestrator-side — same knob, same anomaly name as the retired in-pod budget check |

**HeartbeatCoordinator:** session refresh happens at spawn time (not via a
background heartbeat subprocess). Absent senders between events trip nothing.
The active-Job set is refreshed every poll tick by the event loop via
`set_active_roles(roles)`, so coverage is current.

## Live Proving-Run Procedure

> **History.** This checklist gated the #3164 default-flip. The proving run
> ran on issue #3200 and passed; the default flipped and the in-pod arm was
> retired. Retained for the acceptance criteria of record.

A live BRC pipeline ran through all phases with the orchestrator owning the
event loop and passed this acceptance checklist:

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

## Follow-Up Sequence (completed by #3164)

> **History.** The #3064 PR filed [#3164](https://github.com/jwbron/egg/issues/3164)
> post-merge to carry out the operator-mandated retirement sequence. #3164
> has since landed; the sequence below is the record of what it did.

The sequence #3164 executed:

1. **Live BRC proving run** (issue #3200) with the orchestrator owning the
   event loop, passing the [acceptance checklist](#live-proving-run-procedure).
2. **Flipped the default** to orchestrator ownership.
3. **Retired the in-pod path**, deleting:
   - The in-pod wait arm (the `consensus_wrapper.py` event-pump loop)
   - The 30 s background heartbeat subprocess
   - Wrapper-side #3138 streak code
   - Wrapper-side #2806 failure-code path
   - The `EGG_EVENT_LOOP_OWNER` flag itself (and `get_event_loop_owner`)
   - **End state: no dead/deprecated code; orchestrator ownership is the only mode.**

## References

- [#3064](https://github.com/jwbron/egg/issues/3064) — orchestrator-owned event loop
- [#3023](https://github.com/jwbron/egg/issues/3023) — scrapped first attempt (post-mortem)
- [#3164](https://github.com/jwbron/egg/issues/3164) — default-flip + in-pod-arm retirement (orchestrator ownership is the only mode)
- [#2908](https://github.com/jwbron/egg/issues/2908) — BRC consensus wrapper (foundation)
- [#3138](https://github.com/jwbron/egg/issues/3138) — streak/backoff supervision semantics
- [#2806](https://github.com/jwbron/egg/issues/2806) — AGENT_FAILED path
- [#2761](https://github.com/jwbron/egg/issues/2761) — tracker rebuild from message store
- `orchestrator/supervision_policy.py` — shared streak constants (NEW, slices 3–6)
- `orchestrator/event_loop.py` — orchestrator-side event loop + dedupe + supervision (NEW, slices 2–5)
- `orchestrator/consensus_wrapper.py` — wrapper template (one-shot-only per-event arm; the in-pod wait-loop was retired in #3164)
- `orchestrator/kubernetes_spawner.py` — one-shot Job spawning (NEW entry, slices 2, 4)
- `orchestrator/health_monitor.py` — lifecycle-aware tripwires
- `orchestrator/heartbeat.py` — HeartbeatCoordinator mode guard
