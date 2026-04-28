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

**Use `make test`, not raw `pytest`/`.venv/bin/pytest`.** It narrows to the tests your diff actually touches (transitively, via static imports), so you don't have to guess which suites to run. Reach for direct `pytest` only when you need a flag the wrapper doesn't expose. See [docs/guides/testing.md](docs/guides/testing.md) for the narrowing model and `make test-all` escape hatch.

## Python Environment

If `.venv` is absent, run `make deps` to install all dependencies. This installs `uv` if needed and creates a `.venv` with all dev dependencies. Always use the `.venv` for project-specific Python usage such as tests and linting.

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
  roster), [`submit_task`](docs/guides/sdlc-pipeline.md) (full
  refine → plan → implement), and
  [`babysit_pr`](docs/guides/babysit-pr.md) (implement-phase BRC on a
  PR). The legacy interactive-mode CLI (`bin/egg`) was removed in
  [#1762](https://github.com/jwbron/egg/issues/1762).
- See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, branching, and PR workflow
