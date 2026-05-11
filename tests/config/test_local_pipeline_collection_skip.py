"""Regression test for `integration_tests/local_pipeline/conftest.py`'s
`pytest_collection_modifyitems` hook.

The hook narrows the legacy docker-compose-era `local_pipeline/` tree
to skip until the k3s rewrite lands. An earlier revision matched on a
filename substring (`if "test_k8s_deployment_tools" in item.nodeid`),
which silently skipped EVERY non-`test_k8s_deployment_tools` test in
the entire pytest session — `pytest_collection_modifyitems` in a
sub-conftest receives the full session item list, not just items under
that conftest's directory. That neutralized the integration tier on
PR-CI without anyone noticing.

The predicate is extracted as
`_local_pipeline_nodeid_should_skip` in the conftest specifically so
this test can import it directly without dragging in the conftest's
relative imports + docker mock. The hook itself is a thin wrapper
that just iterates items and calls `add_marker(skip)`.

Pinned contract:
    1. Items whose nodeid starts with `integration_tests/local_pipeline/`
       are marked skip — except `test_k8s_deployment_tools`.
    2. Items outside `integration_tests/local_pipeline/` are NEVER
       marked skip, regardless of filename or parametrize id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFTEST_PATH = REPO_ROOT / "integration_tests" / "local_pipeline" / "conftest.py"


def _load_predicate():
    """Import `_local_pipeline_nodeid_should_skip` from the conftest source.

    We can't import the conftest as a module — it has relative
    imports (`from .helpers import …`) and side-effect-y autouse
    fixtures. Read the source, compile only the predicate function in
    isolation. The predicate is pure (string in → bool out), so this
    isolation is sound.
    """
    source = CONFTEST_PATH.read_text(encoding="utf-8")
    marker = "def _local_pipeline_nodeid_should_skip(nodeid: str) -> bool:"
    start = source.find(marker)
    assert start != -1, (
        f"Could not find _local_pipeline_nodeid_should_skip in {CONFTEST_PATH}. "
        f"Did the function get renamed or removed? The regression test "
        f"depends on this exported predicate seam."
    )
    # Read until the next top-level `def ` / `class ` or end of file.
    rest = source[start + len(marker) :]
    end_offsets = []
    for needle in ("\ndef ", "\nclass "):
        idx = rest.find(needle)
        if idx != -1:
            end_offsets.append(idx)
    end = min(end_offsets) if end_offsets else len(rest)
    func_source = source[start : start + len(marker) + end]

    namespace: dict = {}
    exec(compile(func_source, str(CONFTEST_PATH), "exec"), namespace)
    return namespace["_local_pipeline_nodeid_should_skip"]


@pytest.mark.parametrize(
    "nodeid",
    [
        # The actual sibling suites the bug previously skipped on PR-CI.
        "integration_tests/test_babysit_pr/test_escalation.py::test_x",
        "integration_tests/test_slice_pipeline_e2e.py::test_orphan",
        "integration_tests/sdlc/test_pipeline.py::test_run",
        "integration_tests/test_error_recovery.py::test_recover",
        "integration_tests/test_stack_lifecycle.py::test_squid_process_running",
        # Top-level unit tests must also pass through untouched.
        "gateway/tests/test_session_manager.py::test_validate_ip_mismatch_is_audit_only",
        "tests/config/test_workflows_structure.py::test_test_workflow_has_aggregate",
        # Parametrize ids that mention `local_pipeline` in a non-path
        # position must not be caught.
        "integration_tests/test_other.py::test_param[local_pipeline/whatever]",
    ],
)
def test_sibling_tests_outside_local_pipeline_are_not_skipped(nodeid: str) -> None:
    should_skip = _load_predicate()
    assert should_skip(nodeid) is False, (
        f"Regression: nodeid {nodeid!r} would be marked skip by the "
        f"local_pipeline/ collection hook, but it is outside that "
        f"directory. The predicate must scope its skip to nodeids that "
        f"start with 'integration_tests/local_pipeline/'."
    )


@pytest.mark.parametrize(
    "nodeid",
    [
        "integration_tests/local_pipeline/test_local_pipeline.py::test_x",
        "integration_tests/local_pipeline/test_worktree_integration.py::test_y",
        "integration_tests/local_pipeline/test_concurrent_pipelines.py::test_z",
        "integration_tests/local_pipeline/test_signals.py::test_signal",
        # Hypothetical nested file — still under local_pipeline/, must skip.
        "integration_tests/local_pipeline/sub/test_nested.py::test_n",
    ],
)
def test_local_pipeline_subtree_is_skipped(nodeid: str) -> None:
    should_skip = _load_predicate()
    assert should_skip(nodeid) is True, (
        f"Regression: nodeid {nodeid!r} would NOT be marked skip by "
        f"the local_pipeline/ collection hook. The whole local_pipeline/ "
        f"tree (except test_k8s_deployment_tools) must remain skipped "
        f"until the k3s rewrite lands — see the predicate's docstring."
    )


def test_k8s_deployment_tools_is_not_skipped() -> None:
    """`test_k8s_deployment_tools.py` is the one local_pipeline/ file
    that legitimately runs under k3s — it only asserts the
    lifecycle-auth decorator rejects unauth'd / bogus-bearer calls,
    which works against any orchestrator deployment."""
    should_skip = _load_predicate()
    assert (
        should_skip("integration_tests/local_pipeline/test_k8s_deployment_tools.py::test_dep")
        is False
    ), (
        "Regression: test_k8s_deployment_tools would be marked skip. "
        "The predicate must keep this one file running — it's the "
        "auth-rejection regression suite that works fine under k3s."
    )


def test_windows_style_separators_are_normalized() -> None:
    """nodeids with backslash separators (Windows pytest) must still
    be matched by the directory check."""
    should_skip = _load_predicate()
    assert should_skip(r"integration_tests\local_pipeline\test_local_pipeline.py::test_x") is True
    assert should_skip(r"integration_tests\test_babysit_pr\test_escalation.py::test_x") is False
