"""Context compaction for long-running agent sessions.

Provides :class:`CompactionManager` which monitors token usage against
the model's context window and compacts older messages into a structured
summary when the threshold is reached, preserving recent conversation
context and tool-call/result pair integrity.
"""

from __future__ import annotations

import logging
from typing import Any

from egg_harness.config import get_context_window
from egg_harness.events import EventBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CompactionLoopError(Exception):
    """Raised when compaction is triggered too frequently.

    This indicates a potential compaction loop where the context keeps
    filling up faster than compaction can free it.
    """


# ---------------------------------------------------------------------------
# CompactionManager
# ---------------------------------------------------------------------------


class CompactionManager:
    """Manages context compaction for agent sessions.

    Monitors token usage relative to the model's context window and
    compacts older messages into a structured summary when usage exceeds
    the configured threshold.  Tool-use / tool-result message pairs are
    never split across the compaction boundary.

    Args:
        model: Canonical model identifier (used to look up context window).
        threshold: Fraction of the context window at which compaction fires.
        keep_recent_tokens: Number of recent tokens to preserve verbatim.
        event_bus: Optional event bus for emitting compaction events.
        loop_protection_turns: Minimum turns between automatic compactions.
    """

    def __init__(
        self,
        model: str,
        threshold: float = 0.8,
        keep_recent_tokens: int = 20_000,
        event_bus: EventBus | None = None,
        loop_protection_turns: int = 3,
    ) -> None:
        self._model = model
        self._threshold = threshold
        self._keep_recent_tokens = keep_recent_tokens
        self._event_bus = event_bus
        self._loop_protection_turns = loop_protection_turns

        self._compaction_count: int = 0
        self._last_compaction_turn: int | None = None
        self._current_turn: int = 0

    # -- public interface -----------------------------------------------------

    @property
    def compaction_count(self) -> int:
        """Number of compactions that have been performed."""
        return self._compaction_count

    @property
    def current_turn(self) -> int:
        """The current turn counter value."""
        return self._current_turn

    @current_turn.setter
    def current_turn(self, value: int) -> None:
        self._current_turn = value

    def should_compact(self, total_tokens: int) -> bool:
        """Check whether compaction should be triggered.

        Args:
            total_tokens: Current total token count for the conversation.

        Returns:
            True if *total_tokens* meets or exceeds the compaction
            threshold for the configured model.
        """
        limit = get_context_window(self._model)
        return total_tokens >= self._threshold * limit

    def compact(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Compact older messages into a structured summary.

        Walks backwards from the newest message to find a cut point that
        preserves at least ``keep_recent_tokens`` of recent context while
        never splitting a tool-use / tool-result pair.  Messages before
        the cut point are summarised into a structured markdown text.

        Args:
            messages: Full conversation message list.
            system_prompt: Optional system prompt (reserved for future use).

        Returns:
            A tuple of ``(new_messages, summary)`` where *new_messages*
            begins with a summary injection message followed by the
            retained recent messages.

        Raises:
            CompactionLoopError: If called again within
                ``loop_protection_turns`` of the last compaction.
        """
        self._check_loop_protection()
        return self._do_compact(messages, system_prompt, log_prefix="Compacted context")

    def compact_now(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Force an immediate compaction, bypassing loop protection.

        Behaves identically to :meth:`compact` but resets the loop
        protection counter so that subsequent automatic compactions are
        not blocked.

        Args:
            messages: Full conversation message list.
            system_prompt: Optional system prompt (reserved for future use).

        Returns:
            A tuple of ``(new_messages, summary)``.
        """
        # Reset loop protection so this manual trigger doesn't count
        # against the automatic protection window.
        self._last_compaction_turn = None
        return self._do_compact(messages, system_prompt, log_prefix="Manual compaction")

    def _do_compact(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | None,
        log_prefix: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Shared compaction implementation used by compact() and compact_now()."""
        tokens_before = self._estimate_tokens(messages)
        cut_index = self._find_cut_point(messages)

        old_messages = messages[:cut_index]
        recent_messages = messages[cut_index:]

        summary = self._generate_summary(old_messages, system_prompt)

        summary_message: dict[str, Any] = {
            "role": "user",
            "content": (
                "[Previous conversation summary]\n\n"
                f"{summary}\n\n"
                "[Continuing from where we left off]"
            ),
        }
        new_messages = [summary_message] + recent_messages

        tokens_after = self._estimate_tokens(new_messages)

        self._compaction_count += 1
        self._last_compaction_turn = self._current_turn

        if self._event_bus is not None:
            self._event_bus.emit_compaction(summary, tokens_before, tokens_after)

        logger.info(
            "%s: %d -> %d tokens (compaction #%d)",
            log_prefix,
            tokens_before,
            tokens_after,
            self._compaction_count,
        )

        return new_messages, summary

    # -- internal helpers -----------------------------------------------------

    def _check_loop_protection(self) -> None:
        """Raise ``CompactionLoopError`` if compacting too frequently."""
        if self._last_compaction_turn is None:
            return
        turns_since = self._current_turn - self._last_compaction_turn
        if turns_since < self._loop_protection_turns:
            raise CompactionLoopError(
                f"Compaction requested only {turns_since} turn(s) after "
                f"the last compaction (minimum: {self._loop_protection_turns})"
            )

    def _find_cut_point(self, messages: list[dict[str, Any]]) -> int:
        """Find the index that splits old vs. retained messages.

        Walks backwards from the end, accumulating token estimates until
        ``keep_recent_tokens`` is reached.  Then adjusts the boundary so
        that tool-use / tool-result pairs are never split: if the message
        at the cut point is a ``tool_result`` user message, move the cut
        one position earlier to include its paired ``tool_use`` assistant
        message.

        Returns:
            The index into *messages* where the retained portion begins.
            If all messages fit within ``keep_recent_tokens``, returns 0
            (nothing to compact).
        """
        if not messages:
            return 0

        accumulated_tokens = 0
        cut_index = len(messages)

        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = self._estimate_message_tokens(messages[i])
            if accumulated_tokens + msg_tokens > self._keep_recent_tokens:
                break
            accumulated_tokens += msg_tokens
            cut_index = i

        # Ensure we don't cut between a tool_use and its tool_result.
        # A tool_result user message must stay paired with the preceding
        # assistant message that contains the tool_use block.
        if cut_index > 0 and cut_index < len(messages):
            msg_at_cut = messages[cut_index]
            if self._is_tool_result_message(msg_at_cut):
                cut_index = max(0, cut_index - 1)

        # Never compact everything -- keep at least one message.
        if cut_index <= 0:
            return 0

        return cut_index

    @staticmethod
    def _is_tool_result_message(msg: dict[str, Any]) -> bool:
        """Check if a message is a tool_result user message."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            )
        return False

    @staticmethod
    def _is_tool_use_message(msg: dict[str, Any]) -> bool:
        """Check if a message is a tool_use assistant message."""
        if msg.get("role") != "assistant":
            return False
        content = msg.get("content")
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get("type") == "tool_use" for block in content
            )
        return False

    def _generate_summary(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | None = None,
    ) -> str:
        """Generate a structured markdown summary of compacted messages.

        Extracts key information from the message history and organises
        it into sections: Goal/Task, Progress, Key Decisions, Files
        Modified, and Errors Encountered.
        """
        goal = self._extract_goal(messages, system_prompt)
        progress = self._extract_progress(messages)
        decisions = self._extract_decisions(messages)
        files = self._extract_files(messages)
        errors = self._extract_errors(messages)

        sections: list[str] = []

        sections.append(f"## Goal/Task\n{goal}")
        sections.append(
            "## Progress\n"
            + (
                "\n".join(f"- {item}" for item in progress)
                if progress
                else "- No significant progress recorded"
            )
        )
        sections.append(
            "## Key Decisions\n"
            + (
                "\n".join(f"- {item}" for item in decisions)
                if decisions
                else "- No key decisions recorded"
            )
        )
        sections.append(
            "## Files Modified\n"
            + ("\n".join(f"- `{f}`" for f in sorted(files)) if files else "- No files modified")
        )
        sections.append(
            "## Errors Encountered\n"
            + ("\n".join(f"- {err}" for err in errors) if errors else "- No errors encountered")
        )

        return "\n\n".join(sections)

    @staticmethod
    def _extract_goal(
        messages: list[dict[str, Any]],
        system_prompt: str | None = None,
    ) -> str:
        """Extract the primary goal from early messages or system prompt."""
        if system_prompt:
            # Use first 200 chars of system prompt as a goal hint.
            truncated = system_prompt[:200]
            if len(system_prompt) > 200:
                truncated += "..."
            return truncated

        # Fall back to the first user message content.
        for msg in messages:
            if msg.get("role") == "user":
                content = _extract_text(msg)
                if content:
                    truncated = content[:300]
                    if len(content) > 300:
                        truncated += "..."
                    return truncated

        return "No goal could be determined from the conversation."

    @staticmethod
    def _extract_progress(messages: list[dict[str, Any]]) -> list[str]:
        """Extract progress items from assistant messages."""
        progress: list[str] = []
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            text = _extract_text(msg)
            if not text:
                continue
            # Take the first sentence of each assistant message as a
            # progress item (up to 150 chars).
            first_line = text.split("\n")[0].strip()
            if first_line:
                truncated = first_line[:150]
                if len(first_line) > 150:
                    truncated += "..."
                progress.append(truncated)
        # Limit to avoid overly long summaries.
        return progress[:20]

    @staticmethod
    def _extract_decisions(messages: list[dict[str, Any]]) -> list[str]:
        """Extract key decision markers from messages."""
        decisions: list[str] = []
        decision_keywords = (
            "decided",
            "choosing",
            "chose",
            "going with",
            "decision:",
            "approach:",
            "strategy:",
        )
        for msg in messages:
            text = _extract_text(msg)
            if not text:
                continue
            text_lower = text.lower()
            for keyword in decision_keywords:
                if keyword in text_lower:
                    # Find the sentence containing the keyword.
                    for line in text.split("\n"):
                        if keyword in line.lower():
                            truncated = line.strip()[:150]
                            if len(line.strip()) > 150:
                                truncated += "..."
                            decisions.append(truncated)
                            break
                    break
        return decisions[:10]

    @staticmethod
    def _extract_files(messages: list[dict[str, Any]]) -> set[str]:
        """Extract file paths mentioned in tool calls."""
        files: set[str] = set()
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                inp = block.get("input", {})
                if not isinstance(inp, dict):
                    continue
                for key in ("file_path", "path", "file"):
                    val = inp.get(key)
                    if isinstance(val, str) and val:
                        files.add(val)
        return files

    @staticmethod
    def _extract_errors(messages: list[dict[str, Any]]) -> list[str]:
        """Extract error indicators from messages."""
        errors: list[str] = []
        error_keywords = ("error", "exception", "traceback", "failed")
        for msg in messages:
            text = _extract_text(msg)
            if not text:
                continue
            text_lower = text.lower()
            for keyword in error_keywords:
                if keyword in text_lower:
                    for line in text.split("\n"):
                        if keyword in line.lower():
                            truncated = line.strip()[:200]
                            if len(line.strip()) > 200:
                                truncated += "..."
                            errors.append(truncated)
                            break
                    break
        return errors[:10]

    @staticmethod
    def _estimate_message_tokens(msg: dict[str, Any]) -> int:
        """Estimate token count for a single message (~4 chars/token)."""
        text = _extract_text(msg)
        if text:
            return max(1, len(text) // 4)
        # For structured content (tool blocks, etc.), serialise roughly.
        content = msg.get("content")
        if isinstance(content, list):
            total_chars = sum(len(str(block)) for block in content)
            return max(1, total_chars // 4)
        return 1

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total token count across all messages.

        Uses a rough heuristic of ~4 characters per token.

        Args:
            messages: List of conversation messages.

        Returns:
            Estimated total token count.
        """
        return sum(self._estimate_message_tokens(msg) for msg in messages)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_text(msg: dict[str, Any]) -> str:
    """Extract plain text content from a message.

    Handles both simple string content and structured content-block
    lists (returning only ``text`` blocks).
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_val = block.get("text", "")
                if isinstance(text_val, str):
                    parts.append(text_val)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""
