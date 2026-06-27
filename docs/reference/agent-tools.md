# Agent MCP Tools Reference

> Sandbox agents can call pipeline lifecycle operations (BRC consensus,
> HITL decisions, phase context, progress signals, task completion)
> plus the gateway's Confluence/Jira routes through first-class MCP
> tools on the Claude Agent SDK `tool_use` stream, instead of shelling
> out to `egg-contract` / `egg-orch` (or the `confluence` / `jira`
> wrappers) via `Bash`.

The tools are exposed as an in-process SDK MCP server built with
[`claude_agent_sdk.create_sdk_mcp_server`](https://github.com/anthropics/claude-agent-sdk-python)
and registered on `ClaudeAgentOptions.mcp_servers` by
[`shared/egg_agent/client.py::run_agent_async`](../../shared/egg_agent/client.py).
There is **no new network service, no new auth layer, no new process**
— the tools run in the agent's own Python interpreter and call the
same handler functions the `egg-contract` / `egg-orch` CLIs call.
Iteration 1 (the mechanism + 18 verbs) is
tracked in [#1765](https://github.com/jwbron/egg/issues/1765);
iteration 2 (additional verbs covering the rest of the capability
audit) is tracked in
[#1917](https://github.com/jwbron/egg/issues/1917).

The `mcp__confluence__*` / `mcp__jira__*` namespaces
([#2994](https://github.com/jwbron/egg/issues/2994)) are a thin
**presentation layer over the gateway's `/api/v1/{confluence,jira}/*`
routes** — the same routes the `sandbox/scripts/{confluence,jira}` bash
wrappers POST to. Their handlers carry **no Atlassian credentials** and
add **no new capability**: every policy (space/project allowlist,
read-only vs. the four Jira write routes, CQL/JQL scope extraction,
response redaction, the `private_mode_required` gate) stays enforced at
the gateway. They exist so the routes are visible in the agent's tool
manifest every turn instead of being prose in `environment.md` the agent
has to recall — the motivating discovery failure was an external-repo
pipeline where an
agent saw no `mcp__confluence__*` in its manifest and *guessed* a space
list rather than calling `confluence space list`. Naming matches the
host MCP namespaces (which are deliberately **not** exposed to the
sandbox), so planner-authored task text that references
`mcp__confluence__*` resolves to the restricted sandbox tools.

## Flag — `EGG_MCP_TOOLS`

The MCP tool surface is **on by default** since [#1942](https://github.com/jwbron/egg/issues/1942). The env var now acts as a kill-switch:

| Flag | Effect |
|------|--------|
| `EGG_MCP_TOOLS` unset or any value not listed below | **Default.** Registers the 45 tools (one server per namespace) on `options.mcp_servers` and appends `SYSTEM_PROMPT_NUDGE` to `options.system_prompt`. |
| `EGG_MCP_TOOLS=false` (or `0` / `no` / `off`) | Opt-out. Code path is byte-identical to the pre-#1765 behaviour — no `mcp_servers` registration, no prompt changes, no import cost. |

Iteration 1 (#1765) shipped the flag default-off while the wire-up burned in.
#1942 flipped the default to on and kept the env var as a rollback
switch; a later follow-up (decision-9 in #1917) will remove the flag
entirely once iter-2 has burned in.

To opt a pipeline out, set `EGG_MCP_TOOLS=false` via pod env, Docker
Compose, or the `env` stanza on any submit-task payload. See
[docs/guides/sdlc-pipeline.md — Agent MCP tools
(EGG_MCP_TOOLS flag)](../guides/sdlc-pipeline.md#agent-mcp-tools-egg_mcp_tools-flag)
for the per-pipeline recipe.

## Tool inventory (45 verbs)

All 45 tools are registered as `@tool`-decorated wrappers in
`sandbox/egg_agent_tools/tools/*.py`. The raw `@tool` name is the verb
itself (e.g. `"propose"`, `"register_open_question"`).

### Tool-name resolution (how Claude sees these tools)

The SDK renders an MCP tool in `tool_use` blocks as
`mcp__<server_key>__<raw_tool_name>`. `build_sandbox_mcp_server`
returns a `{namespace: server}` dict — one SDK MCP server per
namespace, keyed by `sdlc`, `brc`, `phase`, `progress`, `task`,
`confluence`, or `jira` — and
`shared/egg_agent/client.py::run_agent_async`
merges that dict into `options.mcp_servers` unless `EGG_MCP_TOOLS` is
explicitly falsy. With raw `@tool` names declared as plain verbs,
Claude's composition naturally produces the semantic names in the
tables below:

- raw name `propose` in server key `brc` → `mcp__brc__propose`
- raw name `register_open_question` in server key `sdlc` →
  `mcp__sdlc__register_open_question`
- ...and so on for every verb.

The tables list the **SDK-visible tool names** (what appears in
`tool_use` blocks and what agents call). The `ToolRegistration.name`
attribute in `sandbox/egg_agent_tools/tools/_registry.py` carries
the same full name for drift-test introspection and nudge
generation. The authoritative sources are the shipping `TOOL_LIST`
and per-namespace dict returned by
`sandbox/egg_agent_tools/server.py::build_sandbox_mcp_server()`, plus
the `SYSTEM_PROMPT_NUDGE` generated at import time by
`sandbox/egg_agent_tools/server.py::_render_nudge()`.

Every tool with a shell-CLI counterpart declares a `cli_command`
attribute on its `ToolRegistration` (e.g. `("egg-orch", "consensus",
"propose")`) so a CI drift test
(`tests/tools/test_mcp_cli_drift.py`) can assert the MCP tool and
the CLI subparser dispatch the same handler function. If a handler
moves, both surfaces move together or CI fails. Adding a new tool
means adding a `cli_command` attribute on the registration (or
explicitly setting it to `None` for new verbs with no CLI
counterpart) — the drift gate will refuse the PR otherwise. Tools
that set `cli_command=None` are governed by an additional gate (see
[`cli_command=None` rationale](#cli_commandnone-rationale-pattern-decision-13))
that requires the handler docstring to explain why no CLI exists.

### `mcp__sdlc__*` — HITL and contract-level operations

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__sdlc__register_open_question` | Create a HITL decision (multiple-choice) on the contract. | `handlers.sdlc.register_open_question` | `egg-contract add-decision` |
| `mcp__sdlc__request_feedback` | Create an open-ended feedback request on the contract. | `handlers.sdlc.request_feedback` | `egg-contract add-feedback` |
| `mcp__sdlc__check_hitl_answers` | Return resolved decisions and feedback (submitted or pending) for the current contract. Without a `phase` arg, returns HITL across all phases; pass `phase` to narrow to a single phase. | `handlers.sdlc.check_hitl_answers` | — *(no CLI; new capability)* |
| `mcp__sdlc__show_contract` | Read the current contract as a dict. Optional `fields=[…]` projection returns only the named top-level keys; an unknown field raises `HandlerError` (no silent skip). State-machine effect: **read-only**. | `handlers.sdlc.show_contract` | `egg-contract show` |
| `mcp__sdlc__verify_criterion` | Mark an acceptance criterion verified on the contract. **REVIEWER role only** — the gateway rejects non-REVIEWER writers; the handler does not re-check (decision-7). State-machine effect: marks the criterion verified; no-op if already verified. | `handlers.sdlc.verify_criterion` | `egg-contract verify-criterion` |
| `mcp__sdlc__check_file_restriction` | Pure-local read against **both** gateway push gates: the role layer (`shared/egg_restrictions/patterns.py`) and the phase layer (`shared/egg_restrictions/phase_patterns.py`, mirror of `gateway/phase_filter.py`). `can_write` is their conjunction — it predicts push acceptance — and the split verdicts (`role_can_write`, `phase_allows`, `blocked_by`, `phase`) show which gate fires. A phase-layer block (e.g. `refiner` writing `.egg-state/drafts/*-plan.md` in the refine phase, reserved to plan) is a real gateway block, not a false claim, and carries no `alternative_role` (#2968). `role`/`phase` default to `EGG_AGENT_ROLE`/`EGG_PHASE`; an unset phase makes the phase layer a no-op (role-only, pre-#2968 behavior). **When reviewing another agent's proposal, pass `role` and `phase` explicitly** (e.g. `role="coder"`, `phase="implement"`) — the defaults give the verdict for the reviewer's *own* role/phase, not the producer's, so a reviewer's default-args check will diverge from what the gateway would have done to the producer. Producers call this before exploring a file outside their boundary (#2529). Read-only; no gateway round-trip. | `handlers.restrictions.check_file_restriction` | — *(no CLI; pattern matching is pure CPU and both pattern sets ship in the sandbox image — a CLI shim would just re-import the same modules)* |
| `mcp__sdlc__report_impasse` | Persist a typed `Impasse` (category, reason, suggested_role, blocked_files, evidence, task_id) under `AgentOutput.impasse` (#2529). For `category=wrong_role`, `task_id` and `suggested_role` are **mandatory** — the handler raises `HandlerError` if either is missing, since the orchestrator's auto-delegation path needs both to rewire `task.role` unambiguously (no role-match fallback when a slice has multiple tasks per role). For other categories (`plan_bug`, `external_blocker`, `unknown`), both fields stay optional — those always escalate to HITL. The orchestrator reads the impasse post-phase and either auto-delegates to `suggested_role` (first attempt, `wrong_role` only) or escalates to HITL (second attempt or non-`wrong_role`). State-machine effect: **the agent must exit cleanly without committing after this returns**. | `handlers.restrictions.report_impasse` | — *(no CLI; structured runtime signal that lives inside agent-output JSON — a parallel CLI write path would just risk drift with the MCP one)* |

### `mcp__brc__*` — Broadcast-Review-Converge consensus

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__brc__propose` | Push committed changes to origin then broadcast a `CONSENSUS_PROPOSE` signal. See [push behavior](#brc_propose-push-behavior) below. | `handlers.brc.brc_propose` | `egg-orch consensus propose --push` |
| `mcp__brc__ack` | Acknowledge (ACK) a peer's proposal. Optional `pre_merge_condition` (str) turns this into a **conditional ACK** — the work is approved but a human must perform the named action before merging (e.g. `git mv old/path new/path`). The obligation is rendered as a "Pre-merge Obligations" section on the auto-created PR. Leave empty for an unconditional ACK. When non-empty, the condition is validated like `reason`: boilerplate and short values are rejected with 400. Optional `pre_merge_condition_resolved_in_diff` (str, commit SHA) marks the obligation as satisfied within the same PR's diff on a re-ACK — the renderer demotes it to a "✅ Resolved within this PR" subsection instead of the merge-blocking banner. Requires a non-empty `pre_merge_condition`; rejected at 400 on a plain ACK. See [Conditional ACK reference](conditional-ack.md). | `handlers.brc.brc_ack` | `egg-orch consensus ack` |
| `mcp__brc__nack` | Reject (NACK) a peer's proposal with blocker list. | `handlers.brc.brc_nack` | `egg-orch consensus nack` |
| `mcp__brc__confirm` | Signal CONFIRMED — producer acknowledges all reviewer ACKs. Returns `ok=True` when the transition to CONFIRMED succeeded. Returns `ok=False` with `status="pending_acks"` when the orchestrator rejected the transition (e.g. not yet fully ACKed, stale ACKs); this is transient — retry after polling for outstanding ACKs. Equivalent to CLI exit code 0 vs 2. | `handlers.brc.brc_confirm` | `egg-orch consensus confirmed` |
| `mcp__brc__get_state` | Full structured consensus state (JSON; accepts `verbose: bool`). Slice-aware (#2761): scopes to the per-slice BRC tracker via `slice_id` (defaults to `EGG_SLICE_ID`), so a per-slice agent sees its own slice's consensus rather than a pipeline-level reconstruction. | `handlers.brc.brc_get_state` | `egg-orch brc get-state` *(verb-level CLI alias added in #2908; `cli_command=None` in MCP registry — schema is in `schemas.py`, not argparse)* |
| `mcp__brc__list_blocking` | Return the list of agent roles currently blocking consensus (derived view). | `handlers.brc.brc_list_blocking` | `egg-orch brc list-blocking` *(verb-level CLI alias added in #2908; `cli_command=None` in MCP registry)* |
| `mcp__brc__send_heartbeat` | Emit a structured `HEARTBEAT` (schema-validated, per-role deduped, rate-limited) to the dedicated `/heartbeat` endpoint on a state transition. Valid states: `WORKING`, `WAITING_ON_ROLE` (+ `waiting_on=<peer>`), `WAITING_FOR_EVENT`, `PROPOSED`, `IDLE` — but agents never emit `WAITING_FOR_EVENT`: the orchestrator owns all waiting and spawns agents one-shot per event (#2908/#3164). | `handlers.message.message_heartbeat` | `egg-orch message heartbeat` |

> **No blocking-wait tools, by design** (#2211, #2908, #3157). The MCP wait tools were removed in #2211 because long polls don't fit the MCP transport — both transports cap tool calls below typical quiet-phase intervals (~30 s streamable-HTTP, ~60 s in-process SDK), and every cap-elapsed return is a wasted LLM turn. They were not replaced with an agent-tier Bash idiom: blocking waits belong to other tiers entirely. Sandbox-side, the orchestrator owns all waiting — it derives the next BRC event in-process and spawns the agent one-shot per event (the in-pod wait arm was retired by #3164); agents never wait on the bus (`docs/reference/agent-wait-patterns.md` §0). Host-side, the SDLC skill blocks in `egg-orch pipeline wait-status` via Bash.
| `mcp__brc__read_peer_artifact` | Read the BRC transcript for a phase from TWO merged sources (#3076 / #3077 phase 1): the orchestrator's live `/brc-transcript` route (the message store holds exactly the in-flight phase — a peer's `CONSENSUS_PROPOSE` is visible the moment it is sent, Delphi-redacted server-side for unreviewed reviewers) and the local `.egg-state/brc-history/<identifier>-<phase>.json` files (phases completed before spawn; per-slice partition `<identifier>-implement-<slice_id>.json` when `EGG_SLICE_ID` is set and `phase == "implement"`, with the `unattributed` sibling merged in unless `include_unattributed=False`). Records dedup by message id, sorted by timestamp. Optional filters: `peer_role` / `producer_role` (alias), `message_type` (str or list). `limit` / `cursor` pagination (default `limit=50`, max 500). The identifier / pipeline id are resolved server-side from `EGG_ISSUE_NUMBER` / `EGG_PIPELINE_ID` (agents cannot pass an arbitrary id; path-traversal hardening). Returns `{items: [...], next_cursor: <str|None>, total_available: <int>, skipped_malformed: <int>, live: <bool>, hint?: <str>}` — `live` says whether the live route contributed; `hint` is present only when both sources are empty: with the live route reachable that genuinely means no BRC messages for the phase yet, without it the emptiness is structural (#3076; brc-history is written at phase *completion*) and the hint points at the event-payload fallback (`pending_reviews[].proposal_commit_sha` + `egg-artifact get <name> --ref <sha>` for spec-registered artifacts, or `git log <sha> --not origin/<base> -p` for code files). | `handlers.brc.brc_read_peer_artifact` | `egg-orch brc read-peer-artifact` *(#2908; thin wrapper, registration still `cli_command=None` — see callout below)* |
| `mcp__brc__resolve_obligation` | Mark a reviewer's conditional-ACK obligation as satisfied in-cycle (#2338). Required: `reviewer_role`, `producer_role`. Optional: `commit_sha`, `note`. The matrix keeps the obligation text for audit, but `get_pre_merge_conditions` filters resolved entries — the PR body and HITL gate stop surfacing the obligation. The orchestrator persists a `CONSENSUS_OBLIGATION_RESOLVED` message so the resolution survives orchestrator restart, and rejects `resolver_role == producer_role` so a producer cannot self-resolve their own obligation. Resolution is per-version: any later ACK / NACK / invalidate on the same edge resets the resolved flag. | `handlers.brc.brc_resolve_obligation` | `egg-orch brc resolve-obligation` *(#2908; thin wrapper, registration still `cli_command=None` — see callout below)* |

#### `brc_propose` push behavior

`mcp__brc__propose` pushes committed changes to origin via the gateway
before broadcasting the proposal. The `push` parameter defaults to
`true`; set it to `false` if you have already pushed through another
route. Push failure short-circuits the handler — no proposal is sent
for an un-pushed artifact.

### `mcp__phase__*` — Phase context

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__phase__get_context` | Bundle `EGG_PIPELINE_ID`, `EGG_PHASE`, `EGG_AGENT_ROLE`, the role-filtered task list, and prior-phase artifact paths (`.egg-state/drafts/`, `.egg-state/agent-outputs/`). | `handlers.phase.phase_get_context` | `egg-orch phase get-context` *(verb-level CLI alias added in #2908; `cli_command=None` in MCP registry — schema is in `schemas.py`, not argparse)* |
| `mcp__phase__get_assigned_tasks` | Return only the tasks assigned to the caller's role (`EGG_AGENT_ROLE`) from the contract. | `handlers.phase.phase_get_assigned_tasks` | — *(no CLI; filtered view over `egg-contract show`)* |
| `mcp__phase__complete_phase` | Mutate `phases.<p>.status` to `"complete"` via the gateway `/api/v1/contract/mutate` path. State-machine effect: **transitions phase status to complete; downstream `phase_complete` signal fires.** | `handlers.phase.complete_phase` | `egg-contract complete-phase` |

Some fields on `mcp__phase__get_context` remain best-effort (e.g.
`active_peers`, `reviewer_peers`, `hitl_pending`); promotion to
required is tracked as a separate follow-up after iter-2 burn-in
(decision-6 in #1917).

### `mcp__progress__*` — Progress signals + overseer surface

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__progress__emit` | Emit a structured progress event: required `step` (step name) and `state` (`working`/`blocked`/`complete`), optional `detail` and `blocker`. | `handlers.progress.progress_emit` | `egg-orch progress emit` |
| `mcp__progress__signal_error` | Signal an error to the orchestrator (`--error <msg>` payload + recoverable flag). | `handlers.progress.progress_signal_error` | `egg-orch signal error` |
| `mcp__progress__heartbeat` | Send a heartbeat so the orchestrator knows the agent is alive (coarse-grained; for fine-grained BRC heartbeats use `mcp__brc__send_heartbeat`). | `handlers.progress.progress_heartbeat` | `egg-orch signal heartbeat` |
| `mcp__progress__overseer_alert` | Broadcast an `OVERSEER_ALERT` to all agents in the pipeline (`to_role="all"` hard-coded). **For observer roles only (overseer/mediator).** The `unmediated-disagreement` anomaly type is intended for observers flagging that no one is adjudicating a disagreement; it is informational. Producers blocked by reviewer NACKs that name an operator-decidable scope question should call `mcp__sdlc__register_open_question` instead — that creates a contract-tracked HITL gate in `pending_decisions`, not just an alert. | `handlers.progress.overseer_alert` | `egg-orch overseer alert` |
| `mcp__progress__query_status` | `GET /api/v1/pipelines/<pipeline_id>/status` — read the structured pipeline status (agent matrix, BRC phase, blocked roles). `pipeline_id` is resolved server-side from `EGG_PIPELINE_ID` / `EGG_ISSUE_NUMBER`; agents cannot query arbitrary pipelines (path-traversal / cross-pipeline-read hardening). When the pipeline is wedged between phases (pipeline is `running`, current phase is `complete`, no pending decisions, no successor scheduled for >60 s), the response includes `wedged_no_successor: {phase, completed_at, since_seconds}`. | `handlers.progress.query_status` | `egg-orch pipeline status` |

### `mcp__task__*` — Task-level mutations

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__task__complete` | Mark a contract task complete, optionally linking a commit SHA. State-machine effect: **transitions task status to complete; idempotent**. | `handlers.task.task_complete` | `egg-contract complete-task` |
| `mcp__task__add_commit` | Link a commit SHA to a task. State-machine effect: **records the commit on the task; does not mark the task complete**. | `handlers.task.add_commit` | `egg-contract add-commit` |
| `mcp__task__update_notes` | Append implementation notes to a task. | `handlers.task.update_notes` | `egg-contract update-notes` |
| `mcp__task__mark_gap` | Append a structured coverage-gap entry to `phases.<p>.tasks.<t>.gaps[]`. **Tester role writes; coder role reads.** Handler stamps `created_at` (ISO-8601 UTC) and generates a stable `gap-<N>` id from `max(existing) + 1`. Validation rejects missing `from_role` / `to_role` / `description`. | `handlers.task.mark_gap` | — *(no CLI; tester→coder coverage-gap handoff is agent-to-agent; operators don't need it)* |

### `mcp__confluence__*` — Confluence reads (gateway-backed, #2994)

Mirror of the `sandbox/scripts/confluence` bash wrapper, one verb per
gateway route. **Read-only**, space-allowlisted, private-mode-gated —
all enforced at the gateway. snake_case args translate to the gateway's
camelCase body. No `egg-*` CLI counterpart (the analog is the bash
wrapper), so every registration is `cli_command=None`.

| Tool | Purpose | Handler | Gateway route |
|------|---------|---------|---------------|
| `mcp__confluence__page_get` | Fetch a page by numeric `page_id`. Optional `body_format` (`['storage']` default, `['atlas_doc_format']`, `['view']`) and `expand`. | `handlers.confluence.confluence_page_get` | `POST /api/v1/confluence/page/get` |
| `mcp__confluence__page_descendants` | List a page's descendants. `depth` bounds the walk; `limit`+`cursor` paginate. | `handlers.confluence.confluence_page_descendants` | `POST /api/v1/confluence/page/descendants` |
| `mcp__confluence__page_footer_comments` | Footer comments on a page; `include_replies` inlines threads. | `handlers.confluence.confluence_page_footer_comments` | `POST /api/v1/confluence/page/footer-comments` |
| `mcp__confluence__page_inline_comments` | Inline comments on a page (gateway falls back to v1 if v2 404s). | `handlers.confluence.confluence_page_inline_comments` | `POST /api/v1/confluence/page/inline-comments` |
| `mcp__confluence__space_pages` | List pages in an allowlisted space. | `handlers.confluence.confluence_space_pages` | `POST /api/v1/confluence/space/pages` |
| `mcp__confluence__space_list` | List the spaces visible to the agent (filtered to the allowlist). **Use this to discover readable spaces — don't guess keys** (the #2994 motivating failure). | `handlers.confluence.confluence_space_list` | `POST /api/v1/confluence/space/list` |
| `mcp__confluence__search` | CQL query. Must statically scope to allowlisted spaces (an `OR` over `space` is denied). | `handlers.confluence.confluence_search` | `POST /api/v1/confluence/search` |
| `mcp__confluence__execute` | Raw read-only REST passthrough (GET-only; non-GET / denied paths → 403). | `handlers.confluence.confluence_execute` | `POST /api/v1/confluence/execute` |

### `mcp__jira__*` — Jira reads + writes (gateway-backed, #2994)

Mirror of the `sandbox/scripts/jira` bash wrapper, one verb per gateway
route. Reads plus the four dedicated write routes (`ticket_create`,
`ticket_edit`, `ticket_comment_add`, `link_create`); project-allowlisted
and private-mode-gated at the gateway. The operator-only `transition`
route is **not** mirrored (the bash wrapper doesn't surface it either).
No `egg-*` CLI counterpart, so every registration is `cli_command=None`.

| Tool | Purpose | Handler | Gateway route |
|------|---------|---------|---------------|
| `mcp__jira__ticket_get` | Fetch a ticket by key (e.g. `ENG-123`); optional `fields`. | `handlers.jira.jira_ticket_get` | `POST /api/v1/jira/ticket/get` |
| `mcp__jira__ticket_comments` | Fetch a ticket's comments. | `handlers.jira.jira_ticket_comments` | `POST /api/v1/jira/ticket/comments` |
| `mcp__jira__ticket_remotelinks` | Fetch a ticket's remote links (surfaces PRs humans opened against it). | `handlers.jira.jira_ticket_remotelinks` | `POST /api/v1/jira/ticket/remotelinks` |
| `mcp__jira__search` | JQL search. Must statically scope to allowlisted projects (an `OR` over `project` is denied); `next_page_token` paginates. | `handlers.jira.jira_search` | `POST /api/v1/jira/search` |
| `mcp__jira__ticket_create` | Create a ticket (`project`, `issue_type`, `summary` required). State-machine effect: **creates a new issue**. `idempotency_key` makes a retry safe. | `handlers.jira.jira_ticket_create` | `POST /api/v1/jira/ticket/create` |
| `mcp__jira__ticket_edit` | Edit a ticket. `labels` (replace) is mutually exclusive with `add_labels`/`remove_labels` (incremental); `notify_users` defaults true to match the `sandbox/scripts/jira` wrapper (pass false to suppress notifications). State-machine effect: **mutates issue fields in place**. | `handlers.jira.jira_ticket_edit` | `POST /api/v1/jira/ticket/edit` |
| `mcp__jira__ticket_comment_add` | Add a comment (`idempotency_key` makes a retry safe). | `handlers.jira.jira_ticket_comment_add` | `POST /api/v1/jira/ticket/comment/add` |
| `mcp__jira__link_create` | Link two tickets (`link_type`, `inward_issue`, `outward_issue`). State-machine effect: **creates an issue link**. | `handlers.jira.jira_link_create` | `POST /api/v1/jira/issue-link/create` |
| `mcp__jira__execute` | Raw read-only REST passthrough (GET-only; non-GET / denied paths → 403). | `handlers.jira.jira_execute` | `POST /api/v1/jira/execute` |

Total: **45 tools** across 7 namespaces (`sdlc`, `brc`, `phase`,
`progress`, `task`, `confluence`, `jira`) — 18 iter-1 + 12 iter-2
(#1917) = 30, then −2 in #2211 (`wait_for_event` + `wait_loop`
removed; bus waits later moved to the consensus wrapper entirely in
#2908 — agents never wait), then +1 in #2338 (`resolve_obligation`), then +2
in #2529 (`check_file_restriction` + `report_impasse` — runtime escape
hatch) = 31, then −3 in #2993 (checkpoint subsystem removed) = 28,
then +17 in #2994 (8 `mcp__confluence__*` + 9 `mcp__jira__*`
gateway-route mirrors) = 45. Covers the BRC consensus loop, HITL
(decisions + feedback + answers), phase context + completion, progress
signals + overseer alerts + status queries, task completion + commits +
notes + coverage-gaps, and the Confluence/Jira gateway routes — every
verb a pipeline agent issues on the hot path. The count (`45`) is
asserted by
`tests/sandbox/egg_agent_tools/test_server.py::TestToolRegistry::test_tool_count_registered`
and the namespace set (`{sdlc, brc, phase, progress, task, confluence,
jira}`) by `TestToolRegistry::test_namespace_set` so the prose numbers
in this doc cannot drift silently.

## Conventions

### Pagination convention (decision-12)

Verbs that return a potentially large list paginate via opaque
cursors instead of start/poll/complete triplets:

| Verb | Default `limit` |
|------|-----------------|
| `mcp__brc__read_peer_artifact` | 50 |

The handler returns `{items: [...], next_cursor: <str|None>}`. Pass
the returned `next_cursor` back as the next call's `cursor` to fetch
the next page; a `None` `next_cursor` means the page is the last one.
The internal encoding of `cursor` is implementation-defined and must
not be parsed or constructed by agents — treat it as an opaque token
that round-trips through the handler. Tampered cursors are rejected
with `HandlerError`. The defaults are
sized to keep a worst-case page under the SDK's 60 s MCP timeout; if
you know your dataset is small, raise `limit` to skip the second
round-trip.

### Output-size cap (`EGG_TOOL_OUTPUT_CAP_BYTES`, #2805)

Every egg-owned tool result is bounded as **model-context/cost
discipline** before it crosses the Claude Agent SDK reader — a runaway
result would otherwise dump tens of thousands of tokens to the model in
a single tool call. The cap is applied at two chokepoints: the
orchestrator MCP server (`handle_tool_call` → `cap_result_dict`) and
every sandbox `@tool` wrapper (`invoke_handler` → `cap_text`), both via
the shared `shared/egg_tool_output.py` helper. The cap is **not** the
crash-prevention layer for the SDK reader (the upstream 1 MiB buffer was
the original concern, but egg raises that to 32 MiB at
`ClaudeAgentOptions.max_buffer_size`, #2884 — see
[Agent Recovery → SDK Reader Buffer](agent-recovery.md#sdk-reader-buffer-the-crash-prevention-layer)).

| Variable | Default | Effect |
|----------|---------|--------|
| `EGG_TOOL_OUTPUT_CAP_BYTES` | `102400` (100 KB) | Max serialized size of a single tool result. Output above the cap is replaced with a structured head-preview marker (`_egg_truncated`) that names how to narrow the call, or — for large unpaginated content — spilled to a temp file (`_egg_output_spilled`) the agent can `Read`/`grep`, with a small inline preview. |

At ~4 B/token for prose/JSON, the 100 KB default ≈ ~25k tokens — a
sensible upper bound for a single model-bound tool result. A
non-positive or non-integer value is **ignored with a logged warning**
(the operator is not left believing a cap is in effect when it isn't);
the helper falls back to the 100 KB default. The orchestrator measures
the cap against `indent=2`-serialized JSON (matching what its MCP server
ships), so raising the cap stays safe against the reader buffer above it
either way.

**Built-in tool cap (complementary):** The cap above covers egg-owned MCP
`@tool` payloads. Built-in Claude Code tools (`Read`, `Grep`, etc.) run
inside the CLI and can't be wrapped the same way. [#2876](https://github.com/jwbron/egg/issues/2876)
adds a PreToolUse hook that predicts when a result would be excessive
*before* the tool runs and denies the call with a narrowing hint. See
[Agent Recovery → Predictive Output Cap](agent-recovery.md#predictive-output-cap-pretooluse)
for the heuristic table and the `EGG_TOOL_OUTPUT_CAP` / `EGG_READ_CAP_BYTES`
operator knobs.

### `cli_command=None` rationale pattern (decision-13)

A `ToolRegistration` declares `cli_command=None` for verbs that have
no CLI counterpart on purpose (new agent-only capabilities or
deliberately-no-CLI affordances). For these verbs the drift gate
(`tests/tools/test_mcp_cli_drift.py`) skips the CLI parity check, but
a separate gate (`tests/tools/test_rule_doc_drift.py`, assertion C)
asserts the handler docstring is non-empty AND contains the substring
`"no CLI"` or `"no-CLI"` so the rationale is captured at the source
and discoverable from the registration. Today the `cli_command=None`
verbs are:

- `mcp__sdlc__check_hitl_answers` — no CLI; aggregates HITL state across phases.
- `mcp__brc__get_state` — no CLI in MCP registry (`cli_command=None`); has a verb-level CLI alias `egg-orch brc get-state` (added in #2908 for the event-pump wrapper). Schema derives from `schemas.py`, not argparse.
- `mcp__brc__list_blocking` — no CLI in MCP registry (`cli_command=None`); has a verb-level CLI alias `egg-orch brc list-blocking` (added in #2908).
- `mcp__brc__read_peer_artifact` — registration says no CLI; thin `egg-orch brc read-peer-artifact` wrapper (#2908). Merges the orchestrator's live `/brc-transcript` route (HTTP, current phase) with the `.egg-state/brc-history/<identifier>-<phase>.json` files on local disk (completed phases) — see #3076.
- `mcp__brc__resolve_obligation` — registration says no CLI; thin `egg-orch brc resolve-obligation` wrapper (#2908). Net-new in-cycle conditional-ACK obligation-resolution capability (#2338); producer/tester-driven via the MCP surface and the wrapper.
- `mcp__phase__get_context` — no CLI in MCP registry (`cli_command=None`); has a verb-level CLI alias `egg-orch phase get-context` (added in #2908 for the event-pump wrapper). Schema derives from `schemas.py`, not argparse.
- `mcp__phase__get_assigned_tasks` — no CLI; filtered view over `egg-contract show`.
- `mcp__task__mark_gap` — no CLI; tester→coder coverage-gap handoff is agent-to-agent.
- `mcp__sdlc__check_file_restriction` — no CLI; pattern matching is pure CPU and the registry ships in the sandbox image — a CLI shim would just re-import the same module (decision-13 rationale in `handlers/restrictions.py`).
- `mcp__sdlc__report_impasse` — no CLI; structured runtime signal that lives inside agent-output JSON — a parallel CLI write path would just risk drift with the MCP one (decision-13 rationale in `handlers/restrictions.py`).
- `mcp__confluence__*` / `mcp__jira__*` (17 verbs, #2994) — no CLI *in the drift-test sense*: their human-facing analog is the **bash** `sandbox/scripts/{confluence,jira}` wrapper, not a Python `egg-*` argparse tree the drift gate can walk, so the registrations set `cli_command=None` and each handler docstring carries the `"no CLI"` rationale. They are gateway-route mirrors (`handlers/{confluence,jira}.py` → `/api/v1/{confluence,jira}/*`); the gateway, not these handlers, holds credentials and enforces policy.

> **CLI surface added in #2908 (registration unchanged):** `mcp__brc__get_state`, `mcp__brc__list_blocking`, `mcp__brc__read_peer_artifact`, and `mcp__brc__resolve_obligation` gained matching `egg-orch brc <verb>` subcommands so the event-pump consensus wrapper (#2908) can drive them from bash without an LLM round-trip. The MCP-side `ToolRegistration` entries in `sandbox/egg_agent_tools/tools/brc.py` still carry `cli_command=None`, so the drift gate continues to treat these four as no-CLI tools and their JSON schemas continue to be hand-authored in `schemas.py` (the bullets above stay accurate from the registration / drift-gate perspective). Promoting the registrations to `cli_command=("egg-orch", "brc", "<verb>")` so the schemas auto-derive from the argparse parsers is a follow-up — until then, treat the CLI subcommands as thin shell wrappers over the same handlers, sharing the handler but not the schema source. A fifth net-new `brc next-action` subcommand has no MCP counterpart by design — the wrapper consumes the derivation directly. See [Orchestrator CLI — BRC verb-level operations](orchestrator-cli.md#brc-verb-level-operations-egg-orch-brc).

When adding a new `cli_command=None` verb, the handler docstring
must explain the no-CLI rationale; CI fails otherwise.

### Two-way rule-doc drift gate (decision-11)

`tests/tools/test_rule_doc_drift.py` asserts a two-way invariant:

- **A.** Every `Prefer this over `egg-…`` line in
  `sandbox/agent-config/rules/*.md` and
  `sandbox/egg_lib/data/hitl_editing_rules.md` resolves to a tool in
  `TOOL_REGISTRY`.
- **B.** Every registration with `cli_command != None` has a matching
  `Prefer this over …` line in at least one of those docs.
- **C.** Every registration with `cli_command == None` has a handler
  docstring containing `"no CLI"` or `"no-CLI"` (the rationale gate
  above).

The gate keeps rule docs and the registry from drifting in either
direction. When adding a new tool, add the `Prefer this over …` line
to the appropriate rule doc in the same PR; CI fails otherwise.

## Input/output schemas

Input schemas are derived automatically from the argparse subparsers
that back the CLI counterparts (`sandbox/egg_lib/orch_cli.py::create_parser`
and `sandbox/egg_lib/contract_cli.py::create_parser`) by
`sandbox/egg_agent_tools/schemas.py::derive_schema_from_argparse`.
Each tool may supply a per-tool override dict for cases where argparse
help is insufficient (e.g. richer descriptions or tighter enum
constraints). Tools whose `ToolRegistration` declares `cli_command=None`
— `phase_get_context`, `phase_get_assigned_tasks`, `check_hitl_answers`,
`task_mark_gap`, the four `mcp__brc__*` verbs covered above
(`brc_get_state`, `brc_list_blocking`, `brc_read_peer_artifact`,
`brc_resolve_obligation`), and the 17 `mcp__confluence__*` /
`mcp__jira__*` verbs (#2994) — declare their JSON schema directly in
`schemas.py` (or, for the Atlassian verbs, inline alongside the `@tool`
definition in `tools/{confluence,jira}.py`); the
`derive_schema_from_argparse` path is skipped because the registration
has no argparse subparser bound to it. The
[#2908](https://github.com/jwbron/egg/issues/2908) `egg-orch brc <verb>`
CLI wrappers, plus the `egg-orch phase get-context` wrapper, reuse the
same handlers but do **not** flip the registrations off
`cli_command=None`; promoting the registrations and auto-deriving
schemas from the argparse parsers is a follow-up.

Output: every tool returns the handler's dict response serialised as a
JSON string per the
[SDK tool contract](https://github.com/anthropics/claude-agent-sdk-python).
On error, the `@tool` wrapper catches `GatewayError` / `TimeoutError`
/ generic `Exception` and returns a structured
`{is_error: True, content: [{type: "text", text: <message>}]}` block.
Gateway flakes therefore surface as tool errors the agent can retry —
never as an agent crash.

## System-prompt nudge (`SYSTEM_PROMPT_NUDGE`)

When the flag is on (the default), `run_agent_async` appends a short bootstrap
paragraph (`≤200` words) to `options.system_prompt`. The paragraph is
**generated programmatically** at module import from `TOOL_NAMESPACES`
— it is not a hand-authored string literal — so adding or renaming a
namespace updates the nudge automatically. Two paired unit tests in
`tests/sandbox/egg_agent_tools/test_server.py::TestSystemPromptNudge`
enforce a symmetric match between the nudge and `TOOL_NAMESPACES`:
`test_each_namespace_appears_in_nudge` asserts every registered
namespace appears as `mcp__<ns>__` in the nudge, and
`test_nudge_substrings_back_to_registered_namespaces` asserts every
`mcp__<ns>__` substring in the nudge corresponds to a registered
namespace (extras in either direction fail CI). The companion
`TestToolRegistry::test_tool_count_registered` and
`test_namespace_set` pin `len(TOOL_REGISTRY) == 45` and
`set(TOOL_NAMESPACES.keys()) == {"sdlc", "brc", "phase", "progress",
"task", "confluence", "jira"}` so a future iteration cannot drift the
prose counts in this file silently.

**The source of truth is `sandbox/egg_agent_tools/server.py::_render_nudge()`.**
This doc does NOT embed a copy of the rendered string — the template
currently iterates over every registered namespace and emits one
bullet per namespace plus a short description, then closes with a
sentence instructing the agent to prefer the `mcp__*` tools over
Bash. To see the exact text your agent will receive, read
`_render_nudge()` or inspect
`sandbox.egg_agent_tools.SYSTEM_PROMPT_NUDGE` at import time. The
renderer is intentionally namespace-driven so the nudge and
`TOOL_NAMESPACES` cannot drift — changes to the tool list update the
nudge on the next import, and the drift test in `test_server.py`
keeps both sides honest.

The nudge points agents at `mcp__<namespace>__*`, which is the
literal name Claude sees in `tool_use` blocks — the per-namespace
server split (one SDK MCP server per `sdlc` / `brc` / `phase` /
`progress` / `task` / `confluence` / `jira` key) makes the composed
`mcp__<server_key>__<raw_name>` resolve directly to the semantic
name the nudge advertises. No mental prefix-prepending required.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Agent container (Python, claude_agent_sdk)                      │
│                                                                 │
│  shared/egg_agent/client.py::run_agent_async                    │
│      └── EGG_MCP_TOOLS ≠ falsy ▶ build_sandbox_mcp_server()     │
│                                                                 │
│                ┌─── sandbox/egg_agent_tools/ ───┐               │
│                │  server.py    (SDK factory)    │               │
│                │  schemas.py   (argparse→JSON)  │               │
│                │  tools/*.py   (@tool wrappers) │               │
│                │       │                        │               │
│                │       │ asyncio.to_thread()    │               │
│                │       ▼                        │               │
│                │  handlers/*.py (pure req→res)  │               │
│                │       │                        │               │
│                │       │ make_gateway_request   │               │
│                │       ▼                        │               │
│                │  GatewayError on 5xx/timeout   │               │
│                └────────┬────────────────────────┘              │
└────────────────────────────┬───────────────────────────────────┘
                             │ HTTP (same gateway path the CLIs use)
                             ▼
                    gateway / orchestrator
```

### Why in-process?

- **Network-mode neutral.** No sandbox-egress requirement — the MCP
  server runs in the agent's own interpreter. Works identically in
  `public` and `private` network modes.
- **Authz by construction.** Sandbox agents cannot invoke orchestrator
  MCP tools they do not register; there is no second MCP server to
  lock down.
- **Zero new dependency.** `claude-agent-sdk` is already a sandbox
  dependency; the `create_sdk_mcp_server` / `@tool` surface is what
  the SDK ships for exactly this case.

### Async + error discipline

`@tool` wrappers invoke their handlers via `asyncio.to_thread(handler,
req)` so the sync `urllib` gateway I/O does not block the agent event
loop. Handlers **raise** exceptions (`GatewayError`, `TimeoutError`,
`HandlerError`) — they **never** call `sys.exit`. The `@tool` wrapper
catches those exceptions and returns them as structured tool-result
error blocks of the form `{is_error: True, content: [{type: "text",
text: <message>}]}`, which the agent can surface as a tool error and
retry. The CLI `cmd_*` shim catches the same `GatewayError` and
renders the pre-#1765 stderr message + exit code for human callers,
so shell behaviour is byte-identical.

> **Handler rule — MUST NEVER `sys.exit`.** Every handler under
> `sandbox/egg_agent_tools/handlers/*.py` returns a dict response or
> raises a typed exception. A handler that calls `sys.exit` would
> terminate the Python interpreter the Claude Agent SDK is running in
> and bring the entire agent down (see the risk-analyst R1 note in
> `.egg-state/agent-outputs/1765-risk_analyst-output.json`). The
> same rule applies transitively to any helper imported by a handler
> — notably `make_gateway_request`, which backs every gateway-fronted
> handler in `egg_agent_tools` and was refactored in #1765 to raise
> `GatewayError` instead of exiting. This rule is
> about **handlers**, not shell CLI shims: unrefactored `cmd_*`
> functions in `sandbox/egg_lib/orch_cli.py` may still call
> `sys.exit(1)` on argparse-level errors (e.g. missing `--role`),
> which is fine because they run in their own process, not inside
> the agent SDK loop. When adding a new verb, inherit this contract:
> handlers raise; `@tool` wrappers catch; any `sys.exit` lives only
> in a CLI shim that runs as a subprocess, never in code imported
> into the agent event loop.

See
[`.egg-state/drafts/1765-plan.md`](../../.egg-state/drafts/1765-plan.md)
for the iter-1 plan,
[`.egg-state/agent-outputs/1765-architect-output.json`](../../.egg-state/agent-outputs/1765-architect-output.json)
for the iter-1 architect's technical decisions, and
[`.egg-state/drafts/1917-plan.md`](../../.egg-state/drafts/1917-plan.md)
for the iter-2 plan that adds the remaining 12 verbs and the rule-doc
drift gate.

## CLI surface preserved (decision-4 of #1765)

Existing `sandbox/bin/egg-*` CLIs are **not deprecated**. Every
refactored `cmd_*` function in `sandbox/egg_lib/contract_cli.py` and
`sandbox/egg_lib/orch_cli.py`
still:

- Accepts the same argparse flags.
- Prints the same stdout text.
- Exits with the same codes.

Only the internal call flow changes — `cmd_*` now builds a request
dict from `argparse.Namespace`, calls the shared `handlers.*`
function, and renders
the response for stdout. Humans, bash scripts, recovery tooling, and
the existing test suite see zero behaviour change. Parity is enforced
by committed fixture tests under `tests/sandbox/test_contract_cli.py`
and `tests/sandbox/test_orch_cli.py` (no auto-record
— every expected value is in the repo).

See [Orchestrator CLI reference](orchestrator-cli.md) and [SDLC Contract
reference](sdlc-contract.md) for the
complete shell CLI surface.

## Known limitations

- **Anchor verbs (decision-2 of #1917):** The capability audit also
  surfaced `anchor_init` / `anchor_update` / `anchor_get`. They are
  deferred to iteration 3 so the anchor design can be done
  deliberately. The phantom `egg-orch anchor *` CLI references in
  `sandbox/agent-config/rules/orchestrator.md` will be retracted
  alongside the iter-3 anchor MCP landing.
- **Directed peer messaging (decision-14 of #1917):**
  `brc_send_message` / `brc_poll_messages` remain deferred pending
  the REQUEST/REPLY subsystem.
- **Phase-context field promotion (decision-6 of #1917):**
  `active_peers` / `reviewer_peers` / `hitl_pending` on
  `mcp__phase__get_context` stay best-effort; promotion to required
  is a separate follow-up.
- **`EGG_MCP_TOOLS` flag removal (decision-9 of #1917):** Kept for
  iter-2 burn-in; removal is a third follow-up.
- **Timeouts:** The SDK's default 60 s MCP-tool timeout is sufficient
  for all 45 verbs (none are long-running; the `mcp__confluence__*` /
  `mcp__jira__*` verbs are single gateway→Atlassian round-trips well
  under the cap). Pagination (decision-12 of #1917) keeps
  `read_peer_artifact` page sizes well under the limit. If a future
  tool needs to exceed 60 s, it must be restructured as a
  start/poll/complete triplet — handled in a follow-up.
- **Observability:** Native SDK `tool_use` naming is enough today —
  `mcp__brc__propose` vs `Bash` surfaces cleanly in the structured
  logging stream.

## Version pin

`claude-agent-sdk` is pinned to `>=0.2.97,<0.3` in
`sandbox/pyproject.toml` and the `CLAUDE_AGENT_SDK_VERSION` ARG in
`sandbox/Dockerfile`. A smoke test at
`tests/sandbox/egg_agent_tools/test_sdk_surface.py` imports
`claude_agent_sdk.create_sdk_mcp_server` and `claude_agent_sdk.tool`
at module load time; if a future pre-1.0 SDK bump changes that
surface, CI fails at test-collection time with a clear pointer to the
SDK release notes rather than silently breaking every sandbox.

## Testing

| Test | Purpose |
|------|---------|
| `tests/sandbox/egg_agent_tools/test_handlers_*.py` | Unit tests for each handler (happy-path, missing-arg, 5xx gateway → `GatewayError`). Includes `test_handlers_confluence.py` / `test_handlers_jira.py` (#2994): snake→camel body translation, required-field validation, list/CSV normalisation, and the `gateway_data_request` unwrap. |
| `tests/sandbox/egg_agent_tools/handlers/test_*.py` | Per-handler unit tests for the iter-2 verbs (`show_contract`, `add_commit`, `update_notes`, `complete_phase`, `verify_criterion`, `read_peer_artifact`, `overseer_alert`, `query_status`, `mark_gap`). |
| `tests/sandbox/egg_agent_tools/test_tools.py` | `@tool` wrappers (JSON-serialised success; `is_error=True` structured block on handler exception). |
| `tests/sandbox/egg_agent_tools/test_server.py` | `build_sandbox_mcp_server` registers all 45 tools; `SYSTEM_PROMPT_NUDGE` symmetric drift test; derived-count assertions (`len(TOOL_REGISTRY) == 45` and the 7-namespace set). |
| `tests/sandbox/egg_agent_tools/test_schemas.py` | `derive_schema_from_argparse` correctness + override merge. |
| `tests/sandbox/egg_agent_tools/test_sdk_surface.py` | SDK import smoke (fails loud on incompatible SDK upgrade). |
| `tests/sandbox/egg_agent_tools/test_full_tool_registry.py` | Integration test: loads `TOOL_LIST` via `create_sdk_mcp_server`; asserts no registration errors and that completion/mutation verbs (`task_complete`, `phase__complete_phase`, `task__add_commit`, `sdlc__verify_criterion`) name the state-machine effect in their description. |
| `tests/shared/egg_agent/test_client.py` | Flag-on/flag-off wire-up in `run_agent_async`; `can_use_tool` passes `mcp__*` tool names. |
| `tests/sandbox/test_contract_cli.py`, `tests/sandbox/test_orch_cli.py` | CLI parity against committed fixtures. |
| `tests/tools/test_mcp_cli_drift.py` | Every tool with a `cli_command` attribute dispatches the same handler as its CLI subparser. |
| `tests/tools/test_rule_doc_drift.py` | Two-way rule-doc invariant: (A) every `Prefer this over `egg-…`` line resolves to a `TOOL_REGISTRY` entry; (B) every `cli_command != None` registration has a matching rule-doc line; (C) every `cli_command == None` registration has a handler docstring mentioning `"no CLI"` or `"no-CLI"` (decision-13 gate). |
| `tests/shared/egg_contracts/test_models_gaps.py` | Pydantic round-trip for `Task.gaps`; back-compat with old contract fixtures (parse to `gaps: []`). |
| `integration_tests/test_sandbox_mcp_tools_e2e.py` | Marker-gated live SDK round-trip — asserts the agent's first `tool_use` block names an `mcp__*` tool. |

## Related

- [Orchestrator CLI](orchestrator-cli.md) — full `egg-orch` shell
  surface (still the source of truth for human operators).
- [SDLC Contract](sdlc-contract.md) — full `egg-contract` shell
  surface.
- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — per-pipeline
  opt-out recipe for `EGG_MCP_TOOLS`.
- [Concurrent Execution Guide](../guides/concurrent-execution.md) —
  where BRC + consensus + message-bus live, which the `mcp__brc__*`
  namespace exposes.
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) —
  `EGG_MCP_TOOLS` alongside other sandbox env flags.
- [#1765](https://github.com/jwbron/egg/issues/1765) — iteration 1
  (mechanism + 18 verbs).
- [#1917](https://github.com/jwbron/egg/issues/1917) — iteration 2
  (12 additional verbs + rule-doc drift gate + decision-13 gate).
- [#1955](https://github.com/jwbron/egg/issues/1955) — closed by
  iteration 2's `mcp__sdlc__show_contract` + state-machine writes.
- [#2994](https://github.com/jwbron/egg/issues/2994) — the
  `mcp__confluence__*` / `mcp__jira__*` gateway-route mirrors
  (discoverability). See also
  [Confluence Wrapper](confluence-wrapper.md) and
  [Jira Wrapper](jira-wrapper.md) for the gateway-side policy surface.
