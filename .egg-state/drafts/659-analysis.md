# Analysis: Tune claude.md files and prompts

> Issue: #659 | Phase: refine

## Problem Statement

The prompt and context management system has grown organically across two execution environments (GitHub Actions and local orchestrator) without a unified architecture. This creates three problems:

1. **Prompt divergence**: The same logical operations (code review, autofix, conflict resolution, contract verification) use different prompt generators in GHA (7 bash scripts in `action/`) vs the local orchestrator (5 Python functions in `orchestrator/routes/pipelines.py`). These have drifted apart in content, structure, and quality.

2. **Token waste**: The assembled `CLAUDE.md` (~15.6KB) is loaded for every agent invocation, regardless of agent role. A TESTER agent receives the full mission, PR workflow, contract CLI, orchestrator CLI, and code standards — most of which is irrelevant to writing tests. Similarly, an internal REVIEWER receives instructions about committing code and creating PRs that it cannot use.

3. **Duplication and maintenance burden**: Rule files exist identically in two directories (`sandbox/claude-rules/` and `sandbox/.claude/rules/`, 18KB each — byte-for-byte identical). Prompt logic is implemented twice: once in bash (`action/build-*-prompt.sh`, ~61KB total) and once in Python (`routes/pipelines.py`).

## Current Behavior

### CLAUDE.md Assembly

At container startup, `sandbox/entrypoint.py:678-716` (`setup_agent_rules()`) combines 7 modular rule files from `/opt/claude-rules/` (copied from `sandbox/claude-rules/` during Docker build) into a single `~/CLAUDE.md`:

- `mission.md` (5.4KB) — Core agent role, workflow, PR lifecycle, git safety
- `environment.md` (2.8KB) — Sandbox constraints, network modes, capabilities
- `code-standards.md` (0.4KB) — Tech stack and style guidelines
- `test-workflow.md` (0.6KB) — Testing framework reference
- `pr-descriptions.md` (0.4KB) — PR description format
- `contract.md` (2.7KB) — SDLC contract CLI reference
- `orchestrator.md` (3.2KB) — Orchestrator CLI reference

This combined file (`~/CLAUDE.md`, 15.6KB) is symlinked to `~/repos/CLAUDE.md`, making it visible to Claude Code for every agent invocation regardless of role.

### GitHub Actions Prompt Generators

Seven bash scripts in `action/` build prompts for GHA-triggered workflows:

| Script | Size | Purpose | Triggered by |
|--------|------|---------|-------------|
| `build-review-prompt.sh` | 7.9KB | Code review | `reusable-review.yml` |
| `build-autofixer-prompt.sh` | 4.7KB | Fix failing checks | `reusable-autofix.yml` |
| `build-conflict-prompt.sh` | 11.3KB | Merge conflict resolution | `reusable-conflict-resolve.yml` |
| `build-contract-verification-prompt.sh` | 8.2KB | Contract verification | `on-pull-request-contract-verify.yml` |
| `build-doc-updater-prompt.sh` | 17.4KB | Documentation updates | `on-push-doc-updater.yml` |
| `build-feedback-prompt.sh` | 3.3KB | Address review feedback | `on-review-feedback.yml` |
| `build-agent-mode-design-review-prompt.sh` | 8.2KB | Agent-mode design review | `on-pull-request-agent-mode-design.yml` |

These scripts load dynamic context (review rules from `.egg/review-rules.md`, conventions from `action/*-conventions.md`) and output prompt files. They run from a trusted `main` checkout for security.

### Local Orchestrator Prompt Generators

Five Python functions in `orchestrator/routes/pipelines.py`:

| Function | Lines | Purpose |
|----------|-------|---------|
| `_build_phase_prompt()` | 1232-1540 | Phase execution (refine, plan, implement, pr) |
| `_build_agent_prompt()` | 1548-1750 | Multi-agent role dispatch (tester, documenter, etc.) |
| `_build_review_prompt()` | 1024-1119 | Internal review (unified, contract, code, etc.) |
| `_build_checker_prompt()` | 2100-2174 | Test/lint discovery and execution |
| `_build_autofix_prompt()` | 2177-2238 | Fix failing checks |

These Python functions do NOT load the same rules/conventions as the bash scripts. They generate simpler, more structured prompts with JSON verdict formats for internal review loops.

### Duplicate Rule Directories

`sandbox/claude-rules/` and `sandbox/.claude/rules/` contain identical files (verified by `diff -rq`). The Dockerfile copies from `claude-rules/` to `/opt/claude-rules/`. The `.claude/rules/` directory exists for Claude Code's native rules loading (when running in development), creating a maintenance burden where changes must be made in both locations.

## Constraints

- **Security**: GHA prompt builders run from a trusted `main` checkout to prevent prompt injection from PRs. Any refactoring must preserve this security boundary. The sandbox codebase itself is part of the trusted codebase, so moving prompt generation there does not weaken security — the action entry point would still checkout `main` to get the prompt code.
- **Backward compatibility**: GHA workflows are used by consuming repos (not just `egg` itself). Changes to the action interface (inputs/outputs) need to be coordinated.
- **Two execution modes**: The local orchestrator runs agents in Docker containers with full multi-agent wave support. GHA runs single agents per job. Prompt generators must work for both.
- **Claude Code CLAUDE.md loading**: Claude Code automatically loads `CLAUDE.md` files from the working directory hierarchy. We cannot selectively load different files per role through this mechanism alone — CLAUDE.md is always loaded in full.
- **Agent-mode design principles**: Prompts should provide objectives, not micromanage procedures. Agents should fetch what they need rather than having data pre-loaded.
- **Conventions are customizable**: The `.egg/*-rules.md` and `action/*-conventions.md` files allow per-repo customization. This needs to be preserved.

## Options Considered

### Option A: Unified Python Prompt Library in Sandbox

**Approach**: Create a Python prompt library in the sandbox codebase (`sandbox/prompts/` or `shared/prompts/`) that contains all prompt generation logic. Both the local orchestrator and GHA workflows call the same Python functions. GHA workflows invoke the prompt generators via a thin shell wrapper (e.g., `python -m prompts.review --pr 123 --repo owner/name`). Remove the bash scripts.

**Pros**:
- Single source of truth for all prompts
- Python is more maintainable than bash for string assembly
- Easy to test with pytest
- Natural integration with the orchestrator
- Supports rule/convention file loading from both contexts

**Cons**:
- GHA needs Python available in the runner (already true — egg action installs it)
- Shell wrapper adds a thin layer but is minimal
- Need to handle GHA-specific context (GitHub Actions env vars) in the Python code

### Option B: Shared Markdown Templates with Dual Renderers

**Approach**: Extract prompt content into shared Markdown template files with placeholders. Both bash and Python renderers consume the same templates. Keep both rendering approaches but centralize the content.

**Pros**:
- Minimal code changes — keeps existing bash/Python infrastructure
- Content is centralized in templates
- Easy to review/edit prompt content

**Cons**:
- Two rendering codepaths still exist and can diverge in behavior
- Template language adds complexity
- Doesn't address token efficiency (CLAUDE.md is still monolithic)
- Maintenance burden of two renderers persists

### Option C: Role-Scoped CLAUDE.md with Unified Python Prompts

**Approach**: Combine Option A (unified Python prompts) with role-aware CLAUDE.md generation. Instead of one monolithic CLAUDE.md, generate a role-specific CLAUDE.md at container startup based on `EGG_AGENT_ROLE`. A TESTER gets mission basics + test workflow + code standards. A REVIEWER gets mission basics + review criteria. Agents only see the context relevant to their role.

**Pros**:
- All benefits of Option A
- Significant token savings (~30-60% reduction per invocation for specialized roles)
- Each agent gets focused, relevant context
- Follows the multi-agent specialization philosophy
- Reduces "noise" that could confuse or distract agents

**Cons**:
- More complex startup logic
- Need to carefully define which rules apply to which roles
- Risk of accidentally omitting needed context for a role
- Must test each role permutation
- `EGG_AGENT_ROLE` is not always set (interactive sessions need full context)

## Recommended Approach

**Option C: Role-Scoped CLAUDE.md with Unified Python Prompts** addresses all three problems identified in the issue:

1. **Unifies prompt generation** — Single Python library replaces 7 bash scripts + 5 Python functions
2. **Reduces token waste** — Role-scoped CLAUDE.md eliminates irrelevant context per agent
3. **Consolidates into sandbox** — All prompt code lives in the sandbox codebase per the issue requirement

The role-scoping for CLAUDE.md can be conservative initially: start with "full" (everything, for interactive/unknown roles) and "minimal" (mission core only, for specialized agents like reviewers/checkers). More granular scoping can be added incrementally.

For GHA integration, the egg action's `entrypoint.sh` would call the Python prompt generator instead of bash scripts. The security model remains the same — `main` is checked out before running the prompt generator.

## Open Questions

1. **Prompt quality audit scope**: Should we also audit the tone and content of prompts for effectiveness (e.g., are reviewer prompts producing useful reviews, are refine prompts producing good analysis)? Or focus strictly on structural consolidation and token efficiency?

2. **Convention file loading**: Should the Python prompt library support loading `.egg/*-rules.md` and `action/*-conventions.md` files for GHA flows, or should these be folded into the Python code as defaults with override support?

3. **Backward compatibility timeline**: Some consuming repos may call `action/build-review-prompt.sh` directly. Should we keep the bash scripts as thin wrappers around the Python library during a transition period, or remove them immediately?

4. **Role-scoping granularity**: For CLAUDE.md role scoping, should we define scopes per-role (CODER, TESTER, REVIEWER, etc.) or per-category (worker, reviewer, utility)?

---

*Authored-by: egg*
