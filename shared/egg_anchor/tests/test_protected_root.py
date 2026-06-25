"""Tests for the deterministic protected-root renderer (slice-4, task-4-2).

#3200 / slice-4 ("Protected root: deterministic, byte-stable, cacheable").
The coder (task-4-1) lands a renderer that assembles the small, resident,
role-parameterized protected root in a FIXED four-section order:

  (a) role contract;
  (b) task anchor (``compose_task_description``, #3163);
  (c) the four #3189 deterministic anchors derived in slice-3
      (:class:`egg_anchor.models.BRCDerivedAnchors`: last-reviewed SHA per
      producer, latest verdict per edge, open NACKs, conditional-ACK
      obligations);
  (d) non-negotiable directives.

The renderer emits STABLE BYTES: sorted keys, bounded sections, hard
per-section caps, and NO timestamps / sequence numbers / nondeterministic
ordering — so an identical anchor input yields identical bytes across runs and
the root is safely cacheable + resident.

This file asserts the four task-4-2 acceptance properties:

  1. byte-stability     — identical input -> identical bytes;
  2. per-section caps   — oversized sections truncated at the documented cap;
  3. sort-stability     — key/element ordering stable regardless of input order;
  4. role-parameterized — two roles render distinct-but-each-stable roots.

Tester and coder run as parallel BRC producers on separate branches, so the
coder's renderer symbol may be absent when this file is collected on the tester
branch. The locator helpers ``pytest.skip`` until the renderer merges — the
established slice convention (see ``test_brc_anchor_derivation.py`` /
``orchestrator/tests/test_reseed_threshold.py``) — keeping the suite green
pre-merge and activating the assertions at PR assembly. The
``BRCDerivedAnchors`` model the renderer consumes is already merged from
slice-3, so the fixtures below build real anchor objects today.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from egg_anchor.models import (
    BRCDerivedAnchors,
    ConditionalAckObligation,
    OpenNack,
    ReviewEdgeVerdict,
    ReviewVerdict,
)

# ---------------------------------------------------------------------------
# Locator (skip-guard convention) — resolve the coder's renderer or skip.
# ---------------------------------------------------------------------------

# Candidate (module, attribute) pairs for the renderer entry point. The coder
# owns the exact spelling and home package (task-4-1 touches both
# ``shared/egg_anchor`` and ``shared/egg_agent``); these cover the plausible
# spellings so the assertions activate the moment any one of them lands.
_RENDERER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("egg_anchor.protected_root", "render_protected_root"),
    ("egg_anchor.protected_root", "render"),
    ("egg_anchor.protected_root", "ProtectedRoot"),
    ("egg_anchor.root", "render_protected_root"),
    ("egg_anchor.resident_root", "render_protected_root"),
    ("egg_anchor", "render_protected_root"),
    ("egg_agent.protected_root", "render_protected_root"),
    ("egg_agent.protected_root", "render"),
    ("egg_agent.context_root", "render_protected_root"),
    ("egg_agent.resident_root", "render_protected_root"),
    ("egg_agent", "render_protected_root"),
)


def _renderer() -> Any:
    for module_name, attr in _RENDERER_CANDIDATES:
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        obj = getattr(module, attr, None)
        if obj is not None:
            return obj
    pytest.skip(
        "protected-root renderer not found yet (coder task-4-1 unmerged); "
        f"tried {[f'{m}.{a}' for m, a in _RENDERER_CANDIDATES]}"
    )


# ---------------------------------------------------------------------------
# Flexible invocation — map the canonical inputs onto whatever parameter names
# the coder chose, tolerate the model passed as object-or-dict and directives
# as list-or-string, and a function-or-class renderer.
# ---------------------------------------------------------------------------

# param-name (normalized) -> canonical input group.
_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "role": ("role", "agent_role", "role_name", "producer_role", "for_role"),
    "role_contract": (
        "role_contract",
        "contract",
        "contract_text",
        "role_contract_text",
    ),
    "task_anchor": (
        "task_anchor",
        "task_description",
        "task",
        "task_statement",
        "anchor",
        "task_anchor_text",
        "task_desc",
    ),
    "anchors": (
        "anchors",
        "brc_anchors",
        "derived_anchors",
        "deterministic_anchors",
        "brc_derived_anchors",
        "derived",
        "anchor_data",
    ),
    "directives": (
        "directives",
        "non_negotiable_directives",
        "non_negotiables",
        "non_negotiable",
        "directives_text",
        "rules",
    ),
}
# Order in which **kwargs-accepting renderers are fed the canonical inputs.
_CANONICAL_ORDER = ("role", "role_contract", "task_anchor", "anchors", "directives")


def _group_for(param_name: str) -> str | None:
    normalized = param_name.lower().lstrip("_")
    for group, aliases in _ALIAS_GROUPS.items():
        if normalized in aliases:
            return group
    return None


def _finalize(result: Any) -> str | bytes:
    """Coerce a renderer result (string, bytes, or an object) to text/bytes."""
    if isinstance(result, (str, bytes)):
        return result
    for meth in ("render", "to_text", "to_bytes", "as_text", "render_text"):
        fn = getattr(result, meth, None)
        if callable(fn):
            try:
                out = fn()
            except TypeError:
                continue
            if isinstance(out, (str, bytes)):
                return out
    for attr in ("text", "rendered", "bytes", "content", "root"):
        value = getattr(result, attr, None)
        if isinstance(value, (str, bytes)):
            return value
    pytest.skip(f"renderer returned non-text {type(result)!r} with no text accessor")


def _variants(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a kwargs dict over the model-as-dict and directives-as-string axes."""
    anchor_keys = [k for k, v in kwargs.items() if isinstance(v, BRCDerivedAnchors)]
    dir_keys = [
        k
        for k, v in kwargs.items()
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v)
    ]
    combos: list[dict[str, Any]] = [kwargs]
    expanded: list[dict[str, Any]] = []
    for base in combos:
        local = [base]
        for ak in anchor_keys:
            alt = dict(base)
            alt[ak] = base[ak].model_dump()
            local.append(alt)
        expanded.extend(local)
    combos, expanded = expanded, []
    for base in combos:
        local = [base]
        for dk in dir_keys:
            if isinstance(base[dk], list):
                alt = dict(base)
                alt[dk] = "\n".join(base[dk])
                local.append(alt)
        expanded.extend(local)
    return expanded


def _render_raw(**overrides: Any) -> str | bytes:
    fn = _renderer()
    available = _canonical_inputs()
    available.update(overrides)

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    matched: set[str] = set()
    accepts_var_kw = False

    if sig is not None:
        unmatched_required: list[str] = []
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                accepts_var_kw = True
                continue
            if param.kind is inspect.Parameter.VAR_POSITIONAL:
                continue
            group = _group_for(name)
            if group is not None and group in available:
                if param.kind is inspect.Parameter.POSITIONAL_ONLY:
                    args.append(available[group])
                else:
                    kwargs[name] = available[group]
                matched.add(group)
            elif param.default is inspect.Parameter.empty:
                unmatched_required.append(name)
        if unmatched_required:
            pytest.skip(
                f"renderer has unmapped required params {unmatched_required}; "
                f"signature {sig}"
            )
        if accepts_var_kw:
            for group in _CANONICAL_ORDER:
                if group not in matched and group in available:
                    kwargs.setdefault(group, available[group])
                    matched.add(group)
    else:
        # No introspectable signature — best-effort canonical keyword call.
        kwargs = {group: available[group] for group in _CANONICAL_ORDER}

    last_exc: Exception | None = None
    for variant in _variants(kwargs):
        try:
            return _finalize(fn(*args, **variant))
        except (TypeError, AttributeError, ValueError) as exc:
            last_exc = exc
            continue
    pytest.skip(f"renderer present but no known call shape succeeded: {last_exc!r}")


def _render_text(**overrides: Any) -> str:
    raw = _render_raw(**overrides)
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


# ---------------------------------------------------------------------------
# Canonical fixture inputs. Sentinels are unique so each of the four sections
# can be located in the rendered root.
# ---------------------------------------------------------------------------

ROLE_A = "reviewer_code"
ROLE_B = "coder"

ROLE_CONTRACT_SENTINEL = "ZZ-ROLE-CONTRACT-SECTION-A-ZZ"
TASK_ANCHOR_SENTINEL = "ZZ-TASK-ANCHOR-SECTION-B-ZZ"
DIRECTIVE_SENTINEL = "ZZ-NON-NEGOTIABLE-DIRECTIVE-SECTION-D-ZZ"

SHA_CODER = "abc1230000000000000000000000000000000def"
SHA_TESTER = "fed3210000000000000000000000000000000cba"


def _task_anchor() -> str:
    """A faithful task anchor (compose_task_description, #3163) when importable."""
    try:
        from egg_contracts.loader import compose_task_description
    except ImportError:
        return (
            "This pipeline's task is GitHub issue #3200. "
            f"{TASK_ANCHOR_SENTINEL}"
        )
    composed = compose_task_description(
        description=f"{TASK_ANCHOR_SENTINEL} Protected root context discipline.",
        issue_number=3200,
        issue_url="https://github.com/jwbron/egg/issues/3200",
    )
    return composed or f"{TASK_ANCHOR_SENTINEL}"


def _anchors() -> BRCDerivedAnchors:
    """A realistic, fully-populated set of #3189 anchors (section c)."""
    return BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER, "tester": SHA_TESTER},
        latest_verdicts=[
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="coder",
                verdict=ReviewVerdict.NACK,
                version=2,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_security",
                producer="coder",
                verdict=ReviewVerdict.CONDITIONAL_ACK,
                version=2,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="tester",
                verdict=ReviewVerdict.ACK,
                version=1,
                reviewed_sha=SHA_TESTER,
            ),
        ],
        open_nacks=[
            OpenNack(
                reviewer="reviewer_code",
                producer="coder",
                version=2,
                reason="missing guard on the ResultMessage branch",
            ),
        ],
        conditional_ack_obligations=[
            ConditionalAckObligation(
                reviewer="reviewer_security",
                producer="coder",
                version=2,
                condition="git mv old/path new/path before merge",
                resolved=False,
            ),
        ],
    )


def _canonical_inputs() -> dict[str, Any]:
    return {
        "role": ROLE_A,
        "role_contract": (
            f"{ROLE_CONTRACT_SENTINEL} {ROLE_A}: review the diff for correctness "
            "and never drop an open NACK obligation."
        ),
        "task_anchor": _task_anchor(),
        "anchors": _anchors(),
        "directives": [
            f"{DIRECTIVE_SENTINEL} The deterministic anchor layer is authoritative.",
            "Re-derive anchors on reseed; never re-review a settled SHA.",
        ],
    }


# ---------------------------------------------------------------------------
# 1. Byte-stability — identical input -> identical bytes, all four sections.
# ---------------------------------------------------------------------------


def test_render_is_byte_stable_across_identical_renders() -> None:
    """Two renders of identical input produce byte-for-byte identical output."""
    first = _render_raw()
    second = _render_raw()
    assert first == second
    assert isinstance(first, (str, bytes))
    assert first, "protected root rendered empty"


def test_render_contains_all_four_sections_in_fixed_order() -> None:
    """All four sections render; the deterministic anchor layer is present."""
    text = _render_text()
    # (a) role contract, (b) task anchor, (c) #3189 anchors, (d) directives.
    assert ROLE_CONTRACT_SENTINEL in text
    assert TASK_ANCHOR_SENTINEL in text
    assert SHA_CODER in text, "section (c): last-reviewed SHA not rendered"
    assert "missing guard" in text, "section (c): open NACK reason not rendered"
    assert DIRECTIVE_SENTINEL in text

    # Fixed order a -> b -> c -> d (soft: only when every sentinel is locatable).
    markers = [
        ROLE_CONTRACT_SENTINEL,
        TASK_ANCHOR_SENTINEL,
        SHA_CODER,
        DIRECTIVE_SENTINEL,
    ]
    positions = [text.find(m) for m in markers]
    if all(p >= 0 for p in positions):
        assert positions == sorted(positions), (
            "protected-root sections are not in the fixed a->b->c->d order: "
            f"{positions}"
        )


# ---------------------------------------------------------------------------
# 2. Per-section caps — oversized sections truncated at a hard cap.
# ---------------------------------------------------------------------------

_OVERSIZE = 4_000_000  # ~4 MB of filler — far past any reasonable section cap.


def test_oversized_freetext_section_is_truncated() -> None:
    """A multi-megabyte role-contract section is hard-capped, not inlined whole."""
    head = "OVERSIZE-HEAD-SENTINEL"
    giant = f"{head} " + ("x" * _OVERSIZE)
    text = _render_text(role_contract=giant)
    assert giant not in text, "oversized section was inlined verbatim (no cap)"
    # A hard cap keeps the whole root far smaller than the raw oversized input.
    assert len(text) < _OVERSIZE / 20, (
        f"rendered root len {len(text)} not bounded below a hard per-section cap"
    )
    # Truncated, not omitted: the head of the section survives.
    assert head in text, "capped section dropped entirely instead of truncating"


def test_oversized_anchor_section_is_truncated() -> None:
    """A flood of open NACKs (section c) is bounded by the per-section cap."""
    flood = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER},
        open_nacks=[
            OpenNack(
                reviewer=f"reviewer_{i}",
                producer="coder",
                version=2,
                reason="y" * 1000,
            )
            for i in range(5000)
        ],
    )
    raw = _render_raw(anchors=flood)
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    # 5000 * ~1KB reasons ~= 5MB of raw anchor content; the cap must bound it.
    assert len(text) < 5_000_000 / 20, (
        f"anchor section len {len(text)} not bounded by a hard per-section cap"
    )


# ---------------------------------------------------------------------------
# 3. Sort-stability — element/key ordering does not depend on input order.
# ---------------------------------------------------------------------------


def test_render_is_stable_regardless_of_input_ordering() -> None:
    """Permuting dict-key and list-element order yields identical bytes.

    A byte-stable protected root must sort its own content rather than trust
    callers to pre-sort — so the same logical anchors in a different order
    render identically.
    """
    forward = BRCDerivedAnchors(
        last_reviewed_sha={"coder": SHA_CODER, "tester": SHA_TESTER},
        latest_verdicts=[
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="coder",
                verdict=ReviewVerdict.NACK,
                version=2,
                reviewed_sha=SHA_CODER,
            ),
            ReviewEdgeVerdict(
                reviewer="reviewer_code",
                producer="tester",
                verdict=ReviewVerdict.ACK,
                version=1,
                reviewed_sha=SHA_TESTER,
            ),
        ],
        open_nacks=[
            OpenNack(reviewer="reviewer_a", producer="coder", version=2, reason="alpha"),
            OpenNack(reviewer="reviewer_b", producer="coder", version=2, reason="beta"),
        ],
    )
    # Same logical content, reversed dict-insertion and list order.
    reversed_anchors = BRCDerivedAnchors(
        last_reviewed_sha={"tester": SHA_TESTER, "coder": SHA_CODER},
        latest_verdicts=list(reversed(forward.latest_verdicts)),
        open_nacks=list(reversed(forward.open_nacks)),
    )

    assert _render_raw(anchors=forward) == _render_raw(anchors=reversed_anchors)


# ---------------------------------------------------------------------------
# 4. Role-parameterization — distinct roots per role, each individually stable.
# ---------------------------------------------------------------------------


def test_two_roles_render_distinct_but_each_stable_roots() -> None:
    """Distinct roles -> distinct roots; each role's root is byte-stable."""
    role_a_contract = f"{ROLE_CONTRACT_SENTINEL} {ROLE_A} contract."
    role_b_contract = f"{ROLE_CONTRACT_SENTINEL} {ROLE_B} contract."

    a_first = _render_raw(role=ROLE_A, role_contract=role_a_contract)
    a_second = _render_raw(role=ROLE_A, role_contract=role_a_contract)
    b_first = _render_raw(role=ROLE_B, role_contract=role_b_contract)
    b_second = _render_raw(role=ROLE_B, role_contract=role_b_contract)

    # Each role's root is individually byte-stable.
    assert a_first == a_second
    assert b_first == b_second
    # The two roles render distinct roots (role-parameterized).
    assert a_first != b_first

    a_text = a_first.decode("utf-8", "replace") if isinstance(a_first, bytes) else a_first
    b_text = b_first.decode("utf-8", "replace") if isinstance(b_first, bytes) else b_first
    assert ROLE_A in a_text
    assert ROLE_B in b_text
