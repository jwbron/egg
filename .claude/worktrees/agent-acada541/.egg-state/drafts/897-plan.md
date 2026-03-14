# Implementation Plan: Deepen SDLC Review Quality

> Issue: #897 | Phase: plan | Pipeline: issue-897

## Summary

SDLC pipeline reviewers produce shallow verdicts compared to PR reviewers because:
(1) the verdict JSON schema tells reviewers to write empty feedback when approving,
(2) the SDLC prompt lacks the thoroughness framing and review conventions that PR
prompts include, and (3) there is no way to surface non-blocking suggestions from
approved verdicts.

This plan implements Approach A from the architecture analysis: expand the
`ReviewVerdict` model with `analysis` and `suggestions` fields, align the SDLC
review prompt builder with PR reviewer standards, and update aggregation to surface
non-blocking observations from all verdicts.

## Approach

**Single PR with three implementation phases** within one commit stream:

1. **Model + aggregation** — Expand the data model and update the aggregation
   function and its callers. This is the structural foundation.
2. **Prompt builder** — Update the verdict format instructions, add review
   conventions, enhance scope preambles, and expand draft-reviewer procedural steps.
3. **Tests** — Update existing tests and add new coverage for the changed code.

Each phase builds on the prior one. The model change is backward-compatible (new
fields default to empty strings), so existing verdict files continue to parse.

## Key Design Decisions

Per the architect's analysis (AD-1 through AD-6):

- **Free-form markdown** for the `analysis` field (not structured JSON sub-objects).
- **Agent-design reviewer exempt** from always-populate-analysis — its criteria
  explicitly allows brief approval when no concerns exist.
- **`AggregatedReviewResult` NamedTuple** replaces the `tuple[str, str]` return type
  from `_aggregate_review_verdicts()` for clarity and extensibility.
- **Review conventions inlined** in the prompt builder — not loaded from
  `action/review-conventions.md` (avoids cross-boundary dependency).
- **Advisory content NOT injected** into retry prompt `prior_feedback` — only
  blocking feedback goes there. Advisory content is logged for observability.
- **No migration** of old verdict files — Pydantic defaults handle missing fields.

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Token consumption increase | Prompt includes length guidance ("thorough but concise, 200-500 words"). Agent-design reviewer exempted. |
| Aggregation return type breaks callers | Only 2 callers exist (verified). Both updated in same phase. NamedTuple fields match old tuple positionally as fallback. |
| Prompt changes don't improve quality | PR reviewer evidence proves same model (Opus) produces thorough reviews with better prompting. Post-deploy comparison validates. |
| test_multi_reviewer.py uses local helpers | Test uses its own `aggregate_verdicts()` function, not production `_aggregate_review_verdicts()`. Update test to also exercise the production function or document the scope gap. |

## Test Strategy

1. **Unit tests** (`orchestrator/tests/test_tier3_execute.py`):
   - `TestReadReviewVerdict`: Add backward-compat test — old JSON without
     `analysis`/`suggestions` fields parses correctly.
   - Add test exercising `ReviewVerdict` with all new fields populated.
   - Update mock `ReviewVerdict()` calls to verify defaults work.

2. **Unit tests** (`orchestrator/tests/test_pipeline_prompts.py`):
   - Add `TestBuildReviewPrompt` class testing the generated prompt:
     - Verdict format JSON includes `analysis` and `suggestions` fields.
     - Review conventions text is present in generated prompt.
     - `"empty if approved"` language is NOT present.
     - Agent-design reviewer preamble does NOT require detailed analysis.
     - Code reviewer preamble includes file-by-file analysis expectation.

3. **Integration tests** (`tests/workflows/test_multi_reviewer.py`):
   - Update `TestReviewVerdictAggregation` tests to use `ReviewVerdict` objects
     (currently uses plain strings via a local helper). Or add parallel tests
     that import and exercise `_aggregate_review_verdicts()` directly.
   - Test that approved verdicts with `analysis`/`suggestions` are collected.
   - Test `AggregatedReviewResult` fields.

4. **Existing tests**: Run full test suite to verify no regressions from model and
   aggregation changes.

## File Impact

| File | Change | Risk |
|------|--------|------|
| `orchestrator/models.py:97-103` | Add `analysis`, `suggestions` fields to `ReviewVerdict`. Add `AggregatedReviewResult` NamedTuple. | Low |
| `orchestrator/routes/pipelines.py:1004-1044` | Update `_get_reviewer_scope_preamble()` with per-type analysis depth expectations. | Low |
| `orchestrator/routes/pipelines.py:1452-1586` | Update `_build_review_prompt()`: verdict format, review conventions, draft-reviewer steps. | Low |
| `orchestrator/routes/pipelines.py:1712-1738` | Update `_aggregate_review_verdicts()`: return `AggregatedReviewResult`, collect from all verdicts. | Medium |
| `orchestrator/routes/pipelines.py:3447` | Update Tier 3 caller to use `AggregatedReviewResult`. | Low |
| `orchestrator/routes/pipelines.py:5396` | Update main phase loop caller to use `AggregatedReviewResult`. | Low |
| `orchestrator/tests/test_tier3_execute.py` | Update `TestReadReviewVerdict`, add backward-compat test, update mocks. | Low |
| `orchestrator/tests/test_pipeline_prompts.py` | Add `TestBuildReviewPrompt` class. | Low |
| `tests/workflows/test_multi_reviewer.py` | Add `_aggregate_review_verdicts()` tests with `ReviewVerdict` objects. | Low |

---

```yaml
# yaml-tasks
pr:
  title: "Deepen SDLC review quality with expanded verdict schema and aligned prompts"
  description: |
    SDLC pipeline reviewers produce shallow ~1-3 sentence verdicts with empty
    feedback even when approving code with real issues, while PR reviewers on
    the same code produce detailed multi-section reviews. This expands the
    ReviewVerdict model with always-populated analysis and suggestions fields,
    aligns SDLC review prompts with PR reviewer thoroughness standards, and
    updates aggregation to surface non-blocking observations from all verdicts.
phases:
  - id: 1
    name: Model and aggregation
    goal: Expand the ReviewVerdict data model and update aggregation to collect analysis from all verdicts
    tasks:
      - id: TASK-1-1
        description: Add `analysis` (str, default="") and `suggestions` (str, default="") fields to ReviewVerdict in orchestrator/models.py with descriptive Field annotations
        acceptance: ReviewVerdict has both new fields with empty-string defaults. Old verdict JSON without these fields parses correctly via Pydantic defaults.
        files:
          - orchestrator/models.py
      - id: TASK-1-2
        description: Define AggregatedReviewResult NamedTuple in orchestrator/models.py with fields verdict, blocking_feedback, advisory_content
        acceptance: AggregatedReviewResult is importable from models.py with the three named fields.
        files:
          - orchestrator/models.py
      - id: TASK-1-3
        description: Update _aggregate_review_verdicts() to return AggregatedReviewResult. Collect feedback from needs_revision verdicts into blocking_feedback (existing behavior). Collect analysis and suggestions from ALL verdicts (including approved) into advisory_content.
        acceptance: Function returns AggregatedReviewResult. blocking_feedback contains only needs_revision feedback. advisory_content contains analysis+suggestions from all verdicts. None verdicts are still skipped.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-4
        description: Update Tier 3 caller at pipelines.py:3447 to destructure AggregatedReviewResult. Use result.blocking_feedback for prior_feedback in retry loop. Log advisory_content.
        acceptance: Tier 3 caller uses named fields from AggregatedReviewResult. Only blocking_feedback is passed to prior_feedback. Advisory content is not injected into retry prompts.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: Update main phase loop caller at pipelines.py:5396 to destructure AggregatedReviewResult. Use result.blocking_feedback for review_feedback. Log advisory_content.
        acceptance: Main loop caller uses named fields from AggregatedReviewResult. Only blocking_feedback is used for review_feedback. Advisory content is not injected into retry prompts.
        files:
          - orchestrator/routes/pipelines.py
  - id: 2
    name: Prompt builder alignment
    goal: Align SDLC review prompts with PR reviewer thoroughness standards
    tasks:
      - id: TASK-2-1
        description: Update the verdict format section in _build_review_prompt() (pipelines.py:1554-1570) to include analysis and suggestions fields in the JSON template. Replace "empty if approved" with instructions to always populate analysis and provide non-blocking suggestions. Clarify feedback field is for blocking issues only.
        acceptance: Verdict JSON template has 5 fields (reviewer, verdict, summary, analysis, suggestions, feedback, timestamp — 7 total). Instructions say "Always provide detailed analysis regardless of verdict". The string "empty if approved" does not appear. feedback field described as blocking-only.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: Add review conventions (5 comment quality standards) to _build_review_prompt() after the criteria section. Inline the standards from review-conventions.md — comprehensive, specific, direct, suggest fixes, provide context. Add "critical infrastructure" and "last line of defense" framing for code reviewers.
        acceptance: Generated prompt contains a Review Conventions section with all 5 quality standards. Code reviewer prompt includes "critical infrastructure" framing. Conventions are inlined in the Python code, not loaded from action/review-conventions.md.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: Update _get_reviewer_scope_preamble() to add analysis depth expectations per reviewer type. Code reviewer — file-by-file analysis. Contract reviewer — criterion-by-criterion verification table. Refine/plan reviewer — section-by-section evaluation. Agent-design reviewer — brief approval acceptable when no concerns.
        acceptance: Each reviewer type's preamble includes its expected analysis format. Agent-design preamble explicitly states brief approval is acceptable. Code preamble mentions file-by-file.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-4
        description: Expand procedural steps for draft-based (non-code) reviewers from 4 steps to 6-7 steps. Add instructions to read thoroughly, cross-reference criteria sections, cite specific evidence in analysis, and evaluate completeness.
        acceptance: Non-code reviewer steps are expanded from 4 to 6-7 steps. Steps include cross-referencing with criteria and citing specific sections. Steps are not overly prescriptive.
        files:
          - orchestrator/routes/pipelines.py
  - id: 3
    name: Tests
    goal: Update existing tests and add coverage for all changed code
    tasks:
      - id: TASK-3-1
        description: Update orchestrator/tests/test_tier3_execute.py — add backward-compat test for ReviewVerdict parsing old JSON (no analysis/suggestions fields). Add test with all new fields populated. Verify existing mock ReviewVerdict() calls still work with defaults.
        acceptance: TestReadReviewVerdict has a test for old-format JSON without analysis/suggestions. At least one test creates ReviewVerdict with analysis and suggestions populated. All existing tests pass.
        files:
          - orchestrator/tests/test_tier3_execute.py
      - id: TASK-3-2
        description: Add TestBuildReviewPrompt class to orchestrator/tests/test_pipeline_prompts.py. Test verdict format JSON includes analysis and suggestions. Test review conventions text appears. Test "empty if approved" is absent. Test agent-design preamble doesn't require detailed analysis. Test code reviewer preamble includes file-by-file expectation.
        acceptance: TestBuildReviewPrompt has 4+ test methods covering verdict format, conventions presence, absence of "empty if approved", and per-reviewer preamble content.
        files:
          - orchestrator/tests/test_pipeline_prompts.py
      - id: TASK-3-3
        description: Update tests/workflows/test_multi_reviewer.py to add tests exercising _aggregate_review_verdicts() with ReviewVerdict objects. Test approved verdicts with analysis/suggestions are collected in advisory_content. Test AggregatedReviewResult fields. Test backward compat (verdicts without new fields).
        acceptance: New test class or methods import and exercise _aggregate_review_verdicts(). Tests verify advisory_content is populated from approved verdicts. Tests verify AggregatedReviewResult named fields.
        files:
          - tests/workflows/test_multi_reviewer.py
      - id: TASK-3-4
        description: Run the full test suite (make test or equivalent) and verify no regressions from model and aggregation changes.
        acceptance: All existing tests pass. No new test failures introduced.
        files: []
```

---

*Authored-by: egg*
