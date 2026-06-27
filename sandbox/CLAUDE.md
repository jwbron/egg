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

This is the first `sandbox/` decomposition; later sandbox slices append their own rows to this table.
