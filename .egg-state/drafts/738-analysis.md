# Analysis: Add an interactive "pre-refine" step

> Issue: #738 | Phase: refine

## Problem Statement

The SDLC pipeline currently begins with the **refine** phase, where an autonomous agent analyzes an issue and produces a structured analysis document. However, the quality of this analysis depends heavily on the clarity and completeness of the original issue description. When issues are vague or underspecified, the refine agent may produce analysis that misses key requirements, leading to wasted review cycles or, worse, correct-but-wrong implementations.

The request is for an interactive **pre-refine step** where a human and Claude collaborate conversationally to produce a clear requirements document *before* the autonomous pipeline begins. This step is fundamentally different from existing pipeline phases: it is human-driven, conversational (free-form back-and-forth), and involves no codebase interaction.

**Desired outcome**: A requirements document that captures the problem statement, functional requirements, constraints, and acceptance criteria — produced through dialogue rather than autonomous analysis. This document then replaces the raw issue body as input to the refine phase.

## Current Behavior

### Pipeline Phase Flow

The current pipeline is: `REFINE → PLAN → IMPLEMENT → PR`

Defined in two places:
- `shared/egg_contracts/models.py:34` — Contract-level `PipelinePhase` enum
- `orchestrator/models.py:15` — Orchestrator-level `PipelinePhase` enum

Phase transitions are validated in `orchestrator/routes/phases.py:50-63`:
```python
PHASE_TRANSITIONS = {
    PipelinePhase.REFINE: [PipelinePhase.PLAN, PipelinePhase.IMPLEMENT],
    PipelinePhase.PLAN: [PipelinePhase.IMPLEMENT],
    PipelinePhase.IMPLEMENT: [PipelinePhase.PR],
    PipelinePhase.PR: [],
}
```

### How the Refine Phase Gets Input

In **issue mode**, the pipeline is created from a GitHub issue. The issue body becomes `pipeline.prompt`, which is injected into the refine agent's prompt via `_build_phase_prompt()` in `orchestrator/routes/pipelines.py`.

In **local mode**, the `egg-sdlc` CLI collects a task description and optional scope interactively (`sandbox/egg_lib/sdlc_cli.py:326-362`), builds a prompt string, and passes it to `client.create_pipeline(mode="local", prompt=prompt)`.

### Agent Execution Model

All existing phases spawn containers running Claude in `--print` mode (non-interactive, autonomous). The agent receives a structured prompt, produces output, and exits. There is no back-and-forth conversation during a phase — only HITL checkboxes for structured decisions between cycles.

The `ContainerSpawner` (`orchestrator/container_spawner.py:192`) sets environment variables (`EGG_PHASE`, `EGG_AGENT_ROLE`, `EGG_PIPELINE_ID`) and runs the agent autonomously.

### Current HITL Mechanisms

The pipeline supports two HITL patterns:
1. **Decisions** — Multiple-choice checkboxes on GitHub issue comments (30-second debounce)
2. **Feedback** — Open-ended text fields in editable comments

Both are asynchronous (post-and-wait), not conversational. They're designed for discrete decision points, not free-form dialogue.

## Constraints

- **Interactive vs autonomous**: The pre-refine step requires real-time conversational interaction (turn-by-turn dialogue). All existing phases are autonomous (agent runs to completion, then human reviews). This is a fundamentally different execution model.
- **Both modes required**: Must work in local mode (CLI) and issue mode (GitHub). The interaction model differs significantly between these — local mode can use TTY, issue mode cannot.
- **No codebase interaction**: The step is purely requirements-focused. The agent should not read source code or explore the repository.
- **Approval gate**: Human must explicitly approve the requirements document before the pipeline advances. This aligns with existing HITL approval patterns.
- **Pipeline state tracking**: The pre-refine step should be observable in pipeline status (SSE events, `egg-pipeline-watch`, `egg-status`).
- **Output format**: A single requirements document at `.egg-state/drafts/{identifier}-requirements.md` that becomes the input to refine.
- **Issue update**: In issue mode, the approved document replaces the GitHub issue description.

## Options Considered

### Option A: New PRE_REFINE pipeline phase with interactive container

**Approach**: Add `PRE_REFINE` as a formal first phase in the pipeline. The orchestrator spawns a container running Claude in interactive mode (not `--print`). The `egg-sdlc` CLI proxies the TTY to the container for local-mode conversations. For issue mode, the conversation uses GitHub issue comment threads (human posts comments, agent responds via `gh issue comment`).

**Changes required**:
- Add `PRE_REFINE` to both `PipelinePhase` enums (`shared/egg_contracts/models.py`, `orchestrator/models.py`)
- Update `PHASE_TRANSITIONS` and `LOCAL_PHASE_TRANSITIONS` in `orchestrator/routes/phases.py`
- Add phase defaults in `shared/egg_contracts/phase_defaults.py`
- New dispatch logic in `orchestrator/routes/pipelines.py` — interactive container instead of `--print`
- TTY passthrough from CLI through orchestrator to container
- Issue-mode conversation loop: agent posts question → waits for human comment → responds
- New phase permissions in `.egg/phase-permissions.json`
- Pipeline visualization updates for the new phase
- Contract model updates for pre-refine review fields

**Pros**:
- Architecturally consistent — tracked as a formal pipeline phase with full lifecycle
- Full observability via SSE events, pipeline status, visualization
- Clean phase transition: `PRE_REFINE → REFINE → PLAN → IMPLEMENT → PR`
- Can be independently skipped/disabled via pipeline config
- State is tracked in the pipeline model (timing, cycles, containers)

**Cons**:
- High complexity — requires interactive container execution, which is a new capability for the orchestrator
- TTY passthrough through orchestrator to container is non-trivial (current containers are fire-and-forget `--print` mode)
- Issue-mode conversation via GitHub comments is inherently slow and clunky compared to real-time chat
- Every downstream component that enumerates phases needs updating (visualization, SSE events, `egg-orch phase`, `egg-pipeline-watch`, etc.)
- The interactive model breaks the "spawn, run, collect output" pattern that all other phases use
- Risk of scope creep: the infrastructure for interactive containers may be over-engineering for a single use case

### Option B: CLI-level conversation before pipeline creation

**Approach**: The conversation happens in the `egg-sdlc` CLI *before* creating the pipeline. In local mode, this extends the existing interactive prompt collection (`sdlc_cli.py:326-362`) into a multi-turn conversation. In issue mode, the CLI fetches the issue body, launches a local Claude session to discuss requirements, and the human approves the output before the pipeline starts. The approved requirements document is stored locally and its content is passed as the pipeline prompt.

**Changes required**:
- Extend `egg-sdlc` CLI local mode with a conversation loop using Claude API
- Add issue-mode conversation flow: fetch issue → discuss → approve → create pipeline
- Store requirements document at `.egg-state/drafts/{identifier}-requirements.md` after pipeline creation
- In issue mode, update the GitHub issue description with the approved requirements
- Pipeline receives the requirements document content as its prompt (no phase changes)

**Pros**:
- Simplest implementation — no changes to phase machinery, transitions, or orchestrator dispatch
- Natural UX — conversation happens where the user already is (the terminal)
- Leverages existing interactive capabilities of `egg-sdlc` CLI
- No new container execution model needed
- Local mode is a natural fit (already interactive)
- Can be made optional — user can skip and go straight to pipeline creation

**Cons**:
- Not tracked as a pipeline phase — invisible to SSE events, `egg-status`, visualization
- No pipeline state tracking (timing, conversation history not persisted in pipeline model)
- Issue-mode conversation happens locally even though the pipeline runs remotely — slight UX mismatch
- Requirements document is produced before the pipeline exists, so `.egg-state/` path management needs special handling
- The conversation is not recoverable if the CLI crashes — no checkpoint mechanism
- Harder to enforce the "human must approve" gate without pipeline-level tracking

### Option C: Orchestrator-managed pre-step (not a formal phase)

**Approach**: The orchestrator has a "pre-refine conversation" step that runs before the pipeline loop starts. It spawns an interactive container, manages the conversation lifecycle, and stores the output. It's tracked in the pipeline model but not as a formal phase — more like a pipeline initialization step with its own state.

**Changes required**:
- Add `pre_refine` field to `Pipeline` model (conversation state, requirements doc path)
- New orchestrator endpoint for starting/managing pre-refine conversation
- Interactive container spawning with CLI TTY proxy
- Conversation state management in pipeline model
- `egg-sdlc` CLI integration for proxying the interactive session
- Issue-mode: conversation via comment thread on the issue

**Pros**:
- Pipeline-level tracking without full phase machinery changes
- Doesn't pollute the phase enum or transition tables
- Conversation state persisted in pipeline model
- Can be skipped — pipeline starts at refine if pre-refine is not needed

**Cons**:
- "Not quite a phase" creates an architectural gray area — special-case code in the orchestrator
- Still requires interactive container infrastructure (same as Option A)
- Visualization and SSE events need custom handling (not a standard phase)
- Two systems for lifecycle tracking: phases for formal phases, custom for pre-refine

## Recommended Approach

**Option A (new PRE_REFINE phase)** is recommended despite its higher complexity, for these reasons:

1. **Architectural consistency**: The SDLC pipeline is designed around phases with clear transitions, state tracking, and lifecycle management. Adding pre-refine as a formal phase keeps this clean model intact. Option B's "invisible step" and Option C's "special-case step" both introduce architectural exceptions.

2. **Observability**: A formal phase gets SSE events, visualization, timing, and status tracking for free. This is important for debugging and monitoring. The issue explicitly asks for a step that "works in both local and issue mode" — pipeline-level tracking makes this observable in both modes.

3. **Recovery**: If the conversation is interrupted, a formal phase can be restarted from its persisted state. CLI-level conversations (Option B) would be lost entirely.

4. **Skip mechanism**: `PipelineConfig` already has `allow_short_circuit` for skipping the plan phase. The same pattern works for making pre-refine optional (e.g., `enable_pre_refine: bool`).

5. **Future extensibility**: Interactive container execution is a capability that could benefit other use cases (e.g., interactive debugging sessions, pair-programming phases). Building it for pre-refine establishes the pattern.

The primary risk is the interactive container model. The implementation should consider using the existing `egg-sdlc` HITL checkpoint flow as a building block — the CLI already has TTY passthrough and interactive editing capabilities. For issue mode, a GitHub comment-based conversation loop (agent posts question, polls for human reply, continues) is clunkier but functional and consistent with the existing HITL decision pattern.

## Open Questions

1. **Issue-mode conversation UX**: In issue mode, how should the conversation happen? Options include: (a) comment-based back-and-forth on the GitHub issue (slow but native), (b) local CLI conversation even for issue-mode pipelines (faster but requires local CLI access), or (c) a separate chat interface. This significantly affects the implementation.

2. **Skippability**: Should the pre-refine step be skippable (opt-in) or mandatory? The issue says the pipeline "currently jumps straight into the refine phase" as a problem, but some well-specified issues may not need conversation. A config flag like `enable_pre_refine` with a default of `true` would allow flexibility.

3. **Conversation model**: Should the conversation use Claude Code's interactive mode (full agent with tool access) or a simpler API-based chat (Claude API direct, no tools)? The issue says "no codebase interaction" which suggests tools should be disabled, but Claude Code's interactive mode provides the best conversational UX.

4. **Requirements document format**: Should there be a structured template for the requirements document (like the analysis template), or should it be free-form? A template ensures consistent downstream consumption by the refine agent, but may feel rigid for a conversational flow.

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: high
parallel_phases: false
```
