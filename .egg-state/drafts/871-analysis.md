# Analysis: Namespace .egg-state files per-pipeline to prevent merge conflicts

> Issue: #871 | Phase: refine

## Problem Statement

The `.egg-state/` directory has inconsistent file namespacing. Most subdirectories (`contracts/`, `drafts/`, `reviews/`) use per-issue or per-pipeline prefixes in filenames (e.g., `850.json`, `850-analysis.md`), so concurrent pipelines on different branches never conflict when merged. However, two subdirectories still use globally-named files:

- `agent-outputs/`: `architect-output.json`, `integrator-output.json`, `risk_analyst-output.json`
- `checks/`: `implement-results.json`

When multiple pipelines run concurrently and their branches merge to main, these globally-named files produce merge conflicts because they contain issue-specific data at the same file path. PR #869 demonstrated this with 4 such conflicts.

The desired outcome is that all `.egg-state/` files use per-issue/pipeline prefixed filenames, eliminating merge conflicts entirely.

## Current Behavior

### Correctly namespaced directories (no conflicts)

| Directory | Pattern | Example | Code |
|-----------|---------|---------|------|
| `contracts/` | `{identifier}.json` | `850.json` | `shared/egg_contracts/loader.py:44` — `get_contract_path()` |
| `drafts/` | `{identifier}-{phase}.md` | `850-analysis.md` | `orchestrator/routes/pipelines.py:1060` — `_get_draft_path()` |
| `reviews/` | `{identifier}-{phase}-{type}-review.json` | `850-implement-code-review.json` | `orchestrator/routes/pipelines.py:1035` — `_verdict_path_for_type()` |

### Globally-named files (cause conflicts)

**`agent-outputs/` handoff files** — Written by agents via prompt instructions, read by the orchestrator:

| File | Writer | Reader | Writer code | Reader code |
|------|--------|--------|-------------|-------------|
| `architect-output.json` | Architect agent (prompt) | `_synthesize_plan_draft_from_outputs()` | `pipelines.py:2524` | `pipelines.py:4344-4345` |
| `integrator-output.json` | Integrator agent (prompt) | Downstream consumers | `pipelines.py:2509` | — |
| `risk_analyst-output.json` | Risk analyst agent (prompt) | `_synthesize_plan_draft_from_outputs()` | `pipelines.py:2590` | `pipelines.py:4346` |

There are also additional globally-named handoff files that were not listed in the issue but follow the same pattern via `load_agent_output()` and `save_agent_output()` in `shared/egg_contracts/orchestrator.py:323-369`:
- `coder-output.json` — Written by coder, read by tester/documenter/integrator
- `tester-output.json` — Written by tester, read by `_read_tester_gaps()` at `pipelines.py:1631` and by coder revision prompts at `pipelines.py:2196`, `pipelines.py:2790`
- `documenter-output.json` — Written by documenter

These additional files are also globally-named and would cause the same conflict pattern.

**`checks/` results file** — Written by the checker+autofixer agent, read by autofix prompt and referenced in documentation:

| File | Writer | Reader | Writer code | Reader code |
|------|--------|--------|-------------|-------------|
| `implement-results.json` | Checker agent (prompt) | Autofix prompt, health checks | `pipelines.py:4063` | `pipelines.py:4136` (prompt ref) |

### How paths are currently constructed

**Prompt-based paths** (hardcoded strings in agent prompts):
- `pipelines.py:2524` — `"Write your analysis to .egg-state/agent-outputs/architect-output.json."`
- `pipelines.py:2509` — `"Write your integration report to .egg-state/agent-outputs/integrator-output.json."`
- `pipelines.py:2590` — `"Write your risk assessment to .egg-state/agent-outputs/risk_analyst-output.json."`
- `pipelines.py:4063` — `"write results to .egg-state/checks/implement-results.json"`
- `pipelines.py:4275` — Same in `_build_check_and_fix_prompt()`

**Programmatic paths** (Python code constructing paths):
- `shared/egg_contracts/orchestrator.py:333` — `load_agent_output()`: `repo_path / ".egg-state" / "agent-outputs" / f"{role.value}-output.json"`
- `shared/egg_contracts/orchestrator.py:364` — `save_agent_output()`: Same pattern
- `pipelines.py:4335` — `_synthesize_plan_draft_from_outputs()`: `outputs_dir / filename` where filename is `"architect-output.json"`
- `pipelines.py:1631` — `_read_tester_gaps()`: hardcoded `"tester-output.json"`

### Gateway and file access patterns

The gateway's file access enforcement uses directory-level patterns (e.g., `.egg-state/agent-outputs/`) rather than specific filenames:
- `gateway/agent_restrictions.py` — All agent roles have `".egg-state/agent-outputs/"` in their `allowed_write` patterns (lines 192, 243, 273, 301, 329, 373, 383, 393, 403, 449)
- `gateway/phase_filter.py:495,505` — REFINE and PLAN phases allow `".egg-state/agent-outputs/*"`
- `shared/egg_contracts/agent_roles.py` — Role definitions also use directory-level patterns

Since these patterns are directory-level wildcards, **renaming files within the directory requires no gateway changes**.

### Readonly mount enforcement

`shared/egg_container/__init__.py:132` — `_IMPLEMENT_READONLY_DIRS = ("drafts", "contracts", "pipelines", "reviews")`. Neither `agent-outputs` nor `checks` are in the readonly set, so **no mount changes needed**.

## Constraints

- **Backward compatibility**: The issue explicitly requires that existing in-flight pipelines not break. Readers should check both old (global) and new (prefixed) paths.
- **Agent-written files**: Several of these files are written by agents responding to prompt instructions (not orchestrator code). Agents write to the exact path specified in their prompt, so prompt changes are required.
- **Programmatic and prompt paths**: Some paths are constructed in Python (`load_agent_output`, `save_agent_output`), others are hardcoded strings in prompt builders. Both need updating.
- **Issue number availability**: The issue number (or pipeline ID for local mode) must be available at every callsite. The prompt builders already receive `issue_number` and `pipeline_id` parameters (or can derive them from `pipeline_mode`).
- **Test coverage**: The test file `orchestrator/tests/test_pipeline_prompts.py` has extensive tests checking prompt content and file operations. The integration test `integration_tests/local_pipeline/mock-sandbox/phase-runner.sh` hardcodes `implement-results.json`.
- **Documentation**: `docs/guides/sdlc-pipeline.md`, `docs/guides/agent-development.md`, `docs/architecture/orchestrator.md`, `sandbox/.claude/commands/tester-mode.md`, `sandbox/.claude/commands/integrator-mode.md` all reference these filenames.

## Options Considered

### Option A: Prefix filenames with issue number (matching existing convention)

**Approach**: Rename all globally-named files to use `{identifier}-{role}-output.json` and `{identifier}-implement-results.json`, matching the convention already used by `contracts/`, `drafts/`, and `reviews/`. Update `load_agent_output()` / `save_agent_output()` to accept an `identifier` parameter. Update all prompt builders to include the identifier in the path. Add backward-compatible fallback reads.

**Pros**:
- Consistent with the existing namespacing convention used by `contracts/`, `drafts/`, `reviews/`
- Simple and predictable — the identifier is already available at all callsites
- Minimal conceptual overhead — developers already understand the `{identifier}-` prefix pattern

**Cons**:
- Requires updating both programmatic code and prompt strings (two different code paths)
- Backward-compat fallback adds temporary code debt (can be removed after all in-flight pipelines complete)

### Option B: Namespace by subdirectory instead of filename prefix

**Approach**: Move files into per-issue subdirectories: `.egg-state/agent-outputs/{identifier}/architect-output.json` instead of `.egg-state/agent-outputs/{identifier}-architect-output.json`.

**Pros**:
- Cleaner directory structure — each pipeline's files are grouped together
- Easier to list/cleanup files for a specific pipeline

**Cons**:
- **Inconsistent with existing convention** — `contracts/`, `drafts/`, `reviews/` all use filename prefixes, not subdirectories
- More changes needed to gateway patterns (currently use `".egg-state/agent-outputs/*"`, would need `".egg-state/agent-outputs/**/*"`)
- More complex backward-compatibility: checking both directory structures
- git doesn't track empty directories, so cleanup is trickier

### Option C: Use a centralized path builder utility

**Approach**: Create a single path-building module (e.g., `shared/egg_contracts/paths.py`) that all code uses to resolve `.egg-state/` paths. This would centralize the naming convention and make future changes easier.

**Pros**:
- Single source of truth for all `.egg-state/` path construction
- Makes it easy to change naming conventions in the future
- Reduces risk of inconsistencies

**Cons**:
- Over-engineering for this specific fix — the immediate problem is just adding a prefix to 4-6 files
- Still needs prompt string updates (agents write based on prompt instructions, not Python utilities)
- `_get_draft_path()` and `_verdict_path_for_type()` already serve this role for their respective directories

## Recommended Approach

**Option A: Prefix filenames with issue number** is recommended. It directly matches the established convention used by `contracts/`, `drafts/`, and `reviews/`, making the codebase consistent. The scope is well-bounded (update `load_agent_output` / `save_agent_output`, update prompt builders, add fallback reads, update tests and docs).

The implementation touches two categories of code:

1. **Programmatic paths** (`load_agent_output`, `save_agent_output`, `_synthesize_plan_draft_from_outputs`, `_read_tester_gaps`): Add `identifier` parameter, construct `f"{identifier}-{role.value}-output.json"`. Fallback to old path if new doesn't exist.

2. **Prompt-embedded paths** (architect, integrator, risk_analyst, checker, check-and-fix prompts): Replace hardcoded filenames with dynamically constructed paths using the issue number / pipeline ID. These prompt builders already receive `pipeline_id` and `pipeline_mode` parameters.

The backward-compatible fallback in readers is straightforward: try the new prefixed path first, fall back to the old global path. This ensures in-flight pipelines that already wrote to the old paths continue to work.

## Open Questions

### Scope of files to namespace

The issue explicitly lists 4 files: `architect-output.json`, `integrator-output.json`, `risk_analyst-output.json`, and `implement-results.json`. However, the `load_agent_output()` / `save_agent_output()` functions use the same global naming pattern for ALL agent roles (`coder-output.json`, `tester-output.json`, `documenter-output.json`, `task_planner-output.json`, etc.). Should ALL `{role}-output.json` files be namespaced, or only the three listed in the issue?

**Recommendation**: Namespace all of them. If we only fix the three listed, the remaining files (`coder-output.json`, `tester-output.json`, `documenter-output.json`) will cause the same merge conflicts when pipelines use those roles concurrently.

### Identifier format for local-mode pipelines

For issue-mode pipelines, the identifier is the issue number (e.g., `871`). For local-mode pipelines, it's the pipeline ID (e.g., `local-a1b2c3d4`). The existing namespaced directories handle both modes (see `_get_draft_path()` and `_verdict_path_for_type()`). Should we follow the same dual-mode pattern for agent outputs?

**Recommendation**: Yes, follow the existing pattern exactly. `load_agent_output` and `save_agent_output` would accept an `identifier: int | str` parameter, matching how `get_contract_path()` works.

### Cleanup of old globally-named files

After this change ships, old globally-named files (`architect-output.json`, etc.) will remain in `.egg-state/agent-outputs/` on branches that were created before the change. Should we:
1. Add a one-time cleanup migration?
2. Let them age out naturally as branches merge?
3. Add a `.gitignore` entry for the old filenames?

### Health check references

`orchestrator/health_checks/tier1/phase_output.py:10` references `architect-output.json` in a docstring. The health check code may also look for this file at the old path. Should health checks be updated to use the new naming, or are they purely informational?

---

*Authored-by: egg*

<!-- egg-decision: scope-of-namespacing -->
## Decision: Scope of files to namespace

Which files should be namespaced with issue/pipeline prefix?

- [ ] Only the 4 files listed in the issue (architect-output.json, integrator-output.json, risk_analyst-output.json, implement-results.json)
- [ ] All agent role output files ({role}-output.json) plus implement-results.json
- [ ] Other (explain in reply)

<!-- egg-decision: old-file-cleanup -->
## Decision: Cleanup strategy for old globally-named files

How should old globally-named files be handled after the change ships?

- [ ] Let them age out naturally (backward-compat fallback handles reads)
- [ ] Add a one-time cleanup script that renames existing files on active branches
- [ ] Add .gitignore entries for old filenames to prevent accidental creation
- [ ] Other (explain in reply)

# metadata
complexity_tier: mid
