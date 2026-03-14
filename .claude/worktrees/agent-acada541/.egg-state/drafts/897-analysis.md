# Analysis: SDLC Pipeline Reviewers Produce Shallow Analysis Compared to PR Reviewers

> Issue: #897 | Phase: refine

## Problem Statement

The SDLC pipeline's internal review agents (code, contract, agent-design, refine, plan) produce significantly shallower analysis than the automated PR reviewers running on GitHub Actions, despite both systems consuming the same shared criteria files (`shared/prompts/code-review-criteria.md`, `shared/prompts/contract-review-criteria.md`, `shared/prompts/agent-design-criteria.md`).

Evidence from PR #895 (issue #871) shows SDLC reviewers writing ~1-3 sentence verdicts with `"feedback": ""` even when scope creep and real issues were present, while PR reviewers on the same code produced 20-row task tables, identified 5 specific code issues (driving a follow-up commit), and gave substantive multi-round reviews.

The desired outcome is SDLC reviewers producing at minimum the same depth of analysis as PR reviewers — with detailed file-by-file analysis, advisory suggestions on approved work, and actionable output rather than a pass/fail stamp.

## Current Behavior

### Verdict Schema Constrains Depth

The `ReviewVerdict` model (`orchestrator/models.py:97-103`) has four fields:

```python
class ReviewVerdict(BaseModel):
    verdict: str       # "approved" or "needs_revision"
    summary: str       # "Brief summary of review findings"
    feedback: str      # "Detailed feedback if needs_revision"
    timestamp: str     # ISO 8601 timestamp
```

The prompt at `orchestrator/routes/pipelines.py:1554-1570` instructs reviewers:

```json
"feedback": "Detailed feedback if needs_revision, empty if approved"
```

This **explicitly tells reviewers to write empty feedback when approving**. There is no field for advisory suggestions, non-blocking observations, or detailed analysis that should always be populated.

### Aggregation Ignores Approved Verdicts

`_aggregate_review_verdicts()` at `orchestrator/routes/pipelines.py:1712-1738` only collects feedback from `needs_revision` verdicts. If a reviewer approves with observations in `summary`, those observations are silently discarded — they are never surfaced to the implementing agent or the human.

### SDLC Prompt is Less Detailed Than PR Prompt

**PR code reviewer** (`action/build-review-prompt.sh`) gets:
- "Comprehensive, thorough code review" framing
- "Critical infrastructure — last line of defense before production"
- 6 detailed procedural steps
- Review conventions from `action/review-conventions.md` (comprehensive, specific, direct, suggest fixes, provide context)
- `<!-- has-suggestions -->` marker for approvals with advisory feedback

**SDLC code reviewer** (`orchestrator/routes/pipelines.py:1509-1526`) for non-draft reviews gets:
- 9 procedural steps (briefer than PR counterpart)
- Scope preamble with "Be thorough" and "Find ALL issues" (`pipelines.py:1014-1022`)
- **No review conventions** — `review-conventions.md` is not loaded
- No equivalent to `has-suggestions` — binary approved/needs_revision only

**SDLC non-code reviewers** (refine, plan, contract, agent-design) for draft-based reviews get:
- 4 steps: read draft, evaluate against criteria, write verdict JSON, commit (`pipelines.py:1523-1526`)
- No thoroughness framing, no review conventions, no procedural guidance
- The criteria themselves are substantive (refine has 6 sections, plan has 7), but the prompt wrapping them gives no instructions on how deep the analysis should be

### No "Approve with Suggestions" Concept in SDLC

PR reviewers have the `<!-- has-suggestions -->` marker (`action/review-conventions.md:34`) which promotes an `approve` to `approve-with-suggestions`, triggering the `on-review-feedback.yml` workflow so suggestions get acted on. The SDLC pipeline has no equivalent — observations from an approving reviewer are structurally lost.

## Constraints

- **Backward compatibility**: The `ReviewVerdict` model is consumed by `_read_review_verdict()` (`pipelines.py:1589-1632`), `_aggregate_review_verdicts()` (`pipelines.py:1712-1738`), and all existing verdict JSON files stored in `.egg-state/reviews/`. Any model changes must handle both old and new format gracefully.
- **JSON output format**: SDLC reviewers write JSON to files that are programmatically parsed, unlike PR reviewers who write free-form markdown to PR comments. The JSON format creates a ceiling on expressiveness unless new fields are added.
- **Token budget**: Making reviewers produce more detailed output increases token consumption per review cycle. This matters because reviews run on Opus, and the SDLC pipeline can run 3 reviewers × up to 3 cycles.
- **Reviewer scope separation**: The SDLC pipeline deliberately separates reviewers by concern (code, contract, agent-design). Adding thoroughness requirements must respect these boundaries — e.g., the agent-design reviewer should not be pressured into writing detailed feedback when there are no agent-design issues.
- **Agent-design reviewer is intentionally brief**: The agent-design criteria explicitly says "Only comment if you find agent-mode design issues. If the PR has no agent-mode concerns, approve with a brief note" (`action/build-agent-mode-design-review-prompt.sh:126-127`). Forcing thoroughness here would be counterproductive.
- **Shared criteria files**: Both systems consume the same criteria files. Changes to shared criteria affect PR reviewers too.
- **Test coverage**: `_aggregate_review_verdicts()` has no direct unit tests — only mock usage in tier 3 tests. The `ReviewVerdict` model is tested in `orchestrator/tests/test_models.py`. Prompt building is tested in `orchestrator/tests/test_pipeline_prompts.py`.

## Options Considered

### Option A: Expand Verdict Schema + Align Prompts (Comprehensive Fix)

**Approach**: Add `analysis` and `suggestions` fields to `ReviewVerdict` that are always populated regardless of verdict. Update the prompt builder to include review conventions and thoroughness instructions matching the PR reviewer. Update aggregation to surface suggestions from approved verdicts.

**Schema change**:
```python
class ReviewVerdict(BaseModel):
    verdict: str
    summary: str
    analysis: str = ""   # NEW: detailed file-by-file or section-by-section analysis (always populated)
    suggestions: str = "" # NEW: non-blocking suggestions (populated even on approve)
    feedback: str = ""    # existing: blocking feedback for needs_revision
    timestamp: str = ""
```

**Prompt changes**:
- Update verdict format instructions to require `analysis` always
- Explicitly say: "Always provide detailed analysis regardless of verdict"
- Remove "empty if approved" language from `feedback` field
- Include review conventions content (from `review-conventions.md`) in SDLC prompts
- Add the "last line of defense" and "critical infrastructure" framing for code reviewers

**Aggregation changes**:
- `_aggregate_review_verdicts()` collects `analysis` + `suggestions` from all verdicts, even approved ones
- Return value changes: `(overall_verdict, combined_feedback, combined_analysis)` or similar

**Pros**:
- Directly addresses all three root causes identified in the issue
- Clear separation between blocking feedback, non-blocking suggestions, and analysis
- Backward compatible — new fields have defaults, old JSON files parse fine
- Aligns SDLC reviewers with the PR reviewer standard

**Cons**:
- Larger scope of changes across model, prompt builder, and aggregation
- Increases token consumption per review (reviewers must always write analysis)
- May be over-structured — adding fields doesn't guarantee quality; the prompt is what really drives behavior

### Option B: Prompt-Only Fix (Minimal Schema Change)

**Approach**: Keep the verdict schema mostly as-is but rewrite the prompt instructions to demand thoroughness. Change "empty if approved" to require detailed feedback always. Load `review-conventions.md` content into SDLC prompts. The existing `summary` and `feedback` fields can carry richer content with better prompting.

**Prompt changes**:
- Remove "empty if approved" instruction
- Replace with: "Always provide detailed analysis in the summary field. Provide non-blocking suggestions in the feedback field even when approving."
- Add review conventions (comprehensive, specific, direct, suggest fixes)
- Add "critical infrastructure" framing for code reviewers
- For non-code reviewers (refine, plan), add structured analysis expectations keyed to their criteria sections

**Aggregation changes**:
- Update `_aggregate_review_verdicts()` to also collect `summary` and `feedback` from approved verdicts (not just needs_revision)

**Pros**:
- Smaller scope — no model changes needed
- Addresses the key issue (prompt instructions drive behavior)
- Lower risk of breaking existing verdict parsing

**Cons**:
- Overloads existing fields (`summary` for analysis, `feedback` for suggestions) — field semantics become ambiguous
- Harder for downstream code to distinguish blocking vs non-blocking feedback
- The "approved with empty feedback" pattern is reinforced by the field description in the model itself

### Option C: Add "Approved with Suggestions" Verdict + Prompt Improvements

**Approach**: Add a third verdict value (`approved_with_suggestions`) alongside the prompt improvements from Option B. When a reviewer approves but has suggestions, they use this verdict. The aggregation logic treats it as `approved` for overall verdict but surfaces the feedback for action.

**Schema change**: Add `approved_with_suggestions` as a valid verdict value. No new fields.

**Prompt changes**: Same as Option B, plus instructions about when to use each verdict.

**Aggregation changes**: `approved_with_suggestions` → overall still `approved`, but feedback is collected and surfaced.

**Pros**:
- Mirrors the PR reviewer's `has-suggestions` concept
- Clear semantic distinction without adding fields
- Moderate scope

**Cons**:
- Three-way verdict adds complexity to all verdict-handling code paths
- Doesn't address the shallow analysis problem for clean approvals (an approve with no suggestions still has no analysis requirement)
- Requires updating every `if verdict == "approved"` check throughout the codebase

## Recommended Approach

**Option A: Expand Verdict Schema + Align Prompts**. This directly addresses all three root causes:

1. **Schema constraint** → New `analysis` and `suggestions` fields that are always populated give reviewers structured space for depth.
2. **Prompt gap** → Including review conventions and thoroughness framing aligns SDLC prompts with PR prompts.
3. **No approve-with-suggestions** → The `suggestions` field serves this purpose without needing a third verdict value.

The key insight from the issue is that **the verdict format structurally discourages thorough analysis**. Option B improves prompting but leaves the structural incentive intact — an agent told to write JSON with a field called `feedback` described as "empty if approved" will naturally minimize effort. Option A changes the structure to match the desired behavior.

Backward compatibility is straightforward: new fields have empty-string defaults, so old JSON files parse without error. The aggregation function already handles `None` verdicts gracefully, and extending it to collect analysis/suggestions from all verdicts is a natural extension.

The agent-design reviewer should be exempted from the "always populate analysis" requirement — its criteria explicitly says to approve briefly when there are no concerns. This aligns with its PR counterpart's behavior.

## Open Questions

### Design Decisions

1. **Should the `analysis` field use a structured sub-format?** For code reviewers, should analysis be a list of per-file findings (like the PR reviewer's file-by-file approach), or free-form markdown? A structured format increases parsability but constrains the reviewer. The PR reviewer uses free-form markdown successfully.

2. **How should approved-verdict suggestions be surfaced to the implementing agent?** Options: (a) include them in the next cycle's prompt as "advisory feedback", (b) write them to a separate file agents can optionally read, (c) only surface them to the human in the phase-completion comment. Option (a) risks agents treating non-blocking suggestions as blocking requirements.

3. **Should the agent-design reviewer be exempt from the always-populate-analysis requirement?** Its PR counterpart explicitly says "approve with a brief note" when there are no agent-mode concerns. Forcing it to write detailed analysis seems counterproductive, but applying different rules per reviewer type adds complexity.

4. **Should review conventions be loaded from `action/review-conventions.md` or duplicated into the orchestrator?** Loading from the file keeps them in sync with PR reviewers but creates a dependency across the action/orchestrator boundary. Duplicating risks drift.

5. **Should the `feedback` field semantics change?** Currently "detailed feedback if needs_revision". Should it become "blocking feedback for needs_revision, non-blocking suggestions for approved"? Or should `suggestions` fully replace its role for non-blocking items, leaving `feedback` as blocking-only?

### Scope Decisions

6. **Should existing verdict files be migrated?** Old verdicts in `.egg-state/reviews/` lack `analysis` and `suggestions` fields. The default empty strings handle this, but should a one-time migration backfill them for consistency?

7. **Should the aggregation function return analysis/suggestions separately from feedback?** The current return type is `tuple[str, str]` (verdict, feedback). Adding analysis/suggestions changes the return signature and all callers. Should it return a typed object instead?

8. **How should the non-code reviewers' (refine, plan) prompts be improved?** The code reviewer has a clear model (the PR reviewer prompt) to align with. The refine and plan reviewers have no PR-side counterpart. How much additional prompting should they get? Their criteria are already substantive — is the gap mainly in the verdict format instructions and the "empty if approved" language?

### Risk/Budget Decisions

9. **What is the acceptable token budget increase?** Always-populate-analysis reviews will use more tokens per review. With 3 reviewers × potentially 3 cycles, this could meaningfully increase pipeline cost. Is there a per-review or per-cycle token budget to stay within?

10. **Should there be a maximum length for the `analysis` field?** Without guidance, a reviewer might produce very long analysis. Should the prompt include length guidance (e.g., "provide a thorough but concise analysis, typically 200-500 words")?

---

*Authored-by: egg*

# metadata
complexity_tier: mid
