"""Unit tests for the promoted :mod:`egg_harness.testing` public API.

These tests guard the public surface promised by issue #2474, slice-1:
``ScriptedProvider`` must be importable from both ``egg_harness.testing``
(barrel) and ``egg_harness.testing.scripted_provider`` (canonical module),
and the two import paths must resolve to the **same class object** so
``isinstance`` checks and identity-based caching work across consumers.

The construction smoke-test (`.name == "scripted"`, empty ``call_history``)
catches any future refactor that accidentally drops the no-arg-friendly
constructor contract documented by the original inline class at
``shared/tests/test_egg_harness/test_integration.py`` (HEAD lines 130-163
before slice-1).

Contract reference: issue #2474 task-1-3.  Acceptance criterion:
``.venv/bin/pytest shared/tests/test_egg_harness/test_scripted_provider.py -q``
shows 2 passed.
"""

from __future__ import annotations


def test_barrel_and_module_resolve_to_same_class() -> None:
    """Importing ``ScriptedProvider`` from the barrel and the module must
    yield the same class object — not just two classes with the same name.

    Identity (``is``) matters because consumers may use ``isinstance``
    or store class references in registries.  Two separate definitions
    would break those patterns silently.
    """
    from egg_harness.testing import ScriptedProvider as BarrelClass
    from egg_harness.testing.scripted_provider import (
        ScriptedProvider as ModuleClass,
    )

    assert BarrelClass is ModuleClass, (
        "egg_harness.testing.ScriptedProvider and "
        "egg_harness.testing.scripted_provider.ScriptedProvider "
        "must be the same class object — got separate definitions"
    )


def test_constructs_with_empty_script_and_exposes_public_surface() -> None:
    """A bare ``ScriptedProvider([])`` must report ``name='scripted'`` and
    an empty ``call_history`` list — these are the load-bearing fields
    inspected by every consumer in ``test_integration.py``.
    """
    from egg_harness.testing import ScriptedProvider

    provider = ScriptedProvider([])

    assert provider.name == "scripted"
    assert provider.call_history == []
    # ``call_history`` must be a list (mutable) — consumers append to it.
    assert isinstance(provider.call_history, list)
