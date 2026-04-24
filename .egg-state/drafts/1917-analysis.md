# Analysis: Ship iteration 2 of agent-facing MCP tools — cover full capability audit (~15 more verbs)

> Issue: #1917 | Phase: refine

## Problem Statement

Iteration 1 of the agent-facing MCP-tool surface (#1765 / PR #1920, merged
f24110b71) shipped a **BRC + HITL core** of ~15 verbs and the mechanism to
add more (in-process `create_sdk_mcp_server` + `@tool` wrappers +
handler-sharing with the shell CLIs + a drift gate). Iteration 1 explicitly
deferred the remaining ~15 verbs from the #1765 capability audit —
peer-artifact reads, checkpoint browsing, anchor management, overseer
escalation, task-gap recording, and the full contract read/write surface —
to this issue.

The capability gap is actively hurting live pipelines. #1955 captured a
refine-phase reviewer that ran `egg-contract show --json 2>&1 | python3 -c
"import json,sys; c=..."` because no `mcp__sdlc__show_contract` exists. That
is exactly the CLI-via-Bash re-discovery pattern #1765/#1946 set out to
eliminate — iteration 1 covered the `add-decision`/`add-feedback`/
`complete-task` write subset but left `show`/`add-commit`/`update-notes`/
`complete-phase`/`verify-criterion` as "agent shells out to Bash + `python3`
to parse JSON".

The desired outcome: every verb the #1765 capability audit identified is
either (a) shipped as a first-class MCP tool in the iteration-1 mechanism,
(b) explicitly documented as human-operator-only with rationale, or
(c) explicitly superseded by another tool. Agents spawned into any phase
should never need to shell out to `egg-*` CLIs for normal agent-role work.

## Current Behavior

### What iteration 1 actually shipped

`sandbox/egg_agent_tools/tools/*.py` registers **18** SDK-visible tools
(the docs at `docs/reference/agent-tools.md` still say "15" — iteration 1
shipped 15, and #1897 added 3 more event-driven message primitives that
landed under the `brc` namespace; the doc is slightly stale but the surface
is the below):

| Namespace | Verbs (18 total) |
|---|---|
| `mcp__sdlc__` (3) | `register_open_question`, `request_feedback`, `check_hitl_answers` |
| `mcp__brc__` (9) | `propose`, `ack`, `nack`, `confirm`, `get_state`, `list_blocking`, `wait_for_event`, `wait_loop`, `send_heartbeat` |
| `mcp__phase__` (2) | `get_context`, `get_assigned_tasks` |
| `mcp__progress__` (3) | `emit`, `signal_error`, `heartbeat` |
| `mcp__task__` (1) | `complete` |

Grounded at:
- Registrations: `sandbox/egg_agent_tools/tools/__init__.py:15-51` and the
  per-namespace modules `brc.py`, `message.py`, `phase.py`, `progress.py`,
  `sdlc.py`, `task.py` (each exports a `REGISTRATIONS: list[ToolRegistration]`).
- Wiring: `shared/egg_agent/client.py::run_agent_async` merges the
  per-namespace dict into `options.mcp_servers` when `EGG_MCP_TOOLS` is not
  falsy (flag flipped on by default in #1946).
- Handlers: `sandbox/egg_agent_tools/handlers/{brc,message,phase,progress,sdlc,task}.py`
  are pure sync functions that raise `GatewayError`/`HandlerError`; wrappers
  invoke them via `asyncio.to_thread` and translate errors to
  `{is_error: True, content: [...]}`.
- Drift gate: `tests/tools/test_mcp_cli_drift.py` asserts every tool with
  `cli_command` dispatches the same handler as its CLI; tools that are
  new capabilities (no CLI) explicitly set `cli_command=None`.
- Nudge: `sandbox/egg_agent_tools/server.py::_render_nudge()` generates the
  `SYSTEM_PROMPT_NUDGE` from `TOOL_NAMESPACES` at import time;
  `test_server.py::test_prompt_nudge_drift` keeps the two sides symmetric.

### Capabilities the audit surfaced that iteration 1 did **not** ship

From `.egg-state/drafts/1765-analysis.md:317-335` (the "Candidate
agent-facing tool surface (~30 tools)" list) minus what iteration 1
actually registered. Grouped by semantic function:

1. **Contract read/write (partial coverage; #1955 evidenced live):**
   - `egg-contract show` → no `mcp__*` equivalent. Today: `egg-contract
     show --json 2>&1 | python3 -c ...` (reviewer_refine in `issue-1556`
     pipeline).
   - `egg-contract add-commit` (link SHA to a task, distinct from
     `complete-task`).
   - `egg-contract update-notes` (append implementation notes to a task).
   - `egg-contract complete-phase` (mark a phase complete on the
     contract, separate from `orch-phase complete`).
   - `egg-contract verify-criterion` — REVIEWER role gating (see
     `contract_cli.py:1386`).
2. **Checkpoint surface** (`sandbox/bin/egg-checkpoint` → 6 subcommands,
   defined in `shared/egg_contracts/checkpoint_cli.py:1942-2063`): `list`,
   `show`, `browse`, `context`, `cost`, `search`. None are exposed to agents
   today.
3. **Peer / inter-agent messaging (non-event primitives):**
   - `egg-orch message send` — directed HANDOFF/STATUS/other typed sends.
     Today: agents shell out; no MCP wrapper.
   - `egg-orch message poll` — non-blocking or short-wait message read.
     Today: shell-only.
   - `brc_peer_read_artifact` — **new capability, no CLI counterpart**.
     Iteration 1's TD9 explicitly deferred this as valued-but-not-critical.
     Reviewers today dig through `.egg-state/brc-history/*.json` by hand to
     see a peer's prior review text.
4. **Anchor operations** (REST only at `orchestrator/routes/anchors.py`;
   endpoints `POST /api/v1/anchors/<agent_id>`, `GET`, `DELETE`,
   `GET /team/<pipeline_id>`, `POST /gc/<pipeline_id>`). Agent rules at
   `sandbox/agent-config/rules/orchestrator.md:20-24` reference
   `egg-orch anchor init/update/show/validate/cleanup` commands — but
   **no such subcommands actually exist** in
   `sandbox/egg_lib/orch_cli.py` (confirmed via `grep -n add_parser`). The
   docs advertise a CLI that was never built; agents today cannot populate
   an anchor at all through the shell surface.
5. **Overseer escalation:** `egg-orch overseer alert` exists in
   `sandbox/egg_lib/orch_cli.py:2598` and is the designated path for the
   overseer agent role to raise anomalies — no MCP wrapper yet.
6. **Task-gap recording:** tester → coder coverage handoff. Today: informal
   note in a NACK body or a contract decision. **No CLI, no MCP, no
   dedicated endpoint.**
7. **Overseer pipeline-status query:** the monitor script uses
   `GET /api/v1/pipelines/<id>/status` directly (see
   `sandbox/overseer_monitor.py:74-78`) — no MCP wrapper, but the overseer
   role is the only agent that needs it on the hot path.
8. **Iteration-1 carry-over (TD9 / `phase_get_context` best-effort
   fields):** `active_peers`, `reviewer_peers`, `hitl_pending` were marked
   optional in iter 1 (the architect's returned-fields split). Iter 2 is
   the documented moment to promote them to first-class.

### Verbs the audit listed that are **not** agent-facing

These exist in the CLIs but are operator/orchestrator-internal and should
not be wrapped:
- `egg-orch health/pipeline/container/gateway/env/decision` — ops/debug.
- `egg-contract agent-{status,start,complete,fail,next}` — orchestrator
  drives these via gateway, not the agent.
- `egg-contract populate`/`validate` — one-off operator tooling.
- `egg-orch consensus withdraw`/`message status` — rarely used, debug.

### Existing agent rule docs still steer agents at the CLIs

`sandbox/agent-config/rules/contract.md:9,17`,
`sandbox/egg_lib/data/hitl_editing_rules.md:19`, and
`sandbox/agent-config/rules/orchestrator.md:20-24` all name the CLI forms.
Iteration 1 added `Prefer this over ...` language for the MCP verbs it
shipped; iteration 2 must do the same for every new verb or the
capability-audit acceptance criterion fails (AC: "agents never need to
shell out to egg-* CLIs for normal agent-role work").

## Constraints

- **Reuse the iteration-1 mechanism.** AC3 pins this: "The mechanism from
  iteration 1 (in-process SDK MCP via `create_sdk_mcp_server` per
  decision-1 of #1765) is reused — this issue adds verbs, not a new
  mechanism." That means: `@tool` wrappers → `handlers/*.py` →
  `make_gateway_request` (or direct module calls for offline verbs); sync
  handlers raising typed errors; `asyncio.to_thread` at the wrapper layer;
  structured error content on exception; drift gate for every verb with a
  CLI counterpart.
- **Handler rule — MUST NEVER `sys.exit`.** Inherited from iteration 1
  (TD8). Every new handler must return a dict or raise; CLI shims that
  still call `sys.exit` stay in their own process and are fine.
- **Authz by construction.** Sandbox agents cannot import the
  orchestrator's MCP tools (submit_task, cancel_task, …). Anything added
  here runs in the agent's own interpreter and calls the gateway —
  orchestrator-privileged operations (spawn, cancel, restart) stay out.
- **Dual-harness reality.** Only the `claude_agent_sdk` harness registers
  the new tools (iter 1 decision-3). The experimental `EGG_HARNESS=egg`
  path has its own tool-registry wrapper in
  `shared/egg_harness_integration/egg_tools.py` that wraps CLIs as
  subprocess tools. Iter 1 deferred parallel wiring; iter 2 should decide
  whether to do the same or extend.
- **No new network service.** Handlers reach the existing gateway path
  via `make_gateway_request`. Anything new (task-gap, peer-read-artifact)
  that needs a new endpoint must land an orchestrator route alongside the
  handler.
- **Private-mode network isolation.** No new PyPI deps at runtime —
  anything new must reuse existing sandbox deps (claude-agent-sdk, stdlib,
  already-baked packages).
- **Rule-doc churn.** Every tool with a `Prefer this over ...` note needs
  a matching line in `sandbox/agent-config/rules/*.md` and
  `sandbox/egg_lib/data/hitl_editing_rules.md`. Iteration 1's drift test
  covered the `TOOL_NAMESPACES` → `SYSTEM_PROMPT_NUDGE` direction; it does
  **not** cover the rule docs, which will drift silently without a
  similar gate.
- **SDK pin.** `claude-agent-sdk>=0.1.65,<0.2` in
  `sandbox/pyproject.toml`. Any new `@tool` feature (e.g. streaming,
  long-running) must still work inside that pin.
- **60-second MCP tool timeout.** Iter 1 documented this as a limitation.
  `peer_read_artifact` on a large transcript or `checkpoint_search`
  scanning many checkpoints could exceed 60s; need a design that either
  paginates, caps by default, or flags when a start/poll/complete
  triplet is needed.
- **Blocked on #1765 shipping.** Iteration 1 has merged (2ee6bc01d has it
  in history via f24110b71); this precondition is satisfied. Flag was
  flipped default-on in #1946.

## Options Considered

The core mechanism is fixed by AC3; the open design space is **which
verbs, how they're grouped, and how the new capabilities (no-CLI verbs)
are surfaced**. The options below are design-level choices the plan phase
will pin down.

### Option A: Minimum-viable iter 2 — ship the #1955 gap + peer_read_artifact only (~7 tools)

**Approach**: Close the evidenced #1955 contract-read pain first, plus the
reviewer-forensics win (`brc_read_peer_artifact`). Defer checkpoint, anchor,
overseer, task-gap to a third iteration.

Verbs (~7):
- `mcp__sdlc__show_contract`
- `mcp__task__add_commit`, `mcp__task__update_notes`
- `mcp__phase__complete_phase`
- `mcp__sdlc__verify_criterion`
- `mcp__brc__read_peer_artifact`
- `mcp__overseer__alert`

**Pros**:
- Smallest surface → lowest churn; fits in one PR comfortably.
- Addresses the live pain (#1955) and the iter-1 explicit-deferral (TD9).
- All seven have clear CLI or REST counterparts, so the drift gate
  remains tight.

**Cons**:
- Does **not** meet AC1 ("every verb surfaced in #1765 capability audit is
  …") — the audit listed checkpoint, anchor, task-gap, full peer-messaging.
- Creates a third iteration later to finish, duplicating review cycles and
  rule-doc touch points.
- The issue title says "~15 more verbs"; this is 7, so the scope
  expectation is mismatched.

### Option B: Full audit — ~15 verbs across contract / checkpoint / peer / anchor / overseer / task-gap namespaces

**Approach**: Ship every verb the #1765 audit identified as agent-facing.
Explicitly document the human-only CLIs as not-for-MCP with rationale
(AC1.b). New namespaces as needed (`checkpoint`, `overseer`, `anchor`,
maybe `peer`).

Candidate verbs (~15):

| Proposed tool | Backing mechanism | Priority |
|---|---|---|
| `mcp__sdlc__show_contract` | `egg-contract show` | P0 (#1955) |
| `mcp__task__add_commit` | `egg-contract add-commit` | P0 |
| `mcp__task__update_notes` | `egg-contract update-notes` | P0 |
| `mcp__phase__complete_phase` | `egg-contract complete-phase` | P0 |
| `mcp__sdlc__verify_criterion` | `egg-contract verify-criterion` (REVIEWER-gated) | P0 |
| `mcp__brc__read_peer_artifact` | new handler, reads `.egg-state/brc-history/*.json` or new endpoint | P1 (iter-1 TD9) |
| `mcp__brc__send_message` | `egg-orch message send` | P1 |
| `mcp__brc__poll_messages` | `egg-orch message poll` | P1 |
| `mcp__overseer__alert` | `egg-orch overseer alert` | P1 (overseer role needs it) |
| `mcp__checkpoint__list` | `egg-checkpoint list` | P1 |
| `mcp__checkpoint__show` | `egg-checkpoint show` | P1 |
| `mcp__checkpoint__search` | `egg-checkpoint search` | P1 |
| `mcp__anchor__init` | `orchestrator/routes/anchors.py` REST (no CLI — see Q3) | P2 |
| `mcp__anchor__update` | same | P2 |
| `mcp__anchor__get` | same | P2 |
| `mcp__task__mark_gap` | **new orchestrator endpoint**; tester → coder coverage-gap handoff | P2 (no existing surface) |

**Pros**:
- Satisfies AC1 / AC2 cleanly — one iteration, one PR to review,
  rule-doc sweep happens once.
- Unblocks overseer-role and task-gap workflows that currently have no
  structured surface.
- Scope matches the issue's "~15 more verbs" estimate.

**Cons**:
- `task_mark_gap` and the anchor trio require **new orchestrator
  endpoints or CLI scaffolding** before the MCP wrapper can land → these
  are NOT drop-in wrappers. Concretely:
  - Anchor: the REST endpoints exist but the `egg-orch anchor *` CLI
    referenced in `sandbox/agent-config/rules/orchestrator.md:20-24`
    doesn't exist in `sandbox/egg_lib/orch_cli.py`. Either wrap REST
    with no CLI/no drift counterpart (iter-1 pattern for new
    capabilities) or add the CLI first.
  - `task_mark_gap`: no contract field, no endpoint, no CLI. Needs a
    design decision on whether to re-use `contract decisions`,
    invent a new contract field, or add a dedicated endpoint.
- Larger PR → longer review cycle, more churn risk.
- Checkpoint `search` / `read_peer_artifact` may hit the 60-s MCP
  timeout on large data and need pagination or a start/poll pattern.

### Option C: Staged iter 2 — two PRs (P0+P1 first, P2 in a follow-up)

**Approach**: Split iteration 2 across two PRs against the same issue:

- **PR-2a** (this issue, first merge): P0 + P1 from Option B — contract
  read/write, peer-read-artifact, checkpoint core 3, overseer alert,
  message send/poll. ~10 tools. All have clear CLI or REST counterparts;
  drift gate stays tight.
- **PR-2b** (this issue, second merge): P2 from Option B — anchor trio,
  `task_mark_gap`. ~4 tools. Lands the new orchestrator endpoints or CLI
  scaffolding alongside.

**Pros**:
- First PR is digestible and unblocks the known-hot pains.
- Second PR is scoped to the design-and-build-backend work, keeping review
  focused.
- Preserves AC1 — the audit is fully covered by the end of iter 2.

**Cons**:
- Two review cycles for one issue; doc updates happen twice.
- Rule-doc sweep needs to be staged (first PR lists the shipped tools;
  second PR adds the remaining ones).
- Coordination cost if one PR stalls behind the other.

### Option D: Option B + iter-1 carry-over fields promoted in the same PR

**Approach**: Option B plus: promote `active_peers`, `reviewer_peers`,
`hitl_pending` on `mcp__phase__get_context` from best-effort to first-class
required fields, as flagged in iter-1's TD9 and reviewer_plan's
non-blocking note.

**Pros**:
- Closes the iter-1 "known limitation" at the same time as the verb
  expansion.
- Single rule-doc sweep for "what phase_get_context returns now".

**Cons**:
- Scope creep; `phase_get_context` payload change is a tool-shape change,
  not a verb addition. Could be a separate, smaller PR.
- Risks conflicting with iter-1's burn-in — if some pipelines still treat
  those fields as optional, a hard promotion might break them.

## Recommended Approach

**Option B** (full audit, one iteration) with **explicit early-split
fallback to Option C if the anchor/task_mark_gap design stalls**.
Rationale:

1. **The issue literally asks for this.** The AC says "every verb
   surfaced … is either (a) shipped, (b) explicitly human-only, or
   (c) superseded." Options A and D don't meet AC1.
2. **One rule-doc sweep is cheaper than two.** The rule-doc update is
   the most tedious part of both iterations and is high-risk for drift;
   doing it once across all verbs minimises the divergence window.
3. **The mechanism is fixed** (AC3). Every new tool is a schema, a
   handler, a wrapper, and a registration — the *scaling factor* is
   low. The open design is bounded to:
   - `task_mark_gap` contract shape (see Q5).
   - Anchor CLI vs. REST-only wrapping (see Q3).
   - Whether checkpoint ships 3 or all 6 verbs (see Q4).
4. **Option C remains a safety valve.** If the plan-phase architect
   concludes that `task_mark_gap` and the anchor trio need more design
   than one iteration can absorb, splitting to Option C is mechanical
   — the P0/P1 verbs stand alone.

Iter-1 carry-over (phase-context field promotion) is explicitly **not
bundled** — that's a tool-shape change to an existing tool, which
deserves its own review (see Q6). A separate, smaller PR is cleaner.

## Open Questions

All questions below are **registered on the contract** (14 decisions +
1 feedback request with 4 sub-questions = 18 open items for the human).

| ID | Question | Shape |
|---|---|---|
| decision-1 | Iter-2 scope shape (A/B/C/D) | multi-choice |
| decision-2 | Anchor approach (REST-wrap vs CLI-first vs defer) | multi-choice |
| decision-3 | Checkpoint coverage (all 6 / core 3 / core 3+context / core 3+cost) | multi-choice |
| decision-4 | `task_mark_gap` shape (endpoint+CLI / reuse decisions / new section / no-CLI) | multi-choice |
| decision-5 | Namespace strategy (new namespaces / existing only / hybrid) | multi-choice |
| decision-6 | `phase_get_context` field-promotion timing | multi-choice |
| decision-7 | `verify_criterion` REVIEWER-role gating | multi-choice |
| decision-8 | Peer-read-artifact source of truth (files / endpoint / hybrid) | multi-choice |
| decision-9 | `EGG_MCP_TOOLS` flag fate | multi-choice |
| decision-10 | Harness coverage (defer / include / track) | multi-choice |
| decision-11 | Rule-doc drift gate (two-way / one-way / skip) | multi-choice |
| decision-12 | Tool-timeout contingencies (paginate / triplet / accept 60s) | multi-choice |
| decision-13 | CLI-counterpart policy for no-CLI capabilities | multi-choice |
| decision-14 | `mcp__brc__send_message`/`poll_messages` semantics vs future REQUEST/REPLY subsystem | multi-choice |
| feedback-1 Q1 | Documenter/doc-updater scope — which docs need updates? | open-ended |
| feedback-1 Q2 | Acceptance metric shape for iter 2 | open-ended |
| feedback-1 Q3 | Any verbs iter 1 surfaced as unfinished that this analysis missed? | open-ended |
| feedback-1 Q4 | Publish an explicit human-operator-only list (AC1.b)? | open-ended |

### Plan-phase carry-over notes (from reviewer_refine non-blocking feedback)

- **Option C split-trigger** (concretised): split to Option C if
  decision-2 resolves to `opt-2` (add CLI first) OR decision-4 resolves
  to `opt-1` (new endpoint + new contract field). Both require
  pre-MCP orchestrator work that naturally fences off into PR-2b.
- **Rule-doc phantom-anchor-CLI retraction**: if decision-2 resolves to
  `opt-1` (REST-wrap with `cli_command=None`),
  `sandbox/agent-config/rules/orchestrator.md:20-24` must be rewritten
  to point at `mcp__anchor__*` and explicitly retract the
  `egg-orch anchor init/update/show/validate/cleanup` references.
- **Docs-refresh must-include** for `feedback-1 Q1`:
  `docs/reference/agent-tools.md` lines 25, 39, 41, 126 (all "15 tools"
  claims) and line 293 ("15 additional verbs" prose) all need
  refreshing as iter 2 merges.
- **`task_mark_gap` as potential sub-issue**: if decision-4 resolves to
  `opt-1` or `opt-3`, plan phase should consider filing a dedicated
  sub-issue so the endpoint/contract-field design work does not
  silently block the iter-2 PR.
- **P0 task decomposition hint**: `task_add_commit` and
  `task_update_notes` share a handler shape (both write the `tasks[]`
  entry on the contract) and can likely be one task in the task
  planner's decomposition.
- **Close-proximity completion verbs**: `mcp__task__complete`,
  `mcp__phase__complete_phase`, and `mcp__task__add_commit` need tool
  `description` fields that explicitly name their state-machine effect
  (same spirit as #1944) so an agent picks correctly without
  re-deriving the taxonomy.
- **`mcp__sdlc__show_contract` payload shape**: live contracts can
  accumulate to many KB; plan phase should consider optional
  field-projection (`fields=["decisions","current_phase"]`) keeping
  the full dump as an opt-in.

---

## Complexity Assessment

**high** — iteration 2 adds ~15 verbs across **new namespaces**,
introduces **new capabilities** (peer-read-artifact, task-gap) that
require either new orchestrator endpoints or new CLI scaffolding,
touches multiple rule-doc files, and has cross-cutting concerns (drift
gate, timeout, harness parity). It is mechanically similar to iter 1 but
touches more surfaces and has more open design choices. Parallelisable
per-namespace if Option C is picked.

---

*Authored-by: egg*
