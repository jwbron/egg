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

## The critical safety finding

The only recovery paths for a CANCELLED pipeline are `restart_agent` and `restart_phase` —
`start_pipeline` returns 409 for CANCELLED pipelines. Both `restart_agent` and `restart_phase`
already bump `run_epoch`. This means:

- **Change 1 + Change 2 together are safe:** Stop clearing on CANCELLED, and namespace by
  `run_epoch`. The old tracker/message stream is orphaned (isolated by the old epoch), and the
  new `run_epoch` gets fresh state. #2053 is preserved.
- **Change 1 alone is NOT safe:** Without namespacing, the old CONFIRMED tracker and old messages
  would be reused by the restarted pipeline, reintroducing #2053.

## Recommended scope

- **Adopt Changes 1 + 2 together** — they're interdependent for safety.
- **Adopt Change 3** — cheap insurance, best-effort.
- **Defer Change 4** — not required for the core fix; per-slice tracker reconstruction is a
  nice-to-have for full Redis-loss recovery.

## Test impact

The #2053 regression test (`test_cancel_clears_runtime_state` in `test_pipelines_api.py`) currently
asserts that `_clear_pipeline_runtime_state` IS called on cancel. This test must be updated to
assert that cancel does NOT clear — only delete and create do.
