# Analysis: Optimize egg LLM calls

> Issue: #388 | Phase: refine

## Problem Statement

The system is hitting daily rate limits within a few hours of usage, indicating excessive token consumption. The user wants to reduce LLM API costs and usage without degrading output quality. This requires a systematic review of all LLM invocations to identify optimization opportunities.

**Current state**: All 10 workflow automations default to Opus (the most expensive model), with only 2 exceptions using cheaper models. No caching mechanisms exist for repeated context.

**Desired outcome**: Reduce token usage by 30-50% through tiered model selection and prompt caching, while maintaining output quality for critical workflows.

## Current Behavior

### Model Usage Distribution

The system has 11 build scripts that generate prompts for LLM workflows:

| Workflow | Build Script | Model | Purpose |
|----------|-------------|-------|---------|
| Code Review | `build-review-prompt.sh:165` | **opus** | PR code reviews |
| Design Review | `build-agent-mode-design-review-prompt.sh:157` | **opus** | Agent-mode design alignment |
| Autofixer | `build-autofixer-prompt.sh:113` | **opus** | Fix failing CI checks |
| Conflict Resolution | `build-conflict-prompt.sh:116` | **opus** | Resolve git merge conflicts |
| Contract Verification | `build-contract-verification-prompt.sh:208` | **opus** | Verify SDLC contracts |
| Feedback Response | `build-feedback-prompt.sh:67` | **opus** | Generate review feedback |
| Plan Review | `build-plan-review-prompt.sh:206` | **opus** | Review implementation plans |
| Refine Review | `build-refine-review-prompt.sh:197` | **opus** | Review analysis quality |
| SDLC Pipeline | `build-sdlc-prompt.sh` | **opus** (default) | Phase-specific prompts |
| **Doc Updater** | `build-doc-updater-prompt.sh:305` | **sonnet** | Analyze code for doc updates |
| Doc Updater (no-op) | `build-doc-updater-prompt.sh:153` | **haiku** | When no code files changed |
| Mention Handler | `build-mention-prompt.sh` | **opus** (assumed) | Handle @mentions |

**Key finding**: 9 out of 11 workflows use Opus, even for tasks that don't require Opus-level reasoning capability.

### LLM Runner Implementation

The core LLM invocation happens in `sandbox/llm/claude/runner.py`:

```python
# Line 27
DEFAULT_MODEL = "opus"

# Line 252 - model fallback
model = model or DEFAULT_MODEL
```

All Claude Code CLI invocations use the `--model` flag with the model specified by build scripts or the default.

### Token Counting Support (Unused)

The gateway already has a token counting endpoint at `gateway/gateway.py:2683`:

```python
@app.route("/v1/messages/count_tokens", methods=["POST"])
def proxy_count_tokens() -> tuple[Response, int] | Response:
```

This capability is not currently used for observability or optimization decisions.

### No Caching Infrastructure

There is no prompt caching, response caching, or context reuse mechanism in the current implementation. Each workflow invocation builds the prompt from scratch and receives a full response.

## Constraints

### Technical Constraints
- **Claude API prompt caching**: Anthropic's prompt caching requires static prefixes that are identical across calls. Our prompts have dynamic elements (PR numbers, issue numbers, file contents).
- **Model capability requirements**: Code reviews, security analysis, and complex reasoning tasks genuinely benefit from Opus capabilities.
- **Non-interactive mode**: Workflows run in CI/GitHub Actions with `--print` mode, limiting real-time optimization decisions.
- **Gateway sidecar**: All API calls route through the gateway, which is the right place to add observability.

### Business Constraints
- **Quality requirement**: The issue explicitly states "we should not reduce output quality as part of this effort."
- **Incremental deployment**: Changes should be testable and reversible.

### Dependencies
- Claude Code CLI for agent execution
- GitHub Actions workflows for automation
- Gateway sidecar for API proxying

## Options Considered

### Option A: Tiered Model Selection by Workflow Type

**Approach**: Categorize workflows by reasoning complexity and assign appropriate models:
- **Haiku** ($0.80/M input, $4/M output): No-op tasks, health checks, simple formatting
- **Sonnet** ($3/M input, $15/M output): Analysis tasks, internal reviews, doc updates
- **Opus** ($15/M input, $75/M output): Code reviews, security analysis, implementation

**Pros**:
- Immediate impact with minimal code changes (update model assignments in build scripts)
- Preserves quality for critical workflows (code review, security)
- Already proven with doc-updater (uses Sonnet) and health checks (uses Haiku)
- Easy to test and roll back

**Cons**:
- Requires judgment calls on which workflows can use cheaper models
- May need per-workflow tuning based on observed quality

**Estimated savings**: 40-60% cost reduction on non-critical workflows

### Option B: Anthropic Prompt Caching for Static Context

**Approach**: Restructure prompts to maximize cache hits:
1. Move static instructions (review rules, guidelines) to a cacheable prefix
2. Keep dynamic content (PR diff, issue body) in the non-cached suffix
3. Use Anthropic's `cache_control` API parameter

**Pros**:
- 90% cost reduction on cached tokens (static context is large)
- No quality impact since all content is still sent
- Benefits all workflows with shared guidelines

**Cons**:
- Requires prompt restructuring across all build scripts
- Cache expiration (5 minutes) may not align with workflow patterns
- Claude Code CLI may not expose cache_control parameter directly

**Estimated savings**: 20-40% on input tokens if static content is ~30-50% of prompt

### Option C: Response Caching for Idempotent Checks

**Approach**: Cache review verdicts for unchanged code:
1. Hash the relevant inputs (commit SHA, diff content)
2. Skip re-review if cached verdict exists for same hash
3. Store cache in `.egg-state/cache/` with TTL

**Pros**:
- Eliminates redundant API calls entirely (100% savings for cache hits)
- Especially effective for re-reviews after comment-only changes

**Cons**:
- Complex cache invalidation logic
- Risk of stale reviews if cache logic has bugs
- Only benefits specific scenarios (re-reviews, unchanged code)

**Estimated savings**: Variable; 10-30% if re-reviews are common

### Option D: Token Usage Observability

**Approach**: Add instrumentation before optimizing:
1. Log token counts for each workflow invocation
2. Aggregate metrics to identify highest-usage workflows
3. Use data to prioritize optimization efforts

**Pros**:
- Data-driven decisions instead of guessing
- Identifies unexpected high-usage patterns
- Gateway already has token counting endpoint (`/v1/messages/count_tokens`)

**Cons**:
- Adds small overhead per call (token counting API)
- Delayed value (need data before acting)
- Doesn't directly reduce usage

**Estimated savings**: Indirect; enables better targeting of other optimizations

## Recommended Approach

**Start with Option A (Tiered Model Selection), then add Option D (Observability).**

### Rationale

1. **Immediate impact**: Tiered model selection provides the fastest path to meaningful cost reduction. Changing `model="opus"` to `model="sonnet"` in build scripts is a low-risk change.

2. **Proven pattern**: The doc-updater workflow already demonstrates this works—it uses Sonnet for analysis and Haiku for no-op cases without quality issues.

3. **Quality-preserving**: Keep Opus for the workflows where quality matters most:
   - Code review (security-critical, complex reasoning)
   - Design review (architectural decisions)
   - Conflict resolution (requires understanding both branches)

4. **Use Sonnet for internal reviews**: Plan review and refine review are internal quality gates where Sonnet's capabilities are sufficient:
   - They follow structured review criteria
   - They produce structured JSON output
   - They are followed by human review anyway

5. **Observability enables iteration**: Adding token counting to the gateway will provide data to validate savings and identify remaining optimization opportunities.

### Proposed Model Assignments

| Workflow | Current | Proposed | Rationale |
|----------|---------|----------|-----------|
| Code Review | opus | **opus** | Security-critical, keep highest capability |
| Design Review | opus | **opus** | Architectural decisions, keep highest capability |
| Conflict Resolution | opus | **opus** | Complex merge reasoning, keep |
| Autofixer | opus | **sonnet** | Mostly linting/formatting fixes, structured task |
| Contract Verification | opus | **sonnet** | Follows explicit criteria, structured output |
| Feedback Response | opus | **sonnet** | Responding to review comments, less complex |
| Plan Review | opus | **sonnet** | Internal review, structured criteria |
| Refine Review | opus | **sonnet** | Internal review, structured criteria |
| SDLC Pipeline | opus | opus/sonnet | Depends on phase complexity |
| Doc Updater | sonnet | **sonnet** | Already optimized |
| Doc Updater (no-op) | haiku | **haiku** | Already optimized |

**Estimated overall savings**: 35-45% reduction in token costs by moving 5 workflows from Opus to Sonnet.

### Implementation Phases

**Phase 1: Model Tiering (Low Risk)**
1. Update model assignments in build scripts
2. Monitor quality via existing review mechanisms
3. Rollback path: revert to opus if quality degrades

**Phase 2: Observability (Enabler)**
1. Add token counting instrumentation in gateway
2. Log metrics to understand usage patterns
3. Use data to identify remaining optimization targets

**Phase 3: Prompt Caching (Future, if needed)**
1. Restructure prompts with static prefixes
2. Requires Claude Code CLI support investigation
3. Higher complexity, defer unless Phase 1 insufficient

## Open Questions

### For Human Input

1. **Quality tolerance for internal reviews**: The plan/refine review workflows are internal quality gates before human review. Are you comfortable with Sonnet for these, given that humans will review the final output anyway?

2. **Autofixer complexity**: The autofixer prompt says "Use opus for autofixing (needs reasoning capability)" but the actual fixes are often straightforward (lint errors, type fixes). Should we try Sonnet for autofixer with a fallback mechanism if quality degrades?

3. **Observability priority**: Should we implement token counting observability first (before any model changes) to establish a baseline, or proceed with model tiering immediately for faster impact?

<!-- HITL Decision -->
```
egg-contract add-decision --question "What is the priority for implementing token observability?" \
  --options "Observability first (measure before changing)" "Model tiering first (faster impact)" "Implement both in parallel" --format markdown
```

---

*Authored-by: egg*
