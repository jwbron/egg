# Plan: Audit dead or deprecated code

> Issue: #478 | Phase: plan

## Summary

This plan implements **Option B: Aggressive Cleanup** as approved by the repository owner. The cleanup removes approximately 900 lines of deprecated shell scripts, consolidates LLM module aliases, removes legacy fallbacks for API keys and OAuth tokens, and eliminates backward compatibility shims across the codebase. Since the owner is the only current user, the risk of breaking external workflows is minimal.

The implementation is organized into 5 phases: deprecated shell scripts removal, LLM module consolidation, gateway legacy code cleanup, sandbox legacy code cleanup, and documentation updates.

## Implementation Phases

### Phase 1: Remove Deprecated Shell Scripts

**Goal**: Delete the three deprecated gateway shell scripts (~888 lines total) that have been superseded by Docker Compose deployment.

**Tasks**:
- [TASK-1-1] Delete `gateway/setup.sh` (407 lines) — Acceptance: File removed, no dangling references in codebase
- [TASK-1-2] Delete `gateway/start-gateway.sh` (348 lines) — Acceptance: File removed, no dangling references
- [TASK-1-3] Delete `gateway/create-networks.sh` (133 lines) — Acceptance: File removed, no dangling references
- [TASK-1-4] Remove `bin/setup-gateway` symlink if it points to deleted script — Acceptance: Symlink removed or updated

**Dependencies**: None

**Exit criteria**: All three shell scripts deleted, no broken references in documentation or other scripts

### Phase 2: Consolidate LLM Module Aliases

**Goal**: Remove backward compatibility aliases in the LLM module, requiring callers to use canonical names directly.

**Tasks**:
- [TASK-2-1] Delete `sandbox/llm/config.py` (14 lines of pure re-exports) — Acceptance: File deleted, imports updated
- [TASK-2-2] Remove `LLMConfig` and `BaseConfig` aliases from `sandbox/llm/__init__.py` — Acceptance: Only `ClaudeConfig` exported
- [TASK-2-3] Remove `ClaudeResult` alias from `sandbox/llm/result.py` — Acceptance: Only `AgentResult` exported
- [TASK-2-4] Remove `AgentConfig` alias from `sandbox/llm/claude/config.py` — Acceptance: Only `ClaudeConfig` exported
- [TASK-2-5] Update `sandbox/llm/claude/__init__.py` to remove `ClaudeResult` export — Acceptance: Clean exports list

**Dependencies**: None

**Exit criteria**: All LLM alias files/lines removed, tests pass

### Phase 3: Gateway Legacy Code Cleanup

**Goal**: Remove deprecated functions and backward compatibility code from the gateway.

**Tasks**:
- [TASK-3-1] Remove `is_private_mode_enabled()` from `gateway/private_repo_policy.py` — Acceptance: Function deleted
- [TASK-3-2] Update `gateway/fork_policy.py` to remove `is_private_mode_enabled` import and usage — Acceptance: Uses session-based mode only
- [TASK-3-3] Remove duplicate `is_private_mode_enabled()` from `gateway/config_validator.py` — Acceptance: Function deleted, callers updated
- [TASK-3-4] Remove legacy `/repos/` path from `gateway/git_client.py` ALLOWED_REPO_PATHS — Acceptance: Only current paths remain
- [TASK-3-5] Remove `ANTHROPIC_OAUTH_TOKEN` legacy fallback from `gateway/anthropic_credentials.py` — Acceptance: Only `CLAUDE_CODE_OAUTH_TOKEN` supported
- [TASK-3-6] Update gateway tests to reflect removed deprecated code — Acceptance: All tests pass

**Dependencies**: None

**Exit criteria**: All deprecated gateway code removed, tests pass

### Phase 4: Sandbox Legacy Code Cleanup

**Goal**: Remove deprecated functions and backward compatibility code from the sandbox.

**Tasks**:
- [TASK-4-1] Remove `get_private_mode_env_vars()` from `sandbox/egg_lib/network_mode.py` — Acceptance: Function deleted
- [TASK-4-2] Remove `get_gateway_current_mode()` from `sandbox/egg_lib/network_mode.py` — Acceptance: Function deleted
- [TASK-4-3] Update `ensure_gateway_mode()` to not rely on removed functions — Acceptance: Function simplified, still works
- [TASK-4-4] Remove legacy `anthropic-api-key` file fallback from `sandbox/egg_lib/auth.py` — Acceptance: Only `secrets.env` supported
- [TASK-4-5] Update sandbox tests to reflect removed deprecated code — Acceptance: All tests pass

**Dependencies**: None

**Exit criteria**: All deprecated sandbox code removed, tests pass

### Phase 5: Shared Library and Documentation Cleanup

**Goal**: Remove deprecated exports from shared libraries, update plan parser, and update documentation.

**Tasks**:
- [TASK-5-1] Remove deprecated `Config` export comment from `shared/egg_config/__init__.py` — Acceptance: Clean docstring
- [TASK-5-2] Remove YAML front matter (legacy) parsing from `shared/egg_contracts/plan_parser.py` — Acceptance: Only yaml-tasks code fence and markdown regex supported
- [TASK-5-3] Update `docs/adr/implemented/ADR-Declarative-Setup-Architecture.md` to remove `anthropic-api-key` reference — Acceptance: Doc updated
- [TASK-5-4] Update `docs/adr/implemented/ADR-Gateway-Credential-Injection.md` to remove `ANTHROPIC_OAUTH_TOKEN` references — Acceptance: Doc updated
- [TASK-5-5] Update `config/secrets.template.env` to remove `ANTHROPIC_OAUTH_TOKEN` line — Acceptance: Template updated

**Dependencies**: Phases 1-4

**Exit criteria**: All documentation reflects current code state, no references to removed features

## Test Strategy

- **Unit tests**: Update existing tests that mock or test deprecated functions. Remove test classes for deleted functionality (e.g., `TestPrivateModeEnabled`, tests for legacy API key file).
- **Integration tests**: Verify Docker Compose deployment still works after shell script removal. Run full test suite to catch any regressions.
- **Manual testing**:
  1. Run `make test` to verify all unit tests pass
  2. Run `make lint` to verify no lint errors
  3. Verify `egg --compose` still works without deprecated scripts
  4. Verify secrets.env with only `CLAUDE_CODE_OAUTH_TOKEN` works

## Rollback Plan

If issues are discovered after merge:

1. **Git revert**: `git revert <commit-hash>` to restore all deleted code
2. **Cherry-pick partial**: If only specific removals cause issues, cherry-pick the revert of those specific commits
3. **Branch restore**: The `egg/issue-478` branch will contain the pre-merge state for reference

All changes are additive deletions with no database migrations or external dependencies, making rollback straightforward.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Deprecated shell scripts still used by CI/CD | Low | Medium | Search for script references before deletion, update any CI that calls them |
| LLM alias removal breaks internal code | Low | Low | Grep for all alias usages, update imports before removal |
| Legacy API key file still in use | Low | Low | Owner confirmed single-user; can re-add if needed |
| Plan parser breaks on legacy documents | Low | Medium | Keep markdown regex fallback; only remove YAML front matter |
| ANTHROPIC_OAUTH_TOKEN still referenced | Medium | Low | This is a GitHub Secret name, not code; update docs to clarify migration |

## Migration Notes

**For users upgrading from previous versions** (N/A since single user):

1. **API Key Configuration**: Move from `~/.config/egg/anthropic-api-key` file to `secrets.env` with `ANTHROPIC_API_KEY=...`
2. **OAuth Token**: Use `CLAUDE_CODE_OAUTH_TOKEN` instead of `ANTHROPIC_OAUTH_TOKEN` in `secrets.env`
3. **Gateway Scripts**: Use `egg --compose` or `docker compose up` instead of `setup.sh`/`start-gateway.sh`
4. **LLM Imports**: Update `from llm import LLMConfig` to `from llm import ClaudeConfig`

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Remove deprecated code and backward compatibility shims"
  description: |
    Comprehensive cleanup of dead code, deprecated functions, and backward
    compatibility shims in preparation for v1 release. Removes ~900 lines of
    deprecated shell scripts, consolidates LLM module aliases, and eliminates
    legacy fallbacks for API keys and OAuth tokens.

    Closes #478
phases:
  - id: 1
    name: Remove Deprecated Shell Scripts
    goal: Delete deprecated gateway shell scripts superseded by Docker Compose
    tasks:
      - id: TASK-1-1
        description: Delete gateway/setup.sh (407 lines)
        acceptance: File removed, no dangling references in codebase
        files:
          - gateway/setup.sh
      - id: TASK-1-2
        description: Delete gateway/start-gateway.sh (348 lines)
        acceptance: File removed, no dangling references
        files:
          - gateway/start-gateway.sh
      - id: TASK-1-3
        description: Delete gateway/create-networks.sh (133 lines)
        acceptance: File removed, no dangling references
        files:
          - gateway/create-networks.sh
      - id: TASK-1-4
        description: Remove bin/setup-gateway symlink if it points to deleted script
        acceptance: Symlink removed or updated
        files:
          - bin/setup-gateway
  - id: 2
    name: Consolidate LLM Module Aliases
    goal: Remove backward compatibility aliases in LLM module
    tasks:
      - id: TASK-2-1
        description: Delete sandbox/llm/config.py (pure re-export file)
        acceptance: File deleted, imports updated
        files:
          - sandbox/llm/config.py
      - id: TASK-2-2
        description: Remove LLMConfig and BaseConfig aliases from sandbox/llm/__init__.py
        acceptance: Only ClaudeConfig exported
        files:
          - sandbox/llm/__init__.py
      - id: TASK-2-3
        description: Remove ClaudeResult alias from sandbox/llm/result.py
        acceptance: Only AgentResult exported
        files:
          - sandbox/llm/result.py
      - id: TASK-2-4
        description: Remove AgentConfig alias from sandbox/llm/claude/config.py
        acceptance: Only ClaudeConfig exported
        files:
          - sandbox/llm/claude/config.py
      - id: TASK-2-5
        description: Update sandbox/llm/claude/__init__.py to remove ClaudeResult export
        acceptance: Clean exports list
        files:
          - sandbox/llm/claude/__init__.py
  - id: 3
    name: Gateway Legacy Code Cleanup
    goal: Remove deprecated functions and backward compatibility code from gateway
    tasks:
      - id: TASK-3-1
        description: Remove is_private_mode_enabled() from gateway/private_repo_policy.py
        acceptance: Function deleted
        files:
          - gateway/private_repo_policy.py
      - id: TASK-3-2
        description: Update gateway/fork_policy.py to remove is_private_mode_enabled usage
        acceptance: Uses session-based mode only
        files:
          - gateway/fork_policy.py
      - id: TASK-3-3
        description: Remove is_private_mode_enabled() from gateway/config_validator.py
        acceptance: Function deleted, callers updated
        files:
          - gateway/config_validator.py
      - id: TASK-3-4
        description: Remove legacy /repos/ path from gateway/git_client.py
        acceptance: Only current paths remain
        files:
          - gateway/git_client.py
      - id: TASK-3-5
        description: Remove ANTHROPIC_OAUTH_TOKEN legacy fallback from anthropic_credentials.py
        acceptance: Only CLAUDE_CODE_OAUTH_TOKEN supported
        files:
          - gateway/anthropic_credentials.py
      - id: TASK-3-6
        description: Update gateway tests to reflect removed deprecated code
        acceptance: All tests pass
        files:
          - gateway/tests/test_private_repo_policy.py
          - gateway/tests/test_fork_policy.py
          - gateway/tests/test_config_validator.py
          - gateway/tests/test_git_validation.py
          - gateway/tests/test_anthropic_credentials.py
  - id: 4
    name: Sandbox Legacy Code Cleanup
    goal: Remove deprecated functions and backward compatibility code from sandbox
    tasks:
      - id: TASK-4-1
        description: Remove get_private_mode_env_vars() from sandbox/egg_lib/network_mode.py
        acceptance: Function deleted
        files:
          - sandbox/egg_lib/network_mode.py
      - id: TASK-4-2
        description: Remove get_gateway_current_mode() from sandbox/egg_lib/network_mode.py
        acceptance: Function deleted
        files:
          - sandbox/egg_lib/network_mode.py
      - id: TASK-4-3
        description: Update ensure_gateway_mode() to not rely on removed functions
        acceptance: Function simplified, still works
        files:
          - sandbox/egg_lib/network_mode.py
      - id: TASK-4-4
        description: Remove legacy anthropic-api-key file fallback from auth.py
        acceptance: Only secrets.env supported
        files:
          - sandbox/egg_lib/auth.py
      - id: TASK-4-5
        description: Update sandbox tests to reflect removed deprecated code
        acceptance: All tests pass
        files:
          - tests/sandbox/test_network_mode.py
          - tests/sandbox/test_auth.py
  - id: 5
    name: Shared Library and Documentation Cleanup
    goal: Remove deprecated exports and update documentation
    tasks:
      - id: TASK-5-1
        description: Remove deprecated Config export comment from shared/egg_config/__init__.py
        acceptance: Clean docstring
        files:
          - shared/egg_config/__init__.py
      - id: TASK-5-2
        description: Remove YAML front matter parsing from plan_parser.py
        acceptance: Only yaml-tasks and markdown regex supported
        files:
          - shared/egg_contracts/plan_parser.py
      - id: TASK-5-3
        description: Update ADR-Declarative-Setup-Architecture.md to remove anthropic-api-key reference
        acceptance: Doc updated
        files:
          - docs/adr/implemented/ADR-Declarative-Setup-Architecture.md
      - id: TASK-5-4
        description: Update ADR-Gateway-Credential-Injection.md to remove ANTHROPIC_OAUTH_TOKEN references
        acceptance: Doc updated
        files:
          - docs/adr/implemented/ADR-Gateway-Credential-Injection.md
      - id: TASK-5-5
        description: Update secrets.template.env to remove ANTHROPIC_OAUTH_TOKEN line
        acceptance: Template updated
        files:
          - config/secrets.template.env
```

---

*Authored-by: egg*
