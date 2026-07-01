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
| 1 | Gateway worktree creation takes a repo **list**, returns dict repo→path | **TRUE** (naming drift, see corrections) | `orchestrator/gateway_client/_worktree.py:13-22` — `create_worktree(self, container_id, repos: list[str], ...)`; returns `worktrees: dict[repo_name → path]` (`_worktree.py:74-79`; gateway handler `gateway/gateway.py:~7827`) |
| 2 | Credentials resolve per repo via `get_token_for_repo(repo)` | **TRUE** | `gateway/git_client/_credentials.py:85-118` — single-repo signature; per-repo `get_auth_mode(repo)` from `repositories.yaml`. Callers invoke once per repo — genuinely multi-repo-capable |
| 3 | `create_pr` / `create_slice_pr` take explicit `repo` param | **TRUE** | `orchestrator/gateway_client/_pr.py:31-137` and `:139-164` |
| 4 | `check_branch_ownership` is repo-agnostic | **TRUE in substance** (nuance, see corrections) | `gateway/policy.py` — ownership derives from branch prefix + PR author; no per-repo logic. But the bot identity / prefix set is a single **global** config (`GATEWAY_BOT_NAME`, `GATEWAY_BOT_BRANCH_PREFIX`) — fine under v1 uniform auth mode |
| 5 | Contract/Slice schema has **no `repo` field** — the load-bearing gap | **TRUE** | `shared/egg_contracts/models.py:322-459` — `Slice` fields: `id`, `name`, `goal`, `status`, `tasks`, `dependencies`, `serialized_chain_order`, `parent_branch_at_creation`, `integration_base_sha`, `commit`, `pr_number`, `pr_url`, `review_feedback`. No repo dimension anywhere in the contract. Dependency mechanism exists: `dependencies: list[str]` of slice IDs (`models.py:362-369`) |
| 6 | `Pipeline.repo` is a singleton; agent env collapses to `repos[0]` (`EGG_PIPELINE_REPO`) | **TRUE** — two collapse sites | `orchestrator/models.py:1131` (`Pipeline.repo: str \| None`); `orchestrator/kubernetes_spawner/_spawn.py:452` (`repo_name = repos[0].split("/")[-1]`), `:464` (`pipeline_repo = repos[0] if repos else None`), `:523` (`EGG_PIPELINE_REPO`); **plus a second collapse the issue does not mention**: `orchestrator/commit_authorship_store.py:932-933` (`next((r for r in repos if r.name == "egg"), repos[0])`) |

## GROUNDING CORRECTIONS — flag for planner

1. **Naming drift in the issue table.** The gateway-client method is
   `create_worktree` (singular), not `create_worktrees`, and the returned
   mapping is `worktrees`, not `repo_volumes`. Substance of the claim is
   right; use live names when writing tasks.
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
4. **A second `repos[0]` collapse exists** at
   `orchestrator/commit_authorship_store.py:932-933` (prefers the repo named
   `"egg"`, falls back to `repos[0]`). The issue names only the agent-env
   collapse. Any "stop the collapse" acceptance criterion must cover both
   sites (and a `grep -rn 'repos\[0\]'` sweep at implement time).

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
2. **Merge-sequencing gate semantics → registered as HITL decision-1**
   (operator picks; see below). This is the piece the issue flags as most
   likely to be underestimated, and the option space (orchestrator polling
   vs. HITL hold vs. draft-until-upstream-merges) is a genuine operator
   fork on rigor-vs-automation.
3. **Test-gate / reviewer-diff scoping:** the slice↔repo 1:1 rule makes this
   mechanical — the test gate runs in the slice's repo's worktree only, and
   the reviewer diff is `git diff` in that worktree against that repo's
   base. No cross-repo diff surface exists in v1.
4. **Naming/status surfaces:** pipeline id/naming keys off the **primary**
   repo (first in the submitted list unless explicitly flagged); branch
   naming is uniform across repos; status surfaces render per-repo PR lists.
   Planner spells out exact renderings.

## Hard parts (carried into the contract; do not under-scope)

1. **Persisted-JSON migration** for `Slice.repo` (+ pipeline repo-list) —
   pattern exists (four precedents), but refine/plan/implement all read
   contracts, so the migration must land before any producer writes the new
   field.
2. **Atomic cross-repo landing is impossible** — GitHub merges PRs
   independently. v1 ships an *ordering hold*, not atomicity. The
   merge-sequencing gate design is HITL decision-1.
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
4. Agent environment exposes the **full** repo→worktree map; both `repos[0]`
   collapse sites (`kubernetes_spawner/_spawn.py`,
   `commit_authorship_store.py`) are gone, plus a sweep for any others.
5. Slice PRs are created in `slice.repo`; each participating repo (≥1 slice)
   gets its own work branch + context PR; PR descriptions cross-reference
   sibling PRs in the pipeline.
6. Cross-repo ordering expressed via existing `Slice.dependencies`; a
   dependent slice whose upstream slice lives in another repo is held by the
   v1 merge-sequencing mechanism chosen in decision-1.
7. Test gate and reviewer diffs scope to the slice's repo only.
8. `make lint` + `make test-all` green; single-repo pipelines are the
   regression baseline (no behavior change for N=1).

## Explicitly OUT of scope (v1)

- Mixed auth modes across repos (later phase; session-model redesign).
- Richer first-class merge-sequencing machinery beyond the v1 hold.
- Cross-repo atomic merge (impossible on GitHub; do not attempt to fake it).
- Any two-repo special case or primary+secondary data-model shape.

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
