# Analysis: Protected Files and Lines (Issue #200)

> Analysis document for implementing file/line protection to prevent the agent from modifying critical configuration.

## Problem Statement

Issue #200 reports that PR #184 "fixed" CI check failures by decreasing test coverage thresholds. This is an undesirable outcome - the agent should not be able to modify certain files or lines that define quality gates.

The goal is to implement a mechanism that restricts the agent from updating:
1. Specific files (e.g., coverage configuration files)
2. Specific lines within files (e.g., threshold values)

## Current Architecture

The egg system uses a **security-by-infrastructure** model where the gateway sidecar enforces all policies. The sandbox container has no direct access to credentials - all git/gh operations are routed through the gateway.

### Existing Protection Mechanisms

| Protection | Location | Status |
|------------|----------|--------|
| Merge blocking | `gateway/policy.py:742-761` | Enforced |
| Branch ownership | `gateway/policy.py:check_branch_ownership()` | Enforced |
| Protected branches (main/master) | `gateway/policy.py:446-464` | Enforced |
| Blocked gh commands | `gateway/github_client.py:BLOCKED_GH_COMMANDS` | Enforced |
| Credential isolation | Gateway architecture | By design |
| File/line protection | — | **Not implemented** |

## Proposed Solution

### Option A: Gateway-Level Diff Validation (Recommended)

Validate git push operations by scanning the commit diff for protected file/line modifications.

**Enforcement Point:** `POST /api/v1/git/push` endpoint in `gateway/gateway.py`

**Flow:**
```
Agent commits changes locally
    ↓
Agent runs `git push origin branch`
    ↓
Gateway receives push request
    ↓
Gateway runs `git diff` on the commits being pushed
    ↓
Protected file policy checks diff against configuration
    ├─ Protected file modified? → Block push with error message
    └─ No violations → Proceed with push
```

**Advantages:**
- Enforced at infrastructure level (cannot be bypassed by agent)
- Consistent with existing gateway policy pattern
- Clear error messages help agent understand constraints
- No additional client-side tooling required

**Disadvantages:**
- Validation happens late (after agent has done work)
- Requires git diff parsing

### Option B: Pre-commit Hook (Client-Side)

Add a pre-commit hook in the sandbox that rejects commits touching protected files.

**Disadvantages:**
- Can be bypassed with `git commit --no-verify`
- Gateway already disables hooks for security (`core.hooksPath=/dev/null`)
- Not suitable as sole enforcement mechanism

**Could be used for:** Fast feedback during development (complementary to Option A)

### Option C: Claude Rules + CLAUDE.md Instructions

Add instructions to `.claude/rules/` telling the agent not to modify certain files.

**Disadvantages:**
- Soft enforcement only (agent could ignore)
- No technical barrier
- Instructions can be forgotten in complex tasks

**Could be used for:** Guidance to help agent avoid protected files proactively

## Recommended Approach: Gateway Policy + Guidance

Implement a **two-layer approach**:

1. **Hard enforcement (Gateway):** Block pushes containing protected file modifications
2. **Soft guidance (Rules):** Tell agent about protected files so it doesn't waste effort

### Configuration Format

Add `protected_files` section to `repositories.yaml`:

```yaml
# Protected files configuration
# These files/lines cannot be modified by the agent
protected_files:
  # Block entire file
  - path: ".coveragerc"
    reason: "Test coverage configuration"

  # Block specific lines (1-indexed, inclusive ranges)
  - path: "pyproject.toml"
    lines: [50-55]  # coverage thresholds
    reason: "Coverage threshold configuration"

  # Glob patterns supported
  - path: ".github/workflows/*.yml"
    lines: [1-10]  # workflow configuration headers
    reason: "CI workflow configuration"

  # Block with different strictness levels
  - path: "gateway/policy.py"
    lines: [742-761]  # merge blocking code
    level: "immutable"  # immutable | warn_on_pr | log_only
    reason: "Critical security policy"
```

### Protection Levels

| Level | Behavior |
|-------|----------|
| `immutable` (default) | Block push entirely |
| `warn_on_pr` | Allow push, add warning comment to PR |
| `log_only` | Allow push, log for audit |

### Implementation Components

1. **`gateway/file_policy.py`** - Core protection logic
   - Load configuration from repositories.yaml
   - Parse git diff output
   - Check for protected file/line violations
   - Return detailed error messages

2. **`gateway/gateway.py`** - Integration point
   - Call file policy check in `/api/v1/git/push` handler
   - Return 403 with clear error message on violation

3. **`gateway/tests/test_file_policy.py`** - Test coverage
   - Unit tests for diff parsing
   - Integration tests for push blocking

4. **`sandbox/.claude/rules/protected-files.md`** - Agent guidance
   - List protected files
   - Explain why they're protected
   - Suggest alternatives

### API Changes

**Modified endpoint:** `POST /api/v1/git/push`

**New error response for protected file violation:**
```json
{
  "success": false,
  "error": "protected_file_violation",
  "message": "Push blocked: changes to protected files detected",
  "details": {
    "violations": [
      {
        "file": ".coveragerc",
        "lines": null,
        "reason": "Test coverage configuration"
      },
      {
        "file": "pyproject.toml",
        "lines": [52, 53],
        "reason": "Coverage threshold configuration"
      }
    ]
  }
}
```

## Implementation Plan

### Phase 1: Core Implementation
- [ ] Create `gateway/file_policy.py` with protection logic
- [ ] Add configuration schema to `config/repo_config.py`
- [ ] Update `config/repositories.yaml.example` with protected_files section
- [ ] Integrate into gateway push endpoint
- [ ] Write unit tests

### Phase 2: Integration
- [ ] Add agent guidance in `.claude/rules/protected-files.md`
- [ ] Create ADR documenting the feature
- [ ] Update gateway README

### Phase 3: Refinement (Future)
- [ ] Add `warn_on_pr` level with PR comment integration
- [ ] Add admin override mechanism (via GATEWAY_TRUSTED_USERS)
- [ ] Add audit logging for violations

## Test Cases

1. **Entire file protection:** Push modifying `.coveragerc` is blocked
2. **Line-specific protection:** Push modifying only protected lines is blocked
3. **Line-specific allowance:** Push modifying non-protected lines in protected file is allowed
4. **Glob pattern matching:** Push modifying any `.github/workflows/*.yml` file is blocked
5. **Multiple violations:** Error message lists all violations
6. **No violations:** Push proceeds normally
7. **Empty configuration:** No protections applied, push proceeds

## Security Considerations

- Configuration file (`repositories.yaml`) must be outside agent's reach
- Gateway loads config at startup; agent cannot modify runtime config
- Use `immutable` level for critical security files (like merge block code)
- Consider adding protected_files to gateway config rather than repositories.yaml for extra isolation

## Open Questions

1. **Config location:** Should protected_files be in:
   - `repositories.yaml` (per-repo config, easier to customize)?
   - Gateway environment (more isolated from agent)?

2. **Line range format:** Should we support:
   - Simple ranges: `[1-10, 50-55]`
   - Individual lines: `[1, 2, 3, 50, 51]`
   - Both?

3. **Diff parsing:** Should we:
   - Parse unified diff format ourselves?
   - Use a library like `unidiff`?
   - Shell out to `git diff --stat` for file-level checks?

## Conclusion

The recommended approach is **Gateway-level diff validation** with **agent guidance** as a secondary measure. This follows egg's existing security model where the gateway is the enforcement layer, and instructions guide the agent's behavior.

The implementation should be straightforward, following the existing patterns in `gateway/policy.py` for policy checks and error handling.

---

*Analysis by: egg*
*Issue: https://github.com/jwbron/egg/issues/200*
