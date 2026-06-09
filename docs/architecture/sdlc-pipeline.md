# SDLC Pipeline Architecture

The SDLC (Software Development Lifecycle) Pipeline provides structurally enforced agent checkpoints and verification gates for autonomous software development.

> **Note:** The GitHub Actions-based SDLC workflow described historically has been superseded by the local distributed orchestrator (`orchestrator/` package). The architectural principles, contract system, role-based access control, and HITL mechanisms remain valid — only the execution layer changed.

For operational details, CLI commands, and triggering instructions, see the [SDLC Pipeline Guide](../guides/sdlc-pipeline.md).

**The core guarantee**: An agent cannot bypass verification gates or self-approve its own work. All state transitions are enforced structurally through role-based mutations and gateway policy enforcement.

**Key properties:**
- **Phased execution**: Work progresses through defined phases (refine → plan → implement); implement is terminal — the context PR opens at the plan→implement boundary
- **Role-based control**: Implementer, Reviewer, and Human roles have distinct permissions
- **Human-in-the-loop**: Critical transitions pause for human approval
- **Audit trail**: All mutations are logged for accountability

## Motivation

Autonomous agents operating on codebases require oversight. Behavioral controls (instructions) are insufficient because:
1. **Prompt injection risk**: Agents may be tricked into bypassing instructions
2. **Model drift**: Agent behavior may vary across runs
3. **Infinite loops**: Without human oversight, agents may cycle indefinitely

This architecture implements **structural enforcement**: the agent physically cannot perform certain operations without appropriate role authorization, regardless of its instructions.

## Threat Model

| Threat | Mitigation |
|--------|------------|
| Agent self-approves work | Role-based mutations prevent implementer from marking tasks complete |
| Agent skips review phase | Phase transitions require reviewer or human role |
| Agent loops indefinitely | PR-based reviews provide human visibility at every cycle |
| Agent modifies own permissions | Role comes from workflow context, not agent environment |
| Changes lack accountability | Audit log tracks all mutations with role and actor |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            SDLC Pipeline                                │
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐  │
│  │   Refine    │───▶│    Plan     │───▶│         Implement           │  │
│  │  (Human)    │    │  (Human)    │    │  (Reviewer; context PR      │  │
│  └─────────────┘    └─────────────┘    │   opened at phase start)    │  │
│                                        └─────────────────────────────┘  │
│        │                  │                  │                          │
│        ▼                  ▼                  ▼                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Contract State                               │    │
│  │  .egg-state/contracts/{identifier}.json                         │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
└──────────────────────────────│──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Gateway Sidecar                                  │
│  Contract API → Role Enforcement → Phase Filter                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### Pipeline Phases

| Phase | Purpose | Exit Requires |
|-------|---------|---------------|
| **refine** | Problem analysis and requirements gathering | Human approval |
| **plan** | Implementation planning with task breakdown | Human approval |
| **implement** | Task execution and code changes; context PR opened automatically at phase start | All checks pass + human merge (terminal phase) |

### Role Permissions

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| **Implementer** | Task commit, notes, files_affected | Task status, phase status, current_phase |
| **Reviewer** | Task status, phase status, current_phase | Task commit, notes, decision resolution |
| **Human** | All fields | — |
| **System** | Initial contract creation | Owned fields after creation |

## Contract Schema

The contract is a JSON document tracking the complete state of an issue through the pipeline:

```json
{
  "schemaVersion": "1.2",
  "issue": { "number": 133, "title": "...", "url": "..." },
  "current_phase": "implement",
  "slices": [{
    "id": "slice-1",
    "name": "Core Library",
    "status": "in_progress",
    "dependencies": [],
    "serialized_chain_order": [],
    "parent_branch_at_creation": null,
    "tasks": [{
      "id": "task-1-1",
      "description": "Create contract schema",
      "status": "complete",
      "commit": "abc1234",
      "review_cycles": 1
    }]
  }],
  "workflow_owner": "my-org",
  "audit_log": [...]
}
```

> **Schema rename (#2137)**: `phases[]` was renamed to `slices[]` and
> `phase-N` IDs to `slice-N` to support the slice-DAG implement model
> (each slice is an independent unit with its own branch, BRC, and PR).
> Pre-#2137 contract JSON (`phases: [...]`) loads transparently via a
> Pydantic load-time migration shim that rewrites both keys and IDs;
> `Contract.phases` remains a read/write property proxy to
> `Contract.slices`, and the `Phase`/`PhaseStatus` aliases preserve
> existing imports. See [Slice-DAG Implement Phase](slice-dag.md) for
> the full design.

> **Schema 1.1 (#2548)**: `schemaVersion` was bumped from `1.0` to `1.1`
> to track the addition of four optional `pr.context_*` fields on
> `PRMetadata` used by the dedicated context-PR mechanism. The bump was
> purely additive at the time. The two-branch topology these fields
> supported was collapsed in #2777 (see schema 1.2 below); three of the
> four fields are now hard-removed and only `pr.context_pr_number`
> remains.
>
> **Schema 1.2 (#2777)**: `schemaVersion` was bumped from `1.1` to `1.2`
> to **hard-remove** three redundant fields on `PRMetadata`:
> `pr.context_branch`, `pr.context_title`, and `pr.context_description`.
> Under the new topology (#2777) the context PR is
> `egg/<pipeline_id>/work → main`, so `context_branch` is always
> derivable as `egg/<pipeline_id>/work` and the program-level title and
> description live on the standard `pr.title` / `pr.description`. The
> sole remaining context-PR field is `pr.context_pr_number` (still the
> PR number of the `egg/<pipeline_id>/work → main` context PR). Pre-1.2
> contracts on disk load transparently: a `model_validator(mode="wrap")`
> migration (`Contract._migrate_schema_version_to_1_2`) strips the three
> removed keys before Pydantic constructs `PRMetadata` and bumps
> `schemaVersion` to `"1.2"`; the new value is persisted on the next
> save. See [v1.1 → v1.2 schema migration note](#schema-v11--v12-migration-note-2777).
>
> **Schema 1.3 (#3033)**: `schemaVersion` was bumped from `1.2` to `1.3`
> to document the addition of an optional top-level `task_description`
> field. The field holds the full, untruncated pipeline `prompt` for
> free-text and JIRA-driven submits (`pipeline.issue_number is None`),
> giving producer and reviewer agents a reliable channel to recover the
> complete task from the contract — the BRC event-pump model does not
> deliver the orchestrator-built spawn prompt to the agent, and the 100-
> character `issue.title` is only a label. The field is `None` for
> GitHub-issue pipelines (agent fetches body via `gh issue view`). The
> bump is purely additive: a `model_validator(mode="after")`
> (`Contract._migrate_schema_version_to_1_3`) promotes any `1.2` contract
> to `1.3` at load time; the new value is persisted on the next save.
> See [v1.2 → v1.3 migration note](#schema-v12--v13-migration-note-3033).
>
> **Context-PR mechanism (#2777 collapse).** The context PR is opened
> **up-front at the plan→implement boundary**, **hard-required** and
> **idempotent**, against the pipeline work branch (`egg/<pipeline_id>/work
> → main`). The legacy `egg/<pipeline_id>/context` doc-only branch and
> the multi-step soft-fail open path were deleted (#2777). The PR phase
> as a separate pipeline stage was also deleted; there is no terminal
> backstop because the up-front open is hard-required and idempotent.
> Slice-1's `parent_branch` resolves to `egg/<id>/work`; the stacked-PR
> reconciler uses the work branch as the canonical fallback when
> retargeting orphaned children.

### Schema v1.1 → v1.2 migration note (#2777)

`PRMetadata` lost three redundant fields in v1.2:

| Removed field | Replacement |
|---------------|-------------|
| `pr.context_branch` | Derived: always `egg/<pipeline_id>/work`. The orchestrator computes the head branch from the pipeline ID; nothing reads `pr.context_branch` from the contract anymore. |
| `pr.context_title` | Use `pr.title`. Program-level framing now reuses the standard title. |
| `pr.context_description` | Use `pr.description`. Program-level narrative now reuses the standard description. |

Operational impact:

- **In-flight contracts on disk** that still contain any of the three
  removed fields load transparently under v1.2: the
  `Contract._migrate_schema_version_to_1_2` wrap-mode migration strips
  the three removed keys before Pydantic constructs `PRMetadata` and
  bumps `schemaVersion` to `"1.2"`. The new value is persisted on the
  next contract save. New pipelines start at v1.2 and never write the
  removed fields.
- **Planner authoring**: plan YAML must no longer emit
  `pr.context_title` or `pr.context_description` — those keys are
  rejected at parse time. Use `pr.title` and `pr.description` to frame
  the work→main context PR.
- **PR-phase removal**: there is no longer a separate "PR" pipeline
  stage. The context PR opens up-front at plan→implement boundary
  (hard-required, idempotent via `GatewayClient.lookup_open_pr`'s
  server-side head+base filter) and per-slice PRs are opened inline by
  `create_slice_pr` (idempotent via the same
  `GatewayClient.lookup_open_pr` primitive, #2777 cq-8 / #2934). The
  legacy `_should_skip_pr_phase_auto_pr` skip gate and the PR-phase
  route/runner were removed.

### Schema v1.2 → v1.3 migration note (#3033)

`Contract` gained one new optional field in v1.3:

| Added field | Purpose |
|-------------|---------|
| `task_description` | Full, untruncated pipeline prompt for free-text and JIRA-driven submits. `None` for GitHub-issue pipelines. |

Operational impact:

- **In-flight contracts on disk** that omit `task_description` load
  cleanly — Pydantic defaults the field to `None`. The
  `Contract._migrate_schema_version_to_1_3` `mode="after"` validator
  stamps `schemaVersion` from `"1.2"` to `"1.3"` on every load;
  the new value is persisted on the next save.
- **`task_description` is `SYSTEM`-owned** in `FIELD_OWNERSHIP` — agents
  must not mutate it. Both `egg-contract show` and
  `mcp__sdlc__show_contract` surface the field.
- **Agent task recovery**: under the BRC event-pump the spawn prompt is
  not delivered to agents. Agents on free-text or JIRA pipelines should
  read `task_description` from the contract rather than inferring the
  task from `issue.title` (100-char label only). See
  [Recovering the task](../reference/sdlc-contract.md#recovering-the-task--task_description).

## HITL (Human-in-the-Loop) Mechanism

For detailed HITL workflow documentation, see [HITL Decisions](../hitl-decisions.md).

When escalation occurs, the system generates a decision block with checkboxes for human input. A 30-second debounce prevents accidental clicks.

Phase approval uses `<!-- egg-phase-approval -->` markers with a single approval checkbox, detected by the orchestrator's decision queue.

## Gateway Integration

### Contract API Endpoints

Contract state is owned by the **orchestrator**. The gateway proxies agent requests to the orchestrator and enforces role authentication; it no longer holds contract state itself.

**Orchestrator endpoints** (authoritative):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/contracts/<identifier>` | GET | Retrieve contract state |
| `/api/v1/contracts/<identifier>/exists` | GET | Check whether a contract exists |
| `/api/v1/contracts/<identifier>/mutate` | POST | Apply mutation with role enforcement |
| `/api/v1/contract-mutations/validate` | POST | Dry-run a mutation without applying |

**Gateway endpoints** (proxy for sandbox agents — same paths as before):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/contract/<identifier>` | GET | Proxied read (forwards to orchestrator) |
| `/api/v1/contract/exists/<identifier>` | GET | Proxied existence check |
| `/api/v1/contract/mutate` | POST | Proxied mutation (role verified, then forwarded) |
| `/api/v1/contract/validate` | POST | Proxied validation |
| `/api/v1/phase/advance` | POST | Advance to next phase |
| `/api/v1/phase/filter` | POST | Check if operation is allowed |

Role is determined from session metadata (set by launcher), not from the agent environment. The gateway sends the verified role to the orchestrator via `X-Egg-Role`.

## Orchestrator Integration

The local distributed orchestrator (`orchestrator/` package) manages the full lifecycle:

- `orchestrator/dispatch.py` — Phase dispatch and management
- `orchestrator/container_spawner.py` — Agent container lifecycle
- `orchestrator/decision_queue.py` — HITL decision handling
- `orchestrator/state_store.py` — Git-backed pipeline state
- `orchestrator/contract_store.py` — Shared-worktree contract I/O and per-identifier locking
- `orchestrator/routes/contracts.py` — REST endpoints for contract reads and mutations

## Security Properties

1. **Role isolation**: Agent cannot escalate its own role
2. **Mutation validation**: Every mutation is checked against role permissions
3. **Audit trail**: All changes are logged with actor and role
4. **Phase enforcement**: Operations are filtered based on current phase
5. **Human gates**: Critical transitions require human approval

## Files and Locations

| Component | Location |
|-----------|----------|
| Contract schema | `.egg/schemas/contract.schema.json` |
| Contract instances | `.egg-state/contracts/{identifier}.json` |
| Phase drafts | `.egg-state/drafts/{identifier}-{analysis\|plan}.md` (preserved on PR branch as pipeline artifacts) |
| BRC consensus history | `.egg-state/brc-history/{identifier}-{phase}.md` and `.json` (committed by each phase as it completes; `.md` is human-readable with YAML metadata blocks, `.json` is machine-readable) |
| Review verdicts | `.egg-state/reviews/{identifier}-{phase}-{reviewer}.json` |
| Contract library | `shared/egg_contracts/` |
| Gateway endpoints | `gateway/contract_api.py` (proxy), `gateway/phase_api.py` |
| Orchestrator contract endpoints | `orchestrator/routes/contracts.py` |
| Orchestrator contract store | `orchestrator/contract_store.py` |
| Orchestrator | `orchestrator/` |
| CLI tools | `sandbox/egg_lib/contract_cli.py` |
| HITL documentation | `docs/hitl-decisions.md` |

## Related Documentation

- [SDLC Pipeline Operational Guide](../guides/sdlc-pipeline.md) — Day-to-day usage
- [Slice-DAG Implement Phase](slice-dag.md) — `Phase`→`Slice` rename, slice scheduler, stacked-PR reconciler
- [The Agentic Feedback Loop](agentic-feedback-loop.md) — Foundational work-review cycle
- [Architecture Overview](README.md) — System design
