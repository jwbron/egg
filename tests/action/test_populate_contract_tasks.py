"""Tests for action/populate-contract-tasks.py."""

import sys
from pathlib import Path

# Add action directory to path so we can import the module
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "action"))

from importlib import import_module

# Import the module dynamically since it has a hyphenated filename
spec = import_module("populate-contract-tasks")
extract_acceptance_criteria = spec.extract_acceptance_criteria


class TestExtractExitCriteria:
    """Tests for extracting exit criteria from plan content."""

    def test_extracts_bold_exit_criteria(self):
        """Test extracting **Exit criteria**: format."""
        content = """
## Phase 1: Setup

Some description here.

**Exit criteria**: All tests pass and linting succeeds
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert criteria[0]["id"] == "ac-1"
        assert criteria[0]["description"] == "All tests pass and linting succeeds"
        assert criteria[0]["verified"] is False

    def test_extracts_non_bold_exit_criteria(self):
        """Test extracting Exit criteria: format (without bold)."""
        content = """
## Phase 1: Setup

Exit criteria: Tests are green
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "Tests are green" in criteria[0]["description"]

    def test_extracts_multiple_exit_criteria(self):
        """Test extracting exit criteria from multiple phases."""
        content = """
## Phase 1: Setup
**Exit criteria**: Phase 1 complete

## Phase 2: Implementation
**Exit criteria**: Phase 2 done
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 2
        assert criteria[0]["id"] == "ac-1"
        assert criteria[1]["id"] == "ac-2"

    def test_multiline_exit_criteria(self):
        """Test extracting multi-line exit criteria."""
        content = """
## Phase 1: Setup

**Exit criteria**: All tests pass
and the build succeeds
and documentation is updated

## Phase 2: Implementation
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        # Should capture all the continuation lines
        assert "tests pass" in criteria[0]["description"].lower()
        assert "build succeeds" in criteria[0]["description"]

    def test_exit_criteria_ends_at_double_newline(self):
        """Test that exit criteria capture ends at double newline."""
        content = """
**Exit criteria**: First criterion only

Some unrelated text that should not be captured.
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "unrelated" not in criteria[0]["description"]

    def test_exit_criteria_ends_at_next_header(self):
        """Test that exit criteria capture ends at next section header."""
        content = """
**Exit criteria**: Tests pass

## Next Section

This should not be captured.
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "Next Section" not in criteria[0]["description"]


class TestExtractTestStrategy:
    """Tests for extracting test strategy items."""

    def test_extracts_test_strategy_bullets(self):
        """Test extracting bullet points from Test Strategy section."""
        content = """
## Test Strategy

- **Unit tests**: Test the parser functions
- **Integration tests**: Verify end-to-end workflow
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 2
        # Note: leading ** is stripped by lstrip("-* ")
        assert "Test:" in criteria[0]["description"]
        assert "Unit tests" in criteria[0]["description"]
        assert "Test the parser functions" in criteria[0]["description"]
        assert "Integration tests" in criteria[1]["description"]

    def test_ignores_h3_test_strategy(self):
        """Test that ### Test Strategy is NOT matched (only ## Test Strategy)."""
        content = """
### Test Strategy

- **Unit tests**: Should not be captured
- **Integration tests**: Should not be captured

## Test Strategy

- **Real tests**: Should be captured
"""
        criteria = extract_acceptance_criteria(content)
        # Should only capture from the ## section, not ###
        assert len(criteria) == 1
        assert "Real tests" in criteria[0]["description"]

    def test_test_strategy_ends_at_next_h2(self):
        """Test that Test Strategy section ends at next ## header."""
        content = """
## Test Strategy

- **Unit tests**: Parser tests

## Next Section

- **Not tests**: Should not be captured
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "Parser tests" in criteria[0]["description"]

    def test_skips_template_placeholders(self):
        """Test that template placeholders like [placeholder] are skipped."""
        content = """
## Test Strategy

- [Add your tests here]
- **Unit tests**: Actual tests
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "Actual tests" in criteria[0]["description"]

    def test_skips_short_items(self):
        """Test that very short bullet points are skipped."""
        content = """
## Test Strategy

- Short
- **Unit tests**: This is a longer description that should be captured
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "longer description" in criteria[0]["description"]

    def test_asterisk_bullets(self):
        """Test that * bullets are also captured."""
        content = """
## Test Strategy

* **Unit tests**: Test with asterisk bullet
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "asterisk bullet" in criteria[0]["description"]


class TestCombinedExtraction:
    """Tests for extracting both exit criteria and test strategy."""

    def test_extracts_both_types(self):
        """Test extracting exit criteria and test strategy together."""
        content = """
## Phase 1: Setup

**Exit criteria**: All tests pass

## Test Strategy

- **Unit tests**: Test the parser
- **Integration tests**: Test end-to-end
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 3
        # First is exit criteria
        assert criteria[0]["id"] == "ac-1"
        assert "tests pass" in criteria[0]["description"].lower()
        # Next are test strategy items
        assert criteria[1]["id"] == "ac-2"
        assert "Test:" in criteria[1]["description"]
        assert criteria[2]["id"] == "ac-3"

    def test_empty_content(self):
        """Test with empty content."""
        criteria = extract_acceptance_criteria("")
        assert len(criteria) == 0

    def test_no_matching_patterns(self):
        """Test content with no matching patterns."""
        content = """
## Introduction

This is just some intro text with no exit criteria or test strategy.
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 0

    def test_case_insensitive_matching(self):
        """Test that matching is case-insensitive."""
        content = """
**EXIT CRITERIA**: Uppercase works

## TEST STRATEGY

- **Unit tests**: Uppercase section
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 2


class TestEdgeCases:
    """Tests for edge cases and robustness."""

    def test_exit_criteria_with_leading_dash(self):
        """Test exit criteria that starts with a dash."""
        content = """
**Exit criteria**: - All tests pass
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "All tests pass" in criteria[0]["description"]

    def test_whitespace_handling(self):
        """Test that extra whitespace is handled correctly."""
        content = """
**Exit criteria**:    Lots of spaces

"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert criteria[0]["description"] == "Lots of spaces"

    def test_test_strategy_at_end_of_file(self):
        """Test that Test Strategy at end of file is captured."""
        content = """
## Test Strategy

- **Unit tests**: Last item in file"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "Last item" in criteria[0]["description"]

    def test_h4_test_strategy_not_matched(self):
        """Test that #### Test Strategy is NOT matched."""
        content = """
#### Test Strategy

- **Deep nested**: Should not be captured

## Test Strategy

- **Correct level**: Should be captured
"""
        criteria = extract_acceptance_criteria(content)
        assert len(criteria) == 1
        assert "Correct level" in criteria[0]["description"]
