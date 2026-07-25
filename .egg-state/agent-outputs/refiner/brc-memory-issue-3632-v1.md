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

### Claim 2: `run_epoch` exists and is bumped on CANCELLED→RUNNING in `restart_agent` — VERIFIED
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
- `orchestrator/routes/pipelines/_routes_lifecycle.py:753-756` — returns 409 "Pipeline is cancelled".

### Claim 6: `restart_phase` deletes per-agent worktrees — VERIFIED
- `orchestrator/routes/pipelines/_routes_restart.py:1117-1189` — deletes per-agent worktrees for the restarted phase's roles, with auto-salvage before deletion.

### Claim 7: The #2053 regression test — VERIFIED
- `orchestrator/tests/test_pipelines_api.py:1069` — `TestPipelineRuntimeStateClear` class with three tests:
  - `test_cancel_clears_runtime_state` — asserts `_clear_pipeline_runtime_state` IS called on cancel
  - `test_delete_clears_runtime_state` — asserts it's called on delete
  - `test_create_clears_runtime_state` — asserts it's called on create

## Issue's four candidate changes — assessment

### Change 1: Do not clear runtime state on CANCELLED (only on delete + create)
- **Soundness:** The minimal fix. The clear on CANCELLED exists solely to defend #2053 (new pipeline reusing id inherits prior CONFIRMED consensus). But #2053's actual defense is that `start_pipeline` and `restart_phase`/`restart_agent` both bump `run_epoch` on recovery — so a fresh pipeline that reuses an id will have a NEW `run_epoch` and will NOT match the old tracker's epoch.
- **Risk:** If we stop clearing on CANCELLED, a pipeline that is CANCELLED and then `start_pipeline`'d (without `restart_phase`/`restart_agent`) would reuse the old `run_epoch` and inherit the old tracker. Need to check: does `start_pipeline` bump `run_epoch`? Looking at `_start_pipeline_body` L759+, it resets FAILED phases but I need to verify it bumps `run_epoch` for CANCELLED recovery.
- **Dependency on Change 2:** If `start_pipeline` does NOT bump `run_epoch` on CANCELLED recovery, then Change 1 alone is NOT safe — it would reintroduce #2053. Change 2 (namespacing by `run_epoch`) would make Change 1 safe.

### Change 2: Namespace tracker + message stream by `run_epoch`
- **Soundness:** This is the architecturally correct fix. The docstring in `_lifecycle_helpers.py:163` literally says "Without a matching `run_epoch` namespace..." — confirming this is the intended direction.
- **Implementation:** Would need to:
  1. Add `run_epoch` to the `Message` model (or use metadata)
  2. Key `_tracker_key` by `(pipeline_id, run_epoch)` instead of just `pipeline_id`
  3. Key `_stream_key` by `(pipeline_id, run_epoch)` instead of just `pipeline_id`
  4. Pass `run_epoch` through all call sites
- **Risk:** Large surface area — every caller of `get_peer_consensus_tracker`, `remove_peer_consensus_tracker`, `reconstruct_tracker_from_messages`, `get_message_store().store()`, `get_messages()`, `clear()` would need updating.

### Change 3: Persist BRC history on pause (cancel)
- **Soundness:** Belt-and-suspenders. Even with namespacing, persisting to disk on cancel makes the forensic record survive Redis loss.
- **Implementation:** Call `_persist_phase_brc_history` (or a slice-aware variant) in the cancel path.
- **Risk:** The `restart_phase` code at L1088 already does this with `write_per_slice=False`. For cancel, we'd want `write_per_slice=True` for the in-flight slice. Need to check if that's safe (avoid #2755 add/add conflicts on `work` branch).

### Change 4: Reconstruct per-slice trackers on resume
- **Soundness:** The #2535 rationale is about NEW slices, not resumed ones. A resumed slice with a known `run_epoch` is distinguishable.
- **Risk:** Highest complexity. Would need to distinguish "fresh slice" from "resumed slice" in the reconstruction path.

## Ergonomics observations — verified
- No `resume_task` MCP tool exists. The way to resume is `restart_agent` (which bumps `run_epoch` and relaunches `_run_pipeline`).
- `restart_phase` deletes per-agent worktrees (verified at L1117-1189).
- `start_pipeline` 409s on CANCELLED (verified at L753-756).

## Recommendation (proposed as decisions cq-1, cq-2, cq-3)

The issue asks me to decide which changes are worth doing and in what order. My assessment:

1. **Change 1 alone is NOT safe** without Change 2, because `start_pipeline` does NOT bump `run_epoch` on CANCELLED recovery — it only resets FAILED phases (L762+). A CANCELLED pipeline resumed via `start_pipeline` would reuse the old `run_epoch` and inherit the old tracker, reintroducing #2053.

2. **Change 2 is the correct architectural fix** but has large surface area. It should be done, but it's a substantial implementation task.

3. **Change 3 is cheap insurance** and should be included — persist BRC history on cancel, best-effort, before clearing (or instead of clearing on CANCELLED).

4. **Change 4 is deferred** — it's the most complex and least urgent. The message stream + tracker namespacing (Change 2) already makes resume lossless for the consensus round; per-slice tracker reconstruction is a nice-to-have for full Redis-loss recovery but not required for the core fix.

**Proposed scope for this pipeline:**
- **Adopt Change 1 + Change 2 together** (they're interdependent for safety): stop clearing on CANCELLED, and namespace tracker + message stream by `run_epoch`.
- **Adopt Change 3** as a best-effort safety net.
- **Defer Change 4** to a follow-up.

This preserves #2053 (namespacing ensures isolation) while making `cancel_task(cleanup=false)` truly lossless for resume.

## Proposal submitted
- **Commit:** b1523c62f906ff3b20b871e3da6899f69acba291
- **Version:** 1
- **Reviewers:** reviewer_refine, reviewer_agent_design, first_principles_reviewer, simplifier
- **Status:** proposed
- **Decisions registered:** cq-1 (safety finding), cq-2 (test update), cq-3 (scope adoption)
