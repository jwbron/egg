# Shared Libraries

Reusable Python libraries shared between the gateway sidecar, orchestrator, and sandbox container. Each top-level directory under `shared/` is an installable Python package consumed by the other subsystems.

- **[README.md](README.md)** — per-package architecture, public API, and usage examples
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **[../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md)** — canonical sub-package + barrel re-export pattern used by the in-flight decompositions tracked below

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Submodule seam tables

Several of the largest shared source files are being decomposed into sub-packages so each module fits the 1,500-line / 100 KB cap from `scripts/file-size-allowlist.yaml`. The canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) is documented in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). The table below maps each decomposed module to its current submodule layout so contributors can find code without scanning the barrel.

The barrel `__init__.py` is the **stable public API** (HITL decision-7 of #2817). External consumers — tests, production importers, mocks — keep importing through the barrel (`from egg_contracts.checkpoint_cli import main`, `from egg_contracts.plan_parser import parse_plan`); the submodule paths below are package-private and may move between releases.

### In-flight decompositions

The following shared-side files are under decomposition in #2817;
rows will be filled in as each slice lands:

| File | Current size | Slice |
|------|--------------|-------|
| `shared/egg_contracts/checkpoint_cli.py` | ~2,233 lines | slice-17 (#2817) — TBD |
| `shared/egg_contracts/plan_parser.py` | ~1,835 lines | slice-25 (#2817) — TBD (was previously tracked under the closed #2569, folded into #2817 on 2026-05-30) |

The terminal slice for each file replaces the TBD row with the
concrete submodule layout once the decomposition lands.
