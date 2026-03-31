"""Tests for filter_agent_files convenience function in phase_filter.

Validates that filter_agent_files correctly delegates to
agent_restrictions.filter_allowed_files and returns consistent results.

Related: issue #1470 — Gateway auto-filter disallowed files on push
"""

import pytest
from agent_restrictions import filter_allowed_files
from phase_filter import filter_agent_files


class TestFilterAgentFilesDelegation:
    """filter_agent_files should produce identical results to filter_allowed_files."""

    @pytest.mark.parametrize(
        "role,files",
        [
            ("coder", ["src/main.py", "tests/test_foo.py", "docs/guide.md"]),
            ("tester", ["tests/test_main.py", "src/app.py"]),
            ("documenter", ["docs/index.md", "src/main.py"]),
            ("unknown_role", ["src/main.py", "tests/test_foo.py"]),
            ("coder", []),
            ("coder", ["src/main.py"]),
        ],
    )
    def test_matches_filter_allowed_files(self, role, files):
        """filter_agent_files should return same results as filter_allowed_files."""
        expected_allowed, expected_blocked = filter_allowed_files(role, files)
        actual_allowed, actual_blocked = filter_agent_files(role, files)
        assert actual_allowed == expected_allowed
        assert actual_blocked == expected_blocked

    def test_returns_tuple_of_lists(self):
        """Return type should be a tuple of two lists."""
        result = filter_agent_files("coder", ["src/main.py"])
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], list)
        assert isinstance(result[1], list)
