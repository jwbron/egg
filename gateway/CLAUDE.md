# Gateway

Policy-enforcement sidecar that sits between agents and GitHub. Validates git/gh operations, enforces phase restrictions, and injects credentials.

- **[README.md](README.md)** — architecture, policy rules, configuration
- **[../docs/index.md](../docs/index.md)** — full documentation index

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.
