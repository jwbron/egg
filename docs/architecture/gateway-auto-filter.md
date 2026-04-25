# Gateway Auto-Filter and Commit-Authorship Registry

> **Note:** [#2039](https://github.com/jwbron/egg/issues/2039) replaced the silent-strip auto-filter described below with a structured `403 restricted_path_modified` rejection. The gateway no longer rewrites pushes to remove blocked paths; it now rejects the push and points the agent at the conditional-ACK recovery pattern ([#1998](https://github.com/jwbron/egg/issues/1998)). The **commit-authorship registry** remains the source of truth for per-commit attribution and continues to back the rejection's own-vs-pulled partition. The "Push handler dispatch" and "Per-commit rewrite algorithm" sections below describe the historical #1882 design — they are preserved for context but the rewrite path and its support code (`gateway/filtered_push.py`) have been removed. The current behavior is summarized in the [gateway README "File-Level Access Restrictions"](../../gateway/README.md#file-level-access-restrictions) section.

> Originally landed in [#1882](https://github.com/jwbron/egg/issues/1882). Revived the unmerged design from [#1470](https://github.com/jwbron/egg/issues/1470) and extended it to handle mixed-role pushes.

## Problem

Role-based file restrictions ([#1494](https://github.com/jwbron/egg/issues/1494)) block a push if any file in the diff is outside the pushing role's allowed set. Before #1882, the gateway returned `403 Push denied` on any violation and relied on either agent self-recovery or the client-side workaround `egg-orch push --scope-filter` ([#1547](https://github.com/jwbron/egg/issues/1547)). Two problems:

1. **Agent tax**: every 403 cost tokens, required the agent to interpret the error correctly, and surfaced noise the orchestrator had to monitor.
2. **Mixed-role pushes were broken**: once an agent pulled / merged in commits authored by another role (cross-role handoff, rebase-on-main), the gateway's file check saw those paths and blocked the whole push. `--scope-filter` could not help — it would squash pulled history and drop legitimate work.

## Outcome

After #1882:

- The gateway auto-filters disallowed files on push. Agents no longer see `403` for agent-role file violations.
- Pulled cross-role commits pass through **bitwise-unchanged** — tree, author, committer, message, trailers preserved — while own-authored commits with blocked paths are individually rewritten with an `Auto-Filtered: true` git trailer appended to the message.
- Push responses gain `pushed_commits` (SHAs actually pushed) and `pulled_commits: [{sha, author_role}, ...]` (cross-role commits observed). Filtered / short-circuit paths add `filtered`, `excluded_files`, `pushed_files`, and `nothing_to_push` as appropriate.
- `sandbox/egg_lib/cli_push.py --scope-filter`, its `_filter_files` helper, and the `EGG_AGENT_FILE_PATTERNS` env-var injection were deleted in the same PR.
- Phase / anchor / protected-file / branch-ownership / private-mode / concurrent-mode checks keep their `403` behavior — the auto-filter is scoped narrowly to agent-role file restrictions.
- `EGG_AGENT_RESTRICTIONS_ENFORCE=false` remains as an emergency kill switch; the auto-filter short-circuits to warn-only plain push, but the success response still carries `filtered: false`, `excluded_files: []`, `pushed_files`, and `pulled_commits` so downstream tooling sees a consistent schema in both enforce and warn-only modes.

## Why a commit-authorship registry?

Before the gateway can filter without destroying pulled work, it needs to know *who authored each commit*. The session-level git identity set by `sandbox/entrypoint.py` (`egg (coder) <coder@egg.local>`) is metadata only and forgeable by a compromised sandbox. The gateway keeps a durable `{sha → role}` mapping, written **authoritatively** from the session token that created each commit.

### Observation point: gateway-inline, not sandbox hook

HITL decision-1(d) originally phrased this as a sandbox-installed `post-commit` hook. That design is **infeasible** in this codebase:

- Sandbox containers have no direct `.git` access — the gateway shadows the worktree's `.git` via tmpfs (`sandbox/entrypoint.py:728-750`), so a hook installed there would never fire.
- The gateway sets `core.hooksPath=/dev/null` globally.
- `git commit --no-verify` would bypass any hook we did manage to install.

The gateway is already the sole commit-creation path via `POST /api/v1/git/execute`. #1882 therefore observes commits **inline in `git-execute`** — the handler snapshots `git rev-parse HEAD` before and after each inner git invocation, then POSTs every new SHA to the orchestrator's `/api/v1/commit-authorship/register` endpoint with the session's role. This catches every commit-creating subcommand (`commit`, `commit --amend`, `cherry-pick`, `revert`, `merge`, `rebase`) with **no sandbox-side code path** and no hook to suppress.

The deviation from the decision-1(d) text was flagged explicitly in the plan for reviewer sign-off: the outcome (every agent commit registered with its role) is functionally identical, and the guarantees are strictly stronger.

### Registry semantics: first-wins

The registry is append-only and idempotent per SHA:

- First `register(sha, role, pipeline_id, ...)` call is authoritative.
- Re-register with the **same role** is a no-op.
- Re-register with a **different role** is rejected with a `collision` audit event; the original binding is preserved. This prevents an observer-suppression-then-rewrite attack where a malicious agent swallows the initial observer call and later re-registers the same SHA under a different role.

### Storage

The registry is a sub-store on the existing `egg/pipeline-state` orphan branch (`orchestrator/state_store.py`), sharded by pipeline ID (`.egg-state/commit-authorship/<pipeline_id>.json`) with a `_orphan.json` fallback for commits registered before a pipeline ID is known. Reuses the existing `fcntl` + `RLock` + optimistic-versioning pattern — no net-new durable store (risk-analyst R-01).

### HTTP surface

- `POST /api/v1/commit-authorship/register` — accepts `{sha, role, pipeline_id, repo, branch}`. Role is authoritative from the session's inter-pod shared-secret; body-supplied values are logged but not trusted.
- `POST /api/v1/commit-authorship/lookup` — bulk `{shas: [...]} → {sha: role | null}`, used by the push handler to partition files.

Both endpoints require the existing gateway↔orchestrator shared-secret header.

## Push handler dispatch

`gateway/gateway.py::git_push` replaces the single `check_agent_restrictions` 403 branch with a three-way dispatch driven by per-commit attribution:

1. **Attribute files**: `get_attributed_changed_files_in_push` walks the unpushed range via the existing per-commit `diff-tree` loop, captures the emitting SHA for each file, and does one bulk `lookup_bulk` against the registry to tag each `AttributedFile` with `authored_by: str | None`.
2. **Partition**: files with `authored_by == push_role` **or** `authored_by is None` (fail-closed) are treated as own-authored and subject to restrictions. Files with `authored_by == <known other role>` are pulled and exempt.
3. **Dispatch**:
   - All own-files allowed → plain push. Response adds `pulled_commits` if any commit in the range was cross-role.
   - Mixed own-allowed + own-blocked → `gateway/filtered_push.py::execute_filtered_push` (see below). Response includes `filtered: true`, `excluded_files`, `pushed_files`, `pushed_commits`, `pulled_commits`.
   - All own-files blocked → `200 nothing_to_push: true` with `excluded_files` and `pulled_commits`. No ref update, no remote push, worktree unchanged.

### Attribution-fallback short-circuit

When `get_attributed_changed_files_in_push` returns an error or an empty commit list (for example, a legacy test mocking only the old file-detection path, or a push with staged changes but no walkable commit range), the handler enters an **attribution-fallback** mode: every file is treated as own-authored-and-unregistered, and if any file is blocked the push is **unconditionally** short-circuited to `200 nothing_to_push: true`. The rewriter is **never** invoked on an empty commit list — that would push HEAD unchanged and leak blocked files to origin. The `push_all_blocked_no_op` audit event carries `attribution_fallback: true` in this path, and the success message reads `Push skipped: attribution unavailable and out-of-scope files detected (fail-closed).`

Three distinct audit events are emitted: `push_auto_filtered`, `push_all_blocked_no_op`, and `push_authorship_unregistered_fallback` (the last fires whenever any commit in the range had `authored_by=None`).

Non-agent sessions (no `g.session.agent_role`) skip attribution entirely and take today's plain-push path.

## Per-commit rewrite algorithm

`gateway/filtered_push.py::execute_filtered_push` walks the unpushed range in topological order (oldest first). For each commit:

- **Pulled** (`authored_by` is a known other role): re-parent onto the previous loop's `new_sha` and reuse the commit verbatim. If the parent chain is unchanged, no new SHA is created.
- **Own** (`authored_by == push_role` or `None`): read the original tree via `git ls-tree -r <commit>`, remove blocked paths, `git write-tree` → `new_tree`. If `new_tree` equals the parent's tree (commit becomes empty after filtering), **drop the commit** and continue with the same `new_parent`. Otherwise `git commit-tree new_tree -p new_parent` with the original message plus an `Auto-Filtered: true` git trailer, reusing the original author / date; record the returned SHA as `new_sha`.

After the walk:

1. `git update-ref refs/heads/<branch> <final_new_tip>` locally.
2. `git push <remote> <branch>`. On failure, restore HEAD via `update-ref <original_head>` and `git reset --hard`; return `500` with the push error.
3. On success: `git read-tree --reset -u <final_new_tip>` to sync index + worktree to the filtered state.
4. For each blocked file from the pre-rewrite tip, restore its blob into the worktree and index as a staged change via `git checkout-index --stage=0` with the old tree's blob — peer roles can pick them up without re-authoring.
5. Register each new own-commit SHA with the registry (`authored_by=push_role`) so a subsequent push by a different role attributes them correctly.

Any error path restores HEAD and the worktree to exactly the pre-push state — the operation is atomic from the agent's perspective.

## Fail-closed invariant

Commits with no registry entry are treated as own-authored. The agent cannot suppress the observer (it is gateway-inline and runs before the response is returned), and even if somehow an unregistered commit reached the push handler, its files flow through the pushing role's restriction check. This preserves the security guarantee that a file a role cannot write cannot be pushed under that role's identity, even under an observer-gap scenario.

The attribution-fallback short-circuit above hardens this further: when the handler cannot compute a commit walk at all, it refuses to invoke the rewriter on an empty commit list and returns `nothing_to_push: true` for every blocked file. There is no code path that reaches `git push` with a blocked-file set under agent credentials.

## Binary-safe re-staging

After a filtered push, the gateway re-stages the blocked blobs into the agent's worktree so a peer role can pick them up without re-authoring. Blobs are read through `git show` **without** Python's `text=True` decoding and written to the worktree as raw bytes (`gateway/filtered_push.py::_git_raw` + `_restage_blocked_files`). This preserves non-UTF-8 payloads (PNG, PDF, compiled artefacts) bitwise — a previous iteration used text mode and silently corrupted binary files on re-stage. After writing the file, `git add <path>` (not `git add --intent-to-add`) stages the blob content, so the next role's `git commit` captures it in full.

## What was removed

- **`sandbox/egg_lib/cli_push.py`**: the `--scope-filter` argparse flag, its filtered-push implementation, the `_filter_files` helper, and the `EGG_AGENT_FILE_PATTERNS` env-var read. The file collapses to a passthrough around `git push`.
- **`orchestrator/concurrent_executor.py`**: the `EGG_AGENT_FILE_PATTERNS` env-var injection at container-spawn time. Nothing consumes it after the cli_push cleanup.
- **Docs and agent-config rules**: every `--scope-filter` mention in `docs/guides/agent-development.md`, `docs/reference/orchestrator-cli.md`, and `sandbox/agent-config/rules/push-recovery.md` was replaced with the auto-filter story.

## Deploy ordering

Ship the orchestrator image **first** so `/api/v1/commit-authorship/*` is live before any gateway starts POSTing to it. The observer is best-effort (logs a WARNING and continues on registry-unavailable), and push-time unregistered commits fall through to fail-closed — both acceptable transiently but avoidable by ordering.

No database migration. The state store creates the `commit-authorship/` subdirectory on first write. Long-running sessions that predate the deploy continue to work; their commits fall through to fail-closed at push time, which matches today's behavior.

## Monitoring

> **Updated for #2039.** The `push_auto_filtered` and `push_all_blocked_no_op` audit events no longer fire — they were tied to the silent-strip and all-blocked short-circuit arms that the rejection model replaced. The current events are:

- `push_denied_restricted_path_modified` — fires whenever a push is rejected because the diff modifies a path the role cannot write. Inspect the event's `attribution_fallback` boolean: `false` is the normal blocked case (registry attribution was available); `true` means the handler could not compute a commit walk and fell back to the attribution-unavailable rejection — a sustained spike there is worth investigating (mocked tests leaking into production, or `git rev-list` disagreeing with the handler's view of the unpushed range).
- `push_authorship_unregistered_fallback` — a steady trickle is normal (long-running sessions that predate deploy). A **sustained spike** after the deploy suggests the git-execute observer is missing some commit-creating subcommand; investigate which subcommand is being missed.

## Deployment prerequisites

The gateway's commit-authorship client calls the orchestrator's `/api/v1/commit-authorship/{register,lookup}` routes, which live behind `require_lifecycle_secret`. The gateway pod therefore needs `EGG_LIFECYCLE_SECRET` injected the same way `orchestrator-deployment.yaml` does — mounted from `gateway-secrets.lifecycle-secret`. Without the secret, every register and lookup 401s and the whole feature degrades to fail-closed own-authored on every push. This env var is set in `k8s/base/gateway-deployment.yaml`; local dev picks it up from `.env`.

## Related documents

- [Git Isolation Architecture](git-isolation.md) — parent document covering the gateway's policy-enforcement model.
- [Agent Development Guide: Push Filtering](../guides/agent-development.md#push-filtering-and-cross-role-pushes) — operational view for agent authors.
- [`sandbox/agent-config/rules/push-recovery.md`](../../sandbox/agent-config/rules/push-recovery.md) — runtime rule surfaced to sandboxed agents.
