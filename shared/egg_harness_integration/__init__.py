"""Egg-specific integration layer for egg_harness.

Bridges the generic harness with egg-specific tools, permissions,
and configuration.
"""

from egg_harness_integration.harness_factory import create_egg_harness

__all__ = ["create_egg_harness"]
