# Implementation Plan: Tune Doc-Updater Bot for Cross-Cutting Docs

> Issue: #507 | Phase: plan

## Overview

The doc-updater bot reliably updates component-level docs but misses high-level cross-cutting documentation — particularly `README.md`, the CLI reference table, the enforcement table, and the deployment guide. This plan implements **Option B** from the analysis: expand the prompt and improve term extraction with targeted heuristics.

**Approach**: Modify `action/build-doc-updater-prompt.sh` to (1) include `README.md` and other root-level docs in the search scope, (2) add heuristic detection for high-risk file patterns that should trigger specific doc checks, and (3) expand the prompt with explicit instructions for cross-referencing CLI definitions, enforcement tables, and deployment consistency.

No changes to the workflow YAML (`on-push-doc-updater.yml`) are needed — the trigger logic is correct.

## Phase 1: Expand `find_related_docs()` to Include Root-Level Docs

### Task 1.1: Search root-level markdown files

**File**: `action/build-doc-updater-prompt.sh` — `find_related_docs()` function (lines 55-128)

**Current behavior**: The function only searches inside `docs/` (line 116: `grep -rl -i -E "$pattern" docs/`). Root-level files like `README.md`, `CONTRIBUTING.md`, and `CHANGELOG.md` are never found.

**Change**: Add a second grep pass that searches root-level `*.md` files for the same term pattern. Combine results with the existing `docs/` search. Exclude structural docs already checked separately (same filter as today, plus `README.md` will be handled by the new explicit check in Phase 2).

**Implementation**:
```bash
# After existing docs/ search, also search root-level markdown files
local root_md_results
root_md_results=$(grep -rl -i -E "$pattern" *.md 2>/dev/null | \
    grep -v -E '^README\.md$' | \
    sort -u || true)

# Combine results
results=$(printf '%s\n%s' "$results" "$root_md_results" | grep -v '^$' | sort -u)
```

**Acceptance criteria**:
- `find_related_docs()` returns matches from both `docs/` and root-level `*.md` files
- `README.md` is excluded from term-based discovery (it gets explicit handling in Phase 2)
- Structural docs already handled in step 3 are still excluded
- Empty results are handled gracefully

## Phase 2: Add High-Risk File Detection Heuristic

### Task 2.1: Implement `detect_high_risk_docs()` function

**File**: `action/build-doc-updater-prompt.sh` — new function between `find_related_docs()` and `build_prompt()`

**Purpose**: When specific source files change, flag specific documentation sections for review. This provides deterministic detection rather than relying only on term matching.

**Heuristic rules**:

| Changed File Pattern | Flagged Doc | Specific Section |
|---------------------|-------------|-----------------|
| `sandbox/egg_lib/cli.py` | `README.md` | CLI Reference table, Flags table |
| `gateway/phase_filter.py`, `gateway/policy.py`, `.egg/phase-permissions.json` | `README.md` | "What's Enforced" table, Phase Permissions table |
| `docker-compose*.yml`, `bin/egg-deploy`, deployment-related | `docs/guides/deployment.md` | Deployment methods, commands |
| `action/action.yml`, `action/entrypoint.sh` | `README.md`, `action/README.md` | GitHub Action section, Action inputs |
| `.github/workflows/` | `docs/guides/github-automation.md` | Workflow table |
| `orchestrator/` | `README.md` | Multi-Agent Orchestration section |
| `sandbox/egg_lib/cli.py` (flags specifically) | `docs/guides/deployment.md` | CLI section cross-link |

**Implementation**:
```bash
detect_high_risk_docs() {
    local changed_files="$1"
    local flags=""

    if echo "$changed_files" | grep -qE 'sandbox/egg_lib/cli\.py'; then
        flags+="README_CLI "
    fi

    if echo "$changed_files" | grep -qE '(gateway/phase_filter\.py|gateway/policy\.py|\.egg/phase-permissions\.json)'; then
        flags+="README_ENFORCEMENT "
    fi

    if echo "$changed_files" | grep -qE '(docker-compose|bin/egg-deploy|sandbox/egg_lib/(compose|deploy))'; then
        flags+="DEPLOYMENT_GUIDE "
    fi

    if echo "$changed_files" | grep -qE '(action/action\.yml|action/entrypoint\.sh)'; then
        flags+="README_ACTION ACTION_README "
    fi

    if echo "$changed_files" | grep -qE '\.github/workflows/'; then
        flags+="GITHUB_AUTOMATION "
    fi

    if echo "$changed_files" | grep -qE 'orchestrator/'; then
        flags+="README_ORCHESTRATION "
    fi

    echo "$flags"
}
```

**Acceptance criteria**:
- Function accepts the changed files list and returns space-separated flag tokens
- Each heuristic rule correctly matches the documented file patterns
- Empty input returns empty output
- Multiple rules can fire simultaneously

## Phase 3: Expand the Prompt

### Task 3.1: Add `README.md` to structural doc checks (step 3)

**File**: `action/build-doc-updater-prompt.sh` — `build_prompt()`, step 3 (lines 209-214)

**Change**: Add `README.md` to the list of structural docs the agent must always read and check.

**New step 3 text**:
```
3. **Check these structural docs** (read them, don't delegate to sub-agents for
   large files):
   - `docs/development/STRUCTURE.md` — Does it list all current directories and
     key files? Are new packages/modules missing?
   - `docs/architecture/README.md` — Does it cover the components added/changed?
   - `docs/index.md` — Are new docs or templates referenced?
   - `README.md` — Does the root README reflect the current state? Check:
     - CLI Reference and Flags tables (compare with `sandbox/egg_lib/cli.py` argparse)
     - "What's Enforced" table (compare with `gateway/phase_filter.py` and `.egg/phase-permissions.json`)
     - Phase Permissions table
     - Multi-Agent Orchestration section
     - GitHub Automation workflow table
     - Quick Start instructions
```

**Acceptance criteria**:
- `README.md` is listed as a structural doc to always check
- Specific sections are called out with cross-reference sources

### Task 3.2: Add cross-referencing instructions (new step 3b)

**File**: `action/build-doc-updater-prompt.sh` — `build_prompt()`, after step 3

**Add new instruction block**:
```
3b. **Cross-reference high-risk sections** (only when flagged changes detected):

    ${high_risk_instructions}

    For each flagged section:
    - Read the SOURCE file to extract the current definitions
    - Read the TARGET doc section to check for discrepancies
    - If they differ, update the doc to match the source
```

The `${high_risk_instructions}` variable is populated from the `detect_high_risk_docs()` output. For each flag, a specific instruction is injected:

- `README_CLI` → "CLI changes detected: Compare argparse definitions in `sandbox/egg_lib/cli.py` against the CLI Reference and Flags tables in `README.md`. Check for missing flags, changed descriptions, or reordered arguments."
- `README_ENFORCEMENT` → "Gateway policy changes detected: Compare `gateway/phase_filter.py` and `.egg/phase-permissions.json` against the 'What's Enforced' and 'Phase Permissions' tables in `README.md`."
- `DEPLOYMENT_GUIDE` → "Deployment-related changes detected: Check `docs/guides/deployment.md` for consistency with README Quick Start and CLI Reference. Ensure deployment commands and options match."
- `README_ACTION` → "GitHub Action changes detected: Compare `action/action.yml` inputs against the GitHub Action section in `README.md`."
- `ACTION_README` → "Action changes detected: Check `action/README.md` for accuracy."
- `GITHUB_AUTOMATION` → "Workflow changes detected: Check `docs/guides/github-automation.md` for accuracy against actual workflow files in `.github/workflows/`."
- `README_ORCHESTRATION` → "Orchestrator changes detected: Check the Multi-Agent Orchestration section in `README.md`."

If no flags are set, this step is omitted (no additional instructions).

**Acceptance criteria**:
- High-risk instructions are only injected when relevant heuristic flags are set
- Each instruction specifies both the SOURCE file and TARGET doc section
- When no flags are set, the step is cleanly omitted (no empty section in the prompt)

### Task 3.3: Add deployment guide to guaranteed checks

**File**: `action/build-doc-updater-prompt.sh` — `build_prompt()`, step 4

**Change**: Add a sentence to step 4 ensuring `docs/guides/deployment.md` is always checked when it appears in related docs OR when deployment-related heuristic flags fire.

**New text appended to step 4**:
```
   Pay special attention to `docs/guides/deployment.md` — it must stay in sync
   with the README Quick Start section. If either document's deployment
   instructions changed, verify both are consistent.
```

**Acceptance criteria**:
- Step 4 includes explicit mention of `docs/guides/deployment.md`
- The instruction references consistency with README Quick Start

## Phase 4: Wire Heuristics into `build_prompt()`

### Task 4.1: Call `detect_high_risk_docs()` and inject results

**File**: `action/build-doc-updater-prompt.sh` — `build_prompt()` function

**Change**: After `related_docs=$(find_related_docs)`, add:
```bash
high_risk_flags=$(detect_high_risk_docs "$changed_files")
high_risk_instructions=$(build_high_risk_instructions "$high_risk_flags")
```

Then include `$high_risk_instructions` in the prompt template where step 3b is defined. Also add a new section to the prompt context block:

```
High-risk doc flags (auto-detected from changed files):
\`\`\`
${high_risk_flags:-none}
\`\`\`
```

**Implementation detail**: Create a helper function `build_high_risk_instructions()` that maps flag tokens to instruction text:

```bash
build_high_risk_instructions() {
    local flags="$1"
    local instructions=""

    if [[ "$flags" == *"README_CLI"* ]]; then
        instructions+="- **CLI Reference**: Compare argparse definitions in \`sandbox/egg_lib/cli.py\` against CLI Reference and Flags tables in \`README.md\`. Check for missing flags, changed descriptions, or reordered arguments.\n"
    fi
    # ... similar for each flag

    echo -e "$instructions"
}
```

**Acceptance criteria**:
- `detect_high_risk_docs()` is called with the changed files list
- `build_high_risk_instructions()` produces human-readable instructions from flags
- High-risk flags are visible in the prompt context block
- Instructions are injected into the prompt only when flags are present
- Prompt remains well-formed when no flags are set

## Phase 5: Testing

### Task 5.1: Manual dry-run validation

Run the prompt builder locally against known commits that were missed (from PR #506):

```bash
# Test against a commit that added CLI flags
COMMIT_SHA=<hash-before-cli-flags> \
DRY_RUN=true \
GITHUB_REPOSITORY=jwbron/egg \
RUNNER_TEMP=/tmp \
bash action/build-doc-updater-prompt.sh

# Verify the prompt includes README.md check and CLI cross-reference instructions
```

**Acceptance criteria**:
- Prompt builder runs without errors
- CLI flag changes trigger `README_CLI` flag
- Gateway policy changes trigger `README_ENFORCEMENT` flag
- README.md appears in structural doc check list
- High-risk instructions are present when relevant flags fire
- Prompt is well-formed when no flags fire (clean output, no empty sections)

### Task 5.2: Validate prompt size stays reasonable

**Acceptance criteria**:
- Prompt size with all flags active is under 6000 chars (current baseline is ~2500 chars)
- No additional files are read into the prompt (agent reads them at runtime)

### Task 5.3: Shellcheck validation

```bash
shellcheck action/build-doc-updater-prompt.sh
```

**Acceptance criteria**:
- No new shellcheck warnings or errors introduced
- Existing patterns preserved (set -euo pipefail, quoting, etc.)

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Agent ignores new instructions | Low | Medium | Instructions are explicit with source/target pairs; heuristics provide pre-flagging |
| Prompt grows too large | Low | Low | Instructions are conditional; only injected when flags fire |
| False positives (flagging docs unnecessarily) | Medium | Low | Agent still decides whether to update; false flags just trigger a read |
| Heuristic patterns go stale | Medium | Low | Patterns use broad path matching; new files in same dirs are caught |
| Infinite loop risk | Very Low | High | No changes to workflow YAML; `[doc-updater]` tag and `paths-ignore` unchanged |
| Sonnet struggles with cross-referencing | Low | Medium | Heuristics pre-flag the specific sections; agent only needs to compare two lists |

## Rollback

All changes are in a single file (`action/build-doc-updater-prompt.sh`). Rollback is a single `git revert`. The workflow YAML is unchanged, so reverting the prompt builder restores exact previous behavior.

## Files Changed

| File | Change Type | Description |
|------|------------|-------------|
| `action/build-doc-updater-prompt.sh` | Modified | Expand `find_related_docs()`, add `detect_high_risk_docs()`, add `build_high_risk_instructions()`, expand prompt template |

## Out of Scope

- **Workflow YAML changes**: Not needed; trigger logic is correct
- **Model upgrade (sonnet → opus)**: Heuristic pre-flagging should keep sonnet effective; revisit if cross-referencing accuracy is insufficient
- **Dedicated validation script (Option C)**: Over-engineered for current needs
- **Documentation audit tracking doc**: The analysis already covers the full audit; the bot improvements address the gaps found

---

*Authored-by: egg*
