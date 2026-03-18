# Agent Roles Reference

All agent roles in egg, their responsibilities, phases, file access permissions, and input/output artifacts.

## Agent Categories

Every agent role belongs to one of five categories. Categories enable dynamic team composition — for example, querying "all review agents" or "all utility agents" — and are defined in the `AgentCategory` enum in `shared/egg_contracts/agent_roles.py`.

| Category | Purpose | Roles |
|----------|---------|-------|
| **EXECUTION** | Produce artifacts (code, tests, docs) | `coder`, `tester`, `documenter` |
| **ANALYSIS** | Analyze tasks and plan work | `refiner`, `architect`, `task_planner`, `risk_analyst` |
| **REVIEW** | Validate quality and correctness | `reviewer_code`, `reviewer_contract`, `reviewer_refine`, `reviewer_plan`, `reviewer_agent_design` |
| **UTILITY** | Cross-cutting support tasks | `autofixer`, `conflict_resolver` |
| **INTERFACE** | Pipeline health and monitoring | `inspector`, `overseer` |

Use `get_roles_by_category(AgentCategory.REVIEW)` to dynamically query roles by category.

## Role Overview

| Role | Category | Phase | Parallel? | Depends On |
|------|----------|-------|-----------|------------|
| `refiner` | Analysis | Refine | No | — |
| `reviewer_refine` | Review | Refine | Yes (with `reviewer_agent_design`) | refiner |
| `reviewer_agent_design` | Review | Refine (egg repo only) | Yes (with `reviewer_refine`) | refiner |
| `architect` | Analysis | Plan | No | — |
| `task_planner` | Analysis | Plan | Yes (with `risk_analyst`) | architect |
| `risk_analyst` | Analysis | Plan | Yes (with `task_planner`) | architect |
| `reviewer_plan` | Review | Plan | No | task_planner, risk_analyst |
| `coder` | Execution | Implement | No | — |
| `tester` | Execution | Implement | Yes (with `documenter`) | coder |
| `documenter` | Execution | Implement | Yes (with `tester`) | coder |
| `reviewer_code` | Review | Implement | Yes (with `reviewer_contract`) | coder, tester |
| `reviewer_contract` | Review | Implement | Yes (with `reviewer_code`) | coder, tester |
| `autofixer` | Utility | Any | Yes | — |
| `conflict_resolver` | Utility | Any | Yes | — |
| `inspector` | Interface | Any | — | — (health checks) |
| `overseer` | Interface | All phases | — | — (pipeline health monitoring) |

All agents within a phase run concurrently via BRC consensus. Concurrency is enabled by default for the refine, plan, and implement phases, and can be extended to additional phases via the `concurrent_phases` config.

## Refine Phase

### `refiner`

**Purpose**: Analyze the task, research the codebase, evaluate approaches, and produce a requirements analysis document.

**File access**:
- Allowed writes: `.egg-state/drafts/`, `.egg-state/agent-outputs/`
- Blocked: All source code (`**/*.py`, `**/*.ts`, etc.), `.egg-state/contracts/`

**Outputs**:
- `.egg-state/drafts/{identifier}-analysis.md` — The analysis document
- `.egg-state/agent-outputs/{identifier}-refiner-output.json` — Handoff data for downstream agents

**Prompt context**: Full issue body, codebase context.

### `reviewer_refine`

**Purpose**: Review analysis quality and completeness; produce an approve/needs-revision verdict.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: All source code, contracts, drafts

**Outputs**:
- `.egg-state/reviews/{identifier}-refine-reviewer_refine-review.json` — Verdict file

### `reviewer_agent_design`

**Scope**: Egg repo only (`jwbron/egg`). Not spawned for pipelines on other repos.

**Purpose**: Review the analysis for agent-mode alignment and anti-patterns (e.g., correct use of egg's structural enforcement model).

**File access**: Same as `reviewer_refine`.

**Outputs**:
- `.egg-state/reviews/{identifier}-refine-reviewer_agent_design-review.json` — Verdict file

## Plan Phase

### `architect`

**Purpose**: Analyze the task, research the codebase, and recommend a high-level implementation approach.

**File access**:
- Allowed writes: `.egg-state/drafts/`, `.egg-state/agent-outputs/`
- Blocked: `src/`, `lib/`, `shared/`, `gateway/`, `sandbox/`, `action/`, `docs/`, `tests/`, `.egg-state/contracts/`, `.egg-state/reviews/`, `.github/`

**Outputs**:
- `.egg-state/agent-outputs/{identifier}-architect-output.json` — Architectural analysis

**Prompt context**: Full issue body, refine analysis.

### `task_planner`

**Purpose**: Break the work into discrete phases and tasks with acceptance criteria. Produces the plan document with a YAML appendix.

**File access**: Same as `architect`.

**Outputs**:
- `.egg-state/drafts/{identifier}-plan.md` — The plan document (includes YAML appendix)
- `.egg-state/agent-outputs/{identifier}-task_planner-output.json` — Handoff data

**Prompt context**: Full issue body, architect output.

### `risk_analyst`

**Purpose**: Identify technical risks and propose mitigation strategies.

**File access**: Same as `architect`.

**Outputs**:
- `.egg-state/agent-outputs/{identifier}-risk_analyst-output.json` — Risk analysis

**Prompt context**: Full issue body, architect output.

### `reviewer_plan`

**Purpose**: Review plan quality, task breakdown, dependencies, test strategy, and alignment with the analysis.

**File access**: Same as `reviewer_refine`.

**Outputs**:
- `.egg-state/reviews/{identifier}-plan-reviewer_plan-review.json` — Verdict file

## Implement Phase

### `coder`

**Purpose**: Write code, create commits, push to the worktree branch.

**File access**:
- Allowed writes: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `**/*.sh`, `**/*.yml`, `**/*.yaml`, `**/*.json`, `**/*.toml`, `.egg-state/agent-outputs/`
- Blocked: `docs/`, `**/README.md`, `**/*.md`, `.egg-state/contracts/`, `tests/`, `test/`, `**/tests/`, `**/test/`, all test file patterns

**Outputs**:
- Commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-coder-output.json` — Handoff data

**Prompt context**: Plan document, summarized background.

### `tester`

**Purpose**: Find gaps in the implementation, write and run tests, run linters and type checkers, and apply auto-fixes.

**File access**:
- Allowed writes: `tests/`, `test/`, `**/tests/`, `**/test/`, all test file patterns (`**/*_test.py`, `**/test_*.py`, `**/*.test.ts`, etc.), `.egg-state/agent-outputs/`
- Blocked: `src/`, `lib/`, `shared/`, `gateway/`, `sandbox/`, `action/`, `docs/`, `**/README.md`, `.egg-state/contracts/`

**Outputs**:
- Test file commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-tester-output.json` — Handoff data (includes lint/type-check results and auto-fixes applied)

**Prompt context**: Summarized background, coder handoff data, task list.

### `documenter`

**Purpose**: Update documentation and READMEs.

**File access**:
- Allowed writes: `docs/`, `**/*.md`, `**/README.md`, `.egg-state/agent-outputs/`
- Blocked: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `tests/`, `test/`, `**/tests/`, `**/test/`, `.egg-state/contracts/`

**Outputs**:
- Documentation commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-documenter-output.json` — Handoff data

**Prompt context**: Summarized background, task list, pointers to relevant docs.

### `reviewer_code`

**Purpose**: Security review, correctness, code quality, test coverage, and documentation quality.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: All source, docs, tests, contracts, drafts

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_code-review.json` — Verdict file

### `reviewer_contract`

**Purpose**: Verify acceptance criteria are met and all tasks are marked complete in the contract.

**File access**: Same as `reviewer_code`.

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_contract-review.json` — Verdict file

## Utility Roles

### `autofixer`

**Category**: Utility

**Purpose**: Automatically fix lint errors, formatting issues, and type-check failures in source and config files. Runs on-demand to clean up code without manual intervention.

**File access**:
- Allowed writes: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `**/*.sh`, `**/*.yml`, `**/*.yaml`, `**/*.json`, `**/*.toml`, `.egg-state/agent-outputs/`
- Blocked: `docs/`, `**/*.md`, `.egg-state/contracts/`

**Outputs**:
- Commits with auto-fix changes on the worktree branch
- `.egg-state/agent-outputs/{identifier}-autofixer-output.json` — Summary of fixes applied

### `conflict_resolver`

**Category**: Utility

**Purpose**: Resolve merge conflicts, inter-agent file conflicts, and coordination issues across concurrent agents. Can write to source, test, doc, and config files to mediate overlapping changes.

**File access**:
- Allowed writes: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `**/*.sh`, `**/*.yml`, `**/*.yaml`, `**/*.json`, `**/*.toml`, `tests/`, `test/`, `**/tests/`, `**/test/`, `docs/`, `**/*.md`, `.egg-state/agent-outputs/`
- Blocked: `.egg-state/` (contracts, drafts, reviews, pipelines)

**Outputs**:
- Conflict resolution commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-conflict_resolver-output.json` — Resolution decisions and rationale

## Interface Roles

### `inspector`

**Category**: Interface

**Purpose**: Health check role used by the Tier 2 semantic health check (`AgentInspectorCheck`). Runs targeted diagnostics inside a sandbox container, collects health-check data, and reports findings via agent-outputs.

**Usage**: Spawned on-demand by the health check framework, not by standard pipeline dispatch.

**File access**:
- Allowed writes: `.egg-state/agent-outputs/`
- Blocked: All source code, tests, docs, configs, contracts, drafts, reviews

### `overseer`

**Purpose**: Pipeline health monitoring agent that detects and responds to agent failures, stalls, loops, and off-track behavior. Uses a two-sub-tier LLM architecture: Haiku classifiers for anomaly detection and Sonnet/Opus decision-makers for corrective action.

**Lifecycle**: Auto-spawned at pipeline start (when `overseer_enabled` is true in `PipelineConfig`). Runs across all phases until pipeline completion — one overseer per pipeline.

**File access**:
- Allowed writes: `.egg-state/oversight/` (structured oversight logs)
- Blocked: All source code, tests, docs, configs, contracts, drafts, reviews

**Access**:
- Orchestrator APIs: pipeline status, container logs, progress queries, health alerts, message bus
- GitHub API: `gh issue create` for diagnostic issue filing
- `egg-orch message send` to redirect individual agents

**Blocked from**:
- All git operations (no repo volume mounted)
- `gh pr merge`, `gh pr create`
- `egg-orch phase advance`, `egg-orch phase complete`
- Direct agent restart (must go through HITL decision queue)

**Outputs**:
- Redirect messages to stalled/off-track agents
- HITL escalation requests for agent restarts
- Autonomous GitHub issues with structured diagnostics (labeled `overseer-alert`)
- Pipeline health summary at completion
- Structured oversight logs in `.egg-state/oversight/`

**Prompt context**: Orchestrator health alerts, structured progress events, agent container logs, pipeline state.

See [Pipeline Health Monitoring Guide](../guides/pipeline-health-monitoring.md) for full details.

## Prompt Context Scoping

Agent prompts are scoped to role-relevant context to avoid unnecessary token usage and to focus each agent on its bounded work:

| Role group | Context provided |
|------------|-----------------|
| Analysis roles (architect, task_planner, risk_analyst) | Full issue body |
| Execution roles (coder, tester, documenter) | Summarized background + pointers to full context |
| Utility roles (autofixer, conflict_resolver) | Targeted context (e.g., lint output, conflict details) |
| Interface roles (inspector, overseer) | Pipeline state, health alerts, agent logs |
| Reviewers | Full plan/draft/diff relevant to their review scope |

## Role-Based Contract Mutations

The gateway enforces which roles can modify which fields of the contract JSON via the `/api/v1/contract/` endpoints:

| Role | Mutable contract fields |
|------|------------------------|
| `implementer` | `tasks[].commit`, `tasks[].notes`, `tasks[].files_affected` |
| `reviewer` | `tasks[].status`, `phases[].status`, `phases[].review_feedback`, `acceptance_criteria[].verified`, `current_phase` |
| `human` | `decisions[].resolved`, `decisions[].resolution`, `decisions[].resolved_by`, `decisions[].resolved_at`, all other fields |
| `system` | Structural fields (`issue`, `schemaVersion`) |

## Role Registry (Source of Truth)

All agent roles are defined in a single canonical location: `shared/egg_contracts/agent_roles.py`. This module provides:

- **`AgentRole`** — `StrEnum` with all role identifiers
- **`AgentCategory`** — `StrEnum` categorizing roles (EXECUTION, ANALYSIS, REVIEW, UTILITY, INTERFACE)
- **`AgentRoleDefinition`** — Dataclass combining role, description, responsibilities, dependencies, file access, and category
- **`AGENT_ROLES`** — Registry mapping each `AgentRole` to its definition
- **`get_role_definition(role)`** — Look up a role's full definition
- **`get_roles_by_category(category)`** — Query all roles in a given category
- **`get_roles_for_phase(phase)`** — Get roles assigned to a pipeline phase
- **`detect_write_overlaps(roles)`** — Find file access conflicts between parallel roles

Other modules (`orchestrator/models.py`, `shared/egg_orchestrator/types.py`) import `AgentRole` from this canonical source rather than defining their own copies.

### Removed Roles

The following roles have been removed but are still handled for backward compatibility during deserialization:

| Removed Role | Migration |
|-------------|-----------|
| `reviewer_unified` | Split into `reviewer_code` + `reviewer_contract` |
| `reviewer` (generic) | Mapped to `reviewer_code` |
| `checker` | Replaced by `tester` |
| `integrator` | Removed — no replacement needed |

## Team Composition Templates

Common agent team configurations for different workflow types:

| Workflow | Agents | Description |
|----------|--------|-------------|
| **Full pipeline** | All phase-specific roles | Complete SDLC with refine → plan → implement |
| **Coder + reviewer** | `coder`, `reviewer_code` | Lightweight implementation with code review |
| **Analysis only** | `refiner`, `reviewer_refine` | Task analysis without implementation |
| **Auto-fix** | `autofixer` | Automated lint/format fixes |

## File Permission Enforcement

Agent file restrictions are enforced at git push time by the gateway. The default behavior is **warn-only**; set `EGG_AGENT_RESTRICTIONS_ENFORCE=true` to make violations block the push.

For the exact allowed and blocked patterns per role, see `gateway/agent_restrictions.py`.

## Related Documentation

- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — Phase execution and agent orchestration
- [Concurrent Execution Guide](../guides/concurrent-execution.md) — BRC consensus protocol
- [Agent Development Guide](../guides/agent-development.md) — How to add new agent roles
- [Architecture Overview](../architecture/README.md) — Role-based access control
