# BRC Consensus History — refine phase

Generated: 2026-04-16T18:30:35Z
Pipeline: issue-1748

### [2026-04-16T18:20:06Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Analysis draft for #1748 (replace egg-babysit with one-off implement-phase BRC cycle): maps current babysit-pr architecture (shared/egg_babysit/, no role-typing, no BRC), enumerates the implement-phase machinery to be reused (orchestrator/routes/pipelines.py:6829 _run_concurrent_phase, shared/egg_contracts/agent_roles.py:1002 get_roles_for_phase, orchestrator/peer_consensus.py BRC tracker, orchestrator/attestation_schemas.py generic schemas, orchestrator/routes/pipelines.py:4353 BRC history persistence), evaluates three approaches (Option A mode-aware reuse via PipelineMode + roster filter + orient-prompt extension; Option B new top-level phase; Option C vendored-BRC inside egg_babysit), recommends Option A with concrete file-line callouts. Lists 5 decision points (PipelineMode migration; no-contract roster filter signal; MCP-skill scope; mid-cycle human-commit handling; conflict_resolver invocation policy) and 6 open feedback questions (additional reviewer pre-filters; fork-PR UX; concurrency/pipeline-id collisions; scope-expansion guardrails; brc-history identifier collisions; hardcoded-origin/main sweep scope; CLI removal vs deprecation shim). Draft committed at 9b62328e2 to .egg-state/drafts/1748-analysis.md.

````yaml
id: 5b8f098b-5039-49
phase: refine
metadata:
  payload:
    summary: 'Analysis draft for #1748 (replace egg-babysit with one-off implement-phase
      BRC cycle): maps current babysit-pr architecture (shared/egg_babysit/, no role-typing,
      no BRC), enumerates the implement-phase machinery to be reused (orchestrator/routes/pipelines.py:6829
      _run_concurrent_phase, shared/egg_contracts/agent_roles.py:1002 get_roles_for_phase,
      orchestrator/peer_consensus.py BRC tracker, orchestrator/attestation_schemas.py
      generic schemas, orchestrator/routes/pipelines.py:4353 BRC history persistence),
      evaluates three approaches (Option A mode-aware reuse via PipelineMode + roster
      filter + orient-prompt extension; Option B new top-level phase; Option C vendored-BRC
      inside egg_babysit), recommends Option A with concrete file-line callouts. Lists
      5 decision points (PipelineMode migration; no-contract roster filter signal;
      MCP-skill scope; mid-cycle human-commit handling; conflict_resolver invocation
      policy) and 6 open feedback questions (additional reviewer pre-filters; fork-PR
      UX; concurrency/pipeline-id collisions; scope-expansion guardrails; brc-history
      identifier collisions; hardcoded-origin/main sweep scope; CLI removal vs deprecation
      shim). Draft committed at 9b62328e2 to .egg-state/drafts/1748-analysis.md.'
    attestation: {}
    artifacts:
    - .egg-state/drafts/1748-analysis.md
    risk_considered: ''
    commit_sha: 9b62328e28b60d5255e076ccff777540e3f2bde3
    files_changed:
    - .egg-state/drafts/1748-analysis.md
    tests_run: []
    tasks_satisfied:
    - refine-analysis-1748
  version: 1
  commit_sha: 9b62328e28b60d5255e076ccff777540e3f2bde3
````

### [2026-04-16T18:21:36Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner

Reviewed .egg-state/drafts/1748-analysis.md (168 lines) at commit 9b62328. Evaluated each section against the 8 agent-mode anti-patterns. No blocking concerns.

### What was checked
1. **Pre-fetching (excessive)** — The proposal orients agents via tools, not baked-in large context. Reviewers/producers run `git diff base...HEAD` themselves; base-ref is threaded as a small metadata value, not the diff contents. Constraints section explicitly states "thread it into the orchestrator/agent layer rather than re-fetching it five places" which is orient-by-value, not pre-fetch-into-prompt. Clean.
2. **Structured output for humans** — None proposed. Consensus flow keeps free-text `--reason` (BRC), and reuses existing attestation schemas. Clean.
3. **Post-processing pipelines** — Explicitly avoided. The recommendation (Option A) routes babysit-pr through the existing `_run_concurrent_phase` / BRC tracker so agents emit `CONSENSUS_PROPOSE/ACK/NACK/CONFIRMED` directly; nothing parses agent stdout to take downstream actions. The draft even rejects Option C partly because it would fork the orchestration substrate. Clean.
4. **Rigid procedures** — Objective-driven. Producer behavior is stated as "rebase/merge pr.base.ref into the staging branch, resolve conflicts within their own role's file scope, then propose" — a goal, not a step-by-step script. The "No babysit-level iteration cap. BRC owns convergence" constraint (line under Constraints) explicitly forbids layering a procedural loop on top of BRC. Clean.
5. **Prompt-level security** — Correctly defers to sandbox/gateway enforcement. "File-access patterns enforce disjoint scopes per role" (Current Behavior) and "Gateway constraints inherit" / "Staging branch isolation" (Constraints) all route restrictions through gateway/sandbox primitives (`EGG_AGENT_FILE_PATTERNS`, gateway policy.py:535), not prompt-level instructions. Clean.
6. **Direct LLM API calls outside sandbox (EGG200)** — None introduced. The proposal reuses the existing orchestrator spawn path (`_run_concurrent_phase` → `create_concurrent_spawn_fn` → containers).
7. **Bypassing Agent SDK** — None introduced. The draft references `egg_agent.build_agent_command(prompt=..., model="sonnet", max_turns=200)` as the existing (kept) pattern (Current Behavior: `shared/egg_babysit/fixer.py:57`, `reviewer.py:63`). Agents continue to route through `egg_agent`.
8. **Hardcoded model IDs (EGG201)** — Only short alias `"sonnet"` cited; no full model IDs introduced. Clean.

### Non-blocking observations
- **Scope-expansion open question (feedback item #4)** — The draft asks whether to add a "do not refactor outside the diff" soft instruction to the orient prompt. From an agent-mode-design lens this is the right framing (ask, don't unilaterally bake in a constraint). When the human answers, prefer a lightweight orientation hint ("stay close to the diff; justify broader changes") over a hard pre-prompt cap on bytes/files — hard caps are the kind of prompt-level constraint that tends to either be ignored or distort the agent's judgment on legitimately-broader fixes. Not a blocker for refine; flag for plan phase.
- **`_build_reviewer_preparation` / `_build_producer_orientation` mode branches** (Option A, cons) — The draft correctly flags that per-mode branches in these prompt builders will accumulate. If a third mode later lands, refactor into per-mode prompt templates rather than growing the `if/elif` chain. Not a concern for this first cut.
- **Staging-branch isolation** — A staging branch is orchestration-level isolation (gateway-enforced push policy on `egg/`-prefixed branches), not prompt-level constraint. Good agent-mode design.
- **On-demand `CONFLICT_RESOLVER` role** — Draft defers overlap handling to a lazy on-demand spawn rather than baking cross-role file-scope merging into every producer prompt. Correct agent-mode framing: keep each role's orient focused.

### Verdict
The analysis cleanly aligns with `docs/guides/agent-mode-design.md`. It preserves BRC consensus as the convergence mechanism, keeps file-scope enforcement in gateway/sandbox primitives, threads base-ref as small metadata rather than pre-fetching diff content, uses the Agent SDK / model aliases already in place, and defers scope-limit policy to a human decision rather than hard-coding. Good to proceed from an agent-design standpoint.

— Authored by egg

````yaml
id: c6ac7ed6-c754-48
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1748-analysis.md
    reason: "Reviewed .egg-state/drafts/1748-analysis.md (168 lines) at commit 9b62328.\
      \ Evaluated each section against the 8 agent-mode anti-patterns. No blocking\
      \ concerns.\n\n### What was checked\n1. **Pre-fetching (excessive)** \u2014\
      \ The proposal orients agents via tools, not baked-in large context. Reviewers/producers\
      \ run `git diff base...HEAD` themselves; base-ref is threaded as a small metadata\
      \ value, not the diff contents. Constraints section explicitly states \"thread\
      \ it into the orchestrator/agent layer rather than re-fetching it five places\"\
      \ which is orient-by-value, not pre-fetch-into-prompt. Clean.\n2. **Structured\
      \ output for humans** \u2014 None proposed. Consensus flow keeps free-text `--reason`\
      \ (BRC), and reuses existing attestation schemas. Clean.\n3. **Post-processing\
      \ pipelines** \u2014 Explicitly avoided. The recommendation (Option A) routes\
      \ babysit-pr through the existing `_run_concurrent_phase` / BRC tracker so agents\
      \ emit `CONSENSUS_PROPOSE/ACK/NACK/CONFIRMED` directly; nothing parses agent\
      \ stdout to take downstream actions. The draft even rejects Option C partly\
      \ because it would fork the orchestration substrate. Clean.\n4. **Rigid procedures**\
      \ \u2014 Objective-driven. Producer behavior is stated as \"rebase/merge pr.base.ref\
      \ into the staging branch, resolve conflicts within their own role's file scope,\
      \ then propose\" \u2014 a goal, not a step-by-step script. The \"No babysit-level\
      \ iteration cap. BRC owns convergence\" constraint (line under Constraints)\
      \ explicitly forbids layering a procedural loop on top of BRC. Clean.\n5. **Prompt-level\
      \ security** \u2014 Correctly defers to sandbox/gateway enforcement. \"File-access\
      \ patterns enforce disjoint scopes per role\" (Current Behavior) and \"Gateway\
      \ constraints inherit\" / \"Staging branch isolation\" (Constraints) all route\
      \ restrictions through gateway/sandbox primitives (`EGG_AGENT_FILE_PATTERNS`,\
      \ gateway policy.py:535), not prompt-level instructions. Clean.\n6. **Direct\
      \ LLM API calls outside sandbox (EGG200)** \u2014 None introduced. The proposal\
      \ reuses the existing orchestrator spawn path (`_run_concurrent_phase` \u2192\
      \ `create_concurrent_spawn_fn` \u2192 containers).\n7. **Bypassing Agent SDK**\
      \ \u2014 None introduced. The draft references `egg_agent.build_agent_command(prompt=...,\
      \ model=\"sonnet\", max_turns=200)` as the existing (kept) pattern (Current\
      \ Behavior: `shared/egg_babysit/fixer.py:57`, `reviewer.py:63`). Agents continue\
      \ to route through `egg_agent`.\n8. **Hardcoded model IDs (EGG201)** \u2014\
      \ Only short alias `\"sonnet\"` cited; no full model IDs introduced. Clean.\n\
      \n### Non-blocking observations\n- **Scope-expansion open question (feedback\
      \ item #4)** \u2014 The draft asks whether to add a \"do not refactor outside\
      \ the diff\" soft instruction to the orient prompt. From an agent-mode-design\
      \ lens this is the right framing (ask, don't unilaterally bake in a constraint).\
      \ When the human answers, prefer a lightweight orientation hint (\"stay close\
      \ to the diff; justify broader changes\") over a hard pre-prompt cap on bytes/files\
      \ \u2014 hard caps are the kind of prompt-level constraint that tends to either\
      \ be ignored or distort the agent's judgment on legitimately-broader fixes.\
      \ Not a blocker for refine; flag for plan phase.\n- **`_build_reviewer_preparation`\
      \ / `_build_producer_orientation` mode branches** (Option A, cons) \u2014 The\
      \ draft correctly flags that per-mode branches in these prompt builders will\
      \ accumulate. If a third mode later lands, refactor into per-mode prompt templates\
      \ rather than growing the `if/elif` chain. Not a concern for this first cut.\n\
      - **Staging-branch isolation** \u2014 A staging branch is orchestration-level\
      \ isolation (gateway-enforced push policy on `egg/`-prefixed branches), not\
      \ prompt-level constraint. Good agent-mode design.\n- **On-demand `CONFLICT_RESOLVER`\
      \ role** \u2014 Draft defers overlap handling to a lazy on-demand spawn rather\
      \ than baking cross-role file-scope merging into every producer prompt. Correct\
      \ agent-mode framing: keep each role's orient focused.\n\n### Verdict\nThe analysis\
      \ cleanly aligns with `docs/guides/agent-mode-design.md`. It preserves BRC consensus\
      \ as the convergence mechanism, keeps file-scope enforcement in gateway/sandbox\
      \ primitives, threads base-ref as small metadata rather than pre-fetching diff\
      \ content, uses the Agent SDK / model aliases already in place, and defers scope-limit\
      \ policy to a human decision rather than hard-coding. Good to proceed from an\
      \ agent-design standpoint.\n\n\u2014 Authored by egg"
  version: 1
````

### [2026-04-16T18:21:42Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: ff007e49-56f0-4e
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-16T18:23:31Z] reviewer_refine → refiner (CONSENSUS_NACK): NACK from reviewer_refine for refiner

Reviewed .egg-state/drafts/1748-analysis.md (168 lines, commit 9b62328) against the 7 refine-review criteria. Verified several citation claims against the source: `shared/egg_contracts/agent_roles.py:534` (reviewer_contract dependency on TASK_PLANNER + RISK_ANALYST — confirmed), `agent_roles.py:972` (_PHASE_ROLES["implement"] roster — confirmed), `pipelines.py:6046-6048` (origin/main hardcoded in reviewer-code orientation — confirmed), `REFINER_ROLE` file-access (blocks `.egg-state/contracts/` — confirmed).

### Blocking

1. **Open questions are not registered and use the wrong format.** The `Open Questions` section (lines 107-160) renders all 5 decisions and the feedback block as shell-command code blocks (`egg-contract add-decision \ ...`) rather than as HITL markers. Per the refine review criteria: "If open questions appear as prose text without corresponding `<!-- egg-hitl-decision ... -->` or `<!-- egg-hitl-feedback ... -->` markers ... flag as needs_revision." The pipeline's HITL processor scans for those markers — a bare shell command in a code block is never picked up. **Fix:** (a) run `egg-contract add-decision --question ... --options ...` for each of the 5 decisions now (the contract for #1748 does not exist per `egg-contract show`, but the add-* commands create it on first use — the refiner's note at line 109 that the contract "is created during the plan phase, after refine completes" is factually wrong; see `.egg-state/contracts/1489.json` and `1481.json`, both of which exist at `current_phase: "refine"` and predate any plan phase). (b) If the API truly rejects the calls, fall back to inline `<!-- egg-hitl-decision id=decision-N -->` / `<!-- egg-hitl-feedback id=feedback-N -->` markers (see `.egg-state/drafts/1028-analysis.md:259-291` for the exact format the processor expects), not shell commands. Either way, the draft as shipped will silently drop 12 questions.

2. **Feedback block is structurally broken as a single shell command.** Lines 152-159 stack seven `--question` flags inside one `egg-contract add-feedback` call. That command supports only one `--question` per invocation, so even if this were run verbatim, only the last `--question` would land. **Fix:** emit seven separate `add-feedback` calls (or seven separate `<!-- egg-hitl-feedback id=feedback-N -->` markers), one per distinct question.

3. **Option A recommendation glosses over a concrete prerequisite that deserves explicit surfacing as an Open Question.** Lines 40 and 102-103 note that `origin/main` is hardcoded in `pipelines.py:6048`, `health_checks/tier1/phase_output.py:175-185`, and `health_checks/context.py:110-113`, but the constraints list (line 40) calls base-branch propagation a "consistency" task while the open questions only touch health-check sweep scope obliquely (feedback #6, line 158). If the plan phase is to produce accurate task breakdowns, the refine output needs a decision on *who owns base-branch parameterization* — is it in-scope for the first cut, or is babysit-pr only supported for PRs whose base is `main` in v1? **Fix:** register this as a decision (three options: full sweep in first cut / mode-gated parameterization / v1 restricted to main-based PRs) so the plan phase can size it.

### Non-blocking

- **Missing metadata footer.** The draft ends at `*Authored-by: egg*` (line 168) with no `# metadata` block. `.egg-state/drafts/1028-analysis.md:297-301` and others include a structured `complexity_tier:` / `parallel_phases:` footer that downstream tooling parses. The `Complexity Assessment` on line 162-164 is prose-only. Consider adding the structured footer (the inline claim `high` on line 164 and "could be parallelized into separate phases" on line 164 translate directly to `complexity_tier: high` and `parallel_phases: true`).
- **Option analysis is thorough but skips one plausible middle path.** Between Option A (in-place mode branching) and Option B (new phase), there is a "factor `_build_*_orientation` into per-role prompt templates now, then Option A slots in cleanly" variant. The draft's own Option-A con at line 70 foreshadows this ("if a third mode lands later, these will need refactoring into per-mode prompt templates") — worth surfacing as a fourth option rather than a deferred concern, since the templating decision is independent of the babysit-pr design.
- **The "reviewer_contract must be filtered" constraint (line 43) is under-specified on how.** The draft correctly identifies three implementation locations (decision-2, lines 120-125), but the constraint as written implies a single correct answer. Consider moving the "how to filter" discussion out of constraints and only keeping the "what must be filtered" invariant there, since the "how" is the decision.
- **Claim on line 40 references `pipelines.py:2972,3119,5855` for base-aware diff helpers without naming them.** Citation is verifiable but a plan-phase reader will hunt; adding function names (e.g., `_get_diff_against_base` or whatever they're actually called) would save a round-trip.
- **Line 47 states "BRC reviewers must reach CONFIRMED, not just ACK" as a constraint, but no question or recommendation addresses whether the babysit-pr first cut carries any enforcement for this beyond what the implement phase already has.** If this is just inherited behaviour, the line belongs in a "Assumptions" section, not "Constraints."
— Authored by egg

````yaml
id: 17052f99-eef9-42
phase: refine
metadata:
  payload:
    reason: "Reviewed .egg-state/drafts/1748-analysis.md (168 lines, commit 9b62328)\
      \ against the 7 refine-review criteria. Verified several citation claims against\
      \ the source: `shared/egg_contracts/agent_roles.py:534` (reviewer_contract dependency\
      \ on TASK_PLANNER + RISK_ANALYST \u2014 confirmed), `agent_roles.py:972` (_PHASE_ROLES[\"\
      implement\"] roster \u2014 confirmed), `pipelines.py:6046-6048` (origin/main\
      \ hardcoded in reviewer-code orientation \u2014 confirmed), `REFINER_ROLE` file-access\
      \ (blocks `.egg-state/contracts/` \u2014 confirmed).\n\n### Blocking\n\n1. **Open\
      \ questions are not registered and use the wrong format.** The `Open Questions`\
      \ section (lines 107-160) renders all 5 decisions and the feedback block as\
      \ shell-command code blocks (`egg-contract add-decision \\ ...`) rather than\
      \ as HITL markers. Per the refine review criteria: \"If open questions appear\
      \ as prose text without corresponding `<!-- egg-hitl-decision ... -->` or `<!--\
      \ egg-hitl-feedback ... -->` markers ... flag as needs_revision.\" The pipeline's\
      \ HITL processor scans for those markers \u2014 a bare shell command in a code\
      \ block is never picked up. **Fix:** (a) run `egg-contract add-decision --question\
      \ ... --options ...` for each of the 5 decisions now (the contract for #1748\
      \ does not exist per `egg-contract show`, but the add-* commands create it on\
      \ first use \u2014 the refiner's note at line 109 that the contract \"is created\
      \ during the plan phase, after refine completes\" is factually wrong; see `.egg-state/contracts/1489.json`\
      \ and `1481.json`, both of which exist at `current_phase: \"refine\"` and predate\
      \ any plan phase). (b) If the API truly rejects the calls, fall back to inline\
      \ `<!-- egg-hitl-decision id=decision-N -->` / `<!-- egg-hitl-feedback id=feedback-N\
      \ -->` markers (see `.egg-state/drafts/1028-analysis.md:259-291` for the exact\
      \ format the processor expects), not shell commands. Either way, the draft as\
      \ shipped will silently drop 12 questions.\n\n2. **Feedback block is structurally\
      \ broken as a single shell command.** Lines 152-159 stack seven `--question`\
      \ flags inside one `egg-contract add-feedback` call. That command supports only\
      \ one `--question` per invocation, so even if this were run verbatim, only the\
      \ last `--question` would land. **Fix:** emit seven separate `add-feedback`\
      \ calls (or seven separate `<!-- egg-hitl-feedback id=feedback-N -->` markers),\
      \ one per distinct question.\n\n3. **Option A recommendation glosses over a\
      \ concrete prerequisite that deserves explicit surfacing as an Open Question.**\
      \ Lines 40 and 102-103 note that `origin/main` is hardcoded in `pipelines.py:6048`,\
      \ `health_checks/tier1/phase_output.py:175-185`, and `health_checks/context.py:110-113`,\
      \ but the constraints list (line 40) calls base-branch propagation a \"consistency\"\
      \ task while the open questions only touch health-check sweep scope obliquely\
      \ (feedback #6, line 158). If the plan phase is to produce accurate task breakdowns,\
      \ the refine output needs a decision on *who owns base-branch parameterization*\
      \ \u2014 is it in-scope for the first cut, or is babysit-pr only supported for\
      \ PRs whose base is `main` in v1? **Fix:** register this as a decision (three\
      \ options: full sweep in first cut / mode-gated parameterization / v1 restricted\
      \ to main-based PRs) so the plan phase can size it.\n\n### Non-blocking\n\n\
      - **Missing metadata footer.** The draft ends at `*Authored-by: egg*` (line\
      \ 168) with no `# metadata` block. `.egg-state/drafts/1028-analysis.md:297-301`\
      \ and others include a structured `complexity_tier:` / `parallel_phases:` footer\
      \ that downstream tooling parses. The `Complexity Assessment` on line 162-164\
      \ is prose-only. Consider adding the structured footer (the inline claim `high`\
      \ on line 164 and \"could be parallelized into separate phases\" on line 164\
      \ translate directly to `complexity_tier: high` and `parallel_phases: true`).\n\
      - **Option analysis is thorough but skips one plausible middle path.** Between\
      \ Option A (in-place mode branching) and Option B (new phase), there is a \"\
      factor `_build_*_orientation` into per-role prompt templates now, then Option\
      \ A slots in cleanly\" variant. The draft's own Option-A con at line 70 foreshadows\
      \ this (\"if a third mode lands later, these will need refactoring into per-mode\
      \ prompt templates\") \u2014 worth surfacing as a fourth option rather than\
      \ a deferred concern, since the templating decision is independent of the babysit-pr\
      \ design.\n- **The \"reviewer_contract must be filtered\" constraint (line 43)\
      \ is under-specified on how.** The draft correctly identifies three implementation\
      \ locations (decision-2, lines 120-125), but the constraint as written implies\
      \ a single correct answer. Consider moving the \"how to filter\" discussion\
      \ out of constraints and only keeping the \"what must be filtered\" invariant\
      \ there, since the \"how\" is the decision.\n- **Claim on line 40 references\
      \ `pipelines.py:2972,3119,5855` for base-aware diff helpers without naming them.**\
      \ Citation is verifiable but a plan-phase reader will hunt; adding function\
      \ names (e.g., `_get_diff_against_base` or whatever they're actually called)\
      \ would save a round-trip.\n- **Line 47 states \"BRC reviewers must reach CONFIRMED,\
      \ not just ACK\" as a constraint, but no question or recommendation addresses\
      \ whether the babysit-pr first cut carries any enforcement for this beyond what\
      \ the implement phase already has.** If this is just inherited behaviour, the\
      \ line belongs in a \"Assumptions\" section, not \"Constraints.\"\n\u2014 Authored\
      \ by egg"
    artifact_references:
    - .egg-state/drafts/1748-analysis.md
  reason: "Reviewed .egg-state/drafts/1748-analysis.md (168 lines, commit 9b62328)\
    \ against the 7 refine-review criteria. Verified several citation claims against\
    \ the source: `shared/egg_contracts/agent_roles.py:534` (reviewer_contract dependency\
    \ on TASK_PLANNER + RISK_ANALYST \u2014 confirmed), `agent_roles.py:972` (_PHASE_ROLES[\"\
    implement\"] roster \u2014 confirmed), `pipelines.py:6046-6048` (origin/main hardcoded\
    \ in reviewer-code orientation \u2014 confirmed), `REFINER_ROLE` file-access (blocks\
    \ `.egg-state/contracts/` \u2014 confirmed).\n\n### Blocking\n\n1. **Open questions\
    \ are not registered and use the wrong format.** The `Open Questions` section\
    \ (lines 107-160) renders all 5 decisions and the feedback block as shell-command\
    \ code blocks (`egg-contract add-decision \\ ...`) rather than as HITL markers.\
    \ Per the refine review criteria: \"If open questions appear as prose text without\
    \ corresponding `<!-- egg-hitl-decision ... -->` or `<!-- egg-hitl-feedback ...\
    \ -->` markers ... flag as needs_revision.\" The pipeline's HITL processor scans\
    \ for those markers \u2014 a bare shell command in a code block is never picked\
    \ up. **Fix:** (a) run `egg-contract add-decision --question ... --options ...`\
    \ for each of the 5 decisions now (the contract for #1748 does not exist per `egg-contract\
    \ show`, but the add-* commands create it on first use \u2014 the refiner's note\
    \ at line 109 that the contract \"is created during the plan phase, after refine\
    \ completes\" is factually wrong; see `.egg-state/contracts/1489.json` and `1481.json`,\
    \ both of which exist at `current_phase: \"refine\"` and predate any plan phase).\
    \ (b) If the API truly rejects the calls, fall back to inline `<!-- egg-hitl-decision\
    \ id=decision-N -->` / `<!-- egg-hitl-feedback id=feedback-N -->` markers (see\
    \ `.egg-state/drafts/1028-analysis.md:259-291` for the exact format the processor\
    \ expects), not shell commands. Either way, the draft as shipped will silently\
    \ drop 12 questions.\n\n2. **Feedback block is structurally broken as a single\
    \ shell command.** Lines 152-159 stack seven `--question` flags inside one `egg-contract\
    \ add-feedback` call. That command supports only one `--question` per invocation,\
    \ so even if this were run verbatim, only the last `--question` would land. **Fix:**\
    \ emit seven separate `add-feedback` calls (or seven separate `<!-- egg-hitl-feedback\
    \ id=feedback-N -->` markers), one per distinct question.\n\n3. **Option A recommendation\
    \ glosses over a concrete prerequisite that deserves explicit surfacing as an\
    \ Open Question.** Lines 40 and 102-103 note that `origin/main` is hardcoded in\
    \ `pipelines.py:6048`, `health_checks/tier1/phase_output.py:175-185`, and `health_checks/context.py:110-113`,\
    \ but the constraints list (line 40) calls base-branch propagation a \"consistency\"\
    \ task while the open questions only touch health-check sweep scope obliquely\
    \ (feedback #6, line 158). If the plan phase is to produce accurate task breakdowns,\
    \ the refine output needs a decision on *who owns base-branch parameterization*\
    \ \u2014 is it in-scope for the first cut, or is babysit-pr only supported for\
    \ PRs whose base is `main` in v1? **Fix:** register this as a decision (three\
    \ options: full sweep in first cut / mode-gated parameterization / v1 restricted\
    \ to main-based PRs) so the plan phase can size it.\n\n### Non-blocking\n\n- **Missing\
    \ metadata footer.** The draft ends at `*Authored-by: egg*` (line 168) with no\
    \ `# metadata` block. `.egg-state/drafts/1028-analysis.md:297-301` and others\
    \ include a structured `complexity_tier:` / `parallel_phases:` footer that downstream\
    \ tooling parses. The `Complexity Assessment` on line 162-164 is prose-only. Consider\
    \ adding the structured footer (the inline claim `high` on line 164 and \"could\
    \ be parallelized into separate phases\" on line 164 translate directly to `complexity_tier:\
    \ high` and `parallel_phases: true`).\n- **Option analysis is thorough but skips\
    \ one plausible middle path.** Between Option A (in-place mode branching) and\
    \ Option B (new phase), there is a \"factor `_build_*_orientation` into per-role\
    \ prompt templates now, then Option A slots in cleanly\" variant. The draft's\
    \ own Option-A con at line 70 foreshadows this (\"if a third mode lands later,\
    \ these will need refactoring into per-mode prompt templates\") \u2014 worth surfacing\
    \ as a fourth option rather than a deferred concern, since the templating decision\
    \ is independent of the babysit-pr design.\n- **The \"reviewer_contract must be\
    \ filtered\" constraint (line 43) is under-specified on how.** The draft correctly\
    \ identifies three implementation locations (decision-2, lines 120-125), but the\
    \ constraint as written implies a single correct answer. Consider moving the \"\
    how to filter\" discussion out of constraints and only keeping the \"what must\
    \ be filtered\" invariant there, since the \"how\" is the decision.\n- **Claim\
    \ on line 40 references `pipelines.py:2972,3119,5855` for base-aware diff helpers\
    \ without naming them.** Citation is verifiable but a plan-phase reader will hunt;\
    \ adding function names (e.g., `_get_diff_against_base` or whatever they're actually\
    \ called) would save a round-trip.\n- **Line 47 states \"BRC reviewers must reach\
    \ CONFIRMED, not just ACK\" as a constraint, but no question or recommendation\
    \ addresses whether the babysit-pr first cut carries any enforcement for this\
    \ beyond what the implement phase already has.** If this is just inherited behaviour,\
    \ the line belongs in a \"Assumptions\" section, not \"Constraints.\"\n\u2014\
    \ Authored by egg"
  revision_count: 1
````

### [2026-04-16T18:23:34Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine (pending_acks)

Reviewer reviewer_refine cannot confirm: unresolved NACKs. Wait for these producers to re-propose before confirming: ['refiner']

````yaml
id: 15eb9dc0-3da1-4e
phase: refine
metadata:
  pending_acks: true
````

### [2026-04-16T18:27:29Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Re-proposal addressing reviewer_refine NACK on #1748 (commit ef462b57b). Blocking fixes: (1) Converted all 7 decisions and 6 feedback items from shell-command code blocks to inline <!-- egg-hitl-decision/feedback id=N --> markers per the format documented at .egg-state/drafts/1028-analysis.md:259-291 (the HITL processor scans for these markers, not shell commands). (2) Added Decision 6 explicitly sizing base-branch parameterization scope (full sweep / mode-gated / v1-restricted-to-main) so plan phase can task-break it accurately. (3) Reformatted the feedback block as 6 distinct <!-- egg-hitl-feedback --> markers, since add-feedback only accepts one --question per invocation. Non-blocking improvements: added structured 'metadata' footer with complexity_tier and parallel_phases; moved 'CONFIRMED not ACK' to a new Assumptions section (inherited substrate, not a new rule); pulled 'how to filter reviewer_contract' out of Constraints (kept the invariant; the how is now Decision 2); named the existing base-aware diff helpers (pipelines.py:2972-2973, :3119-3123, :5855, get_default_branch at :4145-4202); added Option D (refactor _build_*_orientation per-role templates first, then apply Option A) as a fourth approach with explicit pros/cons; added Decision 7 to choose between Option A and Option D. Confirmed via direct test that egg-contract add-decision returns 'Contract for #1748 not found' — the reviewer's claim that the command auto-creates the contract is incorrect for this issue, so the inline-marker fallback is the correct path. Verified existing 1028-analysis.md uses the same fallback format.

````yaml
id: 7147246c-b4c2-45
phase: refine
metadata:
  payload:
    summary: "Re-proposal addressing reviewer_refine NACK on #1748 (commit ef462b57b).\
      \ Blocking fixes: (1) Converted all 7 decisions and 6 feedback items from shell-command\
      \ code blocks to inline <!-- egg-hitl-decision/feedback id=N --> markers per\
      \ the format documented at .egg-state/drafts/1028-analysis.md:259-291 (the HITL\
      \ processor scans for these markers, not shell commands). (2) Added Decision\
      \ 6 explicitly sizing base-branch parameterization scope (full sweep / mode-gated\
      \ / v1-restricted-to-main) so plan phase can task-break it accurately. (3) Reformatted\
      \ the feedback block as 6 distinct <!-- egg-hitl-feedback --> markers, since\
      \ add-feedback only accepts one --question per invocation. Non-blocking improvements:\
      \ added structured 'metadata' footer with complexity_tier and parallel_phases;\
      \ moved 'CONFIRMED not ACK' to a new Assumptions section (inherited substrate,\
      \ not a new rule); pulled 'how to filter reviewer_contract' out of Constraints\
      \ (kept the invariant; the how is now Decision 2); named the existing base-aware\
      \ diff helpers (pipelines.py:2972-2973, :3119-3123, :5855, get_default_branch\
      \ at :4145-4202); added Option D (refactor _build_*_orientation per-role templates\
      \ first, then apply Option A) as a fourth approach with explicit pros/cons;\
      \ added Decision 7 to choose between Option A and Option D. Confirmed via direct\
      \ test that egg-contract add-decision returns 'Contract for #1748 not found'\
      \ \u2014 the reviewer's claim that the command auto-creates the contract is\
      \ incorrect for this issue, so the inline-marker fallback is the correct path.\
      \ Verified existing 1028-analysis.md uses the same fallback format."
    attestation: {}
    artifacts:
    - .egg-state/drafts/1748-analysis.md
    risk_considered: ''
    commit_sha: ef462b57b23561fc017e61370cfefd1e8ee88612
    files_changed:
    - .egg-state/drafts/1748-analysis.md
    tests_run: []
    tasks_satisfied:
    - refine-analysis-1748
  version: 2
  commit_sha: ef462b57b23561fc017e61370cfefd1e8ee88612
````

### [2026-04-16T18:27:29Z] orchestrator → reviewer_agent_design (CONSENSUS_RE_REVIEW): Re-review required: refiner submitted new proposal v2

Producer refiner has submitted a new proposal (version 2) after withdrawal. Your previous confirmation was on an earlier version. Please re-review and ACK/NACK the new proposal.

````yaml
id: 6a20129a-8a3f-43
phase: refine
metadata:
  producer_role: refiner
  version: 2
````

### [2026-04-16T18:29:10Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner

Re-reviewed v2 draft `.egg-state/drafts/1748-analysis.md` at commit ef462b57b (180 lines, +107/-73 vs v1). Evaluated against the 8 agent-mode design anti-patterns. No blocking concerns.

### v2 delta summary
- Decisions/feedback converted from shell-command blocks to inline `<!-- egg-hitl-decision/feedback id=N -->` markers (machine-scannable by HITL processor per `1028-analysis.md:259-291`).
- Added Decision 6 (base-branch parameterization scope: full sweep vs mode-gated vs v1-restricted-to-main).
- Added Decision 7 (refactor `_build_*_orientation` first, Option D) and a matching "Option D" in Options Considered.
- Added an Assumptions section separating inherited-substrate invariants (BRC-CONFIRMED requirement, pre-fetched baseRefName, health-monitor phase-timeouts) from new Constraints.
- Added metadata footer (`complexity_tier: high`, `parallel_phases: true`).
- Named existing base-aware diff helpers in Current Behavior (`pipelines.py:2972-2973`, `:3119-3123`, `:5855`, `get_default_branch()` at `:4145-4202`).

### Anti-pattern evaluation (domain: agent-mode design only)

1. **Pre-fetching (excessive context) — clean.** Orient prompts remain tools-based — reviewers invoke `git diff base...HEAD` at runtime, producers read the PR diff via git. No baked-in diffs, no pre-serialized PR metadata stuffed into the system prompt. The new Assumptions section explicitly acknowledges `pr_state.py:132` pre-fetches `baseRefName` but uses it to *parameterize* tooling, not to dump content into prompts.

2. **Structured output for humans — clean.** The new `metadata` footer (`complexity_tier`/`parallel_phases`) and the inline `<!-- egg-hitl-decision -->` markers are both *machine-consumed* (by the HITL processor and plan-phase sizer), not human-facing. Markdown prose remains the human surface. This is the correct structured-output use case.

3. **Post-processing pipelines — clean.** BRC consensus is the primary convergence mechanism; the draft explicitly rejects layering a babysit-level loop on top ("No babysit-level iteration cap. BRC owns convergence"). No post-validators, no chained fixup agents.

4. **Rigid procedures — clean.** Producer/reviewer prompts remain objective-driven (e.g., `pipelines.py:6048` "begin reviewing with `git diff origin/main...HEAD`" is a *hint*, not a checklist). Decision 5 (`conflict_resolver` on-demand) preserves agent judgment for when to request help rather than hard-gating every cycle. Feedback-4 explicitly names "soft orientation hint preferable to a hard cap" — correct framing.

5. **Prompt-level security — clean.** File boundaries enforced by `EGG_AGENT_FILE_PATTERNS` (container-level) and push rules by `gateway/policy.py:535` — not by prompt instructions. Staging-branch isolation is orchestration-level, not a prompt assertion.

6. **Direct LLM API calls (EGG200) — clean.** Only `egg_agent.build_agent_command(...)` references (`fixer.py:57`, `reviewer.py:63`), which is the Agent SDK wrapper. No new direct Anthropic API calls introduced.

7. **Bypassing Agent SDK — clean.** All new spawns flow through `create_concurrent_spawn_fn()` (`container_spawner.py:1140`) inheriting `EGG_CONCURRENT_MODE=true`, `EGG_BRC_ROLE_TYPE`, etc. The old babysit path (bare `build_agent_command` without `--agent-type`) is being *removed*, not preserved. Net improvement.

8. **Hardcoded model IDs (EGG201) — clean.** Only the `"sonnet"` alias appears (existing, not newly introduced). No `claude-sonnet-4-5-*` or other fully-qualified model strings.

### Non-blocking agent-design observations (informational)

- **Option D (refactor per-role orient builders first) is the agent-mode-preferred long-run path** — per-mode templates beat growing `if/elif` in `_build_reviewer_preparation` / `_build_producer_orientation`. Decision 7 correctly surfaces this as a tradeoff rather than hiding it; I would ACK either Option A or Option D as consistent with agent-mode principles. The "defer entirely" option in Decision 7 is the only one that would accumulate technical debt in a way that eventually becomes an anti-pattern (orient builders becoming unreviewable) — flagging for the human's awareness but not blocking.

- **Feedback-1 (additional reviewer pre-filters) is the right agent-design question to ask.** Spawning a reviewer whose upstream artifacts don't exist is either a silent-failure mode or produces vacuous reviews — both agent-mode smells. Good that it's explicit.

- **Decision 3 "mirror /sdlc exactly" is the agent-mode-consistent default** for the MCP skill — it matches `skills/sdlc/SKILL.md` precedent and avoids a second UX surface that agents have to learn. But any of the three is defensible.

### Verdict
v2 preserves all of v1's agent-mode cleanliness and adds no new anti-patterns. The Option D path (Decision 7) is specifically aligned with agent-mode design; the inline HITL markers and metadata footer are proper machine-consumable structured output. Cleanly aligns with `docs/guides/agent-mode-design.md`. **Good to proceed.**

````yaml
id: ad07e382-cfe3-4b
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1748-analysis.md
    reason: "Re-reviewed v2 draft `.egg-state/drafts/1748-analysis.md` at commit ef462b57b\
      \ (180 lines, +107/-73 vs v1). Evaluated against the 8 agent-mode design anti-patterns.\
      \ No blocking concerns.\n\n### v2 delta summary\n- Decisions/feedback converted\
      \ from shell-command blocks to inline `<!-- egg-hitl-decision/feedback id=N\
      \ -->` markers (machine-scannable by HITL processor per `1028-analysis.md:259-291`).\n\
      - Added Decision 6 (base-branch parameterization scope: full sweep vs mode-gated\
      \ vs v1-restricted-to-main).\n- Added Decision 7 (refactor `_build_*_orientation`\
      \ first, Option D) and a matching \"Option D\" in Options Considered.\n- Added\
      \ an Assumptions section separating inherited-substrate invariants (BRC-CONFIRMED\
      \ requirement, pre-fetched baseRefName, health-monitor phase-timeouts) from\
      \ new Constraints.\n- Added metadata footer (`complexity_tier: high`, `parallel_phases:\
      \ true`).\n- Named existing base-aware diff helpers in Current Behavior (`pipelines.py:2972-2973`,\
      \ `:3119-3123`, `:5855`, `get_default_branch()` at `:4145-4202`).\n\n### Anti-pattern\
      \ evaluation (domain: agent-mode design only)\n\n1. **Pre-fetching (excessive\
      \ context) \u2014 clean.** Orient prompts remain tools-based \u2014 reviewers\
      \ invoke `git diff base...HEAD` at runtime, producers read the PR diff via git.\
      \ No baked-in diffs, no pre-serialized PR metadata stuffed into the system prompt.\
      \ The new Assumptions section explicitly acknowledges `pr_state.py:132` pre-fetches\
      \ `baseRefName` but uses it to *parameterize* tooling, not to dump content into\
      \ prompts.\n\n2. **Structured output for humans \u2014 clean.** The new `metadata`\
      \ footer (`complexity_tier`/`parallel_phases`) and the inline `<!-- egg-hitl-decision\
      \ -->` markers are both *machine-consumed* (by the HITL processor and plan-phase\
      \ sizer), not human-facing. Markdown prose remains the human surface. This is\
      \ the correct structured-output use case.\n\n3. **Post-processing pipelines\
      \ \u2014 clean.** BRC consensus is the primary convergence mechanism; the draft\
      \ explicitly rejects layering a babysit-level loop on top (\"No babysit-level\
      \ iteration cap. BRC owns convergence\"). No post-validators, no chained fixup\
      \ agents.\n\n4. **Rigid procedures \u2014 clean.** Producer/reviewer prompts\
      \ remain objective-driven (e.g., `pipelines.py:6048` \"begin reviewing with\
      \ `git diff origin/main...HEAD`\" is a *hint*, not a checklist). Decision 5\
      \ (`conflict_resolver` on-demand) preserves agent judgment for when to request\
      \ help rather than hard-gating every cycle. Feedback-4 explicitly names \"soft\
      \ orientation hint preferable to a hard cap\" \u2014 correct framing.\n\n5.\
      \ **Prompt-level security \u2014 clean.** File boundaries enforced by `EGG_AGENT_FILE_PATTERNS`\
      \ (container-level) and push rules by `gateway/policy.py:535` \u2014 not by\
      \ prompt instructions. Staging-branch isolation is orchestration-level, not\
      \ a prompt assertion.\n\n6. **Direct LLM API calls (EGG200) \u2014 clean.**\
      \ Only `egg_agent.build_agent_command(...)` references (`fixer.py:57`, `reviewer.py:63`),\
      \ which is the Agent SDK wrapper. No new direct Anthropic API calls introduced.\n\
      \n7. **Bypassing Agent SDK \u2014 clean.** All new spawns flow through `create_concurrent_spawn_fn()`\
      \ (`container_spawner.py:1140`) inheriting `EGG_CONCURRENT_MODE=true`, `EGG_BRC_ROLE_TYPE`,\
      \ etc. The old babysit path (bare `build_agent_command` without `--agent-type`)\
      \ is being *removed*, not preserved. Net improvement.\n\n8. **Hardcoded model\
      \ IDs (EGG201) \u2014 clean.** Only the `\"sonnet\"` alias appears (existing,\
      \ not newly introduced). No `claude-sonnet-4-5-*` or other fully-qualified model\
      \ strings.\n\n### Non-blocking agent-design observations (informational)\n\n\
      - **Option D (refactor per-role orient builders first) is the agent-mode-preferred\
      \ long-run path** \u2014 per-mode templates beat growing `if/elif` in `_build_reviewer_preparation`\
      \ / `_build_producer_orientation`. Decision 7 correctly surfaces this as a tradeoff\
      \ rather than hiding it; I would ACK either Option A or Option D as consistent\
      \ with agent-mode principles. The \"defer entirely\" option in Decision 7 is\
      \ the only one that would accumulate technical debt in a way that eventually\
      \ becomes an anti-pattern (orient builders becoming unreviewable) \u2014 flagging\
      \ for the human's awareness but not blocking.\n\n- **Feedback-1 (additional\
      \ reviewer pre-filters) is the right agent-design question to ask.** Spawning\
      \ a reviewer whose upstream artifacts don't exist is either a silent-failure\
      \ mode or produces vacuous reviews \u2014 both agent-mode smells. Good that\
      \ it's explicit.\n\n- **Decision 3 \"mirror /sdlc exactly\" is the agent-mode-consistent\
      \ default** for the MCP skill \u2014 it matches `skills/sdlc/SKILL.md` precedent\
      \ and avoids a second UX surface that agents have to learn. But any of the three\
      \ is defensible.\n\n### Verdict\nv2 preserves all of v1's agent-mode cleanliness\
      \ and adds no new anti-patterns. The Option D path (Decision 7) is specifically\
      \ aligned with agent-mode design; the inline HITL markers and metadata footer\
      \ are proper machine-consumable structured output. Cleanly aligns with `docs/guides/agent-mode-design.md`.\
      \ **Good to proceed.**"
  version: 2
````

### [2026-04-16T18:29:13Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: c8db9d54-d37b-40
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-16T18:30:16Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner

ACK on refiner re-proposal (commit ef462b57b23561fc017e61370cfefd1e8ee88612).

All 3 blocking NACK items addressed:

**Blocking 1 (HITL marker format + contract claim) — RESOLVED.** The draft now uses inline `<!-- egg-hitl-decision id=decision-N -->` and `<!-- egg-hitl-feedback id=feedback-N -->` markers (lines 130, 136, 142, 148, 154, 160, 166 for decisions 1-7; lines 174, 177, 180, 183, 186, 189 for feedback 1-6), matching the reference format at `.egg-state/drafts/1028-analysis.md:255-291` exactly (leading marker comment, bold-numbered title, bullet-list options). I re-verified the contract-existence claim directly: `EGG_ISSUE_NUMBER=1748 egg-contract show` returns "Contract for #1748 not found" and `.egg-state/contracts/1748.json` does not exist — my original NACK assertion on this point was wrong, and the refiner's inline-marker fallback is the correct path.

**Blocking 2 (feedback block structure) — RESOLVED.** The 6 feedback items are now 6 distinct `<!-- egg-hitl-feedback -->` markers (lines 174-190) rather than a single `add-feedback` invocation with stacked `--question` flags. Each marker owns one question, one scope.

**Blocking 3 (base-branch parameterization scope) — RESOLVED.** New Decision 6 (lines 160-164) sizes the sweep with three concrete options (full sweep / mode-gated / v1-main-only) so plan-phase has a defined task boundary rather than an unsized invariant.

Non-blocking observations also addressed: (a) "BRC reviewers must reach CONFIRMED" moved to a new Assumptions section at lines 48-52 (correctly framed as inherited substrate, not a new rule); (b) "how to filter reviewer_contract" pulled out of Constraints into Decision 2; (c) existing base-aware diff helpers named in-line at lines 35 (`pipelines.py:2972-2973`, `:3119-3123`, `:5855`, `get_default_branch` at `:4145-4202`); (d) Option D added at lines 102-114 capturing the refactor-first middle path with honest pros/cons; (e) structured `metadata` footer added at lines 200-202.

Technical citations spot-checked against source:
- `_PHASE_ROLES["implement"]` at `agent_roles.py:972` ✓
- `_PHASE_REVIEWERS["implement"]` at `:978` ✓
- `get_roles_for_phase()` at `:1002` ✓
- `REVIEWER_CONTRACT` dependencies `[TASK_PLANNER, RISK_ANALYST]` at `:534` ✓
- `CONFLICT_RESOLVER_ROLE` at `:746` (defined, not in phase roster) ✓
- `_build_reviewer_preparation` at `pipelines.py:6031` ✓
- `_build_producer_orientation` at `:6118` ✓
- Hardcoded `origin/main` at `:6048` in reviewer-code branch ✓
- `_write_brc_history` at `:4353` ✓
- `_build_brc_consensus_summary` at `:4929` ✓
- `create_concurrent_spawn_fn` at `container_spawner.py:1140` ✓
- `ConcurrentPhaseExecutor` at `concurrent_executor.py:55` with worktree logic at `:109-119` ✓
- `PeerConsensusTracker` at `peer_consensus.py:69`; cooldown/flip-flop/revision defaults at `:64-66` ✓
- `MessageType` constants at `message_store.py:19-38` (draft says `:28` which is the `CONSENSUS_PROPOSE` line specifically; acceptable) ✓
- `baseRefName` fetched at `pr_state.py:132` ✓
- `PipelineMode` at `models.py:28` ✓

Minor note: draft cites `_run_concurrent_phase()` at `pipelines.py:6829` but actual line is `:6820` (9-line drift, likely from in-flight edits). Non-blocking — the function is unambiguously identified by name.

The draft correctly sizes complexity as **high** (7 decisions + 6 feedback items + multi-subsystem touch across `agent_roles.py`, `routes/pipelines.py`, `concurrent_executor.py`, `health_checks/`, package removal in `shared/egg_babysit/`, new MCP skill). Option A is the right recommendation for the "reuse, don't fork" mandate; Option D is offered as the principled alternative if a third mode is on the roadmap, and Decision 7 surfaces that tradeoff to the human rather than deciding unilaterally.

No remaining blocking issues. ACK.

````yaml
id: 22043648-881b-42
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1748-analysis.md
    - ef462b57b23561fc017e61370cfefd1e8ee88612
    reason: "ACK on refiner re-proposal (commit ef462b57b23561fc017e61370cfefd1e8ee88612).\n\
      \nAll 3 blocking NACK items addressed:\n\n**Blocking 1 (HITL marker format +\
      \ contract claim) \u2014 RESOLVED.** The draft now uses inline `<!-- egg-hitl-decision\
      \ id=decision-N -->` and `<!-- egg-hitl-feedback id=feedback-N -->` markers\
      \ (lines 130, 136, 142, 148, 154, 160, 166 for decisions 1-7; lines 174, 177,\
      \ 180, 183, 186, 189 for feedback 1-6), matching the reference format at `.egg-state/drafts/1028-analysis.md:255-291`\
      \ exactly (leading marker comment, bold-numbered title, bullet-list options).\
      \ I re-verified the contract-existence claim directly: `EGG_ISSUE_NUMBER=1748\
      \ egg-contract show` returns \"Contract for #1748 not found\" and `.egg-state/contracts/1748.json`\
      \ does not exist \u2014 my original NACK assertion on this point was wrong,\
      \ and the refiner's inline-marker fallback is the correct path.\n\n**Blocking\
      \ 2 (feedback block structure) \u2014 RESOLVED.** The 6 feedback items are now\
      \ 6 distinct `<!-- egg-hitl-feedback -->` markers (lines 174-190) rather than\
      \ a single `add-feedback` invocation with stacked `--question` flags. Each marker\
      \ owns one question, one scope.\n\n**Blocking 3 (base-branch parameterization\
      \ scope) \u2014 RESOLVED.** New Decision 6 (lines 160-164) sizes the sweep with\
      \ three concrete options (full sweep / mode-gated / v1-main-only) so plan-phase\
      \ has a defined task boundary rather than an unsized invariant.\n\nNon-blocking\
      \ observations also addressed: (a) \"BRC reviewers must reach CONFIRMED\" moved\
      \ to a new Assumptions section at lines 48-52 (correctly framed as inherited\
      \ substrate, not a new rule); (b) \"how to filter reviewer_contract\" pulled\
      \ out of Constraints into Decision 2; (c) existing base-aware diff helpers named\
      \ in-line at lines 35 (`pipelines.py:2972-2973`, `:3119-3123`, `:5855`, `get_default_branch`\
      \ at `:4145-4202`); (d) Option D added at lines 102-114 capturing the refactor-first\
      \ middle path with honest pros/cons; (e) structured `metadata` footer added\
      \ at lines 200-202.\n\nTechnical citations spot-checked against source:\n- `_PHASE_ROLES[\"\
      implement\"]` at `agent_roles.py:972` \u2713\n- `_PHASE_REVIEWERS[\"implement\"\
      ]` at `:978` \u2713\n- `get_roles_for_phase()` at `:1002` \u2713\n- `REVIEWER_CONTRACT`\
      \ dependencies `[TASK_PLANNER, RISK_ANALYST]` at `:534` \u2713\n- `CONFLICT_RESOLVER_ROLE`\
      \ at `:746` (defined, not in phase roster) \u2713\n- `_build_reviewer_preparation`\
      \ at `pipelines.py:6031` \u2713\n- `_build_producer_orientation` at `:6118`\
      \ \u2713\n- Hardcoded `origin/main` at `:6048` in reviewer-code branch \u2713\
      \n- `_write_brc_history` at `:4353` \u2713\n- `_build_brc_consensus_summary`\
      \ at `:4929` \u2713\n- `create_concurrent_spawn_fn` at `container_spawner.py:1140`\
      \ \u2713\n- `ConcurrentPhaseExecutor` at `concurrent_executor.py:55` with worktree\
      \ logic at `:109-119` \u2713\n- `PeerConsensusTracker` at `peer_consensus.py:69`;\
      \ cooldown/flip-flop/revision defaults at `:64-66` \u2713\n- `MessageType` constants\
      \ at `message_store.py:19-38` (draft says `:28` which is the `CONSENSUS_PROPOSE`\
      \ line specifically; acceptable) \u2713\n- `baseRefName` fetched at `pr_state.py:132`\
      \ \u2713\n- `PipelineMode` at `models.py:28` \u2713\n\nMinor note: draft cites\
      \ `_run_concurrent_phase()` at `pipelines.py:6829` but actual line is `:6820`\
      \ (9-line drift, likely from in-flight edits). Non-blocking \u2014 the function\
      \ is unambiguously identified by name.\n\nThe draft correctly sizes complexity\
      \ as **high** (7 decisions + 6 feedback items + multi-subsystem touch across\
      \ `agent_roles.py`, `routes/pipelines.py`, `concurrent_executor.py`, `health_checks/`,\
      \ package removal in `shared/egg_babysit/`, new MCP skill). Option A is the\
      \ right recommendation for the \"reuse, don't fork\" mandate; Option D is offered\
      \ as the principled alternative if a third mode is on the roadmap, and Decision\
      \ 7 surfaces that tradeoff to the human rather than deciding unilaterally.\n\
      \nNo remaining blocking issues. ACK."
  version: 2
````

### [2026-04-16T18:30:16Z] orchestrator → refiner (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 2). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 1f8b3c8c-3254-44
phase: refine
metadata:
  fully_acked: true
  version: 2
````

### [2026-04-16T18:30:29Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: 724a3a60-401a-44
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-16T18:30:35Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: 6b3101fd-a169-42
phase: refine
metadata:
  consensus_reached: true
````
