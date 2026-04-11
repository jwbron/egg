"""Egg Integration Layer for the egg harness.

Wires egg-platform concerns (CLI tools, CLAUDE.md rules, role-based
permissions, anchor-based compaction) into the generic
:mod:`egg_harness` runtime.

The primary entry point is :func:`create_egg_harness`, which returns a
fully-configured ``(AgentLoop, EventBus, HarnessConfig)`` tuple ready
to run headless agent sessions.
"""

from __future__ import annotations

from egg_harness_integration.harness_factory import create_egg_harness

__all__ = ["create_egg_harness"]
