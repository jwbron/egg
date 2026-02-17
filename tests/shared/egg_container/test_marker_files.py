"""Tests for .egg-readonly marker file content and format.

Covers:
- Marker file content includes phase, directory, and explanation
- Marker file creation for each readonly directory
- Marker files not created for non-implement phases
- Marker file idempotency (overwrite on re-run)
- Marker file ownership via chown
"""

from unittest.mock import patch

from egg_container import (
    _IMPLEMENT_READONLY_DIRS,
    ensure_egg_state_dirs,
)


class TestMarkerFileContent:
    """Validate .egg-readonly marker file content format."""

    def test_marker_content_mentions_phase(self, tmp_path):
        """Marker file should mention the phase name."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            content = marker.read_text()
            assert "implement" in content

    def test_marker_content_mentions_directory(self, tmp_path):
        """Marker file should mention the directory name."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            content = marker.read_text()
            assert dirname in content

    def test_marker_content_mentions_readonly(self, tmp_path):
        """Marker file should explain the readonly restriction."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            content = marker.read_text()
            assert "readonly" in content.lower()

    def test_marker_content_explains_restriction(self, tmp_path):
        """Marker file should explain why the directory is restricted."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            content = marker.read_text()
            # Should mention that plan/contract artifacts are protected
            assert "plan" in content.lower() or "contract" in content.lower()
            # Should mention how to modify (use appropriate SDLC phase)
            assert "phase" in content.lower()

    def test_marker_content_is_nonempty(self, tmp_path):
        """Marker file should have meaningful content."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            content = marker.read_text()
            assert len(content) > 50, f"Marker content too short in {dirname}: {content!r}"

    def test_marker_files_for_all_readonly_dirs(self, tmp_path):
        """Every directory in _IMPLEMENT_READONLY_DIRS gets a marker file."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert marker.exists(), f"Missing .egg-readonly marker in {dirname}"

    def test_marker_includes_egg_state_path(self, tmp_path):
        """Marker mentions the full .egg-state/<dir>/ path."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            content = marker.read_text()
            assert f".egg-state/{dirname}/" in content


class TestMarkerFileEdgeCases:
    """Edge cases for marker file creation."""

    def test_marker_overwritten_on_rerun(self, tmp_path):
        """Running ensure_egg_state_dirs twice overwrites marker files."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        # Read original content
        marker = tmp_path / ".egg-state" / "drafts" / ".egg-readonly"
        first_content = marker.read_text()

        # Run again
        ensure_egg_state_dirs(repo_volumes, phase="implement")
        second_content = marker.read_text()

        # Content should be the same (idempotent)
        assert first_content == second_content

    def test_no_markers_for_plan_phase(self, tmp_path):
        """Plan phase should not create marker files."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="plan")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert not marker.exists()

    def test_no_markers_for_refine_phase(self, tmp_path):
        """Refine phase should not create marker files."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="refine")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert not marker.exists()

    def test_no_markers_for_pr_phase(self, tmp_path):
        """PR phase should not create marker files."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="pr")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert not marker.exists()

    def test_no_markers_for_empty_phase(self, tmp_path):
        """Empty string phase should not create marker files."""
        repo_volumes = {"repo": str(tmp_path)}
        ensure_egg_state_dirs(repo_volumes, phase="")

        for dirname in _IMPLEMENT_READONLY_DIRS:
            marker = tmp_path / ".egg-state" / dirname / ".egg-readonly"
            assert not marker.exists()

    def test_markers_in_multiple_repos(self, tmp_path):
        """Marker files created for all repos in the mapping."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()

        repo_volumes = {"a": str(repo_a), "b": str(repo_b)}
        ensure_egg_state_dirs(repo_volumes, phase="implement")

        for repo in [repo_a, repo_b]:
            for dirname in _IMPLEMENT_READONLY_DIRS:
                marker = repo / ".egg-state" / dirname / ".egg-readonly"
                assert marker.exists(), f"Missing marker in {repo}/{dirname}"

    def test_marker_file_chowned(self, tmp_path):
        """Marker files are chowned when uid/gid provided."""
        repo_volumes = {"repo": str(tmp_path)}
        with patch("os.chown") as mock_chown:
            ensure_egg_state_dirs(repo_volumes, uid=1000, gid=1000, phase="implement")

            # Should chown both directories and marker files
            marker_chown_calls = [
                c for c in mock_chown.call_args_list if ".egg-readonly" in str(c[0][0])
            ]
            assert len(marker_chown_calls) == len(_IMPLEMENT_READONLY_DIRS)

    def test_marker_not_chowned_without_uid(self, tmp_path):
        """No chown on marker files when uid is None."""
        repo_volumes = {"repo": str(tmp_path)}
        with patch("os.chown") as mock_chown:
            ensure_egg_state_dirs(repo_volumes, uid=None, gid=1000, phase="implement")
            mock_chown.assert_not_called()
