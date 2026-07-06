# Plan: decompose the 4 remaining oversize files; empty the file-size allowlist (#3312, #3447, #3450)

> Continuation of #3312 (17/19 slices landed via merged PR #3336), folding in the two
> files that crossed the cap since: `orchestrator/models.py` (#3450) and
> `orchestrator/event_loop.py` (#3447). Scope: all 4 remaining allowlist entries,
> including the two structural outliers (`gateway.py`, `routes/pipelines.py`);
> `_run_pipeline` split head-on (issue #3312 non-negotiable #7); **no descope**
> (the #3312 scope lock and the prior run's approved refine/plan gates carry over).

## Approach

4 per-file decomposition slices (one slice = one file = one PR), ordered easiest to
hardest, serialized into **one linear dependency chain** (slice-1 -> slice-2 ->
slice-3 -> slice-4) because every slice edits the shared
`scripts/file-size-allowlist.yaml` (#3046 forbids unordered overlap on a shared file).

Each slice follows the canonical recipe proven on 17 files by PR #3336
(`docs/guides/decomposition-pattern.md`, worked references in-tree:
`orchestrator/state_store/`, `peer_consensus/`, `mcp_tools/`, `kubernetes_spawner/`,
`gateway_client/`, `gateway/git_client/`, `gateway/worktree_manager/`, ...):
external-importer audit -> step-0 `git mv` baseline commit -> cluster extraction with
an explicit per-symbol re-export barrel + underscore-prefixed private submodules ->
allowlist drop + concrete CLAUDE.md seam row -> R3 container-COPY parity (where
applicable) -> `make lint` + `make test-all` green.

## Grounding (verified live 2026-07-03 on main @ 36ecaa91d)

- `orchestrator/models.py`: 1,521 lines / 68 KB. Pydantic models + enums
  (`PipelineConfig` L570, `Pipeline` L1146, `PhaseExecution` L441, `AgentExecution`
  L213, `RepoSpec` L1120, status enums, helpers `resolve_consensus_timeout_minutes`,
  `resolve_slice_repo`). ~179 importer files: the widest surface in the repo.
- `orchestrator/event_loop.py`: 1,622 lines / 81 KB. Class-dominated: `EventDecision`
  (L331), `JobSupervisor` (L353), `OrchestratorEventLoop` (L990). ~6 importer files.
- `gateway/gateway.py`: 10,648 lines / 419 KB. `app = Flask(__name__)` at L486;
  `app.run(...)` at L10639 under `__main__` (L10647); started via
  `gateway/entrypoint.sh` on hardcoded port 9848. ~35 referencing files.
- `orchestrator/routes/pipelines.py`: 30,520 lines / 1.44 MB. Blueprint routes +
  `_run_pipeline` state machine. ~137 referencing files (dominant patch-target
  surface).
- Dockerfile globs unchanged: `orchestrator/Dockerfile:44` (`COPY orchestrator/*.py`)
  and `gateway/Dockerfile:67` (`COPY gateway/*.py`) are non-recursive; the prior run's
  explicit per-package COPY lines are in-file precedent. `routes/pipelines.py` ships
  under the recursive `COPY orchestrator/routes/` (L45): no Dockerfile change.
- Seam tables exist in `orchestrator/CLAUDE.md`, `gateway/CLAUDE.md`,
  `sandbox/CLAUDE.md`, `shared/CLAUDE.md`; slices append/extend rows.

## Acceptance criteria

- AC-1: all 4 files drop below the global cap (<=1,500 lines AND <=100 KB), and every
  extracted submodule is itself under BOTH caps (further-split in-slice, never a fresh
  allowlist entry).
- AC-2: `scripts/file-size-allowlist.yaml`'s `files:` map is EMPTY after the final
  slice lands (terminal criterion of the #3312 program).
- AC-3: `make lint` + `make test-all` green at EVERY slice boundary. These gates run
  on the source tree, not the built image, so R3 slices carry same-slice Dockerfile
  COPY updates + an image-build/import smoke check (AC-6).
- AC-4: no production importer breaks; no test patch target moves out from under an
  existing test without an in-PR mechanical rewrite; barrel re-exports preserve
  `patch('<module>._foo')` targets (models.py ~179 importer files and
  routes.pipelines ~137 files re-verified at implement time).
- AC-5: concrete seam rows for every decomposed file in the matching CLAUDE.md
  (`orchestrator/CLAUDE.md` for models/event_loop/pipelines, `gateway/CLAUDE.md` for
  gateway.py).
- AC-6: no container-image regression: slices 1/2 add
  `COPY orchestrator/<pkg>/ ./<pkg>/` to orchestrator/Dockerfile; slice 3 adds
  `COPY gateway/gateway/ ./gateway/` AND preserves the Flask launch (barrel-exported
  `app`, entrypoint.sh serving on 9848), each verified by an image build + smoke
  check in the same slice. Slice 4 confirms recursive-COPY shipping (no change).
- AC-7: pure refactor, no behavior changes. `_run_pipeline` is split directly into
  per-phase handlers + a thin loop (non-negotiable #7), not deferred. Bugs surfaced
  get separate follow-up issues ('Part of #3312' / '#3447' / '#3450'), never bundled.
  Branches prefixed `egg/`.
- AC-8: closing bookkeeping: the slice that empties the allowlist verifies all four
  CLAUDE.md seam tables are current; issues #3447 and #3450 are closable by their
  slices, #3312 by the final slice.

## Non-goals

- No behavior changes (pure refactor); surfaced bugs are separate follow-ups.
- No decomposition of test files (lint-exempt), non-Python files, or files outside
  the 4-file set.
- No new layout invented: the canonical sub-package + re-export-barrel pattern
  (docs/guides/decomposition-pattern.md, PR #2335, PR #3336) is followed exactly.

## Risks carried to reviewers

- models.py barrel completeness is the widest-surface risk (~179 importers repo-wide;
  enums and pydantic models imported everywhere). Mitigation: exhaustive section-(d)
  audit, explicit per-symbol re-exports, `make test-all` gate. Confirm nothing
  persistence-sensitive depends on class `__module__`.
- Pydantic forward references / `model_rebuild()`: splitting mutually-referencing
  models across submodules can surface undefined-annotation errors at import time.
  Keep tightly-coupled model clusters together; prefer a split by cohesive domain
  (pipeline/repo-spec models vs agent/phase/decision models per #3450) with the
  barrel resolving import order.
- `_run_pipeline` transition-ordering behavior risk: split scheduled last, handlers
  stay private and are exercised through `_run_pipeline` by the existing dense seam
  coverage (test_consensus_polling, test_brc_nack_iteration, test_concurrent_*,
  test_advance_phase_*).
- R3 packaging blind spot (HIGH, proven): non-recursive globs at
  orchestrator/Dockerfile:44 and gateway/Dockerfile:67 silently drop converted
  packages from images while source-tree gates stay green. Same-slice COPY + smoke
  check, exactly as the 7 precedent slices did.
- Giant slices legitimately take 30-60+ min single steps (`make test-all` on the
  pipelines.py test surface); overseer heartbeat-silence alerts are false-positive
  prone (#3341): verify pod liveness before any restart.
- Allowlist YAML contention resolved by the single linear chain; the section-(e)
  rebase recipe applies when a slice rebases onto the advancing chain tip.

```yaml
# yaml-tasks
pr:
  title: "Decompose the last 4 oversize files; empty the allowlist (#3312)"
  description: |
    Finishes the decomposition program started in #3312: 17 of 19 slices landed via PR
    #3336; this run decomposes the remaining two structural outliers
    (`gateway/gateway.py` ~10.6k lines, `orchestrator/routes/pipelines.py` ~30.5k
    lines) plus the two files that crossed the cap since: `orchestrator/models.py`
    (#3450) and `orchestrator/event_loop.py` (#3447). Drives
    `scripts/file-size-allowlist.yaml`'s `files:` map to EMPTY.

    Each file `F.py` becomes `F/__init__.py` (explicit per-symbol re-export barrel,
    the stable public API) plus underscore-prefixed private `_*.py` submodules,
    following `docs/guides/decomposition-pattern.md` and the 17 in-tree worked
    references. Test patch targets keep resolving through the barrel. The
    `_run_pipeline` state machine is split into per-phase handlers + a thin loop
    (issue #3312 non-negotiable #7). Flask decorators stay in `__init__.py`
    (non-negotiable #8).

    R3 container parity: slices converting a module shipped by a NON-recursive
    Dockerfile glob add the matching recursive `COPY <pkg>/` in the SAME slice plus an
    image-build/import smoke check (source-tree gates cannot catch a missing COPY);
    the gateway.py slice also preserves the Flask launch (barrel `app`, port 9848).

    Pure refactor: no behavior changes; surfaced bugs are filed as separate follow-ups,
    never bundled. Branches prefixed `egg/`. Implements #3312 (closes it), closes
    #3447 and #3450.
  test_plan: |
    - Automated: `make lint` + `make test-all` green at EVERY slice boundary. A missed
      re-export fails the test that patches or imports the moved symbol. Each slice
      runs the section-(d) `git grep` audit before extraction.
    - Per slice: step-0 `git mv` baseline commit is independently green; every
      submodule verified under BOTH caps (further-split in-slice, never a fresh
      allowlist entry).
    - R3 container gate: slices 1/2 build the orchestrator image and smoke-check
      `python -c 'import models'` / `'import event_loop'`; slice 3 builds the gateway
      image and smoke-checks that the container starts and serves on 9848; slice 4
      confirms recursive-COPY shipping by grounding.
    - `_run_pipeline` handlers stay private, tested THROUGH `_run_pipeline` via the
      existing dense seam coverage (test_consensus_polling, test_brc_nack_iteration,
      test_concurrent_*, test_advance_phase_*); isolation tests are a follow-up.
    - Inner loop with `make test` (changeset-aware); full suite via `make test-all`.
  manual_steps: |
    Pre-merge (per slice): reviewer spot-checks (a) the __init__.py re-export list
    against `git grep`, (b) submodule clustering is cohesive, (c) the CLAUDE.md seam
    row matches. Per R3 slice: build the affected container image and run the
    import/start smoke check (source-tree gates do not build images).
    Pre-merge (final slice = pipelines.py): verify the allowlist `files:` map is
    EMPTY and all four CLAUDE.md seam tables are current.
    Post-merge: none (pure refactor; no migrations/config/deploy).
phases:
  - id: 1
    name: "Decompose orchestrator/models.py (1,521 lines; closes #3450)"
    goal: "Decompose `orchestrator/models.py` into a sub-package; drop its allowlist entry; seam row in orchestrator/CLAUDE.md. Head of the linear chain (every slice edits scripts/file-size-allowlist.yaml, so per #3046 overlapping slices are serialized)."
    dependencies: []
    tasks:
      - id: task-1-1
        description: "External-importer audit (section-(d)) for models.py: the WIDEST import surface in the repo (~179 files reference it across orchestrator/, tests/, shared/, gateway/). git grep -nE every public symbol (models, enums, helpers); record externally-referenced symbols (must be re-exported) vs internal-only. Also check for anything __module__-sensitive (serialization, isinstance-by-path, sphinx refs)."
        acceptance: "Audit output lists every public symbol with its external-reference status; the re-export set is derived from it; no __module__-sensitive consumer found (or each one inventoried)."
        files:
          - orchestrator/models.py
      - id: task-1-2
        description: "Step-0 baseline: git mv orchestrator/models.py orchestrator/models/__init__.py; verify imports resolve with make test-all; commit as a standalone bisectable baseline (move only, no extraction)."
        acceptance: "Sub-package baseline commit is green (make test-all); diff is a pure move."
        files:
          - orchestrator/models/__init__.py
      - id: task-1-3
        description: "Extract cohesive clusters into _<cluster>.py submodules split by domain per #3450's suggestion: e.g. _enums.py (status enums), _pipeline.py (Pipeline/RepoSpec/PipelineConfig and pipeline-level helpers), _execution.py (PhaseExecution/AgentExecution/agent-phase models), _decisions.py (HITLDecision/OperatorDirective), _events.py (PipelineEvent/ProgressEvent). Keep tightly-coupled / mutually-referencing pydantic models in the same submodule to avoid forward-reference breakage; call model_rebuild() where a split forces a forward ref. __init__.py does explicit per-symbol re-exports for every externally-referenced symbol from task-1-1; one consistent import style within the sub-package."
        acceptance: "All submodules <=1500 lines / <=100KB; barrel re-exports every external symbol; from models import X resolves for the full audited set; no pydantic forward-ref/import-order errors."
        files:
          - orchestrator/models/
      - id: task-1-4
        description: "Drop models.py's entry from scripts/file-size-allowlist.yaml; add a concrete models/ seam row to orchestrator/CLAUDE.md."
        acceptance: "Allowlist entry removed; CLAUDE.md row added; make lint passes the ratchet."
        files:
          - scripts/file-size-allowlist.yaml
          - orchestrator/CLAUDE.md
      - id: task-1-5
        description: "R3 container-packaging mitigation (SAME SLICE). orchestrator/Dockerfile:44 ships top-level modules via the NON-recursive glob `COPY orchestrator/*.py ./`; once models.py becomes the package directory orchestrator/models/ the glob stops copying it -> ModuleNotFoundError at container start. Add an explicit `COPY orchestrator/models/ ./models/` line (mirroring the existing state_store/peer_consensus/mcp_tools/kubernetes_spawner/gateway_client lines added by PR #3336). BUILD the orchestrator image and smoke-check `python -c 'import models'` inside it: make lint + make test-all run on the SOURCE TREE and cannot catch a missing COPY."
        acceptance: "orchestrator/Dockerfile has an explicit `COPY orchestrator/models/ ./models/` line; the built image imports models (smoke check)."
        files:
          - orchestrator/Dockerfile
      - id: task-1-6
        description: "Green the boundary: make lint + make test-all; mechanical test patch-path rewrites in-slice if any; file test-logic rewrites or surfaced bugs as 'Part of #3450' follow-ups."
        acceptance: "make lint + make test-all green; no behavior change in the diff."
        files:
          - orchestrator/tests/
          - tests/
  - id: 2
    name: "Decompose orchestrator/event_loop.py (1,622 lines; closes #3447)"
    goal: "Decompose `orchestrator/event_loop.py` into a sub-package; drop its allowlist entry; seam row in orchestrator/CLAUDE.md. Depends on the previous slice (single linear chain)."
    dependencies:
      - 1
    tasks:
      - id: task-2-1
        description: "External-importer audit (section-(d)) for event_loop.py (~6 referencing files; small surface). Enumerate every public symbol and unittest.mock.patch target."
        acceptance: "Re-export set derived from external-reference audit."
        files:
          - orchestrator/event_loop.py
      - id: task-2-2
        description: "Step-0 git mv event_loop.py -> event_loop/__init__.py; green baseline commit."
        acceptance: "Pure-move baseline commit green."
        files:
          - orchestrator/event_loop/__init__.py
      - id: task-2-3
        description: "Apply the method-modules-on-class pattern (section-(c)) per issue #3447: the module is class-dominated (EventDecision L331, JobSupervisor L353, OrchestratorEventLoop L990). Keep class definitions + __init__ in the barrel; move method bodies to responsibility-grouped private submodules as module-level functions taking self, bound back onto the class in the barrel (matching the landed state_store/, peer_consensus/, kubernetes_spawner/ slices). Explicit per-symbol re-export barrel preserving every external symbol and patch target."
        acceptance: "All submodules <=1500 lines / <=100KB; class identities stay on the event_loop module path; barrel re-exports every external symbol; patch('event_loop.*') targets resolve."
        files:
          - orchestrator/event_loop/
      - id: task-2-4
        description: "Drop event_loop.py's allowlist entry; add a concrete event_loop/ seam row to orchestrator/CLAUDE.md."
        acceptance: "Allowlist entry removed; CLAUDE.md row added; lint ratchet passes."
        files:
          - scripts/file-size-allowlist.yaml
          - orchestrator/CLAUDE.md
      - id: task-2-5
        description: "R3 container-packaging mitigation (SAME SLICE). Same non-recursive glob as slice 1 (orchestrator/Dockerfile:44). Add an explicit `COPY orchestrator/event_loop/ ./event_loop/` line; BUILD the orchestrator image and smoke-check `python -c 'import event_loop'` inside it."
        acceptance: "orchestrator/Dockerfile has an explicit `COPY orchestrator/event_loop/ ./event_loop/` line; the built image imports event_loop (smoke check)."
        files:
          - orchestrator/Dockerfile
      - id: task-2-6
        description: "make lint + make test-all green; mechanical patch-path rewrites in-slice; follow-ups filed as 'Part of #3447' for anything more."
        acceptance: "Green; no behavior change."
        files:
          - orchestrator/tests/
          - tests/
  - id: 3
    name: "Decompose gateway/gateway.py (10,648 lines, STRUCTURAL OUTLIER, OVER BYTE CAP): Flask @app.route seam (#3312 slice-18 equivalent)"
    goal: "Decompose `gateway/gateway.py` (10,648 lines / 419 KB) into a sub-package; drop its allowlist entry; seam coverage in gateway/CLAUDE.md. Depends on the previous slice (single linear chain)."
    dependencies:
      - 2
    tasks:
      - id: task-3-1
        description: "External-importer audit (section-(d)) for gateway.py: ~35 files reference gateway.gateway / import gateway (incl. config_validator.py); re-run the audit live. Enumerate every @app.route handler and every externally-referenced helper (e.g. get_anthropic_client)."
        acceptance: "Full route+symbol inventory captured; re-export set derived; referencing-file estimate re-verified against the live tree."
        files:
          - gateway/gateway.py
      - id: task-3-2
        description: "Step-0 git mv gateway.py -> gateway/__init__.py; green baseline commit (move only). Confirm the Flask app object and all @app.route registrations still resolve on import."
        acceptance: "Pure-move baseline commit green; Flask app + routes register identically."
        files:
          - gateway/gateway/__init__.py
      - id: task-3-3
        description: "Apply the routes-handling convention (non-negotiable #8 / section-(f)): @app.route decorators stay on thin wrapper functions in __init__.py; wrapper bodies delegate to implementation functions in responsibility-grouped _<cluster>.py submodules (e.g. _git_ops, _pr_lifecycle, _credentials/auth, _validation/policy, _checkpoint, _worktree). Do NOT move the decorators. Explicit per-symbol re-export barrel for every externally-referenced helper."
        acceptance: "All decorators remain in __init__.py; route URL->handler map unchanged; submodules hold the bodies; barrel re-exports every external symbol; patch('gateway.gateway.*') targets resolve."
        files:
          - gateway/gateway/
      - id: task-3-4
        description: "Pre-allocate and verify cluster sizing: at ~10.6k lines several submodules will themselves approach the cap; further-split in-slice (section-(g), recursive barrel pattern) rather than adding any fresh allowlist entry. Confirm EVERY resulting module is under BOTH caps."
        acceptance: "No submodule over 1500 lines / 100KB; any further-split uses a nested barrel; zero new allowlist entries."
        files:
          - gateway/gateway/
      - id: task-3-5
        description: "Drop gateway.py's allowlist entry; author the concrete gateway/ submodule layout rows in gateway/CLAUDE.md's seam table."
        acceptance: "Allowlist entry removed; gateway/CLAUDE.md carries the concrete submodule layout; lint ratchet passes."
        files:
          - scripts/file-size-allowlist.yaml
          - gateway/CLAUDE.md
      - id: task-3-6
        description: "R3 container-packaging + Flask-launch mitigation (SAME SLICE). gateway/Dockerfile:67 ships top-level modules via the NON-recursive glob `COPY gateway/*.py ./`. Converting gateway.py -> gateway/gateway/__init__.py needs TWO same-slice fixes handled together: (a) add `COPY gateway/gateway/ ./gateway/` to gateway/Dockerfile (mirroring the git_client/worktree_manager lines) so the new package dir ships; (b) preserve the Flask launch: gateway.py defines `app = Flask(__name__)` (L486) and `if __name__=='__main__': app.run(...)` (L10639/L10647), started on port 9848 via gateway/entrypoint.sh; keep the same `app` object exported through the barrel and update the launch invocation / any `import gateway` consumer (incl. config_validator.py) so the server still starts. BUILD the gateway image and smoke-check the container starts and serves on 9848."
        acceptance: "gateway/Dockerfile copies the new gateway/gateway/ package; `app` is exported through the barrel and the launch path (entrypoint.sh) starts the Flask server on 9848 unchanged; built image passes a start/serve smoke check."
        files:
          - gateway/Dockerfile
          - gateway/entrypoint.sh
      - id: task-3-7
        description: "make lint + make test-all green at the slice boundary; mechanical patch-path rewrites in-slice; any latent bug surfaced is filed as a 'Part of #3312' follow-up, never bundled."
        acceptance: "Green; no behavior change in the diff."
        files:
          - gateway/tests/
          - tests/
  - id: 4
    name: "Decompose orchestrator/routes/pipelines.py (30,520 lines, STRUCTURAL OUTLIER, OVER BYTE CAP): _run_pipeline split (#3312 slice-19 equivalent, non-negotiable #7)"
    goal: "Decompose `orchestrator/routes/pipelines.py` (30,520 lines / 1.44 MB) into a sub-package; drop the LAST allowlist entry (files: map EMPTY); seam coverage in orchestrator/CLAUDE.md. Depends on the previous slice (single linear chain). Closes #3312."
    dependencies:
      - 3
    tasks:
      - id: task-4-1
        description: "External-importer audit (section-(d)) for pipelines.py: the dominant back-compat surface (~137 referencing files live-counted 2026-07-03; the prior refine estimated ~57 distinct patch targets). Re-run the audit live per cluster; build the complete re-export inventory. This is the load-bearing risk for the whole program."
        acceptance: "Complete external-symbol inventory captured; patch-target set re-verified live; re-export set derived per cluster."
        files:
          - orchestrator/routes/pipelines.py
      - id: task-4-2
        description: "Step-0 git mv pipelines.py -> pipelines/__init__.py; green baseline commit (move only, before ANY extraction). This is the bisectable cut between 'moved' and 'extracted'."
        acceptance: "Pure-move baseline commit green (make test-all)."
        files:
          - orchestrator/routes/pipelines/__init__.py
      - id: task-4-3
        description: "_run_pipeline split (issue #3312 non-negotiable #7, addressed head-on): split the phase-transition state machine into per-phase handlers (_run_refine_phase, _run_plan_phase, the existing _run_implement_phase_slices, _run_pr_phase) plus a thin orchestration loop in _run_loop.py, per the pattern doc's prior design. Per-phase handlers stay PRIVATE and are tested through _run_pipeline for this refactor (isolation tests are a follow-up). Preserve ordering/transition semantics exactly: pure refactor."
        acceptance: "_run_pipeline becomes a thin loop delegating to per-phase handlers; no transition-ordering behavior change; existing dense seam coverage (test_consensus_polling, test_brc_nack_iteration, test_concurrent_*, test_advance_phase_*) stays green."
        files:
          - orchestrator/routes/pipelines/
      - id: task-4-4
        description: "Extract the remaining clusters into _<cluster>.py submodules (e.g. _criteria, _pr_lifecycle, _decisions, _readers, prompt-building as a nested sub-sub-package _prompt_building/ per section-(g)); routes convention (Blueprint decorators stay in __init__.py); explicit per-symbol re-export barrel re-exporting EVERY externally-referenced symbol from task-4-1; recursive barrel where a cluster further-splits. Confirm EVERY module under BOTH caps. No Dockerfile change needed: pipelines/ stays under the recursive `COPY orchestrator/routes/ ./routes/` (orchestrator/Dockerfile:45); confirm by grounding."
        acceptance: "All submodules (and nested sub-packages) <=1500 lines AND <=100KB; decorators in __init__.py; barrel (and nested barrels) re-export every external symbol; all audited patch targets resolve through the barrel; recursive-COPY shipping confirmed."
        files:
          - orchestrator/routes/pipelines/
      - id: task-4-5
        description: "Drop pipelines.py's allowlist entry: this is the LAST entry; confirm scripts/file-size-allowlist.yaml's files: map is now EMPTY (the program's terminal acceptance criterion). Author the concrete pipelines/ submodule layout in orchestrator/CLAUDE.md; verify all four CLAUDE.md seam tables (orchestrator, gateway, sandbox, shared) are current."
        acceptance: "pipelines.py entry removed; files: map empty; orchestrator/CLAUDE.md carries the concrete pipelines/ + _run_pipeline submodule layout; lint passes with an empty allowlist."
        files:
          - scripts/file-size-allowlist.yaml
          - orchestrator/CLAUDE.md
      - id: task-4-6
        description: "make lint + make test-all green at the slice boundary. Mechanical patch-path rewrites in-slice; any latent bug surfaced by the _run_pipeline split is filed as a 'Part of #3312' follow-up, never bundled. Final program verification: empty allowlist + all 4 files under cap + every CLAUDE.md seam table populated."
        acceptance: "Green; allowlist empty; no behavior change; full acceptance set satisfied."
        files:
          - orchestrator/tests/
          - tests/
```

## HITL Resolution

Operator-approved continuation of issue #3312's locked scope (refine decision-1 and
the plan-gate approval from pipeline issue-3312, 2026-06-26): the two giants are fully
in scope and must finish (not optional); `_run_pipeline` split head-on into per-phase
handlers + thin loop; R3 Dockerfile-COPY mitigation in every affected slice;
empty-allowlist done-signal; pure refactor with bugs filed separately. The two
newly-allowlisted files (#3450 models.py, #3447 event_loop.py) are added to the same
program by operator directive (2026-07-03). Single linear chain ordering accepted
(shared allowlist file forces serialization). Proceed at implement.