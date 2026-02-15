# Git Workflow & PR Lifecycle
# Audience: Coder, Refiner, Integrator — agents that commit and manage PRs

## Git Workflow

**Branch naming**: `egg/<description>` format (e.g., `egg/fix-auth-bug`). The gateway only allows pushing to `egg/` or `egg-` prefixed branches.

**Worktrees**: You are in an isolated worktree on a temp branch. Commit directly, then PR. **DO NOT use `git worktree add/remove`** — the gateway manages worktrees. Use `git checkout -b <name> origin/<branch>` instead. `git checkout main` FAILS; always branch from `origin/main`.

**Commit attribution**: Author is `egg <egg@localhost>`. NEVER include "Co-Authored-By: Claude".

**If push/PR fails**: Notify user via Slack with branch name, repo, and summary.

### Preventing PR Cross-Contamination (CRITICAL)

**NEVER mix commits from different tasks.** Before ANY commit:
```bash
git branch --show-current && git log --oneline -3
```

**Wrong branch fix**: `git log --oneline -1` (save hash), create correct branch, `git cherry-pick <hash>`

### PR Lifecycle

**Before updating a PR**: `gh pr view` to check status. If merged/closed, create NEW PR.

**PR approval**: GitHub review status or "LGTM". Other positive comments are feedback, not approval.

### Responding to PR Reviews

Reply to each comment using `gh pr review <PR> --comment --body-file /tmp/review-response.md`. Do NOT use `--body` with inline content — use `--body-file` to avoid shell escaping failures.

**Response format**: `**Agreed.** [what changed]` | `**Disagree.** [reasoning]`

Sign all GitHub comments with: `— Authored by egg`
