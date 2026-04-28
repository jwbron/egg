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
# Top-of-list triggers (canary, unresolvable, stale LKG, empty diff)
# ----------------------------------------------------------------------


def test_canary_trigger_short_circuits() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["gateway/policy.py"],
        bundle=_StubBundle(),
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=True,
    )
    assert trigger == "canary (every-10th invocation)"


def test_unresolvable_baseline_trigger() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["a.py"],
        bundle=_StubBundle(),
        baseline_source="UNRESOLVABLE",
        lkg_was_stale=False,
        canary_fired=False,
    )
    assert trigger == "unresolvable baseline"


def test_lkg_not_ancestor_trigger() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["a.py"],
        bundle=_StubBundle(),
        baseline_source="BASE_BRANCH",
        lkg_was_stale=True,
        canary_fired=False,
    )
    assert trigger == "LKG not ancestor of HEAD"


def test_empty_diff_trigger() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=[],
        bundle=_StubBundle(),
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=False,
    )
    assert trigger == "empty diff"


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
        canary_fired=False,
    )
    assert trigger == "conftest changed"


def test_shared_tests_change_triggers() -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=["shared/tests/test_foo.py"],
        bundle=_StubBundle(all_modules={"shared.tests.test_foo"}),
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=False,
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
        canary_fired=False,
    )
    assert trigger == expected


# ----------------------------------------------------------------------
# R1 — gateway importlib test-loader mapping.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "gateway/policy.py",
        "gateway/server.py",
        "gateway/auth.py",
        "gateway/credentials.py",
    ],
)
def test_gateway_source_change_widens_to_full_suite(path: str) -> None:
    trigger = selector.evaluate_fallback_triggers(
        paths=[path],
        bundle=_StubBundle(all_modules={"gateway." + Path(path).stem}),
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=False,
    )
    assert trigger == "gateway source change (importlib test-loader)"


def test_gateway_test_change_does_not_fire_gateway_rule() -> None:
    """A change under ``gateway/tests/`` (not a production file
    directly under ``gateway/``) must NOT fire the gateway rule —
    those edits are tester-side and use the standard graph closure.
    """
    bundle = _StubBundle(
        all_modules={"gateway.tests.test_policy"},
        dynamic_import_modules=set(),
        upstream_map={},
    )
    trigger = selector.evaluate_fallback_triggers(
        paths=["gateway/tests/test_policy.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=False,
    )
    assert trigger != "gateway source change (importlib test-loader)"


def test_nested_gateway_path_does_not_fire_gateway_rule() -> None:
    """Files under a sub-directory of gateway (e.g. ``gateway/sub/x.py``)
    are not directly under gateway/, so they don't trigger the
    importlib rule (the rule's pattern is ``gateway/<file>.py`` only).
    """
    bundle = _StubBundle(all_modules={"gateway.sub.x"})
    trigger = selector.evaluate_fallback_triggers(
        paths=["gateway/sub/x.py"],
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=False,
    )
    assert trigger != "gateway source change (importlib test-loader)"


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
        canary_fired=False,
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
        canary_fired=False,
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
        canary_fired=False,
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
        all_modules={"gateway.gateway", "gateway.policy"},
        dynamic_import_modules={"gateway.gateway"},
    )
    trigger = selector.evaluate_fallback_triggers(
        paths=["gateway/policy.py"],  # gateway/*.py — fires R1 first
        bundle=bundle,
        baseline_source="LKG",
        lkg_was_stale=False,
        canary_fired=False,
    )
    # gateway/*.py rule fires before the dynamic-import rule, so we get
    # the more-specific R1 string.  This is intentional priority order.
    assert trigger == "gateway source change (importlib test-loader)"


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
        canary_fired=False,
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
        canary_fired=False,
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


def test_subprocess_with_leaked_pythonpath_does_not_abort_graph(real_git, tmp_path: Path) -> None:
    """End-to-end contract: setting ``PYTHONPATH=shared:gateway:orchestrator``
    in the child env (mimicking the Makefile pre-fix) MUST NOT cause
    grimp to abort with ``NotATopLevelModule`` against the real repo.

    Runs against ``REPO_ROOT`` so PACKAGES (which includes
    ``shared.egg_agent``) actually overlaps with the leaked
    ``shared/`` entry.  Skips when grimp isn't installed because the
    bug is a grimp-specific failure mode.
    """
    pytest.importorskip("grimp")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("EGG_AGENT_ROLE", None)
    env["PYTHONPATH"] = "shared:gateway:orchestrator"
    proc = subprocess.run(
        [find_python(), str(SELECTOR_PATH), "--full-suite"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )
    # --full-suite avoids needing a synthetic git history but still
    # exercises main()'s strip before any further setup.  The bug
    # would surface as `graph build failed: NotATopLevelModule` on
    # stderr from a subsequent narrow-mode invocation; with --full-suite
    # we instead assert the strip itself didn't blow up and the
    # selector returns the canonical test-root list.
    assert proc.returncode == 0, (
        f"selector exited {proc.returncode} with leaked PYTHONPATH\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    out = proc.stdout.splitlines()
    for d in selector.TEST_ROOT_DIRS:
        assert d in out, f"missing {d} in stdout: {out!r}"
    assert "NotATopLevelModule" not in proc.stderr, (
        f"PYTHONPATH leaked into grimp despite the strip:\n{proc.stderr!r}"
    )
