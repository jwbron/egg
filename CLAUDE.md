# egg — Agent-Powered SDLC Platform

Start with **[docs/index.md](docs/index.md)** — it has task-specific lookup tables, architecture docs, ADRs, and component READMEs.

## Quick Reference

```bash
make help          # List all targets
make setup         # Install dependencies (requires uv)
make lint          # Run all linters (Python, Shell, YAML, Dockerfile)
make test          # Run full test suite
make lint-fix      # Auto-fix lint issues
make security      # Run security scans (bandit, safety, trivy)
```

## Repo Layout

| Directory | What it is |
|-----------|------------|
| `orchestrator/` | Central SDLC pipeline engine — scheduling, health monitoring, multi-agent coordination |
| `gateway/` | Policy-enforcement sidecar — validates git/gh operations, injects credentials |
| `sandbox/` | Untrusted agent container — Claude Code config, tools, entrypoint |
| `shared/` | Shared Python libraries used across components |
| `docs/` | All documentation — guides, ADRs, architecture, references |
| `integration_tests/` | Cross-component integration tests |
| `scripts/` | Build, release, and CI helper scripts |

## Key Entry Points

- **Headless agents** use the Agent SDK (`egg_agent` package)
- **Interactive use** goes through the `claude` CLI
- See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, branching, and PR workflow
