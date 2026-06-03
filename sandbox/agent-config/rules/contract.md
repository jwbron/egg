# SDLC Contract

Track SDLC pipeline progress through the contract. Full reference:
`$EGG_REPO_PATH/docs/reference/sdlc-contract.md`

**`egg-contract`'s free-text args (`--question`, `--options`, `--notes`) do NOT have file/stdin channels yet.** The slice-5 prose-arg channels added file/stdin variants to `egg-orch` (`consensus propose / ack / nack / withdraw` and `brc resolve-obligation`) only; `sandbox/egg_lib/contract_cli.py` was not touched. When passing LLM-authored prose to `egg-contract`, keep the value free of shell metacharacters (no backticks, no `$(...)`, no unquoted `<`, `>`, `;`, `|`, `&`) — in a `Bash` command string the shell interprets them and the prose is silently corrupted (a backtick or `$(...)` span is *executed* as a command rather than stored). Quote the entire value with single quotes when possible; if the value must contain a single quote, escape it.

Adding `-file` / stdin channels to the contract CLI is a follow-up; until then, keep prose short and shell-safe at the `egg-contract` boundary, or write the prose to a draft file first and reference the file path in the `egg-contract` value rather than embedding the prose inline.

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

- `egg-contract add-decision --question "<text>" --options "A" "B"` — Create a HITL multiple-choice decision. **Available to every role, not just refiner/planner** — producers should call this when reviewer NACKs name an architectural scope question the operator (not the producer) must decide. See [`mission.md`](mission.md) → "HITL Decisions vs. Operational Alerts" for the producer-side checklist; do NOT file an `OVERSEER_ALERT` for this. *(No `--question-file` channel today — keep `<text>` shell-safe; see the prose-arg note above.)*
- `egg-contract add-feedback --question "<text>" --format markdown` — Create an open-ended HITL feedback request.

Tester→coder coverage-gap handoffs are written to
`phases.<p>.tasks.<t>.gaps[]` through the handler layer (no CLI by
design — operators don't need it); see
`sandbox/egg_agent_tools/handlers/task.py::mark_gap`.

Full reference: [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
and [`docs/reference/sdlc-contract.md`](../../../docs/reference/sdlc-contract.md).
