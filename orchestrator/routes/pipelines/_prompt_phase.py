"""phase-prompt + BRC preamble helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _build_phase_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    repo_path: str | None = None,
    operator_directives: list[_pkg.OperatorDirective] | None = None,
    iteration_history: list[_pkg.IterationSummary] | None = None,
) -> str:
    """Build a phase-specific prompt for the sandbox Claude invocation.

    Follows a structured prompt format:
    Context → Task → Restrictions → Completion.
    """
    # --- Context header ---
    lines = [f"You are in the **{phase}** phase of the SDLC pipeline.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # --- Phase iteration context (HITL kickbacks) ---
    # Operator directives have their own section with explicit precedence
    # prose so reviewers cannot faithfully NACK a directive-driven change
    # against a stale default rubric. See issue #2795.
    iteration_context = _pkg._build_phase_iteration_context(operator_directives, iteration_history)
    if iteration_context:
        lines.append(iteration_context)

    # --- Prior review feedback (agentic revision cycles only) ---
    # Scoped to agentic-cycle review feedback since #2795 — HITL kickback
    # feedback now flows through ``operator_directives`` / the iteration
    # context section above.
    if review_feedback:
        if review_cycle > 0:
            lines.append(f"## Prior Review Feedback (Cycle {review_cycle})\n")
        else:
            lines.append("## Prior Review Feedback\n")
        has_tester_findings = _pkg.TESTER_FINDINGS_HEADER in review_feedback
        if phase == "implement":
            revision_action = "Address the feedback below and revise your implementation."
        else:
            revision_action = (
                "Address the feedback below and revise your draft **in-place** "
                "(overwrite the same file)."
            )
        if review_cycle == 0:
            consensus_override = (
                " Even if an existing draft appears "
                "to have reached consensus previously, that consensus is "
                "superseded — you must revise to address this feedback before "
                "proposing a new consensus."
            )
        else:
            consensus_override = ""
        if has_tester_findings:
            lines.append(
                "The reviewer and tester found issues with your previous work. "
                f"{revision_action}{consensus_override}\n"
            )
        else:
            preamble_noun = "implementation" if phase == "implement" else "draft"
            lines.append(
                f"The reviewer found issues with your previous {preamble_noun}. "
                f"{revision_action}{consensus_override}\n"
            )
        lines.append(review_feedback)
        lines.append("")

    # --- Task description ---
    # Skip re-embedding the full task description on revision cycles for
    # implement phase — the coder already knows the task from cycle 0.
    if prompt and not (phase == "implement" and review_cycle > 0):
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # --- Phase-specific instructions ---
    lines.append("## Your Task\n")

    # Get the correct draft path based on mode
    analysis_path = _pkg._get_draft_path(
        "refine", issue_number=issue_number, pipeline_id=pipeline_id
    )
    plan_path = _pkg._get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)

    if phase == "refine":
        lines.extend(
            [
                "Analyze this issue and produce a structured analysis document. Your goal is to:\n",
                "1. Understand the problem or feature request",
                "2. Research the current codebase to understand existing patterns",
                "3. Research externally when the task involves third-party libraries, APIs, "
                "or integrations — use WebSearch and WebFetch (when available) to look up "
                "current documentation, best practices, and known issues. Skip external "
                "research for purely internal changes where codebase context is sufficient.",
                "4. Identify constraints and dependencies",
                "5. Consider multiple implementation approaches",
                "6. Recommend an approach with justification",
                "7. Surface the questions and uncertainties that genuinely need a "
                "human to answer — see `## How to Populate Open Questions` below for "
                "the filter (slice/PR packaging, implementation strategy, and "
                "API/schema details belong to the planner, not the refiner)",
                "",
                "**IMPORTANT**: Do NOT create an implementation plan, task breakdown, "
                "or phased rollout. That is the **plan** phase's job. Stay focused on "
                "**analysis**: understanding the problem, researching the codebase, "
                "evaluating options, and surfacing decisions for the human.",
                "",
                "## Output Format\n",
                "Create an analysis document following the template below. The "
                "fenced block is the **template literal** — copy it as-is and fill "
                "in the bracketed placeholders. The unfenced sections that follow "
                "(`## How to Populate Open Questions`, `## Complexity Assessment`) "
                "are **meta-guidance** — do **not** transcribe them into your "
                "analysis document.\n",
                "````markdown",
                "# Analysis: [Issue Title]\n",
                "> Issue: #[number] | Phase: refine\n",
                "## Problem Statement\n",
                "[Describe the problem or feature request. "
                "What is the current state? What is the desired outcome?]\n",
                "## Current Behavior\n",
                "[Describe how the system currently works in the relevant area. "
                "Include code references where helpful.]\n",
                "## Constraints\n",
                "- [Technical constraints (compatibility, performance, security)]",
                "- [Business constraints (timeline, scope)]",
                "- [Dependencies on other systems or features]\n",
                "## Options Considered\n",
                "### Option A: [Name]\n",
                "**Approach**: [Brief description]\n",
                "**Pros**:",
                "- [Advantage 1]\n",
                "**Cons**:",
                "- [Disadvantage 1]\n",
                "### Option B: [Name]\n",
                "**Approach**: [Brief description]\n",
                "**Pros**:",
                "- [Advantage 1]\n",
                "**Cons**:",
                "- [Disadvantage 1]\n",
                "## Recommended Approach\n",
                "[Which option is recommended and why. Reference the option above.]\n",
                "## Open Questions\n",
                "[Register every open question by following the protocol in "
                "`## How to Populate Open Questions` below the template, then paste "
                "the markdown output of each registration command into this section. "
                "Do **not** copy the protocol instructions themselves into this "
                "document.]\n",
                "---\n",
                "*Authored-by: egg*",
                "````\n",
                "",
                "## How to Populate Open Questions\n",
                "These instructions tell you how to handle the `## Open Questions` "
                "section of the template above. They are **meta-guidance**, not "
                "template content — do **not** transcribe this section into the "
                "analysis document you write.\n",
                "**Every open question MUST be registered as a contract decision or "
                "feedback item using `egg-contract`.** Do not just write questions "
                "as prose — they will not be seen by the human unless registered.\n",
                "**Skip already-resolved questions.** If the Task Description above "
                "includes an `## Additional Context` section, treat anything addressed "
                "there as already decided by the operator (those came from a pre-refine "
                "HITL round). Do NOT call `egg-contract add-decision` or "
                "`egg-contract add-feedback` for questions whose answers are already "
                "captured in `## Additional Context` — re-registering them wastes turns "
                "and produces no-op decisions. Read that section first; if it settles "
                "anything, list those items in a `### Resolved in Pre-Refine` "
                "subsection at the top of `## Open Questions` (one bullet per resolved "
                "item, citing the answer). Only register questions that go beyond what "
                "`## Additional Context` covers. This skip rule is NARROW: it covers "
                "only answers THIS pipeline's operator recorded in "
                "`## Additional Context`. It never covers decisions the task "
                "description names as operator-owned, and never answers inherited "
                "from a prior or cancelled run's seeded context — register those "
                "(see the next rule).\n",
                "**Task-named decisions are non-optional (#3462).** If the task "
                "description or contract names specific decisions as the operator's "
                "to make — or contains any directive to surface decisions as HITL "
                "questions — you MUST register each one via "
                "`egg-contract add-decision`, even when you believe prior context "
                "already resolves it, or that it is non-blocking or deferred. "
                "Belief about resolution is a *recommended disposition*, not a "
                "reason to skip registration: make your recommended answer the "
                "first option (suffix its label with `(recommended)`) and cite the "
                "resolving context in that option's description, so the operator "
                "can confirm in one click while retaining the authority to choose "
                "differently. Documenting a decision in draft prose is a "
                "supplement to registration, never a substitute — unregistered "
                "decisions never reach the operator's decision surface.\n",
                "Surface uncertainties, ambiguities, and assumptions **that genuinely "
                "need a human to answer**. Filter ruthlessly: a good open question is "
                "one the operator must answer because the answer changes what we're "
                "building. A bad open question is one the planner phase will decide on "
                "its own once it sees the analysis — those waste the operator's "
                "attention and pre-anchor the planner. Err toward registering questions "
                "about *what the problem actually is* and *what's in or out of scope* "
                "rather than *how to build it*.\n",
                "**Out of scope for refine open questions** — do NOT register decisions "
                "about:\n"
                "- **Work decomposition / slice-DAG shape / PR packaging** — "
                "**Slice / PR packaging is NOT a refine-phase decision.** The "
                "plan phase owns slice-DAG construction (see "
                "`docs/architecture/slice-dag.md`) and the operator approves the "
                "proposed slice shape at the plan HITL gate. Do not register "
                "`add-decision` items asking how the work should be sliced, how "
                "many PRs to ship, or which parts should run in parallel. If "
                "the task obviously spans multiple parts, name them in Problem "
                "Statement or Constraints — the planner will propose a shape "
                "from the analysis it reads.\n"
                "- **Implementation strategy choices** that the planner can decide "
                'from Problem Statement + Constraints (e.g. "which migration '
                'approach", "which fallback design", "which detector shape"). '
                "Surface these as Options Considered / Recommended Approach in the "
                "analysis prose, not as `add-decision` items.\n"
                "- **API / schema details** the planner phase will work out once it "
                "starts designing. If the operator must constrain the API shape, "
                "frame it as a *constraint* in `## Constraints`, not an open question.\n"
                "Register questions when the answer is a fact only the human knows "
                "(product intent, scope boundaries, external commitments, "
                "user-visible behavior) — not when the answer is a design call the "
                "planner will make.\n",
                "**Multiple-choice questions** — RUN this command for each question "
                "where the human must pick from discrete options:",
                "```bash",
                'egg-contract add-decision --question "Which approach should we use?" \\',
                '  --options "Option A" "Option B" "Option C" --format markdown',
                "```",
                "Copy the markdown output into your analysis. The human can check "
                'a checkbox to select an option. An "Other (explain in reply)" '
                "option is auto-appended.\n",
                "**Open-ended questions** — EXECUTE this command for free-form "
                "questions where you need the human to provide text answers:",
                "```bash",
                "egg-contract add-feedback \\",
                '  --question "What is the expected request volume?" \\',
                '  --question "Are there any constraints on third-party dependencies?" \\',
                "  --format markdown",
                "```",
                "This creates a dedicated comment for the human to fill in answers. "
                'They edit the comment to add their responses and check "Submit '
                'feedback" when done. The pipeline will resume with the feedback '
                "available in the contract.\n",
                "**Advisory seam-listing is fine** — if the task obviously spans "
                "independently-implementable parts, you MAY name them in Problem "
                'Statement or Constraints (e.g. "the change touches the gateway, '
                'the orchestrator, and the sandbox") so the planner has the seam '
                "information. Make it **explicitly advisory**: the planner is free "
                "to slice differently if it sees a better seam. Do not pre-number "
                "parts as `slice-1 / slice-2`, do not draw a DAG, and do not pick "
                "a 1-PR-vs-3-PR shape — those choices belong to the planner.\n",
                "**DO NOT:**",
                "- Write questions as plain markdown text without running "
                "`egg-contract add-decision` or `egg-contract add-feedback`",
                "- Use custom HTML comment markers like "
                "`<!-- DECISION: ... -->` instead of the contract CLI",
                "- Skip registration because you think the questions are minor — "
                "register every question",
                "- Skip registration because you believe a decision is already "
                "resolved, non-blocking, or deferred — register it with your "
                "recommended disposition instead (#3462)",
                "- Attest `no_decisions_rationale` when the task names decisions "
                "to surface — the attestation is presented to the operator as its "
                "own confirmable decision, and a rejected 'none' sends the phase "
                "back for a re-run (#3462)",
                "- Transcribe this `## How to Populate Open Questions` section "
                "into your analysis document — it is meta-guidance, not template "
                "content\n",
                "**Attest your decision ledger when proposing (#3390).** Your "
                "consensus propose is REJECTED unless its attestation carries "
                "the ledger: `--decisions-registered cq-1 cq-2 ...` (every id "
                "you registered this phase) or "
                '`--no-decisions-rationale "<why>"` when you deliberately '
                "registered none. Attested ids must exist on the contract for "
                "this phase, and the draft must cite each one — the "
                "`--format markdown` output you copied above embeds the id, so "
                "the registration flow satisfies the citation automatically. "
                "If your only open questions went into an `add-feedback` "
                "request (no `cq-N` decisions), attest the rationale form and "
                "name the feedback request in it. This is what lets the "
                "operator trust that an empty gate means *deliberately no "
                "decisions*, not *forgot to register*. The explicit-none form "
                "is not a shortcut (#3462): the orchestrator surfaces it to "
                "the operator as its own confirmable decision before the "
                "phase gate, and it is only valid when the phase genuinely "
                "raises no meaningful decision — never when the task names "
                "decisions to surface, and never as a substitute for "
                "registering a decision you believe is already resolved.\n",
                "",
            ]
        )
        lines.extend(
            [
                "## Complexity Assessment\n",
                "After completing your analysis, assess the task complexity:",
                "- **low**: Single-file change, straightforward bug fix, small config update, typo fix",
                "- **medium**: Multi-file change with clear scope, feature addition with known patterns",
                "- **high**: Architectural change, new subsystem, cross-cutting concern, "
                "many independent phases that could be parallelized",
                "",
            ]
        )
        lines.extend(_pkg._EXPLORATION_SUBAGENT_GUIDANCE)
        lines.extend(
            [
                f"Write your analysis to `{analysis_path}`.",
                "Commit and push the draft when done.\n",
                "**IMPORTANT**: Do NOT post your analysis directly to the issue. "
                "The pipeline will have an internal reviewer check your analysis. "
                "If revisions are needed, you'll be re-invoked with feedback. "
                "Only after internal review passes will the analysis be posted "
                "for human approval.",
                "",
            ]
        )

    elif phase == "plan":
        lines.extend(
            [
                "Create a detailed implementation plan, decomposing the work into "
                "slices per the slice-DAG guidance at the end of this section. The "
                "implement-phase pipeline ships each slice as its own stacked PR. "
                "**Slice shape is your call.** A single-slice plan is fine when the "
                "work is cohesive; pick a multi-slice shape when the work has clean "
                "seams that ship independently. If the refine analysis sketched a "
                "decomposition (e.g. naming the components touched), treat it as "
                "**advisory context** — you are free to slice differently if a "
                "better seam exists. The only thing that binds your slice shape is "
                "an explicit slice-DAG HITL decision recorded by the operator on "
                "the contract; if you believe such a decision is wrong, raise it as "
                "an open question in your plan rather than silently overriding.",
                "",
                "Steps:",
                "1. Review any prior analysis",
                "2. Break down the work into phases with discrete tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Identify test strategy — what automated tests cover the changes, "
                "and what manual verification is needed",
                "5. Identify any manual pre-merge or post-merge steps "
                "(migrations, config changes, deployments)",
                "6. Consider rollback and risks",
                "",
                "## Output Format",
                "",
                "Write a markdown plan with a **yaml-tasks** structured appendix at the end.",
                "The prose section explains the approach; the appendix is machine-parsed.",
                "",
                *_pkg._PR_DESCRIPTION_GUIDANCE,
                "",
                "End your document with a fenced YAML block like this:",
                "",
                "````",
                "```yaml",
                "# yaml-tasks",
                "pr:",
                '  title: "Short imperative summary (≤70 chars)"',
                "  description: |",
                *_pkg._PR_DESCRIPTION_YAML_EXAMPLE,
                "  test_plan: |",
                "    - Automated: describe which tests cover the changes",
                "    - Manual: specific steps a reviewer should take to verify",
                "  manual_steps: |",
                "    Pre-merge: any required steps before merging",
                "    Post-merge: any required steps after merging",
                "slices:",
                "  - id: 1",
                "    name: |-",
                "      Slice Name",
                "    goal: |-",
                "      What this slice achieves, written for a reviewer of the",
                "      target repo. This text is rendered verbatim as the lead",
                "      paragraph of the slice's PR body (#3115), so keep it 1-3",
                "      plain-language sentences with no plan-internal",
                "      cross-references (reviewer codes, section numbers, draft",
                "      version markers).",
                "    tasks:",
                "      - id: TASK-1-1",
                "        description: |-",
                "          What to do — safe to include `code: type` snippets,",
                "          URLs, and other punctuation inside a block scalar.",
                "        acceptance: |-",
                "          How to verify it is done",
                "        files:",
                "          - path/to/file.py",
                "```",
                "````",
                "",
                *_pkg._YAML_TASKS_SAFETY_GUIDANCE,
                "",
                "Do NOT use a `pr_plan` key — slice packaging is owned by the "
                "slice-DAG section below, not by an ad-hoc PR list.",
                "",
                "The `test_plan` field is **required** — describe both automated test "
                "coverage and any manual verification steps. The `manual_steps` field "
                "should list any pre-merge or post-merge actions required by the reviewer "
                "or deployer; use an empty string if none.",
                "",
                # ----------------------------------------------------
                # #2137 — slice-DAG planner guidance (mirrors the
                # concurrent task_planner block; keep the two paths
                # aligned so the slice-shape rules behave the same way
                # regardless of which planner runs).
                # ----------------------------------------------------
                "## Slice-DAG guidance (#2137)",
                "",
                "The implement-phase pipeline ships each plan **slice** (formerly "
                "**phase**) as its own stacked PR. The plan you emit drives that "
                "DAG; the rules below are mandatory.",
                "",
                "**Yaml key swap**: prefer the canonical ``slices:`` key in your "
                "``# yaml-tasks`` block (the parser also accepts ``phases:`` for "
                "backward compatibility). New plans should use ``slices:``.",
                "",
                "**Slice-sizing NACK (hard, judgment-based — #2809)**: the plan "
                "reviewer will hard-NACK an oversized slice. Use judgment when "
                "shaping — no fixed LOC budget, but avoid bundling more than ~3 "
                "distinct file-categories in one slice, avoid combining "
                "deletion-heavy work with new-API-introduction work, avoid "
                "slices that would require >3–4 commit-propose-revise cycles, "
                "and avoid bundling independent task groups with no internal "
                "dependency. Subdivide along those seams up front rather than "
                "earning a NACK.",
                "",
                "**Forest constraint (HARD)**: every slice must have at most ONE "
                "DAG parent — the implement-phase pipeline ships every slice as a "
                "stacked PR with exactly one base branch. Multi-parent slices "
                "break the stacking invariant and are rejected at plan ingestion.",
                "",
                "**Auto-serialization rule for would-be multi-parent slices**: "
                "when a slice would naturally have >1 parents, serialise the "
                "upstream slices into a linear chain and record the chosen "
                "ordering on the downstream slice's ``serialized_chain_order`` "
                "field. The list names the upstream slice IDs in their chosen "
                "serialization order.",
                "",
                "**File-overlap rule (HARD, enforced at plan ingestion — "
                "#3046)**: two slices that touch the SAME file must be ordered "
                "on one dependency chain — one a transitive ``dependencies`` "
                "ancestor of the other — never left as parallel roots or "
                "siblings. The implement phase cuts each slice's branch off "
                "its dependency parent, so an unordered overlapping pair forks "
                "independently off the shared base and its edits to the shared "
                "file collide at integration (a guaranteed modify/delete "
                "conflict). Deletion/retirement slices are the classic trap: a "
                "slice that removes a file must depend on every slice that "
                "modifies it. Slices with disjoint file sets stay parallel.",
                "",
                "**Test co-location rule (HARD — #3411)**: a slice that "
                "removes, renames, or rewrites code carries the matching "
                "test updates (skip-guards, deletions, rewrites) in the SAME "
                "slice — never a later one — with the test files listed in "
                "that slice's task ``files:``. Each cumulative slice tip "
                "must be independently green: the per-slice green gate "
                "(#3398) runs the repo's checks at the slice tip before the "
                "PR opens and blocks while any check is red, so deferring "
                "test obsolescence to a later slice guarantees a blocked "
                "slice. Discover the tests that statically reach the "
                "changed files with the changeset-aware selector where the "
                "repo ships it (this repo: ``python3 "
                "scripts/select_tests/__main__.py --impacted-tests "
                "<file>...``; exit 2 = closure unavailable — fall back to "
                "grepping the removed symbols in the test trees).",
                "",
                "Worked example: if ``slice-3`` would naturally have "
                "parents ``[slice-1, slice-2]``, instead emit:",
                "",
                "```yaml",
                "  - id: 1",
                "    name: |-",
                "      Foundations",
                "    # ... (root)",
                "  - id: 2",
                "    name: |-",
                "      Middle",
                "    dependencies:",
                "      - slice-1",
                "  - id: 3",
                "    name: |-",
                "      Downstream",
                "    dependencies:",
                "      - slice-2  # serialised — slice-2 is the only DAG parent",
                "    serialized_chain_order:",
                "      - slice-1",
                "      - slice-2  # records that you deliberately picked",
                "                 # slice-1 → slice-2 → slice-3",
                "```",
                "",
                "Your judgement is the source of truth. The fallback heuristic "
                "when you have no preference is: cluster would-be parents by "
                "``files_affected`` Jaccard overlap (>0.3), then order by "
                "descending downstream fan-out.",
                "",
                f"Write your plan to `{plan_path}`.",
                "Commit and push the draft when done.",
                "",
            ]
        )

    elif phase == "implement":
        # Embed plan or analysis text directly on first cycle
        # (avoids file-I/O turns inside the sandbox).
        draft_embedded = False
        if repo_path and review_cycle == 0:
            draft_text = _pkg._read_phase_draft(
                _pkg.Path(repo_path),
                "plan",
                issue_number=issue_number,
                pipeline_id=pipeline_id,
                branch=branch,
            )
            if draft_text:
                lines.append("## Plan\n")
                lines.append(f"```markdown\n{draft_text}\n```\n")
                draft_embedded = True

            # Embed contract task checklist on first cycle
            contract_tasks = _pkg._render_contract_tasks(
                repo_path, pipeline_id, pipeline_mode, issue_number
            )
            if contract_tasks:
                lines.append(contract_tasks)
                lines.append("")

        if review_cycle == 0:
            # Build numbered step list; only include the "review" step
            # when the draft wasn't already embedded above.
            lines.append("Implement the changes described in the task and plan:")
            lines.append("")

            steps: list[str] = []
            if not draft_embedded:
                steps.append("Review the plan (check `.egg-state/drafts/`)")
            steps.extend(
                [
                    "Implement the required changes — when working with third-party "
                    "libraries or APIs, use WebSearch and WebFetch (when available) to "
                    "look up current documentation, usage examples, and best practices",
                    "After completing each plan phase or task group, commit and push "
                    "immediately — do not batch all work into a final commit. Mark "
                    "tasks done: `egg-contract complete-task --task <id> --commit <sha>`",
                    "Run tests to verify correctness, then commit any fixes",
                ]
            )
            for i, step in enumerate(steps, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

            lines.append("## Parallel Execution with Subagents\n")
            lines.append(
                "You have access to Claude Code's **Agent tool** for spawning subagents. "
                "Use it to parallelize independent work:\n"
            )
            lines.append(
                "- If the plan has multiple independent phases or task groups that don't touch "
                "overlapping files, implement them in parallel by launching one subagent per "
                "phase/group."
            )
            lines.append(
                "- Each subagent gets a clear, self-contained prompt describing its scope "
                "(files to modify, tasks to complete, acceptance criteria)."
            )
            lines.append(
                "- Subagents share your working directory and git state. Ensure parallel "
                "subagents work on **non-overlapping files** to avoid conflicts."
            )
            lines.append(
                "- Subagents should only edit files — do NOT stage or commit from subagents. "
                "After each group of parallel subagents completes, **immediately** commit and "
                "push their combined changes before launching the next group."
            )
            lines.append(
                "- After subagents complete, verify the combined changes compile, pass tests, "
                "and integrate correctly. Do NOT defer all commits to the end."
            )
            lines.append(
                "- For small or sequential tasks, just implement directly — don't over-parallelize."
            )
            lines.append("")
            lines.extend(_pkg._EXPLORATION_SUBAGENT_GUIDANCE)
        else:
            # Revision cycle: slim delta-focused prompt.
            # Guard: if review_feedback is unexpectedly missing, fall
            # back to including the task description so the coder isn't
            # left with a nearly empty prompt.
            if not review_feedback:
                if prompt:
                    lines.append("## Task Description\n")
                    lines.append(prompt)
                    lines.append("")

            lines.append("## Revision Instructions\n")
            if review_feedback:
                has_tester_findings = _pkg.TESTER_FINDINGS_HEADER in review_feedback
                if has_tester_findings:
                    lines.extend(
                        [
                            "The reviewer and tester found issues with your implementation. "
                            "Focus on addressing the specific feedback above.\n",
                            "1. Review the feedback in the **Prior Review Feedback** section above",
                            "2. Check `git diff` to understand the current state of changes",
                            f"3. Check `.egg-state/agent-outputs/"
                            f"{_pkg._pipeline_identifier(issue_number, pipeline_id)}"
                            f"-tester-output.json` for test failures and gaps",
                            "4. Fix the specific issues raised",
                            "5. Run tests to verify your fixes",
                            "6. Commit with descriptive messages",
                            "",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "The reviewer found issues with your implementation. "
                            "Focus on addressing the specific feedback above.\n",
                            "1. Review the feedback in the **Prior Review Feedback** section above",
                            "2. Check `git diff` to understand the current state of changes",
                            "3. Fix the specific issues raised by the reviewer",
                            "4. Run tests to verify your fixes",
                            "5. Commit with descriptive messages",
                            "",
                        ]
                    )
            else:
                lines.extend(
                    [
                        "A revision was requested but no specific feedback was provided. "
                        "Review the task description above and check `git diff` for the current state.\n",
                        "1. Review the task description above and check `git diff`",
                        "2. Verify the implementation meets the requirements",
                        "3. Run tests to verify correctness",
                        "4. Commit with descriptive messages",
                        "",
                    ]
                )

        # Contract CLI instructions for both local and issue mode
        lines.extend(
            [
                "Use the contract CLI to track progress incrementally — update after "
                "each commit, not in a batch at the end:",
                "- `egg-contract show` — View current contract state",
                "- `egg-contract complete-task --task <id> --commit <sha>` — Mark task done and link commit",
                "- `egg-contract complete-phase --phase <id> --commit <sha>` — Mark phase done and link commit",
                "- `egg-contract add-commit --task <id> --commit <sha>` — Link commit to task without marking done",
                "",
            ]
        )

    else:
        lines.append(f"Execute the {phase} phase.\n")

    # --- Phase restrictions ---
    lines.append("## Phase Restrictions\n")
    if issue_number is None and phase in ("refine", "plan"):
        lines.extend(
            [
                "In this phase:",
                "- You CAN push state files to git (contracts, drafts, checkpoints)",
                "- You CAN create HITL decisions (egg-contract add-decision)",
                "- You CAN create feedback requests (egg-contract add-feedback)",
                "- You CANNOT push code changes",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post comments to the GitHub issue (gh issue comment) — write reviews to `.egg-state/reviews/` instead",
                "- You CANNOT edit the GitHub issue (gh issue edit)",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    elif issue_number is None and phase == "implement":
        lines.extend(
            [
                "In this phase:",
                "- You CAN push code changes to git",
                "- You CANNOT push .egg-state/ files (except checkpoints)",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post comments to the GitHub issue (gh issue comment)",
                "- You CANNOT edit the GitHub issue (gh issue edit)",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    else:
        if phase in ("refine", "plan"):
            lines.extend(
                [
                    "- You CAN write drafts to `.egg-state/drafts/`",
                    "- You CAN push draft files (git push)",
                    "- You CAN create HITL decisions (egg-contract add-decision)",
                    "- You CAN create feedback requests (egg-contract add-feedback)",
                    "- You CANNOT post comments to the GitHub issue (gh issue comment) — write reviews to `.egg-state/reviews/` instead",
                    "- You CANNOT edit the GitHub issue (gh issue edit)",
                    "- You CANNOT create PRs (gh pr create)",
                    "",
                ]
            )
        elif phase == "implement":
            lines.extend(
                [
                    "- You CAN push code (git push)",
                    "- You CAN link commits to tasks (egg-contract add-commit)",
                    "- You CANNOT create PRs (the pipeline manages the PR)",
                    "- You CANNOT post comments to the GitHub issue (gh issue comment)",
                    "- You CANNOT edit the GitHub issue (gh issue edit)",
                    "",
                ]
            )
    # --- Completion ---
    lines.append("## Phase Completion\n")
    if phase in ("refine", "plan"):
        lines.append(
            "When your draft is complete, commit and push it. "
            "The pipeline will have an internal reviewer evaluate your work. "
            "If revisions are needed, you'll be re-invoked with feedback. "
            "Only after internal review passes will the output be posted "
            "for human approval."
        )
    else:
        lines.append(
            "When you have completed your work for this phase, "
            "ensure everything is committed and exit successfully."
        )

    return "\n".join(lines)


def _contract_enforcer_role_names() -> frozenset[str]:
    """Roles whose ACK/CONFIRM is gated on contract-task completeness (#3114).

    Lazy wrapper so the preamble builder keys its enforcer-specific
    instructions off the same capability set the orchestrator's signal
    gate enforces (``egg_contracts.agent_roles.CONTRACT_ENFORCER_ROLES``)
    — prose and enforcement stay in lockstep.
    """
    from egg_contracts.agent_roles import CONTRACT_ENFORCER_ROLE_NAMES

    return CONTRACT_ENFORCER_ROLE_NAMES


def _build_brc_preamble(
    role_value: str,
    phase: str,
    repo: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
) -> str:
    """Build the BRC consensus lifecycle preamble for an agent.

    Returns a formatted string block that can be appended to any agent prompt
    to inject BRC protocol instructions. Used by both the coder/refiner path
    (which delegates to _build_phase_prompt) and the generic multi-agent path.

    Includes:
    - Agent roster showing all active agents and what they produce
    - Role-specific proactive preparation instructions
    - Full BRC lifecycle steps (including the generic no-op propose path,
      #3027, for a producer that finds it has no work in this slice)
    """
    try:
        from review_graph import get_review_graph_for_phase

        graph = get_review_graph_for_phase(phase, repo=repo)
        is_producer = graph.is_producer(role_value)
        is_reviewer = graph.is_reviewer(role_value)
        reviewers = graph.reviewers_for(role_value) if is_producer else []
        producers = graph.producers_for(role_value) if is_reviewer else []
        wake_only_producers = graph.wake_only_producers_for(role_value)
        all_roles = sorted(graph.all_roles())
        graph_available = True
    except Exception:
        is_producer = role_value in (
            "coder",
            "tester",
            "documenter",
            "refiner",
            "architect",
            "task_planner",
            "risk_analyst",
            "simplifier",
        )
        is_reviewer = role_value in (
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_contract",
            "tester",
            "reviewer_refine",
            "reviewer_agent_design",
            # first_principles_reviewer is a genuine refine-phase reviewer: it
            # casts a real ACK verdict on the refiner (CRITICAL edge), so the
            # degraded fallback keeps its Reviewer Lifecycle block. It never
            # NACKs (redirects go to the operator as HITL decisions), but it
            # DOES vote, so — unlike the simplifier — it is a real verdict and
            # ``casts_real_verdicts`` (raw ``is_reviewer`` here) stays True.
            "first_principles_reviewer",
            "reviewer_plan",
            # risk_analyst is a genuine dual-role reviewer in the plan graph
            # (CRITICAL reviewer of architect + task_planner, #2809) as well as
            # a producer of the risk register. Listed here so the degraded
            # fallback path keeps its Reviewer Lifecycle / "As a reviewer"
            # block instead of stripping it to producer-only — mirroring the
            # live plan graph. Unlike the simplifier its edges are real
            # verdicts, so ``casts_real_verdicts`` (raw ``is_reviewer`` in the
            # degraded path) correctly stays True for it.
            "risk_analyst",
            # simplifier retains a wake_only advisory edge over the upstream
            # refine/plan producer, so the graph reports it as a reviewer —
            # but it casts no verdict (#3381) and is rendered PRODUCER-ONLY
            # below (the wake_only edge is excluded from the real-reviewer
            # determination). Listed here so the degraded fallback path still
            # recognizes it; producer-only rendering is handled uniformly.
            "simplifier",
        )
        reviewers = []
        producers = []
        wake_only_producers = set()
        all_roles = []
        graph_available = False

    lines: list[str] = [
        "\n\n## CRITICAL: BRC Consensus Protocol\n",
        "You are running in CONCURRENT mode with the Broadcast-Review-Converge "
        "(BRC) protocol. Your job is NOT just your task — it is the **full "
        "BRC lifecycle**.\n",
    ]

    is_dual_role = is_producer and is_reviewer

    # A role whose only reviewed producers are reached via wake_only edges
    # (the de-roled simplifier, #3381) casts no verdict on anyone, so it is a
    # PRODUCER in every behavioural sense — render it as one. We keep the
    # graph-level ``is_dual_role`` flag intact for the banner dispatch below;
    # only the rendered role-type label and the "assigned producers" line
    # exclude wake_only producers, so the preamble does not contradict the
    # producer-only execution banner the simplifier receives.
    real_producers = [p for p in producers if p not in wake_only_producers]
    if graph_available:
        casts_real_verdicts = bool(real_producers)
    else:
        # Degraded path: the graph load failed, so ``producers == []`` for every
        # role and we cannot distinguish wake_only edges from real ones. Fall
        # back to raw ``is_reviewer`` so we don't silently strip the Reviewer
        # Lifecycle / "As a reviewer" coordination block from a *genuine*
        # reviewer (reviewer_code/refine/plan) when the graph is unavailable —
        # pre-#3381 this path gated those blocks on raw ``is_reviewer``. The
        # simplifier — the only wake_only role — stays producer-only: it is
        # excluded here, and is independently rendered producer-only by the
        # ``is_dual_role and role_value == "simplifier"`` banner dispatch, which
        # still fires in the fallback.
        casts_real_verdicts = is_reviewer and role_value != "simplifier"

    if is_producer and casts_real_verdicts:
        role_type_desc = "PRODUCER and REVIEWER (dual role)"
    elif is_producer:
        role_type_desc = "PRODUCER"
    elif casts_real_verdicts:
        role_type_desc = "REVIEWER"
    else:
        role_type_desc = "PARTICIPANT"

    lines.append(f"Your role type: **{role_type_desc}**")
    if reviewers:
        lines.append(f"Your reviewers: {', '.join(reviewers)}")
    if real_producers:
        lines.append(f"Your assigned producers: {', '.join(real_producers)}")
    lines.append("")

    # Agent roster: show all active agents and what they do
    if all_roles:
        roster = _pkg._build_agent_roster(all_roles, role_value, phase)
        if roster:
            lines.append(roster)

    # Dual-role ordering banner (#2749, updated for coder-owns-tests). A
    # dual-role agent (today: only TESTER in the implement graph) receives
    # both the Producer and Reviewer Lifecycle blocks below. The coder now
    # authors its own tests; the tester's job is to review-and-harden them
    # after the coder proposes. So the tester's producer WORK legitimately
    # depends on the coder's ``CONSENSUS_PROPOSE`` — it orients up-front,
    # exits after ORIENT, and is re-invoked by the event-pump wrapper when
    # the coder proposes, at which point it hardens + proposes + ACK/NACKs
    # in one pass. This does not reintroduce the f4c7d780 / 8b81ed32
    # self-block (where the tester idled on a reviewer wait-loop before
    # proposing its own scaffolded work): the coder proposes independently
    # and does not wait on the tester, so the coder's propose is the
    # trigger, and the tester proposes right after. The tester therefore
    # has TWO reviewer rendezvous points, both surfaced as fresh wrapper
    # invocations under the event-pump model: (a) the coder's first
    # ``CONSENSUS_PROPOSE`` re-invokes the tester so it has something to
    # harden; (b) subsequent re-proposes and peer-producer proposals
    # (after the tester has proposed) likewise re-invoke the tester to
    # handle the Reviewer Lifecycle for those events.
    if is_dual_role and role_value == "simplifier":
        # The simplifier is a PRODUCER ONLY (#3381). It is woken to write the
        # companion by the ordinary producer propose-arm (it self-gates on the
        # upstream draft existing), NOT by its advisory edge over the upstream
        # — that edge is wake_only and casts no verdict, so it is inert in
        # consensus derivation (see review_graph.ReviewEdge.wake_only). It is
        # NOT a reviewer in any behavioural sense: it issues no verdict, casts
        # no ACK/NACK, and never critiques the draft. Treating it as a reviewer
        # is what made the companion come out as a review/critique memo instead
        # of a plain-language summary. So it gets a PRODUCER-ONLY banner here
        # and must NOT inherit the tester's review-and-harden banner below.
        lines.append(
            "### Execution Order (READ FIRST — simplifier)\n\n"
            "You are a **producer only**: your single job is to write a "
            "plain-language, human-focused companion to the upstream "
            "producer's draft. You do **not** review, critique, score, or "
            "vote on that draft — you never issue an ACK or a NACK. (An "
            "internal wake-wire re-invokes you when the upstream proposes so "
            "you know its draft is ready; consensus never waits on a verdict "
            "from you, so there is nothing to respond to.)\n\n"
            "**Execute in this order:**\n\n"
            "1. **ORIENT (FIRST).** Read the contract and orient. Your work "
            "depends on the upstream producer's draft existing, so you begin "
            "writing only once that producer issues `CONSENSUS_PROPOSE` — the "
            "event-pump wrapper re-invokes you carrying that proposal. Do not "
            "race ahead before the draft exists.\n"
            "2. **On the upstream producer's PROPOSE**, the wrapper re-invokes "
            "you with the proposal in your event payload. SYNC the worktree, "
            "read the draft, then write and PROPOSE the human-focused "
            "companion (see Producer role below). That is the whole job — "
            "the companion is a simplified summary written *for humans to "
            "read*, never a review of the draft, a list of constraints the "
            "draft should satisfy, or an ACK/NACK rationale.\n"
        )
    elif is_dual_role:
        lines.append(
            "### Dual-Role Execution Order (READ FIRST — #2749, updated for "
            "coder-owns-tests)\n\n"
            "You are both PRODUCER and REVIEWER (TESTER). **The BRC round "
            "cannot close until every producer (including you) has issued "
            "`mcp__brc__propose` / `egg-orch consensus propose`** — so you "
            "MUST eventually propose, and if you never propose your own "
            "hardening you self-block the round. But your producer WORK "
            "(reviewing and **hardening the coder's tests**) genuinely "
            "depends on the coder's proposed tests existing, so unlike a "
            "normal producer you start that work at the coder's PROPOSE, "
            "not before. This does not deadlock: the coder proposes "
            "independently and does **not** wait on you, so its "
            "`CONSENSUS_PROPOSE` is the trigger that unblocks your work; "
            "the event-pump wrapper re-invokes you carrying that PROPOSE "
            "in your event payload, and you propose right after.\n\n"
            "**Execute the lifecycles in this strict order:**\n\n"
            "1. **Producer ORIENT (step 1) comes FIRST.** Run ORIENT now "
            "to load context. **Your role-specific orientation tells you "
            "whether Producer WORK (step 2) runs immediately or is gated "
            "on an upstream producer's `CONSENSUS_PROPOSE`** — e.g. the "
            "implement-phase tester reviews-and-hardens the coder's tests, "
            "so its WORK begins after the coder proposes (#2936). Do not "
            "race ahead of the role-specific orientation. While you are in "
            "ORIENT, you may *opportunistically* do the Reviewer "
            "Lifecycle's `1. PREPARE` work — read the contract, scan the "
            "upstream producer's commits as they land on the branch — but "
            "do NOT start producing artifacts your role-specific "
            "orientation gates on an upstream PROPOSE. Do NOT block on a "
            "reviewer wait as your scheduling primitive: the event-pump "
            "wrapper invokes you again when the upstream producer's "
            "`CONSENSUS_PROPOSE` arrives, at which point you handle the "
            "review AND (if your WORK was gated on it) start producing.\n"
            "2. **On an upstream producer's PROPOSE**, the wrapper "
            "re-invokes you with the proposal in your event payload. "
            "SYNC the worktree, then do your Producer WORK (read the "
            "coder's tests; add the missing regression + adversarial "
            "cases yourself — you share the test scope with the coder; "
            "run the tests) and **PROPOSE** your hardening. In the same "
            "invocation, issue your reviewer verdict on the coder: ACK "
            "if coverage is sound, or NACK naming the specific failing "
            "test / coverage gap.\n"
            "3. **Subsequent invocations** (re-proposes from any "
            "producer — `CONSENSUS_PROPOSE` version > 1 — and "
            "`CONSENSUS_RE_REVIEW` events) surface as new wrapper "
            "invocations. Each one is a fresh review against the new "
            "delta; the per-event prompt includes the full "
            "`git log {last_reviewed_commit_sha}..HEAD --not "
            "origin/{base_branch} -p` so you can audit the change. "
            "Fall through to Reviewer Lifecycle step 3 (SYNC) → step 4 "
            "(REVIEW) → step 5 (ACK/NACK), then exit. Do NOT skip step "
            "4 (REVIEW) — reading the actual referenced files and "
            "forming independent judgment from them is what keeps "
            "re-reviews from becoming rubber-stamps.\n"
        )

    if is_producer:
        producer_lifecycle: list[str] = ["### Producer Lifecycle"]
        # The no-op propose path (#3027) is only valid in the implement
        # phase. In refine/plan the producer's draft is mandatory and the
        # orchestrator rejects no-op explicitly — so don't even surface the
        # affordance to refine/plan producers (architect, refiner,
        # task_planner, risk_analyst), keeping prose and enforcement in
        # lockstep (review feedback on #3029).
        propose_line = (
            "3. **PROPOSE**: When done, run: "
            '`egg-orch consensus propose --summary "..." --artifacts "file1" "file2" '
            '--files-changed "f1.py" "f2.py" --tests-run "test_a" "test_b" '
            '--tasks "task-1-1" "task-1-2" --commit-sha $(git rev-parse HEAD)`. '
            "The `--summary` must be ≥50 chars of substantive content describing what was "
            "built, what was tested, and which contract tasks it satisfies. "
            "Boilerplate like 'looks good' or 'approved' will be rejected."
        )
        if phase in ("refine", "plan") and role_value in (
            # Keep in lockstep with ``_DECISION_ATTESTING_ROLES`` in
            # ``routes/signals/_validation.py`` — the enforcement side of
            # this prose (#3390).
            "refiner",
            "task_planner",
            "architect",
            "risk_analyst",
        ):
            propose_line += (
                "\n\n"
                "   **Attest your decision ledger (#3390 — MANDATORY).** The "
                "orchestrator REJECTS your propose unless its attestation "
                "carries your HITL decision ledger. Pass "
                "`--decisions-registered cq-1 cq-2 ...` listing every decision "
                "you registered this phase (via `egg-contract add-decision` / "
                "`mcp__sdlc__register_open_question`), or "
                '`--no-decisions-rationale "<why>"` when the phase '
                "deliberately raises none — an explicit empty ledger, never an "
                "omission. (Via MCP: the `attestation` arg of "
                "`mcp__brc__propose`, fields `decisions_registered` / "
                "`no_decisions_rationale`.) Attested ids are cross-checked "
                "against the contract, and your draft must cite each attested "
                "`cq-N` (copying the `--format markdown` output into the "
                "draft satisfies this). A decision your draft commits to "
                "without a registered `cq-N` is a reviewer NACK — register "
                "it or remove the unilateral commitment. The rationale form "
                "is not a shortcut (#3462): the operator is asked to confirm "
                "it as its own decision before the phase gate, and a rejected "
                "'none' re-runs the phase. If the task names decisions to "
                "surface — or you believe a decision is already resolved by "
                "prior context — register it with your recommended answer as "
                "the first option instead of attesting none."
            )
        if phase == "implement":
            propose_line += (
                "\n\n"
                "   **Mark your contract tasks complete (#3114).** Record each "
                "delivered task with `mcp__task__complete` (link the commit) — "
                "the contract reviewer's ACK is gated on your rows being "
                "`complete`, so finished-but-unrecorded work blocks the slice. "
                "A task waiting on a peer's work: note it in your proposal and "
                "deliver after the dependency lands; the gate holds the slice "
                "open until then."
            )
            propose_line += (
                "\n\n"
                "   **No work for you in this slice? Submit a no-op propose (#3027).** "
                "If after ORIENT you find your role has no assigned task here AND "
                "nothing to contribute (e.g. a documenter on a code-only slice, a "
                "tester on a doc-only slice, your domain is not impacted by the "
                "diff), do NOT skip silently and do NOT invent busywork — run "
                "`egg-orch consensus propose --no-changes-needed --no-changes-reason "
                '"<why you have no work here>"` (no artifacts or commit-sha needed). '
                "This counts as proposing, so consensus is not blocked waiting on "
                "you; reviewers accept it as a non-blocking no-op (they will not "
                "NACK it). Then CONFIRM (step 5) as normal once peers have proposed. "
                "Reach for a real propose instead the moment you do find work "
                "(e.g. the coder's diff turns out to need docs). Rejected while "
                "you still own incomplete contract tasks here (#3114)."
            )
        producer_lifecycle.extend(
            [
                "1. **ORIENT**: Before starting work, "
                + _pkg._build_producer_orientation(
                    role_value,
                    phase,
                    reviewers,
                    branch=branch,
                ),
                "2. **WORK**: Complete your assigned task (see Your Task below).",
                propose_line,
                "4. **RESPOND TO REVIEWS**: When a reviewer NACKs your "
                "proposal you will be re-invoked to address it. Read every "
                "NACK in the event payload, fix all named blockers, and "
                "re-propose with `--changed-artifacts`. **Aggregation is "
                "enforced by the orchestrator (#2142):** when two or more "
                "distinct reviewers have NACKed the current version, the "
                "re-propose call returns HTTP 409 with the full set "
                "(reviewer, reason, artifact_refs) inline in `details`; "
                "address every NACK then retry. A single-reviewer NACK "
                "does not trigger the barrier — re-propose proceeds "
                "normally.\n\n"
                "   **A NACK naming new findings on your re-propose is "
                "legitimate adversarial review, not goalpost-moving.** "
                "Reviewers re-review v2+ as a fresh delta; \"that's not "
                "what you NACK'd last time\" is not a valid objection. "
                "**You can and should push back on a NACK on its merits** — "
                "if the reviewer misread the code or the concern does not "
                "apply, contest it via a directed message with evidence "
                "(file:line, test, doc reference). What is *not* productive "
                "is contesting a NACK you know is correct — re-reviews are "
                "cheap by design, so when the finding is real, fix it and "
                "re-propose.",
                "5. **CONFIRM**: When all reviewers ACK, run "
                "`egg-orch consensus confirmed` to mark your role's "
                "consensus.",
                "6. **HANDLE RE-REVIEW**: When you are re-invoked with a "
                "`CONSENSUS_RE_REVIEW` event"
                + (
                    " (or a `CONSENSUS_PROPOSE` for a re-propose — "
                    "version > 1, after you NACKed a prior version; "
                    "dual-role agents handle both — see Reviewer "
                    "Lifecycle step 7 for the adversarial re-review "
                    "framing)"
                    if is_dual_role and casts_real_verdicts
                    else ""
                )
                + ", act on it — failure to respond stalls the pipeline. "
                + (
                    "If you are a reviewer of the re-proposing producer, "
                    "re-review and ACK/NACK the new proposal (dual-role "
                    "agents: see Reviewer Lifecycle step 7 below for the "
                    "adversarial re-review framing that applies to this "
                    "case). Otherwise, re-confirm via "
                    "`egg-orch consensus confirmed`."
                    if casts_real_verdicts
                    else "Re-confirm via `egg-orch consensus confirmed`."
                ),
                "7. **RESOLVE OBLIGATIONS YOU SATISFY (#2338)**: If you "
                "land a commit that satisfies a *different* producer's "
                "conditional-ACK obligation in-cycle — typical pattern: "
                "the coder is gateway-blocked from a path under `tests/`, "
                "you (as tester) cherry-pick the satisfying commit onto "
                "the branch — call `mcp__brc__resolve_obligation "
                'reviewer_role="<reviewer>" producer_role="<other_producer>" '
                "commit_sha=$(git rev-parse HEAD)` after pushing. The "
                "matrix keeps the obligation text for audit but stops "
                "surfacing it on the PR body and HITL gate. Skip this "
                "for obligations that genuinely require a human at "
                "merge time (deploys, cross-repo flips) — those should "
                "remain visible to the merger. **Resolve before "
                "`complete_phase`**: once the HITL gate has fired and "
                "written the obligation to `contract.pr.deferred_actions`, "
                "calling `resolve_obligation` afterwards does *not* "
                "retroactively unpersist the entry — the obligation will "
                "still appear in the PR body until the next pipeline run. "
                "Resolve early. Producers cannot self-resolve their own "
                "obligations (the orchestrator rejects "
                "`resolver_role == producer_role`), since that would "
                "single-handedly bypass the reviewer's veto.\n",
            ]
        )
        lines.extend(producer_lifecycle)

    # Gate the Reviewer Lifecycle on ``casts_real_verdicts``, not raw
    # ``is_reviewer`` (#3381). A role whose only reviewed producers are
    # reached via ``wake_only`` edges (the de-roled simplifier) issues no
    # ACK/NACK and must NOT receive the reviewer playbook — REVIEW, ACK/NACK,
    # CONFIRM, adversarial re-review — which would directly contradict its
    # producer-only execution banner. This mirrors the producer-only invariant
    # already asserted for the coder (``test_producer_only_no_sync_step``): a
    # producer-only role gets no ``### Reviewer Lifecycle`` at all. A pure
    # reviewer (``reviewer_refine``) and the dual-role tester both cast real
    # verdicts, so they keep it.
    if is_reviewer and casts_real_verdicts:
        lines.extend(
            [
                "### Reviewer Lifecycle",
                "1. **PREPARE** (while waiting): "
                + _pkg._build_reviewer_preparation(
                    role_value,
                    phase,
                    branch=branch,
                    base_branch=base_branch,
                ),
                "2. **INVOKED PER EVENT**: The orchestrator's event-pump "
                "wrapper invokes you one-shot per actionable event. When a "
                "`CONSENSUS_PROPOSE` arrives for an assigned producer, "
                "you are spawned with the proposal in your event payload. "
                "Do your preparation work from step 1 on the first "
                "invocation; subsequent invocations land you directly at "
                "step 3 (SYNC) with the proposal already in context."
                + (
                    "\n\n   **Dual-role agents (you)** — per the "
                    "*Dual-Role Execution Order* banner above (updated "
                    "for coder-owns-tests): your first invocation does "
                    "ORIENT/PREPARE only. On the coder's "
                    "`CONSENSUS_PROPOSE` the wrapper re-invokes you with "
                    "the proposal in your event payload; SYNC, do your "
                    "Producer WORK (review + harden the coder's tests), "
                    "then PROPOSE your hardening and ACK/NACK the coder "
                    "in the same invocation (fall through to step 3 "
                    "(SYNC) → step 4 (REVIEW) → step 5 (ACK/NACK) here). "
                    "Subsequent invocations (re-proposes — "
                    "`CONSENSUS_PROPOSE` version > 1 — and peer-producer "
                    "proposals) are fresh reviews against the new delta, "
                    "not continuations."
                    if is_dual_role
                    else ""
                ),
                "3. **SYNC**: Before reviewing, sync your worktree so you have the "
                "producer's commits: `git fetch origin && git merge "
                + _pkg._resolve_origin_ref(branch or base_branch)
                + " --no-edit`",
                "4. **REVIEW**: Once a proposal arrives, form independent judgment from "
                "the referenced code artifacts. Read the actual files — do not rely "
                "solely on the proposal summary.",
                "5. **ACK/NACK**: Your `--reason` IS your review. Put your **full analysis** "
                "there — this is what the producer reads and acts on. **Always "
                "pass `--ack-version` / `--nack-version`** with the producer's "
                "current proposal version (#2142) — read it from the "
                "`CONSENSUS_PROPOSE` message that triggered your review (the "
                "`version` field). The orchestrator rejects the verdict with "
                "`stale_version` if the producer has re-proposed since you "
                "started reviewing.\n"
                "\n"
                "   **NACK format** (use when blocking issues exist):\n"
                "   ```\n"
                '   egg-orch consensus nack <role> --files-reviewed "f1" "f2" '
                '--nack-version <N> --reason "\n'
                "   ### Blocking\n"
                "   1. **file.py:123** — Description of the issue. Fix: suggested fix.\n"
                "   2. **file.py:456** — Description of the issue. Fix: suggested fix.\n"
                "   ### Non-blocking\n"
                "   - **file.py:789** — Suggestion for improvement.\n"
                '   "\n'
                "   ```\n"
                "\n"
                "   **ACK format** (use when no blocking issues):\n"
                "   ```\n"
                '   egg-orch consensus ack <role> --files-reviewed "f1" "f2" '
                '--ack-version <N> --reason "\n'
                "   Reviewed [N files / specific areas]. Verified [what was checked].\n"
                "   [Specific observations about correctness, security, etc.]\n"
                "   ### Non-blocking\n"
                "   - **file.py:123** — Optional suggestions for improvement.\n"
                '   "\n'
                "   ```\n"
                "\n"
                "   **Conditional ACK (#1998)** — use when the work is "
                "correct but a human action is needed at merge time "
                "(`git mv`, secret rotation, cross-repo flip): add "
                '`--pre-merge-condition "…"` to the ACK. The obligation '
                "renders as a `Pre-merge Obligations` block in the PR "
                "body. Do NOT use this to smuggle blocking issues past "
                "the producer — if the producer could fix it, NACK "
                "instead.\n"
                "\n"
                "   **Drop satisfied obligations on re-ACK (#2338).** When "
                "you re-ACK at a new proposal version and the conditioning "
                "work has landed in-cycle (the rename is in the diff, the "
                "obligation is moot), drop the obligation: re-ACK without "
                "`--pre-merge-condition`. Do NOT re-attach it with a "
                'self-contradicting "satisfied" hedge — the PR body '
                "renders obligations verbatim under a `do not merge` "
                "banner. To preserve the audit trail instead of dropping, "
                "re-ACK with `--pre-merge-condition-resolved-in-diff <sha>` "
                "alongside `--pre-merge-condition` so the renderer "
                "demotes (not drops) the entry (#2336).\n"
                "\n"
                "   `--reason` must be ≥50 chars of substantive content. "
                "Boilerplate like 'lgtm' or 'no issues' will be rejected.\n"
                "\n"
                "   **Stale-version rejection (#2142):** if the producer "
                "re-proposed while your verdict was in flight, the ACK / "
                "NACK is rejected with HTTP 409 inlining the current "
                "proposal snapshot (version, artifacts, commit_sha). "
                "`git fetch && git merge`, re-review against the new "
                "commit, and re-submit — don't retry the same payload."
                + (
                    "\n\n"
                    "   **Contract-enforcer gate (#3114) — applies to you.** "
                    "Your ACK of a producer is structurally gated on the "
                    "contract: the orchestrator REJECTS it (409 "
                    "`contract_incomplete`) while any task row owned by that "
                    "producer in this slice is not `status=complete`. Read "
                    "the live task records with `mcp__sdlc__show_contract` — "
                    "the `.egg-state/contracts/` copy in your checkout is an "
                    "init-time snapshot; do not trust it. When rows are "
                    "incomplete, NACK the producer citing the exact task "
                    "ids: either the work is missing (it must deliver) or it "
                    "landed unrecorded (it must run `mcp__task__complete`). "
                    "When all rows are complete, your ACK MUST carry "
                    '`attestation={"tasks_verified": ["task-…", …]}` on '
                    "`mcp__brc__ack`, covering every task id the producer "
                    "owns in this slice — absent or non-covering lists are "
                    "rejected (`attestation_required` / "
                    "`attestation_mismatch`). Your CONFIRM is likewise "
                    "rejected while ANY row in the slice is incomplete. A "
                    "producer's declared deferral (\"will land in later "
                    'proposals") is an open obligation, not an end-state — '
                    "hold consensus open until the rows are delivered or a "
                    "human descopes them."
                    if phase == "implement" and role_value in _pkg._contract_enforcer_role_names()
                    else ""
                ),
                "6. **CONFIRM**: When all assigned producers reviewed: "
                "`egg-orch consensus confirmed`",
                "7. **HANDLE RE-REVIEW**: When you are re-invoked with a "
                "`CONSENSUS_RE_REVIEW` event (or a `CONSENSUS_PROPOSE` for "
                "a re-propose — version > 1, after you NACKed a prior "
                "version), act on it — failure to respond stalls the "
                "pipeline. Re-review the re-proposing producer's new "
                "proposal and ACK/NACK it, then re-confirm via "
                "`egg-orch consensus confirmed`.\n\n"
                "   **This is adversarial re-review, not blocker-verification.** "
                "Your re-review has TWO equal-weight mandates: (1) verify the "
                "blockers from your prior NACK were addressed AND (2) audit the "
                "delta since your last review — the commits landed since the "
                "version you last verdicted (per REVIEWER-SYNC.md: `git log "
                "{last_reviewed_commit}..HEAD --not origin/{base_branch} -p`) — "
                "as a fresh reviewer with no NACK history, bounded to that "
                "delta, NOT the whole accumulated surface. Both must pass to "
                "ACK. The orchestrator's adversarial re-prime in the event "
                "body carries the full framing; this is a pointer. New issues "
                "outside your prior NACK's scope are blocking; **NACK without "
                "hesitance** — re-reviews are cheap by design, and the "
                "downstream GitHub reviewer should find nothing in your "
                "re-reviewed deltas.\n",
            ]
        )

    # Directed coordination guidance — role-gated
    lines.append("### Directed Coordination")
    lines.append(
        "In addition to the BRC consensus flow (PROPOSE/ACK/NACK), you can send "
        "directed peer-to-peer messages to specific agents using "
        "`egg-orch message send --to <role> --type <TYPE>`. These directed messages "
        "are **supplementary** to BRC consensus — they do NOT replace the "
        "PROPOSE/ACK/NACK lifecycle and are never required for consensus to proceed.\n"
    )

    if is_producer:
        lines.extend(
            [
                "**As a producer**, use directed messages to coordinate handoffs and "
                "broadcast progress:",
                "- **HANDOFF**: When your work is ready for a specific peer to act on, "
                "send a HANDOFF message so they know to begin. For example, a coder "
                "notifying the tester that implementation is complete.",
                "  ```",
                '  egg-orch message send --to tester --type HANDOFF --subject "Auth module ready" '
                '--body "auth.py is complete, tests can begin"',
                "  ```",
                "- **STATUS**: Broadcast progress updates to all agents when you reach "
                "significant milestones (e.g., halfway through implementation, blocked "
                "on a dependency).",
                "  ```",
                '  egg-orch message send --to all --type STATUS --subject "Implementation 50% complete" '
                '--body "Core logic done, working on edge cases"',
                "  ```\n",
            ]
        )

    # Same gate as the Reviewer Lifecycle above (#3381): a wake-only,
    # verdict-free role (de-roled simplifier) gets no reviewer-coordination
    # guidance, since it never ACK/NACKs.
    if is_reviewer and casts_real_verdicts:
        lines.extend(
            [
                "**As a reviewer**, when you need clarification before "
                "ACK/NACKing, put the question in your NACK `--reason` "
                "block under `### Non-blocking`.  The producer sees it "
                "atomically with the review verdict and the audit "
                "trail is preserved.  The legacy QUESTION message "
                "type was removed in issue #1897; off-protocol chatter "
                "is no longer advertised.  A follow-up issue will "
                "introduce a structured REQUEST/REPLY subsystem that "
                "names a target peer and times out.",
                "",
            ]
        )

    lines.extend(
        [
            "**Event-handler contract (#2908):** The orchestrator's "
            "event-pump wrapper drives your lifecycle. You are invoked "
            "one-shot per actionable BRC event: handle the event per the "
            "lifecycle above, update durable BRC memory (writes happen "
            "automatically inside `egg-orch consensus ack` / `nack` "
            "handlers), then exit naturally. The wrapper polls "
            "`egg-orch brc next-action` and re-invokes you with the next "
            "event. You do NOT block on `egg-orch message wait-loop` "
            "yourself; the wrapper owns the wait and the heartbeat.\n",
            "",
        ]
    )

    return "\n".join(lines)
