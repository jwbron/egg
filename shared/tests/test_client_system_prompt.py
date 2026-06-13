"""Tests for the shared system-prompt addendum append helper (#3175 review).

``egg_agent.client._append_system_prompt_addendum`` is the single shape
behind both the MCP-tool ``SYSTEM_PROMPT_NUDGE`` append and the
LiteLLM-route guidance append. These pin the data-loss-avoidance contract:
a plain-str prompt is extended, a ``None`` prompt becomes the addendum, and
the non-string preset / file forms (which have no defined append semantics)
are preserved unchanged with ``appended=False`` so the caller logs a skip
rather than silently dropping either the caller's prompt or the addendum.
"""

from egg_agent.client import _append_system_prompt_addendum


class _FakePreset:
    """Stand-in for SystemPromptPreset / SystemPromptFile (truthy, non-str)."""


class TestAppendSystemPromptAddendum:
    def test_str_prompt_is_extended_after_blank_line(self):
        new_prompt, appended = _append_system_prompt_addendum("base prompt", "ADD")
        assert appended is True
        assert new_prompt == "base prompt\n\nADD"

    def test_trailing_whitespace_stripped_before_join(self):
        # rstrip keeps the join stable regardless of the caller's trailing
        # newlines — the cacheable prefix must not wobble on whitespace.
        new_prompt, appended = _append_system_prompt_addendum("base prompt\n\n  ", "ADD")
        assert appended is True
        assert new_prompt == "base prompt\n\nADD"

    def test_none_prompt_becomes_addendum(self):
        new_prompt, appended = _append_system_prompt_addendum(None, "ADD")
        assert appended is True
        assert new_prompt == "ADD"

    def test_empty_str_prompt_becomes_addendum(self):
        # Empty string is falsy — falls through to the addendum-only branch,
        # mirroring the inline logic both call sites had before extraction.
        new_prompt, appended = _append_system_prompt_addendum("", "ADD")
        assert appended is True
        assert new_prompt == "ADD"

    def test_preset_prompt_is_preserved_and_skipped(self):
        # The data-loss-avoidance contract: a preset / file prompt cannot be
        # appended to, so it is returned unchanged and appended=False signals
        # the caller (run_agent_async) to log the skip warning.
        preset = _FakePreset()
        new_prompt, appended = _append_system_prompt_addendum(preset, "ADD")
        assert appended is False
        assert new_prompt is preset
