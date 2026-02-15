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

#### 1. Sandbox CLAUDE.md (always-on context for agents)

At container startup, `sandbox/entrypoint.py:678-721` assembles rule files from `/opt/claude-rules/` (the runtime path; the development source is `sandbox/.claude/rules/`) into `~/CLAUDE.md` and `~/repos/CLAUDE.md`.

The assembly is **partially role-scoped**. Five files are always included, and two additional files are conditionally included only when `EGG_PIPELINE_ID` is set (lines 691-693):

| File | Lines | Inclusion |
|------|-------|-----------|
| `mission.md` | 135 | Always |
| `environment.md` | 71 | Always |
| `code-standards.md` | 10 | Always |
| `test-workflow.md` | 16 | Always |
| `pr-descriptions.md` | 20 | Always |
| **Universal subtotal** | **~252** | |
| `contract.md` | 71 | Only when `EGG_PIPELINE_ID` is set |
| `orchestrator.md` | 76 | Only when `EGG_PIPELINE_ID` is set |
| **Pipeline subtotal** | **~147** | |
| **Total (pipeline agent)** | **~399** | |

Note: `README.md` (69 lines) exists in the rules directory as documentation but is **not** included in `rules_order` and is never assembled into CLAUDE.md.

The existing conditional logic already prevents non-pipeline agents from receiving contract/orchestrator docs. However, **within** pipeline agents, there is no further role-based filtering — a reviewer agent still gets mission instructions about PR lifecycle, and a coder gets reviewer-oriented content.

#### 2. Orchestrator Dynamic Prompts (per-phase, per-role)

`orchestrator/routes/pipelines.py` builds prompts dynamically at runtime:

- `_build_review_prompt()` (line 1090) — internal reviewer prompts with typed verdicts
- `_build_phase_prompt()` (line 1299) — phase-specific prompts for refine, plan, implement, pr
- `_build_agent_prompt()` (line 1606) — role-specific prompts for coder, tester, documenter, integrator
- `_build_checker_prompt()` (line 2161) — test/lint runner
- `_build_autofix_prompt()` (line 2238) — fix check failures

These are **Python string builders** that construct prompts as lists of lines joined by newlines.

The orchestrator already has shared prompt infrastructure:

- **`shared/prompts/`** contains 4 criteria files used by both the orchestrator and tests:
  - `code-review-criteria.md` — code review priorities and strategy
  - `contract-review-criteria.md` — contract verification rules
  - `agent-design-criteria.md` — agent-mode design review focus areas
  - `autofixer-rules.md` — auto-fixable vs report-only classification

- **`_read_shared_criteria()`** (line 770) implements a 3-level fallback chain:
  1. User override: `.egg/<user_override>` in the repo (e.g., `.egg/review-rules.md`)
  2. Source tree: `shared/prompts/<filename>` (development/local)
  3. Docker path: `/app/prompts/<filename>` (production)
  4. Fallback: `None` → each caller has inline defaults

This pattern is already used by `_get_code_review_criteria()` (line 835), `_get_contract_review_criteria()` (line 868), `_get_agent_design_criteria()` (line 809), and the autofixer prompt builder (line 2293).

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
| Contract verification | `_build_review_prompt()` with `reviewer_type="contract"` | `build-contract-verification-prompt.sh` in bash |
| Feedback addressing | (not present — orchestrator handles differently) | `build-feedback-prompt.sh` in bash |
| Conflict resolution | (not present — orchestrator doesn't handle conflicts) | `build-conflict-prompt.sh` in bash |

### Conventions File Divergence (Specific Gap)

A concrete and actionable divergence: the action scripts load **conventions files** that the orchestrator does not use at all:

- **`action/review-conventions.md`** (58 lines) — loaded by `build-review-prompt.sh` (line 98) and `build-contract-verification-prompt.sh` (line 86). Contains posting guidelines (use `--body-file`), approval rules (when to request changes vs approve), self-authored PR handling, and comment quality standards.

- **`action/autofixer-conventions.md`** (126 lines) — loaded by `build-autofixer-prompt.sh` (line 67). Contains single-pass workflow rules, lint job structure, failure investigation commands, verification loop requirements, and decision framework for auto-fix vs report.

The orchestrator's `_build_review_prompt()` and `_build_autofix_prompt()` **do not reference any conventions files**. This means agents running locally via the orchestrator lack behavioral guidelines that their GitHub Actions counterparts receive. This is one of the most direct examples of prompt drift between the two systems.

### Token Efficiency Analysis

1. **CLAUDE.md bloat (less severe than initially estimated)**: The existing conditional inclusion of `contract.md` and `orchestrator.md` already prevents 147 lines from reaching non-pipeline agents. However, within pipeline agents, the universal 252 lines still include content irrelevant to specific roles — a reviewer doesn't need the full PR lifecycle workflow, and a checker doesn't need the SDLC contract CLI reference.

2. **Duplication between CLAUDE.md and system prompt**: The CLAUDE.md content loaded at container startup is available to every agent via Claude Code's built-in CLAUDE.md reading. The orchestrator **also** generates per-phase prompts that repeat some of this context (e.g., phase restrictions, contract CLI usage patterns).

3. **Action script overhead**: Each action script fetches repo-specific rules at runtime (`.egg/review-rules.md`, `.egg/conflict-rules.md`, etc.), which is good. But the default fallback rules embedded in the scripts are lengthy (the default review rules in `build-review-prompt.sh` are ~50 lines).

### Agent Specialization Gaps

The multi-agent architecture defines specialized roles (coder, tester, documenter, integrator, reviewer) that are supposed to "check" each other. However:

1. **Within-pipeline role scoping is absent** — All pipeline agents get the same 399-line CLAUDE.md. A tester agent receives the same mission, workflow, and GitHub ops instructions as a coder. The conditional logic only distinguishes pipeline vs non-pipeline, not coder vs reviewer.

2. **Role-specific prompts are minimal** — `_build_agent_prompt()` (line 1606) adds a few role-specific lines (e.g., "Write and run tests for the changes" for tester) but doesn't provide deep specialization instructions.

3. **Reviewer prompts lack cross-checking context** — The issue mentions agents should "check" each other. The current review prompts don't reference what other agents have done or provide criteria for validating agent outputs.

## Constraints

- **Backwards compatibility**: The GitHub Actions workflows are used by external consumers of the egg action who rely on prompt scripts in `action/`. Changes must either maintain the bash interface or provide a migration path.
- **Claude Code CLAUDE.md format**: The `~/CLAUDE.md` file is read automatically by Claude Code. The rules mechanism must continue to work with this format.
- **Sandbox isolation**: Prompt generators in the sandbox can't access GitHub APIs during container startup (no credentials). GitHub Actions prompts can access APIs (tokens available at build time).
- **Existing shared infrastructure**: The `shared/prompts/` directory and `_read_shared_criteria()` fallback chain already exist. Any unification effort should build on this infrastructure rather than creating a parallel system.
- **Testing**: Prompt changes affect agent behavior and are hard to test deterministically. The existing test suite (`tests/action/test_build_*.py`, `orchestrator/tests/test_pipeline_prompts.py`) validates prompt structure but not effectiveness.
- **Token budgets**: Claude's context window has limits. Frontloading too much context wastes tokens on irrelevant instructions; too little leaves agents without critical information.

## Options Considered

### Option A: Incremental Improvement (Trim, Specialize, Leave Structure)

**Approach**: Keep the current dual-system architecture (bash for actions, Python for orchestrator) but:
1. Extend the existing conditional CLAUDE.md assembly to be role-aware (e.g., omit PR lifecycle sections for reviewer-only agents)
2. Trim CLAUDE.md to essential context, move reference docs to on-demand
3. Port conventions files into `shared/prompts/` so both systems can reference them
4. Add lint checks to catch content drift between the two systems

**Pros**:
- Smallest scope, lowest risk
- No changes to GitHub Actions workflow structure
- Can be done incrementally
- Builds on existing conditional assembly pattern

**Cons**:
- Doesn't address the root cause (dual independent systems)
- Drift detection helps but doesn't prevent divergence
- Doesn't satisfy the "move out of GitHub Actions" requirement

### Option B: Templated Prompt System

**Approach**: Create prompt templates as markdown files (in `shared/prompts/templates/`) with Jinja2-style variable expansion. A small Python renderer loads templates and fills variables. Both the orchestrator Python code and action scripts call the renderer.

**Pros**:
- Prompts are readable markdown, not embedded in Python strings or bash heredocs
- Templates can be reviewed and edited by non-developers
- Single template set used by both execution paths
- Easy to add role-based variants

**Cons**:
- Adds a template engine dependency
- Template rendering adds complexity
- Variable expansion in bash requires either a Python step or a simple custom renderer

### Option C: Unified Python Prompt Library (Extending shared/prompts/)

**Approach**: Extend the existing `shared/prompts/` directory with Python prompt generation logic, building on the `_read_shared_criteria()` fallback pattern already in place. Both the local orchestrator and GitHub Actions call into this library. The action scripts become thin wrappers that invoke the Python library.

Specifically:
1. Add prompt builder modules alongside existing criteria files in `shared/prompts/`
2. Extract prompt logic from `orchestrator/routes/pipelines.py` into these modules
3. Rewrite action bash scripts as thin wrappers calling the Python library
4. Add a CLI entry point (`python -m shared.prompts ...`) for GitHub Actions invocation

**Pros**:
- Builds on existing `shared/prompts/` and `_read_shared_criteria()` infrastructure
- Single source of truth for all prompts
- Easy to test (Python unit tests, extending existing test patterns)
- Natural fit for the sandbox codebase (per issue requirement)

**Cons**:
- Requires Python runtime in GitHub Actions (adding a setup-python step)
- Action scripts currently run as pure bash with no Python dependency
- Moderate refactor scope

### Option D: Unified Python Prompt Library + Role-Scoped CLAUDE.md

**Approach**: Combines Option C with deeper role-based CLAUDE.md assembly. Extend `shared/prompts/` with:
1. Prompt builders (migrated from both Python and bash)
2. CLAUDE.md assembler that takes agent role/phase and includes only relevant sections (extending the existing `EGG_PIPELINE_ID` conditional to also filter on `EGG_AGENT_ROLE`)
3. CLI entry point so GitHub Actions can call it from bash
4. Conventions files moved from `action/` into `shared/prompts/` so both systems use them

The action scripts become one-line calls to the Python CLI. CLAUDE.md is assembled per-agent at container startup based on both `EGG_PIPELINE_ID` and `EGG_AGENT_ROLE` environment variables.

**Pros**:
- Full unification — one system for all prompt generation
- Role-scoped context reduces token waste beyond what the current pipeline/non-pipeline split achieves
- Testable, maintainable, single source of truth
- Satisfies all issue requirements (audit, token efficiency, specialization, unification, move to sandbox)
- Builds on existing infrastructure rather than greenfield

**Cons**:
- Largest scope of all options
- Requires Python in GitHub Actions (though it's already used for testing)
- Must carefully preserve the external action interface

## Recommended Approach

**Option D: Unified Python Prompt Library + Role-Scoped CLAUDE.md**

This is the only option that fully addresses all five issue requirements. The key insight is that the issue explicitly asks to "move prompt and claude.md generation workflows out of GitHub Actions and into the sandbox codebase" — Options A and B don't fully achieve this.

The implementation should build on the existing `shared/prompts/` directory and `_read_shared_criteria()` fallback chain rather than creating new infrastructure:

1. Extend `shared/prompts/` with prompt builder modules alongside existing criteria files
2. Extract prompt logic from both `orchestrator/routes/pipelines.py` (move prompt functions out) and `action/*.sh` (rewrite in Python)
3. Move `action/review-conventions.md` and `action/autofixer-conventions.md` into `shared/prompts/` so both execution paths use identical conventions
4. Add a CLI entry point for GitHub Actions to call
5. Extend CLAUDE.md assembly to filter on `EGG_AGENT_ROLE` in addition to the existing `EGG_PIPELINE_ID` conditional
6. Add token-budget analysis tooling to measure prompt sizes

The action bash scripts should become thin wrappers that call the Python CLI, maintaining the existing interface (`prompt-file` and `model` outputs).

## Open Questions

1. **Which CLAUDE.md sections should be role-scoped vs universal?** The existing pipeline/non-pipeline split is coarse. For example, should the reviewer agent still see the full mission statement, or should it get a reviewer-specific subset? Should PR lifecycle docs be omitted for checker/autofix agents?

2. **How should we handle action/ backwards compatibility?** The bash scripts are currently invoked by reusable workflows that external repos may consume. Should we keep the bash wrappers as a stable interface, or can we require Python in the action?

3. **Should we add token budget tracking?** The issue mentions token efficiency as a secondary concern. Should we add automated measurement of prompt sizes (e.g., a CI check that fails if CLAUDE.md exceeds N tokens)?

4. **What's the priority ordering?** The issue lists quality/correctness first and token efficiency second. Given the scope, should we tackle unification first (shared library) or trimming first (reduce bloat in current system)?

---

*Authored-by: egg*
