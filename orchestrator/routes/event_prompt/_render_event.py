"""Event-banner section: role banner + the JSON event description.

Renderers for the leading ``# BRC Event-Pump Handler`` banner and the
``## Event`` JSON block (#3312 slice-6 decomposition). The variable-size
NACK lists are stripped from the JSON copy here so
``_render_nacks_section`` stays the single source of truth for the
rendered NACK bytes under the envelope cap. AST-identical to the
pre-split definitions — pure refactor.
"""

from __future__ import annotations

import json
from typing import Any

from ._caps import _NACK_PAYLOAD_KEYS


def _strip_nacks_for_json(event_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``event_payload`` with NACK lists replaced.

    Each NACK key present in the original is replaced with a small
    cross-reference marker (``"<rendered in '## Open NACKs ...' section>"``
    plus the entry count) so the agent still sees that NACKs are
    attached and that the full payload lives in the dedicated section
    that the envelope-cap pass governs. The structural shape of the
    payload (keys, ordering, non-NACK values) is preserved so the agent
    can still inspect the rest of the JSON for context.
    """
    out: dict[str, Any] = {}
    for key, value in event_payload.items():
        if key in _NACK_PAYLOAD_KEYS and isinstance(value, list):
            out[key] = (
                f"<{len(value)} entr{'y' if len(value) == 1 else 'ies'} "
                "rendered in the '## Open NACKs against the current proposal "
                "version' section below; truncated under the envelope budget "
                "when oversized>"
            )
        else:
            out[key] = value
    return out


def _render_event_section(role: str, event_payload: dict[str, Any] | None) -> str:
    """Render the role banner + event description.

    The event payload is serialised as JSON (sorted keys, 2-space
    indent) so the rendering is deterministic — two callers with the
    same payload produce byte-identical output, which lets snapshot
    tests pin the shape without sensitivity to dict-iteration order.

    Variable-size NACK lists (``nacks`` / ``unresolved_nacks`` /
    ``aggregated_nacks``) are stripped from the JSON before rendering
    so the same data is not also embedded here — ``_render_nacks_section``
    is the single source of truth for the rendered NACK list, and it
    honours the ``PROMPT_ENVELOPE_MAX_BYTES`` truncation budget.
    Without this strip the NACK payload appears twice in the envelope
    (once as JSON here, once as markdown in nacks_section), defeating
    the envelope cap because the truncation pass only touches the
    nacks_section copy. The stripped keys are replaced with a
    cross-reference marker so the agent still sees that NACKs are
    attached and where to find them.
    """
    if event_payload is None:
        event_payload = {}
    action = ""
    if isinstance(event_payload, dict):
        # ``next-action`` puts the chosen verb under ``action`` (see
        # ``orchestrator/routes/consensus.py``'s ``_VALID_ACTIONS``).
        action = str(event_payload.get("action") or "")
        payload_for_json = _strip_nacks_for_json(event_payload)
    else:
        payload_for_json = event_payload
    payload_json = json.dumps(payload_for_json, indent=2, sort_keys=True)

    lines = [
        f"# BRC Event-Pump Handler — Role: {role}",
        "",
        f"You are the **{role}** agent. The wrapper has invoked you to "
        "handle ONE BRC event. Act on it according to your role contract, "
        "update durable BRC memory if you reach a verdict, then exit "
        "naturally. The wrapper will invoke you again with the next event.",
        "",
        "## Event",
        "",
        f"Action: **{action or '(unspecified)'}**",
        "",
        "Payload (JSON):",
        "```json",
        payload_json,
        "```",
        "",
    ]
    return "\n".join(lines)
