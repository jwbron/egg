"""Public testing helpers for egg_harness consumers.

This package exposes utilities that integration tests, regression tests, and
other consumers can use to drive ``egg_harness`` deterministically without
hitting a live LLM.  The most important export is :class:`ScriptedProvider`,
which lets a test hand each agent role a canned trajectory of
:class:`~egg_harness.providers.base.StreamEvent` objects.

Import directly from ``egg_harness.testing``::

    from egg_harness.testing import ScriptedProvider
"""

from __future__ import annotations

from egg_harness.testing.scripted_provider import ScriptedProvider

__all__ = ["ScriptedProvider"]
