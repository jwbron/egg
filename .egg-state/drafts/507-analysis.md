# Analysis: Tune doc-updater bot to cover README and cross-cutting docs

> Issue: #507 | Phase: refine

## Problem Statement

The doc-updater bot successfully updates component-level documentation but misses updates to high-level cross-cutting docs like `README.md` and `docs/guides/deployment.md`. Recent feature additions (multi-agent orchestration, unified work loop, contract file protection, bot identity configuration, CLI flags) were not reflected in the README until a manual fix in #506.

**Current state**: The bot analyzes code changes and updates docs within `docs/`, but root-level markdown files and cross-cutting guides are systematically excluded.

**Desired outcome**: The bot should flag discrepancies between:
1. CLI flags in `sandbox/egg_lib/cli.py` vs the CLI Reference table in `README.md`
2. Gateway enforcement rules vs the "What's Enforced" table in `README.md`
3. Deployment recommendations in `README.md` vs `docs/guides/deployment.md`

## Current Behavior

The doc-updater bot is implemented in two files:

| File | Purpose |
|------|---------|
| `.github/workflows/on-push-doc-updater.yml` | Workflow trigger and execution |
| `action/build-doc-updater-prompt.sh` | Context building and prompt generation |

### Trigger Mechanism

The workflow triggers on pushes to `main` with explicit path exclusions (lines 15-19):

```yaml
paths-ignore:
  - "docs/**"
  - "**/*.md"
  - ".github/workflows/on-push-doc-updater.yml"
```

This prevents the bot from running when only documentation changes, avoiding infinite loops.

### File Detection

The `get_changed_files()` function (lines 23-30) explicitly excludes all markdown:

```bash
git diff --name-only "${base_commit}..HEAD" | \
    grep -v -E '^docs/' | \
    grep -v -E '\.md$'
```

The `find_related_docs()` function (lines 55-128) searches only within `docs/` and excludes structural docs (`docs/index.md`, `docs/development/STRUCTURE.md`, `docs/architecture/README.md`).

### Root Cause

**README.md is excluded at three levels**:
1. **Workflow trigger**: `**/*.md` path-ignore prevents the workflow from running when README changes
2. **File detection**: `\.md$` filter removes README from the changed files list
3. **Related docs search**: Only searches `docs/` directory, never finding root-level files

**Cross-cutting docs are missed because**:
- Term extraction focuses on domain-specific words from code paths
- Generic concepts (feedback loops, collaboration, enforcement) don't match code path terms
- No explicit cross-referencing between CLI definitions and documentation tables

## Constraints

- **Loop prevention**: The `**/*.md` path-ignore exists to prevent infinite loops where the bot creates a doc PR that triggers itself
- **Context limits**: The bot uses `sonnet` model with a 20-minute timeout; adding many files to analyze burns context and time
- **Workflow complexity**: Changes must work within GitHub Actions constraints
- **Backward compatibility**: Existing component-level doc updates must continue to work
- **False positive risk**: Aggressively flagging README for every code change would create noise

## Options Considered

### Option A: Explicit README Checks in Prompt

**Approach**: Modify `build-doc-updater-prompt.sh` to explicitly list README.md sections (CLI Reference, enforcement tables) in the prompt, instructing Claude to cross-reference them against code changes.

**Implementation**:
1. Add README.md to the "structural docs" list in the prompt (alongside STRUCTURE.md, architecture/README.md)
2. Extract CLI flags from `sandbox/egg_lib/cli.py` using grep/awk and include in context
3. Add specific instructions to compare CLI flags against README's CLI Reference table

**Pros**:
- Minimal changes to existing workflow
- No new dependencies or infrastructure
- Claude can intelligently decide when README updates are needed
- Preserves the loop-prevention mechanism (README changes don't trigger the workflow)

**Cons**:
- Adds context overhead (README.md is ~250 lines)
- Relies on Claude to correctly identify discrepancies
- No automated verification that tables match code

### Option B: Pre-flight Discrepancy Detection Script

**Approach**: Add a dedicated script that programmatically compares CLI flags in `cli.py` against README.md tables, outputting discrepancies for Claude to address.

**Implementation**:
1. Create `action/check-readme-sync.sh` that:
   - Parses argparse flags from `sandbox/egg_lib/cli.py`
   - Parses CLI Reference table from `README.md`
   - Outputs list of flags present in code but missing from docs (and vice versa)
2. Modify `build-doc-updater-prompt.sh` to call this script and include discrepancies in prompt
3. Add similar checks for enforcement tables against gateway policy code

**Pros**:
- Deterministic detection of specific discrepancies
- Reduces Claude's workload—only needs to write updates, not find issues
- Can be run independently as a CI check
- Catches all flag additions/removals automatically

**Cons**:
- More complex implementation (parsing argparse and markdown tables)
- Requires maintenance when table format changes
- May miss semantic discrepancies (e.g., outdated descriptions)

### Option C: Separate Cross-Cutting Docs Workflow

**Approach**: Create a new workflow specifically for cross-cutting docs that runs on different triggers and uses different logic.

**Implementation**:
1. Create `.github/workflows/on-push-readme-sync.yml`
2. Trigger on changes to `sandbox/egg_lib/cli.py`, `gateway/` enforcement files, etc.
3. Use path-based triggers instead of path-ignore
4. Include README.md and deployment.md explicitly in scope

**Pros**:
- Clean separation of concerns
- Can use different models/timeouts optimized for this task
- Easier to test and debug independently
- Won't affect existing doc-updater behavior

**Cons**:
- Two workflows to maintain instead of one
- Potential for overlapping PRs if both workflows run
- More complex CI pipeline

### Option D: Hybrid Approach (Discrepancy Script + Extended Prompt)

**Approach**: Combine Option A and Option B—use a script to detect concrete discrepancies and extend the prompt to include README.md as a structural doc.

**Implementation**:
1. Create `action/check-readme-sync.sh` for CLI flag and enforcement table comparison
2. Modify `build-doc-updater-prompt.sh` to:
   - Run the sync check script
   - Include discrepancy report in the prompt
   - Add README.md to the list of docs to check
   - Add deployment.md cross-reference instructions
3. Keep existing loop prevention (workflow won't re-trigger on README changes)

**Pros**:
- Deterministic detection + intelligent analysis
- Catches both structural discrepancies and semantic issues
- Single workflow, extended functionality
- Can surface issues that require human judgment

**Cons**:
- Most complex implementation
- Increased context usage
- More moving parts to test

## Recommended Approach

**Option D: Hybrid Approach** is recommended because it addresses all four suggestions in the issue:

1. **Check README.md explicitly**: Adding it to the structural docs list ensures it's always reviewed
2. **Cross-reference CLI flags**: The sync script provides deterministic comparison
3. **Track the enforcement table**: Script can compare gateway code against README tables
4. **Check deployment guide consistency**: Prompt instructions can compare Quick Start vs deployment.md

The hybrid approach provides both automated detection (catching obvious mismatches) and intelligent analysis (catching semantic issues that require understanding). The sync script can also be exposed as a standalone CI check for immediate feedback on PRs.

**Implementation order**:
1. Add README.md to the prompt's structural docs section (immediate value)
2. Create `action/check-readme-sync.sh` for CLI flag comparison
3. Extend sync script for enforcement table comparison
4. Add deployment.md cross-reference instructions to prompt

## Open Questions

### Multiple-choice: Sync Script Scope

Should the discrepancy detection script (`check-readme-sync.sh`) be:

- [ ] **CLI flags only** — Focus on `cli.py` vs README CLI Reference table
- [ ] **CLI + enforcement** — Also compare gateway policies vs "What's Enforced" table
- [ ] **Full cross-reference** — Include deployment.md recommendations vs Quick Start
- [ ] Other (explain in reply)

### Open-ended Questions

1. **False positive tolerance**: If the bot creates a PR suggesting README updates that turn out to be unnecessary, is that acceptable? Or should it err on the side of not flagging issues?

2. **Ownership boundaries**: Should the doc-updater bot be able to modify README.md directly, or should it only create issues/comments flagging discrepancies for human review?

3. **Priority of changes**: The issue mentions several specific gaps (#472, #457, #466, #453, #483). Are there other cross-cutting docs beyond README.md and deployment.md that should be tracked?

---

*Authored-by: egg*
