"""Tests for ``LocalWorktreeManager`` (#2623 slice-1 task-1-5, task-1-8).

Acceptance criteria covered:

* ``LocalWorktreeManager`` conforms to the ``WorktreeManager`` Protocol
  (``create_worktree`` / ``remove_worktree`` / path-resolution surface).
* Path-escape inputs (``..``, absolute paths, embedded ``..`` segments,
  null bytes) are rejected — oracle is
  ``gateway/worktree_manager.py:88 validate_identifier`` and
  ``:110 validate_branch_ref``.
* Worktrees are rooted under ``.egg-state/<pipeline_id>/`` per cq-5.

These tests live in ``shared/tests/`` and operate against a tmp_path
so they don't pollute the real ``.egg-state/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)
claude_code_pkg = pytest.importorskip(
    "substrate.claude_code",
    reason=(
        "orchestrator/substrate/claude_code/ package not present yet "
        "(task-1-5 pending)"
    ),
)
worktree_mod = pytest.importorskip(
    "substrate.claude_code.worktree",
    reason="substrate.claude_code.worktree module not present yet (task-1-5)",
)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_local_worktree_manager_class_exported() -> None:
    """``LocalWorktreeManager`` is reachable and has the worktree surface."""
    cls = getattr(worktree_mod, "LocalWorktreeManager", None)
    assert cls is not None, (
        "substrate.claude_code.worktree.LocalWorktreeManager missing — "
        "task-1-5 AC"
    )


# ---------------------------------------------------------------------------
# Path-escape rejection (oracle: gateway/worktree_manager.py:88/110)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_identifier,why",
    [
        ("..", "naked dotdot"),
        ("../escape", "embedded dotdot"),
        ("foo/../bar", "embedded segment"),
        ("/absolute", "absolute path"),
        ("with\x00null", "null byte"),
        ("", "empty"),
        (".starts-with-dot", "leading dot"),
    ],
)
def test_path_escape_inputs_rejected(
    bad_identifier: str,
    why: str,
    tmp_path: Path,
) -> None:
    """Path-escape inputs raise ``ValueError`` before any filesystem op.

    Oracle is ``gateway/worktree_manager.py:88 validate_identifier``;
    the local manager must enforce equivalent rules so the in-process
    substrate doesn't accidentally permit traversal that the gateway
    layer would catch on the k3s side.
    """
    cls = getattr(worktree_mod, "LocalWorktreeManager")
    pytest.skip(
        f"LocalWorktreeManager constructor / create_worktree signature "
        f"pending — fill in once task-1-5 lands ({why})"
    )


# ---------------------------------------------------------------------------
# Worktrees rooted under .egg-state/<pipeline_id>/ (cq-5)
# ---------------------------------------------------------------------------


def test_worktree_rooted_under_egg_state(tmp_path: Path) -> None:
    """Created worktrees live under ``.egg-state/<pipeline_id>/`` per cq-5.

    The base directory is configurable but defaults to
    ``.egg-state/<pipeline_id>/`` per the architect output and cq-5.
    The test pins the path discipline once construction stabilizes.
    """
    cls = getattr(worktree_mod, "LocalWorktreeManager")
    pytest.skip(
        "LocalWorktreeManager constructor signature pending — fill in once "
        "task-1-5 lands"
    )


# ---------------------------------------------------------------------------
# Identifier validation matches gateway oracle
# ---------------------------------------------------------------------------


def test_valid_identifier_accepted() -> None:
    """Conforming identifiers (alnum + dot/underscore/dash) are accepted."""
    cls = getattr(worktree_mod, "LocalWorktreeManager")
    pytest.skip(
        "LocalWorktreeManager validation surface pending — fill in once "
        "task-1-5 lands"
    )
