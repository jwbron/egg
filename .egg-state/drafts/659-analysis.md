# Analysis: Tune claude.md files and prompts

> Issue: #659 | Phase: refine

## Problem Statement

The egg system uses a multi-layered prompt architecture to instruct Claude agents across different execution contexts (local SDLC orchestrator, GitHub Actions workflows, standalone sandbox usage). Over time, this has led to:

1. **Token waste from frontloaded context**: The `CLAUDE.md` file (assembled from 7 rule files at container startup) is always loaded in full (~423 lines / ~3,500 tokens) regardless of task type. Rules about SDLC contracts and orchestrator CLI are irrelevant for non-pipeline tasks.
2. **Divergent prompt generators**: The local orchestrator (Python in `orchestrator/routes/pipelines.py`) and GitHub Actions (shell scripts in `action/build-*.sh`) maintain independent implementations of equivalent prompts (review criteria, contract verification, autofixer rules).
3. **Unclear prompt ownership**: Some content lives in `.claude/rules/` files, some in Python string builders, and some in shell scripts—making it hard to know what to update when criteria change.

The issue asks us to audit all CLAUDE.md files and prompts, trim unnecessary frontloaded context, and ensure local and GitHub Actions workflows use the same prompt generators.

## Current Architecture

### Prompt Sources Inventory

The system has **four layers** of prompt content:

| Layer | Location | When Loaded | Token Cost |
|-------|----------|-------------|------------|
| **CLAUDE.md** (rules) | `sandbox/.claude/rules/*.md` → assembled into `~/CLAUDE.md` by `entrypoint.py:setup_agent_rules()` | Always (every Claude session) | ~3,500 tokens |
| **Slash commands** | `sandbox/.claude/commands/*.md` → copied to `~/.claude/commands/` | On invocation only | 0 until used |
| **Action prompt builders** | `action/build-*-prompt.sh` (7 scripts, ~1,530 lines total) | GitHub Actions only | Per-invocation |
| **Orchestrator prompt builders** | `orchestrator/routes/pipelines.py` (`_build_phase_prompt`, `_build_agent_prompt`, `_build_review_prompt`, `_build_autofix_prompt`) | Local SDLC only | Per-invocation |

### CLAUDE.md Assembly

The `entrypoint.py:setup_agent_rules()` function combines 7 rule files in fixed order:

1. `mission.md` (135 lines) — Core agent role, workflow, git operations, PR lifecycle, decision framework
2. `environment.md` (71 lines) — Sandbox constraints, network modes, gateway, filesystem
3. `code-standards.md` (11 lines) — Tech stack, PEP 8, TypeScript conventions
4. `test-workflow.md` (22 lines) — Test execution patterns
5. `pr-descriptions.md` (18 lines) — PR format template
6. `contract.md` (72 lines) — `egg-contract` CLI reference (SDLC-only)
7. `orchestrator.md` (92 lines) — `egg-orch` CLI reference (SDLC-only)

**Problem**: Rules 6-7 (`contract.md`, `orchestrator.md`) total ~164 lines / ~1,200 tokens and are only relevant during SDLC pipeline execution. They waste tokens in standalone sandbox sessions and GitHub Actions bot runs.

### Slash Commands (7 commands)

These load on-demand and are well-scoped:
- `coder-mode.md` (74 lines) — Multi-agent coder role
- `documenter-mode.md` (105 lines) — Multi-agent documenter role
- `integrator-mode.md` (154 lines) — Multi-agent integrator role
- `tester-mode.md` (84 lines) — Multi-agent tester role
- `sdlc.md` (30 lines) — Deprecated redirect to `egg-sdlc` CLI
- `onboarding-docs.md` (122 lines) — Repo documentation generator
- `show-metrics.md` (42 lines) — Activity metrics report

**Issues found**:
- `sdlc.md` is deprecated but still present
- Agent mode commands (coder/documenter/tester/integrator) share significant structural boilerplate that could be templated

### GitHub Actions Prompt Builders (7 scripts)

| Script | Lines | Purpose | Shared Config |
|--------|-------|---------|---------------|
| `build-review-prompt.sh` | 188 | Code review | `.egg/review-rules.md` |
| `build-agent-mode-design-review-prompt.sh` | 180 | Design review | None (all hardcoded) |
| `build-autofixer-prompt.sh` | 132 | Check autofix | `.egg/autofixer-rules.md` |
| `build-conflict-prompt.sh` | 281 | Merge conflict | `.egg/conflict-rules.md` |
| `build-contract-verification-prompt.sh` | 219 | Contract verify | `.egg/contract-rules.md` |
| `build-doc-updater-prompt.sh` | 447 | Doc updates | None (all hardcoded) |
| `build-feedback-prompt.sh` | 86 | PR feedback | None (all hardcoded) |

All scripts support repo-specific overrides via `.egg/<type>-rules.md` files, with sensible hardcoded defaults.

### Orchestrator Prompt Builders (Python)

| Function | Purpose | Equivalent GA Script |
|----------|---------|---------------------|
| `_build_phase_prompt()` | Phase execution (refine/plan/implement/pr) | None (orchestrator-only) |
| `_build_agent_prompt()` | Multi-agent role prompts | None (orchestrator-only) |
| `_build_review_prompt()` | Agent review with verdicts | `build-review-prompt.sh` |
| `_build_autofix_prompt()` | Check autofix | `build-autofixer-prompt.sh` |
| `_build_checker_prompt()` | Pre-autofix validation | `build-autofixer-prompt.sh` (partial) |

## Key Findings

### Finding 1: Review criteria are maintained in two places

The GitHub Actions code review uses criteria from `action/build-review-prompt.sh` (hardcoded defaults + `.egg/review-rules.md`), while the local orchestrator uses Python functions (`_get_unified_criteria()`, `_get_code_review_criteria()`, `_get_agent_design_criteria()`, `_get_contract_review_criteria()`). These are independently maintained and could diverge.

### Finding 2: Autofixer criteria are aligned but duplicated

The orchestrator's `_build_autofix_prompt()` explicitly states it's "modeled on `action/build-autofixer-prompt.sh`" and uses identical auto-fixable vs report-only categories. However, the criteria exist as hardcoded strings in both locations, so updates must be made in two places.

### Finding 3: Contract verification criteria match but diverge in output format

Both systems use identical task verification checklists (implementation exists, acceptance criteria met, commits linked, tests present), but the output format differs: GitHub Actions posts a PR review comment with HTML markers; the orchestrator writes structured JSON verdict files.

### Finding 4: CLAUDE.md frontloads SDLC-specific content for all sessions

The `contract.md` (~72 lines) and `orchestrator.md` (~92 lines) rule files are always included in `CLAUDE.md`, even when the agent is running a simple GitHub Actions bot task (review, autofix, conflict resolution) that never uses the SDLC contract or orchestrator CLI.

### Finding 5: No shared prompt library between GA and orchestrator

The GitHub Actions shell scripts and orchestrator Python code have no shared source of truth for criteria definitions. Each system hardcodes its own version, creating maintenance risk.

### Finding 6: Deprecated sdlc.md command is still present

The `sandbox/.claude/commands/sdlc.md` file is a 30-line redirect to `egg-sdlc` CLI. It serves no functional purpose and should be removed or minimized.

## Constraints

- **Backward compatibility**: GitHub Actions workflows in external repos depend on the `action/build-*-prompt.sh` scripts. Their interfaces (env vars, output format) must remain stable.
- **Docker build**: Rule files are baked into the Docker image at build time. Dynamic per-invocation selection requires entrypoint changes, not Dockerfile changes.
- **Claude Code behavior**: CLAUDE.md is loaded automatically by Claude Code. There is no built-in mechanism to conditionally load sections.
- **Multi-repo support**: The `.egg/<type>-rules.md` override pattern allows external repos to customize criteria. Any shared library must preserve this.
- **Token budget**: Reducing frontloaded context has real value — every token in CLAUDE.md is consumed on every API call for the entire session.

## Options Considered

### Option A: Shared criteria files with conditional CLAUDE.md assembly

**Approach**: Extract review/autofix/contract criteria into shared markdown files (e.g., `shared/prompts/review-criteria.md`, `shared/prompts/autofixer-criteria.md`). Both the shell scripts and Python code read from these shared files at runtime. Modify `entrypoint.py:setup_agent_rules()` to accept an environment variable (e.g., `EGG_CONTEXT_MODE=sdlc|bot|standalone`) that controls which rule files are included in CLAUDE.md.

**Pros**:
- Single source of truth for all criteria
- Conditional CLAUDE.md reduces token waste (~1,200 tokens saved for non-SDLC sessions)
- Clear ownership: criteria live in one place
- Shell scripts can `cat` the shared files; Python can `Path.read_text()`

**Cons**:
- Requires changes to Docker build to include shared prompt files
- Adds a new `shared/prompts/` directory to manage
- Shell scripts need path resolution logic for shared files
- Testing shared files requires integration tests

### Option B: Consolidate prompt generation into Python library

**Approach**: Create a Python prompt library (`shared/egg_prompts/`) that both the orchestrator and a new `build-prompt` CLI tool use. GitHub Actions shell scripts call `python -m egg_prompts.build_review --pr <N>` instead of assembling prompts in bash. Conditional CLAUDE.md assembly via entrypoint env var.

**Pros**:
- Eliminates shell-script prompt logic entirely
- Type-safe, testable prompt generation
- Single implementation for all contexts
- Easier to add new prompt types

**Cons**:
- Rewrites all 7 shell scripts (~1,530 lines)
- GitHub Actions workflows must change invocation pattern
- Python dependency in action environment (already available)
- Larger scope of change and more risk

### Option C: Trim and align without restructuring

**Approach**: Keep the current architecture (shell scripts for GA, Python for orchestrator) but:
1. Trim CLAUDE.md: make `contract.md` and `orchestrator.md` conditional on `EGG_SDLC_ISSUE` being set
2. Align criteria: manually synchronize review/autofix/contract criteria between shell scripts and Python, adding comments pointing to the canonical source
3. Remove deprecated `sdlc.md` command
4. Add token-cost annotations to rule files so future changes are conscious of budget

**Pros**:
- Minimal change, low risk
- No new abstractions or dependencies
- Quick to implement
- Preserves existing patterns

**Cons**:
- Doesn't solve the dual-maintenance problem long-term
- Criteria will drift again without tooling
- Manual synchronization is error-prone
- "Comments pointing to canonical source" is a weak enforcement mechanism

## Recommended Approach

**Option A: Shared criteria files with conditional CLAUDE.md assembly.**

This option provides the best balance of impact and risk:

1. **Shared criteria files** solve the core dual-maintenance problem by establishing a single source of truth, without the scope explosion of Option B's full Python rewrite.

2. **Conditional CLAUDE.md assembly** is straightforward — `entrypoint.py` already reads rules in a specific order; skipping `contract.md` and `orchestrator.md` when `EGG_SDLC_ISSUE` is not set requires ~5 lines of logic.

3. The shell scripts already support loading from external files (`.egg/<type>-rules.md`). Changing them to also check `shared/prompts/<type>-criteria.md` as a fallback before hardcoded defaults is a natural extension.

4. The orchestrator Python code can read the same shared files with `Path.read_text()`, replacing the current hardcoded criteria functions.

**Scope breakdown**:

| Task | Files Affected | Complexity |
|------|---------------|-----------|
| Create `shared/prompts/` with criteria files | New: 4-5 markdown files | Low |
| Update shell scripts to read shared criteria | 4 of 7 scripts (review, autofixer, contract, conflict) | Medium |
| Update orchestrator to read shared criteria | `orchestrator/routes/pipelines.py` | Medium |
| Conditional CLAUDE.md assembly | `sandbox/entrypoint.py` | Low |
| Remove deprecated `sdlc.md` | `sandbox/.claude/commands/sdlc.md` | Low |
| Trim/tighten existing rule files | 7 rule files | Low |
| Add tests for prompt consistency | New test file | Medium |

## Open Questions

1. Should the `mission.md` rule file be split further? It contains both universal agent instructions (git workflow, PR lifecycle) and some SDLC-specific language (e.g., references to "refine phase"). Splitting could save additional tokens for non-SDLC runs, but adds complexity.

2. The agent mode commands (coder/documenter/tester/integrator) share significant boilerplate (file access constraints section, handoff format, quality checklist). Should these be templated or left as standalone files? Templating would reduce total content but add build complexity.

3. Should `show-metrics.md` be trimmed or removed? It's a monitoring utility that may rarely be used and could be replaced with a simpler CLI tool.

---

*Authored-by: egg*
