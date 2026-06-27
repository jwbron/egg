# Handoff: sandbox/CLAUDE.md seam table (coder -> documenter)

The contract_cli.py decomposition (slice-1) needs a "Decomposition seams"
section in `sandbox/CLAUDE.md`. `sandbox/CLAUDE.md` is documenter-owned
(coder is blocked from it by shared/egg_restrictions/patterns.py), so the
coder cannot author it. Suggested content to append after the "## Testing"
section (first sandbox/ slice stands up the table; later sandbox slices
append rows):

---
## Decomposition seams

When a sandbox module outgrows the 1,500-line / 100 KB cap tracked in `scripts/file-size-allowlist.yaml`, decompose it into a sub-package following the canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). The barrel `__init__.py` is the stable public API: external consumers and test patch targets import through the barrel (`from egg_lib.contract_cli import …`, `patch("egg_lib.contract_cli.<symbol>")`) while submodule paths stay package-private. The sub-package reaches the sandbox image automatically — `sandbox/Dockerfile` does a recursive `COPY . /opt/egg-runtime/` with `PYTHONPATH=/opt/egg-runtime/sandbox`, so there is no per-file COPY glob to update.

Decomposed sandbox sub-packages ([#3312](https://github.com/jwbron/egg/issues/3312)):

| Sub-package | Barrel re-exports | Submodules |
|-------------|-------------------|------------|
| `egg_lib/contract_cli/` (`egg-contract` CLI) | all public `cmd_*`, parsers/validators, `make_gateway_request`, `create_parser`, `main`, `GatewayError`/`HandlerError`, plus `_print_contract_summary` | see below |

`egg_lib/contract_cli/` submodule layout:

| Submodule | Responsibility |
|-----------|----------------|
| `__init__.py` | Re-export barrel + `create_parser` (argparse wiring) + `main` entry point |
| `_errors.py` | `GatewayError` / `HandlerError` shim (re-export from `egg_agent_tools.handlers.errors` with local fallback) |
| `_config.py` | Env/config getters (`get_gateway_url`, `get_session_token`, …), id parsers (`parse_task_id`/`parse_criterion_id`/`parse_phase_id`), `validate_commit_sha`, `COMMIT_SHA_PATTERN` |
| `_gateway.py` | `make_gateway_request` + the legacy `_render_gateway_error_and_exit` renderer |
| `_decisions.py` | HITL decision id validation + `format_decision_markdown` |
| `_commands.py` | Contract/task/phase/criterion/decision/feedback `cmd_*` handlers (incl. `_print_contract_summary`) |
| `_agent_commands.py` | Multi-agent orchestration `cmd_agent_*` handlers + role/status constants |

> Note: `sandbox/bin/egg-contract` is a standalone copy of the CLI script (not an importer of `egg_lib.contract_cli`), so it is unaffected by this decomposition.
---
