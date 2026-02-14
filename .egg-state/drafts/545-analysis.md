# Issue #545: Remove GitHub Actions SDLC-Related Architecture

## Problem Statement

The SDLC pipeline orchestration has been rebuilt as a local distributed system
(PR #524, the `orchestrator/` package). The GitHub Actions workflows that
previously performed SDLC orchestration are now redundant. Having two parallel
orchestration systems creates maintenance burden and confusion. This issue
requests removing the GitHub Actions SDLC workflows while preserving
PR-operational workflows (autofixer, merge fixer, PR checks, post-merge doc
updater, PR reviewers).

A core requirement is ensuring the local orchestration system has feature parity
with what the GitHub Actions SDLC workflows provided, documenting any gaps
before removal.

## Current State

### Inventory of `.github/workflows/` (25 files)

#### SDLC Orchestration Workflows (REMOVE)

| Workflow | Purpose | Lines |
|----------|---------|-------|
| `sdlc-pipeline.yml` | Main SDLC pipeline orchestrator (refine→plan→implement→pr) | ~660 |
| `sdlc-work-loop.yml` | Unified work/review/respond cycle for SDLC phases | ~500 |
| `sdlc-hitl.yml` | Human-in-the-loop decision handling (checkbox edits, approvals) | ~895 |
| `sdlc-multi-agent.yml` | Multi-agent dispatch for implement phase (coder/tester/documenter/integrator) | ~956 |
| `on-issue-closed.yml` | Cleanup on issue close (cancel runs, delete branches, remove labels) | ~100 |
| `on-mention.yml` | Respond to @mentions on issues/PRs (SDLC task dispatch) | ~320 |
| `on-pull-request-contract-verify.yml` | Verify SDLC contract compliance on `sdlc:pr` labeled PRs | ~100 |
| `self-improvement.yml` | Daily analysis of workflow runs, creates improvement issues | ~200 |

**Total: 8 workflows, ~3,730 lines**

#### PR Operations Workflows (KEEP)

| Workflow | Purpose |
|----------|---------|
| `on-pull-request.yml` | Trigger code review on PR open/sync |
| `on-check-failure.yml` | Auto-fix failing lint/test checks on PRs |
| `on-merge-conflict.yml` | Auto-resolve merge conflicts in PRs |
| `on-review-feedback.yml` | Address review feedback on bot-authored PRs |
| `on-push-doc-updater.yml` | Auto-update docs after PRs merge to main |
| `on-pull-request-agent-mode-design.yml` | Specialized review for agent-mode design changes |
| `reusable-autofix.yml` | Reusable auto-fix logic |
| `reusable-conflict-resolve.yml` | Reusable conflict resolution logic |
| `reusable-review.yml` | Reusable code review logic |

**Total: 9 workflows (KEEP)**

#### Infrastructure Workflows (KEEP)

| Workflow | Purpose |
|----------|---------|
| `lint.yml` | Linting (Ruff, mypy, shellcheck, yamllint, Docker, Actions) |
| `test.yml` | Unit tests + Bandit security scan |
| `test-action.yml` | Tests for the egg GitHub Action itself |
| `test-e2e.yml` | End-to-end integration tests |
| `test-integration.yml` | Integration + security tests with containers |
| `release-images.yml` | Build and push Docker images to GHCR |
| `dependabot.yml` | Automated dependency updates |

**Total: 7 workflows (KEEP)**

### Categorization Rationale for Edge Cases

**`on-mention.yml` → REMOVE**: While this handles @mentions on both issues and
PRs, it is fundamentally an SDLC dispatch mechanism. PR-specific feedback is
already handled by `on-review-feedback.yml`. The issue explicitly states "we
don't want to port any behavior for interacting with github issues," and
@mention-based task dispatch is an SDLC pattern. The future replacement (a
single GitHub Action running the SDLC pipeline) can reintroduce @mention
handling if needed.

**`on-push-doc-updater.yml` → KEEP**: This is a post-merge automation (triggers
on push to main), not an SDLC pipeline phase. The issue explicitly lists
"post-merge doc updater" as something to keep.

**`self-improvement.yml` → REMOVE**: This is a meta-workflow that analyzes
GitHub Actions runs and creates improvement issues. It only makes sense in the
context of a GitHub Actions-heavy SDLC. The local orchestrator has its own
monitoring via SSE streams and DAG visualization. This can be rebuilt as a local
tool if needed.

**`on-pull-request-contract-verify.yml` → REMOVE**: Only triggers on PRs
labeled `sdlc:pr`, which is an SDLC-specific label. Contract verification is
handled by the local orchestrator's dispatch system.

### Supporting Scripts (REMOVE)

#### `.github/scripts/` (SDLC-specific)

| Script | Purpose | Used By |
|--------|---------|---------|
| `setup-sdlc-labels.sh` | Initialize SDLC pipeline labels | Manual setup |
| `push-contract-update.sh` | Push contract updates with conflict resolution | `sdlc-hitl.yml` |
| `transition-sdlc-label.sh` | Transition SDLC phase labels on issues | `sdlc-pipeline.yml`, `sdlc-work-loop.yml`, `sdlc-hitl.yml` |

#### `.github/scripts/checks/` (SDLC contract checks)

| Script | Purpose |
|--------|---------|
| `run_check.py` | Entry point for running contract checks |
| `base.py` | Abstract base class for check runners |
| `check_fixer.py` | Auto-fix check runner |
| `draft_validation_check.py` | Validate SDLC draft outputs |
| `plan_yaml_check.py` | Validate SDLC plan YAML format |
| `lint_check.py` | Lint check runner (wraps `make lint`) |
| `test_check.py` | Test check runner (wraps `make test`) |
| `merge_conflict_check.py` | Merge conflict check runner |
| `__init__.py` | Package init |

**Note**: These check scripts are NOT referenced by any workflow file. They are
imported only by Python tests (`tests/scripts/test_checks.py`) and the shared
`egg_contracts` package. The local orchestrator has its own check infrastructure.

#### `action/` prompt builders (SDLC-specific, REMOVE)

| Script | Purpose | Used By |
|--------|---------|---------|
| `build-sdlc-prompt.sh` | Build SDLC work prompt | `sdlc-work-loop.yml` |
| `build-unified-review-prompt.sh` | Build unified review prompt | `sdlc-work-loop.yml` |
| `build-agent-mode-design-review-prompt-workloop.sh` | Agent-mode design review (workloop variant) | `sdlc-work-loop.yml` |
| `build-code-review-prompt-workloop.sh` | Code review (workloop variant) | `sdlc-work-loop.yml` |
| `build-contract-verification-prompt-workloop.sh` | Contract verification (workloop variant) | `sdlc-work-loop.yml` |
| `build-contract-verification-prompt.sh` | Contract verification | `on-pull-request-contract-verify.yml` |
| `build-coder-prompt.sh` | Coder agent prompt | `sdlc-multi-agent.yml` |
| `build-tester-prompt.sh` | Tester agent prompt | `sdlc-multi-agent.yml` |
| `build-documenter-prompt.sh` | Documenter agent prompt | `sdlc-multi-agent.yml` |
| `build-integrator-prompt.sh` | Integrator agent prompt | `sdlc-multi-agent.yml` |
| `build-mention-prompt.sh` | @mention response prompt | `on-mention.yml` |
| `build-onboarding-doc-prompt.sh` | Onboarding doc generation | Not used in any workflow |
| `contract-state.sh` | Contract state management | Not used in any workflow |
| `populate-contract-tasks.py` | Populate contract tasks | `sdlc-work-loop.yml` |

#### `action/` prompt builders (PR operations, KEEP)

| Script | Purpose | Used By |
|--------|---------|---------|
| `build-review-prompt.sh` | Code review prompt | `on-pull-request.yml`, `reusable-review.yml` |
| `build-autofixer-prompt.sh` | Auto-fix prompt | `on-check-failure.yml`, `reusable-autofix.yml` |
| `build-conflict-prompt.sh` | Conflict resolution prompt | `on-merge-conflict.yml`, `reusable-conflict-resolve.yml` |
| `build-feedback-prompt.sh` | Feedback response prompt | `on-review-feedback.yml` |
| `build-doc-updater-prompt.sh` | Post-merge doc updater prompt | `on-push-doc-updater.yml` |
| `build-agent-mode-design-review-prompt.sh` | Agent-mode design review | `on-pull-request-agent-mode-design.yml` |

#### `action/` infrastructure (KEEP)

| Script | Purpose |
|--------|---------|
| `entrypoint.sh` | GitHub Action entry point |
| `generate-config.sh` | Config generation for action |
| `action.yml` | GitHub Action definition |
| `README.md` | Action documentation |
| `autofixer-conventions.md` | Autofixer conventions reference |
| `conflict-conventions.md` | Conflict resolution conventions reference |
| `review-conventions.md` | Code review conventions reference |

## Feature Parity Analysis

### Local Orchestrator Coverage

The local orchestrator (`orchestrator/`) already provides equivalents for the
core SDLC pipeline capabilities:

| GitHub Actions Capability | Local Orchestrator Equivalent | Status |
|--------------------------|-------------------------------|--------|
| Phase management (refine→plan→implement→pr) | `orchestrator/dispatch.py` + `shared/egg_contracts/phase_defaults.py` | Implemented |
| Multi-agent dispatch (coder/tester/documenter/integrator) | `orchestrator/container_spawner.py` + `shared/egg_contracts/agent_roles.py` | Implemented |
| Dependency-based execution (wave scheduling) | `shared/egg_contracts/dependency_graph.py` | Implemented |
| HITL decisions (checkboxes, approvals) | `orchestrator/decision_queue.py` | Implemented |
| Contract state management | `orchestrator/state_store.py` (git-backed) | Implemented |
| Pipeline visualization | `orchestrator/dag_visualizer.py` (ASCII/Unicode DAG) | Implemented |
| Real-time monitoring | `orchestrator/sse.py` + `orchestrator/unified_sse.py` (SSE streams) | Implemented |
| Prompt building | `orchestrator/routes/pipelines.py` (ported from shell scripts) | Implemented |
| Phase checks (lint, test, draft validation) | `shared/egg_contracts/phase_defaults.py` | Implemented |

### Capabilities NOT Ported (Intentional)

| GitHub Actions Capability | Reason Not Ported |
|--------------------------|-------------------|
| GitHub issue label management (`sdlc:refine`, etc.) | Issue says "we don't want to port any behavior for interacting with github issues" |
| @mention-based task dispatch | Replaced by direct pipeline API calls |
| Self-improvement analysis of workflow runs | GitHub Actions-specific; local system has its own monitoring |
| SDLC label setup (`setup-sdlc-labels.sh`) | Issue-based SDLC labels are no longer needed |
| Contract push with conflict resolution | Local state store uses git commits directly |

### Potential Gaps to Verify

1. **Issue branch cleanup on close**: `on-issue-closed.yml` deletes branches
   and cancels runs when issues close. The local orchestrator manages worktree
   cleanup via `state_store.py` but may not have explicit branch deletion on
   pipeline cancellation. Verify the local cleanup is sufficient.

2. **Post-review-cycle escalation**: `sdlc-work-loop.yml` has max review cycle
   limits (default 3) with escalation. The local orchestrator has
   `max_review_cycles` in `PhaseConfig` — verify the enforcement is equivalent.

3. **Automated contract verification on PRs**: `on-pull-request-contract-verify.yml`
   runs contract compliance checks on `sdlc:pr` PRs. Since local orchestration
   creates PRs through the pipeline, the contract is verified during the pipeline
   flow — but there's no standalone PR-level gate. This is acceptable since the
   local orchestrator controls the entire flow.

## Constraints and Dependencies

1. **Test files reference check scripts**: `tests/scripts/test_checks.py` tests
   the `.github/scripts/checks/` modules. These tests should be removed or
   relocated when the checks are removed.

2. **`test-action.yml` references SDLC scripts**: The test-action workflow
   runs shellcheck on several prompt builder scripts. After removing SDLC
   scripts, update the shellcheck list to only include kept scripts.

3. **`shared/egg_contracts/` references check names**: The contracts package
   references check names like `draft-validation`, `plan-yaml`, etc. These
   are used by both the local orchestrator and the GHA checks. The shared
   package should be kept — only the `.github/scripts/checks/` runner should
   be removed.

4. **Documentation references**: `docs/guides/sdlc-pipeline.md` and
   `docs/architecture/README.md` reference GitHub Actions workflows. These
   should be updated to reflect the new local-only orchestration.

## Recommended Approach

### Phase 1: Audit and Categorize (this analysis)

Document exactly what to remove, keep, and update. *(Done)*

### Phase 2: Remove SDLC Workflows

Delete the following 8 workflow files:
- `.github/workflows/sdlc-pipeline.yml`
- `.github/workflows/sdlc-work-loop.yml`
- `.github/workflows/sdlc-hitl.yml`
- `.github/workflows/sdlc-multi-agent.yml`
- `.github/workflows/on-issue-closed.yml`
- `.github/workflows/on-mention.yml`
- `.github/workflows/on-pull-request-contract-verify.yml`
- `.github/workflows/self-improvement.yml`

### Phase 3: Remove SDLC Supporting Scripts

Delete the following:
- `.github/scripts/setup-sdlc-labels.sh`
- `.github/scripts/push-contract-update.sh`
- `.github/scripts/transition-sdlc-label.sh`
- `.github/scripts/checks/` (entire directory)

### Phase 4: Remove SDLC Prompt Builders from `action/`

Delete the following from `action/`:
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
- `build-onboarding-doc-prompt.sh` (unused)
- `contract-state.sh` (unused)
- `populate-contract-tasks.py`

### Phase 5: Update References

1. **`test-action.yml`**: Update shellcheck invocations to remove references to
   deleted scripts.
2. **`tests/scripts/test_checks.py`**: Remove or update tests for the deleted
   check scripts.
3. **Documentation**: Update `docs/guides/sdlc-pipeline.md` and
   `docs/architecture/README.md` to remove GitHub Actions SDLC references.
4. **`action/README.md`**: Update to remove references to deleted scripts.

### Phase 6: Verify

1. Run `make lint` and `make test` to ensure no broken imports or references.
2. Verify all kept workflows still function correctly (no missing dependencies).
3. Confirm `test-action.yml` passes without the deleted scripts.

## Summary

**Remove**: 8 workflows + 3 shell scripts + 9 check scripts + 14 prompt
builders = **34 files (~5,000+ lines)**

**Keep**: 9 PR workflows + 7 infrastructure workflows + 6 prompt builders + 7
action infrastructure files = **29 files**

**Update**: 4 files with reference updates needed

The local orchestrator already has feature parity for all SDLC pipeline
capabilities. The removal is safe, with the only gaps being intentional
(no issue interaction, no self-improvement workflow).
