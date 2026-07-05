# Coder BRC memory — issue-3312-v2, slice-4 (decompose orchestrator/routes/pipelines.py, closes the file-size program)

## Operator directive (cq-3, RESOLVED 2026-07-05) — BINDING
- One-shot model is FIXED (build eca7ca740): worktree re-attach preserves clean unpushed commits that descend from the slice base. **Work INCREMENTALLY across invocations toward the single PR.**
- Commit after each cohesive cluster extraction; commits persist across invocations. NO re-slicing — scope/structure stay as contracted.
- Direct `git push` is BLOCKED in pipeline sessions (only `mcp__brc__propose` pushes). So durability = COMMITS, not pushes. Do NOT propose until the decomposition is COMPLETE (barrel under cap + allowlist empty) — a partial propose gets NACKed.
- Recovery: baseline `0228f4a9f` and the extraction chain are in the object store; fast-forward-recover the newest clean tip rather than restarting from baseline.

## Current state (HEAD after this invocation)
- Branch `egg/issue-3312-v2/slice-4`; origin still at base `64fa30773` (nothing pushed — push is blocked).
- Barrel `orchestrator/routes/pipelines/__init__.py` = **21,176 lines** (was 30,520). Still OVER cap — WIP allowlist entry for `orchestrator/routes/pipelines/__init__.py` grandfathers it during extraction; **must be removed at the terminal commit** (files: map → EMPTY is the program's terminal criterion).
- Submodules extracted so far (all under 1500 lines / 100KB):
  - Recovered chain (pre-this-invocation): `_criteria` `_drafts` `_reviews` `_context_pr` `_brc_history` `_statefiles` `_worktree_sync` `_alerts`.
  - This invocation: `_overseer` (740L, 13 overseer detection-plane fns), `_slice_state` (1094L, 15 slice-DAG fns), `_drivers` (259L, 5 driver-thread fns), `_decisions` (253L, 7 HITL/divergence-decision fns).
- **Verification: `pytest --collect-only` over orchestrator/tests+tests = 16,125 tests, 0 import errors.** Per-cluster tests green. `make lint`/ruff clean on every touched file.

## Extraction convention (matches LANDED slice-15 routes/signals; see orchestrator/CLAUDE.md seam tables)
- Flask blueprint: `@pipelines_bp.route` decorators + thin wrappers STAY in the barrel (decision-8). Only helper/handler bodies move.
- Submodule reaches barrel-resident + test-patched globals via `import routes.pipelines as _pkg` → `_pkg.<name>`. Bodies VERBATIM (only free barrel-global refs gain a `_pkg.` prefix).
- Barrel re-exports every moved symbol at the bottom via `from ._x import (...)  # noqa: E402,F401` (keeps the 64 `patch("routes.pipelines.<name>")` seams resolving — all re-exported).
- Module-level CONSTANTS stay barrel-resident (referenced via `_pkg.`); do NOT move them (the tool only moves def/class).
- `global` statements: NONE exist in the barrel → no shared-mutable-state rebinding hazard (state is mutated in place via `_pkg.`).

## The extraction TOOL — REUSE IT: `.egg-state/agent-outputs/coder/slice4_xtract.py`
- Run from repo root: `.venv/bin/python .egg-state/agent-outputs/coder/slice4_xtract.py <submodule> "<title>" sym1 sym2 ...`
- AST/scope-aware; rewrites free barrel-global Name refs → `_pkg.` (text-position insert, verbatim), skips builtins/locals/function-local-imports, captures barrel names bound inside top-level try/except + `if TYPE_CHECKING`. `Literal`/`Annotated` are NOT prefixed (would break ruff forward-ref special-casing) and are auto-imported from typing.
- Post-run manual fixups per cluster: (a) add a `TYPE_CHECKING:` block for any string forward-ref types the moved fns use that pyflakes flags F821 (e.g. `ContainerSpawner` via `..container_spawner` try/except; overseer also needed `CorrectiveExecutor`/`AdjudicationVerdict`/`SpawnedContainer`); (b) drop now-unused forward-ref imports from the BARREL (they moved out → F401); (c) `ruff check --fix --select I001` + `ruff format` both files; (d) `PYTHONPATH=orchestrator .venv/bin/python -c "import routes.pipelines"` + targeted pytest; (e) commit.
- Verify per cluster: `ruff check --select F821 <submodule>` should be empty after fixups.

## Remaining work (still in the barrel, ~122 top-level symbols)
- **Big helper clusters to extract next (use the tool):**
  - prompt-building (LARGE, ~5-6k lines, likely a nested sub-package `_prompt_building/`): `_summarize_issue` `_extract_plan_overview` `_build_role_context` `_build_role_restrictions_section` `_build_impasse_escape_hatch_section` `_render_contract_tasks` `_build_review_prompt` `_build_phase_prompt` `_contract_enforcer_role_names` `_build_brc_preamble` `_build_agent_roster` `_build_reviewer_preparation` `_re_review_priming_block` `_build_producer_orientation` `_build_file_boundary_section` `_build_agent_prompt`. Constants `_ROLE_DESCRIPTIONS`/`_PR_DESCRIPTION_*`/`_EXPLORATION_SUBAGENT_*` stay barrel-resident. `_build_agent_prompt`/`_build_phase_prompt` are patch targets.
  - contract-populate + decisions-ledger + gap-gate (LARGE): `_synthesize_plan_draft` `_slice_gate_block_monolithic_demotion` + classes `PlanDraftMissingOnLocalError` `PlanDraftMissingOnLocalAndOriginError` `PopulateOutcome` `PopulateProducedEmptyContractError` `PopulateResult` `SliceGateMonolithicBlock` + `_populate_*` `_empty_contract_*` `_plan_preflight_*` `_enforce_implement_start_plan_preflight` `_origin_has_plan_draft` `_auto_populate_contract_at_implement_start` `_merge_preserved_slice_runtime` `_sync_pipeline_decisions_to_contract` `_ledger_*` `_handle_explicit_none_attestation_gate` `_find_explicit_none_attestation` `_collect_decision_ledger_status` `_queue_and_await_contract_decisions` `_await_unresolved_gap_gate` `_next_phases_for_epic` `_drain_wontdo_batch_after_apply` `_write_apply_phase_handoff` `_persist_phase_gate_resolution`. (tool DOES move classes.)
  - stacked-PR reconciler: `_start_stacked_pr_reconciler` (~240L) + branch-divergence/timeout alert helpers still in barrel: `_check_brc_progress_gate` `_latest_active_role_heartbeat` `_unresolved_contract_hitl_ids` `_publish_consensus_timeout_alert` `_emit_producer_death_alert` `detect_branch_divergence` `_check_branch_divergence_for_alert` `_publish_branch_divergence_alert` `_branch_divergence_tick` `_handle_brc_consensus_timeout` (several are patch targets).
  - overseer-plane misc still in barrel around lines ~490-1900: host-wait tracking, status-wait cursor, `_spawn`/`_live_event_agents`/`_guard_live_pods_or_force`, `_emit_pipeline_event`, `make_error_response`/`make_success_response`, `_resolve_pipeline`, `_collect_all_pipelines`, `_pipeline_identifier`/`_brc_history_identifier`/`_ensure_pipeline_work_ref`/`_slice_namespace_root`. Group cohesively.
- **task-4-3 (non-negotiable #7): the THREE giant functions must be INTERNALLY SPLIT, not just moved** (each already exceeds the 1500 hard cap so a submodule holding one is still over cap):
  - `_run_pipeline` (~3,300L) → split into per-phase handlers `_run_refine_phase`/`_run_plan_phase`/`_run_implement_phase_slices`(exists)/`_run_pr_phase` + thin `_run_loop`. Preserve transition ordering EXACTLY. Dense seam coverage: test_consensus_polling, test_brc_nack_iteration, test_concurrent_*, test_advance_phase_*.
  - `_run_concurrent_phase` (~1,740L) and `_run_implement_phase_slices` (~1,680L) → internal split into their own submodule(s), recursive barrel if a cluster further-splits.
- **task-4-5/4-6 terminal**: drop the WIP `pipelines/__init__.py` allowlist entry (files: map EMPTY); add a concrete `routes/pipelines/` seam subsection to orchestrator/CLAUDE.md (mirror the slice-15 routes/signals table); verify all 4 CLAUDE.md seam tables current; DELETE `.egg-state/agent-outputs/coder/slice4_xtract.py` (scratch tool, not a deliverable) before the terminal propose. **Dockerfile: NO change needed** — `pipelines/` stays under the recursive `COPY orchestrator/routes/ ./routes/` (orchestrator/Dockerfile:45); confirmed by grounding.
- Then `make lint` + `make test-all` green, and `mcp__brc__propose`.

## Known NON-issues (do not chase)
- Sandbox env failures: `git init` returns non-zero ("not supported in the container") and gateway_client tests erroring on gateway git policy — identical class the recovered chain documented (~143 non-passing). NOT split-induced. They fail in test SETUP before pipelines code runs.
- My naive `python -c "import orchestrator.routes.pipelines"` fails on `agent_salvage` (parent-of-routes module needs `...` not `..`, but the try/except flat fallback resolves under `PYTHONPATH=orchestrator`, which is how the suite runs). Pre-existing from the pure-move; use `PYTHONPATH=orchestrator .venv/bin/python -c "import routes.pipelines"` for smoke checks.

## Open NACK responses
(none — not yet proposed; decomposition incomplete)
