"""Selection records, fallback-trigger evaluator, ``--why`` introspection,
PYTEST_ARGS env split, the narrow-or-fallback orchestrator, argparse
wiring, and the fail-open ``main`` wrapper.

The functions here form the surface the entry-point shim
(``scripts/select_tests/__main__.py``) drives.  Internal callees inside
this module reference each other by bare name so a test that
``monkeypatch.setattr(selector._cli, "_main_inner", ...)`` reaches every
caller; ``selector._main_inner`` (re-exported from the package barrel)
remains the canonical patch target for the existing test suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import _io
from ._constants import (
    FALLBACK_PATH_PATTERNS,
    SELECTION_LOG_DIR,
    SELECTION_SCHEMA_VERSION,
    TEST_ROOT_DIRS,
)
from ._graph import (
    GraphBundle,
    _walk_upstream_combined,
    build_graph,
    is_dynamic_import_touched,
    map_modules_to_test_files,
    pytest_args_have_explicit_path,
    reverse_closure,
)
from ._io import (
    RecordGoodValidationError,
    _atomic_write_text,
    _git_repo_root,
    _is_valid_sha,
    _log,
    changed_files,
    lkg_is_stale,
    path_to_module,
    record_good,
    resolve_baseline,
)

# ----------------------------------------------------------------------
# Selection JSON envelope (TASK-2-4b)
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _short_sha(sha: str | None) -> str:
    if not sha or not _is_valid_sha(sha):
        return "unknown"
    return sha[:7]


def write_selection_record(
    *,
    head: str,
    baseline_sha: str | None,
    baseline_source: str,
    branch: str | None,
    mode: str,
    trigger: str,
    selected_count: int,
    total_count: int,
    compute_ms: int,
    changed_files_list: list[str],
    changed_modules_list: list[str],
    dynamic_import_seeds_hit: list[str],
    repo_root: Path,
) -> Path:
    """Write the per-invocation JSON record.  Returns the written path."""
    record: dict[str, Any] = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "head": head,
        "baseline": {"sha": baseline_sha, "source": baseline_source},
        "branch": branch,
        "mode": mode,
        "trigger": trigger,
        "selected_count": selected_count,
        "total_count": total_count,
        "compute_ms": compute_ms,
        "pytest_ms": None,  # Populated by `--patch-selection-json` later.
        "timestamp": _now_iso(),
        "changed_files": list(changed_files_list),
        "changed_modules": list(changed_modules_list),
        "dynamic_import_seeds_hit": list(dynamic_import_seeds_hit),
    }
    log_dir = repo_root / SELECTION_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"{head}.json"
    _atomic_write_text(out_path, json.dumps(record, indent=2) + "\n")
    return out_path


def patch_selection_record(head: str, pytest_ms: int, repo_root: Path | None = None) -> int:
    """Append `pytest_ms` to the existing
    `.egg-state/selection/<head>.json` record.

    Called by the Makefile `test` wrapper after pytest returns.
    Returns 0 always — a missing record is logged to stderr but is not
    a failure (the wrapper should not abort on a missing log file).
    """
    root = repo_root if repo_root is not None else _git_repo_root()
    path = root / SELECTION_LOG_DIR / f"{head}.json"
    if not path.exists():
        _log(f"select-tests: no selection record at {path}; skipping pytest_ms patch")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"select-tests: could not parse {path}: {e}; skipping pytest_ms patch")
        return 0
    data["pytest_ms"] = pytest_ms
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return 0


# ----------------------------------------------------------------------
# Fallback-trigger evaluator (TASK-2-3)
# ----------------------------------------------------------------------


def _fnmatch(path: str, pattern: str) -> bool:
    """Lightweight ** glob matcher for the FALLBACK_PATH_PATTERNS table."""
    import fnmatch

    return fnmatch.fnmatch(path, pattern)


def evaluate_fallback_triggers(
    *,
    paths: list[str],
    bundle: GraphBundle | None,
    baseline_source: str,
    lkg_was_stale: bool,
) -> str | None:
    """Return an explicit trigger string when the changeset forces a
    full-suite fallback, or None when the narrow path is safe.

    Order matters: the first matching trigger wins so the stderr line
    names the most-specific reason rather than a generic catch-all.
    """
    # 1. Baseline resolution failed before we even got here.
    if baseline_source == "UNRESOLVABLE":
        return "unresolvable baseline"

    # 2. LKG sidecar exists but is no longer an ancestor of HEAD
    #    (force-push / reset).
    if lkg_was_stale:
        return "LKG not ancestor of HEAD"

    # 3. Path-pattern triggers.  Priority order: most-specific first
    #    so the stderr line names the most-informative reason
    #    rather than a generic "non-.py change" catch-all when a
    #    Makefile / pyproject.toml is in the same diff.

    # 3a. conftest at any level.  Match the literal filename or `/conftest.py`
    # at a path boundary so files like `myconftest.py` don't false-fire.
    for raw_path in paths:
        if raw_path == "conftest.py" or raw_path.endswith("/conftest.py"):
            return "conftest changed"

    # 3b. shared/tests/** (Q2 — fixture edits widen).
    for raw_path in paths:
        if raw_path.startswith("shared/tests/"):
            return "shared/tests/ changed"

    # 3c. Static path triggers (Makefile, pyproject.toml, uv.lock, etc.).
    for pattern, trigger_string in FALLBACK_PATH_PATTERNS:
        for raw_path in paths:
            if _fnmatch(raw_path, pattern):
                return trigger_string

    # 3d. Gateway importlib-test-loader mapping (R1).  Hits any
    # `gateway/<file>.py` that is NOT under `gateway/tests/`.  Checked
    # BEFORE the generic non-.py rule so a mixed diff names the
    # specific blind spot.
    #
    # Layout assumption (locked by current repo as of this PR): gateway/
    # production source is FLAT — every .py production file is directly
    # under `gateway/<file>.py`, no subdirectories (verified with
    # `ls gateway/*.py`).  The TASK-2-3 spec phrases the rule as "any
    # changed path matching `gateway/*.py`", which is what the
    # `"/" not in raw_path[len("gateway/") :]` guard implements.  If
    # gateway production code is ever reorganised into subdirectories
    # (e.g., `gateway/api/foo.py`), this check would NOT widen on those
    # subdirectory edits — extend the guard to drop the `"/" not in`
    # clause at that point.  TASK-5-2's parametrized cases cover the
    # current flat layout and would catch a change in semantics.
    for raw_path in paths:
        if (
            raw_path.startswith("gateway/")
            and not raw_path.startswith("gateway/tests/")
            and "/" not in raw_path[len("gateway/") :]
            and raw_path.endswith(".py")
        ):
            return "gateway source change (importlib test-loader)"

    # 3e. Non-.py changes (decision-5) — the catch-all when none of
    # the more-specific path triggers fired.
    for raw_path in paths:
        if not raw_path.endswith(".py"):
            return "non-.py change"

    # 4. Source-file staleness guard (R2).  Without a graph we cannot
    #    evaluate; defer to the unresolvable-module check below.
    if bundle is not None and bundle.missing_source_paths:
        return f"source file missing from graph: {bundle.missing_source_paths[0]}"

    # 5. Unresolvable module path (path doesn't map to a graph node).
    if bundle is not None:
        for raw_path in paths:
            module = path_to_module(raw_path)
            if module is None:
                # Non-.py path — already handled above.
                continue
            if module not in bundle.all_modules:
                return f"unresolvable module path: {raw_path}"

    # 6. Dynamic-import reachability (decision-10).
    if bundle is not None and bundle.dynamic_import_modules:
        resolved_modules: list[str] = [
            m for m in (path_to_module(p) for p in paths) if m is not None
        ]
        if is_dynamic_import_touched(bundle, resolved_modules):
            return "dynamic-import reachability"

    return None


# ----------------------------------------------------------------------
# `--why` introspection (TASK-2-4b)
# ----------------------------------------------------------------------


def _format_chain(chain: list[str]) -> str:
    return "\n".join(chain)


def explain_why(test_path: str, repo_root: Path | None = None) -> int:
    """Implement `--why <test_path>`.

    Loads the grimp graph, computes the selected set against the
    current diff, then either prints the import chain from any changed
    module to the test, or explains why the test is not selected.
    Always exits 0.
    """
    root = repo_root if repo_root is not None else _git_repo_root()
    test_module = path_to_module(test_path)
    if test_module is None:
        _log(f"select-tests: --why: cannot resolve path to module: {test_path}")
        return 0

    try:
        bundle = build_graph(root)
    except Exception as e:  # noqa: BLE001 — fail-open
        _log(f"select-tests: --why: graph build failed: {e}")
        return 0

    if test_module not in bundle.all_modules:
        _log(
            f"select-tests: --why: {test_module} is not a node in the graph; "
            "narrowing decisions cannot reach it"
        )
        return 0

    baseline_sha, baseline_source, _branch = resolve_baseline(repo_root=root)
    if baseline_sha is None:
        _log("select-tests: --why: cannot resolve baseline; full suite would run")
        return 0

    diff = changed_files(baseline_sha, repo_root=root)
    module_path_pairs: list[tuple[str, str]] = []
    for path in diff:
        module = path_to_module(path)
        if module is not None and module in bundle.all_modules:
            module_path_pairs.append((module, path))

    if not module_path_pairs:
        _log("select-tests: --why: no changed modules resolved")
        return 0

    changed_modules_list = [m for m, _ in module_path_pairs]
    closure = reverse_closure(bundle, module_path_pairs)
    is_selected = test_module in closure

    # Try grimp's shortest-chain primitive.
    best_chain: list[str] | None = None
    for src in changed_modules_list:
        try:
            chain = bundle.graph.find_shortest_chain(src, test_module)
        except Exception:  # noqa: BLE001 — fail-open
            chain = None
        if chain:
            chain_list = list(chain)
            if best_chain is None or len(chain_list) < len(best_chain):
                best_chain = chain_list

    if best_chain is None:
        _log("select-tests: --why: no path exists from any changed module")
        return 0

    pretty_chain = "\n".join(f"  → {m}" for m in best_chain)
    if is_selected:
        _log(f"select-tests: --why: {test_module} is in the selected set:")
    else:
        _log(
            f"select-tests: --why: {test_module} is NOT in the selected set; "
            "closest reachable chain follows:"
        )
    print(pretty_chain)
    return 0


# ----------------------------------------------------------------------
# Main entry — orchestrates the narrow / fallback flow (TASK-2-1..2-4b)
# ----------------------------------------------------------------------


def emit_full_suite(reason: str | None = None) -> int:
    """Emit the full test-root list on stdout.  Used by --full-suite,
    fallback paths, and the fail-open wrapper."""
    for d in TEST_ROOT_DIRS:
        print(d)
    if reason is not None:
        _log(reason)
    return 0


def _split_pytest_args_env() -> list[str]:
    """Split PYTEST_ARGS_RAW env var into shell tokens.

    The Makefile `test` recipe sets `PYTEST_ARGS_RAW="$(PYTEST_ARGS)"`
    so the selector can run the path-vs-flag classifier (R5 / Q5).
    Returns [] when the env var is unset or empty.  Falls open on
    shlex parse errors (treat as "no path arg" rather than a hard
    failure).
    """
    import shlex

    raw = os.environ.get("PYTEST_ARGS_RAW", "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=True)
    except ValueError:
        # Unbalanced quotes etc. — treat as "no path", caller will
        # narrow normally and pytest's own arg parser will surface
        # the real syntax error to the user.
        return []


def _run_narrow_or_fallback(repo_root: Path) -> int:
    """The full default-mode path, factored out so main() can call it
    from inside the blanket try/except wrapper without the wrapper
    growing arms-and-legs."""
    t0 = time.monotonic()

    baseline_sha, baseline_source, branch = resolve_baseline(repo_root=repo_root)
    lkg_was_stale = lkg_is_stale(repo_root=repo_root)

    # Resolve HEAD for the selection-record path.  HEAD is always
    # resolvable (sandboxes always have a HEAD) — if it isn't, fall
    # open to full suite.  Call through ``_io`` (rather than via a
    # local-imported binding) so ``monkeypatch.setattr(selector._io,
    # "_run_git", ...)`` reaches this caller too — see the fixture
    # docstring in ``tests/tools/_select_tests_helpers.py``.
    rc, head_stdout, _ = _io._run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc != 0 or not _is_valid_sha(head_stdout.strip()):
        # Best-effort selection record so the telemetry trail still
        # captures the failure.  Fall-open exit 0.
        emit_full_suite("select-tests: full suite (trigger=cannot resolve HEAD)")
        try:
            write_selection_record(
                head="0" * 40,
                baseline_sha=baseline_sha,
                baseline_source=baseline_source,
                branch=branch,
                mode="full_suite",
                trigger="cannot resolve HEAD",
                selected_count=len(TEST_ROOT_DIRS),
                total_count=len(TEST_ROOT_DIRS),
                compute_ms=int((time.monotonic() - t0) * 1000),
                changed_files_list=[],
                changed_modules_list=[],
                dynamic_import_seeds_hit=[],
                repo_root=repo_root,
            )
        except OSError:
            pass
        return 0
    head_sha = head_stdout.strip()

    # Compute the diff against the baseline (or empty list when
    # baseline_source == "UNRESOLVABLE").
    if baseline_sha is not None:
        diff = changed_files(baseline_sha, repo_root=repo_root)
    else:
        diff = []

    # PYTEST_ARGS bypass classifier (R5 / Q5) — runs BEFORE both the
    # empty-diff short-circuit and the fallback evaluator so an
    # explicit-path PYTEST_ARGS short-circuits everything (the
    # developer is steering pytest manually and doesn't want
    # narrowing).  In particular, on a clean tree (`make test
    # PYTEST_ARGS=tests/foo/test_bar.py`) the empty-diff branch must
    # NOT fire — the user wants pytest to run that path.  Flag values
    # like `--hypothesis-seed=gateway/tests/x.py` correctly classify
    # as intersect (narrow) — see pytest_args_have_explicit_path
    # comments and TASK-5-4 for the regression cases.
    pytest_args_tokens = _split_pytest_args_env()
    bypass_narrowing = pytest_args_have_explicit_path(pytest_args_tokens, repo_root)

    if bypass_narrowing:
        # Bypass mode — emit nothing on stdout, let the Makefile fall
        # through to the user's explicit PYTEST_ARGS path.  Record
        # the decision so telemetry catches the override.  We
        # deliberately skip `build_graph` here because the bypass
        # ignores the closure entirely; total_count falls back to the
        # cheap TEST_ROOT_DIRS count.
        compute_ms = int((time.monotonic() - t0) * 1000)
        _log(
            "select-tests: bypass mode — PYTEST_ARGS contains an explicit "
            "test path; narrowing skipped, pytest runs only the user-"
            "supplied path(s)"
        )
        try:
            write_selection_record(
                head=head_sha,
                baseline_sha=baseline_sha,
                baseline_source=baseline_source,
                branch=branch,
                mode="bypass",
                trigger="PYTEST_ARGS explicit path",
                selected_count=0,
                total_count=len(TEST_ROOT_DIRS),
                compute_ms=compute_ms,
                changed_files_list=diff,
                changed_modules_list=[],
                dynamic_import_seeds_hit=[],
                repo_root=repo_root,
            )
        except OSError:
            pass
        return 0

    # Empty diff against a resolvable, current baseline = nothing to
    # test.  Skip pytest entirely — the Makefile prints "no tests
    # selected" when the selector emits zero stdout lines.
    # Unresolvable-baseline / stale-LKG still widen for safety; those
    # are evaluated by the trigger evaluator below.
    if not diff and baseline_source != "UNRESOLVABLE" and not lkg_was_stale:
        compute_ms = int((time.monotonic() - t0) * 1000)
        short_baseline = _short_sha(baseline_sha) if baseline_sha else "?"
        _log(
            f"select-tests: no changes since baseline {short_baseline}; "
            f"selected 0 tests (skipping pytest)"
        )
        try:
            write_selection_record(
                head=head_sha,
                baseline_sha=baseline_sha,
                baseline_source=baseline_source,
                branch=branch,
                mode="narrow",
                trigger="none",
                selected_count=0,
                total_count=0,
                compute_ms=compute_ms,
                changed_files_list=[],
                changed_modules_list=[],
                dynamic_import_seeds_hit=[],
                repo_root=repo_root,
            )
        except OSError:
            pass
        return 0

    # Build the grimp graph.  This is the expensive call; it's also the
    # one most likely to fail in a sandbox without grimp installed.  The
    # outer fail-open wrapper catches the ImportError.
    bundle: GraphBundle | None = None
    try:
        bundle = build_graph(repo_root)
    except Exception as e:  # noqa: BLE001 — fall through to fallback eval
        _log(f"select-tests: graph build failed: {type(e).__name__}: {e}")
        # Without a graph we still want to evaluate the
        # path-pattern triggers — but if none fire, we must widen to
        # full suite anyway because we cannot compute the closure.

    # Pre-compute changed-modules-list + seeds-hit ONCE so both the
    # narrow and full-suite branches can write a uniformly-detailed
    # JSON record (reviewer non-blocking #2).
    module_path_pairs: list[tuple[str, str]] = []
    if bundle is not None:
        for path in diff:
            module = path_to_module(path)
            if module is not None and module in bundle.all_modules:
                module_path_pairs.append((module, path))
    changed_modules_list = [m for m, _ in module_path_pairs]
    seeds_hit: list[str] = []
    if bundle is not None:
        seeds_hit = sorted(m for m in changed_modules_list if m in bundle.dynamic_import_modules)

    # Fallback triggers — first match wins.
    trigger = evaluate_fallback_triggers(
        paths=diff,
        bundle=bundle,
        baseline_source=baseline_source,
        lkg_was_stale=lkg_was_stale,
    )

    # If we have no graph and no explicit trigger, force a full suite
    # with an explicit reason (the graph is necessary for the closure
    # step; missing graph = missing analysis).
    if bundle is None and trigger is None:
        trigger = "graph unavailable"

    # Last-line-of-defense check: when narrowing IS possible (bundle
    # exists, no trigger fired), but neither grimp's import graph nor
    # the bare-name AST resolver can reach any test module from a
    # non-test changed module — widen to full suite.  Such a module is
    # a true blind spot: nothing in the graph claims to import it, so
    # narrowing risks skipping a real consumer that uses a pattern
    # neither analysis can see (e.g., subprocess invocation, runtime
    # plugin discovery, or a bare-name import we haven't taught the
    # resolver about).
    if bundle is not None and trigger is None and module_path_pairs:
        zero_downstream_offenders: list[str] = []
        for module, _path in module_path_pairs:
            if module in bundle.all_test_modules:
                continue  # editing a test pulls only itself; that's fine
            reachable = _walk_upstream_combined(bundle, [module])
            if not (reachable & bundle.all_test_modules):
                zero_downstream_offenders.append(module)
        if zero_downstream_offenders:
            trigger = f"no downstream tests for changed module: {zero_downstream_offenders[0]}"

    compute_ms = int((time.monotonic() - t0) * 1000)

    # Total test count (used for both narrow and full-suite log lines).
    total_count = len(bundle.all_test_modules) if bundle is not None else len(TEST_ROOT_DIRS)

    if trigger is not None:
        # Full-suite fallback path.  Write the SAME detailed record
        # (changed_modules + seeds_hit) the narrow path writes — the
        # operator wants the "why" detail when a fallback fires.
        emit_full_suite()
        _log(f"select-tests: full suite {total_count} tests (trigger={trigger})")
        write_selection_record(
            head=head_sha,
            baseline_sha=baseline_sha,
            baseline_source=baseline_source,
            branch=branch,
            mode="full_suite",
            trigger=trigger,
            selected_count=total_count,
            total_count=total_count,
            compute_ms=compute_ms,
            changed_files_list=diff,
            changed_modules_list=changed_modules_list,
            dynamic_import_seeds_hit=seeds_hit,
            repo_root=repo_root,
        )
        return 0

    # ---- Narrow path ----
    assert bundle is not None  # narrowed above

    closure = reverse_closure(bundle, module_path_pairs)
    test_files = map_modules_to_test_files(bundle, closure, repo_root)
    selected_count = len(test_files)

    for path in test_files:
        print(path)

    short_baseline = _short_sha(baseline_sha)
    elapsed_s = compute_ms / 1000.0
    _log(
        f"select-tests: narrowed {selected_count}/{total_count} tests "
        f"in {elapsed_s:.2f}s (baseline={short_baseline}, trigger=diff)"
    )

    write_selection_record(
        head=head_sha,
        baseline_sha=baseline_sha,
        baseline_source=baseline_source,
        branch=branch,
        mode="narrow",
        trigger="none",
        selected_count=selected_count,
        total_count=total_count,
        compute_ms=compute_ms,
        changed_files_list=diff,
        changed_modules_list=changed_modules_list,
        dynamic_import_seeds_hit=seeds_hit,
        repo_root=repo_root,
    )
    return 0


# ----------------------------------------------------------------------
# CLI entry
# ----------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/select_tests/__main__.py",
        description="Changeset-aware test selection (issue #1973)",
    )
    parser.add_argument(
        "--why",
        metavar="TEST_PATH",
        help="Print the import chain from any changed module to the named test.  Always exits 0.",
    )
    parser.add_argument(
        "--record-good",
        action="store_true",
        help="Atomically write the LKG sidecar.  Validates the sha and "
        "exits non-zero on validation failure.",
    )
    parser.add_argument(
        "--sha",
        metavar="SHA",
        help="40-char sha to record with --record-good (default HEAD).",
    )
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help="Emit all test directories on stdout (used by `make test-all`).",
    )
    parser.add_argument(
        "--patch-selection-json",
        action="store_true",
        help="Append --pytest-ms to the existing .egg-state/selection/<head>.json record.",
    )
    parser.add_argument(
        "--head",
        metavar="SHA",
        help="HEAD sha for --patch-selection-json.",
    )
    parser.add_argument(
        "--pytest-ms",
        metavar="MS",
        type=int,
        help="Pytest wall-clock duration in ms for --patch-selection-json.",
    )
    return parser


def _main_inner(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    repo_root = _git_repo_root()

    # --patch-selection-json is the cheapest mode; handle first.
    if args.patch_selection_json:
        if not args.head or args.pytest_ms is None:
            print(
                "select-tests: --patch-selection-json requires --head and --pytest-ms",
                file=sys.stderr,
            )
            return 0  # fail-open
        return patch_selection_record(args.head, args.pytest_ms, repo_root=repo_root)

    if args.record_good:
        try:
            return record_good(args.sha, repo_root=repo_root)
        except RecordGoodValidationError as e:
            _log(f"select-tests: --record-good validation failed: {e}")
            return 1

    if args.full_suite:
        return emit_full_suite()

    if args.why is not None:
        return explain_why(args.why, repo_root=repo_root)

    # Default mode — narrow or fallback.
    return _run_narrow_or_fallback(repo_root)


def _strip_pythonpath_from_sys_path() -> None:
    """Pop ``PYTHONPATH`` from the env AND remove its entries from
    ``sys.path``.

    A ``PYTHONPATH=shared:gateway:orchestrator`` (the value the Makefile
    exports for pytest) leaves ``shared/`` at the head of ``sys.path``,
    which causes grimp's ``build_graph`` to abort with
    ``NotATopLevelModule: shared.egg_agent`` — ``egg_agent`` becomes
    reachable as a bare top-level package via ``shared/`` AND as
    ``shared.egg_agent`` via the namespace package, and grimp refuses
    the ambiguity.  Python has already resolved the env var to absolute
    paths and prepended them to sys.path at interpreter startup, so
    just popping the env var is not enough — we also strip the
    resolved entries from sys.path.
    """
    pythonpath = os.environ.pop("PYTHONPATH", None)
    if not pythonpath:
        return
    for entry in pythonpath.split(os.pathsep):
        if not entry:
            continue
        # Python prepends the resolved absolute path; try both forms.
        candidates = {entry}
        try:
            candidates.add(str(Path(entry).resolve()))
        except OSError:
            pass
        for candidate in candidates:
            while candidate in sys.path:
                sys.path.remove(candidate)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with the **fail-open** wrapper.

    Any unhandled exception inside _main_inner() is caught here, the
    traceback is printed to stderr, the full test-root list is emitted
    on stdout, and the process exits 0.  A selector bug must NEVER
    block iteration — correctness is preserved by widening to the
    full suite.

    The ONLY exception is the explicit `return 1` from
    --record-good's RecordGoodValidationError handler above, which is
    a pure write operation where silent success on bad input would
    poison LKG.

    To verify the fail-open contract by hand: temporarily remove the
    try/except below and run `scripts/select_tests/__main__.py` after
    deleting grimp from the venv — the selector should NOW raise; with
    the try/except in place, it should emit the full test-root list
    and exit 0 instead.  TASK-5-2 includes a regression test that
    locks this behaviour.
    """
    # Defense-in-depth: a `PYTHONPATH` containing source roots like
    # `shared` makes subpackages reachable as both `shared.egg_agent`
    # and bare `egg_agent`, which causes grimp to abort the graph
    # build with `NotATopLevelModule`.  The Makefile already strips
    # the env via `env -u PYTHONPATH`, but anyone invoking the
    # script directly (or any future caller that forgets the
    # wrapper) gets the same protection here.
    #
    # Popping the env var alone is not enough — Python has already
    # prepended the PYTHONPATH entries to sys.path at interpreter
    # startup (resolved to absolute paths), and grimp inspects
    # sys.path directly during `build_graph`.  Scrub both.
    _strip_pythonpath_from_sys_path()
    try:
        return _main_inner(argv)
    except SystemExit:
        # argparse's `sys.exit` for `--help` etc. — let through.
        raise
    except BaseException:
        # noqa: BLE001 — the WHOLE point is "catch anything".
        traceback.print_exc()
        emit_full_suite("select-tests: full suite (trigger=selector exception)")
        return 0


__all__ = (
    "_build_arg_parser",
    "_fnmatch",
    "_format_chain",
    "_main_inner",
    "_now_iso",
    "_run_narrow_or_fallback",
    "_short_sha",
    "_split_pytest_args_env",
    "_strip_pythonpath_from_sys_path",
    "emit_full_suite",
    "evaluate_fallback_triggers",
    "explain_why",
    "main",
    "patch_selection_record",
    "write_selection_record",
)
