# Gateway

Policy-enforcement sidecar that sits between agents and GitHub. Validates git/gh operations, enforces phase restrictions, and injects credentials.

- **[README.md](README.md)** — architecture, policy rules, configuration
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **[../docs/architecture/upstream-routing.md](../docs/architecture/upstream-routing.md)** — `UpstreamRegistry` seam, LiteLLM topology, per-session routing decision, and the no-op-by-default invariant for non-Claude agent backends ([#2769](https://github.com/jwbron/egg/issues/2769))

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Submodule seam tables

Several of the gateway's largest source files are being decomposed into sub-packages so each module fits the 1,500-line / 100 KB cap from `scripts/file-size-allowlist.yaml`. The canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) is documented in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). The tables below map each decomposed module to its current submodule layout so contributors can find code without scanning the barrel.

The barrel `__init__.py` is the **stable public API** (HITL decision-7 of #2817). External consumers — tests, production importers, mocks — keep importing through the barrel (`from gateway.gateway import get_anthropic_client`); the submodule paths below are package-private and may move between releases. `gateway.py`'s routes still register via `@app.route(...)` decorators on thin wrappers in `__init__.py`; submodules hold the implementation bodies (HITL decision-8, refine feedback Q5).

### `gateway/gateway/` — TBD (#2817 slices 8–12)

Placeholder. Slices 8–12 of #2817 land the decomposition of
`gateway/gateway.py` (~10,690 lines) as a linear chain. Slice-8 lands
the step-0 baseline + flat clusters (`_app_factory.py`, `_auth.py`,
`_checkpoint_routes.py`, `_gh_routes.py`); slices 9–10 each extract one
sub-sub-package (`_git_routes/`, then `_jira_routes/`); slice-11
extracts three (`_confluence_routes/`, `_worktree_routes/`, and
`_anthropic_proxy/`); slice-12 is terminal — it extracts `_sessions.py`
and drops the allowlist entry. Pre-allocated submodule clusters per
the plan (trailing `/` denotes a sub-sub-package, `.py` denotes a flat
module):

| Submodule | Owned symbols |
|-----------|---------------|
| `_git_routes/` | TBD — `git_push`, `git_execute`, `git_fetch` (+ named helpers split out of the security-critical `git_push` mega-handler) |
| `_jira_routes/` | TBD — Jira reads, writes, validators |
| `_confluence_routes/` | TBD — Confluence reads, writes |
| `_worktree_routes/` | TBD — worktree lifecycle endpoints |
| `_anthropic_proxy/` | TBD — Anthropic API proxy + UpstreamRegistry routing |
| `_checkpoint_routes.py` | TBD — checkpoint endpoints |
| `_gh_routes.py` | TBD — GitHub passthrough endpoints |
| `_auth.py` | TBD — credential injection / token refresh |
| `_sessions.py` | TBD — session lifecycle |
| `_app_factory.py` | TBD — Flask app construction, middleware wiring |

The terminal slice (#2817 slice-12) replaces the TBD rows with the
concrete submodule layout once the decomposition lands.

### Other in-flight decompositions

The following gateway-side files are also under decomposition in
#2817; rows will be filled in as each slice lands:

| File | Current size | Slice |
|------|--------------|-------|
| `gateway/worktree_manager.py` | ~2,087 lines | slice-20 (#2817) |
| `gateway/git_client.py` | ~2,068 lines | slice-21 (#2817) |
| `gateway/checkpoint_handler.py` | ~1,777 lines | slice-26 (#2817) |
