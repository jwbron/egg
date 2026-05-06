"""Parity check: sandbox-side ``SLICE_ID_PATTERN`` mirrors the orchestrator (#2486).

The sandbox can't import from the orchestrator package at runtime
(``egg_lib`` ships in the container with no orchestrator code), so the
canonical ``slice-<N>`` regex is mirrored in ``egg_lib._slice_id``.
This test pins the two definitions together so a future tweak on
either side trips CI instead of silently letting them drift.

Imports rely on ``tests/conftest.py`` (which puts ``sandbox/`` and
``shared/`` on ``sys.path``) and on pytest's rootdir discovery for the
``orchestrator`` package.
"""

from __future__ import annotations


def test_sandbox_pattern_matches_orchestrator():
    from egg_lib._slice_id import SLICE_ID_PATTERN as sandbox_pattern

    from orchestrator.slice_id_validation import SLICE_ID_PATTERN as orch_pattern

    assert sandbox_pattern.pattern == orch_pattern.pattern


def test_call_sites_share_the_same_object():
    """All three sandbox call sites import the canonical regex (#2486).

    Importing the *same* compiled object — not a re-defined equivalent —
    means a tweak to ``egg_lib._slice_id.SLICE_ID_PATTERN`` propagates
    to every call site without a follow-up edit.
    """
    from egg_agent_tools.handlers import brc, progress
    from egg_lib import _slice_id, orch_cli

    assert brc._SLICE_ID_PATTERN is _slice_id.SLICE_ID_PATTERN
    assert progress._SLICE_ID_PATTERN is _slice_id.SLICE_ID_PATTERN
    assert orch_cli._SLICE_ID_PATTERN is _slice_id.SLICE_ID_PATTERN
