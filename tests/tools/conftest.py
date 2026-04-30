"""Shared pytest fixtures for ``tests/tools/`` (issue #1973).

The selector test suite (``test_select_tests_*``) auto-patches
``selector._io._run_git`` so tests run inside the egg sandbox where
``git`` on PATH is intercepted by a gateway-proxy wrapper.  See
``_select_tests_helpers.patched_run_git`` for details.
"""

from __future__ import annotations

import pytest

from tests.tools._select_tests_helpers import load_selector, patched_run_git


@pytest.fixture(autouse=False)
def real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``selector._io._run_git`` to invoke the real git binary.

    Opt-in fixture used by tests that drive selector flows which call
    git on a synthetic ``tmp_path`` repository (e.g. ``record_good``,
    ``resolve_baseline``).  Without the patch, the selector's bare
    ``git`` invocations are intercepted by the sandbox gateway and
    fail with ``ERROR: git init is not supported``.

    Also clears ``EGG_AGENT_ROLE`` so that sandbox-inherited values
    don't cause ``record_good()`` to short-circuit as read-only.
    Tests that need a specific role re-set it via ``monkeypatch.setenv``.
    """
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    patched_run_git(monkeypatch)


@pytest.fixture
def selector_module():  # noqa: ANN201 — late-bound module type
    """Return the loaded selector module.

    Centralises the SourceFileLoader dance so test modules don't have
    to call ``load_selector()`` themselves at import time.
    """
    return load_selector()
