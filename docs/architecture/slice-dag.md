# Slice-DAG Implement Phase

The implement phase runs as a **forest of slices**. Each slice is an
independently-implementable unit of work with its own integration branch,
agent team, BRC consensus, and pull request; slice PRs stack along the DAG's
linear chains. The slice run loop drives `SliceScheduler` waves, creates each
slice's integration branch on origin via the gateway *before* agents spawn,
runs BRC consensus per slice, opens a per-slice PR on consensus reach, and
runs the stacked-PR reconciler in a background thread (bound to live gateway
helpers — `list_open_prs` / `list_remote_branches` / `rebase_onto`).

The forest model exists because a single monolithic agent team on one branch
through one BRC consensus cannot hold a large ticket: tickets big enough to
fill the context window (empirically ~33K LOC / 41 files,
[#2105](https://github.com/jwbron/egg/issues/2105)) caused compaction and
quality drops. Slicing bounds each agent team's context to one unit of work.

> **Known limitations** (tracked in
> [#2199](https://github.com/jwbron/egg/issues/2199), the per-slice MCP
> control-verbs follow-up):
>
> 1. The `EGG_PIPELINE_ID` override that scopes BRC `CONSENSUS_*` messages
>    also scopes agent-emitted `HEARTBEAT` and `OVERSEER_ALERT` traffic to
>    the slice tracker, so cross-slice telemetry does not route through the
>    bare `pipeline_id`. The orchestrator-side cascade emission and log line
>    in `_run_implement_phase_slices` are the always-on fallback that keeps
>    deadlocks visible at the pipeline level; full fan-out requires a
>    CLI-side message-type-aware router.
> 2. `SliceScheduler.record_cycle` (two-tier `max_cycles` accounting) and the
>    `hitl_escalator` hook are public API and unit-tested, but the slice run
>    loop does not call `record_cycle` on each BRC re-proposal. The env knobs
>    `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` / `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES`
>    are read but not exercised.

## Vocabulary

| Term | Meaning |
|------|---------|
| **Slice** | One independently-implementable unit of work. Each slice has tasks, dependencies, an integration branch, an agent team, and (eventually) a PR. |
| **Forest** | The slice DAG must be a forest: every slice has **at most one** DAG parent. Multi-parent slices are rejected at plan ingestion. |
| **Wave** | A set of slices whose dependencies are all satisfied. Slices in the same wave can run concurrently. |
| **Stacked PR** | A child slice's PR targets the parent slice's integration branch (not main). Reviewers land slices incrementally as parents merge. |
| **Cascade** | When a slice fails, after a grace window its transitive descendants are marked `BLOCKED_ON_FAILED_DEPENDENCY` rather than running pointlessly. |

## Contract Schema (slices, with `phases` back-compat)

The canonical contract field is `slices[]`. Backwards compatibility with the
legacy `phases[]` name is preserved at every layer so older on-disk contracts
keep loading:

- **`Phase = Slice`** — class alias. Legacy imports keep working.
- **`PhaseStatus = SliceStatus`** — enum alias. Status values
  (`pending` / `in_progress` / `complete` / `blocked`) are identical so
  on-disk JSON loads without translation.
- **`Contract.phases`** — read/write property proxy to `Contract.slices`.
  Reading and assigning both work.
- **Load-time migration shim** — when a legacy contract JSON containing
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
| `repo` | `str \| None` | `None` | The single repository this slice operates in, `owner/name`-shaped (#3393). **Slice ↔ repo is 1:1** — exactly one repo per slice. `None` ⇒ resolved to the pipeline's primary repo at runtime by `resolve_slice_repo(slice, pipeline)` in `orchestrator/models.py` (the contract model cannot see the pipeline, so the default is *not* filled in by the model). See [Per-slice repo](#per-slice-repo-multi-repo-pipelines). |
| `serialized_chain_order` | `list[str]` | `[]` | Architect-emitted ordering for would-be multi-parent slices (#2809). When the architect identifies a slice that would naturally have >1 parents, it serialises the upstream cluster into a chain and records the chosen order on the downstream slice. |
| `parent_branch_at_creation` | `str \| None` | `None` | Git branch the slice's integration branch was forked off when its worktree was provisioned. Eager-persisted under the per-pipeline state lock in the same contract write that flips `SliceStatus.PENDING → IN_PROGRESS` ([#2777](https://github.com/jwbron/egg/issues/2777)), so Layer-C bootstrap reconciliation has a single signal that distinguishes a fresh slice from an interrupted one and the value is durable across orchestrator restarts. Read by the stacked-PR reconciler when the parent's branch has been deleted by a merge so it can compute the correct rebase target. Empty on legacy/orphaned slices that pre-date the eager-persist contract — in that case `_resolve_slice_base_branch` falls back to a merge-base probe against the dependency-derived parent before routing onto `pipeline_branch`. |
| `integration_base_sha` | `str \| None` | `None` | Origin SHA the slice's integration branch was forked at when first created (#2871). Written right after branch creation and before any agent is spawned (so the tip still equals this SHA at that point), but can be overwritten by out-of-band actors such as `restart_phase`, `salvage_agent_commits`, or manual contract edits. Lets `is_slice_branch_merged_into_parent` distinguish an *empty, un-started* branch (tip still equals this SHA → trivially an ancestor of any advanced parent, but not merged work) from a *genuinely merged* one (tip has moved past this SHA). Also used by `create_slice_integration_branch` to verify that an existing integration branch is a resumable additive fork (#2947); when this field is absent or corrupted (e.g. overwritten to the advanced parent tip by a restart actor), that method re-derives the fork point via a runtime `git merge-base` (executed on the gateway) and adopts the branch in place rather than non-fast-forward-failing the slice (#3245). Slices provisioned before this field existed fall back to the prior ancestor-only check. |

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

`validate_forest()` is checked at **two points** in the pipeline:

1. **Propose-time** (`_validate_plan_extensions` in `routes/signals.py`, #3211):
   called while the producer is still alive in BRC, so a non-forest plan is
   NACKed immediately and the architect can re-emit a fixed plan before
   consensus is reached. Violations raise `ValueError` and the BRC propose
   handler returns a structured NACK.
2. **Populate-time** (`_populate_contract_from_plan`): the same validator runs
   again after consensus as a belt-and-suspenders guard. Here a violation
   stashes errors on `Contract.plan_review_feedback` (so the plan reviewer
   NACKs the architect, #2809) **and raises a structured `ForestValidationError`**.
   Slices are not written to the contract in this case — leaving
   `contract.slices` empty so downstream code visibly fails fast.

Running the same validators at both points means the propose-time check and
the populate-time check cannot diverge.

```python
class ForestValidationError(Exception):
    status_code: int = 422
    errors: list[str]
    reason: str  # "forest_violation" or "slice_overlap_violation" (#3046)
    def to_response(self) -> tuple[dict[str, object], int]: ...
```

`ForestValidationError.to_response()` returns
`({"error": self.reason, "errors": [...]}, 422)` — the `error` key reflects
the `reason` discriminator (`"forest_violation"` for multi-parent DAGs,
`"slice_overlap_violation"` for overlapping-but-unordered slices added in
#3046). Any future route that ingests a plan in-band can catch it and return
`body, status = err.to_response(); return jsonify(body), status`. The internal `_populate_contract_from_plan_safe`
wrapper catches `ForestValidationError` with a dedicated structured warning
(separate audit-log discriminator from the catch-all `except Exception`) and
returns a `PopulateResult` mapped via `_forest_error_to_outcome` — the
exception does not propagate past the safe wrapper.

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
list, so both the propose-time and populate-time passes surface every
structural defect at once.

### File-overlap ordering validation (#3046)

`validate_forest` checks the DAG's *shape* (≤1 parent, acyclic). A
separate validator, `validate_slice_file_overlap(slices)`, checks a
*file-semantic* property: **two slices that touch overlapping files must
be ordered along the dependency DAG** — one a transitive `dependencies`
ancestor of the other.

The implement phase cuts each slice's integration branch off its
dependency parent (root slices off `egg/<id>/work`) and ships it as a
stacked PR — see [Per-slice branches](#per-slice-branches--brc-trackers).
So when slice-A and slice-B touch the same file:

- **Ordered** (one transitively depends on the other) → the later
  slice's branch is forked from a base that already contains the
  earlier slice's commits, so the edits stack cleanly. An intermediate
  disjoint slice on the chain is fine — the fork point is still
  transitive.
- **Unordered** (both roots, or siblings in different subtrees) → both
  branches fork independently off the shared base, and their edits to
  the shared file collide at integration. This is a *guaranteed*
  conflict, including unavoidable modify/delete when one slice deletes a
  file the other modifies.

This is the #3023 failure: three slices all declared `dependencies: []`
(parallel roots) while all three touched `consensus_wrapper.py` — one
*deleting* it. The git branch topology faithfully mirrored the DAG (all
three forked off `work`); the **DAG itself** was the defect. The branches
matched the slice DAG perfectly — the slice DAG just didn't encode the
file-level dependency, and nothing rejected it.

`files_affected` is read from each slice's tasks (the same
planner-declared signal `validate_task_role_alignment` uses); slices with
no declared files contribute no overlap signal. The reachability walk is
cycle-safe (a cycle is reported by `validate_forest`, not here).

Like `validate_forest`, this validator runs at **two points**:

1. **Propose-time** (`_validate_plan_extensions`, #3211): checked right after
   `validate_forest` passes, while the producer is still alive in BRC.
   Overlap violations raise `ValueError` and are returned as a NACK so the
   architect can fix the dependency ordering before consensus.
2. **Populate-time** (`_populate_contract_from_plan`): runs again after
   consensus with **identical handling** — slices are not written to the
   contract, structured errors are stashed on `Contract.plan_review_feedback`,
   and a `ForestValidationError` is raised with `reason="slice_overlap_violation"`
   (the safe-wrapper maps it to `PopulateOutcome.SLICE_OVERLAP_VIOLATION`,
   distinct from `FOREST_VIOLATION`, so the operator-facing HITL prose names
   the actual defect). The plan reviewer NACKs the **architect**
   (slice-composition authority, #2809) — see plan-review criteria §12.

Because the forest constraint forbids a diamond (a slice cannot depend on
two parents), the remediation is always to **serialise the overlapping
cluster into one linear `dependencies` chain** — or merge the slices.
Disjoint slices stay parallel, preserving the concurrency that slicing
exists to provide.

## DependencyGraph generification

`DependencyNode`, `ExecutionWave`, `ExecutionPlan`, and `DependencyGraph`
in `shared/egg_contracts/dependency_graph.py` are generic over the node-key
type. The classes use **PEP 695 generic
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

> **Not yet wired** (tracked in
> [#2199](https://github.com/jwbron/egg/issues/2199)). The `record_cycle`
> invocation point is not called from the slice run loop:
> `_run_implement_phase_slices` tracks per-slice exit codes but does not
> call `record_cycle` on each BRC re-proposal, so the env knobs are read at
> constructor time but the trip path is unreached. The hook itself is
> public, unit-tested, and lock-safe; closing the loop on the BRC
> re-proposal counter and wiring the `hitl_escalator` argument through to
> the orchestrator's HITL escalation surface is the open follow-up.

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
the in-process `message_store` keyed on the bare `pipeline_id`:

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

This is the always-on safety net: under the `EGG_PIPELINE_ID`
override, agent-emitted `OVERSEER_ALERT` traffic routes through the
slice tracker rather than the pipeline tracker (see Known limitations).
The orchestrator-side emission keeps
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
integration branch.** The branch is shared per-slice rather than
per-role because a per-role suffix shape (`egg/issue-N/slice-M/{role}/work`)
would make the per-slice PR's diff render empty: the integration branch
opened on origin points at the parent's tip while every agent commit would
live on a per-role sibling branch GitHub does not see in the PR.

| Mode | `slice_id` | Result |
|------|------------|--------|
| Pipeline mode (non-slice phases) | `None` (default) | `pipeline.branch` or `egg/issue-N/work` (tip pushed to `<id>/work` since [#2399](https://github.com/jwbron/egg/issues/2399)). |
| Slice mode | `"slice-2"` or `"2"` | `egg/issue-N/slice-2` — **shared by every role in the slice**. |

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
integration branch for a slice's BRC: `egg/issue-N/slice-M`. Child
slices base off their parent slice's integration branch. Root slices
base off the **latest completed chain tip** when one exists
([#3541](https://github.com/jwbron/egg/issues/3541) — see "Root
linearization & the base-ancestry gate" below), falling back to the
pipeline branch directly (`egg/issue-N/work` since #2399) for the
first root or when every completed tip's branch was merged and
cascade-deleted. Slice integration branches live as siblings
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
per-agent `/api/v1/git/push` allowlist (no new privileged endpoint is
introduced). Pushing by SHA rather than ref name avoids local-ref
resolution failures in the orchestrator's per-pipeline worktree, which is
checked out on `<branch>/work` and carries no local ref matching
`<parent_branch>` ([#2393](https://github.com/jwbron/egg/issues/2393)). On creation failure the run
loop calls `record_failure(slice_id)` and returns early — agents are
not spawned against a missing integration branch. When the branch already
exists (e.g. after an orchestrator-pod restart or a `restart_phase` that
preserves the shared integration branch on origin),
`create_slice_integration_branch` verifies it is a resumable additive
fork using the recorded `integration_base_sha` (#2947); if that recorded
base is absent or corrupted, it re-derives the fork point via a runtime
`git merge-base` (executed on the gateway) and adopts the branch in place
rather than failing with a non-fast-forward rejection (#3245).

The BRC tracker layer (`orchestrator/peer_consensus.py`) was extended so
`create/get/remove_peer_consensus_tracker(pipeline_id, slice_id=None)` keys
the registry under the composite key `{pipeline_id}/{slice_id}`. Per-slice
`CONSENSUS_*` state is naturally isolated. `HEARTBEAT` / `OVERSEER_ALERT`
are intended to flow through the bare `pipeline_id`, but the
`EGG_PIPELINE_ID` override route on the agent CLI currently sends *every*
outbound signal through the slice tracker (see Known limitations). The
orchestrator-side cascade emission and run-loop log lines are the always-on
`pipeline_id`-scoped fallback so deadlocks remain visible at the pipeline
level regardless.

### Root linearization & the base-ancestry gate ([#3541](https://github.com/jwbron/egg/issues/3541))

During a run the pipeline work branch only ever advances with
bookkeeping commits (contract persists, statefiles) — slice PRs are
human-merged after the pipeline finishes. In pipeline `issue-3523` that
meant a root slice admitted after a sibling root's chain had completed
forked from `work` and silently excluded the completed chain's
reviewed, consensus-approved code; every downstream slice inherited the
gap, while the contract kept marking the orphaned commits complete. Two
mechanisms close this:

- **Root linearization** (`_resolve_slice_base_branch` tier 3, via
  `_latest_completed_chain_tip`): a root slice with no recorded parent
  forks from the integration branch of the deepest COMPLETE chain tip
  instead of the work branch. Under serialized execution
  (`max_parallel_slices == 1`) with a single linear chain this makes
  the git topology a single
  linear chain in completion order — every slice's base transitively
  contains all previously completed work, and the final chain tip is a
  complete deliverable. Tips whose branch was merged and
  cascade-deleted are skipped (their content already reached `work`);
  with no live completed tip the root falls back to the pipeline
  branch as before. With genuine slice concurrency (cap > 1),
  in-flight chains are never chained onto — only COMPLETE tips — so
  parallel waves keep their isolation. The resolver reads a **fresh
  contract snapshot** at admission (not the phase-start object) so it
  sees completion statuses flipped while the run loop iterates.
- **Base-ancestry gate** (`_check_slice_base_ancestry`): the
  admission-time counterpart of the slice-close evidence-reachability
  gate (#3125). Right after `create_slice_integration_branch` (the
  branch tip still equals the fork base) and before agents spawn, it
  verifies every commit SHA the contract records as evidence on
  completed predecessor slices is an ancestor of the new branch. The
  predecessor set is the slice's own fork chain (walked via
  `parent_branch_at_creation` / `dependencies`) — the set the base is
  supposed to contain — in **every** topology and concurrency setting.
  This is scope-correct even under serialized execution
  (`max_parallel_slices == 1`): a branching (tree) DAG is legal at any
  cap (`validate_forest` caps parents at ≤1 but not children), so a
  completed *sibling* chain is legitimately not an ancestor and must
  not be gated against — gating every COMPLETE slice would
  false-positive on tree fan-out. The walk still catches the original
  orphaning because a linearized root records the completed chain tip
  it was re-based onto as its `parent_branch_at_creation`. A definitive
  miss fails the admission
  (`record_failure` → cascade + HITL); contract-read or gateway-probe
  failures degrade to a logged skip, mirroring #3125. Like every other
  reachability check in the slice DAG (`_sha_is_ancestor`,
  `is_slice_branch_merged_into_parent`), the gate assumes merges into
  `work` preserve SHAs: the automatic cascade only *deletes* branches
  after a human PR merge — it never merges/squashes into `work` itself
  — so the sole SHA-rewriting path is an operator squash/rebase-merge,
  for which `EGG_SLICE_BASE_ANCESTRY_GATE=off` is the operator kill
  switch (a completed slice's recorded SHA is no longer an ancestor of
  the rewritten `work`).

The planner prompt gained the matching plan-shape rule: a slice that
*reads* another slice's output (documents, verifies, builds on it)
must be its transitive DAG descendant even when their file sets are
disjoint — file overlap is not the only ordering constraint.

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
       persists `Slice.parent_branch_at_creation` AND flips
       `SliceStatus.PENDING → IN_PROGRESS` in the same contract write
       under the per-pipeline state lock ([#2777](https://github.com/jwbron/egg/issues/2777)
       — gives Layer-C bootstrap a single signal that distinguishes a
       fresh slice from an interrupted one), creates the integration
       branch via the gateway, calls
       `_run_concurrent_phase(slice_id=...)` to spawn the slice's
       agent team, awaits BRC consensus, and on consensus reach — after
       the evidence-reachability gate (#3125; cited SHAs that a
       reconciled-push rebase rewrote are re-identified by
       `git patch-id` against the integration branch and treated as
       satisfied, [#3572](https://github.com/jwbron/egg/issues/3572),
       `evidence_rescue.py`, kill switch
       `EGG_EVIDENCE_PATCH_ID_RESCUE`; a slice that still fails the
       gate lands an unresolved HITL `Decision` via
       `_escalate_evidence_gate_to_hitl` instead of parking silently
       after `record_failure`) and the per-slice green
       gate (`slice_green_gate.run_slice_green_gate`, #3398, which
       spawns a sandboxed one-shot check-runner Job to execute the
       repo's configured checks at the integration-branch tip and
       blocks PR-open on a red verdict; staged rollout via
       `EGG_SLICE_GREEN_GATE`, fail-open on infra errors) — calls
       `GatewayClient.create_slice_pr` with `base` resolved from the
       slice's DAG parent (root → latest completed chain tip, else the
       pipeline branch (#3541); child → parent's
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

The run loop runs a **bootstrap reconciliation pass** before the first wave begins. Layer A reconciles slices the contract already marks `COMPLETE`; Layer B reconciles the open-PR side. Layer C ([#2777](https://github.com/jwbron/egg/issues/2777), bundles [#2409](https://github.com/jwbron/egg/issues/2409)) reconciles non-`COMPLETE` slices that did real work before an orchestrator-pod recycle interrupted the prior run. The Layer-C classifier (`_classify_non_complete_slice`) reads `SliceStatus`, queries the gateway for the integration branch's origin commit count, and looks up the slice's consensus tracker, then applies a 5-way decision: (1) `IN_PROGRESS` with no commits → no-op (scheduler re-yields `READY`); (2) `IN_PROGRESS` + commits + no consensus → mark spawned; (3) `IN_PROGRESS` + commits + consensus reached → mark `COMPLETE`; (4) `BLOCKED` without a pending HITL → escalate to HITL; (5) corrupt / unclassifiable → escalate to HITL. Cases 4/5 create unresolved `Decision` objects on the contract via `_escalate_layer_c_hitl` rather than silently re-yielding `READY` (silent classification error is worse than an operator pause). The classifier's gateway-probe failure default is "fresh, re-yield `READY`" — the safer direction for the scheduler — which is deliberately the opposite of `_resolve_slice_base_branch`'s probe-failure default ("derived parent" — the safer direction for the next push). See [`Slice/phase restart hardening`](orchestrator.md#slicephase-restart-hardening-closes-2409) for the full restart-hardening picture, including the slice-aware `restart_phase` clear and the per-slice consensus tracker reconstruction at startup.

Per-slice agent teams are spawned via the existing
`ConcurrentPhaseExecutor` machinery with `slice_id` plumbed through:

- `spawn_all` registers the BRC tracker under the nested
  `{pipeline_id}/{slice_id}` key.
- `_spawn_agent` resolves the head ref via
  `get_worktree_branch(role, slice_id=...)`, which returns the
  shared per-slice integration branch all roles on a slice push to.
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
the body leads with the planner's reviewer-facing slice `goal`, shows
what the branch actually contains (commit subjects + diffstat, #3115),
and carries the slice subject, files affected, and full task
descriptions with acceptance criteria behind a `<details>` fold.
Strategic context (analysis doc, plan doc, refine/plan BRC history)
lives on the **context PR** —
`egg/<pipeline_id>/work → main`, opened up-front at the plan→implement
boundary (#2777) — which slice PRs link to via `context_pr_number`.
Program-level test plan, manual steps, and pre-merge obligations now
live on that context PR (no longer on a "terminal slice umbrella"), so
every slice PR is purely slice-scoped.

- **Title.** `[<program-slug>][<position>] <subject>`, capped at 70
  chars; titles over that length truncate at a word boundary (#3115).
  `program-slug` is derived from `pipeline_id`: `issue-<N>`
  pipelines collapse to `issue-<N>` (version suffix dropped);
  `pipeline-<hash>` pipelines keep a truncated prefix. `position` is
  `slice-N/M` and `subject` is the slice name; the legacy `merge-gate`
  marker and the program-title fallback on the terminal slice were
  removed alongside the umbrella banner (#2777). When `program_title`
  is empty (older contracts / planner skipped the field), every slice
  falls back to the deterministic `{slice_id}: {slice_name}` form
  (#2539).
- **Body (uniform shape, #3115).** Lead paragraph — the planner's
  reviewer-facing per-slice `goal` from the contract (falls back to
  the first sentence of `program_description` for pre-#3115
  contracts) → `**Base PR:** #<context_pr_number>` whenever the
  number is known (the run loop falls back to `pipeline.pr_number`
  when the contract linkage is missing, #3100/#3115) →
  `## What's in this PR` (commit subjects + diffstat computed by
  `_build_slice_diff_summary` from the pushed integration branch;
  best-effort, omitted on any git/fetch failure) → `## This slice`
  (slice name, files affected, full task descriptions + acceptance
  criteria behind a `<details>` fold) → `## Stack` (position, base
  PR, base branch). Program-level test plan, manual steps and
  pre-merge obligations live on the up-front context PR (#2777), not
  on any slice PR. Prose fields (`goal`, inlined program narrative)
  have their YAML block-scalar hard wraps joined back into paragraphs
  before rendering (`unwrap_soft_breaks`, #3122).
- **Reverse linkage (#3122).** After a slice PR opens, the run loop
  parses the PR number from the returned URL, persists it on the
  contract slice (`Slice.pr_number` / `Slice.pr_url` — also on the
  idempotent already-open path, so resumes recover the linkage), and
  refreshes the machine-owned context-PR body so its slice table links
  the new PR (`— #N`). The refresh routes through
  `GatewayClient.update_pr_body` (synthetic session →
  `/api/v1/gh/pr/edit`, same seam as `rebase_onto`'s base retarget)
  and is strictly best-effort: failures log and never fail the slice.
- **No `context_pr_number` — should not occur under #2777.** Because
  the context PR is opened up-front, hard-required and idempotent at
  the plan→implement boundary, every slice PR sees a populated
  `context_pr_number`. If it is unexpectedly missing AND the contract
  carries program metadata, the program narrative (description + test
  plan + manual steps) is inlined around the sections above as a UX
  backstop; the stack is non-mergeable until the operator reconciles
  the missing context PR.

The `## Stack` block is the body's footer; nothing follows it. (No
machine-readable trailing stack line is emitted — a repo-wide search found
no consumer parsing one.)

Task bullets carry full descriptions and a nested `Acceptance criteria:`
line when `task.acceptance_criteria` is set; the whole task list
renders inside a collapsed `<details>` block so traceability does not
crowd out the reviewer-facing summary.

`program_deferred_actions` is **terminal-only** by convention — the
merge gate is the last-to-merge PR in the stack, so obligations live
on exactly one PR across the chain. Each non-terminal body branch
asserts `program_deferred_actions is None` so a mis-routed obligations
payload fails fast instead of being silently dropped (#2354 / #2746).

The implement-phase run loop (`_run_implement_phase_slices` in
`orchestrator/routes/pipelines.py`) threads the kwargs uniformly across
every slice (no terminal-slice selection under #2777):

1. For **every** slice: compute 1-based `slice_index` (position in
   `contract.slices`) and total `slice_count`; collect
   `slice_files_affected` as the union of `task.files_affected` across
   the slice's tasks; pass `slice_goal` from the contract slice
   (#3115); pass `context_pr_number` from
   `program_pr.context_pr_number` (the up-front context PR opened by
   #2777 at the plan→implement boundary), falling back to
   `pipeline.pr_number` when the contract linkage is missing
   (#3100/#3115); compute `commit_subjects` + `diffstat` via
   `_build_slice_diff_summary` (best-effort — `(None, None)` on any
   fetch/git failure and the PR opens without the section).
2. `program_deferred_actions` is **always `None`** on slice PRs —
   pre-merge obligations live on the context PR (#2777), not on any
   slice PR.
3. `terminal_slice_id` is no longer threaded; the legacy "merge-gate"
   marker was removed alongside the umbrella banner.

### Multi-terminal-forest pointer caveat

The slice DAG is a forest (≤1 DAG parent per slice — see
[Forest Validation](#plan-parser--forest-validation)) and a
multi-tree forest can have multiple terminal slices, one per tree.
Under #2777 the program-level test plan, manual steps and pre-merge
obligations live on the context PR (`egg/<pipeline_id>/work → main`),
not on any slice PR, so there is no longer a "chosen terminal" / merge
gate selection step. The implement-phase run loop passes
`context_pr_number` to every slice and `program_deferred_actions`
remains `None` on every slice PR (the obligations section is rendered
into the context PR body, not into a terminal slice). The
multi-terminal-forest pointer ambiguity that motivated the prior
`terminal_ids[-1]` selection is therefore moot — operators reviewing a
multi-tree pipeline see slice PRs that all link to the same context PR
as their merge gate.

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
3. The PR's head branch **is** in the set of extant origin branches
   ([#3479](https://github.com/jwbron/egg/issues/3479)): an open PR
   implies its head exists on origin, so a head missing from the set
   means the branch listing is stale or broken (or the branch really is
   gone and the listing hasn't caught up); in every one of those worlds
   the rebase + force-push cannot succeed, and surfacing the orphan
   would retry a doomed rebase on every tick. In particular an **empty**
   extant set (failed listing) yields zero orphans instead of a rebase
   storm across every open slice PR.
4. The PR's base branch is **not** in the set of extant origin branches.

The intended new base is sourced from `Slice.parent_branch_at_creation`,
not inferred from PR metadata — so the reconciler is robust against
parent slice branches that have been renamed or rebased. Roots, slices
whose base still exists, and slices whose head cannot be confirmed on
origin are silently skipped, making each pass idempotent.

The three callables decouple `reconcile_once` from the gateway client and
GitHub API so unit tests can substitute deterministic fakes. In
production the run loop binds them to live gateway helpers — the
reconciler is fully functional, not a no-op:

- **`list_open_prs`** → `GatewayClient.list_open_prs(...)`, which calls the
  orchestrator-only control-plane route `/api/v1/gh/list_open_prs` with
  launcher auth (not a synthetic agent session — [#2925](https://github.com/jwbron/egg/issues/2925)). The gateway constructs a
  fixed read-only argv server-side — `gh pr list --repo <r> --state open
  --limit <N> --json number,headRefName,baseRefName` — and returns
  `{"prs": [...]}`. The `--state open --limit <N>` qualifiers are what
  keep this route narrow rather than a general `gh` shell. JSON parsing
  failures degrade to an empty list (logged warning) so a transient `gh`
  flake does not cause the reconciler to misclassify orphans.
- **`list_remote_branches`** → `GatewayClient.list_remote_branches(...)`,
  which runs `git ls-remote --heads origin` through the existing
  per-agent `ls-remote` allowlist (`operation="ls-remote"`). The gateway
  emits ls-remote flags **before** the repository argument: `git
  ls-remote` stops option parsing at the first positional, so a post-URL
  `--heads` is silently treated as a never-matching ref pattern and the
  listing comes back empty with exit 0
  ([#3479](https://github.com/jwbron/egg/issues/3479)). The reconciler
  treats the returned set as the join key for the orphan check; an empty
  set on failure preserves the conservative default (no PR is treated as
  orphaned, because no head can be confirmed extant).
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

`list_remote_branches` and `rebase_onto` flow through the existing
per-agent allowlists. `list_open_prs` uses the dedicated control-plane
route `/api/v1/gh/list_open_prs` with launcher auth rather than a
synthetic agent session: no general-purpose privileged gh-command surface
is introduced — the route accepts only `repo`/`limit` and constructs the
fixed read-only argv server-side.

## Per-slice repo (multi-repo pipelines)

A pipeline can coordinate PRs across an **arbitrary number of repositories**
(#3393). The repo set is list-shaped end to end — `Pipeline.repos:
list[RepoSpec]`, one entry per repo, each pinning its own `base_branch`. There
is no two-repo special case and no primary+secondary shape in the data model;
nothing may assume `len(repos)` ∈ {1, 2}. See the
[SDLC Pipeline Guide § Multi-Repo Pipelines](../guides/sdlc-pipeline.md#multi-repo-pipelines)
for the submission surface, the primary-repo concept, and the uniform-visibility
/ uniform-auth rules enforced at submission time.

The slice DAG is where cross-repo coordination lives. Each slice maps to
**exactly one** repo, and cross-repo work is expressed as **multiple slices with
dependencies** — never a single slice touching two repos.

### `Slice.repo` — the 1:1 rule

`Slice.repo` (`str | None`, `owner/name`-shaped) names the one repository a
slice operates in. **Slice ↔ repo is 1:1**: a slice's work, worktree, branch,
review diff, test scope, and PR all live in that single repo. This keeps
worktree selection, PR routing, reviewer diffs, and test gating single-repo at
the slice level — the coordination complexity lives only in the slice DAG.

`Slice.repo` defaults to `None`. The absent ⇒ primary default is resolved at
**runtime** by `resolve_slice_repo(slice, pipeline)` (orchestrator layer), which
returns `slice.repo` when set else `pipeline.primary_repo`. The contract model
holds no repo list and cannot see the pipeline, so the default is deliberately
*not* a model-side migration. Adding the optional field bumped the contract
`schemaVersion` to `1.4` via a **pure additive after-stamp**
(`_migrate_schema_version_to_1_4`, mirroring the `1.3` precedent): a persisted
pre-1.4 contract loads cleanly and `Slice.repo` stays `None` — no field is
filled by the migration.

### Owner/repo-keyed worktree map

The gateway's `create_worktrees(repos=[...])` already returns a
`repo → worktree path` map; that map is keyed by the **full `owner/repo` slug**
(not the bare short name) so two repos with the same short name under different
owners stay distinct. The full map is exposed to the agent environment as
`EGG_PIPELINE_REPOS` — a JSON object of `owner/repo → container worktree path` —
so a per-slice agent can select the worktree for *its* slice's repo rather than
being collapsed onto the primary. Naming-oriented env (`EGG_PIPELINE_REPO`,
`EGG_REPO_PATH`) still resolves to the primary for back-compat.

### Per-repo work branch & context PR

Work branches and context PRs are **lazy-per-repo**: every repo that owns ≥1
slice gets its own `egg/<pipeline_id>/work` branch and its own context PR
(`egg/<pipeline_id>/work → <that repo's base>`); a submitted repo that ends up
with no slices gets neither. A single-repo pipeline gets exactly one work branch
and one context PR, byte-equivalent to the pre-multi-repo path. Context-PR
bodies cross-reference their sibling context PRs in the pipeline.

For a multi-repo pipeline (`len(repos) > 1`), `kubernetes_spawner` asks the
gateway to materialize each repo's work branch on its own remote right after
the worktree is created (`create_worktrees(..., push_branches=True)`, wired
through the gateway's `_materialize_work_branch_on_remote`, #3393 slice-7):
the fresh worktree HEAD is pushed to
`refs/heads/{assigned_branch or work-branch}`. The push runs for every repo in
the list — including the primary, where it's a no-op the orchestrator's
existing push path already covered. It is best-effort, non-forced, and
idempotent: a branch already present on the remote — even one whose tip has
diverged — is treated as already materialized (a non-fast-forward rejection is
swallowed rather than force-pushed, so the primary's contract-init commit is
never clobbered). This guarantees a secondary repo's context/slice PR always
has a head branch to open against instead of soft-failing on a missing one.
Single-repo pipelines pass `push_branches=False` and stay byte-identical to
the pre-#3393 path.

### Per-slice PR routing

`GatewayClient.create_slice_pr` is repo-parameterized; the run loop passes each
slice's resolved repo (`resolve_slice_repo`, falling back to the primary when
`Slice.repo` is absent), so a slice's PR opens in that slice's repo against that
repo's `egg/<id>/work` context branch. Slice-PR bodies render **sibling
cross-references** — the other pipeline PRs (repo + number) and, for a dependent
slice, the upstream slice's PR it is ordered behind.

### Cross-repo ordering via slice dependencies

A cross-repo dependency is an ordinary slice `dependencies` edge whose two
endpoints resolve to different repos: an edge `B → A` is cross-repo iff
`resolve_slice_repo(A) != resolve_slice_repo(B)`. The canonical case — add a new
schema version in repo A, then migrate the consumer in repo B, where B's cutover
can't land until A's PR merges — is expressed as slice B depending on slice A,
with A in repo A and B in repo B. **Dependencies gate merge-readiness, not
development**: B is developed in parallel with A; only B's PR ready-state waits.

### Cross-repo merge-sequencing hold (two-tier)

`orchestrator/cross_repo_merge_gate.py` implements the two-tier hold that
sequences a cross-repo dependent PR behind its upstream. It is pure logic over
injected gateway-read/write and HITL callables, driven on the existing
stacked-PR reconciler cadence.

- **Tier A — automated merge-state hold (default).** The dependent slice's PR
  opens as a **draft** while the upstream PR is unmerged. A bounded poll watches
  the upstream PR's merge state via the orchestrator-only gateway routes
  `POST /api/v1/gh/pr/merge_state` (read) and `POST /api/v1/gh/pr/ready` (write)
  — see [Gateway README](../../gateway/README.md) — and, on merge, auto-marks
  the dependent PR ready via the `mark_pr_ready(repo, pr_number)` gateway verb
  (wrapping `gh pr ready`). **Merge detection keys off the PR `mergedAt` /
  merged boolean, not head-SHA equality** — a squash or rebase merge produces a
  merge-commit SHA ≠ the PR head, so SHA-equality would misfire. Two failure
  terminals fall through to a HITL hold rather than hanging: an upstream that
  reaches **CLOSED-not-merged**, and a poll that exceeds its **attempt bound**
  (`EGG_ORCH_CROSS_REPO_MERGE_GATE_MAX_ATTEMPTS`, default 240 ticks, ~2h at the
  default 30s reconciler cadence — a never-merging upstream). Both surface on
  pipeline status.
- **Tier B — HITL beyond-merge-state hold (opt-in).** For an edge the plan (or
  task description) marks with a beyond-merge-state condition — a
  release/publish of the upstream repo, a version-pin choice, or a genuine
  cannot-continue development block — the dependent slice opts in via the
  `[hold:beyond-merge-state]` marker in its `goal` (or a task description); the
  dependent PR is then held and released **only by a HITL decision**, never by
  programmatic detection. Absent that marker, a cross-repo edge defaults to the
  Tier-A automated hold.

All HITL holds (Tier B, plus the two Tier-A failure terminals) route through the
same decision-queue mechanism and share a single release path — each registers a
HITL Decision offering two operator-selectable options: release the hold (marks
the PR ready) or keep it held (terminal; the PR stays draft for manual
handling).

### Per-repo gate, diff & convention scoping

Because slice ↔ repo is 1:1, the implement-phase gates scope naturally to the
slice's repo:

- The **test gate** runs in the slice's repo worktree only (resolved from the
  `owner/repo`-keyed worktree map).
- The **reviewer diff** is `git diff` in that worktree against **that repo's**
  base branch.
- The slice agent's **cwd** is the slice's repo worktree, and check/lint
  commands resolve from **that repo's** conventions — its own `CLAUDE.md`,
  linters, and check commands. egg's `make lint` / `make test` apply only to
  slices whose repo is egg.

A slice whose repo is egg (the common case) behaves exactly as a single-repo
pipeline does today.

## Architect, planner & plan-reviewer prompts

The dynamic prompt builders for `task_planner` and `reviewer_plan` teach
the agents the slice schema and constraints:

- **Architect (`architect`)** — sole authority for slice composition
  ([#2809](https://github.com/jwbron/egg/issues/2809)). The architect prompt
  declares this authority explicitly and the architect emits a binding
  `architect-slices.yaml` scaffold alongside its analysis JSON
  (`{identifier}-architect-slices.yaml` under `.egg-state/agent-outputs/`).
  The scaffold encodes slice `id` / `name` / `goal` / `dependencies`;
  `tasks:` is intentionally omitted (that is `task_planner`'s job).
- **Planner (`task_planner`)** — sections appended to the plan phase
  prompt:
  1. *Slice composition is NOT the planner's call* (#2809): the
     architect's `architect-slices.yaml` scaffold is binding. The
     planner copies it verbatim into the `# yaml-tasks` appendix
     (same `id` / `name` / `goal` / `dependencies`, same order) and
     fills in `tasks:` under each slice. The planner does not
     silently re-shape slices — if a slice needs to be subdivided,
     the planner raises NACK pressure on the architect (via the
     plan prose, which `risk_analyst` / `reviewer_plan` pick up)
     rather than re-shaping locally.
  2. *Forest constraint* (HARD): every slice must have ≤1 DAG parent;
     the populator hard-rejects multi-parent slices with
     `ForestValidationError`. The architect's scaffold encodes this
     via a single-parent `dependencies` id (`slice-<N>`); the planner
     preserves it.
  3. *Auto-serialization* for would-be multi-parent slices: the
     architect is responsible for serialising the upstream cluster
     and populating `serialized_chain_order` on the downstream
     slice. The fallback heuristic (`files_affected` Jaccard >0.3,
     then descending fan-out) is documented; the architect's own
     ordering is the source of truth. The planner preserves the field
     verbatim from the scaffold.
  4. The yaml-block key swap: `slices:` is canonical, `phases:` is
     backward-compat.
  5. *Test co-location* (HARD,
     [#3411](https://github.com/jwbron/egg/issues/3411)): when a slice
     removes, renames, or rewrites code, the planner enumerates the
     matching test updates (skip-guard, deletion, rewrite) as tasks in
     that same slice — never a later one — listing the test files in
     the tasks' `files:`. Every cumulative slice tip must be
     independently green or the per-slice green gate
     ([#3398](https://github.com/jwbron/egg/issues/3398)) blocks the
     slice PR. The architect's prompt carries the mirror rule (the
     removing slice's `goal` must cover the test updates). The affected
     tests are statically discoverable with the selector's
     `--impacted-tests <file>...` mode (the same import graph
     `make test` narrowing uses); exit 2 means the closure is
     unavailable and the planner greps the removed symbols instead.
- **Plan reviewer (`reviewer_plan`)** — three prompt sections:
  1. *Forest-violation NACK*: when the populator left a "Plan ingestion
     REJECTED" block on `plan_review_feedback` (or a `forest_violation`
     log discriminator is present), the reviewer NACKs the **architect**
     (slice scaffold ownership belongs to the architect,
     [#2809](https://github.com/jwbron/egg/issues/2809)) with the structured
     errors verbatim and instructs re-emission of the scaffold with
     `serialized_chain_order` populated.
  2. *Slice-sizing NACK* (hard, judgment-based). The reviewer is empowered
     AND required to
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
  3. *Test co-location NACK* (hard,
     [#3411](https://github.com/jwbron/egg/issues/3411)). For each
     slice whose tasks remove or rename symbols, the reviewer checks
     that the test files statically referencing those symbols appear in
     that slice's task `files:` (or an ancestor's). Tests that appear
     only in a later slice — or nowhere — earn a NACK routed to the
     **architect** naming the code files, the referencing test files,
     and the slice each currently sits in. See
     `_get_plan_review_criteria()` §13 for the rubric and the
     `--impacted-tests` self-check.

## Configuration knobs

Most slice/scheduler knobs live in `orchestrator/env_config.py` and
return typed values (positive int / positive float) with logged fallbacks
on parse failure. The green-gate knobs below are read directly via
`os.environ.get` in `orchestrator/slice_green_gate.py`.

| Env var | Type | Default | Controls |
|---------|------|---------|----------|
| `EGG_ORCH_MAX_PARALLEL_SLICES` | int | 1 | **Per-pipeline** wave slice spawn concurrency cap (fallback default). Enforced via `iter_ready` and mirrored on the wave's `ThreadPoolExecutor.max_workers`. Overridden per-pipeline by `PipelineConfig.max_parallel_slices` (set at pipeline creation), which takes precedence when non-null. |
| `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` | int | 4 | **Process-wide** slice cap across ALL running pipelines (#2241 gap 1). Enforced by `orchestrator.global_slice_admit.try_admit()` in the run loop; deferred slices stay READY and re-yield next tick. |
| `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` | int | 3 | Per-slice BRC re-proposal ceiling before HITL escalation. *Currently inert — the run loop does not call `record_cycle`; see Known limitations.* |
| `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` | int | 10 | Pipeline-wide summed slice-cycle cap. *Currently inert — see local cycles row.* |
| `EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS` | float | 60.0 | Grace window before a failure cascade marks the downstream subtree `BLOCKED_ON_FAILED_DEPENDENCY`. |
| `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` | float | 30.0 | Reconciler polling cadence for orphaned child PRs. |
| `EGG_ORCH_CROSS_REPO_MERGE_GATE_MAX_ATTEMPTS` | int | 240 | Poll-attempt budget for the cross-repo merge-sequencing gate (#3393) before a never-merging upstream escalates to a HITL hold; ~2h at the default reconciler cadence. See [Cross-repo merge-sequencing hold](#cross-repo-merge-sequencing-hold-two-tier). |
| `EGG_SLICE_BASE_ANCESTRY_GATE` | str | `on` | Operator kill switch for the admission-time base-ancestry gate (#3541 — see [Root linearization & the base-ancestry gate](#root-linearization--the-base-ancestry-gate-3541)): any of `off`/`0`/`false`/`no` disables the gate; any other value (including unset) leaves it enabled. |
| `EGG_SLICE_GREEN_GATE` | str | `off` | Per-slice green gate rollout switch (#3398): `off` skips the gate entirely; `log` runs the repo's configured checks at the slice tip and logs a red verdict without blocking; `on` blocks slice PR-open on a red verdict. Case-insensitive, with aliases — `on` also accepts `1`/`true`/`yes`, and `log` also accepts `log-only`/`log_only`. Unknown values resolve to `off`. |
| `EGG_SLICE_GREEN_GATE_SKIP_CHECKS` | str (comma-separated) | `security` | Configured check *names* (from `repositories.yaml` `checks`) the gate skips. |
| `EGG_SLICE_GREEN_GATE_TIMEOUT_SECONDS` | int | 1800 | Wall-clock budget for the check-runner pod (spawn-to-terminal); a hung suite degrades to fail-open rather than wedging the slice close. |

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

## Design rationale

Why the model is shaped the way it is:

- **Concurrency is an operator-tunable soft cap, not unbounded.**
  `max_parallel_slices` bounds wave spawn concurrency; the default is low
  ([#2466](https://github.com/jwbron/egg/issues/2466)) to constrain
  container/gateway resource pressure during the implement phase.
- **The schema rename carries a one-version load-time migration** so legacy
  `phases[]` JSON keeps loading rather than breaking on the field swap.
- **`max_cycles` is two-tier** (local per-slice + pipeline-global) so a
  single thrashing slice and a pipeline-wide cycle budget can both trip HITL
  escalation independently.
- **The failure cascade is hybrid** — a grace window before blocking, and
  only the failed slice's downstream subtree is blocked — so a transient
  failure can recover and siblings are unaffected.
- **Lens reviewers run per-slice**, accepting the cross-slice-coherence
  trade-off in exchange for bounding each review's context to one slice.
- **BRC tracker keying is hybrid**: the bare `pipeline_id` is intended for
  cross-slice telemetry and the nested `pipeline_id/slice_id` key isolates
  `CONSENSUS_*` state (see Known limitations for the part not yet wired).
- **No general-purpose privileged gh-command surface.** The reconciler reads
  via narrow read-only routes (per-agent allowlists for `ls-remote`/rebase;
  the control-plane fixed-argv `/api/v1/gh/list_open_prs` with launcher
  auth, [#2925](https://github.com/jwbron/egg/issues/2925)).
- **Stacked-PR rebase** relies on GitHub auto-retarget as the primary path,
  with the reconciler as a safety net for paths that don't trigger it.
- **The forest constraint is enforced at plan ingestion** and would-be
  multi-parent slices are serialised via architect-supplied
  `serialized_chain_order`, which is the source of truth
  ([#2809](https://github.com/jwbron/egg/issues/2809)).

## Out of scope

- **Per-slice MCP control verbs** (`restart_slice`, `get_slice_status`,
  `list_slices`) — tracked in
  [#2199](https://github.com/jwbron/egg/issues/2199). The slice-addressable
  hooks the verbs will wrap are public on `SliceScheduler` already
  (`teardown_slice`, `respawn_slice`, `get_slice_status`, plus the
  implicit `list_slices` view via the scheduler's contract reference).
  `restart_agent` already accepts a `slice_id`: the REST endpoint
  (`POST /api/v1/pipelines/<id>/agents/<role>/restart`) takes
  `?slice_id=slice-N` (or `"slice_id"` in the body).
- **The `record_cycle` cycle-cap wiring and the `EGG_PIPELINE_ID`
  cross-slice telemetry hybrid** are both unfinished — see Known
  limitations at the top of this page.
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
