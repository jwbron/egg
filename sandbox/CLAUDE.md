# Sandbox

Untrusted agent container. Provides the isolated execution environment where Claude Code runs, with tools, entrypoint scripts, and Claude Code configuration.

- **[README.md](README.md)** — container setup, environment variables, tool inventory
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **`agent-config/rules/`** — Claude Code rules injected into sandboxed agents (not relevant for local development)

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.
