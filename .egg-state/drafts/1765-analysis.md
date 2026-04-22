# Analysis: Make egg-internal tools discoverable to sandbox agents

> Issue: #1765 | Phase: refine

## Problem Statement

Sandbox agents spawned into a pipeline phase have no structured, upfront
knowledge of the egg-internal CLIs they are expected to drive
(`egg-contract`, `egg-orch`, `egg-pipeline-watch`, `egg-checkpoint`,
`egg-sdlc`, `egg-health-inspect`, `egg-onboarding-docs`). They learn
the surface live, at the cost of real tool-call budget, by running
`--help`, `cat`ing the binaries, `grep`ping for subcommand names, and
`sed`ing line ranges out of the implementation. The issue body records
five tool calls just to rediscover `egg-contract add-decision` in the
first 12 minutes of a single refine phase — before any productive
work. The pattern repeats for every new CLI an agent encounters and
widens with every CLI we ship.

The desired outcome: an agent spawned fresh into a phase **already
knows** which egg-specific tools exist and what each top-level
subcommand does, without having to probe for it — and argument shape
is validated at the tool boundary rather than through argparse exit
codes. The mechanism must keep working in both public and private
network modes, must be additive (existing CLIs keep functioning for
scripts/tests/humans), and must not drift when new CLIs are added.

The issue explicitly asks that we **prioritize the best long-term
shape over the easiest to ship** — architectural fit,
maintainability, and discoverability are the criteria.

## Current Behavior

### Sandbox agent execution paths

Two distinct execution paths both read agent context from the
filesystem:

- **Interactive path** — `sandbox/entrypoint.py:run_interactive()`
  (line 1988) launches the Claude Code CLI via `os.execvpe()` after
  `sandbox/llm/runner.py:build_claude_cmd()` assembles the command.
  Claude Code loads `~/.claude/CLAUDE.md` and `~/.claude/settings.json`
  automatically at startup.
- **SDK path** — `shared/egg_agent/client.py:run_agent_async()`
  drives `claude_agent_sdk.query` programmatically. It passes
  `setting_sources=["project", "user"]` in `ClaudeAgentOptions`
  (client.py:202), which is the only thing that makes the SDK pick up
  the same `CLAUDE.md` / `settings.json` that the interactive CLI
  path uses.

Any discoverability mechanism that targets only one path leaves the
other blind. The SDK path today does **not** pass `mcp_servers` into
`ClaudeAgentOptions`, and the settings.json written at entrypoint
time (entrypoint.py:1031–1052) contains no `mcpServers` key.

### System-prompt assembly

`sandbox/entrypoint.py:setup_agent_rules()` (lines 913–968)
concatenates the files under `sandbox/agent-config/rules/` (in the
fixed order mission → environment → code-standards → test-workflow →
pr-descriptions → orchestrator → contract → checkpoint) into
`~/.claude/CLAUDE.md`, with `"\n\n---\n\n"` between sections. That
file is the static discovery surface the agent sees today.

Three of the seven egg CLIs have a dedicated rules file that
documents their subcommands as a markdown table:
`rules/orchestrator.md` (egg-orch), `rules/contract.md`
(egg-contract), `rules/checkpoint.md` (egg-checkpoint). The remaining
four — `egg-sdlc`, `egg-pipeline-watch`, `egg-health-inspect`,
`egg-onboarding-docs` — are not documented in rules at all. Nothing
in the sandbox currently ships a unified "here are all the egg tools"
catalog.

### CLI surface

`sandbox/bin/` contains seven egg CLIs. Three are symlinks into
`sandbox/egg_lib/*_cli.py`; the rest are standalone scripts. They
share a common argparse shape but do **not** share a schema source —
each CLI builds its own parser:

| CLI | Parser | Subcommand count (approx.) |
|-----|--------|----------------------------|
| egg-contract | `egg_lib/contract_cli.py:create_parser()` (line 1399) | 13 |
| egg-orch | `egg_lib/orch_cli.py:create_parser()` (line 1705) | 12 command groups, ~40 leaf subcommands |
| egg-checkpoint | `shared/egg_checkpoint/cli.py` via sandbox wrapper | 6 |
| egg-pipeline-watch | inline in `sandbox/bin/egg-pipeline-watch` | flags only |
| egg-sdlc | thin wrapper, dispatches to `egg_lib/sdlc_cli.py` | 4 |
| egg-health-inspect | inline Python, monolithic | flags only |
| egg-onboarding-docs | bash wrapper over `egg-sdlc --prompt` | flags only |

No CLI supports `--describe-schema`, machine-readable `--help`, or
manifest export today.

### MCP server today

`orchestrator/mcp_server.py` runs an MCP server on port 9850
(`DEFAULT_MCP_PORT`), exposing ~29 tools defined in
`orchestrator/mcp_tools.py` (e.g. `submit_task`, `get_status`,
`restart_phase`, the five #1759 deployment tools). The server is
consumed by the **orchestrator-side** Claude installation that powers
`/sdlc` — **not** by sandbox agents. There is no MCP client
configuration anywhere in the sandbox image:

- `entrypoint.py:1031` writes a `settings.json` without any
  `mcpServers` key.
- `client.py:194` builds `ClaudeAgentOptions` without passing
  `mcp_servers`.
- `sandbox/egg_lib/config.py` imports `MCP_SERVER_PORT` but only for
  forwarding constants; nothing resolves an MCP URL for the agent
  runtime.

The k8s deployment reinforces this gap:
`k8s/base/orchestrator-deployment.yaml` declares `containerPort: 9850`
(line 52), but `k8s/base/orchestrator-service.yaml` only exposes
`9849` (API), not `9850`. The
`allow-agent-to-orchestrator` NetworkPolicy
(`k8s/base/network-policies.yaml:56–81`) also whitelists only port
`9849` for agent egress. So even the networking plumbing is not
currently ready for a sandbox agent to speak MCP to the orchestrator.

### Network mode

`sandbox/egg_lib/network_mode.py` distinguishes public mode (direct
internet, proxy off) from private mode (proxy-on, no external
internet). Private mode only affects **external** egress — it does
**not** block intra-cluster traffic to the orchestrator, and it only
disables web tools at the SDK level
(`shared/egg_agent/client.py:160–161`, `disallowed_tools=["WebFetch",
"WebSearch"]` when `EGG_PRIVATE_MODE=1`). Orchestrator-hosted tools
therefore work in both modes without further accommodation, assuming
the Service/NetworkPolicy change above is in place.

## Constraints

- **Both execution paths must pick up the new tool surface.**
  Interactive claude CLI and the Agent SDK both need to see
  whatever we add. Fixing one and leaving the other uncovered
  re-creates today's gap.
- **Works in public and private network modes unchanged.** Private
  mode blocks external internet, not intra-cluster traffic, so any
  orchestrator-hosted mechanism is fine — but we must not assume the
  agent can reach the public internet to fetch a schema.
- **Additive to existing CLIs.** Scripts, tests, and humans depend on
  `sandbox/bin/egg-*`. New surface layers on top; CLIs keep working.
- **No long-running process inside the sandbox container.** The issue
  forecloses this variant explicitly. Composition has to stay
  orchestrator-hosted.
- **No drift when a new CLI lands.** The acceptance criteria require
  that adding a new `sandbox/bin/egg-*` shows up in the discovery
  mechanism automatically, without a doc-only step that can
  silently go stale.
- **Argument validation at the tool boundary.** The criterion calls
  out that agents should not have to discover errors through
  argparse exit codes — the mechanism should reject bad calls with a
  structured error before the CLI runs.
- **Deployment plumbing.** In the Kubernetes deployment, exposing a
  new port requires (a) an extra port on `orchestrator-service.yaml`
  (or a second Service), (b) a widened `allow-agent-to-orchestrator`
  NetworkPolicy, and (c) stable DNS (the `orchestrator` Service in
  `egg-system`).
- **Role-aware access.** Sandbox agents must not see pipeline-
  management tools like `submit_task`, `cancel_task`,
  `restart_phase`. Orchestrator-side Claude must not need
  agent-facing mutations like `add-decision`. The design has to keep
  those surfaces apart intentionally, not as a side-effect of
  "easiest to wire."
- **Blast radius on failure.** If the discovery mechanism is
  unavailable (server down, network hiccup), the CLIs need to remain
  the unambiguous fallback path.
- **Related work.** #1759 added operator/debug MCP tools and
  explicitly deferred "agent-facing tool discoverability" to this
  issue. #1759's framing (tools vs. skills, deterministic vs.
  judgment) is a useful prior for how we think about surface shape.

## Options Considered

### Option A — Status-quo polish: auto-generated tool reference doc + improved `--help`

**Approach**: Generate a single `AGENT-TOOLS.md` (or `/opt/egg-runtime/AGENT-TOOLS.md`) from each CLI's `--help` output at image-build time. Mount it in the sandbox and add a pointer to it from the system prompt. Improve each CLI's top-level `--help` to list subcommands with one-line descriptions where they currently dump argparse noise. Optionally expand `agent-config/rules/*.md` to cover all seven CLIs.

**Pros**:

- Very small engineering lift; can ship in days.
- No new runtime dependencies; no network plumbing.
- Works identically on both exec paths (both read the same
  filesystem).
- Works in public and private modes trivially.
- CLIs themselves unchanged — zero risk to existing scripts/tests.

**Cons**:

- Still shell-mediated. Agent constructs a string, invokes the CLI,
  parses text/JSON output. No structured tool-call stream.
- Argument validation still happens via argparse exit codes — fails
  the acceptance criterion about tool-boundary validation.
- Solves maybe half the pain: agents know *what* exists but still
  have to parse prose to learn *how* to invoke each subcommand.
- Prompt budget cost: the catalog has to live in the system prompt
  or be fetched on demand. Either way the agent burns tokens on
  tooling rather than on the task.

### Option B — Tools manifest + system-prompt injection

**Approach**: Ship `sandbox/tools.json` (or equivalent) declaring each CLI's subcommands, flags, types, and one-line descriptions. Render a compact summary into `~/.claude/CLAUDE.md` at spawn time (extend `entrypoint.py:setup_agent_rules()`). Keep the CLIs as the runtime surface.

**Pros**:

- Structured, machine-readable manifest — no prose parsing.
- Discovery is truly upfront: the agent sees the full surface in
  its system prompt from turn 1.
- Still no new runtime dependency or network wiring.
- Works in both exec paths (same filesystem surface).
- Straightforward to add parity tests that assert the manifest
  matches each CLI's argparse.

**Cons**:

- Runtime is still shell-mediated; argument validation happens in
  argparse on the agent side, not at a typed tool boundary.
- System-prompt bloat grows with every new subcommand — the whole
  manifest has to be either in the prompt or pre-loaded somehow.
- Doesn't compose with the MCP direction the orchestrator-side is
  already moving in (#1759). Parallel surface to maintain.
- "Additive" by design, so we accept permanent two-surface
  maintenance.

### Option C — Orchestrator-hosted MCP server for sandbox agents

**Approach**: Expose each in-scope CLI surface as typed MCP tools, served from the orchestrator process. Two plausible shapes: extend `orchestrator/mcp_server.py` with a second agent-facing tool namespace, or stand up a **separate** MCP server process for sandbox agents so operator tools (`submit_task`, `cancel_task`, `restart_phase`) stay structurally isolated from agent tools (`egg-contract add-decision`, `egg-orch consensus propose`). Wire `mcpServers` into `~/.claude/settings.json` at entrypoint time and pass `mcp_servers` into `ClaudeAgentOptions` in `egg_agent.client`. Widen `orchestrator-service.yaml` to expose the MCP port and extend `allow-agent-to-orchestrator` NetworkPolicy to allow that port.

Each CLI subcommand becomes a typed MCP tool with a JSON Schema. Agents call them as first-class tools in the same `tool_use` stream they already use, no shell round-trip. Under the hood the tool handler executes the same logic as the CLI (ideally by importing a shared handler module, not by shelling out to its own CLI, to avoid a re-entrant loop).

**Pros**:

- Satisfies every acceptance criterion:
  - Discovery: MCP advertises the tool list at connect — the agent
    sees it without probing.
  - Argument validation: JSON Schema rejects bad calls at the
    boundary, before the handler runs.
  - No drift: if schemas are derived from the CLI (Decision D4),
    a new `sandbox/bin/egg-*` that follows the pattern shows up
    automatically.
  - Both modes: MCP-to-orchestrator is intra-cluster traffic,
    unaffected by public/private.
  - Additive: CLIs keep working; scripts/tests/humans unaffected.
- Composes with the direction the orchestrator-side MCP surface is
  already headed (#1759). Same transport, same SDK client, same
  operational model (one server to observe, rate-limit, redact).
- Structural access control: sandbox agents get a different tool
  namespace than operator Claude. Prevents accidental reach into
  pipeline-control tools.
- Tool-use stream is what the SDK and claude CLI are already
  optimized for — no prompt-budget tax for "here's how each flag
  works"; the schema lives in the tool definition.

**Cons**:

- Largest engineering surface of the four:
  - New MCP service wiring (or tool-namespace split within
    existing server).
  - MCP client config in both sandbox exec paths.
  - Service port + NetworkPolicy change in k8s.
  - Tool-handler layer for each CLI (ideally a shared
    command-execution core that both CLI and MCP call into).
- Adds a runtime dependency on the orchestrator's MCP server.
  Graceful degradation needs explicit thought: when MCP is down,
  does the agent fall back to CLI? The failure mode has to be
  designed, not emergent.
- Source-of-truth decision (Decision D4) can be controversial:
  hand-written schemas drift; auto-generated schemas bind the
  CLI's argparse to MCP expectations forever.
- Two-surface maintenance (CLI + MCP) until/unless CLIs are
  eventually deprecated (explicit non-goal right now).

### Option D — Hybrid: Agent SDK `@tool` decorators on the orchestrator side

**Approach**: Instead of an MCP server, wrap each CLI as a Python function on the orchestrator side and register the functions as Agent SDK tools via the SDK's `@tool` decorator. Agents see them as first-class tools in the `tool_use` stream. Tools execute in the orchestrator process and return structured data to the agent.

**Pros**:

- Similar leverage to C (typed tools, tool-use stream, schema
  validation) without a separate server process.
- Can reuse the same handler core as Option C if structured well.

**Cons**:

- **The interactive `claude` CLI path inside the sandbox does not see
  `@tool` registrations made on the orchestrator side** — this is
  called out in the issue body, and it means this option has a
  coverage gap by construction. Any human or automated session that
  `exec`s into the container to run `claude` directly is back to
  shell-mediated discovery.
- Tight coupling to the Agent SDK. We've already chosen MCP as the
  orchestrator's external tool surface (#1759); going SDK-only here
  bifurcates our tooling story without an architectural benefit.
- Debuggability: the tool runs in-process on the orchestrator, so
  sandbox-side logs don't tell the full story of what happened.
- Composition story is weaker: no separate protocol to point other
  tooling at.

## Recommended Approach

**Option C, built so that Option B's manifest falls out of it for free**.

C is the only shape that meets every acceptance criterion — typed
tools, connect-time discovery, boundary-level schema validation,
no-drift discipline, mode-independence, additivity — and it's the
only one that composes with the direction the orchestrator-side
tooling has already committed to. Options A and B leave shell-
mediated invocation and argparse-exit-code validation in place; they
are at best interim improvements. Option D gives up the interactive
`claude` path.

The practical construction that makes C tractable:

1. **Extract a shared command-handler layer** from the CLI subparsers
   so that each egg-CLI subcommand has one authoritative handler
   function with typed inputs. The CLI argparse wiring calls into it;
   the new MCP tool handler calls into it; they cannot drift because
   they share code.
2. **Derive MCP tool schemas from the same source** (Decision D4 —
   whether that source is argparse introspection, a shared
   dataclass/Pydantic module, or hand-written schemas with a parity
   test is the open call). This is the lever that satisfies the
   "auto-discovery for new CLIs" acceptance criterion.
3. **Split the MCP surface** (Decision D2) so sandbox agents and
   orchestrator-side Claude see different tool namespaces by
   construction. A separate MCP server process is the cleanest cut;
   a tool-namespace split on the existing server with per-caller
   ACLs is acceptable if operationally simpler.
4. **Wire both sandbox exec paths to the agent MCP endpoint**
   (Decision D8) — `mcpServers` in `~/.claude/settings.json` covers
   the interactive CLI; `mcp_servers` in `ClaudeAgentOptions` covers
   the SDK path. This is non-negotiable: both paths must work.
5. **Update deployment** — expose the MCP port on
   `orchestrator-service.yaml`, widen
   `allow-agent-to-orchestrator` NetworkPolicy, confirm private-mode
   egress still works (it does, because this is intra-cluster).
6. **Rewrite the rules files** (`rules/orchestrator.md`,
   `rules/contract.md`, `rules/checkpoint.md`, plus new files for
   the currently-undocumented CLIs) to say "prefer the typed tools;
   CLIs remain available as a fallback and for scripts," and reference
   a generated catalog so the rules themselves don't drift (Decision
   D7 shapes how aggressively to signal this).
7. **Keep the generated manifest as a by-product** — since schemas
   are derived from a shared source, emitting a markdown catalog of
   "what tools exist, what they do" into the system prompt as a
   belt-and-braces guide is nearly free. That covers the scenario
   where the MCP server is momentarily unreachable: the agent still
   has a correct static catalog, and CLIs still work.

This shape is the most work of the four options, but it is the only
one that both solves the problem fully and composes with where the
orchestrator-side tooling is already headed. The issue explicitly
asks for the best architectural fit rather than the fastest ship — C
is the answer.

## Open Questions

All decisions and feedback questions below are registered via
`egg-contract`. The blocks inline here are the rendered markdown
so the human can check boxes directly.

### Decisions (pick one)

<!-- egg-hitl-decision id=decision-1 -->

**Which architectural shape should we adopt for making egg-internal CLIs discoverable to sandbox agents?**

- [ ] A — Auto-generated tool reference doc mounted in sandbox + improved --help
- [ ] B — tools.json manifest injected into agent system prompt (still shell-mediated)
- [ ] C — Orchestrator-hosted MCP server dedicated to sandbox agents (typed tools in tool_use stream)
- [ ] D — Agent SDK @tool decorators on orchestrator side (SDK path only, does not cover interactive claude CLI)
- [ ] Hybrid: B + C (markdown manifest now, typed MCP later)
- [ ] Hybrid: A + C (reference doc as fallback, MCP as primary)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-2 -->

**If we adopt Option C: should the agent-facing MCP surface be a SEPARATE server process from the existing orchestrator MCP server (port 9850), or a second tool namespace on the existing server with per-caller ACLs?**

- [ ] Separate MCP server (new port, dedicated process, clean blast-radius split between operator tools and agent tools)
- [ ] Same server, separate tool namespace with role-based ACL (one process to operate, ACL checks per tool call)
- [ ] Same server, no split (rely on tool-level permission checks only — simpler but agents can see submit_task/cancel_task)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**If we adopt Option C: how granular should the MCP tool surface be?**

- [ ] One MCP tool per CLI subcommand (e.g. egg_contract_add_decision, egg_orch_consensus_propose, …) — ~40+ tools total, maximum discoverability
- [ ] One MCP tool per top-level CLI (e.g. egg_contract, egg_orch, …) with a subcommand arg — ~7 tools, compact but schemas become unions
- [ ] One MCP tool per CLI command group (e.g. egg_orch_consensus, egg_orch_message, egg_contract_decision, …) — ~15–20 tools, middle ground
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**What should be the source of truth for MCP tool schemas, to prevent drift from the CLIs?**

- [ ] Auto-generate MCP schemas from each CLI's argparse parser (single source of truth, requires argparse introspection helper)
- [ ] Hand-write MCP schemas alongside CLI subparsers; enforce parity via a test that diffs the two (explicit, easier to customize)
- [ ] Extract both CLI and MCP from a shared Pydantic/dataclass schema module (refactor CLIs to consume the same schemas)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**Which CLIs are in scope for exposure as discoverable tools in this issue?**

- [ ] All seven: egg-contract, egg-orch, egg-checkpoint, egg-sdlc, egg-pipeline-watch, egg-health-inspect, egg-onboarding-docs
- [ ] Agent-mutating only: egg-contract + egg-orch (message/consensus/phase/signal) — the highest-traffic surfaces
- [ ] Agent-mutating + read-only observability: egg-contract, egg-orch, egg-checkpoint, egg-pipeline-watch
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-6 -->

**How should agent role/phase gating be enforced at the new tool boundary?**

- [ ] Filter the tool list returned to each agent at MCP connect time based on EGG_AGENT_ROLE (agent sees only tools it is allowed to invoke)
- [ ] Always advertise the full tool list; enforce role checks per tool invocation and return structured errors on deny (current gateway pattern)
- [ ] Mix: filter destructive tools at connect, allow-list informational tools unconditionally
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-7 -->

**Should existing sandbox/bin/egg-* CLIs remain first-class after the typed tool surface lands, or be marked fallback-only?**

- [ ] Keep CLIs as a first-class, fully supported surface (scripts, tests, humans, and agents can all use either; typed tools are preferred but equivalent)
- [ ] Mark CLIs as fallback-only in agent rules (agents prefer MCP tools; CLIs remain for scripts/tests/humans but agents discouraged)
- [ ] Keep CLIs first-class now; schedule deprecation of agent use after a bake-in period
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-8 -->

**How should the MCP client config reach sandbox agents so both execution paths pick it up?**

- [ ] Write mcpServers into ~/.claude/settings.json at entrypoint and pass mcp_servers into ClaudeAgentOptions in egg_agent.client (both paths covered)
- [ ] Settings.json only (interactive claude CLI gets it; SDK path relies on settings.json auto-load — verify SDK actually reads mcpServers from settings.json)
- [ ] Env var EGG_MCP_URL + runtime-resolver in both runner and client.py (config survives file mutations; explicit code path)
- [ ] Other (explain in reply)

### Open-ended feedback

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: Is there a named contract for how heavy agent-to-MCP traffic can become (per-agent or per-pipeline call rate)? Today DEFAULT_RATE_LIMIT is 30 req/min on the orchestrator MCP server; with BRC poll loops + message send + consensus propose, agents may exceed that once tools are first-class.**

> _Your answer here_

**Q2: Are there other egg CLIs on the near-term roadmap that this discovery mechanism should accommodate out of the gate (beyond the seven listed)? Specifically any planned for overseer, retries, or release tooling?**

> _Your answer here_

**Q3: For the k8s deployment path: is it acceptable to widen the egg-agents → egg-system orchestrator NetworkPolicy to allow port 9850, and add port 9850 to the orchestrator Service? Today only 9849 is reachable from agents and only 9849 is exposed on the Service.**

> _Your answer here_

**Q4: If a new agent CLI is added under sandbox/bin/, what is the preferred mechanism for keeping it in sync with the discoverable tool surface — CI check, generated code, runtime reflection, or docs update? (The acceptance criteria require this to not drift.)**

> _Your answer here_

**Q5: Should the same discoverability mechanism also be exposed to the overseer agent, which already has its own allow-list of commands (overseer.md) — or does overseer remain on the current CLI-only surface?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

## Complexity Assessment

**high**

Rationale: Option C (the recommended shape) is an architectural
addition that spans three components — the orchestrator (new MCP
server process or tool namespace + shared handler layer), the
sandbox image (MCP client config in two exec paths, entrypoint.py
changes, settings.json format extension), and the k8s deployment
(Service port exposure + NetworkPolicy widening). It also requires
extracting a shared command-handler layer out of argparse CLIs,
which is a cross-cutting refactor. Several decisions (D1/D2/D3/D4)
each move the blast radius materially, and the network/deployment
change needs validation in both public and private modes.

Even the lightest-weight option (A) still touches agent-config rules,
entrypoint.py, and every CLI's `--help` layout — medium at the low
end. The recommended option is firmly high.

---

*Authored-by: egg*
