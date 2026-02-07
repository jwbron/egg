# Review Conventions

Guidelines for how to communicate review findings.

## Posting Reviews

Use `gh pr review <PR_NUMBER>` to post your review:

```bash
# Approve with comments
gh pr review 123 --approve --body "Looks good! Minor suggestions below."

# Request changes
gh pr review 123 --request-changes --body "Blocking issues need to be addressed."

# Comment only (advisory)
gh pr review 123 --comment --body "Some suggestions for consideration."
```

For inline comments on specific lines, use the `--body-file` flag with a file
containing your review, or post individual comments via the API.

## When to Approve vs Request Changes

- **Approve**: No blocking issues. Minor suggestions are fine to include.
- **Request changes**: Security vulnerabilities, logic errors, breaking changes.
- **Comment**: Advisory feedback, questions, suggestions for future work.

## Comment Quality

- Fewer, higher-signal comments. A noisy reviewer gets ignored.
- Be specific: reference the exact line and explain why it's a problem.
- Suggest a fix when possible.
- Summarize related issues rather than commenting on every instance.

## Signature

End your review with: — Authored by egg
