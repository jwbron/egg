# Analysis: Tune claude.md files and prompts

> Issue: #659 | Phase: refine

## Problem Statement

The egg system has accumulated prompt content across multiple locations — sandbox rule files, orchestrator prompt builders, and GitHub Actions prompt scripts — without a systematic audit for quality, correctness, or token efficiency. The issue requests:

1. **Audit and refine** all CLAUDE.md files and prompts for quality and correctness
2. **Improve token efficiency** by trimming unnecessary frontloaded context
3. **Align prompts with multi-agent architecture** — each agent should have specialized instructions, not generic catch-all prompts
4. **Unify prompt generation** — local orchestration and GitHub Actions workflows should use the same prompt generators
5. **Move prompt generation into the sandbox codebase** — prompt/claude.md generation should not live in GitHub Actions, but in the sandbox code

## Current Behavior

### Prompt Inventory

The system has **three distinct prompt-delivery mechanisms**, each with different code paths:

#### 1. Sandbox CLAUDE.md (always-on context for every agent)

At container startup, `sandbox/entrypoint.py:678-716` combines 7 rule files from `sandbox/.claude/rules/` into `~/CLAUDE.md`:

| File | Lines | Purpose |
|------|-------|---------|
| `mission.md` | 135 | Core agent role, workflow, GitHub ops, PR lifecycle, review responses, git safety |
| `environment.md` | 71 | Sandbox constraints, network modes, filesystem, services |
| `code-standards.md` | 10 | Tech stack, style guides |
| `test-workflow.md` | 16 | Testing commands and workflow |
| `pr-descriptions.md` | 20 | PR format template |
| `contract.md` | 71 | SDLC contract CLI (egg-contract) |
| `orchestrator.md` | 76 | Orchestrator CLI (egg-orch) |
| **Total** | **~468** | |

**Every agent** — regardless of role or phase — gets all 468 lines. A reviewer agent gets contract CLI docs, a refine agent gets PR description format, etc.

#### 2. Orchestrator Dynamic Prompts (per-phase, per-role)

`orchestrator/routes/pipelines.py` builds prompts dynamically at runtime:

- `_build_phase_prompt()` (line 1280) — phase-specific prompts for refine, plan, implement, pr
- `_build_agent_prompt()` (line 1594) — role-specific prompts for coder, tester, documenter, integrator
- `_build_review_prompt()` (line 1064) — internal reviewer prompts with typed verdicts
- `_build_checker_prompt()` (line 2145) — test/lint runner
- `_build_autofix_prompt()` (line 2222) — fix check failures

These are **Python string builders** that construct prompts as lists of lines joined by newlines.

#### 3. GitHub Actions Shell Prompt Scripts (action/ directory)

Seven shell scripts in `action/` build prompts for GitHub Actions workflows:

| Script | Lines | Used By Workflow |
|--------|-------|------------------|
| `build-review-prompt.sh` | 188 | `reusable-review.yml` |
| `build-autofixer-prompt.sh` | 132 | `reusable-autofix.yml` |
| `build-conflict-prompt.sh` | 281 | `reusable-conflict-resolve.yml` |
| `build-contract-verification-prompt.sh` | 219 | `on-pull-request-contract-verify.yml` |
| `build-feedback-prompt.sh` | 86 | `on-review-feedback.yml` |
| `build-agent-mode-design-review-prompt.sh` | ~180 | `on-pull-request-agent-mode-design.yml` |
| `build-doc-updater-prompt.sh` | ~450 | `on-push-doc-updater.yml` |

These are **bash scripts** that construct prompts using heredocs and shell variable expansion, then write to temp files.

### Key Architectural Problem: Dual Prompt Generators

The orchestrator Python code and the action/ bash scripts are **completely independent implementations** that serve analogous functions in different execution contexts:

| Concern | Local Orchestration | GitHub Actions |
|---------|-------------------|----------------|
| Code review | `_build_review_prompt()` in Python | `build-review-prompt.sh` in bash |
| Autofixing | `_build_autofix_prompt()` in Python | `build-autofixer-prompt.sh` in bash |
| Contract verification | (handled by `_build_review_prompt()` with `reviewer_type="contract"`) | `build-contract-verification-prompt.sh` in bash |
| Feedback addressing | (not present — orchestrator handles feedback differently) | `build-feedback-prompt.sh` in bash |
| Conflict resolution | (not present — orchestrator doesn't handle merge conflicts) | `build-conflict-prompt.sh` in bash |

The prompts are **structurally similar** (both follow a pattern of context → task → rules → conventions) but have **diverged in content**. For example:
- The action review prompt loads repo-specific rules from `.egg/review-rules.md` and conventions from `review-conventions.md`, while the orchestrator review prompt doesn't
- The action autofixer prompt references `gh pr checks`, while the orchestrator version reads from `.egg-state/checks/implement-results.json`

### Token Efficiency Issues

1. **CLAUDE.md bloat**: Every agent receives all 468 lines regardless of role. A reviewer doesn't need the contract CLI docs; a refine agent doesn't need PR description format. The `orchestrator.md` (76 lines of CLI reference) and `contract.md` (71 lines of CLI reference) are pure reference material that could be fetched on-demand.

2. **Duplication in CLAUDE.md and system prompt**: The CLAUDE.md content loaded by the container is available to every agent via Claude Code's built-in CLAUDE.md reading. But the orchestrator **also** generates detailed per-phase prompts that repeat some of this context (e.g., phase restrictions, contract CLI usage).

3. **Action script overhead**: Each action script fetches repo-specific rules at runtime (`.egg/review-rules.md`, `.egg/conflict-rules.md`, etc.), which is good. But the default fallback rules embedded in the scripts are lengthy (the default review rules in `build-review-prompt.sh` are ~50 lines).

### Agent Specialization Gaps

The multi-agent architecture defines specialized roles (coder, tester, documenter, integrator, reviewer) that are supposed to "check" each other. However:

1. **All agents get the same CLAUDE.md** — there's no role-based filtering of the base context. A tester agent receives the same mission, workflow, and GitHub ops instructions as a coder.

2. **Role-specific prompts are minimal** — `_build_agent_prompt()` in `pipelines.py` adds a few role-specific lines (e.g., "Write and run tests for the changes" for tester) but doesn't provide deep specialization instructions.

3. **Reviewer prompts lack cross-checking context** — The issue mentions agents should "check" each other. The current review prompts don't reference what other agents have done or provide criteria for validating agent outputs.

## Constraints

- **Backwards compatibility**: The GitHub Actions workflows are used by external consumers of the egg action who rely on prompt scripts in `action/`. Changes must either maintain the bash interface or provide a migration path.
- **Claude Code CLAUDE.md format**: The `~/CLAUDE.md` file is read automatically by Claude Code. The rules mechanism must continue to work with this format.
- **Sandbox isolation**: Prompt generators in the sandbox can't access GitHub APIs during container startup (no credentials). GitHub Actions prompts can access APIs (tokens available at build time).
- **Testing**: Prompt changes affect agent behavior and are hard to test deterministically. The existing test suite (`tests/action/test_build_*.py`, `orchestrator/tests/test_pipeline_prompts.py`) validates prompt structure but not effectiveness.
- **Token budgets**: Claude's context window has limits. Frontloading too much context wastes tokens on irrelevant instructions; too little leaves agents without critical information.

## Options Considered

### Option A: Unified Python Prompt Library

**Approach**: Create a shared Python prompt library (`shared/egg_prompts/`) that contains all prompt generation logic. Both the local orchestrator and GitHub Actions call into this library. The action scripts become thin wrappers that invoke the Python library.

**Pros**:
- Single source of truth for all prompts
- Easy to test (Python unit tests)
- Can implement role-based CLAUDE.md filtering
- Natural fit for the sandbox codebase (per issue requirement)

**Cons**:
- Requires Python runtime in GitHub Actions (adding a setup-python step)
- Action scripts currently run as pure bash with no Python dependency
- Larger refactor scope

### Option B: Incremental Improvement (Trim, Specialize, Leave Structure)

**Approach**: Keep the current dual-system architecture (bash for actions, Python for orchestrator) but:
1. Trim CLAUDE.md to essential context, move reference docs to on-demand
2. Add role-based filtering to CLAUDE.md assembly
3. Manually sync prompt content between the two systems
4. Add lint checks to catch drift

**Pros**:
- Smaller scope, lower risk
- No changes to GitHub Actions workflow structure
- Can be done incrementally

**Cons**:
- Doesn't address the root cause (dual systems)
- Manual sync is error-prone
- Doesn't satisfy the "move out of GitHub Actions" requirement

### Option C: Templated Prompt System

**Approach**: Create prompt templates as markdown files (in `sandbox/prompts/templates/`) with Jinja2-style variable expansion. A small Python renderer loads templates and fills variables. Both the orchestrator Python code and action scripts call the renderer.

**Pros**:
- Prompts are readable markdown, not embedded in Python strings or bash heredocs
- Templates can be reviewed and edited by non-developers
- Single template set used by both execution paths
- Easy to add role-based variants

**Cons**:
- Adds a template engine dependency
- Template rendering adds complexity
- Variable expansion in bash requires either a Python step or a simple custom renderer

### Option D: Unified Python Prompt Library + Role-Scoped CLAUDE.md

**Approach**: Combines Option A with role-based CLAUDE.md assembly. Create `shared/egg_prompts/` with:
1. Prompt builders (migrated from both Python and bash)
2. CLAUDE.md assembler that takes agent role/phase and includes only relevant sections
3. CLI entry point so GitHub Actions can call it from bash

The action scripts become one-line calls to the Python CLI. CLAUDE.md is assembled per-agent at container startup based on the `EGG_AGENT_ROLE` environment variable.

**Pros**:
- Full unification — one system for all prompt generation
- Role-scoped context reduces token waste
- Testable, maintainable, single source of truth
- Satisfies all issue requirements (audit, token efficiency, specialization, unification, move to sandbox)

**Cons**:
- Largest scope of all options
- Requires Python in GitHub Actions (though it's already used for testing)
- Must carefully preserve the external action interface

## Recommended Approach

**Option D: Unified Python Prompt Library + Role-Scoped CLAUDE.md**

This is the only option that fully addresses all five issue requirements. The key insight is that the issue explicitly asks to "move prompt and claude.md generation workflows out of GitHub Actions and into the sandbox codebase" — Options B and C don't fully achieve this.

The implementation should:
1. Create `shared/egg_prompts/` as the single source of truth
2. Migrate all prompt logic from both `orchestrator/routes/pipelines.py` (extract prompt functions) and `action/*.sh` (rewrite in Python)
3. Add a CLI entry point (`python -m egg_prompts build-prompt --type review --pr 123`) for GitHub Actions to call
4. Refactor CLAUDE.md assembly to be role-aware (different content for coder vs reviewer vs tester)
5. Add token-budget analysis tooling to measure prompt sizes

The action bash scripts should become thin wrappers that call the Python CLI, maintaining the existing interface (`prompt-file` and `model` outputs).

## Open Questions

1. **Which CLAUDE.md sections should be role-scoped vs universal?** For example, should the reviewer agent still see the mission statement, or should it get a reviewer-specific mission? Should the contract CLI docs only appear for agents in the SDLC pipeline?

2. **How should we handle action/ backwards compatibility?** The bash scripts are currently invoked by reusable workflows that external repos may consume. Should we keep the bash wrappers as a stable interface, or can we require Python in the action?

3. **Should we add token budget tracking?** The issue mentions token efficiency as a secondary concern. Should we add automated measurement of prompt sizes (e.g., a CI check that fails if CLAUDE.md exceeds N tokens)?

4. **What's the priority ordering?** The issue lists quality/correctness first and token efficiency second. Given the scope, should we tackle unification first (shared library) or trimming first (reduce bloat in current system)?

---

*Authored-by: egg*
