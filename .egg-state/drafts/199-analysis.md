# Analysis: Enable pre-commit hooks safely in sidecar architecture

> Issue: #199 | Phase: refine

## Problem Statement

Pre-commit hooks are currently disabled in the sidecar architecture due to security concerns originally identified in issue #58. The core problem: git hooks execute arbitrary shell scripts, and in the current architecture, git operations run on the gateway sidecar—which has access to GitHub credentials and other sensitive resources that agents should not be able to access.

The current mitigation (implemented in PR #61) disables all hooks via `core.hooksPath=/dev/null` in `gateway/git_client.py:50-56`. This prevents malicious repositories from executing code on the gateway, but it also prevents legitimate use cases like:

- Contract validation hooks for the SDLC pipeline (issue #133)
- Code formatting via pre-commit framework
- Other defense-in-depth validation mechanisms

The goal is to enable pre-commit hooks while maintaining the security boundary that prevents hook scripts from accessing gateway credentials.

## Current Behavior

### Architecture Overview

The egg architecture uses a **gateway-managed worktree model**:

1. **Sandbox container**: Runs the agent (Claude Code). Has NO access to `.git` directories—they are shadowed by tmpfs mounts (`sandbox/egg_lib/runtime.py:314-334`).

2. **Gateway sidecar**: Runs on the host, holds GitHub credentials, executes all git operations. The container routes git commands through the wrapper script (`sandbox/scripts/git`) which calls gateway REST endpoints.

3. **Security boundary**: The container cannot directly execute git operations or access credentials. All git operations go through gateway validation.

### Hook Disabling

Hooks are disabled in two ways:

1. **Primary**: `core.hooksPath=/dev/null` in `git_cmd()` (`gateway/git_client.py:50-56`)
2. **Defense-in-depth**: `--no-verify` flag added for commit/merge/am operations (`gateway/gateway.py:779-780`)

This prevents:
- Pre-commit hooks (e.g., `.git/hooks/pre-commit`)
- The pre-commit framework (`pre-commit run`)
- Any hook that would execute during git operations

### Why This Matters

Issue #133 (SDLC pipeline) wants to use pre-commit validation as a defense-in-depth mechanism for contract role enforcement. Without safe hook execution, this validation must happen elsewhere (e.g., CI only), reducing the defense-in-depth value.

## Constraints

- **Security**: Hook scripts MUST NOT have access to GitHub tokens or gateway credentials
- **Isolation**: Hook execution must be confined to the sandbox security boundary
- **Compatibility**: Solution should work with standard pre-commit framework where possible
- **Performance**: Hook execution should not add significant latency to git operations
- **Simplicity**: Prefer solutions that leverage existing architecture over introducing new components

## Options Considered

### Option A: Sandbox-Side Hook Execution

**Approach**: Execute hooks in the sandbox container rather than the gateway. When a hook-triggering operation (commit, merge, etc.) is requested:

1. Gateway performs the git operation with `--no-verify`
2. Before returning success, gateway calls back to sandbox to run hooks
3. If hooks fail, gateway reverts the operation
4. If hooks pass, operation completes

**Implementation sketch**:
- Add new gateway endpoint: `POST /api/v1/hooks/run`
- Gateway invokes sandbox via HTTP callback (container already has session token)
- Sandbox runs pre-commit framework or custom hooks in isolated environment
- Gateway receives pass/fail result

**Pros**:
- Hooks run in sandbox with no credential access (existing isolation)
- Can use standard pre-commit framework
- Leverages existing container security model
- Hooks have access to full working directory context

**Cons**:
- Requires bidirectional communication (gateway → sandbox)
- Complex failure handling (what if sandbox is unresponsive?)
- Pre-commit framework expects `.git` directory access (may need workaround)
- Adds latency to commit operations

### Option B: Gateway-Mediated Validation (Validation Endpoints)

**Approach**: Replace hooks with explicit validation endpoints. Instead of hooks running automatically during git operations, the agent explicitly calls validation before committing.

**Implementation sketch**:
- Add gateway endpoint: `POST /api/v1/validate/pre-commit`
- Agent calls validation endpoint, passing file list
- Gateway runs validation in a subprocess with restricted permissions
- Agent proceeds with commit only after validation passes

**Pros**:
- No bidirectional communication complexity
- Gateway controls what validation runs
- Can enforce validation in CI as fallback
- Simple to implement

**Cons**:
- Not automatic—agent must explicitly call validation (can be bypassed)
- Loses the "hook" semantic of automatic enforcement
- Requires changes to agent prompts/workflows
- Defense-in-depth is weaker (relies on agent cooperation)

### Option C: Isolated Hook Runner (Subprocess Sandboxing)

**Approach**: Gateway runs hooks in a restricted subprocess with no access to credentials. Uses Linux namespaces, seccomp, or a minimal container.

**Implementation sketch**:
- Create hook execution environment with:
  - No network access (network namespace isolation)
  - No access to credential files (mount namespace)
  - Read-only access to working directory
  - Time-limited execution
- Gateway spawns this environment for hook execution
- Result returned to gateway for pass/fail decision

**Pros**:
- Hooks run automatically as expected
- No bidirectional communication with sandbox
- Strong isolation without full container overhead

**Cons**:
- Adds significant complexity to gateway
- Linux namespace management is error-prone
- May not be compatible with all hook types
- Gateway becomes more complex (currently simple Python/Flask)

### Option D: Pre-Commit in CI Only (Current Partial Solution)

**Approach**: Keep hooks disabled in the agent workflow, but enforce them in CI. The on-pull-request workflow already exists (`on-pull-request-contract-verify.yml`).

**Pros**:
- No code changes required
- CI is already trusted/isolated
- Simple to understand and maintain

**Cons**:
- No defense-in-depth at commit time
- Agent can commit invalid changes (caught later in CI)
- Feedback loop is slower (push → CI → fail → fix → push)
- Doesn't address the original issue requirement

## Recommended Approach

**Option A (Sandbox-Side Hook Execution)** is recommended, with the following design:

### Why Option A?

1. **Leverages existing isolation**: The sandbox already has the right security properties—no credential access, network restrictions in private mode, containerized environment.

2. **Standard tooling**: Works with the pre-commit framework, which the project already has configured (`.pre-commit-config.yaml`).

3. **Automatic enforcement**: Hooks run as part of git operations, not requiring agent cooperation.

4. **Architectural alignment**: The gateway already delegates some operations to the container (e.g., file operations). This extends that pattern.

### Implementation Design

#### Phase 1: Core Hook Execution Path

1. **New gateway endpoint**: `POST /api/v1/hooks/execute`
   - Accepts: `{ "repo_path": "...", "hook_type": "pre-commit", "files": [...] }`
   - Calls sandbox via HTTP to run hooks
   - Returns: `{ "success": true/false, "output": "..." }`

2. **Sandbox hook runner script**: `/opt/egg-hooks/run-hook.sh`
   - Receives hook type and file list
   - Runs pre-commit framework (or custom hooks)
   - Returns exit code and output

3. **Gateway modification**: In `git_execute()`, for `commit` operation:
   - Before running `git commit`, call hook execution endpoint
   - If hooks fail, return error (don't run commit)
   - If hooks pass, run commit (still with `--no-verify` for gateway safety)

#### Phase 2: Pre-Commit Framework Integration

1. **Pre-commit in sandbox**: Install pre-commit in container image
2. **Hook discovery**: Gateway checks for `.pre-commit-config.yaml` before calling hooks
3. **Caching**: Consider pre-commit's cache behavior with containerized execution

#### Phase 3: Contract Validation Hook

1. **Custom hook**: Add contract validation hook to `.pre-commit-config.yaml`:
   ```yaml
   - repo: local
     hooks:
       - id: contract-role-validation
         name: Contract Role Validation
         entry: egg-contract validate-changes
         language: system
         pass_filenames: true
   ```

2. **Validation logic**: `egg-contract validate-changes` checks that:
   - Only worker-owned fields are modified by worker role
   - Reviewer-only fields are not touched by worker

### Addressing `.git` Access

The sandbox doesn't have `.git` access (shadowed by tmpfs). Pre-commit framework requires some git access for staged file detection. Solutions:

1. **Pass file list explicitly**: Gateway determines staged files and passes to hook runner
2. **Minimal git metadata**: Expose read-only subset of git info via gateway API
3. **Mock git for hooks**: Provide a minimal git wrapper that answers hook queries

Option 1 (explicit file list) is simplest and sufficient for most hooks.

### Security Considerations

- **Timeout enforcement**: Hook execution must have timeout to prevent DoS
- **Output size limits**: Limit hook output to prevent memory exhaustion
- **No credential passthrough**: Ensure hook runner has NO access to session tokens or GitHub credentials
- **Audit logging**: Log all hook executions and results

## Open Questions

### Architecture Decisions (Multiple-choice)

1. **Hook execution model**: How should the gateway communicate with the sandbox for hook execution?
   - [ ] HTTP callback to sandbox (sandbox runs a small HTTP server for hooks)
   - [ ] Execute via Docker exec into running container
   - [ ] Use existing gateway→sandbox RPC mechanism (if one exists)
   - [ ] Other (explain in reply)

2. **Staged file detection**: How should hooks know which files to validate?
   - [ ] Gateway passes explicit file list to hook runner
   - [ ] Expose minimal git info via gateway API for hooks to query
   - [ ] Run hooks on all files in repo (simpler but slower)
   - [ ] Other (explain in reply)

### Open-Ended Questions

3. What is the expected latency budget for hook execution? Pre-commit on a large changeset can take 10+ seconds.

4. Should hooks be opt-in per repository, or enabled globally for all repos in the sidecar architecture?

5. Are there any existing plans for gateway↔container bidirectional communication that this could leverage?

---

*Authored-by: egg*
