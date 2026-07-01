# Refine analysis — issue #3393 (multi-repo pipelines: coordinated PRs across repositories in one pipeline)

Refiner grounding pass, verified against the live worktree on 2026-07-01
(HEAD `20b476173`, branch `egg/issue-3393-refiner/work`). Issue body fetched
live via `gh issue view 3393` (OPEN). Every "current state" claim in the issue
was re-verified against the tree; verdicts and corrections below.

## Scope verdict

Scope is operator-specified and well-bounded. The three hard requirements are
BINDING and none is negotiable at refine or plan time:

1. **Arbitrary N repos per pipeline** — list-shaped end to end (submission,
   pipeline state, agent env, PR coordination). No two-repo special case, no
   "primary + one secondary" in the data model. (A *primary* repo for naming
   and slice-default is allowed; nothing downstream may assume exactly one or
   exactly two.)
2. **Slice ↔ repo 1:1** — `Slice` gains a `repo` field; a slice's worktree,
   branch, review diff, test scope, and PR live in that one repo. Cross-repo
   work = multiple slices + existing slice dependencies, never one slice
   touching two repos.
3. **Uniform visibility per run** — submission validates that all repos in a
   run are uniformly private or uniformly public and rejects mixed sets.

v1 additionally requires **uniform auth mode** across all repos (mixed
bot/user auth is explicitly *later*), and a **workable, even if simple,
merge-sequencing hold** for dependent slices whose upstream PR lives in
another repo.

## Grounding: issue "current state" table vs. live tree

The issue's central thesis — *the single-repo assumption is concentrated, not
pervasive* — **holds**. Verdicts per claim:

| # | Issue claim | Verdict | Evidence (live tree) |
|---|---|---|---|
| 1 | Gateway worktree creation takes a repo **list**, returns dict repo→path | **TRUE** (two-layer naming, see corrections) | `orchestrator/gateway_client/_worktree.py:13-22` — `create_worktrees(self, container_id, repos: list[str], ...)` (plural, exactly as the issue says); returns `WorktreeResult.worktrees: dict[repo_name → path]` (`_worktree.py:74-79`; gateway handler `gateway/gateway.py:~7827`) |
| 2 | Credentials resolve per repo via `get_token_for_repo(repo)` | **TRUE** | `gateway/git_client/_credentials.py:85-118` — single-repo signature; per-repo `get_auth_mode(repo)` from `repositories.yaml`. Callers invoke once per repo — genuinely multi-repo-capable |
| 3 | `create_pr` / `create_slice_pr` take explicit `repo` param | **TRUE** | `orchestrator/gateway_client/_pr.py:31-137` and `:139-164` |
| 4 | `check_branch_ownership` is repo-agnostic | **TRUE in substance** (nuance, see corrections) | `gateway/policy.py` — ownership derives from branch prefix + PR author; no per-repo logic. But the bot identity / prefix set is a single **global** config (`GATEWAY_BOT_NAME`, `GATEWAY_BOT_BRANCH_PREFIX`) — fine under v1 uniform auth mode |
| 5 | Contract/Slice schema has **no `repo` field** — the load-bearing gap | **TRUE** | `shared/egg_contracts/models.py:322-459` — `Slice` fields: `id`, `name`, `goal`, `status`, `tasks`, `dependencies`, `serialized_chain_order`, `parent_branch_at_creation`, `integration_base_sha`, `commit`, `pr_number`, `pr_url`, `review_feedback`. No repo dimension anywhere in the contract. Dependency mechanism exists: `dependencies: list[str]` of slice IDs (`models.py:362-369`) |
| 6 | `Pipeline.repo` is a singleton; agent env collapses to `repos[0]` (`EGG_PIPELINE_REPO`) | **TRUE** — three collapse sites | `orchestrator/models.py:1131` (`Pipeline.repo: str \| None`); `orchestrator/kubernetes_spawner/_spawn.py:452` (`repo_name = repos[0].split("/")[-1]`), `:464` (`pipeline_repo = repos[0] if repos else None`), `:523` (`EGG_PIPELINE_REPO`); **plus two more collapses the issue does not mention**: `orchestrator/commit_authorship_store.py:932-933` (`next((r for r in repos if r.name == "egg"), repos[0])`) and `orchestrator/routes/pipelines.py:732` (`overseer_repo = pipeline_repos[0] if pipeline_repos else None`) |

## GROUNDING CORRECTIONS — flag for planner

1. **Two-layer naming — distinguish, don't rename.** The issue's method
   name is CORRECT: the orchestrator gateway-client method is
   `create_worktrees` (plural, `orchestrator/gateway_client/_worktree.py:13`).
   A *different*, gateway-internal per-repo helper named `create_worktree`
   (singular) exists at `gateway/worktree_manager/_create.py:115` — do not
   conflate the two layers. On the return side, the client returns
   `WorktreeResult.worktrees` (not `repo_volumes`), but `repo_volumes` is
   also live: it is the kubernetes-spawner parameter name
   (`orchestrator/kubernetes_spawner/_spawn.py:45`, `_concurrent.py:117`)
   fed from the client's `worktrees` field (`_spawn.py:283`). When writing
   tasks, name the layer: client `create_worktrees` → `worktrees` →
   spawner `repo_volumes`.
2. **The worktree dict is keyed by bare repo NAME, not `owner/repo`.**
   `_worktree.py:74-79` keys by short name (`repo.split("/")[-1]`-shaped).
   Two repos with the same name under different owners (`ownerA/foo`,
   `ownerB/foo`) would collide. v1 must either (a) re-key the agent-facing
   map by full `owner/repo`, or (b) validate-and-reject same-name repo sets
   at submission. Planner picks; (a) is the honest list-shaped fix, (b) is
   an acceptable v1 guard if (a) fans out too far.
3. **`check_branch_ownership` nuance.** It is repo-agnostic in the sense the
   issue means (no single-repo assumption in the policy logic), but the bot
   identity + branch-prefix set is one global configuration. Under v1's
   uniform-auth-mode requirement this is a non-issue; it becomes real work
   only in the deferred mixed-auth phase.
4. **THREE `repos[0]` collapse sites exist, not one.** The issue names only
   the agent-env collapse. Live enumeration:
   (a) `orchestrator/kubernetes_spawner/_spawn.py:452/:464` (+
   `EGG_PIPELINE_REPO` at `:523`) — the agent-env collapse the issue names;
   (b) `orchestrator/commit_authorship_store.py:932-933` (prefers the repo
   named `"egg"`, falls back to `repos[0]`);
   (c) `orchestrator/routes/pipelines.py:732`
   (`overseer_repo = pipeline_repos[0] if pipeline_repos else None`).
   Any "stop the collapse" acceptance criterion must cover all three, plus a
   `grep -rn 'repos\[0\]'` sweep at implement time (the sweep also surfaced
   `sandbox/egg_lib/sdlc_hitl.py:82`, which is guarded by `len(repos) == 1`
   and therefore NOT a collapse — listed so the planner doesn't re-flag it).

## Additional grounding (facts the issue asserts implicitly — verified)

- **Submission surface:** `orchestrator/mcp_tools/_submit.py:20-163` —
  `submit_task` accepts a single `repo` string (`:78-79`) + `base_branch`,
  POSTs to `/api/v1/pipelines`. Nothing accepts a list today. The list-shaped
  submission (each repo with its own `base_branch`) is new surface on both
  the MCP tool and the pipelines route.
- **Visibility:** per-repo lookup exists (`gateway/repo_visibility.py:407-425`,
  `get_repo_visibility(owner, repo)`), and private-mode posture is a **global**
  gateway setting (`gateway/config_validator.py:123-142`,
  `is_private_mode_enabled()`; `gateway/private_repo_policy.py`). There is
  **no submission-time visibility validation today** — the uniform-visibility
  check is genuinely new code, not a tweak to an existing check.
- **Auth/session model:** session registration takes a single `mode` per
  pipeline (`orchestrator/gateway_client/_session.py:16-39`); auth mode is
  per-repo config in `repositories.yaml`. v1's uniform-auth-mode requirement
  maps cleanly onto the existing single-mode session — submission should
  *validate* that all N repos resolve to the same auth mode and reject
  otherwise. Mixed auth = session-model redesign = correctly deferred.
- **Contract persistence/migration:** contracts persist as JSON
  (`.egg-state/contracts/<pipeline_id>.json`); `Contract.schemaVersion` is
  `"1.3"` with four existing migration validators
  (`shared/egg_contracts/models.py:838-862`, `:947-1121`). Adding
  `Slice.repo` follows the established pattern: bump schema version, migrate
  absent field ⇒ the pipeline's primary repo. The back-compat mechanism the
  issue worries about **already exists and has four precedents**.
- **Context PR:** `_open_context_pr_at_implement_start`
  (`orchestrator/routes/pipelines.py:~11001-11250`) opens one context PR on
  `egg/<id>/work → main` in the single repo. This is where the per-repo
  work-branch/context-PR model lands.
- **Slice-PR routing:** `create_slice_pr` is already repo-parameterized; the
  callers currently pass the pipeline's singleton repo. Routing to
  `slice.repo` is a call-site change, not new machinery.

## Design questions — refine-level recommendations (planner owns mechanics)

1. **Work-branch / context-PR model per repo → recommend lazy-per-repo:**
   every repo that has ≥1 slice gets its own work branch (same
   `egg/<id>/work` naming, per repo) and its own context PR; repos submitted
   but ending up with no slices get neither. A single-slice repo still gets
   the standard context PR (uniformity beats special-casing; the context PR
   is the audit surface).
2. **Merge-sequencing gate semantics → RESOLVED by operator (HITL cq-1,
   2026-07-01; see "HITL Resolution" section below).** Binding two-tier
   model: plain merge ordering is **automated** (dependent slice developed
   in parallel, PR held in draft, orchestrator auto-marks ready when the
   upstream PR merges); anything **beyond merge state** (release/publish
   waits, version-pinning choices, genuine development blocks) is
   **HITL-resolved**, never programmatically detected.
3. **Test-gate / reviewer-diff scoping:** the slice↔repo 1:1 rule makes this
   mechanical — the test gate runs in the slice's repo's worktree only, and
   the reviewer diff is `git diff` in that worktree against that repo's
   base. No cross-repo diff surface exists in v1.
4. **Naming/status surfaces:** pipeline id/naming keys off the **primary**
   repo (first in the submitted list unless explicitly flagged); branch
   naming is uniform across repos; status surfaces render per-repo PR lists.
   Planner spells out exact renderings.
5. **Per-repo conventions (issue entailment — do not drop):** an agent
   working a slice operates under the conventions of **that slice's repo** —
   its cwd is the slice's repo worktree, and that repo's `CLAUDE.md`,
   linters, and check commands govern the work and its gates. The egg repo's
   `make lint`/`make test` conventions apply only to slices whose repo is
   egg; other repos bring their own. Planner makes this explicit in slice
   task wording and in how the test gate resolves each repo's check
   commands.

## Hard parts (carried into the contract; do not under-scope)

1. **Persisted-JSON migration** for `Slice.repo` (+ pipeline repo-list) —
   pattern exists (four precedents), but refine/plan/implement all read
   contracts, so the migration must land before any producer writes the new
   field.
2. **Atomic cross-repo landing is impossible** — GitHub merges PRs
   independently. v1 ships an *ordering hold*, not atomicity. The
   merge-sequencing gate semantics were resolved by the operator in HITL
   cq-1 (two-tier: automated merge-ordering hold; HITL for beyond-merge-state
   blocks — see "HITL Resolution" section).
3. **Mixed auth modes are OUT of v1** — submission validates uniformity and
   rejects mixed sets (same shape as the visibility check).

## Acceptance criteria (restated for the contract)

1. `submit_task` (and `/api/v1/pipelines`) accepts a **list** of repos, each
   with its own `base_branch`; single-repo submissions keep working
   unchanged (back-compat).
2. Submission **rejects** (a) mixed-visibility repo sets, (b) mixed-auth-mode
   repo sets, with actionable errors. (And guards the same-name-repo
   collision per correction #2 unless the map is re-keyed to `owner/repo`.)
3. `Slice.repo` exists (exactly one repo per slice; absent ⇒ primary repo via
   schema migration); contract schemaVersion bumped with a migration
   validator in the established pattern.
4. Agent environment exposes the **full** repo→worktree map; all three
   `repos[0]` collapse sites (`kubernetes_spawner/_spawn.py`,
   `commit_authorship_store.py`, `routes/pipelines.py:732`) are gone, plus a
   sweep for any others.
5. Slice PRs are created in `slice.repo`; each participating repo (≥1 slice)
   gets its own work branch + context PR; PR descriptions cross-reference
   sibling PRs in the pipeline.
6. Cross-repo ordering expressed via existing `Slice.dependencies`, with the
   operator-resolved (cq-1) merge-sequencing semantics: cross-repo
   dependencies gate **merge-readiness, not development** — the dependent
   slice is developed in parallel, its PR opened as **draft**, and the
   orchestrator **auto-marks it ready** when the upstream slice's PR merges
   (mechanical, observable signal; no HITL step for plain merge ordering).
   Blocks beyond merge state (waiting on a release/publish of the upstream
   repo, choosing which released version to pin, or a genuine
   cannot-continue development block) are held and released via **HITL
   decisions**, not programmatic detection.
7. Test gate and reviewer diffs scope to the slice's repo only; a slice's
   agent runs with cwd in the slice's repo worktree and operates under that
   repo's own conventions (`CLAUDE.md`, linters, check commands) — per-repo
   convention scoping, not egg's conventions applied to foreign repos.
8. `make lint` + `make test-all` green; single-repo pipelines are the
   regression baseline (no behavior change for N=1).

## Explicitly OUT of scope (v1)

- Mixed auth modes across repos (later phase; session-model redesign).
- Richer first-class merge-sequencing machinery beyond the v1 hold.
- Cross-repo atomic merge (impossible on GitHub; do not attempt to fake it).
- Any two-repo special case or primary+secondary data-model shape.

## HITL Resolution (cq-1) — resolved by operator, 2026-07-01

The operator answered decision cq-1 ("what holds a dependent slice's PR when
its upstream slice's PR lives in another repo?") with a custom ("Other")
resolution — **binding for the planner**:

> **Merge-gated ordering is automated; genuine blocks are HITL-resolved.**
> Cross-repo dependencies gate merge-readiness, not development: the
> dependent slice is developed in parallel, its PR held in draft, and the
> orchestrator auto-marks it ready when the upstream slice's PR merges
> (mechanical, observable signal — no human latency, no HITL release step
> for plain merge ordering). Any block beyond merge state — e.g. waiting on
> a release/publish of repo A, or deciding which released version repo B
> should pin — is resolved by a HITL decision, not programmatic detection:
> signals like "which release" or "this release is out" are
> context-dependent and not worth fragile automation, so the human confirms
> the external condition and releases the hold. Development blocks only
> when work genuinely cannot continue without the upstream artifact, and
> those blocks are likewise HITL-resolved.

Planner-facing consequences:

1. **Dependencies do NOT serialize development.** Cross-repo-dependent
   slices are worked in parallel; the dependency bites only at PR
   merge-readiness.
2. **v1 needs upstream-PR merge polling + draft→ready transition** in the
   orchestrator (mechanical GitHub signal), and a **HITL hold type** for
   beyond-merge-state conditions (release published, version chosen).
   Automating release-detection is explicitly rejected — do not plan it.
3. **Two distinct hold kinds, two release paths:** merge-state hold →
   auto-release; external-condition hold → human-resolved HITL decision.

The resolution is self-contained; **no new HITL decisions are induced** —
the split between the two hold kinds per dependency edge is planner-phase
mechanics under the rule above (default: merge-state hold; escalate to the
HITL kind only where the plan identifies a release/publish or version-pin
condition).

## Decision log

- 2026-07-01: grounded issue #3393 (live body via `gh issue view 3393`)
  against the worktree at `20b476173`. Confirmed the concentrated-gap thesis;
  verified all six current-state claims (verdict table above); found four
  corrections (method/field naming drift; worktree map keyed by bare repo
  name — collision risk; `check_branch_ownership` global-bot nuance; a
  second `repos[0]` collapse in `commit_authorship_store.py`). Confirmed no
  submission-time visibility validation exists today; confirmed contract
  migration machinery has four precedents. Registered HITL decision-1
  (v1 merge-sequencing gate semantics). Wrote v1 of this analysis.
- 2026-07-01 (v2, addressing three NACKs on v1 — all conceded after live
  re-verification): (1) correction #1 was inverted — the client method IS
  `create_worktrees` (plural, `gateway_client/_worktree.py:13`, the issue
  was right); the singular `create_worktree` I conflated it with is the
  gateway-internal helper (`gateway/worktree_manager/_create.py:115`), and
  `repo_volumes` is the live spawner parameter, not drift — correction #1
  rewritten as a two-layer naming map. (2) A THIRD `repos[0]` collapse
  exists at `orchestrator/routes/pipelines.py:732` — correction #4, verdict
  row 6, and AC-4 now enumerate all three (the sweep also cleared
  `sandbox/egg_lib/sdlc_hitl.py:82` as guarded, not a collapse). (3) Added
  the issue's per-repo-conventions entailment (agent cwd + CLAUDE.md /
  linters / check commands of the slice's repo) as design recommendation #5
  and folded it into AC-7.
- 2026-07-01 (v3, operator gate feedback): operator resolved HITL cq-1 with
  a custom two-tier model — automated draft→ready on upstream PR merge for
  plain merge ordering; HITL-resolved holds for beyond-merge-state
  conditions (release/publish, version pinning, genuine development
  blocks). Folded into design recommendation #2, hard part #2, AC-6, and a
  new "HITL Resolution (cq-1)" section with planner-facing consequences.
  No new HITL decisions induced (resolution is self-contained).
