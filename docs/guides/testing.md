# Testing Guide

This guide is the canonical reference for running tests on egg, including
the changeset-aware narrowing model used by `make test`, the
last-known-good (LKG) sidecar that anchors the diff, the fallback
triggers that widen narrow runs to the full suite, and the
introspection tools you can lean on when a test you expected to run
does not get selected.

The goals are:

- **Correctness first.** Never skip a test that exercises a changed
  code path. Whenever static analysis cannot be trusted, the selector
  widens to the full suite with an explicit, named trigger written to
  stderr.
- **Minimal waste.** Avoid running tests that provably cannot be
  affected by the diff.
- **Full-suite escape hatch.** `make test-all` always runs everything,
  so CI and release flows are unaffected.
- **Fail-open.** A bug in the selector must never block iteration —
  any unhandled exception falls back to the full suite with a
  traceback on stderr and exit 0.

> **Quick mental model.** `make test` is the fast inner-loop default
> and only runs tests reachable (via static imports) from your diff.
> `make test-all` is the slow ground-truth target that runs the entire
> suite and is what CI uses. The two stay aligned through the LKG
> sidecar — `make test-all` writes it, `make test` reads it as the
> baseline for "what changed since green".

---

## 1. Overview — `make test`, `make test-all`, `make test-record-good`

| Target | What it does | When to use it |
|---|---|---|
| `make test` | Runs only the tests reachable from the diff between your branch and a known-green baseline. Falls back to the full suite if static analysis can't be trusted. Writes a per-invocation JSON record at `.egg-state/selection/<sha>.json`. **Does not** update LKG, even on green. | The default inner loop — what you run repeatedly while iterating on a change. |
| `make test-all` | Runs the full unit-test suite (`tests/`, `gateway/tests/`, `orchestrator/tests/`, `shared/tests/`) — the same command CI runs. On green, writes the current `HEAD` sha to the LKG sidecar so the next `make test` has a tight baseline. | Before pushing, when CI flagged something narrow runs missed, when you want to seed a fresh branch's LKG, or whenever you want full ground truth. |
| `make test-record-good` | Manual override: writes the current `HEAD` sha to the LKG sidecar without running pytest. | When you know the suite is green from a path that did not go through `make test-all` (e.g. you ran `pytest` directly with custom args, or a green CI run on this exact sha). |

`PYTEST_ARGS` is honored by all three: flags compose with narrowing
(`-k`, `-x`, `-v`, `-m foo` etc.), and explicit test paths bypass
narrowing entirely (the developer is asking for a specific selection
— `make test` respects that). See §5 for the bypass-vs-intersect
rules.

CI runs `make test-all`. The `--cov-fail-under=80` coverage gate is
unchanged by the narrow-default switch — narrowing is a **local
inner-loop optimization only.**

---

## 2. How changeset-aware selection works

The selector is `scripts/select_tests/`, a standalone CLI package
invoked by the `make test` recipe via `python scripts/select_tests/__main__.py`.
The algorithm is:

1. **Resolve the baseline.** In order:
   1. **Read-only role override.** If `EGG_AGENT_ROLE` starts with
      `reviewer_` or equals `refiner`, OR a `.egg-readonly` marker
      file is present in the repo root, the sidecar is bypassed
      entirely (never read, never written). See §6.
   2. **LKG sidecar.** Read
      `.egg-state/last-known-good/<branch>.sha`. The file is accepted
      only if its contents are 40 lowercase hex characters AND the
      sha is an ancestor of `HEAD` (`git merge-base --is-ancestor`).
   3. **Base branch.** Otherwise, compute `git merge-base HEAD
      origin/$BASE_BRANCH` (default `main`). If `origin/<base>` is
      missing or merge-base fails, the run falls back to the full
      suite with trigger `unresolvable baseline`.
2. **Compute the changed-files set.** Union of `git diff --name-only
   <baseline>...HEAD` and `git status --porcelain` (uncommitted
   work). An empty diff against a resolvable, current baseline
   short-circuits to "selected 0 tests" and skips pytest entirely
   — there is nothing reachable from a non-existent change. Empty
   diff combined with an unresolvable baseline or a stale LKG still
   widens to the full suite (those are real safety triggers, not
   changeset reasoning).
3. **Evaluate fallback triggers.** Any trigger from §4 short-circuits
   selection and runs the full suite, with the explicit trigger
   string printed to stderr.
4. **Build the grimp graph.** A module-level static import graph
   over a single `PACKAGES` constant covering every source package
   (`gateway`, `orchestrator`, `sandbox`, all `shared.egg_*`
   subpackages) AND the four test roots (`tests`, `gateway.tests`,
   `orchestrator.tests`, `shared.tests`). Test roots **must** be
   registered — grimp only reports edges between modules it has been
   told about, and an un-registered test root would make the
   downstream-mapping step return an empty set. The graph is cached
   on disk under `.egg-state/grimp-cache/` (gitignored) so successive
   sandbox invocations reuse a warm graph.
5. **Compute the reverse closure.** For each changed module `m`,
   two sources of upstream-test edges are combined:
   - **Grimp edges:** `graph.find_downstream_modules(m, as_package=...)`.
     `as_package=True` for `__init__.py` edits (package-level edit
     can affect anything downstream of the whole package);
     `as_package=False` for leaf-module edits.
   - **Bare-name AST edges:** An AST scan of every module
     registered in the grimp graph (i.e. every `.py` under the
     `PACKAGES` constant — the four source roots and four test
     roots; `scripts/`, `integration_tests/`, and other un-registered
     trees are not scanned) maps bare-name import targets (e.g.
     `from action_guards import …`) to fully-qualified grimp module
     ids, covering the test and production files that import via
     short names rather than fully-qualified package paths. Applies
     to `shared.*`, `orchestrator.*`, `sandbox.*`, AND `gateway.*` —
     gateway tests reach production via `gateway/tests/conftest.py`'s
     `importlib.spec_from_file_location` loader (which makes every
     gateway production module importable by bare name), so the AST
     resolver bridges those edges the same way it does for the
     sys.path-injected packages. See §7 for the full rationale.
6. **Map modules → test files.** Intersect the downstream set with
   the pre-collected set of every `test_*.py` / `*_test.py` file
   in the graph. The selector emits the resulting set of test file
   paths (one per line on stdout) — pytest's own collection handles
   intra-file filtering.
7. **Intersect with `PYTEST_ARGS`** (see §5 for the bypass rules).
8. **Run pytest.** The `make test` recipe pipes stdout into
   `pytest $(SELECTED) -v -m "not functional" $(PYTEST_ARGS)`. If
   the selector emits zero lines, the recipe skips the pytest
   invocation and prints `no tests selected`.

A green narrow run **does not** update the LKG sidecar — only
`make test-all` writes LKG, because only a full-suite green proves
the whole tree is in a runnable state. See §3 for the full update
contract.

### Granularity

- **Selection granularity is module-level**, matching grimp's graph
  resolution. The selector emits test **file paths**, not pytest
  test-id strings — pytest then collects normally inside each file.
- **The grimp graph is rebuilt every invocation** (with a disk cache
  for warm starts). There is no long-lived state to coordinate; each
  `make test` is self-contained.

---

## 3. Sidecar LKG (`.egg-state/last-known-good/<branch>.sha`)

| Field | Value |
|---|---|
| **Location** | `.egg-state/last-known-good/<branch>.sha` (one file per branch) |
| **Format** | Bare 40-character lowercase hex sha (no trailing newline required, no JSON wrapper) |
| **Tracked?** | No — `.egg-state/last-known-good/` is gitignored. Each branch carries its own LKG locally; nothing leaks into git history. |
| **Per-branch isolation** | The filename is the branch name (`git symbolic-ref --short HEAD`), so parallel branches never stomp each other. |
| **Write trigger** | A green `make test-all` (auto), or `make test-record-good` (manual override). |
| **Never written by** | A green `make test` — partial coverage does not prove the full suite is green (see §4 for why this matters). |
| **Detached HEAD** | No write. The selector emits `select-tests: detached HEAD; sidecar write skipped` on stderr and exits 0. |
| **Read trigger** | `make test` — and only when the role is not read-only (see §6). |
| **Validation on read** | 40-char lowercase hex regex AND `git merge-base --is-ancestor`. Anything else is treated as "no LKG, use base branch". |
| **Validation on write** | `--record-good` checks 40-char regex + `git cat-file -e` (object exists) + `git merge-base --is-ancestor`. A typo or stale value exits non-zero **without** writing — the only path in the selector that may exit non-zero. |
| **Merge behavior** | None. The sidecar is per-branch and gitignored, so a merge into `main` carries no LKG state. Branches cut from the new `main` start with no sidecar — their first `make test` falls back to the base-branch baseline, and a subsequent `make test-all` re-seeds. |
| **Concurrent writes** | Last-writer-wins via `tempfile + os.replace`. Both shas represent green states, so a stale value at worst gives the next narrow run a slightly older (still safe) baseline. |

### Why LKG matters

The LKG is a tighter baseline than the base branch. After you've
been iterating on a branch for several commits, the diff against
`origin/main` includes a lot of code that has already been
re-tested. The LKG points to the most recent commit on this branch
that fully passed, so the next `make test` only narrows on changes
**since that point** — minutes of test wall-clock saved on every
inner-loop tick.

If LKG is absent (fresh branch, `rm -rf .egg-state/last-known-good/`,
read-only role), narrowing still works using the base branch as the
baseline. There is no bootstrap ceremony — absence simply means
"use base branch".

---

## 4. Fallback triggers (when `make test` runs the full suite)

Any of the following short-circuits the selector and runs the full
suite, with the explicit trigger string written to stderr (e.g.
`select-tests: full suite 356 tests (trigger=Makefile changed)`):

| Trigger | Why |
|---|---|
| **`**/conftest.py` change** | conftest defines fixtures consumed across many tests; static imports do not capture fixture-injection edges. |
| **`Makefile` change** | The Makefile defines test invocation, env, and pytest args — any change can affect every target. |
| **`pyproject.toml` change** | Test config (`[tool.pytest.ini_options]`, markers, testpaths), dependencies, and tool settings live here. |
| **`uv.lock` change** | A dependency change can affect any module's behavior at runtime. |
| **`.python-version` change** | A Python interpreter change can flip behavior repo-wide. |
| **`.github/workflows/test.yml` change** | The CI definition itself — running narrow risks misrepresenting CI's posture. |
| **`shared/tests/**` change** | `shared/tests/conftest.py` is a universally-consumed cross-package fixture; v1 widens on any path under `shared/tests/` to avoid an allowlist that has not yet been audited. May narrow in a follow-up. |
| **Any non-`.py` change** | Schemas, fixture data, scripts, YAML, Markdown — none are reachable via the import graph. Conservative v1 default; an allowlist of known-safe paths can be added later. |
| **Source file missing from grimp graph** | At graph-construction time, the selector enumerates every non-test `.py` under `gateway/`, `shared/`, `orchestrator/`, `sandbox/` (excluding `__pycache__`, `.venv`, test directories) and asserts each path resolves to a node in `graph.modules`. Any miss (PACKAGES drift, encoding quirk, grimp cache bug) widens to the full suite with trigger `source file missing from graph: <path>`. |
| **Dynamic-import-touched module** | During graph construction, the selector regex-scans each module for `importlib.{import_module,util,machinery}`, `__import__`, `SourceFileLoader`, and entry-point plugin patterns. If any changed module is in (or reverse-reachable from) that set, narrow analysis is unsafe. |
| **Unresolvable changed path** | A changed path that cannot be mapped to an in-repo module (e.g. brand-new file not yet in grimp's graph, a `scripts/*.py` with no wheel binding). |
| **Unresolvable baseline** | LKG missing AND `origin/<base>` missing or `merge-base` failing. |
| **LKG not ancestor of HEAD** | The recorded LKG sha is not reachable from `HEAD` (force-push, reset, history rewrite). |

When a fallback fires, the stderr line uses the **explicit trigger
name** (e.g. `Makefile changed`, `dynamic-import reachability`),
not generic wording — the trigger reason is the single most useful
diagnostic.

---

## 5. Introspection — `--why`, stderr, and the JSON log

### Stderr one-liner

Every `make test` invocation logs a single human-readable line to
stderr:

```
select-tests: narrowed 47/356 tests in 0.18s (baseline=abc1234, trigger=diff)
```

or, when a fallback fires:

```
select-tests: full suite 356 tests (trigger=Makefile changed)
```

The trigger string is always explicit (the table in §4 lists every
value).

### JSON log at `.egg-state/selection/<head_sha>.json`

Every invocation also persists a structured record. The schema:

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Currently `1`. Bumped to `2` only on a backward-incompatible change. |
| `head` | string | `HEAD` sha at invocation time. |
| `baseline` | object | `{ "sha": "<40-hex>", "source": "LKG" \| "BASE_BRANCH" }`. |
| `branch` | string | `git symbolic-ref --short HEAD`, or `"DETACHED"` if detached. |
| `mode` | string | `"narrow"`, `"full_suite"`, or `"bypass"` (PYTEST_ARGS path-bypass). |
| `trigger` | string | Explicit trigger enum value, or `"none"` for a clean narrow. |
| `selected_count` | integer | Number of test files emitted. |
| `total_count` | integer | Total number of test files in the four roots. |
| `compute_ms` | integer | Selector wall-clock in milliseconds. |
| `pytest_ms` | integer | Pytest wall-clock in milliseconds; written by the Makefile after pytest returns (`select_tests/__main__.py --patch-selection-json --head <sha> --pytest-ms <int>`). |
| `timestamp` | string | ISO-8601 UTC timestamp. |
| `changed_files` | list[string] | Paths from `git diff` + `git status`. |
| `changed_modules` | list[string] | Resolved module paths for the changed files. |
| `dynamic_import_seeds_hit` | list[string] | Module paths flagged as containing dynamic-import patterns and reachable from the changed set. |

The file is gitignored. It accumulates over time — see §9.

### `--why <test>` — "why did this test (not) run?"

```bash
.venv/bin/python scripts/select_tests/__main__.py --why tests/action/test_foo.py
```

Prints the shortest import chain from any changed module to the
named test, one module per line with `via` arrows. Three modes:

- **Test is in the selected set.** Prints the chain that selected it.
- **Test is not in the selected set, but is reachable.** Prints
  `test is not in the selected set; closest reachable chain from
  changed modules follows` and then the chain.
- **No path exists.** Prints `no path exists`.

This is the first-line tool when you expected a test to run but it
didn't — it tells you exactly which import edge (or missing edge)
explains the selection.

---

## 6. Role-aware behavior and the fail-open contract

### Read-only roles

The selector treats a role as **read-only** if either of the
following is true:

1. `EGG_AGENT_ROLE` starts with `reviewer_` or equals `refiner`, OR
2. A `.egg-readonly` marker file is present in the repo root.

When read-only:

- The sidecar is **bypassed entirely** — never read, never written,
  even if a sidecar file happens to exist on disk.
- `--record-good` is a no-op (with a stderr notice).
- Baseline resolution proceeds directly to the base branch.

This prevents cross-sandbox LKG contamination in roles that do not
themselves run `make test-all`. A reviewer agent reading another
agent's sidecar (which may reflect a different sandbox's state)
would otherwise produce an inconsistent narrowing decision.

When `EGG_AGENT_ROLE` is unset (local developer, CI, generic
automation), the default is **non-read-only** — LKG is preferred when
present. No env-var ceremony is required to get the fast path.

### Fail-open exit contract

The selector's exit codes are deliberate:

- **`make test` (default), `--full-suite`, and `--why` modes always
  exit 0**, except for argparse syntax errors. The `main()` function
  wraps its body in `try/except BaseException`; on any unhandled
  exception, it prints the traceback to stderr, emits the full
  test-root list on stdout (equivalent to `--full-suite`), and
  exits 0. A bug in the selector must NEVER block agent or
  developer iteration — correctness is preserved by falling back to
  the full suite, and the operator gets the traceback to file a
  follow-up.
- **Fallback to full suite is also exit 0.** A trigger firing is
  the expected path, not an error.
- **`--record-good` is the only path that may exit non-zero**, and
  only on validation failure (typo, non-existent sha, non-ancestor
  sha). A write-side operation must refuse to silently poison the
  sidecar.

---

## 7. Known limits

Static reverse import graphs are powerful, but they cannot see:

- **Subprocess crossings.** A test that shells out to a binary
  (e.g. `subprocess.run(["python", "scripts/foo.py"])`) does not
  have an import edge to that binary. Tests that exercise CLI
  surfaces by subprocess will be missed by narrowing alone — this
  is mitigated in practice because such tests almost always also
  touch a shared test fixture or a subprocess-helper module that
  grimp does see. Run `make test-all` before push for ground truth.
- **Data-file loads.** A test that does
  `Path("fixtures/foo.json").read_text()` has no import edge to
  the data file. Non-`.py` changes fall back to the full suite as
  a blunt-but-stable mitigation.
- **Entry-point plugin registrations.** Plugins discovered via
  `importlib.metadata.entry_points()` (e.g. some pytest plugins)
  do not have static import edges from their consumers. The
  dynamic-import scan picks up the consumer side; missed cases
  surface when CI runs `make test-all`.
- **Bare-name imports across packages.** `shared.*`, `orchestrator.*`,
  `sandbox.*`, and `gateway.*` modules are almost universally
  imported by bare name throughout the codebase (e.g. `from
  egg_logging.signatures import …` rather than `from
  shared.egg_logging.signatures import …`, or `from policy import …`
  rather than `from gateway.policy import …`). Grimp registers
  modules under fully-qualified names, so a plain grimp traversal
  misses those edges. **Mitigation:** the bare-name AST resolver
  (step 5) AST-scans every `.py` and maps bare-name targets back to
  fully-qualified ids, making these edges visible to narrowing. The
  resolver covers `gateway.*` even though `gateway/` is not on
  sys.path during graph build — `gateway/tests/conftest.py`'s
  `importlib.spec_from_file_location` loader makes every gateway
  production module bare-name-importable at test time, and the AST
  resolver only inspects source so the runtime importlib pattern
  doesn't affect its view.

  Note: the AST resolver only delivers narrowing if the
  dynamic-import fallback (R6, "dynamic-import reachability") does
  not fire in its place. Because `_scan_dynamic_imports` regex-scans
  every module's source for `__import__`, `importlib.util.*`,
  `SourceFileLoader`, etc., any gateway module that legitimately
  uses those primitives becomes a seed — and R6 widens for every
  module reachable through that seed's `find_upstream_modules`
  closure. To keep `gateway/*.py` edits narrowable, the importlib
  bootstrap lives in `gateway/_module_loader.py`, a leaf module that
  imports only stdlib; its upstream closure is empty, so R6 only
  fires when the bootstrap itself is edited (which is the right
  call). Keep this invariant in mind when adding any further
  dynamic-import primitives anywhere in `gateway/` — adding them to
  a module that is upstream of much of the gateway package would
  silently re-disable narrowing for everything that flows through
  it.

These limits are the reason the fallback-trigger list in §4 is as
broad as it is — narrowing trades coverage for speed, and any
hint that static analysis cannot be trusted widens to the safe
default.

---

## 8. CI

CI runs `make test-all`, not `make test`. This preserves the
existing `--cov-fail-under=80` gate unchanged: the coverage gate
requires the full suite, and narrowing in CI was explicitly out of
scope. Specifically, `.github/workflows/test.yml`'s unit-job step
is:

```yaml
- name: Run unit tests
  run: |
    make test-all PYTEST_ARGS="--cov=gateway --cov=shared --cov=sandbox --cov-report=term-missing --cov-fail-under=80"
```

`fetch-depth` on the workflow is unchanged — `make test-all` does
not need a baseline-ref fetch (no narrowing path). `make test`
remains the agent/developer fast path; only local inner loops see
the speedup.

---

## 9. Housekeeping

Two directories accumulate on disk and are gitignored:

- `.egg-state/selection/` — one JSON record per `make test`
  invocation, keyed by `HEAD` sha.
- `.egg-state/last-known-good/` — at most one `<branch>.sha` file
  per branch.

**No automatic pruning is performed.** Per-invocation pruning
would add filesystem churn and a subtle GC dependency for marginal
disk savings. If either directory grows uncomfortably:

```bash
rm -rf .egg-state/selection/
rm -rf .egg-state/last-known-good/
```

is the documented recovery. Both are safe:

- The next `make test` will fall back to the base-branch baseline
  (no sidecar present) and run normally.
- The next `make test-all` will re-seed the LKG sidecar.

Also gitignored: `.egg-state/grimp-cache/` (grimp's disk cache for
warm-graph performance). Same `rm -rf` recovery; the next run
rebuilds it.

`.egg-state/` as a whole is **not** gitignored — drafts, contracts,
reviews, brc-history, checks, and agent-outputs all live there and
are tracked. The three subdirectories above are excluded, plus
specific per-machine runtime telemetry files under `.egg-state/oversight/`
(`agent-timing.json`, `*-oversight.jsonl`, `filed-issues.jsonl`,
`*-health-summary.md`, their lockfiles, and `.tmp` leftovers from
atomic writes — see `.gitignore` for the full list). These files
would otherwise appear in `git status` and trip the selector's
non-`.py`-change fallback. Tracked test fixtures in that directory
(e.g. `test-00{2,3,4}-oversight.jsonl`) are unaffected.

---

## 10. Troubleshooting

**"My test did not run."**

1. Look at the stderr one-liner. The trigger string tells you
   whether you got a narrow run or a full-suite fallback. If the
   line says `narrowed N/356 tests`, the selector decided your test
   was not in the closure.
2. Run `--why` against the test in question:
   ```bash
   .venv/bin/python scripts/select_tests/__main__.py --why tests/path/to/test_file.py
   ```
   - Chain printed → the test was selected; check pytest's own
     filtering (e.g. `-k`, `-m`).
   - "Closest reachable chain" → there is an import edge but it did
     not include any of your changed modules. You may want
     `make test-all` to be safe.
   - "No path exists" → the test is genuinely unreachable from your
     diff under static analysis. If you believe a runtime edge
     exists (subprocess, dynamic import, fixture injection),
     `make test-all` is the right answer.
3. As a sanity check, run the full suite:
   ```bash
   make test-all
   ```
   If that test runs and passes/fails as expected, the gap is in
   the static graph — file an issue with the test path and the
   `--why` output, and run `make test-all` for now.

**"`make test` keeps falling back to full suite."**

The stderr trigger string is the diagnostic. Common cases:

- `Makefile changed` / `pyproject.toml changed` / `uv.lock changed`
  — expected; commit and move on.
- `LKG not ancestor of HEAD` — you rebased / force-pushed / reset
  past the recorded LKG. Run `make test-all` once to re-seed the
  sidecar.
- `unresolvable baseline` — `origin/main` is missing or
  inaccessible. Check `git remote -v` and `git fetch origin`.

**"I want to force the full suite for a single run."**

Use `make test-all`. Or, if you want to use a specific subset:
```bash
PYTEST_ARGS="path/to/specific/test.py" make test
```
An explicit path in `PYTEST_ARGS` bypasses narrowing — the
developer is asking for that specific selection.

**"I changed nothing and `make test` did nothing."**

That is the correct behavior. With no changes against a resolvable
baseline, the selector emits `selected 0 tests (skipping pytest)` and
the Makefile prints `no tests selected` and exits 0. To run the full
suite anyway, use `make test-all`.

---

## See also

- [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — repo-level testing
  conventions and the testing section index.
- [`pyproject.toml`](../../pyproject.toml) — pytest markers, test
  paths, and the dev-dependency pin for grimp.
- [`Makefile`](../../Makefile) — the canonical test-target
  definitions (`test`, `test-all`, `test-record-good`).
- [`scripts/select_tests/`](../../scripts/select_tests/__init__.py) — the
  selector package (entry point at `scripts/select_tests/__main__.py`),
  with `--why` and `--record-good` available as CLI flags.
