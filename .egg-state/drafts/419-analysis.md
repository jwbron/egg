# Analysis: Audit workflow triggers for security

> Issue: #419 | Phase: refine

## Problem Statement

The egg system triggers automated agents in response to GitHub events (issue comments, PR reviews, label additions, etc.). We need to ensure that only authorized users or bots can trigger these workflows to prevent:
1. Unauthorized users from invoking expensive agent operations
2. Malicious actors from manipulating the SDLC pipeline
3. Prompt injection attacks via untrusted content

## Current Behavior

The repository contains 23 workflow files with various trigger mechanisms. Here's the current authorization landscape:

### Workflows WITH Authorization Checks

| Workflow | Trigger | Authorization Mechanism |
|----------|---------|------------------------|
| `on-mention.yml` | `issue_comment`, `pull_request_review`, etc. | `authorized_users` input (default: `jwbron`) checked against `github.event.sender.login` |
| `sdlc-hitl.yml` | `issue_comment` (edited) | `authorized_users` input checked in `check-trigger` and `check-approval-trigger` jobs |
| `on-review-feedback.yml` | `pull_request_review`, `issue_comment` | Implicitly authorized (only triggers on bot's own reviews via marker detection) |

### Workflows WITHOUT Explicit Authorization Checks

| Workflow | Trigger | Risk Assessment |
|----------|---------|-----------------|
| `on-check-failure.yml` | `workflow_run` (Lint, Test completion) | **Low** - Only triggers on check failures for PRs, limited attack surface |
| `on-issue-closed.yml` | `issues` (closed) | **Low** - Cleanup only, requires label presence, no agent invocation |
| `on-merge-conflict.yml` | `push` to main, `schedule` | **Low** - Triggers on repo events, not user-controllable content |
| `on-pull-request.yml` | `pull_request` events | **Medium** - Any PR author can trigger review; review is read-only |
| `on-push-doc-updater.yml` | `push` to main | **Low** - Only on main branch (requires merge permission) |
| `sdlc-pipeline.yml` | `issues` (labeled), `workflow_dispatch` | **Medium** - Label trigger could be exploited |
| `sdlc-work-loop.yml` | `workflow_call` only | **Low** - Called by sdlc-pipeline, inherits parent authorization |
| `sdlc-multi-agent.yml` | `workflow_call` only | **Low** - Called by work-loop, inherits parent authorization |
| `self-improvement.yml` | `schedule`, `workflow_dispatch` | **Low** - No external trigger surface |

### Security Patterns Already in Place

1. **Trusted Checkout Pattern**: Many workflows checkout `main` first to build prompts from trusted code before checking out potentially malicious PR branches. This prevents prompt injection via modified prompt scripts.
   - `on-mention.yml:277-294` - "SECURITY: Always run build-mention-prompt.sh from a trusted checkout"
   - `on-review-feedback.yml:377-401`
   - `sdlc-work-loop.yml:284-295`

2. **Self-Trigger Prevention**: Workflows check if `sender.login` matches the bot username to prevent infinite loops.
   - `on-mention.yml:141-146`
   - `sdlc-hitl.yml:107-112`

3. **Marker-Based Detection**: Reviews use HTML comment markers to track state and prevent re-processing.
   - `reusable-review.yml:102-108` - `<!-- egg-automated-review bot=<name> commit=<sha> -->`

4. **Concurrency Controls**: Workflows use concurrency groups to prevent duplicate runs.
   - `on-mention.yml:197-199`
   - `sdlc-pipeline.yml:74-77`

## Constraints

- **GitHub App Authentication**: The bot uses GitHub App tokens, not personal access tokens
- **No Direct Token Access**: The gateway sidecar holds credentials; agents cannot exfiltrate them
- **Branch Ownership**: Gateway enforces `egg/` or `egg-` prefix for pushes
- **Merge Blocking**: Gateway blocks `gh pr merge` - humans must merge via GitHub UI

## Options Considered

### Option A: Add Authorization to All Event-Triggered Workflows

**Approach**: Add `authorized_users` checking to all workflows that respond to GitHub events (PRs, issues, comments).

**Pros**:
- Maximum protection against unauthorized triggers
- Consistent security model across all workflows
- Clear audit trail via the existing pattern

**Cons**:
- Breaks legitimate PR review functionality (external contributors can't get reviews)
- Over-restrictive for read-only operations like code review
- Significant maintenance burden

### Option B: Tiered Authorization Based on Action Type

**Approach**: Apply strict authorization only to workflows that can:
1. Execute agent code that modifies the repository
2. Trigger the SDLC pipeline
3. Consume significant compute resources

Read-only operations (like code review) remain open to all PR authors.

**Pros**:
- Balances security with usability
- Allows external contributors to participate
- Focuses protection on high-risk operations

**Cons**:
- Requires careful classification of workflows
- May miss edge cases

### Option C: Repository/Organization-Level Permissions Only

**Approach**: Rely solely on GitHub's native permission model (branch protection, CODEOWNERS, etc.) without additional authorization checks.

**Pros**:
- Simplest to maintain
- Leverages GitHub's built-in security

**Cons**:
- Insufficient for controlling agent invocation costs
- No protection against authorized users accidentally triggering expensive operations
- Doesn't address the specific concern of unauthorized agent triggers

## Recommended Approach

**Option B: Tiered Authorization Based on Action Type** is recommended.

The current implementation is mostly sound, with authorization checks on the highest-risk workflows (`on-mention.yml`, `sdlc-hitl.yml`). However, there are specific gaps that should be addressed:

### Gap 1: `sdlc-pipeline.yml` Label Trigger (Medium Priority)

**Issue**: The workflow triggers on `issues: [labeled]` with `sdlc:refine` label. Any user with issue label permission (typically all contributors) could add this label to trigger the pipeline.

**Location**: `sdlc-pipeline.yml:20-21`

**Recommendation**: Add authorization check before running init job:
```yaml
check-trigger:
  if: github.event_name == 'issues'
  steps:
    - name: Check if sender is authorized
      # Check github.event.sender.login against authorized_users
```

### Gap 2: `on-pull-request.yml` Review Trigger (Low Priority)

**Issue**: Any PR author can trigger an automated code review, consuming agent tokens.

**Location**: `on-pull-request.yml:4-5`

**Recommendation**: This is acceptable as-is because:
1. Reviews are read-only (no repo modifications)
2. The PR must pass CI checks before review runs (`reusable-review.yml:175-263`)
3. Cost is bounded by the `timeout` parameter (10 minutes)

However, if cost becomes a concern, add authorization check for non-bot PRs.

### Gap 3: `on-check-failure.yml` Autofix Trigger (Low Priority)

**Issue**: Triggered by workflow failures on PRs. An attacker could create a PR with intentionally failing tests to trigger the autofix agent.

**Location**: `on-check-failure.yml:6-9`

**Recommendation**: Add authorization check to verify PR author is authorized, or limit autofix to bot-authored PRs only (which is implicitly done in `on-review-feedback.yml:226-231`).

### Gap 4: Missing Documentation of Authorization Model

**Issue**: The authorization model is implemented but not documented, making it hard to audit or extend.

**Recommendation**: Create `docs/security/authorization-model.md` documenting:
1. Which workflows require authorization
2. How to add/remove authorized users
3. The tiered authorization rationale

## Open Questions

For questions requiring human input:

**1. Should external contributors be able to trigger code reviews?**
- Currently: Yes (any PR author triggers review)
- Alternative: Only authorized users' PRs get reviewed
- This impacts the open-source contribution experience

**2. What is the acceptable cost tolerance for unauthorized review triggers?**
- Current timeout: 10 minutes per review
- If attacks are a concern, we could add rate limiting per user

**3. Should the `authorized_users` list be expanded beyond `jwbron`?**
- Current default: `jwbron` only
- Consider: Team members, trusted bots, etc.

---

*Authored-by: egg*
