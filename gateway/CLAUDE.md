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

## Decomposition seams

When a module outgrows the 1,500-line / 100 KB cap in `scripts/file-size-allowlist.yaml`, it is split into a **sub-package with an explicit re-export barrel**, per the canonical [decomposition pattern](../docs/guides/decomposition-pattern.md) ([#3312](https://github.com/jwbron/egg/issues/3312)). The `__init__.py` barrel is the **stable public API**: external importers and `unittest.mock.patch` targets resolve through it (gateway modules are imported top-level, e.g. `import git_client` / `patch("git_client._foo")`), so they survive the split. Submodules are underscore-prefixed and package-private.

**Container packaging:** `gateway/Dockerfile` ships top-level modules via the non-recursive glob `COPY gateway/*.py ./` (`gateway/Dockerfile:67`), which does **not** match a sub-package directory. Each `gateway/` decomposition therefore adds an explicit `COPY gateway/<name>/ ./<name>/` line in the same slice and smoke-checks `python -c 'import <name>'` inside the built image — source-tree lint/test stays green while a missing `COPY` would break the running image.

### `git_client/` — `git` CLI wrapper ([#3312](https://github.com/jwbron/egg/issues/3312), slice 11)

`git_client.py` (2,393 lines) → `git_client/` (7 submodules; largest `_policy.py`, 817 lines). Function-dominated `git` CLI policy layer: remote-URL handling, path/argument validation, credential-helper management, and push attribution. The barrel does explicit per-symbol re-exports and declares `__all__`, preserving the full public API; module-level `patch("git_client.<symbol>")` targets resolve through it. The `__all__` set also re-exports package-private module constants (`_CHECKOUT_FILE_FLAGS`, `_ASKPASS_SCRIPT`, `_SHA_LINE_RE`) so any `patch("git_client._…")` target survives the split.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Stable public API: per-symbol re-exports + `__all__` | re-exports of every symbol below |
| `_remote.py` | `git` executable + remote-URL parsing, SSH→HTTPS rewrite, authenticated-target resolution | `GIT_CLI`, `git_cmd`, `ssh_url_to_https`, `is_ssh_url`, `is_url_remote`, `resolve_remote_url`, `get_authenticated_remote_target` |
| `_policy.py` (largest, 817 lines) | Static allow/block policy tables (repo-path allowlist, blocked flags, allowed command/flag values, flag normalization map) | `ALLOWED_REPO_PATHS`, `REPOS_PARENT_DIRECTORIES`, `BLOCKED_GIT_FLAGS`, `ALLOWED_FLAG_VALUES`, `GIT_ALLOWED_COMMANDS`, `FLAG_NORMALIZATION`, `_CHECKOUT_FILE_FLAGS` |
| `_validation.py` | Repo-path + git-argument validation/normalization and checkout branch-switch detection | `is_repos_parent_directory`, `validate_repo_path`, `normalize_flag`, `validate_git_args`, `is_branch_switching_checkout`, `is_branch_switching_operation` |
| `_credentials.py` | Credential-helper (askpass) lifecycle + per-repo token resolution | `_ASKPASS_SCRIPT`, `create_credential_helper`, `cleanup_credential_helper`, `get_token_for_repo` |
| `_push_analysis.py` | Changed-file enumeration across a push range | `get_changed_files_in_push`, `_parse_sha_lines`, `_fetch_base_branch_best_effort`, `_fallback_base_candidates`, `_SHA_LINE_RE` |
| `_attribution.py` | Per-commit author attribution of pushed files | `AttributedFile`, `AttributedPushRange`, `get_attributed_changed_files_in_push`, `INFRA_ATTRIBUTION_ROLE`, `INFRA_COMMITTER_EMAILS` |
| `_branch_ops.py` | Branch-switch / reset-target detection + `rebase --onto` argv construction | `is_branch_switch`, `extract_reset_target_ref`, `build_rebase_onto_args` |

Pure refactor: every symbol is AST-identical to the pre-split file — no behavior change. **Dockerfile packaging:** `gateway/Dockerfile` gains `COPY gateway/git_client/ ./git_client/` (the non-recursive `COPY gateway/*.py ./` no longer matches the package dir), verified with an in-image `python -c 'import git_client'` smoke check.

This is the first `gateway/` decomposition; later gateway slices (`worktree_manager/`, `gateway/`) append their own subsections below.
