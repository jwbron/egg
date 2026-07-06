# slice-3 seam-table handoff — `gateway/gateway/` decomposition (#3312, slice-18-equivalent)

For the documenter's `gateway/CLAUDE.md` "Decomposition seams" subsection. This
documents the **landed** split (coder-owned code; CLAUDE.md is documenter-owned).

## Summary

`gateway/gateway.py` (10,648 lines / 419 KB — structural outlier, over the byte
cap) → `gateway/gateway/` sub-package (barrel + 14 submodules). Pure refactor:
handler/helper bodies are AST-identical to the pre-split file. Flask
`@app.route` decorators stay on **thin wrappers in the barrel**
(`__init__.py`); the wrapper bodies delegate to the implementation in the
`_<cluster>` submodule (the routes-handling convention). The barrel does
explicit per-symbol re-exports + declares `__all__`, so external importers and
`patch("gateway.gateway.<name>")` / `patch.object(gateway, "<name>")` targets
resolve unchanged. `gateway.py`'s allowlist entry is removed (the file-size
`files:` map now holds only `orchestrator/routes/pipelines.py`, slice-4's target).

## Submodule layout (all under both caps: ≤1500 lines / ≤100 KB)

| Submodule | Lines | Responsibility |
|-----------|-------|----------------|
| `__init__.py` (barrel) | 1342 | Flask `app`, all 49 `@app.route` thin wrappers, unhandled-exception handler, launcher-secret auth + `require_*` decorators, mount-path translation (`gateway.open` seam), `get_worktree_manager`/`get_anthropic_client`/`get_launcher_secret` seam getters, per-symbol re-exports + `__all__` |
| `__main__.py` | 15 | `python3 -m gateway` container entry point → `main()` |
| `_helpers.py` | 187 | Response builders (`make_response`/`make_error`/`make_success`/`make_worktree_not_found_error`), `audit_log`, orchestrator/squid connectivity checks, commit-observer lookup |
| `_health.py` | 253 | `/api/v1/health`, `/config/reload`, `/proxy/ca-cert` handlers + `_reload_all_config` |
| `_git_ops.py` | 1357 | `/api/v1/git/push` + `/git/fetch` implementations + detached-head hint |
| `_git_execute.py` | 800 | `/api/v1/git/execute` implementation |
| `_gh_ops.py` | 1083 | PR create/comment/edit/close, find-open-pr, list-open-prs, merge-state, ready + `_apply_pr_labels` |
| `_gh_execute.py` | 763 | `/api/v1/gh/execute` implementation |
| `_jira.py` | 847 | Jira read routes (ticket get/search/comments/remotelinks/transition/execute) + error/context helpers |
| `_jira_writes.py` | 964 | Jira write routes (ticket create/edit/comment-add, issue-link) + write validators |
| `_confluence.py` | 1230 | Confluence routes (page get/descendants/comments, space pages/list, search, execute) + space-allowlist helpers |
| `_worktree.py` | 741 | Worktree create/delete/list/prune + container-path mapping + cleanup helpers |
| `_sessions.py` | 943 | Session create/delete/heartbeat/get/update/phase, repos-visibility, sessions-list |
| `_proxy.py` | 946 | `/v1/messages` + `/v1/messages/count_tokens` Anthropic proxy, credential injection, hop/streaming logic |
| `_server.py` | 413 | `main()` + `_run_health_server` (container startup / graceful shutdown) |

## Seam-preservation notes (for the pattern narrative)

- **Barrel re-export**: every moved non-route symbol is re-exported from the
  barrel, plus an `__all__` declaring the public re-export surface; the barrel
  keeps the full gateway-sibling import block so `gateway.gateway.<sibling>`
  patch targets survive.
- **`_b()` accessor**: submodules resolve *patched* seam getters/validators
  (e.g. `get_session_manager`, `validate_repo_path`, `get_github_client`) on the
  barrel at call time via a small `_b()` helper, so `patch("gateway.gateway.X")`
  stays effective after the split. Non-patched cross-submodule helpers use
  direct typed imports (keeps mypy strict happy).
- **`_BarrelLogger` proxy**: submodule `logger` proxies to the barrel's logger
  so tests patching `gateway.logger` observe submodule log calls.
- **Module-singleton seams** (`gateway.subprocess.run`, `gateway.time.sleep`,
  `gateway.open`): kept as barrel attributes; patching the shared stdlib module
  is honoured process-wide.
- **Container packaging** (same slice): `gateway/Dockerfile` gains
  `COPY gateway/gateway/ ./gateway/`; the Flask launch moves from
  `python3 gateway.py` to `python3 -m gateway` (`__main__.py` → `main()`) in
  `gateway/entrypoint.sh`. NOTE: the in-image `python -c 'import gateway'` /
  serve-on-9848 smoke check could not run in the sandbox (no docker); the
  COPY/launch follow the git_client/worktree_manager slice-11/12 pattern.
