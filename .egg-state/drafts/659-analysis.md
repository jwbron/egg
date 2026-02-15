# Analysis: Tune claude.md files and prompts

> Issue: #659 | Phase: refine

## Problem Statement

The egg platform's prompt and CLAUDE.md system has grown organically across two parallel execution environments (local orchestrator and GitHub Actions) without a unified prompt generation strategy. This creates three problems:

1. **Token waste**: Every agent receives all 468 lines of CLAUDE.md rules regardless of role, wasting ~25-30% of the rules budget on irrelevant content per invocation.
2. **Prompt drift**: Seven GitHub Actions prompt builders (bash scripts) and five orchestrator prompt builders (Python functions) have diverged, producing inconsistent agent behavior for equivalent tasks.
3. **Architectural split**: Prompt generation logic lives in two places (bash scripts in `action/` and Python functions in `orchestrator/routes/pipelines.py`) with no shared code, making maintenance and consistency difficult.

The goal is to audit, consolidate, and optimize prompts so that: (1) quality and correctness are preserved or improved, and (2) token efficiency is improved by trimming irrelevant content and eliminating duplication.

## Current Behavior

### CLAUDE.md Assembly

All agents receive the same monolithic CLAUDE.md, assembled at container startup from 7 rule files in `sandbox/.claude/rules/`:

| File | Lines | Primary Audience |
|------|-------|-----------------|
| `mission.md` | 135 | CODER (workflow, git, PRs, commit attribution) |
| `environment.md` | 71 | All (sandbox constraints, network, gateway) |
| `code-standards.md` | 10 | All (tech stack, linting) |
| `test-workflow.md` | 16 | TESTER, CODER |
| `pr-descriptions.md` | 20 | CODER only |
| `contract.md` | 71 | Pipeline agents only (egg-contract CLI reference) |
| `orchestrator.md` | 76 | Pipeline agents only (egg-orch CLI reference) |

Assembly happens in `sandbox/entrypoint.py:678-717` (`setup_agent_rules()`). There is **no role-based filtering** -- the function concatenates all files in a fixed order and writes one `~/CLAUDE.md` that Claude Code automatically loads.

### Prompt Generation: Two Parallel Systems

**Local Orchestrator** (Python, `orchestrator/routes/pipelines.py`):
- `_build_phase_prompt()` (line 1280) -- CODER/REFINER phase instructions
- `_build_review_prompt()` (line 1064) -- reviewer verdict instructions
- `_build_agent_prompt()` (line 1594) -- role-specific instructions (TESTER, DOCUMENTER, INTEGRATOR, ARCHITECT, TASK_PLANNER, RISK_ANALYST)
- `_build_checker_prompt()` (line 2190) -- test/lint runner
- `_build_autofix_prompt()` (line 2267) -- fix failing checks

**GitHub Actions** (Bash scripts, `action/`):
- `build-review-prompt.sh` -- PR code review
- `build-agent-mode-design-review-prompt.sh` -- design pattern review
- `build-autofixer-prompt.sh` -- fix failing PR checks
- `build-conflict-prompt.sh` -- merge conflict resolution
- `build-contract-verification-prompt.sh` -- contract compliance
- `build-doc-updater-prompt.sh` -- documentation updates
- `build-feedback-prompt.sh` -- address review feedback

### Key Differences Between the Two Systems

| Aspect | Local Orchestrator | GitHub Actions |
|--------|-------------------|----------------|
| Language | Python functions | Bash scripts |
| Rules source | Hardcoded inline | Loads from `.egg/*-rules.md` files |
| Output | Prompt string to Claude CLI | Prompt file path to `$GITHUB_OUTPUT` |
| Review output | JSON verdict file | PR review comments |
| Shared code | None | None |

### Overlap Analysis

| Agent Type | Orchestrator | GitHub Actions | Same Logic? |
|------------|-------------|----------------|-------------|
| Autofixer | `_build_autofix_prompt()` | `build-autofixer-prompt.sh` | Mostly -- core categories match, GA has better rules lookup |
| Reviewer | `_build_review_prompt()` | `build-review-prompt.sh` | Partially -- rules similar, output format differs (JSON vs comments) |
| Contract verifier | via `_build_review_prompt(reviewer_type="contract")` | `build-contract-verification-prompt.sh` | Partially -- GA has dedicated flow |
| Conflict resolver | **None** | `build-conflict-prompt.sh` | N/A -- gap in orchestrator |
| Feedback addresser | **None** | `build-feedback-prompt.sh` | N/A -- gap in orchestrator |
| Doc updater | **None** | `build-doc-updater-prompt.sh` | N/A -- gap in orchestrator |
| Design reviewer | **None** | `build-agent-mode-design-review-prompt.sh` | N/A -- gap in orchestrator |

## Constraints

- **Claude Code auto-loads CLAUDE.md**: There is no CLI flag to select a custom system prompt file. Claude Code reads `~/CLAUDE.md` automatically on startup. Any role-based filtering must happen before this file is written.
- **Container startup is per-agent**: Each agent gets its own container, so `setup_agent_rules()` runs independently per agent. This means role-based CLAUDE.md generation is technically feasible -- the `EGG_AGENT_ROLE` environment variable is available at container startup.
- **Backward compatibility**: The `action/` bash scripts are used by consumer repos that `uses: jwbron/egg/action@main`. Changes must not break external consumers.
- **Quality first, then efficiency**: The issue explicitly prioritizes correctness over token savings. Prompt consolidation must not degrade agent behavior.
- **Multi-agent architecture**: Agents check each other (testers check coders, reviewers check workers). Prompts should reinforce role boundaries, not blur them.

## Options Considered

### Option A: Role-Aware CLAUDE.md Assembly

**Approach**: Modify `setup_agent_rules()` in `sandbox/entrypoint.py` to read `EGG_AGENT_ROLE` and include only relevant rule files per role. Keep the existing modular rule file structure but add a role-to-rules mapping.

Example mapping:
- **CODER**: mission.md, environment.md, code-standards.md, test-workflow.md, pr-descriptions.md
- **TESTER**: environment.md, code-standards.md, test-workflow.md
- **REVIEWER**: environment.md, code-standards.md, pr-descriptions.md
- **ARCHITECT/TASK_PLANNER/RISK_ANALYST**: environment.md, code-standards.md

Contract.md and orchestrator.md would be conditionally included only when `EGG_PIPELINE_ID` is set (pipeline mode).

**Pros**:
- Directly reduces token waste per invocation (~25-30% reduction for non-CODER roles)
- Simple implementation -- mapping dict + env var check in existing function
- Preserves modular rule file structure
- Each agent gets only what's relevant to its responsibilities

**Cons**:
- Requires maintaining a role-to-rules mapping as new roles are added
- Risk of accidentally excluding a rule file a role needs
- Doesn't address the prompt generation code duplication between GHA and orchestrator

### Option B: Unified Prompt Generator Module (Python)

**Approach**: Create a shared Python module (e.g., `shared/prompt_builders/`) that generates all prompts for all roles. Both the orchestrator and GitHub Actions would call into this module. The GHA bash scripts would be replaced with thin Python wrappers that the action calls.

**Pros**:
- Single source of truth for all prompt logic
- Eliminates divergence between GHA and orchestrator prompts
- Easier to test (Python unit tests vs bash script testing)
- Natural place to implement role-based CLAUDE.md content too
- Fulfills the issue requirement: "ensure we're using the exact same prompt generators for each flow"

**Cons**:
- Larger refactor -- all 7 bash scripts must be rewritten
- GitHub Actions environment is different from sandbox (no container, different env vars)
- Must ensure the shared module works in both contexts (sandbox container vs GHA runner)
- Risk of regression during migration

### Option C: Trim and Deduplicate Only (Minimal Change)

**Approach**: Keep the two-system architecture but trim each file for token efficiency: remove README.md from assembly, deduplicate git/worktree/gateway content between mission.md and environment.md, move non-interactive mode instructions to an orchestrator-only injection, and align the bash scripts' default rules with the orchestrator's inline rules.

**Pros**:
- Lowest risk -- no architectural changes
- Quick to implement
- Still achieves meaningful token savings (~15-20% reduction)

**Cons**:
- Does NOT address the fundamental problem of prompt divergence
- Ongoing maintenance burden of two parallel systems
- Does not fulfill the issue requirement to "ensure we're using the exact same prompt generators"

### Option D: Hybrid -- Role-Aware Assembly + Shared Prompt Module

**Approach**: Combine Options A and B. First, implement role-aware CLAUDE.md assembly (Option A) for token efficiency. Then, create a shared Python prompt generation module that both the orchestrator and GHA use (Option B), moving all prompt generation out of bash scripts and into the sandbox codebase.

**Pros**:
- Addresses both token waste AND prompt divergence
- Single source of truth for prompts
- Role-appropriate CLAUDE.md content
- Matches the issue requirement to move prompt generation "out of github actions and into the sandbox codebase"

**Cons**:
- Largest scope of work
- Requires careful sequencing (CLAUDE.md trimming first, then prompt consolidation)
- Must design the shared module to work in both container and GHA contexts

## Recommended Approach

**Option D: Hybrid -- Role-Aware Assembly + Shared Prompt Module**, implemented in two stages:

**Stage 1 -- CLAUDE.md optimization**: Modify `setup_agent_rules()` for role-aware assembly, deduplicate overlapping content across rule files, remove README.md from assembly, and conditionally include contract.md/orchestrator.md only in pipeline mode. This delivers immediate token savings with low risk.

**Stage 2 -- Prompt consolidation**: Create a `shared/prompt_builders/` Python module with functions for each agent type. Migrate the 7 GHA bash scripts to thin Python wrappers calling the shared module. Update the orchestrator's `_build_*_prompt()` functions to call the same shared module. This ensures both execution paths use identical prompt logic.

**Justification**: The issue explicitly asks for both concerns -- quality/correctness (prompt consistency) and token efficiency (trimming waste). Option D is the only approach that addresses both. The two-stage approach manages risk by separating the quick-win optimization from the larger refactor.

The issue also explicitly states: "all prompt and claude.md generation workflows should be moved out of github actions and moved into the sandbox codebase." This directly maps to Stage 2 of Option D.

## Open Questions

1. **How should GHA workflows invoke the shared Python module?** The sandbox codebase runs in containers, but GHA workflows run on GitHub-hosted runners. Options include:
   - Install the shared module as a pip package in the GHA runner
   - Use the egg Docker image in GHA with a prompt-generation entrypoint
   - Bundle the Python module in the GitHub Action itself

2. **Should roles that don't exist in GHA (ARCHITECT, TASK_PLANNER, RISK_ANALYST) still have their prompts in the shared module?** These currently only run in the orchestrator, but putting them in the shared module would future-proof for GHA expansion.

3. **Should the CLAUDE.md rule files themselves be refactored (split, merged, or rewritten)?** For example, `mission.md` at 135 lines covers workflow, git operations, PR lifecycle, review responses, git safety, decision framework, non-interactive mode, and notifications. Should this be split into smaller, more composable units?

4. **What is the acceptable token budget for CLAUDE.md per agent invocation?** Currently ~3,200 tokens. With optimization, this could drop to ~1,500-2,000 for most roles. Is there a target the team has in mind?

---

*Authored-by: egg*
