# Slice-DAG Implement Phase

> Status: building blocks shipped (#2137). The orchestrator's implement-phase
> run-loop wire-up is deliberately deferred — every other component listed
> here is in place and unit-testable today.

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
| `serialized_chain_order` | `list[str]` | `[]` | Planner-emitted ordering for would-be multi-parent slices. When the planner identifies a slice that would naturally have >1 parents, it serialises the upstream cluster into a chain and records the chosen order on the downstream slice. |
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
`validate_forest()` and stashes any returned errors on
`Contract.plan_review_feedback` so the plan reviewer NACKs the planner.
Slices are not written to the contract in this case — leaving
`contract.slices` empty so downstream code visibly fails fast.

A typical error message:

```
Slice 'slice-3' has 2 DAG parents (['slice-1', 'slice-2']);
the implement-phase slice DAG must be a forest (≤1 parent per slice).
Serialise the upstream cluster into a chain and record the chosen order
on this slice's 'serialized_chain_order' field — see issue #2137 plan
TASK-2-3 for the auto-serialization rule.
```

## DependencyGraph generification

`shared/egg_contracts/dependency_graph.py` was generified in #2137.
`DependencyNode`, `ExecutionWave`, `ExecutionPlan`, and `DependencyGraph`
are now `Generic[NodeT]`. Existing agent-role-keyed callers continue to use
`DependencyGraph[AgentRole]`; the slice scheduler uses `DependencyGraph[str]`
with slice IDs as node keys. One implementation, two parameterisations.

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
run loop all run in different threads).

### Two-tier `max_cycles` accounting

Each slice has a **local** cap on BRC re-proposal cycles before HITL
escalation, and the pipeline has a **global** cap on summed cycles across
all slices. Either trip calls `hitl_escalator(slice_id, reason)`.

| Default | Env var |
|---------|---------|
| local 3 | `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` |
| global 10 | `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` |

### Failure cascade

When `record_failure(slice_id)` is called, the scheduler arms a timer for
`failure_grace_seconds` (default 60s, env
`EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS`). On expiry, the next
`poll_cascades()` walks the downstream subtree (BFS via dependents) and
marks every transitive descendant `BLOCKED_ON_FAILED_DEPENDENCY`. **Sibling
slices are unaffected** — only the failed slice's downstream subtree is
blocked, and one `OVERSEER_ALERT` (anomaly type `slice-cascade-block`) is
emitted with the full subtree.

## Per-slice branches & BRC trackers

`ConcurrentPhaseExecutor.get_worktree_branch(role, *, slice_id=None)` is
slice-aware:

| `slice_id` | Result |
|------------|--------|
| `None` (default) | Pre-#2137 behaviour — `pipeline.branch` or `egg/issue-N`. |
| `"slice-2"` or `"2"` | `egg/issue-N/slice-2/{role}/work` |

Babysit-pr mode is **not** slice-aware in this PR (refine-phase decision-8
deferred babysit slicing to a follow-up).

A new helper `get_slice_integration_branch(slice_id)` returns the shared
integration branch for a slice's BRC: `egg/issue-N/slice-M`. Per-role
work branches rebase onto it. Roots base their integration branch off the
pipeline branch directly; child slices base off their parent slice's
integration branch.

The BRC tracker layer (`orchestrator/peer_consensus.py`) was extended so
`create/get/remove_peer_consensus_tracker(pipeline_id, slice_id=None)` keys
the registry under the composite key `{pipeline_id}/{slice_id}`. Per-slice
consensus state is naturally isolated — `CONSENSUS_*` messages flow
through one tracker per slice. Cross-slice telemetry (`HEARTBEAT`,
`OVERSEER_ALERT`) keeps the original `pipeline_id` so existing observers
see the whole pipeline (refine-phase decision-14: hybrid scheme).

## Stacked-PR creation

`GatewayClient.create_slice_pr(pipeline_id, repo, *, slice_id, slice_name,
slice_tasks, head, base, ...)` opens one PR per slice with:

- **Title**: `"slice {slice_id}: {slice_name}"` truncated to 70 chars.
- **Body**: the slice name, a bulleted list of tasks (each truncated to
  300 chars), and a footer naming the slice ID, pipeline, and base.

The human-authored `pr.title` / `pr.description` / `pr.test_plan` block
from the plan's `# yaml-tasks` remains the source of truth for the
**terminal slice** (the chain's tip). Sibling roots and intermediate
slices ship with the auto-generated copy.

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
GitHub API so unit tests can substitute deterministic fakes. The
`rebase_onto(branch, new_base, deleted_base) → bool` callable wraps the
existing per-agent rebase allowlist — **no new privileged
orchestrator-role endpoint is introduced** (refine-phase decision-15).

## Configuration knobs

All five slice/scheduler knobs live in `orchestrator/env_config.py` and
return typed values (positive int / positive float) with logged fallbacks
on parse failure.

| Env var | Type | Default | Controls |
|---------|------|---------|----------|
| `EGG_ORCH_MAX_PARALLEL_SLICES` | int | 5 | Per-wave slice spawn concurrency cap. |
| `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` | int | 3 | Per-slice BRC re-proposal ceiling before HITL escalation. |
| `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` | int | 10 | Pipeline-wide summed slice cycles cap. |
| `EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS` | float | 60.0 | Grace window before a failure cascade marks the downstream subtree `BLOCKED_ON_FAILED_DEPENDENCY`. |
| `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` | float | 30.0 | Reconciler polling cadence for orphaned child PRs. |

## Resolved design decisions (from refine phase)

The slicing design was driven by 18 HITL decisions plus a feedback round
during refine. The most consequential are referenced inline above:

- **decision-5** — concurrency: unbounded per wave; `max_parallel_slices=5`
  is an operator-tunable soft cap.
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
  planner-supplied `serialized_chain_order` is the source of truth.
- **decision-18** — forest constraint enforced at plan ingestion only;
  multi-parent slices NACK the planner.

## Out of scope (#2137)

- **Per-slice MCP control verbs** (`restart_slice`, `restart_agent` with
  `slice_id`, `get_slice_status`, `list_slices`) — tracked in #2199. The
  slice-addressable hooks they need (`teardown_slice`, `respawn_slice`,
  `get_slice_status`) are public on `SliceScheduler` already.
- **Babysit-PR slicing** — refine-phase decision-8 keeps `babysit_pr`
  monolithic for now; follow-up tracked separately.
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
