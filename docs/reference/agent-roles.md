# Agent Roles Reference

All agent roles in egg, their responsibilities, phases, file access permissions, and input/output artifacts.

## Agent Categories

Every agent role belongs to one of five categories. Categories enable dynamic team composition — for example, querying "all review agents" or "all utility agents" — and are defined in the `AgentCategory` enum in `shared/egg_contracts/agent_roles.py`.

| Category | Purpose | Roles |
|----------|---------|-------|
| **EXECUTION** | Produce artifacts (code, tests, docs) | `coder`, `tester`, `documenter` |
| **ANALYSIS** | Analyze tasks and plan work | `refiner`, `architect`, `task_planner`, `risk_analyst` |
| **REVIEW** | Validate quality and correctness | `reviewer_code`, `reviewer_code_holistic`, `reviewer_contract`, `reviewer_refine`, `reviewer_plan`, `reviewer_agent_design`, `reviewer_security`, `reviewer_concurrency` |
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
| `reviewer_code` | Review | Implement | Yes (with `reviewer_code_holistic`, `reviewer_contract`, `reviewer_security`, `reviewer_concurrency`) | coder, tester |
| `reviewer_code_holistic` | Review | Implement | Yes (with `reviewer_code`, `reviewer_contract`, `reviewer_security`, `reviewer_concurrency`) | coder, tester |
| `reviewer_contract` | Review | Implement | Yes (with `reviewer_code`, `reviewer_code_holistic`, `reviewer_security`, `reviewer_concurrency`) | coder, tester |
| `reviewer_security` | Review | Implement | Yes (with `reviewer_code`, `reviewer_code_holistic`, `reviewer_contract`, `reviewer_concurrency`) | coder, tester |
| `reviewer_concurrency` | Review | Implement | Yes (with `reviewer_code`, `reviewer_code_holistic`, `reviewer_contract`, `reviewer_security`) | coder, tester |
| `autofixer` | Utility | Any | Yes | — |
| `conflict_resolver` | Utility | Any | Yes | — |
| `inspector` | Interface | Any | — | — (health checks) |
| `overseer` | Interface | Per-phase (spawned/torn down at phase boundaries) | — | — (pipeline health monitoring) |

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

**Scope**: Egg repo only (`jwbron/egg`). Not spawned for pipelines on other repos. The canonical repo string is hardcoded in `shared/egg_contracts/agent_roles.py` (`EGG_REPO`).

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

The three producer roles in this phase — `coder`, `tester`, and `documenter` —
are **mutually exclusive by construction** (see [#1901][issue-1901]). The
coder's scope is defined as the **complement** of the other two: coder can
write any file in the repository *except* paths owned by the tester, the
documenter, or the pipeline itself (`.egg-state/`). Tester and documenter
scopes are defined positively (the files each role owns). If tester's or
documenter's owned scope grows in a later change, the coder's blocklist in
`shared/egg_restrictions/patterns.py` **must be updated in parallel** to
preserve the complement invariant. The same blocklist is mirrored in
`.egg/phase-permissions.json` and `shared/egg_container/__init__.py::_IMPLEMENT_READONLY_DIRS`;
[#1903][issue-1903] tracks unifying those three surfaces behind a single
source of truth. Until that follow-up lands, a `TODO(#1903)` comment marks
each surface so reviewers know to keep them in sync.

[issue-1901]: https://github.com/jwbron/egg/issues/1901
[issue-1903]: https://github.com/jwbron/egg/issues/1903

### `coder`

**Purpose**: Write code, create commits, push to the worktree branch.

**File access**:
- Allowed writes: **any file in the repository EXCEPT** paths owned by the
  `tester`, the `documenter`, or the pipeline (`.egg-state/`). The coder's
  scope is the complement of the other two producer roles in this phase,
  so source code, configuration, shell scripts, build files, top-level
  dotfiles, and extensionless scripts (e.g. `bin/egg`, `sandbox/egg`,
  `sandbox/bin/egg-health-inspect`) are all coder-writable by default.
- Blocked: `docs/`, `**/README.md`, `**/*.md` (documenter's scope);
  `tests/`, `test/`, `**/tests/`, `**/test/`, all test file patterns
  (`**/*_test.py`, `**/test_*.py`, `**/*_test.go`, `**/test_*.go`,
  `**/*.test.{ts,tsx,js,jsx}`, `**/*.spec.{ts,tsx,js,jsx}`),
  `**/conftest.py` (tester's scope); `.egg-state/` (pipeline state);
  plus defense-in-depth blocks on `.github/` (CI workflows and
  CODEOWNERS — preserves the branch-protection invariant) and
  `sandbox/scripts/` (gateway credential shims — preserves the
  credential-routing invariant).
- Block exemptions (always writable, overriding the blocks above):
  `.egg-state/agent-outputs/` (coder's handoff output),
  `.egg-state/agent-anchors/` (per-agent anchor state, scoped by
  `check_anchor_write_permission`),
  `skills/` (skill definitions are functional code),
  `sandbox/agent-config/rules/*.md` and
  `sandbox/agent-config/commands/*.md` (Claude Code agent config
  represented as markdown — functional code, not documentation).

**Outputs**:
- Commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-coder-output.json` — Handoff data

**Directed coordination**: When role boundaries prevent the coder from pushing certain file types (e.g., test files, documentation), use `egg-orch message send --to <role> --type HANDOFF` to notify the responsible agent with file paths, commit SHAs, and guidance. See [Directed Coordination](../guides/concurrent-execution.md#directed-coordination) for details and a worked coder→tester example.

**Prompt context**: Plan document, summarized background.

### `tester`

**Purpose**: Find gaps in the implementation, write and run tests, run linters and type checkers, and report issues for the coder to fix.

**File access**:
- Owned scope (allowed writes): **test files and test infrastructure only.**
  Specifically: `tests/`, `test/`, `**/tests/`, `**/test/` directories;
  all test file patterns — `**/*_test.py`, `**/test_*.py`,
  `**/*_test.go`, `**/test_*.go`,
  `**/*.test.{ts,tsx,js,jsx}`, `**/*.spec.{ts,tsx,js,jsx}`;
  `**/conftest.py`; plus the tester-relevant pin files
  `.python-version`, `**/*.lock`, `**/requirements*.txt`; and
  `.egg-state/agent-outputs/`.
- Blocked: `docs/`, `**/README.md`, `**/*.md` (documenter's scope) and
  `.egg-state/contracts/` (pipeline state). Source code and config
  files outside tests are out of scope by definition — the coder owns
  them (everything not listed in this section or the documenter's).

**Outputs**:
- Test file commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-tester-output.json` — Handoff data (includes lint/type-check results and gaps found)

**Directed coordination**: The tester may receive `HANDOFF` messages from the coder when role boundaries prevent the coder from pushing test files. On receiving a HANDOFF, sync the worktree (`git fetch origin && git merge origin/<branch> --no-edit`), review the coder's guidance, and create the test files. Acknowledge via a `STATUS` or `PROGRESS` message back. See [Directed Coordination](../guides/concurrent-execution.md#directed-coordination).

**Prompt context**: Summarized background, coder handoff data, task list.

### `documenter`

**Purpose**: Update documentation and READMEs.

**File access**:
- Owned scope (allowed writes): **documentation and markdown only.**
  Specifically: the `docs/` tree, every `**/*.md` file (including
  `**/README.md`), and `.egg-state/agent-outputs/`.
- Blocked: all source and implementation file extensions (`**/*.py`,
  `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`,
  `**/*.rb`, `**/*.rs`), all test directories (`tests/`, `test/`,
  `**/tests/`, `**/test/`), and `.egg-state/contracts/`. Source-code
  markdown that is functional (e.g. `sandbox/agent-config/rules/*.md`,
  `sandbox/agent-config/commands/*.md`, `skills/**/*.md`) is owned by
  the coder via block exemptions, not the documenter.

**Outputs**:
- Documentation commits on the worktree branch
- `.egg-state/agent-outputs/{identifier}-documenter-output.json` — Handoff data

**Directed coordination**: The documenter may receive `STATUS` or `PROGRESS` messages from the coder about API changes, new features, or breaking changes that require documentation updates. When the code diff is ambiguous, first read the coder's proposal summary + commit messages (they are the intended documentation of intent); if still unclear, wait for the reviewer pass to raise the ambiguity as a `NACK` rationale (which the coder addresses on re-propose) rather than sending a free-form peer question. The `QUESTION` type was removed in [#1897](https://github.com/jwbron/egg/issues/1897) because it had no reliable respondent. See [Directed Coordination](../guides/concurrent-execution.md#directed-coordination).

**Prompt context**: Summarized background, task list, pointers to relevant docs.

### `reviewer_code`

**Purpose**: Security review, correctness, code quality, test coverage, and documentation quality.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: All source, docs, tests, contracts, drafts

**Subagent fan-out**: On large diffs (`files_changed > 10` OR `loc_added + loc_removed > 500`), `reviewer_code` fans out into Claude Agent SDK subagents — one per implement-phase task partition (capped at 6, with a 5-minute / 300-second per-subagent wall-clock timeout that NACKs the partition on overrun). Each subagent reviews its slice; the parent aggregates findings and emits the single ACK/NACK. A mandatory cross-partition consistency pass runs regardless of whether fan-out fires. Fan-out can be forced sequential via `phase_configs.implement.reviewer_code.parallel = false` (default: `true`).

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_code-review.json` — Verdict file

### `reviewer_code_holistic`

**Purpose**: Single-pass holistic code review focused on cross-module coherence. Runs alongside `reviewer_code`'s slice-by-slice fan-out — its job is the architectural-coherence question no fan-out slice owns.

**Criticality**: CRITICAL — NACKs block consensus on their own and are not averaged against `reviewer_code`'s fan-out ACKs.

**Focus areas** (four mandatory passes):
1. Walk the primary advertised use case end-to-end across the full diff.
2. Cross-check doc-claimed behaviour against what the code actually does.
3. Audit synthetic keys, sentinels, and magic values for cross-module agreement.
4. Hunt silent fallbacks that swallow operator-visible misconfiguration.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: All source, docs, tests, contracts, drafts

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_code_holistic-review.json` — Verdict file

### `reviewer_contract`

**Purpose**: Verify acceptance criteria are met and all tasks are marked complete in the contract.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`, `.egg-state/contracts/`
- Blocked: All source, docs, tests, drafts

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_contract-review.json` — Verdict file

### `reviewer_security`

**Purpose**: ADVISORY security-lens reviewer. Focuses exclusively on cross-file security invariants that a general code reviewer may miss: cross-file allowlist mismatches, handler-vs-validator path mismatches, information-disclosure and authorization-bypass patterns, uncommitted-artifact/Dockerfile-symlink mismatches, secret leakage, and OWASP top-10 patterns spanning multiple changed files.

**Criticality**: ADVISORY — NACKs block consensus informally but do not deadlock BRC until severity-tagged NACK signalling lands. Promotion to CRITICAL is intentionally deferred.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: All source, docs, tests, contracts, drafts

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_security-review.json` — Verdict file

### `reviewer_concurrency`

**Purpose**: ADVISORY concurrency-lens reviewer. Focuses exclusively on concurrency invariants: race conditions, deadlocks, shared-state mutation without synchronization, async-context leakage, retry-storm patterns, resource-cleanup ordering bugs, and BRC-protocol invariants (send→wait ordering, cursor threading, heartbeat-stall windows).

**Criticality**: ADVISORY — same deferral rationale as `reviewer_security` above.

**File access**:
- Allowed writes: `.egg-state/reviews/`, `.egg-state/agent-outputs/`
- Blocked: All source, docs, tests, contracts, drafts

**Outputs**:
- `.egg-state/reviews/{identifier}-implement-reviewer_concurrency-review.json` — Verdict file

## Utility Roles

### `autofixer`

**Category**: Utility

**Purpose**: Automatically fix lint errors, formatting issues, and type-check failures in source and config files. Runs on-demand to clean up code without manual intervention.

**File access**:
- Allowed writes: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `**/*.sh`, `**/*.yml`, `**/*.yaml`, `**/*.json`, `**/*.toml`, `Makefile`, `**/Makefile`, `Dockerfile`, `**/Dockerfile`, `.python-version`, `.node-version`, `.nvmrc`, `.gitignore`, `.gitattributes`, `.editorconfig`, `**/*.lock`, `**/requirements*.txt`, `.egg-state/agent-outputs/`
- Blocked: `docs/`, `**/*.md`, `.egg-state/contracts/`

**Outputs**:
- Commits with auto-fix changes on the worktree branch
- `.egg-state/agent-outputs/{identifier}-autofixer-output.json` — Summary of fixes applied

### `conflict_resolver`

**Category**: Utility

**Purpose**: Resolve merge conflicts, inter-agent file conflicts, and coordination issues across concurrent agents. Can write to source, test, doc, and config files to mediate overlapping changes.

**File access**:
- Allowed writes: `**/*.py`, `**/*.ts`, `**/*.tsx`, `**/*.js`, `**/*.jsx`, `**/*.go`, `**/*.java`, `**/*.rb`, `**/*.rs`, `**/*.sh`, `**/*.yml`, `**/*.yaml`, `**/*.json`, `**/*.toml`, `Makefile`, `**/Makefile`, `Dockerfile`, `**/Dockerfile`, `Procfile`, `.python-version`, `.node-version`, `.nvmrc`, `.gitignore`, `.gitattributes`, `.editorconfig`, `**/*.lock`, `**/requirements*.txt`, `tests/`, `test/`, `**/tests/`, `**/test/`, `docs/`, `**/*.md`, `.egg-state/agent-outputs/`
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

**Purpose**: Pipeline health monitoring agent that detects and responds to agent failures, stalls, loops, off-track behavior, and infrastructure errors. Uses a two-sub-tier LLM architecture: Haiku classifiers for anomaly detection (including infrastructure error identification) and Sonnet/Opus decision-makers for corrective action. Infrastructure errors (git failures, gateway errors, permission denied) are fast-pathed directly to HITL escalation, bypassing the normal nudge/redirect ladder. With [#1962](https://github.com/jwbron/egg/issues/1962), an Opus 4.6 advisor (the Tier-2 decision tier) is invoked **only when** Haiku flags an anomaly **and** a Tier-1 health alert is active simultaneously — see [Advisor Gate](../guides/pipeline-health-monitoring.md#advisor-gate).

**Lifecycle**: Phase-scoped. Auto-spawned at the start of each pipeline phase (when `overseer_enabled` is true in `PipelineConfig`) and torn down when the phase completes, advances, or fails. Each phase gets a fresh overseer instance with no accumulated state from prior phases.

**File access**:
- Allowed writes: `.egg-state/oversight/` (structured oversight logs, dedup state, per-agent timing). The two state files are owned by the overseer:
  - `.egg-state/oversight/filed-issues.jsonl` — append-only JSON Lines record of recommended/filed/skipped issue filings (intra-phase dedup fast path; cross-phase fallback uses `gh issue list --search "{anomaly_signature[:8]}"`). Schema at `egg_overseer.state.FiledIssueRecord`; helpers `load_filed_issues` / `append_filed_issue` (header-on-first-create); `append_filed_issue` acquires an `fcntl.LOCK_EX` flock on its per-state-file sentinel `filed-issues.jsonl.lock` (computed by `_lock_path_for(path) = path.parent / f"{path.name}.lock"`) so concurrent overseer respawns cannot race on the append.
  - `.egg-state/oversight/agent-timing.json` — per-agent phase-entered timestamps and per-anomaly suppression state migrated from `/sdlc`'s in-memory map. Schemas at `egg_overseer.state.AgentTimingState` / `AgentTimingEntry`; read/modify/write is `fcntl.LOCK_EX`-guarded by its own per-state-file sentinel `agent-timing.json.lock` (independent of the JSONL lock above). Helpers `load_agent_timing` / `save_agent_timing` (atomic tmp+rename) and `load_filed_issues` / `append_filed_issue` (header-on-first-create) live in the same module.
- Blocked: All source code, tests, docs, configs, contracts, drafts, reviews

**Required environment variables**:
- `EGG_PIPELINE_REPO` (`owner/repo` format) — injected by the orchestrator at spawn time. Distinct from `EGG_REPO_PATH` (filesystem path). The CLI verb `egg-orch overseer file-issue` sets `--repo $EGG_PIPELINE_REPO` on every `gh issue create`; the gateway cross-checks the `--repo` argument against this env var and rejects mismatches. The sandbox `entrypoint.py` raises if the variable is missing — a misconfigured pipeline failing fast is preferred over one that silently files an issue against the wrong repo.

**Access**:
- Orchestrator APIs: pipeline status, container logs, progress queries, health alerts, message bus
- Sandbox CLI verb: `egg-orch overseer consult-advisor` (handler at `sandbox/egg_lib/orch_cli.py::cmd_overseer_consult_advisor`; calls `egg_overseer.advisor.consult_advisor()` directly so the underlying `run_agent_async` Opus call lives sandbox-side, on the LLM-execution side of the EGG200 boundary documented in `docs/guides/agent-mode-design.md`)
- GitHub API: `gh issue create` for diagnostic issue filing — gateway-mediated. Guardrails are codified in `gateway.agent_restrictions.check_overseer_gh_issue_create`: overseer-role-only, `--repo` must equal `$EGG_PIPELINE_REPO`, `agent:overseer` + priority labels auto-injected if missing, title ≤ 120 chars, body ≤ 50 KB, defense-in-depth secret scan via `egg_overseer.scrubbing.find_secret_kinds`. (As of issue [#1962](https://github.com/jwbron/egg/issues/1962) the function is defined and unit-tested; final wiring into the live `gh` request path is part of the same PR — verify on the merged commit before relying on the gateway-side enforcement.)
- `egg-orch message send` to redirect individual agents
- `egg-orch overseer alert` to broadcast `OVERSEER_ALERT` notifications to the human operator (always uses `message_type=OVERSEER_ALERT` and `to_role=all`)
- `egg-orch overseer file-issue` to file a GitHub issue once a HITL approval has resolved the recommendation. Required flags: `--anomaly-type`, `--priority` (`p0|p1|p2|p3`), `--agent-role`, `--anomaly-signature` (16-hex), `--issue-title-file`, `--issue-body-file`. Optional: `--parent-alert-message-id`, `--dry-run`. The verb runs `find_existing_issue(...)` first and skips `gh` if a dedup match is found.

**Blocked from**:
- All git operations (no repo volume mounted)
- `gh pr merge`, `gh pr create`
- `egg-orch phase advance`, `egg-orch phase complete`
- Direct agent restart (must go through HITL decision queue)
- Cross-repo `gh issue create` (gateway enforces `--repo == $EGG_PIPELINE_REPO`)

**Outputs**:
- Redirect messages to stalled/off-track agents
- HITL escalation requests for agent restarts and infrastructure errors
- `OVERSEER_ALERT` messages, optionally carrying top-level `recommendation="file_issue"` + `recommendation_payload={issue_title, issue_body, priority, anomaly_signature}` for the HITL approval flow (top-level `schema_version=2`; backwards-compatible — `Message.to_dict()` omits the three new fields when unset, so legacy `OVERSEER_ALERT` consumers see byte-identical JSON)
- Autonomous GitHub issues with structured diagnostics (labeled `agent:overseer` + matching priority `p0`/`p1`/`p2`/`p3`) — only after HITL approval; never bypassed
- Pipeline health summary at completion
- Structured oversight logs in `.egg-state/oversight/`

**Prompt context**: Orchestrator health alerts, structured progress events, agent container logs, pipeline state.

See [Pipeline Health Monitoring Guide](../guides/pipeline-health-monitoring.md) for full details.

## Role-Aware Task Assignment

Plan generation assigns tasks to specific execution roles based on file access restrictions. Each task in the YAML appendix can include an optional `role` field (`coder`, `tester`, or `documenter`) that indicates which agent should own the task.

### How It Works

1. **Plan generation**: The `task_planner` prompt includes file restriction information for each execution role (sourced from `get_file_patterns()`). The planner uses this to assign each task to the role permitted to modify the task's files.
2. **Contract propagation**: The `role` field flows through the YAML schema → `ParsedTask` → contract `Task` model, preserving the assignment from plan to execution.
3. **Implement-phase filtering**: When building agent prompts for the implement phase, `_build_role_context()` filters tasks by `task.role` so each agent only sees its own tasks:
   - **Coder**: sees tasks with `role: coder` plus any unassigned tasks (`role: null`)
   - **Tester**: sees only tasks with `role: tester`
   - **Documenter**: sees only tasks with `role: documenter`

### Assignment Rules

Tasks are assigned based on the files they modify:

| File pattern | Assigned role |
|-------------|---------------|
| `**/*.py`, `**/*.ts`, `**/*.js`, config files | `coder` |
| `tests/`, `**/test_*.py`, `**/*.test.ts` | `tester` |
| `docs/`, `**/*.md`, `**/README.md` | `documenter` |
| Mixed (spans multiple roles) | Split into sub-tasks per role |

### Validation

The YAML schema restricts the `role` field to the enum values `coder`, `tester`, and `documenter`. The plan parser also validates role values at parse time — invalid roles generate a parse warning and are treated as unassigned (`null`).

### Backward Compatibility

The `role` field is optional. Role-based task filtering only activates when **at least one task** in the phase has an explicit `role` assignment. Legacy plans (where all tasks have `role: null`) show all tasks to all agents, fully preserving prior behavior. When filtering is active, unassigned tasks (`role: null`) fall through to the coder as the default execution role.

### Example

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Implement
    tasks:
      - id: TASK-1-1
        description: "Add validation logic to auth module"
        acceptance: "Auth validates tokens correctly"
        role: coder
        files:
          - src/auth/validator.py
      - id: TASK-1-2
        description: "Add unit tests for auth validation"
        acceptance: "Tests cover token validation edge cases"
        role: tester
        files:
          - tests/test_auth_validator.py
      - id: TASK-1-3
        description: "Update auth module README"
        acceptance: "README documents new validation behavior"
        role: documenter
        files:
          - src/auth/README.md
```

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
| `implementer` | `tasks[].commit`, `tasks[].notes`, `tasks[].files_affected`, `tasks[].files_affected.*`, `tasks[].status`^1^, `phases[].commit`, `phases[].status`^1^, `decisions[]`^3^, `feedback`^1^, `feedback.*`^1^ |
| `reviewer` | `tasks[].status`^1^, `phases[].status`^1^, `phases[].review_feedback`, `phases[].review_feedback.*`, `acceptance_criteria[].verified`, `current_phase`, `feedback`^1^, `feedback.*`^1^ |
| `human` | `decisions[].resolved`, `decisions[].resolution`, `decisions[].resolved_by`, `decisions[].resolved_at`, `feedback.submitted`^2^, `feedback.submitted_by`^2^, `feedback.submitted_at`^2^, all other fields |
| `system` | Structural fields (`issue`, `schemaVersion`) |

^1^ **Shared ownership**: `tasks[].status`, `phases[].status`, `feedback`, and `feedback.*` are writable by both `implementer` and `reviewer`. Implementer agents mark tasks/phases done during implementation; reviewers can validate or override during review. Both roles can read and write feedback fields during their respective workflow phases.

^2^ **Precedence**: Although `feedback.*` grants implementer/reviewer access to nested feedback fields, these specific subfields (`feedback.submitted`, `feedback.submitted_by`, `feedback.submitted_at`) are human-only. `get_field_owner()` in `roles.py` resolves this via exact-match-first precedence — exact paths always win over wildcard patterns.

^3^ **Precedence**: Same as ^2^ — `decisions[]` grants implementer access to create decisions, but `decisions[].resolved`, `decisions[].resolution`, `decisions[].resolved_by`, and `decisions[].resolved_at` are human-only. Exact paths win over wildcard patterns.

The gateway accepts both coarse roles (`implementer`, `reviewer`) and the fine-grained `AgentRole` values stored in agent session metadata (`coder`, `refiner`, `reviewer_code`, etc.). Fine-grained roles are mapped to their coarse equivalent via `AGENT_ROLE_TO_CONTRACT_ROLE` before field-ownership checks are applied.

## Role Registry (Source of Truth)

All agent roles are defined in a single canonical location: `shared/egg_contracts/agent_roles.py`. This module provides:

- **`AgentRole`** — `StrEnum` with all role identifiers
- **`AgentCategory`** — `StrEnum` categorizing roles (EXECUTION, ANALYSIS, REVIEW, UTILITY, INTERFACE)
- **`AgentRoleDefinition`** — Dataclass combining role, description, responsibilities, dependencies, file access, and category
- **`AGENT_ROLES`** — Registry mapping each `AgentRole` to its definition
- **`AGENT_ROLE_TO_CONTRACT_ROLE`** — Mapping from each fine-grained `AgentRole` to its coarse `Role` (e.g., `coder` → `implementer`, `reviewer_code` → `reviewer`); used by the gateway to authorize contract mutations
- **`get_role_definition(role)`** — Look up a role's full definition
- **`get_contract_role(role)`** — Translate a fine-grained `AgentRole` to its coarse contract `Role`; returns `None` for unknown roles
- **`get_roles_by_category(category)`** — Query all roles in a given category
- **`get_roles_for_phase(phase)`** — Get roles assigned to a pipeline phase
- **`detect_write_overlaps(roles)`** — Find file access conflicts between parallel roles
- **`get_file_patterns(role_value)`** — Return `{"allowed": [...], "blocked": [...]}` write patterns for a role, or `None` if not defined

Other modules (`orchestrator/models.py`, `shared/egg_orchestrator/types.py`, `shared/egg_restrictions/patterns.py`) import `AgentRole` from this canonical source rather than defining their own copies. The `egg_restrictions` re-export means the gateway sees the same enum instance, eliminating the silent-drift failure mode that existed when `egg_restrictions` defined its own parallel class.

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

Agent file restrictions are enforced at multiple layers:

| Layer | What it does | Type |
|-------|-------------|------|
| **SDK tool interception** | Rejects `Write`, `Edit`, and `NotebookEdit` to disallowed paths before execution | Soft — saves tokens, drives delegation |
| **Per-agent worktrees** | Isolates agents' working directories so they can't overwrite each other | Structural — prevents stomping |
| **Gateway push restricted-path rejection** | Rejects any push whose own-authored files include a path the pushing role cannot write; pulled cross-role commits never block the push ([#2039](https://github.com/jwbron/egg/issues/2039)) | Hard — security boundary |

### SDK Tool Interception (Soft Enforcement)

The Agent SDK (`egg_agent`) intercepts file write operations (`Write`, `Edit`, `NotebookEdit`) before execution and checks them against the role's `AgentFilePattern`. If the file is outside the agent's allowed patterns, the SDK's `can_use_tool` callback returns a `PermissionResultDeny` — the tool call is blocked and the error message is returned to the LLM as a tool result. The error message identifies which role owns the target file (e.g., "this file belongs to the 'documenter' role"), helping the agent redirect its work rather than retry.

This prevents agents from wasting context window on out-of-scope work. Without this interception, an agent could spend significant tokens writing files it can never push, only to discover the restriction at push time (#1527).

**Scope:** Only `Write`, `Edit`, and `NotebookEdit` are intercepted. `Bash` is not intercepted because reliably parsing file writes from shell commands is impractical. Any writes that slip through Bash are caught by gateway push validation.

**Availability:** Tool interception is only active in the headless Agent SDK (`egg_agent`) when `EGG_AGENT_ROLE` is set (pipeline mode). The interactive `claude` CLI is not affected. Interception can be disabled per-invocation by passing `intercept_tools=False` to `run_agent_async()`.

**Implementation:** `shared/egg_agent/tool_interceptor.py` contains the `check_file_write_permission()` function. It normalizes the absolute file path to a repo-relative path, then calls `check_agent_file_access()` from `egg_restrictions`. The callback is registered via the `can_use_tool` parameter on `ClaudeAgentOptions`.

### Gateway Push Restricted-Path Rejection (Hard Enforcement)

As of [#2039](https://github.com/jwbron/egg/issues/2039), the gateway **rejects** any push whose own-authored files include a path the pushing role cannot write. The push is rejected with `403 restricted_path_modified` carrying `role`, `blocked_paths`, `recommended_action`, `doc_ref`, `pulled_commits`, and `attribution_fallback` (the last flag tells `push-recovery.md` whether to retry vs escalate). The agent's recovery is to drop the offending edits and re-propose with `--pre-merge-condition` per the conditional-ACK pattern ([#1998](https://github.com/jwbron/egg/issues/1998)). Pulled cross-role commits (attributed to another role via the commit-authorship registry) never block the push. See [Gateway Auto-Filter Architecture](../architecture/gateway-auto-filter.md) for the historical auto-filter design and the commit-authorship registry that still backs attribution.

**Kill switch:** `EGG_AGENT_RESTRICTIONS_ENFORCE=false` falls back to warn-only plain push.

**Non-agent-role restrictions keep 403.** Phase / anchor scope / protected-file / branch-ownership / private-mode / concurrent-mode checks still return `403 Push denied`.

The gateway's `get_attributed_changed_files_in_push()` walks the unpushed range via `rev-list` + `diff-tree` per commit, then does a single bulk `lookup_bulk` against the commit-authorship registry to tag each file with its authoring role (or `None` if unregistered — treated as own-authored per the fail-closed invariant).

For the exact allowed and blocked patterns per role, see `shared/egg_restrictions/patterns.py` (canonical source). The gateway imports from this shared package for push-time validation.

## Per-Agent Git Identity

Each agent commits with a role-scoped author for auditability:

| Role | Git Author | Git Email |
|------|-----------|-----------|
| `coder` | `egg (coder)` | `coder@egg.local` |
| `tester` | `egg (tester)` | `tester@egg.local` |
| `documenter` | `egg (documenter)` | `documenter@egg.local` |
| `reviewer_code` | `egg (reviewer_code)` | `reviewer_code@egg.local` |
| *(any role)* | `egg (<role>)` | `<role>@egg.local` |

This is set automatically by the sandbox entrypoint using the `EGG_AGENT_ROLE` environment variable. If the variable is not set, the default `egg <egg@localhost>` is used.

> **Note:** The git identity is used for display in `git log` only. Authoritative commit attribution for push-time file-restriction enforcement comes from the **commit-authorship registry**, which is populated inline by the gateway's `/api/v1/git/execute` observer using the session's role — not from `commit.author_email`. A compromised sandbox cannot forge another role's authorship by overriding the `user.email` config, because the gateway records who authored each commit based on which session token created it. See [Gateway Auto-Filter Architecture](../architecture/gateway-auto-filter.md#why-a-commit-authorship-registry).

## Related Documentation

- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — Phase execution and agent orchestration
- [Concurrent Execution Guide](../guides/concurrent-execution.md) — BRC consensus protocol
- [Agent Development Guide](../guides/agent-development.md) — How to add new agent roles
- [Architecture Overview](../architecture/README.md) — Role-based access control
