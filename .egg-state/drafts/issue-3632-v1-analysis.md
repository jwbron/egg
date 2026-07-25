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

### Fact 3: STRUCK — `start_pipeline` returns 409 for CANCELLED before reaching the lock block
- `orchestrator/routes/pipelines/_routes_lifecycle.py:753-757` — `start_pipeline` returns 409
  "Pipeline is cancelled" for CANCELLED pipelines, **before** the `with get_pipeline_state_lock(...)`
  block at L759.
- The `pipeline.status = RUNNING` assignment at L801 is therefore **unreachable** for a cancelled
  pipeline. The scenario described in the original Fact 3 (a CANCELLED pipeline resumed via
  `start_pipeline` reusing the old `run_epoch`) **cannot occur**.
- This is confirmed by the analysis's own Fact 7 ("start_pipeline 409s on CANCELLED"), so the
  original analysis contradicted itself between two adjacent numbered facts.
- **The correct safety argument** (binding, from `first_principles_reviewer` via cq-1 resolution):
  With Change 1 alone, the message stream survives a cancel while remaining keyed by bare
  `pipeline_id` (`redis_message_store.py:69`, `_stream_key`), and the tracker likewise
  (`peer_consensus/__init__.py:226`, `_tracker_key`). Resume via `restart_agent`/`restart_phase`
  deliberately RESETS consensus state — one role's or the whole phase's — and flips the pipeline
  to RUNNING. If the orchestrator then restarts, `startup_reconciliation`
  (`startup_reconciliation.py:305`) calls `reconstruct_tracker_from_messages` and replays the
  retained pre-cancel CONSENSUS_* messages, resurrecting the confirmations the restart had just
  cleared. The exposed window opens AFTER the resume flips the pipeline to RUNNING, not between
  CANCELLED and restart_phase (reconstruction skips non-RUNNING pipelines at L305).
- **This is NOT #2053.** #2053 is a *new* pipeline reusing a terminal pipeline's id; Change 1
  explicitly keeps clearing on the create path, so #2053 stays closed on its own terms. The hazard
  above is same-pipeline stale-state replay — a distinct bug. Mislabelling it as #2053 will
  produce the wrong test.

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

### Adopt: Changes 1 + 2 + 3 (together, in one slice)

1. **Stop clearing runtime state on CANCELLED** — only clear on delete and on create. This makes
   `cancel_task(cleanup=false)` truly lossless for resume via `restart_phase`/`restart_agent`.

2. **Namespace the tracker and message stream by `run_epoch`** — so stale-state replay is
   impossible by construction. The correct safety argument (NOT #2053): with Change 1 alone, the
   message stream survives a cancel keyed by bare `pipeline_id`. After resume flips the pipeline
   to RUNNING, an orchestrator restart triggers `startup_reconciliation`
   (`startup_reconciliation.py:305`) which calls `reconstruct_tracker_from_messages` and replays
   the retained pre-cancel CONSENSUS_* messages, resurrecting confirmations the restart had just
   cleared. Namespacing by `run_epoch` ensures the replayed messages belong to the old epoch and
   are not matched against the new round's tracker. The docstring in `_lifecycle_helpers.py:163`
   literally says "Without a matching `run_epoch` namespace..."

3. **Persist BRC history on cancel** — best-effort, before any clearing. Extend #2548 so cancel
   flushes the in-flight slice's history to the branch first. Per cq-3's resolution, this must
   write the per-slice CONSENSUS_* buckets (not just the unattributed sibling that
   `_persist_phase_brc_history` with `write_per_slice=False` writes), or it will reproduce
   exactly the gap that made the last incident's in-flight slice unrecoverable (Fact 5).

**Changes 1 and 2 ship together, in one slice** — per cq-3's resolution, landing 1 alone is
strictly worse than today's behaviour. Change 3 is independent and may land first.

### Defer: Change 4 (per-slice tracker reconstruction on resume)

Highest complexity, not required for the core fix. The namespacing (Change 2) already makes
resume lossless for the consensus round; per-slice tracker reconstruction is a nice-to-have for
full Redis-loss recovery. Per cq-3's resolution, the #2535 rationale (a fresh slice must not
inherit a same-named role's confirm) addresses NEW slices, not resumed ones — so the deferral is
a scope call, not a correctness one, and should be revisited.

### Out of scope: #3633

#3633 (cancel never stops the driver thread or event loop) is a distinct fix in a different code
path. Per cq-3's resolution, it stays tracked separately and is not bundled into this pipeline.

## Test Impact

Per cq-2's resolution, three test changes are required:

1. **Rewrite `test_cancel_clears_runtime_state`** (`test_pipelines_api.py:1083-1112`) to assert
   that cancel does NOT clear runtime state. Rename it (e.g. `test_cancel_preserves_runtime_state`)
   — a test called `test_cancel_clears_runtime_state` that asserts the opposite is a trap for the
   next reader.

2. **Pin the CREATE path explicitly** — `test_create_clears_runtime_state` must assert that create
   still clears. Change 1's entire safety argument for #2053 rests on create still clearing, so
   this assertion is now load-bearing rather than incidental.

3. **Add a NEW regression test** for the hazard described in cq-1's resolution: cancel → resume
   (`restart_agent` or `restart_phase`) → simulated orchestrator restart → assert the pre-cancel
   consensus state is NOT resurrected by `reconstruct_tracker_from_messages`. This is the
   regression that Change 2 exists to prevent, and without it Change 2 is untested. The existing
   `TestPipelineRuntimeStateClear` tests do not cover this path.

## Open Questions (HITL decisions registered on contract) — RESOLVED

### cq-1: Should the refiner raise a HITL decision on the critical safety finding that start_pipeline does NOT bump run_epoch on CANCELLED recovery (only on FAILED), meaning Change 1 alone would reintroduce #2053?

**Resolution (binding, 2026-07-25):** No — do not raise a further HITL gate. The premise of this
question is **void**: `start_pipeline` returns 409 for CANCELLED at `_routes_lifecycle.py:753-757`,
before the lock block at L759, so the L801 assignment is unreachable and the scenario cannot
occur. The refiner's own Fact 7 states this, so the original analysis contradicted itself.

**The conclusion survives for a different and better reason** (from `first_principles_reviewer`):
With Change 1 alone, the message stream survives a cancel keyed by bare `pipeline_id`. Resume via
`restart_agent`/`restart_phase` resets consensus and flips the pipeline to RUNNING. If the
orchestrator then restarts, `startup_reconciliation` (`startup_reconciliation.py:305`) calls
`reconstruct_tracker_from_messages` and replays the retained pre-cancel CONSENSUS_* messages,
resurrecting confirmations the restart had just cleared. The window opens AFTER resume flips to
RUNNING, not between CANCELLED and restart_phase.

**This is NOT #2053** — it is same-pipeline stale-state replay, a distinct bug. The regression
test must exercise cancel → resume → orchestrator restart → assert consensus is NOT resurrected.

### cq-2: Should the #2053 regression test (test_pipelines_api.py:1069, test_cancel_clears_runtime_state) be updated to reflect that cancel no longer clears runtime state (only delete and create do)?

**Resolution (binding, 2026-07-25):** Yes — update `test_cancel_clears_runtime_state` to assert
that cancel does NOT clear, and keep `test_delete_clears_runtime_state` /
`test_create_clears_runtime_state` asserting that delete and create still do. Rename the test
(e.g. `test_cancel_preserves_runtime_state`). Two additions required: (1) pin the CREATE path
explicitly — Change 1's safety argument for #2053 rests on create still clearing; (2) add a NEW
test for cancel → resume → orchestrator restart → assert consensus is NOT resurrected.

### cq-3: Should the refiner adopt Changes 1+2+3 from issue #3632 and defer Change 4?

**Resolution (binding, 2026-07-25):** Yes — adopt Changes 1+2+3, defer Change 4. Four constraints:
1. Changes 1 and 2 ship together in one slice — landing 1 alone is strictly worse than today.
2. Change 3 is independent and may land first — it makes the next incident diagnosable regardless
   of the other two.
3. Change 4 is deferred, not dropped — the #2535 rationale addresses new slices, not resumed ones.
4. #3633 is out of scope for this pipeline — it is a distinct fix in a different code path.

Additionally: the analysis's Fact 5 (that `restart_phase` calls `_persist_phase_brc_history` with
`write_per_slice=False`, so per-slice CONSENSUS_* buckets are never written) is a genuine finding.
Change 3 must write the per-slice buckets, or it will reproduce the gap that made the last incident's
in-flight slice unrecoverable.

## Ergonomics Observations (documented, not blocking)

- No `resume_task` MCP tool exists. The way to resume is `restart_agent` (which bumps
  `run_epoch` and relaunches `_run_pipeline`).
- `restart_phase` deletes per-agent worktrees (verified at L1117-1189).
- `start_pipeline` 409s on CANCELLED (verified at L753-756).
