"""Tests for ``LocalWorktreeManager`` (#2623 slice-1 task-1-5, task-1-8).

Acceptance criteria covered:

* ``LocalWorktreeManager`` exposes ``create(pipeline_id, role)`` and
  ``tear_down(pipeline_id)``.
* Path-escape inputs (``..``, ``/absolute``, embedded ``..`` segments,
  null bytes, empty strings) are rejected.  Oracle:
  ``gateway/worktree_manager.py:88 validate_identifier`` and
  ``:1711`` ``is_relative_to`` guard.
* Worktrees are rooted under ``<base>/<pipeline_id>/<role>/``;
  ``EGG_WORKTREE_BASE`` overrides the default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
worktree_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.worktree",
    reason="orchestrator/substrate/claude_code/worktree.py not present yet",
)


@pytest.fixture
def fake_role() -> Any:
    class _Role:
        value = "refiner"

        def __str__(self) -> str:  # pragma: no cover
            return self.value

    return _Role()


# ---------------------------------------------------------------------------
# Construction + base resolution
# ---------------------------------------------------------------------------


def test_constructor_explicit_base_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The constructor's explicit ``base`` argument wins over ``EGG_WORKTREE_BASE``."""
    monkeypatch.setenv("EGG_WORKTREE_BASE", "/tmp/some-env-override")
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    assert mgr.base == tmp_path


def test_constructor_uses_env_when_no_explicit_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``EGG_WORKTREE_BASE`` is honored when no explicit base is passed."""
    monkeypatch.setenv("EGG_WORKTREE_BASE", str(tmp_path))
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager()
    assert mgr.base == tmp_path


# ---------------------------------------------------------------------------
# Path-escape rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_identifier,why",
    [
        ("..", "naked dotdot"),
        ("../escape", "leading dotdot"),
        ("foo/../bar", "embedded dotdot"),
        ("/absolute", "absolute path"),
        ("with\x00null", "null byte"),
        ("", "empty"),
        (".starts-with-dot", "leading dot"),
        ("with space", "contains space"),
        ("with$dollar", "contains dollar"),
    ],
)
def test_create_rejects_path_escape_pipeline_id(
    bad_identifier: str,
    why: str,
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """Path-escape pipeline_ids raise ``ValueError`` before any filesystem op."""
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        mgr.create(bad_identifier, fake_role)
    # Must mention pipeline_id in the error message — the gateway's
    # validate_identifier shape.
    assert "pipeline_id" in str(excinfo.value) or "Invalid" in str(excinfo.value), (
        f"{why}: error message should reference pipeline_id; got {excinfo.value!r}"
    )


# ---------------------------------------------------------------------------
# Worktree rooted under base/<pipeline_id>/<role>/
# ---------------------------------------------------------------------------


def test_create_places_worktree_under_base_pipeline_role(tmp_path: Path, fake_role: Any) -> None:
    """Created worktree is at ``<base>/<pipeline_id>/<role>/`` per cq-5."""
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    out = mgr.create("pipeline-2623", fake_role)
    expected = (tmp_path / "pipeline-2623" / "refiner").resolve()
    assert out == expected, f"worktree at unexpected path: {out!r} vs {expected!r}"
    assert out.exists()
    assert out.is_dir()


def test_create_pipeline_isolates_roles(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """Two roles in the same pipeline land in distinct subdirs."""
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    refiner_path = mgr.create("pipeline-x", fake_role)

    class _OtherRole:
        value = "coder"

        def __str__(self) -> str:  # pragma: no cover
            return self.value

    coder_path = mgr.create("pipeline-x", _OtherRole())
    assert refiner_path != coder_path
    assert refiner_path.parent == coder_path.parent  # both under <base>/pipeline-x/


# ---------------------------------------------------------------------------
# Tear-down only deletes paths under the base
# ---------------------------------------------------------------------------


def test_tear_down_does_not_remove_paths_outside_base(
    tmp_path: Path, fake_role: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tear_down`` refuses to remove any path that resolves outside ``base``.

    Mirrors ``gateway/worktree_manager.py:1711`` is_relative_to guard.
    """
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    # Plant a guardrail target *outside* the base. The manager tracks
    # only paths it creates, so the tear-down's path-escape guard only
    # triggers when an entry escapes after tracking — simulate that.
    outside = tmp_path.parent / "outside-canary"
    outside.mkdir(exist_ok=True)
    canary = outside / "do-not-delete"
    canary.write_text("canary")
    # Inject a tracked entry that resolves outside base.
    mgr._tracked["pipeline-evil"] = [(outside, "egg/pipeline-evil/refiner")]  # type: ignore[attr-defined]
    mgr.tear_down("pipeline-evil")
    assert canary.exists(), (
        "tear_down must NOT remove paths outside the configured base "
        "(gateway/worktree_manager.py:1711 is_relative_to guard)"
    )


def test_tear_down_removes_created_worktree(
    tmp_path: Path,
    fake_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tear_down`` removes a previously-created worktree under the base."""
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    path = mgr.create("pipeline-clean", fake_role)
    assert path.exists()
    mgr.tear_down("pipeline-clean")
    assert not path.exists(), "tear_down must remove the created worktree dir"


# ---------------------------------------------------------------------------
# Tear-down validates pipeline_id input
# ---------------------------------------------------------------------------


def test_tear_down_rejects_path_escape_pipeline_id(tmp_path: Path) -> None:
    """``tear_down`` rejects path-escape pipeline_ids the same way ``create`` does."""
    LocalWorktreeManager = worktree_mod.LocalWorktreeManager
    mgr = LocalWorktreeManager(base=tmp_path)
    with pytest.raises(ValueError):
        mgr.tear_down("..")
