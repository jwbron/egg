# Analysis: Tune claude.md files and prompts (#659)

## Problem Statement

The codebase has two parallel prompt systems — bash scripts for GitHub Actions workflows and Python functions for the local orchestrator — that serve the same agent roles (reviewer, autofixer, conflict resolver, contract verifier, etc.) but were developed independently. The issue asks to:

1. Audit all claude.md files and prompts for token waste from frontloaded context
2. Ensure each prompt is appropriate for its task
3. Unify prompt generators so local orchestration and GitHub Actions use the same code

## Current Architecture

### CLAUDE.md System

**Source**: 7 rule files in `sandbox/.claude/rules/` (also duplicated at `sandbox/claude-rules/`)
**Combined output**: `~/CLAUDE.md` (~15.5 KB, 423 lines) — built at container startup by `sandbox/entrypoint.py:setup_agent_rules()`
**Mechanism**: Rules are concatenated in fixed order with `---` separators, then symlinked to `~/repos/CLAUDE.md`

The 7 rule files and their sizes:

| File | Size | Purpose |
|------|------|---------|
| `mission.md` | 5,431 B | Agent role, workflow, PR lifecycle, git safety |
| `environment.md` | 2,841 B | Sandbox capabilities, network modes, gateway |
| `code-standards.md` | 386 B | Tech stack, style, commands |
| `test-workflow.md` | 621 B | Test framework reference |
| `pr-descriptions.md` | 445 B | PR format template |
| `contract.md` | 2,670 B | SDLC contract CLI reference |
| `orchestrator.md` | 3,152 B | Orchestrator CLI reference |
| **Total** | **~15.5 KB** | |

### Slash Commands (`.claude/commands/`)

7 command files (~16 KB total): `coder-mode.md`, `tester-mode.md`, `documenter-mode.md`, `integrator-mode.md`, `onboarding-docs.md`, `sdlc.md`, `show-metrics.md`

These are loaded on-demand when invoked, not frontloaded.

### GitHub Actions Prompt Builders (Bash)

7 bash scripts in `action/`:

| Script | Approx Output Size | Used By |
|--------|-------------------|---------|
| `build-review-prompt.sh` | ~2-3 KB | `on-pull-request.yml` via `reusable-review.yml` |
| `build-autofixer-prompt.sh` | ~1.5 KB | `on-check-failure.yml` via `reusable-autofix.yml` |
| `build-conflict-prompt.sh` | ~5 KB (includes API data) | `on-merge-conflict.yml` via `reusable-conflict-resolve.yml` |
| `build-contract-verification-prompt.sh` | ~2.5 KB | `on-pull-request-contract-verify.yml` |
| `build-agent-mode-design-review-prompt.sh` | ~2 KB | `on-pull-request-agent-mode-design.yml` |
| `build-feedback-prompt.sh` | ~1.5 KB | `on-review-feedback.yml` |
| `build-doc-updater-prompt.sh` | ~4 KB (includes git data) | `on-push-doc-updater.yml` |

Each script also loads conventions from `action/*-conventions.md` files (review, autofixer, conflict — ~280 lines, ~8 KB total).

### Orchestrator Prompt Builders (Python)

Functions in `orchestrator/routes/pipelines.py`:

| Function | Purpose | Equivalent GHA Script |
|----------|---------|----------------------|
| `_build_phase_prompt()` | Phase-specific prompts (refine/plan/implement/pr) | None (SDLC-only) |
| `_build_agent_prompt()` | Role-specific multi-agent prompts | Slash commands (partial) |
| `_build_review_prompt()` | SDLC review with verdict JSON | `build-review-prompt.sh` |
| `_build_checker_prompt()` | Run tests/lint, write results | None (GHA uses CI directly) |
| `_build_autofix_prompt()` | Fix check failures | `build-autofixer-prompt.sh` |
| `_get_code_review_criteria()` | Security/correctness/robustness checklist | Inline in `build-review-prompt.sh` |
| `_get_agent_design_criteria()` | Agent-mode anti-pattern checklist | `build-agent-mode-design-review-prompt.sh` |
| `_get_contract_review_criteria()` | Contract verification checklist | `build-contract-verification-prompt.sh` |

## Findings

### Finding 1: Prompt Duplication Between GHA and Orchestrator (HIGH)

The review criteria, autofixer rules, and contract verification logic exist in two separate implementations:

- **Code review criteria**: GHA uses inline rules in `build-review-prompt.sh` (lines 30-77) while the orchestrator uses `_get_code_review_criteria()` in `pipelines.py` (lines 787-809). Content overlaps ~80% but differs in detail level.
- **Autofixer rules**: GHA uses `build-autofixer-prompt.sh` inline defaults + `autofixer-conventions.md`. Orchestrator uses `_build_autofix_prompt()` with similar but separately maintained rules.
- **Agent-mode design criteria**: GHA has a full bash script (`build-agent-mode-design-review-prompt.sh`, 180 lines) while orchestrator has `_get_agent_design_criteria()` (15 lines) — the orchestrator version is significantly less detailed.
- **Contract verification**: GHA has `build-contract-verification-prompt.sh` (219 lines); orchestrator has `_get_contract_review_criteria()` (20 lines).

**Risk**: Divergence over time. When review rules are updated in one place, the other is forgotten.

### Finding 2: CLAUDE.md Contains Context Not Needed for Every Task (MEDIUM)

The combined CLAUDE.md (~15.5 KB) is loaded for every agent invocation regardless of task type. Some sections are rarely relevant:

- **Orchestrator CLI reference** (`orchestrator.md`, 3.2 KB): Only relevant during SDLC pipeline phases, not for ad-hoc coding tasks or GHA workflows.
- **SDLC Contract reference** (`contract.md`, 2.7 KB): Only relevant during SDLC pipeline phases.
- **PR Description Format** (`pr-descriptions.md`, 445 B): Only relevant during PR creation.
- **Non-Interactive Mode section** in `mission.md`: Only relevant in `--print` mode.

Combined, ~6.3 KB (~40%) of the CLAUDE.md is contextually irrelevant for most agent invocations.

However, CLAUDE.md is only ~15.5 KB total, which is modest. The token cost of the extra context is small (roughly 4K tokens). The real concern is cognitive noise — irrelevant instructions may distract the agent from task-relevant rules.

### Finding 3: Duplicate Rule Directories (LOW)

`sandbox/claude-rules/` and `sandbox/.claude/rules/` contain identical files. The Dockerfile copies from `claude-rules/` while `.claude/rules/` serves as the source-of-truth for the Claude Code `.claude` configuration format. This is confusing but functionally harmless — both are in sync.

### Finding 4: Orchestrator Review Prompts Differ Structurally from GHA (MEDIUM)

The orchestrator `_build_review_prompt()` asks agents to write a JSON verdict file to `.egg-state/reviews/`, while GHA review prompts ask agents to post a GitHub review via `gh pr review`. This is a legitimate architectural difference (local vs GitHub), but the review *criteria* should be shared.

Similarly, the orchestrator review prompt has no equivalent of:
- The `review-conventions.md` file (how to post reviews)
- The re-review delta checking workflow
- The review marker system (`<!-- egg-automated-review -->`)

These are GHA-specific conventions and don't apply to local orchestration, so they're appropriately different.

### Finding 5: Doc-Updater Prompt Pre-fetches Appropriately (OK)

The `build-doc-updater-prompt.sh` pre-fetches changed files, commit messages, diff stats, and related docs. This is appropriate because the doc-updater needs this context upfront to determine if docs need updating. It follows agent-mode design guidelines by keeping pre-fetched data to small summaries (file lists, term matches), not full diffs.

### Finding 6: Conflict Prompt Pre-fetches PR Context (OK)

The `build-conflict-prompt.sh` fetches PR context, commit messages, and review comments via GitHub API. These are small summaries (~2 KB typically) that orient the agent. Appropriate.

### Finding 7: Phase Prompt Has Dead Code (LOW)

In `_build_phase_prompt()` (lines 1224-1239 and 1254-1269), the local and non-local branches for the refine and plan phases produce identical output. The `if is_local:` conditional serves no purpose for these phases.

### Finding 8: Slash Commands Duplicate Orchestrator Agent Prompts (LOW-MEDIUM)

The slash commands (`coder-mode.md`, `tester-mode.md`, etc.) and the orchestrator's `_build_agent_prompt()` function both define role-specific instructions for the same agent roles (coder, tester, documenter, integrator). The slash commands are more detailed (include file access constraints, handoff formats, quality checklists), while the orchestrator versions are more minimal.

## Constraints and Dependencies

1. **GHA and local orchestrator have different output mechanisms**: GHA agents post GitHub reviews; local agents write verdict files. This requires some prompt differences.
2. **GHA prompt scripts must run from a trusted checkout** (security): The `reusable-review.yml` checks out `main` to build prompts, preventing malicious PRs from modifying prompt scripts. Any shared module must be available at that point.
3. **Conventions files are GHA-specific**: `review-conventions.md`, `autofixer-conventions.md`, `conflict-conventions.md` contain GitHub-specific posting instructions that don't apply to local orchestration.
4. **CLAUDE.md must be available without repo mount**: It's baked into the Docker image at build time from `sandbox/claude-rules/`, so it can't depend on the mounted repo.
5. **Backward compatibility**: The `.egg/review-rules.md`, `.egg/autofixer-rules.md`, and `.egg/conflict-rules.md` user-override mechanism must be preserved.

## Options

### Option A: Shared Python Module for Review Criteria

Extract review criteria, autofixer rules, and design-review checklists into a shared Python module in `shared/egg_prompts/`. Both the orchestrator and the GHA bash scripts would use the same source:

- Orchestrator imports directly
- GHA bash scripts call a thin Python CLI (`python -m egg_prompts.criteria --type code-review`)

**Pros**: Single source of truth; easy to test; criteria always in sync
**Cons**: Adds a Python dependency to GHA bash scripts; slightly more complex build

### Option B: Shared Markdown Files as Criteria Source

Create shared markdown files (e.g., `shared/prompts/code-review-criteria.md`) that both systems `cat`/read at runtime:

- Orchestrator: `Path("shared/prompts/code-review-criteria.md").read_text()`
- GHA bash scripts: `cat shared/prompts/code-review-criteria.md`

**Pros**: Simple; language-agnostic; easy to review and edit
**Cons**: No parameterization (can't vary by phase without templating); the trusted checkout in GHA already provides access to `main` branch files

### Option C: Move Prompt Building into the GHA Action Itself

Replace individual bash scripts with a single prompt builder inside the `action/` directory that handles all roles, invoked via `action.yml` inputs:

**Pros**: Centralizes GHA prompt logic
**Cons**: Doesn't solve the duplication with the orchestrator; still two systems

### Option D: Conditional CLAUDE.md Sections

Make the CLAUDE.md assembly context-aware, only including sections relevant to the agent's current task:

- Phase-based: Only include `contract.md` and `orchestrator.md` during SDLC pipeline phases
- Role-based: Only include `pr-descriptions.md` during PR creation

**Pros**: Reduces token waste; less cognitive noise
**Cons**: More complex entrypoint logic; harder to debug; savings are modest (~4K tokens)

## Recommendation

**Pursue Option B (shared markdown criteria) + targeted CLAUDE.md trimming.**

### Rationale

1. **Option B is the simplest path to unification.** Shared markdown files can be read by both bash scripts and Python without additional dependencies. The trusted checkout in GHA workflows already provides access to the full repo, so `cat shared/prompts/code-review-criteria.md` works directly.

2. **Partial CLAUDE.md conditioning is worthwhile.** The `contract.md` and `orchestrator.md` sections (~5.9 KB) should only be included when `EGG_PIPELINE_ID` is set, since they're SDLC-specific. This is a simple conditional in `setup_agent_rules()`.

3. **Eliminate the duplicate `claude-rules/` directory.** The Dockerfile should copy from `.claude/rules/` directly (or just reference the same path), removing the redundant directory.

4. **Clean up the dead code** in `_build_phase_prompt()` where `is_local` branches produce identical output.

5. **Enrich orchestrator agent prompts** with the detail level from slash commands, or have the orchestrator load the slash command files as prompt foundations.

### Specific Files to Create/Modify

**Create** (shared criteria as markdown):
- `shared/prompts/code-review-criteria.md`
- `shared/prompts/autofixer-rules.md`
- `shared/prompts/agent-design-criteria.md`
- `shared/prompts/contract-review-criteria.md`
- `shared/prompts/unified-review-criteria-refine.md`
- `shared/prompts/unified-review-criteria-plan.md`
- `shared/prompts/unified-review-criteria-implement.md`

**Modify** (consume shared criteria):
- `action/build-review-prompt.sh` — replace inline default review rules with `cat shared/prompts/code-review-criteria.md`
- `action/build-autofixer-prompt.sh` — replace inline defaults with `cat shared/prompts/autofixer-rules.md`
- `action/build-agent-mode-design-review-prompt.sh` — replace inline criteria with `cat shared/prompts/agent-design-criteria.md`
- `action/build-contract-verification-prompt.sh` — replace inline rules with `cat shared/prompts/contract-review-criteria.md`
- `orchestrator/routes/pipelines.py` — replace `_get_*_criteria()` functions to read from `shared/prompts/`
- `sandbox/entrypoint.py:setup_agent_rules()` — conditionally exclude `contract.md` and `orchestrator.md` when not in SDLC mode
- `sandbox/Dockerfile` — change COPY source from `sandbox/claude-rules/` to `sandbox/.claude/rules/`

**Remove**:
- `sandbox/claude-rules/` directory (use `.claude/rules/` as single source)

**Clean up**:
- `orchestrator/routes/pipelines.py:_build_phase_prompt()` — remove redundant `is_local` branches for refine/plan phases

## Open Questions

1. **Should the `.egg/review-rules.md` user-override mechanism apply to orchestrator reviews too?** Currently it only works in GHA. If unified, the orchestrator should also check for this file.

2. **Should the orchestrator's agent-design review criteria be expanded to match the GHA version?** The GHA `build-agent-mode-design-review-prompt.sh` has significantly more guidance (review philosophy, what to skip, posting instructions) compared to the orchestrator's terse `_get_agent_design_criteria()`.

3. **Should slash command content be merged with orchestrator `_build_agent_prompt()` output?** The slash commands (e.g., `coder-mode.md`) have richer instructions (file access constraints, handoff format, quality checklist) than the orchestrator's `_build_agent_prompt()`. The orchestrator could load these files as prompt foundations.
