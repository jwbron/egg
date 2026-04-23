# Analysis: Make egg-internal tools discoverable to sandbox agents

> Issue: #1765 | Phase: refine

## Problem Statement

Sandbox agents spawned into a pipeline phase do not know, upfront, what
agent-facing capabilities exist or how to invoke them. When an agent
first needs, for example, to *register an open question that needs
human input*, it spends a visible fraction of its turn budget on
*tooling archaeology*: running `<bin> --help`, `cat`-ing a CLI, `grep`-ing
for subcommand names, `sed`-ing specific line ranges of the Python
implementation — the refine-phase example in the issue shows **five
tool calls before any productive work**. The pattern is brittle
(agents guess wrong, fail, retry) and non-compounding (every new agent
starts from zero).

The desired outcome is that a fresh agent, the moment it boots into a
phase, can see and invoke the full set of capabilities it needs *as
first-class tools with typed schemas* — no shell round-trip, no
argparse archaeology, no source reading. Two related but *separate*
sub-questions follow from this:

- **HOW** should we expose agent-facing tools? (mechanism question — where
  do tools live, how do they reach the agent, how is `claude_agent_sdk`
  wired.)
- **WHAT** tools should exist? (surface-design question — what
  *capabilities*, named as verbs the agent performs, should be first-class
  tools.)

This analysis treats them as distinct and answers both.

New capabilities added later should appear automatically through the same
mechanism, and the solution must work in both `public` and `private`
network modes.

### A note on framing

Earlier refine-cycle drafts framed the problem as "wrap the existing
shell CLIs under `sandbox/bin/egg-*` as MCP tools," and scoped tool
naming and selection by CLI taxonomy (`mcp__egg_contract__add_decision`,
"which subcommands to cover"). Human feedback correctly identified
that as the wrong starting point: the agent doesn't care that
`add-decision` is hosted by a binary called `egg-contract` — the agent
cares that it needs to register an open question, and the tool should
be named `register_open_question` (or similar) from that verb. This
analysis re-grounds the surface-design question in an **Agent
Capability Audit** that enumerates what the agent needs to *do* in
each phase; the surface falls out of that, not out of the current CLI
layout. See §"Agent Capability Audit" below.

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
which the agent then has to open and read — another burn. Importantly
they are organized by **CLI**, not by capability the agent has in its
role: a fresh `refiner` has to build its own mental map of "which of
these subcommands do I, specifically, need in this phase?"

### The CLIs themselves

`sandbox/bin/` currently ships 7 egg binaries:

| Binary | Lines | Top-level surface (via `--help`) |
|---|---:|---|
| `egg-contract` | 1 577 | subcommands: `show`, `add-commit`, `update-notes`, `complete-task`, `complete-phase`, `verify-criterion`, `add-decision`, `add-feedback`, `agent-status`, `agent-start`, `agent-complete`, `agent-fail`, `agent-next` |
| `egg-orch` | 2 238 | subcommands: `health`, `env`, `pipeline`, `signal`, `message`, `consensus`, `phase`, `decision`, `container`, `gateway`, `progress`, `overseer`, `push` |
| `egg-checkpoint` | 50 (thin wrapper around `shared/egg_contracts/checkpoint_cli.py`) | subcommands: `list`, `show`, `browse`, `context`, `cost`, `search` |
| `egg-pipeline-watch` | 514 | flat: positional `pipeline_id`, `--ascii/--compact/--once` |
| `egg-sdlc` | 27 | flat TUI launcher; `-r repo -i issue -p prompt --private` |
| `egg-health-inspect` | 163 | flat; JSON output |
| `egg-onboarding-docs` | 151 | flat; positional `repo_dir`, `--dry-run`, `--scope` |

Each subcommand-tree CLI's top-level `--help` is *reasonable* (it lists
subcommands with one-line purposes) — so the issue is not primarily
that help strings are missing. The issue is twofold:

1. The agent doesn't know which binary to ask for help from, reaches
   for Bash instead of a structured tool, and guesses flag shapes.
2. The agent's mental model would more naturally run on verbs
   ("register an open question"), but the current surface forces it to
   first map that verb to a binary-plus-subcommand.

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

## Agent Capability Audit

This section answers the "WHAT" question (the tool-surface question)
from first principles: *what verbs does each sandbox agent role
perform during its phase?* CLI subcommand names are deliberately
absent — the goal is to describe the agent's needs in its own terms.

Phase-to-role mapping is authoritative at
`shared/egg_contracts/agent_roles.py:1020–1038`; BRC state machine at
`orchestrator/peer_consensus.py:1–60`; role-specific prompts at
`orchestrator/routes/pipelines.py:3243–3406` and
`sandbox/agent-config/rules/mission.md`.

### Cross-cutting (every phase, every role)

- **Read my role and the issue spec** (what am I, what is this pipeline
  working on)
- **Read my assigned tasks** (what specifically do I own this phase)
- **Read prior-phase artifacts and checkpoints** (what has been
  decided/done before me)
- **Emit a heartbeat / progress update** (tell the orchestrator I'm
  alive and where I am)
- **Poll for messages from peers** (consensus messages, STATUS broadcasts,
  handoffs)
- **Send a directed message to a peer** (HANDOFF, STATUS, NACK
  clarification) — p2p, supplementary to BRC
- **Register an open question for human input** (HITL decision or
  feedback)
- **Fetch pending human decisions / answered feedback** (has the human
  replied yet)
- **Signal an error or blocked state** (recoverable or terminal)
- **Record a task as complete with commit linkage**
- **Record a commit against a task**
- **Update my agent status** (WORKING / PROPOSED / CONFIRMED)

### BRC lifecycle (producer)

- **Propose my work for consensus** (bundles: summary, artifacts,
  files_changed, tests_run, tasks, commit_sha, push)
- **React to a reviewer NACK** (read the NACK rationale, fix the
  concern, re-propose)
- **React to a reviewer ACK** (note the ACK; when all reviewers ACK,
  confirm)
- **Confirm my work** (move producer to CONFIRMED)
- **Query current consensus state** (who has ACKed, who is blocking,
  is consensus reached yet)

### BRC lifecycle (reviewer)

- **Poll for the producer's proposal**
- **Form my verdict against the review criteria for my role**
- **ACK the proposal with specific citations** (artifact refs, reasons)
- **NACK the proposal with actionable feedback** (artifact refs,
  required fixes)
- **React to a re-proposal** (re-review)
- **Confirm my review** (move reviewer to CONFIRMED)
- **Query current consensus state** (same as producer)

### Phase: refine

**Roles**: `refiner` (producer), `reviewer_refine` (reviewer),
`reviewer_agent_design` (reviewer).

**Refiner-specific verbs**:
- **Read prior review NACK** (the thing that triggered this refine
  cycle, if any)
- **Produce/update an analysis document in `.egg-state/drafts/`**
- **Run repo exploration to ground claims in real file/line citations**
- **Enumerate approach options with trade-offs**

**Reviewer-specific verbs** (both reviewer roles):
- **Check that every claim in the draft cites a concrete code
  location**
- **Check that every open question is registered via an HITL primitive
  (not prose-only)**
- **Cross-check the recommended approach against a design-criteria
  checklist** (agent-mode-design.md for `reviewer_agent_design`)

### Phase: plan

**Roles**: `architect` (producer), `task_planner` (producer),
`risk_analyst` (producer), `reviewer_plan` (reviewer),
`reviewer_contract` (reviewer).

**Architect-specific verbs**:
- **Read the refined analysis from the prior phase**
- **Design the system-wide structure (components, dependencies,
  interactions)**
- **Produce/update an architecture artifact in `.egg-state/drafts/`**

**Task-planner-specific verbs**:
- **Read the refined analysis + architect's design**
- **Decompose the work into implementation tasks with role assignments
  (coder/tester/documenter)**
- **Produce/update a plan artifact with YAML task list**
- **Register tasks into the contract** (so `implement`-phase agents can
  read them)

**Risk-analyst-specific verbs**:
- **Analyze the proposed approach for blockers, security,
  performance, rollout risks**
- **Produce a risk-assessment artifact**

**Reviewer-specific verbs**:
- **Cross-check acceptance criteria completeness**
- **Verify task→role assignments are sensible and non-overlapping**
- **Verify plan scope tracks the refined analysis (no scope creep, no
  unresolved decisions being silently assumed)**

### Phase: implement

**Roles**: `coder` (producer), `tester` (producer), `documenter`
(producer), `reviewer_code` (reviewer), `reviewer_contract` (reviewer).

**Coder-specific verbs**:
- **Read assigned tasks filtered by `role:coder` (or default/no role)**
- **Implement code changes for assigned tasks**
- **Run local tests before proposing**
- **React to tester gaps and re-implement**

**Tester-specific verbs**:
- **Read coder's changes (handoff)**
- **Run the full test suite**
- **Identify coverage gaps**
- **Produce a structured gap report for the coder**

**Documenter-specific verbs**:
- **Read coder's changes**
- **Update README / docs / docstrings / migration guides**

**Reviewer-specific verbs**:
- **Read plan + contract to know what was supposed to be built**
- **Cross-check code changes against acceptance criteria**
- **Cross-check test coverage against code changes**

### Capabilities the agent needs that no current CLI cleanly exposes

(Surfaced by the capability audit; these are first-class candidates
for the agent-facing tool surface even though no 1:1 CLI subcommand
backs them.)

1. **Get full structured BRC state as JSON** (who has proposed, who
   has ACKed/NACKed, who is blocking, vote tally). Today observable
   only by string-parsing `egg-orch consensus status` text output.
2. **List blocking agents by role name** (who specifically is
   preventing consensus right now). Today: inferred from the
   status-command human-readable output.
3. **Read another agent's most recent proposal / review artifact**
   (the review text, not just the proposal summary). Today: dig
   through `.egg-state/brc-history/` by hand.
4. **Record a task gap / acknowledgment** (tester saw X, coder will
   fix in next cycle). Today: informal note in a NACK body or a
   contract decision.
5. **Fetch handoff metadata for a named upstream role** (coder asking
   for tester's gap report artifact paths). Today: hardcoded in role
   prompts.
6. **Query "has my human HITL answer arrived yet?"** as a poll, not
   as a status-text scrape.

These are the kind of tools that would not exist if we mechanically
mirrored the current CLI surface — they are candidates precisely
because the capability audit surfaced them.

### Candidate agent-facing tool surface (~30 tools)

Rough shape implied by the audit, grouped by *semantic function* the
agent performs, not by which binary currently hosts the code. Tool
names here are indicative — exact naming is decision-7 (which
supersedes decision-5).

- `phase_context` — get my role, pipeline_id, phase, assigned tasks,
  issue spec, prior artifact paths
- `task_list`, `task_complete`, `task_add_commit`, `task_mark_gap`
- `checkpoint_list`, `checkpoint_get`, `checkpoint_search`
- `brc_propose`, `brc_ack`, `brc_nack`, `brc_confirm`,
  `brc_get_state`, `brc_list_blocking`
- `peer_send_message`, `peer_poll_messages`, `peer_read_artifact`
- `hitl_register_decision`, `hitl_register_feedback`,
  `hitl_check_answers`
- `progress_emit`, `signal_error`, `signal_heartbeat`
- `overseer_query_status`
- `anchor_init`, `anchor_update`, `anchor_get`

Exact count and exact grouping are part of decision-8 (which is now
framed by the capability audit, not by "all subcommands vs subset";
supersedes decision-6).

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
  must be additive. See decision-4 — the recommendation is to keep
  CLIs indefinitely for non-agent consumers.
- **Role-based authz.** Sandbox agents must not gain orchestrator-side
  privileges such as `submit_task`, `cancel_task`, `restart_phase`
  through any new channel. This is a reason to keep the sandbox-facing
  surface a *different* server from the existing
  `orchestrator/mcp_server.py`.
- **Drift risk (for any tool ↔ backing-handler mapping).** If the tool
  surface is hand-maintained and the backing handlers change, every
  handler change is a two-file PR. The acceptance criterion explicitly
  disallows "separate doc-update step that can drift".
- **System-prompt budget.** Injecting a full tool manifest into the
  system prompt is tempting but expensive — every tool, every
  argument burns input tokens on every turn the agent takes, and most
  of them won't be used.
- **Capability audit completeness.** The capability list above is our
  best first-pass reading of mission.md + the role prompts in
  `orchestrator/routes/pipelines.py` + the BRC state machine. It will
  miss some less-common verbs; the tool surface needs to be
  *extensible* (adding a new tool is a small code change in one
  module) because our first cut will be incomplete. This is a
  non-blocking plan-phase concern, not a refine-phase concern.
- **Not on the table (per issue).** Running an MCP server *inside*
  the sandbox container as a long-running daemon. This is
  architecturally outlier and explicitly ruled out.

## Options Considered

**Two independent questions** — keep them separate when reading what
follows:

- **HOW (mechanism).** Where do tools live, how does the agent reach
  them? Options A–D below.
- **WHAT (surface).** What tools exist and what are they called?
  Answered by the Agent Capability Audit above (and decisions 5 and 6).

The options in this section are **mechanism options** for giving
agents a first-class tool surface. Every mechanism can host any of
the surface choices; mechanism and surface are orthogonal.

### Option A: Status-quo polish — aggregated AGENT-TOOLS.md + better `--help`

**Mechanism.** Auto-generate a single `AGENT-TOOLS.md` from each CLI's
`--help` at image build time, mount it at a well-known path, and
reference it from `sandbox/agent-config/rules/mission.md`. Nudge each
subcommand-tree CLI's top-level `--help` to list subcommands with
one-line purposes (most already do).

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
- Even after doc polish, the agent's mental model is still "CLI
  binary → subcommand → flag" rather than "verb → argument" — so the
  mapping step the agent has to do each time is preserved.

### Option B: Tool manifest rendered into the system prompt

**Mechanism.** Ship a structured manifest of tools (semantic name,
arguments, description) and inject a compact summary into the agent
system prompt at spawn time via `options.system_prompt`.

**Pros.**
- Agent sees the full surface before the first tool call, no I/O.
- No new server, no new network path.
- Works identically in both network modes.

**Cons.**
- Still shell-mediated — the agent reads the manifest and then types
  a Bash command. No typed schema, no argument validation at the
  call boundary.
- Spends input tokens on every turn whether or not the agent uses
  those tools. A full ~30-tool surface with arguments becomes
  non-trivial prompt weight.
- Does not by itself solve "I invoked the tool wrong" — errors still
  surface as argparse exit-code 2 or equivalent.

### Option C: Second orchestrator-hosted MCP server for sandbox agents

**Mechanism.** Stand up a *second* FastMCP server inside the
orchestrator pod (or a sidecar), listening on a separate port with a
sandbox-scoped tool set. Sandbox agents receive
`mcp_servers={"egg": {"type": "http", "url":
"http://orchestrator.egg-system…:9851/mcp", ...}}` in
`ClaudeAgentOptions`. Each verb from the capability audit becomes a
typed `@mcp.tool`.

**Pros.**
- First-class, typed tools with argument validation at the boundary.
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

**Mechanism.** Define each capability-audit verb as a
`@tool`-decorated async function in a new Python module
(`sandbox/egg_agent_tools/` — see decision on placement below;
reviewer feedback from the prior cycle recommended `sandbox/` over
`shared/` for sandbox-process-only code). Call
`create_sdk_mcp_server(name="...", tools=[...])` at import time and
pass `mcp_servers={...}` into `ClaudeAgentOptions` in
`shared/egg_agent/client.py::run_agent_async`. Each handler either
(a) calls the existing Python entry function directly — e.g. the
`hitl_register_decision` handler calls
`sandbox/egg_lib/contract_cli.py::cmd_add_decision()` (the function
at line ~736 that the shell CLI's `add-decision` subparser already
dispatches to) — or (b) subprocesses the shell binary (less clean,
but works for anything stateful).

Tools appear to the agent as `mcp__<server>__<tool>`. Schemas are
Python dicts with typed fields, so the SDK does validation at the
boundary before the tool even runs.

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
  shell CLI and the MCP tool (where a CLI exists). Shell CLI keeps
  working unchanged; human and test invocations see no change (AC
  met). For capabilities with no existing CLI (e.g.
  `brc_get_state` as JSON), the handler is a new function that the
  CLI can *also* dispatch into if we later decide to expose it on
  the shell.
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
- Tool-schema definitions are Python code, not data files — can't be
  edited by a non-Python-literate operator without a code PR.

### Alternative (hybrid): D + a short bootstrap nudge in the prompt

**Mechanism.** Do Option D as the structural answer. Additionally,
inject a ~150-word bootstrap paragraph into the system prompt at
spawn time: "You have first-class MCP tools named `mcp__sdlc__*`,
`mcp__brc__*`, `mcp__phase__*` for your agent capabilities. Prefer
them over Bash." This closes a known failure mode where agents
default to Bash even when a structured tool exists.

**Pros.** All of D's, plus a one-paragraph prompt nudge that costs
<200 input tokens/turn and visibly shifts default behaviour.

**Cons.** Minor prompt-size cost; couples the prompt to tool naming
(manageable since naming is settled as part of this work —
decision-7, which supersedes decision-5).

## Recommended Approach

The recommendation splits cleanly into two:

### (a) Mechanism: Option D + the short prompt nudge

**Option D (in-process SDK MCP server via `create_sdk_mcp_server`)**,
with the small hybrid addition from the "D + short prompt nudge"
sketch.

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
2. **Long-term maintainability.** Handlers are Python functions. Where
   a CLI already exists (e.g. `egg-contract add-decision`), we make
   the shell CLI dispatch *into* the same handler that backs the
   `@tool` — **one** source of truth. Where no CLI exists (e.g.
   `brc_get_state` as JSON), we write the handler once and optionally
   expose a CLI subcommand later. No parallel drift between "shell
   behaviour" and "agent-visible schema".
3. **Discoverability.** Typed schemas give the agent per-tool
   descriptions + argument names + types + required flags — the
   exact information it currently reconstructs from `--help` and
   source reads. The SDK surfaces these as first-class tool-use
   blocks; checkpoint logs group them by name.
4. **Network-mode neutrality.** The in-process path has no network
   dependency, so `public` vs `private` mode is irrelevant — the
   tools just work. Option C would need a per-mode routing decision;
   Options A/B unchanged by mode.
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

### (b) Tool surface: the capability audit drives naming and scope

Name tools by **verbs the agent performs**, not by CLI taxonomy.
Examples (indicative):

- `mcp__sdlc__register_open_question` ← today's
  `egg-contract add-decision`
- `mcp__sdlc__request_feedback` ← today's `egg-contract add-feedback`
- `mcp__sdlc__complete_task` ← today's `egg-contract complete-task`
- `mcp__brc__propose`, `mcp__brc__ack`, `mcp__brc__nack`,
  `mcp__brc__confirm`, `mcp__brc__get_state` (new capability),
  `mcp__brc__list_blocking` (new capability)
- `mcp__phase__get_context`, `mcp__phase__get_assigned_tasks`
- `mcp__checkpoint__list`, `mcp__checkpoint__get`,
  `mcp__checkpoint__search`
- `mcp__peer__send_message`, `mcp__peer__poll_messages`,
  `mcp__peer__read_artifact` (new capability)
- `mcp__progress__emit`, `mcp__progress__signal_error`,
  `mcp__progress__heartbeat`

Why this matters (summarizing what human feedback in the prior cycle
made explicit):

- The agent's mental model is verbs. `register_open_question` is what
  it wants to do; it shouldn't have to know that the primitive today
  lives in a binary called `egg-contract` under a subcommand called
  `add-decision`.
- Some agent-needed capabilities (e.g. "give me BRC state as JSON")
  don't exist on the current CLI at all. Naming by verb means those
  new tools slot in alongside the CLI-backed ones with no taxonomic
  awkwardness.
- Semantic names are stable under CLI refactoring. If someone renames
  `egg-contract` to `egg-agent-contract` tomorrow, the tool name
  `mcp__sdlc__register_open_question` is unchanged.

The detailed list is decision-8 (scope: "which verbs to cover in
iteration 1"; supersedes decision-6) and decision-7 (naming
conventions: "mcp__sdlc__* vs alternatives"; supersedes decision-5).
Recommended defaults are described in the decisions below.

Two sub-choices inside the above are **pending human confirmation at
the HITL gate**, not decided here — we offer them as a recommended
default in each decision:

- For scope in iteration 1: a BRC- and HITL-complete core
  (~15 verbs covering the producer/reviewer lifecycle, HITL
  registration, heartbeat/progress, task completion, and a
  `phase__get_context` orientation tool), with the module
  structured so that adding a new verb is a small local change —
  see decision-8 (supersedes decision-6).
- For harness coverage: Ship for the `claude_agent_sdk` harness first
  and add parallel registration for the experimental `egg` harness
  only when it graduates — see decision-3.

## Open Questions

All open questions have been registered via `egg-contract` and are
reproduced here for reviewer visibility. Recommended options are
marked in each decision; the human gate at the end of refine decides
which to take.

### HITL Decisions

<!-- egg-hitl-decision id=decision-1 -->

**Which mechanism should we adopt for exposing agent-facing tools to sandbox agents?**

- [ ] A: Auto-generated AGENT-TOOLS.md + improved --help (status quo polish)
- [ ] B: Tool manifest rendered into system prompt at spawn time
- [ ] C: Second orchestrator-hosted MCP server (streamable HTTP) scoped to sandbox agents
- [ ] D: In-process SDK MCP server via create_sdk_mcp_server + @tool decorators, wired into egg_agent.client (Recommended)
- [ ] Hybrid D + prompt nudge: in-process SDK tools as primary surface, plus a short tool index injected into the system prompt
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-2 -->

**Should the handler layer under the chosen mechanism be auto-derived from some single source of truth, or hand-written?**

- [ ] Auto-derive handlers+schemas from CLI argparse (single source of truth = argparse); tools are a thin adapter (matches AC 'no doc drift', but locks tools to the CLI taxonomy we just rejected as the naming model)
- [ ] Hand-written @tool definitions whose handlers dispatch into the same Python function the existing shell CLI calls; CLI and tool share the handler, but tool names, argument shapes, and descriptions are hand-curated to match the capability audit (Recommended — matches agent-first naming and still prevents handler drift)
- [ ] Fully hand-written handlers (no shared function with the CLI); highest quality per-tool but worst drift story
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

- [ ] Keep both indefinitely — CLIs are used by humans, tests, entrypoint scripts, and bash wrappers; MCP tools are additive for agents (Recommended — matches AC 'existing CLIs keep working' and explicit prior-cycle human feedback: "agents don't care if they exist")
- [ ] Keep CLIs, but soft-deprecate with a banner that points agents at the MCP tool when one is available
- [ ] Remove CLIs as we finish each MCP tool; force agents onto the structured surface
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-5 SUPERSEDED -->

> **Note on decision-5**: the original decision-5 in the contract was
> framed around CLI taxonomy ("flat namespace" / "per-CLI servers" /
> "subcommand-join form") from a prior draft of this analysis. The
> human pivot ("name tools from the agent's perspective, not from CLI
> taxonomy") makes those options inadequate. decision-5 is
> **superseded by decision-7 below**. Please pick "Other" on
> decision-5 in the contract (with response "superseded by
> decision-7") or leave it pending until the gate is closed.

<!-- egg-hitl-decision id=decision-7 -->

**How should MCP tools be named so agents can pattern-match them quickly?**

- [ ] Semantic / verb-based names grouped by agent function, decoupled from CLI taxonomy — e.g. mcp__sdlc__register_open_question, mcp__brc__propose, mcp__phase__get_context, mcp__checkpoint__search. New capabilities with no CLI counterpart slot in naturally. Stable under CLI refactors. (Recommended — directly implements prior-cycle human feedback that naming should come from the agent's verbs, not from `sandbox/bin/` binary names)
- [ ] CLI-mirroring names — mcp__egg_contract__add_decision, mcp__egg_orch__consensus_propose, one SDK server per CLI binary. Mirrors the shell surface humans know.
- [ ] Flat namespace under one server — mcp__egg__contract_add_decision, mcp__egg__orch_consensus_propose (one server, flat names)
- [ ] Subcommand-join form — mcp__egg__contract/add-decision (preserves exact shell command suffix)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-6 SUPERSEDED -->

> **Note on decision-6**: the original decision-6 in the contract was
> framed as "all egg-* subcommands vs curated subset of existing
> subcommands." The human pivot reframed scope as "what are the
> agent-facing capabilities we need," which the Agent Capability Audit
> now drives. decision-6 is **superseded by decision-8 below**. Please
> pick "Other" on decision-6 in the contract (with response
> "superseded by decision-8") or leave it pending until the gate is
> closed.

<!-- egg-hitl-decision id=decision-8 -->

**What is the iteration-1 agent-facing tool set?**

- [ ] BRC + HITL + phase-context core (~15 tools): brc_propose/ack/nack/confirm/get_state/list_blocking; hitl_register_decision/request_feedback/check_answers; phase_get_context/get_assigned_tasks; progress_emit/signal_error/heartbeat; task_complete (Recommended — covers every agent role's BRC lifecycle + HITL + orientation on day one, without boiling the ocean)
- [ ] Everything in the capability audit (~30 tools): the core above + peer_send/poll/read_artifact, checkpoint_list/get/search, anchor_init/update/get, task_add_commit/mark_gap, overseer_query_status. Complete from day one but larger surface to get right.
- [ ] Only the surface that already has a CLI counterpart — skip new capabilities (brc_get_state, brc_list_blocking, peer_read_artifact, etc.) in iteration 1
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

**Q2: Drift-prevention contract: what's the acceptable mechanism for keeping tool schemas in sync with their backing handlers? Options: (a) CI test that asserts every @tool has a matching shared handler function; (b) a single shared handler module that both CLI and @tool dispatch into (recommended structurally); (c) auto-derive schemas from handler type annotations. Which flavour of 'no drift' do you want?**

> _Your answer here_

**Q3: Bootstrap guidance: even after MCP tools are registered, should we inject a short (<=200 word) 'these tools exist, prefer them over Bash' paragraph into the system prompt, or trust the SDK's native tool-discovery without a prompt nudge? Past experience suggests agents sometimes default to Bash even when a structured tool exists.**

> _Your answer here_

**Q4: Output shape for diagnostic tools: commands like `brc_get_state` / `checkpoint_search` naturally return JSON. Should the MCP tool return (a) the JSON payload verbatim, (b) a compact text summary for token economy, or (c) both as multi-content blocks? This affects tool-call token cost and agent comprehension.**

> _Your answer here_

**Q5: Authn/authz for network-transport MCP (if Option C lands instead of D): sandbox agents currently authenticate to the gateway via EGG_SESSION_TOKEN. Should a sandbox-facing MCP server reuse that token, introduce a new per-agent token, or piggyback on the existing EGG_LIFECYCLE_SECRET? What's the expected token lifecycle across agent restarts?**

> _Your answer here_

**Q6: Observability: should MCP tool calls generate distinct log events / checkpoints separate from the SDK's built-in tool_use stream? Today, Bash calls to egg CLIs show up as 'Bash' tool_use blocks in checkpoints; if they become first-class MCP tools they'll show up under distinct names — does the checkpoint browser need any changes to group or search them?**

> _Your answer here_

**Q7: Timeouts and cancellation: some agent verbs are fast (`hitl_register_decision`) and some can run longer (`checkpoint_search`, or a hypothetical `test_run`). What's the right timeout envelope, and should long-running operations be split into start/poll tool pairs rather than blocking?**

> _Your answer here_

**Q8: Rollout plan preference: should the MCP tool surface be enabled for all roles simultaneously, or gated behind a feature flag (e.g. EGG_MCP_TOOLS=true) during a burn-in window where we compare token/turn-count between the two modes?**

> _Your answer here_

**Q9: Capability audit completeness: the audit in this analysis was built from mission.md + the role prompts + the BRC state machine, but may miss less-common verbs (e.g. special tester or documenter tools). How should we validate completeness before committing to an iteration-1 surface? Options: (a) dogfood the first pass for one pipeline and track any Bash fallbacks in checkpoint logs; (b) survey recent checkpoint histories and enumerate every unique egg-* invocation; (c) trust the audit and add more verbs reactively.**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

## Complexity Assessment

**medium**. The change is contained to a small number of files: a
new `sandbox/egg_agent_tools/` package (per prior-cycle reviewer
guidance on placement) with one handler module per semantic grouping
(sdlc, brc, phase, checkpoint, peer, progress, anchor), a single-line
edit in `shared/egg_agent/client.py::run_agent_async` to pass
`mcp_servers`, optionally a `sandbox/agent-config/rules/` prompt
nudge, and refactoring where relevant CLIs exist so the argparse
dispatch shares handlers with the `@tool`-decorated functions. For
capabilities with no CLI counterpart (brc_get_state,
brc_list_blocking, peer_read_artifact), the handler is new and may
optionally get a CLI wrapper later. It is not a trivial one-file
change (enough surface across several capability groupings, a
drift-prevention mechanism, harness-coverage trade-offs, and a
scope/naming policy to settle), but it is not architectural surgery
either — the codepath already runs the SDK and already pays for
`mcp[cli]`; we are populating an options field that is currently
empty. No new service, no new network path, no cross-system
coordination.

---

*Authored-by: egg*


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

## Resolved Questions (captured for downstream phases)

**decision-1 (mechanism)**: Hybrid D + prompt nudge — in-process SDK MCP server via create_sdk_mcp_server + @tool, wired into egg_agent.client, plus a short system-prompt paragraph pointing agents at the mcp__* tools.

**decision-2 (handler derivation)**: Hybrid — auto-derive the skeleton from existing CLI/argparse-style signatures, allow per-tool overrides for descriptions/schemas where auto-derivation is insufficient.

**decision-3 (harness coverage)**: claude_agent_sdk harness only for iteration 1. Defer parallel registration for experimental EGG_HARNESS=egg until that harness graduates.

**decision-4 (CLI retention)**: Keep the sandbox/bin/egg-* CLIs indefinitely. Humans, tests, entrypoint scripts, and bash recovery wrappers depend on them; MCP tools are additive for agents. Do not deprecate.

**decision-7 (naming; supersedes decision-5)**: Semantic verbs grouped by function (what the agent is DOING). Examples: mcp__sdlc__register_open_question, mcp__brc__propose, mcp__phase__get_context. Rationale: agent's mental model is verbs; tool names are stable under CLI refactoring; capabilities with no CLI counterpart slot in naturally.

**decision-8 (scope; supersedes decision-6)**: BRC + HITL core for iteration 1 (~15 verbs): producer/reviewer lifecycle, HITL registration, heartbeat/progress, task completion, phase context orientation. Full capability audit (~30 verbs) is scoped to iteration 2 — tracked separately in issue #1917.

## Related issues filed during this refine

- #1915 — change_approach feedback via provide_input never reaches agents (found during this refine; workaround is direct send_message after reset)
- #1917 — iteration 2 follow-up for the remaining ~15 verbs from the full capability audit

## Notes for the plan phase

The agent-first framing is the authoritative frame; do not regress to CLI-taxonomy-based thinking. Name tools from the agent's perspective. If plan surfaces a verb that was missed in the refine capability audit, add it to the iteration-1 scope (if clearly core) or defer to #1917 (if optional).
