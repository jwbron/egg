# Plan: BRC consensus — deterministic event-pump + durable agent memory (issue #2908)

> Phase: plan | Recommended approach: refine-phase Option B (stateless event-pump + durable distilled memory) | Scaffold owner: architect at `.egg-state/agent-outputs/issue-2908-replan-architect-slices.yaml`

## Approach

The refine-phase analysis (`.egg-state/drafts/issue-2908-replan-analysis.md`) recommends **Option B**: reframe the consensus agent from a *persistent participant that holds a wait* into a *stateless per-event handler the wrapper invokes*. The wrapper (`orchestrator/consensus_wrapper.py`) becomes a deterministic event pump that invokes the existing `egg-orch message wait-loop` CLI directly (preserves the #2323 cursor-threading fix at `/tmp/egg-wait-cursor-*`); on each actionable BRC event it queries the new server-side next-action endpoint, spawns the agent via `python3 -m egg_agent` (built by `shared/egg_agent/command.py:build_agent_command` — **not** `claude --print`, which is an EGG100-linted anti-pattern per `docs/guides/agent-mode-design.md:90-104`), the agent loads its cached prefix + a distilled memory file + the one event, acts, updates memory, and exits naturally. The wrapper loops until the orchestrator reports the role consensus-confirmed and `is_complete`.

This plan enumerates **discrete actionable tasks within each of the architect's 8 slices** at `.egg-state/agent-outputs/issue-2908-replan-architect-slices.yaml`. **Slice composition is the architect's call (#2809)**: the slice ids, names, goals, dependencies, and DAG shape below are copied verbatim from the architect scaffold; this plan adds `tasks:` under each. Where slices grow, that is NACK pressure on the architect — not a slicing decision for the task_planner.

The four HITL decisions resolved in refine are load-bearing for this plan:

| HITL | Resolution | Plan impact |
|---|---|---|
| **cq-1** (WS8 scope) | Split MCP→CLI collapse to a follow-up. **This issue ships only the net-new CLI verbs the event-pump consumes**; do NOT delete any of the 28 agent-facing MCP tools. Do NOT migrate prose-bearing `consensus propose/ack/nack` to CLI. | architect slice-2 lands the three net-new CLI verbs (`brc list-blocking`, `phase get-context`, `phase get-assigned-tasks`) the wrapper consumes. No MCP retirement. `sandbox/egg_agent_tools/server.py:33-61` `SYSTEM_PROMPT_NUDGE` stays unchanged (architect slice-7 calls this out explicitly). |
| **cq-2** (Qwen cache) | Ship v1 with Qwen route **enabled** on the event-pump (no fallback to old path — cq-4). No keep-warm for v1. WS0 spike must measure cold-read cost across the worst-case ~7-8 min reviewer-parked-on-NACK BRC idle. | architect slice-1 runs WS0 spike with per-event cost instrumentation **measured through the real `python3 -m egg_agent` harness with the production BRC preamble + tool schemas** (BC-1 from risk_analyst). No keep-warm slice in v1; if measurement shows material cold-read waste, follow-up issue. |
| **cq-3** (safety budget terminal state) | OVERSEER_ALERT + HITL decision (resume/abort). **Hard requirement: the no-progress safety budget AND parked-HITL state must be persisted DURABLY SERVER-SIDE** so a restarted SDLC host can resume. | architect slice-4 lands `Pipeline.no_progress_budget` **as a new field on the orchestrator-side Pydantic `Pipeline` model at `orchestrator/models.py:1053`** (next to `Pipeline.decisions` per architect v3 d-4 / d-13) — **NOT** as a SDLC Contract schema bump (architect explicitly rejected option (c) "Move Pipeline.no_progress_budget onto Contract" because it would reintroduce the ~200-live-contracts migration risk per R-3). Persisted via the existing git-backed StateStore + a new `_save_pipeline_durable` sync-flush variant + the HITL park decision wiring at `routes/decisions.py:77-200`; **BC-3 partial-failure semantics baked in** (push failure raises `DurableSaveFailed`; the safety-budget consumer emits OVERSEER_ALERT and falls back to in-memory snapshot — does NOT crash-loop the wrapper). The parked-HITL state is **the existing `Pipeline.decisions: list[HITLDecision]` list** (no new field needed — `Pipeline.decisions` is the same precedent and is already the existing serialization template per architect d-4). |
| **cq-4** (old path retention) | **Delete the old capped-restart wrapper path entirely.** No flagged fallback at any point. | architect splits this across **slice-5 (additive)** + **slice-6 (deletion sweep)** so the tree stays green: slice-5 stands up the new event-pump template wholesale so old code paths in `consensus_wrapper.py` become unreachable; slice-6 deletes `MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT`, the SSE machinery at `consensus_wrapper.py:405-501`, the restart loop at `:555-695`, the recovery-prompt re-templating at `:614-633`, the terminal exit-1 at `:697-712`, the `message_wait_loop` body at `handlers/message.py:267-432` (its `_start_wait_loop_heartbeat` `:234-264` + `_default_emit_wait_loop_heartbeat` `:175-231` helpers too — but the lower-level `message_wait` at `:81-172` STAYS because the CLI wait still uses it), and rewires the 3-restart trigger arm at `routes/pipelines.py:18100-18159` so `_emit_producer_death_alert` (function intact at `:15310-15390`) is gated on safety-budget exhaustion instead. **Cutover playbook**: drain in-flight pipelines on the old wrapper before deploying slice-6's PR; new pipelines start on the event-pump immediately after slice-5 deploys. |

Refine open-feedback Q1 (no operator-side hard budget caps for WS0 — instrument only) and Q2 (brc-memory.md is ephemeral coordination state; recovery must NOT depend on it; orchestrator message history + `peer_consensus.reconstruct_tracker_from_messages` is the durable backstop) are honoured: architect slice-1 emits measurements but no fail thresholds; architect slice-6 treats brc-memory.md as ephemeral and architect slice-5's safety-budget consumer reasons from durable orchestrator state alone.

**Memory shape decision** (refine open decision #1 / risk_analyst R5 in this register / reviewer_plan B2 in v1 NACK) is **committed to distilled / rewrite-and-distill** per refine analysis lines 360-367 and the architect slice-6 goal. Rationale: (cache-cost axis) distilled rewrites burst the per-event prefix on each ACK/NACK but the burst is bounded by a section template so the size and frequency are predictable; (orient-don't-constrain axis per `docs/guides/agent-mode-design.md`) distilled summaries orient the per-event agent without flooding its attention; unbounded append-only memory eventually starts *constraining* what the per-event agent can attend to.

## Slice DAG (forest constraint preserved — single linear chain)

Architect scaffold v2 emits **9 slices in a single linear chain** (every slice has exactly one DAG parent; no fork; no `serialized_chain_order` needed). The architect v2 update subdivided the original slice-5 into 5a (additive event-pump online) + 5b (deletion sweep) per the reviewer_plan §11 size concern — this plan re-aligns task IDs to the new structure:

```
slice-1 (WS0 spike + per-event cost instrumentation, root)
  └── slice-2 (net-new CLI commands)
        └── slice-3 (next-action endpoint + CLI)
              └── slice-4 (durable safety budget + HITL park)
                    └── slice-5 (Event-pump online — additive + caller rewires; no deletion)
                          └── slice-6 (Heartbeat migration + legacy deletion sweep + test/docs grep)
                                └── slice-7 (brc-memory artifact + delta plumbing)
                                      └── slice-8 (prompt collapse)
                                            └── slice-9 (integration validation + docs)
```

## Primitives

Every primitive named in the slice rationale and tasks below has a verified `file:line` citation. `(NEW — TASK-X-Y)` marks primitives the named task creates; `(DELETE — TASK-X-Y)` marks ones it removes; the rest already exist and are unchanged or extended.

### Agent SDK + wrapper

| Primitive | Location | Disposition |
|---|---|---|
| `build_agent_command(prompt, *, model, max_turns, system_prompt)` | `shared/egg_agent/command.py:11` (re-exported `shared/egg_agent/__init__.py:8`); argv shape at `:34-46` | CONSUMED by new wrapper in slice-5 (TASK-5-2); the **`--memory-file PATH` + `--event-json STRING` flags are added here** (TASK-5-1). |
| `_CONSENSUS_WRAPPER_TEMPLATE` | `orchestrator/consensus_wrapper.py:116-713` | REWRITE in slice-5 (TASK-5-2) as the deterministic event pump that invokes the existing `egg-orch message wait-loop` CLI directly. |
| `MAX_CONSENSUS_RESTARTS = 3` | `orchestrator/consensus_wrapper.py:38` | DELETE in slice-6 (TASK-6-1). |
| `_RECOVERY_SYSTEM_PROMPT` | `orchestrator/consensus_wrapper.py:64-99` | DELETE in slice-6 (TASK-6-1). |
| `_RECOVERY_USER_PROMPT` | `orchestrator/consensus_wrapper.py:102-105` | DELETE in slice-6 (TASK-6-1). |
| Restart loop | `orchestrator/consensus_wrapper.py:555-695` (per-restart `OVERSEER_ALERT` at :570-585, issue #2806) | DELETE in slice-6 (TASK-6-1). The per-restart `OVERSEER_ALERT` helper is **re-purposed (not duplicated)** for safety-budget exhaustion via the existing `_emit_producer_death_alert` call site (TASK-6-3). |
| `check_confirmed_and_wait` SSE consumer | `orchestrator/consensus_wrapper.py:397-548` (curl SSE consumer at :419-501, fallback wait at :512-542) | DELETE in slice-6 (TASK-6-1); replaced by `egg-orch consensus next-action --role $R --json` against the slice-3 endpoint. Orchestrator-side SSE emission (`orchestrator/sse.py:111`) stays for the operator-facing stream. |
| Recovery-prompt re-templating block | `orchestrator/consensus_wrapper.py:614-633` | DELETE in slice-6 (TASK-6-1). |
| Terminal exit-1 (3-restart FAIL) | `orchestrator/consensus_wrapper.py:697-712` | DELETE in slice-6 (TASK-6-1). |
| `shlex.quote(prompt_text)` pattern | `orchestrator/consensus_wrapper.py:759-760` | **PRESERVED + extended** in slice-5 (TASK-5-2, BC-2). The per-event prompt + event-json + memory snapshot are all prose; the new template MUST substitute them via shlex.quote-applied argv (mirroring the existing pattern) OR via stdin / a tempfile path. Regression-tested in TASK-5-5. |
| `build_consensus_wrapped_command(...)` | `orchestrator/consensus_wrapper.py:716-775` | REWRITE in slice-5 (TASK-5-2) to emit the event-pump template. |
| Concurrent-executor caller | `orchestrator/concurrent_executor.py:37` (import), `:445-524` (`_spawn_agent`), `:489` (call) | REWIRE in slice-5 (TASK-5-4). |
| Pipelines-route caller (restart path) | `orchestrator/routes/pipelines.py:2792-2796` | REWIRE in slice-5 (TASK-5-4); restart path collapses to the same event-pump entry point. |
| 3-restart trigger arm in pipelines route | `orchestrator/routes/pipelines.py:18100-18159` | REWIRE in slice-6 (TASK-6-3): the trigger condition flips from restart-count exhaustion to safety-budget exhaustion; the call to `_emit_producer_death_alert` stays. |
| `_emit_producer_death_alert(...)` callable | `orchestrator/routes/pipelines.py:15310-15390` | **UNCHANGED** — function stays; only its restart-exhaustion call site is rewired to safety-budget exhaustion in slice-6 (TASK-6-3). |

### Wait-loop + heartbeat (agent-side handlers)

| Primitive | Location | Disposition |
|---|---|---|
| `message_wait_loop(req)` body | `sandbox/egg_agent_tools/handlers/message.py:267-432` | **STRIP-ONLY** in slice-6 (TASK-6-2) per architect v4 slice-6(c): remove only the heartbeat machinery; **KEEP the cursor-threaded loop at `:349-420`** because `cmd_message_wait_loop` at `sandbox/egg_lib/orch_cli.py:1779` still calls `_handlers.message_wait_loop(req)` — the wrapper invokes `egg-orch message wait-loop` (slice-5 TASK-5-2) which routes through this CLI shim. Deleting the body wholesale would break the wrapper at runtime. Specifically delete: heartbeat-emission block at `:306-347` (the `pipeline_id_hb` / `role_hb` capture, `_tick` closure, `start_hb(_tick, hb_interval)` autostart) + the `stop_hb()` + final-WORKING-heartbeat block at `:421-432` inside the `finally`. The lower-level `message_wait` at `:81-172` STAYS unchanged — it backs the single-shot `egg-orch message wait` verb and the wait-loop's inner iterations. |
| `_start_wait_loop_heartbeat(tick, interval)` | `sandbox/egg_agent_tools/handlers/message.py:234-264` | DELETE in slice-6 (TASK-6-2) per architect v4 slice-6(c) — heartbeat ownership migrates to wrapper-side `egg-orch message heartbeat` invocation. |
| `_default_emit_wait_loop_heartbeat` helper | `sandbox/egg_agent_tools/handlers/message.py:175-231` | DELETE in slice-6 (TASK-6-2) per architect v4 slice-6(c). |
| `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60.0` | `sandbox/egg_agent_tools/handlers/message.py:47` | DELETE in slice-6 (TASK-6-2) per architect v4 slice-6(c). Constant value mirrored in the new wrapper template's heartbeat cadence; orchestrator-side threshold consumer at `orchestrator/health_monitor.py:_get_heartbeat_threshold` (line 220) is unchanged. The `AgentState` dataclass at `:82-103` (heartbeat field at `:87`) is the in-memory anchor primed by the slice-4 startup reconciliation. |
| `egg-orch message wait-loop` CLI | `sandbox/egg_lib/orch_cli.py:1695` (handler), `:3364-3431` (subparser) | INVOKED BY new wrapper template in slice-5 (TASK-5-2) — wrapper does NOT roll its own wait/cursor logic; preserves #2323 cursor threading at `/tmp/egg-wait-cursor-*`. |
| `egg-orch message heartbeat` CLI | `sandbox/egg_lib/orch_cli.py:1832` | INVOKED BY new wrapper template in slice-6 (TASK-6-2) at 60s cadence to preserve `health_monitor` threshold (120s default / 600s implement) and #2451 gateway keep-alive. Slice-aware container_id reconstruction at `orchestrator/routes/messages.py:860-867` is preserved on the orchestrator side. |
| `tool_interceptor.check_file_write_permission` | `shared/egg_agent/tool_interceptor.py:27` | UNCHANGED — per-invocation enforcement inherited by every `python3 -m egg_agent` spawn. |
| `phase_filter.validate_agent_push` | `shared/egg_restrictions/checker.py:98` (re-exported `gateway/agent_restrictions.py:35,128`) | UNCHANGED — gateway push-time boundary check. |

### Slice-2 net-new CLI verbs

| Primitive | Location | Disposition |
|---|---|---|
| `brc_list_blocking(req)` handler | `sandbox/egg_agent_tools/handlers/brc.py:726` (calls `PeerConsensusTracker.get_state` at `orchestrator/peer_consensus.py:1637` with `blocking_agents` computation at `:1594-1626`) | (CLI shim NEW — TASK-2-1) `egg-orch brc list-blocking` subparser using direct-handler-import. |
| `brc_get_state(req)` handler | `sandbox/egg_agent_tools/handlers/brc.py:679` | (CLI shim NEW — TASK-2-5) `egg-orch brc get-state` subparser per architect v3 slice-2(a) canonical name (surfaces the same data as `egg-orch consensus status --json`). |
| `phase_get_context(req)` handler | `sandbox/egg_agent_tools/handlers/phase.py:139` | (CLI shim NEW — TASK-2-2) `egg-orch phase get-context` subparser. |
| `phase_get_assigned_tasks(req)` handler | `sandbox/egg_agent_tools/handlers/phase.py:193` | (CLI shim NEW — TASK-2-3) `egg-orch phase get-assigned-tasks` subparser. |
| `brc_resolve_obligation(req)` handler | `sandbox/egg_agent_tools/handlers/brc.py:743` | (CLI shim NEW — TASK-2-7) `egg-orch consensus resolve-obligation` subparser per architect v3 slice-2(f); prose args (if any) via `--note-file` or stdin (#2741 guard). |
| `/api/v1/pipelines/<pid>/status` route + `brc_get_state` response shape | `orchestrator/routes/pipelines.py:3911` (route) + `:4531-4557` (response assembly) + `handlers/brc.py:679+` | EXTEND in slice-2 (TASK-2-6) per architect v3 slice-2(e) — additively carry `no_progress_budget` (from `Pipeline.no_progress_budget` once slice-4 lands the field) and `parked_decisions` (from `Pipeline.decisions` filtered to parked entries). Backwards-compatible. CLI shim gains `--field <dotted.path>` projection. **This is the substrate correction** — replaces the v2 d-13 SDLC-Contract path which would have raised `HandlerError('Unknown field: no_progress_budget')` at runtime (Pipeline fields don't exist on Contract per architect v3 d-13). |
| `egg-orch brc <verb>` root subparser | does not exist | (NEW — TASK-2-1 + TASK-2-5) slice-2 adds the `brc` subparser that hosts `list-blocking` + `get-state`. |
| `egg-orch phase <verb>` root subparser | does not exist | (NEW — TASK-2-2) slice-2 adds the `phase` subparser that hosts `get-context` + `get-assigned-tasks`. |
| Direct-handler-import CLI pattern | exemplified at `sandbox/egg_lib/contract_cli.py:342` (`cmd_show` → `from egg_agent_tools.handlers import sdlc as _handlers` at `:351` → `_handlers.show_contract(req)` at `:372`); MCP↔CLI parity validated by registry-walk in `tests/tools/test_mcp_cli_drift.py` (no centralized dispatcher exists) | USED by every new CLI shim in slice-2 — each new `cmd_*` does a module-top `from sandbox.egg_agent_tools.handlers import <namespace> as _handlers` and calls `_handlers.<fn>(req)` directly. There is no `_handler_dispatch` helper (verified via `grep -n '_handler_dispatch' sandbox/egg_lib/*.py` → zero hits per the v1 NACK). |
| MCP↔CLI drift test | `tests/tools/test_mcp_cli_drift.py` (320 lines, documented-gaps list at line ~28) | EXTEND in slice-2 (TASK-2-4) — flip `TOOL_REGISTRY` entries for `mcp__brc__list_blocking`, `mcp__brc__get_state`, `mcp__phase__get_context`, `mcp__phase__get_assigned_tasks`, `mcp__brc__resolve_obligation` from `cli_command=None` to the new verb names; drop them from the documented-gaps list. Per cq-1 scope split, `mcp__brc__read_peer_artifact` + `mcp__task__mark_gap` stay MCP-only (architect v3 slice-2 confirms). |

### Slice-3 next-action endpoint

| Primitive | Location | Disposition |
|---|---|---|
| Pipeline-status route (consumed by `brc_get_state`) | `orchestrator/routes/pipelines.py` (handler used by `handlers/brc.py:707-712`) | EXTEND in slice-3 (TASK-3-1) — add `GET /api/v1/pipelines/<pid>/consensus/next-action?role=R[&slice=S]` returning `{action, target_producer?, version?, blocking_reason?, parked?, reason}`. |
| `PeerConsensusTracker.get_state` | `orchestrator/peer_consensus.py` (consumed by next-action derivation) | CONSUMED in slice-3 (TASK-3-1) for the next-action derivation. |
| `handle_re_propose(...)` | `orchestrator/peer_consensus.py:898-947` | CONSUMED in slice-3 (TASK-3-1) for `changed_artifacts` / version delta encoding. |
| `reconstruct_tracker_from_messages(...)` | `orchestrator/peer_consensus.py:1955` | CONSUMED in slice-3 (TASK-3-1) for the `tracker_reconstructing` action variant + TTL so a post-host-restart probe doesn't spin. |
| `EventType.CONSENSUS_REACHED` enum + emit sites | `orchestrator/events.py:74`; emit sites at `orchestrator/peer_consensus.py:1863`, `routes/pipelines.py:17945, :18254, :18373, :18523, :18686` | UNCHANGED — wrapper consumer at `consensus_wrapper.py:424/467/473` is removed in slice-5; orchestrator-side emits stay for the operator-facing SSE stream (`orchestrator/sse.py:111`). |
| `egg-orch consensus next-action --role R --json` CLI shim | does not exist | (NEW — TASK-3-2) slice-3 adds a `cmd_consensus_next_action` to `sandbox/egg_lib/orch_cli.py` using the direct-handler-import pattern; the verb's CLI handler calls a new server-side endpoint handler in `orchestrator/routes/pipelines.py`. Distinct from `egg-orch consensus status` (slice-2 TASK-2-6) — `next-action` returns the *derived verdict* `{action, target_producer, ...}` while `status --field` returns the *raw durable state* for host-restart recovery. |

### Slice-4 durable safety budget + HITL park

| Primitive | Location | Disposition |
|---|---|---|
| `Pipeline.no_progress_budget` Pydantic field | `orchestrator/models.py:1053` (the orchestrator-side `Pipeline` model — same anchor as the existing `Pipeline.decisions` field which is the precedent per architect v3 d-4) | (NEW — TASK-4-1). Per-role counter (shape: `dict[str, NoProgressBudgetEntry]` where the entry holds `{remaining_seconds: int, last_progress_at: datetime | null, threshold_seconds: int, alert_emitted: bool}`). Pydantic backfills the field with its default (empty dict or None) on next `model_validate` of an older serialized Pipeline — **no schema migration helper required**, no `_migrate_schema_version_*` needed (architect v3 d-13 explicitly rejected the SDLC Contract schemaVersion 1.2 → 1.3 bump because it would re-introduce the ~200-live-1.2-contracts migration risk per R-3). |
| `Pipeline.decisions` (parked-HITL substrate) | `orchestrator/models.py:1053` (existing — `list[HITLDecision]`) | UNCHANGED — used to hold the `"pipeline parked — no progress for N events. Resume or abort?"` decision per architect d-4 "(orchestrator/models.py:1053 HITLDecision is the template)". No new field needed for the parked-HITL state; the existing `Pipeline.decisions` list is the durable substrate. |
| `save_pipeline()` + best-effort async push | `orchestrator/state_store.py:672` (def), with async-push primitives at `:11` (module docstring), `:127` (`_push_in_flight`), `:804-805` (`Best-effort async push to remote after every commit`), `:890-928` (debounced `_sync_to_remote_async`) | UNCHANGED — the existing async path stays the default for high-frequency writes. |
| `_save_pipeline_durable(pipeline)` / sync-flush variant | does not exist | (NEW — TASK-4-2). Sync-flush variant on `orchestrator/state_store.py` (same file/class as the existing `save_pipeline` at `:672`). Returns ONLY after `git push origin <branch>` completes. **BC-3 partial-failure semantics (HARD)**: on push failure, raises a typed exception `DurableSaveFailed`; the caller in the safety-budget consumer (TASK-5-3) handles this by emitting an OVERSEER_ALERT and continuing the wait-loop with an in-memory snapshot — does NOT exit the wrapper. Bounded retry policy documented in the function's docstring. **Operates on the orchestrator-side Pipeline model** (NOT the gateway-side SDLC Contract — those are different stores reached via different endpoints per architect v3 d-13). |
| `_startup_reconciliation_replay_safety_budget` step | does not exist | (NEW — TASK-4-3). Added to `orchestrator/startup_reconciliation.py`. On orchestrator boot iterates active pipelines and reads the durable `Pipeline.no_progress_budget` from the existing StateStore loader (NOT from `load_contract_from_branch` at `contract_store.py:127`, which reads the gateway-side SDLC Contract — wrong substrate per architect v3 d-13). Primes the in-memory `health_monitor.py` `AgentState` dataclass anchors at `:82-103` so a fresh host monitors the same budget the previous host was tracking. Honours `alert_emitted=true` from durable state — no re-fire. |
| HITL decision-handler pattern | `orchestrator/routes/decisions.py:77-200` (`_handle_restart_agent` at `:77-150` is the architect-named template per architect d-4) | EXTEND in slice-4 (TASK-4-4). Wire `resume` (clears the per-role counter on `Pipeline.no_progress_budget`) and `abort` (transitions pipeline state to FAILED) into the existing decision-handler pattern. |

### Slice-7 brc-memory + delta plumbing

| Primitive | Location | Disposition |
|---|---|---|
| Role allowlist for `.egg-state/agent-outputs/` | `shared/egg_restrictions/patterns.py` — 14 occurrences across every producer/reviewer pattern (ARCHITECT line 367, TASK_PLANNER line 377, REVIEWER_REFINE line 510, REVIEWER_PLAN line 516+, REVIEWER_AGENT_DESIGN line 479-484, REFINER line 488, CODER block-exempt 231, TESTER 277, DOCUMENTER 307); prefix-glob semantics confirmed via `shared/egg_restrictions/matchers.py:33` (`<prefix>/` matches any file under the prefix including subdirs) | UNCHANGED — `brc-memory.md` lands under the existing allowlist; **cross-role READS are not gated** (the file-write interceptor at `shared/egg_agent/tool_interceptor.py:17` only intercepts Write/Edit/NotebookEdit; reads use the standard Read tool with no role check). |
| `brc_ack(req)` / `brc_nack(req)` / `brc_propose(req)` / `brc_confirm(req)` handlers | `sandbox/egg_agent_tools/handlers/brc.py` | EXTEND in slice-7 (TASK-7-2). Action-scaffolded write: reviewer handlers append a structured entry on each ACK/NACK; producer handlers append on each propose/confirm. Dict-arg path (in-process MCP, NOT argv) — **zero #2741 shell-metachar exposure**. |
| `_append_brc_memory_entry(...)` helper | does not exist | (NEW — TASK-7-1) in `sandbox/egg_agent_tools/handlers/brc.py` (or a new `brc_memory.py` module). Distilled / rewrite-and-distill: read existing file, parse structured sections, append a decision-log entry, compact per-producer assessment. **Atomic write** (write-temp + os.replace) to be crash-safe and concurrency-safe per risk_analyst R-4. |
| `--memory-file PATH` flag on `python3 -m egg_agent` | does not exist | (NEW — TASK-5-1). Slice-5 TASK-5-1 introduces both `--memory-file` and `--event-json` flags; slice-7 plumbs the per-role memory path through the wrapper (TASK-7-3). **Behaviour with neither flag MUST remain identical to today** (legacy regression guard per architect slice-5 goal). |
| `--changed-artifacts` payload | does not exist | (NEW — TASK-7-4). Per-event invocation receives metadata-only delta (paths, version markers) — never inlined file contents (refine analysis lines 168-177; agent fetches its own diffs via git tools). |
| Cost-callback (LiteLLM Qwen route) | `config/litellm/cost_callback.py:344` (`cost_logger = LiteLLMCostLogger()`); structured stdout logging only (no on-disk JSON in agent pod — refine analysis lines 41-58 corrects the issue body's `~/.local/state/clm/cost-*.json` reference) | CONSUMED in slice-1 spike (TASK-1-3) via `kubectl logs deployment/egg-litellm`. |

### Slice-8 prompt collapse

| Primitive | Location | Disposition |
|---|---|---|
| `_build_brc_preamble(role_value, phase, ...)` | `orchestrator/routes/pipelines.py:12348` (callers at `:13659, :13692, :13720`) | COLLAPSE in slice-8 (TASK-8-1). Replace STAY-ALIVE / wait-loop mechanics / cursor-threading / pre-confirm-wait foot-gun text with a lean event-handler contract. **Prior-fix preservation audit (HARD AC)**: each prior fix (#2323, #2064/#2482, #1995, #2036, #2451, #2142, #2725) is classified as orchestrator-enforced / CLI-enforced / prompt-only; only prompt-only fixes are preserved as prompt text. |
| `sandbox/agent-config/rules/mission.md` | sandbox-agent rules (~195 lines); BRC-related references at ~lines 137-192 (per architect slice-6 docs-grep AC) | UPDATE in slice-8 (TASK-8-2) — but the **docs-grep zero-hit AC** lives in slice-6 (TASK-6-4) per architect slice-6 (f); slice-8 owns the lean event-handler rewrite content; slice-6 owns the grep evidence that pre-deletion references are gone alongside the code deletions (avoids a brief red-tree window between deletions and prompt rewrite). |
| `SYSTEM_PROMPT_NUDGE` | `sandbox/egg_agent_tools/server.py:61` (rendered by `_render_nudge()`) | **UNCHANGED** — architect slice-8 goal explicitly preserves this because cq-1 forbids MCP-surface changes in this issue. |

### Slice-9 integration validation + docs

| Primitive | Location | Disposition |
|---|---|---|
| Trust-boundary fixture scope | `integration_tests/conftest.py:339` (`egg_stack` session-scoped fixture, kubectl-gated via `_kubectl_available()` → `pytest.skip` when unavailable); `integration_tests/conftest.py:78` (`EggStack.gateway_url: str` attribute on the dataclass — NOT a pytest fixture); `integration_tests/conftest.py:357` (`orchestrator_url` standalone fixture which delegates to `egg_stack.orchestrator_url`) | OBSERVED in slice-9 (TASK-9-1 #2906-repro integration test). **Trust-boundary correction per the reviewer_plan v1 NACK**: `integration_tests/local_pipeline/` was DELETED in commit f7803637d1 (May 11, 2026 — "test: delete deprecated local_pipeline + squid tests; file follow-up issues"); the conftest, the `gateway_url` fixture, and the `local_pipeline_stack` fixture are all gone. The replacement is to place new integration tests as siblings of the existing `test_*.py` files directly under `integration_tests/` and to consume the `egg_stack` fixture (kubectl-gated → same trust-boundary tier as the deleted `local_pipeline_stack`). |
| `docs/reference/agent-wait-patterns.md` | docs | UPDATE in slice-9 (TASK-9-2). Reflect the new event-handler contract; mark prior anti-patterns historical. |
| `docs/guides/concurrent-execution.md` | docs | UPDATE in slice-9 (TASK-9-2). Describe new control flow. |
| `docs/architecture/orchestrator.md` | docs | CROSS-LINK from slice-9 (TASK-9-2) to the new BRC-event-pump architecture section landed in `docs/architecture/brc-event-pump.md` (also TASK-9-2). |

### Existing fix-lineage primitives the migration must preserve

The migration must keep working: cursor threading (#2323) — CLI-enforced via `egg-orch message wait-loop`'s persistence at `/tmp/egg-wait-cursor-*`; pre-confirm-wait rejection (#2064, #2482) — orchestrator-enforced (rejects `wait-loop --for CONSENSUS_CONFIRMED` if caller hasn't yet confirmed); gap-race fix (#1995) — CLI-enforced server-side cursor + `since_id` threading; heartbeat liveness (#2036) — CLI-enforced via the wrapper-side `egg-orch message heartbeat` call (slice-5 TASK-5-4); gateway session keep-alive (#2451) — same heartbeat side-effect; open-NACK aggregation barrier (#2142) — orchestrator-enforced (encoded in slice-3 next-action endpoint as `next_action: address_nacks` when N≥2 reviewer NACKs); conditional-ACK / stale-version re-review (#2482) — orchestrator-enforced (encoded in slice-3 endpoint via `current_version` / `last_observed_version`); producer-allowlist scope (#2725) — gateway-enforced; dual-role producer-first ordering (#2749) — encoded in slice-3 next-action derivation per risk_analyst R-6.

## Risk-analyst BC integration (BC-1, BC-2, BC-3)

The risk_analyst register at `.egg-state/agent-outputs/issue-2908-replan-risk_analyst-output.json` flags three blocking concerns that this plan addresses via explicit task ACs:

- **BC-1** (cache-survival measurement on the production egg-agent prefix): TASK-1-2 + TASK-1-4 require the measurement to come from `python3 -m egg_agent` invocations under the new event-pump prototype with the production `_build_brc_preamble` + tool schemas, NOT raw `claude --output-format json`. Slice-8 prompt collapse is gated on this measurement.
- **BC-2** (shell-prose corruption regression risk on the per-event prompt): TASK-5-2 requires the per-event prompt + event-json + memory snapshot to be passed to `python3 -m egg_agent` either via `shlex.quote`-applied argv (mirroring the existing `consensus_wrapper.py:759-760` pattern) OR via stdin / a tempfile path. TASK-5-5 unit-tests it (new file `tests/orchestrator/test_consensus_wrapper_event_pump.py` per architect slice-5 (c)) with `$`, backtick, single-quote, double-quote, newline payloads.
- **BC-3** (sync-flush partial-failure semantics): TASK-4-2 specifies both success and partial-failure paths. On push failure, `_save_pipeline_durable` raises `DurableSaveFailed`; the safety-budget consumer (TASK-5-3) emits OVERSEER_ALERT and continues the wait-loop with an in-memory budget snapshot — does NOT exit the wrapper. TASK-4-5 unit-tests cover both paths; TASK-5-5 covers the consumer's partial-failure handling.

## Test strategy

**Automated coverage** (per architect slice):

- **slice-1 spike**: no automated tests; the spike's value is the measurement, captured in the spike report at `.egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md`.
- **slice-2 CLI verbs**: unit tests at `tests/sandbox/egg_lib/test_brc_phase_cli_verbs.py` (NEW — TASK-2-5) covering direct-handler-import parity, `--json` payload byte-equality vs the corresponding MCP tool, error paths; extend `tests/tools/test_mcp_cli_drift.py` (TASK-2-4) for the new CLI verbs.
- **slice-3 next-action endpoint**: unit tests at `tests/orchestrator/test_consensus_next_action.py` (NEW — TASK-3-3) covering producer/reviewer/dual-role lifecycles, open-NACK aggregation (#2142), conditional ACK, stale-version re-review (#2482), resolve_obligation surface, producer-first ordering (#2749 dual-role fixture per risk_analyst R-6), `tracker_reconstructing` action variant + TTL, `egg-orch consensus next-action --role R --json` CLI shim parity.
- **slice-4 durable persistence**: unit tests at `tests/orchestrator/test_pipeline_no_progress_budget_field.py` (NEW — TASK-4-6) covering Pydantic backwards-compat (no SDLC schema migration needed per architect v3 d-13 → R-3 structurally retired) AND StateStore smoke against `.egg-state/pipelines/*.json` samples; `tests/orchestrator/test_save_pipeline_durable.py` (NEW — TASK-4-5) covering both BC-3 paths (happy path returns only after `git push` succeeds; mock push failure → raises `DurableSaveFailed`); `tests/orchestrator/test_startup_reconciliation_safety_budget.py` (NEW — TASK-4-5) covering host-kill mid-budget → fresh host loads same budget state via `_startup_reconciliation_replay_safety_budget` against the orchestrator StateStore.
- **slice-5 event-pump online (additive)**: new unit-test file at `tests/orchestrator/test_consensus_wrapper_event_pump.py` (NEW — TASK-5-5) covering event-pump reaches consensus without a restart cap, clean post-event exit does NOT trigger FAIL, `egg-orch consensus next-action --json` replaces SSE consumer cleanly, **BC-2 shell-metachar regression test** (prompt + event-json + memory-file containing `$`, backtick, single-quote, double-quote, newline → agent receives byte-identical), **BC-3 partial-failure path** (mock `_save_pipeline_durable` → `DurableSaveFailed` → wrapper emits OVERSEER_ALERT + continues), host-restart no-re-fire of `alert_emitted=true`, `--memory-file`/`--event-json` flag regression guard (TASK-5-1 — behaviour with neither flag identical to today).
- **slice-6 deletion sweep**: TASK-6-1 acceptance includes `grep -rn` evidence for zero remaining hits on `MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT`, `check_confirmed_and_wait` (per risk_analyst R-7); TASK-6-4 includes `git grep` evidence for zero structural hits on those symbols + "STAY-ALIVE" / "wait-loop" in agent-authored context across `sandbox/agent-config/rules/`, `docs/architecture/orchestrator.md`, `docs/guides/concurrent-execution.md`, `docs/reference/orchestrator-cli.md`, `docs/reference/agent-wait-patterns.md` (per architect slice-6 (f)). Test pivot across the 7 named files (`tests/orchestrator/test_consensus_wrapper.py`, `test_consensus_polling.py`, `test_consensus_race_on_exit.py`, `test_consensus_timeout_recheck.py`, `test_brc_nack_iteration.py`, `test_agent_exits_recorded.py`, restart-arm assertions in `test_producer_death_alert.py`) — restart-semantics tests deleted; event-pump-semantics moved into `test_consensus_wrapper_event_pump.py` (slice-5 TASK-5-5). Fix-lineage assertions on wrapper-emitted heartbeats (60s cadence per #2036/#2451) move to TASK-6-5.
- **slice-7 memory + delta**: unit tests at `tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py` (NEW — TASK-7-5) covering distilled rewrite-and-distill, idempotency, partial-file recovery, brc_ack/brc_nack/brc_propose/brc_confirm dict-arg side-effect, write-permission failure non-fatal, atomic-rename concurrency safety (R-4 regression — 20 concurrent appends → file integrity).
- **slice-8 prompt collapse**: unit test at `tests/orchestrator/test_brc_preamble.py` (NEW — TASK-8-3) asserting the lean preamble carries the file-write boundary + stdin/file rule + the explicit prior-fix-audit table; snapshot tests for one producer role + one reviewer role + one dual-role agent (per architect slice-8 goal). **BC-1 / R-1 gate**: this slice's implementation is gated on slice-1's measurement of cache_read_input_tokens for the *real* egg-agent prefix; if dynamic content lands before the cache breakpoint, slice-8 TASK-8-3 (delta payload structure) must move it to the suffix and the unit test asserts breakpoint placement.
- **slice-9 integration**: a k3s integration test at `integration_tests/test_event_pump_qwen_repro.py` (NEW — TASK-9-1) that runs the #2906 reproducer end-to-end under the event-pump and asserts no "Agent exited without BRC consensus" churn, memory populated + consulted, role-write permissions still enforced, `cache_read_input_tokens` instrumented across consecutive per-event invocations and across an injected long idle (final per-event cost numbers vs the slice-1 baseline). **Lives directly under `integration_tests/`** (sibling of `test_k8s_deployment_tools.py`, `test_sandbox_mcp_tools_e2e.py`, etc.) and consumes the `egg_stack` fixture per the trust-boundary correction.

**Manual verification**:

- After slice-2, reviewer should run `egg-orch --help` and confirm the new `brc` and `phase` subparsers are reachable, and that `egg-orch brc list-blocking`, `egg-orch phase get-context`, `egg-orch phase get-assigned-tasks` are discoverable.
- After slice-5, reviewer should confirm the new event-pump wrapper is the only spawn path (old code is dead but still present); after slice-6, reviewer should `make test-all` and inspect orchestrator status to confirm wrapper-emitted heartbeats land at the 60s cadence and the deletion sweep passed the `grep` zero-hit ACs.
- After slice-9, reviewer should review the integration-test pass evidence and the spike-vs-baseline cost numbers in the spike report + integration-test artifacts.

## Manual steps

**Pre-merge**: none for any slice — every change ships behind the BRC cycle's review gate; no `.github/` files are touched.

**Post-merge**:

- **slice-4**: new Pydantic field on `Pipeline` is backfilled automatically by Pydantic on next `model_validate` of any serialized Pipeline (default_factory=dict); no migrator helper, no SDLC contract schema bump (architect v3 d-13).
- **slice-5 → slice-6**: **CUTOVER PLAYBOOK** — drain in-flight pipelines on the old capped-restart wrapper before deploying slice-6's PR (operator monitors `egg-orch pipeline status` for all active pipelines, waits for them to reach `is_complete=true` or quiesce). slice-5 brings the new event-pump online (old code becomes unreachable but is still present); slice-6 deletes the old code and rewires heartbeats. New pipelines start on the event-pump immediately after slice-5 deploys. No flagged fallback per cq-4. Documented in `docs/architecture/brc-event-pump.md` (TASK-9-2).
- **slice-9**: docs-only; no operator action required at merge time.

```yaml
# yaml-tasks
pr:
  title: "Replace agent-held BRC waits with deterministic event-pump + durable memory"
  description: |
    Closes #2906 and resolves the structural seam that produced the entire lineage of BRC wait bugs (#2323, #2064/#2482, #2036, #1995, #2451). Today the BRC consensus agent holds a blocking `egg-orch message wait-loop` between each event; every re-entry is a seam at which the model can emit a final assistant message and exit `success=True` instead of looping. Claude usually re-enters; qwen3.7-max does not (#2906) — it exits at ~30–50 of a 1000-turn budget, the wrapper sees no `CONSENSUS_CONFIRMED`, and the 3-restart cap (#2806) burns ~$1 and ~20 min per cycle.

    This PR reframes consensus-agent execution from a *persistent participant that holds a wait* into a *stateless per-event handler the wrapper invokes*, with continuity carried by a durable distilled per-role memory file. Nine slices ship sequentially as stacked PRs per architect scaffold v2 (the original slice-5 was subdivided into 5a additive + 5b deletion sweep per reviewer_plan §11):

    1. **WS0 spike + per-event cost instrumentation** (slice-1) — empirically validate the event-pump on the #2906 reproducer (issue-2270 / qwen3.7-max) **through the real `python3 -m egg_agent` harness with the production BRC preamble + tool schemas** (BC-1 measurement requirement). Output is a measurement report under `.egg-state/agent-outputs/`.
    2. **Net-new CLI commands** (slice-2) — `egg-orch brc list-blocking` + `egg-orch brc get-state` (architect v3 slice-2(a) canonical name), `egg-orch phase get-context` + `egg-orch phase get-assigned-tasks`, `egg-orch consensus resolve-obligation` (slice-2(f)), and the **`egg-orch consensus status` extension** (slice-2(e)) — additively carries `no_progress_budget` + `parked_decisions` from the orchestrator-side `Pipeline` model with `--field <dotted.path>` projection. All via the direct-handler-import pattern at `sandbox/egg_lib/contract_cli.py:342`. No MCP retirement per cq-1. **Substrate correction (architect v3 d-13)**: `egg-orch consensus status` reads the orchestrator-side Pipeline; `egg-contract show` reads the gateway-side SDLC Contract — they are different stores and the wrapper's durable read must use the former.
    3. **Server-side next-action endpoint + CLI** (slice-3) — `GET /api/v1/pipelines/<pid>/consensus/next-action?role=R[&slice=S]` returning `{action, target_producer?, version?, blocking_reason?, parked?, reason}`, plus the matching `egg-orch consensus next-action --role R --json` CLI shim. Includes a `tracker_reconstructing` action variant + small TTL so a post-host-restart probe doesn't spin.
    4. **Durable no-progress safety budget + HITL park decision** (slice-4) — `Pipeline.no_progress_budget` added as a new Pydantic field on the orchestrator-side `Pipeline` model at `orchestrator/models.py:1053` (next to the existing `Pipeline.decisions` per architect v3 d-4). **No SDLC Contract schemaVersion 1.2→1.3 bump** — architect v3 d-13 explicitly rejected the schema-bump option to avoid the ~200-live-1.2-contracts migration risk (R-3 structurally retired by this substrate). Parked-HITL state uses the existing `Pipeline.decisions: list[HITLDecision]` list (no new field needed). Includes the new `_save_pipeline_durable` sync-flush variant with BC-3 partial-failure semantics (push failure raises `DurableSaveFailed`; consumer falls back to in-memory snapshot, does NOT crash-loop the wrapper) and the `_startup_reconciliation_replay_safety_budget` step (reads from the orchestrator StateStore, NOT `load_contract_from_branch`).
    5. **Event-pump online — additive + caller rewires; no deletion** (slice-5) — the additive half of the swap. Add `--memory-file PATH` + `--event-json STRING` flags to `python3 -m egg_agent`; rewrite `_CONSENSUS_WRAPPER_TEMPLATE` as the deterministic event-pump bash; rewire callers in `concurrent_executor._spawn_agent` (`:445-524`) and `routes/pipelines.py:2792-2796`. Wire safety-budget consumer to the slice-4 durable field with BC-3 handling. Old code paths in `consensus_wrapper.py` are unreachable (the new template replaces them wholesale) but still present until slice-6 deletes them. Per BC-2, per-event prompt + event-json + memory snapshot pass via shlex.quote-applied argv or stdin/file (#2741 regression guard).
    6. **Heartbeat migration + legacy deletion sweep + test/docs grep** (slice-6) — the deletion half of the swap. Migrate heartbeat ownership: wrapper invokes `egg-orch message heartbeat` at the 60s cadence (preserves `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS=60` invariant at `handlers/message.py:47`); delete `_start_wait_loop_heartbeat` (`:234-264`) + `_default_emit_wait_loop_heartbeat` (`:175-231`). DELETE per cq-4: `MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT`, the SSE machinery at `consensus_wrapper.py:405-501`, the restart loop at `:555-695`, the recovery-prompt re-templating at `:614-633`, the terminal exit-1 at `:697-712`. Delete the `message_wait_loop` body at `handlers/message.py:267-432` (the lower-level `message_wait` at `:81-172` STAYS — the CLI wait still uses it). Rewire the 3-restart trigger arm at `routes/pipelines.py:18100-18159` to safety-budget exhaustion; `_emit_producer_death_alert` (function at `:15310-15390`) stays. Test pivot across 7 named files. Docs-grep AC: zero structural hits on the deleted symbols + STAY-ALIVE / wait-loop (agent-authored) across `sandbox/agent-config/rules/`, `docs/architecture/orchestrator.md`, `docs/guides/concurrent-execution.md`, `docs/reference/orchestrator-cli.md`, `docs/reference/agent-wait-patterns.md`.
    7. **brc-memory artifact + delta-scoped re-analysis plumbing** (slice-7) — `.egg-state/agent-outputs/<role>/brc-memory.md` with structured sections (codebase/change model, per-producer assessment, decision log); memory shape **committed to distilled / rewrite-and-distill**; handler scaffolding in `sandbox/egg_agent_tools/handlers/brc.py` writes memory entries on each ACK/NACK/propose/confirm via the dict-arg interface (zero #2741 shell-metachar exposure). Plumbs the existing `changed_artifacts`/version delta via `--changed-artifacts` into the per-event invocation — metadata only, never inlined contents. Writes use atomic tempfile-rename per R-4.
    8. **Prompt collapse** (slice-8) — replace STAY-ALIVE / wait-loop mechanics / cursor-threading / pre-confirm-wait foot-gun text in `_build_brc_preamble` and `sandbox/agent-config/rules/mission.md` with a lean event-handler contract. Gated on a prior-fix preservation audit table classifying each fix (#2323, #2064/#2482, #1995, #2036, #2451, #2142, #2725) as orchestrator-enforced / CLI-enforced / prompt-only. `SYSTEM_PROMPT_NUDGE` at `sandbox/egg_agent_tools/server.py:61` STAYS UNCHANGED per cq-1.
    9. **Integration validation + docs revision** (slice-9) — end-to-end run of the new event-pump on the #2906 Qwen-route reproducer. Verify zero "Agent exited without BRC consensus" restart-churn alerts, memory populated + consulted, per-role permissions enforced, `cache_read_input_tokens` instrumented across consecutive invocations and across an injected long idle, final per-event cost numbers vs the slice-1 baseline. Update `docs/reference/agent-wait-patterns.md`, `docs/guides/concurrent-execution.md`, cross-link from `docs/architecture/orchestrator.md` to a new `docs/architecture/brc-event-pump.md`.

    For operators: slice-5 brings the new event-pump online; drain in-flight pipelines on the old wrapper before deploying slice-6's PR (the deletion sweep). New pipelines start on the event-pump immediately after slice-5 deploys. No flagged fallback per cq-4. The structural seam is gone: a deterministic Bash loop, not the model, drives BRC waits and termination; no model can fall out by exiting between events. The per-cycle restart cost (~$1 / ~20 min) goes to zero on the failure modes #2906 surfaced.
  test_plan: |
    Automated:
    - slice-1: no tests; spike measurement report at `.egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md` captures per-event token spend (Anthropic via AgentResult.cost_usd, Qwen via `kubectl logs deployment/egg-litellm`) AND wall-clock across the worst-case ~7-8 min reviewer-parked-on-NACK idle. Measurements come from `python3 -m egg_agent` invocations with the production BRC preamble + tool schemas (BC-1).
    - slice-2: `tests/sandbox/egg_lib/test_brc_phase_cli_verbs.py` covers direct-handler-import parity, JSON byte-equality, error paths; `tests/tools/test_mcp_cli_drift.py` extended for new CLI shims.
    - slice-3: `tests/orchestrator/test_consensus_next_action.py` covers producer/reviewer/dual-role lifecycles, open-NACK aggregation (#2142), conditional ACK, stale-version re-review (#2482), resolve_obligation, producer-first ordering (#2749 per R-6), `tracker_reconstructing` action variant + TTL, CLI shim parity.
    - slice-4: `tests/orchestrator/test_pipeline_no_progress_budget_field.py` covers Pydantic backwards-compat (older serialized Pipeline JSON without the new field loads with `no_progress_budget == {}` via `default_factory=dict`) AND StateStore smoke against `.egg-state/pipelines/*.json` samples — **NO SDLC contract migration test** because the substrate is the orchestrator Pipeline (not the SDLC Contract); architect v3 d-13 retired R-3 structurally. `tests/orchestrator/test_save_pipeline_durable.py` covers BC-3 both paths (happy path returns only after git push succeeds; mock push failure → raises DurableSaveFailed; in-memory fallback documented); `tests/orchestrator/test_startup_reconciliation_safety_budget.py` covers host-kill mid-budget → fresh host loads same budget state via the orchestrator StateStore (NOT `load_contract_from_branch`).
    - slice-5: new `tests/orchestrator/test_consensus_wrapper_event_pump.py` covers event-pump reach-consensus, clean-exit no-FAIL, `egg-orch consensus next-action --json` replaces SSE consumer cleanly, BC-2 shell-metachar regression (prompt + event-json + memory-file with $/backtick/quotes/newline → byte-identical agent receive), BC-3 partial-failure path (`_save_pipeline_durable` → `DurableSaveFailed` → wrapper emits OVERSEER_ALERT + continues), host-restart no-re-fire of alert_emitted=true, `--memory-file`/`--event-json` flag regression guard.
    - slice-6: TASK-6-1 acceptance includes grep evidence for zero remaining MAX_CONSENSUS_RESTARTS / _RECOVERY_* / check_confirmed_and_wait hits in production code; TASK-6-4 docs-grep AC covers zero structural hits in docs + sandbox/agent-config/rules/; existing `test_consensus_polling.py`, `test_consensus_race_on_exit.py`, `test_consensus_timeout_recheck.py`, `test_brc_nack_iteration.py`, `test_agent_exits_recorded.py`, the restart-arm assertions in `test_producer_death_alert.py` are deleted in the same commit as the production deletion (R-7 mitigation — tree stays green); event-pump-semantics tests live in `test_consensus_wrapper_event_pump.py` (slice-5 TASK-5-5). Wrapper-emitted-heartbeats unit test lives in TASK-6-5.
    - slice-7: `tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py` covers distilled rewrite-and-distill, idempotency, partial-file recovery, atomic-rename concurrency (R-4 regression — 20 concurrent appends → file integrity), brc_ack/brc_nack/brc_propose/brc_confirm dict-arg side-effects, write-permission failure non-fatal.
    - slice-8: `tests/orchestrator/test_brc_preamble.py` snapshot-asserts the lean preamble + prior-fix audit table (each fix classified); per-event prompt template snapshots for one producer + one reviewer + one dual-role agent (per architect goal). Slice-8 is gated on slice-1 measurement of cache_read_input_tokens on the real egg-agent prefix (R-1).
    - slice-9: `integration_tests/test_event_pump_qwen_repro.py` (placed directly under `integration_tests/`, consuming the `egg_stack` fixture per the trust-boundary correction) runs the #2906 reproducer end-to-end, asserts no churn, memory populated, permissions enforced, cache_read instrumented.

    Manual:
    - After slice-2, run `egg-orch --help` and confirm `brc list-blocking`, `phase get-context`, `phase get-assigned-tasks` are discoverable.
    - After slice-5, confirm the new event-pump wrapper is the only spawn path (old code dead but present); after slice-6, run `make test-all` (which exercises `integration_tests/test_*.py`; tests skip when kubectl is unavailable via `egg_stack`'s gate) and inspect orchestrator status to confirm wrapper-emitted heartbeats land at the expected cadence and the deletion sweep passed the grep zero-hit ACs.
    - After slice-9, review the spike-vs-baseline per-event cost numbers in the report + integration-test artifacts.
  manual_steps: |
    Pre-merge: none — every change ships behind the BRC cycle's review gate; no `.github/` files are touched.

    Post-merge:
    - slice-4: new Pydantic field on `Pipeline.no_progress_budget` is backfilled automatically by Pydantic on next `model_validate` of any serialized Pipeline (default_factory=dict); no SDLC contract schema bump, no migrator helper, no manual step.
    - slice-5 → slice-6: **CUTOVER PLAYBOOK** — drain in-flight pipelines on the old capped-restart wrapper before deploying slice-6's PR (operator monitors `egg-orch pipeline status` for all active pipelines, waits for them to reach `is_complete=true` or quiesce). slice-5 brings the new event-pump online (old code unreachable but still present); slice-6 deletes it. New pipelines start on the event-pump immediately after slice-5 deploys. No flagged fallback per cq-4. Documented in `docs/architecture/brc-event-pump.md` per TASK-9-2.
    - slice-9: docs-only; no operator action required.
slices:
  - id: 1
    name: |-
      WS0 de-risking spike + per-event cost instrumentation
    goal: |-
      Throwaway prototype proving the deterministic event-pump reaches BRC
      consensus without restart churn on the #2906 repro (issue-2270 on
      qwen3.7-max), plus per-event token-spend / wall-clock
      instrumentation under the real harness on that repro path.
      CACHE-TTL QUESTION IS EMPIRICALLY SETTLED — the operator hand-
      measured that BOTH the Anthropic and Qwen routes' prefix cache
      survives a >=60-min idle with ZERO re-creation, and observed BRC
      idles peak at ~10-13 min, so the cache always outlasts the gap on
      both routes. No keep-warm is needed on either route. Slice-1 MUST
      NOT re-derive the TTL ceiling: NO dedicated TTL-bracketing spike,
      NO multi-idle-duration injection (no 5.5 / 10 / 15 min variants,
      no "at least N idle durations" acceptance criterion), NO
      stop/go gate keyed on TTL survival, and NO report question of
      the shape "what Qwen-route TTL ceiling did the measurements
      show". Treat the >=60min figure as a settled INPUT to the
      design.
      Instrument per-event token spend (Anthropic via
      AgentResult.cost_usd, Qwen via config/litellm/cost_callback.py
      stdout captured by `kubectl logs deployment/egg-litellm` — NOT
      `~/.local/state/clm/cost-*.json` which is host-developer-only)
      and per-event wall-clock across a single representative #2906
      repro run. If the per-event cost capture happens to record
      cache_read_input_tokens vs cache_creation token counts as an
      incidental side effect, that is fine — what is forbidden is
      the dedicated idle-duration TTL-ceiling spike.
      HARD MEASUREMENT CONSTRAINT (risk_analyst BC-1): the per-event
      cost / wall-clock numbers MUST come from actual `python3 -m
      egg_agent` invocations under the new event-pump prototype with
      the production BRC preamble (assembled via
      orchestrator/routes/pipelines.py::_build_brc_preamble) and all
      agent-facing MCP tool schemas in the cached prefix (38 @tool
      registrations across sandbox/egg_agent_tools/tools/*). Raw
      `claude --output-format json` per-event cost numbers from the
      WS7 issue-body comments are NOT sufficient because the egg
      prefix layers are larger and structurally different — per-event
      cost must be re-measured under the real harness before any
      later slice (especially slice-8 prompt collapse) is gated on
      the numbers.
      Deliverable: measurement report under .egg-state/agent-outputs/
      that gates the rest of the work per operator feedback Q1
      (instrument only, no auto-fail; no TTL-ceiling stop/go gate).
      No production code change beyond a small hook in
      cost_callback.py if/only if needed to surface per-event
      attribution.
    tasks:
      - id: TASK-1-1
        description: |-
          Build the throwaway event-pump prototype script under `scripts/spike/2908_event_pump_prototype.sh`. The script must (a) loop on `egg-orch message wait-loop --timeout 60` to mimic the production wrapper; (b) on each actionable BRC event, invoke `python3 -m egg_agent` via the existing `shared/egg_agent/command.py:build_agent_command` entry point — **NOT** `claude --print`, which is an EGG100 anti-pattern per `docs/guides/agent-mode-design.md:90-104` and which the refine-phase analysis explicitly corrects; (c) carry a minimal in-script memory of prior actions across iterations (a tmp-file is sufficient for the spike — durable file format is decided in slice-6); (d) exit cleanly when the orchestrator reports the role consensus-confirmed and `is_complete`. The script is throwaway; no test coverage is required.
        acceptance: |-
          `scripts/spike/2908_event_pump_prototype.sh` exists; `grep -n 'claude --print\|claude -p\b'` returns zero hits in the script; running it against a mock orchestrator that emits one CONSENSUS_PROPOSE → ACK → CONSENSUS_CONFIRMED sequence completes without a Python traceback and exits 0; the script invokes `python3 -m egg_agent`, verifiable via `grep -n`.
        role: coder
        files:
          - scripts/spike/2908_event_pump_prototype.sh
      - id: TASK-1-2
        description: |-
          Run TASK-1-1's prototype against the #2906 reproducer (issue-2270 / qwen3.7-max in a k3s test cluster). Drive a single BRC cycle to CONFIRMED. **BC-1 HARD CONSTRAINT**: the run MUST exercise `python3 -m egg_agent` with the full production BRC preamble (assembled via `orchestrator/routes/pipelines.py::_build_brc_preamble` at line 12348) and the 38 MCP tool schemas registered via `sandbox/egg_agent_tools/tools/*`; raw `claude --output-format json` measurements are NOT acceptable. Capture wall-clock per-event, agent exit status, and the count of CONSENSUS_RE_REVIEW events. Record per-event `prompt_tokens`, `cache_read_input_tokens`, `cache_creation_tokens` from the AgentResult metadata (Anthropic route) AND from `kubectl logs deployment/egg-litellm` (Qwen route — the LiteLLM cost callback emits structured stdout per `config/litellm/cost_callback.py:344`; `Claude Code`'s `usage.*` returns all-zero on the Qwen route per refine analysis). The run output is the raw evidence input to TASK-1-4's report.
        acceptance: |-
          A run log is committed under `scripts/spike/2908_run_log.txt`; the run reaches CONSENSUS_CONFIRMED without the wrapper restarting the agent; no "Agent exited without BRC consensus" message appears in the run log; the log evidences `python3 -m egg_agent` invocation lines (NOT raw `claude` invocations).
        role: coder
        files:
          - scripts/spike/2908_run_log.txt
      - id: TASK-1-3
        description: |-
          Build the cost-log source adapter for Qwen-route per-event token-spend instrumentation. **Per the operator's iteration-1 directive, the cache TTL is a settled INPUT and this task does NOT bracket it** — the operator has hand-measured both the Anthropic and Qwen routes' prefix cache and confirmed they survive ≥ 60 min idle with zero re-creation; observed BRC idles peak at ~10-13 min so the cache always outlasts the gap on both routes; no keep-warm is needed on either route. The prior multi-idle injection (5.5 / 10 / 15 min) and "TTL ceiling" bracketing are dropped. What stays is the log-source abstraction so TASK-1-2's per-event measurement and downstream consumers (slice-9 TASK-9-1 integration test) share one source path. **Trust-boundary correction per the refine analysis**: the issue body's `~/.local/state/clm/cost-*.json` path returns zero hits via `grep -rn` — the LiteLLM cost callback at `config/litellm/cost_callback.py:344` emits structured stdout (no on-disk JSON files in the agent pod). The adapter sources Qwen cache-read counts from `kubectl logs deployment/egg-litellm` in cluster and from a stdout-tee file in CI/local; both paths return the same `{prompt_tokens, cache_read_tokens, cache_creation_tokens}` shape. **R-2 mitigation**: this adapter is reused by slice-9 TASK-9-1 so cost-callback instrumentation works the same in cluster and in CI/local. If TASK-1-2's per-event measurement happens to record `cache_read_input_tokens` vs `cache_creation_tokens` as a side effect of cost measurement, that is fine (and may be summarised in TASK-1-4); what is forbidden is any dedicated idle-duration TTL-ceiling spike or stop/go gate keyed on it.
        acceptance: |-
          A cost-log adapter is committed at `scripts/spike/2908_cost_log_adapter.py`; the adapter supports both `kubectl logs deployment/egg-litellm` (cluster) and a stdout-tee file (CI / local) as sources; both source paths return the same `{prompt_tokens, cache_read_tokens, cache_creation_tokens}` payload shape; no multi-idle-duration injection appears in the implementation; no synthetic idle is run by this task; no "TTL ceiling" / "TTL survival" / "bracket" assertions or gates; the adapter is importable from TASK-1-2's per-event measurement code AND from slice-9 TASK-9-1's integration test.
        role: coder
        files:
          - scripts/spike/2908_cost_log_adapter.py
          - config/litellm/cost_callback.py
      - id: TASK-1-4
        description: |-
          Write the spike measurement report at `.egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md` (task_planner write-scope on `.egg-state/agent-outputs/`). The report must explicitly answer: (a) did the event-pump prototype reach consensus on the #2906 repro without the 3-restart churn? (b) per-event cold-read cost in tokens for the production egg-agent prefix (BC-1 — explicitly stating the prefix tested includes `_build_brc_preamble` + tool schemas, NOT raw `claude`). The report also records the per-event wall-clock vs the current persistent-session model baseline (R-5 — if aggregate phase wall-clock regresses by >20%, surface and recommend a follow-up evaluation of a warm-Python-runner fallback). **Per the operator's iteration-1 directive, the report MUST state the cache TTL as a settled INPUT, NOT as a question to measure**: both the Anthropic and Qwen routes' prefix cache survive ≥ 60 min idle with zero re-creation; observed BRC idles peak at ~10-13 min so the cache always outlasts the gap on both routes; no keep-warm is needed on either route. The operator hand-measured this — there is nothing left to measure here. The report MUST NOT include any "Qwen-route TTL ceiling did the measurements show" question or any per-idle-duration bracket (5.5 / 10 / 15 min). If TASK-1-2's per-event measurement happens to record `cache_read_input_tokens` vs `cache_creation_tokens` as a side effect of cost measurement, those numbers MAY be tabulated as observed per-event evidence, but the report MUST NOT frame them as evidence for or against a TTL ceiling and MUST NOT gate any later slice on them.
        acceptance: |-
          `.egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md` exists; states the cache TTL as a settled INPUT (both Anthropic and Qwen routes survive ≥ 60 min idle with zero re-creation; observed BRC idles peak at ~10-13 min; no keep-warm needed on either route — operator hand-measured); contains NO "Qwen-route TTL ceiling did the measurements show" question, NO "{survives ≥ 15 min, lapses 5.5–15 min, ambiguous}" verdict, and NO 5.5 / 10 / 15-min idle-bracket section; explicitly states the prefix tested includes `_build_brc_preamble` + tool schemas (BC-1); lists per-event cold-read cost in tokens; lists aggregate phase wall-clock vs baseline (R-5).
        role: documenter
        files:
          - .egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md
    # root slice — omit dependencies
  - id: 2
    name: |-
      Net-new CLI commands the event-pump depends on
    goal: |-
      Per cq-1 mandate (split MCP→CLI collapse to a follow-up but
      guarantee CLI coverage for the new control flow), add purely
      additive CLI shims around existing handlers per architect v3
      scope:
      (a) `egg-orch brc get-state` (handler `brc_get_state` at
      sandbox/egg_agent_tools/handlers/brc.py:679+; surfaces the
      same data as `egg-orch consensus status --json` but at a
      stable canonical name cq-1 names);
      (b) `egg-orch brc list-blocking` (handler `brc_list_blocking`
      at sandbox/egg_agent_tools/handlers/brc.py:726-740; backs onto
      the orchestrator status endpoint whose `blocking_agents` list
      comes from PeerConsensusTracker.get_state — method def at
      orchestrator/peer_consensus.py:1637; the `blocking_agents`
      computation it returns is the comprehension at :1594-1626);
      (c) `egg-orch phase get-context` (handler at
      sandbox/egg_agent_tools/handlers/phase.py:139);
      (d) `egg-orch phase get-assigned-tasks` (handler
      `phase_get_assigned_tasks` at handlers/phase.py:193);
      (e) **Extension to `egg-orch consensus status`** (handler
      `brc_get_state` at handlers/brc.py:679+ backs onto
      orchestrator-side `/api/v1/pipelines/<pid>/status` at
      orchestrator/routes/pipelines.py:3911 + assembled at
      :4531-4557) to carry the durable orchestrator-side Pipeline
      fields the wrapper needs after host restart: `no_progress_budget`
      (per-role counter map from `Pipeline.no_progress_budget`
      landed in slice-4 TASK-4-1) + `parked_decisions` (the
      "pipeline parked" HITL decisions from `Pipeline.decisions`
      when present). Additive, backwards-compatible response
      fields on the existing endpoint. CLI shim adds optional
      `--field <dotted.path>` projection. **This is the substrate
      correction per the risk_analyst v2 NACK** — replaces the
      earlier draft's `egg-contract show --field` which would have
      hit the gateway-side SDLC Contract model (the wrong substrate
      per architect v3 d-13);
      (f) `egg-orch consensus resolve-obligation` shim (handler
      `brc_resolve_obligation` at handlers/brc.py:743) per architect
      v3 slice-2(f).
      No existing MCP tools are removed (cq-1). Tests in tests/tools/
      extend the MCP↔CLI parity harness at
      tests/tools/test_mcp_cli_drift.py.
    tasks:
      - id: TASK-2-1
        description: |-
          Add a new `brc` root subparser to `sandbox/egg_lib/orch_cli.py` and a `list-blocking` subcommand that wraps the existing `brc_list_blocking` handler at `sandbox/egg_agent_tools/handlers/brc.py:726`. Use the **direct-handler-import pattern** already in production at `sandbox/egg_lib/contract_cli.py:342` (`cmd_show` imports `from egg_agent_tools.handlers import sdlc as _handlers` at module top and calls `_handlers.show_contract(req)` directly) — there is no `_handler_dispatch` helper (verified via `grep -n '_handler_dispatch' sandbox/egg_lib/*.py` → zero hits per the v1 NACK). Flip the `TOOL_REGISTRY` entry for `mcp__brc__list_blocking` from `cli_command=None` to the new verb name.
        acceptance: |-
          `egg-orch brc list-blocking --pipeline <pid>` prints the same JSON the MCP tool returns; `make test` passes `tests/tools/test_mcp_cli_drift.py`; the registry entry's `cli_command` is no longer `None`.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-2-2
        description: |-
          Add a new `phase` root subparser to `sandbox/egg_lib/orch_cli.py` and a `get-context` subcommand that wraps the existing `phase_get_context` handler at `sandbox/egg_agent_tools/handlers/phase.py:139`. Use the direct-handler-import pattern (`from egg_agent_tools.handlers import phase as _handlers` at module top, `_handlers.phase_get_context(req)` inside the new `cmd_*`). Flip the `TOOL_REGISTRY` entry for `mcp__phase__get_context` from `cli_command=None` to the new verb name.
        acceptance: |-
          `egg-orch phase get-context --pipeline <pid>` prints the same payload the MCP tool returns; drift test green; registry entry's `cli_command` is no longer `None`.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/phase.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-2-3
        description: |-
          Add a `get-assigned-tasks` subcommand under the new `phase` root subparser (TASK-2-2) that wraps the existing `phase_get_assigned_tasks` handler at `sandbox/egg_agent_tools/handlers/phase.py:193`. Use the direct-handler-import pattern; share the `_handlers` import added in TASK-2-2. Flip the `TOOL_REGISTRY` entry for `mcp__phase__get_assigned_tasks` from `cli_command=None` to the new verb name.
        acceptance: |-
          `egg-orch phase get-assigned-tasks --pipeline <pid> --role <r>` prints the same payload the MCP tool returns; drift test green; registry entry's `cli_command` is no longer `None`.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/phase.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-2-4
        description: |-
          Update `tests/tools/test_mcp_cli_drift.py` to reflect the new CLI verbs landed in TASK-2-1 / TASK-2-2 / TASK-2-3 / TASK-2-5 / TASK-2-7. Flip the parity-matrix expectations so `mcp__brc__list_blocking`, `mcp__phase__get_context`, `mcp__phase__get_assigned_tasks`, `mcp__brc__get_state`, and `mcp__brc__resolve_obligation` have non-None `cli_command` values; ensure `test_cli_less_tools_are_documented_gaps` reflects the new state (those five tools are removed from the documented-gaps list). Per cq-1's split, `mcp__brc__read_peer_artifact` and `mcp__task__mark_gap` are NOT migrated in this issue and stay in the documented-gaps list (architect v3 slice-2 confirms this scope).
        acceptance: |-
          `make test` passes `test_mcp_cli_drift.py`; the post-update gap list contains only the remaining MCP-only tools (`task__mark_gap`, `brc__read_peer_artifact`).
        role: tester
        files:
          - tests/tools/test_mcp_cli_drift.py
      - id: TASK-2-5
        description: |-
          Add `egg-orch brc get-state` subcommand under the `brc` root subparser (TASK-2-1) that wraps the existing `brc_get_state` handler at `sandbox/egg_agent_tools/handlers/brc.py:679`. Use the direct-handler-import pattern; share the `_handlers` import added in TASK-2-1. Per architect v3 slice-2(a), this is the canonical name cq-1 mentions; it surfaces the same data as `egg-orch consensus status --json` but at the stable canonical name. Flip the `TOOL_REGISTRY` entry for `mcp__brc__get_state` from `cli_command=None` to the new verb name.
        acceptance: |-
          `egg-orch brc get-state --pipeline <pid>` prints the same JSON the MCP tool returns AND matches `egg-orch consensus status --json --pipeline <pid>` (same underlying handler); drift test green; registry entry's `cli_command` is no longer `None`.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-2-6
        description: |-
          **Extend `egg-orch consensus status`** per architect v3 slice-2(e) — replaces the v2 d-13 SDLC-Contract substrate choice. Extend the existing `brc_get_state` handler at `sandbox/egg_agent_tools/handlers/brc.py:679+` and the orchestrator-side `/api/v1/pipelines/<pid>/status` route at `orchestrator/routes/pipelines.py:3911` (response assembled at `:4531-4557`) to additionally include two fields in the response payload: (a) `no_progress_budget` — sourced from `Pipeline.no_progress_budget` once slice-4 TASK-4-1 lands the field; for slice-2 the field is added to the response schema and populated when present (backwards-compat: empty dict if the Pipeline doesn't yet have the field); (b) `parked_decisions` — sourced from `Pipeline.decisions` filtered to entries with the `pipeline parked` shape. Both are additive, backwards-compatible response fields on the existing endpoint. Add a `--field <dotted.path>` argparse projection on the `cmd_consensus_status` CLI shim at `sandbox/egg_lib/orch_cli.py` so the wrapper can read selective fields after host restart (e.g. `egg-orch consensus status --field no_progress_budget --field parked_decisions`). **This is the wrapper's host-restart recovery read path** (consumed by slice-5 TASK-5-3 and slice-6 TASK-6-3) — NOT `egg-contract show --field`, which targets the wrong substrate per architect v3 d-13.
        acceptance: |-
          `egg-orch consensus status --json --pipeline <pid>` payload includes `no_progress_budget` (dict) and `parked_decisions` (list) keys (empty when absent); the `--field <dotted.path>` flag returns only the requested fields when present; legacy callers without `--field` see the full payload extended with the two new keys (backwards-compat preserved); unit test asserts the wrapper-side host-restart read pattern `egg-orch consensus status --field no_progress_budget` returns the right scalar.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
          - orchestrator/routes/pipelines.py
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-2-7
        description: |-
          Add `egg-orch consensus resolve-obligation` CLI shim per architect v3 slice-2(f) wrapping the existing `brc_resolve_obligation` handler at `sandbox/egg_agent_tools/handlers/brc.py:743`. Use the direct-handler-import pattern. Flip the `TOOL_REGISTRY` entry for `mcp__brc__resolve_obligation` from `cli_command=None` to the new verb name. Note: if the handler accepts a `note` or other prose argument, the CLI shim MUST accept it via `--note-file` or stdin (NEVER argv) — preserves the #2741 shell-metachar guard.
        acceptance: |-
          `egg-orch consensus resolve-obligation --pipeline <pid> --reviewer <r> --producer <p>` reaches the handler; drift test green; registry entry's `cli_command` is no longer `None`; if a `--note` equivalent is exposed, it's via `--note-file` or stdin (regression guard against #2741).
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/__init__.py
      - id: TASK-2-8
        description: |-
          Write unit tests for the new CLI verbs at `tests/sandbox/egg_lib/test_brc_phase_cli_verbs.py` covering: invocation reaches the handler (direct-handler-import parity); `--json` output matches the corresponding MCP tool's payload byte-for-byte; error paths (missing args, invalid pipeline) return non-zero exit; **TASK-2-6 host-restart-read path**: assert `egg-orch consensus status --json` payload includes the `no_progress_budget` + `parked_decisions` keys (empty when absent); assert `--field` projection returns only the requested fields; **TASK-2-7 #2741 regression guard**: if `consensus resolve-obligation` accepts any prose argument, the test confirms it comes via `--note-file` or stdin (not argv).
        acceptance: |-
          All listed cases have a unit test; `make test` passes.
        role: tester
        files:
          - tests/sandbox/egg_lib/test_brc_phase_cli_verbs.py
    dependencies:
      - slice-1
  - id: 3
    name: |-
      Server-side consensus next-action endpoint + CLI
    goal: |-
      Add `GET /api/v1/pipelines/<pid>/consensus/next-action?role=R[&slice=S]`
      returning `{action, target_producer?, version?, blocking_reason?,
      parked?, reason}` derived from PeerConsensusTracker.get_state +
      latest bus tip + pending HITL decisions. Add the matching
      `egg-orch consensus next-action --role R --json` CLI shim.
      Include a `tracker_reconstructing` action variant + small TTL so
      a post-host-restart probe doesn't spin. This is the brain the
      new wrapper queries each cycle (decision d-3).
    tasks:
      - id: TASK-3-1
        description: |-
          Add `GET /api/v1/pipelines/<pid>/consensus/next-action?role=R[&slice=S]` route in `orchestrator/routes/pipelines.py`. Derive the response from `PeerConsensusTracker.get_state(pid)` (orchestrator/peer_consensus.py:1594-1626 plus version tracking at :114-124) + latest bus tip (CONSENSUS_PROPOSE / CONSENSUS_RE_REVIEW for the role) + pending HITL decisions from `Pipeline.decisions`. Payload: `{action: <enum>, target_producer?: str, version?: int, blocking_reason?: str, parked?: bool, reason: str}` where `action` ∈ {`propose`, `review`, `ack_required`, `nack_required`, `re_review_required`, `confirm_required`, `wait`, `wait_on_peers`, `wait_on_human`, `address_nacks`, `resolve_obligation`, `tracker_reconstructing`, `complete`}. Encoding: (i) open-NACK aggregation barrier (#2142) — `address_nacks` when ≥2 distinct reviewers have NACKed the current version; (ii) stale-version re-review (#2482) — `re_review_required` with bumped `version` + last-observed cursor; (iii) **dual-role producer-first ordering (#2749 / risk_analyst R-6 — architect v3 slice-3 explicit two-transition table)** — both transitions are HARD ACs: **(iii.a)** dual-role agent with `producer-phase=WORKING AND proposal_version=0` AND a peer's CONSENSUS_PROPOSE pending review → return `action: propose` (producer-first; own first propose precedes reviewer obligations); **(iii.b)** dual-role agent with producer-phase past first-propose (`proposal_version >= 1`) AND a peer's CONSENSUS_PROPOSE pending review → return `action: review` (reviewer obligation now binds); **(iii.c)** dual-role agent with `proposal_version >= 1` AND own NACK pending → return `action: re_review_required` or `address_nacks` (own re-propose takes precedence over peer review); (iv) resolve_obligation surface; (v) confirm-precondition met (#2531 directed nudge) → `confirm_required`; (vi) `tracker_reconstructing` variant with `retry_after_ms` TTL so a post-host-restart probe doesn't spin — returned when `reconstruct_tracker_from_messages` (`peer_consensus.py:1955`) is in progress (returned as `{action: wait, reason: tracker_reconstructing, retry_after_ms: <ms>}` per architect v3 slice-3). Document the wire schema in the route's docstring including the three explicit dual-role transitions.
        acceptance: |-
          Route exists; producer with ≥2 unresolved reviewer NACKs → `address_nacks`; dual-role agent with no own proposal → `propose` (not `review`); `tracker_reconstructing` response carries a positive `retry_after_ms`; unit tests at `tests/orchestrator/test_consensus_next_action.py` (TASK-3-3) cover all branches.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/peer_consensus.py
      - id: TASK-3-2
        description: |-
          Add `egg-orch consensus next-action --role R [--slice S] --json` CLI shim in `sandbox/egg_lib/orch_cli.py`. Use the direct-handler-import pattern; the CLI handler forwards to the new orchestrator HTTP endpoint via the existing `gateway_client` (or direct HTTP if the verb runs from the wrapper without the gateway). `--json` emits the raw payload; non-`--json` prints a readable summary.
        acceptance: |-
          `egg-orch consensus next-action --role coder --json` returns the same JSON the route returns; non-`--json` prints a readable summary; the verb is reachable via `--help`; `make test` passes.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-3-3
        description: |-
          Write unit tests at `tests/orchestrator/test_consensus_next_action.py` covering: producer with no NACKs → `propose` (initial) or `wait_on_peers` (post-propose); producer with one reviewer NACK → `address_nack`; producer with ≥2 reviewer NACKs → `address_nacks` (#2142); reviewer with target producer at unreviewed version → `ack_required` or `nack_required`; reviewer whose ACK is stale → `re_review_required` (#2482); **dual-role explicit three-transition table per architect v3 slice-3 + risk_analyst v2 symmetric-case ask**: (a) `producer-phase=WORKING + proposal_version=0` + peer pending review → `action: propose` (producer-first); (b) `proposal_version >= 1` + peer pending review + no own NACK → `action: review` (reviewer obligation binds); (c) `proposal_version >= 1` + own NACK pending → `re_review_required` or `address_nacks` (own re-propose precedes peer review); confirm-precondition met → `confirm_required` (#2531); resolve_obligation surface; `tracker_reconstructing` → `{action: wait, reason: tracker_reconstructing, retry_after_ms: <ms>}` per architect v3; all confirmed → `complete`; CLI shim parity (`--json` byte-equality with the route response).
        acceptance: |-
          All listed cases have a unit test; ALL THREE dual-role transitions (a)/(b)/(c) are explicitly named in the test class/method names; tests pass; coverage on the next-action derivation function ≥ 95% lines.
        role: tester
        files:
          - tests/orchestrator/test_consensus_next_action.py
    dependencies:
      - slice-2
  - id: 4
    name: |-
      Durable no-progress safety budget + HITL park decision
    goal: |-
      Add `Pipeline.no_progress_budget` (per-role counter) **as a
      new Pydantic field on the orchestrator-side Pipeline model
      at `orchestrator/models.py:1053`** (next to the existing
      `Pipeline.decisions: list[HITLDecision]` which is the
      precedent per architect v3 d-4). Persisted via the existing
      git-backed StateStore so it survives host / orchestrator
      restart (cq-3 hard requirement). **No SDLC Contract schema
      bump** — architect v3 d-13 explicitly rejected the
      schemaVersion 1.2→1.3 option to avoid the ~200-live-1.2-
      contracts migration risk (R-3 is structurally retired by this
      substrate choice). Parked-HITL state lives on the existing
      `Pipeline.decisions: list[HITLDecision]` list (no new field
      needed); the HITL park decision is just a regular
      `HITLDecision` entry. Increment rule: same next-action
      verdict twice with no intervening tracker state change. Reset
      on any state advance for that role. On exhaustion: fire
      OVERSEER_ALERT via the existing `_emit_producer_death_alert`
      callable (orchestrator/routes/pipelines.py:15310-15390) AND
      register a HITL decision `pipeline parked — no progress for
      N events. Resume or abort?`. Wire `resume` (clears the
      per-role counter on `Pipeline.no_progress_budget`) and
      `abort` (transitions to FAILED) into the existing decision-
      handler pattern at orchestrator/routes/decisions.py:77-200
      (`_handle_restart_agent` at `:77-150` is the architect-named
      template). Tests must cover host-death recoverability — kill
      the orchestrator mid-cycle, restart, verify counter +
      parked decision survive. DURABILITY PARTIAL-FAILURE
      SEMANTICS (risk_analyst BC-3): the sync-flush variant
      `_save_pipeline_durable(pipeline)` on
      `orchestrator/state_store.py` must specify BOTH the success
      path (returns only after `git push` succeeds) AND the
      partial-failure path. On `git push` failure: raise a typed
      exception `DurableSaveFailed` — the safety-budget consumer
      (TASK-5-3 in the wrapper) handles this by emitting an
      OVERSEER_ALERT and falling back to an in-memory budget
      snapshot, continuing the wait-loop. Do NOT exit the wrapper
      on partial failure. Bounded retry policy documented in the
      function's docstring. Unit tests in
      `tests/orchestrator/test_save_pipeline_durable.py` must
      cover both paths (push-succeeds and mock push-failure).
    tasks:
      - id: TASK-4-1
        description: |-
          Add `no_progress_budget` as a new Pydantic field on the orchestrator-side **`Pipeline` model at `orchestrator/models.py:1053`** (next to the existing `Pipeline.decisions: list[HITLDecision]` field, which is the precedent per architect v3 d-4 / d-13). Shape: `no_progress_budget: dict[str, NoProgressBudgetEntry] = Field(default_factory=dict, ...)` where `NoProgressBudgetEntry` is a small Pydantic model with `{remaining_seconds: int, last_progress_at: datetime | None, threshold_seconds: int, alert_emitted: bool}`. Per-role keys map role names to entries. **No SDLC Contract schema bump required** — architect v3 d-13 explicitly rejected the schemaVersion 1.2→1.3 bump option ("(c) Move Pipeline.no_progress_budget onto Contract — REJECTED because that requires the schemaVersion 1.2 → 1.3 bump d-4 explicitly avoided, reintroducing the ~200-live-contracts migration risk."). Pydantic backfills the field with its `default_factory=dict` on next `model_validate` of an older serialized Pipeline JSON — no migrator helper is needed. The parked-HITL state lives on the existing `Pipeline.decisions: list[HITLDecision]` list (no new field needed); the slice-4 decision-handler in TASK-4-4 inserts the "pipeline parked" decision as a regular `HITLDecision` entry.
        acceptance: |-
          `orchestrator/models.py:Pipeline` has a new `no_progress_budget: dict[str, NoProgressBudgetEntry]` field with `default_factory=dict`; the `NoProgressBudgetEntry` Pydantic model is defined in the same file with the four named fields; an older serialized Pipeline JSON without the new field loads via `model_validate` and reports `no_progress_budget == {}` (backfill via default); no changes to `shared/egg_contracts/models.py` schemaVersion; no `_migrate_schema_version_*` helper added.
        role: coder
        files:
          - orchestrator/models.py
      - id: TASK-4-2
        description: |-
          Add the sync-flush variant `_save_pipeline_durable(pipeline)` in `orchestrator/state_store.py` alongside the existing async-best-effort `save_pipeline` at line 672. **Operates on the orchestrator-side Pipeline model** (`orchestrator/models.py:1053`) — NOT the gateway-side SDLC Contract; these are different stores reached via different endpoints per architect v3 d-13. Returns ONLY after `git push origin <branch>` completes; same local-commit path as `save_pipeline` but inlines the push (no daemon thread) and surfaces push failure to the caller. **BC-3 HARD AC**: on `git push` failure, raise typed exception `DurableSaveFailed`; document bounded retry policy in the docstring; safety-budget consumer (TASK-5-3) handles `DurableSaveFailed` by emitting an OVERSEER_ALERT and continuing the wait-loop with an in-memory snapshot — does NOT exit the wrapper. Existing async path at `:890-928` stays the default; split is at the call-site, not the helper.
        acceptance: |-
          `_save_pipeline_durable` exists with signature `(state, /, *, branch=None)`; happy path returns only after `git push` reports success; on push failure raises `DurableSaveFailed` (typed exception class); docstring documents the bounded retry policy AND the caller's responsibility to NOT crash-loop the wrapper; existing `save_pipeline` calls unchanged; `tests/orchestrator/test_save_pipeline_durable.py` (TASK-4-5) covers both paths AND production failure-mode (emptyDir wipe + remote ref behind → fresh host loads from `origin/<branch>`).
        role: coder
        files:
          - orchestrator/state_store.py
      - id: TASK-4-3
        description: |-
          Add `_startup_reconciliation_replay_safety_budget` step in `orchestrator/startup_reconciliation.py`. On orchestrator boot iterates active pipelines: (i) reads durable `Pipeline.no_progress_budget` from the **orchestrator-side StateStore** (`orchestrator/state_store.py`'s existing pipeline loader — NOT `load_contract_from_branch` at `contract_store.py:127`, which reads the gateway-side SDLC Contract, the wrong substrate per architect v3 d-13); (ii) primes in-memory `health_monitor.py:82-103` `AgentState` dataclass anchors (heartbeat field at line 87); (iii) honours `alert_emitted=true` from durable state so a fresh host does NOT re-fire OVERSEER_ALERT for an already-alerted budget. Add a re-prime entry point on `health_monitor.py` that accepts the recovered budget state.
        acceptance: |-
          The new step runs on orchestrator boot; `tests/orchestrator/test_startup_reconciliation_safety_budget.py` (TASK-4-5) simulates host-kill mid-budget → fresh host primes the same budget state from the orchestrator StateStore on `origin/<branch>`; `alert_emitted=true` recovered state is honoured (no re-fire); no calls to `load_contract_from_branch` in the new step.
        role: coder
        files:
          - orchestrator/startup_reconciliation.py
          - orchestrator/health_monitor.py
      - id: TASK-4-4
        description: |-
          Wire `resume` (clears the per-role counter in `Pipeline.no_progress_budget`) and `abort` (transitions the pipeline to FAILED) into the existing decision-handler pattern at `orchestrator/routes/decisions.py:77-200` (using `_handle_restart_agent` at `:77-150` as the model per architect d-4). On HITL park decision creation, the handler appends a `HITLDecision` to the existing `Pipeline.decisions: list[HITLDecision]` field and writes via `_save_pipeline_durable` (TASK-4-2) so parked state survives host restart; on resolution, the handler updates the decision's `resolution` field on the same list and persists via the same sync-flush. HITL prompt: `"pipeline parked — no progress for N events. Resume or abort?"`. **Parked state substrate**: the existing `Pipeline.decisions` list — no new field needed; only `Pipeline.no_progress_budget` is new in TASK-4-1.
        acceptance: |-
          A unit test creates a parked-HITL decision via the new handler (a regular `HITLDecision` in `Pipeline.decisions`), asserts persistence via `_save_pipeline_durable`, resolves as `resume` → `Pipeline.no_progress_budget[role].remaining_seconds` is reset to the threshold + the decision's resolution is set → both persisted via sync-flush; resolves as `abort` → pipeline state transitions to FAILED; `make test` passes.
        role: coder
        files:
          - orchestrator/routes/decisions.py
      - id: TASK-4-5
        description: |-
          Tests: (a) `tests/orchestrator/test_save_pipeline_durable.py` — happy path returns only after `git push` succeeds; **BC-3 partial-failure** (mock `git push` failure → raises `DurableSaveFailed`); production failure-mode (emptyDir wipe + remote ref behind → fresh host loads durable fields from `origin/<branch>`). (b) `tests/orchestrator/test_startup_reconciliation_safety_budget.py` — host-kill mid-budget → fresh host loads same budget state via `_startup_reconciliation_replay_safety_budget`; `alert_emitted=true` recovered state does NOT re-fire OVERSEER_ALERT.
        acceptance: |-
          All listed cases pass; production failure-mode explicitly simulates emptyDir wipe + remote ref behind (NOT just process-exit-same-FS); `make test` passes both files.
        role: tester
        files:
          - tests/orchestrator/test_save_pipeline_durable.py
          - tests/orchestrator/test_startup_reconciliation_safety_budget.py
      - id: TASK-4-6
        description: |-
          Unit test at `tests/orchestrator/test_pipeline_no_progress_budget_field.py` covering the new Pydantic field's backwards-compatibility (per architect v3 d-13's rejection of the SDLC Contract schemaVersion bump — risk_analyst R-3 is now structurally avoided because we are NOT migrating any contract). Cases: (a) older serialized Pipeline JSON without the field loads via `Pipeline.model_validate` and reports `no_progress_budget == {}` (Pydantic backfill via `default_factory=dict`); (b) Pipeline with populated `no_progress_budget` round-trips through `model_dump` + `model_validate` without data loss; (c) `NoProgressBudgetEntry` defaults: `remaining_seconds=0`, `alert_emitted=False`, `last_progress_at=None` round-trip correctly; (d) StateStore-level smoke test: load a representative sample of `.egg-state/pipelines/*.json` (live pipeline records on disk) and assert each `Pipeline.model_validate` succeeds with `no_progress_budget` backfilled to `{}` — proves no migration is needed for existing on-disk pipelines.
        acceptance: |-
          All four cases pass; the StateStore-level smoke test against `.egg-state/pipelines/*.json` samples passes (or skips cleanly with a clear message if the dir is empty in CI); no SDLC contract schema-migration test file is created (the ~200 live 1.2 contracts are not touched by this slice); `make test` passes.
        role: tester
        files:
          - tests/orchestrator/test_pipeline_no_progress_budget_field.py
    dependencies:
      - slice-3
  - id: 5
    name: |-
      Event-pump online (additive + caller rewires; no deletion)
    goal: |-
      The additive half of the swap (reviewer_plan §11 subdivision
      of original slice-5 → 5a). Bring the new event-pump online
      without deleting any legacy code yet — old wrapper paths stay
      present but become unreachable as the new template replaces
      `_CONSENSUS_WRAPPER_TEMPLATE` wholesale. Pieces:
      (a) Add `--memory-file PATH` and `--event-json STRING` flags
      to `python3 -m egg_agent` via shared/egg_agent/command.py
      (~:34-46 argv shape); behaviour-with-neither-flag MUST remain
      identical to today (regression guard test).
      (b) Rewrite orchestrator/consensus_wrapper.py's
      `_CONSENSUS_WRAPPER_TEMPLATE` (currently :116-713) as the
      deterministic event-pump bash that invokes
      `egg-orch message wait-loop`, `egg-orch consensus next-action`
      (slice 3), and one-shot `python3 -m egg_agent --memory-file
      ... --event-json ...` per actionable event.
      (c) SHELL-PROSE CORRUPTION GUARD (risk_analyst BC-2, #2741):
      the per-event prompt + event-json + memory snapshot are all
      LLM-authored prose (NACK reasons, files_reviewed lists,
      reviewer reasoning); the new event-pump template MUST pass
      them to `python3 -m egg_agent` either via shlex.quote-applied
      argv (mirroring the existing
      consensus_wrapper.py:759-760 `shlex.quote(prompt_text)`
      pattern) OR via stdin / a tempfile path. A regression test in
      orchestrator/tests/test_consensus_wrapper_event_pump.py must
      inject a prompt + event payload containing $, backtick,
      single-quote, double-quote, newline and assert the agent
      receives the payload byte-identical.
      (d) Rewire `concurrent_executor._spawn_agent` at
      orchestrator/concurrent_executor.py:445-524 and any
      routes/pipelines.py restart-path callers to the new template
      signature.
      (e) Wire the safety-budget consumer against the slice-4
      durable Pipeline.no_progress_budget, including the BC-3
      partial-failure handling (DurableSaveFailed → OVERSEER_ALERT
      + continue loop, NOT exit).
      End-state: new event-pump is the only spawn path. cq-4 is
      preserved — there is no flag path back to the old wrapper
      because the rewrite replaces the template wholesale; old code
      paths in consensus_wrapper.py are simply unreachable until
      slice-6 deletes them.
    tasks:
      - id: TASK-5-1
        description: |-
          Add `--memory-file PATH` and `--event-json STRING` flags to `python3 -m egg_agent` (in `shared/egg_agent/command.py:build_agent_command` argv shape `:34-46`, or in `shared/egg_agent/__main__.py` argv parsing). New wrapper template (TASK-5-2) passes per-role memory path + per-event JSON payload. **Legacy regression guard (architect slice-5(a) goal)**: behaviour with neither flag MUST remain identical to today; existing callers see no change. This task lands FIRST so the wrapper template in TASK-5-2 can call into the new flag surface.
        acceptance: |-
          `python3 -m egg_agent --help` lists the two new flags; running without the flags produces identical behaviour to current `build_agent_command` (compare argv via a unit test); a separate unit test confirms passing `--memory-file <path>` to a non-existent path is non-fatal (creates the file or skips memory-read with a warning).
        role: coder
        files:
          - shared/egg_agent/command.py
          - shared/egg_agent/__main__.py
      - id: TASK-5-2
        description: |-
          Rewrite `_CONSENSUS_WRAPPER_TEMPLATE` in `orchestrator/consensus_wrapper.py` (lines 116-713) as a deterministic Bash event pump. Template: (i) reads `EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `EGG_SLICE_ID` from env; (ii) in a `while true` loop, calls `egg-orch consensus next-action --role $EGG_AGENT_ROLE [--slice $EGG_SLICE_ID] --json` (slice-3 endpoint) and inspects `action`; (iii) `action == complete` → exit 0; (iv) `action == tracker_reconstructing` → sleep `retry_after_ms` and re-poll; (v) `action ∈ {wait_on_peers, wait_on_human}` → `egg-orch message wait-loop --for CONSENSUS_PROPOSE --for CONSENSUS_NACK --for CONSENSUS_ACK --for CONSENSUS_RE_REVIEW --for STATUS --for HANDOFF --for HITL_RESOLVED --for OVERSEER_ALERT --timeout 60`; (vi) any other actionable `action` → spawn agent via `python3 -m egg_agent --memory-file ... --event-json ...` (TASK-5-1 flags). Rewrite `build_consensus_wrapped_command` at `:716-775` to emit the new template. **BC-2 HARD AC**: per-event prompt + `--event-json` + `--memory-file` content snapshot are LLM-authored prose; pass via either (a) `shlex.quote`-applied argv (mirroring `consensus_wrapper.py:759-760`'s existing `shlex.quote(prompt_text)` pattern) OR (b) stdin / tempfile. Regression-tested in TASK-5-5. **Per cq-4 + architect slice-5(b)**: do NOT delete any of the legacy template blocks (`MAX_CONSENSUS_RESTARTS`, `_RECOVERY_*`, SSE machinery, restart loop, recovery-prompt re-templating, terminal exit-1) in this slice — they become unreachable when the new template replaces them wholesale but stay present until slice-6 removes them.
        acceptance: |-
          The new `_CONSENSUS_WRAPPER_TEMPLATE` is the only template emitted by `build_consensus_wrapped_command`; snapshot test of the emitted script asserts the new control flow (wait-loop + next-action + per-event egg_agent invocation; no curl SSE); **the diff for `consensus_wrapper.py` REPLACES the OLD template at the same line range — no two templates coexist in the file even transiently** (architect-v3 belt-and-suspenders AC; reviewer_plan v2 non-blocking nudge); legacy `MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT` symbols STILL EXIST in the file (they are deleted in slice-6 TASK-6-1) but are unreachable from the emitted template (verified via the snapshot); BC-2 shell-metachar test in TASK-5-5 passes.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-5-3
        description: |-
          Wire the safety-budget consumer in the new wrapper template against the slice-4 durable `Pipeline.no_progress_budget` field. Each iteration: (i) **reads `no_progress_budget` via `egg-orch consensus status --field no_progress_budget --field parked_decisions --json` against the slice-2 TASK-2-6 endpoint extension** (NOT `egg-contract show --field` — wrong substrate per architect v3 d-13); (ii) if the wait returns an event, resets `last_progress_at` to now and persists via slice-4 `_save_pipeline_durable` against the orchestrator-side `Pipeline` model; (iii) if `remaining_seconds <= 0` and `alert_emitted == false`, fires `_emit_producer_death_alert` at `routes/pipelines.py:15310` AND opens a parked HITL decision via slice-4 decision-handler (TASK-4-4) — prompt: `"pipeline parked — no progress for N events. Resume or abort?"` — the decision lands in `Pipeline.decisions: list[HITLDecision]` (existing field); (iv) keeps looping (no auto-FAIL per cq-3). On a `parked_decisions` entry with `selected == abort` → exits non-zero; `resume` → clears the per-role counter on `Pipeline.no_progress_budget` (sync-flush) and continues. **BC-3 HARD AC**: if `_save_pipeline_durable` raises `DurableSaveFailed`, wrapper emits OVERSEER_ALERT and continues with in-memory snapshot — does NOT crash-loop. **Host-restart re-fire suppression**: on wrapper startup, the first `egg-orch consensus status --field no_progress_budget --field parked_decisions` call reads durable state; if any entry has `alert_emitted=true`, the alert is NOT re-emitted. The rewiring of the 3-restart trigger arm at `routes/pipelines.py:18100-18159` to safety-budget exhaustion happens in slice-6 (TASK-6-3) alongside the deletion sweep — this task only wires the *consumer side* in the wrapper template.
        acceptance: |-
          Unit test injects budget=0 → (i) OVERSEER_ALERT fires once (`alert_emitted` flag honored); (ii) HITL decision created; (iii) wait-loop continues until decision lands; (iv) `abort` → non-zero exit; `resume` → clears and continues. Separate unit test asserts BC-3: mock `_save_pipeline_durable` → `DurableSaveFailed` → wrapper emits OVERSEER_ALERT + continues. Host-restart unit test asserts `alert_emitted=true` in durable state does NOT re-emit OVERSEER_ALERT.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-5-4
        description: |-
          Rewire `concurrent_executor._spawn_agent` at `orchestrator/concurrent_executor.py:445-524` and the restart-path caller at `orchestrator/routes/pipelines.py:2792-2796` to the new event-pump template signature (TASK-5-2). The new `build_consensus_wrapped_command` no longer accepts `max_restarts` / recovery-prompt placeholders — callers update accordingly. Restart paths now resume the same event pump rather than rebuilding with recovery-prompt placeholders.
        acceptance: |-
          Both call sites pass `make test`; the import at `concurrent_executor.py:37` resolves to the new template; restart paths no longer pass `restart_number` / recovery-prompt placeholders; `grep -n 'max_restarts=\|restart_number=' orchestrator/concurrent_executor.py orchestrator/routes/pipelines.py` returns zero hits in the call sites updated by this task (the legacy template constants in `consensus_wrapper.py` still exist until slice-6 deletes them).
        role: coder
        files:
          - orchestrator/concurrent_executor.py
          - orchestrator/routes/pipelines.py
      - id: TASK-5-5
        description: |-
          New unit-test file at `tests/orchestrator/test_consensus_wrapper_event_pump.py` (architect slice-5(c) names this file). Coverage: event-pump reaches consensus without a restart cap (mock orchestrator emits PROPOSE → ACK → CONFIRMED; wrapper exits 0); clean post-event exit does NOT trigger FAIL (mock a normal `python3 -m egg_agent` exit code 0 and assert wrapper loops); `egg-orch consensus next-action --json` replaces SSE consumer cleanly (assert no curl invocation against the SSE stream); **BC-2 shell-metachar regression** — prompt + event-json + memory-file containing `$`, backtick, single-quote, double-quote, newline → agent receives byte-identical (per architect slice-5(c)); **BC-3 partial-failure path** — mock `_save_pipeline_durable` raising `DurableSaveFailed` → wrapper emits OVERSEER_ALERT + continues; host-restart no-re-fire of `alert_emitted=true` (TASK-5-3 + slice-4 TASK-4-3 invariant); **R-1 / BC-1 cache breakpoint placement** — dynamic per-event content lands in the *suffix* after the cache breakpoint, asserted via the rendered prompt structure; `--memory-file`/`--event-json` flag regression guard (TASK-5-1 — behaviour with neither flag identical to today).
        acceptance: |-
          All listed cases have a unit test; `make test` passes; coverage on the wrapper template generator ≥ 90% lines; BC-2 shell-metachar test passes; BC-3 partial-failure test passes; host-restart no-re-fire test passes; cache-breakpoint placement assertion passes.
        role: tester
        files:
          - tests/orchestrator/test_consensus_wrapper_event_pump.py
      - id: TASK-5-6
        description: |-
          Drop the STAY-ALIVE loop instruction from `_build_brc_preamble` at `orchestrator/routes/pipelines.py:12348+`. **Minimal preamble nudge** for the new wrapper to work: the agent must exit after handling one event. Leave the rest of the preamble intact — FULL collapse is slice-8's job. The minimal nudge is a one-line edit to the producer / reviewer / dual-role STAY-ALIVE step text: "Exit cleanly after handling the one event; the wrapper will re-invoke you on the next event."
        acceptance: |-
          `_build_brc_preamble` output contains the one-line "exit cleanly" instruction in the STAY-ALIVE step; rest of the preamble (cursor-threading examples, wait-loop semantics) is unchanged; snapshot test catches any unintended drift.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
    dependencies:
      - slice-4
  - id: 6
    name: |-
      Heartbeat migration + legacy deletion sweep + test/docs grep
    goal: |-
      The deletion half of the swap (reviewer_plan §11 subdivision
      of original slice-5 → 5b). Co-locates the heartbeat ownership
      migration with the legacy code removal so the tree stays
      green:
      (a) Migrate heartbeat ownership: wrapper invokes `egg-orch
      message heartbeat` at the 60s cadence (preserves the
      _WAIT_LOOP_HEARTBEAT_INTERVAL_SECS=60 invariant at
      sandbox/egg_agent_tools/handlers/message.py:47). Delete the
      agent-side daemon-thread auto-start in
      _start_wait_loop_heartbeat (:234-264) and the
      _default_emit_wait_loop_heartbeat helper (:175-231).
      (b) Delete per cq-4: MAX_CONSENSUS_RESTARTS at
      consensus_wrapper.py:38, _RECOVERY_SYSTEM_PROMPT at :64-99,
      _RECOVERY_USER_PROMPT at :102-105, check_confirmed_and_wait
      and its SSE consumer at :397-548 (the curl SSE machinery
      :405-501 specifically), the restart loop at :555-695, the
      recovery-prompt re-templating at :614-633, the terminal
      exit-1 at :697-712.
      (c) Delete sandbox/egg_agent_tools/handlers/message.py
      message_wait_loop body at :267-432 (the wrapper-side wait is
      the only wait now). The lower-level message_wait at :81-172
      STAYS — it backs the wait CLI the wrapper invokes.
      (d) Rewire the 3-restart trigger arm in
      orchestrator/routes/pipelines.py:18100-18159 so the
      `_emit_producer_death_alert` call site is gated on
      safety-budget exhaustion rather than restart-count
      exhaustion. _emit_producer_death_alert function itself at
      :15310-15390 STAYS — just the trigger condition changes.
      (e) Test pivot across the 7 named files:
      orchestrator/tests/test_consensus_wrapper.py,
      test_consensus_polling.py, test_consensus_race_on_exit.py,
      test_consensus_timeout_recheck.py, test_brc_nack_iteration.py,
      test_producer_death_alert.py, test_agent_exits_recorded.py.
      Each gets restart-semantics tests deleted and event-pump-
      semantics tests added (or moved into the
      test_consensus_wrapper_event_pump.py introduced in slice-5).
      (f) DOCS GREP AC (reviewer_plan non-blocking on slice-7,
      promoted here so docs co-land with code deletions): after
      deletion, `git grep` for MAX_CONSENSUS_RESTARTS,
      _RECOVERY_SYSTEM_PROMPT, _RECOVERY_USER_PROMPT,
      check_confirmed_and_wait, "STAY-ALIVE", "wait-loop" (in
      agent-authored context) across sandbox/agent-config/rules/,
      docs/architecture/orchestrator.md,
      docs/guides/concurrent-execution.md,
      docs/reference/orchestrator-cli.md,
      docs/reference/agent-wait-patterns.md must return zero
      structural hits. Specifically:
      sandbox/agent-config/rules/mission.md references at
      ~lines 137-192 must be rewritten or deleted.
    tasks:
      - id: TASK-6-1
        description: |-
          Delete the legacy restart/recovery path from `orchestrator/consensus_wrapper.py`: `MAX_CONSENSUS_RESTARTS` at line 38; `_RECOVERY_SYSTEM_PROMPT` at `:64-99`; `_RECOVERY_USER_PROMPT` at `:102-105`; the SSE machinery `check_confirmed_and_wait` whole function at `:397-548` (its curl SSE consumer block against `/api/v1/pipelines/<pid>/stream` is at `:419-501`); the restart loop at `:555-695`; the recovery-prompt re-templating block at `:614-633`; the terminal exit-1 at `:697-712`. **Per architect v4 slice-6(c)** the `message_wait_loop` handler is NOT deleted wholesale — see TASK-6-2 for the strip-only treatment. Per cq-4, no flagged fallback. The per-restart `OVERSEER_ALERT` (`:570-585`, #2806) is **re-purposed (not duplicated)** for safety-budget exhaustion via the existing `_emit_producer_death_alert` at `routes/pipelines.py:15310` — extract a shared helper and re-wire in TASK-6-3; do NOT copy-paste a new alert call.
        acceptance: |-
          `grep -n 'MAX_CONSENSUS_RESTARTS\|_RECOVERY_SYSTEM_PROMPT\|_RECOVERY_USER_PROMPT\|check_confirmed_and_wait' orchestrator/consensus_wrapper.py` returns zero hits; `_emit_producer_death_alert` still exists at `routes/pipelines.py:15310-15390` (function intact) — only its restart-exhaustion call site is rewired in TASK-6-3.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-6-2
        description: |-
          **Strip-only treatment of `message_wait_loop` per architect v4 slice-6(c)** — DO NOT delete the function body wholesale. The CLI shim `cmd_message_wait_loop` at `sandbox/egg_lib/orch_cli.py:1779` calls `_handlers.message_wait_loop(req)`; deleting the body would break the wrapper's `egg-orch message wait-loop` invocation (slice-5 TASK-5-2) at runtime. Specifically: (a) **DELETE** the heartbeat-emission block at `sandbox/egg_agent_tools/handlers/message.py:306-347` (the `pipeline_id_hb` / `role_hb` capture, `_tick` closure, `start_hb(_tick, hb_interval)` autostart); (b) **DELETE** the `stop_hb()` + final-WORKING-heartbeat block at `:421-432` inside the `finally`; (c) **DELETE** the helpers `_default_emit_wait_loop_heartbeat` at `:175-231` and `_start_wait_loop_heartbeat` at `:234-264`; (d) **DELETE** the `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60.0` constant at `:47`; (e) **KEEP** the cursor-threaded loop body at `:349-420` (the #2323 cursor-threading invariant slice-5 explicitly preserves AND the body `cmd_message_wait_loop` still calls); (f) **KEEP** the lower-level `message_wait` at `:81-172` (backs the single-shot `egg-orch message wait` verb + the wait-loop's inner iterations). Migrate heartbeat ownership to the wrapper: it invokes the existing `egg-orch message heartbeat` CLI verb (`sandbox/egg_lib/orch_cli.py:1832`) every 60s while blocked on `wait-loop`, carrying `(pipeline_id, role, slice_id, state=WAITING_FOR_EVENT)`. The 60s cadence is preserved (now in the wrapper's own timer); the gateway keep-alive (#2451), the orchestrator-side threshold at `health_monitor.py:_get_heartbeat_threshold` (line 220, 120s default / 600s implement), and the slice-aware container_id reconstruction at `routes/messages.py:860-867` are all unchanged on the orchestrator side.
        acceptance: |-
          `grep -n '_start_wait_loop_heartbeat\|_default_emit_wait_loop_heartbeat\|_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS' sandbox/egg_agent_tools/handlers/message.py` returns zero hits; `grep -n 'def message_wait_loop' sandbox/egg_agent_tools/handlers/message.py` returns ONE hit (function still exists, with stripped body); `grep -n 'def message_wait' sandbox/egg_agent_tools/handlers/message.py` returns at least two hits (`message_wait` at `:81-172` AND `message_wait_loop` both stay, with cursor-threaded loop body at `:349-420` of the latter intact); `cmd_message_wait_loop` at `orch_cli.py:1779` still calls `_handlers.message_wait_loop(req)` and the invocation succeeds in the slice-5 TASK-5-5 unit tests; wrapper emits heartbeats at 60s intervals during a long blocking wait (unit test mocks orchestrator and counts heartbeat signals over a simulated 180s wait); heartbeat invocation uses the existing `egg-orch message heartbeat` CLI verb (no new primitive introduced).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
          - sandbox/egg_agent_tools/handlers/message.py
      - id: TASK-6-3
        description: |-
          Rewire the 3-restart trigger arm at `orchestrator/routes/pipelines.py:18100-18159` so the `_emit_producer_death_alert` (function at `:15310-15390`, STAYS intact) call site is gated on safety-budget exhaustion (slice-4 `pipeline.no_progress_budget.remaining_seconds <= 0` and `alert_emitted == false`) rather than restart-count exhaustion. The function body of `_emit_producer_death_alert` is unchanged — only its trigger condition flips.
        acceptance: |-
          `grep -n 'MAX_CONSENSUS_RESTARTS\|restart_count_exhausted\|restart_number >=' orchestrator/routes/pipelines.py` returns zero hits in the trigger block; `_emit_producer_death_alert` at `:15310-15390` is byte-identical to pre-change; a unit test asserts the alert fires on `remaining_seconds <= 0 && !alert_emitted` and does NOT fire on (legacy) restart count.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-6-4
        description: |-
          Docs sweep per architect slice-6(f) + risk_analyst R-7 mitigation: update `docs/architecture/orchestrator.md`, `docs/guides/concurrent-execution.md`, `docs/reference/orchestrator-cli.md`, `docs/reference/agent-wait-patterns.md`, `sandbox/agent-config/rules/mission.md` (specifically the BRC-related references at ~lines 137-192) to remove references to deleted symbols (`MAX_CONSENSUS_RESTARTS`, `_RECOVERY_SYSTEM_PROMPT`, `_RECOVERY_USER_PROMPT`, `check_confirmed_and_wait`, "STAY-ALIVE", "wait-loop" in agent-authored context). Replace with a brief description of the new event-pump model; the FULL prompt-collapse rewrite is slice-8 and the FULL architecture doc revision is slice-9. **Co-lands with the code deletions** so docs and code stay in sync across the same PR.
        acceptance: |-
          `git grep 'MAX_CONSENSUS_RESTARTS\|_RECOVERY_SYSTEM_PROMPT\|_RECOVERY_USER_PROMPT\|check_confirmed_and_wait' -- docs/ sandbox/agent-config/rules/` returns zero hits; `git grep 'STAY-ALIVE\|wait-loop' -- sandbox/agent-config/rules/mission.md` returns zero hits in BRC sections (~lines 137-192); `make test` passes.
        role: documenter
        files:
          - docs/architecture/orchestrator.md
          - docs/guides/concurrent-execution.md
          - docs/reference/orchestrator-cli.md
          - docs/reference/agent-wait-patterns.md
          - sandbox/agent-config/rules/mission.md
      - id: TASK-6-5
        description: |-
          Test pivot across the 7 named files (architect slice-6(e)): delete the obsolete restart-path tests in the same commit as TASK-6-1's production deletions per risk_analyst R-7 (`tests/orchestrator/test_consensus_polling.py`, `test_consensus_race_on_exit.py`, `test_consensus_timeout_recheck.py`, `test_brc_nack_iteration.py`, `test_agent_exits_recorded.py`, the restart-arm assertions in `test_producer_death_alert.py` — that test's other assertions on `_emit_producer_death_alert` itself stay because the function stays; `tests/orchestrator/test_consensus_wrapper.py` is renamed to `test_consensus_wrapper_event_pump.py` in slice-5 TASK-5-5 or its restart-path content is deleted here). ALSO add a heartbeat-cadence unit test to `tests/orchestrator/test_consensus_wrapper_event_pump.py` (or a new sibling) that mocks the orchestrator and asserts the wrapper invokes `egg-orch message heartbeat` at 60s cadence during a long blocking wait (#2036/#2451 invariant preserved). Add fix-lineage assertions: cursor threading #2323 (preserved by the CLI's `/tmp/egg-wait-cursor-*` persistence — unit-tested by spying on the `wait-loop` invocation), open-NACK barrier #2142 (surfaced through next-action — exercised against the slice-3 endpoint), pre-confirm-wait #2064/#2482 (orchestrator-enforced — exercised via the route), gap-race #1995 (CLI-enforced — unit-tested at the wait-loop invocation level).
        acceptance: |-
          All restart-path tests deleted in this commit (verified via `git status` showing deletions); event-pump heartbeat-cadence test passes; fix-lineage assertions pass; `make test` passes.
        role: tester
        files:
          - tests/orchestrator/test_consensus_wrapper_event_pump.py
    dependencies:
      - slice-5
  - id: 7
    name: |-
      brc-memory artifact + delta-scoped re-analysis plumbing
    goal: |-
      Add the per-role memory file at
      .egg-state/agent-outputs/<role>/brc-memory.md (writable by
      every role today per verified shared/egg_restrictions/patterns.py
      registry). Action-scaffolded write: brc_ack and brc_nack
      handlers (sandbox/egg_agent_tools/handlers/brc.py) append a
      structured entry on each reviewer action; brc_propose and
      brc_confirm append on each producer action. Read on agent boot
      via the new --memory-file flag from slice-5. Plumb the
      orchestrator's existing changed_artifacts/version delta
      (peer_consensus.py:898-947 handle_re_propose) into the
      per-event invocation as a `--changed-artifacts` payload so the
      agent evaluates the delta rather than re-reading everything
      from scratch (decision d-5). Memory file remains EPHEMERAL per
      operator Q2 — cleaned with the pod; recovery backstop is
      `reconstruct_tracker_from_messages` at
      orchestrator/peer_consensus.py:1955+.
    tasks:
      - id: TASK-7-1
        description: |-
          Add `_append_brc_memory_entry(role, producer_role, version, verdict, reason, files_reviewed)` helper in `sandbox/egg_agent_tools/handlers/brc.py` (or new `brc_memory.py` if helpers grow). Reads existing memory file at `.egg-state/agent-outputs/<role>/brc-memory.md` (creates from template if missing — sections `## Codebase / change model`, `## Per-producer assessment`, `## Decision log`), appends a decision-log entry, and **compacts** the per-producer assessment section by collapsing prior entries for the same producer+role pair into a single distilled summary (rewrite-and-distill committed at plan time per refine analysis lines 360-367). **R-4 concurrency safety**: writes use atomic-rename (write `.tmp` + `os.replace`) — crash-safe and concurrency-safe; regression test fires 20 concurrent appends and asserts file integrity. Errors writing memory MUST be non-fatal (log WARNING + continue) — memory is ephemeral coordination state and a write failure must NOT block consensus. Export `BRC_MEMORY_PATH_TEMPLATE = ".egg-state/agent-outputs/{role}/brc-memory.md"`.
        acceptance: |-
          Helper exists with named signature; calling it twice → deterministic file; path resolves under `.egg-state/agent-outputs/<role>/brc-memory.md`; atomic-rename verified (no partial-write window in a kill-9 test); idempotency test passes (replaying same entry → no duplicate decision-log line); per-producer assessment section compacts on each call.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-7-2
        description: |-
          Wire `_append_brc_memory_entry` into `brc_ack` and `brc_nack` (reviewer handlers in `sandbox/egg_agent_tools/handlers/brc.py`) AND into `brc_propose` and `brc_confirm` (producer handlers) per architect slice-7 goal. All four handlers already receive `req: dict[str, Any]` with the relevant fields (`reason`, `files_reviewed`, `producer_role`, `version` for reviewer side; `summary`, `artifacts`, `version` for producer side); read via `req.get(...)` and append on success path only. **Dict-arg path (in-process MCP, NOT argv) — zero #2741 shell-metachar exposure**; call out in the docstring why this is the structural reason BC-2 does NOT apply to memory writes. Memory append fires AFTER orchestrator returns success — rejected propose/ack/nack/confirm does not pollute memory.
        acceptance: |-
          All four handlers call the memory helper on the success path only; a unit test confirms a write-permission error on the memory file does NOT cause the handler to raise; consensus action still succeeds; handler reads inputs from existing `req: dict`; docstring notes the dict-arg path eliminates shell-metachar exposure.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-7-3
        description: |-
          Plumb the memory-file path through the per-event wrapper invocation. Slice-5 TASK-5-1 added `--memory-file PATH` to `python3 -m egg_agent`; this task ensures the wrapper template (TASK-5-2) resolves the per-role path `BRC_MEMORY_PATH_TEMPLATE.format(role=EGG_AGENT_ROLE)` and passes it on every event spawn. The agent reads the file on boot via the new flag; the brc handlers write to it on each action (TASK-7-2). No new code beyond gluing the wrapper to the template.
        acceptance: |-
          Wrapper template invokes `python3 -m egg_agent --memory-file .egg-state/agent-outputs/$EGG_AGENT_ROLE/brc-memory.md ...` on every event spawn; unit test asserts the resolved path matches the per-role template; legacy invocations without `--memory-file` (callers other than the new wrapper) still work (TASK-5-1 legacy regression guard).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-7-4
        description: |-
          Plumb the existing orchestrator-side `changed_artifacts` / version delta (from `peer_consensus.py:898-947` `handle_re_propose`) into the per-event invocation as a `--changed-artifacts` payload on `python3 -m egg_agent`. The slice-3 next-action endpoint (TASK-3-1) already encodes `target_producer`, `version`; this task adds a `changed_artifacts: list[str]` field to that payload (paths only, never inlined contents per refine analysis lines 168-177 — agent fetches its own diffs via git tools). New wrapper template (TASK-5-2) passes the field into `--event-json`. Update the per-event prompt template (in `_build_brc_preamble` or a per-event sub-renderer) to instruct: "Evaluate ONLY the named changed_artifacts against your memory file at <path>. Do not re-read the codebase, earlier commits, or the analysis draft."
        acceptance: |-
          Next-action endpoint payload (TASK-3-1) returns `changed_artifacts: list[str]` on re-review actions; wrapper template forwards via `--event-json`; per-event prompt template renders the instruction; unit test asserts no field longer than N bytes (sanity check that file contents are not inlined); docstring cites `docs/guides/agent-mode-design.md`'s "baking in large diffs" anti-pattern.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/consensus_wrapper.py
      - id: TASK-7-5
        description: |-
          Unit tests at `tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py` covering: `_append_brc_memory_entry` creates file from template when missing; appending preserves stable headings; idempotency (same entry twice → no duplicate); partial-file recovery (truncated file at section boundary → next append rebuilds it); distilled rewrite-and-distill (replay multiple ACKs from same producer → single distilled entry); **R-4 atomic-rename concurrency** — 20 concurrent appends → file integrity (parse the resulting markdown; every append's entry present; no truncated lines); `brc_ack` / `brc_nack` / `brc_propose` / `brc_confirm` dict-arg side-effect (handler call → memory entry); write-permission error path (mock failure → handler still succeeds, warning logged); memory-file-path resolves to `BRC_MEMORY_PATH_TEMPLATE.format(role=role)`.
        acceptance: |-
          All eight listed cases have a unit test; `make test` passes; coverage on `_append_brc_memory_entry` ≥ 90% lines.
        role: tester
        files:
          - tests/sandbox/egg_agent_tools/handlers/test_brc_memory.py
    dependencies:
      - slice-6
  - id: 8
    name: |-
      Prompt collapse (lean event-handler contract)
    goal: |-
      Replace STAY-ALIVE / wait-loop mechanics / cursor-threading /
      pre-confirm-wait foot-gun guidance in _build_brc_preamble
      (orchestrator/routes/pipelines.py:12348-12700-ish) and the
      per-role prompt fragments with a lean event-handler contract
      that describes the per-event invocation model. The agent's
      contract becomes: read memory, act on the one event, update
      memory, exit cleanly. SYSTEM_PROMPT_NUDGE in
      sandbox/egg_agent_tools/server.py:33-61 STAYS UNCHANGED
      because cq-1 forbids MCP-surface changes in this issue. No
      production code beyond the prompt templates and the
      MISSION-style preamble; tests verify the rendered prompt fits
      the new model on at least one producer role + one reviewer
      role + one dual-role agent.
    tasks:
      - id: TASK-8-1
        description: |-
          Collapse `_build_brc_preamble` at `orchestrator/routes/pipelines.py:12348` (callers at `:13659, :13692, :13720`). **HARD AC — prior-fix preservation audit table** in the function docstring: #2323 cursor-threading → CLI-enforced via `egg-orch message wait-loop` (safe to drop prompt text); #2064/#2482 pre-confirm-wait foot-gun → orchestrator-enforced (safe); #1995 gap-race → CLI-enforced server-side cursor (safe); #2036 reviewer heartbeats → CLI-enforced via TASK-6-2 (safe); #2451 gateway keep-alive → CLI-enforced (safe); #2142 open-NACK aggregation barrier → orchestrator-enforced via TASK-3-1 next-action (safe); #2725 producer-allowlist scope → gateway-enforced (safe); #2749 dual-role producer-first ordering → orchestrator-enforced via TASK-3-1 per R-6 (safe). Remove STAY-ALIVE step text, wait-loop semantics block, cursor-threading examples, pre-confirm-wait foot-gun warning. Keep: file-write boundary callout (per-role), prose-via-stdin/file rule for `consensus propose/ack/nack` (#2741), role-specific BRC lifecycle summary. New preamble: lean event-handler contract — "here is the one event; handle it; exit cleanly; the wrapper handles the rest." **R-1 / BC-1 gate**: dynamic per-event content (memory snapshot, NACK reasons, version delta) MUST land in the cache *suffix* (post-breakpoint) — assert in TASK-8-3.
        acceptance: |-
          `grep -n 'STAY ALIVE\|stay-alive\|wait-loop\|cursor.*threading\|pre-confirm-wait' orchestrator/routes/pipelines.py` returns zero hits in the `_build_brc_preamble` block; the function docstring contains the prior-fix audit table with each fix classified; `grep -n 'file-write boundary\|stdin\|--reason-file'` still finds the retained invariants; snapshot test catches drift.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-8-2
        description: |-
          Update `sandbox/agent-config/rules/mission.md` (and any other BRC-related rule file) to match the lean event-handler contract from TASK-8-1. Remove instructions like "after PROPOSE, run egg-orch message wait-loop --for CONSENSUS_ACK ..." and replace with "the wrapper invokes you per event; exit cleanly after handling it." Explicitly preserve the prose-via-stdin/file rule and the file-write boundary callouts. **Note**: the docs-grep zero-hit AC for `sandbox/agent-config/rules/mission.md` already landed in slice-6 TASK-6-4 (architect slice-6(f)); this task carries the lean event-handler *rewrite content* into the same file. The grep AC in TASK-6-4 ensures the deleted-symbol references are gone; this task adds the replacement content.
        acceptance: |-
          `mission.md` BRC sections (~lines 137-192 in pre-deletion file) describe the lean event-handler contract; `grep -rn 'wait-loop\|wait_loop\|STAY ALIVE\|stay-alive' sandbox/agent-config/rules/` returns zero hits in BRC sections (re-confirmed; TASK-6-4 already enforced this); the rule files still describe the file-write boundary and the prose-via-stdin/file rule.
        role: documenter
        files:
          - sandbox/agent-config/rules/mission.md
      - id: TASK-8-3
        description: |-
          Unit tests at `tests/orchestrator/test_brc_preamble.py` covering: lean preamble snapshot (one producer role + one reviewer role + one dual-role agent per architect goal); zero hits for STAY-ALIVE / wait-loop / cursor-threading / pre-confirm-wait text; prior-fix audit table assertion (the docstring contains an entry for each named fix); retained invariants (file-write boundary, stdin/file rule); **R-1 / BC-1 cache-breakpoint placement assertion** — the rendered prompt structure places dynamic content (memory snapshot, NACK reasons, version delta) AFTER the cache breakpoint and the static prefix (mission.md + BRC preamble + tool schemas) BEFORE it. Cite the slice-1 measurement to justify the breakpoint position.
        acceptance: |-
          All listed cases have a unit test; `make test` passes; the snapshot test catches any unintended preamble drift; the cache-breakpoint assertion is satisfied with explicit reference to slice-1's per-event cost numbers.
        role: tester
        files:
          - tests/orchestrator/test_brc_preamble.py
    dependencies:
      - slice-7
  - id: 9
    name: |-
      Integration validation on #2906 repro path + docs revision
    goal: |-
      End-to-end run of the new event-pump on the #2906 Qwen-route
      repro (issue-2270 reproducer). Verify zero `Agent exited
      without BRC consensus` restart-churn alerts, memory file
      populated + consulted, per-role permissions enforced unchanged,
      cache_read_input_tokens instrumented across consecutive
      per-event invocations and an injected long idle. Capture final
      per-event cost numbers vs the WS0 baseline (slice-1). Update
      docs/reference/agent-wait-patterns.md (the wait-loop
      reference) and docs/guides/concurrent-execution.md (the
      concurrent-BRC guide) to describe the new control flow. No
      production code changes — measurement + docs only.
    tasks:
      - id: TASK-9-1
        description: |-
          Integration test at `integration_tests/test_event_pump_qwen_repro.py` running the #2906 reproducer (qwen3.7-max on a representative pipeline) end-to-end under the new event-pump. Assertions: (i) no "Agent exited without BRC consensus" message; (ii) the pipeline reaches `consensus_reached` without the wrapper restarting the agent; (iii) `.egg-state/agent-outputs/<role>/brc-memory.md` exists and contains at least one decision-log entry; (iv) gateway push from each role is accepted (role permissions still enforced — `phase_filter.validate_agent_push` invariant); (v) cache_read_input_tokens instrumented across consecutive per-event invocations and across an injected long idle (final per-event cost numbers captured for the slice-1-vs-slice-9 comparison). **Trust-boundary correction per the reviewer_plan v1 NACK + architect slice-9 goal**: place this test directly under `integration_tests/` (sibling of `test_k8s_deployment_tools.py`, `test_sandbox_mcp_tools_e2e.py`) and consume the `egg_stack` fixture (`integration_tests/conftest.py:339`, kubectl-gated → `pytest.skip` when unavailable). Read `egg_stack.gateway_url` (attribute at `:78`) and `egg_stack.orchestrator_url` (attribute at `:79`) or use the standalone `orchestrator_url` fixture at `:357`. The previously-referenced `integration_tests/local_pipeline/` subdir was deleted in commit f7803637d1 (May 11, 2026); `local_pipeline_stack` / standalone `gateway_url` fixtures no longer exist. **R-2 mitigation**: re-use the log-source adapter from slice-1 TASK-1-3 so cost-callback instrumentation works the same in cluster and in CI/local.
        acceptance: |-
          Test runs under `make test-all` (or the integration-test selector that picks up `integration_tests/test_*.py`) and passes when kubectl is available (skips otherwise per `egg_stack`'s gate); test file located directly under `integration_tests/`, NOT under a non-existent `local_pipeline/` subdir; `kubectl logs` against the wrapper pod shows no "Agent exited without BRC consensus" line; per-event cost numbers captured in test output.
        role: tester
        files:
          - integration_tests/test_event_pump_qwen_repro.py
      - id: TASK-9-2
        description: |-
          Update docs: (a) `docs/reference/agent-wait-patterns.md` — mark prior anti-patterns (STAY-ALIVE loop, agent-held wait-loop) as historical; the five previously-enumerated anti-patterns become an "old model" appendix; (b) `docs/guides/concurrent-execution.md` — describe the new event-pump control flow (deterministic Bash loop, per-event `python3 -m egg_agent` spawn, durable safety budget, HITL park decision); (c) new `docs/architecture/brc-event-pump.md` — full architecture doc covering event-pump topology, no-progress safety-budget defaults (sized against slice-1 measurements), durable contract-side persistence model, cutover playbook (drain in-flight pipelines before deploying slice-6's PR; no flagged fallback per cq-4), BC-3 partial-failure behavior, BC-2 prose-via-shlex.quote-or-stdin rule; (d) cross-link from `docs/architecture/orchestrator.md` to the new `brc-event-pump.md`.
        acceptance: |-
          All four doc updates land; `grep -n 'STAY ALIVE\|MAX_CONSENSUS_RESTARTS\|_RECOVERY_SYSTEM_PROMPT\|check_confirmed_and_wait' docs/` returns zero hits except in the explicit "old model" appendix; new `brc-event-pump.md` is cross-linked from `orchestrator.md`; cutover playbook documented; BC-3 + BC-2 behaviors described.
        role: documenter
        files:
          - docs/reference/agent-wait-patterns.md
          - docs/guides/concurrent-execution.md
          - docs/architecture/brc-event-pump.md
          - docs/architecture/orchestrator.md
      - id: TASK-9-3
        description: |-
          Final spike-vs-baseline cost comparison: write a short comparison note appended to `.egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md` (slice-1) reporting slice-9 integration test's per-event cost numbers vs slice-1 baseline. If the production event-pump regresses by >20% on wall-clock vs the current persistent-session baseline (per risk_analyst R-5), surface as an OVERSEER_ALERT and propose a follow-up issue exploring a warm-Python-runner fallback. Otherwise, declare slice-9 measurement satisfies the per-event cost gate.
        acceptance: |-
          Comparison note exists; slice-1 + slice-9 per-event cost numbers tabulated; wall-clock comparison explicit; if regression > 20%, an OVERSEER_ALERT is registered (verifiable via `egg-orch message list --type OVERSEER_ALERT`); follow-up issue link if needed.
        role: documenter
        files:
          - .egg-state/agent-outputs/issue-2908-replan-ws0-spike-report.md
    dependencies:
      - slice-8
```
