# Agent Roles Reference

All agent roles in egg, their responsibilities, phases, file access permissions, and input/output artifacts.

## Role Overview

| Role | Phase | Parallel? | Depends On |
|------|-------|-----------|------------|
| `refiner` | Refine | No | — |
| `reviewer_refine` | Refine | Yes (with `reviewer_agent_design`) | refiner |
| `reviewer_agent_design` | Refine | Yes (with `reviewer_refine`) | refiner |
| `architect` | Plan | No | — |
| `task_planner` | Plan | Yes (with `risk_analyst`) | architect |
| `risk_analyst` | Plan | Yes (with `task_planner`) | architect |
| `reviewer_plan` | Plan | No | task_planner, risk_analyst |
| `coder` | Implement | No (wave 1) | — |
| `tester` | Implement | Yes (with `documenter`) | coder |
| `documenter` | Implement | Yes (with `tester`) | coder |
| `checker` | Implement | Yes (reviewer, with `reviewer_code`, `reviewer_contract`) | coder |
| `integrator` | Implement | No | coder, tester |
| `reviewer_code` | Implement | Yes (with `reviewer_contract`) | integrator |
| `reviewer_contract` | Implement | Yes (with `reviewer_code`) | integrator |
| `reviewer_unified` | _(deprecated)_ | — | — |
| `inspector` | Any | — | — (health checks) |

Reviewer roles always run as a distinct step after all workers and the integrator complete.

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

**Prompt context**: Plan document (or phase-scoped subset in Tier 3), summarized background.

### `tester`

**Purpose**: Find gaps in the implementation, write and run tests.

**File access**:
- Allowed writes: `tests/`, `test/`, `**/tests/`, `**/test/`, all test file patterns (`**/*_test.py`, `**/test_*.py`, `**/*.test.ts`, etc.), `.egg-state/agent-outputs/`
- Blocked: `src/`, `lib/`, `shared/`, `gateway/`, `sandbox/`, `action/`, `docs/`, `**/README.md`, `.egg-state/contracts/`

**Outputs**:
- Test file commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-tester-output.json` — Handoff data

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

### `checker`

**Purpose**: Run linters, formatters, and auto-fixers on the code (sequential mode); evaluate coder output via BRC protocol (concurrent mode).

**File access** (defined as `CHECKER_PATTERNS` in `gateway/agent_restrictions.py`):
- Allowed writes: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `**/*.sh`, `**/*.yml`, `**/*.yaml`, `**/*.json`, `**/*.toml`, `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: `docs/`, `**/README.md`, `**/*.md`, `.egg-state/contracts/`

In concurrent (BRC) mode, `checker` acts as a reviewer in the implement phase alongside `reviewer_code` and `reviewer_contract`.

**Prompt context**: Summarized background, implementation summary.

### `integrator`

**Purpose**: Run the full test suite, validate integration, and produce a completion signal.

**Tier 2 file access**:
- Allowed writes: `.egg-state/agent-outputs/` only
- Blocked: `src/`, `lib/`, `shared/`, `gateway/`, `sandbox/`, `action/`, `docs/`, `tests/`, `test/`, `.egg-state/contracts/`, `.github/`

**Tier 3 file access** (when `complexity_tier == "high"`):
- Allowed writes: `.egg-state/agent-outputs/`, `src/`, `lib/`, `shared/`, `action/`, `docs/`, `tests/`, `test/`, `bin/`, `config/`, `scripts/`, `orchestrator/`, `integration_tests/`
- Still blocked: `.egg-state/contracts/`, `.github/`, `gateway/`, `sandbox/`

The Tier 3 expanded access is required because the integrator must merge results from multiple phase cycles and fix cross-phase integration issues.

**Outputs**:
- `.egg-state/agent-outputs/{identifier}-integrator-output.json` — Handoff data including test results
- `.egg-state/checks/{identifier}-implement-results.json` — Check results

**Prompt context**: Summarized background, all agent handoff data, check results.

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

## Other Roles

### `inspector`

**Purpose**: Health check role used by the Tier 2 semantic health check (`AgentInspectorCheck`). Sends pipeline context to Claude and interprets a structured JSON verdict.

**Usage**: Spawned on-demand by the health check framework, not by standard pipeline dispatch.

## Prompt Context Scoping

Agent prompts are scoped to role-relevant context to avoid unnecessary token usage and to focus each agent on its bounded work:

| Role group | Context provided |
|------------|-----------------|
| Analysis roles (architect, task_planner, risk_analyst) | Full issue body |
| Execution roles (tester, documenter, integrator) | Summarized background + structured task info + pointers to full context |
| Tier 3 phase-scoped coders | Plan overview + current phase tasks only (not full multi-phase plan) |
| Reviewers | Full plan/draft/diff relevant to their review scope |

## Role-Based Contract Mutations

The gateway enforces which roles can modify which fields of the contract JSON via the `/api/v1/contract/` endpoints:

| Role | Mutable contract fields |
|------|------------------------|
| `implementer` | `tasks[].commit`, `tasks[].notes`, `tasks[].files_affected` |
| `reviewer` | `tasks[].status`, `phases[].status`, `phases[].review_feedback`, `acceptance_criteria[].verified`, `current_phase` |
| `human` | `decisions[].resolved`, `decisions[].resolution`, `decisions[].resolved_by`, `decisions[].resolved_at`, all other fields |
| `system` | Structural fields (`issue`, `schemaVersion`) |

## File Permission Enforcement

Agent file restrictions are enforced at git push time by the gateway. The default behavior is **warn-only**; set `EGG_AGENT_RESTRICTIONS_ENFORCE=true` to make violations block the push.

For the exact allowed and blocked patterns per role, see `gateway/agent_restrictions.py`.

## Related Documentation

- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — Phase execution and agent waves
- [Tier 3 Dispatch Guide](../guides/tier3-dispatch.md) — Integrator Tier 3 permissions
- [Architecture Overview](../architecture/README.md) — Role-based access control
