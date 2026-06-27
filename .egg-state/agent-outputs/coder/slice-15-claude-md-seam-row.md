# slice-15 → documenter handoff: `orchestrator/CLAUDE.md` seam row

The coder cannot write `orchestrator/CLAUDE.md` (documenter-owned path). Please
apply the change below to `orchestrator/CLAUDE.md`.

## 1. Insert this new subsection

Place it in the "## Decomposition seams" section, immediately **before** the
final summary paragraph that begins ``` `routes/decisions/`, `state_store/`, … ```
(i.e. right after the `### kubernetes_spawner/` subsection, slice 14):

```markdown
### `routes/signals/` — sandbox-callback + BRC consensus signal endpoints ([#3312](https://github.com/jwbron/egg/issues/3312), slice 15)

`signals.py` (3,398 lines / 142,839 bytes — **over the byte cap**) → `routes/signals/` (largest submodule `_consensus_verdicts.py`, 1,075 lines / 47KB). The `signals_bp` blueprint plus its two `@signals_bp.route` thin wrappers (`handle_signal`, `handle_batch_signals`) stay in the barrel (decision-8); each delegates to a body in `_dispatch.py`. The barrel keeps the patched module-level seams (`get_state_store`, `resolve_worktree_path`, `subprocess`, `load_contract`, `save_contract`, `create_orchestrator`, `save_agent_output`, `get_repo_path`, `logger`) and the `_AGENT_ROLE_TO_CONTRACT_ROLE` map; private submodules reach them — and every internal cross-module helper — via `import routes.signals as _pkg`, so `patch("routes.signals.<name>")` resolves unchanged. `make_error_response`/`make_success_response` are not patched and are imported directly from `_responses`.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel, 170 lines) | Stable public API: `signals_bp` + the two route thin wrappers (decision-8); patched-seam imports + `_AGENT_ROLE_TO_CONTRACT_ROLE`; per-symbol re-exports of every submodule below | `signals_bp`, `handle_signal`, `handle_batch_signals`, `_AGENT_ROLE_TO_CONTRACT_ROLE` (+ re-exports of all symbols below) |
| `_responses.py` (29 lines) | Shared JSON response builders | `make_error_response`, `make_success_response` |
| `_validation.py` (609 lines) | BRC content + route-version + plan/artifact validators | `_BRC_MIN_CONTENT_LEN`, `_BRC_CONDITION_MIN_LEN`, `_BRC_BOILERPLATE`, `_BRC_CONDITION_KINDS`, `_validate_brc_content`, `_require_route_version`, `_validate_tester_check_coverage`, `_validate_plan_extensions`, `_ARTIFACT_HUMAN_LABEL`, `_artifact_human_label`, `_validate_producer_artifacts`, `_validate_plan_proposal` |
| `_lifecycle.py` (676 lines) | Non-consensus signal handlers + commit-verification helpers | `_SIGTERM_PATTERN`, `_is_sigterm_after_completion`, `_gateway_fetch_tracking_ref`, `_commit_object_resolvable`, `_verify_commit_on_branch`, `_check_branch_progress`, `handle_complete_signal`, `handle_progress_signal`, `handle_error_signal`, `handle_heartbeat_signal`, `handle_readiness_signal` |
| `_consensus_verdicts.py` (largest, 1,075 lines) | Producer/reviewer verdict intake: propose / ack / nack / withdraw + helpers | `_get_re_review_priming_text`, `_resolve_reviewer_delta_range`, `_resolve_pipeline_phase`, `_emit_ready_to_confirm_nudges`, `_stale_version_rejection`, `_contract_completeness_rejection`, `handle_consensus_propose_signal`, `handle_consensus_ack_signal`, `handle_consensus_nack_signal`, `handle_consensus_withdraw_signal` |
| `_consensus_confirm.py` (820 lines) | Confirmation flow: confirmed / excuse-producer / resolve-obligation / producer-push + helpers | `_write_consensus_confirmed_marker`, `_existing_confirmed_for_role`, `handle_consensus_confirmed_signal`, `handle_consensus_excuse_producer_signal`, `handle_consensus_resolve_obligation_signal`, `handle_consensus_producer_push_signal` |
| `_dispatch.py` (174 lines) | `handle_signal` (signal-type → handler dispatch) + `handle_batch_signals` bodies | route bodies (barrel wrappers delegate here) |

Pure refactor, no behaviour change. Patch seams preserved: module-level `patch("routes.signals._foo")` / `patch("routes.signals.<name>")` targets (incl. `patch("routes.signals.subprocess")`, `patch("routes.signals.get_state_store")`, `patch("routes.signals.logger")`) resolve through the barrel; the private submodules reach those barrel-patched dependencies and the internal helpers via `import routes.signals as _pkg`, so the pre-split module-global patch points keep working unchanged. `DecisionStatus` stays a lazy import inside `handle_consensus_excuse_producer_signal` (the `create=True` patch targets the package module attribute, identical to pre-split). **Packaging-neutral:** `orchestrator/routes/` is already shipped by the recursive `COPY orchestrator/routes/ ./routes/` (Dockerfile:45), so the new submodules are auto-included — no Dockerfile change. `make lint` clean (hard cap satisfied; the two consensus submodules sit just over the 800-line soft cap, a non-fatal warning); 963 signals + cross-importer tests pass.
```

## 2. Update the final summary paragraph

Replace the existing closing line:

```markdown
`routes/decisions/`, `state_store/`, `routes/phases/`, `routes/deployment/`, `routes/event_prompt/`, `overseer/monitor/`, `peer_consensus/`, `mcp_tools/`, and `kubernetes_spawner/` are the landed `orchestrator/` decompositions; later orchestrator slices append their own subsections here.
```

with (adds `routes/signals/`):

```markdown
`routes/decisions/`, `state_store/`, `routes/phases/`, `routes/deployment/`, `routes/event_prompt/`, `overseer/monitor/`, `peer_consensus/`, `mcp_tools/`, `kubernetes_spawner/`, and `routes/signals/` are the landed `orchestrator/` decompositions; later orchestrator slices append their own subsections here.
```
