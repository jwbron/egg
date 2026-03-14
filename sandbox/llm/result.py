"""Result type for Claude Code agent invocations.

Re-exports :class:`egg_agent.result.AgentResult` for backward compatibility.
"""

from egg_agent.result import AgentResult

# Backward compatibility alias
ClaudeResult = AgentResult

__all__ = ["AgentResult", "ClaudeResult"]
