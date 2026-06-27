# Sandbox

Untrusted agent container. Provides the isolated execution environment where Claude Code runs, with tools, entrypoint scripts, and Claude Code configuration.

- **[README.md](README.md)** — container setup, environment variables, tool inventory
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **`agent-config/rules/`** — Claude Code rules injected into sandboxed agents (not relevant for local development)

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Decomposition seams

Oversized `sandbox/` modules are split into **sub-packages with an explicit re-export barrel**, per the canonical [decomposition pattern](../docs/guides/decomposition-pattern.md) ([#3312](https://github.com/jwbron/egg/issues/3312)). The `__init__.py` barrel is the **stable public API**: external importers and `unittest.mock.patch` targets resolve through it (`patch("egg_lib.contract_cli._foo")`), so they survive the split. Submodules are underscore-prefixed and package-private.

### `egg_lib/contract_cli/` — `egg-contract` CLI ([#3312](https://github.com/jwbron/egg/issues/3312), slice 1)

`contract_cli.py` (1,501 lines) → `egg_lib/contract_cli/` (largest submodule 480 lines). Argparse command surface over the contract gateway. `create_parser()` (builds the subparser surface inline and wires each subcommand to its `cmd_*` handler) and `main()` live in the barrel; the barrel does explicit per-symbol re-exports and declares `__all__`, preserving the full public API.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Stable public API: builds the CLI (`create_parser`) + entrypoint (`main`); per-symbol re-exports + `__all__` | `create_parser`, `main` (+ re-exports of all symbols below) |
| `_errors.py` | Gateway/handler error-type shim | `GatewayError`, `HandlerError` |
| `_config.py` | Env/config getters, contract-id resolution, id parsers, validators | `get_gateway_url`, `get_issue_number`, `get_pipeline_id`, `get_contract_identifier`, `get_repo_path`, `get_session_token`, `get_container_id`, `parse_task_id`, `parse_criterion_id`, `parse_phase_id`, `validate_commit_sha` |
| `_gateway.py` | Gateway HTTP request + legacy error rendering | `make_gateway_request`, `_render_gateway_error_and_exit` |
| `_decisions.py` | HITL decision validation + markdown | `validate_decision_id`, `format_decision_markdown` |
| `_commands.py` | Contract/task/phase/criterion/decision/feedback subcommands | `cmd_show`, `cmd_add_commit`, `cmd_update_notes`, `cmd_complete_task`, `cmd_complete_phase`, `cmd_verify_criterion`, `cmd_add_decision`, `cmd_add_feedback` |
| `_agent_commands.py` (largest, 480 lines) | Multi-agent lifecycle subcommands | `cmd_agent_status`, `cmd_agent_start`, `cmd_agent_complete`, `cmd_agent_fail`, `cmd_agent_next` |

Pure refactor: every symbol is AST-identical to the pre-split file. Module-level `patch("egg_lib.contract_cli._foo")` targets resolve through the barrel; tests that patch a helper **where it is called** target the caller's submodule (e.g. `get_session_token` → `._gateway`, `get_contract_identifier` → `._commands`).

This is the first `sandbox/` decomposition; later sandbox slices append their own subsections below.

### `entrypoint/` — container entrypoint ([#3312](https://github.com/jwbron/egg/issues/3312), slice 9)

`entrypoint.py` (2,212 lines) → `entrypoint/` (largest submodule `_claude.py` 348 lines). Container startup: user/UID-GID setup, git/CA/Anthropic config, worktrees, Claude Code config, gateway-readiness gate, then `--exec`/orchestrator-mode launch. `main()` — the sequential, timing-instrumented setup orchestration — lives in the barrel so that `patch("entrypoint.setup_user")` (and the other re-exported setup helpers) reaches the call site; the barrel does explicit per-symbol re-exports and declares `__all__`, preserving the full public API.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Stable public API: sequential setup orchestration (`main`); per-symbol re-exports + `__all__` | `main` (+ re-exports of all symbols below) |
| `__main__.py` | `python3 -m entrypoint` console dispatch → `entrypoint.main()` (replaces the single-file `COPY`/`ENTRYPOINT`; see Dockerfile) | module entry |
| `_core.py` | Leaf helpers + earliest-captured start constant | `run_cmd`, `chown_recursive` |
| `_config.py` | Container `Config` dataclass + `Logger` | `Config`, `Logger` |
| `_timing.py` | Startup-timing instrumentation | `StartupTimer`, `timed_phase` |
| `_user.py` | User/UID/GID + repo-permission setup | `setup_user`, `setup_repo_permissions`, `_find_free_uid`, `_resolve_uid_conflict`, `_resolve_gid_conflict` |
| `_worktrees.py` | Worktree validation, prebuilt-deps restore, `~/egg` symlink | `setup_worktrees`, `restore_prebuilt_deps`, `setup_egg_symlink` |
| `_environment.py` | Environment, git, gateway-CA, Anthropic-API setup | `setup_environment`, `setup_git`, `setup_gateway_ca`, `setup_anthropic_api` |
| `_exec.py` | Subprocess exec path with stderr capture + CWD handling | `run_exec`, `_chdir_to_single_repo`, `_exclude_from_git` |
| `_claude.py` (largest, 348 lines) | Claude Code config, agent-rules, bashrc setup | `setup_claude`, `setup_agent_rules`, `setup_bashrc` |
| `_gateway_health.py` | Gateway readiness / network-lockdown health check | `check_gateway_health` |
| `_completion.py` | Orchestrator completion signalling + exit cleanup | `signal_orchestrator_completion`, `cleanup_on_exit` |
| `_command_timeout.py` | Bash command-timeout wrapper installation | `setup_command_timeout` |

Pure refactor: every symbol is AST-identical to the pre-split file — no behavior change. **Dockerfile packaging:** the source file became a sub-package, so the single-file `COPY sandbox/entrypoint.py /usr/local/bin/entrypoint.py` + `ENTRYPOINT entrypoint.py` was replaced by running the package over `PYTHONPATH` (`/opt/egg-runtime/sandbox`) via `ENTRYPOINT ["python3", "-m", "entrypoint"]`; `entrypoint/__main__.py` dispatches to `entrypoint.main()`. Module-level `patch("entrypoint._foo")` targets resolve through the barrel; tests that patch a setup helper called from `main()` (e.g. `patch("entrypoint.setup_git")`) hit the barrel namespace where `main()` references it.

### `egg_lib/orch_cli/` — `egg-orch` CLI ([#3312](https://github.com/jwbron/egg/issues/3312), slice 17)

`orch_cli.py` (5,012 lines / 190,656 bytes — **over BOTH the line and byte cap**) → `egg_lib/orch_cli/` (largest submodule `_parser.py`, 1,355 lines — under the 1,500-line cap; the soft-cap warning is non-fatal and precedented by slice-15/16). Argparse command surface over the orchestrator + gateway APIs, grouped by **subcommand family**. `main()` lives in the barrel; the barrel does explicit per-symbol re-exports and declares `__all__`, preserving the full public API.

**Entry point (path-style, NOT a Dockerfile change):** `bin/egg-orch` was a symlink to the single-file `orch_cli.py`. Because the module became a package, direct execution of `__init__.py` would break relative imports, so the slice adds `orch_cli/__main__.py` (a thin path-fixup shim that prepends the sandbox root to `sys.path` and resolves `main` through the barrel, mirroring `scripts/select_tests/__main__.py`) and repoints the `bin/egg-orch` symlink at `../egg_lib/orch_cli/__main__.py`. **Container packaging is neutral** — unlike the `orchestrator/`/`gateway/` slices (non-recursive `COPY *.py` globs) and unlike `entrypoint/` (single-file `COPY` + `ENTRYPOINT`), the sandbox image ships `egg_lib/` via the **recursive** `COPY . /opt/egg-runtime/` (Dockerfile:361) with `PYTHONPATH=/opt/egg-runtime/sandbox`, so `egg_lib/orch_cli/` is auto-included; `chmod +x …/bin/*` (Dockerfile:362) keeps the repointed symlink executable. No Dockerfile edit required.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel, 324 lines) | Stable public API: eager submodule imports + explicit per-symbol re-exports + `main()` + `__all__`. Re-exports the two live patch seams `orch_request` / `get_agent_role_from_env` so `patch("egg_lib.orch_cli.<seam>")` resolves | `main`, `create_parser`, `orch_request`, `get_agent_role_from_env`, + the full `cmd_*` surface |
| `__main__.py` (33 lines) | Path-style entry shim for the `egg-orch` symlink; `sys.path` fixup → `from egg_lib.orch_cli import main` | `main` (re-resolved) |
| `_http.py` (325 lines) | HTTP/transport infra: API request helpers, URL/env/token resolution, ID validation, JSON output, the `_opener` + `_SAFE_ID_PATTERN`/`_SLICE_ID_PATTERN` globals. **Defines** both patch seams | `ApiError`, `api_request`, `api_request_or_exit`, `orch_request`, `gateway_request`, `get_agent_role_from_env`, `get_session_token`, `validate_id`, `require_pipeline_id`, `print_json`, `resolve_slice_id` |
| `_common.py` (235 lines) | Shared CLI helpers: role resolution + handler-error rendering + prose-arg plumbing (stdin/file channels) | `_require_role`, `_render_handler_error`, `_ProseArgError`, `_emit_argv_prose_deprecation`, `_resolve_prose_arg`, `_resolve_files_reviewed_arg` |
| `_health.py` (234 lines) | Health + gateway-introspection subcommands | `cmd_health`, `cmd_gateway_health`, `cmd_gateway_phase`, `cmd_gateway_permissions`, `cmd_health_alerts`, `cmd_health_resolve` |
| `_pipeline.py` (306 lines) | Pipeline-lifecycle subcommands + wait-status terminal-state constants | `cmd_pipeline_list/get/create/status/delete/wait_status`, `_WAIT_STATUS_TERMINAL_STATUSES`, `_WAIT_STATUS_TO_EVENT_TYPE` |
| `_signal.py` (169 lines) | Signal subcommands | `cmd_signal_complete/progress/error/heartbeat/readiness` |
| `_phase.py` (139 lines) | Phase subcommands | `cmd_phase_get/advance/start/complete/get_context` |
| `_decision.py` (134 lines) | Decision-queue subcommands | `cmd_decision_list/create/resolve/status` |
| `_container.py` (144 lines) | Container subcommands | `cmd_container_list/spawn/get/stop/logs` |
| `_message.py` (684 lines) | Inter-agent message subcommands + wait-cursor file helpers | `cmd_message_send/poll/wait/wait_loop/heartbeat/status`, `_wait_cursor_path`, `_read_cursor_file`, `_write_cursor_file`, `_delete_cursor_file`, `_classify_gateway_error_rc`, `_resolve_from_producer_arg` |
| `_overseer.py` (538 lines) | Overseer subcommands + label/limit constants | `cmd_overseer_alert/file_issue/consult_advisor`, `_OVERSEER_TITLE_MAX_CHARS`, `_OVERSEER_BODY_MAX_BYTES`, `_OVERSEER_VALID_LABEL_PRIORITIES` |
| `_consensus.py` (541 lines) | BRC consensus subcommands | `cmd_consensus_propose/ack/nack/withdraw/confirmed/status`, `_consensus_push`, `_render_stale_version_rejection` |
| `_brc.py` (265 lines) | BRC inspection subcommands + valid-phase constant | `cmd_brc_next_action/get_state/list_blocking/resolve_obligation/read_peer_artifact`, `_VALID_BRC_HISTORY_PHASES` |
| `_progress.py` (142 lines) | Progress/env subcommands | `cmd_env`, `cmd_progress_emit`, `cmd_progress_query` |
| `_parser.py` (largest, 1,355 lines) | argparse wiring: `create_parser()` builds the full subparser tree. Dispatch targets resolve through the barrel (`func=_pkg.cmd_*`) | `create_parser`, `_add_json_flag`, `_non_negative_int` |

Pure refactor, no behavior change: all 4,315 non-blank code lines of the pre-split body reappear verbatim across the submodules once the `_pkg.` barrel-indirection is reversed. **Patch seams preserved:** the two seams patched in the suite — `orch_request` (`patch("egg_lib.orch_cli.orch_request")` in `test_phase_cli`/`test_brc_cli_args`, `monkeypatch.setattr` in `test_cli_session_state`) and `get_agent_role_from_env` (`patch("egg_lib.orch_cli.get_agent_role_from_env")` in `test_message_wait_cli`) — are **defined in `_http.py`, re-exported on the barrel, and reached from every command submodule via `import egg_lib.orch_cli as _pkg` (live barrel-attribute lookup)**, so the barrel-level patches keep intercepting. Every externally-imported symbol (`cmd_consensus_*`, `cmd_phase_*`, `cmd_overseer_*`, `cmd_message_poll`, `create_parser`, `_render_stale_version_rejection`, `_wait_cursor_path`, the `_OVERSEER_*` constants, …) is re-exported through the barrel, so `cli_session_state.py`, `session_state_sync.py`, the `egg_agent_tools.handlers`/`push` modules, and the test suite need **zero edits**.
