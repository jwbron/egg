# SDLC Contract

Track SDLC pipeline progress through the contract. Full reference:
`$EGG_REPO_PATH/docs/reference/sdlc-contract.md`

**Route free text through `--<arg>-file PATH` or stdin, not a bare
`--<arg> "…"`.** The free-text fields (`--question`, `--options`,
`--notes`) carry LLM-authored prose. In a `Bash` command string the
shell interprets backticks, `$(...)`, `$VAR`, `<`, `>`, `;`, `|`, and
`&` — so prose that contains them (a markdown code span, a URL, a `<`
comparison) is silently corrupted, and a backtick or `$(...)` span is
*executed* as a command rather than stored. The slice-5 prose-arg
channels (introduced in [#2908](https://github.com/jwbron/egg/issues/2908)
slice-5) let you route the value as data: pass `--<arg>-file PATH` to
read from a file, or `--<arg> -` to read from stdin. Mixing forms is
rejected — exactly one source per argument. Example:

```bash
cat > /tmp/notes.md <<'EOF'
Multi-line notes with `code spans`, $vars, and <comparators>.
EOF
egg-contract update-notes --task task-1-2 --notes-file /tmp/notes.md
```

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

## HITL gates — open questions and feedback

- `egg-contract add-decision --question-file PATH --options "A" "B"` — Create a HITL multiple-choice decision. **Available to every role, not just refiner/planner** — producers should call this when reviewer NACKs name an architectural scope question the operator (not the producer) must decide. See [`mission.md`](mission.md) → "HITL Decisions vs. Operational Alerts" for the producer-side checklist; do NOT file an `OVERSEER_ALERT` for this.
- `egg-contract add-feedback --question-file PATH --format markdown` — Create an open-ended HITL feedback request.

Tester→coder coverage-gap handoffs are written to
`phases.<p>.tasks.<t>.gaps[]` through the handler layer (no CLI by
design — operators don't need it); see
`sandbox/egg_agent_tools/handlers/task.py::mark_gap`.

Full reference: [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
and [`docs/reference/sdlc-contract.md`](../../../docs/reference/sdlc-contract.md).
