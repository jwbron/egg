# Plan: Replace Claude-as-collaborator with rich CLI for SDLC pipeline

> Issue: #635 | Phase: plan

## Summary

The current `egg --sdlc <issue>` workflow uses Claude (the LLM) as the interactive orchestrator for SDLC pipelines. This is unreliable: Claude gets confused following the multi-step protocol, the token-gated HITL approval mechanism is fragile, and the user experience is poor since pipeline artifacts are mediated through Claude rather than being directly editable.

This plan implements `egg-sdlc`, a rich Python CLI tool using the `rich` library (already available in the sandbox). It runs inside the egg container, displays the DAG visualization (reusing `egg-pipeline-watch` SSE patterns), and presents documents directly at HITL checkpoints for human iteration — via `$EDITOR`, launching Claude for AI assistance, or direct approval. This follows **Option A** from the analysis.

The key insight is that the orchestrator already handles all pipeline execution logic (`_run_pipeline`, container spawning, review cycles, HITL decision queuing). The new CLI only replaces Claude as the *user-facing controller* that creates pipelines, watches progress, and resolves HITL decisions. The orchestrator API, GitHub Actions workflow, and container spawning remain unchanged.

## Implementation Phases

### Phase 1: Core CLI skeleton and orchestrator client

**Goal**: Create the `egg-sdlc` entry point and a reusable orchestrator API client.

**Tasks**:

- [TASK-1-1] Create `sandbox/egg_lib/sdlc_cli.py` — the main CLI module. Implement `main()` with argument parsing: `egg-sdlc [<issue_number>] [--repo <owner/repo>]`. Validate the orchestrator is reachable by hitting `/api/v1/health`. Determine mode (issue vs local) based on arguments.
  - **Acceptance**: `egg-sdlc --help` displays usage. `egg-sdlc` with no orchestrator running prints a clear error.

- [TASK-1-2] Create `sandbox/egg_lib/orch_client.py` — a thin HTTP client wrapping the orchestrator REST API. Methods needed:
  - `health_check() -> bool` — `GET /api/v1/health`
  - `create_pipeline(issue_number, repo, branch, mode, prompt, config) -> dict` — `POST /api/v1/pipelines`
  - `start_pipeline(pipeline_id) -> dict` — `POST /api/v1/pipelines/<id>/start`
  - `get_pipeline(pipeline_id) -> dict` — `GET /api/v1/pipelines/<id>`
  - `get_pipeline_status(pipeline_id) -> dict` — `GET /api/v1/pipelines/<id>/status`
  - `list_decisions(pipeline_id, pending_only) -> list` — `GET /api/v1/pipelines/<id>/decisions`
  - `resolve_decision(pipeline_id, decision_id, resolution) -> dict` — `POST /api/v1/pipelines/<id>/decisions/<did>/resolve`
  - `cancel_pipeline(pipeline_id) -> dict` — `PATCH /api/v1/pipelines/<id>` with `{"status": "cancelled"}`
  - Use `http.client.HTTPConnection` (same as `egg-pipeline-watch`) to avoid external dependencies. Auto-detect orchestrator URL using the same `get_orchestrator_url()` pattern from `egg-pipeline-watch`.
  - **Acceptance**: Each method makes the correct HTTP call and returns parsed JSON. Errors raise descriptive exceptions.

- [TASK-1-3] Create `sandbox/bin/egg-sdlc` — executable entry point script (like `sandbox/bin/egg-pipeline-watch`). Calls `sandbox.egg_lib.sdlc_cli:main()`.
  - **Acceptance**: `egg-sdlc` is executable and delegates to the main module.

- [TASK-1-4] Create `bin/egg-sdlc` — host-side symlink to `sandbox/bin/egg-sdlc` (matching the existing `bin/egg-pipeline-watch -> ../sandbox/bin/egg-pipeline-watch` pattern).
  - **Acceptance**: `bin/egg-sdlc` resolves correctly.

**Dependencies**: None

**Exit criteria**: `egg-sdlc <issue>` can create and start a pipeline via the orchestrator API, then exit.

### Phase 2: Pipeline watch loop with DAG visualization

**Goal**: Implement the SSE-based pipeline watch loop that displays the DAG in real-time, reusing patterns from `egg-pipeline-watch`.

**Tasks**:

- [TASK-2-1] Add a `watch_pipeline()` function to `sandbox/egg_lib/sdlc_cli.py` (or a separate `sdlc_watch.py` if the module grows too large). This function:
  - Connects to the orchestrator SSE endpoint (`/api/v1/pipelines/<id>/stream`)
  - Parses SSE events using the same `parse_sse_stream()` logic from `sandbox/bin/egg-pipeline-watch:208-238`
  - Renders the DAG visualization using the same `display_visualization()` approach (clear screen, render header, DAG, event info)
  - Detects `awaiting_human` status and breaks out of the watch loop to enter HITL mode (Phase 3)
  - Detects terminal states (`complete`, `failed`, `cancelled`) and exits cleanly
  - Handles `KeyboardInterrupt` for clean Ctrl+C exit
  - **Acceptance**: Running `egg-sdlc <issue>` shows a live-updating DAG display identical to `egg-pipeline-watch`. When pipeline reaches `awaiting_human`, the watch loop pauses.

- [TASK-2-2] Implement the main orchestration loop in `sdlc_cli.py`:
  ```
  1. Create pipeline
  2. Start pipeline
  3. Enter watch loop:
     a. Stream SSE, render DAG
     b. On awaiting_human → enter HITL handler (Phase 3)
     c. On HITL resolution → resume watch loop
     d. On terminal state → show summary, exit
  ```
  - **Acceptance**: Pipeline progresses through phases. HITL checkpoints pause the DAG and resume after resolution.

**Dependencies**: Phase 1 (CLI skeleton and API client)

**Exit criteria**: `egg-sdlc <issue>` shows live DAG updates and correctly detects HITL checkpoints.

### Phase 3: HITL checkpoint handler

**Goal**: When the pipeline reaches an `awaiting_human` state, present the draft document and offer interactive options.

**Tasks**:

- [TASK-3-1] Create `sandbox/egg_lib/sdlc_hitl.py` — the HITL interaction module. Implement `handle_hitl_checkpoint(orch_client, pipeline_id, decision)` that:
  1. Reads the draft document from `.egg-state/drafts/{id}-{phase_label}.md` (analysis for refine, plan for plan). The path follows the pattern in `_read_phase_draft()` at `orchestrator/routes/pipelines.py:884`.
  2. Displays a preview of the document using `rich.syntax.Syntax` or `rich.markdown.Markdown` for syntax-highlighted rendering.
  3. Presents an interactive menu using `rich.prompt.Prompt`:
     ```
     [1] Edit with $EDITOR (default: vim)
     [2] Start Claude for AI-assisted editing
     [3] Approve and advance to next phase
     [4] Provide feedback (text input)
     [5] Cancel pipeline
     ```
  4. Handles each menu option (see TASK-3-2 through TASK-3-5)
  5. After the user acts, resolves the HITL decision via the orchestrator API (`POST /api/v1/pipelines/<id>/decisions/<did>/resolve`)
  - **Acceptance**: When pipeline reaches HITL, user sees the document preview and menu. Selecting an option resolves the decision and allows the pipeline to proceed.

- [TASK-3-2] Implement the editor launch option (menu item 1):
  - Stop any `rich` Live display before spawning the editor
  - Use `$EDITOR` env var (default to `vim`) — launch with `subprocess.run([editor, draft_path])` with TTY inherited
  - After editor exits, the modified document is already saved to disk (the orchestrator reads from the file path)
  - Resolve the decision with "Approved (edited)" to advance
  - **Acceptance**: User can edit the draft in vim (or their preferred editor), save, and the pipeline advances.

- [TASK-3-3] Implement the Claude AI assistance option (menu item 2):
  - Launch `claude` in the same container as an interactive subprocess: `subprocess.run(["claude"], cwd=repo_path)`. The user gets a full Claude Code session to iterate on the draft.
  - After Claude exits (user types `/exit`), return to the HITL menu so the user can approve or edit further.
  - **Acceptance**: User can start Claude, make AI-assisted edits, exit Claude, and return to the HITL menu.

- [TASK-3-4] Implement the feedback option (menu item 4):
  - Use `rich.prompt.Prompt.ask()` to collect multiline text feedback
  - Resolve the decision with the feedback text. The orchestrator will pass this feedback to the next review cycle.
  - **Acceptance**: User can type feedback that gets passed to the next iteration.

- [TASK-3-5] Implement the cancel option (menu item 5):
  - Call `cancel_pipeline()` on the orchestrator client
  - Exit the CLI cleanly
  - **Acceptance**: Selecting cancel stops the pipeline and exits egg-sdlc.

- [TASK-3-6] Handle the token-gated approval bypass. Currently, `resolve_decision()` returns 403 for token-gated pipelines (see `orchestrator/routes/decisions.py:290-299`). Since `egg-sdlc` replaces the token-gated flow entirely (the human is directly interacting with the CLI, not through Claude), pipelines created by `egg-sdlc` should NOT be token-gated. Ensure `egg-sdlc` does NOT trigger `setup_sdlc_tokens()` — this happens automatically because `egg-sdlc` creates pipelines directly via the API without the `EGG_SDLC_ISSUE` env var being set in the entrypoint.
  - Verify that the decision resolution path works correctly for non-token-gated pipelines.
  - **Acceptance**: `egg-sdlc` pipelines are not token-gated. HITL decisions resolve via the standard API.

**Dependencies**: Phase 2 (watch loop detects HITL checkpoints)

**Exit criteria**: Full HITL cycle works — user sees document, edits/approves, pipeline advances.

### Phase 4: Local mode support

**Goal**: Support the `egg-sdlc` command with no arguments for prompt-driven local pipelines.

**Tasks**:

- [TASK-4-1] Implement local mode flow in `sdlc_cli.py`:
  - When no `issue_number` argument is provided, enter local mode
  - Prompt the user for a task description using `rich.prompt.Prompt.ask()`
  - Ask 1-2 clarifying questions based on the response
  - Combine into a refined prompt
  - Create pipeline with `mode="local"` and the prompt
  - Start and watch the pipeline (same as issue mode)
  - **Acceptance**: `egg-sdlc` (no args) prompts user, creates local pipeline, and runs it.

**Dependencies**: Phases 1-3

**Exit criteria**: Local mode works end-to-end.

### Phase 5: Remove old Claude-as-collaborator code

**Goal**: Remove the now-superseded token-gated approval mechanism and Claude orchestration code.

**Tasks**:

- [TASK-5-1] Remove `sandbox/.claude/hooks/sdlc-approve.sh` — the hook script is no longer needed since `egg-sdlc` handles HITL directly.
  - **Acceptance**: Hook file deleted.

- [TASK-5-2] Remove `setup_sdlc_tokens()` and `_start_settings_watchdog()` from `sandbox/entrypoint.py:609-773`. Remove the call at line 1727-1728. Remove the `EGG_SDLC_ISSUE` env var handling and CLAUDE.md auto-start injection (lines 722-731).
  - **Files**: `sandbox/entrypoint.py:609-773`, call at `:1727-1728`
  - **Acceptance**: `setup_sdlc_tokens()` and related functions removed. Entrypoint no longer generates tokens or installs hooks.

- [TASK-5-3] Remove the `--sdlc` flag from `sandbox/egg_lib/cli.py:114-119`. Remove the `sdlc_issue=args.sdlc` parameter from the `run_claude()` call at line 222.
  - **Files**: `sandbox/egg_lib/cli.py:114-119`, `:222`
  - **Acceptance**: `egg --sdlc` no longer accepted. Users directed to `egg-sdlc`.

- [TASK-5-4] Remove `sdlc_issue` parameter from `run_claude()` in `sandbox/egg_lib/runtime.py:478-487`. Remove the `EGG_SDLC_ISSUE` env var injection at lines 641-643.
  - **Files**: `sandbox/egg_lib/runtime.py:478-487`, `:641-643`
  - **Acceptance**: `run_claude()` no longer accepts or uses `sdlc_issue`.

- [TASK-5-5] Update `sandbox/.claude/commands/sdlc.md` to reference `egg-sdlc` instead of the manual API call workflow. The slash command should tell users to run `egg-sdlc` directly rather than orchestrating API calls. Keep the file as a redirect/reference so users who type `/sdlc` get guidance.
  - **Files**: `sandbox/.claude/commands/sdlc.md`
  - **Acceptance**: `/sdlc` tells the user to use `egg-sdlc` instead.

- [TASK-5-6] Remove `orchestrator/routes/sdlc_tokens.py` — the token generation and approval endpoints are no longer used. Remove its blueprint registration from the orchestrator app.
  - **Files**: `orchestrator/routes/sdlc_tokens.py`, orchestrator app registration
  - **Acceptance**: `/api/v1/sdlc-tokens/*` endpoints removed.

- [TASK-5-7] Remove `Pipeline.sdlc_token_gated` field from `orchestrator/models.py:210-212`. Remove the token-gate check in `orchestrator/routes/decisions.py:290-307` (the `if pipeline.sdlc_token_gated:` block). Remove the token-gated pipeline setup in `orchestrator/routes/pipelines.py:440,464-468`.
  - **Files**: `orchestrator/models.py:210-212`, `orchestrator/routes/decisions.py:290-307`, `orchestrator/routes/pipelines.py:440,464-468`
  - **Acceptance**: Token-gated logic fully removed from the orchestrator.

**Dependencies**: Phases 1-4 (new CLI must be working before removing old code)

**Exit criteria**: All token-gated approval code removed. No references to `setup_sdlc_tokens`, `sdlc-approve.sh`, `EGG_SDLC_ISSUE`, or `sdlc_token_gated` remain.

### Phase 6: Host-side integration

**Goal**: Wire up `egg-sdlc` as a host-side command that launches a container running the CLI.

**Tasks**:

- [TASK-6-1] Determine whether `egg-sdlc` on the host should be a standalone command or integrated into `egg`. The analysis suggests it should be a standalone entry point. Implement host-side launcher:
  - Option A: `bin/egg-sdlc` is a shell script that runs `egg --exec "egg-sdlc $@"` (uses existing exec infrastructure).
  - Option B: `bin/egg-sdlc` is a symlink like `bin/egg-pipeline-watch`, and the sandbox's `egg-sdlc` is invoked directly inside an already-running container.
  - Since `egg-sdlc` needs TTY access for interactive HITL and editor spawning, and it creates/watches pipelines that spawn other containers, it should run inside the orchestrator's network. The recommended approach is Option A: a host wrapper that calls `egg --exec "egg-sdlc <args>"` which handles container startup, networking, and TTY passthrough.
  - **Acceptance**: Running `egg-sdlc 635` on the host launches a container and runs the CLI interactively.

- [TASK-6-2] Ensure `egg --exec` passes through TTY correctly for interactive use. The `egg-sdlc` CLI needs stdin/stdout for the HITL menu and editor spawning. Verify this works by testing `egg --exec "egg-sdlc --help"`.
  - **Acceptance**: Interactive features (prompts, editor, Claude launch) work through the exec pathway.

**Dependencies**: Phase 4

**Exit criteria**: `egg-sdlc 635` works from the host command line.

### Phase 7: Documentation and workflow update

**Goal**: Update documentation and the workflow referenced in PR #623.

**Tasks**:

- [TASK-7-1] Update `docs/guides/sdlc-pipeline.md` to document the new `egg-sdlc` CLI. Document:
  - Installation/availability (in-container and host-side)
  - Usage: `egg-sdlc [<issue_number>] [--repo <owner/repo>]`
  - HITL interaction model (editor, Claude, approve, feedback)
  - Differences from old `egg --sdlc` flow
  - **Acceptance**: Guide accurately documents the new workflow.

- [TASK-7-2] Update `docs/index.md` to reference `egg-sdlc` in the CLI reference and task guides.
  - **Acceptance**: Index links to `egg-sdlc` documentation.

- [TASK-7-3] Update `bin/README.md` to add `egg-sdlc` entry.
  - **Acceptance**: README lists `egg-sdlc`.

- [TASK-7-4] Review the GitHub Actions workflow (`.github/workflows/sdlc-pipeline.yml`) to confirm it does NOT need changes. The GHA workflow drives the pipeline independently via issue checkboxes and workflow dispatch — it does not use `egg --sdlc` or the token-gated mechanism. This task is verification only.
  - **Acceptance**: GHA workflow confirmed unaffected.

**Dependencies**: Phase 5

**Exit criteria**: All documentation updated. GHA workflow confirmed unaffected.

## Files Modified

| File | Changes |
|------|---------|
| `sandbox/egg_lib/sdlc_cli.py` | **New** — Main CLI implementation |
| `sandbox/egg_lib/sdlc_hitl.py` | **New** — HITL checkpoint handler |
| `sandbox/egg_lib/orch_client.py` | **New** — Orchestrator API client |
| `sandbox/bin/egg-sdlc` | **New** — Executable entry point |
| `bin/egg-sdlc` | **New** — Host-side wrapper/symlink |
| `sandbox/.claude/commands/sdlc.md` | Modify — Redirect to `egg-sdlc` |
| `sandbox/.claude/hooks/sdlc-approve.sh` | **Delete** — Token-gated hook removed |
| `sandbox/entrypoint.py` | Modify — Remove `setup_sdlc_tokens()` and related code |
| `sandbox/egg_lib/cli.py` | Modify — Remove `--sdlc` flag |
| `sandbox/egg_lib/runtime.py` | Modify — Remove `sdlc_issue` parameter |
| `orchestrator/routes/sdlc_tokens.py` | **Delete** — Token endpoints removed |
| `orchestrator/models.py` | Modify — Remove `sdlc_token_gated` field |
| `orchestrator/routes/decisions.py` | Modify — Remove token-gate check |
| `orchestrator/routes/pipelines.py` | Modify — Remove token-gate setup |
| `docs/guides/sdlc-pipeline.md` | Modify — Document new CLI |
| `docs/index.md` | Modify — Add CLI reference |
| `bin/README.md` | Modify — Add `egg-sdlc` entry |

## Test Strategy

- **Unit tests**: Create `sandbox/egg_lib/tests/test_sdlc_cli.py` and `sandbox/egg_lib/tests/test_orch_client.py`. Test argument parsing, orchestrator URL detection, API client methods (mock HTTP), HITL menu logic, and local mode prompt flow.
- **Integration test**: Test the full `egg-sdlc` flow against a running orchestrator. This requires the Docker Compose stack. Verify: pipeline creation, SSE streaming, HITL checkpoint detection, decision resolution, pipeline completion.
- **Removal verification**: After Phase 5, grep the codebase for `setup_sdlc_tokens`, `sdlc-approve.sh`, `EGG_SDLC_ISSUE`, `sdlc_token_gated`, and `--sdlc` to confirm no stale references.
- **Existing tests**: Run `make test` (or `pytest` in relevant directories) to verify no regressions from the code removals.
- **Manual verification**: Run `egg-sdlc <issue>` against a real pipeline, verify DAG display, HITL edit/approve flow, and pipeline completion.
- **Test command**: `pytest sandbox/egg_lib/tests/ -v` for new tests; `make test` for full suite.

## Rollback Plan

1. The new CLI files (`sdlc_cli.py`, `sdlc_hitl.py`, `orch_client.py`, `bin/egg-sdlc`) are all new additions — revert by deleting them.
2. The removed files (`sdlc-approve.sh`, `sdlc_tokens.py`) can be restored from git history: `git checkout HEAD~1 -- <file>`.
3. The modified files (`entrypoint.py`, `cli.py`, `runtime.py`, `models.py`, `decisions.py`, `pipelines.py`) can be reverted individually with `git checkout HEAD~1 -- <file>`.
4. The GHA workflow is unmodified, so the remote pipeline path is unaffected by this change.
5. Since the old `egg --sdlc` and new `egg-sdlc` use the same orchestrator API, they could coexist temporarily during a transition period if needed.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| TTY handoff to editor/Claude fails inside container | Medium | High | Test subprocess TTY inheritance early. Use `subprocess.run()` with inherited stdin/stdout. This is a well-established pattern. |
| `rich` Live display conflicts with subprocess spawning | Medium | Medium | Stop Live display before spawning any subprocess (editor, Claude). Restart after subprocess exits. |
| SSE connection drops during long-running phases | Medium | Low | Implement reconnection logic with exponential backoff (same as `egg-pipeline-watch` handles timeouts). |
| Removing token-gated code breaks existing in-flight pipelines | Low | Medium | Token-gated pipelines are ephemeral (single session). Any in-flight pipeline using old mechanism will need to be recreated. Document in PR description. |
| Host-side exec pathway doesn't pass TTY correctly | Medium | High | Test early in Phase 6. Fallback: run `egg-sdlc` inside an already-running container via `docker exec -it`. |
| Local mode prompt collection is awkward in CLI | Low | Low | Use `rich.prompt` for clean input. Can iterate on UX in follow-up. |

## Migration Notes

- Users currently using `egg --sdlc <issue>` should switch to `egg-sdlc <issue>`.
- The `--sdlc` flag will be removed entirely (no deprecation period, as this is an internal tool with limited users).
- GitHub Actions SDLC workflow is unaffected.
- Existing pipelines created via the old mechanism will continue running but HITL decisions on token-gated pipelines will no longer be resolvable via the old hook. Users should cancel and recreate.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.

```yaml
# yaml-tasks
pr:
  title: "Replace Claude SDLC collaborator with egg-sdlc rich CLI"
  description: |
    Replace the fragile Claude-as-collaborator SDLC pipeline workflow with
    egg-sdlc, a rich Python CLI that directly handles DAG visualization and
    HITL checkpoints. Users can edit drafts with $EDITOR, launch Claude for
    AI assistance, or approve directly. Removes the token-gated approval
    mechanism (sdlc-approve.sh, setup_sdlc_tokens, sdlc_tokens.py).
phases:
  - id: 1
    name: Core CLI skeleton and orchestrator client
    goal: Create egg-sdlc entry point and reusable orchestrator API client
    tasks:
      - id: TASK-1-1
        description: "Create sandbox/egg_lib/sdlc_cli.py with main() entry point, argument parsing, and orchestrator health check"
        acceptance: "egg-sdlc --help displays usage; egg-sdlc with no orchestrator prints clear error"
        files:
          - sandbox/egg_lib/sdlc_cli.py
      - id: TASK-1-2
        description: "Create sandbox/egg_lib/orch_client.py wrapping orchestrator REST API (health, pipeline CRUD, decisions)"
        acceptance: "Each method makes correct HTTP call, returns parsed JSON, raises descriptive exceptions"
        files:
          - sandbox/egg_lib/orch_client.py
      - id: TASK-1-3
        description: "Create sandbox/bin/egg-sdlc executable entry point"
        acceptance: "egg-sdlc is executable and delegates to main module"
        files:
          - sandbox/bin/egg-sdlc
      - id: TASK-1-4
        description: "Create bin/egg-sdlc host-side symlink"
        acceptance: "bin/egg-sdlc resolves correctly"
        files:
          - bin/egg-sdlc
  - id: 2
    name: Pipeline watch loop with DAG visualization
    goal: Implement SSE-based watch loop with real-time DAG display
    tasks:
      - id: TASK-2-1
        description: "Add watch_pipeline() function with SSE parsing, DAG rendering, HITL detection, and terminal state handling"
        acceptance: "Live-updating DAG display; awaiting_human pauses watch loop"
        files:
          - sandbox/egg_lib/sdlc_cli.py
      - id: TASK-2-2
        description: "Implement main orchestration loop: create → start → watch → HITL → resume → complete"
        acceptance: "Pipeline progresses through phases with correct HITL pauses"
        files:
          - sandbox/egg_lib/sdlc_cli.py
  - id: 3
    name: HITL checkpoint handler
    goal: Present draft documents and interactive options at HITL checkpoints
    tasks:
      - id: TASK-3-1
        description: "Create sandbox/egg_lib/sdlc_hitl.py with handle_hitl_checkpoint() — document preview, interactive menu, decision resolution"
        acceptance: "User sees document preview and 5-option menu at HITL checkpoint"
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-2
        description: "Implement editor launch (option 1): stop rich display, spawn $EDITOR, resolve decision"
        acceptance: "User edits draft in vim/editor, saves, pipeline advances"
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-3
        description: "Implement Claude AI assistance (option 2): launch interactive Claude session, return to menu on exit"
        acceptance: "User starts Claude, makes edits, exits, returns to HITL menu"
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-4
        description: "Implement feedback option (option 4): collect text input, resolve decision with feedback"
        acceptance: "User types feedback that gets passed to next iteration"
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-5
        description: "Implement cancel option (option 5): cancel pipeline and exit CLI"
        acceptance: "Pipeline cancelled, CLI exits cleanly"
        files:
          - sandbox/egg_lib/sdlc_hitl.py
      - id: TASK-3-6
        description: "Verify egg-sdlc pipelines are not token-gated; standard decision resolution works"
        acceptance: "HITL decisions resolve via standard API without 403"
        files: []
  - id: 4
    name: Local mode support
    goal: Support prompt-driven local pipelines with no issue number
    tasks:
      - id: TASK-4-1
        description: "Implement local mode: prompt user, collect clarifications, create local pipeline, start and watch"
        acceptance: "egg-sdlc (no args) prompts user, creates local pipeline, runs it"
        files:
          - sandbox/egg_lib/sdlc_cli.py
  - id: 5
    name: Remove old Claude-as-collaborator code
    goal: Remove token-gated approval mechanism and Claude orchestration code
    tasks:
      - id: TASK-5-1
        description: "Delete sandbox/.claude/hooks/sdlc-approve.sh"
        acceptance: "Hook file deleted"
        files:
          - sandbox/.claude/hooks/sdlc-approve.sh
      - id: TASK-5-2
        description: "Remove setup_sdlc_tokens(), _start_settings_watchdog(), and EGG_SDLC_ISSUE handling from entrypoint.py"
        acceptance: "Functions removed; entrypoint no longer generates tokens or installs hooks"
        files:
          - sandbox/entrypoint.py
      - id: TASK-5-3
        description: "Remove --sdlc flag from sandbox/egg_lib/cli.py"
        acceptance: "egg --sdlc no longer accepted"
        files:
          - sandbox/egg_lib/cli.py
      - id: TASK-5-4
        description: "Remove sdlc_issue parameter from run_claude() in runtime.py"
        acceptance: "run_claude() no longer accepts sdlc_issue"
        files:
          - sandbox/egg_lib/runtime.py
      - id: TASK-5-5
        description: "Update sdlc.md slash command to redirect users to egg-sdlc"
        acceptance: "/sdlc tells user to use egg-sdlc"
        files:
          - sandbox/.claude/commands/sdlc.md
      - id: TASK-5-6
        description: "Delete orchestrator/routes/sdlc_tokens.py and remove blueprint registration"
        acceptance: "/api/v1/sdlc-tokens/* endpoints removed"
        files:
          - orchestrator/routes/sdlc_tokens.py
      - id: TASK-5-7
        description: "Remove sdlc_token_gated field from models.py, token-gate check from decisions.py, token-gate setup from pipelines.py"
        acceptance: "Token-gated logic fully removed from orchestrator"
        files:
          - orchestrator/models.py
          - orchestrator/routes/decisions.py
          - orchestrator/routes/pipelines.py
  - id: 6
    name: Host-side integration
    goal: Wire up egg-sdlc as a host command via egg --exec
    tasks:
      - id: TASK-6-1
        description: "Implement bin/egg-sdlc host wrapper using egg --exec for container launch with TTY"
        acceptance: "egg-sdlc 635 on host launches container and runs CLI interactively"
        files:
          - bin/egg-sdlc
      - id: TASK-6-2
        description: "Verify TTY passthrough for interactive features (prompts, editor, Claude)"
        acceptance: "Interactive features work through exec pathway"
        files: []
  - id: 7
    name: Documentation and workflow update
    goal: Update docs and verify GHA workflow unaffected
    tasks:
      - id: TASK-7-1
        description: "Update docs/guides/sdlc-pipeline.md to document egg-sdlc CLI"
        acceptance: "Guide accurately documents new workflow"
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-7-2
        description: "Update docs/index.md to reference egg-sdlc"
        acceptance: "Index links to egg-sdlc documentation"
        files:
          - docs/index.md
      - id: TASK-7-3
        description: "Update bin/README.md to add egg-sdlc entry"
        acceptance: "README lists egg-sdlc"
        files:
          - bin/README.md
      - id: TASK-7-4
        description: "Verify GHA workflow (sdlc-pipeline.yml) needs no changes"
        acceptance: "GHA workflow confirmed unaffected"
        files:
          - .github/workflows/sdlc-pipeline.yml
```

---

*Authored-by: egg*
