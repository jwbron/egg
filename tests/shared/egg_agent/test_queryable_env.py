"""Shared-side locator tests for the slice-5 queryable-environment layer.

#3200 / slice-5, task-5-3 (shared/egg_agent half). task-5-1 lists
``shared/egg_agent/`` among its affected paths, so the queryable-environment
"pull JIT, do not inline the bulk" layer may land as an ``egg_agent`` module.
This file binds to that layer wherever the coder puts it and asserts the two
load-bearing properties that are independent of the orchestrator wiring:

  * the layer carries the honest-limit contract in its own text — JIT pull does
    NOT bound the window; a pulled slice stays resident until the reseed
    (slice-8) bounds it; and

  * the layer routes the bulk to the existing JIT-pull tools
    (``read_peer_artifact`` / ``brc-transcript``) rather than re-inlining it.

Parallel-BRC-producer convention: the coder's symbol may be absent on the
tester branch, so the locator ``pytest.skip``s until it merges (see
``orchestrator/tests/test_reseed_threshold.py`` /
``shared/egg_anchor/tests/test_protected_root.py``). The orchestrator-side
behavioural assertions live in ``orchestrator/tests/test_queryable_env_jit.py``;
this file is the ``egg_agent``-home fallback so at least one seam activates
whichever package the coder chooses.
"""

from __future__ import annotations

import importlib
import inspect
from types import ModuleType
from typing import Any

import pytest

# Candidate modules for the egg_agent-home queryable-environment layer. The
# coder owns the exact name; these cover the plausible spellings.
_LAYER_MODULE_CANDIDATES: tuple[str, ...] = (
    "egg_agent.queryable_env",
    "egg_agent.queryable_environment",
    "egg_agent.jit_pull",
    "egg_agent.brc_queryable",
    "egg_agent.context_pull",
)

# Honest-limit phrasing fragments — match loosely (lowercased substring) so the
# assertion keys on the *contract* ("pull does not bound the window; reseed
# does"), not the coder's exact wording.
_HONEST_LIMIT_FRAGMENTS: tuple[tuple[str, ...], ...] = (
    ("pull", "bound", "window"),
    ("reseed", "bound"),
    ("resident", "compaction"),
)

_JIT_POINTER_TOKENS: tuple[str, ...] = ("read_peer_artifact", "brc-transcript")


def _layer_module() -> ModuleType:
    for module_name in _LAYER_MODULE_CANDIDATES:
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue
    pytest.skip(
        "egg_agent queryable-environment layer not found (coder task-5-1 "
        f"unmerged, or it lives in the orchestrator); tried {list(_LAYER_MODULE_CANDIDATES)}"
    )


def test_layer_records_the_honest_limit_contract() -> None:
    """The layer's source records the honest limit: pull does not bound; reseed does.

    The whole queryable-environment design rests on this caveat being explicit
    (the central tension the prototype must not paper over), so it must be
    written into the layer rather than left implicit.
    """
    module = _layer_module()
    source = inspect.getsource(module).lower()
    assert any(all(tok in source for tok in fragment) for fragment in _HONEST_LIMIT_FRAGMENTS), (
        "queryable-environment layer does not record the honest-limit contract "
        "(pull does not bound the window; the reseed does)"
    )


def test_layer_routes_bulk_to_jit_pull_tools() -> None:
    """The layer references the existing JIT-pull tools rather than re-inlining.

    The bulk it declines to inline must stay reachable, so the layer names at
    least one of the queryable-environment tools.
    """
    module = _layer_module()
    source = inspect.getsource(module)
    assert any(token in source for token in _JIT_POINTER_TOKENS), (
        f"queryable-environment layer names no JIT-pull tool; expected one of {_JIT_POINTER_TOKENS}"
    )


# ---------------------------------------------------------------------------
# Behavioural: actually CALL the renderers (not just grep the source) so the
# tests would fail if a renderer returned "" or raised — the locator tests
# above only assert text presence in the module source.
# ---------------------------------------------------------------------------


def _renderer(name: str) -> Any:  # noqa: ANN401 — located dynamically
    """Return ``name`` from the located layer module, or skip if absent."""
    module = _layer_module()
    fn = getattr(module, name, None)
    if not callable(fn):
        pytest.skip(f"{name} not present on the queryable-environment layer")
    return fn


def test_review_pull_recipe_emits_the_git_log_command() -> None:
    """``render_review_pull_recipe`` emits the exact ``git log`` pull recipe.

    Call-and-assert (not a source grep): a pointer for one producer must render
    the ``git log <start>..<end> --not origin/<base> -p`` command verbatim so
    the agent can pull the delta just-in-time.
    """
    pointer_cls = _renderer("ProducerPullPointer")
    render = _renderer("render_review_pull_recipe")
    out = render([pointer_cls("coder", "abc", "def")], "main")
    assert "git log abc..def --not origin/main -p" in out, (
        f"pull recipe did not render the expected git log command; got: {out!r}"
    )
    assert "coder" in out


def test_pull_handles_name_both_jit_pull_tools() -> None:
    """``render_pull_handles`` names both served-read JIT-pull handles.

    The excluded bulk stays reachable only through these handles, so a real
    call must surface both ``read_peer_artifact`` and the interpolated
    ``brc-transcript`` route for the given pipeline id.
    """
    render = _renderer("render_pull_handles")
    out = render("pipe-123")
    assert "read_peer_artifact" in out
    assert "/pipe-123/brc-transcript" in out, (
        f"pull handles did not interpolate the pipeline id into the route; got: {out!r}"
    )


def test_queryable_env_section_carries_recipe_handles_and_honest_limit() -> None:
    """``render_queryable_env_section`` assembles recipe + handles + honest limit.

    The full pointer block the protected root embeds: a real call must contain
    the per-producer recipe, both pull handles, and the honest-limit contract
    (pull does not bound the window; the reseed does).
    """
    pointer_cls = _renderer("ProducerPullPointer")
    render = _renderer("render_queryable_env_section")
    out = render(
        pipeline_id="pipe-123",
        base_branch="main",
        pointers=[pointer_cls("coder", "abc", "def")],
    )
    assert "git log abc..def --not origin/main -p" in out
    assert "read_peer_artifact" in out
    assert "/pipe-123/brc-transcript" in out
    lowered = out.lower()
    assert "reseed" in lowered and "bound" in lowered, (
        "queryable-env section did not carry the honest-limit contract"
    )


def test_enrichment_is_stale_round_trips_on_sha_match() -> None:
    """``enrichment_is_stale`` is fresh on a SHA match and stale otherwise.

    Behavioural call (not a source grep): a stamp equal to the current proposal
    SHA is fresh; a differing stamp, a missing stamp, and a missing current SHA
    are all stale (fail-safe).
    """
    fn = _renderer("enrichment_is_stale")
    assert fn("abc123", "abc123") is False, "matching SHA wrongly flagged stale"
    assert fn("old456", "abc123") is True, "differing SHA not flagged stale"
    assert fn("", "abc123") is True, "missing stamp not flagged stale (fail-safe)"
    assert fn("abc123", "") is True, "missing current SHA not flagged stale (fail-safe)"
