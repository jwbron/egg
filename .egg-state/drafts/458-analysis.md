# Analysis: Remove Remaining Hardcoded Bot Usernames

> Issue: #458 | Phase: refine

## Problem Statement

The codebase contains approximately 35 instances of the hardcoded bot username `james-in-a-box`. While most workflow inputs properly support bot username configuration via the `bot_username` input parameter, some locations have hardcoded values that cannot be overridden by external adopters. This was flagged as issue #9 in PR #457's code review.

The goal is to make all bot username references configurable so that external repositories can use the egg SDLC workflows with their own GitHub Apps without encountering identity mismatches.

## Current Behavior

### Categories of Hardcoded Values

1. **Workflow input defaults** (acceptable pattern):
   ```yaml
   bot_username:
     default: "james-in-a-box"
   ```
   These are fine — callers can override via input.

2. **Job-level `if:` conditions** (problematic):
   ```yaml
   # .github/workflows/sdlc-hitl.yml:881-882
   if: >-
     github.event.sender.login != 'james-in-a-box' &&
     github.event.sender.login != 'james-in-a-box[bot]'
   ```
   GitHub Actions limitation: job-level `if:` cannot access `needs` outputs, so this hardcodes the self-trigger prevention check.

3. **Shell script defaults** (problematic):
   ```bash
   # action/build-mention-prompt.sh:18
   BOT_USERNAME="${BOT_USERNAME:-james-in-a-box}"
   ```
   These should use a generic default or require explicit configuration.

4. **Python module defaults** (problematic):
   ```python
   # sandbox/egg_lib/self_improvement/config.py:7
   BOT_USERNAME = os.getenv("EGG_BOT_USERNAME", "james-in-a-box")
   ```
   Same issue — should use a generic default like `"egg"`.

5. **Resolve-inputs fallbacks** (redundant):
   ```yaml
   echo "bot_username=${{ inputs.bot_username || 'james-in-a-box' }}"
   ```
   These duplicate the input defaults and should match.

### Files Requiring Changes

| File | Line | Type | Description |
|------|------|------|-------------|
| `.github/workflows/sdlc-hitl.yml` | 881-882 | if condition | Self-trigger prevention |
| `action/build-mention-prompt.sh` | 18 | default | Script fallback |
| `sandbox/egg_lib/self_improvement/config.py` | 7 | default | Python fallback |
| `gateway/policy.py` | 56, 94 | comments | Example values (documentation) |
| `gateway/tests/test_policy.py` | 129-134 | test data | Test fixtures |

Note: Workflow input defaults (`default: "james-in-a-box"`) and their corresponding resolve-inputs fallbacks are acceptable — they maintain backwards compatibility for this repository while allowing external overrides.

## Constraints

- **GitHub Actions limitation**: Job-level `if:` conditions cannot access `needs` outputs, only `inputs` and `github.*` context
- **Backwards compatibility**: Existing workflows using default values should continue working
- **Testing**: Test fixtures can use any bot name as long as tests are self-consistent
- **Documentation**: Comment examples are informational, not functional

## Options Considered

### Option A: Use Input Variables in Job-Level Conditions

**Approach**: Create repository or organization variables (e.g., `vars.BOT_USERNAME`) that can be accessed in job-level `if:` conditions.

**Pros**:
- Clean solution using native GitHub Actions features
- Single source of truth per repository/organization
- No workflow changes needed for adopters once variable is set

**Cons**:
- Requires manual variable setup in each repository
- Variables are not part of workflow_call inputs
- Mixed configuration sources (inputs vs. variables)
- Doesn't work for public repositories forking the workflow

### Option B: Move Self-Trigger Check to Step Level

**Approach**: Remove hardcoded values from job-level `if:` conditions. Add an early step in the job that performs the bot self-trigger check and uses `exit 0` to skip gracefully.

```yaml
process-feedback:
  # Remove problematic if condition with hardcoded username
  if: >-
    contains(github.event.comment.body, '<!-- egg-feedback') &&
    contains(github.event.comment.body, '[x] Submit feedback')
  env:
    BOT_USERNAME: ${{ needs.resolve-inputs.outputs.bot_username }}
  steps:
    - name: Check not self-trigger
      env:
        SENDER_LOGIN: ${{ github.event.sender.login }}
      run: |
        if [[ "$SENDER_LOGIN" == "$BOT_USERNAME" || \
              "$SENDER_LOGIN" == "${BOT_USERNAME}[bot]" ]]; then
          echo "Skipping: triggered by bot itself"
          exit 0
        fi
```

**Pros**:
- Uses the existing configurable `bot_username` input
- No additional setup required for adopters
- Consistent with other authorization checks already at step level
- Works with all deployment models (forks, workflow_call, etc.)

**Cons**:
- Job will "run" (show as started) even when skipping due to self-trigger
- Slightly more complex workflow structure
- Requires careful handling of `exit 0` vs `exit 1`

### Option C: Accept Hardcoded Default, Document Override

**Approach**: Keep `james-in-a-box` as the default but ensure all checks also look at the configurable input. Add documentation explaining that external adopters must pass `bot_username` input.

**Pros**:
- Minimal code changes
- Maintains backwards compatibility
- Already partially implemented

**Cons**:
- Doesn't solve the job-level `if:` limitation
- External adopters will see spurious job runs for bot self-triggers
- Not a complete solution

## Recommended Approach

**Option B: Move Self-Trigger Check to Step Level**

This is the recommended approach because:

1. **Works within existing patterns**: The codebase already has step-level authorization checks that use `needs.resolve-inputs.outputs.bot_username`
2. **No external configuration required**: Adopters just pass `bot_username` input
3. **Complete solution**: Addresses all hardcoded instances without introducing new configuration mechanisms
4. **Consistent**: All bot identity checks would use the same configurable path

### Implementation Summary

1. **sdlc-hitl.yml**: Move the sender login check from job-level `if:` to first step in `process-feedback` job
2. **action/build-mention-prompt.sh**: Change default from `james-in-a-box` to `egg` (generic) or require explicit `BOT_USERNAME` env var
3. **sandbox/egg_lib/self_improvement/config.py**: Change default from `james-in-a-box` to `egg`
4. **gateway/policy.py**: Update example comments (documentation only)
5. **gateway/tests/test_policy.py**: No change needed (test fixtures are self-consistent)

## Open Questions

None — the recommended approach is straightforward and doesn't require additional human input.

---

*Authored-by: egg*
