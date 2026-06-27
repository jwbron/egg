# slice-15 — `orchestrator/routes/signals.py` external-importer audit

File: `orchestrator/routes/signals.py` (3,398 lines / 142,839 bytes — **over the byte cap**).

## External importers (`from routes.signals import …`)

- `orchestrator/api.py` — `signals_bp`
- Tests (`test_signals.py`, `test_brc_*`, `test_conditional_ack.py`,
  `test_consensus_confirmed_idempotent.py`, `test_confirmed_producer_reopen.py`,
  `test_slice_signal_routing.py`, `test_pipeline_prompts.py`,
  `test_contract_completeness_gate.py`, `test_concurrent_integration.py`,
  `test_removal_validation_1165.py`, `test_slice_4_restart_hardening.py`,
  `test_brc_phase_propagation.py`) import, by name:
  `signals_bp`, every `handle_*_signal`, `_existing_confirmed_for_role`,
  `_resolve_pipeline_phase`, `_emit_ready_to_confirm_nudges`,
  `_resolve_reviewer_delta_range`, `_verify_commit_on_branch`,
  `_gateway_fetch_tracking_ref`, `_commit_object_resolvable`,
  `_check_branch_progress`, `_validate_tester_check_coverage`,
  `_validate_plan_proposal`, `_AGENT_ROLE_TO_CONTRACT_ROLE`,
  `_BRC_BOILERPLATE`, `_BRC_CONDITION_MIN_LEN`, `_BRC_MIN_CONTENT_LEN`,
  `_validate_brc_content`.

→ **Every public + test-imported symbol must be re-exported through the barrel.**

## `unittest.mock.patch("routes.signals.X")` seams (authoritative)

These module globals are patched, so submodule bodies must reach them via
`import routes.signals as _pkg` (`_pkg.X`) — a direct/local name would not pick
up the patch:

| Seam | Origin |
|------|--------|
| `get_state_store` (85) | module import (`state_store`) |
| `resolve_worktree_path` (48) | module import (`routes`) |
| `subprocess` (43) | module import |
| `_resolve_pipeline_phase` (43) | internal helper |
| `_gateway_fetch_tracking_ref` (29) | internal helper |
| `load_contract` (13) | module import (`egg_contracts`) |
| `_write_consensus_confirmed_marker` (11) | internal helper |
| `save_contract` (10) | module import |
| `create_orchestrator` (10) | module import |
| `_commit_object_resolvable` (5) | internal helper |
| `logger` (2) | module global |
| `save_agent_output` (1) | module import (`handoffs`) |
| `get_repo_path` (1) | module import (`routes`) |
| `_existing_confirmed_for_role` (1) | internal helper |
| `DecisionStatus` (1, `create=True`) | **lazy** import inside `handle_consensus_excuse_producer_signal` — kept verbatim; the create=True patch targets the package module attribute, the function's local `from models import DecisionStatus` is unaffected (identical to pre-split). |

## Decision

- Uniform rule for moved bodies: reference every pre-split module global that is
  a **patched seam** or an **internal cross-module helper** via `_pkg.` so the
  package attribute (and any patch on it) resolves. Confirmed: no patched/helper
  name is lazily imported inside a body (so no unused-import shadowing) and none
  is used as a default-arg value (so no import-time `_pkg` access).
- Co-located constants stay direct (no `_pkg`): `_BRC_*` (only
  `_validate_brc_content`), `_ARTIFACT_HUMAN_LABEL` (only `_artifact_human_label`),
  `_SIGTERM_PATTERN` (only `_is_sigterm_after_completion`).
- `make_error_response` / `make_success_response` are **not** patched → imported
  directly from `._responses`.
- Routes convention (decision-8): the two `@signals_bp.route` decorators
  (`handle_signal`, `handle_batch_signals`) stay on thin wrappers in the barrel;
  bodies move to `_dispatch.py`.
