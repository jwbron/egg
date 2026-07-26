# In plain terms — the plan for issue #3632

**Make `cancel_task(cleanup=false)` actually preserve state for resume — in one slice, with
three coordinated changes.**

## What's being built

Issue #3632: when you call `cancel_task(cleanup=false)` to pause a pipeline, the system
advertises that state is preserved for later resume. But it isn't — the cancel handler calls
`_clear_pipeline_runtime_state`, which deletes both the consensus tracker (the in-memory record
of which agents approved what) and the Redis message stream (the full history of all BRC
messages). A resumed pipeline starts its consensus round from zero and loses its forensic record.

The fix has three parts, all shipping in a single slice:

### Change 1: Stop clearing on CANCELLED

Only clear runtime state on DELETE, CREATE, and FAILED — not on CANCELLED. This is the
minimal fix that makes `cancel_task(cleanup=false)` actually preserve state.

### Change 2: Namespace tracker + message stream by `run_epoch`

`run_epoch` is a timestamp already bumped on every restart (`restart_agent`, `restart_phase`,
`advance_phase`). Right now it's only used for thread-liveness detection. This change makes the
tracker key and message stream key include `run_epoch`, so each run gets its own isolated
namespace.

**Why this is required, not optional:** With Change 1 alone, the message stream survives a cancel.
After resume flips the pipeline to RUNNING, if the orchestrator restarts, `startup_reconciliation`
calls `reconstruct_tracker_from_messages` and replays the retained pre-cancel CONSENSUS_*
messages — resurrecting confirmations the restart had just cleared. This is **same-pipeline
stale-state replay**, a distinct bug from #2053 (which is about new pipelines reusing ids and
stays closed via the create-path clear). Namespacing by `run_epoch` prevents the replayed
messages from matching the new epoch's tracker.

### Change 3: Persist BRC history to disk on cancel

Even with namespacing, persisting the in-flight BRC transcript to the branch before clearing
provides a forensic record that survives Redis loss. This must write the per-slice CONSENSUS_*
buckets (not just the unattributed sibling that `write_per_slice=False` writes) — that gap is
precisely why the last incident's in-flight slice was unrecoverable.

## What's deferred

- **Change 4** (per-slice tracker reconstruction on resume): highest complexity, not required for
  the core fix. The #2535 rationale (a fresh slice must not inherit a same-named role's confirm)
  addresses new slices, not resumed ones — so the deferral is a scope call, not a correctness one.
- **#3633** (cancel never stops the driver thread): distinct fix in a different code path, tracked
  separately.

## How the work is broken into eight steps

All in one slice (Changes 1+2 must ship together; Change 3 is folded in because it touches the
same files and the implement phase branches slices independently off the shared base, so
overlapping file edits must be in one slice to avoid integration collisions):

1. **Stop clearing on CANCELLED** — split the `if pipeline.status in (CANCELLED, FAILED)` block
   in `_routes_crud.py` so only FAILED triggers `_clear_pipeline_runtime_state`.
2. **Namespace the tracker by `run_epoch`** — update `_tracker_key` and all callers of
   `get_peer_consensus_tracker`, `remove_peer_consensus_tracker`, `reconstruct_tracker_from_messages`.
3. **Namespace the message store by `run_epoch`** — update `_stream_key` and `_counts_key`, and all
   callers of `store()`, `get_messages()`, `clear()`, `get_stream_status()`.
4. **Update docstrings and comments** — `_clear_pipeline_runtime_state` docstring, the #2053
   comment in `_routes_crud.py`.
5. **Add cancel-specific BRC history persistence** — new function in `_brc_history.py` that
   writes per-slice CONSENSUS_* buckets with `write_per_slice=True`.
6. **Wire cancel persistence into the cancel path** — call the new function in the CANCELLED
   branch of `_routes_crud.py`, before any clearing.
7. **Update tests** — rewrite `test_cancel_clears_runtime_state` →
   `test_cancel_preserves_runtime_state`; pin the create-path clear explicitly; add a new
   regression test for cancel → resume → orchestrator restart → assert consensus NOT resurrected;
   add a test for cancel-persists-BRC-history.
8. **Green the boundary** — `make lint + make test-all`.

## What makes this safe

- **#2053 stays closed:** Change 1 keeps clearing on the CREATE path, so a new pipeline reusing
  an old id still gets a fresh tracker and message stream.
- **Stale-state replay is prevented:** Change 2 namespaces by `run_epoch`, so `reconstruct_tracker_from_messages`
  on a resumed pipeline reads only the new epoch's messages (which are empty until new proposals arrive).
- **Forensic record survives:** Change 3 persists the in-flight slice's BRC history to disk on
  cancel, so even a full Redis loss doesn't lose the evidence.
