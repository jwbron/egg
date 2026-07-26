# Architectural Plan — Issue #3632: cancel_task(cleanup=false) destroys consensus + message state

## Executive Summary

The task_planner's plan (v1) is **architecturally sound** with one gap: it does
not account for `_clear_concurrent_state` at `routes/phases/_transitions.py:56`,
which also clears the message store and tracker at phase transitions. With
`run_epoch` namespacing, this path must also be namespaced, or phase transitions
will clear the wrong epoch's state.

**Verdict: ACK with architectural notes.** The plan is correct in structure and
ordering. The single addition needed is threading `run_epoch` through
`_clear_concurrent_state`.

## Binding Constraints (from refine phase, all decisions resolved and approved)

1. **Changes 1+2 ship together in one slice** — landing Change 1 alone is
   strictly worse than today (stale-state replay hazard, NOT #2053).
2. **Change 3 is independent** — may land first, but touches overlapping files.
3. **Change 4 deferred** — per-slice tracker reconstruction.
4. **#3633 out of scope** — distinct fix in a different code path.

## Architectural Assessment

### Change 2: run_epoch namespacing — CORRECT

The `run_epoch` field already exists on the `Pipeline` model
(`models/_pipeline.py:163`) and is bumped on all restart/recovery paths:
- `restart_agent` CANCELLED→RUNNING: `_routes_restart.py:354`
- `restart_phase`: `_routes_restart.py:1046`
- `advance_phase` / `start_pipeline` recovery: `_routes_lifecycle.py:511, 654, 798`

The namespacing approach is correct:
- `_tracker_key` (`peer_consensus/__init__.py:226`) currently uses bare
  `pipeline_id` — needs to include `run_epoch`
- `_stream_key` (`redis_message_store.py:69`) currently uses bare
  `pipeline_id` — needs to include `run_epoch`

**Key design decision:** For a fresh pipeline (`run_epoch=None`), use
`created_at` as the epoch. This matches the existing pattern in
`_run_pipeline.py:56`: `run_epoch = pipeline.run_epoch or pipeline.created_at`.

**Cleanup strategy:** `_clear_pipeline_runtime_state` must clear ALL
`run_epoch` namespaces for a given `pipeline_id` (for DELETE and CREATE
paths). This requires Redis key scanning via `SCAN` pattern matching
(`pipeline:{pipeline_id}:*:messages`).

### Change 1: Stop clearing on CANCELLED — CORRECT, depends on Change 2

The `_clear_pipeline_runtime_state` call at `_routes_crud.py:717` is inside
`if pipeline.status in (CANCELLED, FAILED)`. Splitting this to only clear on
FAILED is correct.

**Safety argument (verified):**
- #2053 (new pipeline id reuse) stays closed via the POST-site clear at
  `_routes_crud.py:514` (on CREATE)
- Stale-state replay (same-pipeline, after resume → orchestrator restart) is
  prevented by Change 2's `run_epoch` namespacing: the new `run_epoch` gets a
  fresh namespace, and `reconstruct_tracker_from_messages` reads only the new
  epoch's messages

### Change 3: Persist BRC history on cancel — CORRECT, with sharpening

The task_planner's plan correctly identifies that `_write_brc_history` must be
called with `write_per_slice=True` on cancel. The existing
`_persist_phase_brc_history` at `_routes_restart.py:1089` calls it with
`write_per_slice=False` — that's the gap that made the last incident's in-flight
slice unrecoverable.

**Additional note:** The cancel path should run this in the background cleanup
thread (alongside container teardown) to avoid blocking the cancel response, as
R4 from the risk_analyst notes.

### Change 4: Per-slice tracker reconstruction — CORRECTLY DEFERRED

The #2535 rationale (fresh slice must not inherit same-named role's confirm)
addresses new slices, not resumed ones. With `run_epoch` namespacing, a resumed
slice's messages are under the old `run_epoch` and won't be replayed into the
new epoch's tracker. The deferral is a scope call, not a correctness one.

## Gap: _clear_concurrent_state must also be namespaced (R6)

The task_planner's plan does not mention `_clear_concurrent_state` at
`routes/phases/_transitions.py:56`. This function is called at phase transitions
(complete_phase, advance_phase) and clears both the message store and the
tracker. With `run_epoch` namespacing, this path must also pass `run_epoch` to:
- `get_message_store().clear(pipeline_id, run_epoch=...)`
- `remove_peer_consensus_tracker(pipeline_id, run_epoch=...)`

Without this, a phase transition would clear the wrong epoch's state, or fail
to clear the current epoch's state entirely.

**Callers of `_clear_concurrent_state`:**
- `routes/phases/_transitions.py` (definition)
- `routes/phases/__init__.py` (advance_phase, complete_phase)
- `routes/pipelines/_routes_lifecycle.py` (start_pipeline recovery, line 638, 672)
- `routes/pipelines/_routes_restart.py` (restart_phase)

## Architectural Recommendations

1. **Add `_clear_concurrent_state` to the plan** — thread `run_epoch` through it
   and all its callers. This is a sub-task of task-1-3 (message store
   namespacing) and task-1-2 (tracker namespacing).

2. **Introduce a `run_epoch` resolution helper** — a single function that
   resolves `(pipeline_id, run_epoch)` from a `Pipeline` object, used by all
   call sites. This avoids duplication and ensures consistency:
   ```python
   def _resolve_epoch_key(pipeline_id: str, pipeline: Pipeline) -> str:
       epoch = pipeline.run_epoch or pipeline.created_at
       return f"{pipeline_id}:{epoch.isoformat()}"
   ```

3. **Add a `clear_all_epochs` method to the message store** — for the DELETE
   and CREATE paths, which need to clear all `run_epoch` namespaces for a
   `pipeline_id`. This is a Redis `SCAN` + `DEL` pattern.

4. **The regression test must cover the full window** — cancel → resume
   (restart_agent bumps run_epoch) → orchestrator restart →
   `startup_reconciliation` calls `reconstruct_tracker_from_messages` →
   assert the new epoch's tracker is empty (not resurrected from old messages).

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| R1: Fix #1 alone re-introduces #2053 | HIGH | Changes 1+2 ship together ✓ |
| R2: run_epoch namespacing touches many call sites | HIGH | Mechanical change, `run_epoch` already threaded through Pipeline model ✓ |
| R3: Per-slice reconstruction risks false consensus | HIGH | Deferred (Change 4) ✓ |
| R4: BRC history persistence must not block cancel | MEDIUM | Run in background cleanup thread ✓ |
| R5: `clear()` must be namespaced | LOW | Part of task-1-3 ✓ |
| R6: `_clear_concurrent_state` must be namespaced | MEDIUM | **ADD to plan** — thread `run_epoch` through `_clear_concurrent_state` |

## Conclusion

The task_planner's plan is architecturally sound. The only gap is R6:
`_clear_concurrent_state` must also be namespaced by `run_epoch`. This is a
straightforward addition to tasks 1-2 and 1-3. I ACK the plan with this
architectural note.
