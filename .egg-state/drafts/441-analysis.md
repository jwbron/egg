# Analysis: Fix SDLC Contract Validation

> Issue: #441 | Phase: refine

## Problem Statement

In PR #439, the implementer agent was able to modify the contract file (`.egg-state/contracts/436.json`) directly by committing changes to it and pushing. This violated the intended SDLC pipeline design where **only the contract validator bot should modify contracts**.

The offending commit (`0df84c18f6c04166ca4f25d4c7dbc61bd83eeae8`) shows the implementer updating task statuses to "complete" and linking commits—actions that should be restricted to the reviewer role.

**Current state:** The gateway validates branch ownership and operation types (git push, gh pr create) but does **not** validate which files are being modified in a push.

**Desired outcome:** The gateway should block pushes that modify contract files (`.egg-state/contracts/*.json`) when the caller has the implementer role. Only the contract validator bot (reviewer role) should be able to modify contracts.

## Current Behavior

### Gateway Push Validation (gateway/gateway.py:406-641)

The gateway currently validates:
1. **Path validation** — Ensures `repo_path` is within allowed directories
2. **Branch ownership** — Checks if the agent can push to the target branch
3. **Private mode enforcement** — Blocks pushes to public repos in private mode
4. **Phase filtering** — Blocks operations not permitted in current phase

**Missing:** File-level validation—the gateway doesn't inspect which files are modified in the commits being pushed.

### Contract Mutation API (gateway/contract_api.py)

There is a separate API endpoint (`/api/v1/contract/mutate`) with role-based field ownership. However, this API is **optional**—agents can bypass it by directly committing changes to contract files and pushing via `git push`.

### Role System (shared/egg_contracts/roles.py)

Roles are defined with field-level ownership:
- **IMPLEMENTER**: Can modify `commit`, `notes`, `files_affected` fields
- **REVIEWER**: Can modify `status`, `verified`, `current_phase` fields
- **HUMAN**: Can modify all fields

But this enforcement only applies to mutations through the contract API, not to direct file modifications via git.

## Constraints

- **Performance**: File-level validation on every push adds overhead (requires `git diff` analysis)
- **False positives**: Must not block legitimate changes to `.egg-state/drafts/` which implementers should be able to modify
- **Role propagation**: Agent role must be available to the gateway at push time
- **Backwards compatibility**: Should not break existing workflows that don't use SDLC contracts
- **Worktree complexity**: Must work with git worktree paths used by the SDLC pipeline

## Options Considered

### Option A: Pre-push File Validation in Gateway

**Approach**: Add file-level validation in `gateway.py:git_push()` before executing the push. Run `git diff` to identify modified files, then check if any protected files are modified by unauthorized roles.

**Implementation**:
1. Add `protected_file_patterns` configuration to `phase-permissions.json` keyed by role
2. Before push, run `git diff --name-only HEAD@{push}..HEAD` to get modified files
3. Check if any files match protected patterns for the current role
4. Block push if unauthorized modifications detected

**Pros**:
- Enforces restrictions at the infrastructure layer (cannot be bypassed)
- Integrates with existing phase-permissions framework
- Single point of enforcement
- Clear error messages to agent

**Cons**:
- Adds latency to every push (must run git diff)
- Complexity in determining the exact commit range to check
- May need special handling for force pushes and rebases

### Option B: Git Pre-receive Hook on GitHub (Server-side)

**Approach**: Use GitHub's server-side pre-receive hooks (GitHub Enterprise only) or GitHub Actions to validate pushed commits.

**Implementation**:
1. Create a GitHub Action triggered on push events
2. Action validates that commits don't modify protected files unless from authorized actors
3. Block merge or create failing check if violations found

**Pros**:
- Defense in depth (validates even if gateway bypassed)
- Works with GitHub's native tooling
- Can provide detailed feedback in PR checks

**Cons**:
- Pre-receive hooks require GitHub Enterprise
- Actions-based approach is post-push (can't prevent the push)
- Requires additional GitHub infrastructure
- Adds complexity to CI pipeline

### Option C: Extended Contract API with Direct Push Blocking

**Approach**: Modify the contract API to be the **only** way to modify contracts, and completely block any push that includes changes to `.egg-state/contracts/` files regardless of role.

**Implementation**:
1. Gateway blocks ALL pushes containing contract file modifications
2. Contract modifications must go through `/api/v1/contract/mutate` endpoint
3. Gateway commits contract changes directly (bypassing agent push)

**Pros**:
- Simplest conceptually—no file can be pushed if it's a contract
- Clear separation of concerns
- Contract API already has role validation

**Cons**:
- Requires rearchitecting how contracts are committed
- Gateway would need write access to perform commits
- Breaks current workflow where agents can commit drafts and contracts together
- More intrusive change

## Recommended Approach

**Option A: Pre-push File Validation in Gateway**

This approach provides the strongest enforcement with minimal architectural changes. It integrates naturally with the existing phase-permissions framework and keeps all policy enforcement in the gateway.

**Key design decisions**:

1. **File protection patterns by role**: Add to `phase-permissions.json`:
   ```json
   {
     "file_restrictions": {
       "implementer": {
         "blocked_patterns": [".egg-state/contracts/*.json"],
         "blocked_reason": "Contract modifications must go through contract API"
       }
     }
   }
   ```

2. **Efficient diff detection**: Use `git diff --name-only origin/<branch>..HEAD` to detect modified files before push. This is the same diff GitHub will receive, ensuring accurate detection.

3. **Clear error messages**: When blocked, return the specific file(s) that triggered the block and explain that contract modifications must use the contract API.

4. **Graceful degradation**: If role is not available or phase filtering is disabled, skip file-level validation to maintain backwards compatibility.

**Implementation scope**:
- Modify `gateway/gateway.py:git_push()` to add file validation step
- Extend `phase-permissions.json` schema with `file_restrictions` section
- Update `gateway/phase_filter.py` to expose file restriction checking
- Add tests for file-level blocking scenarios
- Update documentation

## Open Questions

<!-- HITL Decision: Implementation scope -->

The recommended approach (Option A) can be implemented with varying levels of restriction. Which scope should we implement?

- [ ] **Minimal** — Block only `.egg-state/contracts/*.json` for implementer role
- [ ] **Conservative** — Also block `.egg/schemas/*.json` and `.egg/phase-permissions.json` (config files)
- [ ] **Comprehensive** — Configurable per-phase file restrictions (full flexibility)
- [ ] Other (explain in reply)

---

*Authored-by: egg*
