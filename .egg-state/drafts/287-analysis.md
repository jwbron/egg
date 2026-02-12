# Analysis: Use a separate account for bot reviews

> Issue: #287 | Phase: refine

## Problem Statement

Currently, the egg bot uses a single GitHub account (`james-in-a-box`) for both authoring PRs and reviewing them. This creates a fundamental conflict:

1. **GitHub blocks self-reviews**: The GitHub API prevents users/bots from approving or requesting changes on their own PRs. When the bot tries to review its own PR, the operation fails with "You can't review your own pull request."

2. **Current workaround is suboptimal**: The codebase has extensive workaround code (`sandbox/scripts/gh:575-727`) that detects self-authored PRs and downgrades `--approve` or `--request-changes` to `--comment`. While this preserves review content, the PR never actually gets approved or blocked — the formal review state is lost.

3. **Confusion and complexity**: The self-review handling adds complexity to the codebase and can confuse users who expect a "request changes" review to block merging.

The desired outcome is to use a separate GitHub account for reviews so that:
- Reviews can use the full GitHub review API (approve/request-changes)
- The self-review workaround code can be removed
- PR merge protection based on review status works correctly

## Current Behavior

### Single Bot Account

The bot account `james-in-a-box` is hardcoded in 13+ workflow files:
- `.github/workflows/on-pull-request.yml:23` (bot_username: james-in-a-box)
- `.github/workflows/reusable-review.yml:18` (default value)
- `.github/workflows/on-review-feedback.yml:31` (default value)
- And 10+ other workflow files

### Self-Review Detection Logic

The `gh` wrapper script (`sandbox/scripts/gh:575-631`) contains proactive self-review detection:

```bash
# Compare PR author against bot identity patterns
if [ "$pr_author" = "$bot_name" ] || \
   [ "$pr_author" = "${bot_name}[bot]" ] || \
   [ "$pr_author" = "app/${bot_name}" ]; then
    is_self_authored=true
    review_type="comment"  # Downgrade from approve/request-changes
fi
```

### Fallback Handling

If the proactive detection misses a self-review and GitHub rejects the request (`sandbox/scripts/gh:683-724`), the code falls back to posting an issue comment instead:

```bash
if echo "$error_msg" | grep -qiE "(cannot review your own|can't review your own)"; then
    # Fall back to issue comment with preserved verdict in marker
    call_gateway "/api/v1/gh/pr/comment" "$comment_payload"
fi
```

### Review Conventions Documentation

`action/review-conventions.md:37-45` explicitly documents the self-review limitation:

> When reviewing a PR authored by the same bot account, **use `--comment` instead of
> `--request-changes` or `--approve`**. GitHub does not allow bots to request changes
> on their own PRs, and approval has no effect.

### Marker System

Reviews include a marker comment for tracking:
```
<!-- egg-automated-review bot=<name> commit=<sha> verdict=<verdict> -->
```

The `verdict` field preserves the original intent (approve/request-changes) even when downgraded to a comment for self-reviews.

### Workflow Logic for Self-Reviews

`on-review-feedback.yml:129-137` specifically handles self-reviews posted as issue comments:

```yaml
# For issue_comment events (self-review as comment)
if [[ "$IS_PR" == "true" && \
      ("$COMMENT_USER" == "$BOT_USERNAME" || "$COMMENT_USER" == "${BOT_USERNAME}[bot]") && \
      "$COMMENT_BODY" == *"egg-automated-review"* && \
      "$COMMENT_BODY" != *"verdict=approve"* ]]; then
```

## Constraints

**Technical constraints:**
- GitHub App tokens are installation-scoped — need a second GitHub App for the reviewer account
- Secrets management: workflows will need separate secrets for the reviewer app (REVIEWER_APP_ID, REVIEWER_APP_PRIVATE_KEY, etc.)
- The gateway sidecar routes GitHub operations — may need configuration for the reviewer account
- The `EGG_BOT_NAME` environment variable is used for self-review detection — needs separate handling

**Security constraints:**
- Reviewer account should NOT have push access to prevent accidental code changes during review
- Token permissions should be scoped appropriately (pull-requests: write, but not contents: write)

**Backwards compatibility:**
- The automated review marker format should remain stable
- Existing workflows using `bot_username` input should continue to work

**Operational constraints:**
- Need to provision a second GitHub App and install it on repositories
- Configuration updates in repo settings for the new app

## Options Considered

### Option A: Separate GitHub App for Reviews Only

**Approach**: Create a second GitHub App (e.g., `egg-reviewer`) with a dedicated installation for review operations. The existing `james-in-a-box` app continues to handle implementation (push, PR creation), while the new app handles all review operations.

**Pros:**
- Clean separation of concerns (implementer vs reviewer)
- Full GitHub review API functionality (approve/request-changes work)
- Can remove all self-review workaround code
- Better security model (reviewer has no push access)
- Aligns with SDLC conceptual roles (Role.IMPLEMENTER vs Role.REVIEWER)

**Cons:**
- Requires provisioning and managing a second GitHub App
- More secrets to configure in repository settings (6 → 9-12 secrets)
- Workflow changes to use different tokens for review vs implementation

### Option B: Use a Personal Access Token (PAT) for Reviews

**Approach**: Use a personal access token from a separate GitHub user account for reviews, while keeping the GitHub App for implementation.

**Pros:**
- Simpler than creating a second GitHub App
- PATs are easier to manage

**Cons:**
- PATs have broader permissions than GitHub App tokens
- Tied to a user account (less suitable for organizational use)
- Token rotation is more manual
- GitHub recommends GitHub Apps over PATs for automation

### Option C: Keep Single Account, Accept Comment-Only Reviews

**Approach**: Accept the current limitation where self-reviews are posted as comments. Enhance the marker system to better communicate review verdicts in the PR UI.

**Pros:**
- No infrastructure changes required
- Existing code handles this case

**Cons:**
- Doesn't address the core issue — PR merge protection based on review status won't work
- Confusion persists — "request changes" reviews don't actually block merging
- Self-review code complexity remains

### Option D: Fork the PR to a Different Branch for Review

**Approach**: When reviewing a bot-authored PR, temporarily push to a different branch under a different owner, review that, then sync back.

**Pros:**
- Works within GitHub's constraints without a second account

**Cons:**
- Extremely complex implementation
- Race conditions and sync issues
- Poor user experience (PR history becomes confusing)
- Not a real solution to the underlying problem

## Recommended Approach

**Option A: Separate GitHub App for Reviews Only** is recommended.

**Rationale:**

1. **Proper solution**: This is the only approach that enables full GitHub review API functionality. Reviews will actually block or approve PRs as intended.

2. **Clean separation**: The SDLC already has conceptual roles (IMPLEMENTER, REVIEWER). Having separate GitHub identities aligns the technical implementation with this model.

3. **Simplified codebase**: Can remove ~150 lines of self-review detection and fallback code:
   - `sandbox/scripts/gh:575-727` (proactive detection + fallback)
   - `action/review-conventions.md:37-45` (self-review section)
   - `reusable-review.yml:372-396` (issue comment search for self-reviews)

4. **Better security**: Reviewer account can be scoped with minimal permissions:
   - `pull-requests: write` (to post reviews)
   - NO `contents: write` (can't push code)

5. **Industry standard**: Using separate service accounts for different automation functions is a common pattern in CI/CD systems.

**Implementation outline:**

1. **Create the reviewer GitHub App** (`egg-reviewer`)
   - Permissions: `pull-requests: write`, `issues: read`
   - Install on target repositories

2. **Add reviewer secrets to repositories**
   - `REVIEWER_APP_ID`
   - `REVIEWER_APP_PRIVATE_KEY`
   - `REVIEWER_APP_INSTALLATION_ID`

3. **Update workflow files**
   - Add `reviewer_bot_username` input to reusable workflows
   - Generate reviewer token using the new secrets
   - Use reviewer token for `gh pr review` operations

4. **Remove self-review workaround code**
   - Remove self-review detection from `sandbox/scripts/gh`
   - Remove fallback-to-comment logic
   - Update documentation

5. **Update tests**
   - Remove/update tests for self-review behavior
   - Add tests for the new dual-account flow

## Code to be Removed

After implementing the separate reviewer account, the following code can be removed:

| File | Lines | Description |
|------|-------|-------------|
| `sandbox/scripts/gh` | 575-631 | Proactive self-review detection and downgrade |
| `sandbox/scripts/gh` | 683-724 | Reactive fallback to issue comment |
| `action/review-conventions.md` | 37-45 | Self-authored PRs section |
| `.github/workflows/reusable-review.yml` | 372-396 | Issue comment search for self-review markers |
| `.github/workflows/on-review-feedback.yml` | 129-137 | Issue comment trigger condition for self-reviews |

## Open Questions

1. **Reviewer app name**: What should the reviewer GitHub App be called? Options:
   - `egg-reviewer` (clear purpose)
   - `egg-review-bot` (explicit bot designation)
   - Something else?

2. **Repository installation**: Should the reviewer app be installed on all repositories that use egg, or only specific ones?

3. **Token management in gateway**: Does the gateway sidecar need awareness of the reviewer account, or can it remain agnostic since reviews don't go through branch ownership checks?

4. **Rollout strategy**: Should this be deployed incrementally (one repo at a time) or all at once?

---

*Authored-by: egg*
