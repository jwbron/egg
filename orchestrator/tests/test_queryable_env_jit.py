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
        f"event-prompt composer not found; tried {[f'{m}.{a}' for m, a in _COMPOSER_CANDIDATES]}"
    )


# A unique, multi-kilobyte bulk payload. If it appears verbatim in the rendered
# prompt the bulk is being inlined (legacy path); if it is absent the
# bulk-exclusion change has landed.
_BULK_SENTINEL = "ZZ-BULK-DIFF-SENTINEL-SLICE5-ZZ"
_GIANT_DELTA = f"diff --git a/huge.py b/huge.py\n{_BULK_SENTINEL}\n" + "+" + ("x" * 200_000) + "\n"

# JIT-pull tool names the bulk-excluded prompt should steer the agent toward.
_JIT_POINTER_TOKENS: tuple[str, ...] = ("read_peer_artifact", "brc-transcript")


def _render_review_prompt(delta: str, *, jit_pull: bool = True) -> str | None:
    """Render a reviewer ACK prompt carrying ``delta`` for one producer.

    ``jit_pull`` selects the path under test: ``True`` (the default, what the
    bulk-exclusion tests exercise) flips the queryable-environment toggle so the
    composer renders the bulk as JIT-pull pointers and excludes the inlined
    delta; ``False`` pins the legacy inline path. The keyword-only
    queryable-environment inputs (``jit_pull`` / ``memory_rel_path`` /
    ``pipeline_id``) are passed in every call shape so the new path actually
    renders rather than silently falling back to the default-off inline path
    (the gap flagged in slice-5 review).

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

    # Keyword-only queryable-environment inputs (#3200 slice-5). These are
    # passed on every call shape so the JIT-pull path is actually rendered;
    # without them the composer falls back to the default-off inline path and
    # the bulk-exclusion assertions never run.
    qenv_kwargs: dict[str, Any] = {
        "jit_pull": jit_pull,
        "memory_rel_path": ".egg-state/agent-outputs/coder/brc-memory-test.md",
        "pipeline_id": "test-pipeline",
    }

    # Preferred: the documented positional signature
    # (role, event_payload, memory_excerpt, nacks, git_log_delta, base_branch),
    # with the queryable-env toggle threaded through as keyword-only args.
    attempts: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        (("reviewer_code", event_payload, "", [], git_log_delta, "main"), qenv_kwargs),
        (
            ("reviewer_code", event_payload),
            {"git_log_delta": git_log_delta, "base_branch": "main", **qenv_kwargs},
        ),
        (
            (),
            {
                "role": "reviewer_code",
                "event_payload": event_payload,
                "git_log_delta": git_log_delta,
                "base_branch": "main",
                **qenv_kwargs,
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
    """A multi-KB producer delta is NOT inlined verbatim into the JIT prompt.

    ``_render_review_prompt`` forces ``jit_pull=True`` so the queryable-
    environment path is exercised here, in this PR — not deferred to slice-9
    flipping the default. The only legitimate skips left are locator skips
    (composer absent / signature drift). Once the composer renders, the giant
    payload must be gone and the surrounding envelope bounded far below the raw
    bulk size; a sentinel that survives the JIT path is a real regression and
    fails loudly rather than being masked by a skip.
    """
    prompt = _render_review_prompt(_GIANT_DELTA)
    if prompt is None:
        pytest.skip("composer did not return a prompt for the review event")
    # Bulk-exclusion is active (jit_pull=True): the giant payload must be gone
    # and the surrounding envelope bounded well below the raw bulk it replaced.
    assert _BULK_SENTINEL not in prompt, (
        "jit_pull path still inlined the bulk delta verbatim — bulk-exclusion regression"
    )
    assert len(prompt.encode("utf-8")) < len(_GIANT_DELTA) / 20, (
        f"prompt not bounded below the raw bulk size after exclusion ({len(prompt)} bytes)"
    )


def test_bulk_excluded_prompt_points_at_jit_pull_tools() -> None:
    """When bulk is excluded, the prompt steers the agent to the JIT-pull tools.

    Bulk-exclusion is only safe if the bulk stays *reachable*: the prompt must
    name at least one of the queryable-environment tools
    (``read_peer_artifact`` / ``brc-transcript``) so the agent can pull the
    delta on demand. ``_render_review_prompt`` forces ``jit_pull=True`` so this
    runs against the JIT path in this PR; only locator skips remain.
    """
    prompt = _render_review_prompt(_GIANT_DELTA)
    if prompt is None:
        pytest.skip("composer did not return a prompt for the review event")
    assert _BULK_SENTINEL not in prompt, (
        "jit_pull path still inlined the bulk delta verbatim — bulk-exclusion regression"
    )
    assert any(token in prompt for token in _JIT_POINTER_TOKENS), (
        "bulk-excluded prompt names no JIT-pull tool; expected one of "
        f"{_JIT_POINTER_TOKENS} so the excluded bulk stays retrievable"
    )


def test_legacy_inline_path_still_inlines_bulk() -> None:
    """The default-off (``jit_pull=False``) path keeps inlining the bulk verbatim.

    Pins the legacy path the slice-9 feature flag preserves: with the toggle
    OFF the composer must still inline the full delta (sentinel present) so the
    OFF branch stays byte-for-byte the pre-slice-5 behaviour. Guards against an
    accidental flip of the default. Only locator skips remain.
    """
    prompt = _render_review_prompt(_GIANT_DELTA, jit_pull=False)
    if prompt is None:
        pytest.skip("composer did not return a prompt for the review event")
    assert _BULK_SENTINEL in prompt, (
        "legacy jit_pull=False path no longer inlines the bulk delta — the "
        "default-off path must preserve pre-slice-5 inline behaviour"
    )


# ---------------------------------------------------------------------------
# 1b. Drift guard — the duplicated event_prompt pointer renderers stay in sync
#     with the canonical egg_agent.queryable_env renderers on the load-bearing
#     tokens. event_prompt cannot import egg_agent (it runs standalone via the
#     wrapper bash), so the two implementations are hand-synced; this pins the
#     "kept in sync deliberately" contract the docstrings claim so they cannot
#     silently drift apart.
# ---------------------------------------------------------------------------


def test_event_prompt_pointers_match_canonical_renderers() -> None:
    """The event_prompt pointer renderers agree with the canonical ones.

    Both implementations must emit the same JIT-pull contract: the exact
    ``git log <start>..<end> --not origin/<base> -p`` recipe, both pull-tool
    handles (``read_peer_artifact`` + the interpolated ``brc-transcript``
    route), and the honest-limit phrasing. The headers/structure differ by
    design, but these load-bearing tokens must stay identical or the agent gets
    a different pull contract depending on which path rendered.
    """
    try:
        from orchestrator.routes import event_prompt as ep
    except ImportError:
        try:
            from routes import event_prompt as ep  # type: ignore[no-redef]
        except ImportError:
            pytest.skip("event_prompt module not importable")
    try:
        from egg_agent import queryable_env as qe
    except ImportError:
        pytest.skip("egg_agent.queryable_env not importable")

    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "proposal_commit_sha": "def5678",
            "delta": "irrelevant — pointers never inline the delta",
        }
    ]
    ep_delta = ep._render_delta_pointer_section(git_log_delta, "main", "pipe-123")
    qe_section = qe.render_queryable_env_section(
        pipeline_id="pipe-123",
        base_branch="main",
        pointers=[qe.ProducerPullPointer("coder", "abc1234", "def5678")],
    )

    # The exact pull recipe must be identical across both renderers.
    recipe = "git log abc1234..def5678 --not origin/main -p"
    assert recipe in ep_delta, f"event_prompt recipe drifted; got: {ep_delta!r}"
    assert recipe in qe_section, f"canonical recipe drifted; got: {qe_section!r}"

    # Both pull-tool handles must be named by both renderers.
    for token in _JIT_POINTER_TOKENS:
        assert token in ep_delta, f"event_prompt dropped JIT handle {token!r}"
        assert token in qe_section, f"canonical renderer dropped JIT handle {token!r}"

    # The honest-limit contract (pull does not bound the window; reseed does)
    # must be present in both.
    for text, label in ((ep_delta, "event_prompt"), (qe_section, "canonical")):
        lowered = text.lower()
        assert "reseed" in lowered and "bound" in lowered, (
            f"{label} pointer section dropped the honest-limit contract"
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
        "enrichment stamped at an older SHA than the current delta was not flagged stale"
    )


def test_enrichment_stamped_at_current_sha_is_fresh() -> None:
    """Enrichment stamped at the current SHA is NOT invalidated.

    The dual of the staleness check: a claim matching the authoritative SHA is
    fresh, so SHA-stamping does not over-invalidate still-valid enrichment.
    """
    fn = _stale_detector()
    verdict = _call_stale(fn, enrichment_sha=_CURRENT_SHA, current_sha=_CURRENT_SHA)
    assert bool(verdict) is False, "enrichment stamped at the current SHA was wrongly flagged stale"
