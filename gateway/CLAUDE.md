# Gateway

Policy-enforcement sidecar that sits between agents and GitHub. Validates git/gh operations, enforces phase restrictions, and injects credentials.

- **[README.md](README.md)** — architecture, policy rules, configuration
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **[../docs/architecture/upstream-routing.md](../docs/architecture/upstream-routing.md)** — `UpstreamRegistry` seam, LiteLLM topology, per-session routing decision, and the no-op-by-default invariant for non-Claude agent backends ([#2769](https://github.com/jwbron/egg/issues/2769))

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Submodule seam tables

Several of the gateway's largest source files are being decomposed into sub-packages so each module fits the 1,500-line / 100 KB cap from `scripts/file-size-allowlist.yaml`. The canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) is documented in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). The tables below map each decomposed module to its current submodule layout so contributors can find code without scanning the barrel.

The barrel `__init__.py` is the **stable public API** (HITL decision-7 of #2261). External consumers — tests, production importers, mocks — keep importing through the barrel (`from gateway.gateway import get_anthropic_client`); the submodule paths below are package-private and may move between releases. `gateway.py`'s routes still register via `@app.route(...)` decorators on thin wrappers in `__init__.py`; submodules hold the implementation bodies (HITL decision-8, refine feedback Q5).

### `gateway/gateway/` — TBD (#2261 slice-14)

Placeholder. Slice-14 of #2261 lands the decomposition of
`gateway/gateway.py` (~9,890 lines). Pre-allocated submodule
clusters per the plan:

| Submodule | Owned symbols |
|-----------|---------------|
| `_git_routes/` | TBD — `git_push`, `git_execute`, `git_fetch` (+ named helpers split out of the security-critical `git_push` mega-handler, R5 mitigation) |
| `_jira_routes/` | TBD — Jira reads, writes, validators |
| `_auth.py` | TBD — credential injection / token refresh |
| `_sessions.py` | TBD — session lifecycle |
| `_app_factory.py` | TBD — Flask app construction, middleware wiring |

The terminal slice (#2261 slice-14) replaces the TBD rows with the
concrete submodule layout once the decomposition lands.

### Other in-flight decompositions

The following gateway-side files are also under decomposition in
#2261; rows will be filled in as each slice lands:

| File | Current size | Slice |
|------|--------------|-------|
| `gateway/worktree_manager.py` | ~2,090 lines | slice-8 (#2261) |
| `gateway/git_client.py` | ~2,032 lines | slice-6 (#2261) |
| `gateway/checkpoint_handler.py` | ~1,655 lines | slice-3 (#2261) |
