# egg — Agent-Powered SDLC Platform

Start with **[docs/index.md](docs/index.md)** — it has task-specific lookup tables, architecture docs, and component READMEs.

## Quick Reference

```bash
make help          # List all targets
make deps          # Install all dependencies (installs uv + venv)
make setup         # Install dependencies + pre-commit hooks
make lint          # Run all linters (Python, Shell, YAML, Dockerfile)
make test          # Changeset-aware: tests reachable from the diff (inner-loop default)
make test-all      # Full suite — CI ground truth; updates LKG baseline on green
make lint-fix      # Auto-fix lint issues
make security      # Run security scans (bandit, safety, trivy)
```

**Use `make test`, not raw `pytest`/`.venv/bin/pytest`.** `make test` is changeset-aware — it narrows to the tests your diff actually touches (transitively, via static imports), so you don't have to guess which suites to run. It also picks up `.venv/bin/pytest` automatically, so the venv's pinned versions are what runs. Reach for direct `pytest` only when you need a flag the wrapper doesn't expose, and even then **invoke it from the venv** (`.venv/bin/pytest …`) — the system `pytest` won't have the project's deps. See [docs/guides/testing.md](docs/guides/testing.md) for the narrowing model and `make test-all` escape hatch.

## Python Environment

This project requires a `.venv` for all Python tooling — pytest, ruff, mypy, etc. `make` targets resolve to `.venv/bin/<tool>` automatically, so following the "use `make test` / `make lint`" guidance above keeps you in the venv without thinking about it. If you ever invoke a Python tool directly, prefix it with `.venv/bin/` (e.g. `.venv/bin/pytest`, `.venv/bin/ruff`) — never the system binary.

If `.venv` is absent, run `make deps` to install everything. This installs `uv` if needed and creates a `.venv` with all dev dependencies.

## Repo Layout

| Directory | What it is |
|-----------|------------|
| `orchestrator/` | Central SDLC pipeline engine — scheduling, health monitoring, multi-agent coordination |
| `gateway/` | Policy-enforcement sidecar — validates git/gh operations, injects credentials |
| `sandbox/` | Untrusted agent container — Claude Code config, tools, entrypoint |
| `shared/` | Shared Python packages and agent prompt templates |
| `docs/` | All documentation — guides, architecture, references |
| `integration_tests/` | Cross-component integration tests |
| `scripts/` | Build, release, and CI helper scripts |

## Key Entry Points

- **Headless agents** use the Agent SDK (`egg_agent` package)
- **One-off agent work** goes through the MCP server — see
  [`run_agent_task`](docs/guides/custom-phase.md) (single-phase, subset
  roster) and [`submit_task`](docs/guides/sdlc-pipeline.md) (full
  refine → plan → implement). The legacy interactive-mode CLI
  (`bin/egg`) was removed in
  [#1762](https://github.com/jwbron/egg/issues/1762).
- See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, branching, and PR workflow
