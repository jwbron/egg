# Coder BRC memory — issue-3393 (multi-repo pipelines)

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

### KEY DEVIATION (be consistent on re-spawn; flagged for reviewer_contract)
Plan task-2-2 said put `assert_uniform_visibility`/`assert_uniform_auth` in
`gateway/repo_visibility.py` + `gateway/git_client/_credentials.py` and call them
from the submission path. **The orchestrator image (orchestrator/Dockerfile)
ships `config/repo_config.py` + `shared/` but NOT `gateway/`** — those gateway
modules are un-importable from `routes/pipelines.py` at runtime. So:
- `assert_uniform_auth` → added to **`config/repo_config.py`** (canonical home of
  `get_auth_mode`, bundled into BOTH images), imported by the orchestrator.
- **visibility** uniformity → inline via `GatewayClient.get_repo_visibility`
  (HTTP; the orchestrator's only visibility source; mirrors `_compute_gateway_mode`
  at pipelines.py:3033). `internal` counts as private. Fail-open on an
  indeterminate per-repo lookup (matches existing behavior) with a warning.
This meets all behavioral acceptance criteria; it changes *where* the guards live
(repo_config + inline) vs. the plan's gateway-module placement, to be
runtime-correct and avoid dead code. If a reviewer NACKs wanting the gateway-file
placement, the counter is the Dockerfile container boundary.

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
