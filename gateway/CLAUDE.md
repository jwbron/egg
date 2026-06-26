# Gateway

Policy-enforcement sidecar that sits between agents and GitHub. Validates git/gh operations, enforces phase restrictions, and injects credentials.

- **[README.md](README.md)** — architecture, policy rules, configuration
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **[../docs/architecture/upstream-routing.md](../docs/architecture/upstream-routing.md)** — `UpstreamRegistry` seam, LiteLLM topology, per-session routing decision, and the no-op-by-default invariant for non-Claude agent backends ([#2769](https://github.com/jwbron/egg/issues/2769))

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Module layout

The gateway is a flat package of single-file modules. `gateway.py` holds the Flask app and the `@app.route(...)` REST handlers; the modules below hold the policy, client, and session logic those handlers call into. Import symbols from the module that owns them (e.g. `from gateway.session_manager import SessionManager`).

| Module | Responsibility |
|--------|----------------|
| `gateway.py` | Flask app, REST route handlers for policy-enforced git/gh/Jira/Confluence operations |
| `policy.py` | Ownership and access-control checks (branch ownership, PR create/edit/comment rules) |
| `phase_filter.py` | Phase-specific operation restrictions (refine/plan/implement/pr permit/block lists) |
| `agent_restrictions.py` | Per-role file-write boundaries enforced on push |
| `git_client.py` | `git` CLI wrapper: path/argument validation, credential-helper management |
| `github_client.py` | `gh` CLI wrapper: token management (bot/user modes), command + API-path validation |
| `worktree_manager.py` | Git worktree lifecycle, orphan cleanup, container-to-worktree mapping |
| `session_manager.py` | Per-container session storage (thread-safe, disk-persisted), repo-mode binding |
| `token_refresher.py` | GitHub token refresh |
| `jira_client.py` / `jira_policy.py` | Jira REST client and write-policy checks for `/api/v1/jira/*` routes |
| `confluence_client.py` | Read-only Confluence REST client |
| `routing_policy.py` / `repo_parser.py` / `repo_visibility.py` / `private_repo_policy.py` | Upstream routing, repo identity parsing, and visibility/private-repo policy |
| `rate_limiter.py` / `commit_observer.py` / `phase_api.py` | Rate limiting, commit observation, and the phase-state API surface |

When a module outgrows the 1,500-line / 100 KB cap in `scripts/file-size-allowlist.yaml`, decompose it into a sub-package following the canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). Once decomposed, the barrel `__init__.py` becomes the stable public API: external consumers import through the barrel while submodule paths stay package-private.
