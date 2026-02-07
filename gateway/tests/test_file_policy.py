"""Tests for file protection policy logic."""

# conftest.py loads the modules via importlib
from file_policy import (
    FileProtectionPolicy,
    ProtectedFileConfig,
    file_matches_pattern,
    lines_overlap,
    parse_line_ranges,
    parse_unified_diff,
)


class TestParseLineRanges:
    """Tests for parse_line_ranges function."""

    def test_none_input(self):
        assert parse_line_ranges(None) is None

    def test_empty_list(self):
        assert parse_line_ranges([]) is None

    def test_single_integer(self):
        result = parse_line_ranges([50])
        assert result == [(50, 50)]

    def test_single_string_integer(self):
        result = parse_line_ranges(["50"])
        assert result == [(50, 50)]

    def test_range_string(self):
        result = parse_line_ranges(["50-55"])
        assert result == [(50, 55)]

    def test_mixed_formats(self):
        result = parse_line_ranges([50, "52-55", "60"])
        assert result == [(50, 50), (52, 55), (60, 60)]

    def test_invalid_range_ignored(self):
        result = parse_line_ranges(["invalid-range"])
        assert result is None

    def test_invalid_integer_ignored(self):
        result = parse_line_ranges(["not-a-number"])
        assert result is None


class TestParseUnifiedDiff:
    """Tests for parse_unified_diff function."""

    def test_empty_diff(self):
        result = parse_unified_diff("")
        assert result == {}

    def test_single_file_modification(self):
        diff = """diff --git a/test.py b/test.py
index abc123..def456 100644
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 line1
+new_line
 line2
 line3
"""
        result = parse_unified_diff(diff)
        assert "test.py" in result
        assert result["test.py"] == [2]  # Line 2 was added

    def test_multiple_files(self):
        diff = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,2 +1,3 @@
 existing
+added_line
 more
diff --git a/file2.py b/file2.py
--- a/file2.py
+++ b/file2.py
@@ -5,2 +5,3 @@
 context
+another_add
 more_context
"""
        result = parse_unified_diff(diff)
        assert "file1.py" in result
        assert "file2.py" in result
        assert result["file1.py"] == [2]
        assert result["file2.py"] == [6]

    def test_multiple_hunks(self):
        diff = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,2 +1,3 @@
 line1
+add1
 line2
@@ -50,2 +51,3 @@
 line50
+add2
 line51
"""
        result = parse_unified_diff(diff)
        assert "test.py" in result
        # Line 2 in first hunk, line 52 in second hunk (starts at 51, context line, then add)
        assert 2 in result["test.py"]
        assert 52 in result["test.py"]

    def test_deleted_lines_not_counted(self):
        diff = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,4 +1,3 @@
 line1
-deleted_line
 line2
 line3
"""
        result = parse_unified_diff(diff)
        assert "test.py" in result
        # No lines added, so empty list
        assert result["test.py"] == []


class TestFileMatchesPattern:
    """Tests for file_matches_pattern function."""

    def test_exact_match(self):
        assert file_matches_pattern("test.py", "test.py")

    def test_exact_mismatch(self):
        assert not file_matches_pattern("test.py", "other.py")

    def test_glob_star(self):
        assert file_matches_pattern("test.py", "*.py")
        assert not file_matches_pattern("test.js", "*.py")

    def test_glob_double_star(self):
        assert file_matches_pattern("src/test.py", "**/*.py")
        assert file_matches_pattern("a/b/c/test.py", "**/*.py")

    def test_glob_directory_pattern(self):
        assert file_matches_pattern(".github/workflows/ci.yml", ".github/workflows/*.yml")
        assert not file_matches_pattern(".github/workflows/ci.yaml", ".github/workflows/*.yml")

    def test_glob_question_mark(self):
        assert file_matches_pattern("test1.py", "test?.py")
        assert not file_matches_pattern("test10.py", "test?.py")


class TestLinesOverlap:
    """Tests for lines_overlap function."""

    def test_empty_modified(self):
        result = lines_overlap([], [(1, 10)])
        assert result == []

    def test_empty_protected(self):
        result = lines_overlap([5, 6, 7], [])
        assert result == []

    def test_no_overlap(self):
        result = lines_overlap([1, 2, 3], [(10, 20)])
        assert result == []

    def test_full_overlap(self):
        result = lines_overlap([5, 6, 7], [(1, 10)])
        assert result == [5, 6, 7]

    def test_partial_overlap(self):
        result = lines_overlap([8, 9, 10, 11, 12], [(10, 15)])
        assert result == [10, 11, 12]

    def test_multiple_ranges(self):
        result = lines_overlap([5, 15, 25], [(1, 10), (20, 30)])
        assert result == [5, 25]


class TestProtectedFileConfig:
    """Tests for ProtectedFileConfig dataclass."""

    def test_default_values(self):
        config = ProtectedFileConfig(path="test.py")
        assert config.path == "test.py"
        assert config.lines is None
        assert config.level == "immutable"
        assert config.reason == ""

    def test_all_values(self):
        config = ProtectedFileConfig(
            path="test.py",
            lines=[(1, 10)],
            level="warn_on_pr",
            reason="Test reason",
        )
        assert config.path == "test.py"
        assert config.lines == [(1, 10)]
        assert config.level == "warn_on_pr"
        assert config.reason == "Test reason"


class TestFileProtectionPolicy:
    """Tests for FileProtectionPolicy class."""

    def test_empty_policy(self):
        policy = FileProtectionPolicy([])
        file_changes = {"test.py": [1, 2, 3]}
        result = policy.check_file_modifications(file_changes)
        assert result.allowed
        assert result.violations == []

    def test_entire_file_protected(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(path="protected.py", reason="Critical file")
        ])
        file_changes = {"protected.py": [1, 2, 3]}
        result = policy.check_file_modifications(file_changes)
        assert not result.allowed
        assert len(result.violations) == 1
        assert result.violations[0].file == "protected.py"
        assert result.violations[0].lines is None  # Entire file

    def test_specific_lines_protected(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(
                path="config.py",
                lines=[(50, 55)],
                reason="Coverage thresholds",
            )
        ])
        # Modify lines outside protected range
        result = policy.check_file_modifications({"config.py": [1, 2, 3]})
        assert result.allowed

        # Modify lines inside protected range
        result = policy.check_file_modifications({"config.py": [52, 53]})
        assert not result.allowed
        assert result.violations[0].lines == [52, 53]

    def test_glob_pattern_matching(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(path="*.coveragerc", reason="Coverage config")
        ])
        result = policy.check_file_modifications({".coveragerc": [1]})
        assert not result.allowed

        result = policy.check_file_modifications({"other.py": [1]})
        assert result.allowed

    def test_warn_on_pr_level(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(
                path="docs.md",
                level="warn_on_pr",
                reason="Documentation",
            )
        ])
        result = policy.check_file_modifications({"docs.md": [1]})
        # warn_on_pr allows the push but adds to warnings
        assert result.allowed
        assert len(result.violations) == 0
        assert len(result.warnings) == 1

    def test_log_only_level(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(
                path="audit.py",
                level="log_only",
                reason="Audit tracking",
            )
        ])
        result = policy.check_file_modifications({"audit.py": [1]})
        # log_only allows the push and doesn't add warnings
        assert result.allowed
        assert len(result.violations) == 0
        assert len(result.warnings) == 0

    def test_multiple_violations(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(path="file1.py", reason="Reason 1"),
            ProtectedFileConfig(path="file2.py", reason="Reason 2"),
        ])
        result = policy.check_file_modifications({
            "file1.py": [1],
            "file2.py": [2],
            "file3.py": [3],  # Not protected
        })
        assert not result.allowed
        assert len(result.violations) == 2

    def test_check_diff_for_violations(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(path="protected.py", reason="Critical")
        ])
        diff = """diff --git a/protected.py b/protected.py
--- a/protected.py
+++ b/protected.py
@@ -1,2 +1,3 @@
 line1
+new_line
 line2
"""
        result = policy.check_diff_for_violations(diff)
        assert not result.allowed
        assert len(result.violations) == 1

    def test_to_dict(self):
        policy = FileProtectionPolicy([
            ProtectedFileConfig(path="test.py", reason="Test reason")
        ])
        result = policy.check_file_modifications({"test.py": [1]})
        result_dict = result.to_dict()
        assert result_dict["allowed"] is False
        assert len(result_dict["violations"]) == 1
        assert result_dict["violations"][0]["file"] == "test.py"
        assert result_dict["violations"][0]["reason"] == "Test reason"
        assert result_dict["violations"][0]["level"] == "immutable"


class TestIntegration:
    """Integration tests for common use cases."""

    def test_coverage_threshold_protection(self):
        """Test the specific use case from issue #200."""
        policy = FileProtectionPolicy([
            ProtectedFileConfig(
                path=".coveragerc",
                reason="Test coverage configuration",
            ),
            ProtectedFileConfig(
                path="pyproject.toml",
                lines=[(50, 55)],
                reason="Coverage threshold settings",
            ),
        ])

        # Modifying .coveragerc should be blocked
        result = policy.check_file_modifications({".coveragerc": [5]})
        assert not result.allowed

        # Modifying coverage thresholds in pyproject.toml should be blocked
        result = policy.check_file_modifications({"pyproject.toml": [52]})
        assert not result.allowed

        # Modifying other lines in pyproject.toml should be allowed
        result = policy.check_file_modifications({"pyproject.toml": [10]})
        assert result.allowed

    def test_ci_workflow_protection(self):
        """Test protection of CI workflow files."""
        policy = FileProtectionPolicy([
            ProtectedFileConfig(
                path=".github/workflows/*.yml",
                reason="CI workflow configuration",
            )
        ])

        result = policy.check_file_modifications({
            ".github/workflows/ci.yml": [5],
        })
        assert not result.allowed

        result = policy.check_file_modifications({
            ".github/other-file.txt": [1],
        })
        assert result.allowed

    def test_gateway_policy_protection(self):
        """Test protection of critical gateway policy code."""
        policy = FileProtectionPolicy([
            ProtectedFileConfig(
                path="gateway/policy.py",
                lines=[(742, 761)],
                level="immutable",
                reason="Merge blocking policy - critical security",
            )
        ])

        # Modifying the merge block lines should be blocked
        result = policy.check_file_modifications({
            "gateway/policy.py": [750],
        })
        assert not result.allowed

        # Modifying other parts of the file should be allowed
        result = policy.check_file_modifications({
            "gateway/policy.py": [100],
        })
        assert result.allowed
