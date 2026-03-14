# Analysis: Replace Claude-as-collaborator with rich CLI for SDLC pipeline (#635)

## Problem Statement

The current SDLC pipeline uses Claude (the LLM) as the interactive collaborator for driving the pipeline within a sandbox container. When a user runs `egg --sdlc <issue>`, Claude receives the `/sdlc` slash command prompt and orchestrates the pipeline by making API calls to the orchestrator, watching progress, and handling HITL checkpoints. This approach is problematic because:

1. **Claude gets confused**: The LLM is inconsistent at following the multi-step orchestration protocol (create pipeline, start pipeline, watch via SSE, handle HITL decisions, advance phases).
2. **Unreliable HITL flow**: The current token-gated approval mechanism (`!approve <phase>`) requires Claude to detect approval prompts, read tokens from `/dev/tty` via a hook script, and communicate results. This is fragile.
3. **Poor user experience**: Users cannot directly interact with pipeline artifacts. Documents produced during refine/plan phases are mediated through Claude rather than being directly editable.
4. **Wasted context window**: Claude's context is consumed by orchestration boilerplate rather than actual software engineering.

The issue requests creating `egg-sdlc`, a rich CLI tool that runs directly in the egg container, uses `egg-pipeline-watch` for DAG visualization, and lets humans directly interact with documents at HITL checkpoints (e.g., editing with vim, or starting Claude for AI assistance).

## Current Architecture

### How the SDLC pipeline runs today

**GitHub Actions mode (remote):** The pipeline is driven by `.github/workflows/sdlc-pipeline.yml`, which orchestrates phases through a chain of workflow jobs. HITL decisions use GitHub issue checkboxes detected by `sdlc-hitl.yml`. This mode is mature and well-tested.

**Local mode (current `egg --sdlc`):**
1. Host CLI (`sandbox/egg_lib/cli.py`) passes `--sdlc <issue>` flag
2. `runtime.py` sets `EGG_SDLC_ISSUE` env var in the container
3. `entrypoint.py:setup_sdlc_tokens()` generates approval tokens, installs the `sdlc-approve.sh` hook, and appends an auto-start instruction to `CLAUDE.md` so Claude runs `/sdlc <issue>` immediately
4. Claude interprets `sandbox/.claude/commands/sdlc.md`, makes HTTP calls to the orchestrator API to create/start the pipeline, then runs `egg-pipeline-watch`
5. For HITL approvals: the `sdlc-approve.sh` hook intercepts `!approve <phase>` prompts and validates tokens against the orchestrator

### Key existing components

| Component | Location | Role |
|-----------|----------|------|
| `egg-pipeline-watch` | `sandbox/bin/egg-pipeline-watch` | Real-time SSE-based DAG viewer (386 lines, Python) |
| `egg-status` | `bin/egg-status` | Multi-pipeline dashboard (host-side) |
| Orchestrator API | `orchestrator/` | Pipeline CRUD, phase management, SSE streaming, container spawning |
| DAG Visualizer | `orchestrator/dag_visualizer.py` | ASCII DAG rendering with status symbols |
| Contract system | `shared/egg_contracts/` | Pipeline state, HITL decisions, role-based access |
| `/sdlc` slash command | `sandbox/.claude/commands/sdlc.md` | Claude-interpreted orchestration script |
| Token-gated approval | `sandbox/.claude/hooks/sdlc-approve.sh` + `entrypoint.py:setup_sdlc_tokens()` | Hook-based HITL for local mode |

### PR #623 reference

PR #623 (now merged) documented the `/onboarding-docs` slash command. The issue references it as the workflow that should be updated to use the new system. This means the `egg-sdlc` CLI should be the new entry point that replaces both `egg --sdlc <issue>` and the `/sdlc` slash command used inside containers.

## Constraints and Dependencies

1. **Must run inside the egg container**: The CLI will execute within the sandboxed Docker environment where the orchestrator and gateway are accessible.
2. **Orchestrator API dependency**: The CLI is a client to the existing orchestrator REST API. It does not replace the orchestrator; it replaces Claude as the user-facing controller.
3. **Gateway sidecar**: Git/gh operations still route through the gateway. The CLI itself should not need direct GitHub access.
4. **Terminal UI requirements**: The CLI must be a rich terminal application. `egg-pipeline-watch` already uses ANSI codes, cursor control, and SSE streaming, which provides a pattern.
5. **HITL at phase boundaries**: When a phase completes and requires human review, the CLI must present the document (analysis or plan) and let the user choose how to iterate: direct editing (vim/nano), AI-assisted editing (launch Claude), or approve to proceed.
6. **Backward compatibility**: The GitHub Actions workflow (`sdlc-pipeline.yml`) should remain unchanged. The new CLI only replaces the local interactive flow.
7. **Existing libraries available**: Python 3.12, `rich` (for terminal UI), `requests`, `curses` are all available in the sandbox container.

## Options Considered

### Option A: Rich Python CLI using `rich` library

Build `egg-sdlc` as a Python CLI tool using the `rich` library (already available in the sandbox) for terminal rendering. The tool would be a single entry point that:

- Creates/starts the pipeline via orchestrator API
- Displays the DAG visualization (reusing `egg-pipeline-watch` rendering logic or `rich` panels)
- Polls or SSE-streams for phase completion
- On HITL checkpoint: presents the draft document, offers a menu (edit with $EDITOR, launch Claude, approve, provide feedback)
- After user action: signals the orchestrator to advance

**Pros:**
- `rich` provides panels, tables, progress bars, syntax highlighting, and live displays
- Single process, simple architecture
- Can reuse SSE parsing from `egg-pipeline-watch`
- Familiar Python ecosystem in the sandbox

**Cons:**
- `rich` live displays can conflict with subprocess TTY (e.g., launching vim)
- Need to handle TTY handoff carefully when spawning editors or Claude

### Option B: TUI application using `textual` (rich-based)

Build a full TUI (Text User Interface) application using `textual` (the TUI framework built on `rich`).

**Pros:**
- Full widget-based UI with panels, scrolling, focus management
- Native support for key bindings and event-driven architecture
- Can display DAG in one panel and document in another

**Cons:**
- Heavier dependency (`textual` may need to be added)
- More complex for what is essentially a linear workflow
- TTY handoff to external editors/processes is harder in a full TUI
- Steeper learning curve for maintenance

### Option C: Shell script with `dialog`/`whiptail`

Build `egg-sdlc` as a bash script using `dialog` or `whiptail` for menus and `egg-pipeline-watch` for visualization.

**Pros:**
- Simple, minimal dependencies
- Easy to launch external processes (vim, claude)
- Dialog-based menus are familiar to sysadmins

**Cons:**
- Limited rendering capabilities
- Harder to maintain complex state management
- No syntax highlighting for document preview
- Shell scripting limitations for error handling

### Option D: Minimal Python CLI with subprocess orchestration

Build a lightweight Python CLI that acts primarily as a coordinator: it polls the orchestrator, prints status, and at HITL checkpoints launches the appropriate subprocess (editor, pager, Claude). No rich TUI framework.

**Pros:**
- Simplest approach - minimal new code
- Easy TTY handoff (just exec/subprocess)
- Follows Unix philosophy (compose small tools)
- Can reuse `egg-pipeline-watch --once` for snapshots

**Cons:**
- Less polished visual experience than `rich`
- No live-updating DAG during phase execution (would need to re-run `egg-pipeline-watch`)
- More manual terminal output formatting

## Recommended Approach: Option A (Rich Python CLI)

**Justification:**

Option A provides the best balance of user experience and implementation simplicity. Key reasons:

1. **`rich` is already available**: The sandbox already has `rich` installed (used by other tools). No new dependencies needed.

2. **Reuses existing patterns**: `egg-pipeline-watch` already demonstrates SSE parsing and terminal rendering. The new CLI can import or adapt this code.

3. **HITL UX is the critical differentiator**: The whole point of this change is better human interaction at checkpoints. `rich` provides syntax-highlighted document preview, clear menus, and formatted output that make the HITL experience excellent.

4. **TTY handoff is solvable**: `rich`'s `Live` display can be stopped before spawning subprocesses (vim, Claude), then resumed after. This is a well-known pattern.

5. **Linear workflow fits a CLI**: The SDLC pipeline is fundamentally linear (refine -> plan -> implement -> PR). A TUI framework (Option B) is overkill. A simple CLI with `rich` formatting and a polling loop is sufficient.

### High-level design

```
egg-sdlc [<issue_number>] [--repo <owner/repo>]
```

**Main loop:**
```
1. Validate orchestrator is running
2. Determine mode (issue vs local)
3. Create pipeline via orchestrator API
4. Start pipeline
5. Enter watch loop:
   a. Stream SSE events from orchestrator
   b. Render DAG visualization (full-screen, live-updating)
   c. On phase completion requiring HITL:
      - Pause DAG display
      - Show document preview (rich.syntax or rich.markdown)
      - Present menu:
        [1] Edit with $EDITOR (default: vim)
        [2] Start Claude for AI-assisted editing
        [3] Approve and advance to next phase
        [4] Provide feedback (text input)
        [5] Cancel pipeline
      - After user action, resume DAG display
   d. On pipeline completion: show summary and exit
```

**Key files to create/modify:**

| File | Action | Purpose |
|------|--------|---------|
| `sandbox/bin/egg-sdlc` | Create | New CLI entry point |
| `sandbox/egg_lib/sdlc_cli.py` | Create | Main CLI implementation |
| `sandbox/egg_lib/sdlc_hitl.py` | Create | HITL interaction handlers |
| `sandbox/egg_lib/cli.py` | Modify | Remove `--sdlc` flag (replaced by `egg-sdlc`) |
| `sandbox/entrypoint.py` | Modify | Remove `setup_sdlc_tokens()` and related code |
| `sandbox/.claude/commands/sdlc.md` | Modify | Update to reference `egg-sdlc` instead of API calls |
| `sandbox/.claude/hooks/sdlc-approve.sh` | Remove | No longer needed (CLI handles HITL directly) |
| `bin/egg-sdlc` | Create | Symlink or host-side wrapper |

**Phase-specific HITL behavior:**

| Phase | Document | HITL Action |
|-------|----------|-------------|
| Refine | `.egg-state/drafts/{id}-analysis.md` | Review analysis, edit or approve |
| Plan | `.egg-state/drafts/{id}-plan.md` | Review plan, edit or approve |
| Implement | PR diff (via `gh pr diff`) | View changes, provide feedback or approve |
| PR | N/A | Pipeline complete, human merges via GitHub UI |

**Integration with `egg-pipeline-watch`:** Rather than reimplementing the SSE viewer, `egg-sdlc` should either:
- Import and call `watch_pipeline()` from `egg-pipeline-watch` as a library
- Or embed the SSE watching logic directly, adding HITL interception

The latter is preferred since HITL events need to interrupt the DAG display.

## Open Questions

1. **Should `egg-sdlc` replace `egg --sdlc` on the host side too?** Currently `egg --sdlc 123` is a host CLI flag. Should it become `egg-sdlc 123` (host command that launches a container running `egg-sdlc`)?

2. **Editor integration**: Should the CLI default to `$EDITOR`, `vim`, or offer a built-in editor? How should it handle the case where the user wants to use a GUI editor (unlikely in Docker, but possible with mounted volumes)?

3. **Claude integration at HITL**: When the user chooses "Start Claude for AI-assisted editing", should this launch a new Claude Code session in the same container, or spawn a new container? The existing architecture spawns new containers per phase, but for interactive editing, the same container is more practical.

4. **GitHub Actions workflow update**: The issue mentions updating the workflow from PR #623. This likely means the GitHub Actions pipeline should also be able to invoke `egg-sdlc` in non-interactive mode, or this concern is separate from the CLI work.

5. **Deprecation timeline for `--sdlc` flag**: Should the old `egg --sdlc` pathway be removed immediately, or kept as a deprecated alias?
