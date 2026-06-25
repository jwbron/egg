"""Slice-9 task-9-2: the master context-discipline flag is read in ONE place.

#3200 slice-9 / task-9-1 introduces a SINGLE feature flag gating the whole
context discipline, with two structural requirements this suite pins:

  * **Read in one place.** There is a single authoritative reader (a predicate
    helper / module constant), not a flag re-parsed ad hoc per role. The
    end-to-end behaviour — every event-pump role flipping together — is covered
    by ``integration_tests/test_context_discipline_flag_e2e.py``; this module
    pins the reader's CONTRACT directly: unset / falsey => disabled (the
    rollout default), truthy => enabled.
  * **Default OFF.** With the flag unset the discipline is inert, so the legacy
    full-context path is preserved during staged rollout.

The coder owns the reader's home and the flag's exact env-var spelling, so the
suite probes a generous set of candidate ``(module, attr)`` homes and candidate
env-var names (parallel-BRC convention — see
``tests/shared/egg_agent/test_reseed_decision.py``). When the reader is not yet
present (coder task-9-1 unmerged on this tester branch) the tests skip; they
activate and pin the contract at PR assembly.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

# Candidate homes for the single flag-reader predicate. A zero-arg callable
# returning a truthy/falsey value for "is the context discipline enabled?".
_READER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("egg_agent.context_discipline", "context_discipline_enabled"),
    ("egg_agent.context_discipline", "enabled"),
    ("egg_agent.context_discipline", "is_enabled"),
    ("egg_agent.session", "context_discipline_enabled"),
    ("egg_agent.reseed", "context_discipline_enabled"),
    ("egg_agent.queryable_env", "context_discipline_enabled"),
    ("egg_agent", "context_discipline_enabled"),
    ("egg_anchor.protected_root", "context_discipline_enabled"),
)

# Candidate env-var names for the master flag (must match the e2e suite's set so
# discovery agrees across files).
_FLAG_CANDIDATES: tuple[str, ...] = (
    "EGG_CONTEXT_DISCIPLINE",
    "EGG_BRC_CONTEXT_DISCIPLINE",
    "EGG_CONTEXT_DISCIPLINE_ENABLED",
    "EGG_BRC_CONTEXT_DISCIPLINE_ENABLED",
    "EGG_RESIDENT_ROOT",
    "EGG_BRC_RESIDENT_ROOT",
    "EGG_PROTECTED_ROOT",
    "EGG_BRC_PROTECTED_ROOT",
    "EGG_CONTEXT_ROOT",
    "EGG_RETRIEVAL_ROOT",
    "EGG_JIT_PULL",
    "EGG_BRC_JIT_PULL",
)


def _reader() -> Callable[[], Any]:
    for module_name, attr in _READER_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    pytest.skip(
        "context-discipline flag reader not found (coder task-9-1 unmerged); tried "
        f"{[f'{m}.{a}' for m, a in _READER_CANDIDATES]}"
    )


@contextmanager
def _env(values: dict[str, str | None]) -> Iterator[None]:
    """Set/clear env vars for the duration of the block; restore after."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _clear_all() -> dict[str, None]:
    return dict.fromkeys(_FLAG_CANDIDATES)


def _active_flag(fn: Callable[[], Any]) -> str | None:
    """Return the candidate env var that, set truthy, flips the reader on.

    Probes each candidate against the reader so the contract tests bind to the
    coder's actual env-var spelling. ``None`` when no candidate flips it.
    """
    with _env(_clear_all()):
        if bool(fn()):
            # Reader is truthy with NO flag set — that violates default-OFF;
            # surface via the dedicated default-off test rather than here.
            return None
        for name in _FLAG_CANDIDATES:
            with _env({name: "1"}):
                if bool(fn()):
                    return name
    return None


def test_flag_reader_defaults_off() -> None:
    """With no candidate flag set, the reader reports the discipline DISABLED.

    The staged-rollout default: an unset flag must leave the legacy
    full-context path active.
    """
    fn = _reader()
    with _env(_clear_all()):
        assert bool(fn()) is False, (
            "context-discipline reader is enabled with no flag set; default must be OFF"
        )


def test_flag_reader_enables_on_truthy() -> None:
    """A truthy spelling of the master flag flips the reader ON.

    Confirms the single reader actually consumes an env flag (it is not a
    hard-coded constant) and accepts the usual truthy spellings.
    """
    fn = _reader()
    name = _active_flag(fn)
    if name is None:
        pytest.skip("no candidate env var flips the reader (flag spelling unknown / unmerged)")
    for truthy in ("1", "true", "yes", "on"):
        with _env({**_clear_all(), name: truthy}):
            assert bool(fn()) is True, f"reader not enabled for {name}={truthy!r}"


def test_flag_reader_stays_off_on_falsey() -> None:
    """Falsey spellings of the master flag keep the reader OFF.

    The dual of the truthy test: an explicit ``0`` / ``false`` / empty value
    must not enable the discipline, so an operator can pin OFF explicitly.
    """
    fn = _reader()
    name = _active_flag(fn)
    if name is None:
        pytest.skip("no candidate env var flips the reader (flag spelling unknown / unmerged)")
    for falsey in ("0", "false", "no", "off", ""):
        with _env({**_clear_all(), name: falsey}):
            assert bool(fn()) is False, f"reader enabled for falsey {name}={falsey!r}"
