"""Envelope/excerpt caps, truncation sentinels, and the ``_truncate`` helper.

Shared module-level constants for the per-event prompt composer
(#3312 slice-6 decomposition of ``routes/event_prompt.py``). These were
module globals of the pre-split file; they live here so every renderer /
the composer / the CLI driver pull them from one home rather than each
holding its own copy. AST-identical to the pre-split definitions — pure
refactor, no behaviour change.
"""

from __future__ import annotations

import re

# Cap on the inline memory excerpt — keep the per-event prompt within
# the cacheable prefix budget. Matches the slice-1 writer's
# ``_CODEBASE_PROSE_MAX_CHARS`` so a well-distilled file passes through
# unchanged. The architect's plan acceptance: "composer correctly
# truncates memory excerpts that exceed 2 KB".
MEMORY_EXCERPT_MAX_CHARS: int = 2000

# Cap on the prompt envelope EXCLUDING the git-log delta. The 10 KB
# bound is the architect's plan acceptance: "per-event prompt envelope
# (excluding delta) ≤ 10 KB". The delta itself scales with the
# change and is not counted. The cap is enforced in
# ``compose_event_prompt`` by hard-truncating the NACKs section (the
# variable-size driver — event/contract are bounded and memory is
# already 2 KB capped) when the rendered envelope would otherwise
# overflow. A pathological NACK payload (e.g. 6 reviewers each with a
# multi-KB ``reason``) would otherwise silently bloat the cacheable
# prefix.
PROMPT_ENVELOPE_MAX_BYTES: int = 10240

# Sentinel appended when the NACKs section is byte-truncated to keep
# the envelope under ``PROMPT_ENVELOPE_MAX_BYTES``. Mirrors the shape
# of ``_GIT_LOG_DELTA_MAX_BYTES``'s truncation marker so the agent
# sees the cut explicitly rather than reviewing a silently-clipped
# blocker list.
_ENVELOPE_TRUNCATION_SENTINEL: str = (
    "\n…(NACK list truncated — surrounding envelope exceeded "
    f"{PROMPT_ENVELOPE_MAX_BYTES} bytes; pull the full open-NACK "
    "barrier with ``egg-orch brc get-state`` if you need every "
    "blocker before re-proposing)\n"
)

# Cap on the inline copy of the contract's ``task_description`` (#3123).
# The full text stays one tool call away (``mcp__sdlc__show_contract``);
# this inline excerpt exists so the operator's task framing — including
# binding directives like "adopt prior branch X, do not reimplement" —
# is PUSHED into every one-shot invocation instead of relying on the
# agent pulling it per the rules file. 4 KB inside the 10 KB envelope
# leaves room for the event payload and the (separately truncatable)
# NACKs section.
TASK_DESCRIPTION_MAX_CHARS: int = 4000

_TASK_TRUNCATION_SENTINEL: str = (
    "\n…(task description truncated — read the full text with "
    "``mcp__sdlc__show_contract`` before making scope or adopt-vs-"
    "reimplement decisions)\n"
)

# Cap on the inline copy of the per-iteration operator feedback (#3231).
# The orchestrator-owned event-loop respawn path threads the operator's
# ``request_changes`` / ``change_approach`` kickback — recorded on
# ``PhaseExecution.operator_directives`` (#2795) — into the re-spawned
# producer's prompt via the ``next-action`` event_payload so the
# producer addresses (or explicitly rebuts) it before re-proposing,
# rather than re-reading its own prior draft and re-proposing it
# unchanged (the #1283 / #1915 fake-cycle class). The full directive
# history stays in ``PhaseExecution.operator_directives``; this inline
# excerpt carries the most recent directive (the one the producer must
# answer THIS round) plus a frozen summary of the prior iteration's
# verdicts/NACKs. 4 KB inside the 10 KB envelope leaves room for the
# event payload, the task section, and the (separately truncatable)
# NACKs section.
ITERATION_FEEDBACK_MAX_CHARS: int = 4000

_ITERATION_FEEDBACK_TRUNCATION_SENTINEL: str = (
    "\n…(operator feedback truncated — pull the full directive history "
    "with ``egg-orch brc get-state`` if you need every prior round before "
    "re-proposing)\n"
)


def _truncate(text: str, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars`` characters with an ellipsis sentinel.

    The ellipsis character (``…``) is one Unicode code point but encodes
    to 3 bytes in UTF-8; we measure by ``len(str)`` (code points) here
    because the upstream cap is also expressed in code points. The 10 KB
    envelope assertion later uses bytes, so the truncation is
    conservative against the byte cap.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


# Keys carrying the variable-size NACK payload that ``_render_nacks_section``
# already renders. We strip these from the JSON copy in
# ``_render_event_section`` so the envelope-cap pass over ``nacks_section``
# is the single source of truth for the rendered NACK bytes.
_NACK_PAYLOAD_KEYS: tuple[str, ...] = ("nacks", "unresolved_nacks", "aggregated_nacks")


# Sentinel value for the memory excerpt when ``EGG_BRC_MEMORY!=full``.
# The wrapper still writes through ``write-only`` (slice-1 default in
# slice-2), so the file may exist on disk; the reader path is gated
# separately so slice-4's default-on flip flips reads in one step.
_MEMORY_MODE_FULL = "full"

# Cap on a single ``git log`` subprocess in seconds. Long enough that a
# multi-megabyte delta against a slow filesystem still completes, short
# enough that a hung gateway doesn't deadlock the wrapper loop.
_GIT_LOG_TIMEOUT_SECS = 60

# Cap on the rendered ``git log`` output per producer (bytes). A
# pathologically large refactor could push a single delta past
# Claude's context budget regardless of the cacheable-prefix bound;
# we truncate with an explicit sentinel so the agent can detect the
# truncation rather than silently reviewing half a diff.
_GIT_LOG_DELTA_MAX_BYTES = 256 * 1024  # 256 KiB per producer

# Regex matching a slice-1 ``last_reviewed_commit_sha`` bullet. The
# slice-1 writer renders the value as either a 7-40 char SHA or the
# ``-`` sentinel for "no prior review" (see
# ``sandbox/egg_agent_tools/handlers/brc_memory.py::_render_assessment``).
_LAST_REVIEWED_SHA_RE = re.compile(r"^\s*-\s*last_reviewed_commit_sha\s*:\s*(\S+)\s*$")
_PRODUCER_HEADING_RE = re.compile(r"^\s*###\s+(.+?)\s*$")
