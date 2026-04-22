# Analysis: Replace interactive mode with a generic custom-phase MCP primitive

> Issue: #1762 | Phase: refine

## Problem Statement

The `egg` CLI's default path (`bin/egg` → `sandbox/egg_lib/cli.py::main` →
`sandbox/egg_lib/runtime.py::run_claude`) launches an interactive Claude Code
session inside a sandboxed container, using Docker Compose to bring up the
gateway + orchestrator stack. This design pre-dates the MCP server and the
Kubernetes-as-runtime migration (#1553, see `docs/architecture/kubernetes-migration.md`).

Today all *agents* are already headless (spawned as k8s Jobs by
`orchestrator/kubernetes_spawner.py::spawn_agent_job`, or Docker containers
via the legacy spawner). The interactive `egg` CLI is no longer how a human
collaborates with agents — host sessions (Claude Code / any MCP client)
can drive the orchestrator directly via the MCP server (`submit_task`,
`babysit_pr`, `get_status`, …). The interactive path is dead weight: it
duplicates compose-based deployment, drags the sandbox image into the
startup critical path, and offers no capability the MCP surface can't
already provide.

The feature request has two halves:

1. **Delete the interactive mode** — the `run_claude()` runtime, its CLI
   flags (`--setup`, `--reset`, `--public/--private` session modes,
   `--compose --down/--build`), the `compose.py` module, the interactive
   branch in `sandbox/entrypoint.py::run_interactive()`, the
   compose paths in `bin/egg-deploy`, and the `bin/egg` top-level binary.
2. **Add a generic custom-phase primitive** — `PipelineMode.CUSTOM` plus a
   new MCP tool `run_custom_phase(phase, roles, repo, description, …)`
   that runs a single pipeline phase against a repo with an explicitly
   chosen subset of that phase's roles. BRC applies unchanged; degenerate
   rosters (one producer, no reviewers) short-circuit on first propose.

The net effect: users stop `egg`-ing into a sandbox and instead drive
one-off agent work from their own host MCP client, choosing exactly the
role(s) they need — a single `refiner` to do research, a single `coder`
to open a small PR, or any subset of a phase's full roster.

## Current Behavior

### Interactive mode (to be removed)

- `bin/egg` (43 lines, `/home/egg/.egg-worktrees/issue-1762-membump-refiner/egg/bin/egg`) — thin wrapper invoking
  `egg_lib.cli.main()`.
- `sandbox/egg_lib/cli.py::main` (lines 27–220) — argparse, flag handling,
  `--setup`/`--reset`/`--compose`, then falls through to
  `run_claude(repo_mode=...)` at line 215.
- `sandbox/egg_lib/cli.py::gha_exec` (lines 223–348) — a separate entry
  point used by the GitHub Action (`action/entrypoint.sh`). It lives in
  the same module but does NOT invoke `run_claude`; it builds a
  one-shot `claude --print` command and calls
  `exec_in_new_container()`.
- `sandbox/egg_lib/runtime.py::run_claude` (lines 634–920+) — orchestrates
  image build, `ensure_compose_services()` (line 686), container IP
  allocation, session creation, mount config, finally `docker run` with
  TTY for interactive use.
- `sandbox/egg_lib/compose.py` (932 lines) — `get_compose_file`,
  `get_env_file`, `_generate_env_file`, `ensure_compose_services`,
  `run_compose_mode`. Callers: `cli.py:132` (compose --down/--build) and
  `runtime.py:47` (both `run_claude` and `exec_in_new_container`).
- `sandbox/entrypoint.py::run_interactive` (line 1909) — in-container
  launcher dispatched from `main()` at line 2143 when `sys.argv == 1`.
- `bin/egg-deploy` (383 lines) — compose-based deploy script
  (`bin/egg-deploy up/down/status/logs/build/init`). Uses
  `docker-compose.yml` at line 26. (NOTE: grep confirms the
  `docker-compose.yml` file no longer exists in the tree — the k8s
  migration has already removed it. `egg-deploy` is effectively dead for
  non-init subcommands today.)

### Pipeline modes today

From `orchestrator/models.py:29`:

```python
class PipelineMode(StrEnum):
    ISSUE = "issue"
    BABYSIT = "babysit"
```

- `ISSUE` — full refine → plan → implement → pr pipeline driven from a
  GitHub issue.
- `BABYSIT` — one-off implement-phase BRC cycle against an existing PR
  (added in #1748).

`submit_task` is the MCP entry for ISSUE, `babysit_pr` for BABYSIT
(`orchestrator/mcp_tools.py`).

Today's "short pipeline" — `submit_task --start-phase=implement` (with
`PipelineConfig.start_phase="implement"` validated at
`orchestrator/models.py:469`) — already provides implement-only execution
against a branch with pre-populated `analysis` / `plan` drafts. This
covers a narrow slice of what `run_custom_phase` is meant to generalize.

### Role roster

Phase rosters are defined in `shared/egg_contracts/agent_roles.py:1020`:

```python
_PHASE_ROLES = {
    "implement": [CODER, TESTER, DOCUMENTER],
    "plan":      [ARCHITECT, TASK_PLANNER, RISK_ANALYST],
    "refine":    [REFINER],
}

_PHASE_REVIEWERS = {
    "implement": [REVIEWER_CODE, REVIEWER_CONTRACT],
    "plan":      [REVIEWER_PLAN],
    "refine":    [REVIEWER_REFINE, REVIEWER_AGENT_DESIGN],
}
```

`get_roles_for_phase(phase, repo, has_contract)` at line 1050 already
filters `reviewer_contract` when `has_contract=False` (the mechanism
BABYSIT uses — see
`orchestrator/routes/pipelines.py:957`:`has_contract = mode != BABYSIT`).
Non-egg repos strip `reviewer_agent_design` at line 1084.

### Roster plumbing into executor

`ConcurrentPhaseExecutor.__init__(roles=...)` in
`orchestrator/concurrent_executor.py:102` already accepts an explicit
role override. `get_agent_roles()` (line 126) returns the override if
provided, else falls back to `get_roles_for_phase(...)`. Today the
override is *not* used — `pipelines.py:7243` always calls
`_get_roles_for_phase(phase, has_contract=has_contract, repo=repo)` and
passes the result as `roles=roles` at line 7322. The review graph is
filtered to the active role set at lines 7263–7270.

So a roster subset can be threaded through to the executor with mostly
local changes in `routes/pipelines.py::_execute_concurrent_phase` and
one new `active_roles` field somewhere on the pipeline record.

### BRC short-circuit for degenerate rosters

`ApprovalMatrix.is_fully_acked(producer)` at
`orchestrator/approval_matrix.py:162` iterates
`_graph.critical_reviewers_for(producer)`. When that list is empty (no
critical reviewers for this producer), the loop body never executes and
the method returns `True` — so BRC reaches CONSENSUS_REACHED on the
producer's first proposal. The issue's claim that degenerate rosters
already short-circuit is correct in code.

## Constraints

- **Gateway/orchestrator runtime is k8s**, not compose. Compose
  removal is already partially done (the `docker-compose.yml` file is
  gone); the code references must be removed too. `bin/egg-deploy up`
  (line 284) still invokes `docker compose -f "$COMPOSE_FILE" up` — this
  will fail today.
- **BRC consensus applies unchanged.** The new primitive must NOT
  bypass the message-bus protocol; it selects a subset of the existing
  phase cohort and lets BRC resolve it.
- **Role taxonomy is phase-scoped by design** (see
  `shared/egg_contracts/agent_roles.py:1020-1038`). Cross-phase role
  mixing is explicitly rejected in the spec.
- **Persistence compatibility.** Adding `PipelineMode.CUSTOM` to the enum
  is additive — existing persisted pipelines are `issue`/`babysit` and
  still load. A new `Pipeline.active_roles: list[str] | None` field
  (optional, defaults to `None`) is equally additive.
- **Contract-driven reviewers.** `reviewer_contract` (implement phase)
  and `reviewer_plan` / `reviewer_refine` require upstream artifacts.
  The current `has_contract` flag controls `reviewer_contract`; the
  spec adds an implicit inclusion rule for `reviewer_contract` when
  any analysis/plan artifact is present.
- **GHA entry point.** `egg_lib.cli.gha_exec` is invoked by
  `action/entrypoint.sh` — it is NOT the interactive path. It must be
  preserved (or relocated) when `egg_lib.cli.main` is removed.
- **Existing test coverage.** `tests/sandbox/test_cli_main.py` directly
  tests `egg_lib.cli.main()` (flag handling, setup/reset branches, etc.).
  Tests must be deleted or rewritten when the interactive path goes.
- **BABYSIT mode.** The spec explicitly leaves BABYSIT in place; a
  follow-up may consolidate CUSTOM + PR target → BABYSIT. For this
  issue, BABYSIT stays as-is.
- **Worktree policy.** Per-phase/per-role worktrees in
  `~/.egg-worktrees/` (managed by the gateway) are shared with all
  spawn paths. A custom-phase pipeline spawning a single `refiner`
  still creates a per-role worktree and reviewer worktrees if
  reviewers are in the roster.

## Options Considered

### Option A: Full cutover — remove interactive mode and ship run_custom_phase in one PR

**Approach**: In a single PR, (1) add `PipelineMode.CUSTOM`,
`run_custom_phase` MCP tool, `Pipeline.active_roles` field, and
roster-subset plumbing; (2) delete `bin/egg`, the interactive branch
of `sandbox/egg_lib/cli.py::main`, `sandbox/egg_lib/compose.py`,
the `--compose/--down/--build/--setup/--reset/--public/--private`
flags, `run_claude()`, `run_interactive()`, compose paths in
`bin/egg-deploy`; (3) rewrite user-facing docs to document the new
host-MCP-client workflow.

**Pros**:

- Clean state: no period where a half-dead `bin/egg` exists.
- One coherent doc rewrite rather than two.
- Compose code is already partly broken (no `docker-compose.yml`
  present), so any further latency shipping the removal is just dead
  code accumulating.
- The "what to remove" and "what to add" halves of the issue are
  tightly coupled — shipping them together makes review and the
  resulting PR easier to reason about as a single narrative.

**Cons**:

- Large PR touching ~15 files across orchestrator, sandbox, bin/,
  docs/, and tests. Requires careful review.
- If `run_custom_phase` ships with a latent bug, users have lost
  the interactive fallback and can't work around it with `egg`.

### Option B: Two-phase rollout — ship run_custom_phase first, then remove interactive

**Approach**: PR 1 adds the new primitive without removing anything.
Users can adopt `run_custom_phase` and validate it. PR 2 removes
`bin/egg`, compose, flags, docs — once the new primitive has been
exercised.

**Pros**:

- Lower-risk: the interactive fallback remains during the validation
  window.
- Each PR is smaller and has a clear, narrow scope.
- If `run_custom_phase` needs post-merge fixes, the old path still
  works.

**Cons**:

- Two doc rewrites (docs now describe both paths concurrently, then
  get cleaned up).
- Extended period where the interactive path is documented but
  already-broken (no `docker-compose.yml` exists).
- The issue explicitly asks for BOTH halves — splitting defers
  the deletion users are asking for.

### Option C: Minimal removal + primitive

**Approach**: Add `run_custom_phase` and remove `bin/egg` +
`run_claude()` + compose.py per the issue. Leave `egg_lib/cli.py::main`
in place (just stripped of interactive flags) so `gha_exec` stays
co-located, and leave `egg-deploy`'s compose subcommands as warnings
telling users to use `kubectl` / k8s deployment docs.

**Pros**:

- Smaller diff than Option A, still in a single PR.
- Keeps `gha_exec` in-place (no relocation churn).

**Cons**:

- `egg_lib/cli.py` becomes a stranger file — only `gha_exec` left, a
  name that no longer makes sense next to its module.
- `egg-deploy` stays half-alive with compose semantics, confusing
  operators who read it as current.

## Recommended Approach

**Option A (full cutover in one PR)**, with a few nuances:

1. **Add first, then remove.** Inside the PR, structure commits so
   `run_custom_phase` lands and is exercisable before the removals,
   so reviewers can mentally swap the new primitive in as each removed
   flag/binary is cut.
2. **Relocate gha_exec to its own module**
   (`sandbox/egg_lib/gha_exec.py`) when `cli.py` goes — it's the cleanest
   outcome and avoids a stranded function.
3. **Minimal `Pipeline.active_roles` field, stored at creation time**,
   read by `ConcurrentPhaseExecutor` (already supports `roles` override
   at `concurrent_executor.py:114`) and by `_execute_concurrent_phase`
   when filtering the review graph.
4. **Keep BABYSIT untouched.** The spec defers consolidation.
5. **Reject degenerate reviewer-only rosters** at the route — reviewers
   with no producer in the same phase have no proposal to ACK and
   would idle indefinitely. BRC's short-circuit only helps when the
   producer has no critical reviewers, not the reverse.
6. **Fall back to a synthetic branch** (`egg/custom-<pipeline_id>`)
   when the caller passes no branch and the roster contains producers
   that would write drafts/code; still allow "no branch, no git
   writes" only when the roster is all reviewers (HANDLED by
   validation, see #5 above — but preserved for the future if
   reviewer-only rosters are later whitelisted).
7. **Stage docs update**. `README.md`, `docs/guides/local-quickstart.md`,
   `docs/guides/deployment.md`, `docs/architecture/declarative-setup.md`
   all need updates. The `docs/architecture/kubernetes-migration.md`
   table that shows compose → k8s mapping stays as historical record.

Justification: compose is already half-removed (no
`docker-compose.yml` present); `egg-deploy`'s compose subcommands are
already broken in practice; `bin/egg` exists solely to run the
interactive path; users driving MCP don't invoke `bin/egg` at all.
The safety benefit of Option B (keeping the fallback during
validation) is illusory because the fallback is already broken.
Shipping everything together produces the cleanest cutover and the
narrative of the PR ("we're replacing interactive mode with a custom
phase primitive") matches the narrative of the issue.

## Open Questions

**IMPORTANT: Every open question MUST be registered as a contract decision or feedback item using `egg-contract`.** Do not just write questions as prose — they will not be seen by the human unless registered.

Surface **all** uncertainties, ambiguities, and assumptions that need human input. Do not limit yourself to a small number — every genuine ambiguity, missing requirement, unstated assumption, or design choice that could go multiple ways should be raised here. It is far better to ask too many questions than to proceed with incorrect assumptions.

The following decisions and open questions have been registered via
`egg-contract add-decision` / `egg-contract add-feedback`:

<!-- egg-hitl-decision id=decision-1 -->

**Should the new primitive be named run_custom_phase, or something more descriptive (e.g. run_phase, run_agent_task, start_adhoc_phase)? Naming is locked in once tooling/CLI docs ship.**

- [ ] run_custom_phase (as proposed in issue)
- [ ] run_phase
- [ ] start_adhoc_phase
- [ ] run_agent_task
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-2 -->

**How should the PipelineMode.CUSTOM interact with existing BABYSIT mode when pr_number is provided? Pick one.**

- [ ] Keep modes separate — CUSTOM rejects pr_number in v1; BABYSIT remains for PR targets
- [ ] CUSTOM accepts pr_number and subsumes BABYSIT's per-role staging branches + head-move guards (deprecate BABYSIT later)
- [ ] CUSTOM accepts pr_number but reuses BABYSIT's semantics internally (no consolidation yet — code de-dup is a follow-up)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**The issue says 'remove bin/egg' but bin/egg is the top-level entry point for the sandboxed interactive session. Should we also remove sandbox/egg_lib/cli.py::main and the GHA entry sandbox/egg_lib/cli.py::gha_exec? (gha_exec is separate from interactive mode but currently lives in the same module.)**

- [ ] Remove bin/egg only; keep egg_lib.cli.main but strip interactive branch; keep gha_exec
- [ ] Remove bin/egg AND egg_lib.cli.main; relocate gha_exec to its own module for GHA
- [ ] Remove bin/egg AND all of egg_lib/cli.py AND gha_exec (GHA action runs a different entrypoint)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**Should the orchestrator also remove ensure_compose_services() calls from exec_in_new_container() (which bin/egg-deploy, egg --exec, and GHA use) in this PR, or is compose removal strictly limited to sandbox/egg_lib/compose.py (the file) plus the flags listed?**

- [ ] Remove the entire compose.py module and all call sites — one cutover. ensure_compose_services() becomes a no-op / deleted. exec_in_new_container() relies on k8s being up via egg-deploy.
- [ ] Delete compose.py and all call sites from the egg CLI path only; leave integration_tests/conftest.py as-is (tests migrate separately)
- [ ] Delete compose.py from the host CLI path AND integration_tests fixtures; force k8s-only testing
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**Where should the roster override ('roles' arg to run_custom_phase) be stored on the Pipeline? The issue says 'resolved roster is stored on the pipeline record (not recomputed from phase alone)'.**

- [ ] New field Pipeline.active_roles: list[str] | None — stored at creation, read by ConcurrentPhaseExecutor and pipelines route to filter roles + review graph
- [ ] Store on PhaseExecution.active_roles (phase-scoped) — mirrors phase.artifacts placement, less global state on Pipeline
- [ ] Store on PipelineConfig.active_roles — consistent with start_phase and other config fields
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-6 -->

**How should run_custom_phase handle degenerate rosters that would deadlock BRC — e.g. selecting only a reviewer with no producer in the same phase? Reviewers ACK/NACK producers; without producers they have nothing to review.**

- [ ] Reject at validation: require at least one producer role; empty/reviewer-only rosters return 400
- [ ] Allow reviewer-only rosters and treat them as immediate no-op (reviewers spin up, see no producers, exit with CONSENSUS_REACHED short-circuit)
- [ ] Allow any subset and document the deadlock risk — caller's problem
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-7 -->

**The issue says refine/plan custom phases without a branch should produce 'no git writes'. But producers currently push drafts to .egg-state/drafts/ on a branch. How do callers retrieve analysis/plan output when no branch is passed?**

- [ ] Fall back to auto-generated branch 'egg/custom-<pipeline_id>' — callers always get a branch in the pipeline record and can git show drafts
- [ ] Require a branch when phase=refine|plan (mirrors existing issue/babysit behaviour); 'no branch' only allowed when no producer role is selected
- [ ] Spawn agents in ephemeral mode — drafts are embedded in pipeline.artifacts via orchestrator post-phase collection, no git push at all
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-8 -->

**Roles that aren't currently in _PHASE_ROLES — e.g. overseer (cross-phase) — how should run_custom_phase treat them?**

- [ ] Reject: run_custom_phase is strictly phase-scoped per the issue. Overseer and utility roles (autofixer, conflict_resolver) are not selectable.
- [ ] Allow overseer as an opt-in addition (consistent with get_roles_for_phase(include_overseer=True)) but no other cross-phase roles
- [ ] Allow any role in AgentRole (spec says 'All agent roles are legal within their phase' — interpret overseer as refine/plan/implement since it's cross-phase)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-9 -->

**The issue calls for removing 'the entire bin/egg binary' and 'the interactive default branch in sandbox/egg_lib/cli.py:215'. When should removal land relative to the run_custom_phase primitive shipping?**

- [ ] Ship both together in one PR (clean cutover — users can no longer invoke 'egg' but immediately have run_custom_phase as replacement)
- [ ] Ship run_custom_phase first, then remove interactive mode in a follow-up PR (lower-risk: custom-phase proven before the fallback disappears)
- [ ] Ship the removal first; rely on run_custom_phase landing before anyone needs the removed path (not recommended — breaks users)
- [ ] Other (explain in reply)

<!-- egg-feedback id=feedback-1 -->

Additional open-ended questions have been registered as
`feedback-1`:

- **Q1**: HITL gate integration (in-scope? out-of-scope?) for
  `run_custom_phase`.
- **Q2**: Contract-file keying when an `issue_number` is provided —
  do custom phases share `.egg-state/contracts/issue-<N>.json` with
  ISSUE-mode pipelines, or use a synthetic pipeline-keyed contract?
- **Q3**: Semantics of pre-populated `analysis`/`plan` args — only
  valid for reviewer-only rosters? Or always accepted and producers
  may overwrite?
- **Q4**: Repo allowlist — `repositories.yaml`-only, or arbitrary
  public GitHub repos via gateway allowlist?
- **Q5**: `pr_number` validation in v1 — hard-reject or accept with
  deprecation warning?
- **Q6**: CLI surface alongside MCP — does `bin/egg-sdlc` grow a
  `custom-phase` subcommand, or MCP-only?
- **Q7**: Persisted-state migration for existing pipelines — do
  they need `mode` backfilled?
- **Q8**: Explicit test coverage for the degenerate-roster short-circuit.
- **Q9**: Full list of user-facing docs that need rewriting vs.
  deprecating-with-redirect (README, local-quickstart, deployment,
  declarative-setup).

## Complexity Assessment

**high** — architectural change touching:

- **Orchestrator**: new `PipelineMode.CUSTOM`, new MCP tool + handler,
  new route field (`active_roles`), route-level validation,
  executor filtering, state-store persistence.
- **Sandbox**: deletion of `run_claude()`, `run_interactive()`, large
  swaths of `egg_lib/cli.py`, `egg_lib/compose.py`, the `bin/egg`
  binary, and the `--setup/--reset/--compose/--down/--build/
  --public/--private` flag family.
- **Deploy tooling**: `bin/egg-deploy` compose code removal, decisions
  about `ensure_compose_services` in `exec_in_new_container`.
- **Docs**: README, local-quickstart, deployment, declarative-setup,
  kubernetes-migration historical updates.
- **Tests**: `tests/sandbox/test_cli_main.py` deletion/rewrite,
  new coverage for `run_custom_phase` validation + degenerate-roster
  behavior + active_roles persistence.
- **GHA action**: confirm/relocate `gha_exec` doesn't break
  `action/entrypoint.sh`.

The cross-cutting nature (orchestrator + sandbox + bin + docs +
tests + GHA) and the coupling between "add primitive" and "remove
legacy" make this a high-complexity plan-phase task that would
benefit from explicit role/task decomposition during planning.

---

*Authored-by: egg*
