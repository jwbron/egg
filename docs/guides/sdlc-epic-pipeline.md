# SDLC Pipeline Support for Jira Epics

Drive a Jira **epic** through the same `submit_task` SDLC pipeline that
runs against a single Jira ticket today. The refine output writes to the
epic's `Description`; the plan output materialises as child Jira tickets
under the epic; an optional reassess path updates, consolidates, splits,
or marks obsolete children `Won't Do` when the epic already has children.

> Issue: [#1557](https://github.com/jwbron/egg/issues/1557).
> See also
> [SDLC Pipeline Guide](sdlc-pipeline.md),
> [Submit-Task MCP Reference](../reference/submit-task-mcp.md),
> [Jira Hierarchy Config](../reference/jira-hierarchy-config.md),
> [Jira Wrapper Reference](../reference/jira-wrapper.md),
> [Confluence Wrapper Reference](../reference/confluence-wrapper.md).

## What It Does

The epic flow is the same pipeline shape (`refine` → `plan` → optional
`implement` → `pr`) as today's single-ticket flow. Only the **IO sinks**
change:

- **Refine sink** — after HITL approval, the refined analysis is written
  to the epic's `Description` via the gateway's `editJiraIssue` route.
  Decision-9 chose wholesale-rewrite over patch-merge.
- **Plan sink** — after HITL approval, the planned child tasks are
  materialised as Jira children under the epic (`createJiraIssue` with
  either `parent` or `Epic Link`, plus `createIssueLink` for cross-task
  edges). Obsolete children are transitioned to `Won't Do` by the
  orchestrator directly.

No new pipeline phases, no new HITL surface, no new draft format on
disk. The HITL surface is the existing `.egg-state/drafts/<id>-*.md`
files plus the existing decision tools.

## How to Submit an Epic

The trigger is `submit_task`, with the epic key passed as `jira_ticket`
(the existing parameter — epic-vs-ticket detection happens up front in
the handler) and an optional `mode` parameter:

```python
# Fresh epic — no children yet
submit_task(
    jira_ticket="KORE-100",     # the epic key
    repo="owner/repo",
    description="Roll out feature X across services A and B",
)

# Explicit reassess (override auto-detection)
submit_task(
    jira_ticket="KORE-100",
    repo="owner/repo",
    description="Reassess feature X after the H1 review",
    mode="reassess",
)
```

`mode` accepts `{auto, reassess, fresh}` and defaults to `auto`:

| `mode` | Effect on an **Epic** key | Effect on a **Ticket** key |
|--------|---------------------------|----------------------------|
| `auto` (default) | Auto-detect: `reassess` if the epic has any non-Done children, else `fresh` | No-op — today's flow is unchanged |
| `reassess` | Force reassess; degrades to `fresh` with a logged warning when no children exist | No-op — `mode` is ignored for non-epic keys |
| `fresh` | Force fresh; logs a warning when children exist (operator asked for it explicitly) | No-op |

When `mode` is rejected (unknown value), the handler returns HTTP 400 —
mirroring the existing `qualifier` rejection.

The pipeline identifier follows current Jira naming (`<EPIC-KEY>` or
`<EPIC-KEY>-<qualifier>`); existing `submit_task` qualifier semantics are
unchanged.

> **Note**: The legacy interactive CLI (`bin/egg`) was removed in
> [#1762](https://github.com/jwbron/egg/issues/1762). The `mode` parameter
> only lands on the MCP surface — there is no equivalent CLI flag.
> See the [Submit-Task MCP Reference](../reference/submit-task-mcp.md)
> for the full parameter contract.

## End-to-End Flow

### 1. Epic-vs-ticket detection

When `submit_task` receives a Jira key, the handler dispatches the new
`detect_jira_issuetype` helper (`orchestrator/jira_epic_detect.py`),
which calls the gateway's `GET /api/v1/jira/ticket/{key}` and reads
`fields.issuetype.name`. When the value is `Epic`, the epic flow runs;
otherwise today's single-ticket flow runs unchanged.

The detection probe sits on the critical path of `submit_task`, so it
runs synchronously. A healthy gateway completes in ~2 s; the orchestrator
emits a `STATUS` message when total submit_task latency exceeds 5 s so
operators can tune.

### 2. Existing-children sweep (reassess)

In the reassess path (or `mode=auto` with children present), the
existing-children sweep classifies every child of the epic into one of
four states:

- **`done`** — Jira status is `Done`. Excluded entirely from the plan
  prompt (decision-4).
- **`to_do`** — neither `in_flight` nor `done`. Eligible for edit /
  Won't-Do / consolidation.
- **`in_flight`** — at least one of three signals fires (decision-8 uses
  **OR** semantics):
  - **`jira_status`** — Jira status is one of `{In Progress, In Review,
    Code Review, Blocked}`.
  - **`orchestrator_pr_url`** — the orchestrator has an active pipeline
    for this child whose `phases["pr"].artifacts["pr_url"]` is set.
    (Detected via a reverse-index file
    `.egg-state/jira-child-pipeline-index.json` so the sweep is
    bounded even on installs with hundreds of historical pipelines.)
  - **`remote_link`** — the child's Jira remote links include a GitHub
    PR URL (`^https?://github\.com/.*/pull/\d+`).

All firing signals are recorded on each `in_flight` classification, and
surfaced verbatim to the operator at the in-flight HITL gate (see
[In-flight gating](#in-flight-gating)).

The sweep uses two **independent** JQL queries — `parent = "<KEY>"` and
`"Epic Link" = "<KEY>"` — merged by `key`. Team-managed (Next-gen) Jira
projects don't have the `"Epic Link"` custom field, so a single-OR
disjunctive (`parent = ... OR "Epic Link" = ...`) returns HTTP 400 on
those projects. An HTTP 400 on either query alone is **tolerated**: the
sweep logs `jira_epic_search_field_missing` and treats that query's
result set as empty.

### 3. Refine phase — write to epic Description

The refine prompt branch is epic-shaped when `pipeline.jira_epic_key` is
set: the output is framed as a self-contained epic problem statement +
scope (not a ticket-shaped refinement). The prompt's "Destination" line
reads `Destination: Jira epic <KEY> Description (wholesale rewrite per
decision-9)` so the agent doesn't hand-shape a ticket body.

In the reassess path, the prompt additionally instructs the agent to
assess "what's done, what's changed, and what's no longer relevant"
given the existing-children context the refine input gatherer drops
under `.egg-state/agent-outputs/<id>-refine-input.json`. That input file
contains:

- The epic's own summary + description + remote links.
- Every child ticket's key, summary, status, description.
- Confluence pages discovered per decision-7 (remote links on the epic
  + URL-scrape of the epic's Description body + remote links on linked
  Jira issues, recursive **one** level only).

When the human approves the refine draft, a new sandbox agent — the
`apply_epic` agent — is spawned. It reads the approved draft from
`.egg-state/drafts/<id>-analysis.md`, fetches the current epic
Description, and wholesale-rewrites it via the gateway's
`PUT /api/v1/jira/ticket/<KEY>/edit` route.

**Concurrent-edit guard.** Before writing, the agent computes
`sha256(current Description)` and compares it against
`epic_apply.refine_description_sha256` (the hash the refine input
gatherer recorded at refine kick-off). When the hashes differ — i.e.
an operator edited the epic in Jira between refine kick-off and apply —
the agent opens a divergence HITL ("Operator edited the epic Description
after refine kick-off — confirm or skip overwrite?") and pauses apply
until the operator resolves it. Decision-9's wholesale-rewrite outcome
still drives the final answer; the guard exists so the overwrite isn't
silent.

### 4. Plan phase — write child tickets

The plan prompt branch is also epic-shaped when `pipeline.jira_epic_key`
is set. Every plan node — including pre-existing children in the
reassess path — must emit a fixed-structure ticket body (feedback Q2):

```text
Problem statement
Scope
Acceptance criteria
Out of scope
Cross-links
```

Cross-task edges use exactly one of `{Blocks, Is blocked by, Relates to}`
per edge with a rationale surfaced in the plan draft (feedback Q3).

In the reassess path, the agent produces a **classified diff**:

| Class | Meaning |
|-------|---------|
| `updated` | Existing child with revised scope; apply via `editJiraIssue` |
| `closed` | Existing child marked obsolete; apply via `Won't Do` transition |
| `untouched` | Existing child unchanged |
| `net-new` | Genuinely new child; apply via `createJiraIssue` |
| `consolidated` | N existing children collapse into one surviving key |
| `split` | One existing child becomes N new plan nodes |
| `in_flight` | Marked `do-not-modify-without-confirmation` — per-ticket HITL gate fires before any direct mutation |

`done` children are **excluded from the plan prompt entirely**
(decision-4) — the agent doesn't see them, so it can't re-propose
equivalent work. `done` children remain in the refine input bundle so
the analysis has full context.

For consolidations (N → 1), the agent records the chosen survivor key
plus rationale (decision-5) — the operator can override before
approval. For splits (1 → N), the agent records the original key plus
the new node IDs.

#### `Won't Do` plan-draft rendering

The plan draft renders a top-level
`## Tickets to be transitioned to Won't Do` section with a per-row
`⚠ This transition is permanent and not auto-reversed by egg` warning,
so the operator scrutinises the obsolete list at the plan-approval
gate. Every Won't-Do entry must carry a populated `wont_do_reason`
(the agent's evidence for why the child is obsolete); the parser
rejects entries without one — defence-in-depth so operators never see
a silent "obsolete: <key>" with no rationale.

#### Plan-node → Jira-key mapping (`epic_apply` artifact)

The plan draft's `# yaml-tasks` appendix gains a new top-level
`epic_apply:` block listing each plan-node → action mapping, plus
parallel `consolidations:` and `splits:` blocks. After the plan
parser ingests the YAML, the mapping is persisted to
`Pipeline.phases["plan"].artifacts["epic_apply"]` as an
`EpicApplyArtifact` (decision-10). The artifact records:

- `version` — schema version (forward-compat).
- `idempotency_seed` — per-pipeline UUID stamped at refine-apply time,
  passed as Atlassian's `X-Atlassian-Idempotency-Key` on every
  `createJiraIssue`. Retries after a successful-create-but-failed-record
  do **not** create a duplicate ticket.
- `refine_description_sha256` — the epic Description hash from refine
  kick-off (powers the concurrent-edit guard above).
- `plan_node_to_jira_key` — final mapping after apply.
- `applied_edits[]` — per-call `{kind, target, payload, summary_hash,
  applied_at, status, error}`. Idempotent re-runs skip entries whose
  `status == "applied"`.
- `wont_do_batch[]` — per-Won't-Do `{child_key, wont_do_reason, status,
  error}`.
- `in_flight_gates[]` — per-gate `{child_key, proposed_mutation,
  signal_source (list), signal_detail, linked_pr_url, decision_id}`.

The artifact is mutated through the new
`mcp__sdlc__update_epic_apply` MCP tool — the sandbox-side `apply_epic`
agent persists artifact updates through the orchestrator's MCP server
instead of writing files directly.

### 5. Plan apply — `apply_epic` agent (plan mode)

After the plan HITL approval, the `apply_epic` agent is re-spawned in
plan mode. It reads the plan draft's `epic_apply:` /
`consolidations:` / `splits:` sections, the existing-children sweep
output, and the hierarchy config (see [Operator setup](#operator-setup)),
then performs the batch:

1. `createJiraIssue` for each **net-new** node, using either `parent`
   or `epicLink` per `resolve_hierarchy_field(project_key)`.
2. `editJiraIssue` for each **`edit`** action target.
3. For each **consolidation**: `editJiraIssue` on the survivor +
   `addCommentToJiraIssue` redirect on the others. (The `Won't Do`
   transition itself runs in the orchestrator — see
   [Won't-Do transitions](#wont-do-transitions).)
4. For each **split**: `editJiraIssue` on the original to the
   narrowed scope, `createJiraIssue` for the new nodes.
5. `createIssueLink` for each cross-task edge.

After each call, the agent persists the updated `epic_apply` artifact
via `mcp__sdlc__update_epic_apply`.

### 6. Won't-Do transitions

The `transitions` verb is permanently off the agent-facing gateway
(`gateway/jira_client.py` enforces `ALLOWED_METHODS = frozenset({"GET"})`
plus a path denylist). Won't-Do therefore runs **orchestrator-direct**
via the new `orchestrator/jira_transitions.py` client.

The client:

- Reads the same `~/.config/egg/secrets.env` credentials as the gateway
  (the loader code now lives in `shared/egg_jira_credentials.py` and is
  re-exported through `gateway/jira_credentials.py` for backward
  compat).
- Resolves the project's `Won't Do` transition ID dynamically via
  `GET /rest/api/3/issue/{key}/transitions` (Jira project workflows
  vary; the ID is cached per project).
- Idempotent on re-run: before transitioning, fetches the current
  status; if it's already `Won't Do` (or any status in the `Done`
  category), short-circuits and returns
  `TransitionResult(status="already_in_state")` so retries don't
  400 on "transition is invalid".
- Posts a redirect comment that points at the survivor (for
  consolidations) or notes "obsolete after reassess of epic `<KEY>`"
  (for plain obsolete children).
- Emits one structured audit log per attempted transition:
  `{epic_key, child_key, from_status, to_status, principal=ATLASSIAN_USERNAME}`
  (feedback Q1).

Atomic-batch semantics (decision-3): the orchestrator attempts every
transition in the `wont_do_batch[]` after the single plan approval.
Per-entry success/failure is recorded on the artifact; a partial
failure does **not** halt the remaining transitions. Operators can
manually retry via re-run — idempotency against the persisted artifact
means the re-run picks up only the still-pending entries.

In-flight children are **skipped** by the orchestrator-side Won't-Do
loop; they have separate per-ticket HITL gates (see
[In-flight gating](#in-flight-gating)).

### 7. Plan-gate fork — Stop vs. Continue

After plan apply completes (children created, links written, Won't-Do
batch applied), the plan-gate HITL decision (decision-6) offers exactly
two options:

| Option | Effect |
|--------|--------|
| `Stop-after-plan` | The pipeline terminates: `state=COMPLETE` with `current_phase=plan_stopped`. No PR is created for the epic itself. Operators run `submit_task <CHILD-KEY>` on each created child when they want to begin implement work. |
| `Continue-to-implement` | The orchestrator fans out **one ISSUE-mode pipeline per created Jira child**. Each child pipeline carries `Pipeline.jira_parent_epic_key` so the PR-link writeback (below) fires when its PR opens. |

`plan_stopped` is a new terminal phase on the orchestrator
(`PipelinePhase.PLAN_STOPPED`). Status reports include the new phase
value; the overseer monitor's "no `pr_url` in phase artifacts" alert
is gated on `current_phase != plan_stopped` so a Stop-after-plan
termination doesn't fire a spurious alert.

> **PR shape.** Each Jira child runs as its own independent implement
> pipeline, so the epic produces **per-child-ticket PRs** rather than
> a single epic-level PR. With slice-DAG implement
> ([#2137](https://github.com/jwbron/egg/issues/2137)) landed, an
> individual child's implement may itself produce a stack of PRs along
> its slice DAG; without it, each child produces one PR via today's
> monolithic implement. The epic flow works under either model.

### 8. PR-link writeback

When a child's implement-phase pipeline reaches its PR phase and writes
`phases["pr"].artifacts["pr_url"]`, the orchestrator additionally posts
a comment on the child Jira ticket with the PR URL (feedback Q4) —
**only when `Pipeline.jira_parent_epic_key` is set on the child**.

This means:

- Children fanned out from an epic plan (Continue-to-implement)
  automatically get a PR-link comment on each child's Jira ticket.
- Single-ticket pipelines (no `jira_parent_epic_key`) keep today's
  behaviour: no comment writeback, no extra Jira round-trip.

Idempotency: before writing, the orchestrator checks whether the most
recent N comments on the child ticket already contain the PR URL.
Re-runs do not duplicate the comment.

When the gateway adds a future remote-link **write**, the comment
writeback will be paired with a symmetric remote-link write. Today the
gateway exposes remote-link reads only (TASK-1-6 added the read route).

## In-flight gating

Any **direct mutation** of an `in_flight` child — `edit`, `Won't-Do`,
or `consolidate-away` — is refused by the `apply_epic` agent and instead
opens a per-ticket HITL gate via the new
`mcp__sdlc__register_in_flight_gate` MCP tool. The gate surfaces:

- The proposed mutation (e.g. "Edit description to: …", "Transition to
  Won't Do", "Consolidate into `<KEY>`").
- The firing signal source(s) (decision-8's OR — every firing signal is
  recorded, not just the first).
- The linked PR URL (if any).
- The current Jira status.

The operator chooses `Confirm` or `Skip` per gate. On resolution, the
pipeline re-spawns `apply_epic`, which reads the resolved decisions
and applies only the `Confirm`ed mutations — `applied_edits[]`
idempotency from the prior run means already-applied non-in-flight
mutations are not re-attempted.

Creates that merely **depend on** an in-flight child apply normally;
the gate only fires on a direct mutation of the in-flight target.

### Apply-time re-check

Status (and remote-link state) can change between plan approval and
apply. To catch a child that became `in_flight` after the operator
approved the plan, `apply_epic` re-invokes `sweep_existing_children`
**immediately before each mutation**. A target that newly classifies
as `in_flight` is routed to the same HITL gate rather than mutated.

### Trust-boundary trade-off (v1)

Per decision-11 (hybrid execution), the gateway-mediated writes run on
the sandbox-side `apply_epic` agent rather than from the orchestrator
pod. This delivers the apply-step inside the agent's normal tool
surface but means **gateway-level enforcement of the in-flight gate
is not part of v1**: a buggy or prompt-injected agent could bypass the
gate by skipping `register_in_flight_gate` and calling `editJiraIssue`
or `addCommentToJiraIssue` on an in-flight target directly.

The orchestrator hardens this two ways within v1:

1. The existing-children sweep records the in-flight signal sources on
   `epic_apply.in_flight_gates[]` at plan time, so any later bypass
   is provable from the persisted artifact.
2. The apply step re-runs the sweep immediately before each mutation
   and refuses to apply any mutation whose target is still
   `in_flight` at that moment.

Every applied mutation is persisted on `epic_apply.applied_edits[]`, so
a partial bypass surfaces in audit.

**Full gateway-side enforcement** — an orchestrator-mediated dispatcher
for every `editJiraIssue` / `addCommentToJiraIssue` call routed through
a server-side in-flight re-check — is **future hardening**, tracked as
a planned follow-up issue once the v1 surface is stable. The trade-off
is explicit: decision-11 already delegates per-call gateway dispatch to
the sandbox agent; hardening this verb-specific path requires designing
a new in-flight-mediator surface that is out of scope for #1557.

## Operator setup

The epic flow is opt-in. Existing single-ticket pipelines are
unaffected; nothing changes until an operator submits an epic key plus
the supporting configuration.

### 1. Atlassian credentials with transition permission

For the reassess path's Won't-Do transitions, the orchestrator needs an
Atlassian principal with **transition permission on every Jira project
the operator intends to reassess against**. Add the credentials to
`~/.config/egg/secrets.env` alongside the existing read credentials
(feedback Q1):

```env
ATLASSIAN_USERNAME=ops-bot@example.com
ATLASSIAN_API_TOKEN=ATATT3xFfGF0...
# JIRA_USERNAME / JIRA_API_TOKEN are accepted as per-key fallbacks
# (existing gateway loader behaviour preserved for backward compat).
```

Without transition-capable credentials, the Won't-Do step opens a HITL
asking the operator to enable + retry or skip the batch — no transition
is attempted.

### 2. Feature flag — `EGG_ENABLE_ORCH_JIRA_TRANSITIONS`

The orchestrator-direct transition client is gated behind
`EGG_ENABLE_ORCH_JIRA_TRANSITIONS=true` (default `false`). When
disabled, `JiraTransitionsClient` raises `OrchJiraTransitionsDisabled`,
the caller logs the failure, and a HITL is opened asking the operator
to enable orchestrator-direct transitions.

This preserves the **strict zero-credential default** for installs that
haven't opted into the orchestrator-direct cred surface. Operators on
those installs see the epic flow's create/edit/link steps work — only
the `Won't Do` transitions await the flag flip.

Enable it in the orchestrator's environment (e.g. via the orchestrator
container's env block or systemd unit):

```env
EGG_ENABLE_ORCH_JIRA_TRANSITIONS=true
```

### 3. Per-project hierarchy-field config

Jira projects can be company-managed (Classic) or team-managed
(Next-gen). They differ in how a child ticket points at its parent
epic:

- Company-managed (Classic): the `"Epic Link"` custom field.
- Team-managed (Next-gen): the standard `parent` field.

The `apply_epic` agent needs to know which field to populate per
project. Operators map projects in `~/.config/egg/jira-hierarchy.yaml`:

```yaml
projects:
  ENG: parent
  KORE: epic_link
```

The apply step **refuses unmapped projects** (decision-2) — better to
error loudly than ship children under the wrong field. See the
[Jira Hierarchy Config Reference](../reference/jira-hierarchy-config.md)
for the full schema and worked examples.

### 4. Jira / Confluence gateway prerequisites

The epic flow rides on the existing Jira and Confluence gateway
wrappers. The gateway must be in private mode and the relevant project
/ space allowlists must include the epic's project and any linked
Confluence spaces:

- [Jira Wrapper Reference](../reference/jira-wrapper.md) — read +
  bounded write surface (`ticket/get`, `search`, `ticket/comments`,
  plus the bounded write extension `ticket/create`, `ticket/edit`,
  `ticket/comment/add`, `issue-link/create`). The new
  `GET /api/v1/jira/ticket/{key}/remotelinks` read route powers
  in-flight detection.
- [Confluence Wrapper Reference](../reference/confluence-wrapper.md) —
  read-only space allowlist; the refine input gatherer pulls linked
  pages (decision-7).

## Decision summary

The full HITL decision set from refine is folded into the implementation
as follows:

| ID | Question | Resolution |
|----|----------|------------|
| decision-1 | Slice decomposition | Single slice (1 PR) |
| decision-2 | `parent` vs `Epic Link` hierarchy | Per-project YAML map; error on unmapped |
| decision-3 | Reassess Won't Do — batch vs per-ticket | Single plan approval applies all Won't Do atomically (in-flight gate is separate) |
| decision-4 | Done children in plan prompt | Excluded entirely |
| decision-5 | Consolidation survivor (N→1) | Agent picks with rationale; operator override before approval |
| decision-6 | Implement trigger for created children | Plan-gate offers Stop vs Continue |
| decision-7 | Confluence link discovery scope | Remote links + epic-description URL scrape + linked-Jira-issue remote links (recursive 1 level) |
| decision-8 | In-flight signal precedence | OR semantics; firing signal source logged on each HITL gate |
| decision-9 | Reassess refine-output strategy | Wholesale rewrite of epic Description |
| decision-10 | Plan-node → Jira-key mapping persistence | `phases["plan"].artifacts["epic_apply"]` as `EpicApplyArtifact` |
| decision-11 | Apply-step execution location | Hybrid — `apply_epic` agent for gateway-mediated writes; orchestrator-only for transitions |
| decision-12 | `submit_task` reassess override | Single `mode={auto,reassess,fresh}` param; default `auto` preserves today |

Refine-phase feedback responses:

| Feedback | Resolution |
|----------|------------|
| Q1 — orchestrator Jira cred posture | Reuse `~/.config/egg/secrets.env`; emit per-transition audit log with `{epic_key, child_key, from_status, to_status, principal}` |
| Q2 — plan-node ticket structure | Fixed `Problem statement / Scope / Acceptance criteria / Out of scope / Cross-links` |
| Q3 — Jira link types | Agent picks per-edge from `{Blocks, Is blocked by, Relates to}` with rationale in plan draft |
| Q4 — PR-link writeback | Comment on child ticket when the child's implement opens a PR |

## What this is **not**

- Not a new pipeline, phase engine, or scheduling surface.
- Not a new HITL UX — same draft + decision flow.
- Not a Jira-label state machine (the original `egg-sdlc` /
  `egg-awaiting-response` label-driven framing is out of scope for v1).
- Not implement-phase cross-child coordination or scheduling — each
  child runs as its own independent implement pipeline.

## Limits

- **Confluence recursion depth.** Linked-Jira-issue remote-link
  discovery is capped at **one** level. Two-level chains are not
  followed.
- **Single-OR JQL is rejected.** The existing-children sweep deliberately
  uses two independent JQL queries; a single-OR disjunctive fails on
  team-managed projects without the `"Epic Link"` field.
- **In-flight false negatives** are mitigated, not eliminated. A child
  mid-implement whose pipeline hasn't yet written `pr_url`, whose Jira
  status is still `To Do`, and whose remote-links don't yet name the
  PR is detectable only by the per-ticket HITL gate at apply time.
  Decision-8's OR semantics + the apply-time re-check are the v1
  mitigation; operators should treat the in-flight gate as the
  authoritative confirmation rather than the planner's classification.
- **Plan-apply atomicity** is per-call, not per-batch. A network blip
  mid-batch leaves the epic partially applied; idempotency via
  `epic_apply.applied_edits[]` + `idempotency_seed` covers re-runs.
- **Stop-after-plan** does not auto-trigger implement on any child;
  operators submit each child explicitly via `submit_task <CHILD-KEY>`
  when ready.

## See also

- [SDLC Pipeline Guide](sdlc-pipeline.md) — pipeline shape, phase
  cycling, HITL surface that the epic flow reuses unchanged.
- [Submit-Task MCP Reference](../reference/submit-task-mcp.md) — the
  full `submit_task` parameter contract including the new `mode`
  parameter.
- [Jira Hierarchy Config Reference](../reference/jira-hierarchy-config.md) —
  YAML schema for `~/.config/egg/jira-hierarchy.yaml`.
- [Jira Wrapper Reference](../reference/jira-wrapper.md) — gateway
  Jira surface.
- [Confluence Wrapper Reference](../reference/confluence-wrapper.md) —
  gateway Confluence surface.
- [Slice-DAG Implement Phase](../architecture/slice-dag.md) — stacked
  PR delivery model for individual child implements.
