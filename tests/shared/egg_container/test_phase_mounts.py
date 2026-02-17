"""Tests for phase_readonly_mounts() and ensure_egg_state_dirs() in egg_container.

Validates the readonly mount generation for phase-protected directories
and the directory creation helper used before container spawn.
"""

from unittest.mock import patch

import pytest
from egg_container import (
    _IMPLEMENT_READONLY_DIRS,
    MountSpec,
    ensure_egg_state_dirs,
    phase_readonly_mounts,
)


class TestImplementReadonlyDirs:
    """Verify the constant is correct."""

    def test_contains_expected_dirs(self):
        assert "drafts" in _IMPLEMENT_READONLY_DIRS
        assert "contracts" in _IMPLEMENT_READONLY_DIRS
        assert "reviews" in _IMPLEMENT_READONLY_DIRS

    def test_is_tuple(self):
        assert isinstance(_IMPLEMENT_READONLY_DIRS, tuple)


class TestEnsureEggStateDirs:
    """Tests for ensure_egg_state_dirs()."""

    def test_creates_directories(self, tmp_path):
        """Creates drafts, contracts, reviews under .egg-state/."""
        repo_volumes = {"myrepo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes)

        for dirname in _IMPLEMENT_READONLY_DIRS:
            assert (tmp_path / ".egg-state" / dirname).is_dir()

    def test_idempotent(self, tmp_path):
        """Calling twice does not fail."""
        repo_volumes = {"myrepo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes)
        ensure_egg_state_dirs(repo_volumes)

        for dirname in _IMPLEMENT_READONLY_DIRS:
            assert (tmp_path / ".egg-state" / dirname).is_dir()

    def test_multiple_repos(self, tmp_path):
        """Creates directories for all repos in the mapping."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()

        repo_volumes = {"a": str(repo_a), "b": str(repo_b)}
        ensure_egg_state_dirs(repo_volumes)

        for repo in [repo_a, repo_b]:
            for dirname in _IMPLEMENT_READONLY_DIRS:
                assert (repo / ".egg-state" / dirname).is_dir()

    def test_creates_parent_egg_state_dir(self, tmp_path):
        """Creates .egg-state/ parent if it doesn't exist."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes)
        assert (tmp_path / ".egg-state").is_dir()

    def test_ownership_set_when_uid_gid_provided(self, tmp_path):
        """Directories get chown'd when uid and gid are given."""
        repo_volumes = {"repo": str(tmp_path)}
        with patch("os.chown") as mock_chown:
            ensure_egg_state_dirs(repo_volumes, uid=1000, gid=1000)
            assert mock_chown.call_count == len(_IMPLEMENT_READONLY_DIRS)
            for call_args in mock_chown.call_args_list:
                assert call_args[0][1] == 1000  # uid
                assert call_args[0][2] == 1000  # gid

    def test_no_chown_when_uid_none(self, tmp_path):
        """No chown when uid is None."""
        repo_volumes = {"repo": str(tmp_path)}
        with patch("os.chown") as mock_chown:
            ensure_egg_state_dirs(repo_volumes, uid=None, gid=1000)
            mock_chown.assert_not_called()

    def test_no_chown_when_gid_none(self, tmp_path):
        """No chown when gid is None."""
        repo_volumes = {"repo": str(tmp_path)}
        with patch("os.chown") as mock_chown:
            ensure_egg_state_dirs(repo_volumes, uid=1000, gid=None)
            mock_chown.assert_not_called()

    def test_empty_repo_volumes(self):
        """Empty repo_volumes does nothing."""
        ensure_egg_state_dirs({})


class TestPhaseReadonlyMounts:
    """Tests for phase_readonly_mounts()."""

    def test_implement_phase_returns_mounts(self, tmp_path):
        """Implement phase generates readonly mounts."""
        # Create the directories first
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement")

        assert len(mounts) == len(_IMPLEMENT_READONLY_DIRS)
        for mount in mounts:
            assert isinstance(mount, MountSpec)
            assert mount.mount_type == "bind"
            assert mount.readonly is True

    def test_implement_phase_mount_sources(self, tmp_path):
        """Mount sources point to host directories."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement")

        sources = {m.source for m in mounts}
        for dirname in _IMPLEMENT_READONLY_DIRS:
            assert str(tmp_path / ".egg-state" / dirname) in sources

    def test_implement_phase_mount_destinations(self, tmp_path):
        """Mount destinations use container base path."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement")

        destinations = {m.destination for m in mounts}
        for dirname in _IMPLEMENT_READONLY_DIRS:
            expected = f"/home/egg/repos/myrepo/.egg-state/{dirname}"
            assert expected in destinations

    def test_custom_container_base(self, tmp_path):
        """Custom container_base changes mount destinations."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(
            repo_volumes, "implement", container_base="/custom/path"
        )

        for mount in mounts:
            assert mount.destination.startswith("/custom/path/myrepo/")

    @pytest.mark.parametrize(
        "phase", ["plan", "refine", "pr", "review", None, ""]
    )
    def test_non_implement_phases_return_empty(self, phase, tmp_path):
        """Non-implement phases return no mounts."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, phase)
        assert mounts == []

    def test_missing_directories_skipped(self, tmp_path):
        """Directories that don't exist on host are skipped."""
        # Only create one of the three
        (tmp_path / ".egg-state" / "drafts").mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement")

        assert len(mounts) == 1
        assert mounts[0].source == str(tmp_path / ".egg-state" / "drafts")

    def test_empty_repo_volumes(self):
        """Empty repo_volumes returns empty list."""
        mounts = phase_readonly_mounts({}, "implement")
        assert mounts == []

    def test_multiple_repos(self, tmp_path):
        """Multiple repos generate mounts for each."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        for repo in [repo_a, repo_b]:
            for dirname in _IMPLEMENT_READONLY_DIRS:
                (repo / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"a": str(repo_a), "b": str(repo_b)}
        mounts = phase_readonly_mounts(repo_volumes, "implement")

        # Each repo should have 3 mounts
        assert len(mounts) == 2 * len(_IMPLEMENT_READONLY_DIRS)

    def test_none_phase_returns_empty(self):
        """None phase returns empty list."""
        mounts = phase_readonly_mounts({"r": "/tmp/r"}, None)
        assert mounts == []
