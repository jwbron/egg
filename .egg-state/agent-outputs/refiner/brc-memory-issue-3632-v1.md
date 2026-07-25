# refiner BRC memory — issue #3632

## Pipeline context
- **Issue:** #3632 — `cancel_task(cleanup=false)` destroys consensus + message state its own contract promises to preserve
- **Pipeline ID:** `issue-3632-v1`
- **Phase:** refine
- **Role:** refiner

## Verified facts (2026-07-25)

### Claim 1: `_clear_pipeline_runtime_state` is called on ANY terminal transition including CANCELLED — VERIFIED
- `orchestrator/routes/pipelines/_routes_crud.py:717` — the call site is inside the `if pipeline.status in (CANCELLED, FAILED)` block at line 715, which runs for ALL terminal transitions including CANCELLED.
- The `cancel_task` MCP tool (`orchestrator/mcp_tools/_tasks.py:93`) sends `PATCH /api/v1/pipelines/{id}` with `{"status": "cancelled"}` and does NOT pass `cleanup=true` by default — so the default `cleanup=false` path still hits this clear.
- `_clear_pipeline_runtime_state` (`orchestrator/routes/pipelines/_lifecycle_helpers.py:158`) does exactly two things:
  1. `remove_peer_consensus_tracker(pipeline_id)` — removes the consensus tracker
  2. `get_message_store().clear(pipeline_id)` — deletes the Redis stream + counters
- Both are keyed by bare `pipeline_id` (no `run_epoch`), confirmed in:
  - `orchestrator/peer_consensus/__init__.py:226` — `_tracker_key` uses `{pipeline_id}` or `{pipeline_id}/{slice_id}`, no epoch
  - `orchestrator/redis_message_store.py:69` — `_stream_key` returns `pipeline:{pipeline_id}:messages`, no epoch

### Claim 2: `run_epoch` exists and is bumped on CANCELLED→RUNNING in `restart_agent`/`restart_phase` — VERIFIED
- `orchestrator/routes/pipelines/_routes_restart.py:337-354` — `restart_agent` bumps `run_epoch` on the CANCELLED→RUNNING transition.
- `orchestrator/routes/pipelines/_routes_restart.py:1046` — `restart_phase` also bumps `run_epoch`.
- `orchestrator/routes/phases/_advance.py:487-489` — `advance_phase` also bumps `run_epoch`.
- BUT: `run_epoch` is NOT used to namespace the tracker or message store. It's only used for thread-ownership detection (`_pipeline_superseded_by_restart`).

### Claim 3: Per-slice trackers are NOT reconstructable from messages — VERIFIED
- `orchestrator/concurrent_executor.py:1935-1934` — reconstruction is gated on `self._slice_id is None` (pipeline-level only). Per-slice trackers are recreated fresh on each iteration.
- The comment at L1929-1934 explains the #2535 rationale: per-slice reconstruction risks false consensus when a fresh slice spawns roles whose names match an already-confirmed prior slice.

### Claim 4: BRC history is persisted at slice close (#2548) but NOT at cancel — VERIFIED
- `_commit_slice_brc_history_to_integration_branch` (`orchestrator/routes/pipelines/_brc_history.py:626`) runs after slice consensus is reached, before the slice PR opens.
- `_persist_phase_brc_history` (`orchestrator/routes/pipelines/_brc_history.py:564`) runs at phase transitions (complete/advance).
- `restart_phase` at L1088-1089 does call `_persist_phase_brc_history` but with `write_per_slice=False` — so for slice-aware implement phases, it writes ONLY the unattributed sibling, NOT the per-slice CONSENSUS_* buckets. The in-flight slice's consensus record is NOT persisted to disk on cancel/restart.

### Claim 5: `start_pipeline` 409s on CANCELLED — VERIFIED
- `orchestrator/routes/pipelines/_routes_lifecycle.py:753-757` — returns 409 "Pipeline is cancelled" BEFORE the lock block at L759. The `pipeline.status = RUNNING` assignment at L801 is UNREACHABLE for a cancelled pipeline.

### Claim 6: `restart_phase` deletes per-agent worktrees — VERIFIED
- `orchestrator/routes/pipelines/_routes_restart.py:1117-1189` — deletes per-agent worktrees for the restarted phase's roles, with auto-salvage before deletion.

### Claim 7: The #2053 regression test — VERIFIED
- `orchestrator/tests/test_pipelines_api.py:1069` — `TestPipelineRuntimeStateClear` class with three tests:
  - `test_cancel_clears_runtime_state` — asserts `_clear_pipeline_runtime_state` IS called on cancel
  - `test_delete_clears_runtime_state` — asserts it's called on delete
  - `test_create_clears_runtime_state` — asserts it's called on create

### Claim 8: `startup_reconciliation` reconstructs trackers for RUNNING pipelines — VERIFIED
- `orchestrator/startup_reconciliation.py:305` — only processes RUNNING pipelines.
- `orchestrator/startup_reconciliation.py:322-323` — calls `reconstruct_tracker_from_messages` if the tracker is missing.
- This is the correct safety argument for why Changes 1+2 must ship together: with Change 1 alone, the message stream survives cancel. After resume flips the pipeline to RUNNING, an orchestrator restart triggers reconstruction which replays the retained pre-cancel CONSENSUS_* messages, resurrecting confirmations the restart had just cleared.

## Issue's four candidate changes — assessment (post-operator-feedback)

### Change 1: Do not clear runtime state on CANCELLED (only on delete + create)
- **Soundness:** The minimal fix. The clear on CANCELLED exists to defend #2053 (new pipeline reusing id inherits prior CONFIRMED consensus). #2053 is defended by the create-path clear, NOT the cancel-path clear.
- **Safety:** NOT safe alone. The correct hazard is NOT `start_pipeline` reusing `run_epoch` (that path 409s on CANCELLED — see Claim 5). The real hazard is `startup_reconciliation` replaying the retained message stream after a resume → orchestrator restart → `reconstruct_tracker_from_messages` resurrects pre-cancel CONFIRMED state. This is **same-pipeline stale-state replay**, NOT #2053.
- **Dependency on Change 2:** YES — Change 1 must ship with Change 2. Without namespacing, the retained message stream can be replayed into a reset round.

### Change 2: Namespace tracker + message stream by `run_epoch`
- **Soundness:** The architecturally correct fix. The docstring in `_lifecycle_helpers.py:163` literally says "Without a matching `run_epoch` namespace..."
- **Implementation:** Add `run_epoch` to the `Message` model (or use metadata), key `_tracker_key` and `_stream_key` by `(pipeline_id, run_epoch)`, pass `run_epoch` through all call sites.
- **Risk:** Large surface area — every caller of `get_peer_consensus_tracker`, `remove_peer_consensus_tracker`, `reconstruct_tracker_from_messages`, `get_message_store().store()`, `get_messages()`, `clear()` would need updating.

### Change 3: Persist BRC history on pause (cancel)
- **Soundness:** Belt-and-suspenders. Even with namespacing, persisting to disk on cancel makes the forensic record survive Redis loss.
- **Implementation:** Call `_persist_phase_brc_history` (or a slice-aware variant) in the cancel path. Must write per-slice CONSENSUS_* buckets (not just the unattributed sibling that `write_per_slice=False` writes), or it reproduces the gap that made the last incident's in-flight slice unrecoverable.
- **Risk:** Low. Independent of Changes 1+2. May land first per cq-3 resolution.

### Change 4: Reconstruct per-slice trackers on resume
- **Soundness:** The #2535 rationale addresses NEW slices, not resumed ones. A resumed slice with a known `run_epoch` is distinguishable.
- **Risk:** Highest complexity. Deferred per cq-3 resolution.

## Ergonomics observations — verified
- No `resume_task` MCP tool exists. The way to resume is `restart_agent` (which bumps `run_epoch` and relaunches `_run_pipeline`).
- `restart_phase` deletes per-agent worktrees (verified at L1117-1189).
- `start_pipeline` 409s on CANCELLED (verified at L753-757).

## Operator feedback (iteration 0, 2026-07-25) — APPROVED with corrections

**Approved.** The analysis is sound in its conclusion and scope, with one finding the issue did not have (Fact 5). Three notes carry into planning.

### Corrections applied:
1. **Fact 3 STRUCK** — `start_pipeline` returns 409 for CANCELLED before the lock block. The L801 assignment is unreachable. The scenario cannot occur.
2. **Correct safety argument** (from `first_principles_reviewer`) — stale-state replay by `reconstruct_tracker_from_messages` after resume → orchestrator restart. The window opens AFTER resume flips to RUNNING, not between CANCELLED and restart_phase.
3. **This is NOT #2053** — it is same-pipeline stale-state replay, a distinct bug.
4. **Change 3 must write per-slice CONSENSUS_* buckets** — not just the unattributed sibling.
5. **#3633 is out of scope** — explicitly stated, not silently omitted.

### Test requirements (from cq-2 resolution):
1. Rewrite `test_cancel_clears_runtime_state` → `test_cancel_preserves_runtime_state` (assert cancel does NOT clear).
2. Pin the CREATE path explicitly (load-bearing for #2053 safety).
3. Add NEW test: cancel → resume → orchestrator restart → assert consensus NOT resurrected.

## Proposal submitted (v1, then corrected)
- **Commit (v1):** b1523c62f906ff3b20b871e3da6899f69acba291
- **Status:** Approved with corrections — analysis draft updated to strike Fact 3 and replace with the correct safety argument.
- **Reviewers:** reviewer_refine, reviewer_agent_design, first_principles_reviewer, simplifier
- **Verdict:** all ACKed
- **Decisions registered:** cq-1 (safety finding), cq-2 (test update), cq-3 (scope adoption)
- **Decision resolutions:** all binding, 2026-07-25
