"""Tests for filter_allowed_files function in agent_restrictions.

Covers:
- Basic partitioning of files into allowed/blocked lists
- All standard agent roles (coder, tester, documenter)
- Unknown roles (backwards-compatible: all files allowed)
- Empty file lists
- Mixed allowed/blocked files
- Path traversal attempts
- Case sensitivity of role names
- Consistency with check_agent_file_access results

Related: issue #1470 — Gateway auto-filter disallowed files on push
"""

import pytest
from agent_restrictions import (
    AgentRole,
    check_agent_file_access,
    filter_allowed_files,
)


class TestFilterAllowedFilesBasic:
    """Basic behavior of filter_allowed_files."""

    def test_returns_two_lists(self):
        """filter_allowed_files returns a tuple of two lists."""
        allowed, blocked = filter_allowed_files("coder", ["src/main.py"])
        assert isinstance(allowed, list)
        assert isinstance(blocked, list)

    def test_empty_files_list(self):
        """Empty file list returns two empty lists."""
        allowed, blocked = filter_allowed_files("coder", [])
        assert allowed == []
        assert blocked == []

    def test_all_files_allowed(self):
        """When all files are within role scope, blocked is empty."""
        files = ["src/main.py", "gateway/utils.py", "config.yml"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == files
        assert blocked == []

    def test_all_files_blocked(self):
        """When all files are outside role scope, allowed is empty."""
        files = ["tests/test_foo.py", "docs/guide.md"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert set(blocked) == set(files)

    def test_mixed_files(self):
        """Mix of allowed and blocked files are correctly partitioned."""
        files = ["src/main.py", "tests/test_foo.py", "gateway/handler.py"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert "src/main.py" in allowed
        assert "gateway/handler.py" in allowed
        assert "tests/test_foo.py" in blocked

    def test_preserves_order(self):
        """Order of files is preserved in both lists."""
        files = ["a.py", "tests/test_b.py", "c.py", "docs/d.md"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == ["a.py", "c.py"]
        assert blocked == ["tests/test_b.py", "docs/d.md"]


class TestFilterAllowedFilesUnknownRole:
    """Unknown roles should treat all files as allowed."""

    def test_unknown_role_allows_all(self):
        """Unknown role returns all files as allowed, none blocked."""
        files = ["src/main.py", "tests/test_foo.py", "docs/guide.md"]
        allowed, blocked = filter_allowed_files("nonexistent_role", files)
        assert allowed == files
        assert blocked == []

    def test_empty_role_string(self):
        """Empty string role returns all files as allowed."""
        files = ["src/main.py"]
        allowed, blocked = filter_allowed_files("", files)
        assert allowed == files
        assert blocked == []


class TestFilterAllowedFilesCoderRole:
    """Coder role: source and config allowed, tests/docs/contracts blocked."""

    def test_coder_allows_source_code(self):
        files = ["src/main.py", "gateway/handler.py", "shared/utils.py"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == files
        assert blocked == []

    def test_coder_blocks_test_files(self):
        files = ["tests/test_main.py", "gateway/tests/test_handler.py"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert blocked == files

    def test_coder_blocks_test_file_patterns(self):
        """Coder is blocked from test_*.py and *_test.py patterns."""
        files = ["src/test_foo.py", "src/foo_test.py"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert set(blocked) == set(files)

    def test_coder_blocks_docs(self):
        files = ["docs/guide.md", "docs/architecture.md"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert blocked == files

    def test_coder_blocks_markdown(self):
        files = ["README.md", "CHANGELOG.md"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert blocked == files

    def test_coder_blocks_contracts(self):
        files = [".egg-state/contracts/contract.json"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert blocked == files

    def test_coder_allows_config(self):
        files = ["pyproject.toml", "config.yml", "package.json"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == files
        assert blocked == []

    def test_coder_allows_agent_outputs(self):
        files = [".egg-state/agent-outputs/coder.json"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == files
        assert blocked == []


class TestFilterAllowedFilesTesterRole:
    """Tester role: test files allowed, source/docs/contracts blocked."""

    def test_tester_allows_test_directories(self):
        files = ["tests/test_main.py", "gateway/tests/test_handler.py"]
        allowed, blocked = filter_allowed_files("tester", files)
        assert allowed == files
        assert blocked == []

    def test_tester_allows_test_patterns(self):
        files = ["src/test_foo.py", "src/foo_test.py"]
        allowed, blocked = filter_allowed_files("tester", files)
        assert allowed == files
        assert blocked == []

    def test_tester_allows_conftest(self):
        files = ["tests/conftest.py", "gateway/tests/conftest.py"]
        allowed, blocked = filter_allowed_files("tester", files)
        assert allowed == files
        assert blocked == []

    def test_tester_blocks_docs(self):
        files = ["docs/guide.md"]
        allowed, blocked = filter_allowed_files("tester", files)
        assert allowed == []
        assert blocked == files

    def test_tester_blocks_contracts(self):
        files = [".egg-state/contracts/contract.json"]
        allowed, blocked = filter_allowed_files("tester", files)
        assert allowed == []
        assert blocked == files


class TestFilterAllowedFilesDocumenterRole:
    """Documenter role: docs/markdown allowed, source/tests/contracts blocked."""

    def test_documenter_allows_docs(self):
        files = ["docs/guide.md", "docs/architecture.md"]
        allowed, blocked = filter_allowed_files("documenter", files)
        assert allowed == files
        assert blocked == []

    def test_documenter_allows_readme(self):
        files = ["README.md", "shared/README.md"]
        allowed, blocked = filter_allowed_files("documenter", files)
        assert allowed == files
        assert blocked == []

    def test_documenter_blocks_source(self):
        files = ["src/main.py", "gateway/handler.py"]
        allowed, blocked = filter_allowed_files("documenter", files)
        assert allowed == []
        assert blocked == files

    def test_documenter_blocks_tests(self):
        files = ["tests/test_main.py"]
        allowed, blocked = filter_allowed_files("documenter", files)
        assert allowed == []
        assert blocked == files


class TestFilterAllowedFilesConsistency:
    """filter_allowed_files results should be consistent with check_agent_file_access."""

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.OVERSEER,
            AgentRole.AUTOFIXER,
            AgentRole.CONFLICT_RESOLVER,
            AgentRole.INSPECTOR,
        ],
    )
    def test_blocked_files_match_check_agent_file_access(self, role):
        """Blocked files from filter should match check_agent_file_access."""
        files = [
            "src/main.py",
            "tests/test_foo.py",
            "docs/guide.md",
            ".egg-state/contracts/c.json",
            ".egg-state/agent-outputs/out.json",
        ]
        _, filter_blocked = filter_allowed_files(role, files)
        _, access_blocked, _ = check_agent_file_access(role, files)
        assert set(filter_blocked) == set(access_blocked)

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
        ],
    )
    def test_allowed_plus_blocked_equals_input(self, role):
        """All input files should appear in exactly one of allowed or blocked."""
        files = [
            "src/main.py",
            "tests/test_foo.py",
            "docs/guide.md",
            "config.yml",
            ".egg-state/contracts/c.json",
        ]
        allowed, blocked = filter_allowed_files(role, files)
        assert sorted(allowed + blocked) == sorted(files)

    def test_no_duplicates_in_output(self):
        """No file should appear in both allowed and blocked."""
        files = ["src/main.py", "tests/test_foo.py", "docs/guide.md"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert set(allowed).isdisjoint(set(blocked))


class TestFilterAllowedFilesPathTraversal:
    """Path traversal attempts should be treated as blocked."""

    def test_traversal_paths_blocked(self):
        """Path traversal should result in file being blocked."""
        files = ["../../etc/passwd"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert blocked == files

    def test_traversal_in_mixed_files(self):
        """Path traversal files blocked, normal files processed normally."""
        files = ["src/main.py", "../../etc/passwd"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert "src/main.py" in allowed
        assert "../../etc/passwd" in blocked


class TestFilterAllowedFilesEdgeCases:
    """Edge cases for filter_allowed_files."""

    def test_duplicate_files_in_input(self):
        """Duplicate files in input appear in output as duplicates."""
        files = ["src/main.py", "src/main.py"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert len(allowed) == 2

    def test_files_creates_new_list(self):
        """filter_allowed_files should not modify the input list."""
        files = ["src/main.py", "tests/test_foo.py"]
        original = list(files)
        filter_allowed_files("coder", files)
        assert files == original

    def test_single_file_allowed(self):
        allowed, blocked = filter_allowed_files("coder", ["src/main.py"])
        assert allowed == ["src/main.py"]
        assert blocked == []

    def test_single_file_blocked(self):
        allowed, blocked = filter_allowed_files("coder", ["tests/test_foo.py"])
        assert allowed == []
        assert blocked == ["tests/test_foo.py"]
