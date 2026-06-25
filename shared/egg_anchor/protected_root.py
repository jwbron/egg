"""Deterministic protected-root renderer (#3200, slice-4).

Assembles the small, stable, permanently-resident *protected root* for an
event-pump BRC agent in a FIXED four-section order:

  (a) role contract               — the role's non-negotiable behavioural spec
  (b) task anchor                 — ``compose_task_description`` output (#3163)
  (c) #3189 deterministic anchors — last-reviewed SHA per producer, latest
                                    verdicts, open NACKs, conditional-ACK
                                    obligations (``BRCDerivedAnchors``, slice-3)
  (d) non-negotiable directives

**Byte stability.** The render is byte-identical for identical input: every
collection is sorted by a deterministic key, list counts are bounded, each
section is hard-capped, and NO timestamps / sequence numbers / nondeterministic
ordering enter the output. Identical ``(role, role_contract, task_description,
derived, directives)`` -> identical bytes — which is exactly what makes the
root a cacheable prompt prefix (warm resume, #3186) and a deterministic reseed
source (the #3200 threshold reseed re-renders the same root).

**Purity.** The renderer accepts already-composed strings — the caller runs
:func:`egg_contracts.loader.compose_task_description` for section (b) — so
``egg_anchor`` takes on no new package dependency and stays importable from the
sandbox, the orchestrator, and tests alike. Section (c) is sourced ONLY from
the mechanically-derived :class:`BRCDerivedAnchors` (never agent-authored
prose), so the authoritative anchor layer cannot drift from the message record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import BRCDerivedAnchors

__all__ = ["RootCaps", "render_protected_root"]


@dataclass(frozen=True)
class RootCaps:
    """Hard per-section caps for the protected root (deterministic knobs).

    Character caps bound the free-form sections; count caps bound the
    derived-anchor lists. All values are deterministic constants — changing
    one changes the output bytes uniformly, never per-render. The defaults are
    deliberately generous: the root is meant to be small, and truncation is a
    safety backstop against a pathological contract, not the common path.
    """

    role_contract_chars: int = 6000
    task_chars: int = 8000
    directives_chars: int = 4000
    reason_chars: int = 300
    condition_chars: int = 300
    max_shas: int = 24
    max_verdicts: int = 48
    max_nacks: int = 24
    max_obligations: int = 24


# Stable, content-free markers. Both are count- or position-derived, so they
# never introduce nondeterminism.
_SECTION_TRUNCATION_MARKER = "\n…[truncated]"
_NONE = "(none)"


def _truncate(text: str, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars`` characters, appending a stable marker.

    Character-based (not byte-based) so the result is always valid UTF-8 and
    deterministic for identical input. ``max_chars`` is a hard ceiling on the
    returned length including the marker.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(_SECTION_TRUNCATION_MARKER))
    return text[:keep].rstrip() + _SECTION_TRUNCATION_MARKER


def _elision(remaining: int) -> str:
    return f"  … (+{remaining} more elided)"


def _section(title: str, body: str) -> str:
    body = body.strip() or _NONE
    return f"## {title}\n{body}"


def _normalize_directives(directives: str | Sequence[str] | None) -> str:
    """Render directives as deterministic text, preserving caller order.

    A directive *list* keeps its input order — order is part of the
    directives' meaning and is deterministic for identical input, so it is NOT
    sorted (unlike the keyed anchor collections below).
    """
    if directives is None:
        return ""
    if isinstance(directives, str):
        return directives.strip()
    items = [d.strip() for d in directives if d and d.strip()]
    return "\n".join(f"- {item}" for item in items)


def _render_anchors(derived: BRCDerivedAnchors | None, caps: RootCaps) -> str:
    """Render the #3189 deterministic anchors with sorted keys + bounded counts."""
    if derived is None:
        return "(no reviewed proposals yet)"

    lines: list[str] = []

    # (i) last-reviewed SHA per producer — sort by producer.
    lines.append("Last-reviewed SHA per producer:")
    shas = sorted(derived.last_reviewed_sha.items())
    if shas:
        for producer, sha in shas[: caps.max_shas]:
            lines.append(f"  {producer}: {sha}")
        if len(shas) > caps.max_shas:
            lines.append(_elision(len(shas) - caps.max_shas))
    else:
        lines.append(f"  {_NONE}")

    # (ii) latest verdict per reviewer->producer edge — sort by (producer, reviewer).
    lines.append("Latest verdicts (reviewer -> producer):")
    verdicts = sorted(derived.latest_verdicts, key=lambda v: (v.producer, v.reviewer))
    if verdicts:
        for v in verdicts[: caps.max_verdicts]:
            sha = f" @ {v.reviewed_sha}" if v.reviewed_sha else ""
            lines.append(f"  {v.reviewer} -> {v.producer}: {v.verdict.value} (v{v.version}){sha}")
        if len(verdicts) > caps.max_verdicts:
            lines.append(_elision(len(verdicts) - caps.max_verdicts))
    else:
        lines.append(f"  {_NONE}")

    # (iii) open NACKs — sort by (producer, reviewer).
    lines.append("Open NACKs (current version, unresolved):")
    nacks = sorted(derived.open_nacks, key=lambda n: (n.producer, n.reviewer))
    if nacks:
        for n in nacks[: caps.max_nacks]:
            reason = _truncate(n.reason, caps.reason_chars) if n.reason else "(no reason given)"
            lines.append(f"  {n.reviewer} -> {n.producer} (v{n.version}): {reason}")
        if len(nacks) > caps.max_nacks:
            lines.append(_elision(len(nacks) - caps.max_nacks))
    else:
        lines.append(f"  {_NONE}")

    # (iv) conditional-ACK obligations — sort by (producer, reviewer).
    lines.append("Conditional-ACK obligations:")
    obligations = sorted(
        derived.conditional_ack_obligations, key=lambda o: (o.producer, o.reviewer)
    )
    if obligations:
        for o in obligations[: caps.max_obligations]:
            status = "resolved" if o.resolved else "OPEN"
            condition = _truncate(o.condition, caps.condition_chars)
            lines.append(f"  {o.reviewer} -> {o.producer} (v{o.version}) [{status}]: {condition}")
        if len(obligations) > caps.max_obligations:
            lines.append(_elision(len(obligations) - caps.max_obligations))
    else:
        lines.append(f"  {_NONE}")

    return "\n".join(lines)


def render_protected_root(
    *,
    role: str,
    role_contract: str,
    task_description: str | None = None,
    derived: BRCDerivedAnchors | None = None,
    directives: str | Sequence[str] | None = None,
    caps: RootCaps | None = None,
) -> str:
    """Render the deterministic, byte-stable protected root for ``role``.

    Args:
        role: The agent role (e.g. ``coder``, ``reviewer_code``). Parameterizes
            the root header so two roles render distinct-but-each-stable roots.
        role_contract: The role's behavioural contract text (section a).
            Hard-capped at ``caps.role_contract_chars``.
        task_description: The task anchor (section b) — compose it with
            :func:`egg_contracts.loader.compose_task_description` so the
            #3163 anchoring is applied uniformly. Hard-capped at
            ``caps.task_chars``. ``None`` renders ``(none)``.
        derived: The mechanically-derived #3189 anchors (section c). ``None``
            renders a "no reviewed proposals yet" placeholder.
        directives: Non-negotiable directives (section d) as a single string or
            an ordered sequence of bullet items. Hard-capped at
            ``caps.directives_chars``.
        caps: Optional override of the per-section caps.

    Returns:
        The assembled root as a single string. Byte-identical for identical
        input.
    """
    caps = caps or RootCaps()
    role = (role or "").strip() or "unknown"

    header = f"=== PROTECTED ROOT — role: {role} ==="
    sections = [
        _section("ROLE CONTRACT", _truncate(role_contract or "", caps.role_contract_chars)),
        _section("TASK", _truncate(task_description or "", caps.task_chars)),
        _section("BRC ANCHORS (#3189)", _render_anchors(derived, caps)),
        _section(
            "NON-NEGOTIABLE DIRECTIVES",
            _truncate(_normalize_directives(directives), caps.directives_chars),
        ),
    ]
    return "\n\n".join([header, *sections]) + "\n"
