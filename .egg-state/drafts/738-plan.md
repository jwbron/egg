# Plan: Add an interactive "pre-refine" step

> Issue: #738 | Phase: plan | Pipeline: issue-738

## Approach

This PR adds a new `PRE_REFINE` pipeline phase that enables conversational
requirements gathering before the autonomous refine phase begins. The phase
is modeled as a first-class pipeline phase (not a pre-pipeline CLI hack or
a special HITL gate) to get clean state tracking, SSE observability, restart
capability, and consistent behavior across local and issue modes.

The core interaction model: when the pipeline enters `PRE_REFINE`, the
orchestrator immediately pauses (`AWAITING_HUMAN`). The `egg-sdlc` CLI
detects this and launches an interactive Claude Code session configured with
a requirements-gathering system prompt. Claude asks probing questions, the
human responds conversationally, and together they produce a requirements
document. When the human approves, the pipeline advances to `REFINE` with
the requirements document as the primary input, replacing the raw issue
body/prompt.

This leverages the existing `_launch_claude()` infrastructure in
`sdlc_hitl.py` (already used for HITL option 2 "AI-assisted editing"),
adapting it with a requirements-focused system prompt and a new draft type.

### Key design decisions

1. **First-class `PRE_REFINE` phase** rather than a pre-pipeline CLI step
   or a HITL gate at refine start. This gives full lifecycle tracking (SSE
   events, visualization, timing, restart) and unambiguous pipeline state.
2. **No container spawned** for pre-refine. The orchestrator immediately
   pauses; the CLI handles the interactive session client-side. This avoids
   the complexity of interactive container execution.
3. **Skippable via `skip_pre_refine` config** (default: `false` for
   interactive CLI, auto-`true` for headless/CI). Well-defined issues can
   bypass requirements gathering.
4. **Requirements stored at `.egg-state/drafts/{id}-requirements.md`**.
   Follows the existing draft path convention (`analysis.md` for refine,
   `plan.md` for plan).
5. **Issue body updated with approved requirements** in issue mode. The
   original issue body is preserved in a collapsed `<details>` section.
6. **No codebase access** during pre-refine. Claude's system prompt
   explicitly excludes repository tools. Requirements are purely
   conversational.

### Backward compatibility

Existing pipelines that start at `REFINE` continue to work. Phase
transition validation accepts `REFINE` as a valid starting phase. The
`skip_pre_refine` flag defaults to `false` but existing pipeline creation
paths that don't pass this flag get the current behavior (start at
`REFINE`). The `PipelinePhase` enum addition is safe because all existing
phase switch/match/if-else chains handle the "else/default" case or
enumerate known phases.

## Phase breakdown

### Phase 1: Pipeline model and phase registration

**Goal:** Register `PRE_REFINE` as a first-class phase in all the places
that define or enumerate pipeline phases. This is the foundation that all
subsequent changes build on.

The `PipelinePhase` enum gains `PRE_REFINE` in both the orchestrator and
shared contracts models. Phase transitions gain `PRE_REFINE -> [REFINE]`.
Phase permissions restrict pre-refine to only writing requirements
documents. Phase defaults give pre-refine a minimal check config. The
`skip_pre_refine` flag is added to `PipelineConfig`.

**Files:**
- `orchestrator/models.py` — Add `PRE_REFINE` to `PipelinePhase` enum,
  add `skip_pre_refine` to `PipelineConfig`
- `shared/egg_contracts/models.py` — Add `PRE_REFINE` to contracts
  `PipelinePhase` enum
- `orchestrator/routes/phases.py` — Add `PRE_REFINE -> [REFINE]` to both
  transition maps
- `shared/egg_contracts/phase_defaults.py` — Add `PRE_REFINE` default
  config (no checks, `ISSUE_CHECKBOX` HITL mechanism)
- `.egg/phase-permissions.json` — Add `pre_refine` phase with minimal
  permissions (write `.egg-state/drafts/*requirements*` only)

### Phase 2: Orchestrator pre-refine dispatch and draft wiring

**Goal:** The orchestrator correctly handles the `PRE_REFINE` phase in the
pipeline execution loop: skip if configured, otherwise pause for human
interaction, and wire the requirements document into the refine prompt.

The `_run_pipeline()` function handles `PRE_REFINE` by immediately setting
`AWAITING_HUMAN` status and queuing a decision. `_get_draft_path()` maps
`pre_refine` to `{prefix}-requirements.md`. When the decision is resolved
with approval, the pipeline reads the requirements document and advances
to `REFINE`. The refine prompt builder is updated to use the requirements
document as primary input when available.

**Files:**
- `orchestrator/routes/pipelines.py` — PRE_REFINE handling in
  `_run_pipeline()`, update `_get_draft_path()`, update
  `_build_phase_prompt()` for refine to use requirements document

### Phase 3: CLI pre-refine interactive session

**Goal:** The CLI detects the pre-refine HITL checkpoint and launches an
interactive Claude session configured for requirements gathering.

A new `handle_pre_refine()` function in `sdlc_hitl.py` presents a
pre-refine-specific menu: (1) launch Claude for requirements gathering,
(2) edit with $EDITOR, (3) approve, (4) cancel. The Claude session uses a
system prompt that guides the conversation toward producing a structured
requirements document (problem statement, functional requirements,
constraints, acceptance criteria). The draft file is written incrementally
so partial work survives crashes. Phase detection (`_detect_phase()`) is
updated to recognize pre-refine.

**Files:**
- `sandbox/egg_lib/sdlc_hitl.py` — Add `handle_pre_refine()`, update
  `_detect_phase()`, add requirements-gathering system prompt
- `sandbox/egg_lib/sdlc_cli.py` — Update `watch_pipeline()` to call
  `handle_pre_refine()` when phase is pre_refine

### Phase 4: Issue mode integration

**Goal:** In issue mode, the approved requirements document updates the
GitHub issue description so that the refine agent (which reads the issue
body) receives the refined requirements.

After the human approves the requirements document in issue mode, the CLI
(or orchestrator) updates the GitHub issue body with the approved
requirements, preserving the original issue body in a collapsed `<details>`
section. This ensures the refine agent reads the clarified requirements
rather than the raw issue description.

**Files:**
- `orchestrator/routes/pipelines.py` — After pre-refine approval in issue
  mode, update issue body via `gh issue edit`
- `sandbox/egg_lib/sdlc_hitl.py` — Pass issue number context for issue
  body update

### Phase 5: Tests

**Goal:** Comprehensive test coverage for all pre-refine functionality.
Existing phase tests continue passing.

**Files:**
- `orchestrator/tests/` — Phase transition validation for PRE_REFINE,
  skip logic, draft path resolution, refine prompt with requirements input
- `shared/egg_contracts/tests/` — PipelinePhase enum serialization,
  phase defaults for PRE_REFINE
- `tests/` — Integration tests for pre-refine -> refine flow, backward
  compatibility with existing pipelines

## Test strategy

1. **Unit tests** for each new component:
   - `PRE_REFINE` phase transition validation (can only go to `REFINE`)
   - `_get_draft_path()` returns `{prefix}-requirements.md` for pre_refine
   - `_build_phase_prompt()` uses requirements document as refine input
   - `skip_pre_refine` config causes pipeline to start at `REFINE`
   - `_detect_phase()` recognizes "pre_refine" and "requirements" keywords
   - Phase defaults for `PRE_REFINE` return correct config
   - Phase permissions for `pre_refine` restrict file access

2. **Integration tests** for end-to-end flows:
   - Pipeline creation with `skip_pre_refine=false` starts at `PRE_REFINE`
   - Pipeline creation with `skip_pre_refine=true` starts at `REFINE`
   - Pre-refine approval advances pipeline to `REFINE`
   - Requirements document is passed as input to refine phase
   - Issue body is updated with requirements in issue mode

3. **Backward compatibility tests**:
   - Existing pipelines without `PRE_REFINE` state deserialize correctly
   - Phase transitions from `REFINE` still work (no mandatory pre-refine)
   - `PipelineConfig` without `skip_pre_refine` defaults correctly

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| PRE_REFINE enum touches many phase-handling code paths | High | Medium | Grep for all PipelinePhase usage; most paths have default/else handlers. Test each. |
| Interactive Claude session may not work in headless CI | High | Low | `skip_pre_refine` defaults to `true` when no TTY detected. |
| Requirements doc quality depends on human engagement | Medium | Low | Claude's system prompt guides with probing questions. Refine agent still does its own analysis. |
| Existing pipelines in REFINE state see unknown PRE_REFINE value | Medium | Medium | Phase transition accepts REFINE as starting phase. Deserialization handles missing pre_refine state. |
| Claude session crash loses partial requirements work | Medium | Medium | Draft file written incrementally. Pipeline stays in PRE_REFINE/AWAITING_HUMAN for resume. |

```yaml
# yaml-tasks
pr:
  title: "Add interactive pre-refine step to SDLC pipeline"
  description: |
    Adds a new PRE_REFINE pipeline phase for conversational requirements
    gathering before the autonomous refine phase. When the pipeline enters
    pre-refine, it pauses for human interaction. The CLI launches an
    interactive Claude session that guides the human through producing a
    structured requirements document. The approved document replaces the
    raw issue body as input to the refine phase. The step is skippable
    for well-defined issues and headless CI.
phases:
  - id: 1
    name: Pipeline model and phase registration
    goal: Register PRE_REFINE as a first-class phase in enums, transitions, permissions, and config
    tasks:
      - id: TASK-1-1
        description: Add PRE_REFINE = "pre_refine" to PipelinePhase enum in orchestrator/models.py
        acceptance: PipelinePhase.PRE_REFINE is a valid enum member; existing phases unchanged
        files:
          - orchestrator/models.py
      - id: TASK-1-2
        description: Add PRE_REFINE = "pre_refine" to PipelinePhase enum in shared/egg_contracts/models.py
        acceptance: Both PipelinePhase enums are in sync with PRE_REFINE as a member
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Add PRE_REFINE -> [REFINE] to PHASE_TRANSITIONS and LOCAL_PHASE_TRANSITIONS in phases.py
        acceptance: validate_phase_transition() allows PRE_REFINE -> REFINE and rejects PRE_REFINE -> PLAN/IMPLEMENT/PR
        files:
          - orchestrator/routes/phases.py
      - id: TASK-1-4
        description: Add pre_refine phase entry to .egg/phase-permissions.json with minimal permissions (write .egg-state/drafts/*requirements* only, no PR creation, no code changes)
        acceptance: Gateway enforces pre_refine file restrictions; only requirements draft files are writable
        files:
          - .egg/phase-permissions.json
      - id: TASK-1-5
        description: Add PRE_REFINE default PhaseConfig to phase_defaults.py (no checks, ISSUE_CHECKBOX mechanism, max 3 review cycles)
        acceptance: get_default_phase_config(PipelinePhase.PRE_REFINE) returns valid config without raising KeyError
        files:
          - shared/egg_contracts/phase_defaults.py
      - id: TASK-1-6
        description: Add skip_pre_refine boolean field to PipelineConfig (default false)
        acceptance: PipelineConfig.skip_pre_refine exists; defaults to false; serializes/deserializes correctly
        files:
          - orchestrator/models.py
  - id: 2
    name: Orchestrator pre-refine dispatch and draft wiring
    goal: Handle PRE_REFINE in the orchestration loop with skip logic, HITL pause, and requirements-to-refine handoff
    dependencies:
      - phase-1
    tasks:
      - id: TASK-2-1
        description: Add PRE_REFINE handling in _run_pipeline() — if skip_pre_refine is true, advance directly to REFINE; otherwise set status to AWAITING_HUMAN and queue a decision with the original issue body/prompt as context
        acceptance: Pipeline with skip_pre_refine=false pauses at PRE_REFINE with AWAITING_HUMAN status; pipeline with skip_pre_refine=true skips directly to REFINE
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: Update _get_draft_path() to map pre_refine phase to {prefix}-requirements.md
        acceptance: _get_draft_path("pre_refine", ...) returns correct path for both issue and local modes
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: Update _build_phase_prompt() for the refine phase to use the requirements document as primary input when it exists, falling back to the raw issue body/prompt when no requirements document is available
        acceptance: When requirements doc exists, refine prompt includes it as primary context; when absent, refine prompt uses raw issue body as before
        files:
          - orchestrator/routes/pipelines.py
  - id: 3
    name: CLI pre-refine interactive session
    goal: CLI launches an interactive Claude session for requirements gathering when pipeline is in pre-refine
    dependencies:
      - phase-2
    tasks:
      - id: TASK-3-1
        description: Add handle_pre_refine() function in sdlc_hitl.py that presents a pre-refine menu (Launch Claude for requirements gathering, Edit with $EDITOR, Approve, Cancel) and manages the requirements document lifecycle
        acceptance: Menu displays correctly; Claude launch works; approve resolves the HITL decision; cancel cancels the pipeline
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-2
        description: Configure the Claude session in handle_pre_refine() with a requirements-gathering system prompt that guides conversation toward problem statement, functional requirements, constraints, and acceptance criteria. Claude should write to the requirements draft file incrementally.
        acceptance: Claude session starts with requirements-focused system prompt; draft file is created/updated during session; no codebase access tools available
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-3
        description: Update _detect_phase() to recognize pre_refine phase from decision question text (match "pre-refine", "pre_refine", "requirements")
        acceptance: _detect_phase() returns "pre_refine" for pre-refine decision questions
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-4
        description: Update watch_pipeline() in sdlc_cli.py to route pre_refine HITL checkpoints to handle_pre_refine() instead of the generic handle_hitl_checkpoint()
        acceptance: When pipeline is in pre_refine phase and awaiting_human, CLI calls handle_pre_refine(); other phases continue using handle_hitl_checkpoint()
        files:
          - sandbox/egg_lib/sdlc_cli.py
  - id: 4
    name: Issue mode integration
    goal: Update GitHub issue description with approved requirements in issue mode
    dependencies:
      - phase-2
      - phase-3
    tasks:
      - id: TASK-4-1
        description: After pre-refine approval in issue mode, update the GitHub issue body with the approved requirements document content, preserving the original issue body in a collapsed details section at the bottom
        acceptance: Issue body is updated via gh issue edit; original body preserved in collapsed section; refine agent reads updated issue body
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-4-2
        description: Handle edge case where pipeline is restarted after pre-refine was already approved — detect existing requirements document and skip re-gathering
        acceptance: Re-running a pipeline that already has a requirements document skips pre-refine or presents the existing doc for re-approval
        files:
          - orchestrator/routes/pipelines.py
  - id: 5
    name: Tests
    goal: Comprehensive test coverage for all pre-refine changes; existing tests pass unchanged
    dependencies:
      - phase-1
      - phase-2
      - phase-3
      - phase-4
    tasks:
      - id: TASK-5-1
        description: Write unit tests for PRE_REFINE phase transitions (PRE_REFINE -> REFINE allowed; PRE_REFINE -> PLAN/IMPLEMENT/PR rejected; REFINE as starting phase still valid)
        acceptance: All transition validation tests pass
        files:
          - orchestrator/tests/test_phases.py
      - id: TASK-5-2
        description: Write unit tests for _get_draft_path() returning requirements.md for pre_refine in both issue and local modes
        acceptance: Draft path tests cover pre_refine for issue mode ({issue_number}-requirements.md) and local mode ({pipeline_id}-requirements.md)
        files:
          - orchestrator/tests/test_pipelines.py
      - id: TASK-5-3
        description: Write unit tests for skip_pre_refine config (pipeline starts at REFINE when true, PRE_REFINE when false)
        acceptance: Skip logic tests verify correct starting phase based on config
        files:
          - orchestrator/tests/test_pipelines.py
      - id: TASK-5-4
        description: Write unit tests for _build_phase_prompt() using requirements document as refine input when available, falling back to raw prompt when absent
        acceptance: Prompt building tests verify requirements doc inclusion and fallback behavior
        files:
          - orchestrator/tests/test_pipelines.py
      - id: TASK-5-5
        description: Write unit tests for _detect_phase() recognizing pre_refine keywords
        acceptance: Phase detection tests cover "pre-refine", "pre_refine", "requirements" patterns
        files:
          - sandbox/tests/test_sdlc_hitl.py
      - id: TASK-5-6
        description: Write unit tests for PRE_REFINE phase defaults and PipelineConfig.skip_pre_refine serialization
        acceptance: Phase defaults return valid config; PipelineConfig round-trips with skip_pre_refine field
        files:
          - shared/egg_contracts/tests/test_phase_defaults.py
          - orchestrator/tests/test_models.py
      - id: TASK-5-7
        description: Verify existing tests pass unchanged (phase transition tests, pipeline tests, HITL tests)
        acceptance: All pre-existing tests pass without modification; no regressions
        files:
          - orchestrator/tests/
          - shared/egg_contracts/tests/
```
