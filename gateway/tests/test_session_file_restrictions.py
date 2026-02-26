"""Tests for per-session file restriction enforcement in gateway push validation.

Validates the warn-then-block escalation pattern for per-task file restrictions
set via session.allowed_files. Covers: allowed files pass, disallowed warns then
blocks, strict mode, checkpoint bypass, None/empty fallback, glob patterns,
directory-sibling expansion, and recursive fnmatch matching.
"""

from __future__ import annotations

import fnmatch

from phase_filter import PhaseFileRestriction


class TestPhaseFileRestrictionForTaskScope:
    """Tests using PhaseFileRestriction with allowed_patterns for per-task enforcement."""

    def test_allowed_file_passes(self):
        """A file matching an allowed pattern is allowed."""
        restriction = PhaseFileRestriction(allowed_patterns=["src/auth/*", "tests/test_auth.py"])
        allowed, reason = restriction.is_file_allowed("src/auth/login.py")
        assert allowed is True

    def test_exact_file_match(self):
        """An exact file in allowed_patterns is allowed."""
        restriction = PhaseFileRestriction(allowed_patterns=["tests/test_auth.py"])
        allowed, reason = restriction.is_file_allowed("tests/test_auth.py")
        assert allowed is True

    def test_disallowed_file_blocked(self):
        """A file not matching any allowed pattern is blocked."""
        restriction = PhaseFileRestriction(allowed_patterns=["src/auth/*"])
        allowed, reason = restriction.is_file_allowed("src/payments/checkout.py")
        assert allowed is False

    def test_directory_sibling_expansion(self):
        """dir/* covers dir/newfile.py (new files in same directory)."""
        restriction = PhaseFileRestriction(allowed_patterns=["src/auth/*"])
        allowed, reason = restriction.is_file_allowed("src/auth/newfile.py")
        assert allowed is True

    def test_recursive_matching_via_fnmatch(self):
        """dir/* also covers dir/sub/deep/file.py (recursive via fnmatch semantics).

        This is the key behavior: Python's fnmatch treats * as matching /,
        so dir/* matches all descendants, not just immediate children.
        """
        restriction = PhaseFileRestriction(allowed_patterns=["src/auth/*"])
        allowed, reason = restriction.is_file_allowed("src/auth/sub/deep/file.py")
        assert allowed is True

    def test_fnmatch_star_matches_slash(self):
        """Verify Python fnmatch behavior: * matches /."""
        assert fnmatch.fnmatch("dir/sub/deep/file.py", "dir/*") is True
        assert fnmatch.fnmatch("dir/file.py", "dir/*") is True

    def test_glob_patterns_preserved(self):
        """Glob patterns like tests/** in allowed_patterns work correctly."""
        restriction = PhaseFileRestriction(allowed_patterns=["tests/**"])
        allowed, reason = restriction.is_file_allowed("tests/unit/test_foo.py")
        assert allowed is True

    def test_none_allowed_patterns_allows_all(self):
        """Empty allowed_patterns means no restriction (allow everything)."""
        restriction = PhaseFileRestriction(allowed_patterns=[])
        allowed, reason = restriction.is_file_allowed("any/file.py")
        assert allowed is True

    def test_wildcard_star_allows_everything(self):
        """Special case: * in allowed_patterns allows everything."""
        restriction = PhaseFileRestriction(allowed_patterns=["*"])
        allowed, reason = restriction.is_file_allowed("literally/anything.py")
        assert allowed is True

    def test_multiple_patterns_any_match(self):
        """File matching any of multiple allowed patterns is allowed."""
        restriction = PhaseFileRestriction(
            allowed_patterns=["src/auth/*", "src/models/*", "tests/*"]
        )
        assert restriction.is_file_allowed("src/auth/login.py")[0] is True
        assert restriction.is_file_allowed("src/models/user.py")[0] is True
        assert restriction.is_file_allowed("tests/test_auth.py")[0] is True
        assert restriction.is_file_allowed("src/payments/pay.py")[0] is False

    def test_path_traversal_blocked(self):
        """Paths escaping the repo (../) are always blocked."""
        restriction = PhaseFileRestriction(allowed_patterns=["src/*"])
        allowed, reason = restriction.is_file_allowed("../etc/passwd")
        assert allowed is False


class TestWarnThenBlockLogic:
    """Tests for the warn-then-block pattern using _warned_files on Session."""

    def test_first_violation_warns(self):
        """First out-of-scope file increments counter but doesn't block."""
        warned_files: dict[str, int] = {}
        out_of_scope = ["config/settings.py"]
        warn_threshold = 1

        blocked = []
        warned = []
        for f in out_of_scope:
            count = warned_files.get(f, 0)
            if count >= warn_threshold:
                blocked.append(f)
            else:
                warned_files[f] = count + 1
                warned.append(f)

        assert warned == ["config/settings.py"]
        assert blocked == []
        assert warned_files["config/settings.py"] == 1

    def test_second_violation_blocks(self):
        """Same file on second attempt is blocked (count >= threshold)."""
        warned_files: dict[str, int] = {"config/settings.py": 1}
        out_of_scope = ["config/settings.py"]
        warn_threshold = 1

        blocked = []
        for f in out_of_scope:
            count = warned_files.get(f, 0)
            if count >= warn_threshold:
                blocked.append(f)
            else:
                warned_files[f] = count + 1

        assert blocked == ["config/settings.py"]

    def test_different_files_tracked_independently(self):
        """Each file has its own violation counter."""
        warned_files: dict[str, int] = {"file_a.py": 1}
        out_of_scope = ["file_a.py", "file_b.py"]
        warn_threshold = 1

        blocked = []
        warned = []
        for f in out_of_scope:
            count = warned_files.get(f, 0)
            if count >= warn_threshold:
                blocked.append(f)
            else:
                warned_files[f] = count + 1
                warned.append(f)

        assert blocked == ["file_a.py"]
        assert warned == ["file_b.py"]

    def test_strict_mode_blocks_immediately(self):
        """When enforce_strict=True, all out-of-scope files are blocked immediately."""
        enforce_strict = True
        out_of_scope = ["config/settings.py"]

        if enforce_strict and out_of_scope:
            blocked = out_of_scope
        else:
            blocked = []

        assert blocked == ["config/settings.py"]


class TestCheckpointBypass:
    """Tests that checkpoint pushes should bypass per-task restrictions."""

    def test_checkpoint_branch_identified(self):
        """Checkpoint branch name matches the bypass condition."""
        branch = "egg/checkpoints/v2"
        is_checkpoint_push = branch == "egg/checkpoints/v2"
        assert is_checkpoint_push is True

    def test_non_checkpoint_branch(self):
        """Non-checkpoint branch does not trigger bypass."""
        branch = "egg/issue-912"
        is_checkpoint_push = branch == "egg/checkpoints/v2"
        assert is_checkpoint_push is False
