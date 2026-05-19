"""Claude Code substrate implementations for issue #2623.

Module-level entry points for the four substrate protocols under the
``"claude-code"`` selection:

- ``ClaudeCodeSpawner`` → ``AgentSpawner``
- ``InProcessMessageBus`` → ``MessageBus``
- ``PreToolUseHookPolicy`` → ``PolicyEnforcer``
- ``LocalWorktreeManager`` → ``WorktreeManager``

See ``docs/architecture/claude-code-substrate.md`` for the ADR.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

from .message_bus import InProcessMessageBus
from .policy import PreToolUseHookPolicy
from .spawner import ClaudeCodeSpawner
from .worktree import LocalWorktreeManager

__all__ = [
    "ClaudeCodeSpawner",
    "InProcessMessageBus",
    "LocalWorktreeManager",
    "PreToolUseHookPolicy",
]
