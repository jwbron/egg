# slice-1 external-importer audit — sandbox/egg_lib/contract_cli.py

Recipe: docs/guides/decomposition-pattern.md section (d). The module is
imported as `egg_lib.contract_cli` (PYTHONPATH=sandbox). Only **tests** are
runtime consumers; the `egg_agent_tools` hits are docstring/comment mentions
only (no runtime import). `sandbox/bin/egg-contract` is a standalone *copy*
of the script (not an importer), so it is unaffected by this decomposition.

## Top-level symbols and external-reference status

| Symbol | Kind | External refs | Re-export? |
|--------|------|---------------|------------|
| `COMMIT_SHA_PATTERN` | const | none | internal (used by `validate_commit_sha`); keep importable via barrel for safety → yes |
| `get_gateway_url` | func | test_contract_cli | **yes** |
| `get_issue_number` | func | test_contract_cli | **yes** |
| `get_pipeline_id` | func | test_contract_cli | **yes** |
| `get_contract_identifier` | func | test_contract_cli, test_cli_parity (patch) | **yes** |
| `get_repo_path` | func | test_contract_cli | **yes** |
| `get_session_token` | func | test_contract_cli (patch) | **yes** |
| `get_container_id` | func | test_contract_cli | **yes** |
| `_container_id_field` | func | none (package-internal) | no |
| `parse_task_id` | func | test_contract_cli, test_plan_parser | **yes** |
| `parse_criterion_id` | func | test_contract_cli | **yes** |
| `parse_phase_id` | func | test_contract_cli | **yes** |
| `validate_commit_sha` | func | test_contract_cli | **yes** |
| `make_gateway_request` | func | test_contract_cli | **yes** |
| `_render_gateway_error_and_exit` | func | none (package-internal) | no |
| `cmd_show` | func | (parser dispatch) | **yes** |
| `_print_contract_summary` | func | test_contract_cli (`from ... import _print_contract_summary`) | **yes** (underscore but externally imported) |
| `cmd_add_commit` | func | (parser dispatch) | **yes** |
| `cmd_update_notes` | func | (parser dispatch) | **yes** |
| `cmd_complete_task` | func | test_cli_parity | **yes** |
| `cmd_complete_phase` | func | (parser dispatch) | **yes** |
| `validate_decision_id` | func | test_contract_cli | **yes** |
| `format_decision_markdown` | func | test_contract_cli, test_hitl_integration | **yes** |
| `cmd_verify_criterion` | func | (parser dispatch) | **yes** |
| `cmd_add_decision` | func | test_cli_parity | **yes** |
| `VALID_AGENT_ROLES` | const | (parser choices) | **yes** |
| `VALID_AGENT_STATUSES` | const | none | yes (public const) |
| `cmd_agent_status` | func | (parser dispatch) | **yes** |
| `cmd_agent_start` | func | (parser dispatch) | **yes** |
| `cmd_agent_complete` | func | (parser dispatch) | **yes** |
| `cmd_agent_fail` | func | (parser dispatch) | **yes** |
| `cmd_agent_next` | func | (parser dispatch) | **yes** |
| `cmd_add_feedback` | func | test_cli_parity | **yes** |
| `create_parser` | func | test_contract_cli, test_mcp_cli_drift | **yes** |
| `main` | func | test_contract_cli, test_cli_parity | **yes** |
| `GatewayError`/`HandlerError` | class shim | imported from egg_agent_tools.handlers.errors (re-exported locally as fallback) | barrel re-exports for back-compat |

## Re-export set (barrel public API)

Re-export **all** symbols above except the two package-internal underscore
helpers (`_container_id_field`, `_render_gateway_error_and_exit`), which are
referenced only within the sub-package and are imported directly between
submodules. The single externally-imported underscore symbol
(`_print_contract_summary`) IS re-exported.

## External patch targets that must keep resolving through the barrel

- `patch("egg_lib.contract_cli.get_contract_identifier")` (test_cli_parity, test_contract_cli)
- `patch("egg_lib.contract_cli.get_session_token")` (test_contract_cli)
