# Analysis: Decompose 17 oversize Python source files to clear the file-size allowlist

> Issue: #2817 | Phase: refine

## Problem Statement

`make lint` caps Python source files at **1,500 lines / 100 KB** (the cap added by #2250, motivated by #2248 — ~25k tokens, the Read tool's soft limit). Seventeen files are grandfathered in `scripts/file-size-allowlist.yaml`; the lint ratchets non-allowlisted files but lets allowlisted entries grow unbounded. The desired outcome is an **empty `files:` map** in the allowlist, every file under the global cap, no behavior change, and no broken production importers or test patch targets.

This issue is a **refresh of #2261** — that tracker was closed after the pattern + worked reference landed in #2335 (2026-04-30) but no further slices merged. Files have grown in the interim; one new file (`orchestrator/routes/phases.py`) joined the allowlist via #2777 slice-1. The refresh inherits the pattern, the worked reference, and the seam-table scaffolding from #2335 — the planner should treat those as input, not rebuild them.

Because so many shape decisions are already locked in the issue body's "Non-negotiables" section and in the merged pattern doc (`docs/guides/decomposition-pattern.md`), the analysis below is short on alternatives and long on confirming current state. The interesting design work for this refresh is in the plan phase (slice-DAG shape, sub-stacking inside `pipelines.py`, sequencing under reviewer-bandwidth constraints), not here.

**Seams the planner will likely model as independent units** (advisory — slice/PR packaging is planner-phase):

- A pattern-refresh / docs-only update (replacing stale `#2261` references in `docs/guides/decomposition-pattern.md`, `scripts/file-size-allowlist.yaml` comments, and the CLAUDE.md seam tables with `#2817`; adding rows for `kubernetes_spawner.py` and `routes/phases.py`).
- Fourteen per-file decompositions (the 17 allowlisted files minus `plan_parser.py`, tracked under #2569, minus the two structural outliers below): the smaller files listed in "Verified file sizes" below, each one source → sub-package conversion + cluster carve-outs + allowlist entry drop + seam-table fill-in.
- Two structural-outlier files (`gateway/gateway.py`, `orchestrator/routes/pipelines.py`) which are 4-15× the cap and (in `pipelines.py`'s case) carry the ~2,937-line `_run_pipeline` per-phase refactor.

That seam list is descriptive of where the work lives; the planner is free to slice it however the slice-DAG, sequencing, and reviewer-bandwidth analysis recommends.

## Current Behavior

### Verified file sizes (HEAD on `egg/issue-2817/work`)

| File | Lines (HEAD) | Bytes (HEAD) | Lines (issue body) | Δ |
|---|---:|---:|---:|---|
| `orchestrator/routes/pipelines.py` | 24,559 | 1,131,966 | 24,559 | — |
| `gateway/gateway.py` | 10,690 | 417,679 | 10,690 | — |
| `sandbox/egg_lib/orch_cli.py` | 4,034 | 152,215 | 4,034 | — |
| `orchestrator/gateway_client.py` | 3,713 | 151,421 | 3,713 | — |
| `orchestrator/mcp_tools.py` | 2,820 | 122,478 | 2,820 | — |
| `orchestrator/routes/signals.py` | 2,557 | 101,393 | 2,557 | — |
| `shared/egg_contracts/checkpoint_cli.py` | 2,233 | 81,968 | 2,233 | — |
| `orchestrator/peer_consensus.py` | 2,215 | 96,901 | 2,215 | — |
| `sandbox/entrypoint.py` | 2,210 | 90,563 | 2,210 | — |
| `gateway/worktree_manager.py` | 2,087 | 83,788 | 2,087 | — |
| `gateway/git_client.py` | 2,068 | 69,084 | 2,068 | — |
| `orchestrator/overseer/monitor.py` | 2,024 | 84,829 | 2,024 | — |
| `orchestrator/kubernetes_spawner.py` | 1,873 | 82,651 | 1,873 | — |
| `shared/egg_contracts/plan_parser.py` | 1,835 | 70,233 | 1,835 | — (tracked under #2569) |
| `gateway/checkpoint_handler.py` | 1,777 | 69,606 | 1,777 | — |
| `orchestrator/routes/phases.py` | 1,654 | 71,459 | 1,654 | — |
| `orchestrator/routes/deployment.py` | 1,650 | 58,831 | 1,650 | — |

The issue body's numbers match HEAD exactly. The seventeen entries in `scripts/file-size-allowlist.yaml`'s `files:` map are the same seventeen files (verified by reading the allowlist).

### Pattern + worked reference already landed in #2335

- `docs/guides/decomposition-pattern.md` (318 lines) — canonical recipe: sub-package + `__init__.py` barrel with explicit per-symbol re-exports + underscore-prefixed private submodules + step-0 baseline commit (`git mv` to `<name>/__init__.py` before extracting clusters) + method-modules-on-class pattern for class-dominated files + routes-handling convention (decorators stay in `__init__.py`, wrappers delegate to submodules) + further-split-rather-than-allowlist + follow-up-issue convention for latent bugs.
- `scripts/select_tests/` — the worked reference: `__init__.py` (269 lines, barrel), `__main__.py` (34 lines), `_cli.py` (767 lines), `_constants.py` (198 lines), `_graph.py` (618 lines), `_io.py` (402 lines). All under the cap, already removed from the allowlist.
- `orchestrator/CLAUDE.md` and `gateway/CLAUDE.md` — seam tables with TBD placeholder rows for each in-flight decomposition. **Note:** the seam tables and the `pipelines.py` pre-allocated submodule layout reference `#2261` slice numbers (slice-14, slice-15). #2261 is closed and superseded by this issue, so those slice numbers are stale labels that the planner on #2817 may re-number.

### `_run_pipeline` size has grown

`_run_pipeline` (`orchestrator/routes/pipelines.py:20799`) is now **~2,937 lines** (lines 20799 → 23736), up from ~2,362 when #2261 was filed and ~2,300 cited in this issue's non-negotiable #7. The per-phase refactor target (extract `_run_refine_phase`, `_run_plan_phase`, `_run_implement_phase_slices`, `_run_pr_phase` + thin orchestration loop) is unchanged in shape — `_run_implement_phase_slices` already exists at line 15913 — but the cluster sizing is more aggressive than the seam-table sketch assumed. Mechanical extraction without per-phase decomposition still puts the new file over the cap on day one.

### Test patch surface (still load-bearing)

Counts reproducible with `grep -rhE 'patch\("(orchestrator\.)?routes\.pipelines\.[_a-zA-Z0-9]+' --include='*.py' .` and analogous patterns for `gateway.gateway.` and `mcp_tools.`:

- **49 distinct symbols** patched at `(orchestrator.)?routes.pipelines.<sym>` after collapsing `routes.pipelines.X` and `orchestrator.routes.pipelines.X` onto the suffix; **51 distinct full patch-target strings** when both prefix forms are counted separately. The barrel re-export is the contract that keeps both shapes working.
- **6 distinct full patch-target strings** at `(gateway\.)?gateway.gateway.<sym>` (smaller surface than `pipelines.py` but the gateway file is 10,690 lines and has its own concentration of Flask `@app.route(...)` registrations).
- `mcp_tools` tests do not currently `patch("…mcp_tools.<sym>")` — they construct handlers directly (zero matches). The patch-target invariant from the issue body and the pattern doc still applies (class identity stays in `__init__.py`, helpers move to `_dispatch.py` / `_status.py` / etc.).

### Production-importer surface

Counts reproducible with `grep -rEln '^[[:space:]]*from[[:space:]]+((orchestrator\.)?routes\.pipelines|\.\.routes\.pipelines)[[:space:]]+import' --include='*.py' .` and variants:

- **87 files** (production + tests) import from `routes.pipelines` / `orchestrator.routes.pipelines` / relative `..routes.pipelines`.
- **9 non-test source files** under `orchestrator/`, `gateway/`, `sandbox/`, `shared/`, `scripts/`, `integration_tests/` import from `routes.pipelines` (i.e. production importers, exclusive of `tests/` and `test_*.py`).
- **447 total import statements** across the 87 files.
- **95 unique `from … import …` import-line strings** across all files (collapsing duplicates within and across files; ~94 of these reach a non-test file at least once).

Many use the dual `try: from ..foo / except ImportError: from foo` shape mandated by non-negotiable #4. Symbols include `_read_phase_draft`, `_pipeline_identifier`, `_run_pipeline`, `WORKTREE_BASE_DIR`, `PipelinePhase`, `PopulateOutcome`, `_populate_contract_from_plan`, `_resolve_slice_base_branch`, `_build_agent_prompt`, etc. — far broader than the four importers the issue body explicitly names. The pattern doc's section (d) "External-importer audit recipe" applies and must run per slice.
- `routes/phases.py` does **in-function lazy imports** of `routes.pipelines` symbols to break a cycle (the issue body and prior analysis flag this; verified by spot-grepping `from routes.pipelines import` inside `routes/phases.py`). After `pipelines.py` becomes a sub-package, those lazy imports continue to resolve through the barrel — the cycle-breaking mechanism is preserved.
- `gateway/gateway.py`'s production importers (e.g. `from gateway.gateway import get_anthropic_client`, `get_credentials_manager`, app factory hooks) are smaller in number; the routes-handling convention in the pattern doc (section (f), HITL decision-8 of #2261) keeps `@app.route(...)` decorators in `__init__.py`, so route registration order, error-handler scoping, and `before_request` semantics do not change.

### CLAUDE.md / seam-table state

- `orchestrator/CLAUDE.md` has the `routes/pipelines/` seam table with TBD rows pre-allocated into ten clusters (`_run_loop/`, `_concurrent_phase/`, `_prompt_building/`, `_pr_lifecycle/`, `_worktree_ops/`, `_criteria.py`, `_readers.py`, `_decisions.py`, `_status_helpers.py`, `_brc_verdicts.py`). The "Other in-flight decompositions" sub-table lists six other files (mcp_tools, gateway_client, overseer/monitor, peer_consensus, routes/signals, routes/deployment) with their **#2261** slice numbers. **No row exists for `orchestrator/kubernetes_spawner.py` or `orchestrator/routes/phases.py`.** Non-negotiable #6 calls out adding rows for both.
- `gateway/CLAUDE.md` has the `gateway/gateway/` seam table with five clusters (`_git_routes/`, `_jira_routes/`, `_auth.py`, `_sessions.py`, `_app_factory.py`) and a sub-table for three other files (worktree_manager, git_client, checkpoint_handler) with **#2261** slice numbers.
- `sandbox/CLAUDE.md` has **no submodule seam table at all**, despite the issue putting `sandbox/entrypoint.py` and `sandbox/egg_lib/orch_cli.py` in scope.
- There is **no `shared/CLAUDE.md`**, despite `shared/egg_contracts/checkpoint_cli.py` being in scope. (`plan_parser.py` is tracked under #2569 separately.)
- The allowlist comment block for `plan_parser.py` reads `issue: "2548"` (the slice that breached the cap, not the decomposition tracker). The comment for `orchestrator/routes/phases.py` correctly references "slice-15 cluster in #2261" but #2261 is closed; the entry is now under #2817.

### Runtime-primitive surface for the downstream plan

Primitives the plan phase will rely on (each is a concrete object the planner can audit before locking implementation steps):

- **Lint primitives** (run on every PR; trusted-CI-runner context):
  - `scripts/check-file-sizes.py` — the size lint, reads `scripts/file-size-allowlist.yaml`.
  - `scripts/file-size-allowlist.yaml` — schema documented inline; `files:` map is the closing target.
  - `make lint` target — already pipes `check-file-sizes.py` through pre-commit.
  - `make test-all` — full suite gate at every slice boundary.
- **Symbol-import primitives** (in-sandbox-agent + production runtime):
  - `from routes.pipelines import _foo` / `from orchestrator.routes.pipelines import _foo` (dual shape) — the barrel is the stable API.
  - `unittest.mock.patch("routes.pipelines._foo", ...)` — 51 distinct targets must keep resolving.
  - Flask `@app.route(...)` (`gateway/gateway.py`) and `@<blueprint>.route(...)` (`orchestrator/routes/*.py`) — decorator-based route registration; decorators stay in `__init__.py` (pattern doc §(f)).
  - `from sandbox.egg_lib import orch_cli` and the `egg-orch` console-script entry point that resolves through it — `sandbox/egg_lib/orch_cli.py` becoming `orch_cli/__init__.py` preserves both.
- **Docs primitives** (human-operator + agent context):
  - `docs/guides/decomposition-pattern.md` — canonical recipe; references #2261 as the parent issue (currently stale — see open question Q1).
  - `orchestrator/CLAUDE.md` § "Submodule seam tables" + sub-tables — references #2261 slice numbers (stale).
  - `gateway/CLAUDE.md` § "Submodule seam tables" + sub-tables — references #2261.
  - `sandbox/CLAUDE.md` — no seam table; adding one is in scope of open question Q2.
- **CI primitives** (trusted-CI-runner): `pre-commit`'s `check-file-sizes` hook; GitHub Actions `lint.yml` and `test.yml` workflows.

These primitives all exist today; the plan phase's Primitive-Existence audit should be cheap.

## Constraints

### Issue-body non-negotiables (verbatim, already operator-approved)

1. **Follow the established pattern in `docs/guides/decomposition-pattern.md`** (sub-package + `__init__.py` barrel with explicit per-symbol re-exports + underscore-prefixed private submodules). Don't invent a new layout.
2. **Test back-compat via re-exports.** Every `patch("routes.pipelines._foo")` / `patch("gateway.gateway._bar")` / `patch("orchestrator.mcp_tools._baz")` target keeps resolving from the barrel. (Verified: 51 + 6 + 0 distinct targets today.)
3. **The barrel is the stable public API** (#2261 HITL decision-7). Production importers (`mcp_tools.py` → `_read_phase_draft` / `_pipeline_identifier`; `unified_sse.py` → `_collect_all_pipelines`; `routes/phases.py` lazy in-function imports) keep working.
4. **Dual import pattern** (`try: from ..foo import X / except ImportError: from foo import X`) — new submodules either all use it or all use relative imports; no mixing inside one sub-package.
5. **Allowlist ratchet.** Each PR drops its file's entry from `scripts/file-size-allowlist.yaml`. Lint enforces; don't bypass.
6. **Seam-table updates.** Each landing replaces its TBD row in `orchestrator/CLAUDE.md` or `gateway/CLAUDE.md` with the concrete submodule layout. `kubernetes_spawner.py` and `routes/phases.py` do not yet have rows; add when each decomposition lands.
7. **`_run_pipeline` is in scope.** Per-phase extraction (`_run_refine_phase`, `_run_plan_phase`, `_run_implement_phase_slices`, `_run_pr_phase` + thin orchestration loop), per the seam-table TBDs. Mechanical extraction is insufficient; a permanent allowlist exception is off the table. Per-phase unit-test follow-up tracked by #2319.
8. **Gateway routes.** `gateway.py`'s Flask routes register via `@app.route(...)` decorators on thin wrappers in `__init__.py`; submodules hold implementation bodies (#2261 HITL decision-8). Don't move the decorators.
9. **Branch prefix `egg/`.**
10. **Tests decompose alongside source, where the mapping is natural** (now relaxed — guidance, not a hard gate). Scenario-organized suites may stay topical. `select_tests` reference suite is **not** required to be retrofitted to 1:1. Test files keep using the barrel surface so they don't move when source internal layout shifts.

### Inferred / derived constraints

- **Behavior preservation.** Pure refactor; latent bugs filed separately (`docs/guides/decomposition-pattern.md` §(h)).
- **`make lint` and `make test-all` green at every PR boundary.** Each slice's PR passes independently.
- **Step-0 `git mv` commit per file.** `docs/guides/decomposition-pattern.md` §(b) requires the file→package conversion (`git mv foo.py foo/__init__.py`) to land as its own bisectable commit before any cluster carve-out. Each decomposition slice carries at least one `git mv` commit.
- **No new allowlist entries.** Pattern doc §(g): if a submodule lands at or over the cap, further-split it inside the same slice rather than allowlisting. HITL escalation is the only escape hatch.
- **`shared/egg_contracts/plan_parser.py` is tracked under #2569.** Listed in this issue's table for honesty; its decomposition does **not** land under this issue's PRs. The allowlist entry must be gone for this issue to close (acceptance criterion). See open question Q3.
- **File-boundary gateway enforcement (this phase only).** Refiner can only push to `.egg-state/drafts/` and `.egg-state/agent-outputs/`. Source-file moves happen in implement.

### Dependencies on other issues

- **#2335** — pattern + worked reference. **Merged.** Input, not work.
- **#2569** — `plan_parser.py` decomposition. **Separate tracker, in-flight.** This issue closes only after #2569 lands (its allowlist entry must be gone) or after the operator decouples the closing criterion (Q3).
- **#2319** — unit tests for per-phase handlers extracted from `_run_pipeline`. Follow-up that lands alongside the `pipelines.py` decomposition (or just after). The seam-table TBDs reference it.
- **#2562** — restore coder/reviewer access to full configured check suite once this issue lands. Downstream consumer; not in scope.

## Options Considered

Most decisions are locked by the issue body and the merged pattern doc. The two genuinely open dimensions surfaced below are **scope** decisions (whether the program touches the surface at all) — packaging of the work into slices, if it does, is the planner's call. Two specific packaging options are sketched below the (Y) section as **planner-owned context** only.

### (X) Updating stale `#2261` references — pure scope question

The pattern doc, both CLAUDE.md seam tables, and several allowlist-yaml comments still reference `#2261`, which is closed and superseded by this issue. The operator-only decision is **whether the program absorbs that doc-hygiene churn at all**, registered as `cq-6` (which supersedes `cq-1` — see the Open Questions section for the deprecation note). Two options:

- **Update them as part of this work.** Pattern doc, CLAUDE.md seam tables, and allowlist-yaml comments cite `#2817` once a slice that touches each surface lands. Adds missing rows for `kubernetes_spawner.py` and `routes/phases.py` as TBD placeholders.
- **Leave them as historical pointers.** `#2261` is reachable through the GitHub supersession chain; the program does not absorb the churn.

How updates get packaged across slices — separate docs-only slice up front vs inline within each file decomposition vs hybrid — is **explicitly a planner-phase decision** (slice-DAG shape / sequencing). Refine does not pre-commit a packaging shape, and the planner is free to evaluate the two packaging shapes below as informational context, not as operator-approved options.

### (Y) Pattern-doc updates for new in-scope wrinkles

The merged pattern doc is locked but two wrinkles in this refresh aren't directly addressed:

**Option Y1: Update the pattern doc to reflect #2817's scope changes.** Add a short subsection for the relaxed non-negotiable #10 (test 1:1 retrofit not required for scenario-organized suites; `select_tests` reference is intentional precedent). Update the "Pre-merge checklist" to point at `sandbox/CLAUDE.md` and (if needed) `shared/CLAUDE.md` for non-orchestrator / non-gateway slices.

**Pros**: Pattern doc stays the canonical recipe; agents loading it don't have to also cross-reference the issue body.

**Cons**: Pattern-doc edits during a decomposition program are themselves a behavior-adjacent change; if the operator wants the doc to stay pinned to the #2335 shape, leave the issue body as the corrective.

**Option Y2: Leave pattern doc unchanged; treat issue body as the corrective.** The pattern doc reflects the #2261 era; this issue body's non-negotiables override where they differ (relaxed #10, expanded file list).

**Pros**: One source of truth per program iteration; no pattern-doc churn.

**Cons**: Agents who load only the pattern doc (e.g. for a future, unrelated decomposition) won't see the relaxation. The pattern then accumulates corrections-by-reference rather than direct edits.

### Packaging context for the planner (advisory)

If the operator chooses to update stale `#2261` references (cq-6, option 1), the planner has two natural packaging shapes; both are informational here and either is acceptable from refine's perspective:

- *Docs-only slice up front* — one coherent docs-state at the top of the program; reviewers read consistent references on slice-2 onward; costs an extra slice that drops no allowlist entries.
- *Inlined per slice* — no extra slice; stale references linger until late slices; risk of inconsistency across which slices touched which references.

The planner is the authority on which shape (or a hybrid) is chosen. Refine does not recommend or pre-commit either.

## Recommended Approach

Use the established pattern from #2335 (no alternatives) and apply it across all 17 files. The two open dimensions above are both legitimately the operator's call, not the refiner's — they're registered as open questions below.

Recommended posture for the planner to inherit:

1. **The plan phase owns slice-DAG shape, sequencing, sub-stacking inside `pipelines.py`, and concurrency budget.** Refine does not pre-commit to "one slice per file vs. cohorts vs. sub-stack" — that was #2261's analysis and #2817's planner should re-derive it from current file sizes and the operator's chosen closing criterion (`cq-3`).
2. **`docs/guides/decomposition-pattern.md` is input, not work.** Each slice copies the recipe. Whether to also update the pattern doc for the relaxed non-negotiable #10 is operator-owned (`cq-4`). Whether to refresh stale `#2261` references in the pattern doc / CLAUDE.md seam tables / allowlist comments is operator-owned (`cq-6`, supersedes `cq-1`); how that update gets packaged across slices is planner-owned.
3. **`_run_pipeline` is in scope as a per-phase refactor.** The seam-table TBDs already sketch the shape (`_run_loop/_run_refine.py`, `_run_plan.py`, `_run_implement.py`, `_run_pr.py` + thin orchestration loop). Function has grown to ~2,937 lines, but the strategy is unchanged.
4. **Production-importer audits and test-patch-surface audits run per slice** using `docs/guides/decomposition-pattern.md` §(d)'s `git grep` recipe. Every external-referenced symbol gets re-exported.
5. **Sandbox / shared seam-table scaffolding is in scope contingent on `cq-2`.** If the operator answers "yes, add a seam table to `sandbox/CLAUDE.md` and create `shared/CLAUDE.md`," that work folds into the relevant decomposition slices. If "no, only orchestrator and gateway have seam tables," the `sandbox/entrypoint.py` / `sandbox/egg_lib/orch_cli.py` / `shared/egg_contracts/checkpoint_cli.py` slices land without seam-table rows (acceptable per non-negotiable #6's literal wording, which only names the two CLAUDE.md files that already exist).

## Open Questions

The non-negotiables in the issue body resolve everything #2261's refine asked about layout pattern, re-export style, submodule naming, `_run_pipeline` strategy, gateway route handling, and test-decomposition. Slice DAG shape, sequencing, sub-stacking inside `pipelines.py`, and concurrency budget are explicitly out of scope for refine (planner-owned).

Five questions remain that the operator must answer because they change the closing criterion, the seam-table surface, or document references that the planner will commit to in slice text.

### Registered HITL decisions

(No pre-refine HITL round on this pipeline — the contract has zero prior decisions. Six decision points are registered below; `cq-1` is **superseded by `cq-6`** in response to reviewer_refine v1 NACK feedback. Operators should answer `cq-6` and ignore `cq-1`.)


<!-- egg-hitl-decision id=cq-6 -->

**[SUPERSEDES cq-1] Should stale #2261 references in the pattern doc, CLAUDE.md seam tables, and allowlist comments be updated as part of #2817's work, or left as historical pointers?**

- [ ] Yes, update them as part of this work (planner picks slice packaging — separate docs-only slice, inlined per slice, or some hybrid)
- [ ] No, leave as historical pointers; #2261 is reachable through the supersession chain and the program does not need to absorb the doc-hygiene churn
- [ ] Other (explain in reply)

#### Superseded

<!-- egg-hitl-decision id=cq-1 -->

**~~Stale #2261 references in pattern doc, CLAUDE.md seam tables, and allowlist comments: how should the program handle them?~~** *(Superseded by `cq-6`. The original wording bundled a planner-owned slice-packaging trade-off — separate docs-only slice vs inline — into the operator's scope decision. The reframed `cq-6` above asks only the scope question; slice packaging belongs to the plan phase.)*

- [ ] ~~Mass refresh in a docs-only slice up front~~
- [ ] ~~Inline as each slice lands~~
- [ ] ~~Leave stale references as historical pointers~~
- [ ] ~~Other (explain in reply)~~

<!-- egg-hitl-decision id=cq-2 -->

**Sandbox/shared seam-table scaffolding: should the program add seam tables for sandbox/ and shared/ files in scope?**

- [ ] Add a Submodule seam table to sandbox/CLAUDE.md and create shared/CLAUDE.md with a seam table; sandbox/entrypoint.py, sandbox/egg_lib/orch_cli.py, and shared/egg_contracts/checkpoint_cli.py each get a seam-table row when they decompose (mirrors orchestrator/CLAUDE.md and gateway/CLAUDE.md)
- [ ] Add a seam table to sandbox/CLAUDE.md only; leave shared/ undocumented (checkpoint_cli.py decomposes without a seam-table row)
- [ ] Leave sandbox/CLAUDE.md unchanged and do not create shared/CLAUDE.md (non-negotiable #6 names only orchestrator/ and gateway/; sandbox/ and shared/ decompositions land without seam-table documentation)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-3 -->

**Closing criterion vs #2569 (plan_parser.py decomposition tracker): when does #2817 close?**

- [ ] #2817 stays open until both #2569 and the other 16 file decompositions land (this issue blocks on #2569; closes only when the allowlist files: map is empty AND #2569 has merged)
- [ ] #2817 can close once the 16 non-#2569 files are decomposed, provided #2569 has also landed by then; if not, #2817 stays open until #2569 lands (operationally equivalent to option 1)
- [ ] Decouple: #2817 closes when the 16 non-#2569 files are decomposed, even if plan_parser.py's allowlist entry remains. The remaining entry is removed by #2569 separately, and #2562 can begin once the 16 land regardless of #2569 state
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-4 -->

**Pattern-doc updates for relaxed non-negotiable #10 (test 1:1 retrofit is guidance, not a gate): should docs/guides/decomposition-pattern.md be updated?**

- [ ] Update the pattern doc to reflect the relaxation (add a short subsection explaining that scenario-organized test suites may stay topical and that the select_tests reference is intentionally not retrofit; update the Pre-merge checklist accordingly)
- [ ] Leave the pattern doc unchanged; treat the issue body as the corrective (downstream agents loading the pattern doc will need to cross-reference #2817's issue text to see the relaxation)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-5 -->

**Allowlist comment correctness (plan_parser.py says issue: 2548, phases.py cites slice-15 cluster in #2261): should these be corrected as part of #2817's work?**

> Surface covered by each option includes both the **structured `issue:` field** in `scripts/file-size-allowlist.yaml` and the surrounding **free-text comment block** for the affected entry. For `phases.py`, that's the `issue: "2261"` field and the four `#2261` references in the comment immediately above it. For `plan_parser.py`, that's the `issue: "2548"` field and the comment block referencing #2548 / #2569.

- [ ] Yes, correct both: update plan_parser.py's `issue:` field and comment to reference #2569 (the decomposition tracker, not the slice that breached the cap) and update phases.py's `issue:` field and comment to reference #2817 (the current tracker; #2261 is closed/superseded). Lands in whichever slice touches the allowlist first
- [ ] Yes, correct phases.py only (`issue:` field + comment cite #2817 in place of #2261). Leave plan_parser.py's `issue: "2548"` field alone since #2569 is a separate tracker and the comment is currently honest about the breach origin
- [ ] No, leave both comments and `issue:` fields as-is. They are historical pointers and the operator can follow the supersession chain; correcting them is doc-hygiene churn the decomposition program does not need to absorb
- [ ] Other (explain in reply)

## Complexity Assessment

**high** — 16-17 files (depending on `cq-3`), six over the cap by 1×-2×, four over by 2×-3×, two structural outliers (`gateway/gateway.py` at ~7× the cap; `orchestrator/routes/pipelines.py` at ~16× the cap and containing the ~2,937-line `_run_pipeline` state machine). 49 distinct symbols (51 distinct full strings) patched at `routes.pipelines.*` and 6 distinct full strings at `gateway.gateway.*` must remain stable across moves, and 95 unique `from routes.pipelines import …` import-line strings across 87 files must continue resolving through the barrel. The pattern is locked (lowering risk) but the slice-DAG construction, the per-phase refactor of `_run_pipeline`, and the rebase/conflict choreography across parallel slices in `scripts/file-size-allowlist.yaml` each warrant focused plan-phase design work. The refresh inherits a six-cycle history (issue-2261-v3..v10) of cancelled / failed pipelines; the planner should treat that as a signal to stay conservative on slice size and concurrency.

---

*Authored-by: egg*
