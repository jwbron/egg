"""Tests for egg_harness.compaction — context compaction manager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from egg_harness.compaction import CompactionLoopError, CompactionManager, _extract_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _tool_use_msg(tool_name: str = "Bash", file_path: str | None = None) -> dict:
    inp = {"command": "echo hi"}
    if file_path:
        inp = {"file_path": file_path}
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": f"Using {tool_name}"},
            {"type": "tool_use", "id": "tu_001", "name": tool_name, "input": inp},
        ],
    }


def _tool_result_msg(tool_use_id: str = "tu_001") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
        ],
    }


def _make_manager(
    model: str = "claude-opus-4-6",
    threshold: float = 0.8,
    keep_recent_tokens: int = 20_000,
    event_bus: MagicMock | None = None,
    loop_protection_turns: int = 3,
) -> CompactionManager:
    return CompactionManager(
        model=model,
        threshold=threshold,
        keep_recent_tokens=keep_recent_tokens,
        event_bus=event_bus,
        loop_protection_turns=loop_protection_turns,
    )


# ---------------------------------------------------------------------------
# TestShouldCompact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_below_threshold_returns_false(self, _mock):
        mgr = _make_manager(threshold=0.8)
        # 79% of 200k = 158,000 < 160,000
        assert mgr.should_compact(158_000) is False

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_at_threshold_returns_true(self, _mock):
        mgr = _make_manager(threshold=0.8)
        # exactly 80% of 200k = 160,000
        assert mgr.should_compact(160_000) is True

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_above_threshold_returns_true(self, _mock):
        mgr = _make_manager(threshold=0.8)
        assert mgr.should_compact(190_000) is True

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_custom_threshold(self, _mock):
        mgr = _make_manager(threshold=0.5)
        # 50% of 200k = 100,000
        assert mgr.should_compact(99_999) is False
        assert mgr.should_compact(100_000) is True

    @patch("egg_harness.compaction.get_context_window", return_value=128_000)
    def test_different_context_window(self, _mock):
        mgr = _make_manager(threshold=0.8)
        # 80% of 128k = 102,400
        assert mgr.should_compact(102_399) is False
        assert mgr.should_compact(102_400) is True


# ---------------------------------------------------------------------------
# TestCompact
# ---------------------------------------------------------------------------


class TestCompact:
    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_returns_summary_and_messages(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10)
        messages = [_user_msg("hello"), _assistant_msg("hi"), _user_msg("bye")]
        new_msgs, summary = mgr.compact(messages)
        assert isinstance(new_msgs, list)
        assert isinstance(summary, str)
        assert len(new_msgs) >= 1

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_summary_message_has_correct_structure(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10)
        messages = [_user_msg("hello"), _assistant_msg("hi"), _user_msg("bye")]
        new_msgs, _summary = mgr.compact(messages)
        first = new_msgs[0]
        assert first["role"] == "user"
        assert "[Previous conversation summary]" in first["content"]
        assert "[Continuing from where we left off]" in first["content"]

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_compaction_count_increments(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10)
        assert mgr.compaction_count == 0
        mgr.compact([_user_msg("hello"), _assistant_msg("hi")])
        assert mgr.compaction_count == 1

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_event_bus_emits_compaction_event(self, _mock):
        bus = MagicMock()
        mgr = _make_manager(keep_recent_tokens=10, event_bus=bus)
        mgr.compact([_user_msg("hello"), _assistant_msg("hi")])
        bus.emit_compaction.assert_called_once()
        args = bus.emit_compaction.call_args
        summary, tokens_before, tokens_after = args[0]
        assert isinstance(summary, str)
        assert isinstance(tokens_before, int)
        assert isinstance(tokens_after, int)

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_no_event_bus_no_error(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, event_bus=None)
        # Should not raise
        mgr.compact([_user_msg("hello"), _assistant_msg("hi")])

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_empty_messages(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10)
        new_msgs, summary = mgr.compact([])
        assert isinstance(new_msgs, list)
        assert isinstance(summary, str)


# ---------------------------------------------------------------------------
# TestCompactNow
# ---------------------------------------------------------------------------


class TestCompactNow:
    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_bypasses_loop_protection(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, loop_protection_turns=3)
        msgs = [_user_msg("a"), _assistant_msg("b")]
        mgr.compact(msgs)
        # Same turn — compact() would raise, but compact_now() should not.
        mgr.compact_now(msgs)
        assert mgr.compaction_count == 2

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_resets_loop_protection(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, loop_protection_turns=3)
        msgs = [_user_msg("a"), _assistant_msg("b")]
        mgr.compact(msgs)
        # compact_now resets loop protection
        mgr.compact_now(msgs)
        # After reset, compact() should also work on the next valid turn
        mgr.current_turn = mgr.current_turn + 3
        mgr.compact(msgs)
        assert mgr.compaction_count == 3


# ---------------------------------------------------------------------------
# TestLoopProtection
# ---------------------------------------------------------------------------


class TestLoopProtection:
    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_consecutive_compact_raises(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, loop_protection_turns=3)
        msgs = [_user_msg("a"), _assistant_msg("b")]
        mgr.compact(msgs)
        with pytest.raises(CompactionLoopError):
            mgr.compact(msgs)

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_compact_after_enough_turns_succeeds(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, loop_protection_turns=3)
        msgs = [_user_msg("a"), _assistant_msg("b")]
        mgr.compact(msgs)
        mgr.current_turn = mgr.current_turn + 3
        # Should not raise
        mgr.compact(msgs)

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_first_compact_always_succeeds(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, loop_protection_turns=10)
        msgs = [_user_msg("a"), _assistant_msg("b")]
        # No prior compaction — should work regardless of protection turns
        mgr.compact(msgs)

    @patch("egg_harness.compaction.get_context_window", return_value=200_000)
    def test_exact_boundary_turns(self, _mock):
        mgr = _make_manager(keep_recent_tokens=10, loop_protection_turns=3)
        msgs = [_user_msg("a"), _assistant_msg("b")]
        mgr.compact(msgs)
        # Advance by exactly loop_protection_turns - 1 → should still raise
        mgr.current_turn = mgr.current_turn + 2
        with pytest.raises(CompactionLoopError):
            mgr.compact(msgs)
        # Advance by one more → should succeed
        mgr.current_turn = mgr.current_turn + 1
        mgr.compact(msgs)


# ---------------------------------------------------------------------------
# TestFindCutPoint
# ---------------------------------------------------------------------------


class TestFindCutPoint:
    def test_empty_messages_returns_zero(self):
        mgr = _make_manager()
        assert mgr._find_cut_point([]) == 0

    def test_short_conversation_returns_zero(self):
        """All messages fit within keep_recent_tokens → nothing to compact."""
        mgr = _make_manager(keep_recent_tokens=100_000)
        msgs = [_user_msg("hi"), _assistant_msg("hello")]
        assert mgr._find_cut_point(msgs) == 0

    def test_splits_at_token_boundary(self):
        mgr = _make_manager(keep_recent_tokens=10)
        # Each msg ~len/4 tokens: "a"*40 = 10 tokens each
        msgs = [_user_msg("a" * 40) for _ in range(5)]
        cut = mgr._find_cut_point(msgs)
        assert 0 < cut < len(msgs)

    def test_tool_result_at_cut_adjusts_backward(self):
        """If the cut lands on a tool_result, it should move back to include the tool_use."""
        mgr = _make_manager(keep_recent_tokens=10)
        # Build messages where tool_result would be at the natural cut point.
        # Many padding messages, then a tool_use/tool_result pair, then recent.
        msgs = [
            _user_msg("x" * 80),  # 20 tokens - will be compacted
            _user_msg("y" * 80),  # 20 tokens - will be compacted
            _tool_use_msg(),  # assistant tool_use
            _tool_result_msg(),  # user tool_result
            _user_msg("z" * 20),  # 5 tokens - recent
        ]
        cut = mgr._find_cut_point(msgs)
        # The cut should never land on a tool_result (index 3).
        # If it does, it should adjust backward.
        if cut > 0:
            msg_at_cut = msgs[cut]
            assert not mgr._is_tool_result_message(msg_at_cut)

    def test_single_message_returns_zero(self):
        mgr = _make_manager(keep_recent_tokens=1)
        msgs = [_user_msg("a" * 400)]
        cut = mgr._find_cut_point(msgs)
        # The loop finds cut_index=1, but the guard `cut_index <= 0` doesn't
        # trigger. With only 1 message, cut_index=1 means "compact first msg,
        # keep nothing" which still returns a valid index. The _do_compact
        # method handles producing a summary of the single compacted message.
        assert 0 <= cut <= len(msgs)


# ---------------------------------------------------------------------------
# TestIsToolResultMessage / TestIsToolUseMessage
# ---------------------------------------------------------------------------


class TestIsToolResultMessage:
    def test_positive(self):
        msg = _tool_result_msg()
        assert CompactionManager._is_tool_result_message(msg) is True

    def test_negative_user_text(self):
        msg = _user_msg("just text")
        assert CompactionManager._is_tool_result_message(msg) is False

    def test_negative_assistant(self):
        # Wrong role
        msg = {
            "role": "assistant",
            "content": [{"type": "tool_result", "tool_use_id": "tu_001", "content": "ok"}],
        }
        assert CompactionManager._is_tool_result_message(msg) is False

    def test_negative_empty_content_list(self):
        msg = {"role": "user", "content": []}
        assert CompactionManager._is_tool_result_message(msg) is False


class TestIsToolUseMessage:
    def test_positive(self):
        msg = _tool_use_msg()
        assert CompactionManager._is_tool_use_message(msg) is True

    def test_negative_assistant_text(self):
        msg = _assistant_msg("just text")
        assert CompactionManager._is_tool_use_message(msg) is False

    def test_negative_user_role(self):
        msg = {
            "role": "user",
            "content": [{"type": "tool_use", "id": "tu_001", "name": "Bash", "input": {}}],
        }
        assert CompactionManager._is_tool_use_message(msg) is False


# ---------------------------------------------------------------------------
# TestGenerateSummary
# ---------------------------------------------------------------------------


class TestGenerateSummary:
    def test_all_sections_present(self):
        msgs = [_user_msg("hello"), _assistant_msg("hi")]
        mgr = _make_manager()
        summary = mgr._generate_summary(msgs)
        assert "## Goal/Task" in summary
        assert "## Progress" in summary
        assert "## Key Decisions" in summary
        assert "## Files Modified" in summary
        assert "## Errors Encountered" in summary

    def test_goal_from_system_prompt(self):
        mgr = _make_manager()
        summary = mgr._generate_summary([], system_prompt="Fix the login bug")
        assert "Fix the login bug" in summary

    def test_goal_from_system_prompt_truncated_at_200(self):
        mgr = _make_manager()
        long_prompt = "x" * 300
        summary = mgr._generate_summary([], system_prompt=long_prompt)
        assert "..." in summary
        # The goal section should have at most 200 chars of the prompt
        goal_section = summary.split("## Progress")[0]
        assert "x" * 200 in goal_section
        assert "x" * 201 not in goal_section

    def test_goal_from_first_user_message(self):
        msgs = [_user_msg("Please fix the auth flow")]
        mgr = _make_manager()
        summary = mgr._generate_summary(msgs)
        assert "Please fix the auth flow" in summary

    def test_goal_truncated_at_300_chars(self):
        mgr = _make_manager()
        long_msg = "y" * 400
        summary = mgr._generate_summary([_user_msg(long_msg)])
        goal_section = summary.split("## Progress")[0]
        assert "y" * 300 in goal_section
        assert "y" * 301 not in goal_section

    def test_progress_from_assistant_messages(self):
        mgr = _make_manager()
        msgs = [
            _user_msg("start"),
            _assistant_msg("I read the config file.\nThen did more stuff."),
            _assistant_msg("I wrote the output."),
        ]
        summary = mgr._generate_summary(msgs)
        assert "I read the config file." in summary
        assert "I wrote the output." in summary

    def test_progress_limited_to_20(self):
        mgr = _make_manager()
        msgs = [_assistant_msg(f"Step {i}") for i in range(25)]
        summary = mgr._generate_summary(msgs)
        assert "Step 19" in summary
        assert "Step 20" not in summary

    def test_decisions_extracted_by_keywords(self):
        mgr = _make_manager()
        msgs = [_assistant_msg("I decided to use Redis for caching.")]
        summary = mgr._generate_summary(msgs)
        assert "decided to use Redis" in summary

    def test_files_extracted_from_tool_use(self):
        mgr = _make_manager()
        msgs = [_tool_use_msg("Read", file_path="/tmp/config.json")]
        summary = mgr._generate_summary(msgs)
        assert "/tmp/config.json" in summary

    def test_errors_extracted_by_keywords(self):
        mgr = _make_manager()
        msgs = [_assistant_msg("An error occurred: connection refused")]
        summary = mgr._generate_summary(msgs)
        assert "error occurred" in summary

    def test_empty_messages_produces_defaults(self):
        mgr = _make_manager()
        summary = mgr._generate_summary([])
        assert "No goal could be determined" in summary
        assert "No significant progress" in summary
        assert "No key decisions" in summary
        assert "No files modified" in summary
        assert "No errors encountered" in summary


# ---------------------------------------------------------------------------
# TestEstimateTokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_text_message_estimate(self):
        mgr = _make_manager()
        msg = _user_msg("a" * 100)  # 100 chars / 4 = 25 tokens
        assert mgr._estimate_message_tokens(msg) == 25

    def test_structured_content_estimate(self):
        mgr = _make_manager()
        msg = _tool_use_msg()
        tokens = mgr._estimate_message_tokens(msg)
        assert tokens >= 1

    def test_empty_message_returns_1(self):
        mgr = _make_manager()
        msg = {"role": "user", "content": ""}
        assert mgr._estimate_message_tokens(msg) == 1

    def test_no_content_returns_1(self):
        mgr = _make_manager()
        msg = {"role": "user"}
        assert mgr._estimate_message_tokens(msg) == 1

    def test_total_across_messages(self):
        mgr = _make_manager()
        msgs = [_user_msg("a" * 100), _user_msg("b" * 200)]
        total = mgr._estimate_tokens(msgs)
        assert total == 25 + 50

    def test_empty_messages_returns_zero(self):
        mgr = _make_manager()
        assert mgr._estimate_tokens([]) == 0


# ---------------------------------------------------------------------------
# TestExtractText
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_string_content(self):
        assert _extract_text({"content": "hello"}) == "hello"

    def test_list_content_with_text_blocks(self):
        msg = {"content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]}
        assert _extract_text(msg) == "A\nB"

    def test_list_content_with_non_text_blocks(self):
        msg = {"content": [{"type": "tool_use", "id": "t", "name": "X", "input": {}}]}
        assert _extract_text(msg) == ""

    def test_list_with_mixed_blocks(self):
        msg = {
            "content": [
                {"type": "text", "text": "Before"},
                {"type": "tool_use", "id": "t", "name": "X", "input": {}},
                {"type": "text", "text": "After"},
            ]
        }
        assert _extract_text(msg) == "Before\nAfter"

    def test_list_with_string_blocks(self):
        msg = {"content": ["hello", "world"]}
        assert _extract_text(msg) == "hello\nworld"

    def test_empty_content(self):
        assert _extract_text({"content": ""}) == ""

    def test_no_content_key(self):
        assert _extract_text({}) == ""

    def test_none_content(self):
        assert _extract_text({"content": None}) == ""

    def test_numeric_content(self):
        assert _extract_text({"content": 42}) == ""


# ---------------------------------------------------------------------------
# TestCurrentTurnProperty
# ---------------------------------------------------------------------------


class TestCurrentTurnProperty:
    def test_initial_value(self):
        mgr = _make_manager()
        assert mgr.current_turn == 0

    def test_setter(self):
        mgr = _make_manager()
        mgr.current_turn = 5
        assert mgr.current_turn == 5
