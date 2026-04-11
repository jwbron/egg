"""Tests for egg_harness.prompt — system prompt assembly."""

from __future__ import annotations

from egg_harness.prompt import build_system_prompt


class TestBuildSystemPrompt:
    """Tests for build_system_prompt()."""

    def test_static_strings_joined(self):
        result = build_system_prompt(["Part A", "Part B"])
        assert result == "Part A\n\n---\n\nPart B"

    def test_callable_sources_invoked(self):
        result = build_system_prompt([lambda: "dynamic"])
        assert result == "dynamic"

    def test_mixed_string_and_callable(self):
        result = build_system_prompt(["static", lambda: "dynamic", "end"])
        assert result == "static\n\n---\n\ndynamic\n\n---\n\nend"

    def test_empty_string_filtered(self):
        result = build_system_prompt(["A", "", "B"])
        assert result == "A\n\n---\n\nB"

    def test_none_from_callable_filtered(self):
        result = build_system_prompt(["A", lambda: None, "B"])
        assert result == "A\n\n---\n\nB"

    def test_empty_list_returns_empty_string(self):
        assert build_system_prompt([]) == ""

    def test_all_empty_sources_returns_empty(self):
        result = build_system_prompt(["", lambda: None, ""])
        assert result == ""

    def test_single_source_no_separator(self):
        result = build_system_prompt(["only one"])
        assert result == "only one"
        assert "---" not in result

    def test_separator_format(self):
        result = build_system_prompt(["A", "B"])
        assert "\n\n---\n\n" in result

    def test_three_sources_two_separators(self):
        result = build_system_prompt(["A", "B", "C"])
        assert result.count("\n\n---\n\n") == 2
