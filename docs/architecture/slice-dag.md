# Slice-DAG Implement Phase

> Status: shipped (#2137, HITL decision-20 opt-2). Per the operator's
> resolution of decision-20, the implement-phase run-loop wire-up landed
> in this PR rather than being deferred. The slice loop drives
> `SliceScheduler` waves, creates each slice's integration branch on
> origin via the gateway *before* agents spawn, runs the BRC consensus
> per slice, opens a per-slice PR on consensus reach, and runs the
> stacked-PR reconciler in a background thread. The reconciler's
> `list_open_prs` / `list_remote_branches` callables are bound to live
> gateway helpers — it is no longer a no-op.
>
> **Two trade-offs scoped to #2199** (per-slice MCP control verbs
> follow-up):
>
> 1. The `EGG_PIPELINE_ID` override that scopes BRC `CONSENSUS_*`
>    messages also currently scopes the agent-emitted `HEARTBEAT` and
>    `OVERSEER_ALERT` traffic to the slice tracker. The hybrid scheme
>    promised by decision-14 (cross-slice telemetry routes through the
>    bare `pipeline_id`) is honoured *partially* — the orchestrator-side
>    cascade emission and log line in `_run_implement_phase_slices`
>    provide the always-on fallback so deadlocks remain visible at the
>    pipeline level. Full fan-out requires a CLI-side message-type-aware
>    router (#2199).
> 2. The `record_cycle` two-tier `max_cycles` accounting and the
>    `hitl_escalator` hook on `SliceScheduler` are public API and unit-
>    tested, but the slice run loop does not yet call `record_cycle`
>    on each BRC re-proposal. The env knobs
>    `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` / `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES`
>    are read but not exercised today; #2199 wires the trip flag through
>    the BRC re-proposal loop.

The implement phase used to run as a single monolithic agent team on one
branch through one BRC consensus. Tickets large enough to fill the context
window (empirically ~33K LOC / 41 files per #2105) caused compaction and
quality drops. The slice-DAG model replaces that single-team flow with a
**forest of slices** — each slice has its own integration branch, agent
team, BRC consensus, and pull request. Slice PRs stack along the DAG's
linear chains.

## Vocabulary

| Term | Meaning |
|------|---------|
| **Slice** | One independently-implementable unit of work. Renamed from `Phase` in #2137. Each slice has tasks, dependencies, an integration branch, an agent team, and (eventually) a PR. |
| **Forest** | The slice DAG must be a forest: every slice has **at most one** DAG parent. Multi-parent slices are rejected at plan ingestion. |
| **Wave** | A set of slices whose dependencies are all satisfied. Slices in the same wave can run concurrently. |
| **Stacked PR** | A child slice's PR targets the parent slice's integration branch (not main). Reviewers land slices incrementally as parents merge. |
| **Cascade** | When a slice fails, after a grace window its transitive descendants are marked `BLOCKED_ON_FAILED_DEPENDENCY` rather than running pointlessly. |

## Contract Schema (Phase → Slice)

The contract field `phases[]` was renamed to `slices[]`. Backwards
compatibility is preserved at every layer:

- **`Phase = Slice`** — class alias. Old imports keep working.
- **`PhaseStatus = SliceStatus`** — enum alias. Status values
  (`pending` / `in_progress` / `complete` / `blocked`) are unchanged so
  on-disk JSON loads without translation.
- **`Contract.phases`** — read/write property proxy to `Contract.slices`.
  Reading and assigning both work.
- **Load-time migration shim** — when a pre-#2137 contract JSON containing
  `phases: [...]` is loaded, a Pydantic `model_validator(mode="wrap")`
  rewrites `phases[]` → `slices[]` and each item's `phase-N` ID → `slice-N`,
  including dependency references. The original payload is stashed on the
  private `_legacy_phases` attribute (cleared on round-trip so a re-loaded,
  already-migrated contract does not re-run the migration).

The slice ID pattern accepts both forms: `^(?:slice|phase)-[0-9]+$`.
Helpers like `Contract.get_slice(...)` normalise lookups so callers can
pass either.

### New `Slice` fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `serialized_chain_order` | `list[str]` | `[]` | Architect-emitted ordering for would-be multi-parent slices (#2809). When the architect identifies a slice that would naturally have >1 parents, it serialises the upstream cluster into a chain and records the chosen order on the downstream slice. |
| `parent_branch_at_creation` | `str \| None` | `None` | Git branch the slice's integration branch was forked off when its worktree was provisioned. Read by the stacked-PR reconciler when the parent's branch has been deleted by a merge so it can compute the correct rebase target. |

## Plan Parser & Forest Validation

The plan parser (`shared/egg_contracts/plan_parser.py`) accepts either
`slices:` (canonical post-#2137) or `phases:` (legacy alias) at the top of
the `# yaml-tasks` block. If both are present a warning is emitted and
`slices:` wins.

Forest validation lives in `shared/egg_contracts/plan_parser.py` as the
public helper:

```python
def validate_forest(slices: list[Slice]) -> list[str]:
    """Return one human-readable error string per multi-parent slice.

    Empty list = valid forest (≤1 DAG parent per slice).
    """
```

The orchestrator's `_populate_contract_from_plan` route invokes
`validate_forest()`, stashes any returned errors on
`Contract.plan_review_feedback` (so the plan reviewer NACKs the architect, #2809),
**and then raises a structured `ForestValidationError`**. Slices are not
written to the contract in this case — leaving `contract.slices` empty
so downstream code visibly fails fast.

```python
class ForestValidationError(Exception):
    status_code: int = 422
    errors: list[str]
    def to_response(self) -> tuple[dict[str, object], int]: ...
```

`ForestValidationError.to_response()` returns the canonical
`({"error": "forest_violation", "errors": [...]}, 422)` Flask shape so any
future route that ingests a plan in-band can catch it and return a 422
with the inlined errors. The internal `_populate_contract_from_plan_safe`
wrapper catches `ForestValidationError` with a dedicated structured
warning (separate audit-log discriminator from the catch-all
`except Exception`) and re-raises `ForestValidationError` — only generic
exceptions are swallowed by the safe wrapper.

A typical error message:

```
Slice 'slice-3' has 2 DAG parents (['slice-1', 'slice-2']);
the implement-phase slice DAG must be a forest (≤1 parent per slice).
Serialise the upstream cluster into a chain and record the chosen order
on this slice's 'serialized_chain_order' field — see issue #2137 plan
TASK-2-3 for the auto-serialization rule.
```

### Cycle detection

`validate_forest()` also runs a DFS-based cycle check
(`_detect_cycles` in `plan_parser.py`) so contracts whose `dependencies`
form a cyclic chain are rejected with the same structured-error
treatment. A cycle (e.g. `slice-1 → slice-2 → slice-1`) would otherwise
deadlock the slice run loop's `while not scheduler.all_done()` because no
slice would ever reach READY. Each cycle is reported once with its full
chain:

```
Slice DAG contains a cycle: slice-1 → slice-2 → slice-1.
Slices form an acyclic forest — break the cycle by removing one of the
dependencies, or merge the cycle members into a single slice.
```

Multi-parent and cyclic violations are reported in the same returned
list, so a single populator pass surfaces every structural defect at
once.

## DependencyGraph generification

`shared/egg_contracts/dependency_graph.py` was generified in #2137.
`DependencyNode`, `ExecutionWave`, `ExecutionPlan`, and `DependencyGraph`
are now generic over the node-key type. The classes use **PEP 695 generic
class syntax** (`class DependencyGraph[NodeT: Hashable]: ...`) — matching
`pyproject.toml`'s `target-version = "py314"` — rather than the older
`Generic[NodeT]` + `TypeVar` shape. Existing agent-role-keyed callers
continue to use `DependencyGraph[AgentRole]`; the slice scheduler uses
`DependencyGraph[str]` with slice IDs as node keys. One implementation,
two parameterisations.

## SliceScheduler

`orchestrator/slice_scheduler.py` is the orchestrator-side glue between the
contract and the implement-phase run loop. It is intentionally pure-Python
(no I/O, no gateway calls) so its behaviour is deterministic in unit
tests.

### Lifecycle states (`SchedulerSliceState`)

```
PENDING ─────► READY ─────► RUNNING ─────► COMPLETE
                  ▲             │
                  │             ▼
                  │           FAILED ─── (60s grace) ───► descendants
                  │                                    BLOCKED_ON_FAILED_DEPENDENCY
                  └── TEARDOWN ◄─── teardown_slice()
                          │
                          ▼
                     respawn_slice()
```

The runtime view (above) is **distinct** from `SliceStatus` (the contract
field), which only tracks declarative state.

### Public API

| Method | Purpose |
|--------|---------|
| `iter_ready() → Iterator[(slice_id, parent_slice_id)]` | Yield up to `max_parallel_slices - in_flight` slices whose dependencies are satisfied. |
| `mark_spawned(slice_id)` | Record that the agent team has been spawned (transitions to RUNNING). |
| `record_cycle(slice_id) → bool` | Record a BRC re-proposal cycle. Returns `True` when either the local-per-slice or pipeline-global cap trips; `True` triggers HITL escalation via the injected `hitl_escalator`. |
| `record_complete(slice_id)` | Mark COMPLETE and unblock children. |
| `record_failure(slice_id)` | Mark FAILED and arm the failure-cascade timer. |
| `poll_cascades() → list[CascadeEvent]` | Drain expired cascades; mark transitive descendants `BLOCKED_ON_FAILED_DEPENDENCY` and return events for the run loop to act on (mark contract, emit `OVERSEER_ALERT`). |
| `teardown_slice(slice_id) → bool` | Mark TEARDOWN; called by the (future) `restart_slice` MCP verb. |
| `respawn_slice(slice_id) → bool` | Reset to READY after teardown. |
| `get_slice_status(slice_id) → SliceRuntime \| None` | Per-slice runtime snapshot. |
| `all_done() → bool` | True once every slice has reached a terminal state. |

Every public method acquires the scheduler's `RLock`, so callers may invoke
them from arbitrary threads (the BRC tracker, the cascade poller, and the
run loop all run in different threads). The HITL escalator hook is one
deliberate exception — see "Two-tier `max_cycles` accounting" below.

The constructor lazy-resolves `EGG_ORCH_*` defaults from
`orchestrator.env_config` whenever the corresponding kwarg is left as
`None`. A bare `SliceScheduler(contract)` therefore picks up the
operator's env-var overrides without any explicit threading; callers can
still pin values explicitly (the existing test fixtures do).

### Constructor-time forest revalidation

`SliceScheduler.__init__` calls `validate_forest(contract.slices)` and
raises `ValueError` if any multi-parent or cyclic violations are
returned. This is defense-in-depth on top of plan ingestion: contracts
that bypass `_populate_contract_from_plan` (legacy state-branch
restores, manual `egg-contract` edits, in-process construction from
fixtures) still hit the gate before the run loop spins. The error
message is the same `"; "`-joined string `validate_forest` returns so
the run loop's caller can route it directly to HITL or
`OVERSEER_ALERT`.

### Two-tier `max_cycles` accounting

Each slice has a **local** cap on BRC re-proposal cycles before HITL
escalation, and the pipeline has a **global** cap on summed cycles across
all slices. Either trip calls `hitl_escalator(slice_id, reason)`.

| Default | Env var |
|---------|---------|
| local 3 | `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` |
| global 10 | `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` |

`record_cycle` captures the escalation arguments under the scheduler's
lock and then **invokes `hitl_escalator` after releasing the lock**. The
escalator may issue HTTP / contract-write I/O whose latency would
otherwise serialise every other scheduler operation; a >180 s round trip
would also trip the orchestrator's stuck-phase-transition timeout. (Same
pattern as #2012 for the BRC tracker.)

> **Status: deferred to #2199.** The `record_cycle` invocation point is
> not yet wired into the slice run loop. `_run_implement_phase_slices`
> tracks per-slice exit codes but does not call `record_cycle` on each
> BRC re-proposal; the env knobs are read at constructor time but the
> trip path is dead code today. The hook itself is public, unit-tested,
> and lock-safe — #2199 (per-slice MCP control verbs follow-up) closes
> the loop on the BRC re-proposal counter and wires the
> `hitl_escalator` argument through to the orchestrator's HITL
> escalation surface.

### Failure cascade

When `record_failure(slice_id)` is called, the scheduler arms a timer for
`failure_grace_seconds` (default 60s, env
`EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS`). On expiry, the next
`poll_cascades()` walks the downstream subtree (BFS via dependents) and
marks every transitive descendant `BLOCKED_ON_FAILED_DEPENDENCY`. **Sibling
slices are unaffected** — only the failed slice's downstream subtree is
blocked, and one `OVERSEER_ALERT` (anomaly type `slice-cascade-block`) is
emitted with the full subtree.

`OVERSEER_ALERT` emission is handled by the orchestrator-side run loop,
not by the scheduler. After every `iter_ready` pass,
`_run_implement_phase_slices` calls `scheduler.poll_cascades()`,
logs each event, and pushes a structured `OVERSEER_ALERT` directly into
the in-process `message_store` keyed on the bare `pipeline_id` (TASK-3-4
emission path):

```python
{
  "subject": "slice-cascade-block: <failed_slice_id>",
  "metadata": {
    "anomaly": "slice-cascade-block",
    "priority": "high",
    "failed_slice_id": "<slice-id>",
    "blocked_subtree": [<descendant slice IDs>],
  },
  "phase": "implement",
}
```

This is the always-on safety net: under the v4/v5/v6 `EGG_PIPELINE_ID`
override, agent-emitted `OVERSEER_ALERT` traffic routes through the
slice tracker rather than the pipeline tracker (the trade-off scoped to
#2199 — see status callout). The orchestrator-side emission keeps
cascade visibility flowing through `pipeline_id` regardless, so the
human operator's overseer surface still sees the deadlock even if every
agent in the failed subtree has already shut down.

Cascades can be unwound. If HITL resolves the underlying failure and the
operator calls `teardown_slice` → `respawn_slice` → eventually
`record_complete` on the failed slice, `_unblock_children` re-promotes
both `PENDING` **and** `BLOCKED_ON_FAILED_DEPENDENCY` children whose
remaining dependencies are satisfied. (Without this promotion the
descendants of a respawned-and-completed parent would stay permanently
blocked even after recovery — the failed cascade was previously a
one-way trip.)

## Per-slice branches & BRC trackers

`ConcurrentPhaseExecutor.get_worktree_branch(role, *, slice_id=None)` is
slice-aware. **In slice mode, every agent in a slice shares the slice's
integration branch.** This was a deliberate v6 design correction: an
earlier per-role suffix shape (`egg/issue-N/slice-M/{role}/work`) caused
the per-slice PR's diff to render empty, because the integration branch
opened on origin pointed at the parent's tip while every agent commit
lived on a per-role sibling branch GitHub does not see in the PR.

| Mode | `slice_id` | Result |
|------|------------|--------|
| Pipeline mode (pre-#2137 / non-slice phases) | `None` (default) | `pipeline.branch` or `egg/issue-N/work` (tip pushed to `<id>/work` since #2399). |
| Slice mode (post-v6) | `"slice-2"` or `"2"` | `egg/issue-N/slice-2` — **shared by every role in the slice**. |

> **The slice is the unit of isolation, not the role within the slice.**
> Cross-slice isolation is preserved by the per-slice integration branch;
> within a slice, all agents collaborate on one history — the same
> shared-branch model the non-slice flow has always used, just scoped
> per slice.

The shared-branch model implicitly relies on the gateway's multi-agent
push attribution surface
(`gateway/git_client.py:get_attributed_changed_files_in_push`) to attribute
each commit on the integration branch to the agent that pushed it. The
file-boundary allowlist in the gateway is therefore still enforced
per-role even though every role in the slice shares one head ref.

A helper `get_slice_integration_branch(slice_id)` returns the shared
integration branch for a slice's BRC: `egg/issue-N/slice-M`. Roots base
their integration branch off the pipeline branch directly
(`egg/issue-N/work` since #2399); child slices base off their parent
slice's integration branch. Slice integration branches live as siblings
of the pipeline tip under `egg/issue-N/` — git rejects a leaf ref at
`egg/issue-N` and children at `egg/issue-N/slice-M` simultaneously
("directory file conflict"), so the pipeline tip was moved to
`egg/issue-N/work` in #2399 to free the namespace.

Both helpers `re.fullmatch` the normalised slice id against
`r"slice-[0-9]+"` before embedding it in a git ref. The contract-layer
pydantic regex already enforces this on the source of truth, but the
executor helpers sit on the gateway-facing surface — re-validating closes
the seam against any future caller that forgets the upstream check
(defense-in-depth, per the security reviewer's ACK suggestion).

The slice run loop creates each slice's integration branch on origin
**before agents spawn** by calling
`GatewayClient.create_slice_integration_branch(...)`, which (1) fetches
the parent ref so the commit object is locally reachable, (2) resolves
the parent branch to a SHA on origin via `git ls-remote`, then (3)
pushes `<parent_sha>:refs/heads/<integration_branch>` through the existing
per-agent `/api/v1/git/push` allowlist (no new privileged endpoint;
decision-15 invariant preserved). Pushing by SHA rather than ref name
avoids local-ref resolution failures in the orchestrator's per-pipeline
worktree, which is checked out on `<branch>/work` and carries no local
ref matching `<parent_branch>` (#2393). On creation failure the run
loop calls `record_failure(slice_id)` and returns early — agents are
not spawned against a missing integration branch.

The BRC tracker layer (`orchestrator/peer_consensus.py`) was extended so
`create/get/remove_peer_consensus_tracker(pipeline_id, slice_id=None)` keys
the registry under the composite key `{pipeline_id}/{slice_id}`. Per-slice
`CONSENSUS_*` state is naturally isolated. Refine-phase decision-14
called for `HEARTBEAT` / `OVERSEER_ALERT` to keep flowing through the
bare `pipeline_id`; in practice the `EGG_PIPELINE_ID` override route on
the agent CLI sends *every* outbound signal through the slice tracker
today (see status callout — full hybrid fan-out is scoped to #2199).
The orchestrator-side cascade emission and run-loop log lines are the
always-on `pipeline_id`-scoped fallback so deadlocks remain visible at
the pipeline level regardless.

## Implement-phase run loop

`_run_implement_phase_slices` in `orchestrator/routes/pipelines.py` is
the run-loop entry point that drives a slice DAG. The state-machine
shape:

1. **Construct** a `SliceScheduler` from `contract.slices`. The
   constructor's forest revalidation runs first; multi-parent or
   cyclic contracts fail fast here before any container spawns.
2. **Start** the stacked-PR reconciler in a daemon thread bound to
   `GatewayClient.list_open_prs` and
   `GatewayClient.list_remote_branches` plus
   `GatewayClient.rebase_onto`. The thread polls on
   `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` and stops when
   the run loop's `reconciler_stop` event is set in the `finally`
   block.
3. **For each wave** (`ExecutionWave` from the generified
   `DependencyGraph`):
    1. Drain `scheduler.iter_ready()` until the
       `EGG_ORCH_MAX_PARALLEL_SLICES` budget is exhausted.
    2. Spawn the slices in the wave **in parallel** via
       `concurrent.futures.ThreadPoolExecutor(max_workers=len(ready_batch))`.
       The pool's max-workers mirrors the budget that
       `iter_ready` already enforces, so the executor's concurrency
       cap and `EGG_ORCH_MAX_PARALLEL_SLICES` agree.
    3. Each worker thread runs `_run_one_slice(slice_id, parent_id)`:
       persists `Slice.parent_branch_at_creation` to the contract,
       creates the integration branch via the gateway, calls
       `_run_concurrent_phase(slice_id=...)` to spawn the slice's
       agent team, awaits BRC consensus, and on consensus reach calls
       `GatewayClient.create_slice_pr` with `base` resolved from the
       slice's DAG parent (root → pipeline branch; child → parent's
       integration branch). On failure the worker calls
       `scheduler.record_failure(slice_id)`, which arms the cascade
       timer.
    4. After the wave completes, `scheduler.poll_cascades()` drains any
       expired cascades and emits the orchestrator-side
       `OVERSEER_ALERT` for each (see "Failure cascade").
4. **Loop** until `scheduler.all_done()` returns true (every slice in
   a terminal state).
5. **Tear down** the reconciler thread and aggregate per-slice exit
   codes into the run-loop's return value.

Per-slice agent teams are spawned via the existing
`ConcurrentPhaseExecutor` machinery with `slice_id` plumbed through:

- `spawn_all` registers the BRC tracker under the nested
  `{pipeline_id}/{slice_id}` key.
- `_spawn_agent` resolves the head ref via
  `get_worktree_branch(role, slice_id=...)` (the v6 shared-branch
  shape).
- `check_consensus` looks up the slice-scoped tracker first.

`_run_concurrent_phase` mutates a shallow copy of the sandbox env to set
`EGG_PIPELINE_ID = "{pipeline_id}/{slice_id}"` and exports
`EGG_SLICE_ID`. BRC handlers in the sandbox read `EGG_SLICE_ID` and
attach `slice_id` to every `CONSENSUS_*` payload (#2403), so the
orchestrator routes each signal to the per-slice tracker
(`peer_consensus._tracker_key` composes `{pipeline_id}/{slice_id}`)
rather than the bare pipeline tracker.
`_handle_brc_consensus_timeout` also receives `slice_id` so the
timeout / stuck-phase handler operates on the correct tracker.

## Stacked-PR creation

`GatewayClient.create_slice_pr(pipeline_id, repo, *, slice_id, slice_name,
slice_tasks, head, base, program_title=None, program_description=None,
program_test_plan=None, program_manual_steps=None,
program_deferred_actions=None, terminal_slice_id=None, slice_index=None,
slice_count=None, slice_files_affected=None, context_pr_number=None, ...)`
opens one PR per slice (#2745). Slice PRs are scoped to their own slice:
the body shows the slice subject, files affected, and full task
descriptions with acceptance criteria. Strategic context (analysis doc,
plan doc, refine/plan BRC history) lives on the base/context PR opened
by #2548, which slice PRs link to via `context_pr_number`. The terminal
slice keeps the umbrella treatment (program-level test plan, manual
steps, pre-merge obligations) because it is the merge gate.

- **Title.** `[<program-slug>][<position>] <subject>`, capped at 70
  chars (titles longer than that get truncated to `title[:67] + "..."`).
  `program-slug` is derived from `pipeline_id`: `issue-<N>` pipelines
  collapse to `issue-<N>` (version suffix dropped); `pipeline-<hash>`
  pipelines keep a truncated prefix. `position` is `slice-N/M` for
  non-terminal slices (1-based index over total declared slice count)
  and `merge-gate` for the terminal slice. `subject` is `slice_name`
  for non-terminals and `program_title` for the terminal. When the
  70-char cap fires, the slug + position marker are preserved and the
  `subject` is what gets truncated first — on hash-id pipelines the
  slug + position eat ~20–30 chars, so subjects on long-named slices
  can lose their tail (see `_derive_program_slug` for the budget). When
  `program_title` is empty (older contracts / planner skipped the
  field), every slice falls back to the deterministic
  `{slice_id}: {slice_name}` form (#2539).
- **Body (non-terminal, `context_pr_number` present).** Optional
  1-line program blurb (first sentence of `program_description`) →
  `**Base PR:** #<context_pr_number>` → `## This slice` (slice name,
  files affected, full task descriptions + acceptance criteria) →
  `## Stack` (position, base PR, base branch).
- **Body (terminal — merge gate).** `> Program-level umbrella PR …`
  banner → `program_description` → `## ⚠️ Pre-merge Obligations` /
  `## ✅ Resolved within this PR` (when `program_deferred_actions`
  is non-empty, rendered by `orchestrator/pr_obligations.py`) →
  `## This slice` → `## Test Plan` → `## Manual Steps` → `## Stack`.
- **Body (non-terminal, no `context_pr_number` — UX backstop).**
  Falls back to inlining the full program narrative so the slice PR
  remains reviewable as a standalone diff against `/work`. The stack
  is still unmergeable in this state (no base PR for `work → main`);
  the fallback is a presentational fix only.

The `## Stack` block is the human-facing footer, but `_format_stack_block`
also appends a legacy plain-text line (``Slice <slice-id> of pipeline
<pipeline>. Stacked on top of `<base>`.``) after it, preserved so
existing tooling / scrapers that grep for that exact phrase keep
working.

Task bullets carry full descriptions (the pre-#2745 300-char
truncation is removed) and a nested `Acceptance criteria:` line when
`task.acceptance_criteria` is set.

`program_deferred_actions` is **terminal-only** by convention — the
merge gate is the last-to-merge PR in the stack, so obligations live
on exactly one PR across the chain. Each non-terminal body branch
asserts `program_deferred_actions is None` so a mis-routed obligations
payload fails fast instead of being silently dropped (#2354 / #2746).

The implement-phase run loop (`_run_implement_phase_slices` in
`orchestrator/routes/pipelines.py`) selects the terminal slice and
threads the kwargs:

1. Compute `depended_on = {dep for slice in contract.slices for dep in slice.dependencies}`.
2. `terminal_ids = [s.id for s in contract.slices if s.id not in depended_on]`.
3. `chosen_terminal = terminal_ids[-1]` (last in declared order — see
   the multi-terminal forest note below).
4. For **every** slice: compute 1-based `slice_index` (position in
   `contract.slices`) and total `slice_count`; collect
   `slice_files_affected` as the union of `task.files_affected` across
   the slice's tasks; pass `context_pr_number` from
   `program_pr.context_pr_number` (the base/context PR opened by
   #2548, or `None` if that PR was not opened).
5. For the terminal slice: pass `program_deferred_actions` (collected
   via `_collect_pre_merge_obligations`); set `terminal_slice_id=None`.
6. For non-terminal slices: `program_deferred_actions=None`; set
   `terminal_slice_id=chosen_terminal` **only if**
   `contract.pr.title` is non-empty (suppressed for older contracts
   without a program block).

### Multi-terminal-forest pointer caveat

The slice DAG is a forest (≤1 DAG parent per slice — see
[Forest Validation](#plan-parser--forest-validation)) and a
multi-tree forest can have multiple terminal slices, one per tree.
The current behaviour picks `terminal_ids[-1]` (last declared) as
`chosen_terminal` — that's the slice that gets the `merge-gate`
position marker and the umbrella banner; the per-merge obligations
section also lives on exactly that PR. Other terminal leaves in
non-chosen trees are treated as non-terminals: they receive a
`slice-N/M` position marker and skip the umbrella banner and
obligations section. The choice is deliberate (arbitrary but stable,
deterministic across parallel slice runs); operators reviewing a
multi-tree pipeline should not be surprised that the merge-gate PR
sits in `chosen_terminal`'s subtree.

## Stacked-PR rebase reconciler

`orchestrator/stacked_pr_reconciler.py` catches the edge case where a
parent PR is merged via a path that doesn't trigger GitHub's
auto-retarget (force-push, manual branch deletion). It runs on a fixed
cadence (default 30s, env
`EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS`):

```python
def find_orphaned_child_prs(
    contract: Contract,
    open_prs: list[dict[str, Any]],
    extant_branches: set[str],
) -> list[OrphanedChildPR]: ...

def reconcile_once(
    contract: Contract,
    *,
    list_open_prs: Callable[[], list[dict[str, Any]]],
    list_extant_branches: Callable[[], set[str]],
    rebase_onto: Callable[[str, str, str], bool],
) -> ReconciliationResult: ...
```

A child slice PR is orphaned when:

1. The slice's `parent_branch_at_creation` is set (it is not a root that
   targets the pipeline branch directly).
2. There is an open PR whose head branch matches the slice's integration
   branch (`egg/issue-N/slice-M`).
3. The PR's base branch is **not** in the set of extant origin branches.

The intended new base is sourced from `Slice.parent_branch_at_creation`,
not inferred from PR metadata — so the reconciler is robust against
parent slice branches that have been renamed or rebased. Roots, completed
slices, and slices whose base still exists are silently skipped, making
each pass idempotent.

The three callables decouple `reconcile_once` from the gateway client and
GitHub API so unit tests can substitute deterministic fakes. In
production the run loop binds them to live gateway helpers — the
reconciler is fully functional, not a no-op:

- **`list_open_prs`** → `GatewayClient.list_open_prs(...)`, which runs
  `gh pr list --json number,headRefName,baseRefName,state` through the
  existing per-agent `gh` allowlist. JSON parsing failures degrade to
  an empty list (logged warning) so a transient `gh` flake does not
  cause the reconciler to misclassify orphans.
- **`list_remote_branches`** → `GatewayClient.list_remote_branches(...)`,
  which runs `git ls-remote --heads origin` through the existing
  per-agent `ls-remote` allowlist (`operation="ls-remote"`). The
  reconciler treats the returned set as the join key for the orphan
  check; an empty set on failure preserves the conservative default
  (no PR is treated as orphaned).
- **`rebase_onto`** → `GatewayClient.rebase_onto(pipeline_id, repo_path,
  branch=..., new_base=..., old_base=...) → bool`. This bridges the
  reconciler's `Callable[[str, str, str], bool]` shape to
  `orchestrator.gateway_client._build_rebase_onto_args` — an inlined
  adaptation of `gateway.git_client.build_rebase_onto_args` so the
  orchestrator image (which does not ship `gateway/`) has no import-time
  dependency on the gateway package (#2535). The orchestrator-side
  helper intentionally diverges from the gateway version in two ways
  (documented in its docstring): each ref is `.strip()`-ed in the
  *emitted* argv (leading/trailing whitespace is normalised rather than
  rejected; interior whitespace is still rejected client-side by the
  explicit `isspace`/NUL check that fires before the regex shape
  check), and it does NOT call `gateway.git_client.validate_git_args` (importing
  it would defeat the point of inlining). The argv builder constructs
  the canonical `["--onto", new_base, old_base, branch]` shape and
  validates ref shapes client-side (rejects flag-shaped, whitespace-
  bearing, or non-git-ref inputs); the gateway server re-validates via
  `validate_git_args("rebase", ...)` before execution — extra flags
  (e.g. `--strategy-option=ours`) are rejected (the server is the
  authoritative allowlist boundary; the client-side check is a fast
  fail, not the security floor). After validation the
  bridge submits the args through the existing per-agent `/api/v1/git`
  endpoint via the same temp-session pattern that `create_pr` and
  `fetch_worktree_branch` use; failures (validation reject, HTTP error,
  gateway unavailable) return `False` and the reconciler counts them
  as `rebases_failed`.

**No new privileged orchestrator-role endpoint is introduced** for any
of the three callables (refine-phase decision-15) — every gateway call
flows through the same per-agent allowlists the slice's regular agent
team uses.

## Architect, planner & plan-reviewer prompt updates

The dynamic prompt builders for `task_planner` and `reviewer_plan` were
extended to teach the agents the new schema and constraints:

- **Architect (`architect`)** — sole authority for slice composition
  (#2809, inverting HITL decision-6 opt-2). The architect prompt
  declares this authority explicitly and the architect emits a binding
  `architect-slices.yaml` scaffold alongside its analysis JSON
  (`{identifier}-architect-slices.yaml` under `.egg-state/agent-outputs/`).
  The scaffold encodes slice `id` / `name` / `goal` / `parent_slice_id`;
  `tasks:` is intentionally omitted (that is `task_planner`'s job).
- **Planner (`task_planner`)** — sections appended to the plan phase
  prompt:
  1. *Slice composition is NOT the planner's call* (#2809): the
     architect's `architect-slices.yaml` scaffold is binding. The
     planner copies it verbatim into the `# yaml-tasks` appendix
     (same `id` / `name` / `goal` / `parent_slice_id`, same order) and
     fills in `tasks:` under each slice. The planner does not
     silently re-shape slices — if a slice needs to be subdivided,
     the planner raises NACK pressure on the architect (via the
     plan prose, which `risk_analyst` / `reviewer_plan` pick up)
     rather than re-shaping locally.
  2. *Forest constraint* (HARD): every slice must have ≤1 DAG parent;
     the populator hard-rejects multi-parent slices with
     `ForestValidationError`. The architect's scaffold encodes this
     via `parent_slice_id`; the planner preserves it.
  3. *Auto-serialization* for would-be multi-parent slices: the
     architect is responsible for serialising the upstream cluster
     and populating `serialized_chain_order` on the downstream
     slice. The fallback heuristic (`files_affected` Jaccard >0.3,
     then descending fan-out) is documented; the architect's own
     ordering is the source of truth (HITL decision-17). The
     planner preserves the field verbatim from the scaffold.
  4. The yaml-block key swap: `slices:` is canonical, `phases:` is
     backward-compat.
- **Plan reviewer (`reviewer_plan`)** — two new prompt sections:
  1. *Forest-violation NACK*: when the populator left a "Plan ingestion
     REJECTED" block on `plan_review_feedback` (or a `forest_violation`
     log discriminator is present), the reviewer NACKs the **architect**
     (not the planner — slice scaffold ownership moved to architect in
     #2809) with the structured errors verbatim and instructs re-emission
     of the scaffold with `serialized_chain_order` populated.
  2. *Slice-sizing NACK* (hard, judgment-based; #2809 inverts HITL
     decision-6 opt-2). The reviewer is empowered AND required to
     hard-NACK the architect on `slice_size` when a slice is oversized
     for one BRC cycle. There is no fixed LOC budget — the rubric is
     judgment-based: NACK when a slice bundles >~3 distinct
     file-categories, combines deletion-heavy work with new-API
     introduction, would require >3–4 commit-propose-revise cycles to
     converge, or contains independent task groups with no internal
     dependency. The reviewer names the seam where subdivision is
     appropriate so the architect's re-propose is actionable. See
     `_get_plan_review_criteria()` §11 for the full rubric and worked
     NACK examples. Refiner/operator override remains available when a
     large slice is deliberate (e.g. atomic schema migration) — the
     architect cites the override in the analysis and the reviewer
     ACKs once the rationale is on the record.

## Configuration knobs

All slice/scheduler knobs live in `orchestrator/env_config.py` and
return typed values (positive int / positive float) with logged fallbacks
on parse failure.

| Env var | Type | Default | Controls |
|---------|------|---------|----------|
| `EGG_ORCH_MAX_PARALLEL_SLICES` | int | 2 | **Per-pipeline** wave slice spawn concurrency cap. Enforced via `iter_ready` and mirrored on the wave's `ThreadPoolExecutor.max_workers`. |
| `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` | int | 4 | **Process-wide** slice cap across ALL running pipelines (#2241 gap 1). Enforced by `orchestrator.global_slice_admit.try_admit()` in the run loop; deferred slices stay READY and re-yield next tick. |
| `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` | int | 3 | Per-slice BRC re-proposal ceiling before HITL escalation. *Currently inert — #2199 wires the trip flag through the BRC re-proposal loop.* |
| `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` | int | 10 | Pipeline-wide summed slice-cycle cap. *Currently inert — see local cycles row.* |
| `EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS` | float | 60.0 | Grace window before a failure cascade marks the downstream subtree `BLOCKED_ON_FAILED_DEPENDENCY`. |
| `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` | float | 30.0 | Reconciler polling cadence for orphaned child PRs. |

### Per-pipeline vs. global slice caps

`EGG_ORCH_MAX_PARALLEL_SLICES` (per-pipeline) and
`EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` (process-wide) compose: the
**lower** effective bound wins for any given wave. The per-pipeline
cap is enforced inside `SliceScheduler.iter_ready` and bounds the
size of the wave's `ThreadPoolExecutor`. The global cap is enforced
by `orchestrator.global_slice_admit.try_admit()`, called from the
run loop **before** `mark_spawned` so the per-pipeline accounting
stays honest. Slices that the global cap rejects stay in `READY` and
re-yield on the next 5 s tick. Each slice spawns ~8 containers, so
the global default of 4 matches the operationally observed safe
ceiling on a single host.

The global cap is an **in-process** counter. Operators running
multiple orchestrator replicas (HA pair) get one cap per replica —
the semaphore does not coordinate across processes. Today the
platform deploys one orchestrator per environment so this is fine;
flagging it here for any future HA migration.

The current global admit-state is exposed on
`GET /api/v1/pipelines/<pipeline_id>/status` as
`{cap, admitted, admitted_keys}` and surfaced via the
`mcp__egg__get_pipeline_snapshot` tool's `slice_admit` field, so
operators can see when slices are queued behind the cap rather than
wedged.

## Resolved design decisions (from refine phase)

The slicing design was driven by 18 HITL decisions plus a feedback round
during refine. The most consequential are referenced inline above:

- **decision-5** — concurrency: unbounded per wave; `max_parallel_slices`
  is an operator-tunable soft cap. Initial cap was 5; lowered to 2 in
  #2466 to constrain container/gateway resource pressure during the
  implement phase.
- **decision-7** — schema rename ships with a one-version load-time
  migration; legacy `phases[]` JSON keeps loading.
- **decision-9** — two-tier `max_cycles` (local 3, global 10).
- **decision-10** — failure-cascade hybrid (60 s grace + downstream-only
  block).
- **decision-13** — lens reviewers run per-slice (cross-slice coherence
  trade-off accepted).
- **decision-14** — BRC tracker keying: hybrid (`pipeline_id` for
  cross-slice telemetry, nested `pipeline_id/slice_id` for `CONSENSUS_*`).
- **decision-15** — no privileged orchestrator merge endpoint; reconciler
  authenticates as the existing low-privilege agent identity.
- **decision-16** — stacked-PR rebase: GitHub auto-retarget primary path,
  reconciler safety net.
- **decision-17** — auto-serialization for would-be multi-parent slices:
  architect-supplied `serialized_chain_order` is the source of truth (#2809).
- **decision-18** — forest constraint enforced at plan ingestion only;
  multi-parent slices NACK the architect (#2809).
- **decision-20** — implement-phase run-loop wire-up (TASK-4-2,
  TASK-4-4, TASK-5-1 invocation, TASK-5-3 scheduling). Operator chose
  **opt-2** ("require the wire-up to land here before consensus"); the
  run loop, slice-aware `ConcurrentPhaseExecutor`, integration-branch
  creation, per-slice PR opening, and the reconciler thread all shipped
  in this PR (commits `36d34da9612`, `7f4203469`, `97de1061d` plus
  v1–v3 follow-ups).

## Out of scope (#2137)

- **Per-slice MCP control verbs** (`restart_slice`, `get_slice_status`,
  `list_slices`) — tracked in #2199. The slice-addressable hooks the
  verbs will wrap are public on `SliceScheduler` already
  (`teardown_slice`, `respawn_slice`, `get_slice_status`, plus the
  implicit `list_slices` view via the scheduler's contract reference).
  Note: `restart_agent` with `slice_id` landed in #2399/#2410 — the
  REST endpoint (`POST /api/v1/pipelines/<id>/agents/<role>/restart`)
  now accepts `?slice_id=slice-N` (or `"slice_id"` in the body).
- **`record_cycle` two-tier wiring (#2199)** — `SliceScheduler`'s
  `record_cycle` API and `hitl_escalator` hook are public and unit-
  tested but the slice run loop does not call `record_cycle` on each
  BRC re-proposal yet; the `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` /
  `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` env knobs are read but not
  exercised today.
- **`EGG_PIPELINE_ID` cross-slice telemetry hybrid (#2199)** — the
  agent CLI's `EGG_PIPELINE_ID` override scopes *every* outbound
  message (CONSENSUS_*, HEARTBEAT, OVERSEER_ALERT) to the slice
  tracker. Decision-14's hybrid scheme (cross-slice telemetry on the
  bare pipeline tracker) requires a CLI-side message-type-aware router
  to fully honour. Today the orchestrator-side cascade emission and
  log lines provide the always-on `pipeline_id`-scoped fallback for
  cascade visibility.
- **Cross-slice architectural review** — `reviewer_code_holistic` runs
  per-slice; cross-slice cohesion is not re-checked once slices land.

## Related documentation

- [SDLC Pipeline Architecture](sdlc-pipeline.md) — contract schema and
  threat model.
- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — operational guide
  for the pipeline.
- [Concurrent Execution](../guides/concurrent-execution.md) — BRC
  consensus and message bus.
- [Plan Template](../templates/plan.md) — `# yaml-tasks` block that
  emits `slices:` (or legacy `phases:`).
