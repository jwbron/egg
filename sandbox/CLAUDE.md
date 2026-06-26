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

`contract_cli.py` (1,501 lines) → `egg_lib/contract_cli/`. Argparse command surface over the contract gateway. Each submodule exposes a `register_*_parsers(subparsers)` helper that the barrel's `create_parser()` aggregates; the subcommand registrations and `main()` stay in the barrel.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Shared gateway/env/parse helpers, parser aggregator, entrypoint | `create_parser`, `main`, `make_gateway_request`, `get_contract_identifier`, `parse_task_id`, `parse_criterion_id`, `parse_phase_id`, `validate_commit_sha` |
| `_display.py` | `show` command + contract summary rendering | `cmd_show`, `_print_contract_summary` |
| `_task_ops.py` | Task-level mutations | `cmd_add_commit`, `cmd_update_notes`, `cmd_complete_task` |
| `_phase_ops.py` | Phase + acceptance-criterion ops | `cmd_complete_phase`, `cmd_verify_criterion` |
| `_decision.py` | HITL decision creation | `cmd_add_decision`, `format_decision_markdown`, `validate_decision_id` |
| `_agent_ops.py` (largest, ~450 lines) | Agent-lifecycle commands | `cmd_agent_status`, `cmd_agent_start`, `cmd_agent_complete`, `cmd_agent_fail`, `cmd_agent_next` |
| `_feedback.py` | Open-ended feedback requests | `cmd_add_feedback` |

This is the first `sandbox/` decomposition; later sandbox slices append their own rows to this table.
