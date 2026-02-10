# Analysis: Audit dead or deprecated code

> Issue: #478 | Phase: refine

## Problem Statement

The project is preparing for a v1 release and needs a comprehensive audit of dead code, deprecated code, backwards compatibility shims, and migration code. The goal is a clean codebase that doesn't carry unnecessary maintenance burden.

Note: Issue #451 (SDLC Unification 4/4) already removed significant deprecated code including the circuit breaker module, mark-task/mark-phase CLI commands, and phase-specific review prompt scripts. This audit focuses on **remaining** cleanup opportunities.

## Current Behavior

### 1. Deprecated Shell Scripts (Gateway)

Three shell scripts in `gateway/` are explicitly deprecated but still exist:

| File | Lines | Status |
|------|-------|--------|
| `gateway/setup.sh` | 407 lines | Deprecated notice at top (line 7); warns users but still runs if confirmed |
| `gateway/start-gateway.sh` | 348 lines | Deprecated (line 6); prints warning then continues |
| `gateway/create-networks.sh` | 133 lines | Deprecated (line 5); networks now managed by Docker Compose |

These scripts have been superseded by Docker Compose deployment (`bin/egg-deploy up`) and the `egg --compose` CLI. They print deprecation warnings but remain functional for debugging and backwards compatibility.

### 2. Deprecated Function in private_repo_policy.py

**Location**: `gateway/private_repo_policy.py:106-122`

The `is_private_mode_enabled()` function is marked deprecated:
```python
def is_private_mode_enabled() -> bool:
    """
    DEPRECATED: This function is no longer used for the main policy code path.
    Mode is now determined per-container via session_mode parameter...
    """
```

It remains for backwards compatibility with `fork_policy.py` and `config_validator.py`.

### 3. Backward Compatibility Shims (network_mode.py)

**Location**: `sandbox/egg_lib/network_mode.py:36-75`

Two functions are documented as kept for backward compatibility:

- `get_private_mode_env_vars()` (lines 36-51): "These are no longer used for gateway configuration"
- `get_gateway_current_mode()` (lines 54-75): "gateway now always runs in locked mode"

Both have clear comments explaining they're kept for compatibility but are no longer part of the main code path.

### 4. Legacy API Key File Fallback (auth.py)

**Location**: `sandbox/egg_lib/auth.py:61-64`

The `get_anthropic_api_key()` function has a legacy fallback:
```python
# Legacy: check dedicated file
api_key_file = Config.USER_CONFIG_DIR / "anthropic-api-key"
if api_key_file.exists():
    return api_key_file.read_text().strip()
```

This supports users who haven't migrated to `secrets.env`. Test coverage exists at `tests/sandbox/test_auth.py:76-80`.

### 5. LLM Module Backward Compatibility Aliases

Multiple files re-export symbols for backward compatibility:

| File | Aliases |
|------|---------|
| `sandbox/llm/config.py` | `LLMConfig = ClaudeConfig`, `BaseConfig = ClaudeConfig` |
| `sandbox/llm/__init__.py` | `LLMConfig = ClaudeConfig`, `BaseConfig = ClaudeConfig` |
| `sandbox/llm/result.py:29` | `ClaudeResult = AgentResult` |
| `sandbox/llm/claude/config.py:33` | `AgentConfig = ClaudeConfig` |

These aliases exist so existing code importing the old names continues to work.

### 6. egg_config Legacy Exports

**Location**: `shared/egg_config/__init__.py`

The docstring (lines 26-28) documents legacy exports:
```python
Legacy exports (deprecated):
    - Config: Use BaseConfig instead
    - get_local_repos, get_repos_config_file: Moving to dedicated config classes
```

The `__all__` list (line 75) marks `Config` as "Legacy (deprecated)".

### 7. Legacy Plan Parser Formats

**Location**: `shared/egg_contracts/plan_parser.py`

The parser supports three formats with decreasing priority:
1. YAML code fence with `# yaml-tasks` marker (preferred)
2. YAML front matter (legacy, lines 14-15)
3. Markdown regex with `[TASK-...]` markers (fallback)

The legacy formats are maintained for backward compatibility with existing plan documents.

### 8. Legacy Path in Git Validation

**Location**: `gateway/git_client.py:123`
```python
"/repos/",  # Legacy path
```

Also tested at `gateway/tests/test_git_validation.py:39-40` as "legacy repos path".

### 9. Legacy OAuth Token Fallback

**Location**: `gateway/anthropic_credentials.py:10,154`

Supports both `CLAUDE_CODE_OAUTH_TOKEN` (preferred) and `ANTHROPIC_OAUTH_TOKEN` (legacy) for backward compatibility.

### 10. Backwards Compatibility for Sessions Without Roles

**Location**: `gateway/agent_restrictions.py:353` and `gateway/tests/test_gateway.py:773-867`

When session role is unavailable, file restrictions are skipped for backwards compatibility with legacy sessions. Extensively tested.

## Constraints

1. **User impact**: Removing legacy fallbacks affects users who haven't migrated configurations
2. **Test coverage**: Most legacy paths have explicit test coverage, indicating intentional support
3. **No breaking changes to external APIs**: The deprecated gateway scripts are still used for manual debugging
4. **Documentation accuracy**: ADRs and guides reference some deprecated patterns

## Options Considered

### Option A: Conservative Cleanup (Recommended)

**Approach**: Remove only clearly dead code with no legitimate use cases, preserve intentional backward compatibility shims

**Changes**:
1. **Delete deprecated shell scripts** if Docker Compose is stable enough:
   - `gateway/setup.sh` (407 lines)
   - `gateway/start-gateway.sh` (348 lines)
   - `gateway/create-networks.sh` (133 lines)
2. **Remove `is_private_mode_enabled()` function** if `fork_policy.py` and `config_validator.py` are updated to use session-based mode
3. **Consolidate LLM aliases** by removing intermediate re-export modules (`sandbox/llm/config.py` is only 14 lines of aliases)

**Preserve**:
- Legacy API key file fallback (supports users in transition)
- Network mode backward compat functions (minimal maintenance burden, documented)
- Plan parser legacy formats (documents exist in wild using old formats)
- OAuth token fallback (minimal code, prevents user lockout)
- Session role fallbacks (safety net for edge cases)

**Pros**:
- Removes ~900 lines of deprecated shell scripts
- Low risk of breaking user workflows
- Maintains migration paths for users

**Cons**:
- Keeps some dead code (minimal maintenance burden)

### Option B: Aggressive Cleanup

**Approach**: Remove all deprecated code and backward compatibility shims

**Changes**: Everything in Option A, plus:
- Remove legacy API key file support
- Remove network mode compat functions
- Remove all LLM aliases (force callers to update)
- Remove legacy plan parser formats
- Remove legacy OAuth token fallback
- Remove session role fallbacks

**Pros**:
- Cleanest possible codebase
- No maintenance burden for legacy paths

**Cons**:
- High risk of breaking existing user configurations
- Requires migration tooling/documentation
- Plan documents in existing issues may fail to parse
- Not necessary for v1 (internal tool)

### Option C: Documentation-Only

**Approach**: Document all deprecated code but don't remove anything

**Changes**:
- Add `DEPRECATED.md` documenting all legacy code
- Add removal timeline for each item
- Update CLAUDE.md with deprecation warnings

**Pros**:
- Zero risk of breaking anything
- Clear communication of intent

**Cons**:
- Doesn't reduce maintenance burden
- Dead code remains in codebase

## Recommended Approach

**Option A: Conservative Cleanup** is recommended.

The deprecated shell scripts are the largest source of dead code (~900 lines) and have clear replacements. The remaining backward compatibility code is:
- Minimal in size (typically 5-15 lines each)
- Explicitly documented with "backward compatibility" comments
- Often has test coverage proving intentional support
- Low maintenance burden

For a v1 release, removing user-facing fallbacks (like legacy API key file support) would create friction without meaningful benefit. The shell scripts, however, provide no unique value since Docker Compose handles the same functionality.

## Open Questions

**Q1: Should we remove the deprecated gateway shell scripts?**

These scripts (setup.sh, start-gateway.sh, create-networks.sh) total ~888 lines and are superseded by Docker Compose. However, they may be used for:
- Manual debugging
- Users not using Docker Compose
- CI/CD pipelines that haven't migrated

```bash
egg-contract add-decision --question "Should we delete the deprecated gateway shell scripts?" \
  --options "Yes, delete all three scripts" "Keep for debugging, add deprecation docs" "Defer to post-v1" --format markdown
```

**Q2: Should we consolidate LLM module aliases?**

The `sandbox/llm/config.py` file is only 14 lines and exists solely to re-export `ClaudeConfig` under alternate names. This could be removed if callers are updated.

```bash
egg-contract add-decision --question "Should we remove llm/config.py and require callers to use llm.claude.config?" \
  --options "Yes, update callers and delete" "No, keep for compatibility" --format markdown
```

## Files to Modify (If Option A is approved)

### Delete Entirely (if Q1 approved)
- `gateway/setup.sh` (407 lines)
- `gateway/start-gateway.sh` (348 lines)
- `gateway/create-networks.sh` (133 lines)

### Delete Conditionally (if Q2 approved)
- `sandbox/llm/config.py` (14 lines)

### Update References
- `gateway/setup.sh` is referenced in deprecation notice of `start-gateway.sh`
- `docs/guides/deploy-migration.md` references these scripts

## Validation

Before any removal:
1. Verify Docker Compose deployment works for all use cases covered by shell scripts
2. Search for script references in documentation and update
3. Run full test suite to catch any unexpected dependencies

---

*Authored-by: egg*
