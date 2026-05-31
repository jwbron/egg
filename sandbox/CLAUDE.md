# Sandbox

Untrusted agent container. Provides the isolated execution environment where Claude Code runs, with tools, entrypoint scripts, and Claude Code configuration.

- **[README.md](README.md)** — container setup, environment variables, tool inventory
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **`agent-config/rules/`** — Claude Code rules injected into sandboxed agents (not relevant for local development)

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Submodule seam tables

Several of the sandbox's largest source files are being decomposed into sub-packages so each module fits the 1,500-line / 100 KB cap from `scripts/file-size-allowlist.yaml`. The canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) is documented in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). The table below maps each decomposed module to its current submodule layout so contributors can find code without scanning the barrel.

The barrel `__init__.py` is the **stable public API** (HITL decision-7 of #2817). External consumers — tests, production importers, mocks — keep importing through the barrel (`from egg_lib.orch_cli import main`); the submodule paths below are package-private and may move between releases.

### In-flight decompositions

The following sandbox-side files are under decomposition in #2817;
rows will be filled in as each slice lands:

| File | Current size | Slice |
|------|--------------|-------|
| `sandbox/egg_lib/orch_cli.py` | ~4,034 lines | slice-13 (#2817) — TBD |
| `sandbox/entrypoint.py` | ~2,210 lines | slice-19 (#2817) — TBD |

The terminal slice for each file replaces the TBD row with the
concrete submodule layout once the decomposition lands.
