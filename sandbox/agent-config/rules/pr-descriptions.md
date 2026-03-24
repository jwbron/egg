# PR Description Format

```
<one-line summary - 50 chars max, imperative verb>

<2-3 paragraphs: Context → Changes → Impact>

Issue: <JIRA link or "none">

## Test Plan
- Automated: <which tests cover the changes>
- Manual: <specific steps for reviewers to verify>

## Manual Steps (if any)
- Pre-merge: <migrations, config changes, etc.>
- Post-merge: <deployments, cache invalidation, etc.>
```

**Test plan is mandatory.** Every PR must describe how the changes are tested. Highlight any manual testing that cannot be automated. If there are pre/post-merge steps (migrations, config changes, deployments), list them explicitly under Manual Steps.

**Autonomous mode only**: When `EGG_PIPELINE_ID` is set, append `Authored-by: egg` to the PR body. In interactive/user mode, omit the signature.

**Under 500 words total.** Focus on WHAT and WHY, not implementation details.

**NEVER include**: "Claude Code", claude.ai links, "Co-Authored-By: Claude"

**Breaking changes**: Bold warning at top with migration path.
