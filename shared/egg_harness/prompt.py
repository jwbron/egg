"""Generic system prompt assembly.

Provides utilities for building a system prompt from multiple sources.
Each source can be a static string or a callable that returns a string,
allowing deferred/dynamic prompt construction.
"""

from __future__ import annotations

from collections.abc import Callable

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

PromptSource = str | Callable[[], str]
"""A prompt source: either a literal string or a callable returning one."""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(sources: list[PromptSource]) -> str:
    """Assemble a system prompt from multiple sources.

    Each source is either a string or a zero-argument callable that returns a
    string.  Callables are invoked at assembly time.  Empty or ``None`` results
    are silently skipped.  Non-empty fragments are joined with the standard
    ``"\\n\\n---\\n\\n"`` separator (matching the CLAUDE.md section convention).

    Args:
        sources: Ordered list of prompt sources to concatenate.

    Returns:
        The assembled prompt string.  May be empty if all sources produce
        empty/``None`` results.
    """
    fragments: list[str] = []
    for source in sources:
        if callable(source):
            value = source()
        else:
            value = source

        if value:
            fragments.append(value)

    return "\n\n---\n\n".join(fragments)
