"""agent-prompt assembly helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _build_file_boundary_section(role_value: str, repo: str | None = None) -> str:
    """Build a file boundary section for an agent prompt.

    Sources the role's allowed/blocked patterns from
    ``egg_restrictions.patterns.build_agent_patterns`` so the prompt
    matches what the gateway will actually enforce on push — including
    per-repo ``role_patterns:`` overrides from ``repositories.yaml``
    (#2528). The legacy ``egg_contracts.agent_roles`` patterns were
    Python-only and didn't honour the per-repo knobs, which created a
    contradictory message for non-Python repos: the gateway would
    enforce Go conventions while the prompt told the agent the boundary
    was Python.

    Returns an empty string when no patterns are defined for the role.
    """
    try:
        from egg_restrictions.patterns import get_agent_pattern_for_repo
    except ImportError:
        return ""

    pattern = get_agent_pattern_for_repo(role_value, repo=repo)
    if pattern is None:
        return ""

    if (
        not pattern.allowed_patterns
        and not pattern.blocked_patterns
        and not pattern.hard_blocked_patterns
    ):
        return ""

    lines = [
        "## File Boundaries (Gateway-Enforced)\n",
        f"Your role ({role_value.upper()}) can only push changes to files "
        "matching these patterns. The gateway will **reject your push** if it "
        "includes files outside your boundaries. Only create and modify files "
        "you are allowed to push.\n",
    ]
    if pattern.allowed_patterns:
        lines.append("**Allowed:** " + ", ".join(f"`{p}`" for p in pattern.allowed_patterns))
    if pattern.blocked_patterns:
        lines.append("**Blocked:** " + ", ".join(f"`{p}`" for p in pattern.blocked_patterns))
    # Hard blocks are a stricter tier: they are rejected even when they would
    # otherwise match your allow list or a docs/fixture exemption (#3396). The
    # agent must see them, or it will author a hard-blocked path (e.g.
    # `.egg-state/contracts/fixtures/x.json`, `.github/actions/x/testdata/`),
    # hit a gateway 403, and have no way to understand why.
    if pattern.hard_blocked_patterns:
        hard_line = "**Hard-blocked (never pushable, no exemption applies):** " + ", ".join(
            f"`{p}`" for p in pattern.hard_blocked_patterns
        )
        if pattern.hard_block_exempt_patterns:
            hard_line += " — except " + ", ".join(
                f"`{p}`" for p in pattern.hard_block_exempt_patterns
            )
        lines.append(hard_line)

    # `.github/` staging-dir convention (issue #2508). Surfaced for the
    # coder role specifically because it's the producer that's expected
    # to initiate `.github/` work. The role-pattern check
    # (``startswith(".github/")``) doesn't match `.github-staging/`, so
    # autofixer / conflict_resolver allowlists technically reach the
    # staging path too — but those roles are reactive and aren't asked
    # to plan new `.github/` changes, so the convention's planning-time
    # guidance only needs to land for coder.
    if role_value == "coder":
        lines.append("")
        lines.append(
            "**`.github/` changes**: `.github/` is blocked above. If your "
            "task requires modifying CI workflows, CODEOWNERS, dependabot "
            "config, or anything else under `.github/`, write the proposed "
            "end-state to top-level `.github-staging/` instead, mirroring "
            "the `.github/` structure (e.g. stage "
            "`.github/workflows/test-e2e.yml` as "
            "`.github-staging/workflows/test-e2e.yml`). Call out the "
            "staged files explicitly in your PR body so the human reviewer "
            "knows to move them into `.github/` before merge — see issue "
            "#2508."
        )
    lines.append("")
    return "\n".join(lines)


def _build_agent_prompt(
    role_value: str,
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    repo_path: str | None = None,
    phase_obj=None,
    all_phases=None,
    concurrent: bool = False,
    network_mode: str | None = None,
    operator_directives: list[_pkg.OperatorDirective] | None = None,
    iteration_history: list[_pkg.IterationSummary] | None = None,
) -> str:
    """Build a role-specific prompt for multi-agent execution.

    For the CODER role, delegates to the existing _build_phase_prompt().
    Other roles (TESTER, DOCUMENTER, ARCHITECT, etc.) get
    role-specific instructions.

    Execution roles (tester, documenter) receive a summarized
    background with structured task information instead of the full issue
    body. Analysis roles (architect, task_planner, risk_analyst) receive
    the full issue body.

    Note: Handoff data is passed via the EGG_HANDOFF_DATA environment
    variable, not via the prompt — prompts are built once before
    execution starts.

    Args:
        role_value: Agent role string (e.g. "coder", "tester")
        phase: Pipeline phase name
        pipeline_id: Pipeline ID
        pipeline_mode: "issue" or "local"
        prompt: Original task prompt
        issue_number: GitHub issue number
        repo: Repository name
        branch: Branch name
        review_feedback: Feedback from prior review cycle
        review_cycle: Current review cycle number
        repo_path: Filesystem path to repository (for user override lookup)
        phase_obj: Current plan phase object (optional)
        all_phases: All contract phases (optional)
        concurrent: Whether agent runs in concurrent multi-agent mode.
            When True, adds consensus lifecycle preamble instructing the
            agent to stay alive, poll messages, and participate in consensus.
        network_mode: Pipeline network mode ("public", "private", or None).
            When "private", injects warnings about blocked package downloads.

    Returns:
        Complete prompt string for the agent
    """
    # CODER and REFINER use the existing phase prompt (phase-specific
    # instructions are already tailored for refine vs implement etc.)
    if role_value in ("coder", "refiner"):
        base_prompt = _pkg._build_phase_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=prompt,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            review_feedback=review_feedback,
            review_cycle=review_cycle,
            repo_path=repo_path,
            operator_directives=operator_directives,
            iteration_history=iteration_history,
        )
        # Surface file boundaries so agent knows what it can push (#1431).
        # Pass repo so the rendered patterns match per-repo overrides
        # (#2528) the gateway will enforce on push.
        boundary_section = _pkg._build_file_boundary_section(role_value, repo=repo)
        if boundary_section:
            base_prompt += "\n" + boundary_section
        # Producer escape hatch (#2529) — coder is one of the impassing
        # producer roles, so it must see the actionable
        # check_file_restriction / report_impasse guidance instead of
        # inventing workarounds. Refiner runs in the refine phase and
        # never owns implement-phase tasks, so it doesn't need this.
        if role_value == "coder":
            base_prompt += "\n" + _pkg._build_impasse_escape_hatch_section()
        # In concurrent mode, inject BRC consensus preamble so the coder/refiner
        # knows to propose, respond to reviews, confirm, and stay alive.
        if concurrent:
            base_prompt += _pkg._build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
            )
        return base_prompt

    if role_value.startswith("reviewer_"):
        # Reviewer prompts are fully built by _build_review_prompt with its
        # own criteria/verdict format + iteration-context wiring; we don't
        # accumulate the role-shared ``lines`` block for them. Dispatching
        # here (rather than mid-function with an early return) prevents
        # future drift where a "must always be included" line is added to
        # the accumulation and silently never reaches reviewers (#2795).
        reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")
        review_prompt = _pkg._build_review_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            reviewer_type=reviewer_type,
            issue_number=issue_number,
            review_cycle=review_cycle + 1,
            prior_feedback=review_feedback,
            repo_path=repo_path,
            base_branch=base_branch,
            concurrent=concurrent,
            operator_directives=operator_directives,
            iteration_history=iteration_history,
        )
        if concurrent:
            review_prompt += "\n" + _pkg._build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
            )
        return review_prompt

    # Build context header (shared across all roles)
    lines = [f"You are the **{role_value.upper()}** agent in the **{phase}** phase.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    lines.append(f"Mode: {pipeline_mode}")
    lines.append(f"Agent Role: {role_value}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # Concurrent mode: add BRC consensus lifecycle preamble so agents understand
    # they must stay alive and participate in Broadcast-Review-Converge consensus.
    if concurrent:
        lines.append(
            _pkg._build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
            )
        )

    # Include role-appropriate context instead of the raw issue body.
    # Analysis roles (architect, task_planner, risk_analyst) receive the full
    # issue body. Execution roles (tester, documenter) receive a
    # brief summary with structured task information and context pointers.
    role_context = _pkg._build_role_context(
        role_value=role_value,
        prompt=prompt,
        issue_number=issue_number,
        phase_obj=phase_obj,
        all_phases=all_phases,
        base_branch=base_branch,
    )
    if role_context:
        lines.append(role_context)

    # Phase iteration context: operator directives + prior iteration history.
    # Rendered for all roles (producers AND reviewers) so reviewers cannot
    # NACK a directive-driven change against a stale default rubric (#2795).
    iteration_context = _pkg._build_phase_iteration_context(operator_directives, iteration_history)
    if iteration_context:
        lines.append(iteration_context)

    # Review feedback from prior agentic cycles (scoped to agentic NACKs
    # since #2795 — HITL kickbacks render via the iteration context above).
    if review_feedback:
        lines.append("## Review Feedback\n")
        lines.append(review_feedback)
        lines.append("")

    # Derive the pipeline identifier for namespaced output filenames.
    _identifier = _pkg._pipeline_identifier(issue_number, pipeline_id)

    # Spec-driven agent-output paths (#3077 slice-3): resolve each path
    # via the artifact registry so the prompt prose, the propose-time
    # validator (signals._validate_producer_artifacts), and the gateway
    # artifact-read endpoint (slice-4) all share one source of truth.
    # The slice-2 mandatory consistency test
    # (TestConsistencyC in shared/egg_contracts/tests/test_artifact_spec.py)
    # pins these call sites to the registry; a future row rename
    # surfaces here as a missing prompt path instead of as #3016-style
    # drift between spec and rendered prose.
    from egg_contracts.artifact_spec import resolve_artifact_path as _resolve_artifact_path

    _architect_output_path = _resolve_artifact_path("architect-output", _identifier)
    _architect_slices_path = _resolve_artifact_path("architect-slices", _identifier)
    _risk_analyst_output_path = _resolve_artifact_path("risk-analyst-output", _identifier)
    # Human-focused companion drafts the simplifier produces (one per phase).
    _analysis_human_path = _resolve_artifact_path("analysis-draft-human", _identifier)
    _plan_human_path = _resolve_artifact_path("plan-draft-human", _identifier)

    # Role-specific instructions
    lines.append("## Your Task\n")

    if role_value == "tester":
        # Look up per-repo check commands from repositories.yaml
        repo_checks: list[dict[str, str]] = []
        if repo:
            try:
                repo_checks = _pkg.get_repo_checks(repo)
            except FileNotFoundError:
                repo_checks = []

        lines.extend(
            [
                "**ROLE BOUNDARY: You are the TESTER, not the CODER.** "
                "Do NOT implement application logic, create source files, write configuration, "
                "or set up project infrastructure. Your job is to write tests for the CODER's "
                "implementation, run checks, and report gaps. If the coder hasn't committed yet, "
                "wait — do not implement the solution yourself.",
                "",
                "**Your mandate is two-fold**:",
                "",
                "1. **Comprehensive coverage** — write tests that prevent "
                "regressions, covering the happy path and realistic alternative "
                "paths through every changed area. New behavior gets new tests; "
                "modified behavior gets updated tests; nothing the coder changed "
                "should silently lose coverage.",
                "2. **Adversarial probing** — actively probe the coder's "
                "implementation for bugs and edge cases they missed. Treat the "
                "implementation as suspect until you have tried to break it. "
                "Write tests that target suspected weaknesses. When a test "
                "fails because of a coder-side bug, **the committed failing "
                "test is evidence — the NACK is the bug report**. Pair every "
                "failing test with an explicit NACK on the coder's proposal "
                "that names the failing test in its rationale; otherwise the "
                "bug is easy for the coder to miss. Also list the bug in "
                "`gaps_found` and HANDOFF to coder with the failure output. "
                "The coder owns the fix; you own surfacing the bug.",
                "",
                "You are also responsible for **lint/type-check validation**.",
                "",
                "### When the slice warrants no new tests (#3027)",
                "",
                "Pure refactors (symbol moves, decompositions with no behavior "
                "change), doc-only slices, and other no-test-work slices still "
                "require you to **propose** — BRC consensus blocks until every "
                "producer has proposed at least once. **Don't just heartbeat "
                "and wait for work that isn't coming.** Instead submit a "
                "generic no-op propose:",
                "",
                "1. (Optional but encouraged) run the configured checks against "
                "the coder's diff (`make lint`, `make test`, etc.) to confirm "
                "the slice really is behavior-preserving.",
                "2. Propose a no-op: `egg-orch consensus propose "
                "--no-changes-needed --no-changes-reason '<concrete reason, "
                "e.g. slice-3 is a pure decomposition: symbol moves between "
                "submodules, no behavior change; existing suite covers the "
                "re-exported barrel>'`. No artifacts or commit-sha are needed.",
                "",
                "The no-op counts as proposing (so consensus is not blocked on "
                "you) and is accepted as a non-blocking no-op — reviewers do not "
                "review or NACK it. If the slice **does** have new test work "
                "(real behavior changes, new edge cases, modified contracts), do "
                "NOT use the no-op path — author tests and propose as usual.",
                "",
                "### Testing",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Build coverage tests for the happy path and realistic "
                "alternative paths in every changed area",
                "3. **Adversarially probe** the implementation: identify "
                "suspected bugs and untested edge cases, then write tests that "
                "target them",
                "4. Run all tests. Tests that pass demonstrate coverage; "
                "**tests that fail demonstrate bugs you have found** — keep them",
                "5. For every failing test caused by a coder-side bug: "
                "commit the failing test AND **NACK the coder's proposal, "
                "explicitly naming the failing test in the NACK rationale**. "
                "The committed test alone is not sufficient — the NACK is "
                "what surfaces the bug to the coder. Also list the bug in "
                "`gaps_found` and HANDOFF to the coder with the failure "
                "output. Your `test` configured check will fail until the "
                "coder pushes a fix — that is expected; do NOT propose "
                "consensus until every configured check passes per the "
                "*Configured Checks* section below",
                "6. Commit all test files with descriptive messages",
                "",
                "Adversarial probing — actively try to break the implementation:",
                "- Missing error handling and input validation",
                "- Boundary conditions, off-by-one, empty/null/oversized inputs",
                "- Uncovered code paths and branches (especially error paths)",
                "- Concurrency: races, partial failures, retry behavior, ordering assumptions",
                "- Contract violations: does the code actually match the "
                "acceptance criteria, or just the happy path of them?",
                "- Integration gaps between components and unstated interface assumptions",
                "",
                "Gap-finding focus (still report these in `gaps_found` even "
                "when you cannot write a test for them):",
                "- Logic errors that would require design changes to fix",
                "- Inconsistencies between the implementation and the plan/contract",
                "- Missing test infrastructure that prevents adequate coverage",
                "",
                "### Configured Checks (MANDATORY)",
                "",
                "You MUST run **ALL** configured checks below and fix any failures "
                "before proposing consensus. Skipping checks (e.g., running tests but "
                "not lint) is a common failure mode — do not skip any.",
                "",
            ]
        )

        if repo_checks:
            # Inject explicit check commands from repositories.yaml
            lines.extend(
                [
                    "The following check commands are configured for this repository. "
                    "Run **every one** of them **in order**:",
                    "",
                ]
            )
            for i, check in enumerate(repo_checks, 1):
                name = check["name"].replace("\n", " ").strip()
                cmd = check["command"].replace("\n", " ").strip()
                lines.append(f"{i}. **{name}**: `{cmd}`")
            lines.extend(
                [
                    "",
                    "If ANY check fails in test files you wrote, fix the issue and re-run. "
                    "If failures are in source code, do NOT fix them — report them to the coder.",
                    "",
                    "After running all checks:",
                ]
            )
        else:
            # Fall back to auto-discovery
            lines.extend(
                [
                    "1. **Discover commands**: Look for Makefile, pyproject.toml, package.json, "
                    "setup.cfg, tox.ini, or similar build/test configuration files",
                    "2. **Run linters**: Execute linters (ruff, eslint, golangci-lint, etc.)",
                    "3. **Run type checkers**: Execute type checkers (mypy, pyright, tsc, etc.)",
                    "",
                    "After running all checks:",
                ]
            )

        lines.extend(
            [
                "- **Auto-fix test files only**: Fix auto-fixable issues in test files you wrote "
                "(formatting, import order, simple type errors)",
                "- **Repeat**: Re-run checks to verify fixes. Repeat up to 3 times.",
                "- **Commit test fixes**: Commit all test-file fixes together with a descriptive message",
                "",
                "Auto-fixable (in test files only — commit fixes directly):",
                "- Lint errors in test files (formatting, import order, code style)",
                "- Type errors in test files with clear fixes",
                "",
                "Report only (do NOT modify source code — NACK the coder and explain what's needed):",
                "- Lint or type errors in source code — tell the coder to fix these",
                "- Test failures caused by bugs in the coder's implementation — tell the coder to fix",
                "- Complex logic errors requiring design decisions",
                "- Security issues requiring architectural changes",
                "",
                "When testing third-party library integrations or unfamiliar frameworks, "
                "use WebSearch and WebFetch (when available) to look up testing patterns, "
                "known edge cases, and recommended test approaches for those libraries.",
                "",
                "## Parallel Execution with Subagents\n",
                "If the changes span multiple independent components or modules, you can use "
                "Claude Code's **Agent tool** to parallelize test writing. Launch one subagent "
                "per component to write and run tests concurrently. Each subagent should work "
                "on non-overlapping test files. Subagents should only write files — do NOT "
                "stage or commit from subagents. After all subagents complete, run the full "
                "test suite to verify everything passes together, then stage and commit yourself.",
                "",
                *_pkg._EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )

        # Test execution verification — prevents proposing consensus with
        # unverified tests (issue #1359).
        test_verify_lines = [
            "### Test Execution Verification (CRITICAL)\n",
            "You MUST actually execute the test suite (`go test`, `pytest`, `jest`, etc.). "
            "Passing gofmt, syntax checks, or linting alone does NOT count as tests run.\n",
            "If tests cannot run (e.g., dependency downloads blocked in private network mode, "
            "missing build tools), you MUST:",
            "1. Set `tests_execution_blocked: true` and provide `tests_execution_blocked_reason` "
            "in your attestation when proposing consensus",
            '2. Include an explicit **"TESTS UNVERIFIED"** warning in your proposal summary',
            '3. Do NOT claim your work is "complete" — state that tests are written but unverified',
            "",
            "**Distinguish `tests_execution_blocked` from a no-op propose** "
            "(see the no-op section above): set `tests_execution_blocked=true` "
            "when you DID author / intend tests but the configured checks could "
            "not run (blocked downloads, missing tools) — that is a real "
            "proposal with lower confidence. Use the generic no-op propose "
            "(`--no-changes-needed`) only when the slice genuinely warrants no "
            "new tests at all. Don't conflate the two.",
            "",
        ]
        if network_mode == "private":
            test_verify_lines.extend(
                [
                    "**WARNING: Private network mode is active** — external package downloads "
                    "(go mod download, npm install, pip install, etc.) may be blocked. "
                    "If dependency installation fails, you cannot verify tests. "
                    "Follow the instructions above to flag tests as unverified.",
                    "",
                ]
            )
        lines.extend(test_verify_lines)

        # Check execution verification — prevents proposing consensus without
        # running all configured checks (issue #1414).
        check_verify_lines = [
            "### Check Execution Verification (CRITICAL)\n",
            "You MUST run **every** configured check command and ensure they **pass** "
            "before proposing consensus. Running tests alone is NOT sufficient — "
            "lint, type-check, and security checks must also pass. If you skip a "
            "check or propose with a failing check, the server will reject your "
            "proposal.\n",
            "Before proposing, verify:",
            "- [ ] All configured check commands have been executed",
            "- [ ] All checks pass (or failures have been auto-fixed and re-verified)",
            "- [ ] Any auto-fix commits have been pushed",
            "",
            # Source-failure handling — without this, agents have rationalised
            # inventing ad-hoc check names so their attestation passes, masking
            # red CI on the initial push (issue #1966).
            "### When Source-Code Checks Fail (CRITICAL)\n",
            "If a configured check fails because of the **coder's source code** "
            "(not test files you wrote), you have a binding choice: "
            "**do NOT propose consensus**. The role boundary above forbids you "
            "from fixing source code, and the rules below forbid you from "
            "papering over the failure. Instead:\n",
            "1. **Do NOT fix it yourself** — that crosses the tester role boundary.",
            "2. **Do NOT invent a narrower or renamed check** "
            "(e.g. `pytest-<your-suite>`, `ruff-check-tester-files`) and attest to "
            "*that* in `checks_passed`. Only the literal names from "
            "`repositories.yaml` (`lint`, `test`, `security`, etc.) are valid; "
            "the server will reject anything else, and substituting narrower names "
            "hides real CI failures from reviewers.",
            "3. **Send a HANDOFF message to the coder** describing the failing "
            "check, the command, and the diagnostic output, e.g.:",
            "   ```",
            "   egg-orch message send --to coder --type HANDOFF \\",
            '     --subject "lint failing on src/foo.py" \\',
            '     --body "make lint exits 1: mypy errors in src/foo.py:42 '
            '(incompatible types). Please fix and push; I will re-run lint."',
            "   ```",
            "   If you are also reviewing the coder's own consensus proposal, "
            "NACK it for the same reason — the two channels reinforce each other.",
            "4. **Wait** for the coder to push a fix, then **re-run every "
            "configured check** from scratch. Use `egg-orch message wait-loop` "
            "(see Producer Lifecycle) — do not spin in a shell `for` loop or "
            "prefix with `sleep`.",
            "5. **Only propose consensus once every configured check passes "
            "literally**, with the configured names in `checks_passed`.",
            "",
            "If the coder is unresponsive or the failure genuinely cannot be "
            "fixed within this phase, document it in `gaps_found` and let the "
            "orchestrator escalate via `OVERSEER_ALERT`. Do NOT work around the "
            "block by proposing with a partial or renamed `checks_passed` list.",
            "",
            "### Attestation: `checks_passed` (REQUIRED)\n",
            "When proposing consensus, your attestation MUST include a `checks_passed` "
            "list containing the **name** of every configured check that **passed**. "
            "Do NOT include checks that failed, and do NOT invent ad-hoc names "
            "(e.g. `pytest-<scope>`, `ruff-check-tester-files`) — only the literal "
            "names from `repositories.yaml`. "
            "For example, if the repo has `lint` and `test` checks and both pass, "
            'your attestation must include `"checks_passed": ["lint", "test"]`. '
            "The server will reject your proposal if any configured check is missing "
            "from this list (i.e. did not pass).",
            "",
        ]
        lines.extend(check_verify_lines)

    elif role_value == "documenter":
        lines.extend(
            [
                "Document the CURRENT STATE of the code after this change. "
                "Write as if the code has always worked this way — the "
                "slice/pipeline machinery that produced the change does not "
                "belong in the documentation:",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Update relevant documentation (READMEs, docstrings, API docs) so it "
                "describes how the system works now",
                "3. Add or update inline code comments where they clarify current behavior",
                "4. Commit documentation changes with descriptive messages",
                "",
                "Write snapshots, not changelogs:",
                "- Describe what the code does now, not what changed or when it changed.",
                "- NEVER reference SDLC artifacts — slice numbers, TASK-N ids, phase or "
                "HITL iteration numbers — in any doc, docstring, or inline comment you write.",
                '- Include historical context (issue links, "previously X" rationale, '
                "migration notes) ONLY when it is tangibly valuable to a reader of the "
                'current system, and prefer rationale ("why it is this way") over '
                'chronology ("what it used to be / when it changed").',
                "- When updating an existing doc, fold the new state into the snapshot and "
                "REMOVE now-stale ledger or historical entries rather than appending "
                "another layer.",
                "",
                "When documenting third-party integrations or external APIs, use WebSearch "
                "and WebFetch (when available) to verify current API signatures, link to "
                "official documentation, and confirm usage examples are up to date.",
                "",
                "### When the slice warrants no doc updates (#3027)",
                "",
                "Pure refactors (symbol moves, decompositions with no "
                "surfaced API change), test-only slices, and internal-only "
                "slices that don't touch any documented surface still "
                "require you to **propose** — BRC consensus blocks until "
                "every producer has proposed at least once. **Don't just "
                "heartbeat and wait for work that isn't coming.** Instead "
                "submit a generic no-op propose:",
                "",
                "1. Walk the coder's diff and confirm there is no "
                "documented-surface impact: no public API signature "
                "changes, no behavior changes a user-facing doc describes, "
                "no new feature or flag mentioned in README / docs/, no "
                "docstring contracts that drift.",
                "2. Propose a no-op: `egg-orch consensus propose "
                "--no-changes-needed --no-changes-reason '<concrete reason, "
                "e.g. a pure decomposition: symbol moves between "
                "submodules, no surfaced API change; no README / docs/ / "
                "docstring surface impacted>'`. No artifacts or commit-sha "
                "are needed.",
                "",
                "The no-op counts as proposing (so consensus is not blocked "
                "on you) and is accepted as a non-blocking no-op — reviewers "
                "do not review or NACK it. If the slice **does** have doc "
                "impact (any of the bullets above), do NOT use the no-op "
                "path — author doc changes and propose as usual.",
                "",
                *_pkg._EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
    elif role_value == "architect":
        lines.extend(
            [
                "Analyze the task and produce an architecture analysis:",
                "",
                "1. Understand the problem or feature request from the issue",
                "2. Research the current codebase to understand existing patterns",
                "3. Research externally when the task involves third-party libraries, APIs, "
                "or frameworks — use WebSearch and WebFetch (when available) to verify "
                "assumptions, check current documentation, review architectural patterns, "
                "and look up current best practices. Skip external research for purely "
                "internal changes.",
                "4. Identify key files, constraints, and dependencies",
                "5. Consider multiple implementation approaches",
                "6. Recommend an approach with justification and document technical decisions",
                "7. **Surface runtime-primitive assumptions explicitly (see #2594).** "
                "When your analysis mentions a class, function, HTTP route, env var, "
                "ConfigMap key, test fixture, CLI flag, or decorator, cite it with "
                "`file:line` evidence (`grep -rn` is enough). Call out scope on "
                "**both** of the following orthogonal axes when either matters: "
                "(a) **purpose** — is the primitive unit-test-only (e.g. a test "
                "double like `ScriptedProvider`) vs deployed-pod / production "
                "code; (b) **execution context** — does the consumer run as "
                "`in-sandbox-agent` (agent pod, reaches gateway via `GATEWAY_URL`) "
                "vs `trusted-CI-runner` (pytest from outside the cluster, sees "
                "`orchestrator_url` / lifecycle-secret-gated routes / kubectl). A "
                "primitive can be unit-test-only but invoked from either runner, "
                "or deployed-pod-only but called from either runner — these are "
                "independent dimensions, so spell out whichever applies. Buried "
                "runtime assumptions are the dominant cause of expensive "
                "implement-phase NACKs; surfacing them here makes the plan-phase "
                "audit cheap.",
                "",
                f"Write your analysis to `{_architect_output_path}`.",
                "",
                # ----------------------------------------------------
                # #2809 — architect owns slice composition
                # ----------------------------------------------------
                "## Slice composition authority (#2809)",
                "",
                "**You are the sole authority for slice composition in the "
                "plan phase.** ``task_planner`` enumerates tasks within the "
                "slices you define; ``risk_analyst`` surfaces risks that "
                "feed your design. Neither owns slice shape — you do. "
                "Specifically, you own:",
                "",
                "- **Slice count.** Treat the operator's ``cq-1`` (or "
                "equivalent refine-phase complexity answer) as a coarse "
                "top-level hint, not a literal slice count. Subdivide "
                "further when the natural slice DAG calls for it.",
                "- **Slice boundaries.** Which work goes into which slice, "
                "anchored on design seams.",
                "- **Slice DAG shape.** Parent/child dependencies between "
                "slices. The forest constraint (every slice has at most "
                "ONE DAG parent) is HARD — multi-parent slices break the "
                "stacked-PR invariant. If a slice would naturally have >1 "
                "parents, serialise the upstream slices into a linear "
                "chain and record the chosen ordering on the downstream "
                "slice's ``serialized_chain_order`` field. See "
                "``docs/architecture/slice-dag.md``.",
                "- **File-overlap ⇒ dependency edge (HARD — #3046).** Any two "
                "slices that touch the same file MUST be ordered on one "
                "dependency chain (express the order in ``dependencies`` — a "
                "single-parent id per slice — not just in "
                "``serialized_chain_order``, which the scheduler does not read "
                "for branch topology). Slices that edit a shared file but are "
                "left as parallel roots/siblings fork independently off the "
                "shared base and collide at integration — plan ingestion "
                "hard-rejects this. A slice that deletes or retires a file "
                "must depend on every slice that modifies it. Keep slices with "
                "disjoint file sets parallel so they still run concurrently.",
                "- **Test co-location (HARD — #3411).** A slice that removes, "
                "renames, or rewrites code must carry the matching updates to "
                "the tests exercising that code — skip-guards, deletions, "
                "rewrites — in the SAME slice, never a later one. Every "
                "cumulative slice tip must be independently green: the "
                "per-slice green gate (#3398) runs the repo's checks at each "
                "slice tip and blocks the PR while any check is red, so a "
                "plan that parks test obsolescence in a later slice "
                "guarantees a blocked slice and repair-loop churn on slices "
                "whose only sin is plan topology. In repos that ship the "
                "changeset-aware selector (this repo's "
                "``scripts/select_tests``), the affected tests are "
                "statically discoverable with the same import graph ``make "
                "test`` narrowing uses: ``python3 "
                "scripts/select_tests/__main__.py --impacted-tests "
                "<file>...`` prints every test file that transitively "
                "imports the named files (exit 2 = closure unavailable — "
                "fall back to grepping the removed symbols in the test "
                "trees). Write the removing slice's ``goal`` so it "
                "explicitly includes those test updates; ``task_planner`` "
                "enumerates them as tasks in that slice.",
                "- **Sub-slicing.** When one slice would be too coarse, "
                "subdivide it. Right-size slices for a single BRC cycle: "
                "avoid bundling distinct file-category groups (e.g. "
                "orchestrator + gateway + schema + tests + docs all in "
                "one slice), avoid bundling deletion-heavy work with "
                "new-API-introduction work, and avoid bundling task "
                "groups that have no internal dependency — those are "
                "natural seams for parallel sub-slices. If a slice would "
                "require the implementing producer to "
                "commit-propose-revise more than 3–4 times to converge, "
                "subdivide it.",
                "",
                "Emit the slice scaffold as a YAML file alongside your "
                "JSON analysis. ``task_planner`` will copy this scaffold "
                "**verbatim** into the plan document's ``# yaml-tasks`` "
                "appendix and fill in ``tasks:`` under each slice — the "
                "scaffold is binding. If ``reviewer_plan`` NACKs on "
                "``slice_size`` or the structural lens calls a "
                "sub-division, you re-propose with the updated scaffold; "
                "task_planner re-consumes the new scaffold on the next "
                "BRC cycle.",
                "",
                f"Write the slice scaffold to `{_architect_slices_path}`:",
                "",
                "```yaml",
                "slices:",
                "  - id: 1",
                "    name: |-",
                "      <slice name>",
                "    goal: |-",
                "      <what this slice achieves>",
                "    # root slice — omit ``dependencies``",
                "  - id: 2",
                "    name: |-",
                "      <slice name>",
                "    goal: |-",
                "      <what this slice achieves>",
                "    dependencies: slice-1",
                "```",
                "",
                "Omit ``dependencies`` for root slices; for every non-root "
                "slice set ``dependencies`` to its single parent's "
                "``slice-<id>`` (e.g. ``slice-1``). ``dependencies`` is the "
                "canonical ordering key the plan parser reads (per "
                "`.egg/schemas/yaml-tasks.schema.json`) — the slice DAG is a "
                "forest, so each slice has at most one parent (one id, not a "
                "list). Do NOT include ``tasks:`` in the scaffold — that is "
                "task_planner's job. Keep ``name`` and ``goal`` concise "
                "enough that task_planner can copy them without rewording.",
                "",
                "### File Restrictions",
                "",
                "You MUST only write to:",
                f"- `{_architect_output_path}`",
                f"- `{_architect_slices_path}`",
                "",
                "Do NOT create or modify any other files. Specifically:",
                "- Do NOT modify analysis drafts (`.egg-state/drafts/*-analysis.md`) — "
                "these are finalized in the refine phase and are read-only",
                "- Do NOT create or modify contracts (`.egg-state/contracts/`)",
                "- Do NOT create or modify reviews (`.egg-state/reviews/`)",
                "- Do NOT create or modify plan drafts (`.egg-state/drafts/*-plan.md`)",
                "",
                *_pkg._EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
    elif role_value == "task_planner":
        draft_path = _pkg._get_draft_path(
            "plan", issue_number=issue_number, pipeline_id=pipeline_id
        )
        # Spec-driven (#3077 slice-3) — reuses the helper-resolved path above
        # so the task_planner prose and the architect prompt cannot drift.
        architect_slices_path = _architect_slices_path
        lines.extend(
            [
                "Decompose the architecture analysis into a slice-DAG implementation "
                "plan. The implement-phase pipeline ships each slice as its own "
                "stacked PR.",
                "",
                "**Slice composition is NOT your call (#2809).** ``architect`` owns "
                "slice count, slice boundaries, slice DAG shape, and sub-slicing — "
                f"and emits the binding scaffold at `{architect_slices_path}`. Your job "
                "is to enumerate ``tasks:`` within those slices, **not to re-shape "
                "them**. Copy the architect's scaffold verbatim into the "
                "``# yaml-tasks`` appendix (preserving slice ``id``, ``name``, "
                "``goal``, and ``dependencies``) and add ``tasks:`` under each "
                "slice with task IDs of the form ``TASK-<slice_id>-<n>``.",
                "",
                "If a slice has too many tasks for one BRC cycle, or you discover a "
                "natural sub-seam the architect missed, that is a **slicing problem "
                "the architect must fix** — surface it as NACK pressure (your peer "
                "reviewer ``risk_analyst`` and the structural reviewer "
                "``reviewer_plan`` will NACK ``architect`` on ``slice_size`` when "
                "evidence supports it; you can also flag the concern in your plan "
                "prose so the reviewers pick it up). **Do NOT silently re-shape "
                "slices.** Re-propose against the architect's revised scaffold "
                "once it lands.",
                "",
                "**Test co-location (HARD — #3411).** When a slice removes, "
                "renames, or rewrites code, enumerate the matching test "
                "updates (skip-guard, deletion, rewrite) as tasks IN THAT "
                "SLICE — never in a later slice — and list the test files in "
                "those tasks' ``files:``. Every cumulative slice tip must be "
                "independently green: the per-slice green gate (#3398) "
                "blocks a slice PR while any repo check is red at its tip, "
                "so a test that still imports a symbol removed two slices "
                "earlier blocks the whole stack. Discover the affected "
                "tests with the same import graph ``make test`` narrowing "
                "uses, where the repo ships it (this repo: ``python3 "
                "scripts/select_tests/__main__.py --impacted-tests "
                "<file>...``; exit 2 = closure unavailable — fall back to "
                "grepping the removed symbols in the test trees).",
                "",
                "Steps:",
                f"1. Read the architecture analysis AND the slice scaffold at `{architect_slices_path}`",
                "2. Copy the architect's slice scaffold verbatim into the "
                "``# yaml-tasks`` appendix (same ``id`` / ``name`` / ``goal`` / "
                "``dependencies`` values, in the same order)",
                "3. Enumerate ``tasks:`` under each slice — discrete, "
                "actionable, with clear acceptance criteria and dependency ordering "
                "between tasks",
                "4. Identify the test strategy — what automated tests cover the "
                "changes, and what manual verification is needed",
                "5. Identify any manual pre-merge or post-merge steps "
                "(migrations, config changes, deployments)",
                "",
                "## Output Format",
                "",
                "Write a markdown plan document with a **yaml-tasks** structured",
                "appendix at the end. The prose section should explain the approach;",
                "the appendix is machine-parsed for contract population.",
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
                "        role: coder  # optional: coder (default), tester, or documenter",
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
                # #2594 — primitives audit (cheap plan-phase NACK)
                # ----------------------------------------------------
                "## Primitives audit (#2594)",
                "",
                "Plan-phase NACKs are cheap; implement-phase NACKs on missing "
                "primitives are expensive (8+ pod spawns per slice, ~60–90 min "
                "per cycle). Make the audit cheap by **pre-citing every "
                "primitive your tasks depend on**. For each named class, "
                "function, HTTP route, env var, ConfigMap key, test fixture, "
                "CLI flag, or decorator your plan references:",
                "",
                "1. **Cite existence** with `file:line` (use `grep -rn` to "
                "verify *before* writing the task). If the primitive does not "
                "exist yet because the task itself will create it, mark it "
                "`(NEW — task TASK-X-Y)` so the plan reviewer doesn't NACK on "
                "missing-primitive evidence. When you mark a primitive "
                "`(NEW — task TASK-X-Y)`, you MUST also: (a) ensure the "
                "referenced task's acceptance criteria actually produce that "
                "primitive in the form the plan uses (right kind, right "
                'module, right scope — not just "adds the feature"), and '
                "(b) order downstream tasks that consume the primitive "
                "**after** the creating task in the slice DAG. The plan "
                "reviewer's §9 exception verifies both; mismatches NACK.",
                "2. **Cite trust-boundary scope.** Some primitives exist but "
                "are unavailable in the execution context the task assumes. "
                "Canonical example: `ScriptedProvider` is unit-test-only; "
                "deployed agent pods run the real provider. Likewise the "
                "`integration_tests/` fixture tiering — the only "
                "`gateway_url` pytest fixture lives at "
                "`integration_tests/local_pipeline/conftest.py:261` and is "
                "kubectl-gated via `local_pipeline_stack`. The parent "
                "`integration_tests/conftest.py` does **not** expose "
                "`gateway_url` as a fixture; it exposes `gateway_url` as an "
                "attribute on the `EggStack` dataclass "
                "(`integration_tests/conftest.py:78`), accessed as "
                "`egg_stack.gateway_url`, not as a fixture-injectable "
                "parameter. `orchestrator_url` and lifecycle-secret-gated "
                "routes are also `local_pipeline/`-only. **No pytest fixture "
                "in `integration_tests/` is `in-sandbox-agent`-runnable "
                "today** — every fixture transitively depends on `egg_stack` "
                "or `local_pipeline_stack`, both of which `pytest.skip` when "
                "`_kubectl_available()` returns `False`. Tasks that need any "
                "of `gateway_url` / `orchestrator_url` as a pytest fixture "
                "MUST live under (or below) `local_pipeline/` or an "
                "equivalent trusted directory. Verify with "
                "`grep -rn 'def gateway_url' integration_tests/` — exactly "
                "one hit. The agent-runtime `GATEWAY_URL` env is a "
                "**separate surface** from pytest fixtures; production code "
                "an agent writes can reach the gateway sidecar through it, "
                "but that is not a pytest test. See "
                "`docs/architecture/integration-test-trust-boundary.md`.",
                "",
                "Recommended shape: a short `## Primitives` section in the "
                "prose with one row per primitive (name, `file:line`, "
                "execution-context scope). The plan reviewer will run the "
                "Primitive-Existence Audit (criteria §9) and Trust-Boundary "
                "Audit (criteria §10) against this table; both are hard "
                "NACKs when a named primitive has no grep hit or is used "
                "outside its scope.",
                "",
                # ----------------------------------------------------
                # #2137 — slice-DAG planner guidance
                # ----------------------------------------------------
                "## Slice-DAG guidance (#2137)",
                "",
                "The implement-phase pipeline now ships each plan **slice** "
                "(formerly **phase**) as its own stacked PR. The plan you "
                "emit drives that DAG; the planner rules below are mandatory.",
                "",
                "**Yaml key swap**: prefer the canonical ``slices:`` key in "
                "your ``# yaml-tasks`` block (the parser also accepts "
                "``phases:`` for backward compatibility with already-shipped "
                "planner prompts). New plans should use ``slices:``.",
                "",
                "**Slice sizing is the architect's call (#2809).** Slice "
                "count, boundaries, and DAG shape come from the architect's "
                "scaffold — copy them verbatim. ``reviewer_plan`` will hard "
                "NACK ``architect`` on ``slice_size`` when a slice is "
                "oversized for one BRC cycle (judgment-based — see the "
                "reviewer's §11 rubric); do NOT silently re-shape slices "
                "to dodge a size concern. Raise it as NACK pressure on "
                "architect instead (see the surfacing guidance above).",
                "",
                "**Forest constraint (HARD, enforced at plan ingestion)**: "
                "every slice must have at most ONE DAG parent — the "
                "implement-phase pipeline ships every slice as a stacked "
                "PR with exactly one base branch. The architect's scaffold "
                "encodes this via a single-parent ``dependencies`` id "
                "(``slice-<N>``); preserve it.",
                "",
                "**Auto-serialization for would-be multi-parent slices**: "
                "the architect is responsible for serialising would-be "
                "multi-parent slices and populating "
                "``serialized_chain_order`` on the downstream slice. "
                "Preserve that field verbatim from the scaffold.",
                "",
                "**File-overlap ⇒ ordering (HARD — #3046)**: you fill in each "
                "slice's tasks and their ``files_affected``, so you see the "
                "file sets first. If you find yourself assigning the SAME file "
                "to two slices that the architect left unordered (parallel "
                "roots or siblings), do NOT silently proceed — plan ingestion "
                "hard-rejects overlapping slices with no dependency edge, "
                "because their branches fork independently off the shared base "
                "and collide at integration. Raise NACK pressure on the "
                "architect (via the plan prose) to serialise the overlapping "
                "cluster into one ``dependencies`` chain — or to merge the "
                "slices. Do not re-shape the slice DAG yourself.",
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
                "Your judgement is the source of truth. The fallback "
                "heuristic when you have no preference is: cluster "
                "would-be parents by ``files_affected`` Jaccard overlap "
                "(>0.3), then order by descending downstream fan-out.",
                "",
                f"Write your plan to `{draft_path}`.",
                "",
                *_pkg._EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
        # Append role file restriction info so the planner assigns tasks correctly.
        # Pass the pipeline's repo so per-repo role_patterns from
        # repositories.yaml are rendered (#2528) — keeps planner-prompt
        # boundaries in sync with the gateway's push-time enforcement.
        lines.append(_pkg._build_role_restrictions_section(repo=repo or None))
    elif role_value == "risk_analyst":
        lines.extend(
            [
                "**You are dual-role (producer AND reviewer) in this phase "
                "(#2809).** You produce the risk register AND you review "
                "``architect`` and ``task_planner`` through the risk lens — "
                "your NACK blocks plan-phase consensus until the upstream "
                "producer re-proposes addressing the concern. This mirrors "
                "the implement-phase ``tester`` dual-role pattern (#2749); "
                "the *Dual-Role Execution Order* banner in your BRC "
                "preamble is the authoritative ordering — read it first.",
                "",
                "## Producer role (risk register)",
                "",
                "Assess technical risks for the proposed implementation:",
                "",
                "1. Review the architecture analysis from the ARCHITECT agent",
                "2. Identify technical risks (security, performance, compatibility)",
                "3. Research externally when the change involves third-party dependencies — "
                "use WebSearch and WebFetch (when available) to check for known "
                "vulnerabilities, deprecation notices, and compatibility issues. "
                "Skip external research for purely internal changes.",
                "4. Assess impact and likelihood of each risk",
                "5. Propose mitigation strategies and rollback plans",
                "6. Flag areas that need human review",
                "7. **Flag runtime-primitive and trust-boundary risks (see "
                "#2594).** Plans that depend on classes, fixtures, routes, "
                "or env vars which don't exist in the form the plan assumes "
                "— or which exist but only in a different execution context "
                "than the task uses (e.g. unit-test-only `ScriptedProvider` "
                "vs deployed agent pods; `orchestrator_url` fixture defined "
                "only in `integration_tests/local_pipeline/conftest.py` vs "
                "in-sandbox-agent tests) — are a recurring high-impact "
                "failure mode (see #2474). Call these out explicitly so the "
                "plan reviewer can audit them.",
                "",
                f"Write your risk assessment to `{_risk_analyst_output_path}`.",
                "",
                "## Reviewer role (risk lens on architect + task_planner)",
                "",
                "When ``architect`` or ``task_planner`` proposes (their "
                "``CONSENSUS_PROPOSE`` will wake you via the dual-role "
                "augmentation on your producer waits — see the banner), "
                "review their work through the risk lens and emit ACK or "
                "NACK. ``blocking_concerns`` are NACK-shaped: they block "
                "plan-phase consensus and force the upstream producer to "
                "re-propose addressing them.",
                "",
                "Use this verdict shape in your producer artifact "
                "(risk-register JSON) **and** mirror the verdict / "
                "feedback in your ``egg-orch consensus ack`` / "
                "``egg-orch consensus nack`` ``--reason`` body so the "
                "upstream producer can act on it:",
                "",
                "```json",
                "{",
                '  "verdict": "ACK" | "NACK",',
                '  "risks": [...],',
                '  "top_3_risks": [...],',
                '  "blocking_concerns": [...],',
                '  "feedback": "concrete revision instructions for architect / task_planner (empty on ACK)"',
                "}",
                "```",
                "",
                "NACK when a risk is severe enough that shipping the plan "
                "as-proposed would invite a known-class failure (security "
                "regression, data loss, compliance break, runtime-primitive "
                "or trust-boundary mismatch that would surface as an "
                "expensive implement-phase NACK). ACK when risks are real "
                "but mitigated, or low enough that the plan can ship and "
                "the risks belong in the register as forward-looking "
                "notes. Be specific in ``feedback`` — name the file, "
                "the slice, the missing mitigation — so the upstream "
                "producer's re-propose is actionable.",
                "",
                *_pkg._EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
    elif role_value == "simplifier":
        if phase == "plan":
            _upstream = "task_planner"
            _upstream_draft = "the implementation plan"
            _human_path = _plan_human_path
            _essence = (
                "what will be built, the major steps/phases, the test strategy "
                "in brief, and the key risks"
            )
        else:  # refine
            _upstream = "refiner"
            _upstream_draft = "the refine analysis"
            _human_path = _analysis_human_path
            _essence = "the problem, the recommended approach, and the key trade-offs"
        lines.extend(
            [
                "**You are a producer only in this phase.** You produce a "
                f"human-focused companion to {_upstream_draft}. You do NOT "
                f"review **{_upstream}**'s draft, and you issue no ACK or NACK "
                "on it: an internal wake-wire re-invokes you when it proposes "
                "so you know its draft is ready, and consensus never waits on a "
                "verdict from you. The *Execution Order* banner in your BRC "
                "preamble is the authoritative ordering — read it first.",
                "",
                "## Producer role (human-focused companion)",
                "",
                f"Your WORK depends on **{_upstream}**'s draft existing. ORIENT "
                f"now, then start producing only once **{_upstream}** issues "
                "`CONSENSUS_PROPOSE` (the event pump re-invokes you carrying that "
                "proposal). On that invocation:",
                "",
                f"1. Read **{_upstream}**'s draft of {_upstream_draft}.",
                f"2. Write a HUMAN-FOCUSED companion to `{_human_path}`. This is a "
                "simplified, higher-level summary for a **broad audience — "
                "engineers, PMs, and managers** — not a peer review. Capture the "
                f"essence: {_essence}.",
                "",
                "   Rules:",
                "   - **Broad, mixed audience.** Write so a non-engineer "
                "(PM, manager) can follow *what is changing and why it matters*, "
                "while staying accurate enough for an engineer. Explain any "
                "unavoidable technical term in plain language.",
                "   - **No egg-internal jargon.** Do not mention BRC, consensus, "
                "propose/ACK/NACK, slices / slice-DAG, contracts, phases, "
                "`serialized_chain_order`, Jaccard, or agent-role names. Describe "
                "independently-shippable pieces in plain terms if you must "
                "reference them at all.",
                "   - **No implementation minutiae.** No `file:line` references, "
                "no function / struct / field / type names or other code "
                "identifiers, no per-field enumerations. Describe behaviour and "
                "impact, not the code.",
                "   - **This is NOT a review.** Do not critique, score, or gate "
                'the upstream draft. No ACK/NACK language, no "the draft should '
                'commit to …", no "anti-pattern to reject", no constraint '
                "lists. You have no critique to record anywhere — your only "
                "output is this plain-language summary.",
                f"   - **Exactly one file.** Commit ONLY `{_human_path}`. Do "
                "NOT create any other `.egg-state/drafts/` file — no separate "
                "`*-simplifier-*.md` constraints/guardrails/verification "
                "companion. Any review reasoning goes in the BRC channel "
                "(your verdict), never a second persisted document. A "
                "proposal that introduces a second draft is rejected at "
                "propose time.",
                "   - **Much shorter and more digestible** than the upstream "
                "draft — plain prose and short lists, not exhaustive enumeration.",
                "   - **Faithful** — reflect the upstream draft accurately; "
                "introduce no new scope, claims, or recommendations.",
                "",
                f"3. Commit and push `{_human_path}`, then PROPOSE it via "
                "`egg-orch consensus propose`. The companion is **mandatory** — "
                "always write at least a one-paragraph summary; do NOT take the "
                "no-op propose path. That completes your work for this phase.",
                "",
                *_pkg._EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
    else:
        lines.extend(
            [
                f"Execute your role as {role_value} for this phase.",
                "",
            ]
        )

    # Phase restrictions
    _recovery_base_ref = _pkg._resolve_origin_ref(base_branch)
    lines.append("## Phase Restrictions\n")
    if phase == "implement":
        lines.extend(
            [
                "- You CAN push code changes to git (git push)",
                "- You CAN link commits to tasks (egg-contract add-commit)",
                "- You CANNOT push .egg-state/ files (except checkpoints)",
                "- You CANNOT create PRs (the pipeline manages the PR)",
                "",
                "### Push Recovery",
                "",
                "If your push is rejected due to restricted files on the branch, "
                f"create a clean branch from {_recovery_base_ref} and cherry-pick "
                "only your code commits:",
                "```",
                f"git checkout -b egg/<new-branch> {_recovery_base_ref}",
                "git cherry-pick <your-commit-hash>",
                "git push origin egg/<new-branch>",
                "```",
                "Do NOT retry the same push — fix the branch first.",
                "After pushing to the new branch, use `egg-contract add-commit` to "
                "link your commits so the pipeline can track them on the new branch.",
                "",
            ]
        )
    elif phase in ("refine", "plan"):
        lines.extend(
            [
                "- You CAN write to `.egg-state/drafts/` and `.egg-state/agent-outputs/`",
                "- You CAN push these state files to git (git push)",
                "- You CAN create HITL decisions (egg-contract add-decision)",
                "- You CAN create feedback requests (egg-contract add-feedback)",
                "- You CANNOT modify production code (src/, lib/, gateway/, sandbox/, "
                "action/, docs/, tests/, test/)",
                "- You CANNOT modify contracts (.egg-state/contracts/) or CI config (.github/)",
                "- You CANNOT create PRs (gh pr create)",
                "",
                "### Push Recovery",
                "",
                "If your push is rejected due to restricted files on the branch, "
                f"create a clean branch from {_recovery_base_ref} and cherry-pick "
                "only your state file commits:",
                "```",
                f"git checkout -b egg/<new-branch> {_recovery_base_ref}",
                "git cherry-pick <your-commit-hash>",
                "git push origin egg/<new-branch>",
                "```",
                "Do NOT retry the same push — fix the branch first.",
                "After pushing to the new branch, use `egg-contract add-commit` to "
                "link your commits so the pipeline can track them on the new branch.",
                "",
            ]
        )

    # File boundaries (#1431) — surface allowed/blocked patterns so
    # the agent avoids creating files the gateway will reject on push.
    # Pass repo so the rendered patterns match per-repo overrides
    # (#2528) the gateway will enforce on push.
    boundary_section = _pkg._build_file_boundary_section(role_value, repo=repo)
    if boundary_section:
        lines.append(boundary_section)

    # Producer escape hatch (#2529) — tester/documenter are the other
    # two impassing producer roles (coder is handled in the early-return
    # branch above). They need the actionable
    # check_file_restriction / report_impasse guidance so they don't
    # invent workarounds when their assigned task is structurally
    # impossible.
    if role_value in ("tester", "documenter"):
        lines.append(_pkg._build_impasse_escape_hatch_section())

    lines.append("## Phase Completion\n")
    if concurrent:
        lines.extend(
            [
                "When you have completed your primary work:\n",
                "1. Commit all changes",
                '2. Run: `egg-orch signal readiness --state READY --reason "Work complete"`',
                "3. Enter an **event-driven** stay-alive wait (issue #1897). "
                "Do NOT wrap `egg-orch` in a shell `for i in 1..N` loop, "
                "and do NOT `sleep N` — use the server-side blocking primitive:",
                "```bash",
                "egg-orch message wait-loop \\",
                "  --for CONSENSUS_CONFIRMED \\",
                "  --for CONSENSUS_RE_REVIEW \\",
                "  --for OVERSEER_ALERT \\",
                "  --timeout 60",
                "```",
                "`wait-loop` blocks server-side and loops forever until a "
                "NEW matching BRC event arrives (exit 0) or a permanent error "
                "occurs (exit 1).  There is no outer timeout — the wrapper "
                "owns the 0/1 contract.  Events that predate the call "
                "(including your own just-sent CONSENSUS_CONFIRMED) are "
                "skipped (issue #1925); if you need zero-drop semantics "
                "across a send→wait boundary, capture the ID of your "
                "send and pass `--since <id>`.  See "
                "`docs/reference/agent-wait-patterns.md` for the full "
                "exit-code contract and the five anti-patterns to avoid.",
                "4. If `wait-loop` returns with a message that affects your work, "
                "transition back to WORKING, address it, then signal READY again. "
                "**In particular, if you receive a `CONSENSUS_RE_REVIEW` message, "
                "you MUST re-confirm via `egg-orch consensus confirmed` (or "
                "re-review and ACK/NACK if you are a reviewer of the re-proposing "
                "producer). Ignoring this message will stall the pipeline.**",
                "5. **Do NOT exit.** The orchestrator will stop your container when consensus "
                "is reached.",
            ]
        )
    else:
        lines.append(
            "When you have completed your work, ensure everything is committed and exit successfully."
        )

    return "\n".join(lines)
