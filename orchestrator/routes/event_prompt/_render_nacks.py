"""Open-NACK barrier section (#2142).

Renders the per-reviewer open-NACK list so a producer re-propose
addresses every blocker in one round-trip. AST-identical to the
pre-split definition — pure refactor (#3312 slice-6).
"""

from __future__ import annotations

from typing import Any


def _render_nacks_section(nacks: list[dict[str, Any]] | None) -> str:
    """Render the open-NACK barrier payload (#2142).

    Mirrors the shape of
    ``orchestrator/peer_consensus.py:_open_nacks_barrier_response``:
    one dict per reviewer with ``reviewer``, ``version``, ``reason``,
    ``artifact_refs``. Each NACK's full ``reason`` is rendered verbatim
    so the producer's re-propose addresses every blocker, not just the
    most recent one.
    """
    if not nacks:
        return ""

    lines: list[str] = [
        "## Open NACKs against the current proposal version",
        "",
        "Two or more reviewers have NACKed the current proposal version; "
        "the orchestrator has surfaced them all here so the re-propose "
        "addresses every blocker in a single round-trip (#2142). A "
        "re-propose that resolves only one NACK is rejected with HTTP "
        "409 until all are addressed.",
        "",
    ]
    for nack in nacks:
        reviewer = str(nack.get("reviewer") or "?")
        version = nack.get("version", "?")
        reason = str(nack.get("reason") or "").rstrip()
        artifact_refs = nack.get("artifact_refs") or []
        if not isinstance(artifact_refs, list):
            artifact_refs = [artifact_refs]
        refs_rendered = ", ".join(str(r) for r in artifact_refs) if artifact_refs else "—"
        lines.append(f"### Reviewer: ``{reviewer}`` (v{version})")
        lines.append("")
        lines.append(f"- artifact_refs: {refs_rendered}")
        if reason:
            lines.append("- reason:")
            lines.append("")
            lines.append("  ```")
            for raw_line in reason.splitlines():
                lines.append(f"  {raw_line}")
            lines.append("  ```")
        else:
            lines.append("- reason: (none recorded)")
        lines.append("")
    return "\n".join(lines)
