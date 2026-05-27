# Review Conventions

Guidelines for how to communicate review findings.

## Core Principle

**Be thorough.** This is critical infrastructure—your review catches issues before they reach production. Report every issue you find. A false negative (missing a bug) is far more costly than comprehensive feedback.

## Posting Reviews

**Always use `--body-file`** to post reviews. Writing the body to a file first
avoids shell quoting and heredoc parsing failures with long multi-line content.

```bash
# 1. Write your review to a temp file
cat > /tmp/review-body.md << 'REVIEW_EOF'
Your review content here...

— Authored by egg
REVIEW_EOF

# 2. Post using --body-file
gh pr review 123 --request-changes --body-file /tmp/review-body.md
gh pr review 123 --approve --body-file /tmp/review-body.md
gh pr review 123 --comment --body-file /tmp/review-body.md
```

**Do NOT use `--body` with inline content** — long reviews will fail due to
shell escaping issues. Always write to a file first, then use `--body-file`.

## When to Approve vs Request Changes

- **Request changes**: Security vulnerabilities, logic errors, correctness issues, non-functional features (core purpose doesn't work end-to-end), missing error handling, resource leaks, breaking changes, violations of codebase patterns. When in doubt, request changes.
- **Approve**: No blocking issues found after thorough review. Minor advisory suggestions are fine to include. If you include non-blocking suggestions, add `<!-- has-suggestions -->` anywhere in the review body so the feedback-addressing workflow picks them up. The marker is position-independent—it can appear at the beginning, middle, or end of the review body.
- **Comment**: Non-blocking suggestions, questions, ideas for future improvement.

**Key distinction**: A feature that doesn't work is a correctness issue, not a style issue. If the feature's core functionality is broken — not just degraded or missing edge cases — always request changes, even if the code structure looks reasonable or matches an existing pattern.

**Pre-existing issues are still blocking**: If a PR modifies code that already has broken or inconsistent behavior, request changes to fix it — do not dismiss it as "not a regression." The PR is already in the area, making it the natural place to fix the issue. A PR that adds new code paths through already-broken logic makes the problem worse.

## Self-Authored PRs

When reviewing a PR authored by the same bot account, **use `--comment` instead of
`--request-changes` or `--approve`**. GitHub does not allow bots to request changes
on their own PRs, and approval has no effect.

The `gh` wrapper will auto-downgrade both `--request-changes` and `--approve` to
`--comment` for self-authored PRs, but you should proactively use `--comment` to
avoid the extra API call and log noise.

## Comment Quality

- **Be comprehensive**: Report all issues found, not just the first few. Categorize and structure if there are many.
- **Be specific**: Reference the exact file and line. Explain what the problem is and why it matters.
- **Be direct**: State issues clearly. Do not soften critical feedback with pleasantries.
- **Suggest fixes**: When possible, show what the correct code should look like.
- **Provide context**: Explain the reasoning—link to documentation, security guidelines, or examples in the codebase.

## Do NOT run the test suite

**Never run `make test`** as part of your review. The egg test suite takes
10-15 minutes and is causing this workflow to time out. CI runs the configured
check suite on every PR HEAD — trust those results (the `wait-for-checks` step
in the workflow gates this review on a green status), and NACK if you have
concerns about coverage or behavior rather than re-running tests yourself.

If you need to validate a specific concern about a single function, you may run
individual targeted tests (`.venv/bin/pytest path/to/test_x.py::TestY::test_z`),
but never the full `make test` suite. This restriction is tracked for removal once
#2817 lands and `make test`'s changeset narrowing becomes tight enough.

## Signature

End your review with: — Authored by egg
