# In plain terms — issue #3632

**Pausing a pipeline should not destroy its brain.**

## Background: how pipeline pause/resume works today

When you call `cancel_task(cleanup=false)`, you're supposed to pause a pipeline so it can be
resumed later via `restart_phase` or `restart_agent`. The `cleanup=false` flag is meant to
preserve pipeline state.

But it doesn't work. The cancel call sets the pipeline status to `CANCELLED`, and the PATCH
handler in `_routes_crud.py` has a catch-all that runs `_clear_pipeline_runtime_state` for ANY
terminal transition — including `CANCELLED`. That helper does two things:

1. **Deletes the consensus tracker** — the in-memory record of which agents have reviewed and
   approved which proposals.
2. **Clears the Redis message stream** — the full history of all BRC messages (proposals, ACKs,
   NACKs, CONFIRMED signals) exchanged between agents.

Both of these are exactly what a resume would need to pick up where it left off. The message-store
clear is even explicitly there to prevent `reconstruct_tracker_from_messages` from rebuilding the
old tracker — i.e., the system deliberately closes the one recovery path that could have made
resume lossless.

## The four candidate fixes

The issue proposes four changes. Here's what each one does in plain terms:

### Change 1: Stop clearing runtime state on CANCELLED

Only clear on DELETE and on CREATE (fresh pipeline), not on CANCELLED. This is the minimal fix —
it makes `cancel_task(cleanup=false)` actually preserve state.

**Why it's not safe alone:** The clear exists to defend against #2053 — a fresh pipeline reusing
an id from a prior terminal run inheriting the old CONFIRMED consensus. If we stop clearing on
CANCELLED, we need another way to prevent that leak.

### Change 2: Namespace the tracker and message stream by `run_epoch`

`run_epoch` is a timestamp that gets bumped every time a pipeline is restarted (via
`restart_agent`, `restart_phase`, or `advance_phase`). Right now it's only used for thread
liveness detection — not for isolating state. This change would make the tracker key and message
stream key include the `run_epoch`, so each run gets its own isolated namespace.

**Why it's the correct fix:** With `run_epoch` namespacing, a fresh pipeline reusing an old id
gets a new `run_epoch` and therefore a fresh tracker and message stream. #2053 is closed by
construction.

**Why it's large:** Every caller of `get_peer_consensus_tracker`, `remove_peer_consensus_tracker`,
`reconstruct_tracker_from_messages`, and the message store functions (`store`, `get_messages`,
`clear`) needs to pass `run_epoch` through.

### Change 3: Persist BRC history to disk on cancel

Even with namespacing, persisting the in-flight BRC transcript to the branch before clearing
provides a forensic record that survives Redis loss. This is belt-and-suspenders.

### Change 4: Reconstruct per-slice trackers on resume

Currently, only pipeline-level trackers are reconstructed from messages (per-slice trackers are
recreated fresh each iteration, per #2535). This change would allow per-slice tracker
reconstruction for resumed slices. Highest complexity, lowest urgency.

## The critical safety finding (corrected per operator feedback)

The only recovery paths for a CANCELLED pipeline are `restart_agent` and `restart_phase` —
`start_pipeline` returns 409 for CANCELLED pipelines. Both `restart_agent` and `restart_phase`
already bump `run_epoch`.

**The hazard is NOT #2053.** #2053 is about a *new* pipeline reusing a terminal pipeline's id —
that's defended by the create-path clear, which Change 1 keeps intact. The real hazard is
**same-pipeline stale-state replay**:

With Change 1 alone, the message stream survives a cancel (keyed by bare `pipeline_id`). Resume
via `restart_agent`/`restart_phase` resets consensus state and flips the pipeline to RUNNING. If
the orchestrator then restarts, `startup_reconciliation` (`startup_reconciliation.py:305`)
processes RUNNING pipelines and calls `reconstruct_tracker_from_messages` — which replays the
retained pre-cancel CONSENSUS_* messages, resurrecting the confirmations the restart had just
cleared. The window opens AFTER resume flips the pipeline to RUNNING, not during the CANCELLED
interval (reconstruction skips non-RUNNING pipelines).

- **Change 1 + Change 2 together are safe:** Stop clearing on CANCELLED, and namespace by
  `run_epoch`. The old tracker/message stream is orphaned (isolated by the old epoch), and the
  new `run_epoch` gets fresh state. `reconstruct_tracker_from_messages` would replay the old
  epoch's messages into the old epoch's (empty) tracker, not the new one.
- **Change 1 alone is NOT safe:** Without namespacing, `reconstruct_tracker_from_messages`
  replays the retained pre-cancel CONSENSUS_* messages into the reset round's tracker after an
  orchestrator restart, resurrecting stale confirmations.

## Recommended scope

- **Adopt Changes 1 + 2 together** — they're interdependent for safety.
- **Adopt Change 3** — cheap insurance, best-effort.
- **Defer Change 4** — not required for the core fix; per-slice tracker reconstruction is a
  nice-to-have for full Redis-loss recovery.

## Test impact

Per the operator's resolution of cq-2, three test changes are required:

1. **Rewrite `test_cancel_clears_runtime_state`** (`test_pipelines_api.py:1083`) to assert that
   cancel does NOT clear. Rename it (e.g. `test_cancel_preserves_runtime_state`) — a test called
   `test_cancel_clears_runtime_state` that asserts the opposite is a trap for the next reader.
2. **Pin the CREATE path explicitly** — `test_create_clears_runtime_state` must assert that create
   still clears. Change 1's safety argument for #2053 rests on create still clearing, so this
   assertion is now load-bearing.
3. **Add a NEW regression test** for the hazard described in cq-1's resolution: cancel → resume
   (`restart_agent` or `restart_phase`) → simulated orchestrator restart → assert the pre-cancel
   consensus state is NOT resurrected by `reconstruct_tracker_from_messages`. This is the
   regression that Change 2 exists to prevent, and without it Change 2 is untested.

## Scope constraints (from cq-3 resolution)

1. **Changes 1 and 2 ship together, in one slice** — landing 1 alone is strictly worse than today.
2. **Change 3 is independent and may land first** — it makes the next incident diagnosable
   regardless of the other two.
3. **Change 4 is deferred, not dropped** — the #2535 rationale addresses new slices, not resumed
   ones, so the deferral is a scope call, not a correctness one.
4. **#3633 is out of scope** — it is a distinct fix in a different code path.
