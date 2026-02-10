# Plan: Use a Separate Account for Bot Reviews

> Issue: #287 | Phase: plan

## Summary

This plan implements support for a separate GitHub App (`egg-reviewer`) to perform code reviews, allowing reviews to use the full GitHub Reviews API (approve/request-changes). The existing `james-in-a-box` account continues to handle implementation tasks (push, PR creation). This separation eliminates self-review conflicts and enables proper PR merge protection based on review status.

Based on the analysis (see issue comments), this is **Option A: Separate GitHub App for Reviews Only**. The human is creating the GitHub App; this plan covers adding codebase support for the reviewer token and removing self-review workarounds.

## Implementation Phases

### Phase 1: Gateway Support for Reviewer Token

**Goal**: Add gateway infrastructure to support a second GitHub App token for review operations.

**Tasks**:
- [TASK-1-1] Add reviewer bot configuration environment variables — Acceptance: Gateway reads `GATEWAY_REVIEWER_BOT_NAME` and generates reviewer identity variants (similar to `get_bot_identities()`)
- [TASK-1-2] Add reviewer token support to token_refresher — Acceptance: Token refresher can fetch and cache tokens for the reviewer app using `REVIEWER_APP_ID`, `REVIEWER_APP_PRIVATE_KEY`, `REVIEWER_APP_INSTALLATION_ID`
- [TASK-1-3] Add reviewer mode to GitHubClient — Acceptance: `GitHubClient` supports `mode="reviewer"` that uses reviewer token for operations
- [TASK-1-4] Add policy restrictions for reviewer account — Acceptance: Reviewer account is blocked from push/PR create operations (only review-related API calls allowed)
- [TASK-1-5] Add gateway tests for reviewer token handling — Acceptance: Unit tests verify reviewer token fetching, identity matching, and policy restrictions

**Dependencies**: None (first phase)

**Exit criteria**: Gateway can authenticate as reviewer bot and enforce appropriate restrictions.

### Phase 2: Workflow Updates for Reviewer Token

**Goal**: Update GitHub workflows to use the reviewer account for posting reviews.

**Tasks**:
- [TASK-2-1] Add reviewer secrets to reusable-review.yml — Acceptance: Workflow accepts `REVIEWER_APP_ID`, `REVIEWER_APP_PRIVATE_KEY`, `REVIEWER_APP_INSTALLATION_ID` secrets
- [TASK-2-2] Generate reviewer token in review job — Acceptance: Review job generates a separate token for the reviewer app
- [TASK-2-3] Update egg action to accept reviewer credentials — Acceptance: Action inputs support reviewer app credentials, passes them to gateway
- [TASK-2-4] Update on-pull-request.yml to pass reviewer secrets — Acceptance: Caller workflow passes reviewer secrets to reusable workflow
- [TASK-2-5] Update on-pull-request-agent-mode-design.yml similarly — Acceptance: Agent-mode design review workflow passes reviewer secrets
- [TASK-2-6] Update on-review-feedback.yml to recognize reviewer account — Acceptance: Feedback workflow recognizes reviews from reviewer bot (not just main bot)

**Dependencies**: Phase 1

**Exit criteria**: Review workflows use the reviewer account for posting reviews; reviews can approve/request-changes.

### Phase 3: Remove Self-Review Workarounds

**Goal**: Remove the self-review detection and fallback code now that reviews use a separate account.

**Tasks**:
- [TASK-3-1] Remove proactive self-review detection from gh wrapper — Acceptance: Lines 575-631 of `sandbox/scripts/gh` removed (self-review detection and downgrade logic)
- [TASK-3-2] Remove reactive self-review fallback from gh wrapper — Acceptance: Lines 683-724 of `sandbox/scripts/gh` removed (fallback to issue comment on self-review error)
- [TASK-3-3] Update review-conventions.md — Acceptance: "Self-Authored PRs" section (lines 37-45) removed or replaced with note about separate reviewer account
- [TASK-3-4] Remove issue comment search for self-reviews from reusable-review.yml — Acceptance: Lines 372-397 simplified (issue comment search for self-review markers no longer needed)
- [TASK-3-5] Remove issue_comment trigger from on-review-feedback.yml — Acceptance: Lines 13-14 and 128-137 removed (issue_comment event and associated condition no longer needed)
- [TASK-3-6] Update github-automation.md documentation — Acceptance: Documentation updated to describe separate reviewer account architecture

**Dependencies**: Phase 2

**Exit criteria**: All self-review workaround code removed; codebase simplified.

### Phase 4: Testing and Validation

**Goal**: Ensure the new dual-account flow works correctly end-to-end.

**Tasks**:
- [TASK-4-1] Update test_gh_wrapper.py for removed self-review logic — Acceptance: Tests for self-review handling removed or updated
- [TASK-4-2] Add integration test for reviewer account reviews — Acceptance: Integration test verifies reviewer can approve/request-changes on bot-authored PRs
- [TASK-4-3] Verify backward compatibility with marker format — Acceptance: Existing `<!-- egg-automated-review ... -->` markers continue to work; new reviews include correct bot name

**Dependencies**: Phase 3

**Exit criteria**: All tests pass; reviewer account can post formal reviews.

## Test Strategy

- **Unit tests**:
  - Gateway: Test reviewer token fetching, identity matching, policy restrictions
  - gh wrapper: Update tests to reflect removed self-review logic

- **Integration tests**:
  - End-to-end test with reviewer account posting a review on a bot-authored PR
  - Verify `--approve` and `--request-changes` work correctly

- **Manual testing**:
  1. Create a PR using the main bot account
  2. Trigger review workflow
  3. Verify review is posted by reviewer account with correct verdict
  4. Verify merge protection respects reviewer's request-changes

## Rollback Plan

1. **Feature flag approach**: The reviewer token is optional. If `REVIEWER_APP_*` secrets are not configured, fall back to existing behavior (main bot posts comment-only reviews).

2. **Revert commits**: If issues arise after deployment, revert the PR. Self-review workaround code should be kept in a separate final commit for easy selective revert.

3. **Secrets removal**: Remove reviewer secrets from repository settings to disable the feature.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Reviewer app not properly installed | Low | High | Verify installation before merging; document setup steps |
| Token permission issues | Medium | Medium | Test with minimal permissions first; document required permissions |
| Marker format changes break detection | Low | Medium | Keep marker format stable; test marker parsing |
| Missing edge cases in self-review removal | Medium | Low | Comprehensive review of all self-review code paths; staged rollout |

## Migration Notes

**Repository secrets to add** (done by human):
- `REVIEWER_APP_ID`
- `REVIEWER_APP_PRIVATE_KEY`
- `REVIEWER_APP_INSTALLATION_ID`

**Environment variables for gateway**:
- `GATEWAY_REVIEWER_BOT_NAME` (optional, defaults to `egg-reviewer`)
- Reviewer app credentials passed through existing mechanism

**Breaking changes**: None. Existing workflows continue to work. Reviewer account is additive.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add separate reviewer account for bot reviews"
  description: |
    Implements support for a separate GitHub App (egg-reviewer) to post code reviews,
    eliminating self-review conflicts and enabling full GitHub Reviews API functionality
    (approve/request-changes). The main bot account continues to handle implementation.

    Fixes #287
phases:
  - id: 1
    name: Gateway Support for Reviewer Token
    goal: Add gateway infrastructure to support a second GitHub App token for review operations
    tasks:
      - id: TASK-1-1
        description: Add reviewer bot configuration environment variables
        acceptance: Gateway reads GATEWAY_REVIEWER_BOT_NAME and generates reviewer identity variants
        files:
          - gateway/policy.py
      - id: TASK-1-2
        description: Add reviewer token support to token_refresher
        acceptance: Token refresher can fetch and cache tokens for the reviewer app
        files:
          - gateway/token_refresher.py
      - id: TASK-1-3
        description: Add reviewer mode to GitHubClient
        acceptance: GitHubClient supports mode="reviewer" that uses reviewer token
        files:
          - gateway/github_client.py
      - id: TASK-1-4
        description: Add policy restrictions for reviewer account
        acceptance: Reviewer account is blocked from push/PR create operations
        files:
          - gateway/policy.py
          - gateway/app.py
      - id: TASK-1-5
        description: Add gateway tests for reviewer token handling
        acceptance: Unit tests verify reviewer token fetching, identity matching, and policy restrictions
        files:
          - gateway/tests/test_policy.py
          - gateway/tests/test_github_client.py
  - id: 2
    name: Workflow Updates for Reviewer Token
    goal: Update GitHub workflows to use the reviewer account for posting reviews
    tasks:
      - id: TASK-2-1
        description: Add reviewer secrets to reusable-review.yml
        acceptance: Workflow accepts REVIEWER_APP_ID, REVIEWER_APP_PRIVATE_KEY, REVIEWER_APP_INSTALLATION_ID secrets
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-2-2
        description: Generate reviewer token in review job
        acceptance: Review job generates a separate token for the reviewer app
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-2-3
        description: Update egg action to accept reviewer credentials
        acceptance: Action inputs support reviewer app credentials, passes them to gateway
        files:
          - action/action.yml
          - action/generate-config.sh
      - id: TASK-2-4
        description: Update on-pull-request.yml to pass reviewer secrets
        acceptance: Caller workflow passes reviewer secrets to reusable workflow
        files:
          - .github/workflows/on-pull-request.yml
      - id: TASK-2-5
        description: Update on-pull-request-agent-mode-design.yml similarly
        acceptance: Agent-mode design review workflow passes reviewer secrets
        files:
          - .github/workflows/on-pull-request-agent-mode-design.yml
      - id: TASK-2-6
        description: Update on-review-feedback.yml to recognize reviewer account
        acceptance: Feedback workflow recognizes reviews from reviewer bot
        files:
          - .github/workflows/on-review-feedback.yml
  - id: 3
    name: Remove Self-Review Workarounds
    goal: Remove the self-review detection and fallback code
    tasks:
      - id: TASK-3-1
        description: Remove proactive self-review detection from gh wrapper
        acceptance: Lines 575-631 of sandbox/scripts/gh removed
        files:
          - sandbox/scripts/gh
      - id: TASK-3-2
        description: Remove reactive self-review fallback from gh wrapper
        acceptance: Lines 683-724 of sandbox/scripts/gh removed
        files:
          - sandbox/scripts/gh
      - id: TASK-3-3
        description: Update review-conventions.md
        acceptance: Self-Authored PRs section removed or updated
        files:
          - action/review-conventions.md
      - id: TASK-3-4
        description: Remove issue comment search for self-reviews from reusable-review.yml
        acceptance: Lines 372-397 simplified
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-3-5
        description: Remove issue_comment trigger from on-review-feedback.yml
        acceptance: issue_comment event and associated condition removed
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-3-6
        description: Update github-automation.md documentation
        acceptance: Documentation describes separate reviewer account architecture
        files:
          - docs/guides/github-automation.md
  - id: 4
    name: Testing and Validation
    goal: Ensure the new dual-account flow works correctly end-to-end
    tasks:
      - id: TASK-4-1
        description: Update test_gh_wrapper.py for removed self-review logic
        acceptance: Tests for self-review handling removed or updated
        files:
          - tests/sandbox/test_gh_wrapper.py
      - id: TASK-4-2
        description: Add integration test for reviewer account reviews
        acceptance: Integration test verifies reviewer can approve/request-changes
        files:
          - integration_tests/sdlc/test_review_rejection.py
      - id: TASK-4-3
        description: Verify backward compatibility with marker format
        acceptance: Existing markers continue to work
        files:
          - .github/workflows/reusable-review.yml
```

---

*Authored-by: egg*
