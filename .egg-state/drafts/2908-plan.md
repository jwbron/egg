# Plan: BRC consensus — deterministic event-pump + durable agent memory (issue #2908)

> Phase: plan | Recommended approach: refine-phase Option B (stateless event-pump + durable distilled memory)

## Approach

The refine-phase analysis (`.egg-state/drafts/2908-analysis.md`) recommends
**Option B**: reframe the consensus agent from a *persistent participant that
holds a wait* into a *stateless per-event handler the wrapper invokes*. The
wrapper (`orchestrator/consensus_wrapper.py`) becomes a deterministic event
pump that **invokes the existing `egg-orch message wait-loop` CLI directly**
(does NOT roll its own wait/cursor logic — preserves the #2323
cursor-threading fix at `/tmp/egg-wait-cursor-*`); on each actionable BRC
event it spawns the agent via `python3 -m egg_agent` (built by
`shared/egg_agent/command.py:build_agent_command` — **not** `claude --print`,
which is an EGG100 anti-pattern per `docs/guides/agent-mode-design.md:90-104`);
the agent loads its cached prefix + a distilled memory file + the one event,
acts, updates memory, and exits naturally. The wrapper loops until the
orchestrator reports the role consensus-confirmed and `is_complete`.

The four HITL decisions resolved in refine are load-bearing for this plan:

| HITL | Resolution | Plan impact |
|---|---|---|
| **cq-1** (WS8 scope) | Split MCP→CLI collapse to a follow-up. **This issue ships only the net-new CLI verbs the event-pump consumes** (`brc list-blocking`, `phase get-context`, `brc get-state`, `brc resolve-obligation`, `brc read-peer-artifact`, plus `consensus next-action` / extension of `consensus status`, plus `egg-contract show --field <name>` for arbitrary contract-field projection). Do **not** delete any of the 28 agent-facing MCP tools. Do **not** migrate prose-bearing `consensus propose/ack/nack` to CLI (the #2741 stdin/file-path rule is designed in the follow-up). | All new CLI verbs land in slice-4 via `_handler_dispatch` so they share existing handlers (preserves `tests/tools/test_mcp_cli_drift.py` invariant); no MCP deletions. |
| **cq-2** (Qwen cache) | Ship v1 with Qwen route **enabled** on the event-pump (no fallback to old path — cq-4). No keep-warm for v1. WS0 spike must measure cold-read cost across the worst-case ~15-min BRC idle; **only** if material does slice-8 add a bounded keep-warm (Qwen-route only; suppressed during HITL / awaiting-human waits). | slice-1 runs WS0 spike with TTL instrumentation; slice-8 is conditional on slice-1 outcome. |
| **cq-3** (safety budget terminal state) | OVERSEER_ALERT + HITL decision (resume/abort). **Hard requirement: the no-progress safety budget AND parked-HITL state must be persisted DURABLY SERVER-SIDE in the orchestrator** so a restarted SDLC host can resume. | slice-2 lands the **sync-flush `_save_pipeline_durable()` / `commit_and_push_sync()` variant of `save_pipeline` plus the contract schema bump 1.2→1.3 plus the `_startup_reconciliation_replay_safety_budget` step**. The split is at the call-site (slice-6 callers opt in to sync-flush for HITL-decision + safety-budget-transition writes; other contract writes keep the best-effort-async path). slice-6 wires the wrapper-side consumer that survives host death via the slice-2 startup_reconciliation. |
| **cq-4** (old path retention) | **Delete the old capped-restart wrapper path entirely.** No flagged fallback at any point in this issue. | slice-6 deletes `MAX_CONSENSUS_RESTARTS = 3`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT`, the SSE consensus.reached blocking path, and the restart loop in one slice — no intermediate flag. **Cutover playbook**: drain in-flight pipelines on the old wrapper before deploying slice-6's PR; new pipelines start on the event-pump immediately at deploy time. |

Refine open-feedback Q1 (no operator-side hard budget caps for WS0 — instrument
only) and Q2 (brc-memory.md is ephemeral coordination state; recovery must NOT
depend on it; orchestrator message history is the durable backstop) are
honoured: slice-1 emits measurements but no fail thresholds; slice-5 treats
brc-memory.md as ephemeral and slice-6's restart recovery reasons from durable
orchestrator state alone (the sync-flush `no_progress_budget` and `parked_hitl`
contract fields, plus the slice-2 startup_reconciliation replay).

**Memory shape decision** (refine open decision #1 / risk_analyst R5 / reviewer_plan
B2) is **committed to distilled / rewrite-and-distill** at plan time per refine
analysis lines 360-367 and architect v2 slice-5 goal. Rationale on both axes:
(axis-1 cache cost) distilled rewrites burst the per-event prefix on each
ACK/NACK but the burst is bounded by a section template so the size and
frequency are predictable; slice-1's WS0 measurement is a *check* on whether
the burst materially busts the Qwen prefix cache, not the gating decision.
(axis-2 orient-don't-constrain per `docs/guides/agent-mode-design.md`)
distilled summaries orient the per-event agent without flooding its attention;
unbounded append-only memory eventually starts *constraining* what the
per-event agent can attend to (sea-of-context). If slice-1's WS0 measurement
shows distilled bursts materially bust Qwen cache, slice-5 MAY add a bounded
append-only tail with explicit compaction — but the *default* is distilled
and the decision is made now.

## Slice DAG (forest constraint preserved)

The architect's slice scaffold at
`.egg-state/agent-outputs/2908-architect-slices.yaml` (v2) is copied verbatim
into the `yaml-tasks` appendix below. Eight slices, single-parent forest:

```
slice-1 (WS0 spike, root)
  └── slice-2 (cq-3 durable HITL + no-progress-budget schema + sync-flush + startup_reconciliation)
        └── slice-3 (next-action endpoint — orchestrator/routes pure additive)
              └── slice-4 (net-new CLI verbs — sandbox/egg_lib only)
                    └── slice-5 (brc-memory.md + handler dict-arg memory writes)
                          └── slice-6 (event-pump rewrite + WS1/WS4 + safety-budget consumer + legacy deletion)
                                ├── slice-7 (prompt collapse + delta-scoped re-analysis + prior-fix preservation audit)
                                └── slice-8 (Qwen-route bounded keep-warm — conditional on slice-1 data)
```

slice-7 and slice-8 are independent siblings under slice-6 — neither blocks
the other. Each slice has at most one DAG parent; no
`serialized_chain_order` needed.

## Primitives

Every primitive named in the slice rationale and tasks below has a verified
`file:line` citation. `(NEW — TASK-X-Y)` marks primitives the named task
creates; `(DELETE — TASK-X-Y)` marks ones it removes; the rest already exist
and are unchanged or extended.

### Wrapper-side (orchestrator-process; bash script runs in-pod)

| Primitive | Location | Disposition |
|---|---|---|
| `_CONSENSUS_WRAPPER_TEMPLATE` | `orchestrator/consensus_wrapper.py:116-713` | REWRITE in slice-6 (TASK-6-1) as deterministic event pump that invokes existing `egg-orch message wait-loop` CLI directly |
| `MAX_CONSENSUS_RESTARTS = 3` | `orchestrator/consensus_wrapper.py:38` | DELETE in slice-6 (TASK-6-2) |
| `_RECOVERY_SYSTEM_PROMPT` | `orchestrator/consensus_wrapper.py:64-99` | DELETE in slice-6 (TASK-6-2) |
| `_RECOVERY_USER_PROMPT` | `orchestrator/consensus_wrapper.py:102-105` | DELETE in slice-6 (TASK-6-2) |
| Restart loop with `OVERSEER_ALERT` | `orchestrator/consensus_wrapper.py:555-695` (overseer alert at :570-585, issue #2806) | DELETE in slice-6 (TASK-6-2); the per-restart `OVERSEER_ALERT` block at :570-585 is **re-purposed (not duplicated)** for safety-budget exhaustion in TASK-6-5 — the implementation extracts the existing helper and re-wires it rather than copying it |
| SSE consensus.reached blocking path | `orchestrator/consensus_wrapper.py:397-548` (`check_confirmed_and_wait` at :397, curl SSE consumer at :419-501, fallback wait at :512-542) | DELETE in slice-6 (TASK-6-6); replaced by `egg-orch consensus status --json` against the slice-3 endpoint (architect calls this a `consensus check` operation; the verb name is `consensus status` per orch_cli.py:2783) |
| `build_consensus_wrapped_command(...)` | `orchestrator/consensus_wrapper.py:716-775` | REWRITE in slice-6 (TASK-6-1) to emit the event-pump template |
| Concurrent-executor caller | `orchestrator/concurrent_executor.py:37` (import), `:489` (call) | REWIRE in slice-6 (TASK-6-7) |
| Pipelines-route caller (restart path) | `orchestrator/routes/pipelines.py:2792-2796` | REWIRE in slice-6 (TASK-6-7); restart path collapses to the same event-pump entry point |
| `build_agent_command(prompt, *, model, max_turns, system_prompt)` | `shared/egg_agent/command.py:11-46` (re-exported `shared/egg_agent/__init__.py:8`) | CONSUMED by new wrapper in slice-6 (TASK-6-3). **This is the canonical entry point** — refine-phase corrected the issue body's `claude --print` reference (EGG100 anti-pattern per `docs/guides/agent-mode-design.md:90-104`) and slice-6 must use `build_agent_command` |
| `egg-orch message wait-loop` CLI | `sandbox/egg_lib/orch_cli.py:1695` (handler), `:3364-3431` (subparser) | INVOKED BY new wrapper template in slice-6 (TASK-6-1) — wrapper does NOT roll its own wait/cursor logic; this preserves #2323 cursor threading at `/tmp/egg-wait-cursor-*` |
| `egg-orch message heartbeat` CLI | `sandbox/egg_lib/orch_cli.py:1832` | INVOKED BY new wrapper template in slice-6 (TASK-6-4) at 60s cadence to preserve `health_monitor.py:771` threshold (120s default / 600s implement) and #2451 gateway keep-alive |

### Sandbox / agent-side handlers and CLI (unchanged unless noted)

| Primitive | Location | Disposition |
|---|---|---|
| `message_wait_loop(req)` | `sandbox/egg_agent_tools/handlers/message.py:267` (first-match return at `:405-410`) | Agent-side invocation removed in slice-7 (the wrapper now owns the wait); handler stays callable for backward compatibility |
| `_start_wait_loop_heartbeat(tick, interval)` | `sandbox/egg_agent_tools/handlers/message.py:234-264` | MIGRATE auto-start to wrapper in slice-6 (TASK-6-4); helper stays callable for backward compatibility |
| `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60` | `sandbox/egg_agent_tools/handlers/message.py:47` | Constant value mirrored in the wrapper template's heartbeat cadence (slice-6 TASK-6-4); orchestrator-side threshold consumer at `orchestrator/health_monitor.py:771` is unchanged |
| `tool_interceptor.check_file_write_permission` | `shared/egg_agent/tool_interceptor.py:27` | UNCHANGED — per-invocation enforcement inherited by every `python3 -m egg_agent` spawn |
| `phase_filter.validate_agent_push` | `shared/egg_restrictions/checker.py:98` (re-exported `gateway/agent_restrictions.py:35,128`) | UNCHANGED — gateway push-time boundary check |
| Role allowlist for `.egg-state/agent-outputs/` | `shared/egg_restrictions/patterns.py` — 14 occurrences across every producer/reviewer pattern (ARCHITECT line 367, TASK_PLANNER line 377, REVIEWER_REFINE line 510, REVIEWER_PLAN line 516+, REVIEWER_AGENT_DESIGN line 479-484, REFINER line 488, CODER block-exempt 231, TESTER 277, DOCUMENTER 307); prefix-glob semantics confirmed via `shared/egg_restrictions/matchers.py:33` (`<prefix>/` matches any file under the prefix including subdirs) | UNCHANGED — `brc-memory.md` lands under the existing allowlist; **cross-role READS are not gated** (the file-write interceptor at `shared/egg_agent/tool_interceptor.py:17` only intercepts Write/Edit/NotebookEdit; reads use the standard Read tool with no role check — confirmed in TASK-5-4 R13 verification) |
| `cost_callback.cost_logger` LiteLLM callback | `config/litellm/cost_callback.py` (~344 lines; registered via `litellm_settings.callbacks` line 45) | CONSUMED in slice-1 spike (TASK-1-3) for Qwen-route TTL measurement. **Trust-boundary note**: the callback runs inside the egg-litellm container and emits structured log lines (no on-disk JSON files in the agent pod). The refine analysis's reference to `~/.local/state/clm/cost-*.json` does NOT match the current code (`grep -rn` returns zero hits for that path). TASK-1-3 must source Qwen cache-read counts from the litellm container's structured logs (e.g. `kubectl logs deployment/egg-litellm`), not from a path inside the agent pod |

### Sandbox CLI surface (`egg-orch` / `egg-contract`)

| Primitive | Location | Disposition |
|---|---|---|
| `cmd_consensus_status` / `cons_status` subparser | `sandbox/egg_lib/orch_cli.py:2783` (handler), `:3612-3626` (subparser) — delegates to `brc_get_state` | EXTEND in slice-3 (TASK-3-1) to expose next-action computation in the same payload (preferred per architect open-decision od-3) |
| `cmd_message_wait_loop` / `msg_wait_loop` subparser | `sandbox/egg_lib/orch_cli.py:1695` (handler), `:3364-3431` (subparser) | UNCHANGED — wrapper-side caller in slice-6 |
| `cmd_message_heartbeat` / `msg_heartbeat` subparser | `sandbox/egg_lib/orch_cli.py:1832` | UNCHANGED — wrapper-side caller in slice-6 |
| `cons_propose` / `cons_ack` / `cons_nack` / `cons_confirmed` subparsers | `sandbox/egg_lib/orch_cli.py:3480-3609` (handlers at `:2528`, `:2633`, `:2692`, `:2753`) | UNCHANGED — prose-bearing collapse deferred per cq-1 |
| `egg-orch brc <verb>` subparser (root) | does not exist | (NEW — TASK-4-1 / TASK-4-3 / TASK-4-4) — slice-4 adds a `brc` subparser that hosts `list-blocking`, `get-state`, `resolve-obligation`, `read-peer-artifact`, and (if od-3 took the new-verb path) `next-action` |
| `egg-orch brc list-blocking` CLI shim | does not exist; handler `brc_list_blocking` at `sandbox/egg_agent_tools/handlers/brc.py:726` | (NEW — TASK-4-1) |
| `egg-orch brc get-state` CLI shim | does not exist; handler `brc_get_state` at `sandbox/egg_agent_tools/handlers/brc.py:679` (already reachable indirectly via `consensus status`) | (NEW — TASK-4-4) |
| `egg-orch brc resolve-obligation` CLI shim | does not exist; handler `brc_resolve_obligation` at `sandbox/egg_agent_tools/handlers/brc.py:743` | (NEW — TASK-4-4) |
| `egg-orch brc read-peer-artifact` CLI shim | does not exist; handler `brc_read_peer_artifact` at `sandbox/egg_agent_tools/handlers/brc.py:901`. Marked `cli_command=None` in `tools/__init__.py` registry and named in `tests/tools/test_mcp_cli_drift.py:28` as a net-new gap | (NEW — TASK-4-4) |
| `egg-contract get-context` CLI shim | does not exist; handler `phase_get_context` at `sandbox/egg_agent_tools/handlers/phase.py:139` (marked `cli_command=None`) | (NEW — TASK-4-2). **Note**: `phase_get_context` returns a fixed bundle `{ok, pipeline_id, phase, role, contract_present, current_contract_phase, tasks, artifacts, repo_path}`; it does NOT support arbitrary contract-field projection. The wrapper's host-restart recovery needs arbitrary-field reads (e.g. `pipeline.no_progress_budget`), which is a separate CLI verb (TASK-4-5 below). |
| `egg-contract show --field <name>` CLI shim | does not exist; handler `sdlc_show_contract` (the MCP tool `mcp__sdlc__show_contract` already supports a `fields=[...]` projection) | (NEW — TASK-4-5). **This is the field-projection verb the slice-6 wrapper-side host-restart recovery (TASK-6-9) uses to read `pipeline.no_progress_budget` and `pipeline.parked_hitl` durable fields.** Addresses reviewer_plan blocking item B3. |
| `egg-orch consensus next-action --role R` | does not exist as a verb; the architect open-decision od-3 prefers extending `consensus status --json` (TASK-3-1). Only added as a standalone verb if extension proves insufficient | (NEW — TASK-4-3, **conditional**). Architect's preference is to extend `consensus status` (TASK-3-1); TASK-4-3 only adds an `egg-orch brc next-action` shim if TASK-3-1's review concludes the existing `consensus status` payload cannot carry the field cleanly |
| `_handler_dispatch` parity helper | `sandbox/egg_lib/orch_cli.py` (called by every CLI shim that wraps an `egg_agent_tools.handlers.*` function) | USED by every new CLI shim in slice-4 to satisfy the MCP↔CLI drift invariant |

### Orchestrator routes, signals, and durable state

| Primitive | Location | Disposition |
|---|---|---|
| `handle_consensus_propose_signal` | `orchestrator/routes/signals.py:1204` | EXTEND in slice-3 (TASK-3-1) to populate `next_action` in the returned payload — judgment stays with the agent; the endpoint owns sequencing only |
| `handle_consensus_ack_signal` | `orchestrator/routes/signals.py:1428` | EXTEND in slice-3 (TASK-3-1) |
| `handle_consensus_nack_signal` | `orchestrator/routes/signals.py:1559` | EXTEND in slice-3 (TASK-3-1) |
| `handle_consensus_confirmed_signal` | `orchestrator/routes/signals.py:1806` | EXTEND in slice-3 (TASK-3-1) |
| Pipeline-status endpoint (consumed by `brc_get_state`) | `orchestrator/routes/pipelines.py` (handler used by `handlers/brc.py:707-712`) | EXTEND in slice-3 (TASK-3-1) to include the next-action field per role; the endpoint MAY also surface `pipeline.no_progress_budget` state so the wrapper's safety-budget consumer (slice-6 TASK-6-5) reads it via a single call rather than two separate fetches |
| `EventType.CONSENSUS_REACHED` enum + emit sites | `orchestrator/events.py:74`; emit sites at `orchestrator/peer_consensus.py:1863`, `routes/pipelines.py:17945, :18254, :18373, :18523, :18686` | UNCHANGED — wrapper consumer at `consensus_wrapper.py:424/467/473` is removed in slice-6; orchestrator-side emits stay for the operator-facing SSE stream (`orchestrator/sse.py:111`) |
| `_build_brc_preamble(role_value, phase, ...)` | `orchestrator/routes/pipelines.py:12348` (callers at `:13659, :13692, :13720`) | MINIMAL NUDGE in slice-6 (TASK-6-8) — drop the STAY-ALIVE loop instruction so the agent exits after one event. FULL COLLAPSE in slice-7 (TASK-7-1) with explicit prior-fix preservation audit |
| `pipeline.no_progress_budget` contract field | `.egg-state/contracts/<pid>.json` (schema in `shared/egg_contracts/models.py`) | (NEW — TASK-2-1). Schema bump 1.2 → 1.3 with `_migrate_schema_version_to_1_3` migrator. Holds `{remaining_seconds, last_progress_at, threshold_seconds, alert_emitted}` so a restarted SDLC host resumes monitoring the same budget |
| Parked-HITL durable state `pipeline.parked_hitl` | same | (NEW — TASK-2-1). Holds `{decision_id, parked_at, options, selected, wake_on, resume_token}` so a restarted host finds the awaiting-human gate and resumes waiting on it |
| `save_pipeline()` + best-effort async push | `orchestrator/state_store.py:11` (docstring "async push via a daemon thread"), `:127` (`_push_in_flight`), `:804-805` (`Best-effort async push to remote after every commit` → `_sync_to_remote_async()`), `:890-928` (debounced thread) | UNCHANGED — the existing async path stays the default for high-frequency writes |
| `_save_pipeline_durable(state)` / `commit_and_push_sync(state)` | does not exist | (NEW — TASK-2-2). Sync-flush variant that returns ONLY after `git push origin <branch>` completes; opt-in via call-site only (callers explicitly choose sync-flush for HITL-decision-resolution writes AND safety-budget-state-transition writes). Resolves risk_analyst R1 / reviewer_plan B-shared cq-3 durability gap |
| `load_contract_from_branch` | `orchestrator/contract_store.py:139-142` (docstring: tries `origin/<branch>` first which "reflects the last pushed state" then falls back to local; explicit failure mode "pre-push crashes" lose data on emptyDir-backed pods) | UNCHANGED — TASK-2-2's sync-flush ensures the durable fields are pushed before `save_pipeline_sync` returns, so `load_contract_from_branch` reads consistent state post-restart |
| `_startup_reconciliation_replay_safety_budget` | does not exist; will be added to `orchestrator/startup_reconciliation.py` (existing file in the same pattern as `peer_consensus.reconstruct_tracker_from_messages`) | (NEW — TASK-2-3). On orchestrator boot, re-reads `pipeline.no_progress_budget` from the on-branch contract for every active pipeline and primes the in-memory `health_monitor.py:82-103` anchors so a fresh host monitors the same budget the previous host was tracking |
| In-memory health-monitor anchors | `orchestrator/health_monitor.py:82-103` | EXTENDED in slice-2 (TASK-2-3) to expose a re-prime entry point that the startup_reconciliation calls |
| `DEFAULT_CONTRACTS_DIR = ".egg-state/contracts"` | `shared/egg_contracts/loader.py:23` (helpers at `:70`, `:74`, `:93`, `:272`, `:276`, `:319`) | UNCHANGED — schema bump lives in `shared/egg_contracts/models.py` and the migrator helpers |

### Test surface

| Primitive | Location | Disposition |
|---|---|---|
| MCP↔CLI drift test | `tests/tools/test_mcp_cli_drift.py` (320 lines) | EXTEND in slice-4 (TASK-4-7). The new CLI shims (TASK-4-1 to TASK-4-5) flip `TOOL_REGISTRY` entries from `cli_command=None` to a concrete verb; the test must continue to pass under handler-import parity. `task__mark_gap` (still in the explicit `cli_command=None` gap list per the test docstring at line 28) stays `None`-gapped |
| E2E + server tests | `integration_tests/test_sandbox_mcp_tools_e2e.py` (142 lines), `tests/sandbox/egg_agent_tools/test_server.py` (210 lines) | UNCHANGED — cq-1 keeps the MCP server registered; the per-tool count assertion in `test_server.py` updates only if iter-3 changes the schema set (it does not in this issue) |
| Trust-boundary fixture scope | `integration_tests/local_pipeline/conftest.py:261` (`gateway_url`); `integration_tests/conftest.py:78` (`EggStack.gateway_url` attribute, NOT a pytest fixture) | OBSERVED in slice-5 (TASK-5-6 BRC memory integration test) and slice-6 (TASK-6-11 event-pump #2906 reproducer test) — both **must** live under `integration_tests/local_pipeline/` so `gateway_url` and `local_pipeline_stack` machinery are reachable; `EggStack.gateway_url` is an attribute, not a fixture, and cannot be injected. No `integration_tests/` fixture is in-sandbox-agent runnable today |

### Existing fix-lineage primitives the migration must preserve (R7 + reviewer_plan slice-7 audit requirement)

The refine analysis names these explicitly. slice-6 wrapper-level tests and
slice-7 prompt-collapse audit each classify by orchestrator-enforced / CLI-enforced /
prompt-only and assert preservation:

- **Cursor threading** (#2323) — `/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-*` per-call cursor (`sandbox/egg_lib/orch_cli.py` wait-loop block) — **CLI-enforced**, preserved automatically because the wrapper invokes `egg-orch message wait-loop` directly (slice-6 TASK-6-1); the cursor is owned by the CLI, not by the wrapper or the agent.
- **Pre-confirm-wait rejection** (#2064, #2482) — the orchestrator rejects `wait-loop --for CONSENSUS_CONFIRMED` if the caller hasn't yet confirmed — **orchestrator-enforced**, preserved automatically; slice-7 preamble collapse can safely remove the prompt instruction.
- **Gap-race fix** (#1995) — server-side cursor + `since_id` threading — **CLI-enforced**, preserved by wrapper-owned wait via CLI.
- **Heartbeat liveness** (#2036) — **CLI-enforced** in slice-6 (TASK-6-4), preserved by the wrapper invoking `egg-orch message heartbeat` at 60s cadence.
- **Gateway session keep-alive** (#2451) — same as heartbeats (TASK-6-4); the heartbeat CLI verb's side-effect keeps the gateway session alive.
- **Open-NACK aggregation barrier** (#2142) — **orchestrator-enforced**; unchanged. slice-3 next-action endpoint (TASK-3-1) must encode "blocked by N≥2 reviewer NACKs" as a `next_action: address_nacks` outcome so the agent sees it.
- **Conditional-ACK / stale-version re-review** (#2482) — **orchestrator-enforced**; slice-3 next-action endpoint (TASK-3-1) must surface stale-version via a `current_version` / `last_observed_version` pair.
- **Producer-allowlist scope** (#2725) — **gateway-enforced**; file-write boundary; unchanged.

## Slice rationale — risk-driven

Each slice is anchored to the risks the refine-phase risk_analyst surfaced
(see `.egg-state/agent-outputs/2908-risk_analyst-output.json`). The three
HIGH-severity risks are addressed inside slice-1, slice-2, and slice-5
before any production wrapper change.

- **slice-1 (WS0 spike)** retires **R2** (Qwen cache TTL ceiling) and **R11** (`claude --print` regression risk). The spike's prototype uses `python3 -m egg_agent` per the refine-phase primitive correction, even though it is throwaway, so the production rewrite in slice-6 inherits a working pattern. The spike's measurement is a *check* on the slice-5 distilled memory shape decision rather than the gating decision — slice-5 commits to distilled at plan time per the architect v2 commitment. The cost-callback acquisition path is documented as a trust-boundary constraint (the data lives in the litellm container, not the agent pod).

- **slice-2 (cq-3 durable HITL + budget schema + sync-flush + startup_reconciliation)** retires **R1** (durability gap) by landing **three** pieces: (i) the contract schema bump 1.2 → 1.3 with `pipeline.no_progress_budget` + `pipeline.parked_hitl` fields, (ii) the new `_save_pipeline_durable()` / `commit_and_push_sync()` synchronous-flush helper that returns only after `git push origin <branch>` succeeds (called only from HITL-decision and safety-budget transitions, not from every contract write — preserves high-frequency-write throughput), and (iii) the `_startup_reconciliation_replay_safety_budget` step that primes the in-memory `health_monitor.py` anchors from the durable on-branch contract so a fresh SDLC host monitors the same budget the previous host was tracking. The slice-2 tests cover the production failure mode: emptyDir wipe + remote ref behind → fresh host loads the budget from `origin/<branch>` via `load_contract_from_branch`. All purely additive — no consumers wired yet; slice-6 consumes.

- **slice-3 (next-action endpoint)** retires **R9** (next-action scope) by extending the existing `consensus status --json` payload per architect open-decision od-3, encoding lifecycle states the agent's per-event handler needs (open-NACK aggregation #2142, conditional ACK, stale-version re-review #2482, producer-first ordering #2749, resolve_obligation surface). The endpoint may surface `pipeline.no_progress_budget` state in its response so slice-6's safety-budget consumer reads it via a single call. Pure additive route work.

- **slice-4 (net-new CLI verbs)** retires **R6** (drift-test break) by adding the new CLI shims through `_handler_dispatch` so handler-import parity is preserved. Five new verbs: `brc list-blocking`, `phase get-context`, `brc resolve-obligation`, `brc read-peer-artifact`, `brc get-state`, plus the **`egg-contract show --field <name>`** verb (TASK-4-5) that wraps `mcp__sdlc__show_contract`'s field projection — this verb is the field-projection primitive the slice-6 wrapper-side host-restart recovery uses to read the slice-2 durable fields; reviewer_plan B3 named the gap explicitly. The `egg-orch brc next-action` shim is conditional on slice-3's outcome (added only if extending `consensus status` proves insufficient).

- **slice-5 (brc-memory.md + handler dict-arg scaffolding)** retires **R13** (cross-role memory allowlist — `.egg-state/agent-outputs/` is in every BRC-participating role pattern; reads use the Read tool with no role check, confirmed in TASK-5-4 documentation), **R5** (memory shape — committed to distilled per architect v2 / refine analysis lines 360-367, with slice-1 as a check), and the reviewer_plan non-blocking observation that the dict-arg handler interface (sandbox-side in-process MCP, NOT argv) means there is no possibility of #2741-class shell-metachar corruption on memory writes. The slice intentionally lands the memory writes before the wrapper consumes them so by the time slice-6 ships, the memory file is already populated on the first per-event invocation. TASK-5-6 covers the brc memory integration test owner (reviewer_plan B4).

- **slice-6 (event-pump rewrite + WS1 + WS4 + safety-budget consumer + legacy deletion)** is the central control-flow rewrite. It addresses **R1**'s consumer side (the wrapper re-reads durable state via the slice-2 startup_reconciliation after host death, and via TASK-4-5's `egg-contract show --field` for per-iteration reads), **R3** (cursor pod-restart loss — the wrapper-side wait inherits the persistent `EGG_AGENT_ROLE` and uses the existing `egg-orch message wait-loop` CLI, so the `/tmp/egg-wait-cursor-*` cursor + server-side `since_id` reset are preserved), **R4** (gateway credential expiry on long idles — wrapper-side heartbeat via `egg-orch message heartbeat` CLI), **R7** (prior-fix preservation — each named fix has an assertion in the slice-6 test set), **R8** (no-rollback cutover — the wrapper-side smoke test runs against the #2906 reproducer immediately after the deletion lands; slice-6 task explicitly documents the cutover playbook), and **R14** (SSE silent-failure — the SSE blocking path is deleted; the new wrapper uses `egg-orch consensus status --json` against the deterministic slice-3 endpoint instead). The minimal preamble nudge here (drop STAY-ALIVE) is the smallest change that lets the agent exit after one event; full preamble collapse is deferred to slice-7.

- **slice-7 (prompt collapse + delta re-analysis + prior-fix preservation audit)** retires **R7** (prior-fix-preservation residue — the audit explicitly classifies each prior fix and the lean event-handler contract preserves only the prompt-only ones) and **R12** (metadata-only delta — the per-event prompt template explicitly says "evaluate ONLY the named changed_artifacts; do not re-read the codebase / earlier commits / analysis draft"). The slice also addresses **R10** (per-event subprocess latency — by bounding context, the per-event token cost stays small; if latency proves material, the slice tests assert a measurable bound).

- **slice-8 (Qwen bounded keep-warm — conditional)** is the only slice whose shape depends on slice-1 outcome. The three branches (no-keep-warm docs note; bounded keep-warm code; HITL ambiguity decision) are enumerated below as separate tasks; the implementer picks the path based on the slice-1 measurement. Anthropic route is explicitly out of scope (1h TTL cleared in WS7 empirical).

## Test strategy

**Automated coverage** (per workstream):

- **slice-1 spike**: no automated tests; the spike's value is the measurement, captured in the spike report at `docs/spike/2908-event-pump-spike.md`.
- **slice-2 durable persistence**: unit tests at `tests/orchestrator/test_contract_schema_migration_to_1_3.py` (NEW — TASK-2-4) covering migrator round-trip + no-data-loss; `tests/orchestrator/test_save_pipeline_durable.py` (NEW — TASK-2-5) covering sync-flush returns only after successful `git push origin <branch>`, fails-closed if the push fails (caller sees error), and the production failure-mode test (emptyDir wipe + remote ref behind → fresh host loads the budget from `origin/<branch>`); `tests/orchestrator/test_startup_reconciliation_safety_budget.py` (NEW — TASK-2-5) covering host-kill mid-budget → fresh host loads same budget state via reconciliation.
- **slice-3 next-action endpoint**: unit tests at `tests/orchestrator/test_consensus_next_action.py` (NEW — TASK-3-3) covering producer/reviewer/dual-role lifecycles, open-NACK aggregation (#2142), conditional ACK, stale-version re-review (#2482), resolve_obligation surface, producer-first ordering (#2749 dual-role fixture).
- **slice-4 CLI shims**: extend `tests/tools/test_mcp_cli_drift.py` (TASK-4-7) for the new CLI verbs; unit tests at `tests/sandbox/egg_lib/test_brc_cli_verbs.py` (NEW — TASK-4-6) covering handler-import parity, JSON payload byte-equality, stdin/file-path rule (#2741 regression guard) for any prose-bearing inputs.
- **slice-5 memory file**: unit tests at `tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py` (NEW — TASK-5-5) covering distilled rewrite-and-distill, idempotency, partial-file recovery, `brc_ack`/`brc_nack` dict-arg side-effect, write-permission failure non-fatal; integration test at `integration_tests/local_pipeline/test_brc_memory_handler_e2e.py` (NEW — TASK-5-6) covering ACK through live MCP server → memory file lands at correct per-role path (reviewer_plan B4 owner).
- **slice-6 wrapper**: unit tests at `tests/orchestrator/test_consensus_wrapper.py` (extend existing — TASK-6-10) covering event-pump reaches consensus without a restart cap, clean post-event exit does not FAIL, wrapper-emitted heartbeats fire on the migrated interval, `egg-orch consensus status --json` replaces SSE consumer cleanly, fix-lineage assertions (cursor threading #2323, gateway keep-alive #2451, heartbeat #2036, open-NACK barrier surfaced through next-action #2142, pre-confirm-wait #2064/#2482, gap-race #1995); host-restart simulation at `tests/orchestrator/test_event_pump_host_restart.py` (NEW — TASK-6-10) asserts `alert_emitted=true` recovered from durable state does NOT re-fire the OVERSEER_ALERT.
- **slice-6 integration**: a k3s integration test at `integration_tests/local_pipeline/test_event_pump_qwen_repro.py` (NEW — TASK-6-11) that runs the #2906 reproducer under the event-pump and asserts no "Agent exited without BRC consensus" churn, memory populated + consulted, role-write permissions still enforced. **Lives under `local_pipeline/` per the trust-boundary docs** so `gateway_url` and `local_pipeline_stack` fixtures are reachable.
- **slice-7 prompt collapse + delta**: unit test at `tests/orchestrator/test_brc_preamble.py` (NEW — TASK-7-6) asserting the lean preamble carries the file-write boundary + stdin/file rule + the explicit prior-fix-audit table (each fix classified as orchestrator-enforced / CLI-enforced / prompt-only); per-event prompt template snapshot tests for delta-scoped re-analysis. Unit test at `tests/sandbox/egg_agent_tools/handlers/test_brc_delta.py` (NEW — TASK-7-6) asserting the handler-side delta fetcher returns metadata (paths, SHAs, version markers) and never inlines file contents.
- **slice-8 keep-warm (conditional)**: only if path-b — unit tests at `tests/orchestrator/test_qwen_keep_warm.py` (TASK-8-4) covering Qwen-route-only activation, HITL/awaiting-human suppression, hard refresh cap. Path-a and path-c add no production code.

**Manual verification**:

- Reviewer should run `make test` after slice-4 lands and confirm the new CLI verbs are reachable via `egg-orch --help` / `egg-contract --help`.
- For slice-6, reviewer should run the integration test (`make test-all` will exercise it under the `local_pipeline` marker) and inspect the wrapper-emitted heartbeats in the orchestrator status output to confirm the migration.
- For slice-8 (if path-b), reviewer should review the keep-warm cadence against the slice-1 measured TTL ceiling and confirm the HITL suppression by simulating a long awaiting-human gate.

## Manual steps

**Pre-merge**: none for any slice — every change ships behind the BRC cycle's review gate. No `.github/` files are touched.

**Post-merge**:

- **slice-2**: none — schema bump auto-applies on next contract load via the existing `_migrate_schema_version_to_1_3` migrator.
- **slice-6**: operators monitoring in-flight pipelines must drain the old wrapper before deploying this slice's PR; new pipelines start on the event-pump immediately at deploy time. There is no flagged fallback. Documented in release notes (TASK-6-12 captures this in `docs/architecture/brc-event-pump.md`).
- **slice-8 (path b)**: if bounded keep-warm lands, the litellm container must be redeployed for the new keep-warm route configuration to take effect. If path a, no action.

```yaml
# yaml-tasks
pr:
  title: "Replace agent-held BRC waits with deterministic event-pump + durable memory"
  description: |
    Closes #2906 and resolves the structural seam that produced the entire lineage of BRC wait bugs (#2323, #2064/#2482, #2036, #1995, #2451). Today the BRC consensus agent holds a blocking `egg-orch message wait-loop` between each event; every re-entry is a seam at which the model can emit a final assistant message and exit `success=True` instead of looping. Claude usually re-enters; qwen3.7-max does not (#2906) — it exits at ~30–50 of a 1000-turn budget, the wrapper sees no `CONSENSUS_CONFIRMED`, and the 3-restart cap (#2806) burns ~$1 and ~20 min per cycle.

    This PR reframes consensus-agent execution from a *persistent participant that holds a wait* into a *stateless per-event handler the wrapper invokes*, with continuity carried by a durable distilled memory file. Eight slices ship sequentially as stacked PRs:

    1. **WS0 spike** (slice-1) — empirically validate the event-pump on the #2906 reproducer (qwen3.7-max) and measure Qwen-route provider-cache TTL against the worst-case ~15-min BRC idle; output is a `docs/spike/2908-event-pump-spike.md` report.
    2. **cq-3 durable persistence schema + sync-flush + startup_reconciliation** (slice-2) — bump contract schemaVersion 1.2 → 1.3 with `pipeline.no_progress_budget` + `pipeline.parked_hitl` fields; add `_save_pipeline_durable()` / `commit_and_push_sync()` synchronous-flush variant that returns only after `git push origin <branch>` completes; add `_startup_reconciliation_replay_safety_budget` step that re-reads the durable budget from the on-branch contract on host boot and primes the in-memory `health_monitor.py` anchors. Closes the cq-3 durability gap risk_analyst named (R1).
    3. **Server-side next-action endpoint** (slice-3) — extend `consensus status --json` per architect open-decision od-3 to return `next_action` per role (review/ACK/NACK/propose/confirm/wait vs. all-confirmed), encoding open-NACK aggregation (#2142), conditional ACK, stale-version re-review (#2482), producer-first ordering (#2749), and resolve_obligation surface. Pure additive route work.
    4. **Net-new CLI verbs** (slice-4) — `egg-orch brc list-blocking`, `egg-contract get-context`, `egg-orch brc {get-state, resolve-obligation, read-peer-artifact}`, `egg-contract show --field <name>` (arbitrary contract-field projection wrapping `mcp__sdlc__show_contract` — used by slice-6 host-restart recovery), and optionally `egg-orch brc next-action` if slice-3's extension proves insufficient. All via `_handler_dispatch` so they share existing MCP handlers (preserves `tests/tools/test_mcp_cli_drift.py` invariant; no MCP retirement per cq-1).
    5. **Durable distilled memory artifact** (slice-5) — `.egg-state/agent-outputs/<role>/brc-memory.md` with structured sections (codebase/change model, per-producer assessment, decision log); **memory shape COMMITTED to distilled / rewrite-and-distill** per refine analysis lines 360-367; handler scaffolding in `sandbox/egg_agent_tools/handlers/brc.py` writes memory entries on every ACK/NACK off the existing `reason` + `files_reviewed` dict-arg payload (no shell-metachar exposure).
    6. **Event-pump rewrite + WS1/WS4/safety-budget consumer + legacy deletion** (slice-6) — rewrite `orchestrator/consensus_wrapper.py` as a deterministic event pump that invokes the existing `egg-orch message wait-loop` CLI directly (preserves #2323 cursor threading) and spawns the agent via `build_agent_command` per event (NOT `claude --print`); delete `MAX_CONSENSUS_RESTARTS=3`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT`, the SSE consensus.reached blocking path, and the restart loop (cq-4 — no flagged fallback); migrate heartbeat ownership from the agent's `_start_wait_loop_heartbeat` to the wrapper invoking `egg-orch message heartbeat` at the same 60s cadence (preserves #2036 and #2451); wire the safety-budget consumer against the durable schema landed in slice-2 with host-restart recovery via the slice-2 startup_reconciliation; host-restart suppresses re-firing of `alert_emitted=true` from durable state.
    7. **Prompt collapse + delta-scoped re-analysis + prior-fix audit** (slice-7) — replace STAY-ALIVE / wait-loop / cursor-threading text in `_build_brc_preamble` and `sandbox/agent-config/rules/*.md` with a lean event-handler contract, gated on an explicit prior-fix preservation audit table that classifies each fix as orchestrator-enforced / CLI-enforced / prompt-only and preserves only the prompt-only ones; extend the next-action endpoint to return `changed_artifacts` metadata so per-event context stays metadata-only (no inlined diffs per refine lines 168-177).
    8. **Qwen keep-warm (conditional)** (slice-8) — based on slice-1 measurement, either land a docs note ("no keep-warm needed") or implement a bounded Qwen-route-only keep-warm with HITL suppression and a hard refresh cap, or open a HITL decision if data is ambiguous.

    For operators: drain in-flight pipelines on the old wrapper before deploying slice-6's PR; new pipelines start on the event-pump immediately at deploy time. The structural seam is gone: a deterministic Bash loop, not the model, drives BRC waits and termination; no model can fall out by exiting between events. The per-cycle restart cost (~$1 / ~20 min) goes to zero on the failure modes #2906 surfaced.
  test_plan: |
    Automated:
    - slice-1: no tests; spike report at `docs/spike/2908-event-pump-spike.md` captures measurements.
    - slice-2: `tests/orchestrator/test_contract_schema_migration_to_1_3.py` covers migrator round-trip; `tests/orchestrator/test_save_pipeline_durable.py` covers sync-flush returns only after `git push origin <branch>` succeeds, fails-closed on push failure, and the production failure mode (emptyDir wipe + remote ref behind → fresh host loads the budget from `origin/<branch>`); `tests/orchestrator/test_startup_reconciliation_safety_budget.py` covers host-kill mid-budget reconciliation.
    - slice-3: `tests/orchestrator/test_consensus_next_action.py` covers producer/reviewer/dual-role lifecycles, open-NACK aggregation (#2142), conditional ACK, stale-version re-review (#2482), resolve_obligation, producer-first ordering (#2749).
    - slice-4: `tests/sandbox/egg_lib/test_brc_cli_verbs.py` covers handler-import parity, JSON payload byte-equality, stdin/file-path rule for prose-bearing inputs (#2741 regression guard); `tests/tools/test_mcp_cli_drift.py` extended for the new CLI shims.
    - slice-5: `tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py` covers distilled rewrite-and-distill, idempotency, partial-file recovery, dict-arg side-effect, write-permission failure non-fatal; `integration_tests/local_pipeline/test_brc_memory_handler_e2e.py` covers ACK through live MCP server → memory file at correct per-role path.
    - slice-6 unit: extend `tests/orchestrator/test_consensus_wrapper.py` for event-pump reach-consensus, clean-exit no-FAIL, wrapper-emitted heartbeats, `egg-orch consensus status --json` replaces SSE consumer, and fix-lineage assertions (cursor #2323, gateway keep-alive #2451, heartbeat #2036, open-NACK barrier #2142 surfaced through next-action, pre-confirm-wait #2064/#2482, gap-race #1995); `tests/orchestrator/test_event_pump_host_restart.py` asserts host-restart with recovered `alert_emitted=true` does NOT re-fire the OVERSEER_ALERT.
    - slice-6 integration: `integration_tests/local_pipeline/test_event_pump_qwen_repro.py` runs the #2906 reproducer end-to-end under the event-pump and asserts no churn, memory populated, permissions enforced.
    - slice-7: `tests/orchestrator/test_brc_preamble.py` snapshot-asserts the lean preamble + the prior-fix audit table; `tests/sandbox/egg_agent_tools/handlers/test_brc_delta.py` asserts metadata-only delta contract.
    - slice-8 (conditional, path b only): `tests/orchestrator/test_qwen_keep_warm.py` covers Qwen-route activation, HITL/awaiting-human suppression, hard refresh cap.

    Manual:
    - After slice-4, run `egg-orch --help` and `egg-contract --help` to confirm new verbs are discoverable.
    - After slice-6, run `make test-all` (or the `local_pipeline` subset) and inspect orchestrator status to confirm wrapper-emitted heartbeats land at the expected cadence.
    - After slice-8 path-b, simulate a long awaiting-human HITL gate and confirm keep-warm is suppressed during the wait.
  manual_steps: |
    Pre-merge: none — every change ships behind the BRC cycle's review gate; no `.github/` files are touched.

    Post-merge:
    - slice-2: schema bump auto-applies on next contract load via the existing `_migrate_schema_version_to_1_3` migrator pattern; no manual step.
    - slice-6: **CUTOVER PLAYBOOK** — drain in-flight pipelines on the old capped-restart wrapper before deploying slice-6's PR (operator monitors `egg-orch pipeline status` for all active pipelines, waits for them to reach `is_complete=true` or quiesce). New pipelines start on the event-pump immediately at deploy time. There is no flagged fallback per cq-4. Documented in `docs/architecture/brc-event-pump.md` per TASK-6-12.
    - slice-8 path-b only: redeploy the litellm container so the new keep-warm route configuration takes effect.
slices:
  - id: 1
    name: |-
      WS0 de-risking spike — event-pump prototype + Qwen cache TTL measurement on #2906 repro
    goal: |-
      Empirically validate the event-pump approach end-to-end
      before committing to the production rewrite. Build a
      minimal throwaway wrapper that loops `python3 -m
      egg_agent` (via the SDK entry point already invoked by
      `egg_agent.build_agent_command()` at orchestrator/
      consensus_wrapper.py:748-759 — NOT `claude -p`, which
      is the EGG100-linted anti-pattern per docs/guides/
      agent-mode-design.md:90-104) per BRC event against the
      existing `egg-orch message wait-loop` + existing MCP
      tool surface, and run it on the #2906 reproducer
      (issue-2270, qwen3.7-max). Three gating outputs land
      in this slice as a `docs/spike/` report (architect /
      doc-updater write-scoped): (a) consensus reached
      without the 3-restart churn that motivated this issue,
      with bounded per-event context and memory file
      populated + consulted; (b) Qwen-route provider-cache
      survival measurement across the full worst-case BRC
      idle (~15 min target — observed BRC idles reached
      10-13 min for a producer composing a response to a
      NACK), sourced from the litellm / `cost_callback`
      state files (`~/.local/state/clm/cost-*.json`) because
      Claude Code's `usage.*` returns all-zero on the Qwen
      route; (c) per-event cold-read cost on the Qwen route
      across consecutive per-event invocations and across
      the injected long idle, used by slice-8 to decide
      whether bounded keep-warm is needed. Anthropic-route
      TTL is already cleared in #2908's WS7 empirical
      comment (1h, env in uncached suffix) — no additional
      measurement needed there. The spike's prototype is
      throwaway; nothing it writes is shipped to production
      code paths. Findings shape slice-5 memory shape (only
      as a *check* against the architect's committed
      distilled-default — see slice-5) and slice-8 cache
      decisions. Downstream slices 2-7 do NOT structurally
      depend on slice-1's empirical numbers; they form a
      single linear chain because the forest constraint
      requires it and because each slice consumes the
      previous slice's deliverable (schema, endpoint, CLI,
      memory). The spike runs first because it is the
      cheapest way to falsify the design before downstream
      slices commit to the file boundaries.
    tasks:
      - id: TASK-1-1
        description: |-
          Build the throwaway event-pump prototype script under `scripts/spike/2908_event_pump_prototype.sh`. The script must (a) loop on `egg-orch message wait-loop --timeout 60` to mimic the production wrapper; (b) on each actionable BRC event, invoke `python3 -m egg_agent` via the existing `shared/egg_agent/command.py:build_agent_command` entry point — **NOT** `claude --print`, which is an EGG100 anti-pattern per `docs/guides/agent-mode-design.md:90-104` and which the refine-phase analysis explicitly corrects; (c) carry a minimal in-script memory of prior actions across iterations (a tmp-file is sufficient for the spike — durable file format is decided in slice-5); (d) exit cleanly when the orchestrator reports the role consensus-confirmed and `is_complete`. The script is throwaway; no test coverage is required.
        acceptance: |-
          `scripts/spike/2908_event_pump_prototype.sh` exists; `grep -n 'claude --print\|claude -p\b'` returns zero hits in the script; running it against a mock orchestrator that emits one CONSENSUS_PROPOSE → ACK → CONSENSUS_CONFIRMED sequence completes without a Python traceback and exits 0; the script invokes `python3 -m egg_agent`, verifiable via `grep -n`.
        role: coder
        files:
          - scripts/spike/2908_event_pump_prototype.sh
      - id: TASK-1-2
        description: |-
          Run TASK-1-1's prototype against the #2906 reproducer (issue-2270, qwen3.7-max in a k3s test cluster). Drive a single BRC cycle to CONFIRMED. Capture wall-clock per-event, agent exit status, and the count of CONSENSUS_RE_REVIEW events (which under the old wrapper would trigger restarts). The run output is the raw evidence input to TASK-1-4's report.
        acceptance: |-
          A run log (raw orchestrator pipeline-status output + per-event timings + cost-callback log scrape) is committed under `scripts/spike/2908_run_log.txt`; the run reaches CONSENSUS_CONFIRMED without the wrapper restarting the agent; no "Agent exited without BRC consensus" message appears in the run log.
        role: coder
        files:
          - scripts/spike/2908_run_log.txt
      - id: TASK-1-3
        description: |-
          Instrument Qwen-route provider-cache survival across the full worst-case BRC idle. **Trust-boundary correction**: the refine-phase analysis references `~/.local/state/clm/cost-*.json` but `grep -rn` for that path in the repo returns zero hits — the LiteLLM cost callback at `config/litellm/cost_callback.py` emits structured log lines (no on-disk JSON files in the agent pod). This task must source Qwen cache-read counts from the litellm container's structured logs (via `kubectl logs deployment/egg-litellm` or equivalent) rather than from a path inside the agent pod. Inject a synthetic idle of ~15 minutes (cover the observed 10-13 min worst-case BRC idle) between two consecutive `python3 -m egg_agent` invocations on the Qwen route; record `prompt_tokens`, `cache_read_tokens`, `cache_creation_tokens` for the second invocation. Repeat for 5.5 min and 10 min idles to bracket the TTL ceiling.
        acceptance: |-
          A measurements file at `scripts/spike/2908_qwen_cache_measurements.json` (or `.csv`) records at least 3 idle durations (5.5, 10, 15 min) with the cache_read_tokens / cache_creation_tokens ratio at each; the file is committed under coder allowlist (scripts/, not docs/). The measurements come from the litellm container's structured logs, not from a path inside the agent pod.
        role: coder
        files:
          - scripts/spike/2908_qwen_cache_measurements.json
      - id: TASK-1-4
        description: |-
          Write the spike report at `docs/spike/2908-event-pump-spike.md` summarising TASK-1-2 and TASK-1-3 findings. The report must explicitly answer: (a) did the event-pump prototype reach consensus on the #2906 repro without the 3-restart churn? (b) what Qwen-route TTL ceiling did the measurements show — survives ≥ 15 min, lapses between 5.5 and 15 min, or ambiguous? (c) what per-event cold-read cost would the production wrapper incur given the measured TTL? The report also records the **check** against the slice-5 distilled-memory commitment — does the measured cache burst per ACK/NACK materially bust the Qwen prefix cache? slice-5 commits to distilled at plan time; this report's role is to validate the commitment, not gate it.
        acceptance: |-
          `docs/spike/2908-event-pump-spike.md` exists; explicitly states one of {survives ≥ 15 min, lapses 5.5–15 min, ambiguous}; lists the per-event cold-read cost in tokens; includes a "Distilled-memory check" section that says either "distilled commitment validated by measurement" or "distilled commitment at risk because <numeric finding>".
        role: documenter
        files:
          - docs/spike/2908-event-pump-spike.md
    parent_slice_id: null
  - id: 2
    name: |-
      cq-3 durable HITL + no-progress-budget persistence schema (sync-flush + startup_reconciliation)
    goal: |-
      Land the durable server-side persistence schema that
      replaces the in-memory + best-effort-async-push
      primitive (orchestrator/state_store.py:11 — "async
      push via a daemon thread", :804-805 "Best-effort async
      push to remote after every commit", :890-928 debounced
      thread, contract_store.py:139-142 — origin/<branch>
      "reflects the last pushed state" and explicit failure
      mode "pre-push crashes"). cq-3's HARD requirement is
      that the parked HITL decision AND the no-progress
      safety-budget state survive SDLC-host failure. The
      existing best-effort-async-push primitive does NOT
      meet that bar: save_pipeline returns success after a
      local commit; if the host crashes before the async
      daemon pushes, the emptyDir-backed pod loses the
      local commit and load_contract_from_branch returns
      None on the next host. Three additions:
      (a) New `_save_pipeline_durable(state)` /
      `commit_and_push_sync(state)` variant of save_pipeline
      that returns ONLY after `git push origin <branch>`
      completes successfully (synchronous, blocking) —
      used for HITL-decision-resolution writes AND
      safety-budget-state-transition writes. The
      acceptable latency cost is one remote-push round trip
      per rare mutation, not per every contract write.
      Other contract writes keep the existing
      best-effort-async path so high-frequency writes are
      not regressed. The split is at the call-site, not at
      the helper level — callers explicitly opt in to
      sync-flush. (b) New contract fields
      `pipeline.no_progress_budget` (durable counter +
      threshold + state) and `pipeline.parked_hitl` (the
      currently-parked decision + its resume token), landed
      via the existing schemaVersion 1.2 migrator pattern;
      defaults preserve current behavior (None/0). (c) New
      `_startup_reconciliation_replay_safety_budget` step on
      orchestrator boot that re-reads the durable
      `pipeline.no_progress_budget` from the on-branch
      contract for every active pipeline and primes the
      in-memory health_monitor.py:82-103 anchors (currently
      initialised-to-zero on every restart) so a fresh host
      monitors the same budget the previous host was
      tracking. No consumers wired yet — schema and the
      sync-flush helper land here; slice-6 consumes both.
      Tests: contract round-trips through migrator without
      data loss; sync-flush fails-closed if the push fails
      (caller sees error, can retry); host-kill mid-budget
      → fresh host loads same budget state via
      reconciliation. Hard constraint inherited from cq-3:
      the test for host-recoverability is REQUIRED to pass
      against the new primitive (a fresh host must resume
      from the persisted state); failing here means the
      sole safety net under cq-4 is broken before it
      ships.
    tasks:
      - id: TASK-2-1
        description: |-
          Bump the contract schema from 1.2 → 1.3 in `shared/egg_contracts/models.py` to add two durable fields under `pipeline`: (a) `pipeline.no_progress_budget` — an object holding `{remaining_seconds: int, last_progress_at: ISO8601 | null, threshold_seconds: int, alert_emitted: bool}` so a restarted SDLC host can resume monitoring the same budget rather than reseting it on host death (cq-3 HARD requirement); (b) `pipeline.parked_hitl` — an object holding `{decision_id: str, parked_at: ISO8601, options: list[str], selected: str | null, wake_on: list[str], resume_token: str}` so a restarted host can find the awaiting-human gate and resume waiting on it. Add a `_migrate_schema_version_to_1_3(contract: dict) -> dict` helper following the existing migrator pattern. Defaults preserve current behaviour (`no_progress_budget = None`, `parked_hitl = None`).
        acceptance: |-
          Existing contracts at schemaVersion 1.2 load + migrate + round-trip to 1.3 without data loss; `tests/orchestrator/test_contract_schema_migration_to_1_3.py` (TASK-2-4) covers the migrator. Schema version constant in `shared/egg_contracts/models.py` bumps to `"1.3"`.
        role: coder
        files:
          - shared/egg_contracts/models.py
          - shared/egg_contracts/loader.py
      - id: TASK-2-2
        description: |-
          Add the sync-flush variant `_save_pipeline_durable(state)` (or equivalently `commit_and_push_sync(state)`) in `orchestrator/state_store.py` alongside the existing async-best-effort `save_pipeline`. The new method returns ONLY after `git push origin <branch>` completes successfully (synchronous, blocking) — the implementation calls the same local-commit path as `save_pipeline` but inlines the push (no daemon thread) and surfaces push failure to the caller. The existing async path stays the default for high-frequency writes — split is at the call-site, not at the helper level. Use the existing `gateway_client.push_branch` (or equivalent) so role-write boundary enforcement is preserved. Concurrent-write safety follows existing `save_pipeline` patterns (lock acquisition order matches).
        acceptance: |-
          `_save_pipeline_durable` (or `commit_and_push_sync`) exists with signature `(state, /, *, branch=None)`; calling it on a happy path returns only after `git push` reports success; on push failure, the method raises a clear exception; existing async `save_pipeline` calls are unchanged; `tests/orchestrator/test_save_pipeline_durable.py` (TASK-2-5) covers the happy path, the push-failure fail-closed path, and the production failure-mode (emptyDir wipe + remote ref behind → fresh host loads from `origin/<branch>`).
        role: coder
        files:
          - orchestrator/state_store.py
      - id: TASK-2-3
        description: |-
          Add `_startup_reconciliation_replay_safety_budget` step in `orchestrator/startup_reconciliation.py` (existing file) that on orchestrator boot iterates the active pipelines on the contract store and for each: (i) reads the durable `pipeline.no_progress_budget` from `load_contract_from_branch` (`orchestrator/contract_store.py:139-142` — origin-first); (ii) primes the in-memory `health_monitor.py:82-103` anchors with the recovered state; (iii) honours `alert_emitted=true` from durable state so a fresh host does NOT re-fire the OVERSEER_ALERT for an already-alerted budget. Add a re-prime entry point on `health_monitor.py` that accepts the recovered budget state.
        acceptance: |-
          The new step runs on orchestrator boot; `tests/orchestrator/test_startup_reconciliation_safety_budget.py` (TASK-2-5) simulates host-kill mid-budget → fresh host primes the same budget state from `origin/<branch>`; an `alert_emitted=true` recovered state is honoured (no re-fire).
        role: coder
        files:
          - orchestrator/startup_reconciliation.py
          - orchestrator/health_monitor.py
      - id: TASK-2-4
        description: |-
          Write a unit test at `tests/orchestrator/test_contract_schema_migration_to_1_3.py` covering the `_migrate_schema_version_to_1_3` helper. Cases: existing 1.2 contract with no extra state → 1.3 with `no_progress_budget=None` + `parked_hitl=None`; 1.2 contract with arbitrary other fields round-trip (load → save → load) without corruption; 1.3 → 1.3 is a no-op; non-1.2 inputs raise a clear error.
        acceptance: |-
          All four cases pass; `make test` passes.
        role: tester
        files:
          - tests/orchestrator/test_contract_schema_migration_to_1_3.py
      - id: TASK-2-5
        description: |-
          Write two tests: (a) `tests/orchestrator/test_save_pipeline_durable.py` covering: happy path returns only after `git push` succeeds; mock `git push` failure → method raises and the caller observes the error; the **production failure-mode** test — write durably, simulate an emptyDir wipe (clear the local working tree state, drop the local branch ref), spawn a fresh reader, assert the reader recovers the durable fields from `origin/<branch>` via `load_contract_from_branch`. (b) `tests/orchestrator/test_startup_reconciliation_safety_budget.py` covering: host-kill mid-budget → fresh host loads same budget state via `_startup_reconciliation_replay_safety_budget`; `alert_emitted=true` recovered state does NOT re-fire OVERSEER_ALERT.
        acceptance: |-
          All listed cases pass; the production failure-mode test explicitly simulates emptyDir wipe + remote ref behind (NOT just process-exit-same-FS); `make test` passes both files.
        role: tester
        files:
          - tests/orchestrator/test_save_pipeline_durable.py
          - tests/orchestrator/test_startup_reconciliation_safety_budget.py
    dependencies:
      - slice-1
    parent_slice_id: 1
  - id: 3
    name: |-
      Server-side next-action endpoint (orchestrator/routes — pure additive)
    goal: |-
      Land the `consensus next-action --role R` HTTP
      endpoint (or extension of `consensus status --json`
      per open decision od-3) on the orchestrator that
      returns the next action the role should take —
      review/ACK/NACK/propose/confirm/wait vs. all-confirmed
      — given current BRC state. Judgment stays with the
      agent; the endpoint owns sequencing only. Lifecycle
      decoding requirements (must be covered by the
      endpoint's return values + unit tests):
      dual-role producer-first ordering (#2749), open-NACK
      barrier (#2142), conditional ACK paths,
      stale-version re-review (#2482), resolve_obligation
      surface. The endpoint MAY surface the slice-2
      durable `no_progress_budget` state in its response so
      the wrapper's safety-budget consumer (slice-6) reads
      it via a single endpoint call rather than two
      separate fetches. If extension of `consensus status
      --json` is sufficient (verified against the lifecycle
      requirements above), prefer that — od-3 explicitly
      defers to "verify against the next-action derivation
      requirements". Pure additive route work; no consumer
      changes; lands under orchestrator/routes/ and is
      backwards-compatible with current callers. Tests: one
      per lifecycle-state branch listed above; producer-
      first ordering exercised via dual-role agent fixture.
    tasks:
      - id: TASK-3-1
        description: |-
          Extend the existing `consensus status` HTTP payload (consumed by `cmd_consensus_status` at `sandbox/egg_lib/orch_cli.py:2783` and ultimately by `handlers/brc.py:679` `brc_get_state` against `/api/v1/pipelines/<pid>/status`) to additionally return a `next_action` field per role under the agent matrix. Value is one of: `propose`, `ack_required`, `nack_required`, `re_review_required`, `confirm_required`, `wait_on_peers`, `wait_on_human`, `address_nacks`, `complete`, derived server-side from the existing matrix in `orchestrator/routes/signals.py` handlers (`:1204` propose, `:1428` ack, `:1559` nack, `:1806` confirmed). The derivation must encode: (i) open-NACK aggregation barrier (#2142) — `next_action: address_nacks` when ≥2 distinct reviewers have NACKed the current version; (ii) stale-version re-review (#2482) — `next_action: re_review_required` plus a bumped `current_version` + `last_observed_version` so the agent can detect a re-propose race; (iii) dual-role producer-first ordering (#2749); (iv) resolve_obligation surface — `next_action: resolve_obligation` when a conditional-ACK obligation is in-cycle and the producer can resolve it; (v) confirm-precondition met → `confirm_required` (the directed nudge case from #2531). The endpoint MAY additionally surface `pipeline.no_progress_budget` state in the same payload so slice-6's safety-budget consumer reads it in one call. **Per architect open-decision od-3**: prefer extending `consensus status` over a new verb; only add an `egg-orch brc next-action` shim (slice-4 TASK-4-3) if extension proves insufficient.
        acceptance: |-
          `consensus status --json --role <R>` returns a `next_action` key under each agent matrix entry; for a producer with ≥2 unresolved reviewer NACKs the value is `address_nacks`; for a reviewer whose target producer has not re-proposed since the prior ACK the value is `wait_on_peers`; for a dual-role agent the producer phase derivation takes precedence per #2749; unit tests at `tests/orchestrator/test_consensus_next_action.py` (TASK-3-3) cover all five branches above.
        role: coder
        files:
          - orchestrator/routes/signals.py
          - orchestrator/routes/pipelines.py
          - sandbox/egg_agent_tools/handlers/brc.py
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-3-2
        description: |-
          Extend the same endpoint to optionally include `pipeline.no_progress_budget` + `pipeline.parked_hitl` state (the slice-2 fields) in the payload, gated on a query parameter (e.g. `?include=durable_state`). The wrapper's safety-budget consumer (slice-6 TASK-6-5) uses this to read budget + parked-HITL in a single call rather than two separate fetches. Schema documented in the endpoint's docstring; the response is backwards-compatible when the query param is absent.
        acceptance: |-
          `consensus status --json --role R --include durable_state` returns the slice-2 durable fields under a `durable_state` key; absent the query param, the response is byte-identical to the baseline (backwards-compat); unit test at `tests/orchestrator/test_consensus_next_action.py` covers the optional include.
        role: coder
        files:
          - orchestrator/routes/signals.py
          - orchestrator/routes/pipelines.py
      - id: TASK-3-3
        description: |-
          Write unit tests for the next-action derivation landed in TASK-3-1 and TASK-3-2 at `tests/orchestrator/test_consensus_next_action.py`. Coverage must include: producer with no NACKs → `propose` (initial) or `wait_on_peers` (post-propose); producer with one reviewer NACK → `address_nack`; producer with ≥2 reviewer NACKs → `address_nacks` (open-NACK barrier #2142); reviewer with target producer at unreviewed version → `ack_required` or `nack_required` depending on review-graph; reviewer whose ACK is stale (producer re-proposed) → `re_review_required` (#2482); dual-role agent producer-first ordering (#2749); confirm-precondition met → `confirm_required` (#2531 directed nudge case); resolve_obligation surface; all confirmed → `complete`; `?include=durable_state` query param round-trip.
        acceptance: |-
          All listed cases have a unit test; tests pass under `make test`; coverage on the next-action derivation function ≥ 95% lines.
        role: tester
        files:
          - tests/orchestrator/test_consensus_next_action.py
    dependencies:
      - slice-2
    parent_slice_id: 2
  - id: 4
    name: |-
      Net-new CLI verbs (`egg-orch brc list-blocking`, `egg-contract get-context`, `egg-orch brc next-action`) — sandbox/egg_lib only
    goal: |-
      Wrap the slice-3 next-action endpoint and the existing
      `mcp__brc__list_blocking` / `mcp__phase__get_context`
      handlers with CLI verbs that satisfy the issue body's
      WS8 net-new-CLI requirement (cq-1 resolution: build
      any net-new CLI verbs the new wait-loop / event-pump
      depends on; do NOT delete existing MCP tools — that
      collapse is the cq-1 follow-up). All new verbs land
      under sandbox/egg_lib/orch_cli.py and contract_cli.py
      using `_handler_dispatch` so they share the existing
      MCP handlers (no logic duplication; preserves the
      existing tests/tools/test_mcp_cli_drift.py invariant).
      Hard constraint inherited from WS8 in the issue body
      and from constraint #6 below: any new
      `consensus propose / ack / nack` prose-bearing CLI
      input added here MUST accept text via stdin OR a
      file path, NEVER argv — argv routes prose through
      Bash's `bash -c`, reintroducing the shell-metachar
      corruption mitigated in #2741. The verbs added here
      do not include propose/ack/nack themselves (those
      already exist at orch_cli.py:2528/:2633/:2692) but
      any net-new prose-bearing verbs follow the same rule.
      Tests: drift-test extension covers new verbs;
      stdin/file round-trip preserves prose containing
      shell metachars.
    tasks:
      - id: TASK-4-1
        description: |-
          Add a new `brc` subparser to `sandbox/egg_lib/orch_cli.py` (root `egg-orch brc ...`) and a `list-blocking` subcommand that wraps the existing `brc_list_blocking` handler at `sandbox/egg_agent_tools/handlers/brc.py:726`. Use `_handler_dispatch` so the CLI and MCP tool share a single handler call path. Flip the `TOOL_REGISTRY` entry for `mcp__brc__list_blocking` from `cli_command=None` to the new verb name.
        acceptance: |-
          `egg-orch brc list-blocking --pipeline <pid>` prints the same JSON the MCP tool returns; `make test` passes `tests/tools/test_mcp_cli_drift.py`; the registry entry's `cli_command` is no longer `None`.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-4-2
        description: |-
          Add `egg-contract get-context` CLI verb that wraps the existing `phase_get_context` handler at `sandbox/egg_agent_tools/handlers/phase.py:139`. Use `_handler_dispatch` for parity. Flip the `TOOL_REGISTRY` entry for `mcp__phase__get_context` from `cli_command=None` to the new verb name. Note `phase_get_context` returns a fixed bundle (no arbitrary field projection); for arbitrary contract-field projection, see TASK-4-5.
        acceptance: |-
          `egg-contract get-context --pipeline <pid>` prints the same payload the MCP tool returns; drift test green; registry entry's `cli_command` is no longer `None`.
        role: coder
        files:
          - sandbox/egg_lib/contract_cli.py
          - sandbox/egg_agent_tools/tools/phase.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-4-3
        description: |-
          (Conditional on slice-3 outcome.) If slice-3 TASK-3-1 extended `consensus status --json` to carry `next_action` and reviewers confirm the extension is sufficient (architect open-decision od-3 preferred path), this task is **not-applicable** — the slice ships without an `egg-orch brc next-action` shim. If slice-3's extension proved insufficient, add `egg-orch brc next-action --role <R>` CLI verb under the new `brc` subparser, wrapping the new endpoint's handler. Document the decision in the slice-4 PR description.
        acceptance: |-
          Either (a) the task is marked not-applicable in the propose summary with a brief rationale "consensus status extension carries next_action cleanly" (preferred), OR (b) `egg-orch brc next-action --role R` reachable via `--help`; drift test green.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-4-4
        description: |-
          Add `egg-orch brc resolve-obligation`, `egg-orch brc read-peer-artifact`, and `egg-orch brc get-state` CLI verbs under the new `brc` subparser landed in TASK-4-1, wrapping `brc_resolve_obligation` (handlers/brc.py:743), `brc_read_peer_artifact` (handlers/brc.py:901), and `brc_get_state` (handlers/brc.py:679). All via `_handler_dispatch`. Flip the registry entries: `mcp__brc__resolve_obligation` from `cli_command=None` to `brc resolve-obligation`; `mcp__brc__read_peer_artifact` from `cli_command=None` to `brc read-peer-artifact`; `mcp__brc__get_state` (already reachable indirectly via `consensus status`) gets a direct verb. Update `tests/tools/test_mcp_cli_drift.py:28` documented-gaps list accordingly.
        acceptance: |-
          All three verbs reachable via `--help`; drift test green; the documented-gaps list in the drift test no longer includes `brc__read_peer_artifact` or `brc__resolve_obligation`.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-4-5
        description: |-
          Add `egg-contract show --field <name>` CLI verb that wraps `mcp__sdlc__show_contract`'s field-projection capability (the MCP tool already supports a `fields=[...]` parameter; only the CLI shim is missing). The wrapper's host-restart recovery (slice-6 TASK-6-9) uses this verb to read `pipeline.no_progress_budget` and `pipeline.parked_hitl` durable fields without depending on the fixed `phase_get_context` bundle. Implementation: locate the existing `sdlc_show_contract` handler (or equivalent) and add a CLI shim that accepts repeated `--field <dotted.path>` arguments and returns a JSON object containing only the named fields. Use `_handler_dispatch`. **This addresses reviewer_plan blocking item B3** — TASK-6-9 cannot use `egg-contract get-context --field ...` because `phase_get_context` returns a fixed bundle, not arbitrary projection.
        acceptance: |-
          `egg-contract show --pipeline <pid> --field pipeline.no_progress_budget --field pipeline.parked_hitl` returns a JSON object with those two top-level keys; drift test green; the new verb flips `mcp__sdlc__show_contract`'s registry entry to a CLI command.
        role: coder
        files:
          - sandbox/egg_lib/contract_cli.py
          - sandbox/egg_agent_tools/tools/sdlc.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-4-6
        description: |-
          Write unit tests for the new CLI verbs at `tests/sandbox/egg_lib/test_brc_cli_verbs.py` covering: invocation reaches the handler (handler-import parity); `--json` output matches the corresponding MCP tool's payload byte-for-byte; error paths (missing args, invalid pipeline) return non-zero exit; stdin/file-path rule check — if any of the new CLI verbs accept prose-bearing arguments (e.g. `resolve-obligation --note`), the test confirms the input takes `--note-file` or stdin rather than argv (regression guard against #2741); `egg-contract show --field` returns the requested fields and only those (TASK-4-5).
        acceptance: |-
          All listed cases have a unit test; `make test` passes.
        role: tester
        files:
          - tests/sandbox/egg_lib/test_brc_cli_verbs.py
      - id: TASK-4-7
        description: |-
          Update `tests/tools/test_mcp_cli_drift.py` to reflect the new CLI verbs (TASK-4-1 to TASK-4-5). Specifically: flip the parity-matrix expectations so `mcp__brc__list_blocking`, `mcp__brc__get_state`, `mcp__brc__resolve_obligation`, `mcp__brc__read_peer_artifact`, `mcp__phase__get_context`, and `mcp__sdlc__show_contract` have non-None `cli_command` values; ensure `test_cli_less_tools_are_documented_gaps` reflects the new state (only `task__mark_gap` and any remaining MCP-only tools remain in the gap list).
        acceptance: |-
          `make test` passes `test_mcp_cli_drift.py`; the post-update gap list contains only the remaining MCP-only tools.
        role: tester
        files:
          - tests/tools/test_mcp_cli_drift.py
    dependencies:
      - slice-3
    parent_slice_id: 3
  - id: 5
    name: |-
      `brc-memory.md` artifact (distilled default) + brc_ack/brc_nack handler memory-write scaffolding (dict-arg path)
    goal: |-
      Define the durable per-role memory file that carries
      continuity between per-event `python3 -m egg_agent`
      invocations and wire its writes into the handler
      layer that already runs on every ACK/NACK. Three
      pieces: (a) the file format — markdown with
      structured sections (codebase/change model,
      per-producer assessment, decision log) anchored by
      stable headings so the agent can read-modify-write
      deterministically; written to `.egg-state/agent-
      outputs/<role>/brc-memory.md` (existing allowlist
      covers this prefix for EVERY BRC-participating
      role's allowlist per shared/egg_restrictions/
      patterns.py:362-516+, and matchers.py:33 confirms
      prefix-glob semantics — `<prefix>/` matches any file
      under the prefix including subdirs); (b) handler
      scaffolding in sandbox/egg_agent_tools/handlers/
      brc.py: brc_ack and brc_nack already receive
      `reason` + `files_reviewed` via the **dict-arg
      handler interface** (sandbox-side in-process MCP, NOT
      argv) so the handler appends a structured entry to
      the memory file on each ACK/NACK without any
      possibility of #2741-class shell-metachar corruption
      — the reviewer_plan non-blocking note (4) requires
      this be stated explicitly; (c) ephemeral-vs-durable
      retention treatment per refine feedback Q2:
      brc-memory.md is treated as ephemeral coordination
      state cleaned up with the pod; the orchestrator
      message history is the durable reconstruction
      backstop. The hard caveat from refine Q2 stands: if
      implement-time evidence shows the memory file is
      load-bearing for correct recovery after host/pod
      restart, this slice is revisited and the artifact is
      made durable instead. **Memory shape is COMMITTED to
      distilled / rewrite-and-distill (NOT punted to a
      decision rule against slice-1 data)** per the
      refine-phase analysis at lines 360-367: "an
      unbounded append-only memory eventually starts
      *constraining* what the per-event agent can attend
      to (sea-of-context), while a distilled memory keeps
      'small summaries that orient' as the target shape".
      The rationale spans BOTH axes: (axis-1 cache cost)
      distilled rewrites burst the per-event prefix on
      each ACK/NACK but the burst is bounded by a section
      template so the size + frequency are predictable;
      slice-1's spike measurement is a *check* on whether
      that burst materially busts the Qwen prefix cache,
      not the gating decision; (axis-2 orient-don't-
      constrain per docs/guides/agent-mode-design.md)
      distilled summaries orient without flooding the
      per-event agent's attention. If slice-1's WS0
      measurement shows distilled bursts materially bust
      cache on Qwen, slice-5 MAY add a bounded
      append-only tail with explicit compaction — but the
      *default* is distilled and the decision is made
      now. No consumer changes yet — the wrapper still
      drives the agent the old way; the agent just
      maintains its memory file on each ACK/NACK so by the
      time slice-6 lands, the memory file is already
      populated for the first per-event invocation. The
      first per-event invocation in slice-6 reads the
      populated memory.
    tasks:
      - id: TASK-5-1
        description: |-
          Document the brc-memory.md file format in a docstring at the top of `sandbox/egg_agent_tools/handlers/brc.py` (or in a new helper module `sandbox/egg_agent_tools/brc_memory.py` if the format helpers grow beyond a single function). The format is markdown with stable headings: `## Codebase / change model`, `## Per-producer assessment`, `## Decision log`. The memory shape is **distilled / rewrite-and-distill** (committed at plan time per the architect v2 slice-5 goal and refine analysis lines 360-367); export `BRC_MEMORY_PATH_TEMPLATE = ".egg-state/agent-outputs/{role}/brc-memory.md"` as a helper constant. The docstring cites both axes (cache cost + orient-don't-constrain).
        acceptance: |-
          The memory shape is named in code as `distilled` (a constant or docstring); the file path template is exported; the docstring cites the refine analysis rationale spans both axes.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-5-2
        description: |-
          Add a `_append_brc_memory_entry(role, producer_role, version, verdict, reason, files_reviewed)` helper in `sandbox/egg_agent_tools/handlers/brc.py` (or `brc_memory.py`). The helper reads the existing memory file (creating it from a template if missing), parses the structured sections, appends a decision-log entry under `## Decision log`, and **compacts** the `## Per-producer assessment` section by collapsing prior entries for the same producer + role pair into a single distilled summary (rewrite-and-distill behavior committed in TASK-5-1). Write the file atomically (write-temp + rename). Errors writing the memory file must be non-fatal (log at WARNING + continue) — the memory file is ephemeral coordination state and a write failure must NOT block consensus.
        acceptance: |-
          The helper exists with the named signature; calling it twice in a row produces a deterministic file; the file path resolves under `.egg-state/agent-outputs/<role>/brc-memory.md`; an idempotency test passes (replaying the same entry yields no duplicate decision-log line); the per-producer assessment section compacts on each call.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-5-3
        description: |-
          Wire `_append_brc_memory_entry` into `brc_ack` and `brc_nack` in `sandbox/egg_agent_tools/handlers/brc.py`. Both handlers already receive `req: dict[str, Any]` with `reason`, `files_reviewed`, `producer_role` (verify against the existing handler payload schema — read `req.get("producer_role")` and `req.get("reason")` and `req.get("files_reviewed")`) plus a `version` field for the proposal version reviewed. The memory append fires AFTER the orchestrator returns success (a rejected ACK/NACK does not pollute the memory). Because the input is the **dict-arg handler interface** (in-process MCP, NOT argv), there is no possibility of #2741-class shell-metachar corruption — call this out in the docstring.
        acceptance: |-
          `brc_ack` and `brc_nack` call the memory helper on success path only; a unit test confirms a write-permission error on the memory file does NOT cause the handler to raise; the consensus action still succeeds; the handler reads its inputs from the existing `req: dict` schema; docstring notes the dict-arg path eliminates the shell-metachar exposure.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-5-4
        description: |-
          Document the ephemeral retention treatment AND the R13 cross-role memory READ allowlist verification in `docs/architecture/orchestrator.md` (or a new `docs/architecture/brc-event-pump.md` placeholder section if the architect doc is landed in slice-6). Make explicit: (a) brc-memory.md is ephemeral coordination state; reconstruction backstop is the orchestrator message history (already durable per `mcp__brc__read_peer_artifact` and the durable BRC history at `.egg-state/brc-history/<id>-<phase>.json`); if recovery ever proves to depend on the memory file, slice-5 is revisited and the artifact made durable; cite refine feedback Q2's hard caveat. (b) Cross-role READS are NOT gated — the file-write interceptor at `shared/egg_agent/tool_interceptor.py:17` (`_WRITE_TOOLS`) only covers Write/Edit/NotebookEdit; reads use the standard Read tool with no role check. The implication for memory: dual-role agents can read both their producer and reviewer memory files (intentional, not a leak); single-role agents read only their own. Document the design intent so coders don't add an unintended read-gate.
        acceptance: |-
          A documenter-visible paragraph in `docs/architecture/orchestrator.md` explicitly names brc-memory.md as ephemeral, cites refine Q2's caveat, AND documents the cross-role read allowlist semantics (writes gated, reads not; dual-role agents can read both files).
        role: documenter
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-5
        description: |-
          Write unit tests at `tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py` covering: `_append_brc_memory_entry` creates the file from a template when missing; appending preserves stable headings; idempotency (same entry twice → no duplicate); partial-file recovery (truncated file at section boundary → next append rebuilds it); distilled rewrite-and-distill behavior on the per-producer assessment section (replay multiple ACKs from same producer → single distilled entry); `brc_ack` / `brc_nack` dict-arg side-effect (handler call → memory entry); write-permission error path (mock write failure → handler still succeeds, warning logged).
        acceptance: |-
          All seven listed cases have a unit test; `make test` passes; coverage on `_append_brc_memory_entry` ≥ 90% lines.
        role: tester
        files:
          - tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py
      - id: TASK-5-6
        description: |-
          Write an integration test at `integration_tests/local_pipeline/test_brc_memory_handler_e2e.py` that exercises the brc_ack / brc_nack handlers through the live MCP server in a `local_pipeline_stack` fixture, performs one ACK and one NACK against a synthetic producer, and asserts the memory file lands at `.egg-state/agent-outputs/<role>/brc-memory.md` with a populated decision log. **Lives under `integration_tests/local_pipeline/` per the trust-boundary docs** so the `gateway_url` fixture + `local_pipeline_stack` are reachable; `integration_tests/conftest.py:78`'s `EggStack.gateway_url` attribute is NOT a fixture and cannot be injected here. **This addresses reviewer_plan blocking item B4** — TASK-3-3 acceptance previously referenced this integration test with no owner.
        acceptance: |-
          The test runs under `make test-all` (or the `local_pipeline` integration marker) and passes; the test file is located under `integration_tests/local_pipeline/`; the memory file landed at the expected path with a decision log entry per ACK + NACK.
        role: tester
        files:
          - integration_tests/local_pipeline/test_brc_memory_handler_e2e.py
    dependencies:
      - slice-4
    parent_slice_id: 4
  - id: 6
    name: |-
      Event-pump consensus_wrapper + wrapper-side liveness migration + safety-budget consumer + legacy deletion
    goal: |-
      The central control-flow rewrite. The wrapper becomes
      a deterministic event pump: blocks on the existing
      `egg-orch message wait-loop` CLI (wrapper invokes the
      CLI directly; does NOT roll its own wait/cursor logic
      — reviewer_plan non-blocking note (5) — so the
      #2323 cursor-threading fix at /tmp/egg-wait-cursor-*
      is preserved), invokes `python3 -m egg_agent` (via
      the existing `build_agent_command()` entry point at
      orchestrator/consensus_wrapper.py:748-759, **not
      `claude -p`**, which is the EGG100-linted
      anti-pattern per docs/guides/agent-mode-design.md:
      90-104 that exits after one response and cannot use
      tools or handle multi-turn work) per actionable
      event with the cached prefix + the memory file from
      slice-5 + the one event payload, and loops until the
      orchestrator reports the role consensus-confirmed and
      is_complete. Four tightly-coupled pieces because they
      cannot ship separately without breaking the agent's
      observable behavior: (a) WS1 — rewrite
      orchestrator/consensus_wrapper.py
      `_CONSENSUS_WRAPPER_TEMPLATE` (lines 116-713) as the
      event pump; clean agent exit after handling one
      event is now expected, NOT a restart trigger; delete
      `MAX_CONSENSUS_RESTARTS = 3` cap (consensus_wrapper.py
      :38), the restart loop (lines 555-695),
      `_RECOVERY_SYSTEM_PROMPT` (lines 64-99),
      `_RECOVERY_USER_PROMPT` (lines 102-105), and the
      SSE consensus.reached blocking path
      (consensus_wrapper.py:397-548, including the curl SSE
      consumer at :419-501) — cq-4 mandates the old path
      is deleted entirely with no flagged fallback at any
      point in this issue; (b) WS4 — move heartbeat +
      gateway-session keep-alive ownership from the
      agent's message_wait_loop
      (sandbox/egg_agent_tools/handlers/message.py:175 +
      lines 234-264, the `_start_wait_loop_heartbeat`
      ticker that fires every
      `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60s` at
      message.py:47) to the wrapper's wait, because the
      agent's wait_loop no longer holds the long wait —
      the wrapper does; the wrapper invokes the existing
      `egg-orch message heartbeat` CLI verb (orch_cli.py:
      1832) at the same 60s cadence to preserve the
      orchestrator-side threshold consumer's invariant
      (orchestrator/health_monitor.py:771, 120s default
      / 600s implement); (c) safety-budget consumer wired
      against the slice-2 durable
      `pipeline.no_progress_budget` field — when
      exhausted, fire OVERSEER_ALERT + open a HITL
      decision (resume/abort) using the slice-2 durable
      parked-HITL state; on host death + restart, the
      wrapper re-reads the durable state (via slice-2's
      `_startup_reconciliation_replay_safety_budget`) and
      resumes monitoring the same budget (cq-3 HARD
      requirement); (d) the SSE blocking path collapses
      to a single `egg-orch consensus check` invocation
      against the slice-3 endpoint — orchestrator/sse.py:
      111 (SSEClientManager) STAYS for the operator-facing
      stream surface (out of scope per cq-1).
      orchestrator/concurrent_executor.py:37 import of
      `build_consensus_wrapped_command` is rewired to the
      new template. Minimal agent-prompt change required
      for this slice to ship: drop the stay-alive loop
      instruction from `_build_brc_preamble`
      (orchestrator/routes/pipelines.py:12348) so the
      agent exits after handling one event — the FULL
      preamble collapse stays in slice-7. **Cutover
      playbook (reviewer_plan non-blocking note 6):**
      drain in-flight pipelines on the old wrapper before
      deploying this slice's PR; new pipelines start on
      the event-pump immediately at deploy time; no
      flagged fallback per cq-4. Spike data from slice-1
      is referenced here to size the safety-budget
      thresholds. This slice has the largest blast radius
      in the issue; the BRC cycle MAY need to subdivide
      in-slice (4a wrapper-side heartbeat CLI verb
      additive; 4b wrapper template rewrite + rewire +
      deletion sweep) if the producer hits >3 NACK rounds
      — reviewer_plan non-blocking note (1) — but the
      contract is intentionally one slice because cq-4
      forbids retaining the old path through any
      intermediate state.
    tasks:
      - id: TASK-6-1
        description: |-
          Rewrite `_CONSENSUS_WRAPPER_TEMPLATE` in `orchestrator/consensus_wrapper.py` (current lines 116-713) as a deterministic Bash event pump. The new template: (i) reads `EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `EGG_SLICE_ID` from env; (ii) in a `while true` loop, calls `egg-orch consensus status --json --role $EGG_AGENT_ROLE --include durable_state` and inspects the returned `next_action` field landed by slice-3 (TASK-3-1) + the durable_state landed by TASK-3-2; (iii) if `next_action == complete`, exits 0; (iv) if `next_action == wait_on_peers` or `wait_on_human`, calls `egg-orch message wait-loop --for CONSENSUS_PROPOSE --for CONSENSUS_NACK --for CONSENSUS_ACK --for CONSENSUS_RE_REVIEW --for STATUS --for HANDOFF --for HITL_RESOLVED --for OVERSEER_ALERT --timeout 60` (full event-type set; the wrapper does NOT roll its own wait/cursor logic — invokes the CLI which owns cursor threading per #2323); (v) on any other `next_action` (propose, ack_required, nack_required, etc.), spawns the agent via `build_agent_command` (see TASK-6-3) and re-loops. Rewrite `build_consensus_wrapped_command(...)` at the same file's lines 716-775 to emit the new template.
        acceptance: |-
          `_CONSENSUS_WRAPPER_TEMPLATE` no longer contains `MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, or `_RECOVERY_USER_PROMPT` placeholders; `build_consensus_wrapped_command` returns a bash script that invokes `egg-orch message wait-loop` (no rolled-own wait); a unit test snapshot of the emitted script asserts the new control flow.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-2
        description: |-
          Delete the legacy restart path from `orchestrator/consensus_wrapper.py`: the `MAX_CONSENSUS_RESTARTS = 3` constant (line 38), the `_RECOVERY_SYSTEM_PROMPT` template (lines 64-99), the `_RECOVERY_USER_PROMPT` template (lines 102-105), and the restart loop wrapping them (lines 555-695). Per cq-4, do NOT retain any of these behind a flag — the deletion is final in this slice. The per-restart OVERSEER_ALERT block at lines 570-585 is **re-purposed (not duplicated)** for safety-budget exhaustion in TASK-6-5 — extract the existing helper and re-wire it; do NOT copy/paste a new alert call site.
        acceptance: |-
          `grep -n` for `MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT` in `orchestrator/consensus_wrapper.py` returns zero hits; `grep -n OVERSEER_ALERT` shows the alert is now wired into the safety-budget-exhaustion block (TASK-6-5), not the restart loop; the alert helper is the same function as the deleted-block source (re-purposed, not duplicated).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-3
        description: |-
          Wire the new wrapper template (TASK-6-1) to spawn the agent via `python3 -m egg_agent`, built by `shared/egg_agent/command.py:build_agent_command(prompt, *, model, max_turns, system_prompt)`. The per-event invocation passes a minimal event payload as the prompt — the event JSON from the wait-loop wakeup plus a pointer to `.egg-state/agent-outputs/<role>/brc-memory.md` (from slice-5) — NOT the full agent prompt template. Pin the model to the orchestrator-side `decision.claude_code_alias` already used at `orchestrator/concurrent_executor.py:489`. **Hard constraint**: do NOT introduce `claude --print` or any `claude -p` invocation; the refine analysis flagged this as an EGG100 anti-pattern and the spike (TASK-1-1) already demonstrates the correct entry point.
        acceptance: |-
          The emitted bash script invokes `python3 -m egg_agent` per event with the event payload as the prompt; `grep -n 'claude --print\|claude -p\b' orchestrator/consensus_wrapper.py` returns zero hits; a unit test mocks `build_agent_command` and asserts the per-event call signature.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-4
        description: |-
          Migrate heartbeat + gateway-session keep-alive emission from `sandbox/egg_agent_tools/handlers/message.py:234-264` (the `_start_wait_loop_heartbeat` daemon thread firing every `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60` seconds at file line 47) to the wrapper. The wrapper invokes the existing `egg-orch message heartbeat` CLI verb (sandbox/egg_lib/orch_cli.py:1832) every 60s while blocked on `wait-loop`, carrying `(pipeline_id, role, slice_id, state=WAITING_FOR_EVENT)`. The heartbeat side-effect preserves the gateway keep-alive (#2451) and the orchestrator-side threshold consumer at `orchestrator/health_monitor.py:771` (120s default / 600s implement). Do NOT remove the agent-side helper outright in this slice — the agent's `message_wait_loop` is still callable for backward compatibility; remove the daemon-thread auto-start only.
        acceptance: |-
          The wrapper emits heartbeats at 60s intervals during a long blocking wait (verifiable via a unit test that mocks the orchestrator and counts heartbeat signals over a simulated 180s wait); `_start_wait_loop_heartbeat` no longer auto-fires from `message_wait_loop` (callable still exists for the deprecation period); the heartbeat invocation uses the existing CLI verb (no new heartbeat primitive introduced).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
          - sandbox/egg_agent_tools/handlers/message.py
      - id: TASK-6-5
        description: |-
          Wire the safety-budget consumer in the new wrapper template against the slice-2 durable `pipeline.no_progress_budget` field. Each wait-loop iteration: (i) read the current `no_progress_budget` from the slice-3 endpoint's `durable_state` payload (TASK-3-2 query param `?include=durable_state`); (ii) if the wait returns an event, reset `last_progress_at` to now and persist via the slice-2 sync-flush helper `_save_pipeline_durable` (TASK-2-2); (iii) if `remaining_seconds <= 0` and `alert_emitted == false`, fire the re-purposed OVERSEER_ALERT helper from TASK-6-2 (carry `--priority high --summary 'BRC consensus no-progress budget exhausted' --recommend 'resume_or_abort'`), set `alert_emitted = true` via `_save_pipeline_durable`, then open a parked HITL decision via `egg-contract add-decision` with options `[resume, abort]` and persist the decision to `pipeline.parked_hitl` via `_save_pipeline_durable`; (iv) keep looping (no auto-FAIL per cq-3). On `parked_hitl.selected == abort`, the wrapper signals error and exits non-zero; on `resume`, the wrapper clears `parked_hitl` (sync-flush) and resets the budget. **Host-restart re-fire suppression** (risk_analyst NB4): if the wrapper reads `alert_emitted=true` from the durable state on startup (e.g. after host restart), the alert is NOT re-emitted — `alert_emitted` is the dedupe key. Threshold defaults derived from slice-1 spike data are documented in TASK-6-12.
        acceptance: |-
          A unit test injects a budget=0 condition and asserts: (i) OVERSEER_ALERT fires exactly once (`alert_emitted` flag is honored); (ii) a HITL decision is created with the correct options; (iii) wait-loop continues until decision lands; (iv) operator-selected `abort` causes a non-zero exit, `resume` clears and continues. A separate unit test asserts host-restart with `alert_emitted=true` in durable state does NOT re-emit OVERSEER_ALERT.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-6
        description: |-
          Replace the SSE consensus.reached blocking path at `orchestrator/consensus_wrapper.py:397-548` (the `check_confirmed_and_wait` function + the curl SSE subscription at lines 449-501 + the egg-orch message wait fallback at 506-542) with the new wrapper template's `egg-orch consensus status --json --role $EGG_AGENT_ROLE --include durable_state` polling invocation from TASK-6-1 (the architect refers to this as a `consensus check` operation; the verb name is `consensus status`). The replacement reads `next_action` and either re-loops (still working) or exits 0 (`complete`). Note: orchestrator-side SSE emission stays (`orchestrator/sse.py:111` for the operator-facing stream); only the wrapper-side SSE consumer is deleted.
        acceptance: |-
          `grep -n 'curl.*stream\|consensus\\.reached\|check_confirmed_and_wait' orchestrator/consensus_wrapper.py` returns zero hits; the new wrapper template uses `egg-orch consensus status --json` (TASK-6-1) instead; `orchestrator/sse.py` is unchanged.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-7
        description: |-
          Rewire the two production callers of `build_consensus_wrapped_command` to use the new event-pump template: `orchestrator/concurrent_executor.py:489` (initial spawn) and `orchestrator/routes/pipelines.py:2792-2796` (restart path). The signature change in TASK-6-1 may drop some parameters (e.g. `max_restarts`); the callers update accordingly. Restart paths now resume the same event pump rather than rebuilding with recovery-prompt placeholders.
        acceptance: |-
          Both call sites pass `make test` for any existing wrapper-spawn tests; the import at `orchestrator/concurrent_executor.py:37` resolves to the new template; restart paths no longer pass `restart_number` / recovery-prompt placeholders.
        role: coder
        files:
          - orchestrator/concurrent_executor.py
          - orchestrator/routes/pipelines.py
      - id: TASK-6-8
        description: |-
          Drop the STAY-ALIVE loop instruction from `_build_brc_preamble` at `orchestrator/routes/pipelines.py:12348+`. This is the minimal preamble nudge needed for the new wrapper to work: the agent must exit after handling one event rather than re-entering a blocking wait. Leave the rest of the preamble intact — the FULL collapse (wait-loop / cursor-threading / pre-confirm-wait text removal) is slice-7's job. The minimal nudge is a one-line edit to the producer / reviewer / dual-role STAY-ALIVE step text: "Exit cleanly after handling the one event; the wrapper will re-invoke you on the next event."
        acceptance: |-
          The `_build_brc_preamble` output contains a one-line "exit cleanly" instruction in the STAY-ALIVE step; the rest of the preamble (cursor-threading examples, wait-loop semantics) is unchanged; a snapshot test catches any unintended drift.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-6-9
        description: |-
          Host-restart recovery: the new wrapper template (TASK-6-1) reads `pipeline.no_progress_budget` and `pipeline.parked_hitl` from the durable contract on startup (before entering the wait-loop) via the slice-4 TASK-4-5 `egg-contract show --field` CLI verb (which provides arbitrary field projection — `phase_get_context` returns a fixed bundle and cannot be used here per reviewer_plan B3). Implementation: the first action in the new template is an `egg-contract show --pipeline $EGG_PIPELINE_ID --field pipeline.no_progress_budget --field pipeline.parked_hitl` call; if the budget is mid-countdown, the wrapper resumes from the durable `remaining_seconds`; if a parked HITL is unresolved, the wrapper blocks on `wait-loop --for HITL_RESOLVED` (or the equivalent message-type) until it resolves. The slice-2 `_startup_reconciliation_replay_safety_budget` step (TASK-2-3) handles the orchestrator-side health-monitor anchor re-prime; this task handles the wrapper-side read.
        acceptance: |-
          A simulated host-restart integration test at `tests/orchestrator/test_event_pump_host_restart.py` writes a mid-countdown budget + a parked HITL via `_save_pipeline_durable`, restarts the wrapper process, and asserts the wrapper resumes monitoring the same budget and observes the HITL resolution event when the operator lands a decision; the wrapper-side read uses `egg-contract show --field` (verifiable via subprocess mock).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-10
        description: |-
          Write unit tests for the new wrapper at `tests/orchestrator/test_consensus_wrapper.py` (extend the existing file) plus a host-restart test at `tests/orchestrator/test_event_pump_host_restart.py`. Coverage: event-pump reaches consensus without a restart cap (mock orchestrator emits PROPOSE → ACK → CONFIRMED; wrapper exits 0); clean post-event exit does NOT trigger FAIL (mock a normal `python3 -m egg_agent` exit code 0 and assert wrapper loops); wrapper-emitted heartbeats fire on the migrated interval (TASK-6-4 invariant — mock the orchestrator and count `egg-orch message heartbeat` invocations over a 180s simulated wait); `egg-orch consensus status --json` replaces SSE consumer cleanly (TASK-6-6 — assert no curl invocation against the SSE stream); host-restart with `alert_emitted=true` in durable state does NOT re-emit OVERSEER_ALERT (TASK-6-5 + TASK-6-9 invariant); fix-lineage assertions: cursor threading (#2323), gateway keep-alive (#2451), heartbeat (#2036), open-NACK barrier surfaced through next-action (#2142), pre-confirm-wait rejection (#2064/#2482), gap-race (#1995).
        acceptance: |-
          All listed cases have a unit test; `make test` passes; coverage on the wrapper template generator ≥ 90% lines; explicit fix-lineage tests are named after their issue numbers; the host-restart no-re-fire test passes.
        role: tester
        files:
          - tests/orchestrator/test_consensus_wrapper.py
          - tests/orchestrator/test_event_pump_host_restart.py
      - id: TASK-6-11
        description: |-
          Write an integration test at `integration_tests/local_pipeline/test_event_pump_qwen_repro.py` that runs the #2906 reproducer (qwen3.7-max on a representative pipeline) end-to-end under the new event-pump and asserts: (i) no "Agent exited without BRC consensus" message; (ii) the pipeline reaches `consensus_reached` without the wrapper restarting the agent; (iii) `.egg-state/agent-outputs/<role>/brc-memory.md` exists and contains at least one decision-log entry; (iv) the gateway push from each role is accepted (role permissions still enforced — `phase_filter.validate_agent_push` invariant). **Trust-boundary constraint**: this test MUST live under `integration_tests/local_pipeline/` because it needs the `gateway_url` fixture from `integration_tests/local_pipeline/conftest.py:261` and the `local_pipeline_stack` machinery; `integration_tests/conftest.py:78`'s `EggStack.gateway_url` attribute is NOT a fixture and cannot be injected.
        acceptance: |-
          The test runs under `make test-all` (or the `local_pipeline` integration marker) and passes; the test file is located under `integration_tests/local_pipeline/`, NOT under `integration_tests/` directly; `kubectl logs` against the wrapper pod shows no "Agent exited without BRC consensus" line.
        role: tester
        files:
          - integration_tests/local_pipeline/test_event_pump_qwen_repro.py
      - id: TASK-6-12
        description: |-
          Write the new architecture doc at `docs/architecture/brc-event-pump.md` describing the event-pump topology, the no-progress safety budget defaults (sized against slice-1 measurements), the durable contract-side persistence model (`pipeline.no_progress_budget` + `pipeline.parked_hitl` + the sync-flush helper + the startup_reconciliation step), the cutover playbook (drain in-flight pipelines on the old wrapper before deploying this slice; new pipelines start on the event-pump at deploy time; no flagged fallback per cq-4), and the operator-facing message for the no-rollback transition. Cross-link from `docs/architecture/orchestrator.md`.
        acceptance: |-
          `docs/architecture/brc-event-pump.md` exists; lists the safety-budget threshold defaults; explicitly states the no-rollback cutover playbook (cq-4 derived); links from `docs/architecture/orchestrator.md`.
        role: documenter
        files:
          - docs/architecture/brc-event-pump.md
          - docs/architecture/orchestrator.md
    dependencies:
      - slice-5
    parent_slice_id: 5
  - id: 7
    name: |-
      Prompt collapse + delta-scoped re-analysis + prior-fix preservation audit
    goal: |-
      Now that the wrapper drives the loop, retire the
      agent-side mechanics the agent no longer owns. Two
      coupled pieces (both touch agent prompts and the
      per-event context payload): (a) WS6 prompt collapse
      — replace the STAY-ALIVE / wait-loop / cursor-
      threading / pre-confirm-wait foot-gun text in
      `_build_brc_preamble` (orchestrator/routes/
      pipelines.py:12348, called at :13659, :13692,
      :13720) and in sandbox/agent-config/rules/*.md (the
      existing rule files: mission.md, overseer.md, etc.)
      with a lean event-handler contract: "here is the one
      event; handle it; exit cleanly; the wrapper handles
      the rest" plus the few invariants the agent still
      owns (file-write boundary, prose via stdin/file for
      propose/ack/nack); also remove the now-redundant
      `egg-orch message wait-loop` examples from the
      preamble since the agent no longer calls it.
      **Prior-fix preservation audit (risk_analyst flag):**
      the BRC preamble currently embeds prompt-instruction
      versions of these fixes — #2323 cursor-threading,
      #2064 / #2482 pre-confirm-wait foot-gun, #1995
      gap-race, #2036 reviewer heartbeats, #2451 gateway
      keep-alive, #2142 open-NACK aggregation barrier,
      #2725 producer-allowlist scope. The preamble
      collapse MUST enumerate each fix and classify it as
      (a) orchestrator-side enforced (safe to remove from
      prompt — covered by the route/handler invariant);
      (b) CLI-level enforced (safe — wrapper uses CLI);
      (c) prompt-instruction-only (REQUIRES preservation
      in the lean handler contract). The task acceptance
      criterion gates the audit. (b) WS5 delta-scoped
      re-analysis — extend the slice-3 next-action
      endpoint to additionally return `changed_artifacts`
      + version delta + the agent's prior memory file
      pointer, and update the per-event prompt template to
      say "evaluate ONLY the named changed_artifacts
      against the prior memory; do not re-read the
      codebase / earlier commits / the analysis draft" —
      bounds per-event context to a delta. Per the refine
      analysis at lines 168-177 "Memory-delta is metadata,
      not content" — the agent receives prior assessment
      summary + a metadata delta (changed file paths,
      commit SHAs, version markers, NACK reasons), NOT an
      inlined diff blob or file-contents snapshot baked
      into the per-event prompt. The agent fetches its own
      diffs and file contents via the warm working tree
      and git tools when needed. Pre-fetching large
      content into prompts is the agent-mode-design
      anti-pattern. The handler-side adapter that fetches
      the delta lives in sandbox/egg_agent_tools/handlers/
      brc.py and is wired via the slice-3 next-action
      endpoint + the slice-4 CLI verb. No wrapper changes
      here. **R5 contract clarity (risk_analyst note 3):**
      if slice-7 NACKs on prompt-vs-wrapper drift, slice-6
      stays in production; slice-7 is independently
      revertable because the slice-6 minimal preamble edit
      (drop stay-alive only) already broke the agent's
      observable behavior back to event-handler shape
      sufficiently for the wrapper invariant — the
      remaining preamble text (cursor-threading examples
      etc.) is informational drift, not active wait
      behavior.
    tasks:
      - id: TASK-7-1
        description: |-
          Collapse `_build_brc_preamble` at `orchestrator/routes/pipelines.py:12348` (callers at lines 13659, 13692, 13720). The collapse MUST be gated on a **prior-fix preservation audit table** committed as a docstring at the top of the function, enumerating: #2323 cursor-threading (classify: CLI-enforced, safe to remove prompt text — wrapper uses CLI); #2064 / #2482 pre-confirm-wait (classify: orchestrator-enforced, safe); #1995 gap-race (classify: CLI-enforced, safe); #2036 reviewer heartbeats (classify: CLI-enforced via TASK-6-4, safe); #2451 gateway keep-alive (classify: CLI-enforced, safe); #2142 open-NACK aggregation barrier (classify: orchestrator-enforced via TASK-3-1, safe); #2725 producer-allowlist scope (classify: gateway-enforced, safe). Remove the STAY-ALIVE step text, the wait-loop semantics block, the cursor-threading examples, and the pre-confirm-wait foot-gun warning. Keep: the file-write boundary callout (per-role), the prose-via-stdin/file rule for `consensus propose/ack/nack` (#2741), the role-specific BRC lifecycle summary (proposer ACK/NACK semantics, conditional-ACK, etc.). The new preamble is a lean event-handler contract: "here is the one event; handle it; exit cleanly; the wrapper handles the rest."
        acceptance: |-
          `grep -n 'STAY ALIVE\|stay-alive\|wait-loop\|cursor.*threading\|pre-confirm-wait' orchestrator/routes/pipelines.py` returns zero hits in the `_build_brc_preamble` block; the function docstring contains the prior-fix audit table with each fix classified; `grep -n 'file-write boundary\|stdin\|--reason-file'` still finds the retained invariants; a snapshot test catches drift.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-7-2
        description: |-
          Collapse the BRC-related sections in `sandbox/agent-config/rules/*.md` (mission.md, overseer.md, and any other rule files referencing the agent-held wait pattern) to match the lean event-handler contract from TASK-7-1. Remove instructions like "after PROPOSE, run egg-orch message wait-loop --for CONSENSUS_ACK ..." and replace with "the wrapper invokes you per event; exit cleanly after handling it."
        acceptance: |-
          `grep -rn 'wait-loop\|wait_loop\|STAY ALIVE\|stay-alive' sandbox/agent-config/rules/` returns zero hits in BRC sections; the rule files still describe the file-write boundary and the prose-via-stdin/file rule.
        role: documenter
        files:
          - sandbox/agent-config/rules/mission.md
          - sandbox/agent-config/rules/overseer.md
      - id: TASK-7-3
        description: |-
          Extend the slice-3 next-action endpoint (TASK-3-1) to additionally return `changed_artifacts` metadata + version delta + a memory-file pointer. The new fields on the per-role matrix entry: `changed_artifacts: list[str]` (paths only, never inlined contents), `current_version: int`, `last_observed_version: int` (per the per-role review-graph tracking already in place per #2482), `memory_file: str` (the `.egg-state/agent-outputs/<role>/brc-memory.md` path). Per refine analysis lines 168-177 "Memory-delta is metadata, not content" (R12), the endpoint MUST NOT include file contents in the payload — the agent fetches its own diffs via git tools from the warm working tree. The architecture/design constraint that this is metadata only is encoded in the endpoint's docstring.
        acceptance: |-
          The endpoint payload includes the four named fields; a unit test asserts no field longer than N bytes (sanity check that file contents are not inlined); the docstring explicitly references the metadata-only constraint and cites `docs/guides/agent-mode-design.md`'s "baking in large diffs" anti-pattern.
        role: coder
        files:
          - orchestrator/routes/signals.py
          - orchestrator/routes/pipelines.py
      - id: TASK-7-4
        description: |-
          Add a handler-side delta adapter in `sandbox/egg_agent_tools/handlers/brc.py` (or a new `handlers/brc_delta.py` module if cleaner) that consumes the slice-7 endpoint payload (TASK-7-3) and returns a structured `BrcDelta` object the per-event prompt template can render compactly. The adapter must NOT fetch file contents — that is the agent's job via git tools. Wire the adapter via the slice-3 `consensus status` extension (or slice-4's `egg-orch brc next-action` if that path was taken).
        acceptance: |-
          The adapter exists; calling it returns a structured object with `changed_artifacts`, `current_version`, `last_observed_version`, `memory_file`; never returns file contents; unit-tested for the metadata-only contract.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-7-5
        description: |-
          Update the per-event prompt template the wrapper passes to `build_agent_command` (the event payload format from TASK-6-3) to render the delta from TASK-7-4 compactly and to instruct the agent: "Evaluate ONLY the named changed_artifacts against your memory file at <path>. Do not re-read the codebase, earlier commits, or the analysis draft. If the changed_artifacts list does not give you enough context, fetch only the named files via git tools." This is the smallest possible per-event prompt that preserves the agent's judgement.
        acceptance: |-
          The per-event prompt template contains the literal "Evaluate ONLY the named changed_artifacts" instruction; a snapshot test of the rendered prompt for a sample event asserts the template stays small (a token-count upper bound is documented in the test).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-7-6
        description: |-
          Write unit tests at `tests/orchestrator/test_brc_preamble.py` (snapshot the lean preamble; assert zero hits for STAY-ALIVE / wait-loop / cursor-threading / pre-confirm-wait text; assert the retained invariants are still present; assert the prior-fix audit table from TASK-7-1 is in the docstring) and at `tests/sandbox/egg_agent_tools/handlers/test_brc_delta.py` (delta adapter never inlines file contents; metadata-only round-trip).
        acceptance: |-
          Both test files exist; `make test` passes; the preamble snapshot test catches any unintended drift; the audit-table assertion passes.
        role: tester
        files:
          - tests/orchestrator/test_brc_preamble.py
          - tests/sandbox/egg_agent_tools/handlers/test_brc_delta.py
      - id: TASK-7-7
        description: |-
          Update `docs/reference/agent-wait-patterns.md` to reflect the new event-handler contract. Mark the prior anti-patterns (STAY-ALIVE loop, agent-held wait-loop) as historical; cross-link to `docs/architecture/brc-event-pump.md` (landed in TASK-6-12). The five anti-patterns previously enumerated in this doc become an "old model" appendix for archaeology.
        acceptance: |-
          The doc explicitly marks the old patterns as historical; the new event-handler contract is summarised at the top; cross-links to brc-event-pump.md.
        role: documenter
        files:
          - docs/reference/agent-wait-patterns.md
    dependencies:
      - slice-6
    parent_slice_id: 6
  - id: 8
    name: |-
      Qwen-route bounded keep-warm cache refinement (conditional on slice-1 spike data)
    goal: |-
      Decide whether bounded keep-warm is needed on the
      Qwen route based on the slice-1 spike's measured TTL
      ceiling, and ship only if needed. Three outcomes
      possible at the time slice-8 picks up: (a) slice-1
      data shows the Qwen provider cache survives the full
      worst-case BRC idle (≥ ~15 min observed worst case)
      — slice-8 collapses to a docs note in
      docs/architecture/brc-event-pump.md saying "no
      keep-warm needed on either route, confirmed by
      slice-1 measurement", no production code; (b)
      slice-1 data shows the Qwen cache lapses between 5.5
      and ~15 min — slice-8 adds a bounded keep-warm to
      the wrapper (NOT the agent) that fires only on the
      Qwen route, only during reviewer-parked-on-NACK
      idles where the wait is approaching the measured
      TTL, with a hard cap on total refreshes and an
      explicit suppression while in HITL / awaiting-human
      waits (which can last hours and would burn Qwen
      tokens against an event hours away); (c) slice-1
      data is ambiguous — slice-8 opens a HITL decision
      asking the operator to choose. Anthropic-route is
      explicitly out of scope (1h TTL already cleared in
      the WS7 empirical comment). This slice is a sibling
      of slice-7 under slice-6 because they are
      independent cleanups: slice-7 retires agent-side
      machinery; slice-8 makes a route-specific cost
      decision. Neither blocks the other.
    tasks:
      - id: TASK-8-1
        description: |-
          Read the slice-1 spike report at `docs/spike/2908-event-pump-spike.md` (TASK-1-4) and classify the outcome: (a) Qwen cache survives ≥ 15 min worst-case idle; (b) cache lapses between 5.5 and 15 min; (c) data ambiguous. Document the classification in `docs/architecture/brc-event-pump.md` under a new "Qwen-route keep-warm" section. If outcome (a): mark the section "no keep-warm needed on either route, confirmed by slice-1 measurement" and the rest of slice-8 (TASK-8-2 / 8-3 / 8-4) becomes not-applicable. If outcome (b): proceed to TASK-8-2 and TASK-8-4. If outcome (c): proceed to TASK-8-3.
        acceptance: |-
          `docs/architecture/brc-event-pump.md` has a "Qwen-route keep-warm" section that explicitly names one of {a, b, c} and cites the slice-1 measurement.
        role: documenter
        files:
          - docs/architecture/brc-event-pump.md
      - id: TASK-8-2
        description: |-
          (Outcome (b) only.) Implement bounded keep-warm in `orchestrator/consensus_wrapper.py` — Qwen-route only, fires only during reviewer-parked-on-NACK idles where the wait is approaching the measured TTL ceiling, with a hard cap on total refreshes (e.g. 10 per pipeline) and an explicit suppression while `pipeline.parked_hitl` is unresolved or the wait is `wait_on_human`. Cadence < measured TTL. The keep-warm fires from the wrapper (NOT the agent) by issuing a minimal cached-prefix invocation against the Qwen route. If outcome was (a) per TASK-8-1, this task is skipped and its acceptance is "no code change needed; slice-8 ships docs only".
        acceptance: |-
          (Outcome b) `grep -n 'keep_warm\|keep-warm' orchestrator/consensus_wrapper.py` finds the new helper; the helper is no-op on the Anthropic route; a unit test at `tests/orchestrator/test_qwen_keep_warm.py` (TASK-8-4) asserts: Qwen-route activation, Anthropic-route no-op, HITL/awaiting-human suppression, hard refresh cap. (Outcome a/c) Task is marked not-applicable in the BRC propose summary; no production code lands.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-8-3
        description: |-
          (Outcome (c) only.) Open a HITL decision via `mcp__sdlc__register_open_question` (or `egg-contract add-decision`) asking the operator to choose between {ship without keep-warm and absorb cold-read cost, ship with bounded keep-warm at <measured TTL minus 10%> cadence, block v1 on Qwen route until a more accurate measurement}. Document the path forward in `docs/architecture/brc-event-pump.md`. If outcome was (a) or (b) per TASK-8-1, this task is skipped.
        acceptance: |-
          (Outcome c) A HITL decision is registered for the operator; the docs section explicitly says the decision is pending operator input. (Outcomes a/b) Task is marked not-applicable; no decision is registered.
        role: documenter
        files:
          - docs/architecture/brc-event-pump.md
      - id: TASK-8-4
        description: |-
          (Outcome (b) only.) Write the unit test at `tests/orchestrator/test_qwen_keep_warm.py` that covers Qwen-route-only activation, HITL/awaiting-human suppression, and the hard refresh cap. If outcome was (a) or (c), this task is marked not-applicable.
        acceptance: |-
          (Outcome b) All three cases have a test; `make test` passes. (Outcomes a/c) Task is marked not-applicable.
        role: tester
        files:
          - tests/orchestrator/test_qwen_keep_warm.py
    dependencies:
      - slice-6
    parent_slice_id: 6
```
