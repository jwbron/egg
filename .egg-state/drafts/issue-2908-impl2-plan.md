# Plan (replan2): BRC consensus event-pump + durable agent memory (#2908)

Decomposition of the architect's **6-slice** linear chain (v2)
(`.egg-state/agent-outputs/issue-2908-replan2-architect-slices.yaml`)
into discrete coder / tester / documenter tasks.

The architecture analysis at
`.egg-state/agent-outputs/issue-2908-replan2-architect-output.json` is
the binding scope description; this plan only adds task-level
enumeration under the slices the architect emitted. Slice IDs,
names, goals, and the linear DAG are copied verbatim.

## Approach summary

Reframe BRC consensus from an in-agent `wait_loop` (which any model
can fall out of by emitting a final assistant message) into a
**wrapper-driven deterministic event pump** that invokes the agent
one-shot per actionable event. Continuity rides on a **durable
per-role memory artifact** (`.egg-state/agent-outputs/<role>/brc-memory.md`),
not a live session. Slicing follows the architect's natural-seam
analysis (`slice_composition_rationale.natural_seams_per_slice`):

```
slice-1 (foundations: CLI + memory data plane)
   ↓
slice-2 (event-pump wrapper, behind flag)
   ↓
slice-3 (delta + prompt collapse)
   ↓
slice-4 (spike + flag flip + delete old path)
   ↓
slice-5 (additive: stdin/file prose plumbing + 2 new BRC subcommands)
   ↓
slice-6 (deletion: MCP→CLI tool collapse)
```

Each slice is a single tight BRC cycle. The chain matches the issue's
stated rollout: **build behind a flag, validate, flip default, delete
old path**. The split of the original slice-5 into 5 (additive prose
plumbing + new subcommands) and 6 (MCP deletion + test migration)
follows the architect rubric "avoid bundling deletion-heavy work with
new-API-introduction work" and risk_analyst R4's ordering
("stdin/file prose plumbing lands BEFORE any MCP tool deprecation").

## Primitives audit (#2594)

Every primitive the tasks below depend on, with file:line evidence.
All existence-citations were independently verified by an Explore
subagent at the HEAD of `egg/issue-2908-replan2/work` (commit
`b6088e988`); the v2 architect commit (`6342b2d7a`) and risk_analyst
commit (`58370d704`) did not change any of these citations.

| Primitive | Citation | Execution context |
|---|---|---|
| `_CONSENSUS_WRAPPER_TEMPLATE` | `orchestrator/consensus_wrapper.py:116` | orchestrator pod composes; in-sandbox-agent runs |
| `MAX_CONSENSUS_RESTARTS = 3` | `orchestrator/consensus_wrapper.py:38` | in-sandbox-agent (interpolated into bash) |
| `_RECOVERY_SYSTEM_PROMPT` | `orchestrator/consensus_wrapper.py:64-99` | orchestrator pod composes; agent consumes |
| `is_buffer_overflow` / `is_transient_crash` / `is_startup_failure` | `orchestrator/consensus_wrapper.py:205,299,313` | in-sandbox-agent |
| SSE `consensus.reached` curl path | `orchestrator/consensus_wrapper.py:418-449` | in-sandbox-agent (curl on agent pod) |
| `build_consensus_wrapped_command` | `orchestrator/consensus_wrapper.py:716` (call site `orchestrator/concurrent_executor.py:489`; import at :37) | orchestrator pod |
| `message_wait_loop` handler entry | `sandbox/egg_agent_tools/handlers/message.py:267` | in-sandbox-agent |
| `message_wait_loop` early-exit (`if resp.get("matched"):`) | `sandbox/egg_agent_tools/handlers/message.py:405` (return at :410) | in-sandbox-agent |
| `brc_ack` handler | `sandbox/egg_agent_tools/handlers/brc.py:505` | in-sandbox-agent |
| `brc_nack` handler | `sandbox/egg_agent_tools/handlers/brc.py:586` | in-sandbox-agent |
| `_build_brc_preamble` | `orchestrator/routes/pipelines.py:12348` (callers `:13659, :13692, :13720`) | orchestrator pod composes |
| `mission.md` STAY-ALIVE / wait-loop bullets | `sandbox/agent-config/rules/mission.md` lines 151–154 | in-sandbox-agent (loaded at startup) |
| `SYSTEM_PROMPT_NUDGE` | `sandbox/egg_agent_tools/server.py:61` | in-sandbox-agent |
| MCP registration block | `shared/egg_agent/client.py:299–353` (env gate `:311`, `build_sandbox_mcp_server` `:316`, options.mcp_servers `:323`) | in-sandbox-agent |
| `EGG_MCP_TOOLS` env-flag reader | `shared/egg_agent/client.py:311` (default TRUE; only `false`/`0`/`no`/`off` opts out) | in-sandbox-agent |
| `egg-orch message wait-loop` (`cmd_message_wait_loop`) | `sandbox/egg_lib/orch_cli.py:1695` | in-sandbox-agent (CLI) |
| `consensus status --json` (`cmd_consensus_status`) | `sandbox/egg_lib/orch_cli.py:2783` | in-sandbox-agent (CLI) |
| `cmd_consensus_propose` (argv `--summary`; accepts `--file PATH` for JSON payload; argv `--reason` per parser at `:3265`) | `sandbox/egg_lib/orch_cli.py:2528,2552,3265` | in-sandbox-agent (CLI) |
| `cmd_consensus_ack` (argv `--reason` parser at `:3485`/`:3523-3527`) | `sandbox/egg_lib/orch_cli.py:2633,3485,3523` | in-sandbox-agent (CLI) |
| `cmd_consensus_nack` (argv `--reason` parser at `:3573`) | `sandbox/egg_lib/orch_cli.py:2692,3573` | in-sandbox-agent (CLI) |
| `cmd_consensus_confirmed` (no `--reason` — wrapper calls this to mark consensus, NOT `progress complete`) | `sandbox/egg_lib/orch_cli.py:2753` | in-sandbox-agent (CLI) |
| `cmd_consensus_withdraw` (argv `--reason` parser at `:3600`) | `sandbox/egg_lib/orch_cli.py:2726,3600` | in-sandbox-agent (CLI) |
| Cursor on-disk path | `sandbox/egg_lib/orch_cli.py:1426` | in-sandbox-agent |
| `EGG_ORCHESTRATOR_URL` default | `sandbox/egg_lib/orch_cli.py:132` | in-sandbox-agent |
| `GATEWAY_URL` default | `sandbox/egg_lib/orch_cli.py:144` | in-sandbox-agent |
| `lifecycle_secret` arg (`EGG_LIFECYCLE_SECRET`) | `sandbox/egg_lib/orch_cli.py:324` | in-sandbox-agent |
| `EGG_AGENT_ROLE` / `EGG_PIPELINE_ID` env on pods | `orchestrator/kubernetes_spawner.py:818-823` | in-sandbox-agent (set on pod) |
| `EGG_SLICE_ID` env on pods (when slice-mode) | `orchestrator/kubernetes_spawner.py` (same env block) | in-sandbox-agent |
| `JOB_NAME_FORMAT` | `orchestrator/kubernetes_spawner.py:332` | orchestrator pod (job spec) |
| `python3 -m egg_agent` entrypoint + `--max-turns` arg | `shared/egg_agent/__main__.py:36`; `command.py:11` (`build_agent_command`) | in-sandbox-agent |
| `check_file_write_permission` | `shared/egg_agent/tool_interceptor.py:27` | in-sandbox-agent |
| `phase_filter.check_agent_restrictions` | `gateway/phase_filter.py:1058` | gateway pod |
| `validate_agent_push` | `shared/egg_restrictions/checker.py:98` | gateway pod |
| Role allowlists for `.egg-state/agent-outputs/` (prefix-matched; subdirs allowed) | `shared/egg_restrictions/patterns.py:362,382,436,516` plus coder `:231`, tester `:277`, documenter `:307` | gateway pod + in-sandbox-agent |
| `peer_consensus.py` aggregated-NACK payload `nacks[]` (NB: field name is `nacks`, not `aggregated_nacks`) | `orchestrator/peer_consensus.py:949-1024` (`_open_nacks_barrier_response`) | orchestrator pod |
| `changed_artifacts` ACK-invalidation hook | `orchestrator/peer_consensus.py:902-926` (`matrix.invalidate_overlapping_acks`) | orchestrator pod |
| `reconstruct_tracker_from_messages(..., slice_id=...)` | `orchestrator/peer_consensus.py:1955` (slice_id param at :1960) | orchestrator pod |
| `brc-history` writer path (no slice_id in main filename; sibling `<id>-implement-unattributed.json` is read-side only) | `sandbox/egg_agent_tools/handlers/brc.py:815,1019` | in-sandbox-agent / orchestrator |
| `_persist_atomic_template` helper (tempfile + os.replace) — candidate for promotion to `shared/` so slice-1 memory writer can call it | `shared/egg_overseer/state.py:266` (alt: `shared/egg_contracts/usage_loader.py:95` `_atomic_write`) | trusted-CI / orchestrator + in-sandbox-agent |
| Qwen cost_callback file (cache instrumentation source) | `config/litellm/cost_callback.py:188` | trusted-CI / litellm pod |
| MCP tool files to delete (1,515 LOC across 7 tool namespaces; the 4 infra files plus `server.py` go with them in slice-6) | `sandbox/egg_agent_tools/tools/{brc,checkpoint,message,phase,progress,sdlc,task}.py` (~1,515 LOC) plus `sandbox/egg_agent_tools/{__init__,_common,_registry,_tool_compat}.py` infra | in-sandbox-agent |
| `tests/tools/test_mcp_cli_drift.py` (retire in slice-6) | `tests/tools/test_mcp_cli_drift.py` (12,905 B) | trusted-CI |
| `integration_tests/test_sandbox_mcp_tools_e2e.py` (migrate in slice-6; preserve SDK-spawn exercise) | `integration_tests/test_sandbox_mcp_tools_e2e.py` (5,252 B) | trusted-CI |
| `tests/sandbox/egg_agent_tools/test_server.py` (migrate in slice-6) | `tests/sandbox/egg_agent_tools/test_server.py` (8,164 B) | trusted-CI |
| `tests/sandbox/egg_agent_tools/test_handlers_brc.py` (extend in slice-1) | `tests/sandbox/egg_agent_tools/test_handlers_brc.py` | trusted-CI |
| `orchestrator/tests/test_consensus_wrapper.py` (extend in slice-2/4) | `orchestrator/tests/test_consensus_wrapper.py` (+ `test_consensus_wrapper_anchor.py`, `test_brc_nack_iteration.py`) | trusted-CI |
| `integration_tests/regression/test_brc_concurrency.py` (in-process; cannot drive deployed wrapper end-to-end per slice-2 verification revision) | `integration_tests/regression/test_brc_concurrency.py:1-25` | trusted-CI |
| `docs/architecture/REVIEWER-SYNC.md` (the doc the full-git-log-delta requirement traces back to) | `docs/architecture/REVIEWER-SYNC.md` (search via Grep — exact path verified via doc index lookup at implement time) | trusted-CI |

### New primitives (created by tasks in this plan)

The following symbols do **not** exist at HEAD; the named task creates
them, and downstream tasks order after the creating task per the
plan-reviewer §9 exception.

| Primitive | Created by | Form |
|---|---|---|
| `egg-orch brc next-action --role R [--json]` CLI subcommand | TASK-1-1 (CLI) backed by TASK-1-2 (route) | Subparser registered under existing `brc` parent (sibling of `brc ack`/`brc nack`). Returns JSON `{action: "wait"|"propose"|"ack"|"nack"|"confirm"|"complete", event_payload?: {...}}`. |
| `POST /api/v1/pipelines/{pid}/consensus/next-action` orchestrator route | TASK-1-2 | Route handler under `orchestrator/routes/` deriving next action from `consensus_status` + `nacks[]` aggregation + `changed_artifacts` delta. Returns same JSON the CLI surfaces. |
| `egg-orch brc get-state` CLI (verb-level alias for `consensus status --json`) | TASK-1-3 | Thin subcommand; sources from `brc_get_state` handler (`handlers/brc.py:679-723`). Matches the existing MCP tool name so the wrapper bash can call it directly. |
| `egg-orch brc list-blocking` CLI | TASK-1-4 | Derived view of `consensus.blocking_agents[]` — returns one role per line for shell consumption. |
| `egg-orch phase get-context` CLI | TASK-1-5 | Wraps `mcp__phase__get_context` handler. Returns JSON: pipeline_id, phase, role, assigned tasks, prior-phase artifacts. |
| `.egg-state/agent-outputs/<role>/brc-memory.md` durable memory artifact | TASK-1-6 (writer) + TASK-1-7 (schema doc) | Per-role-per-pipeline distilled memory file with sections per the v2 architect `design.memory_schema`: Codebase / change model; Per-producer assessment (incl. `last_reviewed_commit_sha`, `prior_verdict`, `prior_nack_reasons`, `prior_conditional_obligation`, `summary_of_assessment`); Decision log (capped at last 20 entries). Path uses subdirectory layout (architect od-1). |
| Promoted `atomic_write` helper (shared) | TASK-1-6 | Either promote `_persist_atomic_template` from `shared/egg_overseer/state.py:266` to a shared module, or call it from the memory writer if a cross-module import is clean. The shared helper guarantees no within-pod partial writes (v2 atomic-write contract). |
| `EGG_BRC_MEMORY={off,write-only,full}` env-flag gate | TASK-1-6 | Read in `brc_ack` / `brc_nack` handlers; default `off` until slice-4 flips on. |
| Fail-closed memory-path constructor | TASK-1-6 | Raises if `EGG_AGENT_ROLE` is unset/empty (architect od-1 + risk_analyst R14 + reviewer_plan non-blocker). Never falls through to a degenerate `.egg-state/agent-outputs//brc-memory.md` path. |
| `EGG_BRC_EVENT_PUMP={true,false}` env-flag gate | TASK-2-1 (template branch) and TASK-4-2 (flip default) | Read in `build_consensus_wrapped_command` at template-composition time; selects new event-pump bash branch vs old capped-restart branch. Default false in slice-2, flipped true in slice-4. |
| Idle/no-progress safety budget (env `EGG_BRC_IDLE_BUDGET_MIN`, default 30) | TASK-2-3 | Replacement for `MAX_CONSENSUS_RESTARTS` cap; trips overseer alert. |
| Wrapper-side heartbeat emitter (with `slice_id == os.environ['EGG_SLICE_ID']` payload assertion) | TASK-2-2 + TASK-2-6 (test) | Wrapper bash emits `egg-orch message heartbeat` (existing endpoint) every 30 s while `message wait-loop` is blocking. Migrated from `handlers/message.py:267-429`. Heartbeat payload carries the env-derived slice_id so a regression in slice_id propagation is caught directly. |
| Wrapper-side gateway-session keep-alive | TASK-2-4 | Wrapper bash refreshes the lifecycle-secret-gated session while blocking. Migrated from same handler region (#2451). |
| Per-event prompt composer (`compose_event_prompt`) | TASK-3-1 | New helper that builds the single-event prompt: role banner + event_payload + memory excerpt + full `git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p` delta per producer + NACK payload. Tail-position memory delivery (architect od-6 Option B). |
| Memory-delivery mechanism (Option B — inline at user-prompt tail) | TASK-3-2 | Wrapper bash invokes `python3 -m egg_agent "<event prompt> + <memory excerpt at tail>"`. The illustrative `--append-context` flag in the analysis pseudocode does NOT exist on `build_agent_command` (`shared/egg_agent/command.py:11-46`) — Option B sidesteps it. Option C (net-new `--memory-file` flag) is the explicit fallback. |
| Three-point cache_read measurement schedule | TASK-3-7 (baseline check) + TASK-4-1 (post-slice-3) + TASK-6-7 (post-slice-6) | At each measurement point, capture `cache_read_input_tokens` / Qwen cost_callback aggregate per pipeline. Regression > 20% at any boundary triggers HITL pause (risk_analyst R8). |
| `egg-orch brc resolve-obligation` CLI | TASK-5-2 | Wraps existing `mcp__brc__resolve_obligation` handler. Prose `--note` via stdin or `--note-file PATH`. |
| `egg-orch brc read-peer-artifact` CLI | TASK-5-3 | Wraps existing `mcp__brc__read_peer_artifact` handler. Stdout JSON; supports `--limit`, `--cursor`, `--phase`, `--peer-role`. |
| `--summary-file PATH` / `--reason-file PATH` / `--files-reviewed-file PATH` + stdin sentinel on `consensus propose / ack / nack / withdraw` | TASK-5-1 | Hard constraint from #2741: prose args must NOT flow through `bash -c` argv. Reuses the `propose --file` pattern at `orch_cli.py:2552`. Existing argv `--reason` accepted as fallback during transition (deprecation in a separate cycle). |

## Trust-boundary scope (#10)

The architect-output `runtime_primitive_assumptions` tagged each
primitive on the in-sandbox-agent vs trusted-CI axis. Highlights
relevant to this plan:

- **Wrapper bash runs in-sandbox-agent** — every CLI command the
  wrapper invokes (`brc get-state`, `brc next-action`,
  `message wait-loop`, `consensus confirmed`) inherits the role's
  file-write restrictions via `tool_interceptor.check_file_write_permission`
  (`shared/egg_agent/tool_interceptor.py:27`) and the gateway-side
  push guard (`gateway/phase_filter.py:1058`).
- **The memory file lives in `.egg-state/agent-outputs/<role>/`**,
  inside every participant role's allowlist. The subdirectory layout
  passes existing prefix-pattern matching (verified at
  `shared/egg_restrictions/patterns.py` — `match_pattern` treats
  trailing-slash directory patterns as recursive prefixes). The
  fail-closed path constructor (TASK-1-6) refuses to write if
  `EGG_AGENT_ROLE` is unset/empty.
- **No `class ScriptedProvider` exists in this codebase** (the
  pod-injection avenue was ruled out per #2474 — verified at
  `integration_tests/regression/conftest.py:45` and via
  `grep -rn 'class ScriptedProvider'` returning zero hits). Slice-2
  verification therefore uses wrapper-rendering + heartbeat unit
  tests + the existing in-process `PeerConsensusTracker` regression
  suite at `integration_tests/regression/test_brc_*.py` (the same
  suite the architect's `verification_strategy.slice_2` (iii)
  invokes); true end-to-end validation is **deferred to slice-4**
  (the spike on issue-2270 / qwen3.7-max). Slice-4 uses the session-
  scoped `egg_stack: EggStack` fixture at
  `integration_tests/conftest.py:340` (k3s-backed, started via
  `_k8s_egg_stack()` at `:172`). The actual `gateway_url` /
  `orchestrator_url` / `lifecycle_secret` accessors are attributes
  on the `EggStack` dataclass at `integration_tests/conftest.py:78-90`
  — there is no `local_pipeline/conftest.py:261` fixture and no
  `local_pipeline_stack` fixture (verified — directory does not
  exist). Agent-side pytest fixtures remain not in-sandbox-agent-
  runnable; slice-4 assertions run on the cluster orchestrator,
  not inside the agent pod under test. See
  `docs/architecture/integration-test-trust-boundary.md`.
- **Qwen cache instrumentation** sources from
  `~/.local/state/clm/cost-*.json` files on the litellm pod (per
  `config/litellm/cost_callback.py:188`); the Anthropic-route counter
  rides on `usage.cache_read_input_tokens` from the SDK result. The
  three-point measurement schedule reads from both.
- **mission.md sandbox-image rebuild**: slice-3 rewrites
  `sandbox/agent-config/rules/mission.md`. For the rewrite to reach
  the agent pod, the sandbox image must be rebuilt and the agent pod
  must restart. This MUST happen BEFORE the flag-flip in slice-4 —
  TASK-3-4 acceptance pins the rebuild-verification.

## Test strategy

Every slice carries its own unit + integration tests. Per slice
(verification revised in v2 per reviewer_plan / risk_analyst):

- **slice-1**: unit tests for next-action derivation across producer /
  reviewer / dual-role incl. open-NACK barrier (#2142), conditional
  ACK, stale-version (#2482); memory-write side-effects of
  `brc_ack` / `brc_nack` (all six required fields populated incl.
  `last_reviewed_commit_sha` per producer); atomic-write contract
  test (back-to-back writes never see a partial state); fail-closed
  path-construction test (raise when `EGG_AGENT_ROLE` unset/empty);
  CLI round-trip tests for the four new subcommands with
  lifecycle-secret auth.
- **slice-2**: (i) wrapper-rendering unit test snapshotting the bash
  emitted for `EGG_BRC_EVENT_PUMP=true` (asserts wait-filter set,
  heartbeat invocation site, idle-budget threshold from od-4);
  (ii) wrapper-side heartbeat unit test that asserts payload
  carries `slice_id == os.environ['EGG_SLICE_ID']` (risk_analyst R9);
  (iii) in-process PeerConsensusTracker regression
  (`integration_tests/regression/test_brc_*.py`) passes — establishes
  zero orchestrator-side regression; (iv) true end-to-end validation
  **deferred to slice-4** (the spike).
- **slice-3**: unit tests for `compose_event_prompt` (prompt shape +
  per-role budget); snapshot test for the collapsed
  `_build_brc_preamble`; assertion that the full
  `git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p`
  delta is in the per-event prompt (NOT just `changed_artifacts`);
  sandbox-image-rebuild verification (the new `mission.md` is in the
  agent pod before the slice-3 sandbox rebuild ships); WS7 cache
  measurement #1 (post-slice-3 baseline); per-event prompt envelope
  (excluding git-log delta) bounded ≤ 10 KB.
- **slice-4**: the spike — k3s integration test on the #2906 repro
  (issue-2270, qwen3.7-max) with `EGG_BRC_EVENT_PUMP=true` and
  `EGG_BRC_MEMORY=full`; assertions: consensus reaches CONFIRMED, no
  restart churn, brc-memory.md populated with all six required
  fields per producer entry (incl. `last_reviewed_commit_sha`
  updated per producer), per-event context bounded, `cache_read`
  instrumented on both routes (Anthropic via SDK usage, Qwen via
  cost_callback files); cost-baseline comparison vs restart-churn
  baseline from #2806. WS7 measurement #2 captured here.
- **slice-5**: stdin / `--reason-file` / `--summary-file` /
  `--files-reviewed-file` round-trip tests for prose containing
  `$VAR` / backticks / `;` / `&&` / embedded newlines (the
  #2741-regression-guard suite); unit tests for the two new
  subcommands (`brc resolve-obligation`, `brc read-peer-artifact`);
  argv `--reason` deprecation-warning test; no MCP behavior change
  asserted by regression run.
- **slice-6**: all BRC actions reachable via CLI with no agent MCP
  server registered (the `EGG_MCP_TOOLS` env flag is deleted as
  part of the MCP-registration removal — no orphan flag); migrated
  `test_sandbox_mcp_tools_e2e.py` runs the agent's first action as
  `consensus ack/nack` via stdin/file (preserves SDK-spawn exercise,
  NOT collapsed to direct-handler); per-event wall-clock latency
  unchanged within 5% margin; full regression pass; WS7 cache
  measurement #3 (post-slice-6) — regression > 20% triggers HITL
  pause per risk_analyst R8.

**Manual verification on slice-4 and slice-6** (recorded in
`manual_steps`): human inspects spike output (memory content for
reasoning fidelity; `last_reviewed_commit_sha` actually updates per
producer; cost delta vs restart-churn baseline) and consents to flag
flip; human inspects slice-6 WS7 #3 measurement and consents to
deletion if cache_read regression is within tolerance.

## Manual pre/post-merge steps

- **slice-1 → slice-4 (pre-merge)**: no manual steps; feature flags
  default off, so all changes are inert in production.
- **slice-3 pre-merge**: sandbox-image rebuild + agent pod restart
  must happen BEFORE slice-4's flag flip — TASK-3-4 acceptance pins
  the rebuild verification so the rebuild lands as part of the
  slice-3 deploy.
- **slice-4 pre-merge**: human reviews spike output (memory content,
  `last_reviewed_commit_sha` correctness, WS7 cache measurement #2,
  cost delta) before approving the default-on flip. Rollback plan
  recorded: if the spike falsifies the design, `git revert` slices
  1–3 (no production traffic touched the new path because the flag
  stayed off until slice-4 flipped it).
- **slice-4 post-merge**: monitor 24 h of production BRC traffic for
  any "Agent exited without BRC consensus" entries; if seen, flip
  `EGG_BRC_EVENT_PUMP=false` via deployment env and re-open the slice.
- **slice-5 → slice-6 pre-merge**: no manual steps for slice-5.
  Slice-6 pre-merge requires the WS7 cache measurement #3 sample —
  human inspects and consents.
- **slice-6 post-merge**: confirm no in-flight pipelines are running
  against an agent built before slice-6 (MCP tools are deleted, so
  an old wrapper that still injects them will start with no MCP
  server registered). Gate the deployment on in-flight-pipeline drain
  or cancel.

## Slice-DAG ASCII

```
slice-1 (foundations, additive)
   │
   ▼
slice-2 (event-pump wrapper, behind flag)
   │
   ▼
slice-3 (delta + prompt collapse, flag still off)
   │
   ▼
slice-4 (spike + flag flip + delete old path)
   │
   ▼
slice-5 (additive CLI prose plumbing + 2 new subcommands)
   │
   ▼
slice-6 (MCP → CLI deletion)
```

Forest constraint honoured trivially — every slice has exactly one
parent.

## yaml-tasks appendix

```yaml
# yaml-tasks
pr:
  title: "BRC: event-pump wrapper + durable agent memory (replan2 #2908)"
  description: |
    ## Context

    BRC consensus today depends on the *agent* re-entering a blocking
    `egg-orch message wait-loop` between every event. That re-entry is a
    seam the model can fall out of by emitting a final assistant
    message instead of re-entering the wait. Claude usually re-enters;
    qwen3.7-max does not (#2906) — it exits success=True after one
    match, the wrapper sees no CONSENSUS_CONFIRMED, the 3-restart cap
    trips (#2806), and the pipeline FAILs after ~$1 and ~20 min of
    churn. Prompt-only mitigations narrow the seam for one model; the
    seam itself exists for every model (lineage: #2323, #2064, #2482,
    #2036, #1995, #2451).

    ## Changes

    Reframe consensus-agent execution from a long-lived participant
    that holds blocking waits into a *deterministic wrapper-driven
    event pump* that invokes the agent one-shot per actionable event,
    with continuity carried by a durable per-role memory file. Lands
    in six linear slices behind `EGG_BRC_EVENT_PUMP` (default false
    until slice-4):

    1. **slice-1 — Foundations.** New `egg-orch brc next-action`,
       `brc get-state`, `brc list-blocking`, `phase get-context` CLI
       subcommands. Durable BRC memory artifact at
       `.egg-state/agent-outputs/<role>/brc-memory.md` with
       action-scaffolded writes into `brc_ack` / `brc_nack` handlers,
       atomic writes via promoted `_persist_atomic_template`,
       `last_reviewed_commit_sha` per producer in the schema,
       fail-closed path construction. Gated by
       `EGG_BRC_MEMORY=write-only` (writes accumulate; reads land
       in slice-3). Purely additive — zero behavior change.
    2. **slice-2 — Event-pump wrapper.** Rewrite
       `orchestrator/consensus_wrapper.py` as a deterministic event
       pump gated by `EGG_BRC_EVENT_PUMP`. Drop the 3-restart cap;
       replace with idle/no-progress safety budget. Migrate the
       heartbeat (#2036) and gateway-session keep-alive (#2451) from
       the agent-side handler into the wrapper's blocking wait.
       Heartbeat payload carries `slice_id` from `EGG_SLICE_ID`
       (regression guard). Verification is unit-test-only — no
       in-process test double can drive a deployed pod end-to-end
       (the pod-injection avenue was ruled out per #2474); true
       E2E deferred to slice-4 via the `egg_stack` real-pod
       fixture. Old path retained verbatim alongside.
    3. **slice-3 — Delta + prompt collapse.** Wire per-event
       invocation to hand the agent the memory delta plus the full
       `git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p`
       delta per producer (NOT just orchestrator-side
       `changed_artifacts` — per REVIEWER-SYNC.md the re-review must
       audit the full delta as a fresh review). Strip the
       STAY-ALIVE / wait-loop / cursor-threading guidance from
       `_build_brc_preamble` and `mission.md`; replace with a lean
       event-handler contract. Sandbox image rebuilt + agent pod
       restarted BEFORE slice-4 flag flip. WS7 cache measurement #1.
    4. **slice-4 — Spike + flag flip + delete old path.** Run the
       #2906 repro on k3s with `EGG_BRC_EVENT_PUMP=true` and
       `EGG_BRC_MEMORY=full`; confirm consensus, instrumented
       `cache_read`, populated memory (`last_reviewed_commit_sha`
       updated per producer), bounded per-event context. Flip flag
       defaults to on; delete the capped-restart bash, the
       `_RECOVERY_SYSTEM_PROMPT`, the SSE machinery, and the
       agent-side wait_loop heartbeat code. Rollback plan: revert
       slices 1–3 via `git revert` if spike falsifies.
    5. **slice-5 — Additive CLI prose plumbing + 2 new BRC
       subcommands.** Add stdin / `--reason-file` / `--summary-file`
       / `--files-reviewed-file` plumbing to
       `consensus propose --summary`, `consensus ack --reason`,
       `consensus nack --reason`, `consensus withdraw --reason`
       (#2741 regression guard). Add `egg-orch brc resolve-obligation`
       and `egg-orch brc read-peer-artifact` CLI subcommands the
       slice-6 deletion depends on. Argv kept as fallback during
       transition (deprecation warning).
    6. **slice-6 — MCP → CLI deletion.** Delete the 28 agent-facing
       MCP tools (~1,515 LOC across 7 namespace files + the 4
       infra files + `server.py`), the `SYSTEM_PROMPT_NUDGE`, and the
       MCP registration block in `shared/egg_agent/client.py:299-353`
       INCLUDING the `EGG_MCP_TOOLS` env flag at :311 (no orphan
       flag). Retire `tests/tools/test_mcp_cli_drift.py`. Migrate the
       MCP E2E test so the agent's first action is `consensus
       ack/nack` via stdin/file (preserves SDK-spawn exercise).
       Verify per-event wall-clock latency within 5%. WS7 cache
       measurement #3.

    ## Impact

    Operator-facing: the BRC consensus subsystem becomes
    model-portable — any agent that exits naturally after handling
    one event reaches CONFIRMED, instead of needing prompt nudges to
    keep re-entering an in-process wait. Cost-per-phase drops because
    restart churn disappears, replaced by short bounded per-event
    invocations that hit the prefix cache (≥ 60-min TTL on both
    routes per WS7 closure). Net code deletion: the capped-restart
    template, the SSE machinery, the recovery system prompt, the 28
    MCP tool schemas, the `EGG_MCP_TOOLS` flag, the cursor-threading
    guidance, and the agent-side heartbeat all go away. The agent
    primitive (pod / worktree / SDK / permissions / restrictions) is
    untouched.
  test_plan: |
    Per-slice automated coverage:
    - slice-1: unit tests for next-action derivation (producer /
      reviewer / dual-role / open-NACK barrier #2142 /
      conditional ACK / stale-version #2482), memory-write
      side-effects on `brc_ack`/`brc_nack` covering all six
      required fields (incl. `last_reviewed_commit_sha` per
      producer); atomic-write contract test; fail-closed path
      constructor test (raise on unset `EGG_AGENT_ROLE`); CLI
      round-trip with lifecycle-secret auth.
    - slice-2: wrapper template snapshot for event-pump branch;
      wrapper-side heartbeat unit test asserting `slice_id`
      propagation from `EGG_SLICE_ID`; idle-budget overseer-alert
      threshold test; in-process `PeerConsensusTracker` regression
      (`integration_tests/regression/test_brc_*.py`) with flag off
      establishes zero orchestrator-side regression; E2E deferred
      to slice-4 via `egg_stack` (no in-process test double can
      drive a deployed pod end-to-end per #2474).
    - slice-3: `compose_event_prompt` unit tests; collapsed
      preamble snapshot; full git-log delta in per-event prompt
      asserted; per-event prompt envelope (excluding delta) ≤ 10 KB;
      sandbox-image rebuild verification (new mission.md reachable
      in pod) BEFORE slice-4 flag flip; WS7 cache measurement #1.
    - slice-4: k3s integration test on the #2906 repro (issue-2270,
      qwen3.7-max) with flag + memory both full; assertions on
      CONFIRMED, no restart churn, populated memory with all six
      required fields per producer entry (incl. `last_reviewed_commit_sha`
      updated per producer), cache_read instrumented on both routes
      (Anthropic via SDK usage, Qwen via cost_callback files); WS7
      measurement #2 captured.
    - slice-5: stdin / `--reason-file` / `--summary-file` /
      `--files-reviewed-file` round-trip tests for prose containing
      `$VAR` / backticks / `;` / `&&` / embedded newlines (#2741
      regression guard); CLI subcommand tests for `brc
      resolve-obligation` and `brc read-peer-artifact`; argv
      `--reason` deprecation-warning test.
    - slice-6: BRC actions reachable via CLI with no agent MCP
      server registered; migrated E2E runs first action as
      `consensus ack/nack` via stdin/file (preserves SDK-spawn
      exercise); per-event wall-clock latency within 5% of
      pre-slice-6 baseline; WS7 measurement #3 vs baseline within
      20% (else HITL pause).

    Manual verification:
    - slice-3 pre-merge: sandbox image rebuilt + agent pod restarted
      so the new `mission.md` is reachable in the pod BEFORE slice-4
      flag flip.
    - slice-4 pre-flag-flip: human inspects spike output
      (brc-memory content for reasoning fidelity;
      `last_reviewed_commit_sha` updated per producer; cost-per-phase
      delta vs restart-churn baseline; WS7 measurement #2) and
      consents to default-on flip via PR review.
    - slice-6 pre-deletion: human inspects WS7 measurement #3 and
      consents if cache_read regression is within tolerance.
    - slice-6 pre-merge: human verifies no in-flight pipelines are
      mid-run against pre-slice agents; gate deploy on drain or
      cancel.
  manual_steps: |
    Pre-merge:
    - slice-3: sandbox image rebuilt + agent pod restarted BEFORE
      slice-4's flag flip (the new `mission.md` is the slice-4
      assumption; if pods are still running the old image when the
      flag flips, the event-pump path will reference STAY-ALIVE
      semantics that have been deleted from the preamble).
    - slice-4: human review of spike output (memory content,
      `last_reviewed_commit_sha` correctness, cost delta, WS7 #2)
      before the `EGG_BRC_EVENT_PUMP` default flips to true.
    - slice-6: human inspection of WS7 cache measurement #3 (HITL
      pause if cache_read regression > 20%); confirmation that no
      in-flight pipelines exist before deploy (MCP tools are deleted
      so an old wrapper that still injects them starts with no MCP
      server registered).

    Post-merge:
    - slice-4: monitor 24 h of production BRC traffic for any
      "Agent exited without BRC consensus" entries; fall back via
      `EGG_BRC_EVENT_PUMP=false` deployment env if seen. Rollback
      path for full spike falsification: `git revert` slices 1–3 (no
      production traffic touched the new path because the flag
      stayed off until slice-4).
    - slice-6: monitor latency dashboards 24 h for per-event
      wall-clock regression beyond the 5% budget.
slices:
  - id: 1
    name: |-
      Server-side next-action CLI + BRC memory data plane (additive foundations)
    goal: |-
      Land the read-side primitives the stateless event-pump depends on, without
      changing any existing flow. Build CLI equivalents for the three
      orchestrator-state queries the wrapper will need each event (``egg-orch brc
      get-state``, ``egg-orch brc list-blocking``, ``egg-orch phase get-context``)
      and introduce the durable BRC memory artifact at
      ``.egg-state/agent-outputs/<role>/brc-memory.md`` (path adapted to the flat
      per-role-per-issue layout already in use; see analysis §3.17). Wire
      action-scaffolded memory writes into the existing ``brc_ack`` / ``brc_nack``
      handlers (``sandbox/egg_agent_tools/handlers/brc.py:505,586``) gated by
      ``EGG_BRC_MEMORY=write-only`` so writes accumulate but nothing reads them
      yet. Default behavior unchanged; this slice is purely additive and reversible
      by env flag.
    tasks:
      - id: TASK-1-1
        description: |-
          Add ``egg-orch brc next-action --role R [--json]`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. The subcommand calls the new
          orchestrator route added in TASK-1-2 and returns JSON of shape
          ``{action: "wait" | "propose" | "ack" | "nack" | "confirm" |
          "complete", event_payload?: {...}}``. Register as sibling of the
          existing ``brc ack`` / ``brc nack`` subparsers under the ``brc``
          parent. Honour ``EGG_ORCHESTRATOR_URL`` (orch_cli.py:132) and
          ``EGG_LIFECYCLE_SECRET`` (orch_cli.py:324) for auth. Includes a
          ``--role`` arg that defaults to ``$EGG_AGENT_ROLE`` (set on every
          agent pod per kubernetes_spawner.py:818-823).
        acceptance: |-
          ``egg-orch brc next-action --role coder --json`` against an
          in-process orchestrator fake (FlaskClient + lifecycle-secret
          token, matching the existing ``test_orch_cli_*.py`` pattern)
          returns the documented JSON shape; subcommand registered with
          ``--help`` output describing the ``--role`` and ``--json``
          flags; lifecycle-secret auth tested (rejected without env
          var).
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-1-2
        description: |-
          Add ``POST /api/v1/pipelines/{pid}/consensus/next-action`` route
          handler in ``orchestrator/routes/`` (likely a new
          ``orchestrator/routes/consensus.py`` or extending the existing
          BRC routes — placement decision delegated to the implementing
          coder per orchestrator conventions). Derives next action from
          ``consensus_status`` aggregation + ``peer_consensus.py:949-1024``
          ``_open_nacks_barrier_response`` ``nacks[]`` payload +
          ``changed_artifacts`` delta. Returns the same JSON shape the CLI
          surfaces in TASK-1-1. Decision od-3 from the architect output
          resolves: new dedicated endpoint, not a reuse of
          ``consensus status`` — sequencing logic lives in testable
          orchestrator code, not wrapper bash.
        acceptance: |-
          POST endpoint returns 200 with the documented JSON for each
          (role, BRC-state) combination:
          (1) producer-PROPOSED;
          (2) reviewer with pending proposal;
          (3) dual-role WORKING + peer CONSENSUS_PROPOSE pending →
              next-action returns ``propose`` (NOT ``ack/nack``) per
              #2749 ordering rule (risk_analyst R11 sub-case a);
          (4) dual-role post-own-propose with pending peer review →
              next-action returns ``ack`` / ``nack`` (risk_analyst R11
              sub-case b);
          (5) open-NACK barrier (#2142) blocking re-propose;
          (6) conditional ACK still in effect;
          (7) stale-version (#2482) requiring re-review;
          (8) confirmation eligible;
          (9) role complete.
        role: coder
        files:
          - orchestrator/routes/consensus.py
          - orchestrator/peer_consensus.py
      - id: TASK-1-3
        description: |-
          Add ``egg-orch brc get-state [--verbose]`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. Verb-level alias for the
          existing ``brc_get_state`` handler at
          ``sandbox/egg_agent_tools/handlers/brc.py:679-723``. Returns the
          JSON shape ``{ok, slice_id, consensus: {agents, blocking_agents,
          is_complete}, raw?}`` — matches the MCP-tool surface so the
          wrapper bash can call it directly. ``--verbose`` includes the
          full pipeline-status payload.
        acceptance: |-
          ``egg-orch brc get-state`` returns the same JSON as
          ``mcp__brc__get_state`` from the same env; ``--verbose`` flips
          ``raw`` key on; help text mirrors the MCP tool description.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-1-4
        description: |-
          Add ``egg-orch brc list-blocking`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. Derived view of
          ``consensus.blocking_agents[]`` from ``brc_get_state``. Default
          output: one role per line for shell-friendly consumption
          (``while read role; do …; done``); ``--json`` returns the
          ``{blocking_agents: [...]}`` array.
        acceptance: |-
          Output matches ``mcp__brc__list_blocking`` for the same pipeline;
          newline-delimited default; ``--json`` mode tested; exit code 0
          even when list is empty.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-1-5
        description: |-
          Add ``egg-orch phase get-context [--phase P] [--role R]`` CLI
          subcommand to ``sandbox/egg_lib/orch_cli.py``. Wraps the existing
          ``mcp__phase__get_context`` handler logic; returns JSON of
          pipeline_id, phase, role, assigned tasks, prior-phase artifact
          paths. Defaults pull from ``$EGG_PIPELINE_ID`` /
          ``$EGG_AGENT_ROLE`` env vars.
        acceptance: |-
          Output matches the MCP-tool surface for the same pipeline;
          ``--phase plan --role task_planner`` returns this plan's
          context; lifecycle-secret auth verified.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-1-6
        description: |-
          Add durable BRC memory writer to ``brc_ack`` and ``brc_nack``
          handlers at ``sandbox/egg_agent_tools/handlers/brc.py:505,586``.
          On a successful ACK or NACK, distill a structured memory entry
          into ``.egg-state/agent-outputs/<role>/brc-memory.md``
          (subdirectory layout per architect od-1; path resolved against
          ``EGG_REPO_PATH``). Memory schema must carry the six required
          fields from architect v2 ``design.memory_schema.required_fields``:
          (a) ``## Codebase / change model`` distilled prose;
          (b) ``## Per-producer assessment`` subsections including
          ``producer``, ``last_reviewed_commit_sha`` (the SHA of HEAD at
          review time — slice-3 uses this for
          ``git log {sha}..HEAD --not origin/{base_branch} -p``),
          ``prior_verdict``, ``prior_nack_reasons``,
          ``prior_conditional_obligation``, ``summary_of_assessment``;
          (c) ``## Decision log`` capped at the last 20 entries via
          distill-on-write (od-2). Writes use atomic tempfile + os.replace
          via the promoted ``_persist_atomic_template`` helper from
          ``shared/egg_overseer/state.py:266`` (or
          ``shared/egg_contracts/usage_loader.py:95 _atomic_write`` —
          coder picks the lighter migration). Path constructor raises
          before write if ``EGG_AGENT_ROLE`` is unset/empty (fail-closed,
          per architect od-1 + risk_analyst R14). Gated by
          ``EGG_BRC_MEMORY={off,write-only,full}``: ``off`` skips writes;
          ``write-only`` writes but does not read (read path lands in
          slice-3); ``full`` enables reads. Default ``off`` so slice-1 is
          inert in production.
        acceptance: |-
          ``brc_ack`` and ``brc_nack`` calls with
          ``EGG_BRC_MEMORY=write-only`` produce a well-formed memory file
          with all six schema fields populated per architect v2
          ``design.memory_schema``; decision-log entries capped at 20 via
          distill-on-write; atomic-write contract holds — back-to-back
          handler invocations never see a partial state (test asserts
          via fault injection); path constructor raises on empty
          ``EGG_AGENT_ROLE`` BEFORE creating any file or directory;
          ``EGG_BRC_MEMORY=off`` produces no file; subdirectory
          ``.egg-state/agent-outputs/<role>/`` created if absent; handler
          return values unchanged for callers in every case.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
          - shared/egg_overseer/state.py
      - id: TASK-1-7
        description: |-
          Document the BRC memory schema and layout in
          ``docs/architecture/brc-memory.md`` (new doc). Cover the v2
          schema verbatim: file path
          (``.egg-state/agent-outputs/<role>/brc-memory.md``), scope key,
          all six required fields incl. ``last_reviewed_commit_sha``,
          the three ``EGG_BRC_MEMORY`` modes, atomic-write semantics
          (tempfile + os.replace) and rationale, the fail-closed path
          constructor, distill-on-write decision-log cap at 20, the
          rationale for distill-on-write (architect od-2), and the
          role-allowlist coverage that makes the path writable for every
          participant role (cite ``shared/egg_restrictions/patterns.py``
          line ranges).
        acceptance: |-
          Doc renders cleanly; cross-linked from
          ``docs/architecture/index.md`` and from the consensus subsystem
          README; schema section reproduces the architect v2
          ``design.memory_schema.required_fields`` verbatim; reviewers
          can locate all referenced primitives by file:line.
        role: documenter
        files:
          - docs/architecture/brc-memory.md
          - docs/architecture/index.md
      - id: TASK-1-8
        description: |-
          Unit tests for TASK-1-1..TASK-1-5 CLI subcommands at
          ``tests/sandbox/egg_lib/test_orch_cli_brc.py`` and
          ``tests/sandbox/egg_lib/test_orch_cli_phase.py`` (new files
          alongside the existing ``test_orch_cli_*.py`` suite). Cover
          ``--json`` output shape, lifecycle-secret auth, exit codes,
          empty-list edge cases for ``list-blocking``. Round-trip against
          an in-memory orchestrator fake.
        acceptance: |-
          Tests pass under ``make test``; coverage of each subcommand's
          happy path, auth-missing path, and one edge case
          (empty/blocking-agents-empty for list-blocking; stale-version
          for next-action; verbose mode for get-state).
        role: tester
        files:
          - tests/sandbox/egg_lib/test_orch_cli_brc.py
          - tests/sandbox/egg_lib/test_orch_cli_phase.py
      - id: TASK-1-9
        description: |-
          Unit tests for TASK-1-6 memory writer extending
          ``tests/sandbox/egg_agent_tools/test_handlers_brc.py``.
          Cover: ``EGG_BRC_MEMORY={off,write-only,full}`` modes;
          well-formed entry on ack and nack populating all six required
          fields (incl. ``last_reviewed_commit_sha`` per producer);
          decision-log cap at 20 (distill-on-write); atomic-write
          contract under fault injection (e.g. mocking os.replace to
          fail; assert file system never observes a half-written
          intermediate); fail-closed path constructor (raise on unset
          ``EGG_AGENT_ROLE``); subdirectory creation; scope key per
          (role, slice_id, phase); handler return values unchanged.
        acceptance: |-
          Tests pass under ``make test``; one test per
          ``EGG_BRC_MEMORY`` mode (``off`` produces zero file
          touches; ``write-only`` produces writes but ZERO reads —
          the read code path is exercised through a mocked-fixture
          spy that asserts no read calls happen; ``full`` enables
          both); plus the schema-completeness, decision-log-cap,
          atomic-write (fault injection), fail-closed, and
          subdirectory tests.
        role: tester
        files:
          - tests/sandbox/egg_agent_tools/test_handlers_brc.py
      - id: TASK-1-10
        description: |-
          Unit tests for TASK-1-2 orchestrator route at
          ``orchestrator/tests/test_consensus_next_action.py`` (new
          file). Cover the nine derivation cases listed in TASK-1-2
          acceptance — producer-PROPOSED, reviewer-pending, dual-role
          WORKING+peer PROPOSE-pending (returns ``propose`` per
          risk_analyst R11 sub-case a), dual-role post-own-propose
          (returns ``ack/nack`` per R11 sub-case b), open-NACK
          barrier #2142, conditional ACK, stale-version #2482,
          confirm eligible, role complete — using a FlaskClient
          driving the orchestrator Flask app in-process (the existing
          pattern in ``orchestrator/tests/test_*.py``) plus the
          in-process ``PeerConsensusTracker`` matrix from
          ``orchestrator/peer_consensus.py``. NO ``ScriptedProvider``
          reference — that class does not exist in this codebase
          (verified by reviewer_plan: zero hits in
          ``grep -rn 'class ScriptedProvider' .``); the orchestrator
          route tests work entirely on the in-process Python BRC
          state, not on a deployed agent pod.
        acceptance: |-
          Tests pass under ``make test``; route handler logic
          exercised for each of the nine documented (role, BRC-state)
          combos; 200 status with expected JSON shape verified per
          case; the two dual-role sub-cases are distinct named tests
          (NOT collapsed into one). Suggested test names:
          ``test_next_action_dual_role_pre_propose_returns_propose``
          and
          ``test_next_action_dual_role_post_propose_returns_review``
          (per reviewer_plan v2 non-blocker).
        role: tester
        files:
          - orchestrator/tests/test_consensus_next_action.py
  - id: 2
    name: |-
      Event-pump wrapper + liveness migration (behind feature flag)
    goal: |-
      Rewrite ``orchestrator/consensus_wrapper.py`` as a deterministic event pump
      gated by ``EGG_BRC_EVENT_PUMP=true``: drop the 3-restart FAIL cap
      (``MAX_CONSENSUS_RESTARTS`` at consensus_wrapper.py:38), drop the SSE
      ``consensus.reached`` machinery (consensus_wrapper.py:419–449) and the
      ``_RECOVERY_SYSTEM_PROMPT`` restart prompt (consensus_wrapper.py:64–99),
      replace with a wrapper-driven ``egg-orch message wait-loop`` (the existing
      CLI at ``sandbox/egg_lib/orch_cli.py:1695``) that invokes the agent
      one-shot via the existing ``python3 -m egg_agent`` entry point per
      actionable event. Migrate the heartbeat (#2036) and gateway-session
      keep-alive (#2451) emitters from
      ``sandbox/egg_agent_tools/handlers/message.py:267–429`` into the wrapper's
      blocking wait so liveness no longer depends on the agent being inside
      ``wait_loop``. Replace the restart cap with an idle/no-progress safety
      budget that escalates to overseer. Default behavior unchanged (flag off);
      old wrapper code path retained verbatim alongside.
    dependencies:
      - slice-1
    tasks:
      - id: TASK-2-1
        description: |-
          Add the new event-pump bash template branch to
          ``orchestrator/consensus_wrapper.py``. Gate via
          ``EGG_BRC_EVENT_PUMP`` env var read by
          ``build_consensus_wrapped_command`` (consensus_wrapper.py:716)
          at template-composition time. When unset or ``false``: emit
          the existing ``_CONSENSUS_WRAPPER_TEMPLATE`` (consensus_wrapper.py:116)
          verbatim. When ``true``: emit a new
          ``_EVENT_PUMP_WRAPPER_TEMPLATE`` that runs the deterministic
          loop described in the architect design
          (``execution_loop_pseudocode``). The loop calls
          ``egg-orch brc get-state --json`` (TASK-1-3), checks
          ``role_complete``, calls ``egg-orch brc next-action --json``
          (TASK-1-1) for the next action, and either blocks on
          ``egg-orch message wait-loop`` (existing CLI at
          orch_cli.py:1695) or invokes ``python3 -m egg_agent`` one-shot
          with the composed event prompt (composer lands in slice-3;
          slice-2 ships a minimal prompt stub). On ``role_complete=true``,
          the wrapper calls ``egg-orch consensus confirmed`` (existing
          CLI at orch_cli.py:2753) — NOT a new ``progress complete``
          command — to mark the role's consensus and exit 0. Wrapper
          handles 409 ``stale_version`` and 409 aggregated-NACK from
          ``brc next-action`` as event-pump signals (re-fetch state,
          re-invoke), NOT as transient crashes to retry with backoff.
          The wait filter set must include ``CONSENSUS_PROPOSE``,
          ``CONSENSUS_ACK``, ``CONSENSUS_NACK``, ``STATUS``,
          ``CONSENSUS_RE_REVIEW``, ``OVERSEER_ALERT``.
        acceptance: |-
          With ``EGG_BRC_EVENT_PUMP`` unset: ``build_consensus_wrapped_command``
          emits the existing template byte-for-byte (regression-tested
          via existing snapshot); existing
          ``orchestrator/tests/test_consensus_wrapper.py`` passes
          unchanged. With ``EGG_BRC_EVENT_PUMP=true``: emitted bash loop
          matches the new template; loop terminates on
          ``role_complete=true`` by calling ``egg-orch consensus
          confirmed`` and exits 0; loop handles 409 stale_version by
          re-fetching state without backoff; the snapshot test asserts
          the six-event wait-filter set present; the wait-filter set
          is **constructed conditionally from
          ``consensus_status.is_role_confirmed``** — pre-confirm waits
          OMIT ``CONSENSUS_CONFIRMED`` from the filter (per
          risk_analyst R12 / orchestrator HTTP-400 rejection
          documented in #2064/#2482), post-confirm STAY-ALIVE waits
          INCLUDE it.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-2-2
        description: |-
          Migrate the heartbeat (#2036) emission out of
          ``sandbox/egg_agent_tools/handlers/message.py:267-429``
          (``message_wait_loop`` handler) into the event-pump wrapper
          bash template added in TASK-2-1. The wrapper emits
          ``egg-orch message heartbeat`` (existing CLI subcommand —
          verify name via grep at implement-time, currently
          ``cmd_message_heartbeat``) every 30 s as a background
          subshell while ``egg-orch message wait-loop`` is blocking.
          The heartbeat payload MUST include
          ``slice_id == os.environ['EGG_SLICE_ID']`` (or the
          equivalent shell substitution ``${EGG_SLICE_ID:-}`` passed
          through the CLI) so a regression in slice_id propagation is
          caught directly (risk_analyst R9). Keep the agent-side
          heartbeat path in the *old* template path
          (``EGG_BRC_EVENT_PUMP`` unset) verbatim; only the new
          template owns wrapper-side heartbeating. Slice-4 deletes the
          agent-side path once the flag flips to default.
        acceptance: |-
          New template emits ``egg-orch message heartbeat`` every 30 s
          while wait-loop is blocking (verified by mock + clock
          fast-forward unit test); emitted heartbeat payload includes
          ``slice_id`` sourced from ``EGG_SLICE_ID`` env (verified by
          asserting the request body in a mock unit test); old template
          path unchanged (existing tests pass).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-2-3
        description: |-
          Replace the ``MAX_CONSENSUS_RESTARTS = 3`` cap
          (consensus_wrapper.py:38) with an idle / no-progress safety
          budget driven by env ``EGG_BRC_IDLE_BUDGET_MIN`` (default 30
          minutes per architect od-4; well above the WS7-observed
          10–13 min idle ceiling). When the new template path is
          active and no actionable event has arrived for the budget
          duration, emit ``mcp__progress__overseer_alert`` (anomaly
          ``stuck-phase-transition``, priority ``high``) and continue
          blocking; if no progress for 2× budget, raise the alert
          priority and continue. The old template path keeps
          ``MAX_CONSENSUS_RESTARTS`` verbatim (slice-4 deletes the old
          path).
        acceptance: |-
          With flag off: existing 3-cap behavior unchanged (existing
          tests pass). With flag on: idle budget threshold triggers
          overseer alert at configured duration; alert payload
          includes anomaly type, priority, current BRC state; loop
          continues blocking after alert (not exit 1 → FAILED).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-2-4
        description: |-
          Migrate the gateway-session keep-alive (#2451) out of the
          ``message_wait_loop`` handler into the event-pump wrapper
          bash. Existing keep-alive logic in
          ``sandbox/egg_agent_tools/handlers/message.py`` (around
          :267-429) refreshes the lifecycle-secret-gated session while
          the agent is blocking. The new wrapper performs the same
          refresh as a background subshell alongside the heartbeat
          emitter from TASK-2-2. Old path unchanged.
        acceptance: |-
          With flag on: gateway-session refresh visible in gateway-pod
          access logs at the configured cadence; with flag off:
          existing behaviour unchanged (agent-side keep-alive still
          runs); unit test mocks the refresh endpoint and verifies
          wrapper-side invocation cadence.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-2-5
        description: |-
          Documenter task: update
          ``docs/architecture/orchestrator.md`` and
          ``docs/reference/agent-wait-patterns.md`` to describe the
          event-pump wrapper behaviour gated by ``EGG_BRC_EVENT_PUMP``.
          Cover: how the wrapper loop drives lifecycle; how
          wrapper-side heartbeat + keep-alive replace the agent-held
          versions; the ``slice_id`` propagation invariant on the
          heartbeat payload; how the idle budget replaces the restart
          cap; the 409 stale_version / aggregated-NACK handling; the
          slice-2 verification stance (unit-test-only because no
          in-process test double can drive a deployed pod
          end-to-end per #2474 — true E2E deferred to slice-4 via
          ``egg_stack``). Mark the flag-off path as the temporary
          default until slice-4 flips it. Do NOT update
          ``mission.md`` yet (slice-3 owns that rewrite).
        acceptance: |-
          Both docs render with new sections; cross-linked from each
          other and from the consensus subsystem README;
          ``EGG_BRC_EVENT_PUMP`` and ``EGG_BRC_IDLE_BUDGET_MIN`` env
          vars listed in ``docs/reference/environment-variables.md``
          (or equivalent existing reference doc — locate via Grep).
        role: documenter
        files:
          - docs/architecture/orchestrator.md
          - docs/reference/agent-wait-patterns.md
      - id: TASK-2-6
        description: |-
          Unit tests extending
          ``orchestrator/tests/test_consensus_wrapper.py``. Cover:
          (i) template selection branches for both flag values
          (snapshot test asserting the six-event wait-filter set on
          the flag-on path); (ii) wrapper-side heartbeat cadence
          (mock subprocess + fast-forward); (iii) heartbeat payload
          includes ``slice_id`` sourced from ``EGG_SLICE_ID`` (one
          test pins this directly); (iv) wrapper-side keep-alive
          cadence; (v) idle budget alert at configured threshold;
          (vi) 409 stale_version handled as re-fetch (not
          retry-with-backoff); (vii) ``role_complete=true`` path
          calls ``egg-orch consensus confirmed`` and exits 0;
          (vii.b) the wrapper does NOT also call ``egg-orch progress
          complete`` (defensive guard against the pseudocode-typo
          the architect corrected); (viii) the wait-filter
          construction OMITS ``CONSENSUS_CONFIRMED`` pre-confirm and
          INCLUDES it post-confirm (risk_analyst R12); (ix)
          unset-``EGG_SLICE_ID`` case (plan/refine phase) emits
          either explicit-null or omitted slice_id on the heartbeat
          payload (NOT empty-string). The existing 3-cap tests must
          continue to pass with flag off. End-to-end validation is
          OUT OF SCOPE for slice-2 (deferred to slice-4 spike).
        acceptance: |-
          All tests pass under ``make test``; flag-off snapshot
          matches existing template byte-for-byte; flag-on snapshot
          shows expected new bash loop with the six-event wait filter
          AND the conditional ``CONSENSUS_CONFIRMED`` inclusion
          (pre-/post-confirm); heartbeat-slice_id test fails if the
          wiring regresses (assertion directly on the request body);
          test (vii.b) asserts ``rg 'progress complete'`` against
          the emitted bash returns zero matches; both heartbeat and
          keep-alive cadence tests pass deterministically (no flaky
          sleeps).
        role: tester
        files:
          - orchestrator/tests/test_consensus_wrapper.py
      - id: TASK-2-7
        description: |-
          In-process BRC regression test pass: run
          ``integration_tests/regression/test_brc_*.py`` with
          ``EGG_BRC_EVENT_PUMP=false`` (default) and assert green —
          establishes zero orchestrator-side regression on the
          existing in-process ``PeerConsensusTracker`` path. Do NOT
          add a flag-on E2E test here: no in-process test double can
          drive a deployed agent pod end-to-end — the pod-injection
          ``ScriptedProvider`` avenue was ruled out per #2474 (see
          ``integration_tests/regression/conftest.py:45`` and the
          comment in ``integration_tests/regression/test_brc_concurrency.py``
          at lines 1-25). True end-to-end validation is deferred to
          slice-4's spike on issue-2270/qwen3.7-max using the
          ``egg_stack`` real-pod fixture
          (``integration_tests/conftest.py:340``).
        acceptance: |-
          ``integration_tests/regression/test_brc_*.py`` runs green
          with flag off; no flag-on E2E added in this slice; the
          rationale ("no in-process double can drive a deployed pod
          per #2474; E2E deferred to slice-4 via egg_stack") is
          documented as a comment in the test file or in
          ``docs/architecture/integration-test-trust-boundary.md``.
        role: tester
        files:
          - integration_tests/regression/test_brc_concurrency.py
  - id: 3
    name: |-
      Delta-scoped re-analysis + prompt collapse
    goal: |-
      Wire the per-event invocation to hand the agent (a) the durable memory
      file from slice-1 and (b) the proposal version / ``changed_artifacts``
      delta from the orchestrator's existing version-tracking (#2142, surfaced
      in ``orchestrator/peer_consensus.py``), so on a re-proposal the agent
      evaluates only the delta rather than re-reading the codebase. Collapse
      ``_build_brc_preamble`` (``orchestrator/routes/pipelines.py:12348``) and
      strip the STAY-ALIVE / wait-loop mechanics / cursor-threading /
      pre-confirm-wait foot-gun text — replace with a lean event-handler
      contract that names the single event being processed this turn and the
      single action expected. Update ``sandbox/agent-config/rules/mission.md``
      and any orchestrator/sandbox rules-injection to match. Flag still off by
      default; this slice prepares the per-event prompt shape but does not flip
      the production switch.
    dependencies:
      - slice-2
    tasks:
      - id: TASK-3-1
        description: |-
          Add ``compose_event_prompt(role, event_payload, memory_excerpt,
          nacks, git_log_delta, base_branch) -> str`` helper to
          ``orchestrator/routes/pipelines.py`` (or a new sibling module
          if the file is at the size limit — coder's call). Returns the
          single-event prompt the wrapper invokes the agent with. Shape:
          role banner + one-line event description + memory excerpt
          (≤ 2 KB) appended at tail position (architect od-6 Option B —
          do NOT reference the illustrative ``--append-context`` flag,
          which does not exist on ``build_agent_command``); the FULL
          ``git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p``
          delta per producer (NOT just orchestrator-side
          ``changed_artifacts`` — per
          ``docs/architecture/REVIEWER-SYNC.md`` the re-review must
          audit the full delta as a fresh review or the stateless pump
          systematically weakens adversarial re-review,
          risk_analyst R6); NACK payload from
          ``peer_consensus.py:949-1024`` ``_open_nacks_barrier_response``
          ``nacks[]`` (per-reviewer with reason + artifact_refs); the
          single action expected. The git-log delta is scaled by actual
          change size and is NOT counted against the ≤ 10 KB envelope —
          the envelope bounds the surrounding prose only.
        acceptance: |-
          Helper unit-tested for each role (producer / reviewer /
          dual-role); output envelope ≤ 10 KB for representative event
          payloads (excluding the git-log delta which scales with the
          change); composer correctly truncates memory excerpts that
          exceed 2 KB; git-log delta command is emitted verbatim with
          the per-producer ``last_reviewed_commit_sha`` substituted in;
          NACK payload renders per-reviewer with reason + artifact_refs.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-2
        description: |-
          Wire the event-pump template branch from TASK-2-1 to call
          ``compose_event_prompt`` (TASK-3-1) at per-event invocation
          time. Read the memory excerpt from
          ``.egg-state/agent-outputs/<role>/brc-memory.md`` (slice-1
          writer) when ``EGG_BRC_MEMORY=full``; with
          ``EGG_BRC_MEMORY=write-only`` (slice-1 default), pass empty
          memory_excerpt — writes happen but reads are no-ops,
          preserving slice-1's inert default. Memory is delivered
          inline at the user-prompt tail (architect od-6 Option B);
          the illustrative ``--append-context`` from the analysis
          pseudocode is NOT a real flag on ``build_agent_command``
          (verified at ``shared/egg_agent/command.py:11-46``). Read
          the per-producer ``last_reviewed_commit_sha`` from the
          memory file's structured section and pass it through to
          ``compose_event_prompt`` so the git-log delta command is
          parameterised correctly.
        acceptance: |-
          Wrapper template emits expected ``compose_event_prompt``
          invocation; with ``EGG_BRC_MEMORY=full`` and a populated
          memory file, the prompt includes both the memory excerpt
          and the per-producer git-log delta; with
          ``EGG_BRC_MEMORY=write-only`` (slice-1 default), the prompt
          omits memory but still emits the git-log delta against the
          orchestrator's signal-level ``changed_artifacts`` as a
          fallback baseline; snapshot test verifies both branches.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-3-3
        description: |-
          Collapse ``_build_brc_preamble`` at
          ``orchestrator/routes/pipelines.py:12348``. Delete the
          STAY-ALIVE / wait-loop mechanics / cursor-threading / pre-confirm-wait
          foot-gun guidance (Producer Lifecycle step 4 wait-loop
          plumbing; Producer step 6 STAY-ALIVE loop; cursor /
          ``--since`` guidance). KEEP: agent roster, reviewer/producer
          assignments, dual-role ordering banner; AND the
          dual-mandate adversarial re-review banner at
          ``orchestrator/routes/pipelines.py:12849-12872`` (the
          "Your re-review has TWO equal-weight mandates…" block —
          behavioural framing anchored on by risk_analyst R6, NOT
          seam-related). The three callers at
          ``orchestrator/routes/pipelines.py:13659, :13692, :13720``
          are unchanged — only the preamble text collapses. Slice-3
          keeps the flag off by default so the collapsed preamble
          runs against the *legacy* wrapper path today; slice-4 makes
          it the default once the event-pump path is live.
        acceptance: |-
          Snapshot test for the collapsed preamble lands at
          ``orchestrator/tests/test_brc_preamble_collapsed.py``;
          STAY-ALIVE / wait-loop / cursor sections absent; roster +
          assignments preserved; the phrase ``Both must pass to ACK``
          (verified at ``orchestrator/routes/pipelines.py:12856-12857``
          inside the dual-mandate banner at
          pipelines.py:12849-12872) appears in the post-collapse
          preamble snapshot — phrase choice corrects reviewer_plan
          v2's finding that "Both mandates have equal weight" lives
          at line 13292 inside ``_build_adversarial_reprime`` rather
          than inside ``_build_brc_preamble``; preamble byte size
          drops by ≥ 25% (measured against pre-collapse snapshot;
          the exact number is the snapshot baseline result, not a
          pre-set target — softened from ≥ 40% per reviewer_plan v2
          non-blocker).
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-4
        description: |-
          Rewrite the STAY-ALIVE / wait-loop section of
          ``mission.md`` (lines 151–154 plus surrounding "Concurrent
          Execution Mode" section starting at line 137) to the
          event-handler contract: the agent is invoked one-shot per
          event by the wrapper; act on the single event, update
          memory, exit naturally. Remove "never exit before the
          orchestrator stops you" — under the new model the wrapper
          owns lifecycle. Keep the Anti-Sycophancy /
          Structured-Progress-Reporting / HITL-vs-OVERSEER_ALERT /
          Handling-Agent-Failures sections unchanged.

          **The mission.md rule exists at TWO paths on disk that are
          maintained as byte-identical duplicates with NO automated
          sync** (reviewer_plan blocker; independently verified via
          ``sandbox/Dockerfile:212-214`` `COPY sandbox/claude-rules/*.md`
          and ``sandbox/entrypoint.py:967``
          ``_CLAUDE_RULES_DIR = Path("/opt/claude-rules")``). The
          Dockerfile-baked path that reaches the running agent pod
          is ``sandbox/claude-rules/mission.md`` — that one is the
          canonical runtime source. ``sandbox/agent-config/rules/mission.md``
          is the documentation-style duplicate. **Both files MUST be
          rewritten** so that after the slice-3 commit
          ``diff sandbox/agent-config/rules/mission.md sandbox/claude-rules/mission.md``
          still returns empty AND the new content is present in
          both. Treat ``sandbox/claude-rules/mission.md`` as the
          runtime-load truth source; the other path is kept in lock
          step for now. (A separate follow-up issue should
          consolidate or symlink the two paths — out of scope for
          this slice.)

          The mission.md rewrite reaches the agent pod only after
          the sandbox image is rebuilt and pods are restarted; this
          MUST land BEFORE slice-4's flag flip — the
          rebuild-verification is part of this task's acceptance.

          Role assignment: ``documenter`` (deviating from #1537's
          coder-spirit for agent-config rule files). Reason: the
          gateway-enforced patterns at
          ``shared/egg_restrictions/patterns.py`` exempt
          ``sandbox/agent-config/rules/*.md`` from the coder docs
          block (lines 246-247, per #1537) but do NOT exempt
          ``sandbox/claude-rules/*.md`` — and the Dockerfile-baked
          runtime copy is the ``claude-rules`` path
          (sandbox/Dockerfile:212-214). A coder-role task cannot
          push ``sandbox/claude-rules/mission.md`` (verified via
          ``check_file_restriction``: coder is blocked, documenter
          can write). Documenter has write access to both paths via
          the ``DEFAULT_DOCS_GLOBS`` ``**/*.md`` pattern at
          ``patterns.py:177-181``. Splitting this into two tasks
          (coder + documenter) doubles the BRC review surface for
          one rewrite — keeping it as a single documenter task is
          the lighter-weight resolution. (A follow-up issue should
          either consolidate the two paths or add the ``claude-rules``
          allowlist entry to coder.)
        acceptance: |-
          BOTH ``sandbox/agent-config/rules/mission.md`` AND
          ``sandbox/claude-rules/mission.md`` reflect event-handler
          semantics; the "stay alive" / "wait-loop" / "never exit"
          lines are replaced with the event-handler contract; the
          other four sections unchanged; ``diff
          sandbox/agent-config/rules/mission.md
          sandbox/claude-rules/mission.md`` returns empty after the
          commit (the two duplicates remain byte-identical); ``rg
          'STAY-ALIVE\b|wait-loop|never exit'`` against both files
          returns zero matches. The sandbox-image build step
          (documented in ``docs/guides/sandbox-image.md`` or
          equivalent — locate via Grep at implement-time) is
          exercised and produces a new image tag; the documented
          rebuild-trigger is recorded in the PR body so slice-4 can
          verify the new image deployed BEFORE the flag flip.
        role: documenter
        files:
          - sandbox/agent-config/rules/mission.md
          - sandbox/claude-rules/mission.md
      - id: TASK-3-5
        description: |-
          Documenter: update ``docs/architecture/orchestrator.md``
          (the BRC subsystem section) and
          ``docs/reference/agent-wait-patterns.md`` to describe the
          delta-scoped re-analysis behaviour and the per-event prompt
          shape, including the full
          ``git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p``
          delivery per producer (and why — REVIEWER-SYNC.md adversarial
          re-review requirement, risk_analyst R6). Cover the
          architect's open-decision resolutions: od-1 (subdirectory
          layout), od-2 (distill memory), od-3 (new ``brc
          next-action`` endpoint), od-4 (30-min idle budget), od-6
          (memory inline at tail position — Option B). Link to the
          new ``docs/architecture/brc-memory.md`` from slice-1.
        acceptance: |-
          Both docs reflect slice-3 changes; open-decision
          resolutions documented with their slice-1/slice-2
          implementation citations; cross-links to brc-memory.md
          present; REVIEWER-SYNC.md citation included on the
          full-delta rationale.
        role: documenter
        files:
          - docs/architecture/orchestrator.md
          - docs/reference/agent-wait-patterns.md
      - id: TASK-3-6
        description: |-
          Unit tests for TASK-3-1 ``compose_event_prompt`` at
          ``orchestrator/tests/test_compose_event_prompt.py``. Cover:
          each role's prompt shape; memory excerpt truncation at the
          2 KB cap; NACK delta with 0 / 1 / 2+ reviewers; git-log
          delta command emitted verbatim with the per-producer
          ``last_reviewed_commit_sha`` substituted (NO ``changed_artifacts``-only
          shortcut); total prompt envelope (excluding git-log delta) ≤
          10 KB per case.
        acceptance: |-
          Tests pass under ``make test``; one test per role; envelope
          assertion verified per case; assertion against the
          git-log-delta command string fails on regression to a
          ``changed_artifacts``-only shortcut.
        role: tester
        files:
          - orchestrator/tests/test_compose_event_prompt.py
      - id: TASK-3-7
        description: |-
          Snapshot test for the collapsed preamble at
          ``orchestrator/tests/test_brc_preamble_collapsed.py`` (new
          file). Loads the rendered preamble for each of the three
          caller sites (pipelines.py:13659, :13692, :13720) and
          asserts (a) the new snapshot matches; (b) STAY-ALIVE /
          wait-loop / cursor strings absent; (c) agent roster present;
          (d) byte size drop ≥ 40% vs the prior snapshot baseline.
          Also capture **WS7 cache measurement #1** (post-slice-3
          baseline): run a representative event sequence through the
          new prompt path on a local test pipeline and record
          ``cache_read_input_tokens`` / Qwen cost_callback aggregate
          to ``.egg-state/agent-outputs/ws7-measurement-slice-3.json``
          for slice-4 / slice-6 comparison.
        acceptance: |-
          Snapshot tests pass under ``make test``; snapshots
          committed; absent-strings assertions trigger on regression;
          byte-size assertion stable (with a 5% tolerance band); WS7
          measurement file captured under
          ``.egg-state/agent-outputs/`` with the documented schema
          (per-event ``cache_read_input_tokens``, total invocation
          count, Qwen aggregate).
        role: tester
        files:
          - orchestrator/tests/test_brc_preamble_collapsed.py
  - id: 4
    name: |-
      Spike validation + flag flip + delete old capped-restart wrapper path
    goal: |-
      Run the #2906 repro (issue-2270, qwen3.7-max) end-to-end with
      ``EGG_BRC_EVENT_PUMP=true`` and ``EGG_BRC_MEMORY=full``; confirm consensus
      reaches CONFIRMED without restart churn, the memory file is populated and
      consulted, per-event context is bounded, and ``cache_read`` is observed
      across consecutive per-event invocations on both routes (Anthropic via
      ``usage.cache_read_input_tokens``, Qwen via
      ``~/.local/state/clm/cost-*.json`` per the WS7 closure comment). With
      spike green, flip the flag default to on and delete the old
      capped-restart / SSE / recovery-prompt path code from
      ``consensus_wrapper.py``, plus the now-orphaned wait_loop heartbeat code
      from ``handlers/message.py``. Old code lives behind ``EGG_BRC_EVENT_PUMP``
      one release for emergency rollback; this slice deletes it.
    dependencies:
      - slice-3
    tasks:
      - id: TASK-4-1
        description: |-
          Add k3s integration test
          ``integration_tests/test_event_pump_spike_2906.py``
          (directly under ``integration_tests/`` — the
          ``local_pipeline/`` directory the architect referenced does
          not exist in this codebase per reviewer_plan v2). Uses the
          session-scoped ``egg_stack`` fixture at
          ``integration_tests/conftest.py:340`` (k3s-backed; reaches
          gateway via ``egg_stack.gateway_url`` attribute on the
          ``EggStack`` dataclass at
          ``integration_tests/conftest.py:78``; orchestrator via
          ``egg_stack.orchestrator_url`` at :79; lifecycle bearer via
          ``egg_stack.lifecycle_secret`` at :90). Pre-flight: assert
          the sandbox image built in slice-3 (TASK-3-4) is the
          deployed image tag AND assert the deployed
          ``/opt/claude-rules/mission.md`` (the Dockerfile-baked path
          per ``sandbox/Dockerfile:212-214`` +
          ``sandbox/entrypoint.py:967``) NO LONGER contains the
          STAY-ALIVE / wait-loop phrases — kubectl-exec into the
          running pod and ``grep -E 'STAY-ALIVE\b|wait-loop|never
          exit' /opt/claude-rules/mission.md`` must return zero
          matches. Fail fast on either pre-flight check so a stale
          image with fresh tag (or fresh image with stale mission.md
          content) is caught BEFORE the spike runs. Run the #2906
          repro:
          issue-2270, qwen3.7-max provider configuration,
          ``EGG_BRC_EVENT_PUMP=true``, ``EGG_BRC_MEMORY=full``,
          default idle-budget. Assertions:
          (a) consensus reaches CONFIRMED for every role in the
          pipeline;
          (b) no ``Agent exited without BRC consensus`` log entry;
          (c) brc-memory.md populated per reviewer with all six
          required fields and ``last_reviewed_commit_sha`` updated
          per producer (mechanically derivable from the orchestrator
          signal payload, so a static-value regression is catchable);
          (d) per-event prompt envelope ≤ 10 KB on captured agent
          invocations;
          (e) ``cache_read_input_tokens`` > 0 on Anthropic route via
          SDK usage capture;
          (f) Qwen-route cache read > 0 via
          ``~/.local/state/clm/cost-*.json`` aggregation.
          Capture **WS7 measurement #2** to
          ``.egg-state/agent-outputs/ws7-measurement-slice-4.json``
          for slice-6 comparison.
        acceptance: |-
          Test passes against k3s with the qwen3.7-max provider;
          assertions a–f all green; sandbox-image-deployed pre-flight
          fails fast if (i) the new image is not picked up OR (ii)
          the deployed pod's ``/opt/claude-rules/mission.md`` still
          contains the STAY-ALIVE / wait-loop / never-exit phrases
          (content-grep pre-flight, not just image-tag freshness);
          WS7 #2 file written to ``.egg-state/agent-outputs/``;
          test runs under ``make test-all`` against the
          ``egg_stack`` fixture (kubectl-gated, k3s-backed; the test
          skips if ``_kubectl_available()`` returns False).
        role: tester
        files:
          - integration_tests/test_event_pump_spike_2906.py
      - id: TASK-4-2
        description: |-
          Flip the ``EGG_BRC_EVENT_PUMP`` default in
          ``orchestrator/consensus_wrapper.py``'s
          ``build_consensus_wrapped_command`` from false to true. With
          the default flipped, the new template path is the production
          path; the old template path is only emitted when an operator
          sets ``EGG_BRC_EVENT_PUMP=false`` explicitly. Same flip for
          ``EGG_BRC_MEMORY`` from ``off`` to ``full`` so the
          delta-scoped re-analysis from slice-3 reads the memory file
          in production. Pre-flight: TASK-4-1's spike test green.
        acceptance: |-
          ``build_consensus_wrapped_command`` with unset env emits
          the new template; with explicit
          ``EGG_BRC_EVENT_PUMP=false`` emits the old template (the
          one-release rollback path is preserved); existing snapshot
          tests updated to reflect the new default; BRC integration
          suite passes on the new default; rollback plan documented
          in PR body (``git revert`` slices 1–3 if production traffic
          shows regression).
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-4-3
        description: |-
          Delete the old capped-restart bash template, the
          ``_RECOVERY_SYSTEM_PROMPT`` (consensus_wrapper.py:64-99),
          the SSE ``consensus.reached`` machinery
          (consensus_wrapper.py:418-449), and the
          ``MAX_CONSENSUS_RESTARTS`` constant + use-sites. Keep
          ``is_buffer_overflow`` / ``is_transient_crash`` /
          ``is_startup_failure`` classifiers — they're still valid
          signals under the new idle/no-progress safety budget.
          Delete the agent-side wait_loop heartbeat path from
          ``sandbox/egg_agent_tools/handlers/message.py:267-429`` —
          the wrapper now owns heartbeating (TASK-2-2). Same for the
          gateway-session keep-alive in the same region (TASK-2-4
          migrated; this task deletes the agent-side path).
        acceptance: |-
          ``orchestrator/consensus_wrapper.py`` no longer contains
          ``MAX_CONSENSUS_RESTARTS``, ``_RECOVERY_SYSTEM_PROMPT``, or
          SSE / consensus.reached strings; ``rg 'consensus\.reached|sse_url|_RECOVERY_SYSTEM_PROMPT|MAX_CONSENSUS_RESTARTS'
          orchestrator/consensus_wrapper.py`` returns zero matches
          (defensive grep assertion against partial deletion);
          ``handlers/message.py`` no longer emits heartbeats or
          refreshes the gateway session; the three crash classifiers
          remain; relevant tests in
          ``orchestrator/tests/test_consensus_wrapper.py`` updated
          (or deleted, where old-path-specific tests no longer apply)
          — replacement coverage lands in TASK-4-4.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
          - sandbox/egg_agent_tools/handlers/message.py
      - id: TASK-4-4
        description: |-
          Update tests in
          ``orchestrator/tests/test_consensus_wrapper.py`` and
          ``tests/sandbox/egg_agent_tools/test_handlers_message.py``
          to reflect the deletions in TASK-4-3. Delete tests of the
          retired capped-restart cap, recovery prompt, SSE path,
          and agent-side heartbeat / keep-alive. Add coverage for
          the new wrapper behaviour where it replaces the old (the
          idle-budget test from slice-2 becomes the canonical
          liveness coverage).
        acceptance: |-
          All retired tests removed; remaining tests pass under
          ``make test``; coverage report does not regress for the
          consensus_wrapper module (replacement tests cover the
          equivalent semantics).
        role: tester
        files:
          - orchestrator/tests/test_consensus_wrapper.py
          - tests/sandbox/egg_agent_tools/test_handlers_message.py
      - id: TASK-4-5
        description: |-
          Documenter: rewrite ``docs/architecture/orchestrator.md``
          consensus-wrapper section to describe the post-deletion
          steady state — event-pump as the only path, idle budget
          replaces restart cap, wrapper owns heartbeat + keep-alive.
          Remove the "flag-off legacy path" caveats added in slice-2.
          Document the rollback plan (revert slices 1–3) for
          completeness. Cross-link to
          ``docs/architecture/brc-memory.md`` from slice-1.
        acceptance: |-
          Doc reads as if the event pump has always been the only
          model; legacy-path caveats removed; cross-links present;
          rollback plan documented; rendering clean.
        role: documenter
        files:
          - docs/architecture/orchestrator.md
  - id: 5
    name: |-
      Additive CLI surface: stdin/file prose plumbing + new BRC subcommands
    goal: |-
      Net-additive predecessor to the MCP deletion in slice-6. (a) Add stdin /
      ``--reason-file`` / ``--summary-file`` / ``--files-reviewed-file`` plumbing
      to the prose-bearing CLI commands ``egg-orch consensus propose --summary``,
      ``consensus ack --reason``, ``consensus nack --reason`` (all argv-only
      today per ``sandbox/egg_lib/orch_cli.py:3265,3485,3573,3600,3647,3660``);
      keep argv accepted as a fallback during transition. (b) Add the two
      net-new CLI subcommands the slice-6 deletion depends on:
      ``egg-orch brc resolve-obligation`` and ``egg-orch brc read-peer-artifact``
      (``brc get-state`` / ``list-blocking`` / ``phase get-context`` already
      shipped in slice-1). (c) Ship the #2741 regression-guard test asserting
      prose containing ``$VAR`` / backticks / ``;`` / ``&&`` / embedded newlines
      round-trips byte-equal via stdin and via ``--reason-file``. No MCP changes;
      no existing-flow behavior change. This is the additive-API-introduction
      slice — separating it from the deletion in slice-6 follows the architect
      rubric ("avoid bundling deletion-heavy work with new-API-introduction
      work") and risk_analyst R4's "stdin/file prose plumbing lands BEFORE any
      MCP tool deprecation in WS8".
    dependencies:
      - slice-4
    tasks:
      - id: TASK-5-1
        description: |-
          Add stdin / file alternative for prose-bearing args on
          ``cmd_consensus_propose --summary`` (parser at
          ``sandbox/egg_lib/orch_cli.py:3265``), ``cmd_consensus_ack
          --reason`` (parser at orch_cli.py:3485,3523),
          ``cmd_consensus_nack --reason`` (parser at orch_cli.py:3573),
          and ``cmd_consensus_withdraw --reason`` (parser at
          orch_cli.py:3600). Today these args are argv-only and
          re-introduce the shell-metachar corruption mitigated in
          #2741 when the wrapper bash composes the command. Reuse the
          ``--file PATH`` pattern from existing
          ``cmd_consensus_propose`` (orch_cli.py:2552). New flags:
          ``--summary-file PATH`` (propose), ``--reason-file PATH``
          (ack / nack / withdraw), ``--files-reviewed-file PATH``
          (ack / nack — JSON array on disk, one path per line per
          architect v2 §verification_strategy.slice_5), stdin
          sentinel ``--summary -`` / ``--reason -``. Keep argv
          ``--summary`` / ``--reason`` working for now (deprecation
          lives in a later cycle) but emit a deprecation warning when
          used.
        acceptance: |-
          ``--summary-file PATH`` / ``--reason-file PATH`` /
          ``--files-reviewed-file PATH`` round-trip multi-line UTF-8
          prose containing shell metacharacters intact (``$VAR``,
          backticks, ``;``, ``&&``, newlines); stdin sentinel works
          for ``echo … | egg-orch consensus ack --reason -``; argv
          path emits deprecation warning to stderr; existing CLI
          behavior preserved on argv path (regression test).
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-5-2
        description: |-
          Add ``egg-orch brc resolve-obligation`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. Wraps the existing
          ``mcp__brc__resolve_obligation`` handler in
          ``sandbox/egg_agent_tools/handlers/brc.py``. Args:
          ``--reviewer-role``, ``--producer-role``, ``--commit-sha``
          (optional), ``--note`` (optional; via stdin or
          ``--note-file PATH`` per the #2741 prose-arg rule from
          TASK-5-1).
        acceptance: |-
          CLI subcommand registered; round-trip against the
          orchestrator succeeds; help text mirrors the MCP-tool
          description; prose ``--note`` exercised via stdin and via
          ``--note-file PATH``.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-5-3
        description: |-
          Add ``egg-orch brc read-peer-artifact`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. Wraps the existing
          ``mcp__brc__read_peer_artifact`` handler. Args:
          ``--phase`` (required), ``--peer-role`` (optional),
          ``--message-type`` (optional, repeatable), ``--limit``
          (default 50, max 500), ``--cursor`` (opaque token),
          ``--include-unattributed`` (default true). Stdout JSON.
        acceptance: |-
          CLI subcommand registered; matches handler behaviour for
          slice-scoped and unattributed reads; pagination tested
          with ``--limit`` + ``--cursor`` round-trip.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-5-4
        description: |-
          Documenter: update
          ``docs/reference/agent-tools.md`` (or equivalent — locate
          via Grep ``docs/`` for "consensus propose" /
          "consensus ack") and
          ``docs/reference/agent-wait-patterns.md`` to document the
          new ``--summary-file`` / ``--reason-file`` /
          ``--files-reviewed-file`` flags and stdin sentinel, the
          two new ``brc resolve-obligation`` / ``brc
          read-peer-artifact`` subcommands, and the deprecation
          warning on the argv ``--summary`` / ``--reason`` path.
          Cross-link to #2741 for the shell-metachar rationale.
        acceptance: |-
          Docs reflect the new CLI surface; deprecation note on the
          argv path included; #2741 cross-link present.
        role: documenter
        files:
          - docs/reference/agent-tools.md
          - docs/reference/agent-wait-patterns.md
      - id: TASK-5-5
        description: |-
          #2741 regression-guard test at
          ``tests/sandbox/egg_lib/test_orch_cli_prose_args.py`` (new
          file). For each of ``consensus propose --summary``,
          ``consensus ack --reason``, ``consensus nack --reason``,
          ``consensus withdraw --reason``: round-trip prose
          containing each of ``$VAR``, single backticks, ``$()``,
          ``;``, ``&&``, embedded newlines, UTF-8
          non-ASCII characters — via stdin sentinel ``-`` AND via
          ``--*-file PATH`` — and assert byte-equality between the
          on-disk input and the request body received by the
          orchestrator stub. Also test the ``--files-reviewed-file``
          one-path-per-line semantics. Argv-path tests verify the
          deprecation warning lands on stderr.
        acceptance: |-
          Tests pass under ``make test``; one parametrized test per
          (CLI command × prose payload × delivery channel) case;
          deprecation-warning assertion present on the argv-path
          tests.
        role: tester
        files:
          - tests/sandbox/egg_lib/test_orch_cli_prose_args.py
      - id: TASK-5-6
        description: |-
          Unit tests for TASK-5-2 / TASK-5-3 CLI subcommands at
          ``tests/sandbox/egg_lib/test_orch_cli_brc.py`` (extending
          the file added in slice-1 TASK-1-8). Cover
          ``brc resolve-obligation`` happy path + ``--note`` via
          stdin and via ``--note-file``; ``brc read-peer-artifact``
          paginated round-trip; lifecycle-secret auth on both.
        acceptance: |-
          Tests pass under ``make test``; one test per subcommand's
          happy path plus pagination / prose-channel edge case.
        role: tester
        files:
          - tests/sandbox/egg_lib/test_orch_cli_brc.py
      - id: TASK-5-7
        description: |-
          Capture MCP-surface latency baseline for slice-6's
          comparison test (TASK-6-6 revised acceptance). Add a
          fixture at
          ``integration_tests/test_mcp_baseline_capture.py``
          (directly under ``integration_tests/``; ``local_pipeline/``
          does not exist). Drive a real-LLM 5-role consensus on
          the still-live MCP surface (slice-5 is additive only —
          MCP tools are still registered) using the session-scoped
          ``egg_stack`` fixture at
          ``integration_tests/conftest.py:340`` (kubectl-gated;
          k3s-backed; agents run real Claude / Qwen via the litellm
          route configured for the test stack — no
          ``ScriptedProvider`` reference; that class does not exist
          per reviewer_plan v2). Record per-event wall-clock samples
          (event_type, start_ts, end_ts, agent-process exit code as
          captured from the orchestrator's pipeline-status events,
          NOT from a non-existent in-process provider) and write to
          ``.egg-state/agent-outputs/latency-mcp-baseline.json``.
          The committed JSON file is slice-6's baseline; capturing
          it in slice-5 sidesteps the vendored-tarball maintenance
          burden the original TASK-6-6 carried.
        acceptance: |-
          Test runs against the ``egg_stack`` fixture (kubectl-gated;
          skips if ``_kubectl_available()`` returns False); produces
          ``latency-mcp-baseline.json`` with schema documented in
          the test file (``samples: [{event_type, start_ts,
          end_ts, exit_code}]`` plus aggregate p50/p95); JSON file
          committed at the end of slice-5; slice-6 TASK-6-6 reads
          this file to derive its baseline. No ``ScriptedProvider``
          import or reference.
        role: tester
        files:
          - integration_tests/test_mcp_baseline_capture.py
  - id: 6
    name: |-
      MCP→CLI deletion: delete agent MCP server + migrate tests
    goal: |-
      Mechanical deletion now that slice-5 has shipped the non-argv prose path
      the agent needs. Delete the 28 agent-facing MCP tools across the 11 files
      under ``sandbox/egg_agent_tools/tools/*.py`` (~2,034 LOC total per the wc
      count), ``SYSTEM_PROMPT_NUDGE`` at ``sandbox/egg_agent_tools/server.py:61``
      and ``build_sandbox_mcp_server``, the MCP registration block at
      ``shared/egg_agent/client.py:299–353`` INCLUDING the ``EGG_MCP_TOOLS``
      env-flag check at line 311 (no orphan flag). Retire
      ``tests/tools/test_mcp_cli_drift.py``. Migrate
      ``integration_tests/test_sandbox_mcp_tools_e2e.py`` — re-purposed so the
      agent's first action is ``egg-orch consensus ack/nack`` via stdin/file
      (preserves the SDK-spawn exercise rather than collapsing to direct-handler)
      — and ``tests/sandbox/egg_agent_tools/test_server.py`` (the MCP-registration
      test goes away with the MCP server). The shared handler layer at
      ``sandbox/egg_agent_tools/handlers/*.py`` is UNCHANGED — both surfaces
      already use it; this slice removes the duplicate MCP surface only. Verify
      per-event wall-clock latency is unchanged (subprocess spawn vs in-process
      MCP dispatch on a representative event sample); fall back to a persistent
      ``egg-orch`` daemon over a Unix socket only if measured regression > 5%.
      Operator-facing ``orchestrator/mcp_server.py`` is out of scope.
    dependencies:
      - slice-5
    tasks:
      - id: TASK-6-1
        description: |-
          Delete the 7 MCP tool namespace files at
          ``sandbox/egg_agent_tools/tools/{brc,checkpoint,message,phase,progress,sdlc,task}.py``
          (~1,515 LOC). Delete the 4 infrastructure files
          (``sandbox/egg_agent_tools/tools/{__init__,_common,_registry,_tool_compat}.py``).
          Delete the ``SYSTEM_PROMPT_NUDGE`` constant at
          ``sandbox/egg_agent_tools/server.py:61`` and the
          ``build_sandbox_mcp_server`` factory in the same file. The
          shared handler layer at
          ``sandbox/egg_agent_tools/handlers/*.py`` is RETAINED — both
          surfaces collapse to one (the CLI / direct handler path),
          not zero. ``server.py`` is reduced to whatever else lives
          there (only the MCP-specific exports — verify via grep at
          implement-time; if no non-MCP exports remain, delete
          ``server.py`` too).
        acceptance: |-
          ``rg 'from egg_agent_tools.tools|build_sandbox_mcp_server|SYSTEM_PROMPT_NUDGE'``
          across the tree returns zero matches; the handler layer
          at ``sandbox/egg_agent_tools/handlers/*.py`` unchanged;
          deletion lands in a single coder commit.
        role: coder
        files:
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/checkpoint.py
          - sandbox/egg_agent_tools/tools/message.py
          - sandbox/egg_agent_tools/tools/phase.py
          - sandbox/egg_agent_tools/tools/progress.py
          - sandbox/egg_agent_tools/tools/sdlc.py
          - sandbox/egg_agent_tools/tools/task.py
          - sandbox/egg_agent_tools/tools/__init__.py
          - sandbox/egg_agent_tools/tools/_common.py
          - sandbox/egg_agent_tools/tools/_registry.py
          - sandbox/egg_agent_tools/tools/_tool_compat.py
          - sandbox/egg_agent_tools/server.py
      - id: TASK-6-2
        description: |-
          Delete the MCP registration block in
          ``shared/egg_agent/client.py:299–353``: the
          ``EGG_MCP_TOOLS`` env-flag gate at :311 (no orphan flag —
          per architect v2 slice-6 goal "INCLUDING the EGG_MCP_TOOLS
          env-flag check at line 311"), the
          ``build_sandbox_mcp_server`` import at :316, the
          ``mcp_servers = build_sandbox_mcp_server()`` call at :319,
          the ``options.mcp_servers = {...}`` assignment at :323, and
          the ``SYSTEM_PROMPT_NUDGE`` append at :332. The operator-facing
          ``orchestrator/mcp_server.py`` is out of scope — confirm via
          grep that nothing in the deletion accidentally touches it.
        acceptance: |-
          ``shared/egg_agent/client.py`` no longer references MCP
          tools or the ``EGG_MCP_TOOLS`` env flag; client.py options
          no longer set ``mcp_servers`` (or sets only the operator-facing
          ``orchestrator/mcp_server.py`` if separately registered —
          confirm by grep); ``rg 'EGG_MCP_TOOLS'`` across the tree
          returns zero matches (no orphan references).
        role: coder
        files:
          - shared/egg_agent/client.py
      - id: TASK-6-3
        description: |-
          Retire ``tests/tools/test_mcp_cli_drift.py`` (delete; the
          MCP↔CLI drift contract no longer applies since the MCP
          surface is gone). The shared handler layer keeps both
          surfaces honest in spirit; the formal drift suite is
          retired.
        acceptance: |-
          ``tests/tools/test_mcp_cli_drift.py`` deleted; ``rg
          'test_mcp_cli_drift'`` across the tree returns zero
          matches; existing test suite remains green.
        role: tester
        files:
          - tests/tools/test_mcp_cli_drift.py
      - id: TASK-6-4
        description: |-
          Migrate ``integration_tests/test_sandbox_mcp_tools_e2e.py``
          to exercise the CLI surface. The architect v2 slice-6 goal
          specifies the test must "preserve the SDK-spawn exercise
          rather than collapsing to direct-handler" — the agent's
          first action becomes ``egg-orch consensus ack/nack`` via
          stdin/file (using the slice-5 prose plumbing from TASK-5-1).
          Where the original tests assert the MCP-tool surface
          (schema, registration, system-prompt-nudge), replace with
          equivalent assertions: subcommand exists,
          ``--help`` mirrors the expected fields, stdin/file round-trip
          works. Where they assert handler-layer behaviour, simplify
          to direct handler invocation. Migrate
          ``tests/sandbox/egg_agent_tools/test_server.py`` separately
          — the MCP-registration test goes away with the MCP server.
        acceptance: |-
          ``integration_tests/test_sandbox_mcp_tools_e2e.py`` exercises
          the CLI surface AND the SDK-spawn end-to-end (not just the
          handler layer); ``tests/sandbox/egg_agent_tools/test_server.py``
          no longer asserts MCP registration; both files pass under
          ``make test``; ``rg
          'from sandbox.egg_agent_tools.tools'`` in test paths returns
          zero matches.
        role: tester
        files:
          - integration_tests/test_sandbox_mcp_tools_e2e.py
          - tests/sandbox/egg_agent_tools/test_server.py
      - id: TASK-6-5
        description: |-
          Documenter: update ``docs/architecture/sandbox.md``,
          ``docs/reference/agent-tools.md`` (locate via Grep
          ``docs/`` for "MCP tools" / "SYSTEM_PROMPT_NUDGE" /
          "EGG_MCP_TOOLS"), and the project ``CLAUDE.md`` Quick
          Reference if it references the agent MCP surface. Cover:
          the MCP tool surface is retired in favour of the CLI; the
          ``EGG_MCP_TOOLS`` env flag is no longer recognised; the
          shared handler layer at
          ``sandbox/egg_agent_tools/handlers/*.py`` backs both
          today (CLI only after this slice); the operator-facing
          ``orchestrator/mcp_server.py`` is unaffected and remains the
          operator's MCP surface. The documenter role has direct
          write access to ``CLAUDE.md`` via the
          ``DEFAULT_DOCS_GLOBS`` ``**/*.md`` pattern at
          ``shared/egg_restrictions/patterns.py:177-181`` — no
          staging workaround needed (reviewer_plan non-blocker).
        acceptance: |-
          All references to the agent-side MCP tools updated to the
          CLI surface; ``EGG_MCP_TOOLS`` references removed;
          ``orchestrator/mcp_server.py`` references preserved;
          CLAUDE.md edited in place (if applicable) — no
          ``.egg-state/agent-outputs/`` staging detour.
        role: documenter
        files:
          - docs/architecture/sandbox.md
          - docs/reference/agent-tools.md
          - CLAUDE.md
      - id: TASK-6-6
        description: |-
          Per-event wall-clock latency verification at
          ``integration_tests/test_mcp_to_cli_latency.py``
          (directly under ``integration_tests/``; ``local_pipeline/``
          does not exist).

          **Baseline-capture strategy:** TASK-5-7 captures the
          baseline DURING slice-5 (before slice-6's deletions land)
          on the still-live MCP surface using the ``egg_stack``
          fixture and commits the result to
          ``.egg-state/agent-outputs/latency-mcp-baseline.json``.
          TASK-6-6 (this task) drives the SAME consensus shape on
          the post-deletion CLI-only surface — also via
          ``egg_stack`` (session-scoped at
          ``integration_tests/conftest.py:340``) — reads the
          slice-5-captured baseline, and asserts the comparison.
          Both measurements use the same real-LLM tier — the only
          delta is the tool surface.

          Latency regression budget: ≤ 5%. On regression > 5%,
          slice-6 surfaces a structured ``OVERSEER_ALERT`` priority
          ``medium`` with the measured delta for human review (the
          fallback decision is whether to ship the persistent
          ``egg-orch`` daemon per architect od-5).
        acceptance: |-
          ``latency-mcp-baseline.json`` exists under
          ``.egg-state/agent-outputs/`` at slice-6 entry (captured
          by TASK-5-7); the post-deletion measurement is captured to
          ``.egg-state/agent-outputs/latency-mcp-vs-cli.json``;
          assertion fails only if regression exceeds the 5% budget;
          on failure the test surfaces a structured
          ``OVERSEER_ALERT`` priority ``medium`` with the measured
          delta. No vendored MCP source tarball. No
          ``ScriptedProvider`` import or reference. Test gated
          via ``egg_stack`` (skips if ``_kubectl_available()``
          returns False).
        role: tester
        files:
          - integration_tests/test_mcp_to_cli_latency.py
      - id: TASK-6-7
        description: |-
          Capture **WS7 cache measurement #3** (post-slice-6) to
          ``.egg-state/agent-outputs/ws7-measurement-slice-6.json``.
          Run a representative event sequence through the
          post-slice-6 CLI-only surface on a local test pipeline and
          record ``cache_read_input_tokens`` / Qwen cost_callback
          aggregate. Compare against the WS7 #1 (slice-3) and WS7
          #2 (slice-4) baselines from
          ``.egg-state/agent-outputs/ws7-measurement-slice-{3,4}.json``;
          regression > 20% at this boundary triggers an
          ``OVERSEER_ALERT`` priority ``high`` asking the operator
          whether to roll back the MCP deletion (per risk_analyst R8
          cache-prefix invalidation concern).
        acceptance: |-
          WS7 #3 file written with the documented schema; comparison
          against the two prior measurement files surfaced in the
          test output; alert fires only if regression > 20%; on
          alert, the deletion is reversible by reverting TASK-6-1
          and TASK-6-2 (rollback path documented in the PR body).
        role: tester
        files:
          - integration_tests/test_ws7_cache_measurement.py
```


## HITL Resolution

The following was approved by a human reviewer at the plan phase gate:

APPROVED to implement, but the following operator corrections are BINDING and must be honored during implementation (the issue #2908 body was updated 2026-06-01 with a SCOPE UPDATE block that supersedes conflicting plan text — follow it):

1. NO live Qwen repro. slice-4's spike must NOT create or run test_event_pump_spike_2906.py (or any test) that drives a live qwen3.7-max route on k3s, and must NOT reproduce the #2906 fall-out in-pod. The egg agents run Opus and in-pod Qwen-trajectory repro is not runnable here. Replace slice-4's validation with: wrapper/unit coverage of the event-pump control flow + the durable safety-budget host-restart path + an Opus-route end-to-end that the deterministic loop reaches consensus without restart churn. The 'fixes Qwen' claim rests on the model-agnostic control-flow design, not an in-pod Qwen reproduction. Do NOT gate anything on a cache-TTL measurement (settled: both routes >=60min TTL, no keep-warm).

2. cq-3 is BINDING AS WRITTEN and was NOT honored by the plan. The no-progress safety budget + parked-HITL state MUST be durable SERVER-SIDE: a Pipeline.no_progress_budget field on the orchestrator-side Pipeline model + a sync-flush save variant (returns only after git push) + a startup-reconciliation replay that re-primes the budget after an SDLC-host restart. The plan's in-wrapper EGG_BRC_IDLE_BUDGET_MIN env-var budget is INSUFFICIENT — it is not robust to host death, which is the entire point of cq-3. Implement the durable server-side mechanism. Terminal state stays OVERSEER_ALERT + HITL, no auto-FAIL.

3. brc-memory.md is EPHEMERAL coordination state, NOT durable audit material (feedback-1 Q2). Recovery must NOT depend on the memory file surviving; the orchestrator message history + reconstruct_tracker_from_messages is the durable backstop. The plan repeatedly calls the memory artifact 'durable' — treat it as ephemeral; it can be cleaned up with the pod.

4. cq-4: delete the old capped-restart wrapper with NO flagged fallback. A short-lived flag to stage the cutover within this issue is acceptable ONLY if it is removed before the issue closes (drain-then-cutover), but do NOT ship a permanent/one-release EGG_BRC_EVENT_PUMP escape-hatch as the end state — the old path must be gone.

5. cq-1 reminder: build only the net-new CLI verbs the event-pump consumes; the MCP-tool deletion + prose-arg CLI migration (current slice-5/slice-6) is supposed to be a FOLLOW-UP issue, not this one. If the deletion slices remain, they must at minimum not delete the 28 MCP tools within this issue's scope. Prefer deferring slice-5/slice-6 (MCP collapse) to the follow-up and shipping the event-pump core.

Implement-phase reviewers: enforce these against each slice's acceptance criteria. Where a slice's plan text conflicts with the above, the above wins.
