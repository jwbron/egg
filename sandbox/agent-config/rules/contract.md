# SDLC Contract

Track SDLC pipeline progress through the contract. Full reference:
`$EGG_REPO_PATH/docs/reference/sdlc-contract.md`

**Use the structured contract tool — do not compose an `egg-contract`
command for the `Bash` tool.** The free-text fields (`--question`,
`--options`, `--notes`) carry LLM-authored prose. In a `Bash` command
string the shell interprets backticks, `$(...)`, `$VAR`, `<`, `>`, `;`,
`|`, and `&` — so prose that contains them (a markdown code span, a
URL, a `<` comparison) is silently corrupted, and a backtick or
`$(...)` span is *executed* as a command rather than stored. The
structured tool passes each field as data and never touches a shell:

- **`claude_agent_sdk` harness** — the `mcp__sdlc__*` / `mcp__task__*` /
  `mcp__phase__*` tools (mapped below).
- **`EGG_HARNESS=egg`** — the `EggContract` tool: subcommand as
  `command`, each flag and value as a separate `args` element.

The `egg-contract` commands below are the reference for what each
operation does and stay available to human operators; agents invoke
them through the structured tool.

**Commands:**

| Command | Purpose |
|---------|---------|
| `egg-contract show` | View current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract complete-task --task <id> [--commit <sha>]` | Mark task as complete (optionally link commit) |
| `egg-contract complete-phase --phase <id> [--commit <sha>]` | Mark phase as complete (optionally link commit) |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract verify-criterion --criterion <id>` | Mark an acceptance criterion verified (REVIEWER role only) |
| `egg-contract add-decision --question <text> --options "A" "B"` | Create HITL decision (multiple choice) |
| `egg-contract add-feedback --question <text> --format markdown` | Create feedback request (open-ended) |

**Workflow**: `egg-contract show` → work on tasks → `complete-task` after each task → `complete-phase` after each phase → `add-decision` or `add-feedback` if blocked.

**Env**: `EGG_ISSUE_NUMBER`, `EGG_REPO_PATH` (auto-set).

## MCP tool equivalents (`claude_agent_sdk` harness)

On the `claude_agent_sdk` harness the operations above are also exposed
as in-process MCP tools, which share the same handler the CLI uses
(drift-gate enforced). Prefer them for the reason in the callout above:
free-text routed to the CLI through the `Bash` tool is mangled by the
shell. Iteration-2 ([#1917](https://github.com/jwbron/egg/issues/1917))
added the contract verbs that iteration-1 left as Bash-only:

- `mcp__sdlc__show_contract` — Prefer this over `egg-contract show`. Returns the contract dict (optional `fields=[…]` projection; unknown field raises).
- `mcp__task__add_commit` — Prefer this over `egg-contract add-commit`. Links a commit SHA to a task; does not mark the task complete.
- `mcp__task__update_notes` — Prefer this over `egg-contract update-notes`. Appends implementation notes to a task.
- `mcp__phase__complete_phase` — Prefer this over `egg-contract complete-phase`. Transitions phase status to "complete" (downstream `phase_complete` signal fires).
- `mcp__sdlc__verify_criterion` — Prefer this over `egg-contract verify-criterion`. Marks an acceptance criterion verified; **REVIEWER role only** (the gateway rejects non-REVIEWER writers — no in-process re-check).
- `mcp__task__complete` — Prefer this over `egg-contract complete-task`. Marks a contract task complete and optionally links a commit.
- `mcp__sdlc__register_open_question` — Prefer this over `egg-contract add-decision`. Creates a HITL multiple-choice decision. **Available to every role, not just refiner/planner** — coders should call this when reviewer NACKs name an architectural scope question the operator (not the coder) must decide. See [`mission.md`](mission.md) → "HITL Decisions vs. Operational Alerts" for the producer-side checklist; do NOT file an `OVERSEER_ALERT` for this.
- `mcp__sdlc__request_feedback` — Prefer this over `egg-contract add-feedback`. Creates an open-ended HITL feedback request.

A new no-CLI tool also lives in the contract surface:

- `mcp__task__mark_gap` — Tester→coder coverage-gap handoff written to `phases.<p>.tasks.<t>.gaps[]`. No CLI counterpart by design (decision-4); operators don't need it.

See [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
for the full 29-verb inventory.
