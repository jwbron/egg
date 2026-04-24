# BRC Consensus History — refine phase

Generated: 2026-04-24T19:59:26Z
Pipeline: issue-1973

### [2026-04-24T19:41:03Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_ON_ROLE

reviewer_refine prepared; waiting for refiner CONSENSUS_PROPOSE on 1973-analysis.md

````yaml
id: f2934a57-d1e1-4b
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
````

### [2026-04-24T19:41:30Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Reviewed `.egg-state/drafts/1973-analysis.md` from the agent-design perspective only (per reviewer scope — general quality / correctness / research coverage are out of scope here).

No agent-mode anti-patterns found. The analysis is about test-selection infrastructure (`make test` narrowing via grimp + LKG baseline) and doesn't propose anything that touches the agent-design guardrails:

- **No pre-fetching into prompts.** Selection logic runs in `scripts/`/`Makefile`, not inside an agent prompt; no diffs or file contents are baked into prompts anywhere.
- **No structured-output-for-humans trap.** Output is the list of test files piped to `pytest`; no human-facing JSON coercion proposed.
- **No post-processing pipeline.** The grep-for-`importlib` widening step is an input to selection, not a parser of agent output.
- **No rigid agent procedures.** The doc constrains a build tool's behaviour, not an agent's.
- **No LLM-API-outside-sandbox / direct-API / hardcoded-model-id concerns.** Nothing in the proposal invokes Anthropic or any other model.
- **Agent-sandbox coupling is called out correctly.** §Constraints bullet 5 and Open Question 14 flag the gateway file-boundary rules for the refiner/coder/tester roles pushing `.egg/last-known-good`; the Recommended Approach explicitly flips to a gitignored sidecar to sidestep that, which is the agent-friendly call. Good instinct — sidecar LKG keeps pipeline agents from having to negotiate role-boundary permissions for a cache file.

### Non-blocking
- **§Open Questions #14** is implicitly load-bearing — if the human picks the tracked-file option, the agent-mode implication (needing a role-agnostic allowlist in the gateway) should be surfaced as a visible risk in the plan phase, not buried as a contingency in the contract. Worth a one-line callout in §Recommended Approach bullet 4 so the plan-phase designer sees it without having to chase the decision back through the contract.


````yaml
id: f2b8f14f-956e-40
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1973-analysis.md
    reason: "\nReviewed `.egg-state/drafts/1973-analysis.md` from the agent-design\
      \ perspective only (per reviewer scope \u2014 general quality / correctness\
      \ / research coverage are out of scope here).\n\nNo agent-mode anti-patterns\
      \ found. The analysis is about test-selection infrastructure (`make test` narrowing\
      \ via grimp + LKG baseline) and doesn't propose anything that touches the agent-design\
      \ guardrails:\n\n- **No pre-fetching into prompts.** Selection logic runs in\
      \ `scripts/`/`Makefile`, not inside an agent prompt; no diffs or file contents\
      \ are baked into prompts anywhere.\n- **No structured-output-for-humans trap.**\
      \ Output is the list of test files piped to `pytest`; no human-facing JSON coercion\
      \ proposed.\n- **No post-processing pipeline.** The grep-for-`importlib` widening\
      \ step is an input to selection, not a parser of agent output.\n- **No rigid\
      \ agent procedures.** The doc constrains a build tool's behaviour, not an agent's.\n\
      - **No LLM-API-outside-sandbox / direct-API / hardcoded-model-id concerns.**\
      \ Nothing in the proposal invokes Anthropic or any other model.\n- **Agent-sandbox\
      \ coupling is called out correctly.** \xA7Constraints bullet 5 and Open Question\
      \ 14 flag the gateway file-boundary rules for the refiner/coder/tester roles\
      \ pushing `.egg/last-known-good`; the Recommended Approach explicitly flips\
      \ to a gitignored sidecar to sidestep that, which is the agent-friendly call.\
      \ Good instinct \u2014 sidecar LKG keeps pipeline agents from having to negotiate\
      \ role-boundary permissions for a cache file.\n\n### Non-blocking\n- **\xA7\
      Open Questions #14** is implicitly load-bearing \u2014 if the human picks the\
      \ tracked-file option, the agent-mode implication (needing a role-agnostic allowlist\
      \ in the gateway) should be surfaced as a visible risk in the plan phase, not\
      \ buried as a contingency in the contract. Worth a one-line callout in \xA7\
      Recommended Approach bullet 4 so the plan-phase designer sees it without having\
      \ to chase the decision back through the contract.\n"
  version: 0
````

### [2026-04-24T19:41:33Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design (pending_acks)

Agent reviewer_agent_design cannot confirm: producers ['refiner'] have never proposed (proposal_version == 0). All producers must propose before any agent can confirm consensus.

````yaml
id: 3c194ea0-a516-44
phase: refine
metadata:
  pending_acks: true
````

### [2026-04-24T19:41:55Z] overseer (OVERSEER_ALERT): agent-heartbeat-stall [high]

refiner agent in pipeline issue-1973 is stuck in WORKING state and has never submitted a CONSENSUS_PROPOSE (proposal_version=0)

Detail:
BRC phase: refine. reviewer_refine is WAITING_ON_ROLE for refiner since 2026-04-24T19:41:03Z. reviewer_agent_design attempted CONSENSUS_CONFIRMED and was rejected because refiner has proposal_version==0 (no proposal ever submitted). refiner has been in producer_phase=WORKING for multiple cycles with no progress_events and no CONSENSUS_PROPOSE emitted. Artifact expected: 1973-analysis.md

Recommended action:
Inspect refiner agent logs/checkpoints for errors; if stalled, restart or manually trigger refiner to submit its CONSENSUS_PROPOSE for 1973-analysis.md

````yaml
id: 7aff6b6d-1d49-4b
phase: refine
````

### [2026-04-24T19:43:32Z] overseer (OVERSEER_ALERT): stuck-phase-transition [high]

Pipeline issue-1973 is in hard deadlock: SDLC contract is empty (no tasks, phases, or acceptance criteria) — refiner agent cannot produce any work

Detail:
Root cause: mcp__sdlc__show_contract returned an empty contract (phases=[], acceptance_criteria=[], agent_executions=[], decisions=[], phase_configs=null). mcp__phase__get_assigned_tasks for refiner returned 0 tasks. mcp__phase__get_context returned 0 tasks and 0 artifacts for refiner in refine phase. The refiner agent is running but has no input to work with and therefore cannot produce 1973-analysis.md or emit CONSENSUS_PROPOSE. reviewer_refine and reviewer_agent_design are both blocked waiting. The BRC consensus cycle cannot complete. No checkpoints exist for any agent in this pipeline.

Recommended action:
Human action required: (1) Verify the contract bootstrap step ran correctly for issue-1973. (2) Populate the contract with the correct phases, tasks, and acceptance criteria for the refine phase. (3) Once populated, refiner may need to be restarted so it can pick up its tasks. Alternatively, cancel the pipeline and re-trigger from scratch if bootstrap data is available.

````yaml
id: 8790857a-3607-4d
phase: refine
````

### [2026-04-24T19:44:37Z] overseer (HEARTBEAT): heartbeat: WORKING

Overseer monitoring cycle active. Pipeline in deadlock: refiner has empty contract (0 tasks), cannot propose. Two HIGH OVERSEER_ALERTs sent. Awaiting human intervention to populate contract or cancel pipeline.

````yaml
id: 7d570a92-19c9-46
phase: refine
metadata:
  state: WORKING
````

### [2026-04-24T19:46:53Z] overseer (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Cycle 10. Pipeline deadlocked (refiner: 0 tasks, never proposed). Awaiting operator response to two HIGH OVERSEER_ALERTs. Duration stalled: ~6 min.

````yaml
id: 78be7a3b-bc2b-48
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
  since: '2026-04-24T19:41:03.643955+00:00'
````

### [2026-04-24T19:53:14Z] overseer (OVERSEER_ALERT): stuck-phase-transition [low]

CORRECTION: refiner is NOT stalled — it hit a non-fast-forward push rejection and is actively recovering via git reset + re-propose

Detail:
Earlier HIGH alerts were incorrect. Container logs (19:51:15–19:52:49) show: refiner wrote and committed 1973-analysis.md locally, called mcp__brc__propose which failed (push rejected: non-fast-forward, remote 92f2750e7 ahead). Refiner then fetched, confirmed 1973-analysis.md already exists on origin/egg/issue-1973, performed git reset --hard origin/egg/issue-1973, and is now reading the existing analysis for re-propose. Refiner has been actively working the entire time. BRC proposal_version=0 was because the push never completed. Recovery is in progress.

Recommended action:
No human action required at this time. Monitor for refiner to re-commit and re-propose. If push fails again (another non-fast-forward), investigate branch state on origin/egg/issue-1973.

````yaml
id: ee4b93d6-5b25-4c
phase: refine
````

### [2026-04-24T19:56:13Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Refine-phase analysis for issue #1973 (changeset-aware `make test`) — reconciliation pass. The on-disk analysis at .egg-state/drafts/1973-analysis.md (previously reviewed with non-blocking nits addressed in commit fdcd93c61) had its Open Questions prose referencing 9 decisions + 11 free-form items, but the SDLC contract only held a subset. This reconciliation adds decision-9 (LKG storage medium), decision-10 (dynamic-import handling), decision-11 (CI checkout depth), and decision-12 (graph granularity) to close the gap, and replaces the feedback group with feedback-3 (16 open-ended questions covering perf target, shared-fixture enumeration, fallback-trigger review, canary mode, explicit pytest args, --why flag, fallback-cause printing, testmon follow-up, parallel-agent races, integration scope, docs placement, partial-pass LKG behavior, read-only agent phases, bootstrap, selection logging, and worktree keying). Analysis body is unchanged: grimp-based static reverse import graph as primary selector with conservative fallback triggers, analysis recommends the sidecar LKG storage (flipping the issue's default) with plan-phase confirmation. Complexity: high.

````yaml
id: d7fba75f-ad2a-4b
phase: refine
metadata:
  payload:
    summary: "Refine-phase analysis for issue #1973 (changeset-aware `make test`)\
      \ \u2014 reconciliation pass. The on-disk analysis at .egg-state/drafts/1973-analysis.md\
      \ (previously reviewed with non-blocking nits addressed in commit fdcd93c61)\
      \ had its Open Questions prose referencing 9 decisions + 11 free-form items,\
      \ but the SDLC contract only held a subset. This reconciliation adds decision-9\
      \ (LKG storage medium), decision-10 (dynamic-import handling), decision-11 (CI\
      \ checkout depth), and decision-12 (graph granularity) to close the gap, and\
      \ replaces the feedback group with feedback-3 (16 open-ended questions covering\
      \ perf target, shared-fixture enumeration, fallback-trigger review, canary mode,\
      \ explicit pytest args, --why flag, fallback-cause printing, testmon follow-up,\
      \ parallel-agent races, integration scope, docs placement, partial-pass LKG\
      \ behavior, read-only agent phases, bootstrap, selection logging, and worktree\
      \ keying). Analysis body is unchanged: grimp-based static reverse import graph\
      \ as primary selector with conservative fallback triggers, analysis recommends\
      \ the sidecar LKG storage (flipping the issue's default) with plan-phase confirmation.\
      \ Complexity: high."
    attestation: {}
    artifacts:
    - .egg-state/drafts/1973-analysis.md
    risk_considered: "Primary risks surfaced in the analysis: (1) Gateway file-boundary\
      \ enforcement vs. tracked LKG (decision-8, decision-9); (2) CI coverage gate\
      \ incompatibility with narrowed selection (decision-2); (3) dynamic imports\
      \ in gateway/ hide edges from static graph (decision-10); (4) shallow CI checkout\
      \ blocks base-branch diffs (decision-11); (5) non-Python resource tests not\
      \ reachable via import graph (decision-5). All flagged as HITL decisions on\
      \ the contract \u2014 plan phase cannot proceed until the human resolves them."
    commit_sha: 9fa926edac3b317fdaa9952f9512da7e8abda2e4
    files_changed:
    - .egg-state/drafts/1973-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 1
  commit_sha: 9fa926edac3b317fdaa9952f9512da7e8abda2e4
````

### [2026-04-24T19:56:13Z] orchestrator → reviewer_agent_design (CONSENSUS_RE_REVIEW): Re-review required: refiner submitted new proposal v1

Producer refiner has submitted a new proposal (version 1) after withdrawal. Your previous confirmation was on an earlier version. Please re-review and ACK/NACK the new proposal.

````yaml
id: f4070283-2d4b-4a
phase: refine
metadata:
  producer_role: refiner
  version: 1
````

### [2026-04-24T19:56:52Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Re-reviewed v1 (commit 9fa926edac3b317fdaa9952f9512da7e8abda2e4) from the agent-design perspective only. Diff from my prior review was confined to the "Open Questions" section (reorganized into 12 `decision-N` HITL items + 16 `Q*` open-ended items, cross-referenced to the contract); the load-bearing content (Problem, Current Behavior, Constraints, Options, Recommended Approach) is unchanged.

No agent-mode anti-patterns found in the new version either:

- **No pre-fetching into agent prompts.** The selection logic runs in `scripts/` / `Makefile` at shell time, not inside an agent prompt. Nothing in the design bakes diffs or file contents into prompts.
- **No structured-output-for-humans trap.** The tool emits a test-file list to `pytest` and a selection-decision log (see `Q15` — stderr vs JSON vs both) — both are mechanical-consumer outputs, not human-facing JSON.
- **No post-processing pipeline parsing agent output.** The grep-for-`importlib` widening scans *source code*, not agent transcripts.
- **No rigid agent procedure.** The doc constrains a build tool's behaviour, not an agent's workflow.
- **No direct LLM API calls / bypassed Agent SDK / hardcoded model IDs.** Nothing in the proposal invokes Anthropic or any other model; there is no model code anywhere in the design surface.
- **Agent-sandbox coupling is correctly handled.** §Constraints bullet 5 ("Agent sandbox environment"), §Recommended Approach bullet 4 (flip to sidecar), and `decision-8` / `decision-9` / `Q9` / `Q13` / `Q16` in the contract together capture the gateway role-policy, worktree aliasing, read-only-phase, and concurrent-writer concerns. The sidecar recommendation explicitly sidesteps the "refiner/coder/tester auto-commits `.egg/last-known-good`" path that would otherwise force a role-agnostic gateway allowlist — that's the agent-friendly call and is reasoned about correctly.

### Non-blocking
- **Agent-mode callout in §Recommended Approach bullet 4.** The sidecar recommendation mentions "no clean answer for 'who may push `.egg/last-known-good`?' under the gateway's role-boundary rules" in passing. If the human picks the tracked-file option at `decision-9`, the plan-phase designer will need to explicitly allowlist the file for every pipeline role (refiner/coder/tester/reviewer) in the gateway's file-boundary policy, *and* keep it from colliding with the per-role allowlists. Worth surfacing as a one-liner risk in bullet 4 so that constraint travels into plan phase without the designer having to re-derive it from the contract. Non-blocking because the analysis already makes the agent-friendly recommendation; this is just so the alternative's agent-mode cost is visible if the human overrides it.
- **`Q13` ("read-only agent phases: can they READ LKG") is well-framed** — the correct agent-mode answer is "yes, read is always fine; write stays gated." If the human defers this, the plan phase should default to that semantics rather than leaving it open, since a READ that falls back to base branch is the safe option for every role.


````yaml
id: 2cd6ada1-78b4-4e
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1973-analysis.md
    reason: "\nRe-reviewed v1 (commit 9fa926edac3b317fdaa9952f9512da7e8abda2e4) from\
      \ the agent-design perspective only. Diff from my prior review was confined\
      \ to the \"Open Questions\" section (reorganized into 12 `decision-N` HITL items\
      \ + 16 `Q*` open-ended items, cross-referenced to the contract); the load-bearing\
      \ content (Problem, Current Behavior, Constraints, Options, Recommended Approach)\
      \ is unchanged.\n\nNo agent-mode anti-patterns found in the new version either:\n\
      \n- **No pre-fetching into agent prompts.** The selection logic runs in `scripts/`\
      \ / `Makefile` at shell time, not inside an agent prompt. Nothing in the design\
      \ bakes diffs or file contents into prompts.\n- **No structured-output-for-humans\
      \ trap.** The tool emits a test-file list to `pytest` and a selection-decision\
      \ log (see `Q15` \u2014 stderr vs JSON vs both) \u2014 both are mechanical-consumer\
      \ outputs, not human-facing JSON.\n- **No post-processing pipeline parsing agent\
      \ output.** The grep-for-`importlib` widening scans *source code*, not agent\
      \ transcripts.\n- **No rigid agent procedure.** The doc constrains a build tool's\
      \ behaviour, not an agent's workflow.\n- **No direct LLM API calls / bypassed\
      \ Agent SDK / hardcoded model IDs.** Nothing in the proposal invokes Anthropic\
      \ or any other model; there is no model code anywhere in the design surface.\n\
      - **Agent-sandbox coupling is correctly handled.** \xA7Constraints bullet 5\
      \ (\"Agent sandbox environment\"), \xA7Recommended Approach bullet 4 (flip to\
      \ sidecar), and `decision-8` / `decision-9` / `Q9` / `Q13` / `Q16` in the contract\
      \ together capture the gateway role-policy, worktree aliasing, read-only-phase,\
      \ and concurrent-writer concerns. The sidecar recommendation explicitly sidesteps\
      \ the \"refiner/coder/tester auto-commits `.egg/last-known-good`\" path that\
      \ would otherwise force a role-agnostic gateway allowlist \u2014 that's the\
      \ agent-friendly call and is reasoned about correctly.\n\n### Non-blocking\n\
      - **Agent-mode callout in \xA7Recommended Approach bullet 4.** The sidecar recommendation\
      \ mentions \"no clean answer for 'who may push `.egg/last-known-good`?' under\
      \ the gateway's role-boundary rules\" in passing. If the human picks the tracked-file\
      \ option at `decision-9`, the plan-phase designer will need to explicitly allowlist\
      \ the file for every pipeline role (refiner/coder/tester/reviewer) in the gateway's\
      \ file-boundary policy, *and* keep it from colliding with the per-role allowlists.\
      \ Worth surfacing as a one-liner risk in bullet 4 so that constraint travels\
      \ into plan phase without the designer having to re-derive it from the contract.\
      \ Non-blocking because the analysis already makes the agent-friendly recommendation;\
      \ this is just so the alternative's agent-mode cost is visible if the human\
      \ overrides it.\n- **`Q13` (\"read-only agent phases: can they READ LKG\") is\
      \ well-framed** \u2014 the correct agent-mode answer is \"yes, read is always\
      \ fine; write stays gated.\" If the human defers this, the plan phase should\
      \ default to that semantics rather than leaving it open, since a READ that falls\
      \ back to base branch is the safe option for every role.\n"
  version: 1
````

### [2026-04-24T19:56:55Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: 86a07941-a3fa-45
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T19:59:16Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Reviewed the refine-phase analysis at `.egg-state/drafts/1973-analysis.md` (179 lines) and cross-referenced the SDLC contract (12 decisions + 16 feedback questions in `feedback-3`). Spot-checked every major cited reference against the tree at merged HEAD (`9fa926edac3b317fdaa9952f9512da7e8abda2e4`): `Makefile:251-254`, `pyproject.toml:181-201` and `:208`, `.github/workflows/test.yml` (steps 22/27/37), the four `conftest.py` sys.path chain, `gateway/gateway.py:300-322` dynamic-import block, and `.pre-commit-config.yaml`. Verdict: **ACK, no blocking issues**. This is a substantive, honest, well-researched refine output that meets the phase's bar and is ready to hand to plan.

### Criterion-by-criterion evaluation

**1. Problem Understanding — strong.** §"Problem Statement" correctly distills the issue's goal ("only the tests that could be affected by the current branch's diff" with correctness-first and full-suite escape hatch). §"Current Behavior" quantifies the status quo (356 test files vs 283 source `.py` files, full suite on every invocation) — which is exactly what makes the problem real.

**2. Research Quality — strong.** The analysis does the codebase legwork the issue text explicitly demanded ("the refine phase should do its own exploration"). Concrete evidence:
- Catalogs the **mixed install mode** correctly: wheels for `gateway`, 6 × `shared/egg_*`, `sandbox/egg_lib`; `orchestrator` imported bare via `PYTHONPATH` (verified against `pyproject.toml:208` and `Makefile:251`). That is load-bearing for any grimp-style tool configuration.
- Enumerates all four `conftest.py` files that do `sys.path` injection and names the **invisible-to-static-graph** risk they pose — this is the single most important correctness gotcha of the whole proposal and it is called out correctly.
- Names the specific dynamic-import hotspots with file:line refs (`gateway/gateway.py:304`, `:309-322`, `gateway/commit_observer.py:182`, `gateway/git_client.py:1727`, `gateway/filtered_push.py`, `tests/tools/test_discover_tests.py:13-20`) — verified the gateway.py block exists at 300-322.
- Identifies the **`--cov-fail-under=80` gate** in CI as a hard constraint incompatible with narrowed runs. This would have been a latent footgun if missed.
- Identifies the **shallow checkout** (`fetch-depth: 1` default) problem for `git diff origin/main...HEAD` — verified: the workflow uses bare `actions/checkout@v4` without overriding the default depth.
- Notes the **no-merge-queue / no-pre-merge-hook** reality that breaks the issue's proposed "rewrite LKG on merge" mechanic — that deserves explicit surfacing because the issue assumes infrastructure that doesn't exist.
- Compares concrete tools (`grimp`, `pytest-testmon`, `importlab`, `pyan`, `pydeps`) with external-research links. Correctly identifies the Qik `pygraph` plugin as a production precedent.

**3. Options Analysis — strong.** Five meaningfully different options (A: hand-rolled AST, B: grimp, C: pytest-testmon, D: hybrid, E: path-mapping). Trade-offs are articulated per option. Notable:
- Option C's con about "same-process dynamic imports like `gateway/gateway.py:304/:309-322` **are** observed by testmon via `coverage.py`, so the real miss-mode for this codebase is subprocess-crossing coverage" is a well-earned precision call — not boilerplate.
- Option B correctly identifies `find_downstream_modules(module, as_package=True)` as the grimp primitive that matches the needed reverse-closure, so no graph-algorithm code gets hand-rolled.
- Option E is included for completeness and explicitly noted as rejected by the issue — good epistemic hygiene.

**4. Constraints and Dependencies — strong.** §"Constraints" covers the correctness invariant, Python 3.13, mixed install modes, implicit conftest chain, dynamic imports, coverage gate, sandbox role-policy, shallow CI checkouts, absence of merge queue, and parallel-worktree concerns. Each one is tied to a specific code location or mechanism rather than being generic.

**5. Open Questions — strong.** The 12 decisions + 16 `feedback-3` questions I pulled from the contract match the prose list item-for-item. Spot-checked: `decision-3` (selection mechanism), `decision-9` (LKG storage), `decision-2` (CI coverage), `decision-11` (checkout depth), `decision-8` (gateway/role policy) all exist in the contract with sensible, genuinely-different options. None are "yes/no" boilerplate. Q1-Q16 are free-form and cover the last-mile ambiguities (performance target, shared-fixture fallback list, canary mode, `--why` introspection, worktree keying).

**6. Recommendation Quality — strong.** Option B (grimp + sidecar LKG + conservative fallbacks) is justified with eight numbered reasons that each tie back to findings. The recommendation **flips the issue's default** on LKG storage (tracked → sidecar) and explicitly calls that out as needing human sign-off via `decision-9` — this is exactly the behavior refine should exhibit when the issue text itself invites pushback.

**7. HITL Decision Registration — pass.** The contract contains 12 hitl decisions (`decision-1` through `decision-12`) and one `feedback-3` bundle with Q1-Q16, all created by the refiner via the contract gateway. Every open-question in the analysis is cross-referenced to a contract ID. No orphan prose questions were found.

### Non-blocking
- **decision-4 / decision-9 ordering dependency** — `decision-4` asks for the `.egg/last-known-good` file format; `decision-9` asks whether the LKG is a tracked file at all. If the human picks sidecar (opt-1) or "abandon LKG" (opt-4) on `decision-9`, `decision-4` becomes moot or transforms into a question about the sidecar format. The plan phase should treat `decision-9` as the parent decision and rewrite `decision-4` conditionally — the analysis could have said so inline.
- **Minor line-number drift in a few citations** — `tests/conftest.py:12-16` is actually lines 11-16; `orchestrator/tests/conftest.py:22-29` is actually 23-29; `shared/tests/conftest.py:13-16` is actually 14-16. Substantively correct, off by one or two. Not worth re-proposing for.
- **Source file count** — "283 source `.py` files" is off-by-one versus a fresh `find` (I counted 284 excluding `tests/` prune roots). Noise-level.
- **Option B con self-contradiction** — §"Option B" Cons says grimp is "small, pure-Python surface" but §"Option B" Pros correctly calls it "Rust-backed". Fix: drop "pure-Python" from the Cons or say "Rust core + Python API". Non-blocking; the plan phase will sort out the dependency add.
- **"Pre-commit is `ruff` only"** — the pre-commit config also runs `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-added-large-files`. Minor overstatement; the substantive point (no test hook exists today) is accurate.
- **Test-ordering flakes are not listed as a risk** — narrowing a subset of a suite that has latent ordering dependencies can turn "always-flakes-in-CI" into "flakes-when-this-file-is-edited-only", which is harder to diagnose. Worth adding to the constraints list or to the canary-mode question (`Q4`) rationale. Non-blocking for refine; raise it in plan.
- **grimp + Python 3.13 compatibility is asserted but not verified in the analysis** — the plan phase should add a `make` target or `pyproject.toml` dev-extra trial, and confirm `grimp` resolves under `uv`. Not a refine-phase ask.
- **`.egg-readonly` interaction with LKG reads** (captured by `Q13`) — the analysis notes this but doesn't explain whether the sidecar path under `.egg-state/` would respect existing readonly markers; the plan phase should audit `sandbox/` / `egg_restrictions` before committing to the sidecar path.

Overall: this analysis does the job the refine phase exists for — it pushes back on the issue's proposal where the codebase reality demands it, surfaces every load-bearing sub-decision to the human, and gives the plan phase a coherent recommendation plus a list of explicit human gates. ACK.


````yaml
id: d3fe625f-1e24-4a
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1973-analysis.md
    - .egg-state/contracts/issue-1973.json
    - Makefile
    - pyproject.toml
    - .github/workflows/test.yml
    - tests/conftest.py
    - gateway/tests/conftest.py
    - orchestrator/tests/conftest.py
    - shared/tests/conftest.py
    - gateway/gateway.py
    - .pre-commit-config.yaml
    reason: "\nReviewed the refine-phase analysis at `.egg-state/drafts/1973-analysis.md`\
      \ (179 lines) and cross-referenced the SDLC contract (12 decisions + 16 feedback\
      \ questions in `feedback-3`). Spot-checked every major cited reference against\
      \ the tree at merged HEAD (`9fa926edac3b317fdaa9952f9512da7e8abda2e4`): `Makefile:251-254`,\
      \ `pyproject.toml:181-201` and `:208`, `.github/workflows/test.yml` (steps 22/27/37),\
      \ the four `conftest.py` sys.path chain, `gateway/gateway.py:300-322` dynamic-import\
      \ block, and `.pre-commit-config.yaml`. Verdict: **ACK, no blocking issues**.\
      \ This is a substantive, honest, well-researched refine output that meets the\
      \ phase's bar and is ready to hand to plan.\n\n### Criterion-by-criterion evaluation\n\
      \n**1. Problem Understanding \u2014 strong.** \xA7\"Problem Statement\" correctly\
      \ distills the issue's goal (\"only the tests that could be affected by the\
      \ current branch's diff\" with correctness-first and full-suite escape hatch).\
      \ \xA7\"Current Behavior\" quantifies the status quo (356 test files vs 283\
      \ source `.py` files, full suite on every invocation) \u2014 which is exactly\
      \ what makes the problem real.\n\n**2. Research Quality \u2014 strong.** The\
      \ analysis does the codebase legwork the issue text explicitly demanded (\"\
      the refine phase should do its own exploration\"). Concrete evidence:\n- Catalogs\
      \ the **mixed install mode** correctly: wheels for `gateway`, 6 \xD7 `shared/egg_*`,\
      \ `sandbox/egg_lib`; `orchestrator` imported bare via `PYTHONPATH` (verified\
      \ against `pyproject.toml:208` and `Makefile:251`). That is load-bearing for\
      \ any grimp-style tool configuration.\n- Enumerates all four `conftest.py` files\
      \ that do `sys.path` injection and names the **invisible-to-static-graph** risk\
      \ they pose \u2014 this is the single most important correctness gotcha of the\
      \ whole proposal and it is called out correctly.\n- Names the specific dynamic-import\
      \ hotspots with file:line refs (`gateway/gateway.py:304`, `:309-322`, `gateway/commit_observer.py:182`,\
      \ `gateway/git_client.py:1727`, `gateway/filtered_push.py`, `tests/tools/test_discover_tests.py:13-20`)\
      \ \u2014 verified the gateway.py block exists at 300-322.\n- Identifies the\
      \ **`--cov-fail-under=80` gate** in CI as a hard constraint incompatible with\
      \ narrowed runs. This would have been a latent footgun if missed.\n- Identifies\
      \ the **shallow checkout** (`fetch-depth: 1` default) problem for `git diff\
      \ origin/main...HEAD` \u2014 verified: the workflow uses bare `actions/checkout@v4`\
      \ without overriding the default depth.\n- Notes the **no-merge-queue / no-pre-merge-hook**\
      \ reality that breaks the issue's proposed \"rewrite LKG on merge\" mechanic\
      \ \u2014 that deserves explicit surfacing because the issue assumes infrastructure\
      \ that doesn't exist.\n- Compares concrete tools (`grimp`, `pytest-testmon`,\
      \ `importlab`, `pyan`, `pydeps`) with external-research links. Correctly identifies\
      \ the Qik `pygraph` plugin as a production precedent.\n\n**3. Options Analysis\
      \ \u2014 strong.** Five meaningfully different options (A: hand-rolled AST,\
      \ B: grimp, C: pytest-testmon, D: hybrid, E: path-mapping). Trade-offs are articulated\
      \ per option. Notable:\n- Option C's con about \"same-process dynamic imports\
      \ like `gateway/gateway.py:304/:309-322` **are** observed by testmon via `coverage.py`,\
      \ so the real miss-mode for this codebase is subprocess-crossing coverage\"\
      \ is a well-earned precision call \u2014 not boilerplate.\n- Option B correctly\
      \ identifies `find_downstream_modules(module, as_package=True)` as the grimp\
      \ primitive that matches the needed reverse-closure, so no graph-algorithm code\
      \ gets hand-rolled.\n- Option E is included for completeness and explicitly\
      \ noted as rejected by the issue \u2014 good epistemic hygiene.\n\n**4. Constraints\
      \ and Dependencies \u2014 strong.** \xA7\"Constraints\" covers the correctness\
      \ invariant, Python 3.13, mixed install modes, implicit conftest chain, dynamic\
      \ imports, coverage gate, sandbox role-policy, shallow CI checkouts, absence\
      \ of merge queue, and parallel-worktree concerns. Each one is tied to a specific\
      \ code location or mechanism rather than being generic.\n\n**5. Open Questions\
      \ \u2014 strong.** The 12 decisions + 16 `feedback-3` questions I pulled from\
      \ the contract match the prose list item-for-item. Spot-checked: `decision-3`\
      \ (selection mechanism), `decision-9` (LKG storage), `decision-2` (CI coverage),\
      \ `decision-11` (checkout depth), `decision-8` (gateway/role policy) all exist\
      \ in the contract with sensible, genuinely-different options. None are \"yes/no\"\
      \ boilerplate. Q1-Q16 are free-form and cover the last-mile ambiguities (performance\
      \ target, shared-fixture fallback list, canary mode, `--why` introspection,\
      \ worktree keying).\n\n**6. Recommendation Quality \u2014 strong.** Option B\
      \ (grimp + sidecar LKG + conservative fallbacks) is justified with eight numbered\
      \ reasons that each tie back to findings. The recommendation **flips the issue's\
      \ default** on LKG storage (tracked \u2192 sidecar) and explicitly calls that\
      \ out as needing human sign-off via `decision-9` \u2014 this is exactly the\
      \ behavior refine should exhibit when the issue text itself invites pushback.\n\
      \n**7. HITL Decision Registration \u2014 pass.** The contract contains 12 hitl\
      \ decisions (`decision-1` through `decision-12`) and one `feedback-3` bundle\
      \ with Q1-Q16, all created by the refiner via the contract gateway. Every open-question\
      \ in the analysis is cross-referenced to a contract ID. No orphan prose questions\
      \ were found.\n\n### Non-blocking\n- **decision-4 / decision-9 ordering dependency**\
      \ \u2014 `decision-4` asks for the `.egg/last-known-good` file format; `decision-9`\
      \ asks whether the LKG is a tracked file at all. If the human picks sidecar\
      \ (opt-1) or \"abandon LKG\" (opt-4) on `decision-9`, `decision-4` becomes moot\
      \ or transforms into a question about the sidecar format. The plan phase should\
      \ treat `decision-9` as the parent decision and rewrite `decision-4` conditionally\
      \ \u2014 the analysis could have said so inline.\n- **Minor line-number drift\
      \ in a few citations** \u2014 `tests/conftest.py:12-16` is actually lines 11-16;\
      \ `orchestrator/tests/conftest.py:22-29` is actually 23-29; `shared/tests/conftest.py:13-16`\
      \ is actually 14-16. Substantively correct, off by one or two. Not worth re-proposing\
      \ for.\n- **Source file count** \u2014 \"283 source `.py` files\" is off-by-one\
      \ versus a fresh `find` (I counted 284 excluding `tests/` prune roots). Noise-level.\n\
      - **Option B con self-contradiction** \u2014 \xA7\"Option B\" Cons says grimp\
      \ is \"small, pure-Python surface\" but \xA7\"Option B\" Pros correctly calls\
      \ it \"Rust-backed\". Fix: drop \"pure-Python\" from the Cons or say \"Rust\
      \ core + Python API\". Non-blocking; the plan phase will sort out the dependency\
      \ add.\n- **\"Pre-commit is `ruff` only\"** \u2014 the pre-commit config also\
      \ runs `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-added-large-files`.\
      \ Minor overstatement; the substantive point (no test hook exists today) is\
      \ accurate.\n- **Test-ordering flakes are not listed as a risk** \u2014 narrowing\
      \ a subset of a suite that has latent ordering dependencies can turn \"always-flakes-in-CI\"\
      \ into \"flakes-when-this-file-is-edited-only\", which is harder to diagnose.\
      \ Worth adding to the constraints list or to the canary-mode question (`Q4`)\
      \ rationale. Non-blocking for refine; raise it in plan.\n- **grimp + Python\
      \ 3.13 compatibility is asserted but not verified in the analysis** \u2014 the\
      \ plan phase should add a `make` target or `pyproject.toml` dev-extra trial,\
      \ and confirm `grimp` resolves under `uv`. Not a refine-phase ask.\n- **`.egg-readonly`\
      \ interaction with LKG reads** (captured by `Q13`) \u2014 the analysis notes\
      \ this but doesn't explain whether the sidecar path under `.egg-state/` would\
      \ respect existing readonly markers; the plan phase should audit `sandbox/`\
      \ / `egg_restrictions` before committing to the sidecar path.\n\nOverall: this\
      \ analysis does the job the refine phase exists for \u2014 it pushes back on\
      \ the issue's proposal where the codebase reality demands it, surfaces every\
      \ load-bearing sub-decision to the human, and gives the plan phase a coherent\
      \ recommendation plus a list of explicit human gates. ACK.\n"
  version: 1
````

### [2026-04-24T19:59:16Z] orchestrator → refiner (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 1). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: c2ec0ddd-3dc5-40
phase: refine
metadata:
  fully_acked: true
  version: 1
````

### [2026-04-24T19:59:20Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: a2b76627-8185-4c
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T19:59:26Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: 61741038-fe5e-4a
phase: refine
metadata:
  consensus_reached: true
````
