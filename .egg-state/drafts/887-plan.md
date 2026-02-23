# Implementation Plan: Improve checkpoint discoverability for agents

**Issue**: #887
**Approach**: Prompt-only changes (Architect's Option A)
**PR scope**: Single PR with all changes

## Summary

Agents have access to `egg-checkpoint` CLI and the `checkpoint.md` rule is loaded into every session, but nothing in orchestrator prompts, agent mode commands, or mission rules tells agents _when_ or _why_ to use checkpoints. This PR adds role-specific checkpoint discovery hints at three levels:

1. **Orchestrator prompts** (highest leverage — auto-injected into every agent session)
2. **Agent mode commands** (supplementary reference when agents activate via slash command)
3. **Mission rule + checkpoint rule** (baseline awareness for all agents)

All changes are additive text — no Python logic changes, no schema migrations, no new tooling.

## Approach

The architect recommended Option A (prompt-only changes) and both reviewers approved. The rationale:

- The core problem is discoverability, not tooling. `egg-checkpoint` works. The rule is loaded. Agents just need to know WHEN and WHY.
- Orchestrator prompt injection is the highest-leverage change because it reaches every downstream agent automatically.
- Items 5-8 from the issue (slash command, handoff enrichment, revision summaries, `egg-agent-context` wrapper) are deferred — they add maintenance burden without proportional value.

## Implementation Phases

### Phase 1: Orchestrator prompt injection

Add checkpoint discovery hints to the three prompt-building functions in `orchestrator/routes/pipelines.py`. This is the highest-leverage change — every downstream agent session receives these hints automatically.

**Changes:**

1. **`_build_role_context()` (~line 1291)**: Add a checkpoint pointer to the "For More Context" section after the existing "Coder output" line:
   ```
   - Prior agent sessions: `egg-checkpoint context --pipeline $EGG_PIPELINE_ID` (see checkpoint rule for details)
   ```

2. **`_build_agent_prompt()` tester section (~line 2481)**: Add after the gap-finding focus list:
   ```
   Before writing tests, review the coder's session for context on what was changed and why:
   `egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement`
   ```

3. **`_build_agent_prompt()` documenter section (~line 2498)**: Add after the focus list:
   ```
   Find all changed files across agents:
   `egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`
   ```

4. **`_build_agent_prompt()` integrator section (~line 2512)**: Add after the integration report instruction:
   ```
   Review pipeline overview and costs before integrating:
   `egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files` and `egg-checkpoint cost --pipeline $EGG_PIPELINE_ID`
   ```

5. **`_build_phase_scoped_prompt()` revision checklist (~line 2794)**: Add to the revision checklist:
   ```
   - [ ] Check prior failed sessions: `egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed`
   ```

**Files**: `orchestrator/routes/pipelines.py`

### Phase 2: Agent mode command updates

Add role-specific checkpoint workflow sections to each agent mode command markdown file.

**Changes:**

1. **`tester-mode.md`**: Add a "## Review Prior Work" section (after the handoff/output section, before "## Quality Checklist") with:
   - Command: `egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder`
   - Use case: understand what the coder changed and why before writing tests
   - Command to inspect specific checkpoint: `egg-checkpoint show ckpt-<id>`

2. **`integrator-mode.md`**: Add a "## Pipeline Overview" section (after the agent outputs section, before quality/failure sections) with:
   - Command: `egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`
   - Command: `egg-checkpoint cost --pipeline $EGG_PIPELINE_ID`
   - Use case: understand full pipeline scope and token spend before integration

3. **`documenter-mode.md`**: Add a "## Find Changed Files" section (after the handoff/output section, before "## Quality Checklist") with:
   - Command: `egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`
   - Use case: discover all files touched across agents to ensure documentation covers everything

4. **`coder-mode.md`**: Add a conditional "## Revision Cycle Context" section (after the handoff/output section, before "## Quality Checklist") with:
   - Condition: "If this is a revision cycle (re-running after feedback)"
   - Command: `egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed`
   - Use case: understand what went wrong in prior attempts

**Files**: `sandbox/.claude/commands/tester-mode.md`, `sandbox/.claude/commands/integrator-mode.md`, `sandbox/.claude/commands/documenter-mode.md`, `sandbox/.claude/commands/coder-mode.md`

### Phase 3: Mission rule and checkpoint rule updates

Add checkpoint as a recognized context source in mission.md and optionally enhance checkpoint.md with "when to use" guidance.

**Changes:**

1. **`mission.md` context sources table** (~line 22): Add row:
   ```
   | Checkpoints | `egg-checkpoint` CLI | Prior agent sessions, files touched, token usage |
   ```

2. **`mission.md` workflow step** (Gather context section): Add:
   ```
   In multi-agent pipelines, review prior agent sessions via `egg-checkpoint context --pipeline $EGG_PIPELINE_ID`.
   ```

3. **`checkpoint.md`** (optional enhancement): Add a brief "## When to Use" preamble at the top with role-specific guidance:
   - **Tester**: Review coder's session before writing tests
   - **Documenter**: Find all changed files across agents
   - **Integrator**: Get pipeline overview and cost summary
   - **Coder (revision)**: Check prior failed sessions

**Files**: `sandbox/.claude/rules/mission.md`, `sandbox/.claude/rules/checkpoint.md`

## Test Strategy

Since all changes are text additions to prompt templates and markdown files, the test approach is:

1. **Existing test suite**: Run `make test` (or `pytest`) to verify no regressions — the prompt-building functions have existing tests that should still pass since we're only appending lines.

2. **Prompt output verification**: Write targeted tests (or verify manually) that the prompt-building functions include checkpoint hints:
   - Call `_build_role_context()` with a mock pipeline and verify output contains "egg-checkpoint context"
   - Call `_build_agent_prompt()` for each role and verify role-specific checkpoint command appears
   - Call `_build_phase_scoped_prompt()` with `review_cycle > 0` and verify failed-session hint appears

3. **Markdown lint**: Verify mode command and rule files pass any existing markdown linting.

4. **Manual smoke test**: In a test pipeline, verify that tester/documenter/integrator agents receive checkpoint hints in their prompts by checking the rendered prompt output.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Prompt token increase affects agent performance | Low | Low | Hints are 1-2 lines per role (~10-15 lines total). checkpoint.md (62 lines) already loaded. |
| Agents over-rely on checkpoints, wasting tokens | Low | Low | Hints phrased as optional context, not mandatory steps. |
| Checkpoint queries return empty (no prior checkpoints) | Medium | Low | Agents handle empty results gracefully. No special error handling needed. |
| Orchestrator prompt changes conflict with other PRs | Low | Low | Changes are additive line appends. Git merge handles cleanly. |

## Dependency Ordering

- Phase 1 (orchestrator prompts) and Phase 2 (mode commands) are independent and can be implemented in any order or in parallel.
- Phase 3 (mission/checkpoint rules) is independent of Phase 1 and 2.
- Recommended order: Phase 1 first (highest leverage, provides immediate value), then Phase 2, then Phase 3.

## Deferred Items

These items from issue #887 are explicitly out of scope for this PR:

- **Item 5**: `checkpoint-discovery.md` slash command — pipeline agents don't invoke slash commands
- **Item 6**: Embed `checkpoint_ids` in handoff data — requires understanding checkpoint write timing (race condition risk)
- **Item 7**: Checkpoint summary in revision-cycle prompts — premature without structured linking
- **Item 8**: `egg-agent-context` helper — duplicates `egg-checkpoint context`

```yaml
# yaml-tasks
pr:
  title: "Add checkpoint discovery hints to agent prompts"
  description: |
    Agents have egg-checkpoint CLI and the checkpoint.md rule loaded, but nothing
    tells them when or why to use checkpoints. This PR adds role-specific checkpoint
    discovery hints to orchestrator prompts (auto-injected), agent mode commands,
    and mission/checkpoint rules. All changes are additive text — no logic changes,
    no schema migrations, no new tooling. Covers issue #887 items 1-4; items 5-8
    deferred to follow-up issues.
phases:
  - id: 1
    name: Orchestrator prompt injection
    goal: Add checkpoint hints to orchestrator prompt-building functions so every downstream agent receives them automatically
    tasks:
      - id: TASK-1-1
        description: Add checkpoint pointer to _build_role_context() "For More Context" section
        acceptance: _build_role_context() output includes "egg-checkpoint context --pipeline" line for all execution roles
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-2
        description: Add checkpoint hint to _build_agent_prompt() tester section
        acceptance: Tester prompt includes "egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement"
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-3
        description: Add checkpoint hint to _build_agent_prompt() documenter section
        acceptance: Documenter prompt includes "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files"
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-4
        description: Add checkpoint hint to _build_agent_prompt() integrator section
        acceptance: Integrator prompt includes "egg-checkpoint context" and "egg-checkpoint cost" commands
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: Add failed-session checkpoint hint to _build_phase_scoped_prompt() revision checklist
        acceptance: Revision checklist includes "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed" when review_cycle > 0
        files:
          - orchestrator/routes/pipelines.py
  - id: 2
    name: Agent mode command updates
    goal: Add role-specific checkpoint workflow sections to agent mode command markdown files
    tasks:
      - id: TASK-2-1
        description: Add "Review Prior Work" section to tester-mode.md with checkpoint list command
        acceptance: tester-mode.md contains "## Review Prior Work" section with egg-checkpoint list command
        files:
          - sandbox/.claude/commands/tester-mode.md
      - id: TASK-2-2
        description: Add "Pipeline Overview" section to integrator-mode.md with checkpoint context and cost commands
        acceptance: integrator-mode.md contains "## Pipeline Overview" section with egg-checkpoint context and cost commands
        files:
          - sandbox/.claude/commands/integrator-mode.md
      - id: TASK-2-3
        description: Add "Find Changed Files" section to documenter-mode.md with checkpoint context command
        acceptance: documenter-mode.md contains "## Find Changed Files" section with egg-checkpoint context --files command
        files:
          - sandbox/.claude/commands/documenter-mode.md
      - id: TASK-2-4
        description: Add "Revision Cycle Context" section to coder-mode.md with failed-session checkpoint command
        acceptance: coder-mode.md contains "## Revision Cycle Context" section with egg-checkpoint list --status failed command
        files:
          - sandbox/.claude/commands/coder-mode.md
  - id: 3
    name: Mission rule and checkpoint rule updates
    goal: Add checkpoint as a recognized context source and enhance checkpoint rule with "when to use" guidance
    tasks:
      - id: TASK-3-1
        description: Add Checkpoints row to mission.md context sources table
        acceptance: mission.md context sources table includes Checkpoints row with egg-checkpoint CLI location
        files:
          - sandbox/.claude/rules/mission.md
      - id: TASK-3-2
        description: Add checkpoint context gathering hint to mission.md workflow section
        acceptance: mission.md "Gather context" workflow step mentions egg-checkpoint context for multi-agent pipelines
        files:
          - sandbox/.claude/rules/mission.md
      - id: TASK-3-3
        description: Add "When to Use" section to checkpoint.md with role-specific guidance
        acceptance: checkpoint.md has a "When to Use" section listing tester, documenter, integrator, and coder (revision) use cases
        files:
          - sandbox/.claude/rules/checkpoint.md
  - id: 4
    name: Verification
    goal: Ensure all changes pass existing tests and the checkpoint hints appear in rendered prompts
    tasks:
      - id: TASK-4-1
        description: Run existing test suite to verify no regressions in prompt-building functions
        acceptance: All existing tests pass (pytest / make test)
        files: []
      - id: TASK-4-2
        description: Verify checkpoint hints appear in prompt outputs for each role
        acceptance: Manual or scripted check confirms tester, documenter, integrator, and revision prompts contain checkpoint commands
        files: []
```
