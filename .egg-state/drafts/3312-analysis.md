# Refine analysis — issue #3312 (decompose 19 oversize Python files; empty the allowlist)

Refiner grounding pass, verified against the live worktree on 2026-06-26
(HEAD `46f74f8b7`, parent of the table baseline `c296965d4`). This is a
**refresh of #3111 → #2817 → #2261**; the pattern + worked reference landed
in PR #2335 (merged), but **no decomposition slice has ever landed**.

## Scope verdict — LOCKED, no descope, no HITL

The operator directive is explicit and binding: **all 19 files are in scope,
including the two structural outliers** (`orchestrator/routes/pipelines.py`,
`gateway/gateway.py`) and `_run_pipeline` directly (non-negotiable #7). The
acceptance criterion is an **empty `files:` map**. Proposing a reduced-scope
plan that defers `pipelines.py`/`gateway.py` or decomposes only a subset is
OFF the table.

Because scope is fully author-specified and the operator forbids descope,
**I am registering no HITL clarifying question.** There is no genuine open
decision for the operator at refine time; slice DAG / ordering / PR packaging
are explicitly planner-phase decisions (issue "Out of scope"), not refine
pre-commitments. The refiner's value here is grounding the facts and
correcting the one stale claim below — not re-litigating locked scope.

## The 19 files — live sizes (verified 2026-06-26, this worktree)

Counts have drifted slightly UP from the issue table (the lint ratchets
non-allowlisted files but lets allowlisted ones grow). **Live counts are
authoritative; the issue table is a snapshot.** Lines / bytes:

| File | Lines | Bytes | Over byte cap? |
|---|---:|---:|:--:|
| `orchestrator/routes/pipelines.py` | 27,211 | 1,268,310 | yes |
| `gateway/gateway.py` | 10,385 | 407,978 | yes |
| `sandbox/egg_lib/orch_cli.py` | 5,012 | 190,656 | yes |
| `orchestrator/gateway_client.py` | 4,326 | 183,370 | yes |
| `orchestrator/routes/signals.py` | 3,398 | 142,839 | yes |
| `orchestrator/kubernetes_spawner.py` | 3,041 | 139,022 | yes |
| `orchestrator/mcp_tools.py` | 2,948 | 130,445 | yes |
| `gateway/worktree_manager.py` | 2,507 | 106,355 | yes |
| `gateway/git_client.py` | 2,393 | 85,780 | no |
| `orchestrator/peer_consensus.py` | 2,326 | 102,933 | yes |
| `sandbox/entrypoint.py` | 2,212 | 90,732 | no |
| `orchestrator/overseer/monitor.py` | 2,130 | 90,364 | no |
| `shared/egg_contracts/plan_parser.py` | 1,952 | 75,777 | no |
| `orchestrator/routes/event_prompt.py` | 1,895 | 89,158 | no |
| `orchestrator/routes/deployment.py` | 1,854 | 69,714 | no |
| `orchestrator/routes/phases.py` | 1,736 | 75,190 | no |
| `orchestrator/state_store.py` | 1,635 | 68,623 | no |
| `orchestrator/routes/decisions.py` | 1,562 | 58,930 | no |
| `sandbox/egg_lib/contract_cli.py` | 1,501 | 50,650 | no |

Confirmed: 19 files, 9 over the byte cap (same 9 the issue names). All 19
have live `files:` entries in `scripts/file-size-allowlist.yaml`; the
`caps:` block is `hard_lines: 1500 / hard_bytes: 100000`. Provenance
`issue:` fields are mixed (2248, 2548, 2261, 3033, 3124, 3231) — a note,
not a tracker; drop each entry as its file lands.

## Pattern + reference inputs — verified present

- `docs/guides/decomposition-pattern.md` — present (13,292 bytes). Canonical
  recipe: sub-package + explicit per-symbol re-export barrel (decision-5) +
  underscore-prefixed private submodules (decision-6); step-0 `git mv` to
  `__init__.py` as its own bisectable baseline commit; method-modules-on-class
  pattern for class-dominated files (mcp_tools `PipelineToolHandler`,
  gateway_client `GatewayClient`); routes convention — decorators stay in
  `__init__.py`, bodies delegate to submodules (decision-8); external-importer
  `grep` audit before extracting; further-split in-slice rather than a fresh
  allowlist entry (section g); follow-up-issue convention for bugs (section h).
- `scripts/select_tests/` — present worked reference: `__init__.py` barrel +
  `__main__.py` + `_cli.py` / `_constants.py` / `_graph.py` / `_io.py`. Already
  absent from the allowlist (confirmed). **Copy this exact shape.**
- PR #2335 is the merged template — planner/coder must read its diff first.

## GROUNDING CORRECTION — stale claim in the issue (flag for planner)

The issue (Status section + acceptance criteria + non-negotiable #6) asserts
the CLAUDE.md seam tables "still carry stale `#2261` slice tags and line
counts" — e.g. `gateway/gateway/ — TBD (#2261 slice-14)`,
`routes/pipelines/ — TBD (#2261 slice-15)`. **This is NOT true of the live
tree.** Verified:

- `orchestrator/CLAUDE.md` and `gateway/CLAUDE.md` contain **no `#2261`
  references and no TBD/placeholder decomposition rows.** Each has only a
  component-overview table plus one generic paragraph ("When a module outgrows
  the 1,500-line / 100 KB cap… decompose it following the canonical
  pattern…"). The `#2261` refs that exist live in `docs/` (overseer.md,
  decomposition-pattern.md, post-agent-commit.md,
  pipeline-health-monitoring.md), not in the seam tables.
- `sandbox/CLAUDE.md` exists but has no seam table; `shared/CLAUDE.md` is
  ABSENT (confirmed).

**Implication for the planner (no scope change):** the seam-table acceptance
criterion is "create concrete submodule layouts," not "retag existing #2261
rows" — there are no such rows to retag in those two files. The work to ADD
seam rows per landing, ADD a sandbox/ seam table, and CREATE `shared/CLAUDE.md`
all stands exactly as the issue intends. If the planner wants the literal
"retag #2261" wording honored, the closest live targets are the `docs/` files
above — but that is decomposition-program bookkeeping, optional, not an
acceptance gate. The binding criterion is: concrete seam coverage exists for
every in-scope file at landing.

## Back-compat surface — re-derived ground truth (load-bearing)

Non-negotiable #2/#3: the barrel is the stable API; test `patch(...)` targets
must keep resolving. Re-derived now (don't trust stale counts):

- **`routes.pipelines.*`**: ~**57 distinct** patch targets across ~**137**
  test files reference `routes.pipelines` / `routes/pipelines`. This is the
  dominant back-compat surface; every extracted symbol with an external
  reference must be re-exported through `pipelines/__init__.py`.
- **`gateway.gateway.*`**: ~**12 distinct** patch targets.

These are refine-time estimates to size the risk; the coder re-runs the
section-(d) audit per cluster at implement time (counts shift as main drifts).

## Hardest piece — `_run_pipeline` (non-negotiable #7, binding)

`pipelines.py` (27k lines) contains the `_run_pipeline` phase-transition state
machine. Mechanical extraction won't work and punting to a follow-up is OFF
the table. The pattern doc's prior design splits it into per-phase handlers +
a thin orchestration loop (the `_run_loop.py` / per-cluster shape). The planner
owns the cluster decomposition of this file; the refine constraint is only
that it MUST be addressed directly in this program, not deferred.

## Acceptance criteria (restated for the contract)

1. All 19 files drop below the global cap (≤1,500 lines AND ≤100 KB).
2. `scripts/file-size-allowlist.yaml`'s `files:` map is **empty**.
3. `make lint` + `make test-all` green at **every landing boundary**.
4. No production importer breaks; no test patch target moves out from under an
   existing test without an in-PR rewrite (barrel re-exports preserve them).
5. Concrete submodule seam coverage exists for every in-scope file:
   rows/tables in `orchestrator/CLAUDE.md` + `gateway/CLAUDE.md` (creating
   decomposition seam tables; retag any stale `#2261` refs found while editing,
   per correction above), a seam table added to `sandbox/CLAUDE.md`, and a new
   `shared/CLAUDE.md` covering `plan_parser.py`.
6. Test layout follows non-negotiable #10 (guidance, not a hard 1:1 gate):
   tests keep using the barrel surface so they don't move when internal layout
   shifts; matching `tests/.../foo/` sub-packages where the mapping is clean;
   scenario-organized suites may stay topical.
7. Pure refactor — no behavior changes. Bugs surfaced get a separate follow-up
   issue (`Part of #3312`), never bundled. Branches prefixed `egg/`.

## Explicitly OUT of refine scope (planner owns)

Slice DAG, decomposition ordering, and PR packaging — including the ordering
heuristic the issue recommends (front-load independent per-file slices so they
land value even if `pipelines.py`/`gateway.py` stall again). The refiner does
NOT pre-commit slice boundaries.

## Decision log

- 2026-06-26: grounded issue #3312 (live body via `gh issue view 3312`)
  against the worktree; confirmed all 19 files + sizes + allowlist entries +
  pattern/reference inputs; re-derived patch-target surface (57 pipelines / 12
  gateway); **corrected the stale seam-table `#2261` claim** (no such rows in
  the two CLAUDE.md files); confirmed scope is locked → no HITL; wrote v1.


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

Operator approval: scope confirmed as all 19 files including pipelines.py (~27k) and gateway.py (~10k), with _run_pipeline tackled head-on. No descoping. Proceed to plan phase; the planner owns ordering/packaging only, not scope reduction.
