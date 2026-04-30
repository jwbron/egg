# Analysis: Decompose 15 oversize Python source files to clear the file-size allowlist

> Issue: #2261 | Phase: refine

## Problem Statement

The lint added in #2250 (closing #2248) caps Python source files at **1,500 lines / 100 KB** (~25k tokens — Read tool's soft limit). Fifteen current files exceed the cap and are grandfathered in `scripts/file-size-allowlist.yaml`; growth is rejected but the existing baselines remain. This issue tracks the decomposition program that drives those baselines down to zero so every file is under the global cap, the allowlist is empty, and agent navigation cost is paid once instead of per BRC cycle.

The desired outcome is an **empty allowlist** with all 15 files under the global cap, no behavior change, no breakage of the ~43 symbol patch targets that tests rely on, and seam documentation in `orchestrator/CLAUDE.md` / `gateway/CLAUDE.md` so future contributors know where pieces live.

## Current Behavior

### Current sizes (verified via `wc -l` against HEAD — `egg/issue-2261-refiner/work` at `ebe3bfb4e`)

| File | Lines | Notes |
|---|---:|---|
| `orchestrator/routes/pipelines.py` | **16,401** | Higher than the 15,356 in the issue body — file has grown since #2250 landed. Now contains `_run_pipeline` at line 13524, body ~2,362 lines. |
| `gateway/gateway.py` | **9,890** | Higher than 9,753 in the issue body. |
| `sandbox/egg_lib/orch_cli.py` | 3,537 | |
| `orchestrator/mcp_tools.py` | 2,820 | |
| `orchestrator/gateway_client.py` | 2,357 | |
| `shared/egg_contracts/checkpoint_cli.py` | 2,233 | |
| `sandbox/entrypoint.py` | 2,109 | |
| `gateway/worktree_manager.py` | 2,090 | |
| `orchestrator/overseer/monitor.py` | 2,050 | |
| `orchestrator/peer_consensus.py` | 2,013 | |
| `gateway/git_client.py` | 2,032 | |
| `orchestrator/routes/signals.py` | 1,986 | |
| `scripts/select_tests.py` | **1,875** | Higher than 1,650 in the issue body — file has grown. |
| `gateway/checkpoint_handler.py` | 1,655 | |
| `orchestrator/routes/deployment.py` | 1,604 | |

The bulk-byte outliers (`pipelines.py` + `gateway.py`) account for roughly half the over-cap surface and the heaviest test coupling.

### Test patch surface

`grep` confirms the issue's claim: tests reach into `routes.pipelines.<symbol>`, `gateway.gateway.<symbol>`, and `mcp_tools.<symbol>` via `unittest.mock.patch`. Sampled across the test suite, **at least 43 distinct symbols** are patched at those import paths (representative slice: `_auto_create_pr`, `_build_agent_prompt`, `_build_pr_body`, `_clear_pipeline_runtime_state`, `_commit_statefiles_to_worktree`, `_compute_gateway_mode`, `_detect_default_branch`, `_emit_event`, `_fetch_pr_state`, `_get_draft_path`, `_get_message_store`, `_git_show_draft`, `_handle_pr_creation_failure`, `_inflight_host_waits`, `_persist_phase_brc_history`, `_pipeline_identifier`, `_populate_contract_from_plan`, `_publish_branch_divergence_alert`, `_read_shared_criteria`, `_rebase_pipeline_branch_onto_base`, `_resolve_pipeline`, `_run_concurrent_phase`, `_run_pipeline`, `_spawn_pipeline_run_thread`, `_start_stacked_pr_reconciler`, `_sync_worktree_with_remote`, `_write_brc_history`, `gateway.gateway.get_anthropic_client`, `gateway.gateway.get_credentials_manager`, …). Many test modules patch dozens of these per file (e.g. `test_consensus_polling.py` patches `routes.pipelines.*` 71 times).

If we move a symbol without re-exporting it from the original module, those `patch` calls raise `AttributeError` and we get cascading test failures. **The re-export shim is non-negotiable.**

### Existing decomposition / re-export precedent

- **No prior route-package split exists.** `orchestrator/routes/` is flat — `pipelines.py`, `signals.py`, and `deployment.py` are single files. So this issue is establishing the pattern, not following one.
- **Re-export precedent does exist** in non-route code: `gateway/agent_restrictions.py:30` (`# Re-export from shared package for backwards compatibility / from egg_restrictions.checker import AgentRestrictionResult, …`) and `orchestrator/models.py` (similar comment). This is the idiom the new shims should mirror.
- **Dual-import pattern** (`try: from ..foo import X / except ImportError: from foo import X`) is standard inside `pipelines.py` and `gateway.py` already; submodules must use one style consistently or follow the dual pattern.
- **Slice-DAG (#2137) shipped:** `docs/architecture/slice-dag.md` documents slices, waves, stacked PRs, and the `Phase = Slice` alias. Multi-parent slices are rejected at plan ingestion (the DAG is a forest). Per-slice envs `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` (3) / `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` (10).

### `_run_pipeline` is the structural problem

`_run_pipeline` at `orchestrator/routes/pipelines.py:13524` is **2,362 lines** by itself — already 1.5× the cap. It is a background-thread state machine that loads a pipeline, iterates `PHASE_TRANSITIONS`, spawns agent containers, runs BRC consensus per phase, advances on ACK, re-runs with feedback on NACK, manages overseer/health threads, and handles spurious-PNFE respawn recovery. **Mechanically lifting it into its own submodule does not solve the cap problem** — the new file is still over the cap. Either (a) the function gets a real refactor (extract per-phase handlers + thin orchestration loop) or (b) it needs a permanent allowlist exception with a justification. The issue says punting it is off the table, so a real refactor is in scope.

### CLAUDE.md state

`orchestrator/CLAUDE.md` (13 lines) and `gateway/CLAUDE.md` (11 lines) both exist but document **zero internal seams** — they only point at README.md and cross-cutting docs. Adding seam tables to both is a documented acceptance criterion.

## Constraints

- **Behavior preservation.** Pure refactor; no functional change. If the move surfaces a latent bug, file separately and don't bundle the fix.
- **Test back-compat.** Every `routes.pipelines.<sym>` / `gateway.gateway.<sym>` / `mcp_tools.<sym>` patch target must still resolve from the original module. Moves require re-exports.
- **Production importer back-compat.** External imports the issue calls out (`mcp_tools.py` → `_read_phase_draft`, `_pipeline_identifier`; `unified_sse.py` → `_collect_all_pipelines`; `routes/phases.py` → in-function lazy imports breaking a cycle) must still resolve. Re-exports cover these.
- **Dual-import convention.** New submodules either use one import style consistently *or* keep the existing `try: from ..foo / except ImportError: from foo` dual pattern. No mixed forms inside one file.
- **Allowlist ratchet.** Every PR in the stack drops the affected file's entry in `scripts/file-size-allowlist.yaml` (or removes it once the file is under the cap). The lint already enforces this.
- **Branch prefix `egg/`.** Repo convention. Slice integration branches will be `egg/issue-2261/slice-<n>-<file>`.
- **Slice-DAG forest rule.** Each slice can have at most one DAG parent. If we model "establish pattern" as a parent slice, the 15 file-decomposition slices are siblings under that parent. They cannot have multiple parents.
- **Cap is global, not aspirational.** 1,500 lines / 100 KB. Files that decompose to 1,499 lines but >100 KB still fail; we must hit *both* limits.
- **`make lint` and `make test-all` green at every slice boundary.** Each PR in the stack must pass independently — no "land them together" exceptions.
- **Test-file exemption already exists.** Tests under `test_*.py`, `*_test.py`, and `tests/` directories are exempt; we do not need to decompose oversized parametrized test files.
- **File-boundary gateway enforcement.** During this pipeline, the refiner can only push to `.egg-state/drafts/` and `.egg-state/agent-outputs/`. Source-file moves happen in implement.

## Options Considered

The decomposition problem decomposes (sorry) into three sub-decisions: **(1) module layout pattern**, **(2) `_run_pipeline` strategy**, **(3) slice DAG shape**. I'll enumerate options for each and pair them in the recommendation.

### (1) Module layout pattern

#### Option A1: Sub-package with `__init__.py` re-export barrel

Convert `orchestrator/routes/pipelines.py` → `orchestrator/routes/pipelines/__init__.py`, with submodules `_run_loop.py`, `_criteria.py`, `_pr_lifecycle.py`, `_worktree_ops.py`, `_brc_verdicts.py`, `_status_helpers.py`, `_decisions.py`, `_prompt_building.py`, etc. The `__init__.py` re-exports the public API: `from ._run_loop import _run_pipeline, _spawn_pipeline_run_thread`, etc. Same shape for `gateway/gateway/__init__.py`, `orchestrator/mcp_tools/__init__.py`, etc.

**Pros:**
- `from orchestrator.routes.pipelines import _run_pipeline` continues to resolve through `__init__.py`. Test patch targets (`routes.pipelines.<sym>`) keep working unchanged.
- Clean directory layout that matches Python community convention (e.g. `urllib.parse`, `flask.cli`).
- Submodule names are discoverable via `dir(routes.pipelines)`.
- Each submodule has its own focused namespace and can hit the size cap independently.

**Cons:**
- Flask `app.route(...)` decorators need to be either (a) registered in `__init__.py` only (importing handler functions from submodules) or (b) refactored to use a Flask `Blueprint`. The Blueprint refactor is a behavior-adjacent change that this pure-refactor issue would have to absorb.
- Circular-import risk: today's lazy `routes/phases.py` imports become harder to reason about across a sub-package. Mitigatable with explicit lazy imports but adds friction.
- More files in the diff than Option A2 — somewhat noisier review.

#### Option A2: Sibling-file extraction with thin façade

Keep `orchestrator/routes/pipelines.py` as a thin façade module that re-exports from siblings (`orchestrator/routes/_pipelines_run.py`, `_pipelines_criteria.py`, `_pipelines_pr.py`, …). Façade still owns the Flask `@app.route(...)` registrations.

**Pros:**
- Flask routing stays trivial: route decorators stay on the façade-module functions, which are 1-line wrappers that delegate to siblings.
- Smallest possible diff per slice — easiest to review.
- No directory restructure; less disruptive to grep workflows.

**Cons:**
- Naming conventions get ugly (`_pipelines_*.py` everywhere) and discoverability is worse than a package.
- Facade module still has to grow with each submodule — every new piece of public API needs a re-export line. Easy to drift from the actual symbols and miss a re-export.
- Doesn't generalise as cleanly to non-route files (e.g. `mcp_tools.py` doesn't have a routing concern).

#### Option A3: Mixed — Blueprint for route files, sub-package for non-route files

Use Option A1 (sub-package + re-export barrel) for everything *except* the two `routes/*.py` files. For routes, refactor to Flask `Blueprint` so each submodule registers its own routes on a shared Blueprint. The top-level `__init__.py` aggregates Blueprints.

**Pros:**
- Idiomatic Flask. Each route submodule owns its slice of the URL space cleanly.
- Ends up with the cleanest long-term structure once accepted.

**Cons:**
- Blueprint introduction is **the largest behavior-adjacent change** of any option. Routes get registered on a `Blueprint` instead of the `app`, which changes URL prefix semantics, error-handler scoping, and `before_request` behavior. This is plausibly a bug factory in a pure-refactor issue.
- The plan reviewer / refine reviewer is likely to NACK this as scope creep.

### (2) `_run_pipeline` strategy

#### Option B1: Mechanical extraction only

Move `_run_pipeline` verbatim into `orchestrator/routes/pipelines/_run_loop.py` (or `_pipelines_run.py`). Re-export. Function body is unchanged.

**Pros:**
- Zero behavior risk. The state-machine semantics, race conditions, and respawn recovery are byte-identical.
- Trivial to review.

**Cons:**
- The new file is still 2,362+ lines — **over the cap immediately**. Adds an allowlist entry on the same PR that's supposed to remove one. The issue's acceptance criteria mandate the allowlist is empty.
- Punts the structural problem to a follow-up that may never happen.

#### Option B2: Per-phase handler extraction

Refactor `_run_pipeline` so the body becomes:

```python
def _run_pipeline(...):
    while phase := next_phase():
        handler = PHASE_HANDLERS[phase.name]
        result = handler(pipeline, store, ...)
        if result.advance: store.advance_phase(...)
        elif result.respawn: continue
        else: break
```

Per-phase handlers (`_run_refine_phase`, `_run_plan_phase`, `_run_implement_phase_slices`, `_run_pr_phase`) live in their own modules. Helpers extracted: BRC verdict aggregation, overseer respawn, PNFE recovery, contract serialization.

**Pros:**
- Solves the structural problem. Each per-phase handler is in the 200-500 line range; the orchestration loop becomes 100-200 lines. All under the cap.
- Improves agent navigation cost in line with the issue's stated goal.
- Per-phase handlers are independently testable, which is closer to how new tests get written anyway.

**Cons:**
- Real behavioral risk. The 2,362-line state machine has subtle ordering: respawn loops, gateway-mode probes, run-epoch guards, overseer teardown ordering. Tests cover much of it but not every interleaving.
- Larger PR; harder to review.
- Each per-phase handler needs to share state (worktree path, gateway mode, run epoch, …) — likely via a `_PipelineRunContext` dataclass. Adds new types to the surface.

#### Option B3: Permanent allowlist exception for `_run_pipeline` only

Decompose all the *other* functions out of `pipelines.py`. Leave `_run_pipeline` in its own submodule with a permanent allowlist entry citing this issue's design rationale.

**Pros:**
- All other goals achieved with low risk.
- Allowlist isn't fully empty but is "essentially empty" — one defensible exception.

**Cons:**
- Issue explicitly says punting is off the table: *"`_run_pipeline` is in scope. … punting it to a follow-up is also off the table."* This option directly contradicts that.
- Future agents pay 2,362-line context cost forever.

### (3) Slice DAG shape

#### Option C1: One slice per file, fan-out under a "pattern adoption" parent slice

```
slice-1 (parent): "Establish decomposition pattern + seam docs"
  ├─ slice-2: scripts/select_tests.py
  ├─ slice-3: shared/egg_contracts/checkpoint_cli.py
  ├─ slice-4: sandbox/egg_lib/orch_cli.py
  ├─ slice-5: sandbox/entrypoint.py
  ├─ slice-6: gateway/git_client.py
  ├─ slice-7: gateway/worktree_manager.py
  ├─ slice-8: gateway/checkpoint_handler.py
  ├─ slice-9: orchestrator/gateway_client.py
  ├─ slice-10: orchestrator/peer_consensus.py
  ├─ slice-11: orchestrator/overseer/monitor.py
  ├─ slice-12: orchestrator/routes/signals.py
  ├─ slice-13: orchestrator/routes/deployment.py
  ├─ slice-14: orchestrator/mcp_tools.py
  ├─ slice-15: gateway/gateway.py
  └─ slice-16: orchestrator/routes/pipelines.py
```

**Pros:**
- Maximum reviewability — each PR is a focused, single-file decomposition.
- Maximum parallelism — all 15 child slices can run in waves concurrently subject to `EGG_ORCH_MAX_PARALLEL_SLICES=5`.
- Easy revert per file.
- The "pattern adoption" parent is small (mostly docs + 1-2 small files to validate). Subsequent slices follow the established template.

**Cons:**
- 16 PRs to land. If the pattern adoption slice picks the wrong shape, downstream slices may need rework — though the issue suggests starting cheap precisely to mitigate this.
- Requires the planner to model 15 sibling slices on one parent, which is well within the forest rule.

#### Option C2: Per-package cohort slices

```
slice-1 (parent): pattern adoption
  ├─ slice-2: scripts/* (1 file)
  ├─ slice-3: shared/* (1 file)
  ├─ slice-4: sandbox/* (2 files)
  ├─ slice-5: gateway/{checkpoint_handler,git_client,worktree_manager}.py
  ├─ slice-6: orchestrator/{gateway_client, peer_consensus, mcp_tools}.py + overseer/monitor.py
  ├─ slice-7: orchestrator/routes/{signals,deployment}.py
  ├─ slice-8: gateway/gateway.py (alone)
  └─ slice-9: orchestrator/routes/pipelines.py (alone)
```

**Pros:**
- Fewer PRs — easier on overseer review bandwidth.
- Cohort slices share imports / refactor patterns, so the pattern proves itself within a single PR for 2-3 files.

**Cons:**
- Larger PRs are harder to review and harder to revert.
- A NACK on one file in a cohort blocks the whole cohort.
- Less parallelism — fewer slices means fewer concurrent waves.

#### Option C3: One slice for the program, one PR

**Pros:** lowest coordination overhead.

**Cons:** completely unreviewable; violates everything #2137 was built for. Listed only for completeness; not a real option.

## Recommended Approach

**Combine A1 (sub-package + re-export barrel) + B2 (per-phase handler extraction for `_run_pipeline`) + C1 (one slice per file, fan-out under a pattern adoption parent).**

Rationale:

1. **Sub-package layout (A1) generalises across all 15 files.** Routes, mcp_tools, gateway, sandbox CLIs — all benefit from the same `<module>/__init__.py` re-export pattern. We avoid the Blueprint refactor (A3) because it's behavior-adjacent and the issue is a pure refactor. We avoid the sibling-file shape (A2) because the naming gets ugly across 15 files and discoverability is worse.

2. **`_run_pipeline` gets a real per-phase refactor (B2).** The issue says punting is off the table; B1 (mechanical only) puts the new file over the cap on day one and B3 (permanent exception) violates the explicit "in scope" call-out. B2 is the only option that satisfies the acceptance criteria. The behavioral risk is mitigated by:
   - Doing it in `slice-16` *last*, after the pattern is proven.
   - Keeping the new per-phase handlers as private functions inside `_run_loop.py` initially — the public surface (`_run_pipeline`) is unchanged.
   - The existing test suite is dense at this seam (`test_consensus_polling.py`, `test_brc_nack_iteration.py`, `test_concurrent_*.py`, `test_advance_phase_*.py` — many tests patch the very symbols being refactored). `make test-all` green is the gate.

3. **One slice per file (C1) maximises reviewability and parallelism.** 15 sibling slices under a single pattern-adoption parent. The "pattern adoption" parent (`slice-1`) does three things: (a) creates `docs/guides/decomposition-pattern.md` documenting the re-export shim convention, (b) decomposes one of the smallest, lowest-risk files (`scripts/select_tests.py` is the natural candidate — pure helper, fewest imports) as the reference implementation, (c) updates `orchestrator/CLAUDE.md` and `gateway/CLAUDE.md` with seam-table templates for downstream slices to fill in. Once `slice-1` lands, `slice-2 … slice-15` can run in waves with concurrency `EGG_ORCH_MAX_PARALLEL_SLICES=5`.

4. **`gateway/gateway.py` and `orchestrator/routes/pipelines.py` are the last two slices** because they're the largest, most coupled, and the riskiest. By the time they run, the pattern is proven on 13 simpler files.

5. **`pipelines.py` becomes its own multi-PR sub-stack inside `slice-16`.** Plan phase will likely need to model `pipelines.py` as the *terminal* slice in the chain with internal sub-tasks for each cluster (run loop, PR lifecycle, criteria, BRC verdicts, prompt building, status helpers, decisions, worktree ops). That's a plan-phase decision — refine just flags that it's coming.

This combination respects all six "non-negotiables" in the issue:

| # | Non-negotiable | How the recommendation addresses it |
|---|---|---|
| 1 | Test back-compat via re-exports | A1 sub-package's `__init__.py` re-exports every symbol that tests patch. |
| 2 | External importers preserved | Same re-export barrel; verified per-symbol against `mcp_tools.py`, `unified_sse.py`, `routes/phases.py` callers in plan. |
| 3 | Dual-import convention | New submodules use one consistent style internally; the façade `__init__.py` keeps the existing dual `try/except ImportError` shape for callers. |
| 4 | Allowlist ratchet | Each slice's PR drops the entry on the same commit; lint enforces. |
| 5 | `_run_pipeline` solved in-band | B2 per-phase extraction in `slice-16`. |
| 6 | Branch prefix `egg/` | Slice branches `egg/issue-2261/slice-<n>-<file>`. |

## Open Questions

The questions below need a human decision before plan kicks off. Each one is registered via `egg-contract` so it surfaces on the issue's HITL gate.

*(Questions registered in the contract — see decision/feedback IDs in the contract after refine completes.)*

1. **Layout pattern.** Sub-package + `__init__.py` barrel (A1) vs sibling-file façade (A2) vs Blueprint refactor for routes (A3). Decision affects every slice.
2. **`_run_pipeline` strategy.** Mechanical extract only (B1, leaves cap exceeded) vs per-phase handler refactor (B2, behavior risk) vs permanent allowlist exception (B3, contradicts issue text).
3. **Slice DAG shape.** One slice per file (C1) vs per-package cohorts (C2). C1 = 16 PRs; C2 = ~9 PRs.
4. **Pattern-adoption parent slice.** Should slice-1 be (a) docs-only ("pattern proposal" PR for review before any moves), (b) docs + one small file as reference (recommended), or (c) skip the parent entirely and let each slice independently follow a CLAUDE.md doc?
5. **Re-export style.** Explicit per-symbol (`from ._run_loop import _run_pipeline, _spawn_pipeline_run_thread, ...`) vs `__all__`-driven wildcard (`from ._run_loop import *`) vs hybrid (`__all__` per submodule, explicit barrel). Affects how easy it is to spot drift between symbol moves and re-exports.
6. **Submodule naming.** Underscore-prefixed (`_run_loop.py`, `_criteria.py`) signaling "private to the package" vs unprefixed (`run_loop.py`, `criteria.py`) treating the submodules as part of the public structure. Underscore-prefix matches current codebase convention for module-private helpers; unprefixed is more discoverable.
7. **`mcp_tools.py` cross-package imports.** Today `mcp_tools.py` does `from routes.pipelines import _read_phase_draft, _pipeline_identifier`. After decomposition, do we keep importing these via the re-export barrel (`from routes.pipelines import _read_phase_draft`) or migrate to direct submodule import (`from routes.pipelines._readers import _read_phase_draft`)? Re-export keeps the API stable; direct import skips one layer.
8. **Pre-existing growth.** `pipelines.py` is now 16,401 lines (issue body listed 15,356) and `select_tests.py` is 1,875 (listed 1,650). Should plan use the higher current numbers as the baselines, or do we treat the issue body's numbers as the contract?
9. **Sub-stacking inside `slice-16` (`pipelines.py`).** Should plan model `pipelines.py`'s internal decomposition as a single slice with multiple commits/sub-tasks, or as a chain of sub-slices (`slice-16a`, `slice-16b`, …)? The forest rule allows the chain shape, and stacked PRs already work; but the slice-DAG vocabulary doesn't currently distinguish "internal sub-stack" from "regular slice chain".
10. **Routes registration when splitting `routes/pipelines.py`.** The current file uses `@app.route(...)` decorators directly on handler functions. If we go with A1 (sub-package) and avoid Blueprints (A3), the cleanest path is: keep `@app.route(...)` decorations in `routes/pipelines/__init__.py`, where the body of each route is `return _impl_module.handler(*args)`. Is that an acceptable thinness, or should plan invest in a Blueprint refactor as part of this issue?
11. **Test patch surface drift.** Some test modules patch with both `routes.pipelines._foo` and a more direct `orchestrator.routes.pipelines._foo` form. Is it acceptable to fix any drifted patch paths in the same slice as the file that owns the symbol, or should those test rewrites stay strictly out of scope?
12. **`gateway/gateway.py` Flask routes.** Same question as #10, but for `gateway.py` — which has more concurrent route registrations. Confirm whether the same convention holds.
13. **Sequencing of "small first" vs critical path.** Issue suggests cheap files first to validate. Is a strict alphabetical/size-ordered fan-out fine, or are there integration windows (e.g. release cuts, scheduled deploys) we should align with?
14. **Concurrency budget.** `EGG_ORCH_MAX_PARALLEL_SLICES=5` is the per-pipeline default. With 15 sibling slices, do we cap concurrency lower (e.g. 3) so reviewer bandwidth isn't oversubscribed, or run hot at 5?
15. **Are integration / E2E tests in scope?** `make test-all` covers unit + integration. Are there release-channel smoke tests outside `make test-all` that should also gate each slice's PR?

## Complexity Assessment

**high** — this is a 15-file, 16-PR program with the two largest files in the repo as the climactic slices, a real state-machine refactor of a 2,362-line function, and a test patch surface of 43+ symbols that must remain stable across moves. The slice-DAG shape, the re-export discipline, and the `_run_pipeline` per-phase extraction each warrant focused plan-phase design work.

---

*Authored-by: egg*
