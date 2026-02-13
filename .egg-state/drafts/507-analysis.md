# Analysis: Tune Doc-Updater Bot to Cover README and Cross-Cutting Docs

> Issue: #507 | Phase: refine

## Problem Statement

The doc-updater bot (`on-push-doc-updater.yml` + `action/build-doc-updater-prompt.sh`) reliably updates component-level docs (gateway, sandbox, shared) when code changes land, but misses updates to high-level cross-cutting documentation — particularly `README.md` and `docs/guides/deployment.md`. These files synthesize information from multiple components and are the most visible docs to new users.

Specific gaps caught manually and fixed in PR #506:

| Missed Update | Source PR |
|---------------|-----------|
| Multi-agent orchestration not in README | #472 |
| Unified work loop not reflected in README | #457, #466 |
| Contract file protection missing from enforcement table | #453 |
| Bot identity configuration not mentioned | #483 |
| `--auth` and `--time` CLI flags missing from README reference | CLI changes |
| Quick Start still recommended Docker Compose over `egg` CLI | Various |

**Desired outcome**: The doc-updater bot should proactively flag and update README.md, the CLI reference table, the enforcement table, and the deployment guide when relevant code changes land — without manual intervention.

## Current Behavior

### Doc-Updater Architecture

The bot runs as a GitHub Actions workflow triggered on push to `main` (after PR merges). It follows this flow:

1. **`build-doc-updater-prompt.sh`** (324 lines) analyzes git diffs to extract:
   - Changed code files (excluding `docs/` and `*.md`)
   - Commit messages
   - Diff stats
   - Newly added files
   - Related docs (via term extraction from paths and commit subjects)

2. The prompt is fed to Claude (sonnet model) which reads the diff, checks structural docs (`STRUCTURE.md`, `architecture/README.md`, `docs/index.md`), and creates a PR if updates are needed.

### Root Causes of the Gap

**1. README.md is excluded by the `paths-ignore` filter:**
The workflow YAML (`on-push-doc-updater.yml:17-19`) ignores all `*.md` files:
```yaml
paths-ignore:
  - "docs/**"
  - "**/*.md"
```
This correctly prevents infinite doc-update loops, but also means the bot **never triggers when README.md itself needs updating** due to code changes. The code-change trigger works fine — it runs when `.py`, `.ts`, etc. files change. The issue is not about triggering; it's about the prompt not directing the agent to check README.md.

**2. The prompt checks structural docs but not README:**
The prompt (`build-doc-updater-prompt.sh:209-215`) explicitly instructs the agent to check three structural docs:
- `docs/development/STRUCTURE.md`
- `docs/architecture/README.md`
- `docs/index.md`

But `README.md` (the root README) is **not in this list**. The related-doc discovery (`find_related_docs()`) only searches inside `docs/`, further excluding the root README.

**3. No CLI cross-reference mechanism:**
The prompt has no instruction to compare CLI argparse definitions (`sandbox/egg_lib/cli.py`) against the CLI Reference table in `README.md`. CLI flag additions are one of the most common missed updates.

**4. No enforcement table awareness:**
The prompt doesn't mention the "What's Enforced" table in README or how to detect gateway policy changes that should update it.

**5. Deployment guide not explicitly checked:**
`docs/guides/deployment.md` may appear in the related-docs search if terms match, but it's not a guaranteed check when Quick Start or deployment flow changes.

### What the Bot Already Does Well

- Structural doc checks (`STRUCTURE.md`, `architecture/README.md`, `docs/index.md`)
- Term-based related doc discovery (greps docs for domain terms from changed paths)
- Loop prevention via `[doc-updater]` tag in commit messages
- Diff magnitude awareness (stats, new file detection)
- Clear skip/update guidelines

## Constraints

- **Loop prevention**: The workflow ignores `**/*.md` pushes. Any changes to the bot that cause it to trigger on its own doc updates would create infinite loops. The `[doc-updater]` commit message check is the secondary safeguard.
- **Context budget**: The prompt is sent to Claude sonnet with a 20-minute timeout. Adding too many files to check increases context consumption and risks timeout. README.md is ~362 lines, manageable. But adding a full audit of all docs on every run is not feasible.
- **Prompt builder is bash**: The prompt builder is a shell script. Changes should remain compatible with the existing bash-based pattern.
- **Model limitations**: Sonnet handles doc analysis well but may struggle with complex cross-referencing (e.g., parsing argparse code and comparing to markdown tables). Instructions need to be explicit.
- **Documenter agent overlap**: The multi-agent Documenter (`build-documenter-prompt.sh`) runs during SDLC pipeline implementation and already checks README.md. The doc-updater bot is a separate bot that runs post-merge as a safety net. Both should cover README, but with different triggering contexts.

## Documentation Audit

The issue requests an audit of all documentation. Here is the current state:

### Root-Level Docs

| File | Lines | Status | Notes |
|------|-------|--------|-------|
| `README.md` | 362 | Up to date (after #506) | CLI reference, enforcement table, Quick Start all current |
| `CONTRIBUTING.md` | — | Not audited | Development workflow |
| `RELEASING.md` | — | Not audited | Release process |
| `CHANGELOG.md` | — | Not audited | Version history |

### docs/ Directory

| File | Status | Gap Risk |
|------|--------|----------|
| `docs/index.md` | Current (updated 2026-02-07) | Low — checked by bot already |
| `docs/agentic-feedback-loop.md` | Conceptual — rarely needs updating | Low |
| `docs/collaboration-effectiveness.md` | Conceptual — rarely needs updating | Low |
| `docs/hitl-decisions.md` | Current | Medium — changes when HITL workflow changes |
| `docs/architecture/README.md` | Checked by bot already | Low |
| `docs/development/STRUCTURE.md` | Checked by bot already | Low |
| `docs/architecture/orchestrator.md` | Current | Medium — changes when orchestrator changes |
| `docs/guides/deployment.md` | Up to date (after #506) | **High** — must stay in sync with README Quick Start |
| `docs/guides/local-quickstart.md` | Current | Medium — setup flow changes |
| `docs/guides/github-automation.md` | Current | Medium — workflow changes |
| `docs/guides/sdlc-pipeline.md` | Current | Medium — pipeline changes |
| `docs/guides/reusable-workflows.md` | Current | Low |
| `docs/guides/agent-development.md` | Current | Low |
| `docs/guides/agent-mode-design.md` | Conceptual | Low |
| `docs/guides/deploy-migration.md` | Current | Low |

### Component READMEs

| File | Status | Gap Risk |
|------|--------|----------|
| `gateway/README.md` | Current | Medium — gateway changes frequent |
| `sandbox/README.md` | Current | Medium |
| `shared/README.md` | Current | Low |
| `config/README.md` | Current | Low |
| `action/README.md` | Current | Medium — action inputs change |
| `bin/README.md` | Current | Low |
| `sandbox/.claude/README.md` | Current | Low |

### Key Cross-Cutting Docs (Highest Risk)

These documents synthesize information across components and are most likely to drift:

1. **`README.md`** — CLI flags, enforcement table, Quick Start, workflow table
2. **`docs/guides/deployment.md`** — Must match README Quick Start
3. **`docs/guides/github-automation.md`** — Must match actual workflow behaviors
4. **`docs/guides/sdlc-pipeline.md`** — Must match pipeline implementation

## Options Considered

### Option A: Expand the Prompt Only

**Approach**: Modify `build-doc-updater-prompt.sh` to add explicit instructions for checking README.md, the CLI reference table, the enforcement table, and the deployment guide. No changes to the workflow YAML or term-extraction logic.

**Changes**:
1. Add `README.md` to the structural doc check list in the prompt (step 3)
2. Add a new step for CLI cross-referencing: instruct agent to compare `sandbox/egg_lib/cli.py` argparse definitions against the CLI Reference table in README.md
3. Add a new step for enforcement table checking: instruct agent to check `gateway/phase_filter.py` policy classes against the "What's Enforced" table
4. Add `docs/guides/deployment.md` as a guaranteed check when Quick Start-related code changes

**Pros**:
- Minimal code changes (prompt text only)
- No risk of triggering loops (workflow YAML unchanged)
- Easy to test via `workflow_dispatch` dry run

**Cons**:
- Relies entirely on agent following instructions (no programmatic guarantees)
- CLI cross-referencing via prompt may be unreliable with sonnet
- Doesn't improve the term-extraction to find README references

### Option B: Expand Prompt + Improve Term Extraction

**Approach**: All of Option A, plus modify `find_related_docs()` to also search `README.md` and other root-level docs (not just `docs/`). Add explicit checks for CLI-related and gateway-related files.

**Changes**:
1. All changes from Option A
2. Modify `find_related_docs()` to include root `README.md` in the search
3. Add heuristic: if changed files include `sandbox/egg_lib/cli.py`, always flag README.md CLI Reference section
4. Add heuristic: if changed files include `gateway/phase_filter.py` or `gateway/policy.py`, always flag README.md enforcement table
5. Add heuristic: if changed files touch deployment/compose paths, flag `docs/guides/deployment.md`
6. Include the flagged sections in the prompt context so the agent has them available

**Pros**:
- Programmatic detection of high-risk changes (not just prompt instructions)
- README.md included in term-based discovery
- Heuristics catch the most common missed update patterns
- Agent gets relevant context pre-loaded, improving accuracy

**Cons**:
- More bash code changes (moderate complexity)
- Heuristics may need tuning over time as the codebase evolves
- Still relies on agent for final judgment

### Option C: Full Validation Script

**Approach**: Build a dedicated validation script (Python) that programmatically cross-references CLI argparse definitions against README tables, gateway policies against enforcement tables, and Quick Start instructions against deployment guide. Run it as a pre-check before the agent, passing discrepancies as structured input.

**Pros**:
- Deterministic detection of drift (not relying on LLM interpretation)
- Could also run as a CI check independent of the doc-updater bot
- Most reliable approach

**Cons**:
- Significant new code to write and maintain
- Brittle: hardcoded parsing of both Python source and markdown tables
- Over-engineered for the current scope — the bot already runs an LLM that can do this analysis
- Would need updating whenever table formats change

## Recommended Approach

**Option B: Expand Prompt + Improve Term Extraction**

**Justification**:

1. **Addresses root causes directly**: The main gaps are (a) README.md not in the check list and (b) no heuristic for CLI/gateway-related changes. Option B fixes both.

2. **Right level of automation**: Heuristics flag high-risk files; the LLM agent decides whether to update them. This avoids brittle parsing while still providing programmatic guarantees that the right files are surfaced.

3. **Incremental over existing design**: All changes are within the existing `build-doc-updater-prompt.sh` architecture. No new scripts, no new workflows.

4. **Testable**: Can be verified via `workflow_dispatch` with `dry_run=true` against recent commits that were known to miss updates.

5. **Audit coverage**: The prompt expansion naturally covers the documentation audit findings — README, deployment guide, and CLI reference are now guaranteed checks rather than accidental discoveries.

### Implementation Outline

**File: `action/build-doc-updater-prompt.sh`**

1. **`find_related_docs()`**: Extend grep search to include `README.md` and root-level `*.md` files, not just `docs/`.

2. **New function `detect_high_risk_files()`**: Check if changed files match patterns that indicate specific docs need review:
   - `sandbox/egg_lib/cli.py` → flag README.md CLI Reference + Flags tables
   - `gateway/phase_filter.py`, `gateway/policy.py` → flag README.md enforcement tables
   - `docker-compose.yml`, `bin/egg-deploy`, deployment-related → flag `docs/guides/deployment.md`
   - `action/` changes → flag `action/README.md` and README.md GitHub Action section
   - `.github/workflows/` changes → flag `docs/guides/github-automation.md`

3. **Prompt expansion**: Add to the existing prompt:
   - Step 3: Add `README.md` to the structural doc check list
   - New step 3b: "Check README.md cross-cutting sections" — CLI Reference, Flags table, enforcement table, Quick Start, workflow table
   - New step 3c: "Cross-reference CLI definitions" — compare argparse in `sandbox/egg_lib/cli.py` with README CLI tables
   - Step 4: Ensure `docs/guides/deployment.md` is always in the related docs when deployment paths change

4. **Pass high-risk flags in the prompt**: Include output of `detect_high_risk_files()` as a new section so the agent knows which specific sections to scrutinize.

**File: `.github/workflows/on-push-doc-updater.yml`**

No changes needed. The workflow triggers on code changes (non-md), which is correct. The gap was in the prompt, not the trigger.

## Open Questions

1. **Scope of the documentation audit**: The issue asks for a comprehensive audit. The analysis above covers the full `docs/` tree and root-level docs. Should the audit findings be committed as a separate tracking document (e.g., `docs/doc-audit-results.md`), or is the analysis in this draft sufficient?

2. **Sonnet vs. Opus for cross-referencing**: The current bot uses `sonnet` model for cost/speed balance. CLI cross-referencing (reading Python argparse and comparing to markdown tables) is a moderately complex task. Should the model be upgraded to `opus` for better accuracy, or is the heuristic-based pre-flagging in Option B sufficient to keep sonnet effective?

---

*Authored-by: egg*
