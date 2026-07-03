# Analysis: finish the file-size decomposition program (issues #3312, #3447, #3450)

## Context and history

The `make lint` file-size cap (1,500 lines / 100 KB, added by #2250 closing #2248)
grandfathers oversize Python files in `scripts/file-size-allowlist.yaml`. Issue #3312
tracked decomposing all 19 grandfathered files. The first SDLC run of #3312 landed
**17 of 19 slices** via merged PR #3336 (2026-06-28): every non-giant file is now a
sub-package following the canonical pattern, and the pattern is proven in-repo
(see `orchestrator/state_store/`, `peer_consensus/`, `mcp_tools/`,
`kubernetes_spawner/`, `gateway_client/`, `gateway/git_client/`,
`gateway/worktree_manager/`, `sandbox/entrypoint/`, `sandbox/egg_lib/orch_cli/`, etc.).
That run was paused on a weekly quota wall before the two giants landed. No in-flight
giant work survives anywhere (the old `egg/issue-3312/slice-18` branch contains only
already-landed earlier-slice commits; zero gateway.py decomposition commits).

Since then, two more files crossed the cap and were allowlisted as escape valves:
`orchestrator/models.py` (#3450, pushed over by the #3393 multi-repo stack) and
`orchestrator/event_loop.py` (#3447, pushed over by #3434 / #3425).

The allowlist `files:` map today holds exactly **4 entries**; this run decomposes all
4 and drives the map to EMPTY (the program's terminal acceptance criterion).

## Live inventory (verified 2026-07-03 on main @ 36ecaa91d)

| File | Lines | Bytes | Shape | Importer surface |
|---|---|---|---|---|
| `orchestrator/models.py` | 1,521 | 68,068 | Pydantic-model + enum dominated: `PipelineConfig` (L570), `Pipeline` (L1146), `PhaseExecution` (L441), `AgentExecution` (L213), `RepoSpec`, `HITLDecision`, status enums, plus helpers `resolve_consensus_timeout_minutes` / `resolve_slice_repo` | ~179 files reference it; the widest import surface in the repo; barrel completeness is the load-bearing risk |
| `orchestrator/event_loop.py` | 1,622 | 81,212 | Class-dominated: `EventDecision` (L331), `JobSupervisor` (L353), `OrchestratorEventLoop` (L990) | ~6 files; small surface |
| `gateway/gateway.py` | 10,648 | 419,219 | Flask app: `app = Flask(__name__)` at L486; `app.run(...)` at L10639 under `if __name__ == "__main__"` (L10647); launched via `gateway/entrypoint.sh` on port 9848 | ~35 files reference `gateway.gateway` / `import gateway` (incl. `config_validator.py`) |
| `orchestrator/routes/pipelines.py` | 30,520 | 1,437,633 | Flask Blueprint routes + the `_run_pipeline` phase-transition state machine; the repo's structural outlier (grew from ~27.2k since the prior plan) | ~137 files reference `routes.pipelines`; dominant patch-target surface |

## Container packaging (R3, proven mitigation)

`make lint` / `make test-all` run on the source tree, not the built image, so a module
that drops out of a non-recursive Dockerfile COPY glob is invisible to the gates.
Verified live:

- `orchestrator/Dockerfile:44` is still the non-recursive `COPY orchestrator/*.py ./`.
  Converting `models.py` or `event_loop.py` to a package directory silently drops it
  from the image. Fix: explicit `COPY orchestrator/<pkg>/ ./<pkg>/` in the same slice,
  mirroring the five precedent lines the prior run added (state_store L51,
  peer_consensus L56, mcp_tools L62, kubernetes_spawner L68, gateway_client L75).
- `gateway/Dockerfile:67` is still the non-recursive `COPY gateway/*.py ./`.
  `gateway.py` needs the same-slice `COPY gateway/gateway/ ./gateway/` plus the Flask
  launch fix (the `app` object must stay reachable; entrypoint.sh starts the server on
  hardcoded port 9848). Precedent lines: git_client L71, worktree_manager L75.
- `orchestrator/routes/pipelines.py` ships under the recursive
  `COPY orchestrator/routes/ ./routes/` (Dockerfile:45): no Dockerfile change needed.

## Pattern references

- `docs/guides/decomposition-pattern.md`: canonical recipe (external-importer audit,
  step-0 `git mv` baseline, per-symbol re-export barrel, underscore-private submodules,
  method-modules-on-class variant in section (c), routes/decorator convention in
  section (f), recursive further-split in section (g)).
- 17 landed sub-packages from PR #3336 are in-tree worked references.
- Seam tables exist and are populated in `orchestrator/CLAUDE.md`, `gateway/CLAUDE.md`,
  `sandbox/CLAUDE.md`, `shared/CLAUDE.md`; each slice appends/extends its row.

## Risks

- **models.py barrel completeness** (widest surface, ~179 importers): a missed
  re-export breaks imports repo-wide. Mitigation: exhaustive section-(d) audit +
  explicit per-symbol re-exports + `make test-all` gate. Also verify nothing depends
  on class `__module__` for serialization (pydantic v2 models here serialize by
  field schema, not class path, but confirm no `__module__`-sensitive persistence).
- **`_run_pipeline` split** (issue #3312 non-negotiable #7): subtle transition
  ordering; pure-refactor discipline; handlers stay private, tested through
  `_run_pipeline` via the existing dense seam coverage.
- **Giant-slice quota walls**: the prior run hit subscription streak walls on the
  giants; paced `restart_phase` preserves landed slices.
- **Allowlist YAML contention**: every slice edits it; single linear chain
  (slice-1 -> slice-2 -> slice-3 -> slice-4) per #3046.