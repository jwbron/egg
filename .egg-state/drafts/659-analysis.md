# Analysis: Tune claude.md files and prompts

> Issue: #659 | Phase: refine

## Problem Statement

The egg platform's prompt and CLAUDE.md system has grown across two parallel execution environments (local orchestrator and GitHub Actions). The issue requests an audit with two goals, in priority order: (1) ensure prompt quality and correctness — each agent gets instructions appropriate to its specialized role within the multi-agent architecture, and (2) improve token efficiency by removing unnecessary context.

Additionally, the issue explicitly requests that prompt generation logic be moved out of GitHub Actions bash scripts and into the sandbox codebase, ensuring both execution paths use the same prompt generators.

## Current Behavior

### CLAUDE.md Assembly

CLAUDE.md is assembled at container startup in `sandbox/entrypoint.py` (lines 683–707) from snippet files in `sandbox/.claude/rules/`:

| File | Lines | Inclusion | Primary Audience |
|------|-------|-----------|-----------------|
| `mission.md` | 135 | Always | CODER (workflow, git, PRs, commit attribution) |
| `environment.md` | 71 | Always | All (sandbox constraints, network, gateway) |
| `code-standards.md` | 10 | Always | All (tech stack, linting) |
| `test-workflow.md` | 16 | Always | TESTER, CODER |
| `pr-descriptions.md` | 20 | Always | CODER only (PR format template) |
| `contract.md` | 71 | Pipeline only | Pipeline agents (egg-contract CLI) |
| `orchestrator.md` | 76 | Pipeline only | Pipeline agents (egg-orch CLI) |
| `README.md` | 69 | **Never** (not in `rules_order`) | Documents the rules system |

The `rules_order` list (lines 683–689) defines which files are assembled. Lines 691–693 conditionally extend it for pipeline mode:

```python
if os.environ.get("EGG_PIPELINE_ID"):
    rules_order.extend(["contract.md", "orchestrator.md"])
```

**Actual line counts delivered to agents:**
- **Non-pipeline agents**: 252 lines (mission + environment + code-standards + test-workflow + pr-descriptions)
- **Pipeline agents**: 399 lines (adds contract + orchestrator)

There is **no role-based filtering** — all agents (coder, tester, documenter, reviewer, checker) receive the same CLAUDE.md for their mode.

### Existing Shared Prompt Infrastructure (`shared/prompts/`)

A shared criteria directory already exists at `shared/prompts/` with 4 files (128 lines total):

| File | Lines | Purpose | User Override |
|------|-------|---------|---------------|
| `agent-design-criteria.md` | 29 | Agent-mode design review criteria | None (platform invariant) |
| `autofixer-rules.md` | 16 | Auto-fixable vs report-only rules | `.egg/autofixer-rules.md` |
| `code-review-criteria.md` | 49 | Comprehensive code review guidelines | `.egg/review-rules.md` |
| `contract-review-criteria.md` | 34 | Contract verification rules | `.egg/contract-rules.md` |

These files are consumed by both systems through a shared loading pattern:

**Orchestrator** (`orchestrator/routes/pipelines.py`):
- `_read_shared_criteria()` (line 770) provides a centralized 3-tier fallback loader: user override in `.egg/` → `shared/prompts/<file>` (source tree) → `/app/prompts/<file>` (Docker container)
- The orchestrator Dockerfile copies `shared/prompts/` to `/app/prompts/` (Dockerfile line 25)
- Specialized getters: `_get_agent_design_criteria()` (line 809), `_get_code_review_criteria()` (line 835), `_get_contract_review_criteria()` (line 868)
- `_build_autofix_prompt()` (line 2238) loads `autofixer-rules.md` via the same mechanism

**GitHub Actions** (4 of 7 bash scripts in `action/`):
- `build-review-prompt.sh` — loads `code-review-criteria.md`
- `build-autofixer-prompt.sh` — loads `autofixer-rules.md`
- `build-contract-verification-prompt.sh` — loads `contract-review-criteria.md`
- `build-agent-mode-design-review-prompt.sh` — loads `agent-design-criteria.md`

Each bash script independently reimplements the same fallback pattern (user override → shared file → inline fallback) in shell. The remaining 3 scripts (`build-conflict-prompt.sh`, `build-doc-updater-prompt.sh`, `build-feedback-prompt.sh`) do not use shared criteria files.

**Test coverage**: Comprehensive tests exist in `orchestrator/tests/test_pipeline_prompts.py` (389 lines) covering shared file loading, user overrides, inline fallbacks, Docker path fallback, and format-agnosticity verification. Four additional test files in `tests/action/test_build_*.py` (~1,237 lines total) test the bash scripts.

### Prompt Generation: Two Parallel Systems

**Local Orchestrator** (Python, `orchestrator/routes/pipelines.py`):

| Function | Line | Purpose |
|----------|------|---------|
| `_build_review_prompt()` | 1090 | Internal reviewer prompts (refine/plan/implement) |
| `_build_phase_prompt()` | 1299 | Phase-specific worker prompts (refine, plan, implement, pr) |
| `_build_agent_prompt()` | 1606 | Role-specific multi-agent prompts (tester, documenter, integrator) |
| `_build_checker_prompt()` | 2161 | Test/lint checker agent |
| `_build_autofix_prompt()` | 2238 | Autofixer agent |

`_build_agent_prompt()` delegates to `_build_phase_prompt()` for coder/refiner roles, and generates role-specific instructions for tester, documenter, and integrator roles.

**GitHub Actions** (Bash scripts, `action/`):
- `build-review-prompt.sh` — PR code review
- `build-agent-mode-design-review-prompt.sh` — design pattern review
- `build-autofixer-prompt.sh` — fix failing PR checks
- `build-conflict-prompt.sh` — merge conflict resolution
- `build-contract-verification-prompt.sh` — contract compliance
- `build-doc-updater-prompt.sh` — documentation updates
- `build-feedback-prompt.sh` — address review feedback

### What Is Already Shared vs What Diverges

The **review criteria content** is already shared via `shared/prompts/`. Both systems load the same criteria files for the 4 tasks that have them. The criteria files are intentionally format-agnostic (no `gh` commands, no `GITHUB_OUTPUT` references), verified by tests.

What **diverges** is the surrounding prompt structure: task instructions, context injection, output format directives, and workflow-specific logic. Each bash script independently constructs the full prompt around the shared criteria, and this construction logic has no Python equivalent (or vice versa for orchestrator-only prompts).

| Agent Type | Orchestrator | GHA Script | Criteria Shared? | Prompt Structure Shared? |
|------------|-------------|------------|------------------|--------------------------|
| Code Reviewer | `_build_review_prompt()` | `build-review-prompt.sh` | Yes | No |
| Design Reviewer | N/A | `build-agent-mode-design-review-prompt.sh` | Yes (via getter) | N/A |
| Autofixer | `_build_autofix_prompt()` | `build-autofixer-prompt.sh` | Yes | No |
| Contract Verifier | via `_build_review_prompt(reviewer_type="contract")` | `build-contract-verification-prompt.sh` | Yes | No |
| Conflict Resolver | N/A | `build-conflict-prompt.sh` | N/A | N/A |
| Feedback Addresser | N/A | `build-feedback-prompt.sh` | N/A | N/A |
| Doc Updater | N/A | `build-doc-updater-prompt.sh` | N/A | N/A |

## Constraints

- **Claude Code auto-loads CLAUDE.md**: No CLI flag to select a custom system prompt file. Claude Code reads `~/CLAUDE.md` automatically at startup. Role-based filtering must happen before this file is written.
- **Container startup is per-agent**: Each agent gets its own container with `EGG_AGENT_ROLE` available as an environment variable, making role-based CLAUDE.md assembly technically feasible.
- **Backward compatibility**: The `action/` bash scripts are used by consumer repos via `uses: jwbron/egg/action@main`. Changes must not break external consumers.
- **Quality first**: The issue explicitly prioritizes correctness over token savings. Prompt changes must not degrade agent behavior.
- **Multi-agent architecture**: Agents check each other (testers check coders, reviewers check all workers). Prompts should reinforce role boundaries.
- **Format-agnosticity of shared criteria**: The `shared/prompts/` files contain pure review/verification logic without output-format-specific content. This design must be preserved.
- **User override mechanism**: The `.egg/<file>` per-repo customization must continue to work across all consumption paths.
- **Existing test coverage**: Changes must maintain or migrate the existing test suites for both orchestrator and action prompt builders.

## Options Considered

### Option A: Role-Based CLAUDE.md Filtering Only

**Approach**: Modify `setup_agent_rules()` in `sandbox/entrypoint.py` to read `EGG_AGENT_ROLE` and include only relevant rule files per role.

Candidates for omission from the 252-line always-included core:
- `pr-descriptions.md` (20 lines): Only relevant to agents that create PRs (coder role). Reviewers, testers, checkers, documenters never create PRs.
- `test-workflow.md` (16 lines): Only relevant to agents that run tests (coder, tester, checker). Reviewers and documenters don't run tests.
- Sections of `mission.md` (135 lines, 54% of core): Contains PR lifecycle, review response, and commit attribution sections that are coder-specific. However, splitting this file adds complexity.

Maximum savings from file-level filtering: ~36 lines (~14% of the 252-line core) for roles like reviewer or documenter that don't create PRs or run tests.

**Pros**:
- Straightforward implementation — role mapping + env var check
- Reduces irrelevant context for specialized agents
- Preserves existing modular rule file structure

**Cons**:
- Limited savings (14% for specialized roles, 0% for coder)
- `mission.md` (the largest file) contains a mix of role-specific and universal content; splitting it increases maintenance burden
- Does not address the GHA/orchestrator prompt unification goal

### Option B: Unify GHA Prompt Builders into Python

**Approach**: Move prompt generation logic from the 7 GHA bash scripts into Python modules within the shared or sandbox codebase. GHA workflows would call a Python entrypoint instead of bash scripts. This extends the existing `shared/prompts/` infrastructure from sharing criteria content to sharing the complete prompt construction logic.

**Pros**:
- Single source of truth for all prompt logic — eliminates structural divergence
- Leverages and extends the existing `_read_shared_criteria()` infrastructure
- Directly fulfills the issue's request to "move all prompt and claude.md generation out of github actions"
- Python is easier to test than complex bash scripts with heredocs
- The 4 scripts already using shared criteria are natural migration targets

**Cons**:
- Requires changes to GHA workflow YAML files to invoke Python instead of bash
- GHA environment differs from sandbox (no container, different env vars) — the Python module must work in both contexts
- Migration risk: must verify prompt equivalence during transition
- The 3 scripts without shared criteria have simpler logic but still need migration

### Option C: Content Audit Only (Minimal Change)

**Approach**: Keep the two-system architecture but audit and tighten prompt content. Review each CLAUDE.md snippet for conciseness, verify phase prompts give focused instructions, and align the inline fallback rules between bash scripts and orchestrator to reduce behavioral divergence.

**Pros**:
- Lowest risk — content changes only, no infrastructure changes
- Directly addresses the primary concern (quality and correctness)
- Can be done incrementally, file by file
- Quick to validate with existing tests

**Cons**:
- Does not address prompt structural divergence between GHA and orchestrator
- Does not fulfill the issue's requirement to move generation "into the sandbox codebase"
- Content improvements may drift again without structural enforcement

### Option D: Combined — Content Audit + Role Filtering + Prompt Unification

**Approach**: Address all three aspects in two stages:

**Stage 1 — Audit, refine, and filter**: Review each CLAUDE.md snippet and prompt-building function for quality, role-appropriateness, and conciseness. Add role-based filtering in `setup_agent_rules()` to omit clearly role-irrelevant files (e.g., `pr-descriptions.md` for reviewer/checker/documenter agents, `test-workflow.md` for reviewer/documenter agents). Tighten `mission.md` content where possible. Savings: ~36 lines (14%) for specialized roles from file-level filtering, plus additional savings from content tightening.

**Stage 2 — Unify prompt generation**: Migrate GHA bash prompt builders into Python, extending the existing `shared/prompts/` and `_read_shared_criteria()` infrastructure. The 4 scripts already using shared criteria files are natural starting points. GHA workflows would call a thin Python entrypoint instead of bash scripts.

**Pros**:
- Addresses quality, efficiency, and unification
- Builds on existing `shared/prompts/` infrastructure rather than creating new patterns
- Staged approach reduces risk
- Comprehensive solution for all issue goals

**Cons**:
- Largest scope
- Stage 2 requires careful testing to ensure prompt equivalence during migration

## Recommended Approach

**Option D (Combined)** is recommended. It addresses all three goals from the issue:

1. **Quality and correctness** (Stage 1): Audit and tighten prompt content, verify role-appropriateness of each snippet and prompt function.
2. **Token efficiency** (Stage 1): Role-based filtering saves ~36 lines for specialized agents. Content tightening in `mission.md` (135 lines, the largest snippet at 54% of core) may yield additional savings.
3. **Prompt unification** (Stage 2): Moving GHA bash scripts to Python eliminates structural drift and creates a single source of truth, extending the existing shared criteria infrastructure.

The existing `shared/prompts/` directory with its 3-tier fallback chain, user overrides, format-agnostic criteria files, and comprehensive test coverage (both orchestrator and action tests) provides a proven foundation to build on.

## Open Questions

1. **How should GHA workflows invoke the unified Python prompt module?** Options include:
   - Install the shared module as a pip package in the GHA runner
   - Use the egg Docker image in GHA with a prompt-generation entrypoint
   - Bundle the Python module directly in the GitHub Action

2. **Should `mission.md` be split into smaller, composable units?** At 135 lines (54% of the always-included core), it covers workflow, git operations, PR lifecycle, review responses, git safety, decision framework, non-interactive mode, and notifications. Splitting would enable finer-grained role filtering but increases file count and maintenance burden.

3. **Should Stage 1 (audit/filtering) be completed before Stage 2 (unification), or can they proceed in parallel?** The issue lists quality first, suggesting sequential ordering.

4. **Should inline fallbacks be retained in the Python implementation?** The bash scripts currently embed inline fallback criteria for rollout safety. The Python `_read_shared_criteria()` already has a 3-tier fallback (user override → shared file → Docker path). Adding a 4th tier (inline fallback) matches the bash pattern but adds maintenance burden.

---

*Authored-by: egg*
