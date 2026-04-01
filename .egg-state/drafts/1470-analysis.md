# Analysis: Gateway should auto-filter disallowed files on push instead of blocking entirely

> Issue: #1470 | Phase: refine

## Problem Statement

When agents push commits containing files outside their role's allowed set, the gateway rejects the **entire push** with a 403 error. This causes two cascading problems:

1. **Direct rejection**: An agent (e.g., coder) commits both implementation and test files in one commit, then gets blocked because test files are outside the coder role's boundary.
2. **Inherited rejection**: A downstream agent (e.g., documenter) pushes HEAD which includes prior agents' commits containing disallowed files in the diff from the remote branch. Even though the documenter only modified docs, the diff includes the coder's files.

Agents then enter costly recovery loops — splitting commits, cherry-picking, resetting — burning tokens/time and sometimes failing entirely. The desired outcome is for the gateway to transparently filter disallowed files from the push, allowing the permitted portion through and reporting what was excluded.

## Current Behavior

### Push validation flow (`gateway/gateway.py`, lines 860-1046)

The `git_push()` endpoint performs four sequential file-level checks:

1. **Phase file restrictions** (`check_file_restrictions()`, line 861) — blocks files based on SDLC phase (e.g., implement phase can't modify `.egg-state/contracts/`)
2. **Agent role restrictions** (`check_agent_restrictions()`, line 891) — blocks files based on agent role (e.g., coder can't modify test files)
3. **Anchor write scoping** (line 935) — agents can only write their own anchor file
4. **Phase-specific patterns** (`check_phase_file_restrictions()`, line 999) — additional phase-level patterns

Each check is **binary**: if any disallowed file is found, the entire push returns 403. There is no partial-push capability.

### Changed file detection (`gateway/git_client.py`, lines 1301-1455)

`get_changed_files_in_push()` computes the diff between the remote branch and local HEAD:
- Primary: `git diff --name-only origin/<branch>..HEAD`
- Fallback: Per-commit inspection via `git rev-list` + `git diff-tree` per SHA

This returns **all files changed across all commits** being pushed — not just the current agent's commits. This is why the documenter gets blocked: the diff includes coder's files from earlier commits.

### Role boundary definitions (`gateway/agent_restrictions.py`)

`AGENT_PATTERNS` (lines 650-667) maps each role to allowed/blocked file patterns. The `AgentFilePattern.can_write()` method (lines 74-104) checks blocked patterns first (security precedence), then allowed patterns.

### Precedent: post-agent auto-commit already filters (`gateway/post_agent_commit.py`, lines 180-260)

The auto-commit mechanism (triggered at container exit) already implements file filtering:
- Identifies phase-restricted files via `check_phase_file_restrictions()`
- Restores blocked files to their committed state via `git checkout -- <file>`
- Stages only allowed files
- Only commits/pushes the permitted subset

This demonstrates the filtering pattern already exists in the codebase, but only for auto-commits — not for agent-initiated pushes.

## Constraints

- **Security (fail-closed)**: The gateway's security model is fail-closed throughout. Any auto-filter mechanism must not weaken this — if file detection fails, the push must still be blocked.
- **Git pushes commits, not files**: Git doesn't support pushing partial commits. Filtering files out of a push requires rewriting commit history, which changes SHAs and has implications for branch state.
- **Shared branch architecture**: In concurrent mode, multiple agents share a pipeline branch. Rewriting history affects all agents' views of the branch.
- **Agent worktree isolation**: Each agent has an isolated worktree but shares the same branch history. Filtering must not corrupt the agent's local state.
- **Checkpoint integrity**: Per-push checkpoints (lines 1140-1188) capture commit SHAs. History rewriting would invalidate these references.
- **Backward compatibility**: The existing warn-only mode (`EGG_AGENT_RESTRICTIONS_ENFORCE=false`) should still work and the API response format should remain compatible.

## Options Considered

### Option A: Gateway-side commit rewriting at push time

**Approach**: When the gateway detects disallowed files in a push, it rewrites the commit(s) in the agent's worktree to exclude those files before executing the actual `git push`. This would use `git filter-branch` or manual tree manipulation (`git commit-tree`, `git update-ref`) to create sanitized commits.

**Pros**:
- Transparent to agents — they commit normally and the gateway handles filtering
- Preserves the push-time enforcement model (single enforcement point)
- Agents don't need to understand role boundaries

**Cons**:
- **High complexity**: Rewriting multi-commit pushes is error-prone, especially with merge commits
- **SHA instability**: Rewritten commits have different SHAs, breaking references (checkpoints, other agents' local state)
- **Worktree corruption risk**: Modifying the agent's git history from the gateway side could leave the worktree in an inconsistent state
- **Performance**: `git filter-branch` / tree manipulation is slow, especially on large repos
- **Concurrent mode hazard**: Other agents on the same branch may have already fetched the original SHAs

### Option B: Commit-time enforcement (shift-left to `git commit`)

**Approach**: Intercept `git commit` operations at the gateway level and strip disallowed files from the staging area before the commit is created. The push endpoint remains unchanged — by the time a push happens, all commits already contain only allowed files.

**Pros**:
- Eliminates the problem at the source — no disallowed files ever enter commits
- No history rewriting needed
- Simpler mental model: agents can only commit what they're allowed to push
- Consistent with post-agent-commit's existing filtering pattern

**Cons**:
- **Gateway doesn't currently intercept `git commit`**: The agent runs `git commit` locally in its worktree (the `.git` dir is shadowed by tmpfs but commits still happen locally via the git wrapper). Adding commit interception is a new capability.
- **Doesn't solve the inherited-diff problem**: Even if agent B only commits allowed files, pushing those commits includes agent A's prior commits in the diff. Unless the diff detection is also changed (see Option D).
- **Agent confusion**: Agents may notice files they staged weren't committed, leading to retry loops

### Option C: Push-time selective file filtering (recommended hybrid)

**Approach**: At push time, when disallowed files are detected, the gateway:

1. Identifies which files are disallowed for this agent's role
2. Creates a **temporary branch** from the remote branch tip
3. Cherry-picks each commit being pushed, but rewrites each to exclude disallowed files (using `git diff-tree` to get per-commit changes, then staging only allowed files)
4. Pushes the temporary branch content to the target branch
5. Updates the agent's local branch to match (fast-forward)
6. Returns a success response listing excluded files

For the **inherited-diff problem** specifically: the gateway can distinguish between the agent's own commits and prior agents' commits. If a commit was authored by a different agent, its files should not trigger filtering — they were already pushed (or will be pushed) by the appropriate role. The key change is: **only validate files in the current agent's commits, not the entire diff**.

**Pros**:
- Solves both problems (direct rejection and inherited rejection)
- Preserves other agents' commits intact — only rewrites the current agent's commits
- Clear success response tells agents what was excluded
- Fail-closed: if rewriting fails, falls back to blocking the push

**Cons**:
- Moderate complexity: per-commit rewriting logic
- Still changes SHAs for the current agent's rewritten commits
- Edge cases around merge commits and empty commits (after filtering, a commit may become empty)

### Option D: Scope file detection to agent's own commits only

**Approach**: Modify `get_changed_files_in_push()` to only report files from commits authored by the current agent, ignoring files from prior agents' commits. This solves the inherited-diff problem without any commit rewriting.

Combined with a "warn and continue" mode for the agent's own disallowed files (or the existing warn-only mode), this would:
1. Stop blocking documenters/testers for coder's files in prior commits
2. Optionally warn (but not block) when an agent includes disallowed files in their own commits

**Pros**:
- **Simplest implementation**: Changes only `get_changed_files_in_push()` and its callers
- No commit rewriting, no SHA changes, no worktree corruption risk
- Solves the documented-problem's root cause (inherited diffs)
- Low risk: only changes what files are flagged, not how pushes execute

**Cons**:
- Does NOT auto-filter the agent's own disallowed files — the coder who commits both `src/` and `tests/` still gets blocked for their own commit
- Relies on commit authorship (container ID or agent role in commit metadata) to distinguish agents — needs reliable attribution
- Weakens the security model if commit authorship can be spoofed (though agents are sandboxed)

### Option E: Dual-layer approach (Option D + commit-time filtering)

**Approach**: Combine two changes:
1. **Push time**: Scope file detection to the current agent's commits only (Option D) — solves inherited-diff problem
2. **Commit time**: Add a gateway-level commit interceptor that strips disallowed files from staging before commit — solves direct-rejection problem

**Pros**:
- Clean separation: commit-time prevents the problem, push-time ignores inherited files
- No history rewriting
- No SHA instability
- Agents get immediate feedback at commit time (not delayed to push time)

**Cons**:
- Two enforcement points to maintain (adds complexity to the gateway)
- Requires adding commit interception as a new gateway capability
- Agent-side behavior change: files silently removed from commits

## Recommended Approach

**Option C (Push-time selective file filtering)** is recommended, with elements of Option D.

The core insight is that there are two distinct problems requiring different solutions:

1. **Inherited-diff problem** (documenter blocked by coder's files): Solve by scoping push validation to only the current agent's commits (Option D's approach). This is a low-risk change to `get_changed_files_in_push()`.

2. **Direct-rejection problem** (coder blocked for own commit with mixed files): Solve by rewriting the agent's commits at push time to exclude disallowed files, similar to how `post_agent_commit.py` already filters files.

The recommended hybrid:
- For files in **other agents' commits**: skip validation entirely (they were/will be pushed by the appropriate role)
- For files in **the current agent's commits**: auto-filter by rewriting the commit to exclude disallowed files, report what was excluded in the response

This approach is justified because:
- The `post_agent_commit.py` precedent proves the filtering pattern works
- The fail-closed security model is preserved (rewrite failure = push blocked)
- Agents get clear feedback about excluded files
- The inherited-diff problem is solved cleanly without any rewriting

## Complexity Assessment

**High**. This change touches the gateway's core push validation pipeline, requires per-commit history manipulation, affects the security model, needs careful handling of edge cases (empty commits, merge commits, concurrent agents), and requires comprehensive test coverage across multiple scenarios.

## Open Questions

All questions have been registered as HITL decisions via `egg-orch decision create` (the pipeline's decision queue for pipeline `issue-1470-auto-filter`). Note: `egg-contract` could not reach the contract through the gateway API (404 due to path resolution between container and host), so decisions were registered through the orchestrator's decision system instead. All 6 decisions are confirmed present via `egg-orch decision list issue-1470-auto-filter`.

### Decision 1: Implementation approach
**Question**: Which implementation approach should we use for auto-filtering disallowed files on push?
- [ ] **Option C**: Push-time selective filtering — rewrite agent's own commits to exclude disallowed files, skip validation for other agents' inherited commits *(recommended)*
- [ ] **Option D**: Scope-only — only change file detection to ignore inherited commits, still block agent's own disallowed files (simpler but partial fix)
- [ ] **Option E**: Dual-layer — add commit-time filtering + scoped push detection (most thorough but largest scope)
- [ ] Other (explain in reply)

### Decision 2: Empty commit handling
**Question**: How should the gateway handle commits that become empty after filtering out disallowed files?
- [ ] Skip empty commits silently (drop them from the push)
- [ ] Preserve empty commits with a marker message
- [ ] Fail the push if any commit becomes empty (conservative)
- [ ] Other (explain in reply)

### Decision 3: Plan-gateway alignment
**Question**: Should plan generation be updated to respect gateway role boundaries when assigning files to tasks?
- [ ] Yes — plan should align task file assignments with role boundaries (prevents mismatch at source)
- [ ] No — gateway auto-filtering is sufficient, keep plan assignments advisory
- [ ] Both — align plan AND auto-filter as defense-in-depth
- [ ] Other (explain in reply)

### Feedback 4: All-disallowed commit behavior
**Question**: What is the expected behavior when an agent's commit contains ONLY disallowed files (i.e., all files would be filtered)? Should the push succeed with zero files, or should it return a specific error/warning?

### Feedback 5: Feature rollout strategy
**Question**: Should the auto-filter feature be opt-in (new env var like `EGG_AGENT_AUTO_FILTER=true`) or replace the current blocking behavior by default? What about the existing warn-only mode (`EGG_AGENT_RESTRICTIONS_ENFORCE=false`) — should it be preserved as a third option?

### Feedback 6: Security implications of scoped validation
**Question**: Are there security concerns with scoping push validation to only the current agent's commits? Specifically: if an attacker compromises an agent and crafts commits attributed to a different agent, those commits' files would bypass validation. Is the sandbox isolation sufficient mitigation, or do we need additional safeguards?

---

*Authored-by: egg*
