# Plan: Remove Remaining Hardcoded Bot Usernames

> Issue: #458 | Phase: plan

## Summary

This plan addresses the remaining hardcoded `james-in-a-box` bot usernames identified in PR #457's code review (issue #9). The analysis phase identified 26 instances across the codebase, of which 6 are problematic hardcoded step inputs and 1 is a problematic job-level `if:` condition. The recommended approach moves the self-trigger check from job-level to step-level and makes all workflow step inputs reference repository or organization variables (`vars.BOT_USERNAME`).

## Implementation Phases

### Phase 1: Add Repository Variable Support

**Goal**: Enable workflows to read bot username from repository/organization variables as a single source of truth for this repository's configuration.

**Tasks**:
- [TASK-1-1] Document `BOT_USERNAME` repository variable requirement — Acceptance: README or docs updated with setup instructions for `vars.BOT_USERNAME`
- [TASK-1-2] Update entry-point workflows to use `vars.BOT_USERNAME` with fallback — Acceptance: All 6 hardcoded step inputs replaced with `${{ vars.BOT_USERNAME || 'egg' }}`

**Files to modify**:
- `.github/workflows/on-check-failure.yml` (line 71)
- `.github/workflows/on-pull-request.yml` (line 23)
- `.github/workflows/on-merge-conflict.yml` (lines 117, 137)
- `.github/workflows/on-pull-request-agent-mode-design.yml` (line 31)
- `.github/workflows/on-pull-request-contract-verify.yml` (line 76)

**Dependencies**: None

**Exit criteria**: All entry-point workflows use `vars.BOT_USERNAME` with generic fallback; existing behavior preserved when variable is not set.

### Phase 2: Move Self-Trigger Check to Step Level

**Goal**: Remove the hardcoded bot username from the job-level `if:` condition in `sdlc-hitl.yml` by moving the self-trigger prevention logic into a step that can access `needs` outputs.

**Tasks**:
- [TASK-2-1] Remove hardcoded sender check from job-level `if:` condition — Acceptance: Job `if:` only checks for feedback comment markers, not sender identity
- [TASK-2-2] Add early step to check for self-trigger using configurable bot_username — Acceptance: New step exits gracefully (exit 0) if sender matches configured bot username

**Files to modify**:
- `.github/workflows/sdlc-hitl.yml` (lines 878-882, new step around line 890)

**Dependencies**: Phase 1 (conceptually; can be done in parallel since this uses `needs.resolve-inputs.outputs.bot_username`)

**Exit criteria**: Self-trigger prevention works with any configured bot username; job shows as "started" but exits cleanly when triggered by bot.

### Phase 3: Update Script and Module Defaults

**Goal**: Change hardcoded defaults in shell scripts and Python modules from `james-in-a-box` to a generic default (`egg`) that clearly indicates it should be overridden.

**Tasks**:
- [TASK-3-1] Update `action/build-mention-prompt.sh` default — Acceptance: Default changed to `egg`, comment updated to reflect this
- [TASK-3-2] Update `sandbox/egg_lib/self_improvement/config.py` default — Acceptance: Default changed to `egg`

**Files to modify**:
- `action/build-mention-prompt.sh` (lines 11, 18)
- `sandbox/egg_lib/self_improvement/config.py` (line 7)

**Dependencies**: None (can run in parallel with Phases 1-2)

**Exit criteria**: All script/module defaults use generic `egg` value; functionality unchanged when environment variables are set.

### Phase 4: Update Documentation and Comments

**Goal**: Update documentation examples and error messages to use the generic default or provide clearer guidance.

**Tasks**:
- [TASK-4-1] Update `gateway/policy.py` comment examples — Acceptance: Comments show generic example or explain configuration requirement
- [TASK-4-2] Update test fixtures to use generic bot name — Acceptance: Tests use `test-bot` or similar generic name; all tests pass

**Files to modify**:
- `gateway/policy.py` (lines 56, 94)
- `gateway/tests/test_policy.py` (lines 129-134)

**Dependencies**: None (can run in parallel with Phases 1-3)

**Exit criteria**: All documentation uses generic examples; tests are self-consistent with generic fixtures.

## Test Strategy

- **Unit tests**: Run existing test suite (`make test`) to verify no regressions. The `gateway/tests/test_policy.py` changes will require updating the test expectations.
- **Integration tests**: Manual verification that:
  - Workflows trigger correctly when `vars.BOT_USERNAME` is set
  - Workflows fall back to `egg` default when variable is not set
  - Self-trigger prevention works in `sdlc-hitl.yml`
- **Manual testing**: Create a test PR and verify:
  - Bot comments don't trigger self-feedback loops
  - All workflow jobs complete successfully

## Rollback Plan

If issues arise after deployment:

1. **Immediate**: Set `vars.BOT_USERNAME` to `james-in-a-box` in repository settings to restore original behavior
2. **Code rollback**: Revert the merge commit via:
   ```bash
   git revert <merge-commit-sha>
   git push origin main
   ```
3. **Verification**: Trigger a test workflow to confirm original behavior restored

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Workflows fail when `vars.BOT_USERNAME` not set | Low | Medium | Fallback to `egg` default; document setup requirement |
| Self-trigger check fails at step level | Low | Low | Job will run but exit cleanly; existing step-level auth check provides backup |
| Test fixtures break with generic bot name | Low | Low | Update assertions to match new generic name |
| External adopters confused by change | Medium | Low | Clear documentation in release notes |

## Migration Notes

**For this repository (jwbron/egg)**:
1. After merging, set repository variable: Settings → Secrets and variables → Actions → Variables → New variable: `BOT_USERNAME` = `james-in-a-box`

**For external adopters**:
- Set `vars.BOT_USERNAME` to your GitHub App's username
- Or pass `bot_username` input when calling reusable workflows

**No breaking changes**: All workflows maintain backwards compatibility via fallback defaults.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Remove hardcoded bot usernames, use vars.BOT_USERNAME"
  description: |
    Replaces hardcoded `james-in-a-box` bot usernames with configurable
    `vars.BOT_USERNAME` repository variable. Moves self-trigger prevention
    from job-level to step-level in sdlc-hitl.yml to access configured value.

    Fixes #458
phases:
  - id: 1
    name: Add Repository Variable Support
    goal: Enable workflows to read bot username from repository/organization variables
    tasks:
      - id: TASK-1-1
        description: Document BOT_USERNAME repository variable requirement
        acceptance: README or docs updated with setup instructions for vars.BOT_USERNAME
        files:
          - docs/setup.md
      - id: TASK-1-2
        description: Update entry-point workflows to use vars.BOT_USERNAME with fallback
        acceptance: All 6 hardcoded step inputs replaced with vars.BOT_USERNAME || 'egg'
        files:
          - .github/workflows/on-check-failure.yml
          - .github/workflows/on-pull-request.yml
          - .github/workflows/on-merge-conflict.yml
          - .github/workflows/on-pull-request-agent-mode-design.yml
          - .github/workflows/on-pull-request-contract-verify.yml
  - id: 2
    name: Move Self-Trigger Check to Step Level
    goal: Remove hardcoded bot username from job-level if condition in sdlc-hitl.yml
    tasks:
      - id: TASK-2-1
        description: Remove hardcoded sender check from job-level if condition
        acceptance: Job if only checks for feedback comment markers, not sender identity
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-2-2
        description: Add early step to check for self-trigger using configurable bot_username
        acceptance: New step exits gracefully if sender matches configured bot username
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 3
    name: Update Script and Module Defaults
    goal: Change hardcoded defaults from james-in-a-box to generic egg default
    tasks:
      - id: TASK-3-1
        description: Update action/build-mention-prompt.sh default
        acceptance: Default changed to egg, comment updated
        files:
          - action/build-mention-prompt.sh
      - id: TASK-3-2
        description: Update sandbox/egg_lib/self_improvement/config.py default
        acceptance: Default changed to egg
        files:
          - sandbox/egg_lib/self_improvement/config.py
  - id: 4
    name: Update Documentation and Comments
    goal: Update documentation examples and test fixtures to use generic values
    tasks:
      - id: TASK-4-1
        description: Update gateway/policy.py comment examples
        acceptance: Comments show generic example or explain configuration requirement
        files:
          - gateway/policy.py
      - id: TASK-4-2
        description: Update test fixtures to use generic bot name
        acceptance: Tests use test-bot or similar generic name; all tests pass
        files:
          - gateway/tests/test_policy.py
```

---

*Authored-by: egg*
