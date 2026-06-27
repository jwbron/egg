# Orchestrator

Central coordination engine for SDLC pipelines. Manages agent lifecycle, phase transitions, health monitoring, and multi-agent consensus.

- **[README.md](README.md)** — architecture, API surface, configuration
- **[../docs/architecture/orchestrator.md](../docs/architecture/orchestrator.md)** — design decisions and component diagram
- **[../docs/reference/orchestrator-cli.md](../docs/reference/orchestrator-cli.md)** — CLI reference (`egg-orch`)
- **[../docs/guides/concurrent-execution.md](../docs/guides/concurrent-execution.md)** — multi-agent BRC protocol

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Module layout

The orchestrator is a flat package of single-file modules plus three sub-packages (`routes/`, `overseer/`, `health_checks/`). `routes/` exposes the REST API and drives the per-phase `_run_pipeline` loop; the modules below hold the lifecycle, consensus, state, and monitoring logic those routes call into. Import symbols from the module that owns them (e.g. `from peer_consensus import PeerConsensus`). See [README.md](README.md) for the full API surface.

| Area | Modules | Responsibility |
|------|---------|----------------|
| Pipeline orchestration | `routes/`, `api.py`, `event_loop.py`, `events.py` | REST endpoints, the `_run_pipeline` per-phase loop, the orchestrator-owned BRC event loop, and the pub/sub event bus |
| Agent execution | `concurrent_executor.py`, `container_backend.py`, `kubernetes_spawner.py`, `kubernetes_monitor.py`, `kubernetes_client.py`, `docker_client.py`, `sandbox_template.py`, `agent_model_resolution.py`, `agent_salvage.py` | Spawn agents at phase start on the shared pipeline branch, monitor health, resolve per-agent models, and salvage work from failed containers. `container_spawner.py` / `container_monitor.py` are compatibility shims re-exporting the Kubernetes backend |
| BRC consensus | `peer_consensus.py`, `consensus_wrapper.py`, `review_graph.py`, `approval_matrix.py`, `attestation_schemas.py`, `pr_obligations.py`, `supervision_policy.py` | Broadcast-Review-Converge state, the consensus-wrapped command built for each one-shot agent, the asymmetric reviewer→producer graph, approval/attestation rules, and pre-merge obligations |
| Slice DAG | `slice_scheduler.py`, `global_slice_admit.py`, `slice_id_validation.py`, `dag_visualizer.py`, `stacked_pr_reconciler.py` | Schedule the implement-phase slice DAG, cap concurrent slices per-pipeline and process-wide, and reconcile the stacked per-slice PRs |
| State & stores | `state_store/`, `contract_store.py`, `contract_completeness.py`, `message_store.py`, `redis_message_store.py`, `progress_store.py`, `commit_authorship_store.py`, `decision_queue.py` | Git-backed pipeline state, the live SDLC contract, the inter-agent message bus, progress events, commit-authorship records, and the HITL decision queue |
| Health & monitoring | `health_monitor.py`, `health_checks/`, `heartbeat.py`, `overseer/`, `metrics.py`, `status_reporter.py`, `startup_reconciliation.py` | Tripwire health rules, structured heartbeats, the overseer advisor, metrics, collaborator status updates, and restart-time container reconciliation |
| MCP surface | `mcp_server.py`, `mcp_tools.py` | Streamable-HTTP MCP server and the tool schemas that let external Claude Code sessions manage pipelines |
| Streaming | `sse.py`, `unified_sse.py`, `webhooks.py`, `probe_listener.py` | Per-pipeline and unified SSE streams, outbound webhooks, and the state-store probe listener |
| External clients | `gateway_client.py`, `jira_epic.py`, `jira_reassess.py` | Gateway HTTP client and Jira epic / reassessment integration |
| Handoffs & operator actions | `handoffs.py`, `impasse_routing.py`, `operator_actions.py`, `wontdo_drain.py` | Inter-agent handoff data, impasse detection and routing, operator-grade contract mutations, and won't-do drain |
| CLI & config | `cli.py`, `env_config.py`, `prompt_loader.py`, `models.py` | The `egg-orch` CLI, environment configuration, prompt-template loading, and shared dataclasses |
| Cross-cutting policy | `action_guards.py`, `lifecycle_auth.py`, `redaction.py`, `log_filter.py`, `resilience.py` | Action guards, lifecycle auth, log redaction / filtering, and retry/resilience helpers |

When a module outgrows the 1,500-line / 100 KB cap in `scripts/file-size-allowlist.yaml`, decompose it into a sub-package following the canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). Once decomposed, the barrel `__init__.py` becomes the stable public API: external consumers import through the barrel while submodule paths stay package-private.

## Decomposition seams

Oversized `orchestrator/` modules are split into **sub-packages with an explicit re-export barrel**, per the canonical [decomposition pattern](../docs/guides/decomposition-pattern.md) ([#3312](https://github.com/jwbron/egg/issues/3312)). The `__init__.py` barrel is the **stable public API**: external importers and `unittest.mock.patch` targets resolve through it (`patch("routes.decisions._foo")`), so they survive the split. Submodules are underscore-prefixed and package-private. For Flask-blueprint modules the `@<bp>.route` decorators stay in the barrel on thin wrappers (decision-8); the handler bodies live in the private submodules. For class-dominated modules the class definition stays in the barrel and each method body moves to a private submodule as a module-level function taking `self` explicitly, bound back onto the class in the barrel (**method-modules-on-class**, [pattern](../docs/guides/decomposition-pattern.md) §c).

### `routes/decisions/` — HITL decision endpoints ([#3312](https://github.com/jwbron/egg/issues/3312), slice 2)

`decisions.py` (1,562 lines) → `routes/decisions/` (largest submodule `_resolve.py`, 449 lines). The `decisions_bp` blueprint plus its seven `@decisions_bp.route` thin wrappers stay in the barrel (decision-8); each delegates to a handler body in a private submodule. The barrel does explicit per-symbol re-exports of the externally-referenced and test-patched surface.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Stable public API: holds `decisions_bp` + the seven route thin wrappers (decision-8); per-symbol re-exports of the patched surface | `decisions_bp`, `list_decisions`, `queue_decision`, `get_decision`, `resolve_decision`, `cancel_decision`, `answer_feedback`, `get_queue_status` (+ re-exports of all `_`-symbols below) |
| `_responses.py` (28 lines) | Shared JSON response builders | `make_error_response`, `make_success_response` |
| `_query.py` (247 lines) | Decision read + create endpoint bodies | `list_decisions`, `queue_decision`, `get_decision` |
| `_resolve.py` (largest, 449 lines) | Decision-resolution endpoint + contract-decision resolution | `resolve_decision`, `_resolve_contract_decision` |
| `_handlers.py` (312 lines) | HITL resolution-dispatch hooks | `_handle_restart_agent`, `_normalize_choice_resolution`, `_handle_conditional_ack_gate`, `_maybe_complete_task_from_resolution`, `_COMPLETE_TASK_RESOLUTION_RE` |
| `_graph_mutations.py` (242 lines) | Conditional-ACK consensus-graph mutations | `_persist_deferred_actions`, `_force_nack_conditional_edges`, `_invalidate_conditional_acks` |
| `_lifecycle.py` (295 lines) | Cancel / feedback-answer / queue-status endpoint bodies | `cancel_decision`, `answer_feedback`, `get_queue_status` |

Pure refactor: every symbol is AST-identical to the pre-split file. Module-level `patch("routes.decisions._foo")` / `patch("routes.decisions.<name>")` targets resolve through the barrel; the private submodules reach barrel-patched dependencies and dispatch hooks via `import routes.decisions as _pkg`, so the pre-split module-global patch points keep working unchanged.

### `state_store/` — git-backed pipeline state ([#3312](https://github.com/jwbron/egg/issues/3312), slice 3)

`state_store.py` (1,635 lines) → `state_store/` (largest submodule `_crud.py`, 450 lines). Class-dominated module: follows the **method-modules-on-class** shape ([pattern](../docs/guides/decomposition-pattern.md) §c) rather than the Flask-blueprint shape. The `StateStore` class definition stays in the barrel and keeps its identity on the `state_store` module path; each method body lives in a private submodule as a module-level function taking `self`, bound back onto the class in the barrel. Every external symbol and `unittest.mock.patch` target re-exports through the barrel.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel, 194 lines) | Stable public API: the `StateStore` class definition + all method bindings; re-exports the exception / validation / factory / lock / sync surface; keeps `shutil` + `time` imported so their patch seams resolve through the barrel | `StateStore`, `STATE_BRANCH` (+ re-exports of every symbol below) |
| `_errors.py` (78 lines) | Exception hierarchy + pipeline-id validation | `StateStoreError`, `GitOperationError`, `InvalidPipelineIdError`, `PipelineNotFoundError`, `StateValidationError`, `VersionConflictError`, `validate_pipeline_id`, `_validate_pipeline_id`, `PIPELINE_ID_PATTERN` |
| `_locks.py` (62 lines) | Per-pipeline state-lock registry | `get_pipeline_state_lock`, `release_pipeline_state_lock`, `_pipeline_state_locks`, `_state_locks_lock` |
| `_factory.py` (90 lines) | Store construction + repo discovery | `get_state_store`, `discover_repo_paths` |
| `_git.py` (151 lines) | Cross-process locking + git execution | `_run_git`, `_git_op`, `_get_pipeline_path`, `_ensure_dir`, `_cleanup_stale_locks` |
| `_worktree.py` (350 lines) | State-branch worktree lifecycle | `_ensure_worktree`, `_add_worktree_with_branch_recovery`, `_remove_stale_admin_dir`, `_remove_admin_dir_for_path`, `_lock_worktree`, `_state_branch_exists` |
| `_commit.py` (78 lines) | In-worktree commit helpers | `_commit_state`, `_get_current_commit`, `_generate_commit_message` |
| `_sync.py` (394 lines) | Best-effort remote sync + failure tracking | `sync_to_remote`, `_sync_to_remote_async`, `_reconcile_diverged_remote`, `_restore_from_remote`, `_record_sync_outcome`, `_detect_gateway_mode`, `_sync_failure_state`, `_sync_failure_state_lock` |
| `_crud.py` (largest, 450 lines) | Pipeline CRUD + lifecycle | `load_pipeline`, `save_pipeline`, `create_pipeline`, `delete_pipeline`, `pipeline_exists`, `list_pipelines`, `get_active_pipelines`, `pipelines_for_jira_ticket`, `update_pipeline` |

Pure refactor, no behaviour change. Patch seams preserved: class-level `patch.object(StateStore, "_run_git")` resolves via the method binding; module-global `patch("state_store.get_pipeline_state_lock")` / `patch("state_store.discover_repo_paths")` resolve because submodules reach them via `import state_store as _pkg`; `patch("state_store.shutil.rmtree")` / `patch("state_store.time.sleep")` resolve because the barrel keeps `shutil` / `time` imported. The one non-mechanical edit: `_sync_to_remote_async` reads `self._MAX_PUSH_RETRIES` (was `StateStore._MAX_PUSH_RETRIES`; identical `ClassVar` lookup).

This subsection and `routes/decisions/` are the landed `orchestrator/` decompositions; later orchestrator slices append their own subsections here.
