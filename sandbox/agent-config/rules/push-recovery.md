# Push Filtering

Your role has an allowed-file pattern set. The gateway enforces it on every push and auto-filters out-of-scope files for you — you do **not** need to run a special recovery command.

## What happens on `git push` / `egg-orch push`

The gateway inspects every commit in the unpushed range, partitions files by authorship (see below), and applies one of four outcomes:

- **All your own files are in scope.** Plain push. Nothing to do.
- **Your files mix allowed and blocked.** The gateway rewrites the unpushed range using `git commit-tree` / `git update-ref`:
  - Blocked files are dropped from your commits; the commit message gets an `[auto-filtered]` suffix.
  - Commits that become empty after filtering are dropped entirely.
  - Commits authored by other roles (pulled via merge/rebase) pass through **bitwise-unchanged**.
  - After the push, your local `HEAD` is fast-forwarded to match origin, and the blocked files are **re-staged as uncommitted changes** in your worktree so another role can pick them up.
- **All your own files are blocked.** Response returns `200 nothing_to_push: true` with the excluded file list. No ref update, no remote push. Your worktree is untouched. Re-author the work under the correct role (or coordinate via the message bus).
- **Other restrictions** (phase gate, anchor scope, protected file, branch ownership, private mode, concurrent mode). These still return `403`. The auto-filter is only for agent-role file restrictions.

## Response body fields

Every `200` from `/api/v1/git/push` now includes:

- `pushed_commits` — SHAs actually pushed to the remote (post-rewrite, if any).
- `pulled_commits` — `[{sha, author_role}, ...]` listing any commits in the range attributed to a role other than yours. Empty list when the push was own-only.

When the gateway rewrote or short-circuited the push, the response also includes:

- `filtered: true`
- `excluded_files` — the files it removed.
- `pushed_files` — the files that actually made it through.
- `nothing_to_push: true` — set when all your own files were blocked.

## Reading the result in an agent loop

Typical pattern after a push:

1. Check `response.filtered`. If absent or `false`, nothing special happened.
2. If `nothing_to_push: true`, your commits were all out of scope. Inspect `excluded_files`, decide whether to hand the work off to another role via the message bus, or drop/relocate the files before re-attempting.
3. If `filtered: true` without `nothing_to_push`, your push succeeded with some files excluded. The blocked files are now staged (uncommitted) in your worktree — if you need them gone, `git restore --staged <file>` and `git checkout -- <file>`. If a peer role should commit them, leave them in place and send a HANDOFF via `egg-orch message send`.
4. `pulled_commits` is informational. It tells you which SHAs in the push were authored by other roles and passed through unchanged.

## Unregistered commits and fail-closed

The gateway reads each commit's author role from a commit-authorship registry maintained by the orchestrator. Commits that have no registry entry are treated as your own — the registry cannot be suppressed to bypass restrictions. If your push surfaces `push_authorship_unregistered_fallback` in the audit trail, it means at least one commit in the range has no registry record and was checked against your role's patterns.

## Kill switch

If `EGG_AGENT_RESTRICTIONS_ENFORCE=false` is set, the gateway logs a WARNING and plain-pushes (no rewrite, no filtering, no new response fields). This is an operator emergency switch, not something you should normally see.

## Preventing surprises

Before committing, verify your changes are in scope:

- See the **File Boundaries** section of your system prompt for your role's allowed/blocked patterns. The orchestrator injects them into the prompt; the former `EGG_AGENT_FILE_PATTERNS` env var was removed in [#1882](https://github.com/jwbron/egg/issues/1882).
- `git diff --name-only` before `git commit` to review paths.
- If you need to modify out-of-scope files, send a HANDOFF to the correct role via `egg-orch message send --to <role> --type HANDOFF`.
