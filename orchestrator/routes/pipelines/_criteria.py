"""Review-criteria builders for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pre-split barrel. The only patched module
global these reach is ``_read_shared_criteria`` (and ``logger``); both are
reached through ``import routes.pipelines as _pkg`` so
``patch("routes.pipelines._read_shared_criteria")`` keeps intercepting.
"""

from __future__ import annotations

from pathlib import Path  # noqa: F401 -- used by _read_shared_criteria

import routes.pipelines as _pkg  # noqa: E402 -- package barrel for patch seams


def _read_shared_criteria(
    filename: str,
    user_override: str | None = None,
    repo_path: str | None = None,
) -> str | None:
    """Read shared criteria from file, checking user override first.

    Search order:
    1. .egg/<user_override> in the repo (if user_override provided)
    2. shared/prompts/<filename> relative to source tree
    3. /app/prompts/<filename> (Docker container path)

    Returns the file content, or None if no file found (caller uses inline fallback).
    """
    # Check user override first
    if user_override and repo_path:
        override_path = Path(repo_path) / ".egg" / user_override
        if override_path.is_file() and override_path.stat().st_size > 0:
            return override_path.read_text()

    # Try source tree path (development / tests)
    source_path = Path(__file__).parent.parent.parent.parent / "shared" / "prompts" / filename
    if source_path.is_file():
        return source_path.read_text()

    # Try Docker container path (production)
    docker_path = Path("/app/prompts") / filename
    if docker_path.is_file():
        return docker_path.read_text()

    return None


def _get_agent_design_criteria() -> str:
    """Return agent-mode design review criteria."""
    content = _pkg._read_shared_criteria("agent-design-criteria.md")
    if content is not None:
        return content
    _pkg.logger.warning("Shared agent-design-criteria.md not found, using inline fallback")
    return (
        "Flag these **clear** anti-patterns:\n\n"
        "1. **Excessive pre-fetching** — Baking large diffs (10KB+) or full file contents "
        "into prompts instead of letting the agent fetch what it needs\n"
        "2. **Structured output for humans** — Requiring JSON when output goes directly "
        "to humans rather than machines\n"
        "3. **Post-processing pipelines** — Scripts that parse agent output to take actions "
        "the agent could take directly\n"
        "4. **Rigid procedures** — Micromanaging step-by-step procedures when objectives "
        "would suffice\n"
        "5. **Prompt-level security** — Using instructions for constraints that should be "
        "sandbox-enforced\n"
        "6. **Direct LLM API calls outside sandbox** — Calling the Anthropic API from "
        "orchestrator, gateway, or shared code instead of delegating to sandbox containers\n"
        "7. **Direct API calls bypassing the Agent SDK** — Using raw HTTP calls to the "
        "Anthropic API instead of run_agent() (in-sandbox) or build_agent_command() "
        "(orchestrator-spawned containers). Unlike item 6 (scoped to infra code), "
        "this applies everywhere including sandbox code.\n"
        "8. **Hardcoded model identifiers** — Using full model IDs (date-pinned or "
        "version-pinned) instead of short aliases (sonnet, opus, haiku)\n"
    )


def _get_code_review_criteria(repo_path: str | None = None) -> str:
    """Return code review criteria."""
    content = _pkg._read_shared_criteria(
        "code-review-criteria.md",
        user_override="review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    _pkg.logger.warning("Shared code-review-criteria.md not found, using inline fallback")
    return (
        "### Security (highest priority)\n"
        "- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)\n"
        "- Authentication/authorization flaws\n"
        "- Credential exposure, hardcoded secrets\n"
        "- SSRF, open redirects, unsafe deserialization\n\n"
        "### Correctness\n"
        "- Logic errors, off-by-one, boundary conditions\n"
        "- Race conditions, deadlocks, concurrency bugs\n"
        "- Null/undefined handling, missing error paths\n"
        "- Resource leaks (connections, file handles, memory)\n"
        "- End-to-end feature functionality: verify new features work in their "
        "real execution environment\n\n"
        "### Robustness\n"
        "- Missing input validation at trust boundaries\n"
        "- Unhandled exceptions that could crash the system\n"
        "- Missing retry logic for transient failures\n"
        "- Inadequate timeouts for external calls\n\n"
        "### Design\n"
        "- Violations of existing codebase patterns\n"
        "- Breaking changes to public interfaces\n"
        "- Tight coupling that will hinder future changes\n\n"
        "### Severity Classification\n\n"
        "**Blocking** (request changes):\n"
        "- Security vulnerabilities\n"
        "- Non-functional features — the feature's core purpose does not work "
        "end-to-end\n"
        "- Logic errors that produce incorrect results\n"
        "- Breaking changes to existing functionality\n"
        "- Resource leaks or crashes\n"
        "- Pre-existing broken or inconsistent behavior in code the PR "
        "modifies\n\n"
        "**Non-blocking** (suggestions):\n"
        "- Code quality improvements (naming, structure, duplication)\n"
        "- Defense-in-depth additions\n"
        "- Missing edge case handling that doesn't affect the core feature\n"
        "- Documentation gaps\n"
        "- Style or convention deviations not caught by linters\n\n"
        "**Do not dismiss issues as 'not a regression'**: If a PR modifies "
        "code that has existing broken or inconsistent behavior, the issue is "
        "blocking even if the PR didn't introduce it. A PR that adds a new "
        "code path through already-inconsistent logic makes the inconsistency "
        "worse.\n\n"
        "**Beware of false analogies**: When comparing new code to existing "
        "patterns, verify the analogy holds at the execution-model level. "
        "Two features may look structurally similar in config but have "
        "completely different execution paths. If the existing pattern works "
        "via mechanism A but the new code relies on mechanism B that doesn't "
        "exist, the comparison is invalid — classify based on actual "
        "functionality, not superficial similarity.\n\n"
        "### Skip\n\n"
        "- Style issues handled by linters (formatting, import order)\n"
        "- Type annotation completeness (type checkers handle this)\n"
        "- Auto-generated files (migrations, lock files)\n"
        "- `.egg-state/` pipeline artifacts (contracts, drafts, BRC history "
        "— managed by the orchestrator)\n"
    )


def _get_contract_review_criteria(repo_path: str | None = None) -> str:
    """Return contract verification criteria."""
    content = _pkg._read_shared_criteria(
        "contract-review-criteria.md",
        user_override="contract-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    _pkg.logger.warning("Shared contract-review-criteria.md not found, using inline fallback")
    return (
        "### Task Verification\n"
        "For each task in the contract, verify:\n"
        "1. The described functionality is present in the code\n"
        "2. The acceptance criteria for the task is satisfied\n"
        "3. If a commit is linked, verify it relates to the task\n"
        "4. Where applicable, tests cover the new functionality\n\n"
        "### Phase Consistency\n"
        "- All tasks in completed phases are actually implemented\n"
        "- Phase status matches task completion state\n"
        "- No orphaned code exists that isn't covered by any task\n\n"
        "### Acceptance Criteria Verification\n"
        "For each acceptance criterion:\n"
        "1. Examine the implementation to verify it meets the criterion\n"
        "2. Note any gaps in your review\n\n"
        "### Contract Integrity\n"
        "- No implementation changes violate previously verified criteria\n"
        "- New changes don't break existing contract compliance\n"
        "- All required files listed in tasks are present\n"
    )


def _get_refine_review_criteria() -> str:
    """Return review criteria for the dedicated refine reviewer."""
    return (
        "### 1. Problem Understanding\n"
        "- Does the analysis correctly identify the core problem or feature request?\n"
        "- Is the current behavior (if applicable) accurately described?\n"
        "- Are the goals and desired outcomes clear?\n\n"
        "### 2. Research Quality\n"
        "- Has the agent explored the relevant parts of the codebase?\n"
        "- Are existing patterns and conventions identified?\n"
        "- Is the technical context accurate and thorough?\n\n"
        "### 3. Options Analysis\n"
        "- Are the proposed options meaningfully different?\n"
        "- Are trade-offs clearly articulated for each option?\n"
        "- Is the reasoning logical and well-founded?\n\n"
        "### 4. Constraints and Dependencies\n"
        "- Are technical constraints identified (performance, compatibility, etc.)?\n"
        "- Are dependencies on other code or systems noted?\n"
        "- Are potential risks or complications surfaced?\n\n"
        "### 5. Open Questions\n"
        "- Are open questions specific enough for a human to answer?\n"
        "- Do questions address genuine ambiguities?\n"
        "- Are questions actionable?\n"
        "- **Does each question require a human, or could the planner decide it?** "
        "NACK questions that ask about work decomposition / slice-DAG shape / "
        "PR packaging — those belong to the plan phase's HITL gate, not the "
        "refine gate. NACK questions about implementation strategy "
        "(API shape, migration approach, fallback design, detector design) "
        "unless the answer is a fact only the operator knows (product intent, "
        "scope boundary, external commitment, user-visible behavior). Good "
        "refine questions are about *what the problem is* and *what's in/out "
        "of scope*; the planner handles *how to build it*.\n\n"
        "### 6. Recommendation Quality\n"
        "- Is there a clear recommended approach?\n"
        "- Is the recommendation justified with specific reasons?\n"
        "- Does the recommendation align with the analysis findings?\n\n"
        "### 7. HITL Decision Registration & Un-surfaced Decisions (#3390)\n"
        "- Run `egg-contract show` and verify a contract decision or feedback "
        "item exists for every open question in the analysis, and that each "
        "decision-bearing section cites its `cq-N` (the `--format markdown` "
        "output of `egg-contract add-decision` embeds it). Open questions as "
        "bare prose with no registered `cq-N` ⇒ **NACK** — the producer must "
        "register each via `egg-contract add-decision` / "
        "`egg-contract add-feedback` and re-propose. (Deterministic "
        "propose-time checks already validate the producer's *attested* ids; "
        "your job is the judgment half the validators cannot do.)\n"
        "- **Un-surfaced decisions — NACK.** Read the draft for choices it "
        "quietly *commits to* that should be the operator's call — e.g. "
        '"we will drop the legacy filter", a scope narrowing/widening, a '
        "user-visible behavior change, abandoning a stated requirement — "
        "with no registered `cq-N` backing the choice. Consensus must not "
        "close on a draft that bakes in a human-grade decision outside the "
        "HITL channel: the producer either registers the decision (the gate "
        "then surfaces it) or rewrites the draft to remove the unilateral "
        "commitment.\n"
        "- **Calibration — do not over-NACK.** An implementation choice the "
        "planner can make from the analysis (API shape, migration approach, "
        "fallback design, detector shape) is NOT a human-grade decision; do "
        "not force registration of those. The bar is the same as §5: answers "
        "only the operator owns (product intent, scope boundaries, external "
        "commitments, user-visible behavior).\n"
        "- If the ledger is deliberately empty (the producer attested "
        "`no_decisions_rationale`), verify the rationale holds: requirements "
        "genuinely unambiguous, no assumptions made silently. NACK if you "
        "find a hidden operator-grade choice.\n"
        "- **Task-named decisions — NACK an explicit-none ledger (#3462).** "
        "If the task description names decisions as the operator's to make "
        "(or directs that decisions be surfaced as HITL questions), each "
        "must have a registered `cq-N` — even when the draft argues prior "
        'context already resolves it. "Already resolved" is a recommended '
        "disposition to register (recommended option citing the resolving "
        "context), not a reason to skip; a `no_decisions_rationale` "
        "attestation on such a task is a **NACK** regardless of how "
        "defensible the rationale reads.\n\n"
        + _human_companion_review_criteria(
            companion="`*-analysis-human.md`",
            parent="the refine analysis",
            producer="refiner",
        )
    )


def _human_companion_review_criteria(*, companion: str, parent: str, producer: str) -> str:
    """Forcing checklist for verifying the simplifier's human companion.

    Shared by the refine and plan reviewer rubrics so the companion is
    judged at VERDICT time (#3381), not merely mentioned in the reviewer's
    "while waiting" preparation text. The simplifier is a producer-only role
    whose companion is gated CRITICAL by this reviewer; the companion is the
    only artifact with no automated content check, so this reviewer is the
    sole gate on its format. Walk every item before ACKing the **simplifier**
    (this section governs the simplifier's proposal, never the {producer}'s).
    """
    return (
        f"### Human-Focused Companion ({companion} — the simplifier's "
        "proposal)\n"
        f"The simplifier produces {companion}, a plain-language companion to "
        f"{parent} for a **broad audience — engineers, PMs, and managers**. "
        "It is gated CRITICAL by you and has no automated content check, so "
        "you are the only gate on its format. **You must walk this checklist "
        "and answer every item before you ACK the simplifier** (this is a "
        f"separate verdict from your review of the {producer}; NACK the "
        "**simplifier**, never the "
        f"{producer}, for companion defects):\n"
        f"1. **Is it a summary, not a review?** Open {companion} and the full "
        f"{parent} side-by-side. NACK if the companion reads as a "
        "review/critique of the draft rather than a summary of it — any "
        'verdict/scoring framing ("verdict", "what I verified", "I '
        'affirm", "sound", ACK/NACK language), any directives aimed at a '
        'later phase ("the plan should commit to", "don\'t let the plan '
        'inflate", "guardrails", "anti-pattern to reject"), or any '
        "constraint lists. The companion explains the change to a human; it "
        "does not judge it.\n"
        "2. **Is it free of implementation minutiae?** NACK if it contains "
        "`file:line` references (e.g. `foo.go::Bar` / `L209`), function / "
        "struct / field / type names or other code identifiers, or per-field "
        "enumerations. It should describe behaviour and user-visible impact, "
        "not the code.\n"
        "3. **Is it materially lighter than the parent?** NACK if it is a "
        "near-copy or as long/dense as the full draft. It must be "
        "substantially shorter and more digestible — plain prose and short "
        "lists.\n"
        "4. **Is it readable by a non-engineer?** NACK if a PM or manager "
        "could not follow *what is changing and why it matters*, or if it "
        "leaks egg-internal jargon (BRC, consensus, slice-DAG, contract, "
        "phase, agent-role terms).\n"
        f"5. **Is it faithful?** NACK if it misrepresents {parent}, omits a "
        "material point, or introduces new scope/claims.\n"
        "A missing or empty companion is a NACK — it is mandatory.\n"
    )


def _get_first_principles_review_criteria() -> str:
    """Return review criteria for the adversarial first-principles reviewer.

    The escalation instructions interpolate the accept-path's sentinel option
    labels from ``routes.decisions`` so the labels the agent writes (here) and
    the labels the resolve hook matches stay a single source of truth — they
    cannot drift. Lazy import avoids a module-load cycle.
    """
    from routes.decisions import (
        FIRST_PRINCIPLES_ADOPT_OPTION,
        FIRST_PRINCIPLES_CANCEL_OPTION,
        FIRST_PRINCIPLES_PROCEED_OPTION,
    )

    return (
        "You are the **first-principles reviewer**. Your subject is the "
        "pipeline's **seed** — the operator's task statement (run "
        "`egg-contract show` and read `task_description`, plus the linked "
        "issue) — and the **direction** the refiner's analysis is taking. You "
        "judge whether the *premise is sound and the direction is "
        "appropriate*, NOT the quality of the analysis — that is "
        "`reviewer_refine`'s job, so do not duplicate it.\n\n"
        "### 1. Interrogate the premise\n"
        "- Is the stated problem real, and is solving it worth the work?\n"
        "- Is the premise contradicted by what's actually in the codebase — "
        "the thing it proposes to build already exists, or the problem is "
        "already handled?\n"
        "- Will the stated direction actually achieve the stated goal, or does "
        "it solve something adjacent?\n\n"
        "### 2. Surface significant redirects (where warranted)\n"
        "Raise a redirect only when you can name a concrete, evidence-backed "
        "alternative — never a vague 'have you considered'. Valid redirects:\n"
        "- A **materially simpler path** that achieves the same goal.\n"
        "- A **fundamentally different approach** that is better on the "
        "merits.\n"
        "- A **scope change** — widen it if the seed under-reaches the real "
        "goal, narrow it if it over-reaches.\n"
        "- **Don't build it** — the work is unnecessary, already solved, or "
        "solves a non-problem.\n"
        "Back each redirect with evidence: a codebase fact (`file:line`), the "
        "seed's own stated goal, or a specific contradiction. Raising more "
        "than one is fine — it is acceptable to be relatively noisy — but "
        "consolidate related concerns and hold every one to the "
        "concrete-and-evidenced bar.\n\n"
        "### 3. What NOT to raise (stay in your lane)\n"
        "- Analysis-quality issues (research depth, option trade-offs, "
        "completeness) — `reviewer_refine` owns those.\n"
        "- Work decomposition, slice-DAG shape, PR packaging, or "
        "implementation strategy (API shape, migration approach) — those "
        "belong to the plan phase and the planner.\n"
        "- Taste, stylistic preference, or 'did you consider X' with no "
        "concrete better alternative.\n"
        "If the premise and direction are sound, say so briefly and ACK — a "
        "clean pass is a common and correct outcome. Do not manufacture an "
        "objection to look diligent.\n\n"
        "### 4. How to act — escalate, never NACK\n"
        "- **Never NACK the refiner on first-principles grounds.** A NACK only "
        "re-runs the refiner, which cannot change the operator-owned seed; "
        "premise and direction are the operator's call, not the refiner's to "
        "fix.\n"
        "- When you have a redirect, **file one phase-scoped HITL decision** "
        "via the `mcp__sdlc__register_open_question` tool so the operator can "
        "act on it with one click (the **accept-path**). Pass these args:\n"
        '  - `phase`: `"refine"`.\n'
        "  - `question`: state the concern, then the concrete redirect and "
        "why (operator-facing prose).\n"
        "  - `options`: these EXACT labels, in this order — do NOT paraphrase, "
        "the orchestrator matches them verbatim to drive the accept-path: "
        f'`["{FIRST_PRINCIPLES_ADOPT_OPTION}", '
        f'"{FIRST_PRINCIPLES_PROCEED_OPTION}", '
        f'"{FIRST_PRINCIPLES_CANCEL_OPTION}"]`.\n'
        "  - `redirect_seed`: the FULL rewritten seed — the complete "
        "`task_description` as it should read if the operator adopts your "
        "redirect (not a diff, not just the objection). This rides the same "
        "RPC that files the decision, so the orchestrator can read it back "
        "directly; do NOT write it to a free-standing file (a reviewer "
        "worktree has no path to carry one to the orchestrator).\n"
        "  On the operator's choice the orchestrator will: **adopt** → rewrite "
        "the seed to your `redirect_seed` and re-run the refine phase against "
        "it; **proceed** → leave the direction unchanged; **don't build** → "
        "cancel the pipeline. If you have only an objection with no concrete "
        "alternative direction, you do not have a redirect — do not file the "
        "decision (omit `redirect_seed`).\n"
        "- Then **ACK the refiner**: your first-principles pass is done and "
        "any concerns are filed for the operator. Your ACK does not endorse "
        "the direction — it records that you reviewed it; the open decision "
        "independently holds the refine→plan gate until the operator resolves "
        "it.\n"
    )


def _get_plan_review_criteria() -> str:
    """Return review criteria for the dedicated plan reviewer."""
    return (
        "### 1. Alignment with Analysis\n"
        "- Does the plan implement the recommended approach from the analysis?\n"
        "- If the plan deviates from the analysis, is the reason explained?\n"
        "- Are all requirements from the analysis addressed?\n\n"
        "### 2. Task Breakdown\n"
        "- Are tasks discrete, actionable, and properly scoped?\n"
        "- Is each task small enough to implement in a single pass?\n"
        "- Are task boundaries clear (no overlapping responsibilities)?\n\n"
        "### 3. Acceptance Criteria\n"
        "- Does each task have clear, testable acceptance criteria?\n"
        "- Are criteria specific enough to verify completion?\n"
        "- Do criteria cover both happy path and edge cases?\n\n"
        "### 4. Dependency Ordering\n"
        "- Are task dependencies correctly identified?\n"
        "- Is the ordering logical (foundations before features)?\n"
        "- Are there opportunities for parallelism that are missed?\n\n"
        "### 5. Risk Assessment\n"
        "- Are technical risks identified (security, performance, compatibility)?\n"
        "- Are mitigation strategies concrete and actionable?\n"
        "- Is the rollback plan realistic?\n\n"
        "### 6. Test Strategy\n"
        "- Is the test strategy appropriate for the scope of changes?\n"
        "- Are both unit and integration tests considered?\n"
        "- Are test scenarios aligned with acceptance criteria?\n\n"
        "### 7. Completeness\n"
        "- Does the plan cover all aspects of the original request?\n"
        "- Are documentation updates included where needed?\n"
        "- Are there any obvious gaps or missing tasks?\n\n"
        "### 8. Task Role ↔ Files Alignment (deterministic, see #2527)\n"
        "- Task role↔files alignment is enforced **orchestrator-side** at "
        "`CONSENSUS_PROPOSE`: a planner proposal whose task `role:` "
        "assignments cannot push their `files:` (per "
        "`shared/egg_restrictions/patterns.py`, the same blocklist the "
        "gateway uses) is rejected with HTTP 400 before the proposal "
        "reaches you. By the time you act on a `CONSENSUS_PROPOSE`, "
        "structural role↔files alignment is therefore already validated — "
        "no manual check is required for this dimension.\n"
        "- If you want belt-and-suspenders verification, you can run the "
        "validator yourself against the proposed plan: "
        '`python3 -c "from egg_contracts.plan_parser import parse_plan_file, '
        "validate_task_role_alignment as v; r = parse_plan_file('<plan-path>'); "
        "print('\\n'.join(v(r.to_contract_slices())))\"`. "
        "Errors here would predict a push-time `403 "
        "restricted_path_modified` — NACK the planner and quote the "
        "structured errors verbatim if any surface.\n\n"
        "### 9. Primitive-Existence Audit (hard NACK, see #2594)\n"
        "Plans are cheap to NACK at this phase and expensive to NACK "
        "at implement-phase (8+ pod spawns per slice, ~60–90 min "
        "wall clock per implement cycle). For #2474, a single "
        "`grep -rn ScriptedProvider sandbox/ k8s/ orchestrator/` "
        "returning zero hits would have prevented ~10.7 h of "
        "compute. Do that grep **now**.\n\n"
        "For every primitive the plan names — class, function, HTTP "
        "route, env var, ConfigMap key, test fixture, CLI flag, "
        "decorator — produce a small evidence table in your review "
        "document. Example shape:\n\n"
        "| primitive | kind | grep | result |\n"
        "|-----------|------|------|--------|\n"
        "| `ScriptedProvider` | class | `grep -rn 'class ScriptedProvider' sandbox/ k8s/ orchestrator/` | 0 hits → NACK |\n"
        "| `orchestrator_url` fixture | fixture | `grep -rn 'def orchestrator_url' integration_tests/` | `integration_tests/local_pipeline/conftest.py:NN` — sibling, not parent (see §10) |\n\n"
        "Prescribed greps by kind:\n"
        "- **class / function**: `grep -rn '<NAME>' <relevant dirs>` "
        "finds at least one definition site.\n"
        "- **HTTP route**: blueprint registers the path + method the "
        "plan uses (search `orchestrator/routes/` and `gateway/`).\n"
        "- **env var / ConfigMap key**: a consumer the plan assumes "
        "actually reads it.\n"
        "- **test fixture**: defined in a conftest **reachable from "
        "the test's directory** (parent vs sibling matters — see §10).\n"
        "- **CLI flag**: parser registers it.\n\n"
        "**NACK rule**: any named primitive whose grep returns zero "
        "hits in the directories the plan implies is a hard NACK. "
        "Quote the failed command verbatim in your verdict so the "
        "planner can re-draft. If the primitive exists but in a "
        "different form than the plan assumes (different module, "
        "different signature, different scope — e.g. unit-test-only "
        "vs deployed-pod), NACK and quote the actual `file:line`.\n\n"
        "**Exception — `(NEW — task TASK-X-Y)` annotations.** Plans "
        "introduce new primitives by design; the producer prompt "
        "tells the planner to mark such primitives "
        "`(NEW — task TASK-X-Y)` so the audit doesn't false-NACK "
        "the very task that creates them. When you see this "
        "annotation: **do not NACK on missing-grep evidence**. "
        "Instead verify that the referenced task's acceptance "
        "criteria genuinely create the primitive in the form the "
        "plan uses (right kind, right module, right scope), and "
        "that downstream tasks consuming the primitive depend on "
        "the creating task. NACK only if the creating task does "
        "not actually produce the primitive or the dependency "
        "ordering is wrong.\n\n"
        "### 10. Trust-Boundary Audit (hard NACK, see #2594)\n"
        "Some primitives exist but are not available in the "
        "execution context the plan assumes. The canonical example: "
        "`ScriptedProvider` is a unit-test-only fake; deployed agent "
        "pods (`sandbox/`) run the real provider, so a k3s "
        "integration test cannot inject canned LLM trajectories "
        "into a deployed pod without separate infra work. The "
        "`integration_tests/` fixture layout encodes a parallel "
        "distinction along the **pytest-fixture** axis: the "
        "`gateway_url` and `orchestrator_url` fixtures are both "
        "defined only in `integration_tests/local_pipeline/conftest.py` "
        "and both transitively depend on `local_pipeline_stack`, "
        "which `pytest.skip`s when kubectl is unavailable. The "
        "parent `integration_tests/conftest.py` exposes `egg_stack` "
        "(also kubectl-gated) — `egg_stack.gateway_url` is an "
        "attribute on the `EggStack` dataclass, not a standalone "
        "fixture. There is no `in-sandbox-agent`-runnable pytest "
        "fixture in `integration_tests/` today; the in-sandbox-agent "
        "tier reaches the gateway via the `GATEWAY_URL` env at "
        "agent runtime, which is a separate surface from pytest "
        "fixtures.\n\n"
        "For each task that interacts with the orchestrator, "
        "gateway, or k3s cluster, identify the **execution context** "
        "and confirm the named primitives are available in that "
        "context:\n\n"
        "- **in-sandbox-agent** — driven by an egg agent pod. "
        "Production code the agent writes reaches gateway-mediated "
        "routes via the `GATEWAY_URL` env var. No `orchestrator_url`. "
        "No lifecycle-secret-gated routes. Cannot inject "
        "ScriptedProvider into a pod. **No pytest fixture in "
        "`integration_tests/` resolves here today** — every fixture "
        "is kubectl-gated and skips in the sandbox.\n"
        "- **trusted-CI-runner** — driven by pytest from outside "
        "the cluster (CI / dev machine running `make test` against "
        "k3s). Sees every pytest fixture in `integration_tests/` "
        "(parent and `local_pipeline/`), including `gateway_url`, "
        "`orchestrator_url`, lifecycle-secret-gated routes, and "
        "`kubectl` pod-log access. Test files live under "
        "`integration_tests/` (gateway-only) or "
        "`integration_tests/local_pipeline/` (orchestrator-scoped).\n"
        "- **human-operator** — manual / `egg-orch` CLI. Not a "
        "test-execution context; flag any task that implicitly "
        "requires this.\n\n"
        "See "
        "`docs/architecture/integration-test-trust-boundary.md` "
        "for the authoritative tier → fixture / route mapping.\n\n"
        "**NACK rule**: if a task's named primitives are not "
        "available in its declared (or implied) execution context, "
        "NACK and name the specific mismatch. Common forms — NACK "
        "each one:\n\n"
        '- "task TASK-1-8 writes an in-sandbox-agent pytest test '
        "depending on the `gateway_url` fixture, but that fixture is "
        '`trusted-CI-runner`-only and skips when kubectl is absent"\n'
        '- "task TASK-2-3 places a test that imports '
        "`orchestrator_url` under `integration_tests/foo/` — pytest "
        "resolves fixtures lexically from the nearest conftest "
        "upward, so a sibling of `local_pipeline/` cannot see that "
        'fixture and the test fails at collection time"\n'
        '- "task TASK-3-1 calls a `@require_lifecycle_secret` route '
        "from an `in-sandbox-agent`-context handler — "
        "`EGG_LIFECYCLE_SECRET` is not present in sandbox pods, so "
        'the route returns 403"\n'
        '- "task TASK-4-2 references `ScriptedProvider` from '
        "`sandbox/` (or any deployed-pod path) — it is a unit-test "
        "double under `shared/tests/`, not a runtime-injectable "
        'provider"\n\n'
        "### 11. Slice Sizing (hard NACK, judgment-based — see #2809)\n"
        "Slice sizing is owned by the **architect**, not the "
        "task_planner. ``reviewer_plan`` is empowered AND required to "
        "hard-NACK the architect when a slice is oversized for one "
        "BRC cycle. This is a separate rubric key from the slice-DAG "
        "shape checks so the NACK is unambiguously routed to the "
        "architect for slice re-shaping (re-spawn ``architect`` with "
        "the subdivision feedback).\n\n"
        "**No fixed tasks-per-slice budget.** Use judgment. NACK when "
        "any of the following holds:\n\n"
        "- A single slice touches **more than ~3 distinct "
        "file-categories** (e.g. orchestrator + gateway + schema + "
        "tests + docs all in one slice probably wants subdivision).\n"
        "- A single slice combines **deletion-heavy work** with "
        "**new-API-introduction work** — these usually want different "
        "review attention and ship better as separate slices.\n"
        "- A single slice would require the implementing producer to "
        "**commit-propose-revise more than 3–4 times** to converge "
        "(typical signal: many independent commit clusters with "
        "different reviewer surfaces).\n"
        "- A single slice contains **independent task groups with no "
        "internal dependency** — natural seams for parallel "
        "sub-slices.\n\n"
        "**NACK format**: name the seam where subdivision is "
        "appropriate so the architect's re-propose is actionable. "
        "Examples:\n\n"
        '- "slice-1 bundles gateway allowlist edits, orchestrator '
        "route handlers, and shared/egg_contracts schema changes — "
        "three distinct file-categories with different reviewer "
        "surfaces. Subdivide along the gateway / orchestrator "
        '/ schema seam."\n'
        '- "slice-2 bundles ~600 LOC of removals across "'
        "orchestrator/* with ~200 LOC of new gateway-Jira routes — "
        "deletion-heavy + new-API in one cycle. Ship the removals "
        'as one slice and the new routes as a downstream slice."\n'
        '- "slice-3 contains 9 tasks across 4 independent feature '
        "areas (search, profile, settings, notifications) with no "
        "cross-area dependency — subdivide into one slice per "
        'area."\n\n'
        "The architect re-proposes with the subdivision applied (the "
        "existing BRC re-review loop handles convergence). "
        "task_planner re-consumes the revised "
        "``architect-slices.yaml`` scaffold on the next BRC cycle. "
        "**Refiner / operator can override sizing concerns** if there "
        "is a deliberate reason to ship a large slice (e.g. atomic "
        "schema migration that cannot be split safely) — in that "
        "case the architect should cite the override in the analysis "
        "and the reviewer can ACK once the rationale is on the "
        "record.\n\n"
        "### 12. Slice File-Overlap Ordering (deterministic hard NACK — see #3046)\n"
        "Complements §11. When slices are subdivided, any two that touch "
        "the **same file** must be **ordered** along one dependency chain — "
        "one a transitive ``dependencies`` ancestor of the other — never "
        "left as parallel roots or siblings. The implement phase cuts each "
        "slice's integration branch off its dependency parent (roots off "
        "``work``), so two overlapping slices with no edge between them fork "
        "independently off the shared base and their edits to the shared "
        "file collide at integration (a guaranteed modify/delete conflict — "
        "the #3023 incident, where three slices all touched "
        "``consensus_wrapper.py``, one deleting it).\n"
        "This is enforced **orchestrator-side at plan ingestion**: an "
        "overlapping-but-unordered DAG is rejected before the slices are "
        "written to the contract, surfacing as a ``slice_overlap_violation`` "
        "discriminator (or a 'Plan ingestion REJECTED: slices touch "
        "overlapping files' block on ``plan_review_feedback``). When you see "
        "it, NACK the **architect** and quote the structured errors "
        "verbatim; instruct it to serialise the overlapping cluster into one "
        "linear ``dependencies`` chain — a slice that deletes/retires a file "
        "depends on every slice that modifies it — or to merge the slices. "
        "Disjoint slices stay parallel so they still run concurrently.\n"
        "Belt-and-suspenders self-check: "
        '`python3 -c "from egg_contracts.plan_parser import parse_plan_file, '
        "validate_slice_file_overlap as v; r = parse_plan_file('<plan-path>'); "
        "print('\\n'.join(v(r.to_contract_slices())))\"`.\n\n"
        "### 13. Test Co-location (hard NACK — see #3411)\n"
        "Complements §12 on the test dimension. When a slice removes, "
        "renames, or rewrites code, the tests exercising that code must be "
        "updated, removed, or skip-guarded **in the same slice** — never in "
        "a later one. Every cumulative slice tip must be independently "
        "green: the per-slice green gate (#3398) executes the repo's "
        "checks at the slice tip before opening the PR and blocks while "
        "any check is red, so a plan that parks test obsolescence in a "
        "later slice guarantees gate blocks and repair-loop churn on "
        "slices whose only sin is plan topology (the #3280 stack shipped "
        "a 46-failure window across slices 3–4 exactly this way: slice-3 "
        "removed ``spawn_overseer_*`` from the spawner, the tests "
        "exercising them were only touched in slice-5).\n"
        "For each slice whose tasks remove or rename symbols, check: do "
        "the test files that statically reference those symbols appear in "
        "that slice's task ``files:`` (or a ``dependencies`` ancestor's)? "
        "If they appear only in a LATER slice — or nowhere — NACK the "
        "**architect** (slice shape is architect-owned, #2809), naming "
        "the code files, the referencing test files, and the slice each "
        "currently sits in, so the re-propose moves the test updates into "
        "the removing slice.\n"
        "Belt-and-suspenders self-check (repos shipping the changeset-"
        "aware selector; this repo does): `python3 "
        "scripts/select_tests/__main__.py --impacted-tests <file>...` "
        "prints every test file that transitively imports the named files "
        "— the same import graph `make test` narrowing uses. Exit 2 means "
        "the closure could not be computed: fall back to grepping the "
        "removed symbols in the test trees, and never read empty output "
        "on exit 2 as 'no impacted tests'.\n\n"
        "### 14. HITL Decision Registration & Un-surfaced Decisions (#3390)\n"
        "- Run `egg-contract show` and check the plan-phase decision ledger: "
        "every plan-phase open question must be a registered contract "
        "decision (`cq-N`), and the plan draft must cite the id where the "
        "question is raised. A plan-grade question living only in prose ⇒ "
        "**NACK** the producer that owns it (task_planner for the plan "
        "draft, architect for slice-shape questions, risk_analyst for "
        "risk-acceptance questions).\n"
        "- **Un-surfaced decisions — NACK.** A plan that silently commits to "
        "a choice only the operator owns — dropping a requirement, changing "
        "user-visible behavior, accepting a risk the operator never saw, "
        "de-scoping acceptance criteria — without a registered `cq-N` bakes "
        "a human-grade decision into the pipeline outside the HITL channel. "
        "NACK: the producer registers the decision or removes the "
        "unilateral commitment.\n"
        "- **Calibration — do not over-NACK.** Design calls the plan phase "
        "legitimately owns (task decomposition, API shape, migration "
        "approach, slice ordering within the architect's constraints) are "
        "NOT operator decisions — do not force registration of those. The "
        "bar is answers only the operator owns (product intent, scope "
        "boundaries, external commitments, user-visible behavior).\n"
        "- A deliberately empty ledger arrives as a producer's "
        "`no_decisions_rationale` attestation — verify it holds; NACK if "
        "the plan hides an operator-grade choice.\n"
        "- **Task-named decisions — NACK an explicit-none ledger (#3462).** "
        "If the task description or refine analysis names decisions as the "
        "operator's to make (or directs that decisions be surfaced as HITL "
        "questions), each must have a registered `cq-N` — even when the "
        'plan argues prior context already resolves it. "Already '
        'resolved" is a recommended disposition to register (recommended '
        "option citing the resolving context), not a reason to skip; a "
        "`no_decisions_rationale` attestation on such a task is a **NACK** "
        "regardless of how defensible the rationale reads.\n\n"
        + _human_companion_review_criteria(
            companion="`*-plan-human.md`",
            parent="the implementation plan",
            producer="task_planner",
        )
    )


def _get_security_review_criteria(repo_path: str | None = None) -> str:
    """Return security-lens review criteria (issue #1965).

    The shared file inherits from ``code-review-criteria.md`` and adds
    lens-specific rules (cross-file allowlist mismatches,
    handler-vs-validator path mismatches, info-disclosure / authz bypass,
    uncommitted-artifact mismatches, secret leakage, OWASP cross-file
    patterns). Falls back to a short inline placeholder when the shared
    file isn't available.
    """
    content = _pkg._read_shared_criteria(
        "security-review-criteria.md",
        user_override="security-review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    _pkg.logger.warning("Shared security-review-criteria.md not found, using inline fallback")
    return (
        "Inherits from `code-review-criteria.md`; only lens-specific rules "
        "below override or extend it.\n\n"
        "### Security lens (focus areas)\n"
        "- **Cross-file allowlist mismatch** — handler in one file references "
        "a check defined / extended in a different file (the PR #1964 "
        "`^project$` pattern).\n"
        "- **Handler-vs-validator path mismatch** — verify the validator's "
        "regex / allowlist actually covers every code path the handler "
        "reaches.\n"
        "- Information-disclosure and authorization-bypass patterns at "
        "trust boundaries.\n"
        "- Uncommitted-artifact / Dockerfile-symlink mismatches (the PR "
        "#1964 `sandbox/scripts/jira` pattern).\n"
        "- Secret leakage via logs, error text, environment dumps, or "
        "version-controlled config.\n"
        "- OWASP top-10 patterns spanning more than one changed file.\n"
    )


def _get_code_review_holistic_criteria(repo_path: str | None = None) -> str:
    """Return holistic-lens review criteria (issue #2126).

    The shared file inherits from ``code-review-criteria.md`` and adds
    holistic-lens rules (end-to-end use-case walk, doc↔code symmetry,
    synthetic-key / sentinel cross-module audit, silent-fallback hunt).
    """
    content = _pkg._read_shared_criteria(
        "code-review-holistic-criteria.md",
        user_override="code-review-holistic-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    _pkg.logger.warning("Shared code-review-holistic-criteria.md not found, using inline fallback")
    return (
        "Inherits from `code-review-criteria.md`; only holistic-lens rules "
        "below override or extend it.\n\n"
        "### Holistic lens (focus areas)\n"
        "- Walk the primary advertised use case end-to-end across the "
        "full diff. NACK silent dead-ends like the `__checkout__` bug "
        "on PR #2105.\n"
        "- Cross-check doc-claimed behaviour against what the code does. "
        "NACK doc-claimed inference / migration paths that do not exist.\n"
        "- Audit synthetic keys, sentinels, and magic values for "
        "cross-module agreement.\n"
        "- Hunt silent fallbacks that swallow operator-visible "
        "misconfiguration.\n"
        "- Defer line-by-line correctness to `reviewer_code`.\n"
    )


def _get_concurrency_review_criteria(repo_path: str | None = None) -> str:
    """Return concurrency-lens review criteria (issue #1965).

    The shared file inherits from ``code-review-criteria.md`` and adds
    lens-specific rules (race conditions, deadlocks, shared-state
    mutation, async-context leakage, retry storms, resource-cleanup
    ordering, BRC-protocol invariants).
    """
    content = _pkg._read_shared_criteria(
        "concurrency-review-criteria.md",
        user_override="concurrency-review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    _pkg.logger.warning("Shared concurrency-review-criteria.md not found, using inline fallback")
    return (
        "Inherits from `code-review-criteria.md`; only lens-specific rules "
        "below override or extend it.\n\n"
        "### Concurrency lens (focus areas)\n"
        "- Race conditions and deadlocks.\n"
        "- Shared-state mutation without proper synchronization.\n"
        "- Async-context leakage and retry-storm patterns.\n"
        "- Resource-cleanup ordering bugs.\n"
        "- BRC-protocol invariants (send→wait ordering, cursor threading "
        "per #1925, heartbeat-stall windows per #2012).\n"
    )


def _get_review_criteria_for_type(
    reviewer_type: str, phase: str, repo_path: str | None = None
) -> str:
    """Dispatch to the correct criteria function based on reviewer type."""
    if reviewer_type == "agent-design":
        return _get_agent_design_criteria()
    elif reviewer_type == "code":
        return _get_code_review_criteria(repo_path=repo_path)
    elif reviewer_type == "code-holistic":
        return _get_code_review_holistic_criteria(repo_path=repo_path)
    elif reviewer_type == "contract":
        return _get_contract_review_criteria(repo_path=repo_path)
    elif reviewer_type == "refine":
        return _get_refine_review_criteria()
    elif reviewer_type == "first-principles-reviewer":
        return _get_first_principles_review_criteria()
    elif reviewer_type == "plan":
        return _get_plan_review_criteria()
    elif reviewer_type == "security":
        return _get_security_review_criteria(repo_path=repo_path)
    elif reviewer_type == "concurrency":
        return _get_concurrency_review_criteria(repo_path=repo_path)
    else:
        raise ValueError(f"Unknown reviewer type: {reviewer_type}")


# --- Risk-router review stance (#3523 S6 / task-6-2) -------------------------
#
# The deterministic risk router (:mod:`risk_router`) maps a slice's risk tier
# to an optional review *stance* — precision-first framing on trivial tiers
# (favour fewer, high-confidence findings), recall-first on high tiers (favour
# coverage: a missed bug ships). This mirrors the /review skill's stance
# progression (precision at medium -> "PLAUSIBLE by default" at high). The two
# framings live HERE, in code — deliberately NOT in the shared
# ``shared/prompts/*-criteria.md`` files — so slice-1's prompt-only criteria
# stay independent of this wiring (task-6-2 constraint: "no shared/prompts file
# is edited by this slice").
#
# It is applied by ONE conditional in :func:`_get_reviewer_scope_preamble`, and
# only under the ``on`` arm of the shared ``EGG_RISK_ROUTER`` flag: ``off`` /
# ``log`` leave every reviewer prompt byte-identical to legacy. The prompt
# bodies are never forked — the stance is a short tail appended to the existing
# scope preamble.

_STANCE_PRECISION_FIRST = (
    "\n\n**Review stance: precision-first (low-risk slice).** This slice routed "
    "to a low risk tier, so favour precision over recall: report only findings "
    "you can state a concrete failing scenario for, and prefer a clean, concise "
    "pass over speculative flags. Do not manufacture concerns to look diligent — "
    "a brief, correct approval is the right outcome when the change is sound."
)

_STANCE_RECALL_FIRST = (
    "\n\n**Review stance: recall-first (high-risk slice).** This slice routed to "
    "a high risk tier, so favour recall over precision: a missed bug here is far "
    "more costly than an extra advisory. Pass every candidate with a nameable "
    "failure scenario through rather than silently dropping half-believed ones; "
    "keep a plausible-but-unconfirmed concern as advisory instead of dropping "
    "it, and drop a claim only when you can show it is wrong against the code."
)


def _review_stance_framing(
    changed_files: object | None,
    repo_path: str | None = None,
) -> str:
    """Router-selected stance framing tail, or ``""`` (the single conditional).

    Returns the precision- or recall-first framing chosen by the router's tier
    for ``changed_files`` — but ONLY under ``EGG_RISK_ROUTER=on``. In ``off`` /
    ``log`` mode, or when no changed-file set is threaded, or when the tier maps
    to no stance (the neutral middle tier), or when the config fails to load
    (fail-open), it returns ``""`` so the reviewer prompt is unchanged.
    """
    if changed_files is None:
        return ""
    from review_graph import resolve_risk_decision, risk_router_mode

    if risk_router_mode() != "on":
        return ""
    decision = resolve_risk_decision(changed_files, repo_root=repo_path)
    if decision is None or decision.stance is None:
        return ""
    from risk_router import ReviewStance

    if decision.stance == ReviewStance.PRECISION_FIRST:
        return _STANCE_PRECISION_FIRST
    if decision.stance == ReviewStance.RECALL_FIRST:
        return _STANCE_RECALL_FIRST
    return ""


def _get_reviewer_scope_preamble(
    reviewer_type: str,
    phase: str,
    *,
    changed_files: object | None = None,
    repo_path: str | None = None,
) -> str:
    """Return a scope preamble that tells the reviewer what to focus on.

    When a caller threads the slice's ``changed_files`` and ``EGG_RISK_ROUTER``
    is ``on``, a single router-selected stance framing (#3523 S6) is appended to
    the preamble; with the flag ``off`` / ``log`` or no ``changed_files`` the
    preamble is byte-identical to legacy.
    """
    return _reviewer_scope_preamble_body(reviewer_type, phase) + _review_stance_framing(
        changed_files, repo_path
    )


def _reviewer_scope_preamble_body(reviewer_type: str, phase: str) -> str:
    """The legacy scope-preamble bodies (unchanged; stance is appended by caller)."""
    if reviewer_type == "agent-design":
        return (
            "This is a specialized **agent-mode design review**. Focus ONLY on "
            "agent-mode design principles. Do NOT review general code quality, "
            "security, or correctness — other reviewers handle those.\n\n"
            "**Only flag issues if you find clear agent-mode design anti-patterns.** "
            "If the output has no agent-mode concerns, a brief approval is acceptable "
            "— you do not need to produce a lengthy analysis when there are no concerns."
        )
    elif reviewer_type == "code":
        return (
            "This is a **comprehensive code review**. Focus on security, correctness, "
            "and robustness. Agent-mode design alignment is handled by another reviewer.\n\n"
            "**Be direct.** Do not soften feedback. State issues clearly and explain "
            "why they matter.\n\n"
            "**Be thorough.** Find ALL issues on the first pass. Do not stop after "
            "identifying a few problems.\n\n"
            "**Analysis format:** Provide file-by-file analysis covering each changed "
            "file. For each file, note what changed, whether the change is correct, "
            "and any issues or observations."
        )
    elif reviewer_type == "code-holistic":
        return (
            "This is a CRITICAL **holistic code review** (issue #2126). "
            "You run alongside `reviewer_code` — your job is the "
            "cross-module coherence question line-by-line review does not "
            "own. **Don't verify every line; `reviewer_code` covers "
            "that.**\n\n"
            "**Lens scope:** read the diff once with the whole PR in mind, "
            "then run all four passes from the criteria below: (1) walk "
            "the primary advertised use case end-to-end (the `__checkout__` "
            "dead-end on PR #2105 is the canonical miss); (2) check that "
            "every doc-claimed behaviour is actually implemented and every "
            "user-facing code path is documented; (3) confirm synthetic "
            "keys / sentinels / magic values are recognised by every "
            "consumer in another module; (4) hunt silent fallbacks "
            "(`except Exception:`, swallowed `None`s, default no-op "
            "branches) where the operator would expect a signal.\n\n"
            "**Distinct CRITICAL role.** Your NACK gates consensus on its "
            "own — it is not averaged against `reviewer_code`'s "
            "verdict. If the architectural-coherence question fails, "
            "NACK even when the line-by-line review is clean.\n\n"
            "**Analysis format:** Name the pass that found the issue, the "
            "producer / consumer modules the asymmetry spans, and the "
            "user-visible failure shape. If all four passes come back "
            "clean a concise ACK is acceptable, but the BRC bus enforces "
            "a minimum content length on ACK / NACK bodies, so write at "
            "least a sentence or two summarising what you checked."
        )
    elif reviewer_type == "contract":
        return (
            "This is a **contract verification review**. Verify that the implementation "
            "matches the contract and all acceptance criteria are met. Do NOT review "
            "general code quality or security — other reviewers handle those.\n\n"
            "**Analysis format:** Provide a criterion-by-criterion verification — for each "
            "acceptance criterion, state whether it is met and cite the specific evidence."
        )
    elif reviewer_type == "refine":
        return (
            "This is a **refine phase review**. Focus on the quality and completeness "
            "of the analysis produced during the refine phase. Evaluate problem "
            "understanding, codebase research, options analysis, and the recommended "
            "approach. Agent-mode design alignment is handled by another reviewer.\n\n"
            "**Analysis format:** Provide section-by-section evaluation of the refine "
            "output — assess each major section for depth, accuracy, and completeness."
        )
    elif reviewer_type == "first-principles-reviewer":
        return (
            "This is an adversarial **first-principles review**. Focus ONLY on "
            "whether the premise is sound and the direction appropriate — the "
            "seed and where the refiner's analysis is heading. Do NOT review "
            "analysis quality, code, or implementation detail; other agents "
            "own those.\n\n"
            "You escalate by surfacing HITL decisions for the operator, not by "
            "NACKing the refiner. If the direction is sound, a brief approval "
            "and ACK is the right outcome — do not manufacture an objection."
        )
    elif reviewer_type == "plan":
        return (
            "This is a **plan phase review**. Focus on the quality and completeness "
            "of the implementation plan. Evaluate task breakdown, acceptance criteria, "
            "dependency ordering, risk assessment, and test strategy. Agent-mode "
            "design alignment is handled by another reviewer.\n\n"
            "**Analysis format:** Provide section-by-section evaluation of the plan — "
            "assess task decomposition, acceptance criteria quality, dependency ordering, "
            "and risk coverage."
        )
    elif reviewer_type == "security":
        return (
            "This is a CRITICAL **security-lens review** (issue #2139). "
            "A NACK from this lens blocks consensus until the producer "
            "re-proposes. Focus ONLY on the security lens; defer code "
            "quality, performance, and non-security findings to "
            "`reviewer_code`.\n\n"
            "**Lens scope:** cross-file allowlist mismatches, "
            "handler-vs-validator path mismatches, information-disclosure / "
            "authorization-bypass patterns at trust boundaries, "
            "uncommitted-artifact / Dockerfile-symlink mismatches, secret "
            "leakage, and OWASP top-10 patterns that span more than one "
            "changed file. Be especially alert to allowlist-mismatch "
            "patterns where a handler in one file accepts traffic that a "
            "validator in another file was supposed to reject.\n\n"
            "**Analysis format:** Provide a finding-by-finding lens report. "
            "If the diff has no security concerns, a concise approval is "
            "acceptable — verbose reports without findings are not required, "
            "but the BRC bus enforces a minimum content length on ACK / "
            "NACK bodies, so write at least a sentence or two summarizing "
            'what you checked (not a single-word "LGTM").'
        )
    elif reviewer_type == "concurrency":
        return (
            "This is a CRITICAL **concurrency-lens review** (issue #2139). "
            "A NACK from this lens blocks consensus until the producer "
            "re-proposes. Focus ONLY on the concurrency lens; defer code "
            "quality, performance, and non-concurrency findings to "
            "`reviewer_code`.\n\n"
            "**Lens scope:** race conditions, deadlocks, shared-state "
            "mutation without synchronization, async-context leakage, "
            "retry-storm patterns, resource-cleanup ordering bugs, and "
            "BRC-protocol invariants (send→wait ordering, cursor "
            "threading per #1925, heartbeat-stall windows per #2012).\n\n"
            "**Analysis format:** Provide a finding-by-finding lens report. "
            "If the diff has no concurrency concerns, a concise approval is "
            "acceptable — verbose reports without findings are not required, "
            "but the BRC bus enforces a minimum content length on ACK / "
            "NACK bodies, so write at least a sentence or two summarizing "
            'what you checked (not a single-word "LGTM").'
        )
    else:
        raise ValueError(f"Unknown reviewer type: {reviewer_type}")
