# Plan: Add SDLC pipeline support for Jira epics

> Issue: #1557 | Phase: plan

## Summary

Treat a Jira **epic** as the SDLC unit of work. The host's
`submit_task` MCP call accepts an epic key, the existing
refine → plan pipeline runs against it, and on each HITL approval a
new sandbox-side **applier** role drives the appropriate Jira sink
(epic Description on refine, child create / edit / link / Won't-Do on
plan). The reassess path extends the fresh-epic path so an epic that
already has children gets its existing tickets classified
(Done / In-flight / Updatable), consolidated, split, or left alone
without re-creating equivalent work.

The work decomposes into two stacked slices per the operator's
decision-1 (option C — `[A+B+C+D fresh-epic path] → [E+F+G reassess
path]`). Slice 2 strictly extends slice 1: it adds the JQL sweep, the
in-flight detection signals, the orchestrator-only Won't-Do
transitions, and the reassess-mode prompt branch on top of the
fresh-epic plumbing.

## Approach

The design honours all 16 resolved decisions from the refine analysis
and the six feedback answers. Highlights:

- **Epic detection up front** (decision-2). At `submit_task` time the
  orchestrator pre-fetches the ticket via the gateway with
  `fields=['issuetype','status','description','summary','parent']`
  and persists `is_epic` + `pipeline_mode` ('fresh' | 'reassess') on
  the Pipeline model. A new `mode` arg on `submit_task` ('auto' |
  'fresh' | 'reassess', default 'auto' per feedback Q5) lets the
  operator override the detector.
- **Mode-parameterised prompts** (decision-16). Refiner and
  task-planner prompts get a single `mode` block (`epic-fresh`,
  `epic-reassess`, `ticket`, `github_issue`) injected at spawn so the
  same prompt file covers every shape. The orchestrator's prompt-prep
  helper **strips the non-matching mode blocks server-side** before
  the prompt is sent to the agent (per risk_analyst R10 mitigation
  (b)), so the agent never sees competing mode branches and the
  pattern is robust across model upgrades.
- **Per-task ticket-shaped descriptions** (decision-10). The
  `task-planner.md` epic mode requires every task `description:` to be
  a ticket-ready body with `Problem`, `Scope`, `Acceptance`,
  `Out of Scope`, `Links` sections. Schema is unchanged — the
  description field carries the convention.
- **Applier as a new sandbox role + REVIEWER_CONTRACT for apply
  consensus** (decision-8 + architect's slice-3 design + risk_analyst
  R1 mitigation). Spawned after every epic-mode HITL approval; reads
  contract artifacts; calls the jira sandbox CLI for create / edit /
  link mutations. Stays behind the existing gateway audit + auth
  boundary. The new `apply` phase has `_PHASE_REVIEWERS["apply"] =
  [REVIEWER_CONTRACT]` — the contract reviewer ACKs on
  contract-state convergence (every Task with `jira_action='create'`
  has a non-null `jira_key` matching `^[A-Z][A-Z0-9_]*-[0-9]+$`,
  every Task has `jira_action_status` in `{'applied','failed'}`,
  no in-flight child mutated without the `in-flight-confirmed`
  marker). The applier role also extends the orchestrator side:
  `PipelinePhase.APPLY = "apply"` joins the existing enum; the
  gateway's `VALID_TRANSITIONS` gains conditional edges
  `PLAN -> APPLY` and `APPLY -> IMPLEMENT` gated on
  `Pipeline.is_epic`.
- **Contract-stored mapping + lifecycle status** (decision-11 +
  feedback Q1 + risk_analyst R7). `Task` gains optional `jira_key`,
  `jira_action`, and `jira_action_status: Literal['pending',
  'in_flight','applied','failed'] | None` fields. The applier writes
  `'in_flight'` to the contract before each gateway call and
  `'applied'` (or `'failed'` with reason) after, so partial-apply
  recovery distinguishes "already done" from "not started" for every
  action type — not just create. On re-run, the applier skips tasks
  where `jira_action_status == 'applied'` and re-attempts tasks in
  `{'pending','failed'}`. Long-window idempotency lives on the
  contract; short-window (≤5 min) is covered by
  `gateway/jira_idempotency.py`.
- **Per-project hierarchy** (decision-3). The existing
  `gateway/jira_policy.py:163` `epic_link_field()` hook is
  authoritative; no auto-detection. Slice 1 wires the applier's
  create-call to use it.
- **Reassess sweep** (decisions 5 + 12 + 13 + 14). JQL is constrained
  to `project = <P> AND parent = <K>` (same-project only). Children
  classify via `statusCategory.key` (`done` / `indeterminate` / `new`);
  Done children are excluded from the planner prompt; `in_flight` is
  derived from `indeterminate` status **and** the open-PR signal.
- **Two-signal in-flight PR detection** (decision-7). Slice 2 adds an
  orchestrator reverse-index (`jira_ticket → [pipelines]`) plus a new
  read-only gateway route `POST /api/v1/jira/ticket/remotelinks` so
  human-opened PRs (no egg pipeline) still get caught.
- **Orchestrator-only Won't-Do route** (decision-15). Won't-Do
  transitions land via a new gateway route gated on a loopback +
  shared-secret token — agent-facing routes still 403 on transitions,
  so the "creds only in gateway" invariant holds.
- **Stub-Jira test fixture** (architect's `open_questions_for_
  reviewer_plan` #2). The integration tests run against an
  in-process Flask fake at `integration_tests/fixtures/stub_jira.py`
  (TASK-1-7a). The k3s test stack gains a `stub-jira` container; the
  gateway pod's `JIRA_BASE_URL` env var is overridden to point at it.
  The fake supports the four routes the applier hits: `GET /rest/api
  /3/issue/{KEY}`, `POST /rest/api/3/issue`, `PUT /rest/api/3/issue
  /{KEY}`, `POST /rest/api/3/issueLink`, plus the slice-2 surfaces
  `GET /rest/api/3/issue/{KEY}/remotelink`, `POST /rest/api/3/issue
  /{KEY}/transitions`, and `POST /rest/api/3/search` (so the
  reassess sweep's JQL goes somewhere). New end-to-end tests live
  under `integration_tests/epic_pipeline/` (NEW dir) so they don't
  collide with the pure-contract tests under `integration_tests/sdlc/`.

- **Reverse-index storage shape** is registered as **decision-17**
  via `mcp__sdlc__register_open_question` (per risk_analyst HR3) so
  the operator picks before slice-2 implement starts. Default if no
  pick is made: option A (in-memory only, rebuilt on startup).

- **Single-PR-per-issue stacking**. Decision-1 picked option C: two
  slices stacked, slice 2 depends on slice 1. The implement-phase
  pipeline ships them as two stacked PRs along the slice DAG.

## Primitives

Every primitive cited below is verified by `grep`/`Read`. `(NEW —
task TASK-X-Y)` markers tag primitives created by this plan; the
listed task is the unique creator, and downstream consumers all live
strictly downstream in the slice DAG (slice 2 consumers downstream of
slice 1 creators; intra-slice consumers downstream of intra-slice
creators).

### Already in the tree

| Primitive | Citation | Execution-context scope |
|-----------|----------|-------------------------|
| `submit_task` MCP tool definition | `orchestrator/mcp_tools.py:67-127` | host (operator's Claude) |
| `submit_task` handler `_handle_submit_task` | `orchestrator/mcp_tools.py:1272-1381` | orchestrator |
| `submit_task` jira_ticket validation | `orchestrator/mcp_tools.py:1287-1292` | orchestrator |
| `submit_task` pipeline_id derivation (jira branch) | `orchestrator/mcp_tools.py:1301-1307` | orchestrator |
| `Pipeline.jira_ticket` field + validator | `orchestrator/models.py:981-1004` | orchestrator (Pydantic) |
| `Pipeline.pr_number` (babysit) field | `orchestrator/models.py:860-864` | orchestrator |
| `Task` model | `shared/egg_contracts/models.py:182-242` | orchestrator (Pydantic) |
| `Slice` model | `shared/egg_contracts/models.py:243+` | orchestrator (Pydantic) |
| `_HITL_GATE_PHASES = {"refine", "plan"}` | `orchestrator/routes/pipelines.py:17344` | orchestrator |
| `_persist_phase_gate_resolution` | `orchestrator/routes/pipelines.py:18274+` | orchestrator |
| Phase-gate resolution call site (refine) | `orchestrator/routes/pipelines.py:20506` | orchestrator |
| `EGG_JIRA_TICKET` / `EGG_JIRA_PROJECT` env injection | `orchestrator/routes/pipelines.py:19390-19404` | in-sandbox-agent (set by orchestrator) |
| `state_store.create_pipeline` | `orchestrator/state_store.py:972-992` | orchestrator |
| `AgentRole` enum | `shared/egg_contracts/agent_roles.py:46-90` | orchestrator + in-sandbox-agent |
| `AGENT_ROLES` registry | `shared/egg_contracts/agent_roles.py:894-912` | orchestrator |
| `_PHASE_ROLES` map | `shared/egg_contracts/agent_roles.py:1107-1112` | orchestrator |
| `_PHASE_REVIEWERS` map | `shared/egg_contracts/agent_roles.py:1113-1130` | orchestrator |
| `get_roles_for_phase` | `shared/egg_contracts/agent_roles.py:1285-1330` | orchestrator |
| File-restriction patterns module | `shared/egg_restrictions/patterns.py` | gateway (write-policy enforcer) |
| `CODER_PATTERNS` | `shared/egg_restrictions/patterns.py:108-184` | gateway |
| `DOCUMENTER_PATTERNS` | `shared/egg_restrictions/patterns.py:229-267` | gateway |
| `_PLAN_AGENT_BLOCKED` | `shared/egg_restrictions/patterns.py:271-285` | gateway |
| `ARCHITECT_PATTERNS` | `shared/egg_restrictions/patterns.py:287-296` | gateway |
| `parse_yaml_code_fence` | `shared/egg_contracts/plan_parser.py:258` | orchestrator |
| `parse_tasks_from_yaml` | `shared/egg_contracts/plan_parser.py:359` | orchestrator |
| `parse_phases_from_yaml` (slices) | `shared/egg_contracts/plan_parser.py:413` | orchestrator |
| `validate_forest` | `shared/egg_contracts/plan_parser.py:1288` | orchestrator |
| `parse_plan` | `shared/egg_contracts/plan_parser.py:1065` | orchestrator |
| Refiner prompt | `plugins/refine-plan/skills/refine-plan/agents/refiner.md` | in-sandbox-agent |
| Task-planner prompt | `plugins/refine-plan/skills/refine-plan/agents/task-planner.md` | in-sandbox-agent |
| Architect prompt | `plugins/refine-plan/skills/refine-plan/agents/architect.md` | in-sandbox-agent |
| Risk-analyst prompt | `plugins/refine-plan/skills/refine-plan/agents/risk-analyst.md` | in-sandbox-agent |
| Gateway `POST /api/v1/jira/ticket/get` | `gateway/gateway.py:4929-5009` | gateway |
| Gateway `POST /api/v1/jira/search` | `gateway/gateway.py:5012-5133` | gateway |
| Gateway `POST /api/v1/jira/ticket/comments` | `gateway/gateway.py:5136+` | gateway |
| Gateway `POST /api/v1/jira/ticket/create` | `gateway/gateway.py:5580+` | gateway |
| Gateway `POST /api/v1/jira/ticket/edit` | `gateway/gateway.py:5839-5996` | gateway |
| Gateway `POST /api/v1/jira/ticket/comment/add` | `gateway/gateway.py:5999+` | gateway |
| Gateway `POST /api/v1/jira/issue-link/create` | `gateway/gateway.py:6104+` | gateway |
| Gateway `POST /api/v1/jira/execute` | `gateway/gateway.py:5198+` | gateway |
| `JIRA_WRITE_VERBS_DENIED` | `gateway/jira_client.py:133` | gateway |
| `validate_jira_api_path` | `gateway/jira_client.py:217-283` | gateway |
| `validate_fields` (Jira ticket-get fields list) | `gateway/jira_client.py:286+` | gateway |
| JQL extractor `extract_search_projects` | `gateway/jira_search.py:55-128` | gateway |
| `JiraPolicy.epic_link_field()` | `gateway/jira_policy.py:163` | gateway |
| `_VALID_EPIC_LINK_FIELDS` allowlist | `gateway/jira_policy.py:91` | gateway |
| `IDEMPOTENCY_TTL_SECONDS = 300` | `gateway/jira_idempotency.py:66` | gateway |
| Confluence `page/get` route | `gateway/gateway.py:6515+` | gateway |
| Confluence ADF helpers | `gateway/jira_adf.py:38+` (no URL extractor) | gateway |
| `config/context-filters.yaml` jira block | `config/context-filters.yaml:11-50` | gateway / operator-managed |
| Sandbox `jira` CLI | `sandbox/scripts/jira` | in-sandbox-agent |
| Sandbox `confluence` CLI | `sandbox/scripts/confluence` | in-sandbox-agent |
| `EggStack` dataclass + `gateway_url` attribute | `integration_tests/conftest.py:71-93` (`gateway_url: str` at `:78`); pytest fixtures `egg_stack` at `:308` and `orchestrator_url` at `:325`. `gateway_url` is **not** a standalone fixture — tests reach the URL via `egg_stack.gateway_url` (per `docs/architecture/integration-test-trust-boundary.md`). | local-test-only (kubectl-gated) |
| `PipelinePhase` enum | `shared/egg_contracts/models.py:62-68` (`REFINE`, `PLAN`, `IMPLEMENT`, `PR`) | orchestrator (Pydantic) |
| `VALID_TRANSITIONS` map | `gateway/phase_transition.py:41-47` | gateway / orchestrator |
| `get_next_phase` | `gateway/phase_transition.py:201-216` | gateway / orchestrator |
| `epicLink` shorthand dispatch in ticket-create (already wired through `JiraPolicy.epic_link_field()`) | `gateway/gateway.py:5358, 5413, 5594, 5697-5748` | gateway |
| `ApprovalMatrix.is_fully_acked` | `orchestrator/approval_matrix.py:316-326` | orchestrator |
| Existing in-sandbox CLI for transitions (none — `/transitions` denied at gateway, see `gateway/jira_client.py:133`) | `(absent by design)` | gateway invariant |
| Existing `integration_tests/sdlc/` test convention | pure-Python contract tests (`test_happy_path.py`, `test_hitl_flow.py`); imports `egg_contracts`, no `egg_stack`, no kubectl. New kubectl-gated end-to-end tests for this issue therefore live under `integration_tests/epic_pipeline/` (NEW dir, see TASK-1-7 / TASK-2-9) with its own conftest that imports `egg_stack` from the parent. | local-test-only (kubectl-gated) |

### NEW (created by this plan)

| Primitive | Created in | Execution-context scope |
|-----------|-----------|-------------------------|
| `submit_task` `mode` arg ('auto' / 'fresh' / 'reassess') | `(NEW — task TASK-1-1)` | host → orchestrator |
| Orchestrator pre-fetch + `is_epic_for_ticket(...)` helper | `(NEW — task TASK-1-1)` | orchestrator |
| `Pipeline.is_epic` (bool) field | `(NEW — task TASK-1-1)` | orchestrator (Pydantic) |
| `Pipeline.pipeline_mode` ('fresh' / 'reassess' / null) field | `(NEW — task TASK-1-1)` | orchestrator (Pydantic) |
| `Pipeline.pr_url` (str / null) field | `(NEW — task TASK-2-2)` | orchestrator (Pydantic) |
| `EGG_PIPELINE_MODE` / `EGG_IS_EPIC` env vars (mode mapping rule: `is_epic=True + pipeline_mode='fresh' → 'epic-fresh'`; `is_epic=True + pipeline_mode='reassess' → 'epic-reassess'`; `jira_ticket is not None → 'ticket'`; else `'github_issue'`) | `(NEW — task TASK-1-1)` | in-sandbox-agent (set by orchestrator) |
| Loader-side mode-block strip helper (regex-strips fenced `## [mode: X]` blocks not matching the active mode in refiner / task-planner / applier prompts) | `(NEW — task TASK-1-1)` | orchestrator |
| `Task.jira_key` (str / null) field | `(NEW — task TASK-1-3)` | orchestrator (Pydantic) |
| `Task.jira_action` literal field | `(NEW — task TASK-1-3)` | orchestrator (Pydantic) |
| `Task.jira_action_status` literal field (`'pending'` / `'in_flight'` / `'applied'` / `'failed'`) — risk_analyst R7 | `(NEW — task TASK-1-3)` | orchestrator (Pydantic) |
| Plan-parser support for `jira_key` / `jira_action` / `jira_action_status` per-task YAML keys | `(NEW — task TASK-1-3)` | orchestrator |
| `AgentRole.APPLIER` enum value (`"applier"`) | `(NEW — task TASK-1-4)` | orchestrator + in-sandbox-agent |
| `APPLIER_ROLE` `AgentRoleDefinition` registration in `AGENT_ROLES` | `(NEW — task TASK-1-4)` | orchestrator |
| `_PHASE_ROLES["apply"] = [APPLIER]` registration | `(NEW — task TASK-1-4)` | orchestrator |
| `_PHASE_REVIEWERS["apply"] = [REVIEWER_CONTRACT]` registration | `(NEW — task TASK-1-4)` | orchestrator |
| `PipelinePhase.APPLY = "apply"` enum value | `(NEW — task TASK-1-4)` | orchestrator (Pydantic) |
| `VALID_TRANSITIONS[PLAN].append(APPLY)` + `VALID_TRANSITIONS[APPLY] = [IMPLEMENT]` (gated on `Pipeline.is_epic`) | `(NEW — task TASK-1-4)` | gateway / orchestrator |
| `APPLIER_PATTERNS` file-write restriction in `patterns.py` | `(NEW — task TASK-1-4)` | gateway |
| Apply-phase scheduling (orchestrator phase-scheduler advancement on HITL approve when `is_epic`) | `(NEW — task TASK-1-4)` | orchestrator |
| Applier prompt `applier.md` | `(NEW — task TASK-1-5)` | in-sandbox-agent |
| Reviewer-contract supplement for apply-phase contract-state convergence checks | `(NEW — task TASK-1-5)` | in-sandbox-agent |
| Stub-Jira fake (`integration_tests/fixtures/stub_jira.py` Flask app) + `stub-jira` k3s container + `JIRA_BASE_URL` override | `(NEW — task TASK-1-7)` | local-test-only (kubectl-gated) |
| Refiner / task-planner mode-parameterisation block | `(NEW — task TASK-1-2)` | in-sandbox-agent |
| Reassess-mode prompt branches in refiner / task-planner | `(NEW — task TASK-2-5)` | in-sandbox-agent |
| Reassess sweep helper (JQL + classification) | `(NEW — task TASK-2-1)` | orchestrator |
| `pipelines_for_jira_ticket(...)` reverse-index API | `(NEW — task TASK-2-2)` | orchestrator (state_store) |
| Pipeline `pr_url` capture on PR-open | `(NEW — task TASK-2-2)` | orchestrator |
| Gateway route `POST /api/v1/jira/ticket/remotelinks` (read) | `(NEW — task TASK-2-3)` | gateway |
| `validate_jira_api_path` allow-rule for `/issue/{key}/remotelink` GET | `(NEW — task TASK-2-3)` | gateway |
| `sandbox/scripts/jira ticket remotelinks <KEY>` subcommand | `(NEW — task TASK-2-3)` | in-sandbox-agent |
| In-flight detection helper (status + PR signals) | `(NEW — task TASK-2-4)` | orchestrator |
| Gateway route `POST /api/v1/jira/ticket/transition` (orchestrator-only, allowlisted) | `(NEW — task TASK-2-6)` | gateway |
| Loopback + shared-secret token check for `/transition` | `(NEW — task TASK-2-6)` | gateway |
| Applier extension: in-flight refusal + Won't-Do batch + consolidate / split (orchestrator-side scheduling) | `(NEW — task TASK-2-7)` | orchestrator |
| Applier prompt extension: per-`jira_action` mutation routing + in-flight refusal documentation | `(NEW — task TASK-2-8)` | in-sandbox-agent |

### Trust-boundary scope notes

- The new `/transition` route is **orchestrator-only** (loopback +
  shared-secret token). Agent-facing Jira surface continues to deny
  transitions via `JIRA_WRITE_VERBS_DENIED`
  (`gateway/jira_client.py:133`).
- The `/remotelinks` route is read-only and is added to the existing
  agent-facing Jira gating (`@require_private_mode` + project
  allowlist).
- The **applier** role runs inside the sandbox and uses only the
  agent-facing gateway routes. It does not get Atlassian credentials
  directly; all writes go through gateway audit and idempotency.
- The integration-test trust-boundary still applies: tests that need
  `gateway_url` as a pytest fixture live under `integration_tests/`
  and depend on the kubectl-gated `EggStack` (`integration_tests/
  conftest.py:71+`). Pure unit tests live under
  `gateway/tests/`, `orchestrator/tests/`, and
  `shared/egg_contracts/tests/`.

## Test strategy

- **Unit (orchestrator + gateway)**: Pipeline / Task model serialisation
  with the new fields; plan-parser ingestion of `jira_key` /
  `jira_action`; APPLIER role registry + patterns; epic-detection
  helper against a mocked gateway response; Won't-Do allowlist
  enforcement; reverse-index round-trips; in-flight classifier truth
  table.
- **Unit (gateway routes)**: `/ticket/transition` with valid /
  rejected status names; `/ticket/remotelinks` read happy path +
  4xx for non-allowlisted projects; `validate_jira_api_path` allow
  rule for the new GET path; loopback + shared-secret rejection
  semantics.
- **Integration (local-pipeline, kubectl-gated)**: tests live under
  `integration_tests/epic_pipeline/` with a `conftest.py` that imports
  `egg_stack` from the parent (and reaches the gateway URL via
  `egg_stack.gateway_url`, **not** a non-existent `gateway_url`
  fixture). The k3s test stack runs the new `stub-jira` Flask
  container with `JIRA_BASE_URL` overridden on the gateway pod
  (TASK-1-7a). End-to-end `submit_task` against the stub: fresh-epic
  path produces refine HITL → apply (epic Description write) → plan
  HITL → apply (children create + links + Won't-Do batch); reassess
  path against a seeded epic with Done / In-flight / Updatable
  children verifies classification, in-flight refusal, and the
  REVIEWER_CONTRACT apply-phase ACK on contract-state convergence.
- **Manual verification (operator)**: kick off `submit_task
  jira_ticket="<EPIC>"` from the host Claude session, walk the HITL
  surfaces, observe the epic Description write, child create, link
  creation, and Won't-Do transition in the Jira UI. Manual step
  documented in `pr.test_plan`.

## Manual pre-merge / post-merge steps

- **Pre-merge**: ensure `config/context-filters.yaml` lists the
  Atlassian projects the operator wants the epic pipeline to write
  to, and that `epic_link_field` is set per project where the
  default `parent` is wrong (classic projects need
  `customfield_10014`).
- **Pre-merge**: set the orchestrator-only shared-secret token for
  the `/transition` route in the gateway secret bundle (operator
  rotates the existing Atlassian secret bundle to add the new
  loopback token).
- **Post-merge**: re-deploy gateway + orchestrator together — the new
  `/transition` and `/remotelinks` routes need both ends in sync.
- **Post-merge**: run `submit_task` against a low-risk seed epic in a
  test project to confirm end-to-end behaviour before exercising
  against production Atlassian projects.

## Out of scope (deferred follow-ups)

- **Confluence-page enrichment of refine inputs** (Q6 nice-to-have,
  decision-9). Scope deliberately deferred — the operator can paste
  Confluence URLs into `submit_task description` if context is
  needed. A follow-up issue can wire the URL-scan + Confluence read
  call.
- **PR ↔ Jira remote-link write companion** (Q3 / Q6 nice-to-have).
  Q6 marks the read path as MUST (covered by TASK-2-3) and the write
  path as NICE. Defer to a follow-up; the implement phase of each
  child pipeline can stamp the remote-link via the existing gateway
  ticket-create / edit + a future `POST /api/v1/jira/ticket/
  remotelinks/create` route.
- **Cross-project epic decomposition** (decision-12 baseline).
  Deferred — only same-project children are visible to the reassess
  sweep. Cross-project epics are unusual; if needed, loosen the JQL
  extractor in a follow-up.
- **Multi-Atlassian-site posture** (Q4). Single-site MVP. The
  `gateway/jira_policy.py` allowlist already implies single-site;
  defer multi-site indirection to a future issue.

## Yaml-tasks appendix

```yaml
# yaml-tasks
pr:
  title: "Add SDLC pipeline support for Jira epics (#1557)"
  description: |
    ## Context

    Today `submit_task <TICKET>` runs the egg refine → plan pipeline
    against a Jira ticket and produces one PR per ticket. A Jira
    **epic** is a different shape of work: a multi-ticket container
    that should fan out into N child tickets, each becoming its own
    downstream implement pipeline. This PR teaches the orchestrator
    to recognise epics, run the same refine → plan agents against
    them with mode-aware prompts, and apply the resulting Jira
    mutations (epic Description write, child create / edit /
    Won't-Do, issue links) on HITL approval. It also adds the
    reassess path so an epic that already has children classifies
    them (Done / In-flight / Updatable) instead of re-creating
    equivalent work.

    ## Changes

    1. **Epic detection at `submit_task` time** — pre-fetch the
       ticket's `issuetype` via the gateway, persist `is_epic` and
       `pipeline_mode` on the Pipeline model, and inject
       `EGG_PIPELINE_MODE` / `EGG_IS_EPIC` into the sandbox so the
       refiner / task-planner prompts know which mode to use. New
       `mode` arg on `submit_task` ('auto' / 'fresh' / 'reassess')
       lets the operator override the detector.
    2. **Mode-parameterised refiner / task-planner prompts** — both
       prompts get a `mode` block so the same file covers ticket,
       github_issue, epic-fresh, and epic-reassess shapes. Epic
       prompts produce ticket-shaped task descriptions
       (Problem / Scope / Acceptance / OOS / Links) ready for direct
       paste into a Jira body.
    3. **Per-task Jira mapping on the contract** — `Task` gets
       optional `jira_key` and `jira_action`
       ('create' / 'edit' / 'wontdo' / 'split-of' / 'consolidate-into')
       fields; the plan parser extracts them from the YAML appendix.
       The applier walks this mapping to drive idempotent re-runs.
    4. **New APPLIER agent role + apply phase + REVIEWER_CONTRACT
       apply-phase reviewer** — `PipelinePhase.APPLY` joins the
       enum; `VALID_TRANSITIONS` gains `PLAN -> APPLY` and
       `APPLY -> IMPLEMENT` gated on `Pipeline.is_epic`. The
       orchestrator schedules an apply phase after every
       epic-mode HITL approval (refine and plan). The applier
       reads the contract + drafts and calls the existing jira
       sandbox CLI for create / edit / link mutations;
       REVIEWER_CONTRACT ACKs on contract-state convergence
       (every `jira_action='create'` Task has a `jira_key`,
       every Task's `jira_action_status` reached
       `'applied'` or `'failed'`, no in-flight child mutated
       without `in-flight-confirmed`). `Task` gains a
       `jira_action_status` lifecycle field so the applier can
       record per-call progress and idempotently recover from
       partial-apply failures.
    5. **Reassess sweep** — orchestrator helper queries existing
       children (`project = <P> AND parent = <K>`) via the gateway
       JQL search; classifies each via `statusCategory.key`; feeds
       Updatable + In-flight + net-new context into the planner
       prompt; excludes Done children entirely (decision-5).
    6. **In-flight detection** — orchestrator reverse-index
       `jira_ticket → [pipelines]` (with `Pipeline.pr_url`
       persisted on PR-open) plus a new read-only gateway route
       `POST /api/v1/jira/ticket/remotelinks` so human-opened PRs
       still get caught.
    7. **Won't-Do transitions** — new gateway route `POST
       /api/v1/jira/ticket/transition`, orchestrator-only
       (loopback + shared-secret token), allowlisted to
       `Won't Do` / `Won't Fix`. Agent-facing Jira routes still
       deny transitions; the orchestrator-only route preserves the
       "creds only in gateway" invariant.
    8. **Tests** — unit + integration coverage for every new path
       (model serialisation, plan-parser extraction, role registry,
       gateway route allowlists, applier mutation flow,
       in-flight classifier, reassess JQL, idempotency).

    ## Impact

    - Operators get a one-call `submit_task jira_ticket="<EPIC>"`
      surface for both fresh and reassessed epics. The host Claude
      session walks the same draft + decision HITL surface used
      today for tickets — no new UI.
    - The egg pipeline can now mutate Jira state (Description writes,
      child tickets, links, Won't-Do transitions) on HITL approval.
      All mutations stay behind the gateway audit + idempotency
      cache; the only orchestrator-side credential addition is the
      new shared-secret loopback token for the transition route.
    - Implement-phase pipelines for individual child tickets
      continue to work unchanged — each child runs `submit_task
      <CHILD-KEY>` exactly as today, with #2137's slice-DAG
      stacking applying inside each child as needed.
  test_plan: |
    Automated:
    - `make test` covers unit suites for the new Pipeline / Task
      fields, plan-parser extraction of `jira_key` / `jira_action`,
      APPLIER role registration, in-flight classifier, reassess JQL
      shape, gateway `/transition` allowlist, gateway `/remotelinks`
      read, and applier mutation idempotency.
    - `make test-integration` (kubectl-gated) exercises the
      end-to-end `submit_task` flow against a scripted-Jira fake
      under `integration_tests/`. Cover both fresh and reassess
      paths; assert epic Description write, child create + link,
      Won't-Do batch transition, and in-flight refusal.

    Manual:
    - From the host Claude session, run `submit_task
      jira_ticket="<EPIC-KEY>" mode="auto"` against a low-risk seed
      epic in a test Atlassian project. Walk the refine HITL gate;
      confirm the applier writes the analysis to the epic
      Description (visible in the Jira UI). Walk the plan HITL
      gate; confirm the applier creates child tickets, links them
      with `Blocks` / `Relates`, and (if any obsolete children
      present) transitions them to `Won't Do` with a comment
      pointing at the survivor.
    - Re-run `submit_task jira_ticket="<EPIC-KEY>-v2" mode="auto"`
      after seeding a Done child + an In-flight child + an
      Updatable child + an obsolete child; confirm classification
      diff in the plan draft, confirm Done child is omitted from
      the plan, confirm in-flight child is not mutated without an
      explicit per-ticket HITL.
    - Verify `submit_task <CHILD-KEY>` against any created child
      still works — the implement phase of a child pipeline is
      unchanged.
  manual_steps: |
    Pre-merge:
    - Update `config/context-filters.yaml` `jira.projects` to list
      the Atlassian project keys the epic pipeline may write to.
    - Set `jira.epic_link_field` per project for any classic /
      team-managed project where the default `parent` is wrong
      (classic projects need `customfield_10014`).
    - Add the orchestrator-only shared-secret token for the
      `/transition` route to the gateway secret bundle (rotate the
      existing Atlassian secret bundle).
    - The orchestrator and gateway must be redeployed together;
      stage the rollout so both new routes (`/transition` +
      `/remotelinks`) land in lockstep.

    Post-merge:
    - Run a smoke test: `submit_task jira_ticket="<TEST-EPIC>"
      mode="auto"` against a seeded test epic in the test
      Atlassian project. Confirm the refine + plan HITL gates and
      the applier outcomes.
    - Watch the gateway audit log for the first production
      `/transition` invocations to confirm the loopback +
      shared-secret check denies non-orchestrator callers.
slices:
  - id: 1
    name: |-
      Fresh-epic path end-to-end (A+B+C+D)
    goal: |-
      `submit_task` on an epic with no children produces refine →
      HITL → apply (epic Description write) → plan → HITL → apply
      (child create + link). Per decision-1 option C this slice has
      no DAG parent.
    tasks:
      - id: TASK-1-1
        description: |-
          **Epic detection + pipeline-context plumbing + loader-side
          mode-block strip (part A).**
          Add a `mode` argument to the `submit_task` MCP tool
          schema (`orchestrator/mcp_tools.py:67-127`) and handler
          (`orchestrator/mcp_tools.py:1272-1381`) accepting `'auto'
          | 'fresh' | 'reassess'`, defaulting to `'auto'`
          (feedback Q5). Add `Pipeline.is_epic: bool = False` and
          `Pipeline.pipeline_mode: Literal['fresh','reassess'] |
          None = None` fields next to `Pipeline.jira_ticket`
          (`orchestrator/models.py:981-1004`). Add an orchestrator
          helper `is_epic_for_ticket(ticket: str) -> tuple[bool,
          dict]` that calls the gateway `POST
          /api/v1/jira/ticket/get` (`gateway/gateway.py:4929-5009`)
          with `fields=['issuetype','status','description',
          'summary','parent']`, returns `(issuetype.name ==
          'Epic', payload)`. Wire `_handle_submit_task` and
          `state_store.create_pipeline`
          (`orchestrator/state_store.py:972-992`) to set `is_epic`
          + `pipeline_mode`: when `mode='auto'` and `is_epic`,
          probe for existing children (cheap `POST
          /api/v1/jira/search` with `project = <P> AND parent =
          <K>` LIMIT 1) and pick `'reassess'` if any exist,
          `'fresh'` otherwise. Inject `EGG_PIPELINE_MODE` and
          `EGG_IS_EPIC` env vars next to `EGG_JIRA_TICKET`
          (`orchestrator/routes/pipelines.py:19390-19404`)
          following the canonical mapping rule:
          `is_epic=True + pipeline_mode='fresh' → 'epic-fresh'`;
          `is_epic=True + pipeline_mode='reassess' → 'epic-reassess'`;
          `is_epic=False + jira_ticket is not None → 'ticket'`;
          else `'github_issue'`. Validation: `mode='reassess'` is
          rejected when `is_epic=False`; `mode='fresh'` against an
          epic that already has children logs a warning but
          proceeds. Add a loader-side mode-block strip helper
          (e.g. `prep_mode_aware_prompt(prompt_text, mode)` in
          `orchestrator/prompt_loader.py` — new module) that
          regex-strips fenced `## [mode: X]` blocks from the
          refiner / task-planner / applier prompt files when `X`
          does not match the active mode, BEFORE the prompt is
          passed to the agent runner. Risk_analyst R10 mitigation:
          the agent never sees competing mode branches in-context,
          so the pattern is robust across model upgrades. Wire this
          helper into the existing prompt-loading code path in
          `orchestrator/routes/pipelines.py` so every spawned agent
          gets a stripped prompt.
        acceptance: |-
          - `submit_task` accepts `mode` arg; bad values 400.
          - `Pipeline.is_epic` and `Pipeline.pipeline_mode`
            persisted; round-trip through `state_store` preserves
            them.
          - On a mocked Jira `issuetype.name == 'Epic'` the
            handler stores `is_epic=True`; on `'Story'` it stays
            `False`.
          - `mode='auto'` resolves to `'fresh'` when the children
            JQL returns 0 hits and `'reassess'` when it returns
            ≥1.
          - Sandbox spawn includes `EGG_PIPELINE_MODE` and
            `EGG_IS_EPIC` populated per the canonical mapping
            rule above; existing `EGG_JIRA_TICKET` /
            `EGG_JIRA_PROJECT` injection unchanged.
          - `prep_mode_aware_prompt(prompt_text,
            'epic-fresh')` returns the prompt with all
            `## [mode: epic-reassess|ticket|github_issue]` blocks
            removed; the `## [mode: epic-fresh]` block is
            preserved verbatim. Round-trips to other modes
            symmetrically.
          - Unit tests in `orchestrator/tests/test_mcp_tools.py`,
            `orchestrator/tests/test_models.py`, and
            `orchestrator/tests/test_prompt_loader.py` cover all
            branches and the strip helper's corner cases (no
            fenced blocks → unchanged; nested fenced blocks
            preserved; malformed `## [mode: …]` headers left
            in place).
        role: coder
        files:
          - orchestrator/mcp_tools.py
          - orchestrator/models.py
          - orchestrator/state_store.py
          - orchestrator/routes/pipelines.py
          - orchestrator/prompt_loader.py
      - id: TASK-1-2
        description: |-
          **Mode-parameterised refiner + task-planner prompts (part
          B fresh-mode, part C fresh-mode).** Update
          `plugins/refine-plan/skills/refine-plan/agents/refiner.md`
          and `plugins/refine-plan/skills/refine-plan/agents/
          task-planner.md` with a top-of-file `mode` switch
          (`mode: 'ticket' | 'github_issue' | 'epic-fresh' |
          'epic-reassess'`, sourced from the `EGG_PIPELINE_MODE`
          env). For `epic-fresh`: refiner produces a self-contained
          epic problem statement + scope (the analysis becomes the
          epic Description body); task-planner produces every
          `description:` field as a Jira-ticket-shaped body with
          required sections `## Problem`, `## Scope`,
          `## Acceptance`, `## Out of Scope`, `## Links`. Reassess
          mode is left as a stub block (filled in by TASK-2-5).
          Cross-references to the new `EGG_IS_EPIC` env and
          example output skeletons must be inline so the agent has
          no need to grep.
        acceptance: |-
          - Both prompt files include the mode switch and the
            `epic-fresh` branch with the section template.
          - `epic-fresh` task-planner output documented as
            requiring all five `## …` sections per task.
          - Diff also adds a one-line note that `epic-reassess`
            details land in slice 2.
          - No coder file edits in this task.
        role: documenter
        files:
          - plugins/refine-plan/skills/refine-plan/agents/refiner.md
          - plugins/refine-plan/skills/refine-plan/agents/task-planner.md
      - id: TASK-1-3
        description: |-
          **Plan-parser + Task model schema for ticket mapping +
          apply lifecycle (part C + risk_analyst R7).** Extend
          `Task` (`shared/egg_contracts/models.py:182-242`) with
          three optional fields:
          - `jira_key: str | None = None` (regex
            `^[A-Z][A-Z0-9_]*-[0-9]+$`).
          - `jira_action: Literal['create','edit','wontdo',
            'split-of','consolidate-into'] | None = None`.
          - `jira_action_status: Literal['pending','in_flight',
            'applied','failed'] | None = None` — durable apply
            lifecycle. The applier writes `'in_flight'` to the
            contract before each gateway call and
            `'applied'` (or `'failed'` with reason in
            `Task.notes`) after; on re-run, the applier skips
            tasks where `jira_action_status == 'applied'` and
            re-attempts `{'pending','failed'}`. Without this
            field, idempotent re-run can only handle the
            `'create' + jira_key already populated` case; this
            extends it to edit / link / wontdo too.
          Update the YAML-task parser
          (`shared/egg_contracts/plan_parser.py:359-413`) to
          extract `jira_key`, `jira_action`, and
          `jira_action_status` from each task block and propagate
          them into the parsed `Task` object. `parse_plan`
          (`shared/egg_contracts/plan_parser.py:1065`) already
          delegates to the per-task helper; verify the keys
          survive end-to-end. Reject `jira_action` /
          `jira_action_status` values not in the literal
          allow-set with a `ParseWarning`.
        acceptance: |-
          - `Task(...)` accepts the three new fields and
            round-trips through the contract JSON serialiser.
          - `parse_yaml_code_fence` + `parse_tasks_from_yaml`
            lift `jira_key`, `jira_action`, and
            `jira_action_status` from a fixture YAML.
          - Non-literal `jira_action` or `jira_action_status`
            produces a warning, not a silent drop.
          - Default value of `jira_action_status` is `None`
            (treated as `'pending'` by the applier); explicit
            `'pending'` round-trips identically.
          - Unit tests in
            `shared/egg_contracts/tests/test_models.py` and
            `shared/egg_contracts/tests/test_plan_parser.py`
            cover the new fields end-to-end including the apply
            lifecycle status transitions.
        role: coder
        files:
          - shared/egg_contracts/models.py
          - shared/egg_contracts/plan_parser.py
      - id: TASK-1-4
        description: |-
          **APPLIER role + apply phase enum + apply-phase
          scheduling (part D).** Cross-cuts three layers:

          1. **Phase enum + transitions** — Add
             `PipelinePhase.APPLY = "apply"` to the
             `PipelinePhase` enum at
             `shared/egg_contracts/models.py:62-68` so the
             orchestrator can represent the new phase in
             `Pipeline.current_phase`. Extend
             `VALID_TRANSITIONS` at
             `gateway/phase_transition.py:41-47` with
             `VALID_TRANSITIONS[PLAN] = [APPLY, IMPLEMENT]`
             and `VALID_TRANSITIONS[APPLY] = [IMPLEMENT]`.
             Both edges are gated on `Pipeline.is_epic` in the
             orchestrator-side scheduler (TASK-1-4 step 3) —
             non-epic pipelines continue to advance directly
             from PLAN to IMPLEMENT.

          2. **Role registration** — Add
             `AgentRole.APPLIER = "applier"` to the `AgentRole`
             enum (`shared/egg_contracts/agent_roles.py:46-90`).
             Define `APPLIER_ROLE` `AgentRoleDefinition` next to
             the other analysis roles (~line 380); register it
             in `AGENT_ROLES`
             (`shared/egg_contracts/agent_roles.py:894-912`).
             Add an `"apply"` entry to `_PHASE_ROLES`
             (`shared/egg_contracts/agent_roles.py:1107-1112`)
             with `[AgentRole.APPLIER]`. Add an `"apply"` entry
             to `_PHASE_REVIEWERS`
             (`shared/egg_contracts/agent_roles.py:1113-1130`)
             with `[AgentRole.REVIEWER_CONTRACT]` per the
             architect's slice-3 design + risk_analyst R1
             mitigation: REVIEWER_CONTRACT ACKs on
             contract-state convergence (every Task with
             `jira_action='create'` has a non-null `jira_key`
             matching `^[A-Z][A-Z0-9_]*-[0-9]+$`; every Task
             has `jira_action_status` in
             `{'applied','failed'}`; no in-flight child
             mutated without the `in-flight-confirmed` marker).

          3. **File-write restrictions** — Define
             `APPLIER_PATTERNS` in
             `shared/egg_restrictions/patterns.py` (allowed:
             `.egg-state/agent-outputs/`; blocked: same
             blocklist as `_PLAN_AGENT_BLOCKED` extended with
             `src/`, `gateway/`, `sandbox/`, `shared/`,
             `orchestrator/`, `plugins/`).

          4. **Scheduler wiring** — Wire the orchestrator phase
             scheduler in `orchestrator/routes/pipelines.py`
             so that on `pipeline.is_epic`, after a HITL
             phase_gate resolution=approve flips state via
             `_persist_phase_gate_resolution`
             (`orchestrator/routes/pipelines.py:18274+`), the
             scheduler advances `Pipeline.current_phase` to
             `APPLY` and spawns the applier pod (plus
             REVIEWER_CONTRACT for consensus). The apply phase
             reads the contract + relevant draft (analysis for
             refine-apply, plan + per-Task `jira_key` /
             `jira_action` / `jira_action_status` for
             plan-apply) and terminates when REVIEWER_CONTRACT
             ACKs the producer's CONSENSUS_PROPOSE.
        acceptance: |-
          - `PipelinePhase.APPLY` exists and round-trips through
            `Pipeline.current_phase`.
          - `VALID_TRANSITIONS[PLAN]` includes `APPLY` and
            `VALID_TRANSITIONS[APPLY] = [IMPLEMENT]`; non-epic
            pipelines still advance PLAN → IMPLEMENT
            unchanged because the scheduler skips APPLY when
            `Pipeline.is_epic == False`.
          - `AgentRole.APPLIER` exists; `AGENT_ROLES[APPLIER]`
            is populated.
          - `get_roles_for_phase('apply')` returns `[APPLIER,
            REVIEWER_CONTRACT]` (single producer + single
            reviewer).
          - `APPLIER_PATTERNS` registered in
            `shared/egg_restrictions/patterns.py` and surfaces
            via the existing role↔patterns lookup.
          - On an epic-mode pipeline, the orchestrator
            schedules an apply phase after every refine + plan
            HITL approval; on non-epic pipelines no apply phase
            is scheduled.
          - The apply phase terminates after the
            REVIEWER_CONTRACT ACK lands (per the existing BRC
            consensus flow).
          - Unit tests cover the scheduling decision in both
            `is_epic=True` and `is_epic=False` cases plus the
            VALID_TRANSITIONS edge additions.
        role: coder
        files:
          - shared/egg_contracts/agent_roles.py
          - shared/egg_contracts/models.py
          - shared/egg_restrictions/patterns.py
          - gateway/phase_transition.py
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: |-
          **Applier prompt + reviewer-contract apply-phase
          supplement.** Author two new prompt files:

          1. `plugins/refine-plan/skills/refine-plan/agents/
             applier.md` describing the applier's job: read the
             current phase context (`EGG_PIPELINE_MODE`, the
             just-approved phase, the contract path, the draft
             path); for refine-apply, write the analysis to the
             epic Description via `jira ticket edit
             "$EGG_JIRA_TICKET" --description-file <path>`; for
             plan-apply, walk `Task.jira_key`,
             `Task.jira_action`, and `Task.jira_action_status`
             and call the appropriate jira CLI subcommand
             (`sandbox/scripts/jira ticket create|edit|link
             create`). The prompt must specify the
             apply-lifecycle invariant (risk_analyst R7):
             before each gateway call, write
             `jira_action_status='in_flight'` to the contract
             via `mcp__task__update_notes` (or a future
             `mcp__task__set_status` MCP); after each call,
             write `'applied'` or `'failed'` (with reason in
             `Task.notes`). On re-run, skip tasks where status
             is `'applied'`; re-attempt tasks where status is
             in `{'pending', None, 'failed'}`. Reject unknown
             `jira_action` values with a structured failure that
             bubbles up via `mcp__progress__signal_error`. Note
             that Won't-Do transitions are NOT in the applier's
             purview (they live in slice 2's orchestrator-only
             route, drained from a handoff JSON the applier
             produces).

          2. `plugins/refine-plan/skills/refine-plan/agents/
             reviewer-contract-apply.md` (or an `[mode:
             apply]` block in the existing
             reviewer-contract.md, mirroring decision-16 for
             prompts) describing the apply-phase reviewer-side
             checks: (i) every Task with `jira_action='create'`
             has a non-null `jira_key` matching
             `^[A-Z][A-Z0-9_]*-[0-9]+$`; (ii) every Task in
             scope has `jira_action_status` in
             `{'applied','failed'}` (no leftover `'pending'`
             or `'in_flight'`); (iii) for any Task with
             `jira_action_status='failed'`, the failure
             reason is recorded in `Task.notes`; (iv) no Task
             whose `jira_key` belongs to an in-flight child
             was mutated without `Task.notes` containing
             `in-flight-confirmed`. The reviewer ACKs on
             contract-state convergence, NOT on prompt-output
             text quality (risk_analyst R1 mitigation).
        acceptance: |-
          - `applier.md` exists and names every CLI subcommand
            the applier may use; references the existing
            `gateway/jira_idempotency.py:66` 5-min cache;
            calls out the `jira_action_status`
            write-before-call invariant.
          - `reviewer-contract-apply.md` (or the
            `[mode: apply]` block in `reviewer-contract.md`)
            exists and enumerates all four convergence checks
            with the specific regex / state values the
            reviewer evaluates.
          - Both prompts document the APPLIER /
            REVIEWER_CONTRACT roles' file-write boundaries.
        role: documenter
        files:
          - plugins/refine-plan/skills/refine-plan/agents/applier.md
          - plugins/refine-plan/skills/refine-plan/agents/reviewer-contract-apply.md
      - id: TASK-1-6
        description: |-
          **Per-project `epic_link_field` test coverage.** The
          dispatch from the `epicLink` shorthand to either
          `parent` or `customfield_10014` is **already wired**
          today via `JiraPolicy.epic_link_field()`
          (`gateway/jira_policy.py:163`); the ticket-create
          route at `gateway/gateway.py:5358, 5413, 5594,
          5697-5748` already calls it. Verified at HEAD: `grep
          -n "epic_link_field\|epicLink" gateway/gateway.py`
          shows imports at lines 162, 307 and dispatch use in
          the create route. This task therefore adds **test
          coverage only** — no production-code changes — for
          both `epic_link_field='parent'` and
          `epic_link_field='customfield_10014'` translation
          paths so the operator-managed setting is exercised
          before relying on it for child-ticket creation.
        acceptance: |-
          - Test fixtures in
            `gateway/tests/test_jira_routes.py` exercise the
            ticket-create route with `epic_link_field='parent'`
            (default; emits `parent: <KEY>`) and
            `epic_link_field='customfield_10014'` (emits
            `fields: {'customfield_10014': '<KEY>'}` payload).
          - No production-code changes in `gateway/gateway.py`
            or `gateway/jira_policy.py` unless a test reveals
            an actual gap.
        role: tester
        files:
          - gateway/tests/test_jira_routes.py
      - id: TASK-1-7
        description: |-
          **Stub-Jira fake + k3s deployment (test infrastructure
          for TASK-1-8 / TASK-2-9).** Per architect's
          `open_questions_for_reviewer_plan` #2, build an
          in-process Flask fake at
          `integration_tests/fixtures/stub_jira.py` (writable by
          tester per `TESTER_PATTERNS`
          `shared/egg_restrictions/patterns.py:185-227`)
          implementing the Atlassian routes the applier + sweep
          + transition + remote-link surfaces hit:
          - `GET /rest/api/3/issue/{KEY}` (returns the seeded
            ticket payload including `issuetype`, `status`,
            `statusCategory`, `description`, `parent`).
          - `POST /rest/api/3/issue` (createJiraIssue; assigns a
            new key in the configured project, persists in
            in-memory store).
          - `PUT /rest/api/3/issue/{KEY}` (editJiraIssue;
            mutates description / summary / parent).
          - `POST /rest/api/3/issueLink` (createIssueLink;
            persists link records).
          - `POST /rest/api/3/issue/{KEY}/transitions`
            (transitions; allowlisted to `Won't Do` / `Won't
            Fix` for slice-2 testing).
          - `GET /rest/api/3/issue/{KEY}/remotelink` (returns
            the seeded remote-link list for slice-2 in-flight
            detection).
          - `POST /rest/api/3/search` (JQL search; honours the
            `project = X AND parent = K` shape used by the
            reassess sweep).
          A test helper `seed_epic(stub, key, children=...)`
          populates the in-memory store. Add a `stub-jira`
          container to the k3s test stack (the existing
          `_k8s_egg_stack` in `integration_tests/conftest.py:166`
          gains a sibling deployment); the gateway pod's
          `JIRA_BASE_URL` env var is overridden to point at the
          stub's cluster service. Document the fixture's surface
          in `integration_tests/fixtures/README.md` (NEW).
        acceptance: |-
          - `integration_tests/fixtures/stub_jira.py` runs
            standalone via `python -m
            integration_tests.fixtures.stub_jira` and serves
            all enumerated routes.
          - The k3s test stack spawns a `stub-jira` deployment
            and the gateway pod uses `JIRA_BASE_URL`
            override to reach it.
          - Round-trip test: `seed_epic` + create child + link
            + transition + read-back → consistent state.
          - Unit tests in
            `integration_tests/fixtures/tests/test_stub_jira.py`
            (new) cover each route.
        role: tester
        files:
          - integration_tests/fixtures/stub_jira.py
          - integration_tests/fixtures/tests/test_stub_jira.py
          - integration_tests/conftest.py
      - id: TASK-1-8
        description: |-
          **Slice-1 unit + integration test coverage.** Tests for
          TASK-1-1 (epic detection, env injection,
          mode-aware-prompt strip helper), TASK-1-3 (plan-parser
          + Task model fields including `jira_action_status`),
          TASK-1-4 (PipelinePhase.APPLY enum,
          VALID_TRANSITIONS, APPLIER role registry +
          REVIEWER_CONTRACT apply-phase reviewer + scheduling
          decision). Integration tests under a new directory
          `integration_tests/epic_pipeline/` (with its own
          `conftest.py` that imports `egg_stack` from the
          parent — kubectl-gated end-to-end tier; tests reach
          the gateway URL via `egg_stack.gateway_url`, NOT via
          a non-existent `gateway_url` fixture; see
          `docs/architecture/integration-test-trust-boundary.md`)
          covering an epic-fresh pipeline end-to-end against
          the stub-jira fake from TASK-1-7: assert the
          applier sends `editJiraIssue` for the epic
          Description and `createJiraIssue` + `createIssueLink`
          for each planned child; assert
          `Task.jira_action_status` is `'applied'` on each
          completed task; assert REVIEWER_CONTRACT ACKs the
          apply-phase consensus on contract-state convergence.
          Re-run the same pipeline twice and verify second-pass
          apply is a no-op (idempotency: tasks with status
          `'applied'` are skipped).
        acceptance: |-
          - `make test` passes on the new orchestrator + shared
            + gateway suites.
          - `make test-integration` (kubectl-gated) passes the
            new fresh-epic end-to-end flow under
            `integration_tests/epic_pipeline/`.
          - Idempotent re-run produces zero new gateway writes
            on the second pass (every Task already has status
            `'applied'`).
          - REVIEWER_CONTRACT successfully ACKs the apply-phase
            BRC consensus when contract state converges; NACKs
            when a Task with `jira_action='create'` is missing
            `jira_key`.
        role: tester
        files:
          - orchestrator/tests/test_mcp_tools.py
          - orchestrator/tests/test_models.py
          - orchestrator/tests/test_prompt_loader.py
          - shared/egg_contracts/tests/test_models.py
          - shared/egg_contracts/tests/test_plan_parser.py
          - shared/egg_contracts/tests/test_agent_roles.py
          - gateway/tests/test_phase_transition.py
          - integration_tests/epic_pipeline/conftest.py
          - integration_tests/epic_pipeline/test_epic_fresh_path.py
  - id: 2
    name: |-
      Reassess path (E+F+G)
    goal: |-
      `submit_task` on an epic with pre-existing children classifies
      Done / In-flight / Updatable, the planner consolidates / splits
      / leaves-alone correctly, the applier honors in-flight markers,
      and obsolete children transition to Won't Do via the
      orchestrator-only gateway route. Per decision-1 option C this
      slice depends on slice 1.
    dependencies:
      - slice-1
    tasks:
      - id: TASK-2-1
        description: |-
          **Reassess sweep helper (part E).** Add a helper in
          `orchestrator/` (new module e.g.
          `orchestrator/jira_reassess.py`) that, given an epic key
          and project, calls the gateway `POST /api/v1/jira/search`
          (`gateway/gateway.py:5012-5133`) with JQL `project = <P>
          AND parent = <KEY>` (decision-12 — same-project only;
          conformant with `gateway/jira_search.py:55-128`'s
          extractor), fetches each child's `summary`, `status`,
          `statusCategory`, `description`, and classifies each as:
          - `done` if `statusCategory.key == 'done'` (decision-13)
          - `in_flight` if `statusCategory.key == 'indeterminate'`
            OR the child has an open PR (TASK-2-4)
          - `updatable` otherwise
          Returns a structured `ReassessSweepResult` with one entry
          per child. Wire the orchestrator to call this helper
          when `pipeline.pipeline_mode == 'reassess'` and inject
          the serialised result into the sandbox env as
          `EGG_REASSESS_SWEEP_PATH` (a path to a JSON file in
          `.egg-state/agent-outputs/`); Done children are written
          to a separate `EGG_DONE_CHILDREN_PATH` file with summary
          + key only (decision-5: excluded from prompt body but
          kept as provenance).
        acceptance: |-
          - Helper unit-tested against a mocked gateway response
            covering all three classes.
          - JQL passes `gateway/jira_search.py` extractor (verify
            with a unit test that the produced query parses).
          - Wiring in `orchestrator/routes/pipelines.py` only fires
            on `pipeline_mode == 'reassess'`.
          - Sweep result + Done-children handoff files land in
            `.egg-state/agent-outputs/` and the env vars point at
            them.
        role: coder
        files:
          - orchestrator/jira_reassess.py
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: |-
          **Pipeline reverse-index + pr_url persistence (part F
          signal a).** Add `Pipeline.pr_url: str | None = None`
          field next to `Pipeline.pr_number`
          (`orchestrator/models.py:860-864`). Persist it whenever
          the implement-phase opens a PR (find the existing PR-open
          site that already sets `pr_number`; `grep` for `pr_number =`
          assignments under `orchestrator/routes/pipelines.py`).
          Add a state-store API
          `state_store.pipelines_for_jira_ticket(ticket: str) ->
          list[Pipeline]` (in `orchestrator/state_store.py`) that
          scans the indexed pipelines and returns those whose
          `jira_ticket == ticket`. Implementation may be a
          straight in-memory filter against the pipeline cache
          plus a per-ticket secondary index for O(1) lookup if
          performance demands it. Document the index in the
          state-store docstring.
        acceptance: |-
          - `Pipeline.pr_url` round-trips through state_store.
          - `state_store.pipelines_for_jira_ticket('ENG-1')`
            returns every pipeline with that ticket; returns
            `[]` for unknown tickets.
          - PR-open code path now sets `pr_url` alongside the
            existing `pr_number` write.
          - Unit tests in `orchestrator/tests/test_models.py` and
            `orchestrator/tests/test_state_store.py` cover both
            paths.
        role: coder
        files:
          - orchestrator/models.py
          - orchestrator/state_store.py
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: |-
          **Read-only `/remotelinks` gateway route (part F signal b
          + decision-9 dependency).** Add `POST /api/v1/jira/ticket/
          remotelinks` to `gateway/gateway.py` returning the
          Atlassian `GET /rest/api/3/issue/{key}/remotelink`
          payload, gated on `@require_private_mode` and the
          existing project allowlist (mirror the auth + audit shape
          of `POST /api/v1/jira/ticket/get` at `gateway/gateway.py:
          4929-5009`). Update `validate_jira_api_path`
          (`gateway/jira_client.py:217-283`) to allow `GET
          /rest/api/3/issue/<KEY>/remotelink`. Confirm
          `JIRA_WRITE_VERBS_DENIED` (`gateway/jira_client.py:133`)
          is unaffected (read verb only). Add a `jira ticket
          remotelinks <KEY>` subcommand to `sandbox/scripts/jira`.
        acceptance: |-
          - New route returns 200 + remote-link payload for an
            allowlisted project; 403 for a denied project.
          - `validate_jira_api_path` accepts the new GET path; a
            POST/PUT/DELETE on the same path is still denied.
          - Sandbox CLI subcommand exits 0 on a happy-path call
            and surfaces upstream errors.
          - Unit tests in `gateway/tests/test_jira_routes.py`
            cover the route + path validator changes.
        role: coder
        files:
          - gateway/gateway.py
          - gateway/jira_client.py
          - sandbox/scripts/jira
      - id: TASK-2-4
        description: |-
          **In-flight detection helper (part F).** Add an
          orchestrator helper in `orchestrator/jira_reassess.py`
          (created in TASK-2-1) that, given a child key,
          classifies `in_flight` if any of:
          - `statusCategory.key == 'indeterminate'` from the
            ticket-get payload (already fetched in the sweep);
          - `state_store.pipelines_for_jira_ticket(key)` returns
            ≥1 pipeline with non-null `pr_url` and the PR is
            still open (call the existing GitHub-side check); or
          - The new `/remotelinks` route returns ≥1 entry whose
            URL matches `^https?://github\.com/.+/pull/\d+$`.
          Update the sweep classification in TASK-2-1 to call
          this helper. Wire the in-flight signal into the
          `EGG_REASSESS_SWEEP_PATH` JSON so the planner prompt
          can render the `do-not-modify-without-confirmation`
          marker.
        acceptance: |-
          - Helper unit-tested against all three signal sources
            independently and combined.
          - Sweep result includes an `in_flight: bool` per child
            and an `in_flight_evidence: list[str]` enumerating
            which signals fired.
          - Pure-status `in_flight` round-trips even when the
            reverse-index returns empty (humans pause work).
        role: coder
        files:
          - orchestrator/jira_reassess.py
      - id: TASK-2-5
        description: |-
          **Reassess-mode prompt branches (part E).** Fill in the
          `epic-reassess` branch of the refiner and task-planner
          prompts left as stubs by TASK-1-2.
          - `refiner.md (epic-reassess)`: instruct the agent to
            assess what's done (read Done summary list from
            `EGG_DONE_CHILDREN_PATH`), what's changed, what's no
            longer relevant; cite the existing children with their
            keys; produce an analysis the operator can read
            alongside the sweep diff.
          - `task-planner.md (epic-reassess)`: receive the
            Updatable + In-flight + net-new children from the
            sweep; produce plan tasks with `jira_key` populated
            for each pre-existing key (action `'edit'`); produce
            new tasks with `jira_action='create'` for net-new
            work; for consolidation produce one survivor task
            (action `'edit'`) and N obsolete tasks (action
            `'wontdo'`) referencing the survivor; for splits
            produce one narrowed task (action `'edit'`) and N
            new tasks (action `'create'`); refuse to mutate any
            child marked `in_flight` without an explicit per-
            ticket HITL flag (decision-4 + #2289 marker). Surface
            the planner's per-cluster survivor choice + rationale
            in the plan draft so the operator can override
            (decision-6 option C). Append a "Plan diff" section
            naming `updated`, `closed`, `untouched`, `net-new`,
            `consolidated`, `split`, `in_flight` clusters.
        acceptance: |-
          - Both prompts now include filled-in `epic-reassess`
            branches with the rules above.
          - `task-planner.md` documents the survivor-choice
            override flow.
          - `task-planner.md` documents that mutations on
            `in_flight` children require a per-ticket HITL marker.
          - The Plan diff section is reified in the prompt's
            example output.
        role: documenter
        files:
          - plugins/refine-plan/skills/refine-plan/agents/refiner.md
          - plugins/refine-plan/skills/refine-plan/agents/task-planner.md
      - id: TASK-2-6
        description: |-
          **Orchestrator-only `/transition` gateway route (part
          G).** Add `POST /api/v1/jira/ticket/transition` to
          `gateway/gateway.py` accepting `{key, transition_name,
          comment}`. Allowlist `transition_name` to `Won't Do` and
          `Won't Fix` only (decision-15). Auth: require a loopback
          source (request must originate inside the cluster
          network, e.g. caller IP in the orchestrator's k8s
          subnet) AND a shared-secret token (`X-Egg-Orchestrator-
          Token`) compared in constant time against an env-injected
          gateway secret. Add an internal helper to
          `gateway/jira_client.py` that bypasses
          `validate_jira_api_path` for this specific transition
          path (mirror the four existing internal-only methods at
          `gateway/jira_client.py:491+`). On success post the
          configured comment via the existing `addCommentToJiraIssue`
          flow. Audit-log every invocation including caller IP,
          transition name, and ticket key. Do NOT add a sandbox
          CLI subcommand — agents continue to be denied
          transitions.
        acceptance: |-
          - Route exists; non-allowlisted `transition_name` returns
            400.
          - Missing or wrong `X-Egg-Orchestrator-Token` returns 401.
          - Caller from outside the orchestrator subnet returns 403.
          - Successful invocation transitions the ticket and adds
            the comment in a single audit-logged operation.
          - `JIRA_WRITE_VERBS_DENIED` (`gateway/jira_client.py:133`)
            and `validate_jira_api_path` (`:217-283`) remain
            unchanged (transitions still denied for the agent path).
          - Unit tests in `gateway/tests/test_jira_routes.py`
            cover allowlist, auth, audit, and a happy-path
            transition.
        role: coder
        files:
          - gateway/gateway.py
          - gateway/jira_client.py
      - id: TASK-2-7
        description: |-
          **Apply-phase post-consensus Won't-Do batch drain
          (part G + part D extension — orchestrator side).**
          Trigger chain: HITL operator approves the plan-gate →
          `_persist_phase_gate_resolution`
          (`orchestrator/routes/pipelines.py:18274+`) flips the
          decision state and returns the HTTP response → the
          orchestrator phase scheduler (TASK-1-4) advances
          `Pipeline.current_phase` from `PLAN` to `APPLY` and
          spawns the applier pod + REVIEWER_CONTRACT → the
          applier reads `EGG_REASSESS_SWEEP_PATH`, walks
          `Task.jira_key` / `Task.jira_action` /
          `Task.jira_action_status` and either calls the jira
          CLI (for `'edit' / 'create' / 'split-of' /
          'consolidate-into'`) or appends to a Won't-Do handoff
          JSON at `.egg-state/agent-outputs/<pipeline>-wontdo.
          json` (for `'wontdo'`). The applier's CONSENSUS_PROPOSE
          / REVIEWER_CONTRACT ACK flow terminates the apply
          phase. **Only THEN** — in a new
          `_drain_wontdo_batch_after_apply` hook in
          `orchestrator/routes/pipelines.py` triggered by the
          apply-phase CONSENSUS_CONFIRMED — does the
          orchestrator iterate the handoff JSON and call the
          new `/transition` route (TASK-2-6) for each entry.
          The drain runs OUT-of-band from the HITL HTTP
          response so Jira API latency does not block the
          operator's approve POST. Decision-4 batches all
          Won't-Do transitions on the single plan-gate
          approval; per-Task `jira_action_status` flips to
          `'applied'` (or `'failed'` with reason) on each
          transition.
          - Any task whose `jira_key` belongs to an `in_flight`
            child (per the sweep handoff at
            `EGG_REASSESS_SWEEP_PATH`) is **refused by the
            applier** at gateway-call time unless the task
            carries a per-ticket override marker (`Task.notes`
            contains the literal string `in-flight-confirmed`).
            Refused mutations write `jira_action_status='failed'`
            with reason `'in-flight not confirmed'` and skip;
            the operator can re-run after adding the marker
            (the apply phase will re-spawn and pick up the
            new state).
        acceptance: |-
          - The Won't-Do drain runs in
            `_drain_wontdo_batch_after_apply`, NOT inside
            `_persist_phase_gate_resolution` — verified by a
            unit test that asserts the HITL POST returns within
            the existing latency SLA (mocked `/transition`
            with a 5-second sleep does NOT delay the HITL
            response).
          - Won't-Do handoff JSON (produced by the applier) is
            drained by the orchestrator via `/transition` after
            applier consensus; per-Task `jira_action_status`
            flips to `'applied'` after a successful transition.
          - In-flight refusal enforced in the applier at
            gateway-call time; refused tasks surface as
            `jira_action_status='failed'` with reason in
            `Task.notes`.
          - Re-run with `in-flight-confirmed` added to a task's
            notes succeeds for that task only on the next apply
            phase spawn.
          - Unit tests in
            `orchestrator/tests/test_pipelines_apply.py` (new)
            cover routing + in-flight refusal + Won't-Do batch
            drain timing.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-8
        description: |-
          **Applier prompt extension (part D extension — sandbox
          side).** Update the applier prompt at
          `plugins/refine-plan/skills/refine-plan/agents/
          applier.md` (created in TASK-1-5) to document the
          reassess-mode mutation routing the applier performs
          when the plan-apply phase runs on an epic-reassess
          pipeline:
          - `Task.jira_action == 'edit'` → `jira ticket edit`
            on `Task.jira_key`.
          - `Task.jira_action == 'create'` → `jira ticket create`
            (parent set to epic per TASK-1-6).
          - `Task.jira_action == 'consolidate-into'` → record the
            survivor pointer and skip (the survivor task has
            `'edit'` action; the obsolete tasks all have
            `'wontdo'` action).
          - `Task.jira_action == 'split-of'` → record the parent
            split-source pointer (informational only; the parent
            task has `'edit'` action narrowing scope and the new
            tasks have `'create'` action).
          - `Task.jira_action == 'wontdo'` → NOT executed by the
            applier — instead emit a structured handoff JSON to
            `.egg-state/agent-outputs/` listing every Won't-Do
            key + the comment text. The orchestrator (TASK-2-7)
            iterates the list and calls the orchestrator-only
            `/transition` route.
          - In-flight refusal: any task whose `jira_key` belongs
            to an `in_flight` child (per
            `EGG_REASSESS_SWEEP_PATH`) is refused unless
            `Task.notes` contains the literal string
            `in-flight-confirmed`.
        acceptance: |-
          - `applier.md` reassess-mode section documents every
            `jira_action` route + the in-flight refusal rule.
          - The Won't-Do handoff JSON shape is described
            explicitly so the orchestrator knows what to drain.
        role: documenter
        files:
          - plugins/refine-plan/skills/refine-plan/agents/applier.md
      - id: TASK-2-9
        description: |-
          **Slice-2 unit + integration test coverage.** Tests for
          TASK-2-1 (sweep classification), TASK-2-2 (reverse-index
          + pr_url + decision-17 storage shape), TASK-2-3
          (`/remotelinks` route + path validator), TASK-2-4
          (in-flight helper truth table), TASK-2-6
          (`/transition` route allowlist + auth + audit), TASK-2-7
          (apply-phase post-consensus Won't-Do drain + HITL
          response latency invariant + in-flight refusal lifecycle).
          Integration test under
          `integration_tests/epic_pipeline/test_epic_reassess_
          path.py` (kubectl-gated; uses the `egg_stack` fixture
          + `egg_stack.gateway_url` attribute, sharing the
          `conftest.py` introduced by TASK-1-8) against the
          stub-jira fake from TASK-1-7. Seed an epic with
          children covering every classification class (Done /
          In-flight / Updatable / Net-new); assert the applier
          and post-apply orchestrator step produce the right
          edit / create / link / Won't-Do outcomes; assert
          `jira_action_status` lifecycle reaches `'applied'` on
          each task; assert REVIEWER_CONTRACT ACKs the
          contract-state convergence after the second apply
          phase.
        acceptance: |-
          - `make test` passes on the new and updated suites.
          - `make test-integration` passes the new reassess
            end-to-end flow.
          - In-flight refusal exercised by an integration test
            scenario where the planner emits an `'edit'` action
            on an `in_flight` child without the override marker;
            assert `jira_action_status='failed'` and the apply
            phase re-spawns successfully when the operator
            adds `in-flight-confirmed` to `Task.notes`.
        role: tester
        files:
          - orchestrator/tests/test_jira_reassess.py
          - orchestrator/tests/test_models.py
          - orchestrator/tests/test_state_store.py
          - orchestrator/tests/test_pipelines_apply.py
          - gateway/tests/test_jira_routes.py
          - integration_tests/epic_pipeline/test_epic_reassess_path.py
      - id: TASK-2-10
        description: |-
          **Shared-secret lifecycle documentation for the
          orchestrator-only `/transition` route.** Document the
          new `X-Egg-Orchestrator-Token` shared-secret token
          for the `/transition` route added in TASK-2-6:
          generation procedure, mounting on both orchestrator
          and gateway pods (existing Atlassian secret bundle in
          k8s), rotation procedure, and the loopback-source
          requirement. Place the documentation in
          `docs/architecture/orchestrator.md` (or equivalent),
          with a cross-reference from the gateway-side
          deployment notes. Touch only documentation files
          (documenter scope).
        acceptance: |-
          - `docs/architecture/orchestrator.md` documents the
            shared-secret token's purpose, generation,
            mounting, and rotation procedure.
          - The doc cross-references the `/transition` route
            and explains why agent-facing routes still deny
            transitions.
          - No production-code changes.
        role: documenter
        files:
          - docs/architecture/orchestrator.md
```
