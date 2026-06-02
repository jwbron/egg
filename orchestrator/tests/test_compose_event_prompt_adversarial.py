"""Adversarial hardening tests for ``compose_event_prompt`` (#2908 slice-3).

Authored by the slice-3 tester (per #2936 coder-owns-tests, dual-role
review-and-harden). Probes edge cases and adversarial inputs the coder
test suite (``test_compose_event_prompt.py``) does not yet cover:

* Memory-excerpt boundary conditions at exactly the 2 KB cap.
* Memory-excerpt with multi-byte UTF-8 characters (truncation by code
  points, not bytes — verifies the docstring claim that bytes ≤ chars).
* Whitespace-only memory excerpt — section must be omitted, not rendered
  as an empty fenced block (silent-fallback hunt: an "empty memory"
  rendering would confuse the agent into thinking memory is missing
  rather than just inert).
* NACK rendering with degenerate inputs: missing reason, missing
  artifact_refs, ``None`` artifact_refs, string artifact_refs (defensive
  coercion path), multi-line reasons.
* git-log delta rendering with empty SHA, empty delta string, multiple
  producers, alternative base branches with slashes, and the explicit
  guard that ``changed_artifacts`` in the event payload does NOT replace
  the full delta (the architect's hard rule per REVIEWER-SYNC.md +
  risk_analyst R6).
* Event payload fallbacks: ``type`` key vs ``action`` key, non-dict
  payloads, JSON-non-serializable payload values (verifies graceful
  failure mode is documented behaviour, not an accidental crash).
* Section ordering invariant: event → delta → NACKs → contract →
  memory (architect od-6 Option B — memory at tail).
* Role / base_branch whitespace normalisation.
"""

from __future__ import annotations

import json
import re

import pytest

from orchestrator.routes.event_prompt import (
    MEMORY_EXCERPT_MAX_CHARS,
    PROMPT_ENVELOPE_MAX_BYTES,
    _render_memory_section,
    _render_nacks_section,
    _render_producer_delta_section,
    _truncate,
    compose_event_prompt,
)

# ---------------------------------------------------------------------------
# Memory excerpt boundary conditions
# ---------------------------------------------------------------------------


def test_memory_excerpt_at_exactly_cap_is_not_truncated() -> None:
    """Memory excerpt of exactly ``MEMORY_EXCERPT_MAX_CHARS`` passes through.

    The composer uses ``len(text) <= max_chars`` as the trim condition;
    a payload of exactly the cap must NOT receive the ellipsis sentinel.
    Off-by-one in the boundary would either inject an ellipsis when none
    is warranted or fail to truncate one character past the cap.
    """
    payload = "C" * MEMORY_EXCERPT_MAX_CHARS
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        payload,
        [],
        [],
        "main",
    )
    # The full run must be present.
    assert payload in prompt
    # No truncation sentinel inside the memory section.
    section = prompt.split("## Durable BRC memory")[1].split("```markdown")[1].split("```")[0]
    assert "…" not in section


def test_memory_excerpt_one_char_over_cap_is_truncated() -> None:
    """Memory excerpt of cap+1 chars receives the ellipsis sentinel.

    The truncated body length is exactly ``MEMORY_EXCERPT_MAX_CHARS``
    code points: ``max_chars - 1`` characters of payload + the ``…``
    sentinel (one code point).
    """
    payload = "D" * (MEMORY_EXCERPT_MAX_CHARS + 1)
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        payload,
        [],
        [],
        "main",
    )
    section = prompt.split("## Durable BRC memory")[1].split("```markdown")[1].split("```")[0]
    # Body inside the fenced block strips its surrounding newlines.
    body = section.strip("\n")
    assert len(body) == MEMORY_EXCERPT_MAX_CHARS, (
        f"truncated body should be exactly {MEMORY_EXCERPT_MAX_CHARS} code points, got {len(body)}"
    )
    assert body.endswith("…")
    # The non-ellipsis prefix is ``max_chars - 1`` chars of "D".
    assert body[:-1] == "D" * (MEMORY_EXCERPT_MAX_CHARS - 1)


def test_memory_excerpt_multibyte_truncation_counts_codepoints() -> None:
    """Truncation is by Unicode code points, not by UTF-8 byte length.

    The composer caps memory at 2000 *characters*; the rendered byte
    size of a multi-byte character payload is therefore larger than
    2000 bytes. This test guards against a refactor that switches to
    byte-length truncation, which would either silently drop content
    on ASCII payloads or split a multi-byte sequence and crash the
    UTF-8 encoder downstream.
    """
    # Each "é" is 2 UTF-8 bytes; cap+200 chars → ~ (cap+200) * 2 bytes
    # encoded, well past the byte equivalent of the char cap.
    payload = "é" * (MEMORY_EXCERPT_MAX_CHARS + 200)
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        payload,
        [],
        [],
        "main",
    )
    section = prompt.split("## Durable BRC memory")[1].split("```markdown")[1].split("```")[0]
    body = section.strip("\n")
    # Body is exactly ``max_chars`` code points (max_chars-1 of "é" + "…").
    assert len(body) == MEMORY_EXCERPT_MAX_CHARS
    assert body.endswith("…")


def test_memory_excerpt_whitespace_only_omits_section() -> None:
    """A memory excerpt of only whitespace must NOT render a memory block.

    The composer's ``_render_memory_section`` strips the truncated body
    and returns an empty string when nothing remains. A regression to
    "always render the section header when memory was passed" would
    surface as an empty fenced block to the agent — silent-fallback
    hunt: the agent should see the section ONLY when there's content.
    """
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "   \n\t  \n  ",
        [],
        [],
        "main",
    )
    assert "## Durable BRC memory" not in prompt


def test_memory_excerpt_none_omits_section() -> None:
    """``None`` memory excerpt (vs empty string) must not crash and must omit."""
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        None,  # type: ignore[arg-type]
        [],
        [],
        "main",
    )
    assert "## Durable BRC memory" not in prompt


def test_truncate_unit_round_trip() -> None:
    """``_truncate`` helper round-trips its documented contract."""
    assert _truncate("", 100) == ""
    assert _truncate("abc", 100) == "abc"
    assert _truncate("abc", 3) == "abc"
    # Length cap-1 + ellipsis means the result has len == max_chars exactly.
    out = _truncate("a" * 10, 5)
    assert len(out) == 5
    assert out.endswith("…")
    assert out[:-1] == "aaaa"


# ---------------------------------------------------------------------------
# NACK rendering: degenerate / adversarial inputs
# ---------------------------------------------------------------------------


def test_nack_with_missing_reason_uses_none_recorded_sentinel() -> None:
    """A NACK without a reason renders the ``(none recorded)`` sentinel."""
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 2,
            "artifact_refs": ["src/foo.py"],
        }
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )
    assert "## Open NACKs" in prompt
    assert "(none recorded)" in prompt
    # The reviewer name is still rendered so the producer knows WHO NACKed.
    assert "reviewer_code" in prompt


def test_nack_with_empty_artifact_refs_renders_em_dash_placeholder() -> None:
    """An empty artifact_refs list renders ``—`` instead of an empty list."""
    nacks = [
        {
            "reviewer": "reviewer_security",
            "version": 1,
            "reason": "Audit logging missing.",
            "artifact_refs": [],
        }
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )
    # The placeholder is on the artifact_refs bullet line.
    assert re.search(r"artifact_refs:\s*—", prompt), "em-dash placeholder missing for empty refs"


def test_nack_with_string_artifact_refs_coerced_to_list() -> None:
    """A NACK with a string (not list) for artifact_refs renders the string."""
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 1,
            "reason": "Single file blocker.",
            "artifact_refs": "src/foo.py",
        }
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )
    # The lone artifact survives the defensive coercion.
    assert "src/foo.py" in prompt


def test_nack_with_multiline_reason_renders_each_line_indented() -> None:
    """A multi-line NACK reason renders each line indented inside the fence.

    The composer indents reason lines by two spaces inside a ```` ``` ````
    fenced block so the markdown renders the bullet as a code block and
    the agent reads the full text verbatim, including the second line.
    """
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 2,
            "reason": "Line one of the blocker.\nLine two of the blocker.\nLine three references file.py:42.",
            "artifact_refs": ["src/foo.py"],
        }
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )
    assert "Line one of the blocker." in prompt
    assert "Line two of the blocker." in prompt
    assert "Line three references file.py:42." in prompt


def test_nack_section_omits_when_nacks_argument_is_none() -> None:
    """``nacks=None`` (not just ``[]``) must omit the section."""
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        None,
        [],
        "main",
    )
    assert "## Open NACKs" not in prompt


# ---------------------------------------------------------------------------
# git-log delta rendering: edge cases + the no-shortcut guard
# ---------------------------------------------------------------------------


def test_delta_with_multiple_producers_renders_each_separately() -> None:
    """Multiple per-producer deltas each surface their own command + diff."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "1111111",
            "delta": "diff coder",
        },
        {
            "producer": "documenter",
            "last_reviewed_commit_sha": "2222222",
            "delta": "diff documenter",
        },
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "main",
    )
    # Both per-producer headings present.
    assert "### Producer: ``coder``" in prompt
    assert "### Producer: ``documenter``" in prompt
    # Both commands rendered.
    assert "git log 1111111..HEAD --not origin/main -p" in prompt
    assert "git log 2222222..HEAD --not origin/main -p" in prompt
    # Both bodies rendered.
    assert "diff coder" in prompt
    assert "diff documenter" in prompt


def test_delta_with_empty_sha_uses_no_prior_review_sentinel() -> None:
    """An empty ``last_reviewed_commit_sha`` surfaces an explicit sentinel.

    A re-reviewer needs to see that there was no prior anchor so they
    can scope the audit to the full branch history rather than silently
    reviewing a degenerate ``..HEAD`` range.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "",
            "delta": "(delta)",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "main",
    )
    assert "<no prior review — full branch history>" in prompt


def test_delta_with_empty_delta_body_uses_no_op_sentinel() -> None:
    """An empty rendered delta surfaces ``(no commits in range...)``."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "deadbee",
            "delta": "",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "main",
    )
    assert "(no commits in range — re-review is a no-op)" in prompt


def test_delta_command_uses_slash_in_base_branch() -> None:
    """Base branches like ``release/v2`` substitute literally into ``origin/<branch>``."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "fedcba9",
            "delta": "(delta)",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "release/v2",
    )
    assert "git log fedcba9..HEAD --not origin/release/v2 -p" in prompt


def test_delta_section_present_even_when_event_carries_changed_artifacts() -> None:
    """Regression guard: ``changed_artifacts`` in payload does NOT shortcut the delta.

    The architect plan explicitly forbids a ``changed_artifacts``-only
    shortcut — REVIEWER-SYNC.md + risk_analyst R6 require the full
    git-log diff for adversarial re-review. This test surfaces a
    payload that carries ``changed_artifacts`` AND a non-empty git-log
    delta and verifies the verbatim command is still emitted; a
    regression that surfaces only the artifact list would fail this
    assertion.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "delta": "+ new line",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {
            "action": "ack",
            "changed_artifacts": ["src/foo.py", "src/bar.py"],
            "version": 3,
        },
        "",
        [],
        git_log_delta,
        "main",
    )
    # The full delta block is present...
    assert "## Per-producer re-review delta" in prompt
    assert "git log abc1234..HEAD --not origin/main -p" in prompt
    assert "+ new line" in prompt
    # ...and changed_artifacts is NOT used as the substitute scope description.
    # The full-delta REVIEWER-SYNC framing is mentioned verbatim.
    assert "REVIEWER-SYNC.md" in prompt


# ---------------------------------------------------------------------------
# Event payload variants
# ---------------------------------------------------------------------------


def test_event_payload_type_key_falls_through_when_action_missing() -> None:
    """``event_payload["type"]`` is accepted when ``action`` is absent."""
    prompt = compose_event_prompt(
        "reviewer_code",
        {"type": "CONSENSUS_PROPOSE"},
        "",
        [],
        [],
        "main",
    )
    assert "Action: **CONSENSUS_PROPOSE**" in prompt


def test_event_payload_action_takes_precedence_over_type() -> None:
    """When both ``action`` and ``type`` are present, ``action`` wins."""
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack", "type": "CONSENSUS_PROPOSE"},
        "",
        [],
        [],
        "main",
    )
    assert "Action: **ack**" in prompt


def test_event_payload_non_dict_does_not_crash() -> None:
    """A non-dict payload (e.g. list) renders ``(unspecified)`` without crashing."""
    # Passing a list — the composer's ``isinstance(event_payload, dict)``
    # check should keep ``action`` empty and surface the fallback banner.
    prompt = compose_event_prompt(
        "reviewer_code",
        [1, 2, 3],  # type: ignore[arg-type]
        "",
        [],
        [],
        "main",
    )
    assert "Action: **(unspecified)**" in prompt


def test_event_payload_renders_with_sorted_json_keys() -> None:
    """JSON payload serialisation uses ``sort_keys=True`` for determinism.

    Two payloads with the same keys in different insertion order must
    serialise to the same string so snapshot tests aren't flaky on
    dict-iteration order changes.
    """
    p1 = compose_event_prompt(
        "coder",
        {"action": "propose", "b": 2, "a": 1},
        "",
        [],
        [],
        "main",
    )
    p2 = compose_event_prompt(
        "coder",
        {"a": 1, "action": "propose", "b": 2},
        "",
        [],
        [],
        "main",
    )
    assert p1 == p2


# ---------------------------------------------------------------------------
# Section ordering invariant
# ---------------------------------------------------------------------------


def test_section_ordering_event_delta_nacks_contract_memory() -> None:
    """All sections appear in canonical order (event → delta → NACKs → contract → memory).

    The architect's od-6 Option B fixes memory at the tail. The remaining
    section order is implied by ``compose_event_prompt``'s ``parts`` list
    assembly; this test pins it so a refactor that reshuffles the order
    fails loudly.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "delta": "(delta)",
        }
    ]
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 2,
            "reason": "Blocker.",
            "artifact_refs": ["src/foo.py"],
        }
    ]
    memory = "## Codebase / change model\n\nReuse this."
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        memory,
        nacks,
        git_log_delta,
        "main",
    )

    event_idx = prompt.index("# BRC Event-Pump Handler")
    delta_idx = prompt.index("## Per-producer re-review delta")
    nacks_idx = prompt.index("## Open NACKs")
    contract_idx = prompt.index("## What to do")
    memory_idx = prompt.index("## Durable BRC memory")

    assert event_idx < delta_idx < nacks_idx < contract_idx < memory_idx, (
        "section ordering invariant broken: "
        f"event={event_idx} delta={delta_idx} nacks={nacks_idx} "
        f"contract={contract_idx} memory={memory_idx}"
    )


# ---------------------------------------------------------------------------
# Whitespace / strip normalisation
# ---------------------------------------------------------------------------


def test_role_whitespace_stripped() -> None:
    """``role="  coder  "`` is stripped to ``coder`` in the role banner."""
    prompt = compose_event_prompt(
        "  coder  ",
        {"action": "propose"},
        "",
        [],
        [],
        "main",
    )
    assert "Role: coder" in prompt
    # No literal whitespace surrounding the role token in the banner.
    assert "Role:   coder" not in prompt


def test_base_branch_whitespace_stripped() -> None:
    """``base_branch="  main  "`` is stripped before substitution."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "delta": "(delta)",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "  main  ",
    )
    assert "git log abc1234..HEAD --not origin/main -p" in prompt
    assert "origin/  main" not in prompt


# ---------------------------------------------------------------------------
# Helper-level invariants (whitebox)
# ---------------------------------------------------------------------------


def test_render_memory_section_returns_empty_for_empty_input() -> None:
    """``_render_memory_section("")`` returns the empty string verbatim."""
    assert _render_memory_section("") == ""


def test_render_nacks_section_returns_empty_for_empty_list() -> None:
    """``_render_nacks_section([])`` returns the empty string verbatim."""
    assert _render_nacks_section([]) == ""
    assert _render_nacks_section(None) == ""


def test_render_producer_delta_section_returns_empty_for_empty_list() -> None:
    """``_render_producer_delta_section([], "main")`` returns ``("", 0)``."""
    section, total_bytes = _render_producer_delta_section([], "main")
    assert section == ""
    assert total_bytes == 0


def test_render_producer_delta_section_reports_byte_totals() -> None:
    """The returned ``total_delta_bytes`` reflects encoded UTF-8 length."""
    delta_body = "diff body é"  # 12 chars; "é" is 2 bytes; total = 13 bytes
    section, total_bytes = _render_producer_delta_section(
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "abc1234",
                "delta": delta_body,
            }
        ],
        "main",
    )
    assert section
    assert total_bytes == len(delta_body.encode("utf-8"))


# ---------------------------------------------------------------------------
# Envelope budget — pathological NACK list
# ---------------------------------------------------------------------------


def test_envelope_holds_under_many_nacks_at_realistic_size() -> None:
    """Eight NACKs with realistic reason size stay under the 10 KB envelope.

    Eight reviewers is the upper-bound BRC ever exercises today; the
    architect plan acceptance is "envelope (excluding delta) ≤ 10 KB
    for representative event payloads". Probes the upper end of the
    "representative" range.
    """
    nacks = [
        {
            "reviewer": f"reviewer_{i}",
            "version": 2,
            "reason": "Adversarial blocker " + ("x" * 80),
            "artifact_refs": [f"src/file_{i}.py", f"tests/test_{i}.py"],
        }
        for i in range(8)
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose", "nacks": nacks},
        "",  # no memory so we measure the NACK contribution
        nacks,
        [],
        "main",
    )
    envelope_bytes = len(prompt.encode("utf-8"))
    assert envelope_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {envelope_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes"
    )


# ---------------------------------------------------------------------------
# JSON payload non-serialisable values — documented graceful failure
# ---------------------------------------------------------------------------


def test_json_non_serialisable_payload_raises_predictable_error() -> None:
    """A payload with a non-JSON-serializable value raises ``TypeError``.

    The composer does not currently sanitise payload values; an object
    such as a datetime or a set will crash with ``TypeError`` at the
    ``json.dumps`` boundary. This test pins that behaviour so a future
    refactor either preserves it (callers know to coerce) or replaces
    it with a structured fallback (and updates the contract). Either
    way, a silent skip is the wrong behaviour and this assertion will
    flag it.
    """
    with pytest.raises(TypeError):
        compose_event_prompt(
            "coder",
            {"action": "propose", "set_field": {1, 2, 3}},  # type: ignore[dict-item]
            "",
            [],
            [],
            "main",
        )


# ---------------------------------------------------------------------------
# Negative: verify the verbatim command IS the only delta-scope description
# ---------------------------------------------------------------------------


def test_no_alternative_delta_scope_descriptions_present() -> None:
    """The delta section uses ``git log {sha}..HEAD --not origin/{base}``.

    Regression guard against future refactors that might surface a
    natural-language description of the scope ("commits since ...") in
    place of the verbatim command. The architect plan: "git-log delta
    command is emitted verbatim with the per-producer
    ``last_reviewed_commit_sha`` substituted in" — surfacing a prose
    summary instead would systematically weaken adversarial re-review
    by making the scope unverifiable.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "delta": "(delta)",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "main",
    )
    # Pin the literal command form.
    assert re.search(
        r"git log abc1234\.\.HEAD --not origin/main -p",
        prompt,
    ), "verbatim git-log command must be present"
    # The scope-description bullet uses the literal command in backticks
    # — not a prose paraphrase. Pin the bullet so a prose-only refactor
    # fails this assertion.
    assert "Re-review scope (executed by the wrapper):" in prompt
    assert "`git log abc1234..HEAD --not origin/main -p`" in prompt


def test_json_payload_block_uses_sorted_keys() -> None:
    """The ``Payload (JSON):`` fenced block is sorted-keys serialisable.

    Pins the serialisation invariant the snapshot tests depend on. If a
    future refactor switches to ``sort_keys=False``, the assertion below
    will fail because the rendered block won't match a sorted-keys
    serialisation of the same dict.
    """
    payload = {"z_last": 1, "a_first": 2, "m_mid": 3, "action": "ack"}
    prompt = compose_event_prompt(
        "reviewer_code",
        payload,
        "",
        [],
        [],
        "main",
    )
    expected = json.dumps(payload, indent=2, sort_keys=True)
    assert expected in prompt
