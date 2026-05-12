# Analysis: Add SDLC pipeline support for Jira epics

> Issue: #1557 | Phase: refine

## Problem Statement

Today, `submit_task` (the egg MCP entrypoint) accepts a Jira ticket key and runs the full refine → plan → implement pipeline against it as if it were a single unit of work. The pipeline ID becomes the ticket key, drafts land in `.egg-state/drafts/<TICKET>-analysis.md` / `-plan.md`, HITL approvals flow through the operator's normal Claude Code host session, and the implement phase produces one PR.

A **Jira epic** is a different shape of work — it is a multi-ticket container that, on planning, should fan out into N child tickets, each of which becomes its own implement pipeline / PR. The orchestrator infrastructure is already capable of running these per-child pipelines (every child gets `submit_task <CHILD-KEY>` the same way today's tickets do), but two specific sinks are missing:

1. **Refine output for an epic should land in the epic's Jira Description field**, not just stay as a refined problem statement on a single ticket.
2. **Plan output for an epic should decompose into child Jira tickets** under the epic (`createJiraIssue` per node + `createIssueLink` for cross-task dependencies), not stay as a single plan doc scoped to one ticket.

The issue also requires a **reassess path** for epics that already have children: read existing children, classify them (Done / In-flight / Updatable), consolidate / split / leave-alone where appropriate, flag obsolete ones for Won't-Do, and only create new children for genuinely new work. Per #2289-folded-in scope, **in-flight children** (status indicates active work, or an open PR exists) must carry a `do-not-modify-without-confirmation` marker so mutations against them require per-ticket HITL gates.

Desired outcome: a single `submit_task <EPIC-KEY>` from the operator's Claude Code host session runs refine → plan → HITL → apply against an epic (fresh or reassess), driving the epic's Description on approval and emitting the right edit / create / Won't-Do set of Jira mutations across its children. Each created child can then be picked up by `submit_task <CHILD-KEY>` and behaves identically to today's Jira-ticket pipeline (1 PR per child, or a stack along the slice DAG when the child is large enough to need #2137's stacked-PR delivery).

## Current Behavior

### `submit_task` entry point

`orchestrator/mcp_tools.py:67-127` defines the `submit_task` tool schema; `orchestrator/mcp_tools.py:1272-1381` handles invocation.

- Jira ticket format validation (`mcp_tools.py:1287-1292`): regex `^[A-Za-z][A-Za-z0-9]+-[0-9]+$` (e.g. `KORE-1234`).
- Pipeline ID derivation (`mcp_tools.py:1301-1307`): `pipeline_id = TICKET.upper()` (or `TICKET-qualifier`); branch = `egg/{pipeline_id}`.
- No upfront Jira fetch — the ticket key is **purely an identifier**; description / type / status are not read at `submit_task` time. The orchestrator exports `EGG_JIRA_TICKET` (and `EGG_JIRA_PROJECT` derived by splitting on `-`) into the sandbox env; that is the only Jira-related signal the agents get at spawn.
- Pipeline creation then dispatches to `POST /api/v1/pipelines` followed by `POST /api/v1/pipelines/{id}/start` (`mcp_tools.py:1333-1380`).

### Pipeline model (`orchestrator/models.py:816-1004`)

`Pipeline.jira_ticket` is stored as an optional string and validated for shape. The class comment (`models.py:986-988`) says explicitly: "Advisory only — the gateway does NOT use this for policy gating; only the project allowlist can authorise a Jira call (issue #1556 refine decision #9)." There is **no index from `jira_ticket` → pipelines** in the state store and **no `pr_url` persisted on the pipeline** (only `pr_number` and `pr_head_sha` for babysit-mode pipelines per `models.py:860-872`).

### Jira gateway surface

Issues #1556 (read-only v1, **closed/merged**), #1924 (write verbs, **closed/merged**), #2192 (bounded write verbs, **merged**) landed the agent-facing gateway surface. Routes in `gateway/gateway.py`:

| Verb | Route | Notes |
|------|-------|-------|
| Get ticket | `POST /api/v1/jira/ticket/get` | `gateway.py:4929-5009`. `fields` is optional; if omitted, **no field list** is sent and Atlassian's default field set is returned. `expand=renderedBody,renderedFields` is always added. |
| Search (JQL) | `POST /api/v1/jira/search` | `gateway.py:5012-5133`. **Conservative JQL extractor** (`gateway/jira_search.py:55-128`) requires `project = X` or `project IN (...)` at top level; AND-combined with arbitrary other clauses. **`OR` is rejected**; **bare `parent = K` / `"Epic Link" = K` are rejected** without a `project` scope. |
| Comments | `POST /api/v1/jira/ticket/comments` | `gateway.py:5136-...` |
| Create ticket | `POST /api/v1/jira/ticket/create` | `gateway.py:5580-...`. Supports setting `parent` / Epic Link at create time per `gateway/jira_policy.py` config. |
| Edit ticket | `POST /api/v1/jira/ticket/edit` | `gateway.py:5839-5996`. Editable: `summary` (≤255), `description` (≤32 KiB, plain wrapped to ADF or pre-built ADF dict), `labels` / `addLabels` / `removeLabels`. **No status / transitions / arbitrary custom fields.** |
| Add comment | `POST /api/v1/jira/ticket/comment/add` | `gateway.py:5999-...` |
| Link create | `POST /api/v1/jira/issue-link/create` | `gateway.py:6104-...`. Link-type allowlist via `jira.link_types` in `config/context-filters.yaml`; default `["Blocks", "Relates"]`. Idempotency cache via `gateway/jira_idempotency.py` (5 min TTL). |
| Execute (passthrough) | `POST /api/v1/jira/execute` | `gateway.py:5201-...`. GET-only, regex allowlist. Specifically **excludes** `search/jql` (must go through `/search` so the JQL scope extractor runs) and **excludes** `/transitions`, `/remotelink`, etc. |

**Transitions are forbidden by design** (`gateway/jira_client.py:133-146` `JIRA_WRITE_VERBS_DENIED`, `gateway/jira_client.py:217-283` `validate_jira_api_path`). The path segment "transitions" is hard-denied.

**Remote-links are NOT exposed**: `/rest/api/3/issue/{key}/remotelink` is not in the read-only allowed paths.

Sandbox CLI: `sandbox/scripts/jira` exposes `ticket get|edit|create|comments`, `ticket comment add`, `search`, `link create`, `execute`, `help`.

### Confluence gateway surface (#1931, merged)

`gateway/confluence_client.py` + companion routes `POST /api/v1/confluence/page/get`, `space/pages`, `page/descendants`, `page/footer-comments`, `page/inline-comments`, `space/list`, `search`. Atlassian creds shared with Jira; same `@require_private_mode` gate; same space allowlist via `config/context-filters.yaml`. Sandbox CLI: `sandbox/scripts/confluence`.

**Gap**: no helper anywhere in the repo parses ADF / description text for embedded Confluence URLs (`https://*.atlassian.net/wiki/spaces/...`). If the refine inputs need to pull pages linked from the epic description, either (a) a description-text URL-scan helper is needed, or (b) a new gateway route exposing remote-links is needed (today neither exists).

### Refine phase

Refiner prompt lives at `plugins/refine-plan/skills/refine-plan/agents/refiner.md` (no Jira vs GitHub branching — issue-shape-agnostic). It writes a markdown analysis to `.egg-state/drafts/<id>-analysis.md` and a JSON handoff (`analysis_path`, `recommended_option`, `files_researched`, `options_considered`, `open_questions`, `external_research_done`).

On HITL approval (`orchestrator/routes/pipelines.py:20070-20160`), the orchestrator only flips the decision status to `resolved=approve` and advances the phase. **No mutation hooks fire** — nothing posts the analysis to a GitHub issue or a Jira ticket today. Drafts live in the work branch and contracts (`.egg-state/contracts/<id>.json`) capture the decision audit trail; that is the entire "sink" today.

### Plan phase

Task-planner prompt at `plugins/refine-plan/skills/refine-plan/agents/task-planner.md`. Output: plan markdown plus a `# yaml-tasks` fenced block parsed by `shared/egg_contracts/plan_parser.py` (`parse_phases_from_yaml` at line 413, `parse_plan` entry at line 1065; the `ParsedTask` / `ParsedPhase` dataclasses sit at lines 76-150):

```yaml
slices:
  - id: 1
    name: Slice name
    dependencies: ""           # parent slice ID or ""
    tasks:
      - id: TASK-1-1
        description: |-
          Free-form markdown
        acceptance: |-
          Acceptance criteria
        role: coder | tester | documenter
        files: [path/to/file.py]
```

Forest invariant: `plan_parser.py:1284-1350` rejects slices with more than one parent and rejects cycles. **Task descriptions are free-form** — there is no "Jira-ticket-shaped" sub-structure today.

Same HITL gate flow on plan approval: contract is populated with tasks/phases/criteria (`pipelines.py:21165-21173`) and the implement phase begins. **No apply step exists** today — plan approval only advances state.

### Pipeline-state ↔ Jira/PR linkage

- No `jira_ticket → [pipelines]` reverse index.
- No `pipeline → PR URL` storage (only `pr_number` on babysit pipelines).
- No remote-link writes from the orchestrator into Jira when a PR is created.

### `/impact-analysis` skill (referenced in issue)

**Does not exist in the repo.** The issue references a `parent = <KEY> OR "Epic Link" = <KEY>` query shape "already demonstrated by the `/impact-analysis` skill", but `**/impact-analysis*` and `**/impact_analysis*` glob to nothing. The pattern needs to be implemented; and as currently shaped it would be **rejected by the JQL extractor** (`OR` is not allowed; both clauses must AND with a `project` scope — see decision-12). The literal `parent = K OR "Epic Link" = K` shape must be re-shaped into **two AND-`project`-scoped queries** issued in sequence (one `project = X AND parent = K`, one `project = X AND "Epic Link" = K`) and the result sets union'd by the orchestrator — single-query equivalents are blocked by the JQL extractor's no-OR rule.

### `#2137` (independent implement phases / stacked slice PRs)

Closed/merged. Implement phases are slice-scoped: each slice generates its own PR; siblings run in parallel; dependents wait. For this issue's MVP it does not matter: each Jira child runs as its own independent `submit_task` pipeline, and inside that pipeline #2137 dictates whether the child ships as one PR or as a stack along the child's own slice DAG. The epic-level pipeline of #1557 does **not** produce a slice DAG of code-shipping slices; its plan output is a Jira-decomposition graph that becomes N independent downstream pipelines.

### Primitive existence (for the plan phase's audit)

Concrete primitives the plan phase will rely on:

| Primitive | Where | Execution context |
|-----------|-------|-------------------|
| `submit_task` MCP tool | `orchestrator/mcp_tools.py:67-127` | host (operator's Claude session) |
| `submit_task` handler | `orchestrator/mcp_tools.py:1272-1381` | orchestrator |
| `Pipeline.jira_ticket` field | `orchestrator/models.py:981-1004` | orchestrator (Pydantic) |
| Refiner prompt | `plugins/refine-plan/skills/refine-plan/agents/refiner.md` | in-sandbox-agent |
| Task-planner prompt | `plugins/refine-plan/skills/refine-plan/agents/task-planner.md` | in-sandbox-agent |
| Architect / risk-analyst prompts | `plugins/refine-plan/skills/refine-plan/agents/{architect,risk-analyst}.md` | in-sandbox-agent |
| Plan YAML parser | `shared/egg_contracts/plan_parser.py:76-150` | orchestrator |
| `_run_pipeline` + HITL phase_gate | `orchestrator/routes/pipelines.py:20070-20160` | orchestrator |
| Phase-complete advancement | `pipelines.py:21165-21173` | orchestrator |
| Gateway Jira routes | `gateway/gateway.py:4929-6232` | gateway (in-cluster) |
| Gateway Jira client | `gateway/jira_client.py` | gateway |
| JQL scope extractor | `gateway/jira_search.py:55-128` | gateway |
| Project + link-type allowlist | `gateway/jira_policy.py`, `config/context-filters.yaml` | gateway |
| Jira write idempotency cache | `gateway/jira_idempotency.py` | gateway (5-min TTL) |
| Jira sandbox CLI | `sandbox/scripts/jira` | in-sandbox-agent |
| Confluence routes / CLI | `gateway/confluence_client.py`, `sandbox/scripts/confluence` | gateway / in-sandbox-agent |
| `EGG_JIRA_TICKET` / `EGG_JIRA_PROJECT` env vars | `orchestrator/routes/pipelines.py:~19287` | in-sandbox-agent (set by orchestrator) |

Net-new primitives needed for #1557 (all are decisions surfaced in Open Questions below):

| Primitive | Likely execution context | Decision ref |
|-----------|--------------------------|--------------|
| Orchestrator-side "is_epic" flag on Pipeline (or `jira_epic` param on `submit_task`) | orchestrator | decision-2 |
| Orchestrator-side reverse-index `jira_ticket → [pipelines]` + persisted PR URL | orchestrator | decision-7 |
| Orchestrator post-approval apply hook | orchestrator | decision-8 |
| Plan-node ↔ Jira-key mapping persisted on contract task | orchestrator (Pydantic) | decision-11 |
| New gateway route `POST /api/v1/jira/ticket/transition` (orchestrator-only, allowlisted to Won't-Do/Won't-Fix) | gateway | decision-15 |
| New gateway route `POST /api/v1/jira/ticket/remotelinks` (read-only) — only if option B of decision-9 wins | gateway | decision-9 |
| Description URL-scan helper for Confluence links — only if option A or B of decision-9 wins | orchestrator or in-sandbox-agent | decision-9 |
| Per-task ticket-shaped output (either the existing `description` shaped to a template or a new `jira_ticket_body` sibling field) | in-sandbox-agent (planner) + orchestrator (schema) | decision-10 |
| Mode-aware prompt parameterization (refine + plan) | orchestrator (prompt-building) + in-sandbox-agent (prompt body) | decision-16 |
| Configurable `done_statuses` / `in_flight_statuses` (or use `statusCategory.key`) | gateway / orchestrator | decision-13 + decision-14 |
| Hierarchy field per project (`parent` vs `customfield_10014`) — `gateway/jira_policy.py` already has `epic_link_field()` hook | gateway | decision-3 |

## Constraints

**Technical:**

- **Zero credentials in the sandbox** (hard invariant, `docs/architecture/credential-injection.md`). Jira creds live only in the gateway. Any orchestrator-side mutation must either go through the gateway (preferred) or use a separate orchestrator-only credential bundle (decision-15).
- **`@require_private_mode`** gate on every Jira route — Jira routes 403 in public-mode sandbox sessions. Apply step's writes must run from the right session mode.
- **Idempotency**: the gateway has a 5-min idempotency cache (`gateway/jira_idempotency.py`) keyed by verb / project / key. Apply re-runs within 5 minutes will dedup at the gateway; longer-window idempotency must be enforced upstream via the task↔key mapping (decision-11 + feedback Q1).
- **JQL scope rule**: every search must AND with a `project = X` clause (`gateway/jira_search.py:55-128`). The reassess sweep's JQL must follow this — cross-project epic decomposition is degraded unless decision-12 changes that.
- **Plan-parser forest invariant**: `plan_parser.py:1284-1350` rejects multi-parent slices and cycles. The epic-pipeline plan output is a Jira-decomposition graph, not a code slice DAG, so this invariant only applies if we lean on `slices:` to represent the epic-plan structure (which is itself a decision — see decision-10's implications).
- **Atlassian-API quirks**: ticket-edit cannot set arbitrary custom fields today; transitions are forbidden by the agent-facing gateway. Anything that needs those fields must add a new orchestrator-only route (decision-15) or remain out of scope.
- **`fields` parameter behavior**: with `fields` omitted, `gateway/jira_client.py` does not pass a field list to Atlassian — the default field set is returned, which is not guaranteed to include `issuetype` long-term. Epic-detection callers should request it explicitly (decision-2).
- **File-write boundaries (gateway-enforced)**: REFINER (this role) can only push `.egg-state/drafts/` and `.egg-state/agent-outputs/`. Implementation work for #1557 spans `orchestrator/`, `gateway/`, `shared/`, `plugins/refine-plan/`, `sandbox/scripts/jira` — those are coder / tester / documenter territory, not refiner. **Plan must allocate each task's `role:` so file-write boundaries hold**: roughly `coder` for `orchestrator/`, `gateway/`, `shared/`, `sandbox/scripts/jira`; `documenter` for `plugins/refine-plan/skills/refine-plan/agents/*.md`, `docs/`, and `config/context-filters.yaml` schema doc; `tester` for `orchestrator/tests/`, `gateway/tests/`, `shared/tests/`. Tasks that touch both code and docs will need to be split per role.

**Business / scope:**

- MVP UX = operator's normal Claude Code host session calling `submit_task` (see feedback Q5). No new driver, no Jira-label state machine, no new HITL UX.
- Implement-phase cross-child coordination is out of scope; each child runs as its own independent pipeline.
- Resolved by the issue: per-child-ticket PRs (one implement pipeline per child); in-flight children carry `do-not-modify-without-confirmation` markers.

**Dependencies:**

- #1556 (Jira read), #1924 (Jira write), #2192 (bounded writes), #1931 (Confluence read), #2137 (stacked slice PRs), #2289 (in-flight handling) — **all merged/closed**. #1557 is unblocked.
- "Soft" dependency on #2137 only matters inside each downstream per-child pipeline, not in the epic pipeline itself.

**Architectural posture:**

- "Infrastructure beats config" — restrictions enforced at the gateway, not in agent instructions.
- Apply mutations are deterministic mechanical steps that happen on HITL approval; they sit above the BRC consensus model (BRC is for producer↔reviewer convergence on creative output, not for state-changing application of pre-approved decisions).
- Single Atlassian site assumed in v1 (see feedback Q4); the project allowlist already implies single-site.

## Options Considered

The decisions below are mostly **independent dimensions** of the design (detection timing, hierarchy field, apply location, prompt structure, etc.), so framing them as discrete-options A/B/C decisions in the Open Questions section captures more than a synthesized "Option A vs Option B" comparison would. The two high-level shapes that the decisions roll up into are below; everything else lives as a registered decision.

### Option A: Orchestrator-driven apply, parameterized prompts, contract-stored mapping (recommended baseline)

**Approach**: At `submit_task` time the orchestrator pre-fetches the ticket (with `fields=[issuetype, status, description, summary, parent]`) and persists `is_epic` on the Pipeline. The refine prompt is parameterized via `mode: epic | ticket | github_issue` and produces an epic-scoped analysis. The plan prompt produces per-task Jira-ticket-shaped descriptions and a plan-node ↔ existing-Jira-key mapping (consolidate / split / leave-alone). The orchestrator adds a **post-approval apply hook** on phase_gate resolution=approve: for refine, `editJiraIssue` writes the analysis to the epic Description; for plan, `editJiraIssue` / `createJiraIssue` / `createIssueLink` execute the mapping using the gateway's existing routes plus a new orchestrator-only transition route for Won't-Do. The plan-node ↔ Jira-key mapping is persisted on the contract task (`jira_key`, `jira_action` fields) so re-runs idempotently no-op. In-flight detection uses an orchestrator reverse-index from `jira_ticket → [pipelines]` (each pipeline persists its PR URL on PR-open), with status pulled in-band from the `getJiraIssue` response.

**Pros**:
- Single source of truth (the contract) for the mapping.
- Existing gateway idempotency cache + a contract-stored mapping make apply re-entry safe.
- One new gateway route (transition; orchestrator-only) keeps the agent-facing gateway clean.
- "Mode" parameter keeps refiner / planner prompts as single source of truth across all pipeline shapes.
- Apply is a deterministic mechanical step sitting above BRC — no double-consensus cycle.

**Cons**:
- Pre-fetch at `submit_task` time adds Jira RTT to a previously zero-IO MCP call.
- Apply hook is **new orchestrator behavior** — today HITL approval only advances state; this adds a side-effect class.
- The reverse-index from `jira_ticket` to pipelines is net-new state-store schema.
- Won't-Do transition route needs auth design (orchestrator-only — likely loopback + shared-secret).

### Option B: Sandbox-driven apply via a new `applier` agent role + BRC consensus on apply

**Approach**: After plan-gate HITL approval, the orchestrator spawns an `applier` role inside the sandbox. That role reads the contract task↔key mapping and calls the existing `jira` sandbox CLI to execute the mutations. Won't-Do transitions either (a) remain out-of-scope (markdown-only recommendation), or (b) require a new gateway transition route accessible to the applier role only. A separate reviewer agent ACKs the apply outcome via BRC.

**Pros**:
- Reuses the existing sandbox + audit + BRC infrastructure end-to-end.
- All mutations stay behind the agent-facing gateway; orchestrator never gains Atlassian creds.
- Apply receives the same independent review treatment as any other producer output.

**Cons**:
- Apply is **deterministic mechanical work**, not creative producer output; running it through BRC produces no signal at high cost (extra agent spawn, extra consensus cycle, extra prompt context window).
- Failure modes (partial apply, network errors) bubble out of an agent prompt rather than out of orchestrator code, which is harder to reason about for state-machine purposes.
- Pushes more responsibility into prompts (the apply prompt has to track per-mutation success / partial-apply / retry) when this kind of work is naturally code, not LLM.
- Adds a new phase to the pipeline state machine (or a new role to the plan phase).

## Recommended Approach

**Option A (orchestrator-driven apply, parameterized prompts, contract-stored mapping)**, subject to the decisions registered below. The rationale is that apply is deterministic mechanical orchestration, not creative producer output, and the orchestrator already owns the equivalent state-changing primitive for advancing pipeline phases on HITL resolution; adding "and also POST these Jira mutations" to that same code path keeps the state machine honest. The parameterized prompt design (decision-16, opt 1) keeps refine / plan agents as single sources of truth. The contract-stored mapping (decision-11, opt 1) carries the task↔key relationship through restarts and re-runs and combines with the gateway's existing idempotency cache to make apply re-entry safe.

Recommended slice decomposition (decision-1, opt 3): **two slices on a dependency edge** — slice-1 = A+B+C+D (fresh-epic path end-to-end: submit_task detection + refine prompt + plan prompt + apply), slice-2 = E+F+G (reassess sweep + in-flight detection + Won't-Do transitions) built on slice-1's primitives. Two PRs, no parallelism gain (reassess strictly extends fresh-epic, so a sequential edge is natural) but each PR is reviewable in isolation and slice-2 doesn't have to mock slice-1's hooks. Inside each slice, the cross-component role allocation noted in the Constraints section gives the planner a deterministic split: coder for orchestrator/gateway/shared/sandbox-script changes, documenter for prompt-file and config-doc changes, tester for tests.

Big-rock dimensions left to the operator: slice decomposition (decision-1), in-flight detection mechanism (decision-7), and the Won't-Do credential / route question (decision-15). Everything else is detail-shaping.

## Open Questions

**Decisions (multiple-choice — register via `mcp__sdlc__register_open_question`):**

- **decision-1 — Slice decomposition** (work-decomposition decision: A=plumbing, B=refine prompt, C=plan prompt, D=apply, E=reassess sweep, F=in-flight detection, G=Won't-Do transitions). Surfaces as a `phase_gate` choice between 1, 2, 3, and 4 slices with the shape of the slice DAG named explicitly. **Recommended baseline: option C** ([A+B+C+D fresh-epic path end-to-end] → [E+F+G reassess path], 2 PRs) — reassess strictly extends fresh-epic, so a dependency edge is natural and the second slice gets to land against the first instead of mocking it.
- **decision-2 — Epic detection timing**: orchestrator pre-fetch at `submit_task` time (recommended) vs explicit `jira_epic` param vs sandbox-side runtime detection.
- **decision-3 — Hierarchy field**: per-project config (recommended) vs auto-detect via project metadata vs `parent` with `Epic Link` fallback vs hybrid.
- **decision-4 — Reassess Won't-Do approval**: batch on plan-gate approval vs per-ticket HITL vs hybrid vs out of scope (markdown-only recommendation).
- **decision-5 — Done-children plan-prompt signal**: exclude entirely vs include with do-not-replan marker (summary only) vs include with do-not-replan marker (full description).
- **decision-6 — Consolidation survivor heuristic**: oldest vs most-linked vs planner-picks-with-HITL-override vs highest-status vs hybrid.
- **decision-7 — In-flight PR detection mechanism**: orchestrator reverse-index only vs both signals (index + remote-links route) vs remote-links only vs Jira status only. **Storage-shape sub-decision (decision-7a, raise during plan)**: the pipeline store is JSON-on-disk per-pipeline-ID today, so a `jira_ticket → [pipelines]` lookup is O(N) unless backed by (i) a sidecar index file rewritten on pipeline create / PR open, (ii) an in-memory derived index rebuilt on orchestrator startup by scanning the pipeline directory, or (iii) a SQLite cache. Pick during planning; folding it into decision-7 directly would overload the option list.
- **decision-8 — Apply step location**: orchestrator-side post-approval hook (recommended) vs new sandbox-side `applier` agent role vs hybrid with verifier.
- **decision-9 — Confluence-link extraction**: URL-scan description vs scan + new remote-links route vs out of scope in v1. **Placement note**: under option 1 or 2, the helper most naturally runs **in-sandbox inside the refiner** (already has the description, already has Confluence-CLI access via `sandbox/scripts/confluence`); placing it orchestrator-side would require giving the orchestrator a Confluence client. Reuse the existing in-sandbox path unless an explicit reason emerges.
- **decision-10 — Plan-YAML schema for ticket-shaped tasks**: reuse `tasks[].description` with section template (recommended) vs add sibling `jira_ticket_body` field vs structured sub-tree. **Slice-granularity sub-question (decision-10a, raise during plan)**: at what `slices:` granularity does the epic-plan emit child tickets — (i) one slice with N tasks where each task = one Jira child; (ii) N slices of 1 task each; (iii) N slices with cross-task dependency edges encoded via slice `dependencies:` to mirror Blocks links. Option (iii) is the closest semantic match to "Blocks" edges in Jira but interacts with the plan-parser forest invariant (`plan_parser.py:1284-1350` — multi-parent slices are rejected) so cycles / fan-in clusters need serialisation. Pick during planning.
- **decision-11 — Plan-node ↔ Jira-key mapping persistence**: on contract task (recommended) vs in plan draft markdown vs sidecar file.
- **decision-12 — JQL discovery: project scope**: same-project children only (recommended) vs loosen JQL extractor to allow Epic Link as scope vs all-allowlisted-projects loop.
- **decision-13 — "Done" status set**: `statusCategory.key == 'done'` (recommended) vs hard-coded status name list vs per-project config.
- **decision-14 — "In-flight" status set**: `statusCategory.key == 'indeterminate'` (recommended, paired with decision-13) vs hard-coded list vs per-project config.
- **decision-15 — Orchestrator-side transitions creds**: new orchestrator-only gateway transition route (recommended) vs direct Atlassian creds in orchestrator vs out of scope.
- **decision-16 — Refine/plan prompt structure**: parameterize via `mode` (recommended) vs split into per-mode prompt files vs single bloated prompt.

**Open-ended feedback (registered via `mcp__sdlc__request_feedback` as `feedback-1`):**

- **Q1**: Partial-apply recovery semantics (idempotent re-run / hard error / undo log).
- **Q2**: Pipeline-ID collision behavior on re-runs against an already-piped epic (qualifier / archive-and-replace / resume).
- **Q3**: PR ↔ Jira-ticket linkage when an implement pipeline opens a PR (remote-link / comment / both / neither).
- **Q4**: Multi-Atlassian-site posture — MVP single-site or leave a site indirection in `jira_policy.py` from day one.
- **Q5**: Operator UX for kicking off the pipeline (`submit_task` only, or do we need a `epic_mode` arg, or special description framing for reassess).
- **Q6**: V1 must-haves vs nice-to-haves across fresh-epic / reassess / Confluence / Won't-Do / PR-linkage scope.

## Complexity Assessment

**high** — broad surface across orchestrator + gateway + sandbox + prompts + contract schema, with at least three net-new infrastructure pieces (orchestrator reverse-index, post-approval apply hook, orchestrator-only gateway transition route) rather than extensions of existing patterns. Under the recommended 2-slice decomposition (decision-1 option C) the seven A–G parts cluster as **A+B+C+D in slice-1** (fresh-epic end-to-end) and **E+F+G in slice-2** (reassess path); under option A (single slice) the seven parts collapse into one slice's task list. The slice DAG question in decision-1 is the lever that decides whether this ships as one large PR, two dependent PRs, or 2–4 parallel/dependent PRs.

---

*Authored-by: egg*
