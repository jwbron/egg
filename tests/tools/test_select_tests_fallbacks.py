"""TASK-5-2 — Fallback-trigger tests for scripts/select_tests.py.

Each fallback trigger from algorithm §5 has at least one parametrized
case below.  The tests drive ``selector.evaluate_fallback_triggers``
directly when grimp isn't required, and full subprocess invocations
when end-to-end behavior matters (e.g. the fail-open contract from
TASK-2-1).

R1 (gateway-importlib) and R2 (source-staleness) get dedicated cases.
The fail-open regression test pins TASK-2-1's blanket try/except — it
includes an inline comment explaining how to manually verify the
contract by removing the try/except, as required by the AC.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import (
    REPO_ROOT,
    SELECTOR_PATH,
    _git,
    commit_file,
    find_python,
    init_git_repo,
    load_selector,
)

selector = load_selector()


# ----------------------------------------------------------------------
# Direct ``evaluate_fallback_triggers`` cases — bundle=None means the
# graph wasn't built (which itself is a trigger), so most tests pass a
# minimal stub.
# ----------------------------------------------------------------------


class _StubBundle:
    """Fake GraphBundle for trigger-eval tests.

    Only the fields ``evaluate_fallback_triggers`` reads are exposed:
    ``all_modules``, ``dynamic_import_modules``, ``missing_source_paths``,
    plus a stub ``graph.find_upstream_modules`` for the dynamic-import
    reachability check.
    """

    def __init__(
        self,
        *,
        all_modules: set[str] | None = None,
        dynamic_import_modules: set[str] | None = None,
        missing_source_paths: list[str] | None = None,
        upstream_map: dict[str, set[str]] | None = None,
    ) -> None:
        self.all_modules = all_modules or set()
        self.dynamic_import_modules = dynamic_import_modules or set()
        self.missing_source_paths = missing_source_paths or []
        self.graph = _StubGraph(upstream_map or {})


class _StubGraph:
    def __init__(self, upstream_map: dict[str, set[str]]) -> None:
        self._upstream = upstream_map

    def find_upstream_modules(self, module: str, *, as_package: bool = False) -> set[str]:
        return self._upstream.get(module, set())

    def find_downstream_modules(self, module: str, *, as_package: bool = False) -> set[str]:
        return set()


# ----------------------------------------------------------------------
# Top-of-list triggers (unresolvable, stale LKG)
# ----------------------------------------------------------------------


def test_unresolvable_baseline_trigger() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["a.py"],
        bundle=_StubBundle(),
        baseline_source="UNRESOLVABLE",
        lkg_was_stale=False,
    )
    assert trigger == "unresolvable baseline"


def test_lkg_not_ancestor_trigger() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["a.py"],
        bundle=_StubBundle(),
        baseline_source="BASE_BRANCH",
        lkg_was_stale=True,
    )
    assert trigger == "LKG not ancestor of HEAD"


def test_empty_diff_no_longer_triggers_full_suite() -> None:
    """Empty diff against a resolvable baseline must NOT widen to the
    full suite — it short-circuits to ``selected_count=0`` in
    ``_run_narrow_or_fallback`` so pytest is skipped entirely.  The
    trigger evaluator returns None for this case."""
    trigger = selector.evaluate_fallback_triggers(
        paths=[],
        bundle=_StubBundle(),
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger is None


def test_pytest_args_bypass_takes_precedence_over_empty_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when the diff is empty AND ``PYTEST_ARGS_RAW`` has
    an explicit test path, the bypass branch MUST fire — not the
    empty-diff short-circuit.  Otherwise ``make test
    PYTEST_ARGS=tests/foo/test_bar.py`` on a clean tree silently
    drops the user's path (Makefile keys off ``mode=bypass`` to
    invoke pytest with the user's args).

    Drives ``_run_narrow_or_fallback`` in-process with a stubbed
    ``_run_git`` to simulate a clean tree on a resolvable baseline,
    independent of the sandbox's gateway-wrapped git binary."""
    # Build a real test file so pytest_args_have_explicit_path resolves.
    tests_dir = tmp_path / "tests" / "tools"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_dummy.py"
    target.write_text("def test_one():\n    assert True\n", encoding="utf-8")

    # Stub `_run_git` so resolve_baseline / HEAD lookups succeed
    # against an entirely synthetic repo (no real .git directory).
    fake_head = "0" * 39 + "a"
    fake_baseline = "0" * 39 + "b"

    def fake_run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        if args[:2] == ["rev-parse", "HEAD"]:
            return 0, fake_head + "\n", ""
        if args[:1] == ["rev-parse"] and args[-1] == "--abbrev-ref":
            return 0, "main\n", ""
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n", ""
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, str(tmp_path) + "\n", ""
        if args[:2] == ["merge-base", "HEAD"]:
            return 0, fake_baseline + "\n", ""
        if args[:1] == ["merge-base"] and "--is-ancestor" in args:
            return 0, "", ""
        if args[:2] == ["cat-file", "-e"]:
            return 0, "", ""
        if args[:1] == ["diff"]:
            # Empty diff = clean tree.
            return 0, "", ""
        if args[:1] == ["status"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(selector, "_run_git", fake_run_git)
    # User asked for an explicit test path — bypass MUST win.
    monkeypatch.setenv("PYTEST_ARGS_RAW", "tests/tools/test_dummy.py")
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    # Avoid LKG sidecar reads — tmp_path has no .egg-state.
    monkeypatch.chdir(tmp_path)

    rc = selector._run_narrow_or_fallback(tmp_path)
    assert rc == 0

    # Selection record must record `mode=bypass`, not `mode=narrow`.
    record_path = tmp_path / ".egg-state" / "selection" / f"{fake_head}.json"
    assert record_path.is_file(), f"missing record at {record_path}"
    import json

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["mode"] == "bypass", record
    assert record["trigger"] == "PYTEST_ARGS explicit path", record


# ----------------------------------------------------------------------
# Path-pattern triggers — each test uses the documented explicit
# trigger string; a generic word ("fallback") is NOT acceptable.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "gateway/tests/conftest.py",
        "orchestrator/tests/conftest.py",
        "shared/tests/conftest.py",
        "tests/action/conftest.py",
    ],
)
def test_conftest_at_any_level_triggers(path: str) -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=[path],
        bundle=_StubBundle(all_modules={"x"}),
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == "conftest changed"


def test_shared_tests_change_triggers() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["shared/tests/test_foo.py"],
        bundle=_StubBundle(all_modules={"shared.tests.test_foo"}),
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == "shared/tests/ changed"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Makefile", "Makefile changed"),
        ("pyproject.toml", "pyproject.toml changed"),
        ("uv.lock", "uv.lock changed"),
        (".python-version", ".python-version changed"),
        (".github/workflows/test.yml", ".github/workflows/test.yml changed"),
    ],
)
def test_static_path_triggers(path: str, expected: str) -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=[path],
        bundle=_StubBundle(all_modules={"x"}),
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == expected


# ----------------------------------------------------------------------
# Gateway production edits no longer widen — the AST resolver bridges
# the importlib test-loader pattern via `gateway.` in
# `BARE_NAME_STRIP_PREFIXES`, so a `gateway/<file>.py` edit must
# resolve to None here and be handled by narrow analysis.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "gateway/policy.py",
        "gateway/auth.py",
        "gateway/checkpoint_handler.py",
        "gateway/worktree_manager.py",
    ],
)
def test_gateway_source_change_does_not_widen(path: str) -> None:
    """`gateway/<file>.py` edits used to short-circuit to the full
    suite via the dedicated importlib-test-loader trigger.  The AST
    resolver now records the test→production edges grimp cannot see,
    so these edits must fall through to narrow analysis."""
    bundle = _StubBundle(all_modules={"gateway." + Path(path).stem})
    trigger = selector.evaluate_fallback_triggers(
        paths=[path],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger is None


# ----------------------------------------------------------------------
# Non-.py fallback (decision-5)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "gateway/prompts/foo.txt",
        "shared/data/seed.json",
        "scripts/install-calico.sh",
        "docs/index.md",
        "Dockerfile",
    ],
)
def test_non_py_change_triggers(path: str) -> None:
    bundle = _StubBundle(all_modules={"x"})
    trigger = selector.evaluate_fallback_triggers(
        paths=[path],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == "non-.py change"


# ----------------------------------------------------------------------
# R2 — source-file staleness guard
# ----------------------------------------------------------------------


def test_source_file_missing_from_graph_triggers() -> None:
    bundle = _StubBundle(
        all_modules={"x"},
        missing_source_paths=["shared/egg_config/_orphan.py"],
    )
    trigger = selector.evaluate_fallback_triggers(
        paths=["shared/egg_config/x.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == "source file missing from graph: shared/egg_config/_orphan.py"


# ----------------------------------------------------------------------
# Unresolvable module path
# ----------------------------------------------------------------------


def test_unresolvable_module_path_triggers() -> None:
    """A .py file whose module id is NOT in the graph (e.g., a brand
    new file not yet under any registered package) widens to full
    suite with the explicit reason."""
    bundle = _StubBundle(
        all_modules={"existing.module"},
    )
    trigger = selector.evaluate_fallback_triggers(
        paths=["foo/bar.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger is not None
    assert "unresolvable module path" in trigger
    assert "foo/bar.py" in trigger


# ----------------------------------------------------------------------
# Dynamic-import reachability (decision-10)
# ----------------------------------------------------------------------


def test_dynamic_import_reachability_changed_module_in_seed_set() -> None:
    """A changed module that IS in the dynamic-import seed set fires
    the trigger directly."""
    bundle = _StubBundle(
        all_modules={"sandbox.plugin_loader"},
        dynamic_import_modules={"sandbox.plugin_loader"},
    )
    trigger = selector.evaluate_fallback_triggers(
        paths=["sandbox/plugin_loader.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == "dynamic-import reachability"


def test_dynamic_import_reachability_via_upstream() -> None:
    """A changed module that imports a dynamic-import seed (transitively)
    fires the dynamic-import trigger."""
    # Use a non-gateway module so R1 doesn't fire first.
    bundle = _StubBundle(
        all_modules={"sandbox.runner", "sandbox.plugin_loader"},
        dynamic_import_modules={"sandbox.plugin_loader"},
        # `sandbox.runner` imports `sandbox.plugin_loader`, so plugin_loader's
        # upstream set contains runner.
        upstream_map={"sandbox.plugin_loader": {"sandbox.runner"}},
    )
    trigger = selector.evaluate_fallback_triggers(
        paths=["sandbox/runner.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger == "dynamic-import reachability"


# ----------------------------------------------------------------------
# Negative case — narrow path is safe (no trigger).
# ----------------------------------------------------------------------


def test_simple_leaf_change_no_trigger() -> None:
    bundle = _StubBundle(all_modules={"orchestrator.foo"})
    trigger = selector.evaluate_fallback_triggers(
        paths=["orchestrator/foo.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
    )
    assert trigger is None


# ----------------------------------------------------------------------
# Fail-open regression test (TASK-2-1)
# ----------------------------------------------------------------------
# IMPLEMENTATION NOTE — to manually verify the fail-open contract,
# temporarily remove the ``try: ... except BaseException:`` block in
# ``select_tests.main`` and re-run this test.  Without the try/except,
# the test below FAILS because the selector raises instead of widening
# to the full suite.  With the try/except in place, the selector emits
# the test-root list on stdout and exits 0.  Do NOT delete this comment
# — TASK-5-2's AC explicitly requires it as a discovery aid for future
# maintainers (issue #1973 plan §TASK-5-2).


def test_fail_open_unhandled_exception_emits_full_suite_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A synthetic exception inside ``_main_inner`` MUST be caught by
    the blanket try/except in ``main()``: traceback to stderr, full
    test-root list to stdout, exit 0."""

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("synthetic selector failure")

    monkeypatch.setattr(selector, "_main_inner", boom)
    rc = selector.main([])
    assert rc == 0
    captured = capsys.readouterr()
    # stdout — full test-root list.
    for d in selector.TEST_ROOT_DIRS:
        assert d in captured.out, f"missing test-root {d} in stdout"
    # stderr — traceback contains the synthetic message.
    assert "synthetic selector failure" in captured.err


def test_fail_open_argparse_error_still_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """argparse syntax errors raise SystemExit which the wrapper
    re-raises (per the docstring on ``main``).  An unknown flag must
    cause a non-zero exit, NOT trigger the fail-open path."""
    with pytest.raises(SystemExit) as exc_info:
        selector.main(["--no-such-flag"])
    # argparse uses exit code 2.
    assert exc_info.value.code != 0


def test_empty_diff_subprocess_skips_pytest(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a clean repo with no diff against the baseline must
    emit zero stdout lines, log `selected 0 tests (skipping pytest)`,
    and exit 0.  The Makefile keys off the empty stdout to skip pytest
    entirely — running the full suite for a no-op would defeat the
    point of changeset-aware narrowing."""
    init_git_repo(tmp_path)
    # Mirror the real repo's gitignore for selector-internal sidecars
    # so any files the selector writes don't surface as untracked
    # entries in `git status --porcelain` and false-fire the `non-.py
    # change` trigger on subsequent invocations.
    commit_file(
        tmp_path,
        ".gitignore",
        ".egg-state/last-known-good/\n.egg-state/selection/\n.egg-state/grimp-cache/\n",
        "gitignore selector sidecars",
    )
    commit_file(tmp_path, "shared/egg_config/x.py", "x = 1\n", "first")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head_sha)
    # NOTE: no uncommitted change — diff against origin/main is empty.

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("EGG_AGENT_ROLE", None)
    proc = subprocess.run(
        [find_python(), str(SELECTOR_PATH)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"selector exited {proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # Empty stdout — pytest-skip signal to the Makefile.
    assert proc.stdout.strip() == "", (
        f"expected empty stdout, got: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # Stderr explains the decision.
    assert "selected 0 tests" in proc.stderr, (
        f"expected 'selected 0 tests' in stderr, got: {proc.stderr!r}"
    )
    # And it must NOT have reported a fallback trigger or full-suite
    # widening — the whole point is that empty diff no longer widens.
    assert "full suite" not in proc.stderr, (
        f"empty diff must not widen to full suite: {proc.stderr!r}"
    )
    assert "trigger=empty diff" not in proc.stderr


def test_empty_diff_with_pytest_args_explicit_path_takes_bypass(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: empty diff + ``PYTEST_ARGS_RAW`` containing an
    explicit test path must classify as ``mode=bypass``, not the
    silent ``mode=narrow`` empty-diff skip.  Otherwise
    ``make test PYTEST_ARGS=tests/foo/test_bar.py`` on a clean tree
    would silently drop the user's path — pytest would never run.

    The bypass contract (``docs/guides/testing.md``) says: an explicit
    path in PYTEST_ARGS bypasses narrowing.  That contract must hold
    even when the diff is empty."""
    init_git_repo(tmp_path)
    commit_file(
        tmp_path,
        ".gitignore",
        ".egg-state/last-known-good/\n.egg-state/selection/\n.egg-state/grimp-cache/\n",
        "gitignore selector sidecars",
    )
    # Create a real test file so pytest_args_have_explicit_path can
    # resolve the path against an actual file on disk.
    commit_file(
        tmp_path,
        "tests/tools/test_dummy.py",
        "def test_one():\n    assert True\n",
        "add dummy test",
    )
    head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head_sha)
    # NO uncommitted change — diff against origin/main is empty.

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("EGG_AGENT_ROLE", None)
    # The user is asking for a specific test — bypass MUST win over
    # the empty-diff short-circuit.
    env["PYTEST_ARGS_RAW"] = "tests/tools/test_dummy.py"
    proc = subprocess.run(
        [find_python(), str(SELECTOR_PATH)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"selector exited {proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # Bypass mode emits empty stdout (Makefile falls through to
    # PYTEST_ARGS) and logs the bypass decision to stderr.
    assert "bypass mode" in proc.stderr, f"expected 'bypass mode' in stderr, got: {proc.stderr!r}"
    # The empty-diff skip log MUST NOT have fired — that's the
    # regression we're guarding against.
    assert "skipping pytest" not in proc.stderr, (
        f"empty-diff skip must not fire when PYTEST_ARGS has an explicit path: {proc.stderr!r}"
    )
    # Selection record must be `mode=bypass` so the Makefile keys off
    # it correctly (`Makefile:308` greps for `"mode": "bypass"` in
    # `.egg-state/selection/<head>.json` to decide whether to invoke
    # pytest with the user's PYTEST_ARGS).
    record_path = tmp_path / ".egg-state" / "selection" / f"{head_sha}.json"
    assert record_path.is_file(), (
        f"selection record missing at {record_path}; selector stderr: {proc.stderr!r}"
    )
    import json

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["mode"] == "bypass", record


def test_fail_open_subprocess_grimp_unavailable(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the selector via ``subprocess`` against a synthetic repo;
    grimp is genuinely not installed in the sandbox, so the graph
    build raises ImportError.  The selector MUST still exit 0 with
    the full test-root list on stdout (or no tests if `_run_narrow_or_fallback`
    sees an explicit trigger first)."""
    # Build a minimal repo so the selector reaches `_run_narrow_or_fallback`
    # (it needs HEAD to exist).
    init_git_repo(tmp_path)
    commit_file(tmp_path, "shared/egg_config/x.py", "x = 1\n", "first")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head_sha)
    # Add an uncommitted change to force a non-empty diff.
    (tmp_path / "shared" / "egg_config" / "x.py").write_text("x = 2\n", encoding="utf-8")
    # Run the selector with PYTHONPATH that doesn't include grimp,
    # cwd=tmp_path so the selector resolves the synthetic repo.
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("EGG_AGENT_ROLE", None)
    proc = subprocess.run(
        [find_python(), str(SELECTOR_PATH)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"selector exited {proc.returncode} (fail-open contract violated)\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # stdout MUST be either empty (rare) or contain test-root paths;
    # the selector should NOT raise an unhandled exception that bubbles
    # to a non-zero exit.  The trigger reason should appear on stderr.
    # Either (a) the graph-unavailable trigger fires explicitly, or
    # (b) the fail-open wrapper kicks in — both are acceptable.
    assert (
        "graph unavailable" in proc.stderr
        or "selector exception" in proc.stderr
        or "trigger=" in proc.stderr  # any explicit trigger
    ), f"no fallback trigger logged on stderr: {proc.stderr!r}"


# ----------------------------------------------------------------------
# PYTHONPATH leak — the Makefile exports `PYTHONPATH=shared:gateway:orchestrator`
# for pytest, which previously also reached `select_tests.py`.  With
# `shared/` on sys.path, grimp's `build_graph` aborts with
# `NotATopLevelModule: shared.egg_agent` and the selector silently
# fell back to the full suite.  The two regression cases below pin
# both the helper and the end-to-end contract.
# ----------------------------------------------------------------------


def test_strip_pythonpath_pops_env_and_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_strip_pythonpath_from_sys_path`` MUST remove PYTHONPATH from
    ``os.environ`` AND strip its entries from ``sys.path`` (both raw
    and ``Path.resolve()`` forms — Python prepends the resolved
    absolute path at interpreter startup, but a sloppy caller could
    inject the raw form too)."""
    raw = "shared:gateway:orchestrator"
    monkeypatch.setenv("PYTHONPATH", raw)
    raw_entries = raw.split(os.pathsep)
    resolved_entries = [str(Path(e).resolve()) for e in raw_entries]
    # Inject both forms at the head of sys.path, then assert they're
    # all gone after the strip.
    original_path = list(sys.path)
    try:
        for entry in [*resolved_entries, *raw_entries]:
            sys.path.insert(0, entry)
        selector._strip_pythonpath_from_sys_path()
        assert "PYTHONPATH" not in os.environ
        for entry in [*raw_entries, *resolved_entries]:
            assert entry not in sys.path, f"{entry!r} still on sys.path"
    finally:
        sys.path[:] = original_path


def test_strip_pythonpath_no_op_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No PYTHONPATH → no env mutation, no sys.path mutation."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    snapshot = list(sys.path)
    selector._strip_pythonpath_from_sys_path()
    assert "PYTHONPATH" not in os.environ
    assert sys.path == snapshot


def test_subprocess_with_leaked_pythonpath_does_not_abort_graph() -> None:
    """End-to-end contract: setting ``PYTHONPATH=shared:gateway:orchestrator``
    in the child env (mimicking the Makefile pre-fix) MUST NOT cause
    grimp's ``build_graph`` to abort with ``NotATopLevelModule``.

    Drives ``--why`` against a real test path so the selector actually
    reaches ``build_graph`` (the ``--full-suite`` short-circuits before
    grimp is touched and would let the regression slip through silently).
    ``explain_why`` calls ``build_graph`` immediately; under the bug it
    logs ``select-tests: --why: graph build failed: NotATopLevelModule:``
    on stderr, and with the strip in ``main()`` neither marker appears.

    ``--why`` is purely read-only — no selection record write — so this
    test has no effect on subsequent ``make test`` runs in the same
    checkout.

    Skips when grimp isn't installed because the bug is a grimp-specific
    failure mode.
    """
    pytest.importorskip("grimp")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("EGG_AGENT_ROLE", None)
    env["PYTHONPATH"] = "shared:gateway:orchestrator"
    proc = subprocess.run(
        [
            find_python(),
            str(SELECTOR_PATH),
            "--why",
            "tests/test_python_syntax.py",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )
    # Fail-open contract guarantees rc==0; assert it explicitly so a
    # different non-zero exit (e.g. an unhandled exception escaping the
    # wrapper) doesn't go unnoticed.
    assert proc.returncode == 0, (
        f"selector exited {proc.returncode} with leaked PYTHONPATH\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # The two buggy stderr signatures: grimp's exception class name and
    # the explain_why fail-open log line that wraps it.  Either appearing
    # means PYTHONPATH leaked through the strip and into build_graph.
    assert "NotATopLevelModule" not in proc.stderr, (
        f"PYTHONPATH leaked into grimp despite the strip:\n{proc.stderr!r}"
    )
    assert "graph build failed" not in proc.stderr, (
        f"build_graph aborted (likely from PYTHONPATH leak):\n{proc.stderr!r}"
    )
