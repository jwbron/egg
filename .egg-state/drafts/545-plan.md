# Implementation Plan: Remove GitHub Actions SDLC-Related Architecture

**Issue:** #545
**Branch:** `egg/issue-545`

## Overview

Remove all GitHub Actions SDLC orchestration workflows, supporting scripts, and prompt builders that are now superseded by the local distributed orchestration system (PR #524). Preserve PR-operational workflows (autofixer, merge fixer, PR checks, post-merge doc updater, PR reviewers) and infrastructure workflows (lint, test, release). Update all cross-references in tests and documentation.

## Feature Parity Confirmation

The prior analysis (`545-analysis.md`) confirmed the local orchestrator has full parity for all SDLC pipeline capabilities. Intentionally not ported (per issue requirements): GitHub issue label management, @mention-based task dispatch, self-improvement workflow analysis, and SDLC label setup. No gaps require remediation before removal.

---

## Phase 1: Remove SDLC Workflow Files

**Goal:** Delete the 8 SDLC orchestration workflow files.

### Task 1.1: Delete SDLC workflow files

Delete the following files from `.github/workflows/`:

| File | Lines | Purpose |
|------|-------|---------|
| `sdlc-pipeline.yml` | ~660 | Main SDLC pipeline orchestrator |
| `sdlc-work-loop.yml` | ~500 | Unified work/review/respond cycle |
| `sdlc-hitl.yml` | ~895 | Human-in-the-loop decision handling |
| `sdlc-multi-agent.yml` | ~956 | Multi-agent dispatch for implement phase |
| `on-issue-closed.yml` | ~100 | Cleanup on issue close |
| `on-mention.yml` | ~320 | @mention-based SDLC task dispatch |
| `on-pull-request-contract-verify.yml` | ~100 | SDLC contract compliance check |
| `self-improvement.yml` | ~200 | Daily workflow analysis |

**Acceptance criteria:**
- All 8 files are deleted
- No remaining workflow references these deleted workflows via `workflow_call` or `workflow_run`

---

## Phase 2: Remove SDLC Supporting Scripts

**Goal:** Delete SDLC-specific scripts from `.github/scripts/`.

### Task 2.1: Delete SDLC shell scripts

Delete from `.github/scripts/`:
- `setup-sdlc-labels.sh`
- `push-contract-update.sh`
- `transition-sdlc-label.sh`

**Keep:** `create-release.sh` (not SDLC-related).

### Task 2.2: Delete the entire `.github/scripts/checks/` directory

Delete the entire directory including:
- `__init__.py`
- `base.py`
- `check_fixer.py`
- `draft_validation_check.py`
- `lint_check.py`
- `merge_conflict_check.py`
- `plan_yaml_check.py`
- `run_check.py`
- `test_check.py`

**Acceptance criteria:**
- `.github/scripts/` contains only `create-release.sh`
- `.github/scripts/checks/` directory no longer exists

---

## Phase 3: Remove SDLC Prompt Builders from `action/`

**Goal:** Delete SDLC-specific prompt builder scripts from `action/`.

### Task 3.1: Delete SDLC prompt builder scripts

Delete from `action/`:
- `build-sdlc-prompt.sh`
- `build-unified-review-prompt.sh`
- `build-agent-mode-design-review-prompt-workloop.sh`
- `build-code-review-prompt-workloop.sh`
- `build-contract-verification-prompt-workloop.sh`
- `build-contract-verification-prompt.sh`
- `build-coder-prompt.sh`
- `build-tester-prompt.sh`
- `build-documenter-prompt.sh`
- `build-integrator-prompt.sh`
- `build-mention-prompt.sh`
- `build-onboarding-doc-prompt.sh` (unused by any workflow)
- `contract-state.sh` (unused by any workflow)
- `populate-contract-tasks.py`

**Keep in `action/`:**
- `build-review-prompt.sh` (used by `on-pull-request.yml`, `reusable-review.yml`)
- `build-autofixer-prompt.sh` (used by `on-check-failure.yml`, `reusable-autofix.yml`)
- `build-conflict-prompt.sh` (used by `on-merge-conflict.yml`, `reusable-conflict-resolve.yml`)
- `build-feedback-prompt.sh` (used by `on-review-feedback.yml`)
- `build-doc-updater-prompt.sh` (used by `on-push-doc-updater.yml`)
- `build-agent-mode-design-review-prompt.sh` (used by `on-pull-request-agent-mode-design.yml`)
- `entrypoint.sh`, `generate-config.sh`, `action.yml`, `README.md`
- Convention docs: `autofixer-conventions.md`, `conflict-conventions.md`, `review-conventions.md`

**Acceptance criteria:**
- All 14 SDLC prompt builder scripts are deleted
- All 6 PR-operational prompt builders remain
- All action infrastructure files remain

---

## Phase 4: Update Cross-References

**Goal:** Fix all broken references to deleted files in tests, workflows, and documentation.

### Task 4.1: Update `test-action.yml` shellcheck step

**File:** `.github/workflows/test-action.yml` (line 150)

Current:
```
shellcheck --severity=warning action/entrypoint.sh action/generate-config.sh action/build-mention-prompt.sh action/build-review-prompt.sh action/build-autofixer-prompt.sh
```

Remove `action/build-mention-prompt.sh` from the shellcheck list (this script is being deleted). Consider adding other kept scripts to the list (e.g., `build-conflict-prompt.sh`, `build-feedback-prompt.sh`, `build-doc-updater-prompt.sh`, `build-agent-mode-design-review-prompt.sh`) for better coverage.

**Acceptance criteria:**
- Shellcheck step no longer references deleted scripts
- Shellcheck step still validates all kept infrastructure scripts

### Task 4.2: Remove `tests/scripts/test_checks.py`

**File:** `tests/scripts/test_checks.py` (504 lines)

This file tests the `.github/scripts/checks/` modules being deleted. The entire test file should be removed.

**Note:** `tests/scripts/test_check_docker_and_claude_invocations.py` and `tests/scripts/test_check_reviewer_job_names.py` are unrelated to SDLC (they test `scripts/check-docker-and-claude-invocations.py` and `scripts/check-reviewer-job-names.py`). These must be kept.

**Acceptance criteria:**
- `tests/scripts/test_checks.py` is deleted
- `tests/scripts/__init__.py` is kept (needed by remaining test files)
- Other test files in the directory are unmodified

### Task 4.3: Update `action/README.md`

Remove references to deleted SDLC prompt builder scripts. Keep documentation for the PR-operational scripts and action infrastructure.

**Acceptance criteria:**
- No references to deleted SDLC scripts remain in `action/README.md`
- Documentation for kept scripts is preserved

### Task 4.4: Update documentation files

The following docs reference the deleted GitHub Actions SDLC workflows and need updating:

| File | Type of references |
|------|-------------------|
| `docs/guides/sdlc-pipeline.md` | Heavy references to all SDLC workflows; needs major rewrite to describe local orchestrator |
| `docs/architecture/README.md` | References `sdlc-multi-agent.yml`, `sdlc-pipeline.yml`, `sdlc-work-loop.yml`, `sdlc-hitl.yml` |
| `docs/guides/reusable-workflows.md` | Sections for `on-mention.yml` and `sdlc-pipeline.yml`; references `setup-sdlc-labels.sh` |
| `docs/guides/github-automation.md` | References `on-mention.yml`, `self-improvement.yml`, `sdlc-pipeline.md` |
| `docs/guides/agent-development.md` | References `sdlc-multi-agent.yml` for adding agents |
| `docs/hitl-decisions.md` | Heavy references to `sdlc-hitl.yml` |
| `docs/templates/phase-completion.md` | References `sdlc-hitl.yml` |
| `docs/templates/feedback.md` | References `sdlc-hitl.yml` |
| `docs/agentic-feedback-loop.md` | Links to `sdlc-pipeline.md` guide |
| `docs/index.md` | Links to `sdlc-pipeline.md` guide; references `sdlc-*.yml` workflows |
| `docs/development/STRUCTURE.md` | Lists `sdlc-*.yml` in directory structure |
| `docs/adr/implemented/ADR-SDLC-Pipeline.md` | References all SDLC workflows extensively |

**Strategy for docs updates:**

- **`docs/guides/sdlc-pipeline.md`**: Rewrite to describe local orchestrator as the primary system. Remove all GitHub Actions workflow references. Point to `orchestrator/` package instead.
- **`docs/architecture/README.md`**: Update the workflow reference table to remove SDLC entries; update to reference orchestrator package.
- **`docs/guides/reusable-workflows.md`**: Remove sections for `on-mention.yml` and `sdlc-pipeline.yml`. Remove `setup-sdlc-labels.sh` reference.
- **`docs/guides/github-automation.md`**: Remove `on-mention.yml` and `self-improvement.yml` sections.
- **`docs/guides/agent-development.md`**: Update "Add to multi-agent workflow" section to reference local orchestrator's `container_spawner.py` instead.
- **`docs/hitl-decisions.md`**: Update references from `sdlc-hitl.yml` to the local orchestrator's `decision_queue.py`.
- **`docs/templates/phase-completion.md`** and **`docs/templates/feedback.md`**: Update `sdlc-hitl.yml` references to local orchestrator equivalents.
- **`docs/index.md`**: Update links and task-context references to remove GHA SDLC entries.
- **`docs/development/STRUCTURE.md`**: Remove the `sdlc-*.yml` entries from the directory tree listing.
- **`docs/adr/implemented/ADR-SDLC-Pipeline.md`**: Add a note that the GitHub Actions implementation has been replaced by the local orchestrator. Keep the ADR for historical context but mark the GHA-specific sections as superseded.

**Acceptance criteria:**
- No documentation file references a deleted workflow or script
- All documentation accurately reflects the local orchestrator as the current system
- Links to still-existing documents are not broken

---

## Phase 5: Verification

**Goal:** Ensure no broken references, tests pass, linting passes.

### Task 5.1: Run `make lint`

Verify no linting errors introduced by the changes.

**Acceptance criteria:**
- `make lint` passes cleanly (or has only pre-existing failures documented in PR)

### Task 5.2: Run `make test`

Verify no test failures caused by removed files or broken imports.

**Acceptance criteria:**
- `make test` passes cleanly (or has only pre-existing failures documented in PR)

### Task 5.3: Verify no dangling references

Run a grep for all deleted filenames across the repository to confirm no remaining references:
- Search for `sdlc-pipeline.yml`, `sdlc-work-loop.yml`, `sdlc-hitl.yml`, `sdlc-multi-agent.yml`, `on-issue-closed.yml`, `on-mention.yml`, `on-pull-request-contract-verify.yml`, `self-improvement.yml`
- Search for deleted script names
- Exclude `.egg-state/` directory from search

**Acceptance criteria:**
- No file in the repository (outside `.egg-state/`) references a deleted file

### Task 5.4: Verify kept workflows have no broken dependencies

Confirm the 9 PR-operational and 7 infrastructure workflows still have all their referenced scripts, actions, and reusable workflows intact.

**Acceptance criteria:**
- All kept workflows reference only existing files

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Accidentally deleting a PR-operational script | PR automation breaks | Careful categorization (verified in analysis); shellcheck and tests catch missing files |
| Missing documentation cross-references | Stale docs | Phase 5.3 grep scan catches remaining references |
| `shared/egg_contracts/` breakage | Local orchestrator breaks | These packages are NOT being touched; only `.github/scripts/checks/` runners are removed |
| Broken imports in remaining tests | CI failures | Phase 5.2 test run catches this |
| Other repos using reusable SDLC workflows | External callers break | The reusable workflow references use `@main` — callers need to update, but this is expected since the local orchestrator replaces the GHA approach |

## Rollback Plan

All changes are file deletions and documentation edits. Rollback is straightforward:
1. Revert the PR commit(s)
2. All deleted files are restored from git history

No database migrations, infrastructure changes, or external system modifications are involved.

## Test Strategy

1. **Pre-implementation baseline**: Run `make lint` and `make test` before any changes to establish baseline
2. **After each phase**: Run `make lint` and `make test` to catch breakage early
3. **Final verification**: Full lint + test + grep scan for dangling references
4. **Workflow validation**: Manually verify that `test-action.yml` shellcheck list only references existing scripts

## Summary

| Phase | Files Deleted | Files Updated |
|-------|--------------|---------------|
| 1. Workflows | 8 | 0 |
| 2. Supporting scripts | 3 + 9 (checks dir) | 0 |
| 3. Prompt builders | 14 | 0 |
| 4. Cross-references | 1 (test file) | ~12 docs + 2 config files |
| 5. Verification | 0 | 0 |
| **Total** | **35 files** | **~14 files** |
