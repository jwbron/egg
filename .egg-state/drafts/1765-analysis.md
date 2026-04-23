# Analysis: Make egg-internal tools discoverable to sandbox agents

> Issue: #1765 | Phase: refine

## Problem Statement

Sandbox agents spawned into a pipeline phase do not know, upfront, what
egg-specific CLIs exist or how to use them. When an agent first needs
`egg-contract add-decision` (or any similar subcommand), it spends a
visible fraction of its turn budget on *tooling archaeology*: running
`<bin> --help`, `cat`-ing the script, `grep`-ing for subcommand names,
`sed`-ing specific line ranges of the Python implementation — the
refine-phase example in the issue shows **five tool calls before any
productive work**. The pattern is brittle (agents guess wrong, fail,
retry) and non-compounding (every new agent starts from zero).

The desired outcome is that a fresh agent, the moment it boots into a
phase, can see and invoke the full egg-agent tool surface *as first-class
tools with typed schemas* — no shell round-trip, no argparse
archaeology, no source reading. New egg CLIs added under
`sandbox/bin/` should appear automatically through the same mechanism,
and the solution must work in both `public` and `private` network modes.

## Current Behavior

### Agent spawn and runtime

`orchestrator/kubernetes_spawner.py` creates a K8s Job that runs
`python3 -m egg_agent` (built by
`shared/egg_agent/command.py::build_agent_command`, line 42–54). That
entry point calls `shared/egg_agent/client.py::run_agent_async` (line
65) which wraps `claude_agent_sdk.query()` (line 246) with a
`ClaudeAgentOptions` object (line 194–209). Today the options set:

- `permission_mode="bypassPermissions"`
- `model`, `cwd`, `env`
- `setting_sources=["project", "user"]` — reads CLAUDE.md and
  `settings.json` from the filesystem
- `disallowed_tools` (WebFetch/WebSearch in private mode)
- `can_use_tool` callback for role-based file-write blocking
- *(not set)* `mcp_servers` — the single option that would let the
  agent see first-class, typed, non-Bash tools

The experimental `EGG_HARNESS=egg` path (client.py:100–127) routes to
`shared/egg_harness/` with its own native tool registry
(`create_bash_tool`, `create_read_tool`, etc.) and does **not** use MCP.

### What bootstraps into a fresh agent today

`sandbox/agent-config/rules/` is concatenated into `~/.claude/CLAUDE.md`
at container startup (see `rules/README.md`). It includes:

- `mission.md`, `environment.md`, `code-standards.md`,
  `test-workflow.md`, `pr-descriptions.md` — general guidance
- `orchestrator.md` — a 30-line markdown table of **essential**
  `egg-orch` commands, not the full tree
- `contract.md`, `checkpoint.md` — analogous short tables
- `anchor-recovery.md`, `push-recovery.md`, `overseer.md` — recovery
  protocols

These tables list a curated handful of commands (e.g.
`egg-orch anchor init|update|show|validate|cleanup`) but **not**
argument schemas and **not** every subcommand. They point at
`$EGG_REPO_PATH/docs/reference/orchestrator-cli.md` for full detail,
which the agent then has to open and read — another burn.

### The CLIs themselves

`sandbox/bin/` currently ships 7 egg binaries:

| Binary | Lines | Top-level subcommands (via `--help`) |
|---|---:|---|
| `egg-contract` | 1 577 | `show`, `add-commit`, `update-notes`, `complete-task`, `complete-phase`, `verify-criterion`, `add-decision`, `add-feedback`, `agent-status`, `agent-start`, `agent-complete`, `agent-fail`, `agent-next` |
| `egg-orch` | 2 238 | `health`, `env`, `pipeline`, `signal`, `message`, `consensus`, `phase`, `decision`, `container`, `gateway`, `progress`, `overseer`, `push` |
| `egg-checkpoint` | 50 (thin wrapper around `shared/egg_contracts/checkpoint_cli.py`) | `list`, `show`, `browse`, `context`, `cost`, `search` |
| `egg-pipeline-watch` | 514 | positional `pipeline_id`, `--ascii/--compact/--once` |
| `egg-sdlc` | 27 | rich TUI; `-r repo -i issue -p prompt --private` |
| `egg-health-inspect` | 163 | (JSON output) |
| `egg-onboarding-docs` | 151 | positional `repo_dir`, `--dry-run`, `--scope` |

Each CLI's own top-level `--help` is *reasonable* (it lists
subcommands with one-line purposes) — so the issue is not primarily
that help strings are missing. The issue is that the agent doesn't
know which binary to ask for help from, reaches for Bash instead of a
structured tool, and guesses flag shapes.

### Existing MCP surface

`orchestrator/mcp_server.py` runs one FastMCP server (streamable HTTP,
port 9850, Bearer auth via `EGG_LIFECYCLE_SECRET`) whose tools are
defined in `orchestrator/mcp_tools.py::PIPELINE_TOOLS` — `submit_task`,
`run_agent_task`, `babysit_pr`, `get_status`, `cancel_task`,
`get_consensus_status`, `restart_phase`, etc. This server is oriented
at **orchestrator-side Claude** (Claude Code on a developer's host).
Sandbox agents do **not** connect to it, and if they did they'd gain
privilege they should not have (spawning new pipelines from inside one).

The stack already depends on `mcp[cli]>=1.20.0` (pyproject.toml
optional-dev) and `claude-agent-sdk` is installed in the sandbox image.
No new top-level runtime dependency is required to expose MCP tools to
sandbox agents.

### Network reachability

`sandbox/agent-config/rules/environment.md` and
`sandbox/egg_lib/network_mode.py` describe two modes:

- `public` — container bypasses proxy, has direct internet
- `private` — container routes through `http://gateway.egg-system…:9849`
  Squid proxy; no direct egress

In **both** modes the orchestrator is addressable intra-cluster at
`orchestrator.egg-system.svc.cluster.local:9849` (K8s service), and
the gateway at `gateway.egg-system…:9848`. So any network-transport
MCP option must serve at an intra-cluster endpoint the sandbox can
reach in both modes. An **in-process** MCP server (Option D) sidesteps
the network question entirely.

## Constraints

- **Dual-harness reality.** Two agent harnesses coexist (`claude-sdk`
  default, `egg` experimental). An MCP-based solution is native to
  the SDK harness; the egg harness has its own tool-registry surface
  and would need parallel wiring. No single hook covers both without
  duplication.
- **Private-mode network-isolation.** The sandbox image is built
  without PyPI access at runtime; any new Python dependency must be
  baked into the image and both gateway-proxy and intra-cluster
  addressing must keep working.
- **Backward compatibility.** The existing CLIs under `sandbox/bin/`
  are invoked by the entrypoint, by tests, by bash recovery scripts,
  by human operators, and (per AC) must keep working. New surface
  must be additive.
- **Role-based authz.** Sandbox agents must not gain orchestrator-side
  privileges such as `submit_task`, `cancel_task`, `restart_phase`
  through any new channel. This is a reason to keep the sandbox-facing
  surface a *different* server from the existing
  `orchestrator/mcp_server.py`.
- **Drift risk.** If the tool surface is hand-maintained, every CLI
  change (new subcommand, new flag) is a two-file PR. The acceptance
  criterion explicitly disallows "separate doc-update step that can
  drift".
- **System-prompt budget.** Injecting a full tool manifest into the
  system prompt is tempting but expensive — every CLI, every
  subcommand, every flag burns input tokens on every turn the agent
  takes, and most of them won't be used.
- **Not on the table (per issue).** Running an MCP server *inside*
  the sandbox container as a long-running daemon. This is
  architecturally outlier and explicitly ruled out.

## Options Considered

### Option A: Status-quo polish — aggregated AGENT-TOOLS.md + better `--help`

**Approach.** Auto-generate a single `AGENT-TOOLS.md` from each CLI's
`--help` at image build time, mount it at a well-known path, and
reference it from `sandbox/agent-config/rules/mission.md`. Nudge each
CLI's top-level `--help` to list subcommands with one-line purposes
(most already do).

**Pros.**
- Zero new runtime surface. Works identically in both network modes.
- Keeps every existing contract (CLIs intact, humans unaffected).
- Cheap to ship; low blast radius.

**Cons.**
- Does **not** change the tool-call shape — agent still issues shell
  commands, still pays the argparse round-trip, still gets back
  unstructured text.
- No argument validation at the tool boundary; malformed flags still
  discover themselves via exit code 2.
- Mitigates the "I don't know what exists" half of the problem, does
  nothing for the "I don't know how to invoke it" half.

### Option B: Tool manifest rendered into the system prompt

**Approach.** Ship `sandbox/tools.json` declaring each CLI's
subcommands, flags, one-line descriptions. At spawn time,
`egg_agent.client` (line 208–209) assembles a compact summary and
passes it as `options.system_prompt` (or appends it to whatever
phase-specific prompt is passed in).

**Pros.**
- Agent sees the full surface before the first tool call, no I/O.
- No new server, no new network path.
- Works identically in both modes.

**Cons.**
- Still shell-mediated — the agent reads the manifest and then types
  a Bash command. No typed schema, no argument validation.
- Spends input tokens on every turn whether or not the agent uses
  those tools.
- Drift-susceptible unless the manifest is auto-generated from
  argparse, and even then the system prompt can get stale across
  restarts if the image and orchestrator aren't pinned to the same
  build.

### Option C: Second orchestrator-hosted MCP server for sandbox agents

**Approach.** Stand up a *second* FastMCP server inside the
orchestrator pod (or a sidecar), listening on a separate port with a
sandbox-scoped tool set. Sandbox agents receive
`mcp_servers={"egg": {"type": "http", "url":
"http://orchestrator.egg-system…:9851/mcp", ...}}` in
`ClaudeAgentOptions`. Each subcommand becomes a typed `@mcp.tool`.

**Pros.**
- First-class, typed tools with argument validation.
- Shares FastMCP with the existing orchestrator MCP server pattern
  — consistent architecture.
- Natural place to enforce authz (this server never exposes
  `submit_task` / `cancel_task`).
- Observability piggybacks on orchestrator-side logging and rate
  limiting.

**Cons.**
- Adds a new long-running process to operate, monitor, redeploy, and
  keep healthy. Two FastMCP servers in the orchestrator pod roughly
  doubles its tool-surface configuration.
- Introduces a network dependency: if the sandbox-↔-orchestrator link
  flakes (or the second server restarts mid-turn), tool calls fail
  in ways agents have to handle.
- Authn/authz design: new token? reuse `EGG_SESSION_TOKEN`?
  Another knob to maintain.
- Tools execute *outside* the sandbox, so they don't naturally see
  the agent's env vars (`EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `cwd`,
  ...). Every tool has to accept those as arguments or rely on the
  orchestrator cross-referencing the calling session — non-trivial.
- Each tool call is a JSON round-trip through the cluster network
  (latency ≫ in-process dispatch).

### Option D: In-process SDK MCP server via `create_sdk_mcp_server` + `@tool`

**Approach.** Define each CLI subcommand's handler as a
`@tool`-decorated async function in a new `shared/egg_agent_tools/`
module. Call `create_sdk_mcp_server(name="egg-contract", tools=[...])`
(and one per CLI) at import time. In `egg_agent.client.run_agent_async`
(line 194), pass `mcp_servers={"egg_contract": contract_server,
"egg_orch": orch_server, ...}` into `ClaudeAgentOptions`. Handlers
either (a) invoke the CLI's existing Python entry function directly —
e.g. the `add_decision` handler calls
`sandbox/egg_lib/contract_cli.py::cmd_add_decision()` (the function
at line ~736 that the shell CLI's `add-decision` subparser already
dispatches to) — or (b) subprocess the shell binary (less clean, but
works for anything stateful).

Tools appear to the agent as `mcp__egg_contract__add_decision`,
`mcp__egg_orch__pipeline_status`, etc. Schemas are Python dicts with
typed fields, so the SDK does validation at the boundary before the
tool even runs.

**Pros.**
- Typed schemas, argument validation at tool boundary (AC met).
- No new network surface — tools execute in the agent's own process,
  with the agent's env (`EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`,
  `cwd=worktree`) visible by default.
- Works **identically** in public and private modes — no proxy,
  no egress question (AC met).
- Works without any new auth layer — the tool runs in the agent's
  trust boundary.
- No new process to operate / monitor / version.
- Reuses the existing CLI handlers as the backing implementation, so
  the authoritative behaviour is **one** codepath shared between the
  shell CLI and the MCP tool. Shell CLI keeps working unchanged; human
  and test invocations see no change (AC met).
- The `mcp__<server>__<tool>` naming pattern means the existing
  checkpoint browser and tool-use log stream get nicely-grouped
  entries without any new plumbing.
- Architecturally clean separation of concerns: orchestrator-side MCP
  server (for human-driven Claude Code, exposing pipeline
  admin tools) stays on its existing port; sandbox agents get their
  own in-process tool surface with a *different* tool set. Authz by
  construction — sandbox agents can't even name `submit_task`.

**Cons.**
- Only works on the SDK-backed harness. The experimental
  `EGG_HARNESS=egg` path uses `shared/egg_harness/tools/`, which
  would need parallel registration (same handler functions, different
  registry). The issue's decision question on architectural fit
  implies accepting this — the egg harness is opt-in and experimental.
- Tools execute in the sandbox container's Python interpreter, so
  handlers must be careful about blocking (the SDK runs them via the
  event loop). The existing argparse CLIs are synchronous; wrapping
  them in `asyncio.to_thread` is the obvious adapter.
- Does **not** cover scenarios where the sandbox shells into a nested
  `claude` CLI session — but the legacy interactive-mode CLI was
  removed in #1762, so this is a non-issue today.
- Tool-schema definitions are Python code, not data files — can't be
  edited by a non-Python-literate operator without a code PR.

### Alternative (hybrid): D + B — SDK tools as primary surface, short bootstrap paragraph in prompt

**Approach.** Do Option D as the structural answer. Additionally,
inject a ~150-word bootstrap paragraph into the system prompt at
spawn time: "You have first-class MCP tools named `mcp__egg_*__*`
for everything under `egg-contract`, `egg-orch`, `egg-checkpoint`,
`egg-pipeline-watch`. Prefer them over Bash." This closes a known
failure mode where agents default to Bash even when a structured tool
exists.

**Pros.** All of D's, plus a one-paragraph prompt nudge that costs
<200 input tokens/turn and visibly shifts default behaviour.

**Cons.** Minor prompt-size cost; couples the prompt to tool naming
(manageable since naming is settled as part of this work).

## Recommended Approach

**Option D (in-process SDK MCP server via `create_sdk_mcp_server`)**, with
the small hybrid addition from the "D + B" sketch (a short bootstrap
nudge in the system prompt).

Rationale against the criteria the issue explicitly names
(architectural consistency, long-term maintainability, discoverability,
composition with the existing system):

1. **Architectural consistency.** The codepath already runs
   `claude_agent_sdk.query()` with a `ClaudeAgentOptions` object
   (client.py:194–209). Adding `mcp_servers={...}` to that options
   object is a surgical edit — the smallest possible new architectural
   surface. By contrast, Option C introduces a second long-running
   FastMCP service to operate; Options A/B keep the current
   Bash-through-CLI pattern that is the observed source of the pain.
2. **Long-term maintainability.** Handlers are Python functions. If
   we make the shell CLI dispatch *into* those same handlers (i.e.
   the `add-decision` subparser's default function becomes "call
   `egg_agent_tools.contract.add_decision()`"), we get **one** source
   of truth — and the AC "new egg CLI shows up automatically" is
   satisfied because adding a subcommand means adding a `@tool` that
   the CLI also dispatches to. No parallel drift between "shell
   behaviour" and "agent-visible schema".
3. **Discoverability.** Typed schemas give the agent per-tool
   descriptions + argument names + types + required flags — the
   exact information it currently reconstructs from `--help` and
   source reads. The SDK surfaces these as first-class tool-use
   blocks; checkpoint logs group them by name.
4. **Network-mode neutrality.** The in-process path has no network
   dependency, so `public` vs `private` mode is irrelevant — the
   tools just work. Option C would need a per-mode routing decision
   (direct intra-cluster DNS in both modes, ok, but still a
   decision); Options A/B unchanged by mode.
5. **Authz by construction.** The sandbox-facing tool registry is a
   different Python module from `orchestrator/mcp_tools.py`. Sandbox
   agents physically cannot see `submit_task` because the handler
   doesn't exist in their process. No trust boundary to re-enforce.
6. **Composition.** Keeps the existing orchestrator MCP server
   (host-side Claude Code integration) unchanged. Does not require
   coordinating changes with `orchestrator/mcp_server.py` beyond the
   tool-surface split.

One explicit limitation we are accepting: Option D does not cover the
experimental `egg` harness (`EGG_HARNESS=egg`) without parallel
registration against `shared/egg_harness/tools/`. Given the harness
is opt-in and experimental, we propose deferring that parallel
registration until the egg harness graduates, and documenting this
caveat in the CLI-discoverability doc. (See decision-3.)

Two sub-choices inside the above are **pending human confirmation at
the HITL gate**, not decided here — we offer them as a recommended
default in each decision:

- Start with a curated high-value subset (the BRC lifecycle commands:
  consensus propose/ack/nack/confirmed, message send/poll, contract
  add-decision/add-feedback/complete-task/update-notes, decision
  resolve, phase get/advance, anchor init/update/show), with the
  registry auto-surfacing new subcommands within already-covered CLIs
  — see decision-2 and decision-6.
- Ship for the `claude_agent_sdk` harness first and add parallel
  registration for the experimental `egg` harness only when it
  graduates — see decision-3.

## Open Questions

All open questions have been registered via `egg-contract` and are
reproduced here for reviewer visibility. Recommended options are
marked in each decision; the human gate at the end of refine decides
which to take.

### HITL Decisions

<!-- egg-hitl-decision id=decision-1 -->

**Which approach should we adopt for exposing egg CLIs to sandbox agents?**

- [ ] A: Auto-generated AGENT-TOOLS.md + improved --help (status quo polish)
- [ ] B: Tool manifest rendered into system prompt at spawn time
- [ ] C: Second orchestrator-hosted MCP server (streamable HTTP) scoped to sandbox agents
- [ ] D: In-process SDK MCP server via create_sdk_mcp_server + @tool decorators, wired into egg_agent.client (Recommended)
- [ ] Hybrid D+B: in-process SDK tools as primary surface, plus a short tool index injected into the system prompt
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-2 -->

**Should the MCP tool surface be auto-generated from CLI argparse introspection, or hand-curated?**

- [ ] Auto-generated: single source of truth is each CLI's argparse; new subcommands surface automatically (matches AC 'no doc drift')
- [ ] Hand-curated: explicit @tool definitions with hand-written descriptions/schemas; tighter quality, but risks drift
- [ ] Hybrid: auto-generate the skeleton, allow per-tool overrides for descriptions/schemas where the argparse help is insufficient (Recommended)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-3 -->

**Must the solution also cover the experimental 'egg' harness (EGG_HARNESS=egg), or claude_agent_sdk only for now?**

- [ ] claude_agent_sdk harness only — the egg harness is opt-in and experimental; parallel registration can be added later when it graduates (Recommended)
- [ ] Both harnesses from day one — register the same tool handlers in both egg_agent.client (SDK) and shared/egg_harness/tools (egg harness registry)
- [ ] Only the egg harness — treat this as the forcing function to migrate sandbox agents onto the new harness
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-4 -->

**Should the existing shell CLIs under sandbox/bin/egg-* remain as first-class, or be deprecated once the MCP tool surface lands?**

- [ ] Keep both indefinitely — CLIs are used by humans, tests, entrypoint scripts, and bash wrappers; MCP tools are additive for agents (Recommended — matches AC 'existing CLIs keep working')
- [ ] Keep CLIs, but soft-deprecate with a banner that points agents at the MCP tool when one is available
- [ ] Remove CLIs as we finish each MCP tool; force agents onto the structured surface
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-5 -->

**How should MCP tools be named so agents can pattern-match them quickly?**

- [ ] Flat namespace under one server: mcp__egg__contract_add_decision, mcp__egg__orch_pipeline_status, mcp__egg__checkpoint_search (one server, flat names)
- [ ] Per-CLI servers: mcp__egg_contract__add_decision, mcp__egg_orch__pipeline_status, etc. (one SDK server per CLI binary) (Recommended — mirrors the CLI surface the humans already know)
- [ ] Subcommand-join form: mcp__egg__contract/add-decision (preserve the exact shell command as the tool name suffix)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-6 -->

**What is the tool-surface scope — all subcommands of every egg CLI, or a curated high-value subset?**

- [ ] Everything — every subcommand of egg-contract, egg-orch, egg-checkpoint, egg-pipeline-watch, egg-sdlc, egg-health-inspect, egg-onboarding-docs becomes an MCP tool (maximizes discoverability, matches AC 'new CLI shows up automatically')
- [ ] High-value subset first — only the subcommands agents hit in their BRC lifecycle (consensus propose/ack/nack/confirmed, message send/poll, contract add-decision/add-feedback/complete-task/update-notes, decision resolve, phase get/advance, anchor ops); expand iteratively (Recommended)
- [ ] BRC-only — just the subcommands the PROPOSE/ACK/NACK/CONFIRM lifecycle depends on; all others stay shell-only
- [ ] Other (explain in reply)

---

### Open-Ended Feedback

The feedback request was registered via `egg-contract add-feedback`
(feedback-1). All questions are reproduced below; the human gate
reviews the comment and edits it in place to respond.

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: Access-control slice: if we DO add a separate sandbox-facing MCP server (Option C), what's the minimum tool set we want exposed, and which orchestrator-side tools (submit_task / cancel_task / restart_phase / advance_phase / get_consensus_status) should NEVER be reachable from inside a sandbox? Is there a concrete threat model you want us to design against?**

> _Your answer here_

**Q2: Drift-prevention contract: what's the acceptable mechanism for keeping the CLI and the tool schemas in sync? Options: (a) CI test that parses each CLI's argparse and diffs against the @tool registry; (b) single source of truth (argparse) with tools derived at import time; (c) make the CLI a thin wrapper over the @tool functions (CLI calls the same handler internally). Which flavour of 'no drift' do you want?**

> _Your answer here_

**Q3: Bootstrap guidance: even after MCP tools are registered, should we inject a short (<=200 word) 'these tools exist, prefer them over Bash' paragraph into the system prompt, or trust the SDK's native tool-discovery without a prompt nudge? Past experience suggests agents sometimes default to Bash even when a structured tool exists.**

> _Your answer here_

**Q4: Output shape for diagnostic subcommands: commands like 'egg-orch pipeline status' emit rich terminal tables/JSON. Should the MCP tool return (a) the JSON payload verbatim, (b) a compact text summary for token economy, or (c) both as multi-content blocks? This affects tool-call token cost and agent comprehension.**

> _Your answer here_

**Q5: Authn/authz for network-transport MCP (if Option C lands instead of D): sandbox agents currently authenticate to the gateway via EGG_SESSION_TOKEN. Should a sandbox-facing MCP server reuse that token, introduce a new per-agent token, or piggyback on the existing EGG_LIFECYCLE_SECRET? What's the expected token lifecycle across agent restarts?**

> _Your answer here_

**Q6: Observability: should MCP tool calls generate distinct log events / checkpoints separate from the SDK's built-in tool_use stream? Today, Bash calls to egg CLIs show up as 'Bash' tool_use blocks in checkpoints; if they become first-class MCP tools they'll show up under distinct names — does the checkpoint browser need any changes to group or search them?**

> _Your answer here_

**Q7: Timeouts and cancellation: SDK MCP server tool calls default to 60s connection timeout; some egg subcommands (e.g. 'egg-pipeline-watch', 'egg-checkpoint search') can run longer. What's the right timeout envelope, and should long-running operations be split into start/poll tool pairs rather than blocking?**

> _Your answer here_

**Q8: Rollout plan preference: should the MCP tool surface be enabled for all roles simultaneously, or gated behind a feature flag (e.g. EGG_MCP_TOOLS=true) during a burn-in window where we compare token/turn-count between the two modes?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

## Complexity Assessment

**medium**. The change is contained to a small number of files (a
new `shared/egg_agent_tools/` package, a single-line edit in
`shared/egg_agent/client.py::run_agent_async` to pass `mcp_servers`,
optionally a `sandbox/agent-config/rules/` prompt nudge, and
refactoring the existing CLI argparse dispatch to share handlers with
the `@tool`-decorated functions). It is not a trivial one-file change
(enough surface across several CLIs, a drift-prevention mechanism,
harness-coverage trade-offs, and a scope/naming policy to settle), but
it is not architectural surgery either — the codepath already runs the
SDK and already pays for `mcp[cli]`; we are populating an options
field that is currently empty. No new service, no new network path, no
cross-system coordination.

---

*Authored-by: egg*
