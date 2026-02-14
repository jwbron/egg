# Issue #659 Analysis: Tune claude.md Files and Prompts

## Problem Statement

The issue requests an audit of all claude.md files and prompts to:
1. Reduce token waste by trimming unnecessary context from front-loaded prompts
2. Ensure each prompt is appropriate for its specific task
3. Unify prompt generators so local orchestration and GitHub Actions workflows use the same code paths

## Current Architecture

### Prompt Delivery System

There are **two independent prompt systems** that operate in different contexts:

| System | Context | How Prompts Reach Claude |
|--------|---------|--------------------------|
| **CLAUDE.md rules** | Always-on context | Assembled from modular `.md` files at container startup, read automatically by Claude Code |
| **Task prompts** | Per-invocation | Built dynamically and passed as CLI argument to `claude --print` |

### CLAUDE.md Assembly

**Source files**: `sandbox/.claude/rules/` (7 files)
**Assembly**: `sandbox/entrypoint.py:setup_agent_rules()` concatenates them with `---` separators
**Output**: `~/CLAUDE.md` (15,529 bytes, ~3,800 tokens)
**Symlink**: `~/repos/CLAUDE.md` → `~/CLAUDE.md`

**Rule files and approximate sizes:**

| File | Purpose | Approx. Size |
|------|---------|-------------|
| `mission.md` | Core role, workflow, git operations, PR lifecycle | ~5,400 bytes |
| `environment.md` | Sandbox constraints, network modes, gateway | ~2,800 bytes |
| `code-standards.md` | Tech stack, coding conventions | ~400 bytes |
| `test-workflow.md` | Test execution patterns | ~600 bytes |
| `pr-descriptions.md` | PR format template | ~500 bytes |
| `contract.md` | SDLC contract CLI reference | ~2,800 bytes |
| `orchestrator.md` | Orchestrator CLI reference | ~3,000 bytes |

### Task Prompt Sources

**Local Orchestration** (in `orchestrator/routes/pipelines.py`):
- `_build_phase_prompt()` — Single-agent phase prompts (refine, plan, implement, pr)
- `_build_agent_prompt()` — Multi-agent role-specific prompts (coder, tester, documenter, integrator, architect, task_planner, risk_analyst, reviewer_*)
- `_build_review_prompt()` — Reviewer agent prompts with verdict format
- `_build_checker_prompt()` — Test/lint checker prompts
- `_build_autofix_prompt()` — Auto-fixer prompts

**GitHub Actions** (in `action/build-*-prompt.sh`, 7 scripts):
- `build-autofixer-prompt.sh` — Fix failing CI checks
- `build-review-prompt.sh` — Code review
- `build-contract-verification-prompt.sh` — Contract compliance review
- `build-feedback-prompt.sh` — Address review feedback
- `build-conflict-prompt.sh` — Merge conflict resolution
- `build-doc-updater-prompt.sh` — Documentation updates
- `build-agent-mode-design-review-prompt.sh` — Agent-mode design review

**Agent Mode Commands** (in `sandbox/.claude/commands/`, 7 files):
- `coder-mode.md`, `tester-mode.md`, `documenter-mode.md`, `integrator-mode.md`
- `sdlc.md`, `onboarding-docs.md`, `show-metrics.md`

---

## Key Findings

### Finding 1: CLAUDE.md Contains Sections Irrelevant to Most Tasks

The full CLAUDE.md (~3,800 tokens) is loaded for **every** agent invocation regardless of task. Several sections are only relevant in specific contexts:

| Section | When Relevant | When Irrelevant |
|---------|---------------|-----------------|
| Contract CLI (~2,800 bytes) | SDLC pipeline tasks only | GH Actions review/autofix/conflict agents |
| Orchestrator CLI (~3,000 bytes) | Pipeline-managed agents only | GH Actions agents, standalone tasks |
| PR Description Format (~500 bytes) | PR creation only | Refine, plan, review, autofix, conflict |
| Non-Interactive Mode (~400 bytes) | GH Actions/CI only | Interactive sandbox sessions |
| Notifications (~300 bytes) | Long-running tasks | Short GH Actions tasks |

**Token waste estimate**: ~1,500-2,000 tokens per invocation for agents that don't need contract/orchestrator/PR-format sections. For the 7+ GH Actions workflows that run frequently, this adds up.

### Finding 2: Duplicated Prompt Content Between Systems

The orchestrator and GH Actions have **parallel but separate implementations** for the same reviewer types:

| Concept | Orchestrator (Python) | GH Actions (Bash) |
|---------|----------------------|-------------------|
| Code review criteria | `_get_code_review_criteria()` | `fetch_review_rules()` in `build-review-prompt.sh` |
| Agent-design review | `_get_agent_design_criteria()` | `build-agent-mode-design-review-prompt.sh` |
| Contract verification | `_get_contract_review_criteria()` | `build-contract-verification-prompt.sh` |
| Autofix rules | `_build_autofix_prompt()` | `build-autofixer-prompt.sh` |

These are **not shared code** — they're independently maintained copies that can drift apart. The content is similar but not identical. For example:

- **Code review criteria**: The orchestrator's `_get_code_review_criteria()` lists security, correctness, robustness, and design categories. The GH Actions `build-review-prompt.sh` has the same categories with additional "How to Review" and "Skip" guidance that the orchestrator lacks.
- **Agent-design criteria**: The orchestrator's `_get_agent_design_criteria()` lists 5 anti-patterns. The GH Actions `build-agent-mode-design-review-prompt.sh` has the same 5 patterns plus extensive "Review Philosophy" and "What to Skip" sections.
- **Autofix rules**: Both have the same auto-fixable vs. report-only distinction, but the GH Actions version supports repo-specific `.egg/autofixer-rules.md` overrides that the orchestrator version doesn't.

### Finding 3: Agent Mode Commands Duplicate Orchestrator Prompts

The `.claude/commands/` files (coder-mode.md, tester-mode.md, etc.) contain role instructions that overlap with `_build_agent_prompt()` in the orchestrator. Both define:
- Role responsibilities and file access constraints
- Handoff file formats and locations
- Quality checklists
- Workflow steps

When an agent runs in the orchestrated pipeline, it receives **both** the `.claude/commands/` content (via CLAUDE.md rules) **and** the orchestrator-built prompt — creating redundancy.

### Finding 4: GH Actions Prompts Are Well-Designed but Isolated

The GH Actions prompt builders follow good patterns:
- Minimal, task-focused prompts
- Support for repo-specific rule overrides (`.egg/review-rules.md`, etc.)
- Re-review support with delta context
- Proper conventions for posting via `gh pr review --body-file`

However, they are bash scripts that cannot be reused by the Python orchestrator, creating two parallel prompt ecosystems.

### Finding 5: `_build_phase_prompt()` Has Redundant Branches

In `orchestrator/routes/pipelines.py`, the `_build_phase_prompt()` function has identical code for local and issue mode in the refine and plan phases (lines 1208-1223 and 1238-1253). The `is_local` branching produces the same output for both paths.

### Finding 6: CLAUDE.md Includes Sandbox-Specific Info for Non-Sandbox Contexts

The CLAUDE.md rules reference Docker container paths (`~/repos/`, `~/context-sync/`), sandbox services (PostgreSQL, Redis), and network modes that are only relevant inside the sandbox. When CLAUDE.md content is surfaced in other contexts (like the system prompt you're reading now), it wastes tokens with irrelevant Docker/sandbox details.

---

## Recommendations

### Approach A: Modular CLAUDE.md with Conditional Assembly (Recommended)

**Concept**: Make CLAUDE.md assembly context-aware — only include sections relevant to the current agent's role and task.

**Changes**:
1. **Split rules into required and optional**:
   - **Always include**: `mission.md` (core workflow + git), `environment.md` (sandbox constraints), `code-standards.md`
   - **Include conditionally**: `contract.md` (only for SDLC pipeline agents), `orchestrator.md` (only for pipeline-managed agents), `pr-descriptions.md` (only for PR-creating agents), `test-workflow.md` (only for code-touching agents)

2. **Modify `setup_agent_rules()` in `entrypoint.py`**:
   - Accept an `agent_context` parameter (e.g., "sdlc-coder", "gha-reviewer", "gha-autofixer", "standalone")
   - Include only the relevant rule files for that context
   - Estimated token savings: 1,000-2,000 tokens per GH Actions invocation

3. **Trim verbose sections**:
   - `contract.md`: Remove the HITL Decision vs Feedback examples — the CLI help covers this. Keep only the command table and workflow.
   - `orchestrator.md`: Remove the Common Workflows examples and Related CLIs section. Keep only the Quick Reference table.
   - `mission.md`: The "Responding to PR Reviews" section with the full bash example is only needed for agents that respond to reviews. Move to a conditional include.

### Approach B: Shared Prompt Library

**Concept**: Create a shared Python module that both the orchestrator and GH Actions scripts can import for review criteria, autofix rules, etc.

**Changes**:
1. **Create `shared/egg_prompts/`** with:
   - `review_criteria.py` — Code review, agent-design, contract verification criteria
   - `autofix_rules.py` — Auto-fixable vs report-only classification
   - `review_conventions.py` — Posting conventions (body-file, signing)
   - `conflict_rules.py` — Conflict resolution rules

2. **Refactor GH Actions prompt builders** to call a Python helper that returns the criteria text, rather than maintaining inline bash heredocs.

3. **Refactor orchestrator** to import from the shared module.

**Trade-off**: This adds a Python dependency to the GH Actions scripts (currently pure bash). The bash scripts already have PYTHONPATH set up for `egg_lib`, so this is feasible.

### Approach C: Trim-Only (Minimal Change)

**Concept**: Just reduce token waste without restructuring the architecture.

**Changes**:
1. **Trim `contract.md`**: Remove HITL examples and Processing section (~800 bytes saved)
2. **Trim `orchestrator.md`**: Remove Common Workflows and Related CLIs (~800 bytes saved)
3. **Trim `mission.md`**: Consolidate PR Review Response section into a one-liner reference (~400 bytes saved)
4. **Fix duplicate code**: Remove the identical is_local branches in `_build_phase_prompt()`

---

## Recommended Implementation Plan

### Phase 1: Trim and Deduplicate (Low Risk)

1. **Trim CLAUDE.md rule files** — Remove verbose examples and redundant content from `contract.md`, `orchestrator.md`, and `mission.md`. Target: reduce from ~15.5KB to ~10-11KB.
2. **Fix `_build_phase_prompt()` dead code** — Remove the identical is_local branching for refine and plan phases.
3. **Audit agent mode commands** — Remove content from `.claude/commands/` files that duplicates what the orchestrator already passes via prompts.

### Phase 2: Shared Prompt Library (Medium Risk)

4. **Create `shared/egg_prompts/`** — Extract review criteria, autofix rules, and conventions into a shared Python module.
5. **Update orchestrator** — Import from shared module instead of inline strings.
6. **Update GH Actions scripts** — Add a Python wrapper that calls the shared module and outputs text for the bash scripts to use.
7. **Add tests** — Ensure prompt content parity between flows.

### Phase 3: Conditional CLAUDE.md Assembly (Medium Risk)

8. **Modify `setup_agent_rules()`** — Accept context parameter for conditional assembly.
9. **Update orchestrator spawn logic** — Pass agent context to container startup.
10. **Update GH Actions `gha_exec()`** — Determine context from task type.

### Constraints and Dependencies

- **Testing**: Prompt changes are hard to test — rely on integration tests and manual verification of agent behavior.
- **Backwards compatibility**: The `.claude/commands/` are used by the interactive `egg-sdlc` CLI, not just the orchestrator. Changes must preserve interactive use.
- **Shared module path**: The `PYTHONPATH` in GH Actions already includes `shared/`, so a new `shared/egg_prompts/` module is immediately importable.
- **GH Actions security**: Prompt builders run from trusted `main` branch, so shared modules are also trusted.

### Open Questions

1. **Should GH Actions agents even get CLAUDE.md?** Currently they do (via the sandbox container). Much of CLAUDE.md is about the SDLC pipeline workflow, which GH Actions agents don't use. A stripped-down version could save significant tokens.
2. **How much token reduction is enough?** The current ~3,800 tokens for CLAUDE.md is modest. The bigger savings come from ensuring GH Actions prompts don't include irrelevant SDLC/orchestrator content.
3. **Should `.claude/commands/` be kept?** They're useful for interactive mode but redundant in orchestrated mode. One option: keep them for interactive use but don't include them when running in orchestrated pipeline mode.

---

## Summary

The prompt system has grown organically with two parallel implementations (orchestrator Python + GH Actions bash). The main issues are:

1. **Token waste**: ~1,500-2,000 tokens of irrelevant CLAUDE.md content per GH Actions invocation
2. **Drift risk**: Review criteria, autofix rules, and conventions are maintained independently in two codebases
3. **Redundancy**: Agent mode commands overlap with orchestrator-built prompts

The recommended approach is a three-phase plan: trim first (quick wins), then share prompt content (unify criteria), then make CLAUDE.md assembly conditional (reduce per-invocation waste). Phase 1 can be done independently with minimal risk.

Authored-by: egg
