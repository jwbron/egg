# Plan (replan2): BRC consensus event-pump + durable agent memory (#2908)

Decomposition of the architect's 5-slice linear chain
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
analysis (`slice_composition_rationale.linear_chain_not_parallel`):

```
slice-1 (foundations)
   ↓
slice-2 (event-pump wrapper, behind flag)
   ↓
slice-3 (delta + prompt collapse)
   ↓
slice-4 (spike + flag flip + delete old path)
   ↓
slice-5 (MCP → CLI tool collapse)
```

Each slice is a single tight BRC cycle (~5–8 producer tasks). The
chain matches the issue's stated rollout: **build behind a flag,
validate, flip default, delete old path**. Slice-5 is intentionally
last because the MCP surface is dead code only once the event pump
is the default.

## Primitives audit (#2594)

Every primitive the tasks below depend on, with file:line evidence.
All existence-citations were independently verified by an Explore
subagent at the HEAD of `egg/issue-2908-replan2/work` (commit
`b6088e988`).

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
| `cmd_consensus_propose` | `sandbox/egg_lib/orch_cli.py:2528` (accepts `--file PATH` for JSON payload) | in-sandbox-agent (CLI) |
| `cmd_consensus_ack` / `cmd_consensus_nack` | `sandbox/egg_lib/orch_cli.py:2633,2692` (today `--reason` is argv-only — slice-5 adds stdin/file alternative; cmd_consensus_withdraw `:2726` is same shape) | in-sandbox-agent (CLI) |
| Cursor on-disk path | `sandbox/egg_lib/orch_cli.py:1426` | in-sandbox-agent |
| `EGG_ORCHESTRATOR_URL` default | `sandbox/egg_lib/orch_cli.py:132` | in-sandbox-agent |
| `GATEWAY_URL` default | `sandbox/egg_lib/orch_cli.py:144` | in-sandbox-agent |
| `lifecycle_secret` arg (`EGG_LIFECYCLE_SECRET`) | `sandbox/egg_lib/orch_cli.py:324` | in-sandbox-agent |
| `EGG_AGENT_ROLE` / `EGG_PIPELINE_ID` env on pods | `orchestrator/kubernetes_spawner.py:818-823` | in-sandbox-agent (set on pod) |
| `JOB_NAME_FORMAT` | `orchestrator/kubernetes_spawner.py:332` | orchestrator pod (job spec) |
| `python3 -m egg_agent` entrypoint + `--max-turns` arg | `shared/egg_agent/__main__.py:36` (passes through to `run_agent`); `command.py:11` (`build_agent_command`) | in-sandbox-agent |
| `check_file_write_permission` | `shared/egg_agent/tool_interceptor.py:27` | in-sandbox-agent |
| `phase_filter.check_agent_restrictions` | `gateway/phase_filter.py:1058` | gateway pod |
| `validate_agent_push` | `shared/egg_restrictions/checker.py:98` | gateway pod |
| Role allowlists for `.egg-state/agent-outputs/` (prefix-matched; subdirs allowed) | `shared/egg_restrictions/patterns.py:362,382,436,516` plus coder `:231`, tester `:277`, documenter `:307` | gateway pod + in-sandbox-agent |
| `peer_consensus.py` aggregated-NACK payload `nacks[]` (issue refers to "aggregated_nacks" — the actual field name is `nacks`) | `orchestrator/peer_consensus.py:949-1024` (`_open_nacks_barrier_response`) | orchestrator pod |
| `changed_artifacts` ACK-invalidation hook | `orchestrator/peer_consensus.py:902-926` (`matrix.invalidate_overlapping_acks`) | orchestrator pod |
| `reconstruct_tracker_from_messages(..., slice_id=...)` | `orchestrator/peer_consensus.py:1955` (slice_id param at :1960) | orchestrator pod |
| `brc-history` writer path (no slice_id in main filename; sibling `<id>-implement-unattributed.json` is read-side only) | `sandbox/egg_agent_tools/handlers/brc.py:815,1019` | in-sandbox-agent / orchestrator |
| Qwen cost_callback file (cache instrumentation source) | `config/litellm/cost_callback.py:188` | trusted-CI / litellm pod |
| MCP tool files to delete (1,515 LOC across 7 files; 4 infra files retained per slice-5 scope) | `sandbox/egg_agent_tools/tools/{brc,checkpoint,message,phase,progress,sdlc,task}.py` | in-sandbox-agent |
| `tests/tools/test_mcp_cli_drift.py` (retire in slice-5) | `tests/tools/test_mcp_cli_drift.py` (12,905 B) | trusted-CI |
| `integration_tests/test_sandbox_mcp_tools_e2e.py` (migrate in slice-5) | `integration_tests/test_sandbox_mcp_tools_e2e.py` (5,252 B) | trusted-CI |
| `tests/sandbox/egg_agent_tools/test_server.py` (migrate in slice-5) | `tests/sandbox/egg_agent_tools/test_server.py` (8,164 B) | trusted-CI |
| `tests/sandbox/egg_agent_tools/test_handlers_brc.py` (extend in slice-1) | `tests/sandbox/egg_agent_tools/test_handlers_brc.py` | trusted-CI |
| `orchestrator/tests/test_consensus_wrapper.py` (extend in slice-2/4) | `orchestrator/tests/test_consensus_wrapper.py` (+ `test_consensus_wrapper_anchor.py`, `test_brc_nack_iteration.py`) | trusted-CI |

### New primitives (created by tasks in this plan)

The following symbols do **not** exist at HEAD; the named task creates
them, and downstream tasks order after the creating task per the
plan-reviewer §9 exception.

| Primitive | Created by | Form |
|---|---|---|
| `egg-orch brc next-action --role R [--json]` CLI subcommand | TASK-1-1 (CLI) backed by TASK-1-2 (route) | Subparser registered under existing `brc` parent (sibling of `brc ack`/`brc nack`). CLI returns JSON `{action: "wait"|"propose"|"ack"|"nack"|"confirm"|"complete", event_payload?: {...}}`. |
| `POST /api/v1/pipelines/{pid}/consensus/next-action` orchestrator route | TASK-1-2 | Route handler under `orchestrator/routes/` deriving next action from `consensus_status` + `nacks[]` aggregation + `changed_artifacts` delta. Returns same JSON the CLI surfaces. |
| `egg-orch brc get-state` CLI (verb-level alias for `consensus status --json`) | TASK-1-3 | Thin subcommand; sources from `brc_get_state` handler (`handlers/brc.py:679-723`). Matches the existing MCP tool name so the wrapper bash can call it directly. |
| `egg-orch brc list-blocking` CLI | TASK-1-4 | Derived view of `consensus.blocking_agents[]` — returns one role per line for shell consumption. |
| `egg-orch phase get-context` CLI | TASK-1-5 | Wraps `mcp__phase__get_context` handler. Returns JSON: pipeline_id, phase, role, assigned tasks, prior-phase artifacts. |
| `egg-orch progress complete` CLI | TASK-1-6 | Convenience: emits a structured `complete` progress event so the wrapper can mark `progress complete && exit 0`. Backed by existing `progress emit` handler with `state="complete"`. |
| `.egg-state/agent-outputs/<role>/brc-memory.md` durable memory artifact | TASK-1-7 (writer) + TASK-1-8 (schema doc) | Per-role-per-pipeline distilled memory file; scope key is `(role, slice_id?, phase)` matching brc-history scoping convention. Path uses subdirectory layout per architect's open-decision od-1 recommendation. |
| `EGG_BRC_MEMORY={off,write-only,full}` env-flag gate | TASK-1-7 | Read in `brc_ack` / `brc_nack` handlers; default `off` until slice-4 flips on. |
| `EGG_BRC_EVENT_PUMP={true,false}` env-flag gate | TASK-2-1 (template branch) and TASK-4-2 (flip default) | Read in `build_consensus_wrapped_command` at template-composition time; selects new event-pump bash branch vs old capped-restart branch. Default false in slice-2, flipped true in slice-4. |
| Idle/no-progress safety budget (env `EGG_BRC_IDLE_BUDGET_MIN`, default 30) | TASK-2-3 | Replacement for `MAX_CONSENSUS_RESTARTS` cap; trips overseer alert. |
| Wrapper-side heartbeat emitter | TASK-2-4 | Wrapper bash emits `egg-orch message heartbeat` (existing endpoint) every 30s while `message wait-loop` is blocking. Migrated *from* `handlers/message.py:267-429`. |
| Wrapper-side gateway-session keep-alive | TASK-2-5 | Wrapper bash refreshes the lifecycle-secret-gated session while blocking. Migrated from same handler region (#2451). |
| Per-event prompt composer (`compose_event_prompt`) | TASK-3-1 | New helper that builds the single-event prompt: role + event_payload + memory excerpt + NACK delta + changed_artifacts. Lives next to `_build_brc_preamble`. |
| `egg-orch brc resolve-obligation` CLI | TASK-5-1 | Wraps existing `mcp__brc__resolve_obligation` handler. |
| `egg-orch brc read-peer-artifact` CLI | TASK-5-2 | Wraps existing `mcp__brc__read_peer_artifact` handler. Stdout JSON; supports `--limit`, `--cursor`, `--phase`, `--peer-role`. |
| `--reason-file PATH` / stdin sentinel `-` on `consensus ack/nack/withdraw` | TASK-5-3 | Hard constraint from #2741: prose args must NOT flow through `bash -c` argv. Reuses the `propose --file` pattern at `orch_cli.py:2552`. |

## Trust-boundary scope (#10)

The architect-output `runtime_primitive_assumptions` tagged each
primitive on the in-sandbox-agent vs trusted-CI axis. Highlights
relevant to this plan:

- **Wrapper bash runs in-sandbox-agent** — every CLI command the
  wrapper invokes (`brc get-state`, `brc next-action`,
  `message wait-loop`, `progress complete`) inherits the role's
  file-write restrictions via `tool_interceptor.check_file_write_permission`
  (`shared/egg_agent/tool_interceptor.py:27`) and the gateway-side
  push guard (`gateway/phase_filter.py:1058`).
- **The memory file lives in `.egg-state/agent-outputs/<role>/`**,
  inside every participant role's allowlist. The subdirectory layout
  passes existing prefix-pattern matching (verified at
  `shared/egg_restrictions/patterns.py` — `match_pattern` treats
  trailing-slash directory patterns as recursive prefixes).
- **Spike validation in slice-4** is a trusted-CI / k3s-cluster
  exercise — the #2906 repro uses `local_pipeline/` fixtures
  (`integration_tests/local_pipeline/conftest.py:261` is the only
  `gateway_url` pytest fixture; kubectl-gated via `local_pipeline_stack`).
  No agent-side pytest fixture is in-sandbox-agent-runnable today;
  the spike's checks run on the cluster orchestrator, not inside the
  agent pod under test. See
  `docs/architecture/integration-test-trust-boundary.md`.
- **Qwen cache instrumentation** sources from
  `~/.local/state/clm/cost-*.json` files on the litellm pod (per
  `config/litellm/cost_callback.py:188`); the Anthropic-route counter
  rides on `usage.cache_read_input_tokens` from the SDK result. Slice-4
  reads from both.

## Test strategy

Every slice carries its own unit + integration tests. Per slice:

- **slice-1**: unit tests for next-action derivation across producer /
  reviewer / dual-role incl. open-NACK barrier (#2142), stale-version
  (#2482); memory-write side-effects of `brc_ack` / `brc_nack`
  (action-scaffolded fields populated); CLI round-trip tests for the
  five new subcommands with lifecycle-secret auth.
- **slice-2**: wrapper template snapshot test for event-pump branch;
  wrapper-side heartbeat unit test (mock `egg-orch message heartbeat`);
  idle-budget overseer-alert test at configured threshold; full
  regression run with `EGG_BRC_EVENT_PUMP=false` (default) on
  `integration_tests/regression/test_brc_*.py` to prove zero
  regression; with flag on, ScriptedProvider-driven E2E completes
  consensus without the 3-cap.
- **slice-3**: unit tests for `compose_event_prompt`; snapshot test
  for the collapsed `_build_brc_preamble`; per-event prompt size
  ≤ 10 KB assertion; regression run with flag on.
- **slice-4**: the spike itself is the integration test — k3s repro
  of #2906 (issue-2270, qwen3.7-max) end-to-end with
  `EGG_BRC_EVENT_PUMP=true` and `EGG_BRC_MEMORY=full`; assertions:
  consensus reaches CONFIRMED, no restart churn, memory populated
  + consulted, per-event context bounded, `cache_read` observed on
  both routes. Cost-baseline comparison: input + cache-read tokens
  vs restart-churn baseline from #2806.
- **slice-5**: all BRC actions reachable via CLI with no agent MCP
  server registered (`EGG_MCP_TOOLS` permanently false then deleted);
  prose-arg stdin/file round-trip uncorrupted (#2741 regression
  guard); per-event wall-clock latency unchanged within 5% margin on
  representative event sample; full regression pass.

**Manual verification on slice-4** (recorded in `manual_steps`):
human inspects the spike run's brc-memory.md output to confirm
content reflects actual reasoning, not boilerplate; human reviews
cost-per-phase delta and consents to flag flip.

## Manual pre/post-merge steps

- **slice-1 → slice-4**: no pre-merge or post-merge manual steps;
  feature flags default off, so all changes are inert in production.
- **slice-4 pre-merge**: human reviews spike output (brc-memory
  content + cost delta) before approving the default-on flip.
- **slice-4 post-merge**: monitor 24h of production BRC traffic for
  any "Agent exited without BRC consensus" log entries; if seen,
  flip `EGG_BRC_EVENT_PUMP=false` via deployment env and re-open the
  slice.
- **slice-5 post-merge**: no in-flight pipelines must be running
  against an agent built before slice-5 — the MCP tools are deleted,
  so an old wrapper that still injects them will start with no
  MCP server registered. Gate the deployment on in-flight-pipeline
  drain or cancel.

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
slice-5 (MCP → CLI tool collapse)
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
    in five linear slices behind `EGG_BRC_EVENT_PUMP` (default false
    until slice-4):

    1. **slice-1 — Foundations.** New `egg-orch brc next-action`,
       `brc get-state`, `brc list-blocking`, `phase get-context`,
       `progress complete` CLI subcommands. Durable BRC memory artifact
       at `.egg-state/agent-outputs/<role>/brc-memory.md` with
       action-scaffolded writes into `brc_ack` / `brc_nack` handlers,
       gated by `EGG_BRC_MEMORY=write-only` (writes accumulate, no
       readers yet). Purely additive — zero behavior change.
    2. **slice-2 — Event-pump wrapper.** Rewrite
       `orchestrator/consensus_wrapper.py` as a deterministic event
       pump gated by `EGG_BRC_EVENT_PUMP`. Drop the 3-restart cap;
       replace with idle/no-progress safety budget. Migrate the
       heartbeat (#2036) and gateway-session keep-alive (#2451) from
       the agent-side handler into the wrapper's blocking wait. Old
       path retained verbatim alongside.
    3. **slice-3 — Delta + prompt collapse.** Wire per-event
       invocation to hand the agent the memory delta plus the
       orchestrator's existing `nacks[]` + `changed_artifacts` delta,
       so re-proposals re-analyze only the delta. Strip the
       STAY-ALIVE / wait-loop / cursor-threading guidance from
       `_build_brc_preamble` and `mission.md`; replace with a lean
       event-handler contract.
    4. **slice-4 — Spike + flag flip + delete old path.** Run the
       #2906 repro on k3s with `EGG_BRC_EVENT_PUMP=true` and
       `EGG_BRC_MEMORY=full`; confirm consensus, instrumented
       `cache_read`, populated memory, bounded per-event context.
       Flip flag default to on; delete the capped-restart bash, the
       `_RECOVERY_SYSTEM_PROMPT`, the SSE machinery, and the
       agent-side wait_loop heartbeat code.
    5. **slice-5 — MCP → CLI tool collapse.** Delete the 28
       agent-facing MCP tools (~1,515 LOC across 7 namespace files)
       plus the `SYSTEM_PROMPT_NUDGE` and the MCP registration block.
       Build CLI parity for the ~10 still-MCP-only commands
       (`brc resolve-obligation`, `brc read-peer-artifact`, etc.).
       Hard constraint: prose-bearing args (`reason`,
       `files_reviewed`) flow via stdin or `--reason-file PATH`,
       never argv (#2741 regression guard). Migrate the MCP↔CLI
       drift test and the MCP E2E tests to direct-handler tests
       against the shared handler layer.

    ## Impact

    Operator-facing: the BRC consensus subsystem becomes
    model-portable — any agent that exits naturally after handling one
    event reaches CONFIRMED, instead of needing prompt nudges to keep
    re-entering an in-process wait. Cost-per-phase drops because
    restart churn (which doubled or tripled token spend on failures)
    disappears, replaced by short bounded per-event invocations that
    hit the prefix cache (≥ 60-min TTL on both routes per WS7
    closure). Net code deletion: the capped-restart template, the
    SSE machinery, the recovery system prompt, the 28 MCP tool
    schemas, the cursor-threading guidance, and the agent-side
    heartbeat all go away. The agent primitive (pod / worktree / SDK
    / permissions / restrictions) is untouched.
  test_plan: |
    Per-slice automated coverage:
    - slice-1: unit tests for next-action derivation (producer /
      reviewer / dual-role / open-NACK barrier #2142 /
      stale-version #2482), memory-write side-effects on
      `brc_ack`/`brc_nack`, CLI round-trip with lifecycle-secret auth.
    - slice-2: wrapper template snapshot for event-pump branch;
      wrapper-side heartbeat unit test; idle-budget overseer-alert
      threshold test; full BRC regression with flag off; ScriptedProvider
      E2E with flag on.
    - slice-3: `compose_event_prompt` unit tests; collapsed
      preamble snapshot; per-event prompt size ≤ 10 KB assertion;
      regression with flag on.
    - slice-4: k3s integration test on the #2906 repro
      (issue-2270, qwen3.7-max) with flag and memory both full;
      assertions on CONFIRMED, no restart churn, memory populated,
      cache_read instrumented on both routes
      (Anthropic via SDK usage, Qwen via cost_callback files).
    - slice-5: BRC actions reachable via CLI with no agent MCP
      server registered; prose-arg stdin/file round-trip
      uncorrupted (#2741 regression); per-event wall-clock latency
      within 5% of pre-slice baseline.

    Manual verification:
    - slice-4 pre-flag-flip: human inspects spike output
      (brc-memory.md content for reasoning fidelity; cost-per-phase
      delta vs restart-churn baseline) and consents to default-on
      flip via PR review.
    - slice-5 pre-merge: human verifies no in-flight pipelines are
      mid-run against pre-slice agents; gate deploy on drain or
      cancel.
  manual_steps: |
    Pre-merge: slice-4 requires human review of spike output before
    the `EGG_BRC_EVENT_PUMP` default flips to true. Slice-5 requires
    confirmation that no in-flight pipelines exist before deploy.

    Post-merge: slice-4 post-merge monitor 24h of production BRC
    traffic for any "Agent exited without BRC consensus" entries;
    fall back via `EGG_BRC_EVENT_PUMP=false` deployment env if seen.
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
          ``egg-orch brc next-action --role coder --json`` against a
          ScriptedProvider-backed pipeline returns the documented JSON
          shape; subcommand registered with ``--help`` output describing the
          ``--role`` and ``--json`` flags; lifecycle-secret auth tested
          (rejected without env var).
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
          (role, BRC-state) combination: producer-PROPOSED, reviewer with
          pending proposal, dual-role mid-review, open-NACK barrier
          (#2142) blocking re-propose, stale-version (#2482) requiring
          re-review, confirmation eligible, role complete.
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
          Add ``egg-orch progress complete`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. Thin wrapper that emits a
          structured ``complete`` progress event via the existing
          ``progress emit`` handler with ``state="complete"``. The wrapper
          bash loop will call this once the role is confirmed before
          exiting cleanly. Existing ``cmd_progress_emit`` lives at
          orch_cli.py:2908; the new command shells through it with the
          appropriate args set.
        acceptance: |-
          ``egg-orch progress complete`` emits a single progress event
          with ``state=complete`` and ``step=role-complete``; structured
          event visible in ``progress query`` output.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-1-7
        description: |-
          Add durable BRC memory writer to ``brc_ack`` and ``brc_nack``
          handlers at ``sandbox/egg_agent_tools/handlers/brc.py:505,586``.
          On a successful ACK or NACK, distill a structured memory entry
          (action-scaffolded from existing ``reason`` + ``files_reviewed``
          fields plus producer / version / timestamp) into
          ``.egg-state/agent-outputs/<role>/brc-memory.md`` (subdirectory
          layout per architect od-1 recommendation; path resolved against
          ``EGG_REPO_PATH``). Scope key is (role, slice_id?, phase) to
          match brc-history scoping (handlers/brc.py:815). Gated by
          ``EGG_BRC_MEMORY={off,write-only,full}``: ``off`` skips
          writes; ``write-only`` writes but does not read; ``full``
          enables reads (read path lands in slice-3). Default ``off`` so
          slice-1 is inert in production. Memory shape: distill /
          rewrite each event (architect od-2 recommendation; bounded
          per-event size).
        acceptance: |-
          ``brc_ack`` and ``brc_nack`` calls with
          ``EGG_BRC_MEMORY=write-only`` produce a well-formed memory
          file with one entry per call; entries include reviewer role,
          producer, version, reason, files_reviewed, timestamp;
          ``EGG_BRC_MEMORY=off`` produces no file; existing handler
          return values unchanged in all cases (no behavior change for
          callers); subdirectory created if absent.
        role: coder
        files:
          - sandbox/egg_agent_tools/handlers/brc.py
      - id: TASK-1-8
        description: |-
          Document the BRC memory schema and layout in
          ``docs/architecture/brc-memory.md`` (new doc). Cover: file path
          (``.egg-state/agent-outputs/<role>/brc-memory.md``), scope key,
          structured per-entry shape (action-scaffolded fields), the
          three ``EGG_BRC_MEMORY`` modes, the rationale for distill-on-write
          (architect od-2), the read-side semantics that land in slice-3,
          and the role-allowlist coverage that makes the path writable
          for every participant role (cite patterns.py line ranges).
        acceptance: |-
          Doc renders cleanly; cross-linked from
          ``docs/architecture/index.md`` and from the consensus subsystem
          README; covers the four sections above; reviewers can locate
          all referenced primitives by file:line.
        role: documenter
        files:
          - docs/architecture/brc-memory.md
          - docs/architecture/index.md
      - id: TASK-1-9
        description: |-
          Unit tests for TASK-1-1..TASK-1-6 CLI subcommands at
          ``tests/sandbox/egg_lib/test_orch_cli_brc.py`` and
          ``tests/sandbox/egg_lib/test_orch_cli_phase.py`` (new files
          alongside the existing ``test_orch_cli_*.py`` suite). Cover
          ``--json`` output shape, lifecycle-secret auth, exit codes,
          empty-list edge cases for ``list-blocking``. Round-trip
          against an in-memory orchestrator fake.
        acceptance: |-
          Tests pass under ``make test``; coverage of each subcommand's
          happy path, auth-missing path, and one edge case
          (empty/blocking-agents-empty for list-blocking; stale-version
          for next-action; verbose mode for get-state).
        role: tester
        files:
          - tests/sandbox/egg_lib/test_orch_cli_brc.py
          - tests/sandbox/egg_lib/test_orch_cli_phase.py
      - id: TASK-1-10
        description: |-
          Unit tests for TASK-1-7 memory writer extending
          ``tests/sandbox/egg_agent_tools/test_handlers_brc.py``.
          Cover: ``EGG_BRC_MEMORY={off,write-only,full}`` modes;
          well-formed entry on ack; well-formed entry on nack; idempotent
          append (calling ack twice does not corrupt prior entries);
          subdirectory creation; scope key per (role, slice_id, phase);
          handler return values unchanged.
        acceptance: |-
          Tests pass under ``make test``; one test per ``EGG_BRC_MEMORY``
          mode plus the idempotency / subdirectory / scope tests.
        role: tester
        files:
          - tests/sandbox/egg_agent_tools/test_handlers_brc.py
      - id: TASK-1-11
        description: |-
          Unit tests for TASK-1-2 orchestrator route at
          ``orchestrator/tests/test_consensus_next_action.py`` (new file).
          Cover the seven derivation cases listed in TASK-1-2 acceptance
          (producer-PROPOSED, reviewer-pending, dual-role-mid-review,
          open-NACK barrier #2142, stale-version #2482, confirm eligible,
          role complete) using ScriptedProvider-driven fixtures from
          ``orchestrator/tests/conftest.py``.
        acceptance: |-
          Tests pass under ``make test``; route handler logic exercised
          for each documented (role, BRC-state) combo; 200 status with
          expected JSON shape verified per case.
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
          orch_cli.py:1695) or invokes ``python3 -m egg_agent``
          (existing entrypoint at ``shared/egg_agent/__main__.py:36``)
          one-shot with the composed event prompt (composer lands in
          slice-3; slice-2 ships a minimal prompt stub). Wrapper
          handles 409 ``stale_version`` and 409 aggregated-NACK from
          ``brc next-action`` as event-pump signals (re-fetch state,
          re-invoke), NOT as transient crashes to retry with backoff.
        acceptance: |-
          With ``EGG_BRC_EVENT_PUMP`` unset: ``build_consensus_wrapped_command``
          emits the existing template byte-for-byte (regression-tested
          via existing snapshot); existing ``orchestrator/tests/test_consensus_wrapper.py``
          passes unchanged. With ``EGG_BRC_EVENT_PUMP=true``: emitted
          bash loop matches the new template; loop terminates on
          ``role_complete=true`` and calls ``egg-orch progress complete``
          before exiting 0; loop handles 409 stale_version by
          re-fetching state without backoff.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-2-2
        description: |-
          Migrate the heartbeat (#2036) emission out of
          ``sandbox/egg_agent_tools/handlers/message.py:267-429``
          (``message_wait_loop`` handler) into the event-pump
          wrapper bash template added in TASK-2-1. The wrapper emits
          ``egg-orch message heartbeat`` (existing CLI; verify
          subcommand surface at orch_cli.py — currently
          ``cmd_message_heartbeat`` per the audit) every 30 s as a
          background subshell while ``egg-orch message wait-loop`` is
          blocking. Keep the agent-side heartbeat path in the *old*
          template path (``EGG_BRC_EVENT_PUMP`` unset) verbatim; only
          the new template owns wrapper-side heartbeating. Slice-4
          deletes the agent-side path once the flag flips to default.
        acceptance: |-
          New template emits ``egg-orch message heartbeat`` every 30 s
          while wait-loop is blocking (verified by mock + clock
          fast-forward unit test); old template path unchanged
          (existing tests pass); structured progress query shows
          wrapper-side heartbeats only when flag is on.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-2-3
        description: |-
          Replace the ``MAX_CONSENSUS_RESTARTS = 3`` cap
          (consensus_wrapper.py:38) with an idle / no-progress safety
          budget driven by env ``EGG_BRC_IDLE_BUDGET_MIN`` (default 30
          minutes per architect od-4 recommendation; well above the
          WS7-observed 10–13 min idle ceiling). When the new template
          path is active and no actionable event has arrived for the
          budget duration, emit ``mcp__progress__overseer_alert``
          (anomaly ``stuck-phase-transition``, priority ``high``) and
          continue blocking; if no progress for 2× budget, raise the
          alert priority and continue. The old template path keeps
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
          versions; how the idle budget replaces the restart cap; the
          409 stale_version / aggregated-NACK handling. Mark the
          flag-off path as the temporary default until slice-4 flips
          it. Do NOT update ``mission.md`` yet (slice-3 owns that
          rewrite).
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
          template selection branches for both flag values (snapshot
          test); wrapper-side heartbeat cadence (mock subprocess +
          fast-forward); wrapper-side keep-alive cadence; idle budget
          alert at configured threshold; 409 stale_version handled as
          re-fetch (not retry-with-backoff); ``role_complete=true``
          path calls ``progress complete`` and exits 0. The
          existing 3-cap tests must continue to pass with flag off.
        acceptance: |-
          All tests pass under ``make test``; flag-off snapshot
          matches existing template byte-for-byte; flag-on snapshot
          shows expected new bash loop; both heartbeat and keep-alive
          cadence tests pass deterministically (no flaky sleeps).
        role: tester
        files:
          - orchestrator/tests/test_consensus_wrapper.py
      - id: TASK-2-7
        description: |-
          Regression integration test pass: with
          ``EGG_BRC_EVENT_PUMP=false`` (default), run
          ``integration_tests/regression/test_brc_*.py`` and assert
          green. With ``EGG_BRC_EVENT_PUMP=true`` + ScriptedProvider,
          add ``integration_tests/test_event_pump_e2e.py`` that
          drives a 2-role consensus to CONFIRMED through the new
          wrapper loop, with no restart-cap intervention. Test
          fixtures inherit from
          ``integration_tests/conftest.py::EggStack``.
        acceptance: |-
          Existing regression suite green with flag off; new E2E test
          reaches CONFIRMED via the event-pump loop with flag on;
          assertion that ``MAX_CONSENSUS_RESTARTS`` cap is NOT hit in
          the flag-on path (the cap logic is bypassed by template
          selection).
        role: tester
        files:
          - integration_tests/test_event_pump_e2e.py
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
          nacks_delta, changed_artifacts) -> str`` helper to
          ``orchestrator/routes/pipelines.py`` (or a new sibling module if
          the file is at the size limit — coder's call). Returns the
          single-event prompt the wrapper invokes the agent with. Shape:
          role banner + one-line event description + small memory excerpt
          (≤ 2 KB) + NACK delta from the ``nacks[]`` payload (peer_consensus.py:949-1024)
          + ``changed_artifacts`` list + the single action expected.
          Total budget ≤ 10 KB. Used by the event-pump template added in
          TASK-2-1 — the slice-2 prompt stub is replaced here.
        acceptance: |-
          Helper unit-tested for each role (producer / reviewer /
          dual-role); output ≤ 10 KB for representative event payloads;
          composer correctly truncates memory excerpts that exceed the
          per-event budget; NACK delta is per-reviewer (one entry per
          NACKing reviewer) with reason + artifact_refs.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-2
        description: |-
          Wire the event-pump template branch from TASK-2-1 to call
          ``compose_event_prompt`` (TASK-3-1) at per-event invocation
          time, reading the memory excerpt from
          ``.egg-state/agent-outputs/<role>/brc-memory.md`` (slice-1
          writer) when ``EGG_BRC_MEMORY=full`` (read mode; gated by
          the existing slice-1 env flag). With ``EGG_BRC_MEMORY=write-only``
          (slice-1 default), pass empty memory_excerpt — writes happen
          but reads are no-ops, preserving slice-1's inert default.
        acceptance: |-
          Wrapper template emits expected ``compose_event_prompt``
          invocation; with ``EGG_BRC_MEMORY=full`` and a populated
          memory file, the prompt includes the memory excerpt; with
          ``EGG_BRC_MEMORY=write-only`` (slice-1 default), the prompt
          omits memory; snapshot test verifies both branches.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-3-3
        description: |-
          Collapse ``_build_brc_preamble`` at
          ``orchestrator/routes/pipelines.py:12348``. Delete the
          STAY-ALIVE / wait-loop mechanics / cursor-threading / pre-confirm-wait
          foot-gun guidance (Producer Lifecycle step 4 wait-loop
          subordinated to CONSENSUS_PROPOSE/CONSENSUS_ACK/CONSENSUS_NACK/STATUS
          plumbing; Producer step 6 STAY-ALIVE loop; cursor / ``--since``
          guidance). KEEP: agent roster, reviewer/producer assignments,
          dual-role ordering banner (these are not seam-related). The
          three callers at ``orchestrator/routes/pipelines.py:13659,
          :13692, :13720`` are unchanged — only the preamble text
          collapses. Slice-3 keeps the flag off by default so the
          collapsed preamble runs against the *legacy* wrapper path
          today; slice-4 makes it the default once the event-pump path
          is live.
        acceptance: |-
          Snapshot test for the collapsed preamble lands at
          ``orchestrator/tests/test_brc_preamble_collapsed.py``;
          STAY-ALIVE / wait-loop / cursor sections absent; roster +
          assignments preserved; preamble byte size drops by ≥ 40%
          (measured against pre-collapse snapshot).
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-4
        description: |-
          Rewrite the STAY-ALIVE / wait-loop section of
          ``sandbox/agent-config/rules/mission.md`` (lines 151–154 plus
          surrounding "Concurrent Execution Mode" section starting at
          line 137) to the event-handler contract: the agent is invoked
          one-shot per event by the wrapper; act on the single event,
          update memory, exit naturally. Remove "never exit before the
          orchestrator stops you" — under the new model the wrapper
          owns lifecycle, not the agent. Keep the Anti-Sycophancy /
          Structured-Progress-Reporting / HITL-vs-OVERSEER_ALERT /
          Handling-Agent-Failures sections unchanged.
        acceptance: |-
          ``mission.md`` reflects event-handler semantics; the "stay
          alive" / "wait-loop" / "never exit" lines are replaced with
          the event-handler contract; the other four sections
          unchanged; doc renders cleanly.
        role: documenter
        files:
          - sandbox/agent-config/rules/mission.md
      - id: TASK-3-5
        description: |-
          Documenter: update ``docs/architecture/orchestrator.md`` (the
          BRC subsystem section) and ``docs/reference/agent-wait-patterns.md``
          to describe the delta-scoped re-analysis behaviour and the
          per-event prompt shape. Cover the architect's open-decision
          resolutions: od-1 (subdirectory layout, slice-1 lands), od-2
          (distill memory shape), od-3 (new ``brc next-action`` endpoint,
          slice-1 lands), od-4 (30-min idle budget). Link to the
          new ``docs/architecture/brc-memory.md`` from slice-1.
        acceptance: |-
          Both docs reflect slice-3 changes; open-decision resolutions
          documented with their slice-1/slice-2 implementation
          citations; cross-links to brc-memory.md present.
        role: documenter
        files:
          - docs/architecture/orchestrator.md
          - docs/reference/agent-wait-patterns.md
      - id: TASK-3-6
        description: |-
          Unit tests for TASK-3-1 ``compose_event_prompt`` at
          ``orchestrator/tests/test_compose_event_prompt.py``. Cover:
          each role's prompt shape; memory excerpt truncation at
          budget; NACK delta with 0 / 1 / 2+ reviewers; changed_artifacts
          with 0 / 1 / many entries; total prompt ≤ 10 KB assertion
          per case.
        acceptance: |-
          Tests pass under ``make test``; one test per role; budget
          assertion verified per case.
        role: tester
        files:
          - orchestrator/tests/test_compose_event_prompt.py
      - id: TASK-3-7
        description: |-
          Snapshot test for the collapsed preamble at
          ``orchestrator/tests/test_brc_preamble_collapsed.py`` (new
          file). Loads the rendered preamble for each of the three
          caller sites (pipelines.py:13659, :13692, :13720) and asserts
          (a) the new snapshot matches; (b) STAY-ALIVE / wait-loop /
          cursor strings absent; (c) agent roster present; (d) byte
          size drop ≥ 40% vs the prior snapshot baseline.
        acceptance: |-
          Tests pass under ``make test``; snapshots committed; absent
          assertions trigger on regression; byte-size assertion stable
          (with a 5% tolerance band).
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
          ``integration_tests/local_pipeline/test_event_pump_spike_2906.py``
          (or equivalent under ``local_pipeline/``; the fixture
          ``gateway_url`` from
          ``integration_tests/local_pipeline/conftest.py:261`` is the
          only one usable here). Run the #2906 repro: issue-2270,
          qwen3.7-max provider configuration, ``EGG_BRC_EVENT_PUMP=true``,
          ``EGG_BRC_MEMORY=full``, default idle-budget. Assertions:
          (a) consensus reaches CONFIRMED for every role within the
          pipeline; (b) no ``Agent exited without BRC consensus`` log
          entry; (c) brc-memory.md populated with ≥ 1 ACK/NACK entry
          per participating reviewer; (d) per-event prompt size ≤
          10 KB (assert via captured agent invocations); (e)
          ``cache_read_input_tokens`` > 0 on Anthropic route via SDK
          usage capture; (f) Qwen-route cache read > 0 via
          ``~/.local/state/clm/cost-*.json`` aggregation.
        acceptance: |-
          Test passes against k3s with the qwen3.7-max provider;
          assertions a–f all green; test runs under ``make test-all``
          on the local_pipeline fixture stack (kubectl-gated).
        role: tester
        files:
          - integration_tests/local_pipeline/test_event_pump_spike_2906.py
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
          in production.
        acceptance: |-
          ``build_consensus_wrapped_command`` with unset env emits the
          new template; with explicit ``EGG_BRC_EVENT_PUMP=false`` emits
          the old template (one-release rollback path preserved);
          existing snapshot tests updated to reflect the new default;
          BRC integration suite passes on the new default.
        role: coder
        files:
          - orchestrator/consensus_wrapper.py
      - id: TASK-4-3
        description: |-
          Delete the old capped-restart bash template, the
          ``_RECOVERY_SYSTEM_PROMPT`` (consensus_wrapper.py:64-99), the
          SSE ``consensus.reached`` machinery (consensus_wrapper.py:418-449),
          and the ``MAX_CONSENSUS_RESTARTS`` constant + use-sites. Keep
          ``is_buffer_overflow`` / ``is_transient_crash`` /
          ``is_startup_failure`` classifiers — they're still valid
          signals under the new idle/no-progress safety budget. Delete
          the agent-side wait_loop heartbeat path from
          ``sandbox/egg_agent_tools/handlers/message.py:267-429`` —
          the wrapper now owns heartbeating (TASK-2-2). Same for the
          gateway-session keep-alive in the same region (TASK-2-4
          migrated; this task deletes the agent-side path).
        acceptance: |-
          ``orchestrator/consensus_wrapper.py`` no longer contains
          ``MAX_CONSENSUS_RESTARTS``, ``_RECOVERY_SYSTEM_PROMPT``, or
          SSE / consensus.reached strings; ``handlers/message.py`` no
          longer emits heartbeats or refreshes the gateway session;
          the three crash classifiers remain; relevant tests in
          ``orchestrator/tests/test_consensus_wrapper.py`` updated
          (or deleted, where the old-path-specific tests no longer
          apply) — replacement coverage lands in TASK-4-4.
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
          retired capped-restart cap, recovery prompt, SSE path, and
          agent-side heartbeat / keep-alive. Add coverage for the
          new wrapper behaviour where it replaces the old (idle-budget
          test from slice-2 becomes the canonical liveness coverage).
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
          Cross-link to ``docs/architecture/brc-memory.md`` from
          slice-1.
        acceptance: |-
          Doc reads as if the event pump has always been the only
          model; legacy-path caveats removed; cross-links present;
          rendering clean.
        role: documenter
        files:
          - docs/architecture/orchestrator.md
  - id: 5
    name: |-
      MCP→CLI tool collapse (delete agent MCP server, migrate tests)
    goal: |-
      Delete the 28 agent-facing MCP tools (``sandbox/egg_agent_tools/tools/*.py``,
      ~1700 LOC across 11 files), the ``SYSTEM_PROMPT_NUDGE`` block at
      ``sandbox/egg_agent_tools/server.py:61``, and the MCP registration block at
      ``shared/egg_agent/client.py:299–353``. Build CLI parity for the ~10 tools
      not yet covered (``egg-orch brc resolve-obligation``,
      ``egg-orch brc read-peer-artifact``; ``get-state`` / ``list-blocking`` /
      ``phase get-context`` already shipped in slice-1). Hard constraint:
      prose-bearing CLI commands (``consensus propose/ack/nack``'s ``reason`` /
      ``files_reviewed`` args) must accept text via stdin or ``--file PATH`` —
      never argv — to avoid re-introducing the shell-metachar corruption
      mitigated in #2741. Retire / repurpose
      ``tests/tools/test_mcp_cli_drift.py``; migrate
      ``integration_tests/test_sandbox_mcp_tools_e2e.py`` and
      ``tests/sandbox/egg_agent_tools/test_server.py`` to direct-handler tests
      against the shared handler layer (which keeps both surfaces honest).
      Verify per-event wall-clock latency is unchanged (subprocess spawn vs
      in-process MCP dispatch on a representative event sample). Operator-facing
      ``orchestrator/mcp_server.py`` is out of scope.
    dependencies:
      - slice-4
    tasks:
      - id: TASK-5-1
        description: |-
          Add ``egg-orch brc resolve-obligation`` CLI subcommand to
          ``sandbox/egg_lib/orch_cli.py``. Wraps the existing
          ``mcp__brc__resolve_obligation`` handler in
          ``sandbox/egg_agent_tools/handlers/brc.py``. Args:
          ``--reviewer-role``, ``--producer-role``, ``--commit-sha``
          (optional), ``--note`` (optional; via stdin or
          ``--note-file PATH`` per the #2741 prose-arg rule).
        acceptance: |-
          CLI subcommand registered; round-trip against the orchestrator
          succeeds; help text mirrors the MCP-tool description; prose
          ``--note`` exercised via stdin and via ``--note-file PATH``.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-5-2
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
          slice-scoped and unattributed reads; pagination tested with
          ``--limit`` + ``--cursor`` round-trip.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-5-3
        description: |-
          Add stdin / file alternative for prose-bearing args on
          ``cmd_consensus_ack`` (orch_cli.py:2633),
          ``cmd_consensus_nack`` (orch_cli.py:2692), and
          ``cmd_consensus_withdraw`` (orch_cli.py:2726). Today the
          ``--reason`` arg is argv-only — this re-introduces the
          shell-metachar corruption mitigated in #2741 when the
          wrapper bash composes the command. Reuse the
          ``--file PATH`` pattern from ``cmd_consensus_propose``
          (orch_cli.py:2552). New flags: ``--reason-file PATH`` and
          stdin sentinel ``--reason -``. ``cmd_brc_*`` ``files_reviewed``
          args same shape. Keep argv ``--reason`` working for now
          (deprecation lives in a separate cycle) but emit a warning
          when used.
        acceptance: |-
          ``--reason-file PATH`` round-trips a multi-line UTF-8 reason
          containing shell metacharacters intact (``$(`` , backticks,
          ``;`` , newlines); stdin sentinel works for ``echo … | egg-orch
          consensus ack --reason -``; argv path emits deprecation
          warning; #2741 regression guard test passes.
        role: coder
        files:
          - sandbox/egg_lib/orch_cli.py
      - id: TASK-5-4
        description: |-
          Delete the 7 MCP tool namespace files at
          ``sandbox/egg_agent_tools/tools/{brc,checkpoint,message,phase,progress,sdlc,task}.py``
          (~1,515 LOC). Delete the ``SYSTEM_PROMPT_NUDGE`` constant at
          ``sandbox/egg_agent_tools/server.py:61`` and any
          ``build_sandbox_mcp_server`` factory that registers them.
          Keep the 4 infrastructure files (``__init__.py``,
          ``_common.py``, ``_registry.py``, ``_tool_compat.py``) until
          a follow-up cleanup confirms no remaining consumer — gate
          their deletion on grep returning zero matches across the
          tree. The shared handler layer at
          ``sandbox/egg_agent_tools/handlers/*.py`` is RETAINED — both
          surfaces collapse to one (the CLI / direct handler path),
          not zero.
        acceptance: |-
          ``rg 'from egg_agent_tools.tools|build_sandbox_mcp_server|SYSTEM_PROMPT_NUDGE'``
          across the tree returns zero matches; the handler layer at
          ``sandbox/egg_agent_tools/handlers/*.py`` unchanged;
          ``shared/egg_agent/client.py`` no longer imports from
          ``egg_agent_tools.tools`` or registers MCP servers (handled
          in TASK-5-5).
        role: coder
        files:
          - sandbox/egg_agent_tools/tools/brc.py
          - sandbox/egg_agent_tools/tools/checkpoint.py
          - sandbox/egg_agent_tools/tools/message.py
          - sandbox/egg_agent_tools/tools/phase.py
          - sandbox/egg_agent_tools/tools/progress.py
          - sandbox/egg_agent_tools/tools/sdlc.py
          - sandbox/egg_agent_tools/tools/task.py
          - sandbox/egg_agent_tools/server.py
      - id: TASK-5-5
        description: |-
          Delete the MCP registration block in
          ``shared/egg_agent/client.py:299–353``: the
          ``EGG_MCP_TOOLS`` env-flag gate at :311, the
          ``build_sandbox_mcp_server`` import at :316, the
          ``mcp_servers = build_sandbox_mcp_server()`` call at :319,
          the ``options.mcp_servers = {...}`` assignment at :323, and
          the ``SYSTEM_PROMPT_NUDGE`` append. The ``EGG_MCP_TOOLS``
          env flag itself becomes permanently unread; document the
          retirement in the doc update from TASK-5-7.
        acceptance: |-
          ``shared/egg_agent/client.py`` no longer references MCP
          tools or the ``EGG_MCP_TOOLS`` env flag; client.py
          options no longer set ``mcp_servers`` (or sets only the
          operator-facing ``orchestrator/mcp_server.py`` if separately
          registered — confirm by grep).
        role: coder
        files:
          - shared/egg_agent/client.py
      - id: TASK-5-6
        description: |-
          Retire ``tests/tools/test_mcp_cli_drift.py`` (delete; the
          MCP↔CLI drift contract no longer applies since the MCP
          surface is gone). Migrate
          ``integration_tests/test_sandbox_mcp_tools_e2e.py`` and
          ``tests/sandbox/egg_agent_tools/test_server.py`` from
          MCP-protocol tests to direct-handler tests against
          ``sandbox/egg_agent_tools/handlers/*.py``. Where the
          original tests assert the MCP-tool surface (schema,
          registration, system-prompt-nudge), replace with the
          equivalent CLI assertion (subcommand exists, ``--help``
          mirrors expected fields). Where they assert
          handler-layer behaviour, simplify to direct handler invocation.
        acceptance: |-
          ``tests/tools/test_mcp_cli_drift.py`` deleted; the two
          migrated test files pass under ``make test``; migrated
          tests no longer import from
          ``sandbox.egg_agent_tools.tools``; coverage report for the
          handler layer does not regress.
        role: tester
        files:
          - tests/tools/test_mcp_cli_drift.py
          - integration_tests/test_sandbox_mcp_tools_e2e.py
          - tests/sandbox/egg_agent_tools/test_server.py
      - id: TASK-5-7
        description: |-
          Documenter: update ``docs/architecture/sandbox.md``,
          ``docs/reference/agent-tools.md`` (or equivalent — locate via
          Grep ``docs/`` for "MCP tools" / "SYSTEM_PROMPT_NUDGE"), and
          the project ``CLAUDE.md`` Quick Reference if it references
          the agent MCP surface. Cover: the MCP tool surface is
          retired in favour of the CLI; the shared handler layer
          backs both today (CLI only after this slice); the
          operator-facing ``orchestrator/mcp_server.py`` is unaffected
          and remains the operator's MCP surface.
        acceptance: |-
          All references to the agent-side MCP tools updated to the
          CLI surface; CLAUDE.md (if changed) is at the project root
          per the planner phase restrictions — staged under
          ``.egg-state/agent-outputs/`` if push-blocked.
          ``orchestrator/mcp_server.py`` references preserved.
        role: documenter
        files:
          - docs/architecture/sandbox.md
          - docs/reference/agent-tools.md
      - id: TASK-5-8
        description: |-
          Per-event wall-clock latency verification at
          ``integration_tests/local_pipeline/test_mcp_to_cli_latency.py``
          (new file under local_pipeline/). Drive a 5-role consensus
          to CONFIRMED with the post-slice-5 CLI-only surface; record
          per-event wall-clock from ``brc next-action`` dispatch to
          agent-process exit; compare against a pre-slice-5 baseline
          captured by re-running with the legacy MCP surface temporarily
          re-enabled (gated by a one-shot ``EGG_MCP_TOOLS_LEGACY=true``
          env, deleted after this slice). Latency regression budget:
          ≤ 5%. On regression > 5%, slice-5 emits a feedback request
          asking the operator whether to ship a persistent
          ``egg-orch`` daemon (architect od-5) as a follow-up.
        acceptance: |-
          Latency-comparison test runs on the local_pipeline fixture
          stack; results captured to
          ``.egg-state/agent-outputs/latency-mcp-vs-cli.json``; assertion
          fails only if regression exceeds the budget; on failure the
          test surfaces a structured ``OVERSEER_ALERT`` priority
          ``medium`` with the measured delta for human review.
        role: tester
        files:
          - integration_tests/local_pipeline/test_mcp_to_cli_latency.py
```
