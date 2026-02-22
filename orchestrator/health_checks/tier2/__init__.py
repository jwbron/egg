"""
Tier 2 (agent inspector) health checks.

Semantic checks that use Claude to analyze agent state and detect
issues that programmatic Tier 1 checks cannot catch.
"""

from health_checks.tier2.agent_inspector import AgentInspectorCheck

__all__ = ["AgentInspectorCheck"]
