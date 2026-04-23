# Analysis: Gateway should auto-filter disallowed files on push, and handle pulled cross-role commits

> Issue: #1882 | Phase: refine

## Problem Statement

Today the gateway rejects a push with HTTP 403 when any changed file lies outside the pushing role's allowed patterns (`gateway/gateway.py:972-1024`). Agents must recover via the opt-in, client-side `egg-orch push --scope-filter` command (`sandbox/egg_lib/cli_push.py`), which:

1. Costs the agent extra tokens to interpret the 403, choose a recovery path, and run the fallback.
2. Assumes the whole diff was authored by the pushing role. If the agent fetched or merged in commits from *another* role (e.g. a conflict-resolver rebase or a cross-role handoff), `--scope-filter` would rewrite legitimate upstream history and drop work — so it cannot help here and can actually make things worse.
3. Fails silently in "worst-case" scenarios where pulled commits include out-of-scope files that the current role has no business editing.

The issue asks us to revive the unmerged **gateway-side** auto-filter from commit `6f0877f50` (branch `egg/issue-1470`, "Auto-filter disallowed files on gateway push instead of blocking"), and extend it so the gateway can distinguish **own commits** (which must obey the pushing role's restrictions) from **pulled commits** (which are already upstream and must pass through untouched regardless of scope).

Concretely the feature is two changes stitched together:

1. **Revive the gateway-side auto-filter.** Port `6f0877f50` forward. When a push's diff contains any file outside the role's scope, the gateway should (a) rewrite the top commit(s) to exclude blocked files, (b) push successfully with a 200 response that includes an `excluded_files` field, and (c) leave the filtered-out work as uncommitted changes in the agent's worktree so the next role can pick them up. The current 403 path becomes a fallback (or disappears entirely).

2. **Distinguish own-commits from pulled-commits.** Today `get_changed_files_in_push()` (`gateway/git_client.py:1301`) does per-commit `diff-tree` across `rev-list origin/branch..HEAD`, but is author-agnostic — every commit in that range contributes to the union `changed_files` list fed into `check_agent_restrictions()`. Add an author filter that keys on `commit.author` (or, optionally, a signature) so commits whose author does not match the pushing role are treated as "pulled" and exempt from that role's file restrictions.

Both parts must land together: without part 2, the part-1 auto-filter would happily rewrite pulled commits (rewriting history we had no business touching); without part 1, the per-role exemption still leaves legitimate mixed pushes failing with 403.

## Current Behavior

### 1. How the gateway currently enforces role-based file restrictions

`@app.route("/api/v1/git/push")` in `gateway/gateway.py` runs two back-to-back file checks against the files listed by `get_changed_files_in_push()`:

1. `check_file_restrictions(role, changed_files)` — phase-level blocklist (e.g. all roles are blocked from writing `.egg-state/contracts/`). On violation returns **403** (`gateway.py:942-965`).
2. `check_agent_restrictions(role, changed_files)` — role-scoped file patterns from `shared/egg_restrictions/patterns.py::AGENT_PATTERNS`. On violation returns **403** with a remediation hint that points the agent at `egg-orch push --scope-filter` (`gateway.py:972-1024`).

Both checks operate on the full set of files changed across *all* commits in the push, with no awareness of who authored which commit.

### 2. How `get_changed_files_in_push` computes the diff

`gateway/git_client.py:1301-1500` uses per-commit `diff-tree` (not a tree-level diff) to scope the check to commits actually being pushed. The rationale (see `#1535`, `#1539`) is the opposite direction of this issue: avoid *false positives* where the tree diff would report files from *previous* pushes by other agents on the same branch. The current code iterates `rev-list origin/<branch>..HEAD`, then `git diff-tree --no-commit-id --name-only -r <sha>` for each commit, and unions the file list. **Commit author is read nowhere in this path.**

### 3. How the client-side `--scope-filter` fallback works

`sandbox/egg_lib/cli_push.py` reads `EGG_AGENT_FILE_PATTERNS` (a JSON env var populated by `orchestrator/concurrent_executor.py:267-282`), filters files by allowed/blocked/block_exempt, soft-resets to `merge-base`, re-stages only allowed files, recommits with the original message (via `-C ORIG_HEAD`), and pushes. This only works when *every* unpushed commit on the branch was authored by the current role — otherwise the soft-reset rewrites upstream history.

### 4. The unmerged `#1470` implementation (commit `6f0877f50`)

That commit added three pieces:

1. `filter_allowed_files(role, files)` in `gateway/agent_restrictions.py` — partitions files into (allowed, blocked) using the existing `AgentFilePattern.can_write()` logic, falling back to "allow everything" for unknown roles.
2. `filter_agent_files(role, files)` in `gateway/phase_filter.py` — thin wrapper around the above so the gateway could import from a stable location.
3. `_execute_filtered_push()` in `gateway/gateway.py` — saves `HEAD`, soft-resets to `old_ref_sha` (or `merge-base HEAD origin/main` on new-branch pushes), unstages blocked files, recommits with original message + `[auto-filtered]` suffix, runs the push, and **restores `HEAD` via `git reset --hard`** so blocked files remain as uncommitted changes in the worktree. On any failure the original HEAD is also restored.

The top-level push handler in that commit replaced the 403 branch with: if `enforce` and any file is blocked and at least one file is allowed, call `_execute_filtered_push()` and return 200 with `filtered=true` + `excluded_files`; if zero files are allowed, return 200 with `nothing_to_push=true` and `excluded_files` listing everything. The "warn-only" path (`EGG_AGENT_RESTRICTIONS_ENFORCE=false`) was untouched.

None of these three pieces exist on `main` today (grep of `shared/egg_restrictions/` and `gateway/` confirms). The branch `egg/issue-1470` was never merged; #1547 shipped the client-side `--scope-filter` workaround instead, and #1470 was closed.

### 5. Git identity in agent containers

`sandbox/entrypoint.py::setup_git` (lines 593-606) sets `git config --global user.email "<role>@egg.local"` and `user.name "egg (<role>)"` based on `EGG_AGENT_ROLE`. This is env-driven metadata only — not cryptographic — but it is consistent and written at container entrypoint before any agent code runs. The gateway already trusts this for audit logs (`audit_log("push_denied_agent_role_restriction", ..., details.role = session_role)`).

## Constraints

- **Security: fail closed.** `get_changed_files_in_push` already fails closed when `diff-tree` errors (`git_client.py:1402-1415`) — this invariant must survive any refactor. If author detection fails for a commit, we must NOT default to "treat as pulled and skip checks"; that would give any commit that corrupts its author trailer a free pass. Default should be "treat as own commit and enforce restrictions."
- **No history rewrite of pulled commits.** The auto-filter's soft-reset rewrites the tip commit. If pulled commits are present, the filter path must (a) either only rewrite commits authored by the pushing role while leaving pulled commits intact, or (b) detect the pulled-commit case and bail out with a clear error. Rewriting another role's commit would silently drop its work.
- **Backwards-compatible for single-role pushes.** The dominant case today is a push with 0 pulled commits; the `#1470` commit's behavior should apply unchanged to that case. We can't regress the common path while fixing the edge case.
- **No new infra for a lightweight win.** The issue body flags signed-commit verification (GPG/SSH per-role keys) as "more infra, but tamper-resistant." The sandbox is the only place agents can author commits, and the gateway is already the trust boundary between the sandbox and GitHub. If the author-metadata path is enough to close the bug, we should not pay for key management unless a reviewer identifies a concrete attack that justifies it.
- **Audit trail preservation.** Any auto-filtered push must land in the audit log with enough detail (pushed role, author-attributed commits, excluded files, pulled-commit authors) that an operator can reconstruct what happened from logs alone.
- **Atomic rewrite.** The `_execute_filtered_push` pattern (soft-reset → unstage → recommit → push → restore) must be atomic from the agent's perspective: either the push succeeds and worktree state is exactly as it was before the push (minus the committed-then-pushed files), or the push fails and worktree state is exactly as it was before. Partial rewrite is not acceptable because the agent has no way to undo mid-state.

## Options Considered

### Axis A — Filtering strategy (what the gateway does with blocked files)

#### Option A1: Block-only (status quo — do nothing)

Reject the push with 403 and rely on the agent to re-run with `--scope-filter`.

**Pros**: zero new code; agents already handle this in practice.
**Cons**: this is the bug. `--scope-filter` can't fix pulled-commit pushes; it costs tokens every time; it relies on the agent's judgment under stress. Fails the explicit ask of the issue. Rejected.

#### Option A2: Port `6f0877f50` forward, scoped to own-commits only

Bring back `filter_allowed_files`, `filter_agent_files`, and `_execute_filtered_push`. Enable the gateway auto-filter by default. The soft-reset rewrite path only engages when all unpushed commits were authored by the pushing role (author-based filter from Axis B). For mixed-author pushes, treat pulled commits as exempt when checking restrictions, then do a plain `git push` (no rewrite) for commits that pass the check.

**Pros**: matches the issue's requested direction exactly. Backward-compatible for today's single-role pushes. Lightweight — no key management. Makes the 403 path truly a fallback (or removable after a release).
**Cons**: The filter path currently rewrites the *tip* commit and unstages blocked files into the worktree. If own-authored files and blocked own-authored files are spread across multiple commits, the commit structure collapses into a single squashed commit with "[auto-filtered]" suffix. Reviewers need to be OK with this squash semantics (decision-2 below).

#### Option A3: Port `6f0877f50`, but leave blocked files as a separate commit on the branch authored as a different role

Similar to A2, but instead of leaving blocked files as uncommitted changes in the worktree, commit them with the "next expected role's" identity (e.g. if coder pushes a test file, commit it as the tester and push *that* too).

**Pros**: next role doesn't have to re-discover the uncommitted files; blocked work lands immediately.
**Cons**: the gateway would forge another agent's identity on a commit, which violates the authorship signal we're relying on for Axis B. Creates a chicken-and-egg with author-based filtering (a forged-author commit would be exempt from the forger's restrictions on its next push). Rejected.

#### Option A4: Reject-with-auto-fix (explicit two-step protocol)

Reject with 403 but the gateway itself returns the filtered-commit SHA that the agent can cherry-pick, plus a hint. Agent still has to push a second time.

**Pros**: no history rewrite on the gateway side; the agent keeps authorship.
**Cons**: still costs the agent tokens; doesn't help the pulled-commit case at all. Strictly worse than A2 for the stated bug.

**Recommended for Axis A: Option A2.**

### Axis B — How to identify pulled-commit authorship

#### Option B1: Author-email match against the role identity (lightweight)

In `get_changed_files_in_push()` (or a new sibling function), for each commit on `rev-list`, also read `git log -1 --format=%ae <sha>`. Compare against the expected author email for the pushing role (`<role>@egg.local`, as set in `sandbox/entrypoint.py:601`). If the author email matches the pushing role, apply file restrictions to that commit's diff; if it matches *another known role* (`tester@egg.local`, `documenter@egg.local`, …), skip restriction check for that commit's files; if it matches neither, treat as own-commit (fail-closed default).

**Pros**: zero new infrastructure. The sandbox already sets these identities. The gateway already trusts them in audit logs. Works today for any roster configured via the `egg_restrictions` role table. Straight port of the "lightweight" option called out in the issue.
**Cons**: agents can theoretically forge another role's identity by running `git config user.email=<other-role>@egg.local` before committing. This is "agent authorship can't be fully trusted," but the sandbox is already the trust boundary — an agent that can override `user.email` can do far worse than bypass this restriction. Not a new attack surface.

#### Option B2: Per-role signing keys (GPG or SSH)

Provision per-role signing keys mounted into each agent container. Require `git commit -S`. In the gateway, `git log --format=%G? <sha>` reports signature verification status; verify and extract the key identity.

**Pros**: tamper-resistant. The gateway doesn't have to trust the sandbox for authorship.
**Cons**: Significant new infrastructure (key generation, distribution, rotation, revocation on container breakout). Adds latency to every commit. Doesn't solve a bug that actually exists — there is no open bug where agents are forging identities on commits. Over-engineering for this feature.

#### Option B3: Gateway-side commit attribution via session-token + commit-SHA registry

Each time an agent commits, the post-agent-commit hook (`gateway/post_agent_commit.py`) records "SHA X was authored in session S, which was for role R" into a gateway-side registry. At push time the gateway consults the registry rather than the commit author trailer.

**Pros**: no reliance on sandbox-set git identity; registry is the authoritative source.
**Cons**: requires a durable store (currently none for this). Commits created outside the post-commit hook path (e.g. via `git cherry-pick` or any commit made before the hook fires) would be unregistered. Corner cases abound.

**Recommended for Axis B: Option B1.**

### Axis C — Rollout / enablement

#### Option C1: Enable auto-filter + author-exemption by default in the same release

Port `6f0877f50`, wire up B1 into `check_agent_restrictions`, flip the default behavior. Keep `EGG_AGENT_RESTRICTIONS_ENFORCE=false` as a kill switch.

**Pros**: fixes the bug. One PR, one release.
**Cons**: behavior change for every agent in a single cut.

#### Option C2: Ship auto-filter off, author-exemption on; flip default a release later

Ship the code but keep the gateway in "warn-only + author-exempt" mode for one release, then flip to enforce-with-auto-filter.

**Pros**: conservative rollout.
**Cons**: the "warn-only" mode is not the default today; flipping it off, then back on in a different configuration, is a more confusing migration than just changing behavior once. Warn-only mode also means pushes continue to fail for mixed-role pushes in the intermediate release (nothing actually rewrites), so we haven't fixed the bug until flip-day anyway.

#### Option C3: Ship author-exemption standalone, leave auto-filter for a follow-up

Only land Axis B. Mixed pushes whose own-authored commits pass restrictions now succeed. Own-authored pushes that mix allowed and blocked files still 403.

**Pros**: smallest, safest change. Addresses the *worst* half of the bug (pulled-commit false positives) immediately.
**Cons**: leaves `--scope-filter` as the primary path for own-only pushes that mix scopes. The #1470 work still doesn't land. Issue remains partially open.

**Recommended for Axis C: Option C1.** (See decision-6 to confirm.)

### Axis D — Semantics of the rewritten commit

When the gateway rewrites the tip commit to drop blocked files, what does the resulting commit message / author / timestamp look like?

#### Option D1: Original message + `[auto-filtered]` suffix (matches `6f0877f50`)

Preserve original author, committer becomes gateway identity, timestamp updated. Message is `<original> [auto-filtered]`.

#### Option D2: Original message verbatim, committer = gateway

Same as D1 minus the suffix.

#### Option D3: Original message verbatim, author AND committer = gateway

Gateway takes full authorship.

**Recommended for Axis D: Option D1** (the suffix is a useful debugging breadcrumb when inspecting branch history). Registered as decision-4 below.

## Recommended Approach

**Axis A: A2 — port `6f0877f50` forward, scoped by author.**
**Axis B: B1 — author-email match against `<role>@egg.local` identity.**
**Axis C: C1 — single-release cutover.**
**Axis D: D1 — append `[auto-filtered]` to commit message.**

Net shape of the change:

1. **Restore the `#1470` building blocks.** Bring back (or reintroduce in `shared/egg_restrictions/`) the equivalents of `filter_allowed_files()` and `filter_agent_files()`. Prefer placing `partition_files_by_role(role, files) -> (allowed, blocked)` in `shared/egg_restrictions/checker.py` alongside `check_agent_file_access` so both the gateway and the client-side `cli_push.py` can reuse it; this also removes duplicated glob-matching logic between `cli_push.py::_filter_files` and the new gateway partition.
2. **Add author attribution to `get_changed_files_in_push`.** Change its return type from `tuple[list[str], str | None]` to `tuple[list[AttributedFile], str | None]` (or add a sibling function) where each entry carries `(file, authored_by_pushing_role: bool)`. Internally: for each commit in `rev-list`, run `git log -1 --format=%ae <sha>`, compare against the expected `<role>@egg.local` email, and tag every file from that commit accordingly. On `git log` error for any commit, fail closed (treat as own commit).
3. **Skip restrictions for not-own-authored files.** In `check_agent_restrictions` (or its caller in `gateway.py`), filter `changed_files` to only "authored by pushing role" before the restriction check. Pulled commits stop triggering the check.
4. **Add `_execute_filtered_push`.** Port the commit forward, rebased on current `gateway.py`. Behavior is identical to `6f0877f50`: soft-reset to remote tip / merge-base, unstage blocked files, recommit with suffix, push, restore original HEAD via `reset --hard`. The only new wrinkle: blocked files that are already in *pulled* commits must never hit the unstage step (those commits are not the tip commit we're rewriting). Axis B's author filter handles this implicitly because such files are already marked "not own-authored" and thus never end up in the blocked list.
5. **Replace the 403 branch in `gateway.py`.** When `check_agent_restrictions` now returns "blocked" (after the own-authored filter), partition own-authored files into allowed/blocked, call `_execute_filtered_push` for the mixed case, return `nothing_to_push` for the all-blocked case. Keep `EGG_AGENT_RESTRICTIONS_ENFORCE=false` as a warn-only kill switch.
6. **Response schema.** Successful auto-filtered push returns 200 with `{ "filtered": true, "excluded_files": [...], "pushed_files": [...], "pulled_commits": [{"sha": ..., "author": ...}] }`. The `pulled_commits` field is new-in-this-PR so agents and audit tooling can see what was passed through untouched (decision-5).
7. **Demote `--scope-filter` in docs.** Keep the command (it's still useful for local workflows outside a gateway session), but update `docs/guides/agent-development.md` and `docs/reference/orchestrator-cli.md` to describe it as a fallback, not the primary recovery path. Update `sandbox/agent-config/rules/push-recovery.md` (if present) to reflect that the gateway now handles the common case.
8. **Tests.** Minimum coverage:
   - Own-authored push, all allowed → plain push succeeds, no rewrite (existing behavior).
   - Own-authored push, all blocked → 200 with `nothing_to_push=true` (new).
   - Own-authored push, mixed → 200 with auto-filter rewrite; worktree contains unstaged blocked files after push (new).
   - Mixed-author push, own-authored portion clean, pulled portion contains out-of-scope files → plain push succeeds, pulled commits pass through untouched (the bug this issue reports).
   - Mixed-author push, own-authored portion has blocked files → auto-filter rewrites only the own-authored tip commits, pulled commits pass through unchanged.
   - Author email resolution failure → fail closed (restrictions apply as if own-authored), don't break the push just because `git log` hiccupped.
   - Warn-only mode (`EGG_AGENT_RESTRICTIONS_ENFORCE=false`) → no rewrite, warn log line with full detail (existing behavior).
   - New response fields (`filtered`, `excluded_files`, `pushed_files`, `pulled_commits`) present on all filter-path responses.

## Open Questions

The following decisions / feedback items are being registered via `egg-contract` so the human can steer the plan phase before we commit to an implementation.

### Multiple-choice decisions

1. **Which commit-authorship mechanism should the gateway trust?** (decision-1) — Options: `Author email from git log (lightweight, no new infra)` / `Per-role GPG/SSH signing keys (tamper-resistant, new infra)` / `Gateway-side commit-SHA registry (no infra but stateful)`. Recommended: author email.
2. **How should `_execute_filtered_push` handle multi-commit own-authored pushes?** (decision-2) — Options: `Squash all own-authored commits into one with [auto-filtered] suffix (matches #1470)` / `Rewrite each commit individually, preserving commit structure (more complex, preserves author's commit boundaries)` / `Reject multi-commit pushes when any file is blocked (force the agent to squash first)`.
3. **What should happen when ALL files in the push are blocked for the pushing role?** (decision-3) — Options: `Return 200 with nothing_to_push=true, leave uncommitted changes in worktree (matches #1470)` / `Return 403 as before — there is nothing legitimate to push` / `Return 200 with nothing_to_push=true but ALSO preserve the original commit in worktree head (no rewrite)`.
4. **Should the auto-filtered commit message include a `[auto-filtered]` suffix?** (decision-4) — Options: `Yes — debugging breadcrumb, matches #1470` / `No — preserve original message verbatim, surface filter in response metadata only` / `Configurable via env var, default yes`.
5. **Should the push response include a `pulled_commits` field?** (decision-5) — Options: `Yes — list of {sha, author} pairs for each non-own-authored commit in the push (transparency for agents + audit tooling)` / `No — keep response minimal, log pulled commits to audit log only` / `Only on filtered pushes (i.e., when pulled_commits intersect with auto-filter decision)`.
6. **What is the rollout order for auto-filter + author-exemption?** (decision-6) — Options: `Enable both by default in the same release (recommended)` / `Ship in warn-only mode first, flip to enforce next release` / `Ship author-exemption standalone, leave auto-filter for a follow-up`.
7. **Where should the new `partition_files_by_role` / filter helper live?** (decision-7) — Options: `shared/egg_restrictions/checker.py (cross-component, reused by both gateway and cli_push)` / `gateway/agent_restrictions.py (gateway-local, matches #1470 original location)` / `New module gateway/push_filter.py (separation-of-concerns)`.
8. **Should `sandbox/egg_lib/cli_push.py --scope-filter` be deprecated in the same PR?** (decision-8) — Options: `Keep as-is (still useful outside gateway sessions)` / `Mark deprecated in --help, remove next release` / `Remove in the same PR (gateway now handles all cases)`.
9. **Unknown-role default when author email matches no known role?** (decision-9) — Options: `Treat as own-commit, enforce restrictions (fail closed)` / `Treat as pulled commit, skip restrictions (fail open)` / `Block the push with a 403 (strict, suggests identity tampering)`.

### Open-ended feedback

1. **What attack model should drive the choice between author-email vs signed commits?** — Is there a documented threat where an agent forges another role's identity at commit time, or is the trust model "the sandbox is the boundary" and we should match that for push authorship too?
2. **Should the gateway allow pulled commits authored by the "conflict-resolver" role through unrestricted?** — conflict-resolver is a utility role that may legitimately touch files outside the pushing role's scope; the issue body hints at this via "pulled/merged in during sync" but doesn't enumerate which roles.
3. **Is it acceptable for the auto-filter path to rewrite a signed commit (GPG/SSH)?** — Today no role signs commits, but if that changes (Option B2 in future), `git reset --soft` + `git commit` would drop signatures. Policy call.
4. **How should we handle the `post_agent_commit` hook in the auto-filter path?** — The hook runs on push; if the gateway rewrites the commit before pushing, the hook sees the rewritten commit. Is that the desired semantics, or should the hook run against the original pre-filter tip?
5. **Should the audit log distinguish "auto-filtered" vs "filtered due to pulled-commit exemption" pushes?** — Two different code paths with different operational implications.
6. **Are there roles whose file patterns we should explicitly exclude from the auto-filter path?** — e.g. should `autofixer` or `conflict_resolver` auto-filter at all, given their cross-cutting scope? Or should the auto-filter only apply to execution roles?
7. **What's the expected behavior when the gateway's rewrite collides with a pre-push hook on the server side (rare, but possible if the repo enables protected-branch checks that inspect commit message format)?**
8. **Should `EGG_AGENT_FILE_PATTERNS` (the env var consumed by cli_push) be updated to include the same pattern set the gateway uses, or is duplication between the gateway-side source of truth and the sandbox env var acceptable?**

*Authored-by: egg (refiner)*


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

Resolved decisions from refine HITL (2026-04-22/23):

**decision-1 (authorship mechanism)**: **B3 — gateway-side commit registry**. User chose this over the refiner's recommended B1 (author-email). The plan phase must design the durable store and hook semantics. Key risks the refiner already flagged to address: (a) no durable store exists today — pick one and justify; (b) commits created outside the post-agent-commit hook path (git cherry-pick, rebase --interactive, commits made before the hook fires, amends) would be unregistered — need an explicit policy for unregistered commits (should tie into decision-9 fail-closed); (c) the registry becomes the authoritative source of truth for commit-authorship, so its consistency/durability guarantees matter for audit trails. Axis A (filtering strategy) still defaults to A2 (port 6f0877f50 forward); the change is only in how the gateway attributes each commit to a role.

**decision-6 (rollout order)**: **C1 — single-release cutover**. Ship auto-filter + author-attribution-via-registry + scope-filter removal together in one PR. Keep EGG_AGENT_RESTRICTIONS_ENFORCE=false as a kill switch.

**decision-9 (unknown-role default)**: **Fail closed**. Any commit whose authorship cannot be resolved via the registry is treated as own-authored for restriction-check purposes. No 403 specifically for 'unknown author' — they just fall under the pushing role's restrictions like any other. This plays well with decision-1 (B3): a commit that was never recorded in the registry defaults to the pushing role's scope.

**decision-8 (scope-filter removal)**: **Remove entirely in the same PR** — captured during pre-refine. Delete sandbox/egg_lib/cli_push.py's --scope-filter, update any callers, update docs (docs/guides/agent-development.md, docs/reference/orchestrator-cli.md, any agent-config rules that mention it).

**Decisions 2, 3, 4, 5, 7**: Deferred to the plan phase. The refiner's recommendations (D1 suffix, nothing_to_push=true for all-blocked, pulled_commits response field, shared/egg_restrictions/ helper location, squash multi-commit own-authored pushes) are reasonable defaults; the plan phase should adopt them unless a reviewer surfaces a concrete concern.

**Open-ended feedback items from the draft**: The plan phase should pay particular attention to #1 (attack model — with B3 the registry is the trust boundary, not the sandbox-set identity, so the attack model shifts) and #4 (post_agent_commit hook timing — B3 makes the hook critical infrastructure, not just an audit-log producer).
