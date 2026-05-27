# Release note — Context-PR collapse, PR-phase deletion, schema 1.1 → 1.2

**Issue:** [#2777](https://github.com/jwbron/egg/issues/2777) —
collapse the parallel-stack-root context-branch topology introduced
by [#2548](https://github.com/jwbron/egg/issues/2548), delete the
standalone PR phase, drop the "umbrella" PR terminology, and clean up
the deprecated `ConsensusEvaluator` module. Bumps the contract
schema from 1.1 to 1.2.

## What changed

### 1. Context-PR topology collapsed

The Context PR no longer lives on a dedicated `egg/<id>/context`
branch. It now opens directly on the pipeline tip branch
(`egg/<id>/work`) against the configured base branch (typically
`main`).

- **Deleted**: `GatewayClient.create_context_branch`, the
  `ContextBranchDiverged` exception, the gateway push-exemption regex
  `_CONTEXT_BRANCH_RE = r"^egg/[A-Za-z0-9][A-Za-z0-9_-]*/context$"`,
  the `_resolve_slice_1_context_branch_from_contract` fallback, and
  the `_gather_context_pr_files` helper that copied refine/plan
  artifacts onto a temp worktree.
- **Replaced by**: `_open_context_pr_at_implement_start(pipeline_id)`
  in `orchestrator/routes/pipelines.py` — the single hard-required
  opener that runs once at the plan→implement boundary.
- **Slice-1 base resolution**: slice-1's parent branch is now
  `egg/<id>/work` (resolved by the new `_resolve_slice_base_branch`
  helper); subsequent slices still stack on their predecessor's
  integration branch.

### 2. Standalone PR phase deleted

`PipelinePhase.PR` is removed from the phase enum, the phase graph
collapses to terminal `IMPLEMENT`, and every previously phase-scoped
PR-creation step is gone.

- **Deleted**: `_should_skip_pr_phase_auto_pr`,
  `_finalize_pr_phase_failed`, the
  `_maybe_open_base_pr_for_plan_to_implement` soft-fail wrapper, the
  `_context_pr_events_emitted` dedup set + lock, the `context_pr.failed`
  / `context_pr.skipped` event-bus entries, the `dag_visualizer.py`
  PR node + edge, the gateway `phase_filter.py` PR row, and the
  `mcp_tools.py::advance_phase` PR target validation.
- **Replaced by**: a single up-front opener at the plan→implement
  boundary. Both monolithic and slice-DAG pipelines go through the
  same path — the previous `len(contract.slices) > 1` branch
  suppression is gone.

### 3. Schema 1.1 → 1.2 (breaking)

`schemaVersion` bumped from `"1.1"` to `"1.2"`. Two breaking changes:

| Field / Member | Schema 1.1 | Schema 1.2 |
|----------------|-----------|-----------|
| `PRMetadata.context_title` | optional planner-authored | **removed** |
| `PRMetadata.context_description` | optional planner-authored | **removed** |
| `PRMetadata.context_branch` | optional orchestrator-populated | **removed** |
| `PRMetadata.context_pr_number` | optional orchestrator-populated | retained (now references `egg/<id>/work → main` PR) |
| `PipelinePhase.PR` | enum member | **removed** |

PR title and body for the program-level Context PR now come from
`pr.title` and `pr.description` (the regular per-pipeline fields)
rather than the deleted `pr.context_title` / `pr.context_description`
siblings.

### 4. "Umbrella" terminology dropped

The terminal slice — historically called the "umbrella PR" — is now
called the **merge-gate PR**. The literal banner
`> **Program-level umbrella PR — terminal slice of pipeline `{pipeline_id}`.**`
is gone, along with the `umbrella_has_program_block` plumbing and
the umbrella narrative comments in `gateway_client.py` and
`pipelines.py`. The merge-gate PR still renders the program-level
test plan, manual steps, and pre-merge obligations — only the banner
and naming changed. Subsumes
[#2389](https://github.com/jwbron/egg/issues/2389).

### 5. `ConsensusEvaluator` deleted

The legacy `orchestrator/consensus.py` module (the
`ConsensusEvaluator` class and `get_consensus_evaluator()` singleton,
deprecated since BRC consensus landed) is deleted. The
`evaluator.clear(pipeline_id)` call in `restart_phase`'s
consensus-clear block is removed. BRC's `PeerConsensusTracker`
(`orchestrator/peer_consensus.py`) is the only consensus path in
production.

### 6. `pr_phase_no_pr` overseer alert removed

With the PR phase deleted, the
`_check_pr_phase_outcome` safety-net probe in
`orchestrator/overseer/monitor.py` and the `pr_phase_no_pr` alert
type cannot fire. Both are removed.

### 7. New helpers introduced

- `_is_slice_dag_mode(contract: Contract) -> bool` — single source of
  truth for `len(contract.slices) > 1` (the previous repeated
  checks at multiple call sites were collapsed).
- `_resolve_slice_base_branch(contract: Contract, slice_index: int) -> str` —
  resolves a slice's parent branch with a merge-base fallback when
  the recorded parent is gone.
- `PlanPreflightError` — typed plan-phase pre-flight validator that
  runs at plan-phase completion (AC-1a). Rejects malformed plans at
  the gate rather than at PR-open time.
- `ContextPrCreationError` — typed exception raised by
  `_open_context_pr_at_implement_start` when the gateway open fails.
  The orchestrator catches it and queues a HITL decision; there is
  no soft-fail `return None` path anymore.

## Why a clean break (no silent migration)

The 1.0→1.1 schema bump was additive — pre-1.1 contracts auto-promoted
on load via a Pydantic `model_validator(mode="after")`. The 1.1→1.2
bump is **not** additive: it removes three `PRMetadata` fields and one
`PipelinePhase` enum member. A silent migration would have to drop
data without operator visibility.

Feedback Q5 (recorded in the refine BRC history for #2777) confirmed
**zero in-flight pipelines** would be affected by a clean break. The
landed schema 1.2 therefore raises a clear Pydantic `ValidationError`
when a legacy v1.1 contract on disk still carries the removed fields,
with the message: `schemaVersion bumped to 1.2; legacy fields removed`.
Operators see the error at the orchestrator's contract-load path and
know exactly what to fix.

## Migration procedure

If a legacy v1.1 contract surfaces after deploying #2777:

1. Locate the contract JSON
   (`.egg-state/contracts/issue-<N>.json` or `<pipeline-id>.json`).
2. Bump `schemaVersion` from `"1.1"` to `"1.2"`.
3. Delete the three removed `pr.*` fields if present:
   - `pr.context_title`
   - `pr.context_description`
   - `pr.context_branch`
4. Retain `pr.context_pr_number` — it now references the
   `egg/<id>/work → main` PR opened at the plan→implement boundary.
5. If `pr.title` and `pr.description` are unpopulated on a v1.1
   contract that did populate `context_title` / `context_description`,
   copy the deleted `context_title` / `context_description` values
   into them so the Context PR carries the intended framing.
6. Validate the file loads: `egg-contract --pipeline-id <id> show`.

For a typical in-flight pipeline, the simpler path is to re-run the
plan phase: the planner emits a fresh v1.2 contract from the plan
draft on disk and the orchestrator advances to implement.

## Operator-visible behaviour changes

- **PR opening now happens earlier.** The `egg/<id>/work → main` PR
  appears on GitHub at the plan→implement boundary, not at pipeline
  completion. Reviewers can begin approving the program-level
  rollup while slices are still implementing.
- **Idempotent retries are free.** `_open_context_pr_at_implement_start`
  runs `gh pr list --head egg/<id>/work --base main --state open` as a
  pre-flight. If the PR already exists (e.g. a `restart_phase`
  re-entered the boundary), the existing number is persisted on
  `contract.pr.context_pr_number` and the opener returns without a
  second `gh pr create`. The same idempotent pre-flight was added to
  `GatewayClient.create_slice_pr` (cq-8) so transient `gh` failures
  no longer cascade the slice to `FAILED`.
- **Failures escalate, not soft-fail.** A gateway failure during
  `_open_context_pr_at_implement_start` raises `ContextPrCreationError`
  and surfaces as a HITL decision. The prior soft-fail `return None`
  path that left the slice stack unmergeable is gone.
- **Branch hygiene at pipeline delete.** `egg-orch pipeline delete <id>`
  removes `egg/<id>/work` and per-container worktree branches. There
  is no separate `egg/<id>/context` branch to clean up anymore — the
  Context PR is opened against `egg/<id>/work` directly. Closing the
  PR is sufficient to fully retire a pipeline's GitHub surface.

## Pointers

- **Opener**: [`orchestrator/routes/pipelines.py::_open_context_pr_at_implement_start`](../../orchestrator/routes/pipelines.py).
- **Pre-flight validator**: [`shared/egg_contracts/plan_parser.py::PlanPreflightError`](../../shared/egg_contracts/plan_parser.py).
- **Slice base resolution**: [`orchestrator/routes/pipelines.py::_resolve_slice_base_branch`](../../orchestrator/routes/pipelines.py), [`_is_slice_dag_mode`](../../orchestrator/routes/pipelines.py).
- **Schema**: [`shared/egg_contracts/models.py::PRMetadata`](../../shared/egg_contracts/models.py), [`PipelinePhase`](../../shared/egg_contracts/models.py).
- **Related docs**:
  - [Orchestrator Architecture — Context PR](../architecture/orchestrator.md#context-pr-2548-collapsed-in-2777)
  - [SDLC Pipeline Architecture](../architecture/sdlc-pipeline.md)
  - [Concurrent Execution — Slice PR Stack](../guides/concurrent-execution.md#slice-pr-stack)
  - [Slice-DAG Implement Phase](../architecture/slice-dag.md)
  - [Pipeline Health Monitoring](../guides/pipeline-health-monitoring.md)
  - [Conditional ACK Reference](../reference/conditional-ack.md)
