"""review-prompt + role-context helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _summarize_issue(prompt: str | None, issue_number: int | None = None) -> str:
    """Extract a 1-2 sentence summary from the issue title and first paragraph.

    Used to give execution agents (tester, documenter) a brief
    orientation without embedding the full issue body. Analysis agents
    (architect, task_planner, risk_analyst) still receive the full issue.

    Extracts the first markdown heading (or first non-empty line) as the title,
    then the first paragraph as supporting context.
    """
    if not prompt or not prompt.strip():
        return f"Working on issue #{issue_number}." if issue_number else ""

    lines = prompt.strip().splitlines()

    # Extract title: first markdown heading, or first non-empty line
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            title = s.lstrip("# ").strip()
        else:
            title = s
        body_start = i + 1
        break

    # Extract first paragraph after title (up to ~300 chars)
    first_para_lines: list[str] = []
    for line in lines[body_start:]:
        s = line.strip()
        if not s:
            if first_para_lines:
                break
            continue
        first_para_lines.append(s)

    first_para = " ".join(first_para_lines)
    if len(first_para) > 300:
        first_para = first_para[:297] + "..."

    # Build summary
    issue_ref = f" (issue #{issue_number})" if issue_number else ""
    summary = f"**Background**: {title}{issue_ref}"
    if first_para:
        summary += f"\n\n{first_para}"

    return summary


def _extract_plan_overview(plan_text: str) -> str:
    """Extract the plan overview section (before individual phase details).

    Returns the summary/overview portion of the plan, stopping before
    individual phase task listings (### Phase N: ...) and the yaml-tasks
    appendix. This gives the coder high-level context without the full plan.
    """
    lines = plan_text.splitlines()
    overview_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Stop at individual phase headings
        if stripped.startswith("### Phase ") or stripped.startswith("### phase-"):
            break
        # Stop at the yaml-tasks appendix
        if "yaml-tasks" in stripped:
            break
        # Stop at structured task appendix
        if stripped.startswith("## Structured Task Appendix"):
            break
        # Stop at issue-to-task mapping (detailed reference section)
        if stripped.startswith("## Issue-to-Task Mapping"):
            break
        overview_lines.append(line)

    # Trim trailing blank lines
    while overview_lines and not overview_lines[-1].strip():
        overview_lines.pop()

    return "\n".join(overview_lines)


def _build_role_context(
    role_value: str,
    prompt: str | None,
    issue_number: int | None = None,
    phase_obj=None,
    all_phases=None,
    base_branch: str | None = None,
) -> str:
    """Build role-appropriate context to replace raw issue body embedding.

    Analysis roles (architect, task_planner, risk_analyst) receive the full
    issue body since they need it for problem understanding and planning.

    Execution roles (tester, documenter) receive a brief summary
    with structured task information and pointers to full context.

    Args:
        role_value: Agent role string
        prompt: Original task prompt (full issue body)
        issue_number: GitHub issue number
        phase_obj: Current plan phase object (phase context)
        all_phases: All contract phases (phase context)

    Returns:
        Role-appropriate context string to embed in the agent prompt
    """
    from egg_contracts.agent_roles import EXECUTION_ROLE_VALUES

    # Analysis roles need the full issue body for problem understanding
    if role_value in ("architect", "task_planner", "risk_analyst"):
        if prompt:
            return f"## Task Description\n\n{prompt}\n"
        return ""

    lines: list[str] = []

    # Brief summary for execution roles
    summary = _pkg._summarize_issue(prompt, issue_number)
    if summary:
        lines.append(f"## Background\n\n{summary}\n")

    # Phase-specific context
    if phase_obj is not None:
        lines.append(f"## Phase Scope: {phase_obj.name} ({phase_obj.id})\n")

        if role_value == "tester":
            lines.append(
                f"Focus your testing on code changed in plan phase `{phase_obj.id}`. "
                "The following tasks were implemented in this phase:\n"
            )
        elif role_value == "documenter":
            lines.append(
                "Document the current state of the code in the areas these tasks "
                "touch — a snapshot of how the system works now, not a log of what "
                "changed. The following tasks were implemented in this phase:\n"
            )
        else:
            lines.append("The following tasks were implemented in this phase:\n")

        # Filter tasks by role for execution agents.
        # Only apply role-based filtering when at least one task has a role
        # assigned — legacy plans (all role=None) show all tasks to all agents,
        # preserving backward compatibility.
        _has_any_role = any(t.role is not None for t in phase_obj.tasks)
        if role_value in EXECUTION_ROLE_VALUES and _has_any_role:
            # Unassigned tasks (role=None) default to coder.
            filtered_tasks = [
                task
                for task in phase_obj.tasks
                if task.role == role_value or (task.role is None and role_value == "coder")
            ]
        else:
            filtered_tasks = list(phase_obj.tasks)

        for task in filtered_tasks:
            lines.append(f"- **{task.id}**: {task.description}")
            if getattr(task, "acceptance_criteria", None):
                lines.append(f"  - Acceptance: {task.acceptance_criteria}")
            if getattr(task, "files_affected", None):
                lines.append(f"  - Files: {', '.join(task.files_affected)}")
        lines.append("")

    if all_phases and phase_obj is not None and role_value in ("tester", "documenter"):
        # Brief orientation about other phases for context
        other_phases = [p for p in all_phases if p.id != phase_obj.id]
        if other_phases:
            lines.append("### Other Phases (for orientation)\n")
            for phase in other_phases:
                status = getattr(phase, "status", "unknown")
                lines.append(f"- {phase.id}: {phase.name} [{status}]")
            lines.append("")

    # Context pointers — agents can get more detail on demand
    lines.append("## For More Context\n")
    if issue_number:
        lines.append(f"- Full issue: `gh issue view {issue_number}`")
    _rc_base_ref = _pkg._resolve_origin_ref(base_branch)
    lines.append(f"- Changed files: `git diff {_rc_base_ref}...HEAD` or check handoff data")
    lines.append("- Coder output: check `EGG_HANDOFF_DATA` environment variable")
    lines.append("")

    return "\n".join(lines)


def _build_role_restrictions_section(repo: str | None = None) -> str:
    """Build a prompt section describing file access restrictions per execution role.

    This section is injected into the task_planner prompt so that it can
    assign each task to the correct execution role (coder, tester, documenter)
    based on which files the task will modify.

    Args:
        repo: Optional ``owner/repo`` for per-repo pattern overrides
            (#2528). When set, the rendered patterns reflect
            ``role_patterns:`` from ``repositories.yaml`` for the repo
            so the planner sees the same boundaries the gateway will
            enforce. When ``None``, falls back to global defaults.

    Returns:
        Formatted markdown string describing role file boundaries.
    """
    from egg_restrictions.patterns import get_agent_patterns_for_repo

    lines: list[str] = [
        "## Execution Role File Restrictions",
        "",
        "Each task should include a `role` field (coder, tester, or documenter) "
        "indicating which agent should execute it. Assign roles based on the file "
        "access restrictions below. Tasks without a `role` field default to coder.",
        "",
    ]

    patterns_by_role = get_agent_patterns_for_repo(repo)
    for role_name in ("coder", "tester", "documenter"):
        pattern = patterns_by_role.get(role_name)
        if pattern is None:
            continue
        lines.append(f"### {role_name}")
        if pattern.allowed_patterns:
            lines.append(f"- **Allowed**: {', '.join(f'`{p}`' for p in pattern.allowed_patterns)}")
        if pattern.blocked_patterns:
            lines.append(f"- **Blocked**: {', '.join(f'`{p}`' for p in pattern.blocked_patterns)}")
        # Hard blocks are rejected even when they'd match the allow list or a
        # fixture/docs exemption (#3396) — the planner must see them so it
        # doesn't route a hard-blocked path (e.g. a fixture under `.github/`
        # or any `.egg-state/` subdir) to this role.
        if pattern.hard_blocked_patterns:
            hard = f"- **Hard-blocked (never pushable)**: {', '.join(f'`{p}`' for p in pattern.hard_blocked_patterns)}"
            if pattern.hard_block_exempt_patterns:
                hard += (
                    f" (except {', '.join(f'`{p}`' for p in pattern.hard_block_exempt_patterns)})"
                )
            lines.append(hard)
        lines.append("")

    lines.append(
        "Assign `role: tester` to tasks that only touch test files, "
        "`role: documenter` to tasks that only touch docs/README files, "
        "and `role: coder` (or omit the field) for everything else. "
        "If a task spans multiple roles, split it into separate tasks per role."
    )
    lines.append("")

    # Staging-dir convention for `.github/` (issue #2508).
    lines.append("### `.github/` changes — use the `.github-staging/` convention")
    lines.append("")
    lines.append(
        "Every producer role is blocked from writing under `.github/` "
        "(CI workflows, CODEOWNERS, dependabot config) — this is a "
        "branch-protection invariant, not a planner mistake. Tasks that "
        "need to modify those files must instead write the proposed "
        "end-state to top-level `.github-staging/`, mirroring the "
        "`.github/` structure (e.g. a proposed change to "
        "`.github/workflows/ci.yml` is staged at "
        "`.github-staging/workflows/ci.yml`). The producing agent must "
        "call the staged files out in the PR body so the human "
        "reviewer moves them into `.github/` before merge. Assign such "
        "tasks to `role: coder` and make the staging path explicit in "
        "the task's `files_affected`. `.github-staging/` must remain "
        "tracked by git (do not add it to `.gitignore`); otherwise the "
        "staged files won't be in the PR commit and the reviewer's "
        "`git mv` will fail."
    )
    lines.append("")

    # Runtime escape hatch — the actionable producer-side guidance (the
    # "call these two tools, do not invent a workaround, exit cleanly"
    # text) lives in ``_build_impasse_escape_hatch_section`` and is
    # injected into producer prompts (coder/tester/documenter); see
    # issue #2529. Here we tell the planner only that the post-failure
    # delegation path exists, so it knows the orchestrator can rewire a
    # mis-assigned task without re-planning. The planner does not emit
    # impasses itself.
    lines.append("### Runtime delegation (post-failure)")
    lines.append("")
    lines.append(
        "If a producer discovers mid-execution that its assigned task "
        "is structurally impossible, it emits a typed Impasse via "
        "``mcp__sdlc__report_impasse`` and the orchestrator may "
        "auto-delegate the task to a different producer role (see "
        "issue #2529). You don't need to plan for this — it's a "
        "runtime safety net for plan bugs, role-restriction "
        "mismatches, and external blockers."
    )
    lines.append("")

    return "\n".join(lines)


def _build_impasse_escape_hatch_section() -> str:
    """Build the producer-facing runtime escape hatch section (#2529).

    Injected into the coder/tester/documenter prompts so producers know
    to call ``mcp__sdlc__check_file_restriction`` /
    ``mcp__sdlc__report_impasse`` instead of inventing workarounds when
    they hit a structurally impossible task. The planner never emits
    impasses, so this section is omitted from its prompt — see
    ``_build_role_restrictions_section`` for the planner-facing
    summary.
    """
    return "\n".join(
        [
            "## Impossible task? Use the runtime escape hatch — DO NOT invent workarounds",
            "",
            (
                "If you discover mid-execution that the task you've been "
                "assigned is structurally impossible (file restrictions "
                "block your role, the plan is buggy, an external "
                "dependency is missing), STOP. Do not invent a "
                "workaround like staging the files in another directory "
                "or asking another agent to do it via a freeform handoff "
                "document — past pipelines (#2474, #2529) wasted ~10+ "
                "min and triggered downstream NACKs that way."
            ),
            "",
            "Instead, use the two MCP tools:",
            "",
            (
                '1. `mcp__sdlc__check_file_restriction({path: "..."})` — '
                "cheap pure-local read against `shared/egg_restrictions/"
                "patterns.py`. Confirms whether your role can write the "
                "path and returns `alternative_role` (the producer role "
                "that *can* write it, when exactly one covers it). Call "
                "this BEFORE exploring a file you suspect is outside "
                "your boundary."
            ),
            "",
            (
                "2. `mcp__sdlc__report_impasse({category, reason, "
                "task_id, suggested_role, blocked_files})` — emits a "
                "typed Impasse signal and exits cleanly. **`task_id` is "
                "required for ``wrong_role`` impasses** (look it up in "
                "your spawn prompt or via `egg-contract show`); without "
                "it the orchestrator cannot route precisely and "
                "escalates to HITL. The orchestrator detects the "
                "impasse post-phase and either delegates to "
                "``suggested_role`` (first attempt) or escalates to "
                "HITL (second attempt or no eligible role). Categories: "
                "``wrong_role`` (file restrictions; auto-delegateable), "
                "``plan_bug`` / ``external_blocker`` / ``unknown`` "
                "(always HITL). Once you've called this tool, do NOT "
                "commit code or call any other producer tool — just "
                "exit."
            ),
            "",
        ]
    )


def _render_contract_tasks(
    repo_path: str,
    pipeline_id: str,
    pipeline_mode: str,
    issue_number: int | None = None,
) -> str | None:
    """Load contract and render tasks as a markdown checklist.

    Returns None if the contract cannot be loaded.
    """
    try:
        from egg_contracts.loader import load_contract
        from egg_contracts.models import TaskStatus
    except ImportError:
        return None

    # Contracts are keyed by pipeline_id (loader's compat shim handles
    # legacy paths for in-flight pipelines that predate key unification).
    try:
        contract = load_contract(pipeline_id, _pkg.Path(repo_path))
    except Exception:
        return None

    if not contract.slices:
        return None

    lines = ["## Contract Tasks\n"]
    for slice_ in contract.slices:
        if not slice_.tasks:
            continue
        lines.append(f"### {slice_.name}\n")
        for task in slice_.tasks:
            check = "x" if task.status == TaskStatus.COMPLETE else " "
            lines.append(f"- [{check}] **{task.id}**: {task.description}")
            if task.acceptance_criteria:
                lines.append(f"  - Acceptance: {task.acceptance_criteria}")
            if task.files_affected:
                lines.append(f"  - Files: {', '.join(task.files_affected)}")
        lines.append("")

    return "\n".join(lines) if len(lines) > 1 else None


def _list_changed_files_for_review(repo_path: str, base_ref: str) -> list[str]:
    """Read-only: the reviewer's changed-file set (``base...HEAD``), or ``[]``.

    Uses the SAME three-dot range the reviewer is told to review so the pack's
    file set matches the diff under review. Any git failure degrades to ``[]``
    (the caller then skips the shared prefix) — never raises.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _maybe_apply_evidence_prefix(
    prompt: str,
    *,
    reviewer_type: str,
    repo_path: str | None,
    base_ref: str,
) -> str:
    """Wire the shared-evidence prefix (#3523 S7 / task-7-2) into the LIVE prompt.

    This is the integration the seam functions in ``_criteria`` /
    ``consensus_wrapper`` exist for. Behaviour by ``EGG_REVIEW_EVIDENCE_PREFIX``:

    - ``off`` -> ``prompt`` returned unchanged (byte-identical to legacy).
    - ``log`` -> ``prompt`` unchanged; emit a structured record of the would-be
      shared prefix (mode, wave role, prefix byte length) into the BRC log
      stream, mirroring the S6 risk-router log-mode precedent
      (``review_graph.get_review_graph_for_phase``). This is the "measure
      before enabling" step the issue makes an explicit acceptance criterion.
    - ``on``  -> prepend the byte-identical ``[system prefix][evidence pack]``
      at the very front so sibling same-model reviewers share one cacheable
      prefix; the per-lens instruction stays at the tail.

    Only specialist lenses that share the prefix build a pack; the tester and
    finding-verifier stay cold-start (``shares_evidence_prefix`` == False).
    Deterministic: the same repo state yields a byte-identical pack, hence an
    identical prefix across the wave. Fail-open: any gathering error leaves the
    prompt unchanged so prompt assembly is never blocked.
    """
    from evidence_gatherer import (
        evidence_prefix_mode,
        gather_evidence,
        shares_evidence_prefix,
    )

    mode = evidence_prefix_mode()
    if mode == "off":
        return prompt
    # Reconstruct the full AgentRole name the sharing/cold-start guard keys on
    # ("code" -> "reviewer_code", "code-holistic" -> "reviewer_code_holistic").
    reviewer_role = "reviewer_" + reviewer_type.replace("-", "_")
    if not shares_evidence_prefix(reviewer_role):
        return prompt
    if not repo_path:
        return prompt

    changed_files = _pkg._list_changed_files_for_review(repo_path, base_ref)
    if not changed_files:
        return prompt
    try:
        pack = gather_evidence(changed_files, repo_path, base_ref=base_ref)
    except Exception:
        # Fail-open: a gathering failure must never block the review wave.
        return prompt

    from routes.pipelines._criteria import build_shared_evidence_prefix

    prefix = build_shared_evidence_prefix(pack)
    if not prefix:
        return prompt

    if mode == "log":
        from consensus_wrapper import evidence_prefix_log_record

        record = evidence_prefix_log_record(
            wave_roles=[reviewer_role],
            shared_prefix_bytes=len(prefix.encode("utf-8")),
            mode="log",
        )
        _pkg.logger.info(
            "evidence_prefix log-mode: would prepend shared evidence pack (#3523 S7)",
            changed_file_count=len(changed_files),
            **record,
        )
        return prompt

    # on: prepend the byte-identical prefix at the very front of the prompt.
    return prefix + "\n\n" + prompt


def _build_review_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    reviewer_type: str = "code",
    issue_number: int | None = None,
    review_cycle: int = 1,
    prior_feedback: str | None = None,
    repo_path: str | None = None,
    last_reviewed_commit: str | None = None,
    base_branch: str | None = None,
    concurrent: bool = False,
    operator_directives: list[_pkg.OperatorDirective] | None = None,
    iteration_history: list[_pkg.IterationSummary] | None = None,
) -> str:
    """Build a review prompt for the reviewer agent.

    In sequential mode, tells the reviewer to write a typed verdict JSON
    file to .egg-state/reviews/.  In concurrent (BRC) mode, the reviewer's
    ACK/NACK reason IS the review output — no verdict file is written.
    """
    draft_path = _pkg._get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)

    verdict_path: str | None = None
    if not concurrent:
        verdict_path = _pkg._verdict_path_for_type(
            phase,
            reviewer_type,
            issue_number=issue_number,
            pipeline_id=pipeline_id,
        )

    lines = [
        f"You are reviewing the **{phase}** phase output of the SDLC pipeline "
        f"({reviewer_type} reviewer).\n",
        "## Scope\n",
        _pkg._get_reviewer_scope_preamble(reviewer_type, phase),
        "",
        "## Context\n",
        f"Pipeline ID: {pipeline_id}",
        f"Phase: {phase}",
        f"Reviewer: {reviewer_type}",
        f"Review cycle: {review_cycle}",
        "",
        "## Your Task\n",
    ]

    # Delta review: for re-reviews with a known last-reviewed commit,
    # instruct the reviewer to focus on the delta.
    #
    # Two-dot `git diff A..HEAD` would wrongly include any base-branch merges
    # landed between A and HEAD. `git log A..HEAD --not origin/<base> -p`
    # explicitly excludes commits reachable from the base branch, so the
    # reviewer sees only PR-authored work (issue #1758).
    is_delta_review = review_cycle > 1 and last_reviewed_commit and not draft_path
    _base_ref = _pkg._resolve_origin_ref(base_branch)
    _delta_base_branch = _base_ref.removeprefix("origin/")
    diff_command = (
        f"git log {last_reviewed_commit}..HEAD --not {_base_ref} -p"
        if is_delta_review
        else f"git diff {_base_ref}...HEAD"
    )

    if draft_path:
        lines.append(f"1. Read the draft at `{draft_path}`")
    elif is_delta_review:
        lines.append(
            f"1. First run `git fetch origin {_delta_base_branch}`, then review "
            f"the delta using `{diff_command}` (see **Delta Review** below)"
        )
    else:
        lines.append(
            f"1. Review the implementation using `git log --oneline -10` and `{diff_command}`"
        )

    # Add procedural steps for code reviewers (matching GHA reviewer thoroughness).
    # Both ``code`` and ``code-holistic`` get the same numbered procedural-step
    # scaffold, but steps 2 and 8 differ by lens: ``code`` reviews every file
    # systematically and evaluates against the code-review criteria, while
    # ``code-holistic`` skims the diff once and runs the four cross-module
    # passes from the holistic criteria file. See issue #2126 — the prior
    # unified wording told the holistic reviewer to "review every changed
    # file systematically", which contradicted the holistic criteria's
    # "don't verify every line".
    #
    # The operator-copy-paste framing (step 5) and pre-existing-broken-behavior
    # clause (added after step 8) are deliberately scoped to code/code-holistic
    # only. The shapes generalize — a security reviewer reading a `curl | bash`
    # snippet, a concurrency reviewer reading a `gunicorn` launch line, or a
    # contract reviewer reading an acceptance-criterion snippet would all
    # benefit from "would this command execute as written?" — but the four
    # #2724 misses that motivated these additions were code-lens issues
    # (`pip install -r requirements.txt`, `${ANSWER}` shell-interpolated,
    # `datetime.utcnow()` deprecation, non-atomic write). Keeping these on the
    # code-lens branch avoids prompt bloat for narrower-lens reviewers whose
    # rubrics already cover the same ground in lens-specific shape. Expand
    # scope only if observed misses in other-lens reviews motivate it.
    if reviewer_type in ("code", "code-holistic") and not draft_path:
        if reviewer_type == "code-holistic":
            lines.append(
                "2. **Skim the full diff once** to build a mental map of "
                "what the PR adds, who the user is, and what the user's "
                "primary path through the change looks like — do not "
                "re-verify every line; that is the code reviewer's job"
            )
        else:
            lines.append("2. Get the full diff and **review every changed file systematically**")
        lines.append(
            "3. Read surrounding context — check how changed code integrates with the rest of the codebase"
        )
        lines.append(
            "4. Trace data flow from input to output, especially for security-sensitive paths"
        )
        lines.append(
            "5. Verify end-to-end functionality — for new features, trace the complete "
            "execution path in the real deployment environment. Check that config files, "
            "environment variables, and dependencies are actually available where the code runs. "
            "**Read every documented snippet, install command, and code example "
            "as an operator about to copy-paste it.** Apply this verification "
            "ladder to each snippet:\n"
            "   - Would the command execute as written?\n"
            "   - Does the documented file exist (`ls` or `find` it)?\n"
            "   - Does the library/API the snippet calls match the actual "
            "signature (use WebSearch for deprecations and version-dependent "
            "behavior)?\n"
            "\n"
            "   The four blocking findings on PR #2724 (escaped to the GitHub "
            "bot) were all of this shape — `pip install -r requirements.txt` "
            "against a non-existent file, `${ANSWER}` shell-interpolated as a "
            "bare Python identifier, `datetime.utcnow()` deprecated since "
            "Python 3.12, non-atomic file write — and would all have been "
            "caught by reading the snippet as a copy-paster instead of as a "
            "documentation reader."
        )
        lines.append(
            "6. Research when uncertain — use WebSearch and WebFetch (when available) "
            "to look up library behavior, check official documentation, verify "
            "API usage patterns, and confirm the code follows current best practices"
        )
        lines.append("7. Consider edge cases the author may not have tested")
        if reviewer_type == "code-holistic":
            lines.append(
                "8. Run the four mandatory passes from the criteria below "
                "(end-to-end primary use case, doc ↔ code symmetry, "
                "synthetic-key / sentinel coordination, silent-fallback hunt)"
            )
        else:
            lines.append("8. Evaluate against the criteria below")
        # Procedural surfacing of the pre-existing-broken-behavior clause
        # from code-review-criteria.md:71. Buried in the rubric body it's
        # easy to skim past — the #2724 misses on `pip install -r
        # requirements.txt` and the Python-version mismatch both lived
        # in lines the PR reflowed but did not author, and the reviewer
        # treated them as out-of-scope context. Promoting it to a
        # numbered step (read before reviewing, not consulted mid-review)
        # makes it fire on lines the PR touches by reflowing, not only
        # on lines it authors fresh.
        lines.append(
            "**(Pre-existing broken behavior in modified code is blocking.)** "
            "Any unchanged line the PR reflows, surrounds, or otherwise "
            "modifies its area of belongs to this review's scope. If the "
            "PR's hunks reflow an install section, a documented snippet, or "
            "a config example, verify the *whole section* works as advertised "
            "— not just the lines marked `+`. The code is already being "
            "changed in that area; this is the natural place to fix it. "
            "Pre-existing bugs in modified code are NACK-blocking — do not "
            'dismiss as "not a regression."'
        )
        if concurrent:
            lines.append(
                "9. Deliver your full review via ACK/NACK (see BRC protocol below). "
                "Your `--reason` IS your review — include all findings there."
            )
        else:
            lines.append(f"9. Write your verdict to `{verdict_path}` as JSON")
            lines.append("10. Commit the verdict file")
        lines.append("")
        lines.append(
            "**Find ALL issues on the first pass** — do not stop after identifying "
            "a few problems. You are the last line of defense before code reaches "
            "production."
        )
    elif draft_path:
        # Expanded procedural steps for draft-based (non-code) reviewers
        lines.append("2. Read the draft thoroughly — do not skim")
        lines.append(
            "3. Cross-reference each section of the draft against the review criteria below"
        )
        lines.append("4. Cite specific sections, quotes, or omissions as evidence in your analysis")
        lines.append("5. Evaluate completeness — identify any criteria not adequately addressed")
        lines.append("6. Assess overall quality and coherence of the draft")
        if concurrent:
            lines.append(
                "7. Deliver your full review via ACK/NACK (see BRC protocol below). "
                "Your `--reason` IS your review — include all findings there."
            )
        else:
            lines.append(f"7. Write your verdict to `{verdict_path}` as JSON")
            lines.append("8. Commit the verdict file")
    else:
        lines.append("2. Evaluate it against the criteria below")
        if concurrent:
            lines.append(
                "3. Deliver your full review via ACK/NACK (see BRC protocol below). "
                "Your `--reason` IS your review — include all findings there."
            )
        else:
            lines.append(f"3. Write your verdict to `{verdict_path}` as JSON")
            lines.append("4. Commit the verdict file")
    lines.append("")

    # Review criteria
    lines.append("## Review Criteria\n")
    lines.append(_pkg._get_review_criteria_for_type(reviewer_type, phase, repo_path=repo_path))
    lines.append("")

    # Review conventions — quality standards aligned with PR reviewer thoroughness
    lines.append("## Review Conventions\n")
    if reviewer_type in ("code", "code-holistic"):
        lines.append(
            "You are a critical part of the engineering infrastructure — the last line "
            "of defense before code reaches production. Your review must meet these "
            "quality standards:\n"
        )
    else:
        lines.append("Your review must meet these quality standards:\n")
    lines.append(
        "1. **Be comprehensive.** Review the entire scope, not just the obvious parts. "
        "Do not stop after finding the first few issues."
    )
    lines.append(
        "2. **Be specific.** Reference exact file paths, line numbers, function names, "
        "and code snippets. Vague feedback is not actionable."
    )
    lines.append(
        "3. **Be direct.** State issues plainly without hedging or softening language. "
        '"This will fail when X" not "you might want to consider X".'
    )
    lines.append(
        "4. **Suggest fixes.** When identifying a problem, include a concrete suggestion "
        "for how to resolve it."
    )
    lines.append(
        "5. **Provide context.** Explain *why* something is an issue — the impact, "
        "the risk, or the principle being violated."
    )
    lines.append("")

    # Verdict classification — only for code reviewers (aligned with review-conventions.md)
    # Non-code reviewers get appropriate guidance from their type-specific criteria
    # (e.g., _get_plan_review_criteria() already says "flag as needs_revision")
    if reviewer_type in ("code", "code-holistic"):
        _nack_label = "NACK" if concurrent else "`needs_revision`"
        _ack_label = "ACK" if concurrent else "`approved`"
        lines.append(f"### When to {_nack_label} vs {_ack_label}\n")
        lines.append(
            f"**{_nack_label} for**: Security vulnerabilities, logic errors, correctness "
            "issues, non-functional features (core purpose doesn't work end-to-end), missing "
            "error handling, resource leaks, breaking changes, violations of codebase patterns. "
            f"When in doubt, {_nack_label}."
        )
        lines.append(
            f"**{_ack_label} for**: No blocking issues found after thorough review. "
            "Non-blocking suggestions should still be included."
        )
        lines.append("")
        lines.append(
            "**Key distinction**: A feature that doesn't work is a correctness issue, not a "
            "style issue. If the feature's core functionality is broken — not just degraded or "
            f"missing edge cases — always {_nack_label}, even if the code structure looks "
            "reasonable or matches an existing pattern."
        )
        lines.append("")

    # Delta review directive for re-reviews
    if is_delta_review:
        lines.append("## Delta Review\n")
        lines.append(
            f"This is review cycle {review_cycle}. Focus on new changes since your "
            f"last review. First run `git fetch origin {_delta_base_branch}` to "
            f"ensure the base branch is available, then use "
            f"`git log {last_reviewed_commit}..HEAD --not {_base_ref} -p` to see "
            "the delta — this excludes any base-branch commits that were merged "
            "in since your last review, so you only see PR-authored changes. "
            "Verify prior feedback was addressed AND review new code thoroughly."
        )
        lines.append("")

    # Phase iteration context: operator directives + prior iteration
    # history. Surfaced to reviewers so they cannot faithfully NACK a
    # directive-driven change against a stale default rubric (#2795).
    iteration_context = _pkg._build_phase_iteration_context(operator_directives, iteration_history)
    if iteration_context:
        lines.append(iteration_context)

    # Prior feedback for re-reviews
    if review_cycle > 1 and prior_feedback:
        lines.append("## Prior Review Feedback\n")
        lines.append(
            "This is a re-review. The previous review found issues. "
            "Verify that the following feedback was addressed:\n"
        )
        lines.append(prior_feedback)
        lines.append("")

    # Verdict format — only for sequential (non-concurrent) reviewers.
    # In concurrent/BRC mode, the ACK/NACK reason IS the review output.
    if not concurrent:
        lines.append("## Verdict Format\n")
        lines.append(f"Write the following JSON to `{verdict_path}`:\n")
        lines.append("```json")
        lines.append("{")
        lines.append(f'  "reviewer": "{reviewer_type}",')
        lines.append('  "verdict": "approved" or "needs_revision",')
        lines.append('  "summary": "Brief summary of findings (1-2 sentences)",')
        lines.append('  "analysis": "Detailed analysis of the reviewed work (see below)",')
        lines.append('  "suggestions": "Non-blocking suggestions for improvement",')
        lines.append('  "feedback": "Blocking issues requiring revision before approval",')
        lines.append('  "timestamp": "ISO 8601 timestamp"')
        lines.append("}")
        lines.append("```\n")
        lines.append("**Field guidelines:**\n")
        lines.append(
            "- **analysis**: Always provide detailed analysis regardless of verdict. "
            "Describe what you reviewed, what you found, and your reasoning."
        )
        lines.append(
            "- **suggestions**: Non-blocking observations and improvement ideas. "
            "Include these even when approving — they help the team improve over time."
        )
        lines.append(
            "- **feedback**: Reserved for **blocking issues only** — problems that must "
            "be fixed before the work can be approved. Leave empty when approving."
        )
        lines.append(
            "\nIf the work meets all criteria, set verdict to `approved`. "
            "If significant issues remain, set verdict to `needs_revision` "
            "and provide actionable feedback in the `feedback` field."
        )

    # Phase restrictions for reviewers
    lines.append("")
    lines.append("## Phase Restrictions\n")
    lines.append("- You CAN read all source files and review artifacts")
    if not concurrent:
        lines.append("- You CAN write verdict files to `.egg-state/reviews/`")
    if reviewer_type == "contract":
        lines.append(
            "- You CAN update the contract in `.egg-state/contracts/` (e.g. marking items as done)"
        )
    lines.append("- You CANNOT push code (git push)")
    lines.append("- You CANNOT create or update PRs")
    lines.append("- You CANNOT modify source files (src/, lib/, docs/, tests/)")
    lines.append("")

    prompt = "\n".join(lines)

    # Shared-evidence prompt prefix (#3523 S7 / task-7-2): under
    # EGG_REVIEW_EVIDENCE_PREFIX, sibling same-model reviewers in a wave share a
    # byte-identical [system prefix][evidence pack] so the prompt cache stays
    # warm. off/log leave `prompt` byte-identical to legacy (log only records the
    # would-be behaviour); on prepends the shared prefix. Applied LAST so the
    # prefix is the literal leading bytes of the reviewer prompt (the cacheable
    # span). The tester/finding-verifier are producers here, not `reviewer_*`
    # types, so they never reach this path — cold-start preserved.
    return _pkg._maybe_apply_evidence_prefix(
        prompt,
        reviewer_type=reviewer_type,
        repo_path=repo_path,
        base_ref=_base_ref,
    )
