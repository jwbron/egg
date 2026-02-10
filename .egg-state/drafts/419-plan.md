# Plan: Audit workflow triggers for security

> Issue: #419 | Phase: plan

## Summary

This plan implements a tiered authorization model for GitHub workflow triggers, addressing the security gaps identified in the analysis phase. Based on the human feedback requiring configurable authorization for predefined users/orgs, we will add authorization checks to three key workflows (`sdlc-pipeline.yml`, `on-pull-request.yml`, `on-check-failure.yml`) and create documentation for the authorization model.

## Implementation Phases

### Phase 1: Extract Reusable Authorization Logic

**Goal**: Create a reusable script for authorization checking to ensure consistency across all workflows and reduce duplication.

**Tasks**:
- [TASK-1-1] Create `.github/scripts/check-authorization.sh` script — Acceptance: Script accepts `SENDER_LOGIN`, `AUTHORIZED_USERS`, and `BOT_USERNAME` as inputs; outputs `authorized=true|false`; handles comma-separated user lists, bot self-trigger prevention, and GitHub organization membership lookups
- [TASK-1-2] Add unit tests for the authorization script — Acceptance: Tests cover authorized user, unauthorized user, bot self-trigger, and org membership scenarios

**Dependencies**: None

**Exit criteria**: Authorization script exists and passes all tests

### Phase 2: Add Authorization to SDLC Pipeline

**Goal**: Prevent unauthorized users from triggering the SDLC pipeline via label addition.

**Tasks**:
- [TASK-2-1] Add `authorized_users` input to `sdlc-pipeline.yml` — Acceptance: Input is configurable with default value `jwbron`; follows same pattern as `on-mention.yml`
- [TASK-2-2] Add `check-trigger` job to `sdlc-pipeline.yml` for `issues: [labeled]` events — Acceptance: Job runs before `init` job for label-triggered events; checks `github.event.sender.login` against `authorized_users`; skips for `workflow_dispatch` and `workflow_call` events
- [TASK-2-3] Update `init` job to depend on `check-trigger` — Acceptance: `init` only runs if `check-trigger` passes or was skipped (for non-label triggers)

**Dependencies**: Phase 1

**Exit criteria**: Unauthorized users cannot trigger SDLC pipeline by adding labels

### Phase 3: Add Authorization to Code Review Workflow

**Goal**: Ensure only predefined users can trigger automated code reviews on PRs.

**Tasks**:
- [TASK-3-1] Add `authorized_users` input to `on-pull-request.yml` — Acceptance: Input is configurable with default value `jwbron`
- [TASK-3-2] Add `check-author` job to `on-pull-request.yml` — Acceptance: For `pull_request` events, checks if PR author (`github.event.pull_request.user.login`) is in `authorized_users` list; skips for `workflow_dispatch`
- [TASK-3-3] Update `reusable-review.yml` to pass authorization input — Acceptance: Review workflow respects authorization from caller

**Dependencies**: Phase 1

**Exit criteria**: Only authorized users' PRs trigger automated code reviews

### Phase 4: Add Authorization to Autofix Workflow

**Goal**: Prevent unauthorized users from triggering autofix on their PRs.

**Tasks**:
- [TASK-4-1] Add `authorized_users` input to `on-check-failure.yml` — Acceptance: Input is configurable with default value `jwbron`
- [TASK-4-2] Add authorization check in `should-run` job — Acceptance: For `workflow_run` triggers, fetches PR author and checks against `authorized_users`; blocks autofix for unauthorized PRs
- [TASK-4-3] Update `reusable-autofix.yml` to accept and pass authorization — Acceptance: Autofix workflow respects authorization from caller

**Dependencies**: Phase 1

**Exit criteria**: Only authorized users' PRs can trigger autofix

### Phase 5: Documentation

**Goal**: Document the authorization model for maintainers and contributors.

**Tasks**:
- [TASK-5-1] Create `docs/security/authorization-model.md` — Acceptance: Documents which workflows require authorization; explains how to add/remove authorized users; describes the tiered model rationale; includes examples for common scenarios
- [TASK-5-2] Update root README or CLAUDE.md with link to security docs — Acceptance: Security documentation is discoverable

**Dependencies**: Phases 2-4

**Exit criteria**: Authorization model is documented and discoverable

## Test Strategy

- **Unit tests**: Shell script tests for `check-authorization.sh` covering:
  - Authorized user (returns `authorized=true`)
  - Unauthorized user (returns `authorized=false`)
  - Bot self-trigger prevention (returns `authorized=false`)
  - Multiple authorized users (comma-separated list works)
  - GitHub organization membership (if implemented)

- **Integration tests**: Manual testing via:
  - Create test PR from unauthorized account, verify no review triggered
  - Add `sdlc:refine` label from unauthorized account, verify no pipeline triggered
  - Create failing PR from unauthorized account, verify no autofix triggered

- **Manual testing**:
  1. Verify existing authorized user workflows still function
  2. Verify `workflow_dispatch` bypass works for admins
  3. Check GitHub Actions logs for clear authorization failure messages

## Rollback Plan

All changes are additive authorization checks. To rollback:

1. Revert the commits that added authorization checks
2. Or set `authorized_users` to `*` (if we implement wildcard support) to bypass checks
3. Each workflow change is isolated, so individual workflows can be reverted independently

Specific commands:
```bash
# Revert specific workflow
git revert <commit-sha-for-workflow>

# Or disable by setting authorized_users to broad list
# In workflow_dispatch, pass authorized_users: "user1,user2,user3,..."
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breaking existing authorized user workflows | Low | High | Thorough testing before merge; default to current behavior (jwbron authorized) |
| Over-blocking legitimate use cases | Medium | Medium | Start with minimal authorized user list; expand based on feedback |
| Performance impact from org membership checks | Low | Low | Cache org membership results; use lazy evaluation |
| Inconsistent authorization across workflows | Low | Medium | Use shared authorization script; document model clearly |
| GitHub API rate limiting on membership checks | Low | Medium | Batch requests where possible; implement caching |

## Migration Notes

- **No database migrations**: This change only affects workflow files
- **Config changes**: Repositories consuming these workflows as reusable workflows will need to pass `authorized_users` input if they want to customize the default
- **Breaking changes**: PRs from users not in `authorized_users` will no longer trigger reviews or autofix. This is the intended security improvement.
- **Backwards compatibility**: Default `authorized_users` value matches current behavior (only `jwbron`)

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add authorization checks to workflow triggers"
  description: |
    Implements tiered authorization model for GitHub workflow triggers to ensure
    only authorized users can invoke automated agents. Adds configurable
    `authorized_users` input to sdlc-pipeline, on-pull-request, and on-check-failure
    workflows. Creates shared authorization script and security documentation.

    Fixes #419
phases:
  - id: 1
    name: Extract Reusable Authorization Logic
    goal: Create reusable script for authorization checking
    tasks:
      - id: TASK-1-1
        description: Create check-authorization.sh script
        acceptance: Script handles authorized users, bot self-trigger, and outputs authorized flag
        files:
          - .github/scripts/check-authorization.sh
      - id: TASK-1-2
        description: Add unit tests for authorization script
        acceptance: Tests cover authorized, unauthorized, and bot self-trigger scenarios
        files:
          - .github/scripts/test-check-authorization.sh
  - id: 2
    name: Add Authorization to SDLC Pipeline
    goal: Prevent unauthorized users from triggering SDLC pipeline via labels
    tasks:
      - id: TASK-2-1
        description: Add authorized_users input to sdlc-pipeline.yml
        acceptance: Input is configurable with default value jwbron
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-2
        description: Add check-trigger job for label events
        acceptance: Job checks sender against authorized_users for label triggers
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-3
        description: Update init job to depend on check-trigger
        acceptance: Init only runs if authorization passes or was skipped
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 3
    name: Add Authorization to Code Review Workflow
    goal: Ensure only predefined users can trigger automated code reviews
    tasks:
      - id: TASK-3-1
        description: Add authorized_users input to on-pull-request.yml
        acceptance: Input is configurable with default value jwbron
        files:
          - .github/workflows/on-pull-request.yml
      - id: TASK-3-2
        description: Add check-author job for PR events
        acceptance: Job checks PR author against authorized_users
        files:
          - .github/workflows/on-pull-request.yml
      - id: TASK-3-3
        description: Update reusable-review.yml to pass authorization
        acceptance: Review workflow respects authorization from caller
        files:
          - .github/workflows/reusable-review.yml
  - id: 4
    name: Add Authorization to Autofix Workflow
    goal: Prevent unauthorized users from triggering autofix on their PRs
    tasks:
      - id: TASK-4-1
        description: Add authorized_users input to on-check-failure.yml
        acceptance: Input is configurable with default value jwbron
        files:
          - .github/workflows/on-check-failure.yml
      - id: TASK-4-2
        description: Add authorization check in should-run job
        acceptance: Job fetches PR author and checks against authorized_users
        files:
          - .github/workflows/on-check-failure.yml
      - id: TASK-4-3
        description: Update reusable-autofix.yml to accept authorization
        acceptance: Autofix workflow respects authorization from caller
        files:
          - .github/workflows/reusable-autofix.yml
  - id: 5
    name: Documentation
    goal: Document the authorization model
    tasks:
      - id: TASK-5-1
        description: Create authorization-model.md documentation
        acceptance: Documents workflows, authorized users config, and model rationale
        files:
          - docs/security/authorization-model.md
      - id: TASK-5-2
        description: Update README with link to security docs
        acceptance: Security documentation is discoverable
        files:
          - README.md
```

---

*Authored-by: egg*
