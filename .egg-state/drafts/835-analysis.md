# Analysis: Scope agent prompts to role-relevant context only

> Issue: #835 | Phase: refine

## Problem Statement

Every agent in the SDLC pipeline currently receives the full original issue body (`pipeline.prompt`) as its "Task Description", regardless of role. A tester does not need the feature request narrative — it needs to know what code changed and what to verify. A documenter does not need the technical constraints from the issue — it needs to know what was built and what users need to know. In Tier 3 dispatch, the phase-scoped coder prompt also embeds the **entire plan draft** even though the agent only implements one phase. This wastes context tokens and risks agents working outside their assigned scope.

The goal is to replace the one-size-fits-all `pipeline.prompt` embedding with role-appropriate context summaries, while keeping full context accessible on demand via file paths and CLI commands.

## Current Behavior

### `_build_agent_prompt()` — all non-coder roles (`pipelines.py:2014-2018`)

Every tester, documenter, integrator, architect, task_planner, and risk_analyst receives the raw issue body verbatim under `## Task Description`:

```python
if prompt:
    lines.append("## Task Description\n")
    lines.append(prompt)  # Full issue body, regardless of role
```

For a tester in Tier 3 phase 3, this includes context about phases 1 and 2 that is irrelevant noise.

### `_build_phase_scoped_prompt()` — Tier 3 coders (`pipelines.py:2293-2304`)

A Tier 3 coder implementing phase 2 sees the full plan including phases 1 and 3:

```python
if review_cycle == 0:
    draft_text = _read_phase_draft(...)
    if draft_text:
        lines.append("## Plan\n")
        lines.append(f"```markdown\n{draft_text}\n```\n")  # Full plan
```

The phase scope section at `pipelines.py:2307-2320` correctly limits the task checklist to the current phase, but the plan section above it embeds everything.

### Tier 3 tester/documenter (`_run_tier3_implement`, `pipelines.py:2555-2642`)

Both the tester and documenter receive `pipeline.prompt` (full issue body) via `_build_agent_prompt()`, then get a phase scope section appended. The tester at `pipelines.py:2555-2579` shows this pattern clearly — `prompt=pipeline.prompt` passes the full issue, then phase scope lines are concatenated.

### `_build_phase_prompt()` — Tier 2 coders (implement phase, `pipelines.py:1725-1742`)

For Tier 2, the coder gets the full plan/analysis draft embedded on cycle 0. This is generally appropriate since Tier 2 tasks are medium complexity and the coder owns the full scope. No change needed here.

## Constraints

- **Backward compatibility with architect/task_planner/risk_analyst**: These roles legitimately need the full issue body for analysis. The issue explicitly lists them as non-goals for context scoping.
- **Reviewer prompts are already separate**: `_build_review_prompt()` has its own prompt construction at `pipelines.py:1229-1348` and is not affected.
- **Handoff data mechanism unchanged**: Handoff data flows via `EGG_HANDOFF_DATA` env var (set in `_execute_wave_with_spawn_fn`). This issue only changes prompt content, not the handoff mechanism.
- **"Summarize, don't omit" principle**: Agents still need a 1-2 sentence background summary for orientation. The goal is relevance, not aggressive token minimization.
- **Full context on demand**: Agents must have pointers (`gh issue view <N>`, `cat .egg-state/drafts/...`) to access the full context when needed.
- **Test coverage**: Existing prompt tests in `test_pipeline_prompts.py` (738 lines, 12 test classes) cover plan embedding, revision modes, contract rendering, and shared criteria. New role-context logic needs comparable coverage.

## Options Considered

### Option A: New `_build_role_context()` helper replacing `pipeline.prompt` inline

**Approach**: Create a single `_build_role_context(role, phase_obj, pipeline, handoff_summary)` function that returns role-appropriate markdown. Replace the `if prompt:` block in `_build_agent_prompt()` with a call to this helper. For roles that need the full body (architect, task_planner, risk_analyst), the helper returns the original `pipeline.prompt`. For scoped roles (tester, documenter, integrator), it builds a structured summary. A companion `_summarize_issue()` helper extracts a 1-2 sentence summary from the issue title + first paragraph.

For `_build_phase_scoped_prompt()`, extract only the plan overview section and current phase detail, embedding other phases as one-line summaries.

**Pros**:
- Single point of control for role-context mapping — easy to extend for new roles
- Matches the design proposed in the issue itself
- Clean separation: `_build_role_context()` handles what context to include; `_build_agent_prompt()` handles prompt structure
- `_summarize_issue()` is reusable across all roles

**Cons**:
- Requires parsing/summarizing the issue body, which may be fragile if issue formats vary
- Introduces a new abstraction layer in already-complex prompt building code

### Option B: Role-specific branches inside `_build_agent_prompt()` without helper extraction

**Approach**: Expand the existing role-specific `if/elif` block in `_build_agent_prompt()` (currently at `pipelines.py:2029-2158`) to include role-specific context *before* the `## Your Task` section. Move the `if prompt:` block inside the conditional, so architect/task_planner/risk_analyst get the full body while tester/documenter/integrator get inline summaries. For `_build_phase_scoped_prompt()`, add plan filtering logic directly.

**Pros**:
- Minimal structural change — no new functions, just expanding existing conditionals
- Easier to review since changes are localized to existing blocks
- No risk of over-abstracting

**Cons**:
- `_build_agent_prompt()` is already 300 lines; adding context-building logic per role makes it harder to maintain
- Plan filtering for Tier 3 duplicates logic if done inline in `_build_phase_scoped_prompt()`
- Harder to test individual role context in isolation

### Option C: Role-specific prompt builder functions per agent type

**Approach**: Create separate functions like `_build_tester_prompt()`, `_build_documenter_prompt()`, `_build_integrator_prompt()` that each build a complete prompt from scratch. Have `_build_agent_prompt()` dispatch to these instead of using one monolithic function.

**Pros**:
- Maximum clarity — each role's prompt is fully self-contained
- Easy to test each role independently
- No risk of cross-role context leakage

**Cons**:
- Significant duplication of shared prompt structure (context header, phase restrictions, contract CLI instructions)
- Much larger diff, touching more code
- Divergence risk: shared improvements must be applied to every builder function
- Over-engineering for what is primarily a context-scoping change

## Recommended Approach

**Option A** — `_build_role_context()` helper. This is the approach the issue itself proposes, and it strikes the right balance between maintainability and scope of change. The key arguments:

1. **Single responsibility**: Context selection is separated from prompt structure. `_build_agent_prompt()` stays focused on the shared prompt skeleton, while `_build_role_context()` owns what content each role sees.

2. **Testability**: The helper can be unit tested in isolation — given a role and pipeline state, verify the returned context contains the right elements and omits the wrong ones.

3. **Manageable scope**: The core change touches `_build_agent_prompt()` (replace `if prompt:` block), `_build_phase_scoped_prompt()` (filter plan to overview + current phase), and the Tier 3 tester/documenter prompt construction in `_run_tier3_implement()`. The shared prompt skeleton (header, phase restrictions, completion guidance) stays untouched.

4. **Issue alignment**: The proposed `_build_role_context()` and `_summarize_issue()` helpers match the issue's design. Diverging from the issue's design without strong reason adds review friction.

For `_summarize_issue()`, a simple approach — use issue title + first paragraph (truncated at ~200 chars) — is sufficient. The issue explicitly says "A few hundred extra tokens of useful background is fine."

## Touch Points

| File | Function | Change |
|------|----------|--------|
| `orchestrator/routes/pipelines.py` | `_build_agent_prompt()` | Replace `if prompt:` block with `_build_role_context()` call; keep full body for architect/task_planner/risk_analyst |
| `orchestrator/routes/pipelines.py` | `_build_phase_scoped_prompt()` | Filter plan to overview + current phase detail; other phases as one-line summaries |
| `orchestrator/routes/pipelines.py` | `_run_tier3_implement()` | Pass `phase_obj` and contract data to tester/documenter prompt construction instead of `pipeline.prompt` |
| `orchestrator/routes/pipelines.py` | New: `_build_role_context()` | Role-context dispatch function |
| `orchestrator/routes/pipelines.py` | New: `_summarize_issue()` | Extract 1-2 sentence summary from issue title + body |
| `orchestrator/tests/test_pipeline_prompts.py` | New test classes | Test role-context output for each scoped role; test plan filtering; test summary extraction |

## Open Questions

None — the issue is well-specified with clear acceptance criteria, explicit non-goals, and a concrete design. The recommended approach aligns with the issue's proposal. No HITL decisions are needed before proceeding to plan.

---

*Authored-by: egg*

<!-- yaml-frontmatter
complexity_tier: mid
-->

# metadata
complexity_tier: mid
