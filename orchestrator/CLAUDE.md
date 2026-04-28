# Orchestrator

Central coordination engine for SDLC pipelines. Manages agent lifecycle, phase transitions, health monitoring, and multi-agent consensus.

- **[README.md](README.md)** — architecture, API surface, configuration
- **[../docs/architecture/orchestrator.md](../docs/architecture/orchestrator.md)** — design decisions and component diagram
- **[../docs/reference/orchestrator-cli.md](../docs/reference/orchestrator-cli.md)** — CLI reference (`egg-orch`)
- **[../docs/guides/concurrent-execution.md](../docs/guides/concurrent-execution.md)** — multi-agent BRC protocol

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.
