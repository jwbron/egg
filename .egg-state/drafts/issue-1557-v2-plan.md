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
  same prompt file covers every shape.
- **Per-task ticket-shaped descriptions** (decision-10). The
  `task-planner.md` epic mode requires every task `description:` to be
  a ticket-ready body with `Problem`, `Scope`, `Acceptance`,
  `Out of Scope`, `Links` sections. Schema is unchanged — the
  description field carries the convention.
- **Applier as a new sandbox role** (decision-8). Spawned after every
  epic-mode HITL approval; reads contract artifacts; calls the jira
  sandbox CLI for create / edit / link mutations. Stays behind the
  existing gateway audit + auth boundary.
- **Contract-stored mapping** (decision-11). `Task` gains optional
  `jira_key` and `jira_action` fields; the applier reads them per
  task and drives idempotent re-runs (feedback Q1) by treating any
  task whose `jira_key` already matches the post-mutation state as a
  no-op. Long-window idempotency lives on the contract; short-window
  (≤5 min) is covered by `gateway/jira_idempotency.py`.
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
| `CODER_PATTERNS` | `shared/egg_restrictions/patterns.py:108-189` | gateway |
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
| `EggStack` / `gateway_url` test fixtures | `integration_tests/conftest.py:71-325` (`gateway_url` at `:325`, `egg_stack` at `:308`) | local-test-only (kubectl-gated) |

### NEW (created by this plan)

| Primitive | Created in | Execution-context scope |
|-----------|-----------|-------------------------|
| `submit_task` `mode` arg ('auto' / 'fresh' / 'reassess') | `(NEW — task TASK-1-1)` | host → orchestrator |
| Orchestrator pre-fetch + `is_epic_for_ticket(...)` helper | `(NEW — task TASK-1-1)` | orchestrator |
| `Pipeline.is_epic` (bool) field | `(NEW — task TASK-1-1)` | orchestrator (Pydantic) |
| `Pipeline.pipeline_mode` ('fresh' / 'reassess' / null) field | `(NEW — task TASK-1-1)` | orchestrator (Pydantic) |
| `Pipeline.pr_url` (str / null) field | `(NEW — task TASK-2-2)` | orchestrator (Pydantic) |
| `EGG_PIPELINE_MODE` / `EGG_IS_EPIC` env vars | `(NEW — task TASK-1-1)` | in-sandbox-agent (set by orchestrator) |
| `Task.jira_key` (str / null) field | `(NEW — task TASK-1-3)` | orchestrator (Pydantic) |
| `Task.jira_action` literal field | `(NEW — task TASK-1-3)` | orchestrator (Pydantic) |
| Plan-parser support for `jira_key` / `jira_action` per-task YAML keys | `(NEW — task TASK-1-3)` | orchestrator |
| `AgentRole.APPLIER` enum value (`"applier"`) | `(NEW — task TASK-1-4)` | orchestrator + in-sandbox-agent |
| `APPLIER_ROLE` `AgentRoleDefinition` registration in `AGENT_ROLES` | `(NEW — task TASK-1-4)` | orchestrator |
| `_PHASE_ROLES["apply"] = [APPLIER]` registration | `(NEW — task TASK-1-4)` | orchestrator |
| `APPLIER_PATTERNS` file-write restriction in `patterns.py` | `(NEW — task TASK-1-4)` | gateway |
| Apply-phase scheduling (orchestrator post-HITL spawn) | `(NEW — task TASK-1-4)` | orchestrator |
| Applier prompt `applier.md` | `(NEW — task TASK-1-5)` | in-sandbox-agent |
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
| Applier extension: in-flight refusal + Won't-Do batch + consolidate / split | `(NEW — task TASK-2-7)` | in-sandbox-agent + orchestrator |

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
- **Integration (local-pipeline)**: end-to-end `submit_task` against a
  scripted-Jira fake — fresh-epic path produces refine HITL → apply
  (epic Description write) → plan HITL → apply (children create +
  links + Won't-Do batch); reassess path against a seeded epic with
  Done / In-flight / Updatable children verifies classification and
  in-flight refusal.
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
    4. **New APPLIER agent role + apply phase** — registered in
       `AgentRole`, `AGENT_ROLES`, `_PHASE_ROLES['apply']`, and
       `patterns.py`. The orchestrator schedules an apply phase
       after every epic-mode HITL approval (refine and plan); the
       applier reads the contract + drafts and calls the existing
       jira sandbox CLI for create / edit / link mutations.
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
          **Epic detection + pipeline-context plumbing (part A).**
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
          (`orchestrator/routes/pipelines.py:19390-19404`).
          Validation: `mode='reassess'` is rejected when
          `is_epic=False`; `mode='fresh'` against an epic that
          already has children logs a warning but proceeds.
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
            `EGG_IS_EPIC`; existing `EGG_JIRA_TICKET` /
            `EGG_JIRA_PROJECT` injection unchanged.
          - Unit tests in `orchestrator/tests/test_mcp_tools.py`
            and `orchestrator/tests/test_models.py` cover all
            branches.
        role: coder
        files:
          - orchestrator/mcp_tools.py
          - orchestrator/models.py
          - orchestrator/state_store.py
          - orchestrator/routes/pipelines.py
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
          **Plan-parser + Task model schema for ticket mapping
          (part C).** Extend `Task`
          (`shared/egg_contracts/models.py:182-242`) with optional
          `jira_key: str | None = None` (regex `^[A-Z][A-Z0-9_]*-
          [0-9]+$`) and `jira_action: Literal['create','edit',
          'wontdo','split-of','consolidate-into'] | None = None`.
          Update the YAML-task parser
          (`shared/egg_contracts/plan_parser.py:359-413`) to
          extract the new keys from each task block and propagate
          them into the parsed `Task` object. `parse_plan`
          (`shared/egg_contracts/plan_parser.py:1065`) already
          delegates to the per-task helper; verify the keys
          survive end-to-end. Reject `jira_action` values not in
          the literal allow-set with a `ParseWarning`.
        acceptance: |-
          - `Task(...)` accepts the new fields and round-trips
            through the contract JSON serialiser.
          - `parse_yaml_code_fence` + `parse_tasks_from_yaml` lift
            `jira_key` and `jira_action` from a fixture YAML.
          - Non-literal `jira_action` produces a warning, not a
            silent drop.
          - Unit tests in `shared/egg_contracts/tests/test_models.py`
            and `shared/egg_contracts/tests/test_plan_parser.py`
            cover the new fields end-to-end.
        role: coder
        files:
          - shared/egg_contracts/models.py
          - shared/egg_contracts/plan_parser.py
      - id: TASK-1-4
        description: |-
          **APPLIER role + apply-phase scheduling (part D).** Add
          `AgentRole.APPLIER = "applier"` to the `AgentRole` enum
          (`shared/egg_contracts/agent_roles.py:46-90`). Define
          `APPLIER_ROLE` `AgentRoleDefinition` next to the other
          analysis roles (~line 380); register it in `AGENT_ROLES`
          (`shared/egg_contracts/agent_roles.py:894-912`). Add a
          new `"apply"` entry to `_PHASE_ROLES`
          (`shared/egg_contracts/agent_roles.py:1107-1112`) with
          `[AgentRole.APPLIER]` and an empty `_PHASE_REVIEWERS`
          entry (decision-8 selected applier-with-BRC; reviewer
          added in TASK-1-7 if needed — see below). Define
          `APPLIER_PATTERNS` in `shared/egg_restrictions/
          patterns.py` (allowed: `.egg-state/agent-outputs/`;
          blocked: same blocklist as `_PLAN_AGENT_BLOCKED`
          extended with `src/`, `gateway/`, `sandbox/`, `shared/`,
          `orchestrator/`, `plugins/`). Wire the orchestrator
          phase scheduler in `orchestrator/routes/pipelines.py` to
          spawn the apply phase after every HITL phase_gate
          resolution=approve when `pipeline.is_epic` is true:
          extend `_persist_phase_gate_resolution`
          (`orchestrator/routes/pipelines.py:18274+`) and the
          existing post-HITL hook at `:20506` so that, on epic-
          mode pipelines, the apply phase runs between
          refine→plan and plan→implement. The apply phase reads
          the contract + relevant draft (analysis for refine-apply,
          plan + Task.jira_key/jira_action for plan-apply) and
          terminates on consensus.
        acceptance: |-
          - `AgentRole.APPLIER` exists; `AGENT_ROLES[APPLIER]` is
            populated.
          - `get_roles_for_phase('apply')` returns `[APPLIER]` (no
            reviewer).
          - `APPLIER_PATTERNS` registered in
            `shared/egg_restrictions/patterns.py` and surfaces via
            the existing role→patterns lookup.
          - On an epic-mode pipeline, the orchestrator schedules
            an apply phase after every refine + plan HITL
            approval; on non-epic pipelines no apply phase is
            scheduled.
          - The apply phase terminates after the applier reaches
            consensus (BRC degenerates with one producer + zero
            reviewers via `ApprovalMatrix.is_fully_acked()`).
          - Unit tests cover the scheduling decision in both
            `is_epic=True` and `is_epic=False` cases.
        role: coder
        files:
          - shared/egg_contracts/agent_roles.py
          - shared/egg_restrictions/patterns.py
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: |-
          **Applier prompt.** Author
          `plugins/refine-plan/skills/refine-plan/agents/
          applier.md` describing the applier's job: read the
          current phase context (`EGG_PIPELINE_MODE`, the just-
          approved phase, the contract path, the draft path);
          for refine-apply, write the analysis to the epic
          Description via `jira ticket edit "$EGG_JIRA_TICKET"
          --description-file <path>`; for plan-apply, walk
          `Task.jira_key` + `Task.jira_action` and call the
          appropriate jira CLI subcommand
          (`sandbox/scripts/jira ticket create|edit|link
          create`). Emphasise idempotent re-entry: if a task
          already has `jira_key` set and `jira_action='create'`,
          treat as no-op and continue (the contract is the
          durable record; gateway 5-min cache is the second
          layer). Reject unknown `jira_action` values with a
          structured failure that bubbles up via
          `mcp__progress__signal_error`. Note that Won't-Do
          transitions are NOT in the applier's purview (they
          live in slice 2's orchestrator-only route).
        acceptance: |-
          - Prompt under `plugins/refine-plan/skills/refine-plan/
            agents/applier.md` exists.
          - Prompt names every CLI subcommand the applier may use
            and references the existing
            `gateway/jira_idempotency.py:66` 5-min cache.
          - Prompt explicitly calls out idempotent re-entry rules.
          - Documents that the applier runs under the APPLIER role
            and may only write `.egg-state/agent-outputs/`.
        role: documenter
        files:
          - plugins/refine-plan/skills/refine-plan/agents/applier.md
      - id: TASK-1-6
        description: |-
          **Per-project epic_link_field wiring + ticket-create
          parent/Epic Link selection.** Verify and (if absent)
          wire the existing `JiraPolicy.epic_link_field()`
          (`gateway/jira_policy.py:163`) into the ticket-create
          path (`gateway/gateway.py:5580+`). The applier requests
          `parent: <EPIC-KEY>` on every `createJiraIssue`; the
          gateway translates that into either a `parent` payload
          or a `customfield_10014` payload per the project's
          configured `epic_link_field`. No agent prompt changes —
          the applier always uses the canonical `parent` shorthand.
          Add a unit test in `gateway/tests/test_jira_routes.py`
          covering both `epic_link_field='parent'` and
          `epic_link_field='customfield_10014'` translation.
        acceptance: |-
          - `gateway/gateway.py:5580+` ticket-create reads
            `policy.epic_link_field()` and emits the correct
            payload key.
          - Test fixtures cover both `parent` and `customfield_10014`
            paths.
          - Default (no project config) stays `parent`.
        role: coder
        files:
          - gateway/gateway.py
          - gateway/jira_policy.py
      - id: TASK-1-7
        description: |-
          **Slice-1 unit + integration test coverage.** Tests for
          TASK-1-1 (epic detection, env injection), TASK-1-3
          (plan-parser + Task model fields), TASK-1-4 (APPLIER role
          registry + scheduling decision), TASK-1-6 (epic_link_field
          translation). Integration test under
          `integration_tests/sdlc/` covering an epic-fresh pipeline
          end-to-end against a scripted-Jira fake: assert the
          applier sends `editJiraIssue` for the epic Description
          and `createJiraIssue` + `createIssueLink` for each
          planned child. Re-run the same pipeline twice and
          verify second-pass apply is a no-op (idempotency).
        acceptance: |-
          - `make test` passes on the new orchestrator + shared +
            gateway suites.
          - `make test-integration` (kubectl-gated) passes the
            new fresh-epic end-to-end flow.
          - Idempotent re-run produces zero new gateway writes
            on the second pass.
        role: tester
        files:
          - orchestrator/tests/test_mcp_tools.py
          - orchestrator/tests/test_models.py
          - shared/egg_contracts/tests/test_models.py
          - shared/egg_contracts/tests/test_plan_parser.py
          - shared/egg_contracts/tests/test_agent_roles.py
          - gateway/tests/test_jira_routes.py
          - integration_tests/sdlc/test_epic_fresh_path.py
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
          **Applier extension for reassess mutations + Won't-Do
          batch (part G + part D extension).** Update the applier
          prompt
          (`plugins/refine-plan/skills/refine-plan/agents/
          applier.md`) and the orchestrator post-plan-gate hook
          (`orchestrator/routes/pipelines.py:_persist_phase_gate_
          resolution`) so that on plan-apply for an epic-reassess
          pipeline:
          - `Task.jira_action == 'edit'` calls `jira ticket edit`
            on `Task.jira_key`.
          - `Task.jira_action == 'create'` calls `jira ticket
            create` (parent set to epic per TASK-1-6).
          - `Task.jira_action == 'consolidate-into'` records the
            survivor pointer and skips (the survivor task has
            `'edit'` action; the obsolete tasks all have
            `'wontdo'` action).
          - `Task.jira_action == 'split-of'` records the parent
            split-source pointer (informational only; the parent
            task has `'edit'` action narrowing scope and the new
            tasks have `'create'` action).
          - `Task.jira_action == 'wontdo'` is NOT executed by the
            applier — instead the applier emits a structured
            handoff JSON to `.egg-state/agent-outputs/` listing
            every Won't-Do key + the comment text, and the
            orchestrator post-apply hook iterates the list and
            calls the new `/transition` route (TASK-2-6) for each
            entry. Decision-4 batches all Won't-Do transitions on
            the single plan-gate approval.
          - Any task whose `jira_key` belongs to an `in_flight`
            child (per the sweep handoff at
            `EGG_REASSESS_SWEEP_PATH`) is **refused** unless the
            task carries a per-ticket override marker
            (`Task.notes` contains the literal string
            `in-flight-confirmed`). Refused mutations log a
            structured `mcp__progress__signal_error` with
            `recoverable=True` and skip; the operator can re-run
            after adding the marker.
        acceptance: |-
          - Applier routes each `jira_action` value to the right
            CLI subcommand or no-op as documented.
          - Won't-Do handoff file produced; orchestrator drains
            the list via `/transition` after applier consensus.
          - In-flight refusal documented in
            `applier.md` + enforced in orchestrator code; refused
            tasks surface in the apply phase's checkpoint.
          - Re-run with `in-flight-confirmed` added to a task's
            notes succeeds for that task only.
          - Unit tests in `orchestrator/tests/test_pipelines_apply.py`
            (new) cover routing + in-flight refusal + Won't-Do
            batch.
        role: coder
        files:
          - orchestrator/routes/pipelines.py
          - plugins/refine-plan/skills/refine-plan/agents/applier.md
      - id: TASK-2-8
        description: |-
          **Slice-2 unit + integration test coverage.** Tests for
          TASK-2-1 (sweep classification), TASK-2-2 (reverse-index
          + pr_url), TASK-2-3 (`/remotelinks` route + path
          validator), TASK-2-4 (in-flight helper truth table),
          TASK-2-6 (`/transition` route allowlist + auth + audit),
          TASK-2-7 (applier mutation routing + in-flight refusal +
          Won't-Do batch). Integration test under
          `integration_tests/sdlc/` covering an epic-reassess
          pipeline end-to-end with seeded children covering every
          classification class; assert the applier and post-apply
          orchestrator step produce the right edit / create /
          link / Won't-Do outcomes against a scripted-Jira fake.
        acceptance: |-
          - `make test` passes on the new and updated suites.
          - `make test-integration` passes the new reassess
            end-to-end flow.
          - In-flight refusal exercised by an integration test
            scenario where the planner emits an `'edit'` action
            on an `in_flight` child without the override marker.
        role: tester
        files:
          - orchestrator/tests/test_jira_reassess.py
          - orchestrator/tests/test_models.py
          - orchestrator/tests/test_state_store.py
          - orchestrator/tests/test_pipelines_apply.py
          - gateway/tests/test_jira_routes.py
          - integration_tests/sdlc/test_epic_reassess_path.py
```
