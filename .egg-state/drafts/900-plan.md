# Implementation Plan: Type-Aware HITL Rendering (Revision 3)

> Issue: #900 | Phase: plan | Pipeline: issue-900 | Revision: 3

**Revision notes:** Revision 3 (task_planner agent). Carries forward all
feedback resolutions from revision 2. Cleaned up duplicate test strategy item,
specified concrete test file paths for all tasks, and tightened acceptance
criteria on TASK-3-5.

## Summary

The local `egg-sdlc` HITL checkpoint handler uses a single generic 5-option menu
for all human input regardless of type. This plan implements Approach A from the
architecture analysis: add `decision_type` and `questions` fields to `HITLDecision`,
thread them through the API, update phase gate creation to tag decisions, switch
resolution payloads to structured JSON, add `create_decision()` to `OrchClient`,
and rework the terminal handler to dispatch to type-specific UX flows.

## Approach

**Single PR with three implementation phases** within one commit stream:

1. **Data layer and orchestrator behavior** — Extend the `HITLDecision` model with
   `decision_type` and `questions` fields, thread them through `DecisionQueue`,
   the REST API, and `OrchClient`. Update phase gate creation to tag decisions as
   `phase_gate`. Update resolution parsing to try JSON first with string fallback.
   This establishes the structural foundation that the terminal handler depends on.

2. **Terminal handler rework** — Refactor `handle_hitl_checkpoint()` to dispatch on
   `decision_type`. Implement three UX flows: `phase_gate` (draft review with
   approve/request changes), `choice` (render `decision.options` as numbered list),
   and `feedback` (prompt each question individually). Add universal options
   (general feedback, change approach, cancel) to every checkpoint. Construct
   JSON resolution payloads. Show confirmation after each action.

3. **Tests** — Update existing test suites for model changes, new resolution
   parsing, and type-aware terminal rendering. Add new test cases for each
   decision type flow, JSON resolution payloads, backward compatibility
   with bare string resolutions, decision endpoint serialization, and
   OrchClient.create_decision().

Each phase builds on the prior one. All model changes are backward-compatible
(new fields have defaults), so existing state files and API consumers continue
to work.

## Key Design Decisions

Per the architect's analysis (TD-1 through TD-5):

- **`decision_type` as plain string** (not StrEnum) for forward compatibility.
  Values: `phase_gate`, `choice`, `feedback`. Default: `choice`.
- **`questions` as `list[dict]`** with keys `id`, `question`, `answer`. Default: `[]`.
  Each dict represents one feedback question.
- **Resolution payloads are JSON strings** in the existing `resolution` field.
  The orchestrator tries `json.loads` **first** (critical ordering per R-4), falls
  back to keyword matching for legacy bare-string resolutions on `JSONDecodeError`.
- **Universal options use letter keys** (`[f]`, `[a]`, `[c]`) separate from
  numbered context-specific choices, providing visual separation.
- **Follow-up decision for bare "request changes"** remains as fallback for
  legacy resolutions but is **skipped** when JSON resolution includes non-empty
  feedback (eliminates the extra round-trip for new clients).
- **Fallback to generic menu** when `decision_type` is missing or unrecognized,
  preserving backward compatibility with older orchestrator versions.
- **Feedback UX uses collect-all-then-review-and-submit** (TD-5): After all questions
  are answered, show a numbered summary with `[s]` Submit / `[r]` Redo question N /
  `[c]` Cancel. Avoids complex terminal cursor manipulation.

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| JSON resolution parsing breaks legacy resolutions | Wrap `json.loads` in `try/except JSONDecodeError`, fall back to existing keyword matching. **JSON-first ordering is critical** (R-4). Test both formats. |
| Raw JSON injected into agent prompts (R-1) | Resolution parsing MUST extract `payload.get('feedback', resolution)` for `hitl_revision_feedback`, not assign the raw JSON string. Test that agents receive readable feedback text. |
| API serialization omits new fields (R-2) | Update BOTH serialization sites in `decisions.py` (list endpoint lines 109-121 and create response lines 182-191). TASK-3-5 verifies via GET. |
| Follow-up decision missing type (R-3) | Follow-up decision at `pipelines.py:5899` must pass `decision_type='phase_gate'` and carry through context/draft. |
| Silent field drop between queue and model (R-9) | `Pipeline.add_decision()` and `DecisionQueue.queue_decision()` must be updated atomically in the same commit. |
| Old state files lack new fields | Pydantic defaults (`decision_type='choice'`, `questions=[]`) handle missing fields. Add backward-compat test. |
| Multi-question feedback input interrupted (Ctrl+C/EOF) | Wrap each `input()` call in `try/except (EOFError, KeyboardInterrupt)`. Show partial answers with recovery options. |
| Terminal handler test refactoring scope | Existing tests validate the generic menu which becomes the fallback. Don't delete them — they verify backward compatibility. New tests cover type-specific flows. |
| Phase gates created before upgrade appear as `choice` | Phase gates are always created by the orchestrator (not agents), and the orchestrator code is updated in Phase 1 to tag them. Old resolved decisions in state files are not re-rendered. |

## Test Strategy

1. **Model tests** (`orchestrator/tests/test_models.py`):
   - `HITLDecision` creation with new fields and defaults.
   - Backward compatibility: old-format dict without `decision_type`/`questions` parses correctly.
   - `Pipeline.add_decision()` accepts and passes through new fields.

2. **Resolution parsing tests** (`orchestrator/tests/test_hitl_revision.py`):
   - JSON resolution `{"action": "approve"}` → approval path.
   - JSON resolution `{"action": "request_changes", "feedback": "..."}` → revision path with readable feedback (not raw JSON) in agent prompt.
   - JSON resolution `{"action": "select", "selected": "MongoDB"}` → approval path.
   - JSON resolution `{"action": "change_approach", "feedback": "..."}` → revision path.
   - JSON resolution `{"action": "submit_feedback", "answers": {...}}` → approval path.
   - Bare string "Approved" → approval path (backward compat).
   - Bare string "request changes" → follow-up dance (backward compat).
   - Malformed JSON → falls back to string matching.

3. **Terminal handler tests** (`tests/sandbox/test_sdlc_hitl.py`):
   - `phase_gate` type: shows edit/claude/approve/request-changes options. Resolves with JSON.
   - `choice` type: renders `decision.options` as numbered list. Resolves with JSON.
   - `feedback` type: prompts each question individually, shows summary, supports redo. Resolves with JSON.
   - Universal options (general feedback, change approach, cancel) present on all types.
   - Confirmation messages displayed after each action.
   - Fallback to generic menu for unknown `decision_type`.

4. **Integration tests** (`tests/workflows/test_hitl_integration.py`):
   - End-to-end: phase gate created with `decision_type='phase_gate'`, rendered correctly.
   - End-to-end: JSON resolution parsed and routes to correct path.

5. **Decision endpoint tests** (`orchestrator/tests/test_decisions_routes.py` or existing decisions test file):
   - POST create decision with `decision_type` and `questions` fields.
   - POST create decision without new fields (defaults applied).
   - GET list/get returns `decision_type` and `questions` in serialization.

6. **OrchClient.create_decision() tests** (`tests/sandbox/test_orch_client.py`):
   - Verify HTTP POST request construction with `decision_type` and `questions`.
   - Verify response parsing and error handling.
   - Verify default parameter values (`decision_type='choice'`, `questions=None`).

7. **Full suite**: Run `make test` to verify no regressions.

## File Impact

| File | Change | Risk |
|------|--------|------|
| `orchestrator/models.py:169-181` | Add `decision_type`, `questions` fields to `HITLDecision`. Update `Pipeline.add_decision()` signature. | Low |
| `orchestrator/decision_queue.py:126-166` | Add `decision_type`, `questions` params to `queue_decision()`. | Low |
| `orchestrator/routes/decisions.py:109-201` | Accept new fields in POST. Include in GET serialization. | Low |
| `orchestrator/routes/pipelines.py:5857-5862` | Set `decision_type='phase_gate'` in phase gate creation. | Low |
| `orchestrator/routes/pipelines.py:5885-5970` | JSON-first resolution parsing with string fallback. | Medium |
| `sandbox/egg_lib/orch_client.py:129+` | Add `create_decision()` method. | Low |
| `sandbox/egg_lib/sdlc_hitl.py:219-341` | Rework `handle_hitl_checkpoint()` for type-aware dispatch. | High |
| `orchestrator/tests/test_models.py` | Update for new fields and defaults. | Low |
| `tests/sandbox/test_sdlc_hitl.py` | Add type-specific rendering tests. | Medium |
| `orchestrator/tests/test_hitl_revision.py` | Add JSON resolution parsing tests. | Low |
| `tests/workflows/test_hitl_integration.py` | Add end-to-end typed decision tests. | Low |
| `orchestrator/tests/` (decisions route tests) | Add decision endpoint tests for new fields. | Low |
| `tests/sandbox/test_orch_client.py` | Add `create_decision()` unit tests. | Low |

## Review Feedback Addressed

| # | Feedback | Resolution |
|---|----------|------------|
| 1 | Missing test task for decision endpoints | Added TASK-3-5: decision endpoint tests covering POST with/without new fields and GET serialization |
| 2 | Missing test coverage for OrchClient.create_decision() | Added TASK-3-6: unit tests for create_decision() HTTP request construction, response parsing, defaults, and error handling |
| 3 | TASK-2-4 'edit answers before submitting' underspecified | Specified concrete UX: collect-all-then-review-and-submit with `[s]` Submit / `[r]` Redo question N / `[c]` Cancel from numbered summary |

---

```yaml
# yaml-tasks
pr:
  title: "Add type-aware HITL rendering for decisions, feedback, and phase approval"
  description: |
    The local egg-sdlc HITL checkpoint handler uses a single generic 5-option
    menu for all human input. This adds a decision_type field to HITLDecision
    (phase_gate, choice, feedback), extends the orchestrator API to accept typed
    decisions with structured feedback questions, and reworks the terminal handler
    to dispatch to type-specific UX flows with JSON resolution payloads. Universal
    'general feedback' and 'change approach' options are available on every checkpoint.
phases:
  - id: 1
    name: Data layer and orchestrator behavior
    goal: Extend the HITLDecision model, thread new fields through the API, tag phase gates, and update resolution parsing to JSON-first
    tasks:
      - id: TASK-1-1
        description: "Add `decision_type` (str, default='choice') and `questions` (list[dict], default=[]) fields to HITLDecision in orchestrator/models.py. Update Pipeline.add_decision() to accept and pass through decision_type and questions parameters. IMPORTANT: must be updated atomically with TASK-1-2 to prevent silent field drop (R-9)."
        acceptance: "HITLDecision has both new fields with correct defaults. Pipeline.add_decision() signature includes decision_type and questions. Old state files without these fields parse via Pydantic defaults."
        files:
          - orchestrator/models.py
      - id: TASK-1-2
        description: "Add decision_type and questions parameters to DecisionQueue.queue_decision() in orchestrator/decision_queue.py. Pass through to Pipeline.add_decision(). Must be committed together with TASK-1-1 to prevent silent field drop."
        acceptance: "queue_decision() accepts decision_type (default='choice') and questions (default=[]). Values are passed to add_decision() and persisted in the decision object."
        files:
          - orchestrator/decision_queue.py
      - id: TASK-1-3
        description: "Update the POST create-decision endpoint in orchestrator/routes/decisions.py to accept decision_type and questions in the request body. CRITICAL (R-2): update BOTH serialization sites — the list endpoint (lines 109-121) and create response (lines 182-191) both use hand-built dicts. Add decision_type and questions to both. The list endpoint is the critical one as it feeds the terminal handler."
        acceptance: "POST /api/v1/pipelines/{id}/decisions accepts optional decision_type and questions. GET list and get endpoints include decision_type and questions in response dicts. Requests without new fields still work (defaults applied)."
        files:
          - orchestrator/routes/decisions.py
      - id: TASK-1-4
        description: "Update phase gate creation in orchestrator/routes/pipelines.py to pass decision_type='phase_gate' when calling dq.queue_decision(). Also update the follow-up decision at line 5899 (bare 'request changes' follow-up) to pass decision_type='phase_gate' and carry through the same context (draft_content). Missing this causes a visible UX regression where follow-ups render as choice menus (R-3)."
        acceptance: "Phase gate decisions have decision_type='phase_gate'. Follow-up decisions also have decision_type='phase_gate' and include context. Non-phase-gate decisions are unaffected."
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: "Update phase gate resolution handling in orchestrator/routes/pipelines.py with JSON-FIRST ordering (critical): (1) try json.loads(resolution), (2) if valid JSON with 'action' field, dispatch on action value, (3) if JSONDecodeError, fall back to existing keyword matching. Map 'approve' to approval path, 'request_changes'/'change_approach' to revision path, 'select' to approval path. CRITICAL: when action is 'request_changes'/'change_approach', set hitl_revision_feedback = payload.get('feedback', resolution) — do NOT assign raw JSON to hitl_revision_feedback (R-1). When JSON resolution has non-empty feedback, skip the follow-up decision (feedback already included)."
        acceptance: "JSON resolution {\"action\":\"approve\"} routes to approval. JSON {\"action\":\"request_changes\",\"feedback\":\"...\"} triggers phase re-run with readable feedback text (not raw JSON) in agent prompt. JSON with non-empty feedback skips follow-up decision. Bare string \"Approved\" still works (backward compat). Bare string \"request changes\" still triggers follow-up (backward compat). No behavior change for existing callers."
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-6
        description: "Add create_decision() method to OrchClient in sandbox/egg_lib/orch_client.py. Signature: create_decision(pipeline_id, question, options=None, decision_type='choice', questions=None, context='') -> dict. Uses POST /api/v1/pipelines/{id}/decisions."
        acceptance: "OrchClient has create_decision() that sends decision_type and questions to the orchestrator API. Method follows existing patterns (uses _request(), returns parsed JSON)."
        files:
          - sandbox/egg_lib/orch_client.py
  - id: 2
    name: Terminal handler rework
    goal: Rework handle_hitl_checkpoint() to dispatch on decision_type with type-specific UX, universal options, JSON resolution payloads, and confirmation display
    tasks:
      - id: TASK-2-1
        description: "Refactor handle_hitl_checkpoint() in sandbox/egg_lib/sdlc_hitl.py to dispatch on decision.get('decision_type', 'choice'). Route to _handle_phase_gate(), _handle_choice(), or _handle_feedback() helper functions. For unknown types, fall back to existing generic menu behavior."
        acceptance: "handle_hitl_checkpoint() reads decision_type and dispatches to the correct handler. Unknown types fall back to generic menu. Function signature unchanged."
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-2-2
        description: "Implement _handle_phase_gate() for phase_gate decisions. Show draft preview, offer numbered options [1] Edit with $EDITOR, [2] Start Claude, [3] Approve, [4] Request changes. On approve, resolve with JSON {\"action\":\"approve\"}. On request changes, prompt for feedback text, resolve with {\"action\":\"request_changes\",\"feedback\":\"...\"}. Show confirmation message."
        acceptance: "Phase gate menu shows edit/claude/approve/request-changes options. Approve resolves with JSON {\"action\":\"approve\"}. Request changes collects feedback and resolves with JSON {\"action\":\"request_changes\",\"feedback\":\"...\"}. Confirmation displayed (e.g., 'Approved: advancing from refine -> plan')."
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-2-3
        description: "Implement _handle_choice() for choice decisions. Read decision['options'] and render as numbered choices [1]-[N]. On selection, resolve with JSON {\"action\":\"select\",\"selected\":\"<option>\"}. Show confirmation message (e.g., 'Selected: MongoDB')."
        acceptance: "Decision options rendered as numbered list. Selection resolves with JSON {\"action\":\"select\",\"selected\":\"...\"}. Confirmation shows selected option text."
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-2-4
        description: "Implement _handle_feedback() for feedback decisions using collect-all-then-review-and-submit pattern. Read decision['questions'] and prompt for each question individually. After all questions answered, display a numbered summary of all answers, then offer three options: [s] Submit answers, [r] Redo a question (prompts 'Which question? (1-N):', re-prompts that question, returns to summary), [c] Cancel (return to main menu). On submit, resolve with JSON {\"action\":\"submit_feedback\",\"answers\":{\"q-id\":\"answer\",...}}. Wrap each input() in try/except (EOFError, KeyboardInterrupt) — on interruption mid-collection, show partial answers with options: [s] Submit partial, [r] Resume from last, [c] Discard all. If decision.questions is empty, fall back to single free-text input."
        acceptance: "Each question prompted individually. After all answers collected, numbered summary displayed. User can redo any individual question by number via [r]. User can submit via [s] or cancel via [c] from summary. Ctrl+C during collection shows partial answers with recovery options. Resolution is JSON {\"action\":\"submit_feedback\",\"answers\":{...}}. Empty questions list falls back to single free-text input."
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-2-5
        description: "Add universal options to all three handler functions. Always show at the bottom of each menu — [f] General feedback (free-text that accompanies primary action), [a] Change approach / suggest different approach (resolves with {\"action\":\"change_approach\",\"feedback\":\"...\"}), [c] Cancel pipeline. General feedback is included as 'feedback' field in the JSON resolution alongside the primary action."
        acceptance: "Universal options [f], [a], [c] appear on every decision type. [f] collects text and includes it in the resolution payload's feedback field alongside the primary action. [a] collects direction text and resolves with change_approach action. [c] cancels pipeline."
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-2-6
        description: "Add confirmation display after every action in all handlers. Show a formatted line confirming what was submitted (e.g., 'Approved: advancing from refine -> plan', 'Selected: MongoDB', 'Feedback submitted (2 answers)', 'Cancelled pipeline')."
        acceptance: "Every resolution path (approve, request changes, select, submit feedback, change approach, cancel) prints a confirmation line with a checkmark and action summary."
        files:
          - sandbox/egg_lib/sdlc_hitl.py
  - id: 3
    name: Tests
    goal: Update existing tests and add coverage for all new behavior including type-aware rendering, JSON resolution parsing, decision endpoint serialization, OrchClient.create_decision(), and backward compatibility
    tasks:
      - id: TASK-3-1
        description: "Update orchestrator/tests/test_models.py for HITLDecision model changes. Add tests for new fields with defaults. Add backward-compat test loading old-format dict without decision_type/questions. Add test for Pipeline.add_decision() with new parameters. Add test that creates a decision with decision_type='phase_gate' via queue_decision() and verifies the stored decision has the correct type."
        acceptance: "Tests verify HITLDecision defaults (decision_type='choice', questions=[]). Old-format dicts parse correctly. Pipeline.add_decision() passes new fields through."
        files:
          - orchestrator/tests/test_models.py
      - id: TASK-3-2
        description: "Add JSON resolution parsing tests to orchestrator/tests/test_hitl_revision.py. Test approve, request_changes, select, change_approach, and submit_feedback action types. Test bare string fallback for 'Approved', 'request changes', and free-text feedback. Test malformed JSON falls back to string matching. Test that hitl_revision_feedback contains readable feedback text, not raw JSON (R-1 verification)."
        acceptance: "All JSON action types route correctly (approve->approval, request_changes->revision, select->approval). Bare strings still work. Malformed JSON treated as bare string. Agent prompt feedback is readable text."
        files:
          - orchestrator/tests/test_hitl_revision.py
      - id: TASK-3-3
        description: "Update tests/sandbox/test_sdlc_hitl.py for type-aware rendering. Add test class for each decision type (phase_gate, choice, feedback). Test that phase_gate shows draft preview and edit/approve/request-changes options. Test that choice renders decision.options as numbered list. Test that feedback prompts each question, shows summary, and supports redo via [r]. Test universal options present on all types. Test JSON resolution payload format. Test fallback to generic menu for unknown decision_type."
        acceptance: "Each decision type has dedicated test methods verifying menu display, option handling, and JSON resolution format. Universal options tested on all types. Fallback tested for unknown decision_type. Existing generic menu tests preserved for backward compatibility."
        files:
          - tests/sandbox/test_sdlc_hitl.py
      - id: TASK-3-4
        description: "Update tests/workflows/test_hitl_integration.py with end-to-end tests for typed decisions. Test that phase gate decisions are created with decision_type='phase_gate'. Test JSON resolution is parsed correctly through the full flow. Test follow-up decision has decision_type='phase_gate' (R-3 verification)."
        acceptance: "Integration test creates a phase gate decision and verifies decision_type field. Integration test resolves with JSON and verifies correct routing. Follow-up decision verified as phase_gate type."
        files:
          - tests/workflows/test_hitl_integration.py
      - id: TASK-3-5
        description: "Add decision endpoint tests for new fields. Test POST create-decision with decision_type and questions parameters. Test POST without new fields (verify defaults applied). Test GET list-decisions and get-decision include decision_type and questions in response serialization. Place tests in the orchestrator test directory alongside existing decision tests."
        acceptance: "POST with decision_type='feedback' and questions list creates decision with correct type. POST without new fields creates decision with decision_type='choice' and questions=[]. GET list/get endpoints include decision_type and questions in response for all decisions."
        files:
          - orchestrator/tests/test_decisions_routes.py
      - id: TASK-3-6
        description: "Add unit tests for OrchClient.create_decision() in tests/sandbox/test_orch_client.py. Verify the method constructs the correct HTTP POST request body with decision_type and questions parameters. Verify default parameter behavior (decision_type='choice', questions=None omitted). Verify response parsing returns decision dict. Verify error handling on HTTP failure."
        acceptance: "Tests verify create_decision() sends correct POST body. Tests verify default params produce expected request. Tests verify response parsing returns decision dict. Tests verify HTTP errors are handled gracefully."
        files:
          - tests/sandbox/test_orch_client.py
      - id: TASK-3-7
        description: "Run full test suite (make test or equivalent) and verify no regressions from all changes."
        acceptance: "All existing tests pass. No new test failures introduced."
        files: []
```

---

*Authored-by: egg*
