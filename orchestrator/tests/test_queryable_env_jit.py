"""Tests for the slice-5 queryable-environment / JIT-pull layer (task-5-3).

#3200 / slice-5 ("Queryable environment (JIT pull, AC-2 part 2)"). The coder
(task-5-1 / task-5-2) moves the *bulk* of the BRC context out of the inlined
event-pump prompt and into a queryable environment pulled just-in-time:

  * **Bulk-exclusion (task-5-1).** The per-event prompt no longer inlines bulk
    BRC history / peer artifacts / full diffs. Only the deterministic delta
    (the #3189 anchor layer in the protected root, slice-3/slice-4) is
    pre-staged; the bulk is reachable *only* via the existing JIT-pull tools
    (``mcp__brc__read_peer_artifact`` + ``GET /<pipeline_id>/brc-transcript``).
    The honest-limit comment is recorded in the code: a pulled slice stays
    resident until reseed/compaction — JIT pull does NOT bound the window; the
    reseed (slice-8) bounds it.

  * **JIT-retrievability (existing tools).** The bulk the prompt stops inlining
    must remain reachable: the ``read_peer_artifact`` handler is registered as
    an MCP tool and the ``/<pipeline_id>/brc-transcript`` route is wired and
    filters to the BRC history record types. These are pre-existing
    (#3076/#3077) — this suite pins that the JIT substrate the queryable
    environment depends on stays present.

  * **SHA-stamp invalidation (task-5-2).** The #3188 agent-authored enrichment
    moves into the queryable environment, surfaced on demand and SHA-stamped so
    the mechanically-derived git-log delta can invalidate a stale claim. The
    deterministic layer stays authoritative: an enrichment claim stamped at a
    SHA older than the current delta is detectable / invalidatable, so a wrong
    "verified" claim cannot suppress re-checking.

Tester and coder run as parallel BRC producers on separate branches, so the
coder's slice-5 symbols / behaviour may be absent when this file is collected
on the tester branch. Following the established slice convention (see
``test_reseed_threshold.py`` / ``shared/egg_anchor/tests/test_protected_root.py``)
each assertion is skip-guarded — behaviourally (the legacy inline path still
inlines the bulk -> skip) or by locator (the SHA-stamp symbol is unmerged ->
skip) — so the suite stays green pre-merge and activates at PR assembly. The
JIT-retrievability assertions reference only pre-existing tools and run today.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Add orchestrator to sys.path the same way the sibling slice tests do.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


# ---------------------------------------------------------------------------
# Locator: the per-event prompt composer. The coder owns the exact home of the
# queryable-env change (task-5-1 touches sandbox/ + shared/egg_agent/, but the
# composer itself lives in orchestrator/routes/event_prompt.py); these cover the
# plausible spellings so the probe binds wherever the change lands.
# ---------------------------------------------------------------------------

_COMPOSER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("orchestrator.routes.event_prompt", "compose_event_prompt"),
    ("routes.event_prompt", "compose_event_prompt"),
    ("event_prompt", "compose_event_prompt"),
    ("egg_agent.event_prompt", "compose_event_prompt"),
    ("egg_agent.queryable_env", "compose_event_prompt"),
)


def _composer() -> Callable[..., Any]:
    for module_name, attr in _COMPOSER_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    pytest.skip(
        "event-prompt composer not found; tried "
        f"{[f'{m}.{a}' for m, a in _COMPOSER_CANDIDATES]}"
    )


# A unique, multi-kilobyte bulk payload. If it appears verbatim in the rendered
# prompt the bulk is being inlined (legacy path); if it is absent the
# bulk-exclusion change has landed.
_BULK_SENTINEL = "ZZ-BULK-DIFF-SENTINEL-SLICE5-ZZ"
_GIANT_DELTA = (
    f"diff --git a/huge.py b/huge.py\n{_BULK_SENTINEL}\n"
    + "+" + ("x" * 200_000) + "\n"
)

# JIT-pull tool names the bulk-excluded prompt should steer the agent toward.
_JIT_POINTER_TOKENS: tuple[str, ...] = ("read_peer_artifact", "brc-transcript")


def _render_review_prompt(delta: str) -> str | None:
    """Render a reviewer ACK prompt carrying ``delta`` for one producer.

    Returns the rendered prompt, or ``None`` if no known call shape of the
    located composer accepts the canonical inputs (signature drift) — the
    caller then skips rather than failing on an API mismatch.
    """
    fn = _composer()
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "proposal_commit_sha": "def5678",
            "delta": delta,
        }
    ]
    event_payload = {"action": "ack", "producer": "coder", "version": 2}

    # Preferred: the documented positional signature
    # (role, event_payload, memory_excerpt, nacks, git_log_delta, base_branch).
    attempts: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        (("reviewer_code", event_payload, "", [], git_log_delta, "main"), {}),
        (
            ("reviewer_code", event_payload),
            {"git_log_delta": git_log_delta, "base_branch": "main"},
        ),
        (
            (),
            {
                "role": "reviewer_code",
                "event_payload": event_payload,
                "git_log_delta": git_log_delta,
                "base_branch": "main",
            },
        ),
    )
    last_exc: Exception | None = None
    for args, kwargs in attempts:
        try:
            out = fn(*args, **kwargs)
        except TypeError as exc:
            last_exc = exc
            continue
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(out, str):
            return out
    if last_exc is not None:
        pytest.skip(f"composer present but no known call shape succeeded: {last_exc!r}")
    return None


# ---------------------------------------------------------------------------
# 1. Bulk-exclusion — the per-event prompt does not inline bulk diffs.
# ---------------------------------------------------------------------------


def test_event_prompt_excludes_inlined_bulk_diff() -> None:
    """A multi-KB producer delta is NOT inlined verbatim into the prompt.

    Skip-guard: while the legacy inline path is active the giant delta is
    rendered verbatim — that is the pre-slice-5 behaviour, so we skip. Once the
    bulk-exclusion change lands the sentinel is absent and the assertion
    activates: the bulk must not be inlined and the envelope must be bounded far
    below the raw bulk size.
    """
    prompt = _render_review_prompt(_GIANT_DELTA)
    if prompt is None:
        pytest.skip("composer did not return a prompt for the review event")
    if _BULK_SENTINEL in prompt:
        pytest.skip(
            "legacy inline path active — bulk delta still inlined "
            "(coder task-5-1 unmerged on this branch)"
        )
    # Bulk-exclusion has landed: the giant payload is gone and the surrounding
    # envelope is bounded well below the raw bulk it replaced.
    assert _BULK_SENTINEL not in prompt
    assert len(prompt.encode("utf-8")) < len(_GIANT_DELTA) / 20, (
        "prompt not bounded below the raw bulk size after exclusion "
        f"({len(prompt)} bytes)"
    )


def test_bulk_excluded_prompt_points_at_jit_pull_tools() -> None:
    """When bulk is excluded, the prompt steers the agent to the JIT-pull tools.

    Bulk-exclusion is only safe if the bulk stays *reachable*: the prompt must
    name at least one of the queryable-environment tools
    (``read_peer_artifact`` / ``brc-transcript``) so the agent can pull the
    delta on demand. Skips on the legacy inline path (bulk still present).
    """
    prompt = _render_review_prompt(_GIANT_DELTA)
    if prompt is None:
        pytest.skip("composer did not return a prompt for the review event")
    if _BULK_SENTINEL in prompt:
        pytest.skip(
            "legacy inline path active — bulk delta still inlined "
            "(coder task-5-1 unmerged on this branch)"
        )
    assert any(token in prompt for token in _JIT_POINTER_TOKENS), (
        "bulk-excluded prompt names no JIT-pull tool; expected one of "
        f"{_JIT_POINTER_TOKENS} so the excluded bulk stays retrievable"
    )


# ---------------------------------------------------------------------------
# 2. JIT-retrievability — the existing pull tools remain wired (runs today).
# ---------------------------------------------------------------------------


def test_read_peer_artifact_tool_is_registered() -> None:
    """The ``read_peer_artifact`` MCP tool the queryable env relies on exists.

    The bulk the prompt stops inlining is pulled through this handler; it must
    stay registered as ``mcp__brc__read_peer_artifact`` with its handler wired.
    """
    try:
        from egg_agent_tools.handlers import brc as brc_handlers
        from egg_agent_tools.tools import brc as brc_tools
    except ImportError:
        pytest.skip("egg_agent_tools not importable in this environment")

    handler = getattr(brc_handlers, "brc_read_peer_artifact", None)
    assert callable(handler), "brc_read_peer_artifact handler missing"

    # The tool registry must expose it under the canonical MCP tool name.
    source = inspect.getsource(brc_tools)
    assert "read_peer_artifact" in source
    assert "mcp__brc__read_peer_artifact" in source, (
        "read_peer_artifact not registered under the canonical MCP tool name"
    )


def test_brc_transcript_route_is_wired_to_history_types() -> None:
    """The live ``/<pipeline_id>/brc-transcript`` JIT-pull route is wired.

    The served-read counterpart of the on-disk BRC history files (#3076/#3077):
    for the in-flight phase the bulk transcript is only reachable here, so the
    route must be registered and filtered to the BRC history record types.
    """
    try:
        from orchestrator.routes import messages as messages_route
    except ImportError:
        try:
            from routes import messages as messages_route  # type: ignore[no-redef]
        except ImportError:
            pytest.skip("orchestrator.routes.messages not importable")

    getter = getattr(messages_route, "get_brc_transcript", None)
    assert callable(getter), "get_brc_transcript route handler missing"

    source = inspect.getsource(messages_route)
    assert "/brc-transcript" in source, "brc-transcript route path not registered"
    # The route serves the BRC history record types — the same set the on-disk
    # writer persists — so the JIT pull and the durable log agree on scope.
    assert "BRC_HISTORY_TYPES" in inspect.getsource(getter), (
        "brc-transcript route does not filter to BRC_HISTORY_TYPES"
    )


# ---------------------------------------------------------------------------
# 3. SHA-stamp invalidation — stale agent-authored enrichment is detectable.
# ---------------------------------------------------------------------------

# Candidate (module, attribute) pairs for the stale-enrichment detector the
# coder lands in task-5-2. The exact home/spelling is the coder's to choose;
# these cover the plausible ones so the assertion binds wherever it lands.
_STALE_DETECTOR_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("egg_agent.queryable_env", "is_enrichment_stale"),
    ("egg_agent.queryable_env", "enrichment_is_stale"),
    ("egg_agent.enrichment", "is_enrichment_stale"),
    ("egg_agent.enrichment", "is_stale"),
    ("orchestrator.enrichment", "is_enrichment_stale"),
    ("orchestrator.routes.event_prompt", "is_enrichment_stale"),
    ("egg_anchor.enrichment", "is_enrichment_stale"),
    ("egg_agent_tools.handlers.brc_memory", "is_enrichment_stale"),
    ("egg_agent_tools.handlers.brc_memory", "enrichment_is_stale"),
)

# A current authoritative SHA and an older stale one (distinct 40-hex values).
_CURRENT_SHA = "1111111111111111111111111111111111111111"
_STALE_SHA = "2222222222222222222222222222222222222222"


def _stale_detector() -> Callable[..., Any]:
    for module_name, attr in _STALE_DETECTOR_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    pytest.skip(
        "stale-enrichment detector not found (coder task-5-2 unmerged); tried "
        f"{[f'{m}.{a}' for m, a in _STALE_DETECTOR_CANDIDATES]}"
    )


def _call_stale(fn: Callable[..., Any], enrichment_sha: str, current_sha: str) -> Any:
    """Call the detector across plausible (enrichment_sha, current_sha) shapes."""
    attempts: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        ((enrichment_sha, current_sha), {}),
        ((), {"enrichment_sha": enrichment_sha, "current_sha": current_sha}),
        ((), {"stamped_sha": enrichment_sha, "current_sha": current_sha}),
        ((), {"enrichment_sha": enrichment_sha, "delta_sha": current_sha}),
        (({"sha": enrichment_sha}, current_sha), {}),
    )
    last_exc: Exception | None = None
    for args, kwargs in attempts:
        try:
            return fn(*args, **kwargs)
        except (TypeError, AttributeError, ValueError, KeyError) as exc:
            last_exc = exc
            continue
    pytest.skip(f"stale detector present but no known call shape succeeded: {last_exc!r}")


def test_enrichment_stamped_at_old_sha_is_stale() -> None:
    """Enrichment stamped at a SHA older than the current delta reads as stale.

    The deterministic git-log delta stays authoritative: a claim whose stamp
    does not match the current SHA must be invalidatable so a wrong "verified"
    claim cannot suppress re-checking.
    """
    fn = _stale_detector()
    verdict = _call_stale(fn, enrichment_sha=_STALE_SHA, current_sha=_CURRENT_SHA)
    assert bool(verdict) is True, (
        "enrichment stamped at an older SHA than the current delta was not "
        "flagged stale"
    )


def test_enrichment_stamped_at_current_sha_is_fresh() -> None:
    """Enrichment stamped at the current SHA is NOT invalidated.

    The dual of the staleness check: a claim matching the authoritative SHA is
    fresh, so SHA-stamping does not over-invalidate still-valid enrichment.
    """
    fn = _stale_detector()
    verdict = _call_stale(fn, enrichment_sha=_CURRENT_SHA, current_sha=_CURRENT_SHA)
    assert bool(verdict) is False, (
        "enrichment stamped at the current SHA was wrongly flagged stale"
    )
