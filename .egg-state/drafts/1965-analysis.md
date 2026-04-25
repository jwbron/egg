# Analysis: Improve the generalized reviewer

> Issue: #1965 | Phase: refine

## Problem Statement

The pipeline's BRC `reviewer_code` is **confirming consensus while missing
genuine blocking issues** that the GitHub Actions `egg-reviewer` then catches
after the PR is opened. Two examples from PR #1964 motivate the work:

1. A wrapper script (`sandbox/scripts/jira`) was referenced by the Dockerfile
   but **never committed** — the deliverable shipped as a broken symlink. The
   BRC reviewer did not notice the cross-file mismatch.
2. `/api/v1/jira/execute` with `path=project` **bypasses the project-allowlist
   check** — an information-disclosure bug. Again the BRC reviewer missed the
   handler-vs-allowlist-extractor mismatch across files.

The desired outcome: when the pipeline opens a PR, the GHA auto-reviewer
should find little to no issues, and **no blocking ones**. Anything blocking
must be caught and fixed inside the pipeline.

The issue body has already pre-resolved the high-level scope: ship **A
(subagent delegation inside `reviewer_code`) + B (two new specialised
reviewer roles, `reviewer_security` and `reviewer_concurrency`)**, both
small, both v1. Multiple alternatives (Option D pre-PR gate, Option E
static analyser, conditional-ACK policy, severity rubric, `babysit_pr`
fan-out, GHA reviewer changes) are explicitly **out of scope** and have
been spun off to other issues (#1997, #1998, #1999) or deferred.

The refine-phase work here is therefore narrow: confirm the surface area,
surface the residual implementation-shape decisions the issue body itself
flags, and recommend an approach.

## Current Behavior

**Reviewer prompt construction.**
`orchestrator/routes/pipelines.py` builds review prompts in two layers:

- `_get_reviewer_scope_preamble(reviewer_type, phase)` (lines 3262–3313)
  returns a role-specific preamble for `code`, `agent-design`, `contract`,
  `refine`, and `plan`. The `code` preamble already says "Be thorough.
  Find ALL issues on the first pass" — the problem is enforcement, not
  instruction.
- `_build_review_prompt(...)` (lines 4217–4400+) assembles the full prompt.
  Crucially, the diff is **not embedded** as text — the reviewer is told
  to run `git diff origin/<base>...HEAD` (or a delta-review log command on
  re-reviews) itself. The function takes **no `contract` parameter**.

**Reviewer-role → criteria-file mapping.**
`orchestrator/routes/pipelines.py:8317` does the entire mapping with one
line:

```python
reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")
```

So `reviewer_security` → `security` and `reviewer_concurrency` →
`concurrency` already fall out automatically. The dispatcher
`_get_review_criteria_for_type(reviewer_type, phase, repo_path)` then
calls `_get_<type>_review_criteria(...)` per type — adding two new
loaders is mechanical.

**Reviewer role registration.**
`shared/egg_contracts/agent_roles.py` (lines 44–82) defines `AgentRole`
as a `StrEnum`; reviewer roles are simple string members (`REVIEWER_CODE
= "reviewer_code"`, etc.).

**Review graph.**
`orchestrator/review_graph.py` uses a small dataclass:

```python
ReviewEdge(reviewer_role="reviewer_code", producer_role="coder",
           criticality=ReviewCriticality.CRITICAL)
```

`ReviewCriticality` (lines 15–19) has exactly two values: `CRITICAL` and
`ADVISORY`.  Default is `CRITICAL`. Adding four new `ADVISORY` edges
(`reviewer_security` → `coder`, `reviewer_security` → `tester`,
`reviewer_concurrency` → `coder`, `reviewer_concurrency` → `tester`) is
again mechanical.

**Criteria files.**
Live under `shared/prompts/`. `code-review-criteria.md` is the canonical
example. `REVIEWER-SYNC.md` (`shared/prompts/REVIEWER-SYNC.md`) is the
sync guide that keeps GHA and BRC reviewers from drifting; per the
issue, only `REVIEWER-SYNC.md` will be touched on the GHA side — the
GHA reviewer code itself is not.

**Subagent / `Task` tool availability.**
`shared/egg_agent/client.py:162–167` only blocks `WebFetch` /
`WebSearch` in private mode. The Claude Agent SDK's `Task` subagent
tool is **not blocked** anywhere — no allow-list edits, no MCP-server
plumbing, no orchestrator routing changes are needed for `reviewer_code`
to spawn subagents. This was decision-10.

**Attestation.**
`orchestrator/peer_consensus.py` validates attestation only when
`review.attestation` is truthy; `validate_attestation` in
`orchestrator/attestation_schemas.py:205–237` instantiates the model
with whatever data is supplied (Pydantic defaults absorb missing
fields). `REVIEWER_ATTESTATION_MODELS` currently registers only
`reviewer_code`, `reviewer_contract`, `tester` — `reviewer_refine`,
`reviewer_agent_design`, `reviewer_plan` ship without models and work
fine. Pitfall 4 calls this out: do **not** add models for the two new
reviewers unless day-1 ACKs will actually carry attestation payloads.

**Integration test fixtures.**
`integration_tests/sdlc/` contains `conftest.py`, `test_happy_path.py`,
`test_hitl_flow.py`, `test_refine_plan_review_cycles.py`,
`test_review_rejection.py`, `test_role_enforcement.py`. There is no
`fixtures/` directory yet — adding `integration_tests/sdlc/fixtures/`
and `test_reviewer_1964_regression.py` is greenfield.

## Constraints

- **In-pipeline behaviour only.** GHA reviewer code (`action/...`) is
  off-limits. Only `shared/prompts/REVIEWER-SYNC.md` is updated to
  document the SDLC-only asymmetry (decision-9 / feedback-9).
- **Backward-compatible.** Existing reviewer roles, edges, and prompts
  must keep working. The two new reviewers enter as `ADVISORY` so they
  cannot deadlock consensus on day 1 (decision-11). Promotion to
  `CRITICAL` happens after #1997 lands.
- **Cost / latency budget.** 2–5× cost on large PRs is acceptable;
  5–10 min latency on large PRs is acceptable; no hard per-pipeline
  ceiling (feedback-1 / feedback-2).
- **Threshold gating.** Below ~10 changed files / ~500 LOC, fan-out is
  off by default (decision-7 / feedback-4). Parent reviewer can
  override either direction.
- **Subagent parallelism.** Configurable per-pipeline; default
  **parallel** (feedback-11). Configuration site is unspecified — see
  open questions.
- **Acceptance is trend-based.** GHA blocking-count per pipeline-PR
  should trend to zero across N PRs. No hard numeric gate (decision-9).
- **Phase boundaries.** Refine/plan reviewers do not get subagent
  fan-out; only `reviewer_code` does (decision-3).
- **No redundant role mapping.** The single
  `replace("reviewer_", "").replace("_", "-")` line at
  `pipelines.py:8317` covers the new reviewers automatically. No dict,
  no if/elif chain — the plan must verify this and add a unit test
  rather than introduce a mapping (pitfall 1).

## Options Considered

The strategic option choice (A + B small) is locked by the issue body.
What remains are **two implementation-shape decisions** the issue
explicitly calls out as "pick one explicitly", plus a handful of
narrower questions that surface naturally from the surface area.

### Pitfall-2 options: how does `reviewer_code` learn the diff size?

The threshold gate (~10 files / ~500 LOC) needs `git diff --numstat`
output. `_build_review_prompt()` does not have the diff at build time
— it embeds a shell command for the reviewer to run.

#### Option 2A: Orchestrator-side numstat plumbing

**Approach**: Run `git diff --numstat <base>...HEAD` from the
orchestrator at prompt-build time, compute `(files_changed, loc)`,
thread the metrics into `_get_reviewer_scope_preamble()` (or a new
helper), and emit either a "Subagent fan-out: enabled" or "disabled"
block in the prompt text.

**Pros**:
- Orchestrator decides, reviewer obeys — single source of truth.
- Threshold logic and tests live in Python, not in the model's head.
- Easy to log and tune (we can grep telemetry for "enabled vs not").

**Cons**:
- Adds a `git` shell-out at prompt-build time (orchestrator-side, but
  inside an HTTP route handler — possibly tens of ms of latency).
- Adds a parameter (or two) to `_get_reviewer_scope_preamble()`,
  rippling through call sites.
- Requires a working `repo_path` for the orchestrator at the moment
  the prompt is built; we already have `repo_path` plumbed through.

#### Option 2B: Reviewer self-gates in the prompt

**Approach**: Put the threshold rule inside the prompt text. Tell the
reviewer: "First run `git diff --numstat <base>...HEAD`; if files > 10
or loc > 500, spawn N `Task` subagents partitioned by feature; otherwise
review yourself."

**Pros**:
- Zero orchestrator-side plumbing — pure prompt change.
- Scales naturally: the reviewer can adjust partitions on the fly based
  on what it sees.
- Matches existing pattern (we already tell the reviewer to run
  `git diff` itself).

**Cons**:
- Threshold compliance is non-deterministic — hard to assert in tests
  without a live LLM call.
- The decision is invisible to telemetry unless we add explicit prompt
  instructions to log it.
- Subtle prompt-engineering risk: the reviewer might decide to partition
  too aggressively or too coarsely.

### Pitfall-3 options: how does `reviewer_code` see the implement-phase task list?

The fan-out partition strategy is "by logical subsystem / feature,
inferred from the plan phase's task decomposition" (decision-4). The
reviewer needs `phases.implement.tasks[]` from the contract.

#### Option 3A: Orchestrator-side contract loader

**Approach**: Add a `contract` parameter to `_build_review_prompt()` (or
load by `pipeline_id` inside it), extract `phases.implement.tasks[]`,
serialise the task titles into the prompt under a "Suggested partitions"
heading.

**Pros**:
- Reviewer gets the partition list embedded — no extra MCP calls.
- Deterministic: tests can assert prompt content against known
  contracts.
- Centralises contract-coupling in one place.

**Cons**:
- Adds yet another parameter to an already long signature, OR adds a
  contract-load shell to a hot path.
- Couples the prompt text to contract schema — schema drift breaks
  prompts.
- If the contract has no implement-phase tasks (e.g., custom-phase
  invocation), we need a fallback path anyway.

#### Option 3B: Reviewer calls `mcp__sdlc__show_contract` itself

**Approach**: Tell the reviewer in its prompt: "Call
`mcp__sdlc__show_contract` and self-extract
`phases.implement.tasks[]`. If the list is empty, skip fan-out."

**Pros**:
- Zero contract plumbing in `_build_review_prompt()`.
- Reviewer can pull richer context (acceptance criteria, prior tasks)
  on demand.
- Aligns with the broader pattern of giving agents MCP access rather
  than pre-baking everything.

**Cons**:
- Adds a tool call (and one more failure mode) to every fan-out review.
- Reviewer may quietly skip the call and partition arbitrarily.
- Harder to assert in unit tests — needs live MCP or a stubbed
  gateway.

(Both options must include the explicit fallback: if implement-phase
tasks are empty, **skip fan-out entirely**.)

### Other latent decisions

- **Threshold composition.** "~10 files / ~500 LOC" — is this `OR`
  (whichever trips first) or `AND` (both must trip)? Issue is
  ambiguous. Open question.
- **Subagent diff scope.** Each subagent needs *something* to read.
  Does each subagent re-run `git diff` and grep its slice, or does
  the parent reviewer slice the diff and pass each subagent a
  patch fragment? Open question.
- **Lens-criteria authoring.** Feedback-10 says "dedicated criteria
  files" for security/concurrency. Should those files **inherit**
  from `code-review-criteria.md` (e.g., via an explicit "see also"
  reference) or be **standalone** lens-focused criteria with no
  cross-link? Open question.
- **Per-pipeline subagent parallelism config site.** Feedback-11 says
  "configurable per-pipeline; default parallel". Where does the knob
  live — env var, `phase_configs` field on the contract, settings.json,
  CLI flag on `egg-orch pipeline create`? Open question.
- **Regression-replay test execution mode.** Feedback-12 specifies a
  cached PR #1964 diff fixture that asserts both previously-missed
  issues are flagged. Should the test ship with **two** modes — a
  CI-unconditional prompt-asserts mode (no LLM) and an opt-in
  live-LLM mode gated by `RUN_REVIEWER_REPLAY=1` — or just one of
  the two? Open question.

## Recommended Approach

Adopt **A + B small** with the following implementation shape, subject
to human resolution of the open questions below:

1. **Pitfall 2 → Option 2B (reviewer self-gates).** Lower-risk, no
   orchestrator-side plumbing churn, matches the existing "the
   reviewer runs `git diff` itself" pattern. Deterministic-test loss
   is minor and offset by feedback-12's regression-replay fixture
   which exercises the path end-to-end. Logging the gate decision
   becomes a prompt-instructed `STATUS` message, not Python
   instrumentation.

2. **Pitfall 3 → Option 3B (reviewer calls
   `mcp__sdlc__show_contract`).** Same rationale: keep
   `_build_review_prompt()` lean; agents already use MCP for
   contract reads. Fallback is explicit: empty implement-phase
   tasks → skip fan-out. The "deterministic test" loss is less
   meaningful here because the partition strategy is heuristic
   anyway.

3. **No attestation models** for `reviewer_security` /
   `reviewer_concurrency` on day 1, per pitfall 4. Add only if a
   later iteration needs to enforce structured payloads.

4. **Lens-focused dedicated criteria files**, with an explicit
   "Inherits from `code-review-criteria.md` — only the lens-specific
   rules below override or extend it" header in each new file.
   Cheap to write, prevents accidental criteria duplication, and
   keeps `REVIEWER-SYNC.md` honest.

5. **Regression-replay test in two modes**: CI-unconditional
   prompt-text asserts (does the prompt builder include the
   subagent block when given a 12-file / 800-LOC fixture? does it
   skip when given a 3-file / 50-LOC fixture?), plus an opt-in
   live-LLM replay gated by `RUN_REVIEWER_REPLAY=1` for nightly /
   manual runs.

The plan phase will then decompose this into tasks. The seven open
questions below must be resolved (or explicitly waived) before plan
can finalise.

## Open Questions

All open questions below have been registered as decisions/feedback on
the contract via `mcp__sdlc__register_open_question` /
`mcp__sdlc__request_feedback`. Markdown copies are preserved here for
traceability.

(Decision/feedback bodies will be inserted by the registration calls
that follow; plain prose copy is below for human convenience.)

1. **Pitfall 2 — threshold metrics path.** Server-side numstat
   plumbing (Option 2A) vs reviewer self-gating in prompt
   (Option 2B)?
2. **Pitfall 3 — implement-phase task list path.** Orchestrator-side
   contract loader (Option 3A) vs reviewer-side
   `mcp__sdlc__show_contract` self-fetch (Option 3B)?
3. **Threshold composition.** "~10 files / ~500 LOC" — interpret as
   `OR` (trigger when either trips) or `AND` (require both)?
4. **Subagent diff scope.** Does each subagent re-run `git diff` and
   filter its slice, or does the parent reviewer pre-slice the diff
   and pass patch fragments to each subagent?
5. **New criteria-file inheritance model.** Should
   `security-review-criteria.md` / `concurrency-review-criteria.md`
   inherit from `code-review-criteria.md` via an explicit "see also"
   header, or be fully standalone?
6. **Per-pipeline subagent parallelism config site.** Where does the
   "default parallel, override per pipeline" knob live — env var,
   `phase_configs` contract field, `settings.json`, or CLI flag on
   `egg-orch pipeline create`?
7. **Regression-replay test execution mode.** Ship CI-unconditional
   prompt-asserts only, opt-in live-LLM replay only, or both modes
   (prompt-asserts always + live-LLM gated by `RUN_REVIEWER_REPLAY=1`)?

## Complexity Assessment

**medium**.

Touches eight or so files (two new criteria docs, two new role enum
entries, four new review-graph edges, two new criteria loaders, one
prompt-text update for the subagent block, one updated
`REVIEWER-SYNC.md`, one new fixture, one new regression test). Each
change is mechanical and follows an existing pattern. The only design
ambiguity is pitfalls 2 & 3 above, both already pre-flagged in the
issue body. No new subsystems, no schema migrations, no orchestrator
routing changes, no MCP verb additions.

The path is well-trodden — the recently merged work for
`reviewer_refine` and `reviewer_agent_design` already established the
pattern for adding ADVISORY-by-default reviewers without attestation
models. Plan-phase will likely decompose this into a single
implement-phase pass with a handful of tasks rather than parallel
phases.

---

*Authored-by: egg*
