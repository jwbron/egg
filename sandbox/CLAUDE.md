# Sandbox

Untrusted agent container. Provides the isolated execution environment where Claude Code runs, with tools, entrypoint scripts, and Claude Code configuration.

- **[README.md](README.md)** — container setup, environment variables, tool inventory
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **`.claude/rules/`** — Claude Code rules injected into sandboxed agents (not relevant for local development)

## Testing

```bash
make test                      # Full suite from repo root
pytest sandbox/tests/          # Sandbox tests only
```
