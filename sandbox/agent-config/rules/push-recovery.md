# Push Filtering

Your role has an allowed-file pattern set. The gateway enforces it on every push: pushes that modify a path your role cannot write are **rejected** with HTTP 403.

## What happens on `git push` / `egg-orch push`

The gateway inspects every commit in the unpushed range, partitions files by authorship, and applies one of three outcomes:

- **All your own files are in scope.** Plain push. Nothing to do.
- **Any of your own files are out of scope.** The gateway rejects the push with HTTP 403 and a structured error body (see below). No commits land on origin; your local worktree is untouched.
- **Other restrictions** (phase gate, anchor scope, protected file, branch ownership, private mode, pipeline session). These also return 403 — different error codes, same shape.

Pulled commits authored by other roles in the same push range are **not** filtered against your patterns; only your own commits are checked.

## Response body for `restricted_path_modified`

When the gateway rejects a push because your commits modify a restricted path, the body contains:

```json
{
  "error": "restricted_path_modified",
  "role": "<your-role>",
  "blocked_paths": ["<path>", ...],
  "recommended_action": "Drop the edits to the listed paths and re-propose with --pre-merge-condition flagging a manual change for the human reviewer (see issue #1998 for the conditional-ACK pattern).",
  "doc_ref": "#1998",
  "pulled_commits": [{"sha": "...", "author_role": "..."}, ...],
  "attribution_fallback": false
}
```

## Recovering from `restricted_path_modified`

The supported recovery is the **conditional-ACK pattern** from #1998:

1. Drop or revert your edits to the listed `blocked_paths` (e.g. `git checkout origin/main -- <path>` or `git rebase -i` to drop the commit).
2. Re-propose the work as a partial change. In your `mcp__brc__propose` payload, attach a `--pre-merge-condition` describing the manual edit a human reviewer must make to the restricted file before merge. The reviewer's conditional-ACK records the obligation on the PR body.
3. Push again with the offending edit dropped.

Do **not** try to "restore" the file with a follow-up commit on the same branch — that commit will also be rejected for the same reason. The only path past the restriction is to drop the edit and document the required manual change for the reviewer.

## Unregistered commits and fail-closed

The gateway reads each commit's author role from a commit-authorship registry maintained by the orchestrator. Commits that have no registry entry are treated as your own. If the gateway cannot compute attribution at all (registry error, no walkable commit range), the rejection body has `"attribution_fallback": true`. Retry the push once; if it persists, escalate — something is wrong with the observer or the unpushed-range computation.

## Kill switch

If `EGG_AGENT_RESTRICTIONS_ENFORCE=false` is set, the gateway logs a WARNING and plain-pushes (no enforcement). This is an operator emergency switch; you should not normally see it.

## Preventing surprises

Before committing, verify your changes are in scope:

- See the **File Boundaries** section of your system prompt for your role's allowed/blocked patterns.
- `git diff --name-only` before `git commit` to review paths.
- If your task requires modifying an out-of-scope file, plan for the conditional-ACK pattern from the start: do not edit the restricted file; instead, capture the required edit in the `--pre-merge-condition` payload of your proposal, or send a HANDOFF to the role that owns the file via `egg-orch message send --to <role> --type HANDOFF`.
