# Task Breakdown — Issue #3632: cancel_task(cleanup=false) destroys consensus + message state

## Pipeline context
- **Issue:** #3632 — `cancel_task(cleanup=false)` destroys the BRC consensus tracker and message history it promises to preserve
- **Pipeline ID:** `issue-3632-v1`
- **Phase:** plan
- **Role:** task_planner

## Binding scope (from refine phase, all decisions resolved and approved)

### Adopted changes
1. **Change 1:** Stop clearing runtime state on CANCELLED — only clear on delete and create.
2. **Change 2:** Namespace the consensus tracker and message stream by `run_epoch` so stale-state replay is impossible by construction.
3. **Change 3:** Persist BRC history on cancel (with per-slice CONSENSUS_* buckets, not just the unattributed sibling).

### Deferred
- **Change 4:** Per-slice tracker reconstruction on resume — deferred (highest complexity, not required for core fix; #2535 rationale addresses new slices, not resumed ones).

### Out of scope
- **#3633:** Cancel never stops the driver thread or event loop — distinct fix in a different code path, tracked separately.

### Ordering constraint (from cq-1 resolution)
**Changes 1 and 2 must ship together in one slice.** The hazard is NOT #2053 (new pipeline id reuse — that stays closed via the create-path clear). The hazard is **same-pipeline stale-state replay**: with Change 1 alone, the message stream survives cancel keyed by bare `pipeline_id`; after resume flips the pipeline to RUNNING, `startup_reconciliation` (`startup_reconciliation.py:305`) calls `reconstruct_tracker_from_messages` and replays the retained pre-cancel CONSENSUS_* messages, resurrecting confirmations the restart just cleared. Namespacing by `run_epoch` (Change 2) prevents the replayed messages from matching the new epoch's tracker.

**Change 3 is independent and may land first** — it is pure additive insurance (persisting to disk on cancel) with no dependency on the other two.

## Slice structure

### Slice 1: Changes 1 + 2 (together) — Lossless resume with stale-state-replay protection

**Goal:** Make `cancel_task(cleanup=false)` truly lossless for resume, and prevent stale-state replay after resume → orchestrator restart.

**Files to modify:**
- `orchestrator/routes/pipelines/_routes_crud.py` — Stop calling `_clear_pipeline_runtime_state` on CANCELLED (only on FAILED, delete, and create).
- `orchestrator/routes/pipelines/_lifecycle_helpers.py` — Update `_clear_pipeline_runtime_state` docstring to reflect the new behavior (clear on delete + create only, not cancel).
- `orchestrator/peer_consensus/__init__.py` — Key `_tracker_key` by `(pipeline_id, run_epoch)`.
- `orchestrator/redis_message_store.py` — Key `_stream_key` and `_counts_key` by `(pipeline_id, run_epoch)`.
- `orchestrator/routes/pipelines/_brc_history.py` — Update `_persist_phase_brc_history` and `_write_brc_history` to pass `run_epoch` through to the message store.
- `orchestrator/startup_reconciliation.py` — Pass `run_epoch` to `reconstruct_tracker_from_messages`.
- `orchestrator/concurrent_executor.py` — Pass `run_epoch` to `reconstruct_tracker_from_messages` and tracker calls.
- `orchestrator/routes/pipelines/_routes_restart.py` — Pass `run_epoch` to all tracker/message-store calls.
- `orchestrator/routes/pipelines/_routes_lifecycle.py` — Pass `run_epoch` to all tracker/message-store calls.
- `orchestrator/routes/pipelines/_routes_crud.py` (create path) — Pass `run_epoch` to all tracker/message-store calls.

**Tasks:**

- **task-1-1:** Modify `_routes_crud.py` to stop clearing runtime state on CANCELLED. The `_clear_pipeline_runtime_state` call at line 717 is inside the `if pipeline.status in (CANCELLED, FAILED)` block. Split this: only call `_clear_pipeline_runtime_state` for FAILED (and for delete/create paths). For CANCELLED, preserve the runtime state (tracker + message store) so resume is lossless.
  - **Acceptance:** `cancel_task(cleanup=false)` no longer calls `_clear_pipeline_runtime_state`; the message stream and consensus tracker survive cancel.
  - **Files:** `orchestrator/routes/pipelines/_routes_crud.py`

- **task-1-2:** Namespace the consensus tracker by `run_epoch`. Update `_tracker_key` in `peer_consensus/__init__.py` to include `run_epoch` in the key. Update all callers of `get_peer_consensus_tracker`, `remove_peer_consensus_tracker`, `reconstruct_tracker_from_messages` to pass `run_epoch`. The `run_epoch` is already bumped on CANCELLED→RUNNING in `restart_agent` (`_routes_restart.py:354`) and `restart_phase` (`_routes_restart.py:1046`).
  - **Acceptance:** Tracker keys include `run_epoch`; a resumed pipeline gets a fresh tracker namespace, so pre-cancel CONSENSUS_* messages cannot be replayed into the new round.
  - **Files:** `orchestrator/peer_consensus/__init__.py`, `orchestrator/routes/pipelines/_routes_restart.py`, `orchestrator/routes/pipelines/_routes_crud.py`, `orchestrator/routes/pipelines/_routes_lifecycle.py`, `orchestrator/concurrent_executor.py`, `orchestrator/startup_reconciliation.py`

- **task-1-3:** Namespace the message store by `run_epoch`. Update `_stream_key` and `_counts_key` in `redis_message_store.py` to include `run_epoch`. Update all callers of `get_message_store().store()`, `get_messages()`, `clear()`, `get_stream_status()` to pass `run_epoch`. The `Message` model already has a `metadata` field — `run_epoch` can be passed as a parameter to the message store methods.
  - **Acceptance:** Message stream keys include `run_epoch`; `reconstruct_tracker_from_messages` on a resumed pipeline reads only the new epoch's messages.
  - **Files:** `orchestrator/redis_message_store.py`, `orchestrator/routes/pipelines/_brc_history.py`, `orchestrator/routes/signals.py`, `orchestrator/routes/pipelines/_routes_restart.py`, `orchestrator/routes/pipelines/_routes_crud.py`, `orchestrator/routes/pipelines/_routes_lifecycle.py`, `orchestrator/concurrent_executor.py`, `orchestrator/startup_reconciliation.py`

- **task-1-4:** Update `_clear_pipeline_runtime_state` docstring and behavior. The docstring currently says it's called on terminal transitions; update it to reflect that it's only called on delete, create, and FAILED (not CANCELLED). Also update the comment in `_routes_crud.py` that references #2053.
  - **Acceptance:** Docstring accurately reflects the new call sites; #2053 safety argument is preserved (create-path clear still defends against new-pipeline id reuse).
  - **Files:** `orchestrator/routes/pipelines/_lifecycle_helpers.py`, `orchestrator/routes/pipelines/_routes_crud.py`

- **task-1-5:** Update tests. Rewrite `test_cancel_clears_runtime_state` → `test_cancel_preserves_runtime_state` (assert cancel does NOT clear). Pin the create-path clear explicitly in `test_create_clears_runtime_state`. Add a NEW regression test: cancel → resume → simulated orchestrator restart → assert consensus state is NOT resurrected by `reconstruct_tracker_from_messages`.
  - **Acceptance:** Test suite reflects the new contract: cancel preserves, create/delete still clear; new regression test covers the stale-state-replay hazard.
  - **Files:** `orchestrator/tests/test_pipelines_api.py`

- **task-1-6:** Green the boundary — `make lint + make test-all`.
  - **Acceptance:** All tests pass, no behavior change in the diff.
  - **Files:** (test files as needed)

### Slice 2: Change 3 — Persist BRC history on cancel (independent, may land first)

**Goal:** Make the in-flight slice's BRC history survive to disk on cancel, so forensic evidence is preserved even if Redis is lost. Must write per-slice CONSENSUS_* buckets (not just the unattributed sibling that `write_per_slice=False` writes).

**Files to modify:**
- `orchestrator/routes/pipelines/_routes_crud.py` — Call `_persist_phase_brc_history` (or a cancel-specific variant) in the cancel path, before any clearing.
- `orchestrator/routes/pipelines/_brc_history.py` — Add a cancel-specific persistence function that writes per-slice CONSENSUS_* buckets. The existing `_persist_phase_brc_history` calls `_write_brc_history` with `write_per_slice=False` (line ~626 in the restart path); the cancel path needs `write_per_slice=True` or a dedicated function.

**Tasks:**

- **task-2-1:** Add a cancel-specific BRC history persistence function in `_brc_history.py`. This function should call `_write_brc_history` with `write_per_slice=True` for the implement phase, ensuring per-slice CONSENSUS_* buckets are written to disk. It should be best-effort (never block cancel).
  - **Acceptance:** On cancel, the in-flight slice's BRC history (including per-slice CONSENSUS_* buckets) is written to `.egg-state/brc-history/` on the integration branch.
  - **Files:** `orchestrator/routes/pipelines/_brc_history.py`

- **task-2-2:** Wire the cancel-specific persistence into the cancel path in `_routes_crud.py`. Call the new function in the `if pipeline.status in (CANCELLED, FAILED)` block, before `_clear_pipeline_runtime_state` (which still runs for FAILED).
  - **Acceptance:** Cancel persists BRC history before any clearing; the in-flight slice's evidence survives to disk.
  - **Files:** `orchestrator/routes/pipelines/_routes_crud.py`

- **task-2-3:** Add a test verifying that cancel persists per-slice BRC history. The test should set up a pipeline with an in-flight slice that has CONSENSUS_* messages, cancel it, and assert the per-slice BRC history file exists on disk.
  - **Acceptance:** Test confirms cancel writes per-slice CONSENSUS_* buckets to `.egg-state/brc-history/`.
  - **Files:** `orchestrator/tests/test_pipelines_api.py` (or a BRC history test file)

- **task-2-4:** Green the boundary — `make lint + make test-all`.
  - **Acceptance:** All tests pass.
  - **Files:** (test files as needed)

## Slice dependency

```
slice-1 (Changes 1+2) ──► implement phase
                         │
slice-2 (Change 3) ──────┘
```

Slice 2 (Change 3) is independent and may land first. Slice 1 (Changes 1+2) is the core fix and must be one unit. Both land in the implement phase.

## Test requirements (from cq-2 resolution, binding)

1. **Rewrite `test_cancel_clears_runtime_state`** → `test_cancel_preserves_runtime_state`: assert cancel does NOT clear runtime state.
2. **Pin the CREATE path explicitly**: `test_create_clears_runtime_state` must assert create still clears (load-bearing for #2053).
3. **Add NEW regression test**: cancel → resume → orchestrator restart → assert consensus NOT resurrected by `reconstruct_tracker_from_messages`.

## Key code locations verified

| Claim | File:Line | Verified |
|-------|-----------|----------|
| `_clear_pipeline_runtime_state` called on CANCELLED | `_routes_crud.py:715-720` | ✅ The call is inside `if pipeline.status in (CANCELLED, FAILED)` |
| `_clear_pipeline_runtime_state` does two things | `_lifecycle_helpers.py:158-200` | ✅ `remove_peer_consensus_tracker` + `get_message_store().clear` |
| `run_epoch` bumped on CANCELLED→RUNNING | `_routes_restart.py:350-354` | ✅ In `restart_agent` |
| `run_epoch` bumped in `restart_phase` | `_routes_restart.py:1046` | ✅ |
| `run_epoch` NOT used to namespace tracker | `peer_consensus/__init__.py:226` | ✅ `_tracker_key` uses bare `pipeline_id` |
| `run_epoch` NOT used to namespace message stream | `redis_message_store.py:69` | ✅ `_stream_key` uses bare `pipeline_id` |
| `start_pipeline` 409s on CANCELLED | `_routes_lifecycle.py:753-757` | ✅ Before the lock block at L759 |
| `startup_reconciliation` only processes RUNNING | `startup_reconciliation.py:305` | ✅ |
| `reconstruct_tracker_from_messages` uses bare pipeline_id | `peer_consensus/__init__.py:324-331` | ✅ |
| Per-slice tracker reconstruction gated on `self._slice_id is None` | `concurrent_executor.py:1935` | ✅ |
| `_persist_phase_brc_history` called with `write_per_slice=False` in restart | `_routes_restart.py:1088-1089` | ✅ |
| Test class `TestRuntimeStateLeakageOnBranchReuse` | `test_pipelines_api.py:1069` | ✅ Three tests: cancel/delete/create |

## Risks

1. **Large surface area for Change 2 (run_epoch namespacing):** Every caller of tracker and message-store functions needs updating. This is mechanical but touches many files. Mitigated by the fact that `run_epoch` is already threaded through the pipeline model and bumped on all the right transitions.
2. **Stale-state replay regression test:** The new test (cancel → resume → orchestrator restart → assert not resurrected) requires simulating an orchestrator restart, which may need careful mocking of `startup_reconciliation`.
3. **Change 3 per-slice bucket writing:** Must ensure `write_per_slice=True` on cancel doesn't conflict with the #2755 add/add merge issue on the `work` branch. The cancel path writes to the pipeline's worktree, which should be fine since it's not a slice PR.
