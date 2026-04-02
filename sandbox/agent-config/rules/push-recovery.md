# Push Recovery

When your `git push` is rejected by the gateway due to agent-role file restrictions, follow these steps to recover.

## Automatic Recovery (Recommended)

Use the scope-filter command to automatically strip out-of-scope files and push only allowed ones:

```bash
egg-orch push --scope-filter
```

This command:
1. Reads your role's file patterns from `EGG_AGENT_FILE_PATTERNS`
2. Identifies all files across unpushed commits (using merge-base, not just HEAD~1)
3. Removes files that don't match your allowed patterns
4. Squashes the filtered commits and pushes

If all files are out of scope, the command exits with an error — you have no in-scope changes to push.

## Manual Recovery

If automatic recovery is not available:

1. **Save your current HEAD**: `git log --oneline -1` (note the commit hash)
2. **Soft-reset to before your commits**: `git reset --soft $(git merge-base origin/<branch> HEAD)`
3. **Unstage everything**: `git reset HEAD -- .`
4. **Re-add only allowed files**: `git add <allowed-files>`
5. **Recommit**: `git commit -m "Your commit message"`
6. **Push**: `git push origin <branch>`

## Understanding the Error

The gateway error response includes:
- `blocked_files`: Files you tried to modify that are outside your role's scope
- `allowed_patterns`: Glob patterns your role is allowed to write to
- `remediation`: A summary of recovery steps

## Preventing Future Failures

Before committing, verify your changes are in scope:
- Check `EGG_AGENT_FILE_PATTERNS` for your role's allowed/blocked patterns
- Use `git diff --name-only` to review which files you're about to commit
- If you need to modify out-of-scope files, coordinate with the appropriate agent role via the message bus
