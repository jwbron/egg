"""reviewer-preparation prompt helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _build_agent_roster(all_roles: list[str], current_role: str, phase: str) -> str:
    """Build a roster of all active agents for the current phase.

    Shows each agent's role, what they do, and what they produce so that
    every agent understands who else is running and what to expect.
    """
    roster_lines = ["### Active Agents in This Phase\n"]
    roster_lines.append(
        "The following agents are running **simultaneously**. "
        "Each must complete their task AND reach CONFIRMED via BRC.\n"
    )
    for role in all_roles:
        desc, artifacts = _pkg._ROLE_DESCRIPTIONS.get(
            role, ("Executes assigned role", "role-specific artifacts")
        )
        marker = " **(you)**" if role == current_role else ""
        roster_lines.append(f"- **{role}**{marker}: {desc}. Produces: {artifacts}.")
    roster_lines.append("")
    return "\n".join(roster_lines)


def _build_reviewer_preparation(
    role_value: str,
    phase: str,
    *,
    branch: str | None = None,
    base_branch: str | None = None,
) -> str:
    """Build proactive preparation instructions for reviewer agents.

    Tells reviewers what to do while waiting for proposals — e.g., reading
    the contract, familiarizing themselves with the codebase, preparing
    review criteria. This avoids idle waiting and produces better reviews.

    Args:
        role_value: The reviewer role (e.g. ``reviewer_code``).
        phase: Pipeline phase name.
        branch: The pipeline's work branch, if any.
        base_branch: The resolved base branch for diff/log commands. Falls
            back to ``main`` when ``None``.
    """
    base_ref = _pkg._resolve_origin_ref(base_branch)

    if phase == "implement":
        if role_value == "reviewer_code":
            return (
                "Start reviewing immediately — do not wait idle for proposals. "
                "(a) Read the contract with `egg-contract show` to understand "
                "what was planned. "
                "(b) Review the issue/PR description for context. "
                "(c) Check for commits on the branch: run "
                f"`git fetch origin && git log --oneline {base_ref}..origin/{branch or '$(git branch --show-current)'}` "
                "and if changes exist, begin reviewing the diff with "
                f"`git diff {base_ref}...HEAD`. "
                "(d) Note existing test patterns and code conventions. "
                "By the time a proposal arrives, you should already have "
                "a thorough understanding of the changes and be ready to "
                "ACK or NACK with specific, detailed feedback. "
                "When reviewing the tester's proposal, check whether tests were "
                "actually executed (look for `tests_run` and `tests_execution_blocked` "
                "in the attestation). If the tester reports `tests_execution_blocked: true`, "
                "this is a blocking concern — NACK unless the limitation is clearly "
                "documented and the tests are syntactically valid. "
                "Also scrutinize low `tests_run` counts relative to change scope — "
                "a multi-file change with only 1 test run warrants investigation. "
                "If a producer has no work in this slice it submits a generic "
                "no-op propose (`no_changes_needed=true`, #3027): the orchestrator "
                "treats that as a non-blocking no-op and will not surface it to "
                "you for review — there is nothing to ACK or NACK, and it does "
                "not block consensus."
            )
        elif role_value == "reviewer_code_holistic":
            return (
                "Start preparing immediately — do not wait idle for proposals. "
                "(a) Read the contract with `egg-contract show` to extract "
                "the primary advertised use case (this is the path you will "
                "walk end-to-end once the producer proposes). "
                "(b) Review the issue / PR description and any doc files "
                "the contract names — collect the doc-claimed behaviours "
                "into a checklist for the symmetry pass. "
                "(c) Identify the producer / consumer module pairs the plan "
                "touches; these are where synthetic-key and silent-fallback "
                "asymmetries hide. "
                "(d) Once commits land "
                f"(`git fetch origin && git log --oneline {base_ref}..origin/{branch or '$(git branch --show-current)'}`), "
                f"skim `git diff {base_ref}...HEAD` once with the whole PR "
                "in mind — do not verify line-by-line; defer that to "
                "`reviewer_code`. Your job is the architectural-coherence "
                "question line-by-line review does not own."
            )
        elif role_value == "reviewer_contract":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "every task and its acceptance criteria, "
                "(b) reviewing the issue description for original requirements, "
                "(c) noting which tasks are marked as must-have vs nice-to-have. "
                "When proposals arrive, you will verify each task's acceptance "
                "criteria is met — prepare a checklist now."
            )
        elif role_value == "tester":
            return (
                "While waiting for the coder's proposal, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "what's being implemented, "
                "(b) identifying edge cases and boundary conditions from the "
                "requirements, "
                "(c) checking the existing test infrastructure (test frameworks, "
                "fixtures, test utilities). "
                "Start writing test scaffolding for known requirements while "
                "waiting — you can finalize once you see the actual implementation."
            )
    elif phase == "plan":
        if role_value == "reviewer_plan":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the issue description to understand the original "
                "request, "
                "(b) exploring the codebase to understand the current architecture "
                "and components that may be affected, "
                "(c) identifying potential risks or constraints the planners "
                "should address. "
                "Form your own mental model of how you would approach this — "
                "then compare against the proposals when they arrive. "
                "\n\n"
                "**#2137 slice-DAG checks (mandatory):** "
                "(1) **Forest-violation NACK** — if the contract was "
                "rejected at plan ingestion with a "
                "``forest_violation`` log discriminator (or the contract's "
                "``plan_review_feedback`` carries a 'Plan ingestion REJECTED' "
                "block), NACK the architect and cite the structured errors "
                "verbatim. Instruct the architect to re-emit the slice "
                "scaffold with ``serialized_chain_order`` populated on the "
                "downstream slice. The SAME NACK applies to a "
                "``slice_overlap_violation`` rejection (#3046 — a 'Plan "
                "ingestion REJECTED: slices touch overlapping files' block): "
                "two or more slices touch the same file with no dependency "
                "ordering, so their branches fork independently off the shared "
                "base and collide at integration. Instruct the architect to "
                "serialise the overlapping cluster into one linear "
                "``dependencies`` chain (or merge the slices) so each later "
                "slice's branch is cut from the earlier one. "
                "(2) **Slice-sizing NACK (hard, judgment-based — #2809)**: "
                "slice composition is owned by the **architect**, not the "
                "task_planner. You ARE empowered and required to hard-NACK "
                "the architect on ``slice_size`` when a slice is oversized "
                "for one BRC cycle. Use judgment — no fixed tasks-per-slice "
                "or LOC budget. NACK when a slice bundles more than ~3 "
                "distinct file-categories, combines deletion-heavy with "
                "new-API-introduction work, would require >3–4 "
                "commit-propose-revise cycles, or contains independent "
                "task groups with no internal dependency. Name the seam in "
                "your NACK so the architect's re-propose is actionable. "
                "See criteria §11 for the full rubric and examples."
                "\n\n"
                "**Human-focused plan companion (the simplifier's "
                "``*-plan-human.md``):** the simplifier produces a simplified, "
                "plain-language companion to the plan for a **broad audience — "
                "engineers, PMs, and managers**. You review it (CRITICAL). "
                "**Read it side-by-side with the full plan** and ACK only when "
                "it (a) faithfully captures the plan's essence, (b) is "
                "materially lighter and more digestible than the full plan — "
                "not a near-copy, (c) is readable by a non-engineer, and (d) "
                "is free of egg-internal jargon (no "
                "BRC/consensus/slice-DAG/contract/role terms). NACK the "
                "**simplifier** (not the task_planner) if it misrepresents the "
                "plan, leaks pipeline jargon, omits a material point, merely "
                "duplicates the full plan, or — critically — reads as a "
                "**review/critique** of the plan rather than a summary of it "
                '(ACK/NACK language, "should commit to", "anti-pattern to '
                'reject", constraint lists) or buries the reader in '
                "implementation detail (`file:line` refs, function/struct/field "
                "names). A missing or empty companion is a NACK — the companion "
                "is mandatory."
            )
    elif phase == "refine":
        if role_value in ("reviewer_refine", "reviewer_agent_design"):
            base = (
                "While waiting for the refiner's proposal, prepare by: "
                "(a) reading the prior review feedback that triggered this "
                "refinement cycle, "
                "(b) checking the current state of the code to understand "
                "what was already implemented, "
                "(c) verifying which review concerns are still outstanding. "
                "When the proposal arrives, focus on whether the specific "
                "feedback items were addressed."
            )
            if role_value == "reviewer_refine":
                base += (
                    "\n\n"
                    "**Human-focused analysis companion (the simplifier's "
                    "``*-analysis-human.md``):** the simplifier produces a "
                    "simplified, plain-language companion to the analysis for a "
                    "**broad audience — engineers, PMs, and managers**. You "
                    "review it (CRITICAL). **Read it side-by-side with the full "
                    "analysis** and ACK only when it (a) faithfully captures the "
                    "analysis's essence, (b) is materially lighter and more "
                    "digestible than the full draft — not a near-copy, (c) is "
                    "readable by a non-engineer, and (d) is free of "
                    "egg-internal jargon. NACK the **simplifier** (not the "
                    "refiner) if it misrepresents the analysis, leaks pipeline "
                    "jargon, omits a material point, merely duplicates the full "
                    "draft, or — critically — reads as a **review/critique** of "
                    "the analysis rather than a summary of it (ACK/NACK "
                    'language, "should commit to", "anti-pattern to reject", '
                    "constraint lists) or buries the reader in implementation "
                    "detail (`file:line` refs, function/struct/field names). A "
                    "missing or empty companion is a NACK — it is mandatory."
                )
            return base
        if role_value == "first_principles_reviewer":
            return (
                "While waiting for the refiner's proposal, prepare your "
                "first-principles pass: (a) read the seed — `egg-contract "
                "show` and the linked issue — and restate, in your own words, "
                "the problem it claims to solve and why; (b) explore the "
                "codebase to test that premise against reality (does the thing "
                "already exist? is the problem already handled? is there a far "
                "simpler path?); (c) form your own view of whether this is the "
                "right direction and what a materially better one would be. "
                "When the refiner proposes, you are checking the *premise and "
                "direction*, not the analysis quality — surface any concrete "
                "redirect as a phase-scoped HITL decision for the operator and "
                "ACK the refiner. Never NACK on first-principles grounds."
            )

    # Generic fallback
    return (
        "While waiting for proposals, read the contract "
        "(`egg-contract show`), explore the codebase for context, "
        "and prepare your review criteria. "
        "Do NOT inspect producer artifacts before proposals arrive."
    )


def _re_review_priming_block(
    *,
    version: int | None = None,
    delta_range: str | None = None,
) -> str:
    """Adversarial re-prime injected at the moment of every re-review.

    Counter-anchors the persistent reviewer against the "verify named
    blockers got fixed" framing that long-lived context naturally
    biases toward (see #2724 post-mortem: slice-1 v2 was ACK'd despite
    the v2 delta introducing a non-executable inline `python3 -c`
    snippet that a downstream GitHub-bot reviewer caught immediately).

    Three design choices worth flagging:

    - **Delta-scoped, not exploration-forcing.** The block tells the
      reviewer to re-read *the delta since their own last review*
      adversarially, not to re-traverse the codebase. The amortized
      exploration from cycle-1 is the feature; re-Reading every
      referenced file on every cycle would throw away BRC's cost
      advantage.
    - **Per-reviewer delta, not a fixed version pair (#2887).** The
      block was originally hardcoded to the v1→v2 transition and took
      no arguments, yet was appended verbatim to every re-review (v3,
      v4, …). On N>2 cycles the stale "audit the v2 delta as a fresh
      reviewer, ignore your v1 NACK history" prose read as "re-audit
      the whole accumulated surface," widening scope each cycle and
      blocking multi-round convergence. The block is now parameterized
      by the current proposal version (``vN`` / its prior ``v(N-1)``)
      and, on per-reviewer ``CONSENSUS_RE_REVIEW`` notices, anchored to
      that reviewer's own ``<last_reviewed_sha>..HEAD`` ``delta_range``
      (resolved orchestrator-side from the reviewer's last-verdicted
      version). When ``delta_range`` is absent (the broadcast
      ``CONSENSUS_PROPOSE`` body, ``to_role=all`` — one text for
      reviewers sitting at different last-reviewed versions) the block
      references the reviewer-self-tracked range from REVIEWER-SYNC.md
      (``git log {last_reviewed_commit}..HEAD --not origin/{base} -p``)
      instead.
    - **Economic framing is explicit.** "Re-reviews are cheap / NACK
      without hesitance" is load-bearing — without it, persistent
      reviewers naturally optimize for convergence (ACK to end the
      cycle) over rigor. The orchestrator absorbs the cost of extra
      cycles; the reviewer should not be carrying it.

    The block is appended to ``CONSENSUS_RE_REVIEW`` message bodies
    (signals.py, both withdrawal/re-propose and push-after-propose
    paths) and to ``CONSENSUS_PROPOSE`` bodies when the producer is
    re-proposing (version > 1, ``changed_artifacts`` set). Reviewers
    who NACK'd the prior version receive ``CONSENSUS_PROPOSE`` rather
    than ``CONSENSUS_RE_REVIEW`` on a re-propose, so both surfaces need
    the re-prime to reach every reviewer.

    Args:
        version: The current (re-proposed) proposal version ``N``. When
            ``None`` (legacy / defensive callers) the block falls back
            to generic "current" / "prior" wording without numbered
            anchors.
        delta_range: A concrete ``<sha>..HEAD`` git range scoping this
            reviewer's mandate-2 audit to the commits landed since their
            own last verdict. Only available on the per-reviewer
            ``CONSENSUS_RE_REVIEW`` path; omitted on the broadcast
            ``CONSENSUS_PROPOSE`` body.
    """
    # Adjective placed before "review"/"verdict" ("Your v6 review" /
    # "Your current review"); and the prior-version qualifier placed
    # before "blockers"/"NACK history" ("named v5 blockers" / "named
    # prior blockers"). Both read naturally with or without a version.
    vN = f"v{version}" if version is not None else "current"
    vNm1 = f"v{version - 1}" if version is not None and version >= 2 else "prior"
    # Mandate-2's delta anchor. On the per-reviewer path we have an
    # authoritative range; on the broadcast path we point at the
    # reviewer-self-tracked range REVIEWER-SYNC.md already defines, so
    # each reviewer scopes to the commits since *their* last review
    # rather than the whole accumulated surface.
    if delta_range:
        delta_clause = (
            f"the delta since your last review (`git log {delta_range} "
            "--not origin/<base> -p` — the commits landed since the "
            "version you last verdicted)"
        )
        delta_short = f"this delta (`{delta_range}`)"
    else:
        # NOTE: `{last_reviewed_commit}` and `{base_branch}` here are
        # *literal* braces, deliberately matching the placeholder names
        # the reviewer agent already learned from REVIEWER-SYNC.md
        # (shared/prompts/REVIEWER-SYNC.md:110) — the agent substitutes
        # them at read-time from its own bookkeeping. Do NOT convert this
        # string to an f-string: there are no Python locals named
        # `last_reviewed_commit` / `base_branch` here, so f-stringifying
        # would raise `NameError` at call time. The per-reviewer branch
        # above uses `<base>` instead because that path embeds a
        # concrete, orchestrator-resolved range — only `<base>` remains
        # for the reviewer to fill in, so the angle-bracket convention
        # makes the (already-resolved vs. still-to-resolve) distinction
        # visible at a glance.
        delta_clause = (
            "the delta since your last review (per REVIEWER-SYNC.md: "
            "`git log {last_reviewed_commit}..HEAD --not "
            "origin/{base_branch} -p` — the commits landed since the "
            "version you last verdicted, NOT the whole accumulated "
            "proposal surface)"
        )
        delta_short = "this delta (the commits since your last review)"
    return (
        "\n\n**Adversarial re-review**\n\n"
        f"**Your {vN} review has TWO equal-weight mandates:**\n\n"
        f"1. **Verify named {vNm1} blockers were addressed** — confirm "
        "the producer fixed what you NACK'd.\n"
        f"2. **Audit {delta_clause} as a fresh reviewer** — ignore your "
        f"{vNm1} NACK history. Read that diff as if you'd never seen the "
        "prior version. Apply your lens (security threat-model, "
        "concurrency races, contract AC, line-by-line bugs, "
        "silent-fallback shapes — whichever your role owns) to the "
        "delta itself, not to whether your previous concerns were "
        "satisfied. **Mandate 2 is bounded to this delta** — it does "
        "NOT ask you to re-traverse the whole accumulated surface from "
        "earlier cycles; that work was amortized when you first "
        "reviewed those commits.\n\n"
        "Both mandates have equal weight. If (1) passes but (2) finds new "
        "issues, you NACK. ACK requires both pass.\n\n"
        "**The named-blockers anchor is a known trap. Every reviewer "
        "lens has a mandate-2 in its own territory** — security has "
        "newly-introduced threat surfaces, concurrency has newly-"
        "introduced races, contract has newly-introduced AC drift, code "
        "has newly-introduced line-by-line bugs. The four issues that "
        "escaped PR #2724 to the GitHub bot were all of code-lens shape "
        "(`${ANSWER}` as bare Python, deprecated `datetime.utcnow()`, "
        "non-atomic write, bare `except: pass`) — the persistent "
        'reviewer correctly answered mandate 1 ("did prior issues get '
        'fixed? yes") and skipped mandate 2 ("does this delta introduce '
        'new issues? actually yes"). The shape generalizes: whatever '
        "your lens, this delta can introduce issues your prior NACK "
        "didn't name. Watching the producer deliver a targeted fix "
        'pulls strongly toward "verify my fix-request landed → ACK." '
        "Recognize the pull and do mandate 2 anyway.\n\n"
        "**How to execute mandate 2:**\n\n"
        "- Read each new hunk as an operator who's about to copy-paste / "
        "run / integrate it. Would this code execute as written? Would "
        "these docs send a copy-paster down a working path?\n"
        "- Apply every rubric pass to the new hunks. New issues outside "
        "the scope of your prior NACK are blocking; your prior NACK does "
        "not bound this re-review.\n"
        "- **Fresh-reviewer simulation.** Before issuing your "
        f"{vN} verdict, ask: would a reviewer who has only seen "
        f"{delta_short} with no NACK history ACK this? If you can't "
        "argue yes from that diff alone, NACK.\n"
        "- **External-bot anchor.** Imagine `egg-reviewer[bot]` reads "
        f"only {delta_short} with no NACK context. What would it flag? "
        "Anything it'd flag, you should NACK first.\n\n"
        f"**Your {vN} verdict must enumerate both halves** so mandate 2 "
        "doesn't silently disappear from the record:\n\n"
        f"- (a) Which {vNm1} blockers you verified-fixed (mandate 1).\n"
        "- (b) What new issues you audited-and-did-not-find (mandate 2). "
        'Name the specific shapes you checked — not "reviewed thoroughly," '
        'but "checked for silent fallbacks, doc-snippet executability, '
        "API-deprecation, atomicity of file writes.\" If you can't "
        "enumerate (b), you haven't done mandate 2.\n\n"
        "**Re-reviews are cheap by design.** Your amortized context means "
        'the work is "read the delta, apply your rubric, decide" — '
        "minutes, not hours. NACK without hesitance; the orchestrator "
        "absorbs cycles. Two NACKs on the same producer where the second "
        "names new findings is the correct trajectory, not "
        "goalpost-moving. The downstream GitHub reviewer should find "
        "nothing in this delta. Anything it catches that lives in this "
        "cycle's diff is a miss attributable to this re-review."
    )


def _build_producer_orientation(
    role_value: str,
    phase: str,
    reviewers: list[str],
    branch: str | None = None,
) -> str:
    """Build orientation instructions for producer agents.

    Tells producers what to research before starting work — understanding
    context, knowing what reviewers will check, and checking existing code
    patterns. This produces higher-quality first proposals and fewer NACKs.

    A producer that orients and finds it has no work in this slice takes the
    generic no-op propose path described in the Producer Lifecycle (#3027) —
    no special orientation text is needed.

    Args:
        role_value: Producer role (e.g. ``coder``).
        phase: Pipeline phase name.
        reviewers: Names of reviewers that will review this producer.
        branch: The pipeline's working branch, used for sync instructions.
    """
    reviewer_awareness = ""
    if reviewers:
        reviewer_names = ", ".join(reviewers)
        reviewer_awareness = (
            f" Your work will be reviewed by **{reviewer_names}** — "
            "keep their review criteria in mind as you work."
        )

    # The simplifier runs in both the refine and plan phases as a PRODUCER
    # ONLY (the human-focused companion). It carries an advisory review edge
    # over the upstream producer purely as the event-pump wake-wire — that is
    # what re-invokes it on the upstream's PROPOSE — but it issues no verdict
    # and never reviews the draft (#3381). Its work depends on the upstream
    # producer's draft existing, so — like the implement-phase tester — it
    # orients up-front and starts producing only once the upstream proposes.
    if role_value == "simplifier":
        if phase == "plan":
            upstream, draft_desc = "task_planner", "the implementation plan"
        else:  # refine
            upstream, draft_desc = "refiner", "the refine analysis"
        sync_note = ""
        if branch:
            sync_note = (
                f" When re-invoked on the PROPOSE, sync your worktree first: "
                f"`git fetch origin && git merge origin/{branch} --no-edit`."
            )
        return (
            f"your WORK depends on **{upstream}**'s draft of {draft_desc} "
            "existing — do NOT write your companion before it is pushed. ORIENT "
            "now (read the contract and the issue/task description so you "
            "understand the subject), then exit; the event pump re-invokes you "
            f"when **{upstream}** issues `CONSENSUS_PROPOSE`, carrying that "
            "proposal in your event payload. On that invocation: read the "
            "upstream draft, then write a simplified, higher-level companion "
            "that captures its essence in plain, jargon-free language for a "
            "broad audience (engineers, PMs, and managers) — a summary, NOT a "
            "review of the draft — and PROPOSE it. That is the whole job: you "
            f"do NOT review **{upstream}**'s draft and you issue no ACK or NACK "
            "on it." + sync_note + reviewer_awareness
        )

    if phase == "implement":
        if role_value == "coder":
            return (
                "read the contract (`egg-contract show`) to understand all tasks "
                "and acceptance criteria. Explore the codebase to find existing "
                "patterns, conventions, and the files you will modify. Check for "
                "existing tests that cover the areas you will change — do not "
                "break them." + reviewer_awareness
            )
        elif role_value == "tester":
            sync_note = ""
            if branch:
                sync_note = (
                    f" Before starting work, sync your worktree: "
                    f"`git fetch origin && git merge origin/{branch} --no-edit`."
                )
            return (
                "read the contract (`egg-contract show`) to understand what is "
                "being implemented. Check the existing test infrastructure — "
                "test frameworks, fixtures, conftest files, and naming conventions. "
                "Identify edge cases from the requirements before writing tests. "
                "**Your mandate is two-fold**: comprehensive regression "
                "coverage AND adversarial probing for bugs the coder missed "
                "— see the *Your Task* → mandate block for the full "
                "instruction (including the failing-test → NACK → HANDOFF "
                "workflow when you catch a coder-side bug). "
                "**Scaffold-first while the coder is producing**: draft test "
                "scaffolding from the plan alone — test file paths from "
                "`tasks[].files`, function signatures from each task's acceptance "
                "criteria, fixture imports, and mock-input scenarios from the YAML. "
                "Leave assertion bodies as TODOs. Do NOT call `wait-loop` for the "
                "coder's CONSENSUS_PROPOSE before drafting these scaffolds — the "
                "scaffold work does not depend on coder output and recovers "
                "downstream-producer time. Your propose-ready iteration should "
                "start at the coder's first commit, not their first propose. "
                "**You MUST propose** even when the slice warrants no new tests "
                "(pure refactor / doc-only / symbol moves with no behavior "
                "change): the BRC consensus blocks until every producer has "
                "proposed. For that case, submit a generic no-op propose "
                "(#3027) — `egg-orch consensus propose --no-changes-needed "
                "--no-changes-reason '<why: e.g. pure refactor, existing tests "
                "cover>'`. It is accepted as a non-blocking no-op (reviewers do "
                "not review or NACK it). Do NOT just heartbeat indefinitely "
                "waiting for test work that isn't there — that deadlocks the "
                "slice." + sync_note + reviewer_awareness
            )
        elif role_value == "documenter":
            sync_note = ""
            if branch:
                sync_note = (
                    f" Before starting work, sync your worktree: "
                    f"`git fetch origin && git merge origin/{branch} --no-edit`."
                )
            return (
                "read the contract (`egg-contract show`) to understand what is "
                "being implemented. Check existing documentation structure — "
                "README files, doc directories, inline documentation patterns. "
                "Identify which docs describe the surfaces this work touches, so "
                "you can fold the resulting state into them as a snapshot of "
                "current behavior once the implementation is complete. "
                "**You MUST propose** even when the slice warrants no doc "
                "updates (pure refactor / test-only / internal-only with no "
                "documented-surface impact): the BRC consensus blocks until "
                "every producer has proposed. For that case, submit a generic "
                "no-op propose (#3027) — `egg-orch consensus propose "
                "--no-changes-needed --no-changes-reason '<why: e.g. no "
                "documented surface impacted by the coder's diff>'`. It is "
                "accepted as a non-blocking no-op (reviewers do not review or "
                "NACK it). Do NOT just heartbeat indefinitely waiting for doc "
                "work that isn't there — that deadlocks the slice." + sync_note + reviewer_awareness
            )
    elif phase == "plan":
        if role_value == "architect":
            return (
                "read the issue/task description carefully. Explore the codebase "
                "to understand the current architecture, component boundaries, "
                "and dependencies. Identify the areas that will be affected by "
                "the proposed changes." + reviewer_awareness
            )
        elif role_value == "task_planner":
            return (
                "read the issue/task description carefully. Review the codebase "
                "structure to understand the scope of work. Break the work into "
                "tasks with clear acceptance criteria that reviewers can verify."
                + reviewer_awareness
            )
        elif role_value == "risk_analyst":
            return (
                "read the issue/task description carefully. Research the affected "
                "areas of the codebase for potential risks — security, "
                "performance, backwards compatibility, and third-party "
                "dependencies." + reviewer_awareness
            )
    elif phase == "refine":
        if role_value == "refiner":
            return (
                "read the prior review feedback carefully. Understand exactly "
                "what concerns were raised and what changes are expected. Check "
                "the current state of the code before making modifications. "
                "When the draft you are refining is an analysis or plan, "
                "surface every runtime-primitive assumption explicitly at the "
                "phase_gate (see #2594) — name each class, function, route, "
                "env var, ConfigMap key, fixture, CLI flag, or decorator the "
                "downstream plan will depend on, with `file:line` evidence "
                "and execution-context scope (in-sandbox-agent vs "
                "trusted-CI-runner vs human-operator). This makes the "
                "plan-phase Primitive-Existence and Trust-Boundary audits "
                "cheap." + reviewer_awareness
            )

    # Generic fallback
    return (
        "read the contract (`egg-contract show`) and explore the codebase "
        "to understand context, patterns, and conventions before starting." + reviewer_awareness
    )
