# Plan: HITL feedback flow is awkward and unclear

> Issue: #375 | Phase: plan

## Summary

This plan implements a dedicated feedback comment system for the SDLC pipeline's HITL (Human-In-The-Loop) flow. Based on the analysis in issue #375 and human feedback, we will consolidate all questions (both checkbox-based decisions and open-ended questions) into a single, editable feedback comment. Humans will edit this comment to fill in answers and check a "Submit feedback" checkbox to trigger processing.

Key decisions from human feedback:
- **Free-form answers**: Answers will be free-form text (not structured blockquotes) for natural feel
- **Consolidated feedback**: All questions (checkbox and open-ended) go in a single feedback comment
- **Submit checkbox**: Require explicit "Submit feedback" checkbox to trigger processing

## Implementation Phases

### Phase 1: Contract Schema & Models

**Goal**: Extend the contract schema and Pydantic models to support feedback comments

**Tasks**:
- [TASK-1-1] Add `feedback` field to contract schema — Acceptance: Schema validates with new feedback field containing `id`, `questions`, `responses`, `submitted`, `submitted_by`, `submitted_at` fields
- [TASK-1-2] Add `FeedbackQuestion` and `Feedback` Pydantic models — Acceptance: Models match schema, unit tests pass for serialization/deserialization
- [TASK-1-3] Add `feedback` field to Contract model — Acceptance: Contract model includes optional feedback field, existing contract validation still works

**Dependencies**: None

**Exit criteria**: Schema and models updated, unit tests pass, existing contracts remain valid

### Phase 2: Feedback Comment Generation

**Goal**: Create functions to generate the dedicated feedback comment markdown

**Tasks**:
- [TASK-2-1] Create `feedback.py` module with `FeedbackQuestion` dataclass and generation functions — Acceptance: `generate_feedback_comment()` produces well-formatted markdown with questions, answer placeholders, and submit checkbox
- [TASK-2-2] Add `egg-feedback` marker format (`<!-- egg-feedback id=feedback-N -->`) — Acceptance: Marker is correctly embedded in generated comment and parseable by regex
- [TASK-2-3] Create template for feedback comment with instructions — Acceptance: Template includes clear instructions for humans, answer sections with placeholder text, and submit checkbox

**Dependencies**: Phase 1 (for model types)

**Exit criteria**: `generate_feedback_comment()` function works, unit tests verify markdown output format

### Phase 3: Feedback Comment Parsing

**Goal**: Parse human-edited feedback comments to extract answers

**Tasks**:
- [TASK-3-1] Create `parse_feedback_comment()` function to extract answers — Acceptance: Function correctly extracts free-form answers following each question heading
- [TASK-3-2] Parse submit checkbox state — Acceptance: Function detects whether `[x] Submit feedback` is checked
- [TASK-3-3] Handle edge cases (empty answers, malformed edits) — Acceptance: Parser gracefully handles missing answers, extra whitespace, and partial edits

**Dependencies**: Phase 2

**Exit criteria**: Parsing functions work with various input formats, unit tests cover edge cases

### Phase 4: Contract CLI Extension

**Goal**: Add CLI command for agents to create feedback comments

**Tasks**:
- [TASK-4-1] Add `egg-contract add-feedback` command — Acceptance: Command accepts `--question "Q1" --question "Q2"` format and outputs markdown
- [TASK-4-2] Support `--format markdown` and `--format json` output — Acceptance: Both output formats work correctly
- [TASK-4-3] Register feedback in contract with unique ID — Acceptance: Running command updates contract `feedback` field with new feedback entry

**Dependencies**: Phase 1, Phase 2

**Exit criteria**: CLI command works end-to-end, feedback registered in contract

### Phase 5: Workflow Handler

**Goal**: Extend `sdlc-hitl.yml` to handle feedback comment edits

**Tasks**:
- [TASK-5-1] Add `handle-feedback` job triggered on `<!-- egg-feedback` marker — Acceptance: Job triggers when feedback comment is edited by authorized user
- [TASK-5-2] Implement debounce logic (30 seconds, same as decisions) — Acceptance: Rapid edits don't trigger multiple runs, only processes after debounce expires
- [TASK-5-3] Parse feedback responses and update contract — Acceptance: Contract `feedback.responses` field populated with parsed answers, `submitted=true`, audit entry added
- [TASK-5-4] Trigger pipeline continuation after feedback submitted — Acceptance: After feedback processed, pipeline resumes with feedback available in prompt

**Dependencies**: Phase 3, Phase 4

**Exit criteria**: Workflow correctly detects edits, parses responses, updates contract, and resumes pipeline

### Phase 6: Agent Prompt Integration

**Goal**: Update agent prompts to use feedback comments and access responses

**Tasks**:
- [TASK-6-1] Update refine/plan phase prompts to use `egg-contract add-feedback` for open-ended questions — Acceptance: Prompts instruct agents to use feedback comments instead of listing questions as plain text
- [TASK-6-2] Inject pending feedback into agent context — Acceptance: When feedback exists but is not submitted, agent prompt includes the pending questions
- [TASK-6-3] Inject submitted feedback responses into agent context — Acceptance: When feedback is submitted, responses are available in agent prompt for processing

**Dependencies**: Phase 4, Phase 5

**Exit criteria**: Agents can create feedback comments and access responses in subsequent invocations

### Phase 7: Documentation & Migration

**Goal**: Update documentation and ensure smooth rollout

**Tasks**:
- [TASK-7-1] Update `docs/hitl-decisions.md` with feedback comment documentation — Acceptance: Documentation explains when/how to use feedback comments vs checkbox decisions
- [TASK-7-2] Add feedback comment example to `docs/templates/` — Acceptance: Template file shows correct feedback comment format
- [TASK-7-3] Update CLAUDE.md with feedback workflow instructions — Acceptance: Agent instructions include feedback comment usage

**Dependencies**: Phase 6

**Exit criteria**: Documentation complete and accurate

## Test Strategy

- **Unit tests**:
  - `test_feedback.py`: Test feedback comment generation, parsing, edge cases
  - Update `test_hitl.py`: Ensure existing HITL decision tests still pass
  - `test_models.py`: Test new Feedback model serialization

- **Integration tests**:
  - `test_feedback_flow.py`: End-to-end test of feedback creation, editing, parsing, contract update
  - Verify workflow triggers correctly on comment edits

- **Manual testing**:
  1. Create a test issue with SDLC label
  2. Advance to refine phase
  3. Verify agent creates feedback comment with questions
  4. Edit comment to add answers
  5. Check submit checkbox
  6. Verify debounce countdown appears
  7. Verify contract updated with responses
  8. Verify pipeline resumes

## Rollback Plan

If issues are discovered after deployment:

1. **Immediate rollback**: Revert the commit(s) introducing feedback handling in `sdlc-hitl.yml`
   ```bash
   git revert <commit-sha>
   git push origin main
   ```

2. **Partial rollback**: If only parsing is broken, the workflow can fall back to ignoring feedback comments (they won't block the pipeline, just won't be processed)

3. **Schema migration**: The new `feedback` field is optional with a default of `null`, so existing contracts remain valid. No migration script needed.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Free-form parsing unreliable | Medium | Medium | Use clear question markers (`**Q1:**`) and forgiving regex; test extensively |
| Multiple feedback comments per issue | Low | Low | Validate only one active feedback per phase; warn if duplicates detected |
| User edits answer format unexpectedly | Medium | Low | Graceful degradation: if parsing fails, include raw comment in agent prompt |
| Debounce race conditions | Low | Medium | Reuse existing debounce pattern from decisions; check for concurrent runs |
| Workflow authorization bypass | Low | High | Reuse existing authorization checks (only `jwbron` can trigger) |

## Migration Notes

- **No database migrations**: Contract JSON schema is backward-compatible
- **No breaking changes**: Existing checkbox decisions continue to work unchanged
- **Gradual rollout**: Agents can be updated to use feedback comments incrementally; old question format still works during transition
- **Feature flag** (optional): Could add `use_feedback_comments` flag to contract to opt-in per issue

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add dedicated feedback comment for HITL flow"
  description: |
    Implements a dedicated feedback comment system for the SDLC HITL workflow.
    Consolidates all questions (checkbox and open-ended) into a single editable
    comment with a submit checkbox. Humans edit to provide answers and check
    submit to trigger processing.

    Fixes #375
phases:
  - id: 1
    name: Contract Schema & Models
    goal: Extend contract schema and Pydantic models to support feedback comments
    tasks:
      - id: TASK-1-1
        description: Add feedback field to contract schema
        acceptance: Schema validates with new feedback field containing id, questions, responses, submitted, submitted_by, submitted_at fields
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-1-2
        description: Add FeedbackQuestion and Feedback Pydantic models
        acceptance: Models match schema, unit tests pass for serialization/deserialization
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Add feedback field to Contract model
        acceptance: Contract model includes optional feedback field, existing contract validation still works
        files:
          - shared/egg_contracts/models.py
  - id: 2
    name: Feedback Comment Generation
    goal: Create functions to generate the dedicated feedback comment markdown
    tasks:
      - id: TASK-2-1
        description: Create feedback.py module with FeedbackQuestion dataclass and generation functions
        acceptance: generate_feedback_comment() produces well-formatted markdown with questions, answer placeholders, and submit checkbox
        files:
          - shared/egg_contracts/feedback.py
      - id: TASK-2-2
        description: Add egg-feedback marker format
        acceptance: Marker is correctly embedded in generated comment and parseable by regex
        files:
          - shared/egg_contracts/feedback.py
      - id: TASK-2-3
        description: Create template for feedback comment with instructions
        acceptance: Template includes clear instructions for humans, answer sections with placeholder text, and submit checkbox
        files:
          - shared/egg_contracts/feedback.py
          - docs/templates/feedback.md
  - id: 3
    name: Feedback Comment Parsing
    goal: Parse human-edited feedback comments to extract answers
    tasks:
      - id: TASK-3-1
        description: Create parse_feedback_comment() function to extract answers
        acceptance: Function correctly extracts free-form answers following each question heading
        files:
          - shared/egg_contracts/feedback.py
      - id: TASK-3-2
        description: Parse submit checkbox state
        acceptance: Function detects whether Submit feedback checkbox is checked
        files:
          - shared/egg_contracts/feedback.py
      - id: TASK-3-3
        description: Handle edge cases in parsing
        acceptance: Parser gracefully handles missing answers, extra whitespace, and partial edits
        files:
          - shared/egg_contracts/feedback.py
          - tests/shared/egg_contracts/test_feedback.py
  - id: 4
    name: Contract CLI Extension
    goal: Add CLI command for agents to create feedback comments
    tasks:
      - id: TASK-4-1
        description: Add egg-contract add-feedback command
        acceptance: Command accepts --question format and outputs markdown
        files:
          - shared/egg_contracts/cli.py
      - id: TASK-4-2
        description: Support --format markdown and --format json output
        acceptance: Both output formats work correctly
        files:
          - shared/egg_contracts/cli.py
      - id: TASK-4-3
        description: Register feedback in contract with unique ID
        acceptance: Running command updates contract feedback field with new feedback entry
        files:
          - shared/egg_contracts/cli.py
  - id: 5
    name: Workflow Handler
    goal: Extend sdlc-hitl.yml to handle feedback comment edits
    tasks:
      - id: TASK-5-1
        description: Add handle-feedback job triggered on egg-feedback marker
        acceptance: Job triggers when feedback comment is edited by authorized user
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-5-2
        description: Implement debounce logic for feedback
        acceptance: Rapid edits don't trigger multiple runs, only processes after debounce expires
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-5-3
        description: Parse feedback responses and update contract
        acceptance: Contract feedback.responses field populated with parsed answers
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-5-4
        description: Trigger pipeline continuation after feedback submitted
        acceptance: After feedback processed, pipeline resumes with feedback available in prompt
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 6
    name: Agent Prompt Integration
    goal: Update agent prompts to use feedback comments and access responses
    tasks:
      - id: TASK-6-1
        description: Update phase prompts to use egg-contract add-feedback for open-ended questions
        acceptance: Prompts instruct agents to use feedback comments instead of listing questions as plain text
        files:
          - action/build-sdlc-prompt.sh
      - id: TASK-6-2
        description: Inject pending feedback into agent context
        acceptance: When feedback exists but is not submitted, agent prompt includes the pending questions
        files:
          - action/build-sdlc-prompt.sh
      - id: TASK-6-3
        description: Inject submitted feedback responses into agent context
        acceptance: When feedback is submitted, responses are available in agent prompt for processing
        files:
          - action/build-sdlc-prompt.sh
  - id: 7
    name: Documentation & Migration
    goal: Update documentation and ensure smooth rollout
    tasks:
      - id: TASK-7-1
        description: Update docs/hitl-decisions.md with feedback comment documentation
        acceptance: Documentation explains when/how to use feedback comments vs checkbox decisions
        files:
          - docs/hitl-decisions.md
      - id: TASK-7-2
        description: Add feedback comment example to docs/templates/
        acceptance: Template file shows correct feedback comment format
        files:
          - docs/templates/feedback.md
      - id: TASK-7-3
        description: Update CLAUDE.md with feedback workflow instructions
        acceptance: Agent instructions include feedback comment usage
        files:
          - CLAUDE.md
```

---

## Phase Approval

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
