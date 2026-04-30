# Orchestrator

Central coordination engine for SDLC pipelines. Manages agent lifecycle, phase transitions, health monitoring, and multi-agent consensus.

- **[README.md](README.md)** — architecture, API surface, configuration
- **[../docs/architecture/orchestrator.md](../docs/architecture/orchestrator.md)** — design decisions and component diagram
- **[../docs/reference/orchestrator-cli.md](../docs/reference/orchestrator-cli.md)** — CLI reference (`egg-orch`)
- **[../docs/guides/concurrent-execution.md](../docs/guides/concurrent-execution.md)** — multi-agent BRC protocol

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Submodule seam tables

Several of the orchestrator's largest source files are being decomposed into sub-packages so each module fits the 1,500-line / 100 KB cap from `scripts/file-size-allowlist.yaml`. The canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) is documented in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). The tables below map each decomposed module to its current submodule layout so contributors can find code without scanning the barrel.

The barrel `__init__.py` is the **stable public API** (HITL decision-7 of #2261). External consumers — tests, production importers, mocks — keep importing through the barrel (`from routes.pipelines import _foo`); the submodule paths below are package-private and may move between releases.

### `orchestrator/routes/pipelines/` — TBD (#2261 slice-15)

Placeholder. Slice-15 of #2261 lands the decomposition of
`orchestrator/routes/pipelines.py` (~16,400 lines) plus the per-phase
refactor of `_run_pipeline`. Pre-allocated submodule clusters per the
plan:

| Submodule | Owned symbols |
|-----------|---------------|
| `_run_loop/` | TBD — `_run_pipeline`, `PHASE_HANDLERS`, per-phase handlers (`_run_refine.py`, `_run_plan.py`, `_run_implement.py`, `_run_pr.py`) |
| `_concurrent_phase/` | TBD — `_spawn.py`, `_consensus_wait.py` |
| `_prompt_building/` | TBD — pre-split nested sub-package |
| `_pr_lifecycle/` | TBD — pre-split nested sub-package |
| `_worktree_ops/` | TBD — pre-split nested sub-package |
| `_criteria.py` | TBD — review-criteria helpers |
| `_readers.py` | TBD — `_read_phase_draft`, `_pipeline_identifier`, peer artifact readers |
| `_decisions.py` | TBD — plan/contract decision helpers |
| `_status_helpers.py` | TBD — phase status / wait helpers |
| `_brc_verdicts.py` | TBD — BRC verdict aggregators |

The terminal slice (#2261 slice-15) replaces the TBD rows with the
concrete submodule layout once the decomposition lands.

### Other in-flight decompositions

The following orchestrator-side files are also under decomposition in
#2261; rows will be filled in as each slice lands:

| File | Current size | Slice |
|------|--------------|-------|
| `orchestrator/mcp_tools.py` | ~2,820 lines | slice-12 (#2261) |
| `orchestrator/gateway_client.py` | ~2,357 lines | slice-11 (#2261) |
| `orchestrator/overseer/monitor.py` | ~2,050 lines | slice-7 (#2261) |
| `orchestrator/peer_consensus.py` | ~2,013 lines | slice-5 (#2261) |
| `orchestrator/routes/signals.py` | ~1,986 lines | slice-4 (#2261) |
| `orchestrator/routes/deployment.py` | ~1,604 lines | slice-2 (#2261) |
