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

When a module outgrows the 1,000-code-line cap in `scripts/file-size-allowlist.yaml`, it is split into a **sub-package with an explicit re-export barrel**, per the canonical [decomposition pattern](../docs/guides/decomposition-pattern.md) ([#3312](https://github.com/jwbron/egg/issues/3312)). The `__init__.py` barrel is the **stable public API**: external importers and `unittest.mock.patch` targets resolve through it (gateway modules are imported top-level, e.g. `import git_client` / `patch("git_client._foo")`), so they survive the split. Submodules are underscore-prefixed and package-private.

Line and byte figures in the slice records below are **the measurements that were in force when each split landed** — most predate the #3671 re-baseline onto code lines (and its loose 150 KB byte backstop), so a "over the byte cap" note there describes the then-current 100 KB cap, not today's. `scripts/file-size-allowlist.yaml` is the live source of truth for both the caps and which files are currently exempt.

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

### `worktree_manager/` — Git worktree lifecycle ([#3312](https://github.com/jwbron/egg/issues/3312), slice 12)

`worktree_manager.py` (2,507 lines, 106,355 bytes — over the byte cap) → `worktree_manager/` (10 submodules; largest `_git_ops.py`, ~970 lines). Class-dominated: a single `WorktreeManager` (27 methods) plus module-level dataclasses, validators, and startup entry points. The split uses the **method-modules-on-class** shape — each submodule holds a cluster of `WorktreeManager` method implementations that the barrel binds onto the class, so the public class object is unchanged and `patch("worktree_manager.WorktreeManager._foo")` targets resolve through it. The barrel does explicit per-symbol re-exports and declares `__all__`, keeping `WorktreeManager`, the `WorktreeInfo` / `WorktreeRemovalResult` dataclasses, the `validate_identifier` / `validate_branch_ref` validators, the `startup_cleanup` / `get_active_docker_containers` entry points, and the `WORKTREE_BASE_DIR` / `REPOS_BASE_DIR` constants as the stable public API.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Stable public API: `WorktreeManager` class + per-symbol re-exports + `__all__`; binds the method-modules onto the class | `WorktreeManager`, `WorktreeInfo`, `WorktreeRemovalResult`, `validate_identifier`, `validate_branch_ref`, `startup_cleanup`, `get_active_docker_containers`, `WORKTREE_BASE_DIR`, `REPOS_BASE_DIR` |
| `_lifecycle.py` | Worktree creation, default-branch resolution, and lookup | `__init__`, `resolve_default_branch`, `create_worktree`, `lookup_worktree` |
| `_git_ops.py` (largest, ~970 lines) | `git` plumbing for worktree add: push-upstream config, credential env, assigned fork-point resolution, safe-ref reset of reused worktrees, `git worktree add` invocation | `_configure_push_upstream`, `_git_credential_env`, `_resolve_assigned_fork_point`, `_reset_reused_worktree_to_safe_ref`, `_run_git_worktree_add`, `_tracking_refspec` |
| `_filesystem.py` | Ownership fixups and git-dir discovery | `_chown_single`, `_chown_recursive`, `_find_worktree_git_dir` |
| `_phase.py` | Phase-scoped worktree create/cleanup | `create_phase_worktree`, `cleanup_phase_worktrees` |
| `_removal.py` | Worktree + branch removal and clean-worktree teardown | `remove_worktree`, `_delete_worktree_branch`, `cleanup_clean_worktree` |
| `_listing.py` | Worktree enumeration and container/repo path resolution | `list_worktrees`, `list_worktrees_for_pipeline`, `get_worktree_paths` |
| `_orphan_mgmt.py` (~629 lines) | Orphan/stale cleanup, prune, and pack-file GC | `cleanup_orphaned_worktrees`, `prune_stale_worktrees`, `git_worktree_prune_all`, `list_orphan_worktree_dirs`, `cleanup_orphaned_pack_files`, `cleanup_stale_pipeline_worktrees`, `_is_pipeline_anchored` |
| `_session.py` | Per-repo lock acquisition (container-to-worktree mapping serialization) | `_get_repo_lock` |
| `_validation.py` | Identifier + branch-ref validation (re-exported through the barrel) | `validate_identifier`, `validate_branch_ref` |
| `_startup.py` | Startup-cleanup entry point + active-container discovery + byte formatting | `startup_cleanup`, `get_active_docker_containers`, `_format_bytes` |

Pure refactor: every symbol is AST-identical to the pre-split file — no behavior change. **Dockerfile packaging:** `gateway/Dockerfile` gains `COPY gateway/worktree_manager/ ./worktree_manager/` (the non-recursive `COPY gateway/*.py ./` no longer matches the package dir), verified with an in-image `python -c 'import worktree_manager'` smoke check.

> Seam table documents the slice-12 target layout (per the architect slice goal); finalized per-submodule symbol placement tracks the coder's landed decomposition and is retagged to the shipped layout on the post-landing doc pass.

### `gateway/gateway/` — Flask app + REST route handlers ([#3312](https://github.com/jwbron/egg/issues/3312), slice 18)

`gateway.py` (10,648 lines, 419 KB — the structural outlier, over both the line and byte caps) → `gateway/gateway/` (barrel + 14 submodules; largest `_git_ops.py`, 1,357 lines). This is the Flask app and the policy-enforced `git`/`gh`/Jira/Confluence/session/proxy REST surface. The split follows the **routes-handling convention**: all 50 `@app.route(...)` decorators stay on thin wrapper functions in the `__init__.py` barrel, and each wrapper delegates to an implementation function in a responsibility-grouped `_<cluster>.py` submodule — so the URL→handler map and the `gateway.gateway` import path are unchanged. The barrel does explicit per-symbol re-exports and declares `__all__`, keeping the full public API (`app`, `main`, `GitHubClient`, `WorktreeManager`, the policy constants/enums, and every helper) as the stable surface, so `patch("gateway.gateway.<symbol>")`, `patch.object(gateway, "<symbol>")`, and `monkeypatch.setattr` targets across the ~35 referencing files resolve unchanged. Cross-submodule seams are resolved on the barrel at call time via a `_b()` accessor; a `_BarrelLogger` proxy forwards `gateway.logger`; and module-singleton seams (`subprocess.run` / `time.sleep` / `open`) stay barrel attributes so their patch targets survive the split.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Flask `app`, all 50 `@app.route` thin wrappers, `main` bootstrap; per-symbol re-exports + `__all__`; `_b()` call-time seam resolution, `_BarrelLogger`, module-singleton seam attrs | `app`, `main`, re-exports of every symbol below |
| `__main__.py` | `python3 -m gateway` entry-point shim (replaces `python3 gateway.py`) → calls barrel `main()` | `main` (imported) |
| `_helpers.py` | JSON response/error builders, audit logging, orchestrator/squid connectivity checks, barrel accessor + logger proxy | `make_response`, `make_error`, `make_success`, `make_worktree_not_found_error`, `audit_log`, `_check_orchestrator_connectivity`, `_check_squid_health`, `_b`, `_BarrelLogger` |
| `_health.py` | Health-check + config-reload endpoints, proxy CA-cert exposure | `health_check`, `config_reload`, `get_proxy_ca_cert`, `_reload_all_config` |
| `_git_ops.py` (largest, 1,357 lines) | Policy-enforced `git push` / `git fetch` (attribution, upstream config, detached-head hinting) | `git_push`, `git_fetch`, `_detached_head_hint` |
| `_git_execute.py` | Generic validated `git` command execution | `git_execute` |
| `_gh_ops.py` | `gh` PR lifecycle: create/comment/edit/close, open-PR lookup, merge-state/ready checks, label application | `gh_pr_create`, `gh_pr_comment`, `gh_pr_edit`, `gh_pr_close`, `gh_find_open_pr`, `gh_list_open_prs`, `gh_pr_merge_state`, `gh_pr_ready`, `_apply_pr_labels` |
| `_gh_execute.py` | Generic validated `gh` command execution | `gh_execute` |
| `_jira.py` | Jira read routes + orchestrator-authorized transition | `jira_ticket_get`, `jira_search`, `jira_ticket_comments`, `jira_ticket_remotelinks`, `jira_ticket_transition`, `jira_execute` |
| `_jira_writes.py` | Jira write routes + key/text/label validation | `jira_ticket_create`, `jira_ticket_edit`, `jira_ticket_comment_add`, `jira_issue_link_create`, `_validate_jira_write_keys` |
| `_confluence.py` | Read-only Confluence proxy: space-key resolution, limit clamping, page-id/space validation, upstream-error shaping | `_resolve_space_key_for_payload`, `_confluence_clamp_limit`, `_validate_confluence_page_id`, `_validate_confluence_space_key`, `_confluence_error_from_upstream` |
| `_worktree.py` | Worktree REST ops + container-path→worktree mapping, stale-pack/dir cleanup | `map_container_path_to_worktree`, `worktree_create`, `worktree_delete`, `worktree_list`, `worktrees_prune` |
| `_sessions.py` | Session lifecycle, heartbeats, phase updates, repo-visibility, session listing | `session_create`, `session_delete`, `session_get`, `session_heartbeat`, `session_update`, `session_update_phase`, `repos_visibility`, `sessions_list` |
| `_proxy.py` | Upstream LLM proxy: route-chain resolution, credential/attribution injection, hop preparation + streaming | `_prepare_hop`, `_inject_upstream_credentials`, `_inject_anthropic_credentials`, `_resolve_route_chain`, `_with_attribution_headers`, `_PreparedHop` |
| `_server.py` | Server bootstrap `main()` + background health-server thread | `main`, `_run_health_server` |

Pure refactor: every implementation body is AST-identical to the pre-split file — no behavior change. **Container packaging + launch (R3):** `gateway/Dockerfile` gains `COPY gateway/gateway/ ./gateway/` (the non-recursive `COPY gateway/*.py ./` no longer matches the package dir), and because a package cannot be run as `python3 gateway.py`, the launch becomes `python3 -m gateway` (via `gateway/gateway/__main__.py` → barrel `main()`) in `gateway/entrypoint.sh`; the Flask server still binds port 9848 unchanged.

`git_client/` was the first `gateway/` decomposition; `gateway/gateway/` (this slice) completes the gateway-package split.
