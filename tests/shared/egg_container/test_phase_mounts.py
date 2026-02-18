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
        assert "pipelines" in _IMPLEMENT_READONLY_DIRS
        assert "reviews" in _IMPLEMENT_READONLY_DIRS

    def test_has_four_dirs(self):
        """Must match the 4 blocked_patterns in phase-permissions.json for implement."""
        assert len(_IMPLEMENT_READONLY_DIRS) == 4

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
            # Only directory chowns (no marker files without phase)
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

    def test_marker_files_created_for_implement_phase(self, tmp_path):
        """Marker files are placed in readonly dirs during implement phase."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert marker.exists(), f"Missing marker in {dirname}"
            content = marker.read_text()
            assert "implement" in content
            assert dirname in content
            assert "readonly" in content

    def test_no_marker_files_without_phase(self, tmp_path):
        """No marker files created when phase is None."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes)

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert not marker.exists()

    def test_no_marker_files_for_non_implement_phase(self, tmp_path):
        """No marker files created for non-implement phases."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="plan")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert not marker.exists()

    def test_marker_files_chowned_when_uid_gid_provided(self, tmp_path):
        """Marker files get chown'd when uid and gid are given."""
        repo_volumes = {"repo": str(tmp_path)}
        with patch("os.chown") as mock_chown:
            ensure_egg_state_dirs(repo_volumes, uid=1000, gid=1000, phase="implement")
            # 3 directory chowns + 3 marker file chowns
            assert mock_chown.call_count == 2 * len(_IMPLEMENT_READONLY_DIRS)


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
        mounts = phase_readonly_mounts(repo_volumes, "implement", container_base="/custom/path")

        for mount in mounts:
            assert mount.destination.startswith("/custom/path/myrepo/")

    @pytest.mark.parametrize("phase", ["plan", "refine", "pr", "review", None, ""])
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

    def test_local_volumes_used_for_existence_check(self, tmp_path):
        """local_volumes paths are used for is_dir() but mount sources come from repo_volumes."""
        # Create dirs under tmp_path (the "local" path)
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        host_path = "/home/jwies/.egg-worktrees/some-repo"
        repo_volumes = {"myrepo": host_path}
        local_volumes = {"myrepo": str(tmp_path)}

        mounts = phase_readonly_mounts(repo_volumes, "implement", local_volumes=local_volumes)

        # Should find all dirs via local_volumes
        assert len(mounts) == len(_IMPLEMENT_READONLY_DIRS)
        # Mount sources must use host paths (repo_volumes), not local paths
        for mount in mounts:
            assert mount.source.startswith(host_path)
            assert str(tmp_path) not in mount.source

    def test_local_volumes_none_falls_back_to_repo_volumes(self, tmp_path):
        """When local_volumes is None, repo_volumes paths are used for checks."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement", local_volumes=None)

        assert len(mounts) == len(_IMPLEMENT_READONLY_DIRS)

    def test_local_volumes_missing_dir_skipped(self, tmp_path):
        """Dirs missing from local_volumes path are skipped even if host path exists."""
        # Don't create any dirs under tmp_path
        host_path = "/some/host/path"
        repo_volumes = {"myrepo": host_path}
        local_volumes = {"myrepo": str(tmp_path)}

        mounts = phase_readonly_mounts(repo_volumes, "implement", local_volumes=local_volumes)
        assert mounts == []

    @pytest.mark.parametrize("role", [
        "reviewer_code", "reviewer_contract", "reviewer_agent_design",
        "reviewer_refine", "reviewer_plan", "reviewer",
    ])
    def test_reviewer_roles_skip_reviews_readonly(self, role, tmp_path):
        """Reviewer agents are exempted from the reviews/ readonly mount."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement", agent_role=role)

        destinations = {m.destination for m in mounts}
        assert "/home/egg/repos/myrepo/.egg-state/reviews" not in destinations
        # Other dirs still readonly
        assert "/home/egg/repos/myrepo/.egg-state/drafts" in destinations
        assert "/home/egg/repos/myrepo/.egg-state/contracts" in destinations
        assert "/home/egg/repos/myrepo/.egg-state/pipelines" in destinations
        assert len(mounts) == len(_IMPLEMENT_READONLY_DIRS) - 1

    @pytest.mark.parametrize("role", ["coder", "tester", "integrator", "documenter"])
    def test_non_reviewer_roles_keep_reviews_readonly(self, role, tmp_path):
        """Non-reviewer agents still get reviews/ mounted readonly."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement", agent_role=role)

        destinations = {m.destination for m in mounts}
        assert "/home/egg/repos/myrepo/.egg-state/reviews" in destinations
        assert len(mounts) == len(_IMPLEMENT_READONLY_DIRS)

    def test_no_role_keeps_reviews_readonly(self, tmp_path):
        """No agent_role (default) keeps reviews/ readonly."""
        for dirname in _IMPLEMENT_READONLY_DIRS:
            (tmp_path / ".egg-state" / dirname).mkdir(parents=True)

        repo_volumes = {"myrepo": str(tmp_path)}
        mounts = phase_readonly_mounts(repo_volumes, "implement", agent_role=None)

        destinations = {m.destination for m in mounts}
        assert "/home/egg/repos/myrepo/.egg-state/reviews" in destinations
