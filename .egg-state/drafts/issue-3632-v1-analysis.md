# Refiner Analysis — Issue #3632

## Problem

`cancel_task(cleanup=false)` advertises itself as the pause-and-resume primitive:

> With `cleanup=false` (default) the pipeline state is preserved and can be resumed later via
> `restart_phase` or `restart_agent`.

It is not. The same call destroys the BRC consensus tracker and the pipeline's entire message
history, so a resumed pipeline restarts its consensus round from zero and its forensic record for
any slice that had not yet closed is gone. There is no non-lossy way to pause a running pipeline
today.

## Root Cause

`update_pipeline` calls `_clear_pipeline_runtime_state` on **any** transition to a terminal status,
which includes CANCELLED — the status `cancel_task(cleanup=false)` sets
(`orchestrator/routes/pipelines/_routes_crud.py:717`):

```python
# Evict per-pipeline runtime state (consensus tracker, legacy
# consensus evaluator, message store) so a future pipeline
# that reuses this id (same branch) does not inherit this
# run's CONFIRMED consensus or message history (#2053).
_pkg._clear_pipeline_runtime_state(pipeline_id, reason=f"pipeline_{pipeline.status.value}")
```

That helper (`orchestrator/routes/pipelines/_lifecycle_helpers.py:158`) does exactly two things:

```python
remove_peer_consensus_tracker(pipeline_id)   # the consensus state
get_message_store().clear(pipeline_id)       # the Redis message stream
```

Both are precisely what a resume would need. The message-store clear is explicitly there to stop
`reconstruct_tracker_from_messages` from rebuilding the tracker — i.e. it deliberately closes the
one recovery path that could have made resume lossless.

## Verified Facts

### Fact 1: `_clear_pipeline_runtime_state` is called on ANY terminal transition including CANCELLED
- `orchestrator/routes/pipelines/_routes_crud.py:717` — the call site is inside the
  `if pipeline.status in (CANCELLED, FAILED)` block at line 715, which runs for ALL terminal
  transitions including CANCELLED.
- The `cancel_task` MCP tool (`orchestrator/mcp_tools/_tasks.py:93`) sends `PATCH
  /api/v1/pipelines/{id}` with `{"status": "cancelled"}` and does NOT pass `cleanup=true` by
  default — so the default `cleanup=false` path still hits this clear.
- `_clear_pipeline_runtime_state` (`orchestrator/routes/pipelines/_lifecycle_helpers.py:158`)
  does exactly two things:
  1. `remove_peer_consensus_tracker(pipeline_id)` — removes the consensus tracker
  2. `get_message_store().clear(pipeline_id)` — deletes the Redis stream + counters
- Both are keyed by bare `pipeline_id` (no `run_epoch`), confirmed in:
  - `orchestrator/peer_consensus/__init__.py:226` — `_tracker_key` uses `{pipeline_id}` or
    `{pipeline_id}/{slice_id}`, no epoch
  - `orchestrator/redis_message_store.py:69` — `_stream_key` returns
    `pipeline:{pipeline_id}:messages`, no epoch

### Fact 2: `run_epoch` exists and is bumped on CANCELLED→RUNNING in restart_agent/restart_phase
- `orchestrator/routes/pipelines/_routes_restart.py:337-354` — `restart_agent` bumps
  `run_epoch` on the CANCELLED→RUNNING transition.
- `orchestrator/routes/pipelines/_routes_restart.py:1046` — `restart_phase` also bumps
  `run_epoch`.
- `orchestrator/routes/phases/_advance.py:487-489` — `advance_phase` also bumps `run_epoch`.
- BUT: `run_epoch` is NOT used to namespace the tracker or message store. It's only used for
  thread-ownership detection (`_pipeline_superseded_by_restart`).

### Fact 3: `start_pipeline` does NOT bump `run_epoch` on CANCELLED recovery
- `orchestrator/routes/pipelines/_routes_lifecycle.py:762-798` — `start_pipeline` only bumps
  `run_epoch` on the FAILED recovery path (L796-798). For CANCELLED, it just sets
  `pipeline.status = RUNNING` at L801 without bumping `run_epoch`.
- **This is the critical safety finding**: if we stop clearing on CANCELLED (Change 1) without
  also namespacing by `run_epoch` (Change 2), a CANCELLED pipeline resumed via `start_pipeline`
  would reuse the old `run_epoch` and inherit the old tracker, reintroducing #2053.

### Fact 4: Per-slice trackers are NOT reconstructable from messages
- `orchestrator/concurrent_executor.py:1935-1934` — reconstruction is gated on
  `self._slice_id is None` (pipeline-level only). Per-slice trackers are recreated fresh on each
  iteration.
- The comment at L1929-1934 explains the #2535 rationale: per-slice reconstruction risks false
  consensus when a fresh slice spawns roles whose names match an already-confirmed prior slice.

### Fact 5: BRC history is persisted at slice close (#2548) but NOT at cancel
- `_commit_slice_brc_history_to_integration_branch`
  (`orchestrator/routes/pipelines/_brc_history.py:626`) runs after slice consensus is reached,
  before the slice PR opens.
- `_persist_phase_brc_history` (`orchestrator/routes/pipelines/_brc_history.py:564`) runs at
  phase transitions (complete/advance).
- `restart_phase` at L1088-1089 does call `_persist_phase_brc_history` but with
  `write_per_slice=False` — so for slice-aware implement phases, it writes ONLY the unattributed
  sibling, NOT the per-slice CONSENSUS_* buckets. The in-flight slice's consensus record is NOT
  persisted to disk on cancel/restart.

### Fact 6: The #2053 regression test
- `orchestrator/tests/test_pipelines_api.py:1069` — `TestPipelineRuntimeStateClear` class with
  three tests:
  - `test_cancel_clears_runtime_state` — asserts `_clear_pipeline_runtime_state` IS called on cancel
  - `test_delete_clears_runtime_state` — asserts it's called on delete
  - `test_create_clears_runtime_state` — asserts it's called on create

### Fact 7: `start_pipeline` 409s on CANCELLED
- `orchestrator/routes/pipelines/_routes_lifecycle.py:753-756` — returns 409 "Pipeline is
  cancelled".

### Fact 8: `restart_phase` deletes per-agent worktrees
- `orchestrator/routes/pipelines/_routes_restart.py:1117-1189` — deletes per-agent worktrees for
  the restarted phase's roles, with auto-salvage before deletion.

## Proposed Scope

### Adopt: Changes 1 + 2 + 3

1. **Stop clearing runtime state on CANCELLED** — only clear on delete and on create. This makes
   `cancel_task(cleanup=false)` truly lossless for resume via `restart_phase`/`restart_agent`.

2. **Namespace the tracker and message stream by `run_epoch`** — so #2053 is closed by
   construction. A fresh pipeline reusing an id from a prior terminal run will have a new
   `run_epoch` and will NOT match the old tracker's epoch. This is the architecturally correct
   fix — the docstring in `_lifecycle_helpers.py:163` literally says "Without a matching
   `run_epoch` namespace..."

3. **Persist BRC history on cancel** — best-effort, before any clearing. Extend #2548 so cancel
   flushes the in-flight slice's history to the branch first.

### Defer: Change 4 (per-slice tracker reconstruction on resume)

Highest complexity, not required for the core fix. The namespacing (Change 2) already makes
resume lossless for the consensus round; per-slice tracker reconstruction is a nice-to-have for
full Redis-loss recovery but not required for the core fix.

## Test Impact

The #2053 regression test at `test_pipelines_api.py:1069` (`test_cancel_clears_runtime_state`)
explicitly asserts `_clear_pipeline_runtime_state` IS called on cancel. This test must be updated
to assert that cancel does NOT clear (only delete and create do).

## Open Questions (HITL decisions registered on contract)

### cq-1: Should the refiner raise a HITL decision on the critical safety finding that start_pipeline does NOT bump run_epoch on CANCELLED recovery (only on FAILED), meaning Change 1 alone would reintroduce #2053?

**Options:**
- opt-1: Yes — raise as HITL decision, require operator sign-off before implementing Change 1 without Change 2
- opt-2: No — document in proposal, let implementer handle
- opt-3: Defer to plan phase

**Refiner position:** The refiner has documented this finding in the analysis draft (see Fact 3
above) and in the proposal's risk_considered field. The finding is a hard safety constraint:
Change 1 (stop clearing on CANCELLED) is only safe when paired with Change 2 (run_epoch
namespacing). This is not a question of preference — it's a correctness requirement. The refiner
recommends **opt-1**: raise this as a HITL decision so the operator explicitly acknowledges the
interdependency before implementation proceeds.

### cq-2: Should the #2053 regression test (test_pipelines_api.py:1069, test_cancel_clears_runtime_state) be updated to reflect that cancel no longer clears runtime state (only delete and create do)?

**Options:**
- opt-1: Yes — update test to assert cancel does NOT clear, only delete+create do
- opt-2: Keep test as-is, add new test for cancel-not-clearing
- opt-3: Defer test changes to implement phase

**Refiner position:** The test at `test_pipelines_api.py:1083-1112` explicitly asserts
`_clear_pipeline_runtime_state` IS called on cancel. If Change 1 is adopted, this test must be
updated — it currently encodes the exact behavior we are changing. The refiner recommends
**opt-1**: update the test to assert cancel does NOT clear, and keep the delete/create tests
as-is.

### cq-3: Should the refiner adopt Changes 1+2+3 from issue #3632 (stop clearing runtime state on CANCELLED, namespace tracker+message stream by run_epoch, persist BRC history on cancel) and defer Change 4 (per-slice tracker reconstruction on resume)?

**Options:**
- opt-1: Yes adopt Changes 1+2+3 defer Change 4
- opt-2: Adopt only Change 1 minimal fix
- opt-3: Adopt all four changes full fix
- opt-4: Adopt Changes 1+2+3+4 but reorder

**Refiner position:** The refiner recommends **opt-1**. Changes 1+2 are interdependent for
safety (Change 1 alone reintroduces #2053). Change 3 is cheap insurance. Change 4 is the most
complex and least urgent — per-slice tracker reconstruction is a nice-to-have for full Redis-loss
recovery but not required for the core lossless-resume fix.

## Ergonomics Observations (documented, not blocking)

- No `resume_task` MCP tool exists. The way to resume is `restart_agent` (which bumps
  `run_epoch` and relaunches `_run_pipeline`).
- `restart_phase` deletes per-agent worktrees (verified at L1117-1189).
- `start_pipeline` 409s on CANCELLED (verified at L753-756).
