# egg — Agent-Powered SDLC Platform

Start with **[docs/index.md](docs/index.md)** — it has task-specific lookup tables, architecture docs, and component READMEs.

## Quick Reference

```bash
make help          # List all targets
make deps          # Install all dependencies (installs uv + venv)
make setup         # Install dependencies + pre-commit hooks
make lint          # Run all linters (Python, Shell, YAML, Dockerfile)
make test          # Run full test suite
make lint-fix      # Auto-fix lint issues
make security      # Run security scans (bandit, safety, trivy)
```

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
- **Interactive use** goes through the `claude` CLI
- See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, branching, and PR workflow
