# Analysis: Tune claude.md files and prompts

> Issue: #659 | Phase: refine

## Problem Statement

The system uses CLAUDE.md files and dynamically-generated prompts across two execution paths: GitHub Actions (GHA) workflows and the local orchestrator. These have diverged over time, leading to three problems:

1. **Token waste**: The base CLAUDE.md (~15.5 KB, 423 lines) is loaded for every agent session regardless of role. It contains redundant content (git worktree warnings appear 3 times, merge-blocking policy appears 4 times) and exhaustive CLI references that most agents rarely need.

2. **Prompt duplication**: GHA shell scripts (`action/build-*.sh`, 1,526 lines) and orchestrator Python functions (`orchestrator/routes/pipelines.py`, ~500 lines of prompt builders) independently generate prompts for the same roles (reviewer, autofixer, contract verifier) with different wording, different conventions, and different output formats.

3. **Role misalignment**: The multi-agent architecture gives each agent a specialized duty, but all agents receive identical CLAUDE.md content. A TESTER agent gets 76 lines of orchestrator CLI reference it will never use. A REVIEWER agent gets commit/push workflow instructions that its restrictions explicitly forbid.

The desired outcome is a leaner, unified prompt system where:
- Each agent receives only the context it needs
- GHA and orchestrator use the same prompt generators for equivalent roles
- Prompt generation code lives in the sandbox codebase, not in GHA workflow scripts

## Current Behavior

### CLAUDE.md Assembly

The base CLAUDE.md is assembled at container startup by `sandbox/entrypoint.py:setup_agent_rules()` (lines 678-716). It concatenates 7 rule files from `sandbox/.claude/rules/` in fixed order:

| File | Lines | Content |
|------|-------|---------|
| `mission.md` | 135 | Core mission, workflow, git patterns, PR lifecycle, decision framework |
| `environment.md` | 71 | Sandbox constraints, network modes, gateway info |
| `code-standards.md` | 10 | Tech stack and style |
| `test-workflow.md` | 16 | Test execution patterns |
| `pr-descriptions.md` | 20 | PR format guidelines |
| `contract.md` | 71 | `egg-contract` CLI reference (26 lines of commands + examples) |
| `orchestrator.md` | 76 | `egg-orch` CLI reference (26 commands listed) |

All agents get the same combined output (~15.5 KB). No role-based filtering exists.

### GHA Prompt Generation

7 shell scripts in `action/` generate prompts for GHA-triggered workflows:

| Script | Lines | Role |
|--------|-------|------|
| `build-review-prompt.sh` | 187 | PR code review |
| `build-autofixer-prompt.sh` | 131 | Fix failing CI checks |
| `build-conflict-prompt.sh` | 280 | Resolve merge conflicts |
| `build-contract-verification-prompt.sh` | 218 | Verify SDLC contract completion |
| `build-doc-updater-prompt.sh` | 446 | Update docs after code changes |
| `build-agent-mode-design-review-prompt.sh` | 179 | Agent-mode design alignment review |
| `build-feedback-prompt.sh` | 85 | Address review feedback |

These scripts also reference 3 convention documents (`autofixer-conventions.md`, `review-conventions.md`, `conflict-conventions.md`, totaling 461 lines).

### Orchestrator Prompt Generation

Python functions in `orchestrator/routes/pipelines.py` generate prompts for sandbox agents:

| Function | ~Lines | Role |
|----------|--------|------|
| `_build_phase_prompt()` | 200 | CODER/REFINER for refine/plan/implement phases |
| `_build_review_prompt()` | 95 | REVIEWER (6 subtypes: unified, code, contract, agent-design, refine, plan) |
| `_build_checker_prompt()` | 74 | Run tests/lint |
| `_build_autofix_prompt()` | 61 | Fix check failures |
| `_build_agent_prompt()` | 200 | Role-specific prompts for TESTER, DOCUMENTER, INTEGRATOR, ARCHITECT, etc. |

### Multi-Agent Slash Commands

8 files in `sandbox/.claude/commands/` (715 lines total) provide interactive mode instructions: `coder-mode.md`, `tester-mode.md`, `documenter-mode.md`, `integrator-mode.md`, `sdlc.md`, `onboarding-docs.md`, `show-metrics.md`.

### Key Duplication Points

**Reviewer prompts**: GHA `build-review-prompt.sh` and orchestrator `_build_review_prompt()` both define review criteria (security, correctness, design) with different wording. GHA posts via `gh pr review`; orchestrator writes JSON verdicts to `.egg-state/reviews/`.

**Autofixer prompts**: GHA `build-autofixer-prompt.sh` and orchestrator `_build_autofix_prompt()` both define auto-fixable categories. GHA reads failures via `gh pr checks`; orchestrator reads from pre-computed JSON.

**Contract verification**: GHA `build-contract-verification-prompt.sh` and orchestrator `_build_review_prompt()` (contract subtype) both verify task completion against criteria. Different output formats.

**No shared code**: There are zero shared modules between GHA and orchestrator prompt generation. `sandbox/egg_lib/context.py` bridges infrastructure config but not prompts. No TODOs or ADRs mention unification.

## Constraints

- **Backward compatibility**: GHA workflows are live in production. Changes to prompt scripts must not break existing PR review, autofix, and conflict resolution flows.
- **Security model**: GHA workflows checkout prompt scripts from `main` branch to prevent untrusted PR code from exfiltrating tokens. Any refactoring must preserve this trust boundary.
- **Agent-mode design**: The project follows an agent-mode design philosophy where prompts should be minimal and agents fetch context themselves. Prompt bloat contradicts this principle.
- **Token budget**: CLAUDE.md is loaded into every session. At ~15.5 KB, it consumes significant context window capacity before any task-specific prompt is added. The orchestrator prompts add another ~2-5 KB per agent.
- **Testing**: GHA prompt scripts have integration tests (`tests/action/test_build_*.py`). Changes must maintain test coverage.
- **Container build pipeline**: CLAUDE.md rules are copied at Docker build time and assembled at container startup. Role-specific customization requires changes to the entrypoint or the build process.

## Options Considered

### Option A: Consolidate prompts into Python, keep shell wrappers thin

**Approach**: Create a shared Python prompt library (`shared/egg_prompts/`) containing all prompt builders for every role. GHA shell scripts become thin wrappers that call the Python library (e.g., `python -m egg_prompts.build reviewer --pr 123`). The orchestrator imports the same library directly.

**Pros**:
- Single source of truth for all prompts across both execution paths
- Python is easier to test, maintain, and refactor than shell scripts
- Convention documents can be embedded or loaded by the library
- Type-safe, testable prompt construction

**Cons**:
- Requires Python runtime in GHA environment (already available via the Docker action image)
- Shell-to-Python migration is a significant refactor
- GHA trust model needs careful handling (Python library must still be loaded from `main`)

### Option B: Keep shell and Python separate, extract shared conventions

**Approach**: Move shared content (review criteria, autofixer categories, contract verification rules) into convention documents in a shared location (e.g., `shared/conventions/`). Both GHA shell scripts and orchestrator Python read from these files. Prompts remain in their respective languages.

**Pros**:
- Smaller change surface; less risk to existing workflows
- Conventions are DRY without restructuring prompt generation code
- Preserves the existing GHA security model unchanged

**Cons**:
- Prompt structure and wording still diverge between GHA and orchestrator
- Shell scripts remain hard to test and maintain
- Two codebases to maintain for prompt generation logic

### Option C: Migrate GHA workflows to use orchestrator-spawned sandbox agents

**Approach**: Instead of GHA building prompts and running Claude directly, have GHA workflows call the orchestrator API to spawn sandbox agents with the appropriate role. The orchestrator already handles reviewer, autofixer, and checker roles. Extend it to handle conflict resolution, doc updates, and feedback addressing.

**Pros**:
- Complete unification: one prompt generation path for all agents
- GHA workflows become simple trigger→spawn dispatchers
- New roles are added in one place
- Leverages existing sandbox infrastructure (CLAUDE.md, entrypoint, gateway)

**Cons**:
- Major architectural change requiring orchestrator API to be reachable from GHA
- Conflict resolver, doc updater, and feedback addresser are currently GHA-only and would need orchestrator implementations
- Adds latency (GHA → orchestrator → sandbox vs GHA → Claude directly)
- The orchestrator is currently designed for SDLC pipelines, not ad-hoc GHA tasks

### Option D: Trim CLAUDE.md and add role-based filtering only (minimal approach)

**Approach**: Focus solely on token efficiency. Deduplicate redundant content in CLAUDE.md, compress CLI references, and add role-based filtering to `setup_agent_rules()` so each agent only receives relevant sections. Don't unify GHA and orchestrator prompts.

**Pros**:
- Fastest to implement; lowest risk
- Directly addresses token waste
- No changes to GHA workflows or orchestrator prompt builders

**Cons**:
- Doesn't address prompt duplication between GHA and orchestrator
- Doesn't move prompt generation out of GHA
- Defers the structural problem

## Recommended Approach

**Option A (Consolidate into Python)** is recommended, combined with the CLAUDE.md trimming from Option D as an immediate first step.

Rationale:
1. The issue explicitly asks to "ensure we're using the exact same prompt generators for each flow" and to "move [prompt generation] out of github actions and into the sandbox codebase." Option A directly satisfies both requirements.
2. Option B leaves prompt divergence in place. Option C is architecturally sound but is a much larger change (requires orchestrator API availability from GHA and implementing 3 new agent roles in the orchestrator). Option D ignores the unification requirement.
3. The Python prompt library can be structured as a package in `shared/egg_prompts/` and imported by both the orchestrator and a thin CLI entry point that GHA scripts invoke. This preserves the GHA security model (the library is loaded from `main` via checkout) while eliminating duplication.
4. CLAUDE.md trimming (Option D) is complementary and should happen regardless of the prompt unification strategy. It can be done first to capture immediate token savings.

### CLAUDE.md trimming targets:
- Consolidate 3 git worktree warnings into 1 (save ~150 words)
- Consolidate 4 merge-blocking policy mentions into 1-2 (save ~100 words)
- Compress `orchestrator.md` CLI reference from 76 lines to ~25 lines (most agents use 3-4 commands)
- Compress `contract.md` from 71 lines to ~30 lines
- Add role-based filtering in `entrypoint.py` so REVIEWER agents skip commit/push workflow sections, TESTER agents skip orchestrator CLI, etc.
- Target: ~11 KB (30% reduction) while preserving 95%+ utility

### Prompt unification structure:
```
shared/egg_prompts/
├── __init__.py
├── base.py              # Common prompt structures, context headers
├── conventions/
│   ├── autofixer.md     # Moved from action/
│   ├── review.md        # Moved from action/
│   └── conflict.md      # Moved from action/
├── builders/
│   ├── reviewer.py      # Unified review prompt builder
│   ├── autofixer.py     # Unified autofixer prompt builder
│   ├── conflict.py      # Conflict resolution prompt builder
│   ├── contract.py      # Contract verification prompt builder
│   ├── doc_updater.py   # Doc updater prompt builder
│   ├── feedback.py      # Feedback addresser prompt builder
│   └── phase.py         # SDLC phase prompts (refine/plan/implement)
└── cli.py               # CLI entry point for GHA scripts
```

GHA scripts would be replaced by:
```bash
python -m egg_prompts.cli reviewer --pr $PR_NUMBER --repo $GITHUB_REPOSITORY
```

## Open Questions

1. **Role-based CLAUDE.md filtering granularity**: Should agents receive a minimal CLAUDE.md per role (e.g., REVIEWER gets only mission + environment, no commit workflow), or should all agents continue to get most content with only the most irrelevant sections removed? Finer filtering saves more tokens but adds complexity to `entrypoint.py`.

2. **GHA execution model**: Should GHA workflows continue to run Claude directly (with the Python prompt library generating the prompt), or should they spawn sandbox containers via the orchestrator? The former is simpler; the latter would fully unify execution paths but requires orchestrator API accessibility from GHA.

3. **Scope of "same prompt generators"**: The issue says "ensure we're using the exact same prompt generators for each flow." Does this mean the prompt content must be identical, or is it acceptable for the same Python function to produce slightly different prompts based on execution context (e.g., GHA reviewer posts via `gh pr review` while orchestrator reviewer writes JSON verdicts)?

4. **Conflict resolver, doc updater, feedback addresser**: These 3 roles exist only in GHA today. Should they be implemented in the orchestrator as well, or is it acceptable to unify their prompt generation code without adding them to the SDLC pipeline?

5. **Slash commands vs orchestrator prompts**: The slash commands (`sandbox/.claude/commands/*.md`) provide interactive-mode agent instructions that partially overlap with orchestrator `_build_agent_prompt()` output. Should these be generated from the same source, or is it acceptable to keep them separate since they serve different use cases (interactive vs automated)?

---

*Authored-by: egg*
