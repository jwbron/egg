# Analysis: Make `make test` changeset-aware (only run tests affected by diff from base branch)

> Issue: #1973 | Phase: refine

## Problem Statement

`make test` today runs the full unit-test suite (~356 test files across `tests/`, `gateway/tests/`, `orchestrator/tests/`, `shared/tests/`) regardless of what changed. For the inner loop that SDLC pipeline agents (and humans on a branch) spend most of their time in, that is both slow and wasteful: iterating on a single leaf module under `gateway/` still runs every orchestrator and shared test.

The desired outcome is a `make test` target that runs **only the tests that could be affected by the current branch's diff**, while preserving a correctness-first posture (never skip a test that exercises a changed code path) and keeping a full-suite escape hatch for CI, release, and any time the narrowing logic is unsure. The proposal in the issue sketches a static reverse import graph with a tracked Last-Known-Good (LKG) baseline; much of it is explicitly flagged as negotiable and is what this analysis focuses on.

## Current Behavior

- `Makefile:251-254` defines `make test`: sets `PYTHONPATH := shared:gateway:orchestrator`, then runs `pytest tests/ gateway/tests/ orchestrator/tests/ shared/tests/ -v -m "not functional" $(PYTEST_ARGS)`.
- `pyproject.toml:181-201` (there is no `pytest.ini` / `setup.cfg`) defines `testpaths`, `timeout = 60`, and the custom markers (`integration`, `functional`, `e2e`, `security`, `agent_flaky`).
- `pyproject.toml:208` builds wheels for 8 packages: `gateway`, `shared/egg_config`, `shared/egg_container`, `shared/egg_contracts`, `shared/egg_logging`, `shared/egg_git`, `shared/egg_restrictions`, `sandbox/egg_lib`. `orchestrator/` is **not** a wheel — it is imported as bare modules via the `PYTHONPATH` tweak and `orchestrator/tests/conftest.py:22-29`.
- **Four `conftest.py` files** do `sys.path` injection before tests run: `tests/conftest.py:12-16`, `shared/tests/conftest.py:13-16`, `gateway/tests/conftest.py` (+ `importlib` rewrite for hyphenated dir), `orchestrator/tests/conftest.py:22-29`. Tests import via bare names (`from models import Pipeline`), not fully-qualified package paths.
- **Dynamic imports** in source: `gateway/gateway.py:304` (`__import__`), `gateway/gateway.py:309-322` (`importlib.util.spec_from_file_location` + `exec_module` block), `gateway/commit_observer.py:182` and `gateway/git_client.py:1727` (`importlib.util`), plus `__import__("re")` / `__import__("threading")` string-literal patterns in `gateway/filtered_push.py` and `gateway/gateway.py:2837`. Tests also use `SourceFileLoader` to load hyphenated scripts (`tests/tools/test_discover_tests.py:13-20`). `sandbox/egg_lib/__init__.py:7` notes `SourceFileLoader` is used by the `egg` launcher.
- **CI** (`.github/workflows/test.yml:31-37`) invokes `make test PYTEST_ARGS="--cov=gateway --cov=shared --cov=sandbox --cov-report=term-missing --cov-fail-under=80"`. The `--cov-fail-under=80` gate is computed against the tests that actually run — narrowing the set will drop aggregate coverage below 80% and fail CI unless the gate is disabled for narrowed runs.
- **Checkout** in CI uses `actions/checkout@v4` with default `fetch-depth: 1`, so `origin/main` is not locally available for `git diff` without an explicit deeper fetch.
- **No existing test-selection tooling** in the repo: no `pytest-testmon`, no `grimp`, no `pydeps`/`importlab`, no `pyan`. The only existing graph infrastructure is `shared/egg_contracts/dependency_graph.py` (an agent-role DAG, not an import graph) and `orchestrator/review_graph.py` (BRC reviewer graph). `scripts/check-claude-imports.py` is regex-only.
- **No pre-merge hook / merge queue** today — PRs are merged via the GitHub UI. Pre-commit is `ruff` only (`.pre-commit-config.yaml`). There is no mechanism today that would automatically rewrite a tracked file on merge.
- Existing test count: **356 test files**, **283 source `.py` files** across `gateway/`, `orchestrator/`, `shared/`, `sandbox/` (excluding tests). That skew (tests outnumber source 1.25×) matters when reasoning about the payoff of narrowing.

## Constraints

- **Correctness first**: any test that transitively imports a changed module, or otherwise exercises it (via fixtures, `importlib`, plugin registries, subprocess, data files), must run. Miss-correctness is worse than no narrowing.
- **Python 3.13** runtime (`.github/workflows/test.yml:23`).
- **Monorepo layout with mixed install modes**: some packages are wheels (`gateway`, `shared/egg_*`, `sandbox/egg_lib`), some are bare-name imports via `PYTHONPATH` (`orchestrator`, and the `shared/` packages locally). Any import-graph tool must be configured with the correct set of top-level packages and the same `sys.path` the tests see — otherwise it will miss edges or treat internal imports as external.
- **Conftest chain is implicit**: pytest walks up from every test file collecting `conftest.py` fixtures. A change to `tests/conftest.py`, `shared/tests/conftest.py`, `gateway/tests/conftest.py`, or `orchestrator/tests/conftest.py` affects **every** test below it even though no `import` statement makes that edge visible to a static graph.
- **Dynamic imports in gateway**: `gateway/gateway.py:304-317` loads modules by name / path at runtime. A static graph will not see these edges.
- **CI coverage gate**: the current `--cov-fail-under=80` is a hard constraint that is incompatible with narrowed runs unless reconfigured.
- **Agent sandbox environment**: SDLC agents run in restricted sandbox containers with a gateway-policed git remote. Auto-commits to LKG on a branch must still pass the gateway's file-boundary rules for the **coder/tester/reviewer** roles; `.egg/last-known-good` needs a role-agnostic allowlist or the LKG update mechanism has to move off-tree.
- **Shallow checkouts**: CI checkout is `fetch-depth: 1`; `git diff origin/main...HEAD` requires a deeper fetch. Any implementation that uses the base branch as baseline must ensure the baseline ref is actually present locally.
- **No merge queue**: the proposed "pre-merge hook rewrites `.egg/last-known-good` to main's pre-merge HEAD" has no execution surface today — GitHub merge commits are produced by the UI, not by a hook we control. A merge-commit rewrite needs either a merge queue, a GitHub Action that opens a follow-up commit, or a different LKG storage design that doesn't require merge-time mutation.
- **Parallel branches**: agents frequently iterate on multiple branches in parallel via git worktrees. Whatever LKG storage we pick must not alias across worktrees.
- **`make test` performance target**: no quantitative budget is stated in the issue; the value is a smaller selected set and a faster feedback loop, but the break-even point for graph-construction overhead should be validated.

## Options Considered

### Option A: Custom AST-based reverse import graph (the issue's baseline proposal)

**Approach**: Hand-roll an AST walker that parses every `.py` file under `gateway/`, `orchestrator/`, `shared/`, `sandbox/`, and the four test roots, resolves imports to in-repo modules, builds a reverse-edge graph, and computes the transitive closure from the changed file set. Grep for `importlib` / `__import__` / plugin markers and widen selection when matched. Tracked `.egg/last-known-good` file picks the baseline.

**Pros**:
- Zero third-party dependency; fully in-house and auditable.
- Follows the same AST-graph style already used by `shared/egg_contracts/dependency_graph.py`.
- Fits in a single script under `scripts/` — easy to iterate on without API commitments.

**Cons**:
- Reinvents a solved problem (graph construction, namespace-package handling, relative-import resolution, cycle handling, PEP 420 implicit packages).
- Correctness of the parser becomes a permanent maintenance burden — every Python version bump (match statements, PEP 695 type params) is a bug-surface.
- Still doesn't solve dynamic imports except via crude grep.
- No caching story unless we add one; full parse every invocation is ~300 files.

### Option B: `grimp`-based static reverse import graph

**Approach**: Use [`grimp`](https://pypi.org/project/grimp/) (actively maintained, v3.14 as of 2025-12) to build the import graph from a declared set of top-level packages; query `graph.find_downstream_modules(changed_module)` (i.e., the reverse-transitive closure) per changed file; map the resulting module set onto test files. Same LKG-vs-base-branch baseline resolution as Option A. Widening for dynamic imports and fallback triggers handled the same way. Optionally wrap in a `scripts/select-tests.py` helper invoked by the `Makefile`.

**Pros**:
- Grimp handles relative imports, namespace packages, `TYPE_CHECKING` blocks, and cycles correctly; actively maintained on current Python versions.
- `find_downstream_modules(module, as_package=True)` is the exact primitive we need — no graph-algorithm code to write ourselves.
- Used in production for monorepo test selection (Qik's `pygraph` plugin does exactly this).
- Fast: Rust-backed graph builder; negligible for ~300 files.
- Leaves us room to add layer/architecture checks later (Grimp's other use case).

**Cons**:
- Adds a runtime dependency to the dev extras (small, pure-Python surface).
- Configuring it for our mixed wheel/bare-PYTHONPATH layout requires thought — `orchestrator` is imported as bare names, so we must pass `orchestrator` as a top-level package and make sure grimp sees it.
- Like Option A, still misses dynamic imports, subprocess launches, fixture-driven indirect coupling, and data-file loads.

### Option C: Coverage-based dynamic selection (`pytest-testmon`)

**Approach**: Install `pytest-testmon`; CI/dev run with `--testmon`; testmon maintains `.testmondata` (SQLite) of coverage-per-test and re-runs only tests whose covered code changed.

**Pros**:
- Correct by construction for what gets *executed*: catches dynamic imports, fixtures, `importlib` dispatch, indirect monkeypatching — anything static analysis misses.
- Function-level granularity (narrower than module).
- Mature, well-documented, actively maintained (v2.2.0 in 2025).

**Cons**:
- Requires a seed full run to populate the database; the DB must travel with the branch or be rebuilt per CI job (the tarpas/testmon maintainers explicitly recommend full runs on main to avoid drift).
- `.testmondata` is the equivalent of LKG but with much larger storage (coverage map, not a SHA) — either tracked in git (noisy, binary), stored as a CI artifact (CI-only, doesn't help the local inner loop), or rebuilt every run (defeats the point).
- Subprocess calls, file-based data, and test-order-dependent state are the explicit weak spots (per testmon's own docs). Same-process dynamic imports like `gateway/gateway.py:304` / `:309-322` **are** observed by testmon via `coverage.py`, so the real miss-mode for this codebase is subprocess-crossing coverage (sandbox launches, child `pytest` invocations), not in-process `importlib`.
- Interacts awkwardly with `pytest --cov` (our CI coverage gate) — both plugins instrument coverage.py differently.

### Option D: Hybrid — static graph (grimp) as first-pass + pytest-testmon under `make test-fast` (or similar)

**Approach**: Use grimp-based selection for the normal `make test` flow (cheap, stateless, fast). Add an opt-in `make test-fast` (or `--testmon`) that layers pytest-testmon on top for tight local loops where the developer/agent is iterating on one test. Keep `make test-all` as the unconditional full-suite target, which updates LKG on pass.

**Pros**:
- Complementary failure modes: static catches what testmon misses (never-executed tests that should now run) and testmon catches what static misses (dynamic edges).
- Gives agents a fast default and humans an even-faster opt-in.

**Cons**:
- Two systems to build, two systems to maintain, two sources of correctness surprises.
- Reasonable as a phase-2 follow-up, overkill as a phase-1 design.

### Option E: Directory-based path mapping (rejected by the issue)

**Approach**: Map `gateway/**` → `gateway/tests/**`, etc. Run the test subdirectory matching the changed directory.

**Pros**: Trivially simple.

**Cons**: Misses cross-package imports (`orchestrator/` tests routinely import `gateway` and `shared`; `shared` changes affect everyone). The issue already rules this out — included here only for completeness.

## Recommended Approach

**Option B (grimp-based static reverse import graph) with conservative fallbacks, backed by a sidecar LKG file** — with the explicit caveat that several sub-decisions (LKG storage and merge semantics in particular) should be settled in plan phase before implementation, and a follow-up testmon layer (Option D) is deferred to a later issue if static selection turns out to be too coarse.

Justification:

1. **Static wins for our workload.** The SDLC-pipeline inner loop wants a fast, stateless, deterministic selection per commit — no "prime the database first, then run" step. Grimp gives us that without us owning a parser. Testmon-style dynamic selection is a better fit for a human developer's watch-mode than for CI-shaped agent runs.
2. **Grimp over hand-rolled** because we already have enough AST-maintenance surface; we should not pay that tax again for a strictly-better available library. The dependency is cheap; the code we'd otherwise write is expensive to get right (namespace packages, `TYPE_CHECKING`, relative-import edge cases).
3. **Conservative fallbacks** are load-bearing. Any of these should force a full run: lockfile / `Makefile` / `pyproject.toml` / root or package-level `conftest.py` change; a changed file that does not map to a repo module; detection of `importlib` / `__import__` / `SourceFileLoader` / entry-point-plugin patterns touching a module in the closure; empty diff or unresolvable baseline; LKG no longer an ancestor of `HEAD`.
4. **LKG storage should move off `.egg/last-known-good` as a tracked file** and onto a non-tracked sidecar (e.g., `.egg-state/last-known-good/<branch>.sha`, in `.gitignore`). Rationale: a tracked file forces an auto-commit after every `make test-all` pass, which pollutes branch history, interacts poorly with rebase/squash merges, and has no clean answer for "who may push `.egg/last-known-good`?" under the gateway's role-boundary rules. A sidecar avoids all of those. The tradeoff — sidecar doesn't travel with a fresh clone and is lost on `git clean -fdx` — is acceptable because LKG is a *cache*, and a missing LKG gracefully falls back to the base branch. Git notes and stash-based approaches were considered but add their own coordination cost. **This flips the proposal's default** and needs explicit human sign-off (see Open Questions).
5. **Merge-time LKG rewrite is unnecessary with the sidecar approach** — no tracked state means no merge-time mutation. If we stick with a tracked file, the "rewrite to `main`'s pre-merge HEAD on merge" logic lives in a new GitHub Action (merge queue is not on the table today) and should be scoped explicitly.
6. **Dynamic-import widening** needs to be more than a grep. Recommendation: (a) during graph construction, scan for `importlib.{import_module,util,machinery}`, `__import__`, `SourceFileLoader`, and known entry-point patterns, and record which modules contain them; (b) if any changed module is one of those *or* is reachable from one (as source or sink), widen to full suite. This is conservative but honest about what static analysis can see.
7. **CI coverage gate**: CI should not use the narrowed `make test`. Recommend pointing `.github/workflows/test.yml` at `make test-all` (or keeping the current `make test` invocation but wiring `make test-all` semantics to equal the current behaviour — naming is an open question). Narrowing in CI is out-of-scope for this issue; the proposal is explicit that the CI/release path should keep the current behaviour.
8. **Non-Python changed files**: conservative fallback to full suite unless an allowlist maps them to "no test impact" (README changes, top-level docs). Allowlist content should be minimal on day 1 to stay safe.

The load-bearing constraints from the issue (correctness-first, minimal waste, full-suite escape hatch, per-branch isolation) are honored; the per-branch isolation in particular becomes trivial with a sidecar design because there is no shared tracked state.

## Open Questions

Every ambiguity below needs an answer from the human before the plan phase can design the implementation. **All items are registered via `egg-contract`**; this prose list mirrors the nine HITL decisions and the eleven most important feedback questions for readability. The contract also holds one additional free-form feedback item — **"Fallback-trigger list completeness"** (`feedback-1/Q10`) — not duplicated below.

### Decision questions (multiple-choice — see contract)

1. **Selection mechanism** — grimp vs hand-rolled AST vs pytest-testmon vs hybrid. The recommendation is grimp; the issue explicitly flags this as reopen-able.
2. **LKG storage** — tracked file (`.egg/last-known-good`), git notes, non-tracked sidecar in `.egg-state/`, or abandon LKG entirely and always baseline on `$(BASE_BRANCH)`. The recommendation flips the proposal's default to sidecar.
3. **LKG update mechanism** — auto-commit after every passing `make test-all`, amend onto HEAD, sidecar file write (no commit), or manual-only (`make test-record-good`).
4. **Merge behaviour for LKG** — rewrite to main's pre-merge HEAD (proposal), strip on merge, leave branch-local values alone, or N/A (if we pick sidecar).
5. **Dynamic-import handling** — grep-and-widen on any match, widen-only-if-closure-overlap, runtime import-hook-on-seed-run, or a shadow-imports file (Qik `qikimports.py` style).
6. **CI behaviour** — CI uses `make test-all` (full suite always), CI uses `make test` with narrowing enabled, or CI uses `make test` but without the `--cov-fail-under=80` gate when narrowing is active.
7. **Shallow checkout** — deepen `actions/checkout` to `fetch-depth: 0`, fetch baseline ref on demand inside `make test`, or require callers to have baseline already fetched (fall back to full suite when absent).
8. **Target name** — `make test-all` (issue proposal), `make test-full`, or keep `make test` as full-suite and introduce a new narrowed target (`make test-changed` / `make test-fast`).
9. **Graph granularity** — module-level (grimp default), file-level (one node per `.py`), or function-level (requires pyan or testmon).

### Open-ended questions (free-form — see contract)

10. What performance/size target makes this issue successful? (e.g., "a one-file change in a `gateway/` leaf module should select ≤ N tests and complete in ≤ T seconds on the reference runner.") Without a target, we cannot validate the implementation.
11. How should the narrowed `make test` interact with coverage reporting — drop coverage entirely, run coverage only against the selected source set, or keep global coverage but skip the threshold gate?
12. For the `shared/tests/` fixtures that are "used repo-wide," which exact files should trigger a full-suite fallback? (Currently ambiguous; recommend enumerating.)
13. Are there non-Python files under the repo (YAML/JSON/text) loaded at test runtime that should either (a) force a full-suite fallback or (b) be added to an explicit no-impact allowlist? Spot-check didn't turn up fixture directories, but the human may know of some.
14. Does the gateway's file-boundary policy permit the **refiner/coder/tester** roles to push an auto-commit that touches `.egg/last-known-good`? If not, the tracked-file design is dead on arrival for agent-driven runs regardless of other merits.
15. In worktree scenarios (agents running multiple branches in parallel via `git worktree`), is a sidecar file keyed by `<branch>` sufficient, or do we need keying by worktree path too?
16. Should there be a "canary" mode — e.g., every Nth `make test` run on a branch forces a full suite to catch cases where static selection silently misses a test — and if so, what cadence?
17. Is there an appetite for adding `pytest-testmon` in a follow-up issue (Option D), or should we treat this issue as closing the door on dynamic selection entirely?
18. How should `make test` behave when invoked with explicit paths/args (e.g., `make test PYTEST_ARGS="gateway/tests/test_specific.py"`) — bypass narrowing (treat as developer override), intersect with the narrowed set, or union?
19. Should the implementation surface *why* a test was selected (e.g., a `--why` flag that prints the import chain from changed module → test)? Useful for agents diagnosing "I expected this test to run and it didn't."
20. When `make test` falls back to the full suite due to a trigger file change, should it still print which trigger caused the fallback, or is the existing verbose log sufficient?

---

## Complexity Assessment

**high** — this is a cross-cutting change with multiple load-bearing sub-decisions (selection mechanism, LKG storage, LKG update mechanism, merge behaviour, CI integration, dynamic-import handling) that interact. It touches the `Makefile`, CI workflow(s), adds a new `scripts/` helper (or `tools/` module), introduces a new dependency (if Option B), adds new fallback triggers, and needs its own unit-test fixture (synthetic mini-repo). Several phases could be parallelized (graph builder vs LKG plumbing vs Makefile/CI wiring vs fallback-trigger detection), which is the classic "high" shape per the refine guidelines.

---

## External Research

- [pytest-testmon (PyPI)](https://pypi.org/project/pytest-testmon/) — dynamic coverage-based selection; strengths and weaknesses as a CI gating tool.
- [pytest-testmon hidden dependencies](https://www.testmon.org/blog/hidden-test-dependencies/) — vendor-authored discussion of when dynamic selection misses.
- [grimp (PyPI)](https://pypi.org/project/grimp/) and [Grimp usage docs](https://grimp.readthedocs.io/en/stable/usage.html) — static import-graph library; `find_downstream_modules` is the reverse-closure primitive.
- [Qik pygraph plugin](https://qik.build/en/0.2.2/plugin_pygraph/) — production example of grimp for monorepo test selection, including the `qikimports.py` shadow-imports workaround for dynamic edges.
- [importlab](https://github.com/google/importlab) — Google's import dependency library; considered but less actively maintained than grimp.
- [findimports](https://github.com/mgedmin/findimports) and [pyan3](https://pypi.org/project/pyan3/) — alternative static analyzers; not recommended for this use case.
- [HRT case study](https://www.python.org/success-stories/building-a-dependency-graph-of-our-python-codebase/) — large-codebase experience report; motivates the static-graph-first approach.

*Authored-by: egg*
