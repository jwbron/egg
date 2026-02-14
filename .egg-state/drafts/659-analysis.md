# Analysis: Tune claude.md files and prompts (#659)

## Problem Statement

The issue requests an audit of all CLAUDE.md files and prompts to:
1. Eliminate token waste from frontloaded context
2. Ensure each prompt is appropriate for its task
3. Ensure local orchestration and GitHub Actions workflows use the same prompt generators

## Current State Inventory

### CLAUDE.md Files

There is one canonical CLAUDE.md, assembled at container startup from modular source files:

| Source File | Size | Content |
|-------------|------|---------|
| `sandbox/.claude/rules/mission.md` | 5,372B | Core workflow, git ops, PR lifecycle, decisions |
| `sandbox/.claude/rules/environment.md` | 2,841B | Sandbox, network modes, gateway, filesystem |
| `sandbox/.claude/rules/contract.md` | 2,670B | SDLC contract CLI and HITL mechanisms |
| `sandbox/.claude/rules/orchestrator.md` | 3,152B | egg-orch CLI reference table |
| `sandbox/.claude/rules/test-workflow.md` | 621B | Testing approach and framework reference |
| `sandbox/.claude/rules/pr-descriptions.md` | 445B | PR format template |
| `sandbox/.claude/rules/code-standards.md` | 386B | Language standards and commands |
| **Total** | **~15.5KB** | Combined into `~/CLAUDE.md` at startup |

Loading mechanism: `sandbox/entrypoint.py:setup_agent_rules()` concatenates all files with `---` separators, writes to `~/CLAUDE.md`, and symlinks `~/repos/CLAUDE.md` to it. Claude Code auto-loads this file.

### GitHub Actions Prompt Builders (Shell Scripts)

| Script | Size | Purpose | Conventions File |
|--------|------|---------|-----------------|
| `action/build-review-prompt.sh` | 7.9KB | Code review | `review-conventions.md` (2.7KB) |
| `action/build-agent-mode-design-review-prompt.sh` | 8.2KB | Agent-mode design review | None |
| `action/build-autofixer-prompt.sh` | 4.7KB | Fix failing checks | `autofixer-conventions.md` (3.7KB) |
| `action/build-conflict-prompt.sh` | 11.3KB | Merge conflict resolution | `conflict-conventions.md` (8.9KB) |
| `action/build-contract-verification-prompt.sh` | 8.2KB | Contract compliance review | Uses `review-conventions.md` |
| `action/build-feedback-prompt.sh` | 3.3KB | Address review feedback | None |
| `action/build-doc-updater-prompt.sh` | 17.4KB | Post-merge doc updates | None |

These scripts generate prompts dynamically, injecting PR context and repo-specific rules from `.egg/` files.

### Orchestrator Prompt Builders (Python Functions)

| Function | Location | Purpose |
|----------|----------|---------|
| `_build_phase_prompt()` | `orchestrator/routes/pipelines.py:1131` | Phase-specific sandbox prompts (refine, plan, implement, pr) |
| `_build_agent_prompt()` | `orchestrator/routes/pipelines.py:1372` | Role-specific multi-agent prompts (coder, tester, documenter, etc.) |
| `_build_review_prompt()` | `orchestrator/routes/pipelines.py:938` | SDLC pipeline reviewer prompts |
| `_build_checker_prompt()` | `orchestrator/routes/pipelines.py:1883` | Checker agent (discover and run tests/lint) |
| `_build_autofix_prompt()` | `orchestrator/routes/pipelines.py:1960` | Fix check failures in pipeline |

## Key Findings

### Finding 1: Prompt Generators Are NOT Shared Between Flows

**This is the central issue.** Local orchestration and GitHub Actions use completely separate prompt generators for equivalent tasks:

| Task | GitHub Actions | Local Orchestrator |
|------|---------------|-------------------|
| **Code review** | `build-review-prompt.sh` (shell) | `_build_review_prompt()` (Python) |
| **Autofix** | `build-autofixer-prompt.sh` (shell) | `_build_autofix_prompt()` (Python) |
| **Contract verification** | `build-contract-verification-prompt.sh` (shell) | `_build_review_prompt()` with `reviewer_type="contract"` |

The shell scripts and Python functions produce similar but **not identical** prompts. For example:

- **Review prompt**: The shell script includes thorough review rules (security, correctness, robustness, design), review conventions, re-review support, and `--body-file` posting instructions. The Python `_build_review_prompt()` is a simpler structured format with verdict JSON output, no review rules, and no conventions.
- **Autofix prompt**: The shell script includes configurable rules from `.egg/autofixer-rules.md`, conventions, and workflow context. The Python version has hardcoded rules and no conventions support.
- **Contract verification**: The shell script has comprehensive contract rules, CLI instructions, review markers, and re-review support. The Python version has no contract-specific review type.

These divergences mean the agent receives different instructions depending on execution context, which can lead to inconsistent behavior.

### Finding 2: CLAUDE.md Token Budget Is Reasonable But Has Waste

At ~15.5KB (~4,000 tokens), the CLAUDE.md is not egregiously large, but some sections add tokens with limited value for certain agent types:

**Sections with low relevance for GHA agents:**
- `orchestrator.md` (3.1KB) — The egg-orch CLI reference is only relevant when running in orchestrated mode. GitHub Actions agents never use these commands. This is **20% of total tokens** with zero value for GHA.
- `contract.md` (2.7KB) — SDLC contract commands are only relevant during pipeline execution. Code reviewers, conflict resolvers, and autofixers never use `egg-contract`. This is **17% of total tokens** wasted for most GHA agents.

**Sections that are already well-optimized:**
- `code-standards.md` (386B) — Concise and appropriate.
- `test-workflow.md` (621B) — Concise and appropriate.
- `pr-descriptions.md` (445B) — Concise and appropriate.

**Minor redundancies within CLAUDE.md:**
- Git push troubleshooting appears in both `mission.md` ("If push/PR fails") and `environment.md` ("Git Push" section with detailed troubleshooting).
- Worktree warnings appear in both `mission.md` and `environment.md` (Gateway Sidecar section).
- "NEVER merge PRs" appears in `mission.md` (explicitly) and `environment.md` (gateway restrictions). Redundancy is arguably justified for a critical constraint.

### Finding 3: Convention Files Duplicate Prompt Content

The `action/` directory has convention markdown files (`review-conventions.md`, `autofixer-conventions.md`, `conflict-conventions.md`) that contain detailed instructions which partially overlap with the corresponding prompt scripts. The prompt scripts include fallback inline conventions when the file doesn't exist, creating two places where the same guidance lives.

This is by design (conventions can be overridden per-repo) but means the generated prompts can be larger than necessary when the conventions file repeats what's already in the prompt script.

### Finding 4: `_build_phase_prompt()` Has Dead Code Branches

In `pipelines.py:1188-1216` and `1231-1246`, the `is_local` and `else` branches for `refine` and `plan` phases produce **identical output**. The conditional branching exists but does nothing:

```python
if is_local:
    lines.extend(["Write your analysis to `{path}`.", "Commit and push..."])
else:
    lines.extend(["Write your analysis to `{path}`.", "Commit and push..."])  # identical
```

### Finding 5: Doc-Updater Prompt Does Heavy Pre-Processing

`build-doc-updater-prompt.sh` (17.4KB) does significant pre-processing in bash — extracting terms from file paths, searching for related docs, detecting high-risk patterns. This is the largest prompt script by far. While the design is defensible (the pre-processing reduces agent tool calls), the bash implementation is complex and fragile. The agent receives the results as context, which is appropriate.

### Finding 6: Review-Type Prompts Lack Unified Structure

The code review (`build-review-prompt.sh`), agent-mode design review (`build-agent-mode-design-review-prompt.sh`), and contract verification review (`build-contract-verification-prompt.sh`) all follow similar patterns but are independently maintained. They share:
- Re-review detection via `LAST_REVIEW_COMMIT`
- `--body-file` conventions for posting
- Model selection (all use opus)
- Review marker HTML comments

This shared structure isn't factored out, leading to drift risk.

## Recommendations

### Approach A: Shared Prompt Generator Library (Recommended)

Create a shared Python library that both the orchestrator and GitHub Actions use for prompt generation. The GHA shell scripts would call a Python CLI to generate prompts instead of building them inline.

**Implementation:**
1. Create `shared/prompt_builders/` with modules for each prompt type (review, autofix, conflict, etc.)
2. Each module exposes `build_prompt(**kwargs) -> str` that produces the canonical prompt
3. Add a CLI entry point: `python -m prompt_builders.review --pr 123 --repo owner/name`
4. Refactor shell scripts to call the Python CLI instead of building prompts inline
5. Refactor orchestrator functions to import from the shared library
6. Move convention files into the shared library as defaults

**Benefits:**
- Single source of truth for each prompt type
- Testable in Python (pytest vs shell testing)
- Type-safe with structured kwargs
- Conventions and rules can be loaded consistently

**Risks:**
- Python dependency in GHA runner (mitigated: Python is already available in the action container)
- Migration complexity — need to verify prompt equivalence during transition

### Approach B: CLAUDE.md Conditional Assembly

Make CLAUDE.md assembly context-aware, only including sections relevant to the agent's role.

**Implementation:**
1. Add a `AGENT_CONTEXT` environment variable (e.g., `pipeline`, `reviewer`, `autofixer`, `conflict-resolver`)
2. Modify `setup_agent_rules()` in `entrypoint.py` to conditionally include sections based on context
3. Define a mapping: which rules each context needs

**Benefits:**
- Reduces token waste for specialized agents (could save 30-40% for GHA agents)
- No change to prompt scripts needed

**Risks:**
- More complex assembly logic
- Risk of under-including needed context

### Approach C: Minimal — Trim and Align

Keep the current dual-system (shell + Python) but manually align prompts and trim CLAUDE.md.

**Implementation:**
1. Remove `orchestrator.md` and `contract.md` from CLAUDE.md for GHA agents
2. Manually update Python prompt builders to match shell script content
3. Remove dead code branches in `_build_phase_prompt()`
4. Document the need to keep prompts in sync

**Benefits:**
- Lowest effort
- No architectural changes

**Risks:**
- Drift will recur without structural enforcement
- Manual sync is error-prone

### Recommended Priority

1. **Approach A** (Shared Prompt Generator Library) — addresses the root cause: prompts diverge because they're maintained in two languages in two places
2. **Approach B** (Conditional CLAUDE.md) — quick win for token reduction, independent of Approach A
3. Clean up dead code in `_build_phase_prompt()` regardless of approach

## Specific Token Savings Estimates

| Change | Tokens Saved | Agent Types Affected |
|--------|-------------|---------------------|
| Conditional CLAUDE.md (drop orchestrator.md for GHA) | ~800 tokens | All GHA agents |
| Conditional CLAUDE.md (drop contract.md for non-pipeline) | ~700 tokens | Reviewer, autofixer, conflict |
| Remove redundant git push / worktree guidance | ~80 tokens | All agents |
| Remove dead code branches in phase prompt | 0 (code cleanup) | N/A |

Total potential savings per GHA invocation: ~1,500 tokens of input context (~10% of CLAUDE.md).

## Implementation Constraints

- GHA prompt scripts run in the `egg` container (Python available)
- Shell scripts are called from reusable workflows — changing the interface (env vars in, file + model out) would require workflow updates
- Orchestrator Python functions are called inline — refactoring to a shared module is straightforward
- Convention and rules files (`.egg/*.md`) should continue to be per-repo overridable
- Tests exist for several prompt builders — any refactoring must preserve test coverage

## Files to Modify

### Core Changes
- `shared/prompt_builders/` — New shared library (or within `shared/egg_prompts/`)
- `orchestrator/routes/pipelines.py` — Refactor `_build_*_prompt()` to use shared library
- `action/build-review-prompt.sh` — Refactor to call Python CLI
- `action/build-autofixer-prompt.sh` — Refactor to call Python CLI
- `action/build-conflict-prompt.sh` — Refactor to call Python CLI
- `action/build-contract-verification-prompt.sh` — Refactor to call Python CLI
- `action/build-feedback-prompt.sh` — Refactor to call Python CLI
- `action/build-doc-updater-prompt.sh` — Refactor to call Python CLI
- `action/build-agent-mode-design-review-prompt.sh` — Refactor to call Python CLI

### CLAUDE.md Assembly
- `sandbox/entrypoint.py` — Add conditional assembly logic
- `sandbox/.claude/rules/` — No changes to content, assembly changes only

### Cleanup
- `orchestrator/routes/pipelines.py:1188-1246` — Remove dead `is_local`/`else` identical branches

### Tests
- `orchestrator/tests/test_pipeline_prompts.py` — Update for shared library
- `tests/action/test_build_review_prompt.py` — Update for new Python CLI
- `tests/action/test_build_conflict_prompt.py` — Update
- `tests/action/test_build_agent_mode_design_review_prompt.py` — Update
- New test: `tests/shared/test_prompt_builders.py` — Canonical prompt tests
