# Analysis: Optimize egg LLM calls

> Issue: #388 | Phase: refine

## Problem Statement

The user is hitting daily rate limits within a few hours of usage. This indicates excessive token consumption across the egg agent system. The goal is to reduce token usage without sacrificing output quality by:
1. Auditing current model usage across all workflows
2. Identifying opportunities to use cheaper models (sonnet, haiku) where appropriate
3. Optimizing prompts and workflows to reduce unnecessary LLM calls
4. Removing any hardcoded model version pins

## Current Behavior

### Model Selection by Component

Based on the codebase audit, here is the current model usage:

| Component | Model Used | Location | Purpose |
|-----------|------------|----------|---------|
| **Main Agent Runner** | `opus` (alias) | `sandbox/llm/claude/runner.py:27` | Core implementation tasks |
| **Interactive Runner** | `opus` (alias) | `sandbox/llm/runner.py:23` | Interactive sessions |
| **Entrypoint Config** | `opus` (alias) | `sandbox/entrypoint.py:687` | Default model setting |
| **CLI Runner** | `opus` (alias) | `sandbox/egg_lib/cli.py:276` | CLI-based invocation |
| **Health Check** | `claude-3-haiku-20240307` | `shared/egg_config/configs/llm.py:17` | API connectivity check |

### Model Selection by Workflow (build scripts)

| Workflow Script | Model | Rationale Given |
|-----------------|-------|-----------------|
| `build-review-prompt.sh` | `opus` | "needs thorough analysis" |
| `build-agent-mode-design-review-prompt.sh` | `opus` | "needs thorough analysis" |
| `build-agent-mode-design-review-prompt-workloop.sh` | `sonnet` | "specialized check" |
| `build-unified-review-prompt.sh` | `opus` | "consistent with existing" |
| `build-code-review-prompt-workloop.sh` | `opus` | "needs thorough analysis" |
| `build-contract-verification-prompt.sh` | `opus` | "needs thorough reasoning" |
| `build-contract-verification-prompt-workloop.sh` | `opus` | "needs thorough reasoning" |
| `build-conflict-prompt.sh` | `opus` | "needs reasoning for merges" |
| `build-feedback-prompt.sh` | `opus` | "needs reasoning capability" |
| `build-autofixer-prompt.sh` | `opus` | "needs reasoning capability" |
| `build-doc-updater-prompt.sh` | `sonnet` or `haiku` | "good balance" / "no changes needed" |
| `build-coder-prompt.sh` | *(not specified)* | Uses default (opus) |
| `build-tester-prompt.sh` | *(not specified)* | Uses default (opus) |
| `build-documenter-prompt.sh` | *(not specified)* | Uses default (opus) |
| `build-integrator-prompt.sh` | *(not specified)* | Uses default (opus) |
| `build-sdlc-prompt.sh` | *(not specified)* | Uses default (opus) |
| `build-mention-prompt.sh` | *(not specified)* | Uses default (opus) |

### Integration Tests

| Test File | Model | Purpose |
|-----------|-------|---------|
| `integration_tests/conftest.py:555` | `sonnet` | Default for test fixtures |
| `integration_tests/test_agent_security_fuzz.py:33` | `sonnet` | Security fuzz testing |

### Hardcoded Version Strings (Potential Issues)

| Location | Version String | Context |
|----------|----------------|---------|
| `shared/egg_config/configs/llm.py:17` | `claude-3-haiku-20240307` | Health check model - **hardcoded** |
| Test files (multiple) | `claude-opus-4-5-20251101` | Test fixtures only |
| Schema examples | `claude-opus-4-5-20251101` | Documentation only |

**Finding**: Only the health check model has a hardcoded version string that affects runtime. All other production code uses aliases (`opus`, `sonnet`, `haiku`).

### Cost Model

Current Anthropic pricing (USD per million tokens):

| Model | Input | Output | Cache Read (0.1x) | Cache Write (1.25x) |
|-------|-------|--------|--------------------|---------------------|
| **Opus 4.5/4.6** | $5.00 | $25.00 | $0.50 | $6.25 |
| **Sonnet 4.5** | $3.00 | $15.00 | $0.30 | $3.75 |
| **Haiku 4.5** | $1.00 | $5.00 | $0.10 | $1.25 |

Relative savings vs Opus 4.6:
- Sonnet 4.5: ~40% cheaper
- Haiku 4.5: ~80% cheaper

> **Note**: `shared/egg_contracts/usage.py` previously hardcoded legacy Opus 4.1 rates ($15/$75), overstating costs by 3x. Fixed in #539.

## Constraints

- **Quality constraint**: Output quality must not degrade - the user explicitly stated this
- **Architecture constraint**: Model selection happens at multiple layers (runner, build scripts, entrypoint)
- **Alias system**: Claude Code CLI handles alias resolution, so we can use `opus`, `sonnet`, `haiku` without version pins
- **Caching**: The cost model tracks cache tokens but no prompt caching (`cache_control` headers) is currently implemented
- **Rate limits**: Daily rate limits suggest the issue is total request volume/tokens, not concurrent requests

## Options Considered

### Option A: Selective Model Downgrade by Task Type

**Approach**: Downgrade specific workflows to sonnet or haiku based on task complexity requirements. Keep opus for tasks requiring deep reasoning.

**Proposed Model Assignments**:

| Task Category | Recommended Model | Rationale |
|---------------|------------------|-----------|
| **Core implementation** (coder, sdlc-implement) | `opus` | Requires deep reasoning, code understanding |
| **Analysis/Planning** (refine, plan phases) | `opus` | Requires architectural thinking |
| **Code review** (initial review) | `opus` | Quality-critical, catches bugs |
| **Code review workloop** (follow-up cycles) | `sonnet` | Iterative fixes, narrower scope |
| **Contract verification** | `sonnet` | Checklist-based verification |
| **Documentation updates** | `sonnet` | Prose generation, less reasoning |
| **Tester agent** | `sonnet` | Test generation follows patterns |
| **Autofixer** | `sonnet` | Targeted fixes, narrow scope |
| **Conflict resolution** | `opus` | Complex merge reasoning |
| **Health check** | `haiku` | Minimal API verification |
| **Doc updater (no changes)** | `haiku` | Early exit path |

**Pros**:
- Meaningful cost reduction (est. 20-30% based on the ~40% Opus-to-Sonnet gap and task distribution)
- Preserves quality for high-stakes tasks
- Minimal code changes (model parameter in build scripts)
- Easy to roll back if quality issues arise

**Cons**:
- Requires judgment calls on which tasks can use cheaper models
- May need iteration to find right model for each task
- Some edge cases may need opus but get sonnet

### Option B: Implement Prompt Caching

**Approach**: Add `cache_control` headers to system prompts and stable context to leverage Anthropic's prompt caching feature.

**Implementation**:
1. Identify stable prompt sections (role descriptions, guidelines, templates)
2. Add `cache_control: {"type": "ephemeral"}` to these sections in API requests
3. Structure prompts with cacheable sections first

**Pros**:
- 90% cost reduction on cache hits for input tokens
- No quality impact - same prompts, just cached
- Works across all models

**Cons**:
- Requires changes to gateway/API proxy layer
- Cache has 5-minute TTL - may not help for long-running single tasks
- Most effective for repeated similar queries
- More complex implementation than model selection

### Option C: Workflow Consolidation

**Approach**: Reduce the number of LLM calls by combining related tasks or eliminating redundant calls.

**Potential Consolidations**:
1. **Review cycles**: Combine contract verification + code review into single pass
2. **Agent handoffs**: Reduce context rebuilding between coder → tester → documenter
3. **Iterative reviews**: Skip early review cycles, do deeper final review

**Pros**:
- Reduces total API calls, not just per-call cost
- Simpler pipeline with fewer moving parts

**Cons**:
- Architecture changes required
- Risk of reducing feedback granularity
- May introduce regressions in pipeline behavior
- Larger scope of changes

### Option D: Hybrid Approach (Recommended)

**Approach**: Combine selective model downgrade (Option A) with targeted prompt caching (Option B) for maximum impact with manageable risk.

**Phase 1 - Model Selection** (immediate):
- Update build scripts to specify appropriate models per task type
- Add model parameter to scripts that currently rely on default

**Phase 2 - Prompt Caching** (follow-up):
- Implement cache_control headers in gateway for system prompts
- Structure prompts to maximize cache hit rate

**Pros**:
- Captures most benefits with staged rollout
- Phase 1 is low-risk and quick to implement
- Phase 2 provides additional gains without blocking Phase 1

**Cons**:
- More work overall than a single approach
- Requires two implementation cycles

## Recommended Approach

**Recommendation**: Option D (Hybrid Approach), starting with Phase 1 (Model Selection).

**Justification**:
1. Model selection changes are low-risk and reversible
2. Estimated 20-30% cost reduction from Phase 1 alone
3. No architectural changes required for Phase 1
4. Phase 2 (caching) can be evaluated after Phase 1 results are measured

### Proposed Model Assignments (Phase 1)

```yaml
# High-stakes tasks - keep opus
opus:
  - build-coder-prompt.sh          # Core implementation
  - build-sdlc-prompt.sh           # SDLC phases (refine, plan, implement, pr)
  - build-review-prompt.sh         # Initial code review
  - build-conflict-prompt.sh       # Merge conflict resolution
  - build-mention-prompt.sh        # Human interaction responses

# Medium complexity - downgrade to sonnet
sonnet:
  - build-tester-prompt.sh         # Test generation
  - build-documenter-prompt.sh     # Documentation
  - build-integrator-prompt.sh     # Integration tasks
  - build-autofixer-prompt.sh      # Targeted fixes
  - build-feedback-prompt.sh       # Feedback responses
  - build-code-review-prompt-workloop.sh       # Iterative review
  - build-contract-verification-prompt.sh      # Verification
  - build-contract-verification-prompt-workloop.sh
  - build-agent-mode-design-review-prompt.sh   # Design review
  - build-agent-mode-design-review-prompt-workloop.sh
  - build-unified-review-prompt.sh             # Unified review

# Low complexity - use haiku
haiku:
  - build-doc-updater-prompt.sh (no-changes path)  # Already using haiku
  - Health check                                    # Already using haiku
```

### Health Check Model Version

The health check model at `shared/egg_config/configs/llm.py:17` is hardcoded to `claude-3-haiku-20240307`. This should be updated to use the alias `haiku` for future-proofing, though this is low priority since it only affects health checks.

## Implementation Details

### Files to Modify

1. **Build scripts** (add explicit model output):
   - `action/build-tester-prompt.sh` - add `model=sonnet`
   - `action/build-documenter-prompt.sh` - add `model=sonnet`
   - `action/build-integrator-prompt.sh` - add `model=sonnet`
   - `action/build-coder-prompt.sh` - add `model=opus` (explicit)
   - `action/build-sdlc-prompt.sh` - add `model=opus` (explicit)
   - `action/build-mention-prompt.sh` - add `model=opus` (explicit)
   - `action/build-autofixer-prompt.sh` - change to `model=sonnet`
   - `action/build-feedback-prompt.sh` - change to `model=sonnet`
   - `action/build-contract-verification-prompt.sh` - change to `model=sonnet`
   - `action/build-contract-verification-prompt-workloop.sh` - change to `model=sonnet`
   - `action/build-unified-review-prompt.sh` - change to `model=sonnet`

2. **Health check** (optional, low priority):
   - `shared/egg_config/configs/llm.py:17` - change to alias `haiku`

### Testing Strategy

1. Run integration tests with new model assignments
2. Monitor token usage/cost metrics for 1 week
3. Review output quality for downgraded tasks
4. Adjust assignments if quality issues are found

## Open Questions

These questions require human input to finalize the implementation approach:

1. **Quality tolerance**: For tasks downgraded to sonnet, is there a quality threshold below which we should revert to opus? How should this be measured?

2. **SDLC phase granularity**: The `build-sdlc-prompt.sh` handles all four phases (refine, plan, implement, pr). Should these remain unified at opus, or should some phases (like pr which mostly generates PR descriptions) use a cheaper model?

3. **Review quality priority**: Code reviews are currently opus. Given that reviews are critical for catching issues, should we:
   - Keep all reviews at opus? (conservative, higher cost)
   - Only use opus for first review, sonnet for follow-ups? (proposed)
   - Use sonnet for all reviews? (aggressive, lowest cost)

4. **Measurement baseline**: Is there existing token usage data we can use as a baseline to measure the impact of these changes?

---

*Authored-by: egg*
