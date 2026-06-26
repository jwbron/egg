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
| State & stores | `state_store.py`, `contract_store.py`, `contract_completeness.py`, `message_store.py`, `redis_message_store.py`, `progress_store.py`, `commit_authorship_store.py`, `decision_queue.py` | Git-backed pipeline state, the live SDLC contract, the inter-agent message bus, progress events, commit-authorship records, and the HITL decision queue |
| Health & monitoring | `health_monitor.py`, `health_checks/`, `heartbeat.py`, `overseer/`, `metrics.py`, `status_reporter.py`, `startup_reconciliation.py` | Tripwire health rules, structured heartbeats, the overseer advisor, metrics, collaborator status updates, and restart-time container reconciliation |
| MCP surface | `mcp_server.py`, `mcp_tools.py` | Streamable-HTTP MCP server and the tool schemas that let external Claude Code sessions manage pipelines |
| Streaming | `sse.py`, `unified_sse.py`, `webhooks.py`, `probe_listener.py` | Per-pipeline and unified SSE streams, outbound webhooks, and the state-store probe listener |
| External clients | `gateway_client.py`, `jira_epic.py`, `jira_reassess.py` | Gateway HTTP client and Jira epic / reassessment integration |
| Handoffs & operator actions | `handoffs.py`, `impasse_routing.py`, `operator_actions.py`, `wontdo_drain.py` | Inter-agent handoff data, impasse detection and routing, operator-grade contract mutations, and won't-do drain |
| CLI & config | `cli.py`, `env_config.py`, `prompt_loader.py`, `models.py` | The `egg-orch` CLI, environment configuration, prompt-template loading, and shared dataclasses |
| Cross-cutting policy | `action_guards.py`, `lifecycle_auth.py`, `redaction.py`, `log_filter.py`, `resilience.py` | Action guards, lifecycle auth, log redaction / filtering, and retry/resilience helpers |

When a module outgrows the 1,500-line / 100 KB cap in `scripts/file-size-allowlist.yaml`, decompose it into a sub-package following the canonical pattern (sub-package + explicit per-symbol re-export barrel + underscore-prefixed submodules) in [../docs/guides/decomposition-pattern.md](../docs/guides/decomposition-pattern.md). Once decomposed, the barrel `__init__.py` becomes the stable public API: external consumers import through the barrel while submodule paths stay package-private.
