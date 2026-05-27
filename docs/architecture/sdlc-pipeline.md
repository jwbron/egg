# SDLC Pipeline Architecture

The SDLC (Software Development Lifecycle) Pipeline provides structurally enforced agent checkpoints and verification gates for autonomous software development.

> **Note:** The GitHub Actions-based SDLC workflow described historically has been superseded by the local distributed orchestrator (`orchestrator/` package). The architectural principles, contract system, role-based access control, and HITL mechanisms remain valid — only the execution layer changed.

For operational details, CLI commands, and triggering instructions, see the [SDLC Pipeline Guide](../guides/sdlc-pipeline.md).

**The core guarantee**: An agent cannot bypass verification gates or self-approve its own work. All state transitions are enforced structurally through role-based mutations and gateway policy enforcement.

**Key properties:**
- **Phased execution**: Work progresses through defined phases (refine → plan → implement). The standalone PR phase was collapsed in [#2777](https://github.com/jwbron/egg/issues/2777); the context PR is now opened by the orchestrator at the plan→implement boundary instead.
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
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                  │
│  │   Refine    │───▶│    Plan     │───▶│  Implement  │  ←  context PR    │
│  │  (Human)    │    │  (Human)    │    │ (Reviewer)  │     opened at     │
│  └─────────────┘    └─────────────┘    └─────────────┘     plan→impl     │
│        │                  │                  │             boundary      │
│        ▼                  ▼                  ▼                            │
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
| **implement** | Task execution and code changes (terminal phase since [#2777](https://github.com/jwbron/egg/issues/2777)) | All checks pass (CI + PR review); merge of the up-front context PR |

The standalone **PR phase was removed in [#2777](https://github.com/jwbron/egg/issues/2777)**. The orchestrator now opens the `egg/<id>/work → main` Context PR at the plan→implement boundary via `_open_context_pr_at_implement_start`, applying the same idempotent `gh pr list` pre-flight in both monolithic and slice-DAG modes. See [Context PR](orchestrator.md#context-pr-2548-collapsed-in-2777) for the opener mechanics.

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

> **Schema 1.2 (#2777)**: `schemaVersion` was bumped from `1.1` to `1.2`
> to track two breaking changes:
> 1. The `pr.context_title`, `pr.context_description`, and
>    `pr.context_branch` fields on `PRMetadata` (introduced in schema
>    1.1 by #2548) were **removed**. The standalone context-branch
>    topology was collapsed; the program-level Context PR now opens
>    directly on `egg/<id>/work` and uses the regular `pr.title` /
>    `pr.description` fields. `pr.context_pr_number` is retained — it
>    references the new direct-on-work PR.
> 2. `PipelinePhase.PR` was removed from the phase enum; the standalone
>    PR phase no longer exists.
>
> Unlike the additive 1.0→1.1 promotion, the 1.1→1.2 bump is a **clean
> break**: feedback Q5 confirmed zero in-flight pipelines, so legacy
> v1.1 contracts on disk surface a clear Pydantic `ValidationError`
> ("schemaVersion bumped to 1.2; legacy fields removed") rather than a
> silent migration. The orchestrator-internal helper
> `_open_context_pr_at_implement_start(pipeline_id)`
> (`orchestrator/routes/pipelines.py`) opens the Context PR at the
> plan→implement boundary using the regular `pr.title` /
> `pr.description` fields. See the [v1.1 → v1.2 migration
> note](../releases/2777-pr-phase-collapse.md) for the full migration
> procedure and recovery steps.

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
| BRC consensus history | `.egg-state/brc-history/{identifier}-{phase}.md` and `.json` (re-written in PR phase as safety net; `.md` is human-readable with YAML metadata blocks, `.json` is machine-readable) |
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
