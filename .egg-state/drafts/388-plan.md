# Plan: Optimize egg LLM calls

> Issue: #388 | Phase: plan

## Summary

This plan implements Option D (Hybrid Approach) to reduce token usage and address daily rate limit exhaustion. Phase 1 adds explicit model selection to build scripts, using sonnet for lower-complexity workflows while keeping opus for quality-critical tasks. Phase 2 implements prompt caching in the gateway to achieve additional savings on repeated context. Based on human guidance: reviews, implementation, plans, and refinement stay on opus; specialized reviewers can use sonnet; simple tasks use simpler models.

## Implementation Phases

### Phase 1: Model Selection Updates

**Goal**: Add explicit model selection to all prompt builder scripts, downgrading appropriate workflows to sonnet while preserving opus for quality-critical tasks.

**Tasks**:
- [TASK-1-1] Add model output to agent prompt builders — Acceptance: `build-coder-prompt.sh`, `build-tester-prompt.sh`, `build-documenter-prompt.sh`, `build-integrator-prompt.sh` all output `model=` to GITHUB_OUTPUT
- [TASK-1-2] Add model output to SDLC and mention prompts — Acceptance: `build-sdlc-prompt.sh` and `build-mention-prompt.sh` output `model=opus` explicitly
- [TASK-1-3] Downgrade specialized reviewers to sonnet — Acceptance: `build-agent-mode-design-review-prompt.sh` outputs `model=sonnet`; workloop variant already uses sonnet
- [TASK-1-4] Downgrade autofixer and feedback to sonnet — Acceptance: `build-autofixer-prompt.sh` and `build-feedback-prompt.sh` output `model=sonnet`
- [TASK-1-5] Keep contract verification at opus (conservative) — Acceptance: Verify `build-contract-verification-prompt.sh` remains at opus per human guidance
- [TASK-1-6] Keep code review at opus — Acceptance: `build-review-prompt.sh` and `build-unified-review-prompt.sh` remain at opus
- [TASK-1-7] Update health check to use alias — Acceptance: `shared/egg_config/configs/llm.py` uses `haiku` alias instead of hardcoded version string

**Dependencies**: None

**Exit criteria**: All build scripts explicitly output a model selection; no scripts rely on implicit defaults; tests pass

### Phase 2: Prompt Caching Implementation

**Goal**: Implement Anthropic's prompt caching feature in the gateway to achieve up to 90% cost reduction on cached input tokens.

**Tasks**:
- [TASK-2-1] Add cache_control injection to gateway proxy — Acceptance: Gateway modifies outgoing requests to add `cache_control` to system prompt content blocks
- [TASK-2-2] Create configuration for cacheable prompts — Acceptance: Config file or environment variable controls which prompt types get cache headers
- [TASK-2-3] Structure prompts for optimal caching — Acceptance: System prompts are structured with stable content first (role, guidelines) followed by dynamic content (context, files)
- [TASK-2-4] Add cache metrics to transcript logging — Acceptance: Transcript buffer captures cache_read_input_tokens and cache_creation_input_tokens with meaningful labels
- [TASK-2-5] Add cache hit rate monitoring — Acceptance: Usage tracking includes cache hit rate percentage per workflow type

**Dependencies**: Phase 1 (model selection should be stable before adding caching complexity)

**Exit criteria**: Cache tokens appear in usage tracking; cache hit rates are visible in monitoring

### Phase 3: Validation and Monitoring

**Goal**: Verify cost reduction without quality degradation; establish ongoing monitoring.

**Tasks**:
- [TASK-3-1] Run integration test suite with new model assignments — Acceptance: All existing integration tests pass
- [TASK-3-2] Manual quality spot-check of sonnet outputs — Acceptance: Sampled outputs from downgraded workflows meet quality bar
- [TASK-3-3] Document model assignments in CLAUDE.md — Acceptance: CLAUDE.md includes rationale for model selection per workflow
- [TASK-3-4] Create usage comparison report — Acceptance: Report shows token usage before/after changes

**Dependencies**: Phase 1, Phase 2

**Exit criteria**: Tests pass; quality verified; documentation updated

## Test Strategy

- **Unit tests**: Verify each build script outputs the expected model value; test gateway cache_control injection logic
- **Integration tests**: Run full SDLC pipeline on a test issue; verify workflows complete successfully with new model assignments
- **Manual testing**:
  1. Trigger each downgraded workflow manually and review output quality
  2. Verify cache tokens appear in usage tracking after Phase 2
  3. Compare token costs for similar tasks before and after changes

## Rollback Plan

**Phase 1 rollback**: Revert model parameter changes in build scripts. Each script change is isolated and can be individually reverted.

```bash
# Revert specific file
git checkout origin/main -- action/build-tester-prompt.sh

# Or revert entire phase
git revert <phase-1-commit-hash>
```

**Phase 2 rollback**: Disable cache injection via configuration or remove the cache_control injection code path. The gateway should have a feature flag to disable caching.

```bash
# Set environment variable to disable
export EGG_PROMPT_CACHING_ENABLED=false
```

**Emergency rollback**: If quality issues are detected in production:
1. Create hotfix branch
2. Revert to opus for affected workflow
3. Push immediately (no review required for revert)

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Quality degradation from sonnet | Medium | High | Keep quality-critical tasks (reviews, implementation, planning) on opus; only downgrade specialized/simple tasks |
| Cache miss rate too high | Medium | Low | Cache provides additive benefit; worst case is no savings, not degradation |
| Test failures from model change | Low | Medium | Run full test suite before merging; models should be interchangeable for correctness |
| Prompt structure changes break caching | Low | Low | Cache failures are silent (fall back to non-cached); no functional impact |
| Rate limits still exceeded | Low | Medium | This addresses token volume; if rate limits persist, may need to investigate call frequency |

## Migration Notes

No database migrations required.

**Configuration changes**:
- New environment variable `EGG_PROMPT_CACHING_ENABLED` (default: true) controls Phase 2 caching
- Health check model changes from hardcoded `claude-3-haiku-20240307` to alias `haiku`

**Breaking changes**: None. Model selection is additive; existing workflows continue to work with explicit model output.

**Backwards compatibility**: Build scripts that don't output a model will still use the entrypoint default (opus). This is intentional for gradual rollout.

---

## Model Assignment Summary

Based on human guidance ("reviews, implementation, plans, and refinement should all use opus; specialized reviewers can be sonnet"):

| Workflow | Model | Rationale |
|----------|-------|-----------|
| `build-coder-prompt.sh` | opus | Core implementation (human requirement) |
| `build-sdlc-prompt.sh` | opus | Contains refine, plan, implement phases (human requirement) |
| `build-review-prompt.sh` | opus | Code review quality-critical (human requirement) |
| `build-unified-review-prompt.sh` | opus | Code review quality-critical (human requirement) |
| `build-contract-verification-prompt.sh` | opus | Contract verification needs reasoning (conservative) |
| `build-conflict-prompt.sh` | opus | Merge conflicts need deep reasoning |
| `build-mention-prompt.sh` | opus | Human interaction responses need quality |
| `build-tester-prompt.sh` | sonnet | Test generation follows patterns |
| `build-documenter-prompt.sh` | sonnet | Documentation is simpler task |
| `build-integrator-prompt.sh` | sonnet | Integration validation is narrower scope |
| `build-agent-mode-design-review-prompt.sh` | sonnet | Specialized reviewer (human approved) |
| `build-autofixer-prompt.sh` | sonnet | Targeted fixes, narrow scope |
| `build-feedback-prompt.sh` | sonnet | Feedback responses, narrower scope |
| `build-doc-updater-prompt.sh` | sonnet/haiku | Already optimized (no changes) |

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Optimize LLM calls with model selection and prompt caching"
  description: |
    Reduces token usage to address daily rate limit exhaustion. Implements selective
    model downgrading (sonnet for simpler tasks) and prompt caching in the gateway.
    Quality-critical workflows (reviews, implementation, planning) remain on opus.

    Closes #388
phases:
  - id: 1
    name: Model Selection Updates
    goal: Add explicit model selection to all prompt builder scripts
    tasks:
      - id: TASK-1-1
        description: Add model output to agent prompt builders (coder, tester, documenter, integrator)
        acceptance: All four scripts output model= to GITHUB_OUTPUT
        files:
          - action/build-coder-prompt.sh
          - action/build-tester-prompt.sh
          - action/build-documenter-prompt.sh
          - action/build-integrator-prompt.sh
      - id: TASK-1-2
        description: Add model output to SDLC and mention prompts
        acceptance: Both scripts output model=opus explicitly
        files:
          - action/build-sdlc-prompt.sh
          - action/build-mention-prompt.sh
      - id: TASK-1-3
        description: Downgrade specialized reviewers to sonnet
        acceptance: build-agent-mode-design-review-prompt.sh outputs model=sonnet
        files:
          - action/build-agent-mode-design-review-prompt.sh
      - id: TASK-1-4
        description: Downgrade autofixer and feedback to sonnet
        acceptance: Both scripts output model=sonnet
        files:
          - action/build-autofixer-prompt.sh
          - action/build-feedback-prompt.sh
      - id: TASK-1-5
        description: Keep contract verification at opus (verify, no change needed)
        acceptance: build-contract-verification-prompt.sh remains at opus
        files:
          - action/build-contract-verification-prompt.sh
      - id: TASK-1-6
        description: Keep code review at opus (verify, no change needed)
        acceptance: build-review-prompt.sh and build-unified-review-prompt.sh remain at opus
        files:
          - action/build-review-prompt.sh
          - action/build-unified-review-prompt.sh
      - id: TASK-1-7
        description: Update health check to use haiku alias instead of hardcoded version
        acceptance: llm.py uses haiku alias
        files:
          - shared/egg_config/configs/llm.py
  - id: 2
    name: Prompt Caching Implementation
    goal: Implement Anthropic prompt caching in the gateway for 90% input token savings on cache hits
    tasks:
      - id: TASK-2-1
        description: Add cache_control injection to gateway proxy for system prompts
        acceptance: Gateway adds cache_control to system prompt content blocks
        files:
          - gateway/gateway.py
      - id: TASK-2-2
        description: Create configuration for cacheable prompts
        acceptance: EGG_PROMPT_CACHING_ENABLED env var controls caching
        files:
          - gateway/gateway.py
          - shared/egg_config/configs/llm.py
      - id: TASK-2-3
        description: Structure prompts for optimal caching (stable content first)
        acceptance: System prompts have role/guidelines before dynamic context
        files:
          - action/build-coder-prompt.sh
          - action/build-sdlc-prompt.sh
      - id: TASK-2-4
        description: Add cache metrics to transcript logging
        acceptance: Transcript captures cache tokens with meaningful labels
        files:
          - gateway/transcript_buffer.py
      - id: TASK-2-5
        description: Add cache hit rate monitoring to usage tracking
        acceptance: Usage tracking shows cache hit rate per workflow
        files:
          - shared/egg_contracts/usage.py
          - shared/egg_contracts/usage_loader.py
  - id: 3
    name: Validation and Monitoring
    goal: Verify cost reduction without quality degradation
    tasks:
      - id: TASK-3-1
        description: Run integration test suite with new model assignments
        acceptance: All integration tests pass
        files: []
      - id: TASK-3-2
        description: Manual quality spot-check of sonnet outputs
        acceptance: Sampled outputs meet quality bar
        files: []
      - id: TASK-3-3
        description: Document model assignments in CLAUDE.md
        acceptance: CLAUDE.md includes model selection rationale
        files:
          - CLAUDE.md
      - id: TASK-3-4
        description: Create usage comparison report
        acceptance: Report shows before/after token usage
        files: []
```

---

*Authored-by: egg*
