# Coder BRC memory — issue-3393 (multi-repo pipelines)

## Slice-6 — per-repo test-gate + reviewer-diff scoping + per-repo conventions

**Branch:** `egg/issue-3393-slice-6-coder/work`
**Task:** task-6-1 — implemented. **File:** `orchestrator/routes/pipelines.py` only.

### Change model (what landed)
All wiring is in `_run_concurrent_phase` (the per-slice team spawn driver,
receives `slice_id`, `pipeline`, `worktree_repo_path`, `repos`,
`repo_volumes`). The implement-phase test gate and reviewer diff both run
INSIDE the agent containers (tester's configured checks; reviewers'
`git diff origin/<base>...HEAD`) — there is NO orchestrator-side
`make test`/`make lint` subprocess in pipelines.py (grep confirmed: only
prompt/doc strings). So scoping = threading the slice's repo / worktree /
base-branch into the prompt builder + spawn, not moving a subprocess.

- **New helper `_resolve_slice_worktree_path(pipeline, slice_repo, fallback)`**
  (beside `_resolve_pipeline_worktree_path`): returns
  `WORKTREE_BASE_DIR/pipeline.id/<slice_repo bare>` if it exists, else
  fallback (pipeline-primary worktree). Mirrors the existing per-repo
  worktree layout (slice-3 owner/repo keying).
- **Slice-repo scoping block** in `_run_concurrent_phase`, GATED on
  `slice_id and len(pipeline.repos) > 1` (so N=1 skips it entirely — no
  extra contract read, byte-identical): loads the contract, finds the
  slice, `resolved = resolve_slice_repo(slice_obj, pipeline)`. Only when
  `resolved != pipeline.repo` does it diverge — sets `slice_repo`,
  `slice_repo_path` (via the new helper), `slice_base_branch` (from the
  matching `RepoSpec.base_branch`), and `slice_repos = [resolved, *others]`.
  Contract-load failure soft-degrades to the primary (logged, non-blocking).
- **Base-branch resolution** now prefers `slice_base_branch or
  pipeline.base_branch`, then auto-detects in `slice_repo_path`.
- **`_build_agent_prompt`** now gets `repo=slice_repo`,
  `repo_path=str(slice_repo_path)` (was `pipeline.repo` /
  `worktree_repo_path`). This is the lever for AC "check/lint from the
  slice repo's conventions": `get_repo_checks(repo)` (tester prompt
  ~15963) + file-boundary `get_agent_pattern_for_repo(repo)` both key off
  `repo`; the reviewer diff base flows via `base_branch`.
- **`create_concurrent_spawn_fn`** now gets `repos=slice_repos`. Verified
  `_spawn.py:460` derives `primary_repo = next(iter(repos))` →
  `repo_path`/`EGG_REPO_PATH` (agent cwd) from the first repo, so
  slice-first ordering sets cwd to the slice's repo worktree. `repo_volumes`
  (full owner/repo map, slice-3) is passed unchanged.

### Converged with tester task-6-2 (commit 3db72777e)
Tester pinned a required coder-owned accessor via a task-6-1 gap:
`routes.pipelines._resolve_slice_gate_repo(slice, pipeline) -> str | None`
== `resolve_slice_repo(slice, pipeline)` (their `TestSliceGateRepoAccessor`
skips until it exists, then activates). Added it as the single gate-repo
source of truth and use it inside the scoping block. Their
`TestPerRepoWorktreeSelection` validates the existing
`routes.resolve_worktree_path` selects the slice's per-repo subdir — my
`_resolve_slice_worktree_path` is the functionally-equivalent
WORKTREE_BASE_DIR/pid/<repo_short> lookup used at the call site.

### N=1 byte-equivalence
`len(pipeline.repos) <= 1` ⇒ scoping block skipped ⇒ `slice_repo ==
pipeline.repo`, `slice_repo_path == worktree_repo_path`, `slice_repos ==
repos`, `slice_base_branch == None` ⇒ base resolution + prompt + spawn
identical to pre-change. The legacy slice-1 validator synthesises a
one-element `repos` for any legacy pipeline, so the guard holds.

### Documented boundary (preempt reviewer questions)
- **cwd via repos-reorder, not a new spawner param:** task-6-1 files=only
  pipelines.py, and `_spawn.py` already derives cwd from `repos[0]`;
  reordering is the minimal, in-scope lever. For a secondary-repo slice
  `EGG_PIPELINE_REPO` becomes the slice repo — correct under slice↔repo 1:1
  (authorship + naming should follow the slice's repo). `EGG_PIPELINE_REPOS`
  (full map) still exported from `repo_volumes`, unchanged.
- **`_build_slice_diff_summary` (PR-body "What's in this PR") NOT rescoped:**
  it's PR rendering (best-effort, soft-fails), not the reviewer's audit
  diff or the test gate, and its parent/integration branches are already
  per-slice. Left on the pipeline worktree; multi-repo PR-body diff is
  gated on task-7-1 (secondary worktrees) anyway.
- **Runtime multi-repo end-to-end depends on task-7-1** (secondary-repo
  worktree/branch materialisation → populates `repo_volumes` with the
  secondary + creates the on-disk worktree the helper resolves). This slice
  is structurally complete + forward-compatible: once task-7-1 lands, a
  secondary slice's team spawns in its own repo worktree with NO further
  change here.

### Validation (no venv — deps cert-blocked, same as slices 2/3/4)
- `py_compile` clean; `ruff check` "All checks passed!" on pipelines.py.
- Full pytest deferred to the tester (task-6-2/6-3): env can't `pip install`
  (charset-normalizer fetch fails on UnknownIssuer cert).

## Slice-4 — slice-PR routing to slice.repo + lazy per-repo context PR

**Branch:** `egg/issue-3393-slice-4-coder/work` (base = slice-3 tip aa567fa67)
**Tasks:** task-4-1, task-4-2 — implemented in this slice.
**Files:** `orchestrator/gateway_client/_pr.py`, `orchestrator/routes/pipelines.py`.

### task-4-1 (slice-PR → slice.repo + cross-repo refs)
- `_pr.py`: `create_slice_pr` gains `sibling_pr_refs` + `upstream_pr_ref`
  params; new module-level `_format_pr_ref` (renders `owner/repo#N`,
  drops malformed/`<1`/bool) + `_append_related_prs_section` (emits
  `## Related PRs` only when non-empty). Section placed after
  `## This slice`, before `## Stack`.
- `pipelines.py` slice caller (~19229): compute
  `slice_repo = resolve_slice_repo(slice_obj, pipeline) or pipeline.repo`
  (local import of `resolve_slice_repo` from `models`); build
  `sibling_pr_refs` = OTHER slices whose resolved repo != slice_repo AND
  have `pr_number` (CROSS-repo only); `upstream_pr_ref` = first-dependency
  slice's PR only when cross-repo. Stored in `slice_pr_data`; the
  `create_slice_pr` call now passes `repo=slice_pr_data["slice_repo"] or
  pipeline.repo` + the two ref sets.
- **N=1 byte-equivalent:** slice_repo == primary == pipeline.repo; all
  refs are cross-repo-only ⇒ empty ⇒ section omitted ⇒ body unchanged.
  Same-repo stacking stays in `## Stack`.

### task-4-2 (lazy per-repo context PR)
- **Tester-pinned interfaces (converged with task-4-3, commit 52ed962fc):**
  (1) module-level `_repos_with_slices(contract, pipeline) -> list[str]`
  = participating repos owning ≥1 slice, ordered by `pipeline.repos`,
  deduped, slice-less excluded (matches tester's `_expected_
  participating_repos`). (2) `_compose_context_pr_body` renders each
  slice's PR link REPO-QUALIFIED (`owner/repo#N`) when the slice's
  resolved repo != the context PR's repo, bare `#N` when same-repo — new
  `context_repo` param (default = primary). Tester test
  `test_cross_repo_sibling_is_repo_qualified` asserts `jwbron/consumer#200`
  present + `#100` bare.
- `_compose_context_pr_body` also keeps optional `sibling_context_prs`
  (default None ⇒ N=1 unchanged); renders `## Coordinated repos`
  (`owner/repo#N`) only when provided (context-PR↔context-PR refs).
- New `_maybe_open_secondary_context_prs` (guard: `len(pipeline.repos)>1`,
  never raises) → `_open_secondary_context_prs`. Called at BOTH return
  sites of `_open_context_pr_at_implement_start` (idempotent hit + fresh
  create). Helper: loads contract, computes repos-with-≥1-slice via
  `resolve_slice_repo`, drops primary, and per secondary repo
  lookup-or-create a `egg/<id>/work` context PR (per-repo base from
  `RepoSpec.base_branch` else `main`), then cross-refs all bodies
  (primary + secondaries) via `update_pr_body`. Slice-less submitted
  repos skipped.
- **N=1 byte-equivalent:** guard `<=1` (N=1 repos has exactly 1) ⇒ helper
  never invoked ⇒ zero extra work; primary opener path untouched.

### KNOWN LIMIT (documented in code + proposal) — secondary-worktree dep
Opening a secondary context PR (and routing an N>1 slice PR) needs that
repo's `egg/<id>/work`/integration branch to exist on its remote, which
needs a SECONDARY-repo worktree to push. Slice-3 explicitly deferred
threading the full repo set into worktree CREATION (only the primary is
materialised today). So at runtime secondary `create_pr` typically fails
on a missing head branch — helper SOFT-FAILS (log + continue) and ADOPTS
an already-open secondary PR via launcher-auth `lookup_open_pr` (works
per-repo, no worktree). Iteration + cross-ref structure is complete and
forward-compatible: once secondary worktree/branch creation is wired
(a later slice), secondary context PRs open with NO further change here.
This is the honest slice-4 boundary — reviewer_contract: the acceptance
"every repo with ≥1 slice gets a work branch + context PR" is delivered
STRUCTURALLY; runtime completion is gated on the deferred worktree wiring.

### Validation (no venv — cert-blocked, same as slices 2/3)
- `py_compile` + system `ruff check` clean on both files.
- Pure-logic sanity: `_format_pr_ref`/`_append_related_prs_section`
  (N=1 empty, dedup, malformed-drop, upstream+siblings) + coordinated-
  repos renderer all pass. Full pytest deferred to tester (task-4-3).

## Slice-3 — stop the repos[0] collapse; owner/repo-keyed worktree map

**Proposed commit:** `5601063cb` (branch `egg/issue-3393-slice-3-coder/work`)
**Tasks:** task-3-1, task-3-2 — both implemented, committed, marked complete.

### Change model (what landed)
- **task-3-1 (three collapse sites removed + full env map):**
  - `kubernetes_spawner/_spawn.py`: `primary_repo = next(iter(repos or []), None)`
    (repos is canonically primary-first; avoids literal `repos[0]`). EGG_REPO_PATH
    + EGG_PIPELINE_REPO still derive from the primary. NEW: `EGG_PIPELINE_REPOS`
    env = JSON `{owner/repo: /home/egg/repos/<bare-name>}` built from
    `repo_volumes` keys, so a per-slice agent can select ITS repo (AC-4). Left
    unset when `repo_volumes` empty/None.
  - `commit_authorship_store.py:_resolve_authorship_repo_path`: dropped the
    `len==1: return repos[0]` + blind `repos[0]` fallback. New order: primary
    hint via `EGG_PIPELINE_REPO` (match bare name) → `egg` → `next(iter(repos))`.
    Hint is usually absent in the orchestrator process ⇒ degrades to prior
    egg/first heuristic (behavior-preserving there).
  - `routes/pipelines.py:_spawn_overseer_agent`: `overseer_repo =
    next(iter(pipeline_repos or []), None)` (was `pipeline_repos[0]`).
- **task-3-2 (owner/repo re-key at source):**
  - `gateway/gateway.py` `worktree_create` (~7767): key response map by full
    `repo` slug (was `repo_name` bare). Bare-name callers unaffected (repo ==
    repo_name). On-disk dir/mount stay bare.
  - `gateway_client/_models.py` + `_worktree.py`: doc-only (client passthrough).

### Load-bearing consumer fix (why pipelines.py is in BOTH tasks)
Re-keying broke the pipeline-level worktree path derivation at
`routes/pipelines.py` (~24620): it matched `repo_short` (bare) against the map
and rebuilt `WORKTREE_BASE_DIR/worktree_id/<key>`. Fixed: match `pipeline.repo`
(full slug, now the key) and strip the owner prefix (`.split('/')[-1]`) when
joining the on-disk path. The spawner reuse helpers
(`_validate_worktree_for_reuse` / `_clean_reused_worktree` /
`_find_missing_worktrees`) ALREADY keyed vols by full `ref` and built bare-name
paths — no change needed. `host_path_mounts` (line ~630) already handles
owner/repo keys.

### Scope decisions (preempt reviewer questions)
- **Did NOT touch `pipelines.py:24556` (`pipeline_repos = [pipeline.repo]`).**
  Not one of the three enumerated collapse sites; not a `repos[0]` token; the
  plan scoped slice-3 to the three named sites + the re-key. Threading the full
  repo LIST into pipeline-level worktree CREATION is out of slice-3 scope.
  Consequence: the exposed map is structurally list-shaped/owner-keyed but today
  holds the primary until a later slice threads the full set into creation.
- **Did NOT touch `gateway.py:8584` (`/api/v1/sessions/create`).** Separate
  endpoint/flow; plan scoped only the `/api/v1/worktree/create` response (~7827).
  Left bare-name; repo_volumes for the spawner comes from worktree/create, not
  sessions/create.
- **Same-short-name container-path collision is pre-existing & out of scope.**
  Both `ownerA/foo` and `ownerB/foo` still mount to `/home/egg/repos/foo` (host
  path leaf = bare name). Ruling #6 only mandates distinct MAP KEYS, which we
  deliver. Distinct container paths would be a deeper change.

### Validation done locally (no venv — deps can't be installed here)
- `python3 -m py_compile` clean on all 6 changed files; system `ruff check`
  "All checks passed!".
- Behavioral sanity (pure logic): primary-pick (`next(iter(...))`) for
  populated/empty/None; EGG_PIPELINE_REPOS map shape; authorship resolver
  (primary-hint wins / egg / first / N=1) — all pass.
- `grep -rn 'repos\[0\]' orchestrator/` shows only comments + the INTENTIONAL
  `models.py` primary_repo accessor/validator mirror (allowlisted by plan) +
  the `len==1`-guarded `sandbox/egg_lib/sdlc_hitl.py:82`. No collapse remains.
- Full pytest deferred to tester (env can't install deps).

## Slice-2 — list-shaped submission + uniform visibility/auth validation

**Proposed commit:** `390def500` (branch `egg/issue-3393-slice-2-coder/work`)
**Tasks:** task-2-1, task-2-2 — both implemented, committed, marked complete.

### Change model (what landed)
- **task-2-1 (list-shaped submission):**
  - `mcp_tools/_tool_defs.py`: added `repos` array param to `submit_task`
    ({repo, base_branch?, primary?} items); relaxed `required` → `["description"]`.
  - `mcp_tools/_submit.py`: normalize `repos` canonically **primary-first**
    (index 0 == primary; transient `primary` flag dropped), mirror primary onto
    legacy `repo`/`base_branch`; error if both `repo` and `repos`, or neither.
  - `routes/pipelines.py`: `_normalize_submission_repos()` validates+orders the
    list (same repo/base regexes as singleton; **same-short-name-different-owner
    NOT rejected**), derives primary onto the singleton early (before the
    Missing-repo/format guards), builds `repos_specs: list[RepoSpec]` and passes
    it to `create_pipeline`. **No repos[0] collapse** (that's slice 3).
  - `state_store/_crud.py`: `create_pipeline` gained optional `repos` kwarg →
    `Pipeline.repos`. N=1 unchanged: `repos=None` ⇒ slice-1 validator synthesizes
    the one-element list from the legacy singleton.

- **task-2-2 (uniformity validation):** `_assert_repo_set_uniform()` in
  pipelines.py rejects mixed-visibility or mixed-auth repo sets with an
  actionable, repo-naming **400**. Single repo (after de-dup) = trivially
  uniform, no gateway round-trip.

### Uniformity guard placement — reconciled with tester's task-2-3 interface
The tester (task-2-3, commit 6ecabe787, merged into this branch at convergence)
pinned the interface: `gateway/repo_visibility.py` must expose
`validate_visibility_uniformity(repos)` and `validate_auth_mode_uniformity(repos)`
(both raise ValueError naming offenders; internal==private; same-name/diff-owner
NOT rejected; single/uniform no-op). Their gateway tests patch
`repo_visibility.get_repo_visibility` and `repo_config.get_auth_mode`.

Implemented to match EXACTLY, with a single source of truth per dimension:
- `gateway/repo_visibility.py`: `validate_visibility_uniformity` (canonical
  visibility logic, splits owner/name → `get_repo_visibility(owner, name)`) +
  `validate_auth_mode_uniformity` (delegates to `repo_config.assert_uniform_auth`).
- `config/repo_config.py`: `assert_uniform_auth(repos)` — canonical auth rule,
  beside `get_auth_mode`, bundled into BOTH gateway + orchestrator images.

### v2 — reviewer_security NACK addressed (fail-closed on indeterminate visibility)
reviewer_security v1 NACK: the `if vis is None: continue` silently dropped a repo
from the uniformity vote, so a multi-repo private+public set could be ADMITTED
when a secondary momentarily resolved to None (no downstream re-check —
`_compute_gateway_mode` reads only the PRIMARY repo). Fixed in BOTH twins:
- Multi-repo (`len(unique) > 1`): a repo whose visibility is not a known
  `public|private|internal` bucket (None OR unrecognized label) now FAILS CLOSED
  — orchestrator returns an actionable 400, gateway `validate_visibility_uniformity`
  raises ValueError, both naming the repo. N=1 short-circuits before any lookup
  (no availability cost; gateway helper now also short-circuits `len<=1`).
- Auth `except Exception` (config-read failure) now also fails closed for
  consistency (local bundled read ⇒ genuinely exceptional).
Verified fail-closed (None + unrecognized) + still-uniform + still-mixed cases
in isolation; tester's existing gateway/orchestrator uniformity tests remain green.

**Container-boundary fact (still load-bearing):** the orchestrator image
(orchestrator/Dockerfile) ships `config/repo_config.py` + `shared/` but NOT
`gateway/`, so `routes/pipelines.py` CANNOT import
`gateway.repo_visibility.validate_visibility_uniformity`. The orchestrator
submission enforcement therefore uses `_assert_repo_set_uniform`: auth via
`repo_config.assert_uniform_auth` (shared with the gateway helper) and visibility
inline via `GatewayClient.get_repo_visibility` (HTTP; mirrors `_compute_gateway_mode`).
The inline visibility comparison is the deliberate HTTP-boundary twin of the
gateway helper (cross-referenced in code); the small mirror is forced by the
boundary. `gateway/git_client/_credentials.py` was NOT touched — the tester's
auth patch targets `repo_config.get_auth_mode`, and the canonical helper lives in
repo_config.

### Validation done locally (no venv — network/cert blocked, can't pip install)
- `ruff check` clean on all 5 changed files; `py_compile` clean.
- Behavioral sanity (system python + pydantic): Pipeline repos↔singleton mirror,
  `primary_repo`, `resolve_slice_repo`, `assert_uniform_auth` uniform/mixed/single/
  empty, and `_normalize_submission_repos` (primary flag, bare-string entries,
  same-short-name allowed, all error paths) — all pass.
- Full pytest suite deferred to the tester (env can't install deps here).

### Files touched (beyond plan's named lists, all necessary + justified)
_tool_defs.py (schema), _submit.py, routes/pipelines.py, state_store/_crud.py,
config/repo_config.py. Did NOT touch gateway/repo_visibility.py or
gateway/git_client/_credentials.py (see deviation).
