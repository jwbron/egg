# Analysis: Improve egg-sdlc local HITL — type-aware rendering for decisions, feedback, and phase approval

> Issue: #900 | Phase: refine

## Problem Statement

The local `egg-sdlc` HITL checkpoint handler (`sandbox/egg_lib/sdlc_hitl.py`) uses a single generic 5-option menu for all human input, regardless of what type of input is needed. Whether the human is approving a phase, choosing between database options, or answering open-ended questions, the same `[1]-[5]` menu appears. This creates six distinct UX problems:

1. **No distinction between input types.** Phase approval, multiple-choice decisions, and open-ended feedback all use the same menu.
2. **Decision options aren't surfaced.** When an agent queues a decision with specific options (e.g., "PostgreSQL vs MongoDB vs SQLite"), the terminal only shows the question text — the options are invisible. The `decision.options` field is never read or displayed in the handler.
3. **No confirmation of received input.** After the human submits, the pipeline continues without showing a summary of what was captured.
4. **Feedback is unstructured.** Option `[4]` collects raw multi-line text with no per-question prompting when the agent asked multiple specific questions.
5. **No structured feedback in local mode.** The contract system's `feedback` field (multi-question with individual answers) only flows through GitHub comments. In local mode, there is no equivalent — agents cannot request structured multi-question feedback that renders in the terminal.
6. **No "change approach" option.** The menu lacks an explicit way for the human to signal "this approach is wrong, start over" vs. "here's some feedback on the current work." All non-approval input is treated identically.

The desired outcome is a type-aware HITL renderer: each input type (`phase_gate`, `choice`, `feedback`) gets a UX tailored to its purpose, with confirmation after submission and universal options (general feedback, change approach, cancel) always available on every checkpoint.

## Current Behavior

### HITLDecision Model (`orchestrator/models.py:169-182`)

The `HITLDecision` model has these fields: `id`, `question`, `context`, `options` (list of strings), `status`, `created_at`, `resolved_at`, `resolution` (a plain string or null). There is:

- **No `decision_type` field** — the model cannot distinguish a phase gate from a discrete choice from a feedback request.
- **No `questions` field** — there is no way to represent multi-question structured feedback.
- **Resolution is a plain string** — the orchestrator parses resolution intent via keyword matching (`_APPROVE_KEYWORDS`, `_BARE_OPTION_LABELS` in `pipelines.py:5889-5892`), not structured payloads.

### Phase Gate Creation (`orchestrator/routes/pipelines.py:5841-5862`)

When a phase completes and HITL gates are enabled, the orchestrator creates a decision via `dq.queue_decision()` with:
- `question`: `"The {phase} phase has completed. Please review the {label} and approve to continue, or provide feedback to request changes."`
- `options`: `["approve", "request changes"]`
- `context`: the draft content

This phase gate decision is structurally identical to any agent-created multiple-choice decision. The terminal handler uses regex on the question text (`_detect_phase()` at `sdlc_hitl.py:343-359`) to guess whether it's a phase gate.

### Terminal Handler (`sandbox/egg_lib/sdlc_hitl.py:219-341`)

`handle_hitl_checkpoint()` always presents the same 5-option menu regardless of decision type:

```
[1] Edit with $EDITOR (vim)
[2] Start Claude for AI-assisted editing
[3] Approve and advance to next phase
[4] Provide feedback (text input)
[5] Cancel pipeline
```

Key gaps:
- `decision.options` is never read or displayed — agent-specified options are invisible.
- Feedback is collected as a single raw text block via `_prompt_text()`.
- Resolution is sent as a plain string ("Approved" or the feedback text).
- No confirmation of what was submitted.

### OrchClient (`sandbox/egg_lib/orch_client.py:180-196`)

The `OrchClient` has `list_decisions()` and `resolve_decision()` but **no `create_decision()` method**. Agents inside containers cannot programmatically create decisions through the orchestrator API — they rely on `egg-contract` for the GitHub-based decision/feedback flow, which doesn't apply to local mode.

### Decision Creation Endpoint (`orchestrator/routes/decisions.py:138-192`)

The POST endpoint accepts `question`, `context`, and `options` but has no support for `decision_type` or `questions` fields. The response serialization (`decisions.py:109-121`) also omits these fields.

### Resolution Handling (`orchestrator/routes/pipelines.py:5885-5970`)

The orchestrator checks the resolution string against `_APPROVE_KEYWORDS` (e.g., "approved", "approve", "lgtm", "yes", "") and `_BARE_OPTION_LABELS` (e.g., "request changes"). If the resolution contains actual feedback text, it triggers a phase re-run with `hitl_revision_feedback` injected into the agent prompt. A circuit breaker (`max_hitl_review_cycles`, default 3) prevents unbounded revision loops.

### Watch Loop (`sandbox/egg_lib/sdlc_cli.py:268-288`)

The SSE watch loop detects `awaiting_human` status, fetches the first pending decision, and calls `handle_hitl_checkpoint()`. On resolution, it breaks the inner loop and reconnects to SSE. This architecture naturally supports re-runs since the orchestrator emits a new `awaiting_human` event when the next checkpoint arrives.

## Constraints

### Technical

- **Backward compatibility**: Existing `.egg-state/pipelines/*.json` files contain serialized `HITLDecision` objects without `decision_type` or `questions` fields. New fields must have defaults so old state files remain loadable.
- **Resolution parsing**: The orchestrator currently checks resolutions via string keyword matching. Switching to JSON resolution payloads requires a fallback path for legacy plain-text resolutions (during rolling upgrades or with older terminal clients).
- **API compatibility**: The decision creation endpoint must accept the new fields without breaking existing consumers that don't send them.
- **Agent code**: Existing agents using `resolve_decision()` with plain strings must continue to work.
- **Terminal constraints**: The handler runs in a standard terminal (not a TUI framework). It uses `input()` for interactive prompts and `subprocess` for editor/Claude launches. Must not introduce dependencies on curses or other TUI libraries.
- **Serialization round-trip**: Decision objects are serialized to JSON for both the REST API and the state store. New fields must serialize cleanly through both paths.

### Scope boundaries (from issue)

- **In scope**: `orchestrator/models.py`, `orchestrator/routes/decisions.py`, `orchestrator/routes/pipelines.py`, `sandbox/egg_lib/orch_client.py`, `sandbox/egg_lib/sdlc_hitl.py`.
- **Out of scope**: GitHub issue comment HITL (issue-mode path), `egg-contract` CLI, contract schema, shared models. The issue explicitly states these are unchanged.

### Dependencies

- The `sdlc_cli.py` watch loop should require no changes (the issue expects this, and the architecture supports it).
- Existing test suites (`orchestrator/tests/test_hitl_revision.py`, `integration_tests/local_pipeline/test_hitl_edge_cases.py`, `integration_tests/sdlc/test_hitl_flow.py`) will need updating to cover new fields and type-aware behavior.

## Options Considered

### Option A: Implement as specified in the issue

**Approach**: Follow the issue's design exactly — add `decision_type` and `questions` fields to `HITLDecision`, update all endpoints, add `create_decision()` to `OrchClient`, rework `handle_hitl_checkpoint()` to dispatch on `decision_type`, switch resolution payloads to structured JSON.

**Pros**:
- Cleanly separates the three input types with an explicit `decision_type` field rather than fragile heuristics
- Gives local mode parity with GitHub-based structured feedback
- Universal options (general feedback, change approach, cancel) available on every checkpoint
- JSON resolution payloads let the orchestrator parse human intent without keyword matching
- The issue author has already designed the UX mockups, resolution formats, and field schemas in detail

**Cons**:
- Requires updating the orchestrator's resolution parsing in `pipelines.py` to handle both JSON and legacy string resolutions during the transition
- Adds complexity to the `HITLDecision` model (two new fields, one with nested structure)
- The `questions` field uses `list[dict]` which is flexible but has no schema enforcement at the model level

### Option B: Minimal — type detection via heuristics, no model changes

**Approach**: Keep the `HITLDecision` model unchanged. Instead, have `handle_hitl_checkpoint()` infer the decision type from existing fields: if `options` contains "approve" -> phase gate; if `options` is non-empty -> choice; else -> feedback. No structured feedback support; skip `create_decision()` in OrchClient.

**Pros**:
- No model or API changes — only the terminal handler changes
- Lower risk, faster to implement
- No backward compatibility concerns

**Cons**:
- Heuristic detection is fragile (what if an agent creates a decision with an "approve" option that isn't a phase gate?)
- No structured multi-question feedback in local mode (problem #5 unsolved)
- Agents still can't create typed decisions programmatically
- Resolution remains a plain string — no way to distinguish "change approach" from regular feedback
- Doesn't solve the fundamental problem; it's a polish layer on a structurally limited model

### Option C: Extend model but defer structured feedback

**Approach**: Add `decision_type` to `HITLDecision` and update the terminal handler for `phase_gate` and `choice` types, but defer the `questions` field and multi-question feedback UX to a follow-up issue. Add `create_decision()` to OrchClient. Use JSON resolution payloads.

**Pros**:
- Addresses 5 of 6 problems with a smaller scope
- Gets the most impactful changes (type-aware rendering, option display, change approach, confirmation) shipped first
- `questions` field and feedback UX can be designed with learnings from the type-aware rendering
- Reduces risk of getting the multi-question UX wrong

**Cons**:
- Structured feedback (problem #5) remains unsolved, requiring a follow-up issue
- Two rounds of changes to the model and API instead of one
- The issue describes a cohesive design — splitting it may create inconsistencies

## Recommended Approach

**Option A: Implement as specified in the issue.**

The issue provides a thorough, well-designed specification with UX mockups, resolution payload schemas, and a clear file-by-file change list. The six problems are interdependent — e.g., the `questions` field enables the feedback UX, which relies on `decision_type` dispatch, which feeds into the JSON resolution format. Implementing them together avoids having to revisit the resolution parsing twice.

The backward compatibility risks are manageable: Pydantic's default values handle old state files, and a fallback path for legacy string resolutions (try JSON parse, fall back to keyword matching) is straightforward.

The main implementation risk is the scope — this touches five files across two packages (orchestrator and sandbox). However, the changes are well-contained: model extension, endpoint updates, client method addition, and terminal handler rework. Each piece can be tested in isolation.

## Open Questions

### Questions addressed to the issue author

1. **`decision_type` enforcement**: Should `decision_type` be validated as an enum or kept as a free-form string? The issue says `str` with values `phase_gate`, `choice`, `feedback`, but a `Literal` or enum would prevent typos. Using an enum risks breaking forward compatibility if new types are added later.

2. **Default `decision_type` for existing decisions**: The issue says `default: "choice"`. When loading old state files that lack `decision_type`, they'll all appear as `choice`. Is that the right default, or should it be `None`/`"unknown"` to make the absence explicit? Phase gates created before this change would render as choices.

3. **`questions` field schema**: The issue describes `list[dict]` with `id`, `question`, `answer` keys. Should this use a nested Pydantic model (e.g., `FeedbackQuestion`) for type safety, or stay as untyped dicts for flexibility? A nested model would enforce structure but be harder to extend later.

4. **JSON resolution backward compatibility**: When the orchestrator receives a resolution, it currently does string matching. After this change, it needs to try JSON parsing first, then fall back to string matching. Should the fallback be permanent (to support older terminal clients) or time-limited (deprecated after N releases)?

5. **Follow-up decision handling**: Currently, when the human selects "request changes" without providing feedback, the orchestrator re-queues a follow-up decision (`pipelines.py:5899-5907`). With JSON resolution payloads, `action: "request_changes"` always includes a `feedback` field. Does this eliminate the need for the follow-up decision flow entirely, or should the follow-up be kept as a safety net?

6. **Phase re-run loop visibility**: The issue says "the terminal shows the updated draft/results and the menu again" after a phase re-run. Currently the watch loop reconnects to SSE and waits for the next `awaiting_human` event. Should there be any explicit terminal output during the re-run (e.g., a spinner, phase status updates), or is the existing SSE visualization sufficient?

7. **Editor/Claude options on non-phase-gate checkpoints**: The current menu always shows "Edit with $EDITOR" and "Start Claude." The issue's mockups only show these for `phase_gate` decisions. Should `choice` and `feedback` checkpoints omit the editor options entirely, or keep them available for editing the draft while answering?

8. **`create_decision()` in OrchClient — who calls it?**: The issue says agents can create typed decisions from within containers. In practice, during the implement phase, agents use `egg-contract add-decision` (GitHub-based). What is the expected use case for `OrchClient.create_decision()` in local mode? Is this for agents running in local pipelines to create ad-hoc decisions, or for some other orchestrator-internal purpose?

9. **Cancel pipeline scope**: The current "Cancel pipeline" option calls `client.cancel_pipeline()`. The issue's mockup shows `[c] Cancel pipeline` as a universal option. Should cancellation also be represented in the JSON resolution payload (e.g., `{"action": "cancel"}`), or should it continue using the separate `cancel_pipeline()` API call?

10. **General feedback alongside primary action**: The issue shows that "general feedback" can accompany any action (e.g., approve with feedback, select option with feedback). The resolution payloads include a `feedback` field alongside `action`. Should this general feedback be injected into the agent's prompt on the *next* phase, even when the current phase is approved? Currently, feedback only triggers a re-run — there's no mechanism to pass advisory feedback forward.

---

*Authored-by: egg*

<!-- metadata -->
# metadata
complexity_tier: high
parallel_phases: false
