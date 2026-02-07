# Review Conventions

Guidelines for how to communicate review findings.

## Core Principle

**Be thorough.** This is critical infrastructure—your review catches issues before they reach production. Report every issue you find. A false negative (missing a bug) is far more costly than comprehensive feedback.

## Posting Reviews

Use `gh pr review <PR_NUMBER>` to post your review:

```bash
# Request changes (use for any blocking issues)
gh pr review 123 --request-changes --body "Issues identified that need to be addressed."

# Approve (only when no blocking issues remain)
gh pr review 123 --approve --body "LGTM. No blocking issues found."

# Comment only (advisory, non-blocking feedback)
gh pr review 123 --comment --body "Advisory suggestions for consideration."
```

For inline comments on specific lines, use the `--body-file` flag with a file
containing your review, or post individual comments via the API.

## When to Approve vs Request Changes

- **Request changes**: Security vulnerabilities, logic errors, correctness issues, missing error handling, resource leaks, breaking changes, violations of codebase patterns. When in doubt, request changes.
- **Approve**: No blocking issues found after thorough review. Minor advisory suggestions are fine to include.
- **Comment**: Non-blocking suggestions, questions, ideas for future improvement.

## Comment Quality

- **Be comprehensive**: Report all issues found, not just the first few. Categorize and structure if there are many.
- **Be specific**: Reference the exact file and line. Explain what the problem is and why it matters.
- **Be direct**: State issues clearly. Do not soften critical feedback with pleasantries.
- **Suggest fixes**: When possible, show what the correct code should look like.
- **Provide context**: Explain the reasoning—link to documentation, security guidelines, or examples in the codebase.

## Signature

End your review with: — Authored by egg
