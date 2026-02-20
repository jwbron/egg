# Plan: Add an interactive "pre-refine" step

> Issue: #738 | Phase: plan | Pipeline: issue-738 | Revision: 3

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
3. **Skippable via `skip_pre_refine` config** (default: `true` in
   PipelineConfig for backward compatibility; the interactive CLI explicitly
   sets `false`). Well-defined issues and headless/CI callers can bypass
   requirements gathering without any code changes.
4. **Requirements stored at `.egg-state/drafts/{id}-requirements.md`**.
   Follows the existing draft path convention (`analysis.md` for refine,
   `plan.md` for plan).
5. **Issue body updated with approved requirements** in issue mode. The
   original issue body is preserved in a collapsed `<details>` section.
6. **No codebase access** during pre-refine. Claude's system prompt
   explicitly excludes repository tools. Requirements are purely
   conversational.
7. **Starting phase set by `_run_pipeline()` logic**, not by changing the
   Pipeline model default. The `Pipeline.current_phase` default stays
   `REFINE` (orchestrator/models.py). At the start of `_run_pipeline()`,
   the function checks `PipelineConfig.skip_pre_refine`: if `false` AND
   no existing requirements document exists, it sets `current_phase` to
   `PRE_REFINE` before entering the phase loop.

### Backward compatibility

Existing pipelines that start at `REFINE` continue to work unmodified:

- **Pipeline model default unchanged**: `Pipeline.current_phase` defaults to
  `PipelinePhase.REFINE` (no change to orchestrator/models.py).
- **Config default is skip**: `PipelineConfig.skip_pre_refine` defaults to
  `true`. Existing callers that create pipelines without passing this flag
  get `skip_pre_refine=true` and start at REFINE as before.
- **CLI opt-in**: Only the interactive `egg-sdlc` CLI explicitly sets
  `skip_pre_refine=false` via the `config` dict parameter on
  `create_pipeline()`. A `--skip-pre-refine` flag overrides this.
- **Phase transitions still valid**: REFINE remains a valid starting phase.
  Phase transition validation accepts both PRE_REFINE -> REFINE and
  starting directly at REFINE.
- **Gateway dicts updated**: All explicit dict lookups in
  `gateway/phase_filter.py` (`PhasePermissions` at line 411,
  `PhaseFileRestriction` at line 489) and `gateway/phase_transition.py`
  (`VALID_TRANSITIONS` at line 41) include PRE_REFINE entries. These are
  NOT fallback-safe — they require explicit entries to avoid KeyError.
- **Phase defaults updated**: `_DEFAULT_PHASE_CONFIGS` in
  `shared/egg_contracts/phase_defaults.py` (line 80) includes PRE_REFINE.
  `get_default_phase_config()` does a direct dict lookup (line 114) that
  would KeyError without the entry.

### Review feedback addressed (revision 2→3)

All 5 items from the plan review are addressed:

1. **Gateway phase infrastructure** → TASK-1-7 (phase_filter.py enum +
   PhasePermissions + PhaseFileRestriction), TASK-1-8 (phase_transition.py
   VALID_TRANSITIONS)
2. **dag_visualizer.py** → TASK-1-9 (PHASE_ORDER + PHASE_NAMES)
3. **`_get_draft_path()` in sdlc_hitl.py** → TASK-3-5 (explicit pre_refine
   branch before generic else fallback)
4. **Pipeline starting phase ambiguity** → TASK-2-1 specifies the mechanism:
   `_run_pipeline()` checks `skip_pre_refine` at startup and conditionally
   sets `current_phase` to `PRE_REFINE`. Pipeline model default stays REFINE.
5. **Backward compatibility contradiction** → `skip_pre_refine` defaults to
   `true` (not false). Only the interactive CLI explicitly sets `false`.

## Phase breakdown

### Phase 1: Pipeline model and phase registration (all locations)

**Goal:** Register `PRE_REFINE` as a first-class phase in ALL locations that
define or enumerate pipeline phases. This is the foundation that all
subsequent changes build on.

The `PipelinePhase` enum gains `PRE_REFINE` in all three locations:
orchestrator, contracts, and gateway. Phase transitions gain
`PRE_REFINE -> [REFINE]` in both orchestrator and gateway. The gateway's
`PhasePermissions` and `PhaseFileRestriction` dicts gain entries for
PRE_REFINE. The DAG visualizer gains PRE_REFINE in its phase order and
names. Phase defaults and config are updated.

**Files:**
- `orchestrator/models.py` — Add `PRE_REFINE` to `PipelinePhase` enum
  (line 15), add `skip_pre_refine` to `PipelineConfig` (default: `true`)
- `shared/egg_contracts/models.py` — Add `PRE_REFINE` to contracts
  `PipelinePhase` enum (line 34)
- `gateway/phase_filter.py` — Add `PRE_REFINE` to the gateway
  `PipelinePhase` enum (line 28), add entry to `PhasePermissions` dict in
  `_get_default_permissions()` (line 411), add entry to
  `PhaseFileRestriction` dict in `_get_default_phase_file_restrictions()`
  (line 489)
- `gateway/phase_transition.py` — Add `PRE_REFINE -> [REFINE]` to
  `VALID_TRANSITIONS` dict (line 41)
- `orchestrator/routes/phases.py` — Add `PRE_REFINE -> [REFINE]` to both
  `PHASE_TRANSITIONS` (line 50) and `LOCAL_PHASE_TRANSITIONS` (line 58)
- `shared/egg_contracts/phase_defaults.py` — Add `PRE_REFINE` entry to
  `_DEFAULT_PHASE_CONFIGS` dict (line 80) with no checks, `ISSUE_CHECKBOX`
  mechanism, max 3 review cycles
- `.egg/phase-permissions.json` — Add `pre_refine` phase with minimal
  permissions (write `.egg-state/drafts/*requirements*` only)
- `orchestrator/dag_visualizer.py` — Insert `PRE_REFINE` at index 0 of
  `PHASE_ORDER` (line 49), add `PRE_REFINE: 'Pre-Refine'` to `PHASE_NAMES`
  (line 57)

### Phase 2: Orchestrator pre-refine dispatch and draft wiring

**Goal:** The orchestrator correctly handles the `PRE_REFINE` phase in the
pipeline execution loop: skip if configured, otherwise pause for human
interaction, and wire the requirements document into the refine prompt.

**Starting phase mechanism** (in `_run_pipeline()`): At startup before the
phase loop, check `PipelineConfig.skip_pre_refine`. If `false` AND no
existing requirements document at the expected draft path, set
`pipeline.current_phase` to `PRE_REFINE`. This is how pipelines get routed
to pre-refine without changing the Pipeline model default.

**PRE_REFINE phase handler**: When the phase loop reaches `PRE_REFINE`, set
status to `AWAITING_HUMAN` and queue a decision with the original issue
body/prompt as context. When the decision is resolved with approval, read
the requirements document and advance to `REFINE`.

**Draft path**: `_get_draft_path()` maps `pre_refine` to
`{prefix}-requirements.md`.

**Refine input wiring**: `_build_phase_prompt()` for the refine phase checks
for a requirements document at the expected draft path. If found, it uses
the requirements document as primary input context. If absent, it falls
back to the raw issue body/prompt.

**Files:**
- `orchestrator/routes/pipelines.py` — PRE_REFINE handling in
  `_run_pipeline()` (starting phase logic + phase handler), update
  `_get_draft_path()`, update `_build_phase_prompt()` for refine to use
  requirements document

### Phase 3: CLI pre-refine interactive session

**Goal:** The CLI detects the pre-refine HITL checkpoint and launches an
interactive Claude session configured for requirements gathering.

**Draft path mirror**: Update `_get_draft_path()` in `sdlc_hitl.py`
(line 27) to add an explicit branch for `pre_refine` ->
`{prefix}-requirements.md`. This MUST be an explicit branch BEFORE the
generic else fallback (which would incorrectly produce
`{prefix}-pre_refine.md`). Mirrors TASK-2-2 in orchestrator.

**Requirements-gathering session**: A new `handle_pre_refine()` function
presents a pre-refine-specific menu: (1) launch Claude for requirements
gathering, (2) edit with $EDITOR, (3) approve, (4) cancel. The Claude
session uses a system prompt that guides the conversation toward producing
a structured requirements document (problem statement, functional
requirements, constraints, acceptance criteria). The draft file is written
incrementally so partial work survives crashes.

**Phase detection**: `_detect_phase()` (line 343) is updated to recognize
pre-refine from decision question text (match "pre-refine", "pre_refine",
"requirements").

**CLI routing**: `watch_pipeline()` in `sdlc_cli.py` routes pre_refine
HITL checkpoints to `handle_pre_refine()` instead of the generic
`handle_hitl_checkpoint()`.

**Files:**
- `sandbox/egg_lib/sdlc_hitl.py` — Update `_get_draft_path()` for
  pre_refine, add `handle_pre_refine()`, update `_detect_phase()`, add
  requirements-gathering system prompt
- `sandbox/egg_lib/sdlc_cli.py` — Update `watch_pipeline()` to call
  `handle_pre_refine()` when phase is pre_refine

### Phase 4: Issue mode integration and pipeline creation

**Goal:** In issue mode, the approved requirements document updates the
GitHub issue description so that the refine agent (which reads the issue
body) receives the refined requirements. Update pipeline creation to
support skip_pre_refine.

**Issue body update**: After the human approves the requirements document in
issue mode, the CLI (or orchestrator) updates the GitHub issue body with the
approved requirements, preserving the original issue body in a collapsed
`<details>` section. This ensures the refine agent reads the clarified
requirements rather than the raw issue description.

**Restart handling**: If a pipeline is restarted after pre-refine was
already completed, detect the existing requirements document and skip
re-gathering (the `_run_pipeline()` startup logic already handles this
via the "no existing requirements document" check in TASK-2-1).

**CLI flag**: Add `--skip-pre-refine` flag to egg-sdlc CLI. Interactive
mode sets `skip_pre_refine=false` by default. The flag is passed through
`orch_client.create_pipeline()` via the existing `config` dict parameter
(orch_client.py line 160 already accepts `config: dict`). No new parameter
needed on `create_pipeline()`.

**Files:**
- `orchestrator/routes/pipelines.py` — After pre-refine approval in issue
  mode, update issue body via `gh issue edit`
- `sandbox/egg_lib/sdlc_hitl.py` — Pass issue number context for issue
  body update
- `sandbox/egg_lib/sdlc_cli.py` — Add `--skip-pre-refine` flag, set
  `skip_pre_refine=false` for interactive mode, pass in `config` dict
- `sandbox/egg_lib/orch_client.py` — No changes needed; `create_pipeline()`
  already accepts `config` dict

### Phase 5: Tests

**Goal:** Comprehensive test coverage for all pre-refine functionality.
Existing tests pass unchanged.

**Files:**
- `orchestrator/tests/` — Phase transition validation for PRE_REFINE,
  skip logic, draft path resolution, refine prompt with requirements input,
  dag_visualizer PHASE_ORDER/PHASE_NAMES
- `shared/egg_contracts/tests/` — PipelinePhase enum serialization,
  phase defaults for PRE_REFINE
- `gateway/tests/` — Gateway phase enum, PhasePermissions, PhaseFileRestriction,
  VALID_TRANSITIONS for PRE_REFINE
- `sandbox/tests/` — _detect_phase() recognition, _get_draft_path() for
  pre_refine

## Test strategy

1. **Unit tests** for each new component:
   - `PRE_REFINE` phase transition validation (can only go to `REFINE`) —
     both orchestrator and gateway transition maps
   - `_get_draft_path()` returns `{prefix}-requirements.md` for pre_refine
     — both orchestrator and sdlc_hitl.py versions
   - `_build_phase_prompt()` uses requirements document as refine input
   - `skip_pre_refine` config causes pipeline to start at `REFINE`
   - `_detect_phase()` recognizes "pre_refine", "pre-refine", "requirements"
   - Phase defaults for `PRE_REFINE` return correct config
   - Phase permissions for `pre_refine` restrict file access
   - Gateway `PhasePermissions` and `PhaseFileRestriction` include PRE_REFINE
   - `dag_visualizer` PHASE_ORDER includes PRE_REFINE at index 0

2. **Integration tests** for end-to-end flows:
   - Pipeline with `skip_pre_refine=false` starts at `PRE_REFINE`
   - Pipeline with `skip_pre_refine=true` (or default) starts at `REFINE`
   - Pre-refine approval advances pipeline to `REFINE`
   - Requirements document is passed as input to refine phase
   - Issue body is updated with requirements in issue mode

3. **Backward compatibility tests**:
   - Existing pipelines without `PRE_REFINE` state deserialize correctly
   - Phase transitions from `REFINE` still work (no mandatory pre-refine)
   - `PipelineConfig` without `skip_pre_refine` defaults to `true` (skip)

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| PRE_REFINE enum touches 3 enums, 2 transition maps, 2 gateway dicts, 1 visualizer, 1 phase defaults dict | High | Medium | Phase 1 comprehensively lists all 8 locations with line numbers. Each is a discrete, additive change. |
| Interactive Claude session may not work in headless CI | High | Low | `skip_pre_refine` defaults to `true`. Only interactive CLI opts in. |
| Gateway dict lookups KeyError without PRE_REFINE entries | High | High | TASK-1-7 and TASK-1-8 explicitly add PRE_REFINE to all gateway dicts. |
| `phase_defaults.py` `_DEFAULT_PHASE_CONFIGS` KeyError | High | High | TASK-1-5 adds PRE_REFINE to the dict. `get_default_phase_config()` does direct dict lookup. |
| sdlc_hitl.py `_get_draft_path()` produces wrong filename | High | Medium | TASK-3-5 adds explicit pre_refine branch (not relying on generic else). |
| Pipeline never reaches PRE_REFINE if model default stays REFINE | High | High | TASK-2-1 specifies `_run_pipeline()` startup logic to set current_phase to PRE_REFINE when `skip_pre_refine=false`. |
| Requirements doc quality depends on human engagement | Medium | Low | Claude's system prompt guides with probing questions. Refine agent still does its own analysis. |
| Existing pipelines in REFINE state see unknown PRE_REFINE value | Medium | Medium | Pipeline model default stays REFINE. `skip_pre_refine` defaults true. Only interactive CLI opts in. |
| Claude session crash loses partial requirements work | Medium | Medium | Draft file written incrementally. Pipeline stays in PRE_REFINE/AWAITING_HUMAN for resume. |
| sdlc_hitl.py `_get_draft_path()` drifts from orchestrator version | Medium | Medium | Both functions have mirror comments. TASK-3-5 and TASK-2-2 are linked. |

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
    (PipelineConfig.skip_pre_refine defaults to true for backward
    compatibility; the interactive CLI sets false).
phases:
  - id: 1
    name: Pipeline model and phase registration (all locations)
    goal: Register PRE_REFINE in all enums, transition maps, gateway dicts, visualizer, permissions, and config
    tasks:
      - id: TASK-1-1
        description: Add PRE_REFINE = "pre_refine" to PipelinePhase enum in orchestrator/models.py (line 15)
        acceptance: PipelinePhase.PRE_REFINE is a valid enum member; existing phases unchanged
        files:
          - orchestrator/models.py
      - id: TASK-1-2
        description: Add PRE_REFINE = "pre_refine" to PipelinePhase enum in shared/egg_contracts/models.py (line 34)
        acceptance: Both orchestrator and contracts PipelinePhase enums have PRE_REFINE with identical string value
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Add PRE_REFINE -> [REFINE] to PHASE_TRANSITIONS (line 50) and LOCAL_PHASE_TRANSITIONS (line 58) in orchestrator/routes/phases.py
        acceptance: validate_phase_transition() allows PRE_REFINE -> REFINE and rejects PRE_REFINE -> PLAN/IMPLEMENT/PR
        files:
          - orchestrator/routes/phases.py
      - id: TASK-1-4
        description: Add pre_refine phase entry to .egg/phase-permissions.json with minimal permissions (write .egg-state/drafts/*requirements* only, no PR creation, no code changes)
        acceptance: Gateway enforces pre_refine file restrictions; only requirements draft files are writable
        files:
          - .egg/phase-permissions.json
      - id: TASK-1-5
        description: "Add PRE_REFINE entry to _DEFAULT_PHASE_CONFIGS dict (line 80) in phase_defaults.py: no checks (empty list), ISSUE_CHECKBOX mechanism, max_review_cycles=3"
        acceptance: get_default_phase_config(PipelinePhase.PRE_REFINE) returns valid PhaseConfig without KeyError
        files:
          - shared/egg_contracts/phase_defaults.py
      - id: TASK-1-6
        description: "Add skip_pre_refine boolean field to PipelineConfig class (line 200) in orchestrator/models.py with default=True for backward compatibility"
        acceptance: PipelineConfig.skip_pre_refine exists; defaults to True; serializes/deserializes correctly; existing callers creating PipelineConfig without this field get True (skip)
        files:
          - orchestrator/models.py
      - id: TASK-1-7
        description: "Add PRE_REFINE to gateway/phase_filter.py: (a) add PRE_REFINE to the gateway PipelinePhase enum (line 28), (b) add PRE_REFINE entry to PhasePermissions in _get_default_permissions() (line 411) with minimal ops (gh issue comment, git push, egg-contract show), (c) add PRE_REFINE entry to PhaseFileRestriction in _get_default_phase_file_restrictions() (line 489) with allowed_patterns=['.egg-state/drafts/*requirements*', '.egg-state/checkpoints/*', '.egg-state/agent-outputs/*']"
        acceptance: Gateway PipelinePhase has PRE_REFINE; PhasePermissions[PRE_REFINE] returns valid permissions; PhaseFileRestriction[PRE_REFINE] allows only requirements drafts, checkpoints, and agent outputs; no KeyError on PRE_REFINE lookups
        files:
          - gateway/phase_filter.py
      - id: TASK-1-8
        description: Add PRE_REFINE -> [PipelinePhase.REFINE] to VALID_TRANSITIONS dict (line 41) in gateway/phase_transition.py
        acceptance: Gateway phase transition validation allows PRE_REFINE -> REFINE and rejects other transitions from PRE_REFINE
        files:
          - gateway/phase_transition.py
      - id: TASK-1-9
        description: "Update orchestrator/dag_visualizer.py: insert PRE_REFINE at index 0 of PHASE_ORDER list (line 49, before REFINE), add PRE_REFINE: 'Pre-Refine' to PHASE_NAMES dict (line 57)"
        acceptance: DAG rendering, compact status, progress bar, and report generation all include Pre-Refine phase at the beginning
        files:
          - orchestrator/dag_visualizer.py
  - id: 2
    name: Orchestrator pre-refine dispatch and draft wiring
    goal: Handle PRE_REFINE in the orchestration loop with starting phase logic, skip, HITL pause, and requirements-to-refine handoff
    dependencies:
      - phase-1
    tasks:
      - id: TASK-2-1
        description: "Add PRE_REFINE handling in _run_pipeline(): (a) at startup before the phase loop, check PipelineConfig.skip_pre_refine — if False AND no existing requirements document at the expected draft path, set pipeline.current_phase to PRE_REFINE (this is the mechanism that routes pipelines to pre-refine without changing the Pipeline model default which stays REFINE); (b) in the phase handler switch, if phase is PRE_REFINE, set status to AWAITING_HUMAN and queue a decision with the original issue body/prompt as context; (c) on resolution with approval, read the requirements document and advance to REFINE"
        acceptance: Pipeline with skip_pre_refine=False pauses at PRE_REFINE with AWAITING_HUMAN status; pipeline with skip_pre_refine=True (default) skips directly to REFINE; pipeline with existing requirements document skips pre-refine regardless of flag
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: "Update _get_draft_path() in orchestrator/routes/pipelines.py (line 1037) to add explicit branch: if phase == 'pre_refine', return f'{prefix}-requirements.md'"
        acceptance: _get_draft_path("pre_refine", ...) returns correct path for both issue and local modes
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: "Update _build_phase_prompt() in orchestrator/routes/pipelines.py (line 1484) for the refine phase: check for a requirements document at the expected draft path. If found, include it as primary input context in the prompt. If absent, fall back to the raw issue body/prompt."
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
        description: Configure the Claude session in handle_pre_refine() with a requirements-gathering system prompt that guides conversation toward problem statement, functional requirements, constraints, and acceptance criteria. Claude should write to the requirements draft file incrementally. No codebase access tools should be available.
        acceptance: Claude session starts with requirements-focused system prompt; draft file is created/updated during session; no codebase access tools available
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-3
        description: "Update _detect_phase() (line 343) in sdlc_hitl.py to recognize pre_refine phase from decision question text (match 'pre-refine', 'pre_refine', 'requirements')"
        acceptance: _detect_phase() returns "pre_refine" for pre-refine decision questions
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-4
        description: Update watch_pipeline() in sdlc_cli.py to route pre_refine HITL checkpoints to handle_pre_refine() instead of the generic handle_hitl_checkpoint()
        acceptance: When pipeline is in pre_refine phase and awaiting_human, CLI calls handle_pre_refine(); other phases continue using handle_hitl_checkpoint()
        files:
          - sandbox/egg_lib/sdlc_cli.py
      - id: TASK-3-5
        description: "Update _get_draft_path() in sandbox/egg_lib/sdlc_hitl.py (line 27) to add explicit branch for pre_refine phase: if phase == 'pre_refine', return f'.egg-state/drafts/{prefix}-requirements.md'. This must be an explicit branch BEFORE the generic else fallback (which would incorrectly produce {prefix}-pre_refine.md). Mirrors TASK-2-2 change in orchestrator."
        acceptance: sdlc_hitl._get_draft_path('pre_refine', ...) returns {prefix}-requirements.md (not {prefix}-pre_refine.md); function stays in sync with orchestrator version
        files:
          - sandbox/egg_lib/sdlc_hitl.py
  - id: 4
    name: Issue mode integration and pipeline creation
    goal: Update GitHub issue description with approved requirements in issue mode; add skip_pre_refine CLI flag
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
        description: "Handle pipeline restart edge case: detect existing requirements document in _run_pipeline() startup logic and skip re-gathering (already handled by TASK-2-1's 'AND no existing requirements document' condition — this task verifies and tests that path)"
        acceptance: Re-running a pipeline that already has a requirements document skips pre-refine regardless of skip_pre_refine flag
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-4-3
        description: "Add --skip-pre-refine flag to egg-sdlc CLI (sdlc_cli.py). Interactive mode sets skip_pre_refine=false by default. Pass parameter through to create_pipeline() via the existing config dict parameter (orch_client.py line 160 already accepts config: dict). No changes needed to orch_client.py itself."
        acceptance: "egg-sdlc --skip-pre-refine creates pipeline with config={'skip_pre_refine': true}; interactive mode without flag passes config={'skip_pre_refine': false}; parameter reaches orchestrator via existing config dict"
        files:
          - sandbox/egg_lib/sdlc_cli.py
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
        description: Write unit tests for PRE_REFINE phase transitions — both orchestrator (PHASE_TRANSITIONS, LOCAL_PHASE_TRANSITIONS) and gateway (VALID_TRANSITIONS). PRE_REFINE -> REFINE allowed; PRE_REFINE -> PLAN/IMPLEMENT/PR rejected; REFINE as starting phase still valid.
        acceptance: All transition validation tests pass for both orchestrator and gateway
        files:
          - orchestrator/tests/test_phases.py
          - gateway/tests/
      - id: TASK-5-2
        description: Write unit tests for _get_draft_path() returning requirements.md for pre_refine — both orchestrator version and sdlc_hitl.py mirror version
        acceptance: Draft path tests cover pre_refine for issue mode ({issue_number}-requirements.md) and local mode ({pipeline_id}-requirements.md) in both implementations
        files:
          - orchestrator/tests/test_pipelines.py
          - sandbox/tests/test_sdlc_hitl.py
      - id: TASK-5-3
        description: Write unit tests for skip_pre_refine config and starting phase logic — pipeline starts at REFINE when skip_pre_refine=True or default, PRE_REFINE when skip_pre_refine=False and no existing requirements doc
        acceptance: Skip logic tests verify correct starting phase based on config and presence of requirements doc
        files:
          - orchestrator/tests/test_pipelines.py
      - id: TASK-5-4
        description: Write unit tests for _build_phase_prompt() using requirements document as refine input when available, falling back to raw prompt when absent
        acceptance: Prompt building tests verify requirements doc inclusion and fallback behavior
        files:
          - orchestrator/tests/test_pipelines.py
      - id: TASK-5-5
        description: "Write unit tests for _detect_phase() recognizing pre_refine keywords"
        acceptance: "Phase detection tests cover 'pre-refine', 'pre_refine', 'requirements' patterns"
        files:
          - sandbox/tests/test_sdlc_hitl.py
      - id: TASK-5-6
        description: "Write unit tests for PRE_REFINE phase defaults (get_default_phase_config returns valid config), PipelineConfig.skip_pre_refine serialization round-trip, and gateway PhasePermissions/PhaseFileRestriction containing PRE_REFINE entries"
        acceptance: Phase defaults return valid config; PipelineConfig round-trips with skip_pre_refine field; gateway dicts don't KeyError on PRE_REFINE
        files:
          - shared/egg_contracts/tests/test_phase_defaults.py
          - orchestrator/tests/test_models.py
          - gateway/tests/
      - id: TASK-5-7
        description: Write unit tests for dag_visualizer PHASE_ORDER containing PRE_REFINE at index 0 and PHASE_NAMES containing 'Pre-Refine'
        acceptance: Visualization tests confirm PRE_REFINE appears in phase order and has correct display name
        files:
          - orchestrator/tests/
      - id: TASK-5-8
        description: Verify existing tests pass unchanged (phase transition tests, pipeline tests, HITL tests)
        acceptance: All pre-existing tests pass without modification; no regressions
        files:
          - orchestrator/tests/
          - shared/egg_contracts/tests/
          - gateway/tests/
          - sandbox/tests/
```
