# Plan: Update all SDLC related workflows to be reusable

> Issue: #255 | Phase: plan

## Summary

This plan converts the egg SDLC workflows into reusable workflows that other repositories can adopt. Following the "Hybrid - Reusable Core + Thin Wrappers with Defaults" approach from the analysis, we will parameterize hardcoded values (bot username, authorized users, action reference, branch prefix) across 11 SDLC workflows while maintaining backward compatibility via defaults matching current `jwbron/egg` configuration.

The key insight from the existing `reusable-review.yml` pattern is that workflows already support `workflow_call` triggers - we need to extend this parameterization consistently across all SDLC workflows and update the existing thin wrappers to demonstrate proper usage.

## Implementation Phases

### Phase 1: Core Parameterization Foundation

**Goal**: Add core input parameters to `reusable-review.yml` and update event-triggered wrappers to pass them.

**Tasks**:
- [TASK-1-1] Add `bot-username` input to `reusable-review.yml` (replacing hardcoded `james-in-a-box`) — Acceptance: Input defined with default `james-in-a-box`, used in `BOT_USERNAME` env var and all shell steps
- [TASK-1-2] Add `action-ref` input to `reusable-review.yml` (replacing hardcoded `jwbron/egg/action@main`) — Acceptance: Input defined with default `jwbron/egg/action@main`, used in `uses:` step dynamically
- [TASK-1-3] Update `on-pull-request.yml` to pass new inputs — Acceptance: Wrapper passes `bot-username` and `action-ref` with defaults matching current behavior
- [TASK-1-4] Update `on-pull-request-agent-mode-design.yml` to pass new inputs — Acceptance: Wrapper uses new inputs correctly
- [TASK-1-5] Update `on-pull-request-contract-verify.yml` to pass new inputs — Acceptance: Wrapper uses new inputs correctly

**Dependencies**: None

**Exit criteria**: All three PR-triggered review workflows work with new parameterized `reusable-review.yml`

### Phase 2: Autofix and Conflict Resolution Workflows

**Goal**: Convert `on-check-failure.yml` and `on-merge-conflict.yml` to reusable workflows with proper parameterization.

**Tasks**:
- [TASK-2-1] Create `reusable-autofix.yml` from `on-check-failure.yml` core logic — Acceptance: New reusable workflow accepts `bot-username`, `action-ref`, `pr_number`, `failed_workflow`, `failed_run_id` inputs
- [TASK-2-2] Convert `on-check-failure.yml` to thin wrapper calling `reusable-autofix.yml` — Acceptance: Existing workflow behavior preserved, uses reusable workflow internally
- [TASK-2-3] Create `reusable-conflict-resolve.yml` from `on-merge-conflict.yml` resolve logic — Acceptance: New reusable workflow accepts `bot-username`, `action-ref`, `pr_number`, `base_ref` inputs
- [TASK-2-4] Convert `on-merge-conflict.yml` to use `reusable-conflict-resolve.yml` — Acceptance: Scheduled and manual triggers work, resolve logic delegated to reusable workflow
- [TASK-2-5] Add `prompt-script` input to both reusable workflows — Acceptance: Consuming repos can override default prompt scripts

**Dependencies**: Phase 1 (establishes pattern)

**Exit criteria**: Both autofix and conflict resolution work as reusable workflows, backward compatible

### Phase 3: Feedback and Mention Workflows

**Goal**: Parameterize `on-review-feedback.yml` and `on-mention.yml` to accept bot identity configuration.

**Tasks**:
- [TASK-3-1] Add `workflow_call` trigger to `on-review-feedback.yml` with inputs — Acceptance: Accepts `bot-username`, `action-ref`, `authorized-users`, `max-feedback-rounds` as inputs with defaults
- [TASK-3-2] Replace hardcoded `james-in-a-box` checks in `on-review-feedback.yml` with input parameter — Acceptance: Job-level `if` condition uses workaround for input access, shell steps use env var
- [TASK-3-3] Add `workflow_call` trigger to `on-mention.yml` with inputs — Acceptance: Accepts `bot-username`, `action-ref`, `authorized-users`, `mention-patterns` as inputs
- [TASK-3-4] Replace hardcoded `@james-in-a-box` and `@egg` patterns with configurable input — Acceptance: Mention patterns defined as input, used in job-level conditions via workaround
- [TASK-3-5] Replace hardcoded `jwbron` authorization check with `authorized-users` input — Acceptance: Both workflows accept comma-separated list of authorized users

**Dependencies**: Phase 1

**Exit criteria**: Feedback and mention workflows support external configuration

### Phase 4: Core SDLC Pipeline Parameterization

**Goal**: Parameterize the main `sdlc-pipeline.yml` and `sdlc-hitl.yml` workflows.

**Tasks**:
- [TASK-4-1] Add `workflow_call` trigger to `sdlc-pipeline.yml` with full input set — Acceptance: Accepts `bot-username`, `action-ref`, `authorized-users`, `branch-prefix`, `sdlc-label` as inputs with defaults
- [TASK-4-2] Replace hardcoded `james-in-a-box` in git config and comment filtering — Acceptance: Git identity uses input, comment filtering uses input pattern
- [TASK-4-3] Replace hardcoded `egg/issue-` branch prefix with parameterized input — Acceptance: Branch naming uses `${branch-prefix}/issue-{N}` pattern
- [TASK-4-4] Add `workflow_call` trigger to `sdlc-hitl.yml` with authorization inputs — Acceptance: Accepts `bot-username`, `authorized-users`, `branch-prefix` as inputs
- [TASK-4-5] Replace hardcoded `jwbron` authorization in `sdlc-hitl.yml` — Acceptance: Authorization check uses input parameter via job-level workaround

**Dependencies**: Phase 1-3 (patterns established)

**Exit criteria**: Core SDLC orchestration supports external repos

### Phase 5: Utility Workflows and Documentation

**Goal**: Parameterize remaining workflows and add documentation for consuming repos.

**Tasks**:
- [TASK-5-1] Parameterize `on-issue-closed.yml` branch prefix — Acceptance: Uses `branch-prefix` input for `egg/issue-{N}` pattern
- [TASK-5-2] Parameterize `on-push-doc-updater.yml` with `action-ref` input — Acceptance: Uses configurable action reference
- [TASK-5-3] Parameterize `self-improvement.yml` with `action-ref` input — Acceptance: Uses configurable action reference
- [TASK-5-4] Create `docs/guides/reusable-workflows.md` consumer guide — Acceptance: Documents all inputs, shows example wrapper workflows
- [TASK-5-5] Create example wrapper workflows in `examples/` directory — Acceptance: Working examples for PR review, autofix, and SDLC pipeline

**Dependencies**: Phases 1-4

**Exit criteria**: All SDLC workflows parameterized, documentation complete

## Test Strategy

- **Unit tests**: Validate YAML syntax with `yamllint` for all modified workflows
- **Integration tests**:
  - Create a test repository that calls the reusable workflows with non-default inputs
  - Verify workflows run with custom bot username, action ref, and branch prefix
  - Test backward compatibility by ensuring `jwbron/egg` workflows still work with defaults
- **Manual testing**:
  - Trigger each workflow manually via `workflow_dispatch` to verify parameterization
  - Verify comment filtering works with custom bot usernames
  - Verify branch creation uses correct prefix pattern

## Rollback Plan

1. All changes are additive (new inputs with defaults) — existing behavior preserved
2. If issues arise, revert the specific workflow file to main:
   ```bash
   git checkout main -- .github/workflows/<workflow>.yml
   git push origin egg/issue-255
   ```
3. Reusable workflows are called via relative paths (`./.github/workflows/`) so reverting is isolated
4. No database migrations or external state changes

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breaking `workflow_call` limitations (no nested reusable workflows) | Low | High | Flatten job logic into single reusable workflow rather than chaining |
| Job-level `if` conditions can't access inputs directly | Med | Med | Use documented workaround: pass inputs as job outputs from prior job |
| GitHub Action `uses:` can't be dynamic expressions | High | High | Document that `action-ref` requires wrapper workflow to set correct value; provide examples |
| Secrets not passed by default to reusable workflows | Low | Low | Already handled via explicit `secrets:` block in existing pattern |
| Cross-repo workflow calls require public repo | Low | Med | Document requirement; egg repo is already public |

## Migration Notes

**For consuming repositories:**

1. Copy example wrapper workflows from `examples/` to your `.github/workflows/`
2. Update inputs to match your bot identity:
   - `bot-username`: Your GitHub App bot username
   - `action-ref`: `jwbron/egg/action@main` (or pin to specific version)
   - `authorized-users`: Comma-separated list of authorized GitHub usernames
   - `branch-prefix`: Your preferred branch prefix (e.g., `agent` instead of `egg`)
3. Configure required secrets in repository settings:
   - `BOT_APP_ID`
   - `BOT_APP_PRIVATE_KEY`
   - `BOT_APP_INSTALLATION_ID`
   - `ANTHROPIC_OAUTH_TOKEN`

**Breaking changes:** None. All inputs have defaults matching current behavior.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Make SDLC workflows reusable for external repositories"
  description: |
    Converts all SDLC-related workflows to reusable workflows that other repositories
    can adopt. Parameterizes hardcoded values (bot username, authorized users, action
    reference, branch prefix) while maintaining backward compatibility via defaults.

    Closes #255
phases:
  - id: 1
    name: Core Parameterization Foundation
    goal: Add core input parameters to reusable-review.yml and update wrappers
    tasks:
      - id: TASK-1-1
        description: Add bot-username input to reusable-review.yml
        acceptance: Input defined with default, used in BOT_USERNAME env var and shell steps
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-1-2
        description: Add action-ref input to reusable-review.yml
        acceptance: Input defined with default, used in uses step
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-1-3
        description: Update on-pull-request.yml to pass new inputs
        acceptance: Wrapper passes inputs with defaults matching current behavior
        files:
          - .github/workflows/on-pull-request.yml
      - id: TASK-1-4
        description: Update on-pull-request-agent-mode-design.yml to pass new inputs
        acceptance: Wrapper uses new inputs correctly
        files:
          - .github/workflows/on-pull-request-agent-mode-design.yml
      - id: TASK-1-5
        description: Update on-pull-request-contract-verify.yml to pass new inputs
        acceptance: Wrapper uses new inputs correctly
        files:
          - .github/workflows/on-pull-request-contract-verify.yml
  - id: 2
    name: Autofix and Conflict Resolution Workflows
    goal: Convert autofix and conflict resolution to reusable workflows
    tasks:
      - id: TASK-2-1
        description: Create reusable-autofix.yml from on-check-failure.yml core logic
        acceptance: New reusable workflow accepts bot-username, action-ref, pr_number inputs
        files:
          - .github/workflows/reusable-autofix.yml
      - id: TASK-2-2
        description: Convert on-check-failure.yml to thin wrapper
        acceptance: Existing behavior preserved, uses reusable workflow internally
        files:
          - .github/workflows/on-check-failure.yml
      - id: TASK-2-3
        description: Create reusable-conflict-resolve.yml from on-merge-conflict.yml
        acceptance: New reusable workflow accepts bot-username, action-ref, pr_number inputs
        files:
          - .github/workflows/reusable-conflict-resolve.yml
      - id: TASK-2-4
        description: Convert on-merge-conflict.yml to use reusable workflow
        acceptance: Scheduled and manual triggers work, resolve logic delegated
        files:
          - .github/workflows/on-merge-conflict.yml
      - id: TASK-2-5
        description: Add prompt-script input to both reusable workflows
        acceptance: Consuming repos can override default prompt scripts
        files:
          - .github/workflows/reusable-autofix.yml
          - .github/workflows/reusable-conflict-resolve.yml
  - id: 3
    name: Feedback and Mention Workflows
    goal: Parameterize feedback and mention workflows for external configuration
    tasks:
      - id: TASK-3-1
        description: Add workflow_call trigger to on-review-feedback.yml with inputs
        acceptance: Accepts bot-username, action-ref, authorized-users, max-feedback-rounds
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-3-2
        description: Replace hardcoded james-in-a-box in on-review-feedback.yml
        acceptance: Job-level if uses workaround, shell steps use env var
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-3-3
        description: Add workflow_call trigger to on-mention.yml with inputs
        acceptance: Accepts bot-username, action-ref, authorized-users, mention-patterns
        files:
          - .github/workflows/on-mention.yml
      - id: TASK-3-4
        description: Replace hardcoded mention patterns with configurable input
        acceptance: Mention patterns defined as input, used in conditions
        files:
          - .github/workflows/on-mention.yml
      - id: TASK-3-5
        description: Replace hardcoded jwbron authorization with authorized-users input
        acceptance: Both workflows accept comma-separated list of authorized users
        files:
          - .github/workflows/on-review-feedback.yml
          - .github/workflows/on-mention.yml
  - id: 4
    name: Core SDLC Pipeline Parameterization
    goal: Parameterize main SDLC pipeline and HITL workflows
    tasks:
      - id: TASK-4-1
        description: Add workflow_call trigger to sdlc-pipeline.yml with full input set
        acceptance: Accepts bot-username, action-ref, authorized-users, branch-prefix inputs
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-2
        description: Replace hardcoded james-in-a-box in git config and filtering
        acceptance: Git identity and comment filtering use input parameter
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-3
        description: Replace hardcoded egg/issue- branch prefix with input
        acceptance: Branch naming uses parameterized prefix pattern
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-4
        description: Add workflow_call trigger to sdlc-hitl.yml with authorization inputs
        acceptance: Accepts bot-username, authorized-users, branch-prefix inputs
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-4-5
        description: Replace hardcoded jwbron authorization in sdlc-hitl.yml
        acceptance: Authorization check uses input parameter
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 5
    name: Utility Workflows and Documentation
    goal: Parameterize remaining workflows and add consumer documentation
    tasks:
      - id: TASK-5-1
        description: Parameterize on-issue-closed.yml branch prefix
        acceptance: Uses branch-prefix input for pattern matching
        files:
          - .github/workflows/on-issue-closed.yml
      - id: TASK-5-2
        description: Parameterize on-push-doc-updater.yml with action-ref input
        acceptance: Uses configurable action reference
        files:
          - .github/workflows/on-push-doc-updater.yml
      - id: TASK-5-3
        description: Parameterize self-improvement.yml with action-ref input
        acceptance: Uses configurable action reference
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-5-4
        description: Create docs/guides/reusable-workflows.md consumer guide
        acceptance: Documents all inputs, shows example wrapper workflows
        files:
          - docs/guides/reusable-workflows.md
      - id: TASK-5-5
        description: Create example wrapper workflows in examples/ directory
        acceptance: Working examples for PR review, autofix, and SDLC pipeline
        files:
          - examples/workflows/on-pull-request.yml
          - examples/workflows/on-check-failure.yml
          - examples/workflows/sdlc-pipeline.yml
```

---

## Phase Approval

When posting this plan as a GitHub comment, include an approval section at the end.
Use the phase-completion template format with the `<!-- egg-phase-approval -->` marker:

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
