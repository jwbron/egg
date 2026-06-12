"""Unit tests for ``compose_event_prompt`` (#2908 slice-3 TASK-3-1).

Authored by the slice-3 coder (per #2936 coder-owns-tests). The
tester reviews-and-hardens these in their own pass.

Coverage map (matches the plan's TASK-3-6 acceptance):

* One prompt-shape test per role variant (producer / reviewer /
  dual-role) so the surface stays stable across both the per-producer
  ``ack/nack`` path and the standalone ``propose`` path.
* Memory-excerpt truncation at the 2 KB cap.
* Open-NACK payload rendering across 0 / 1 / 2+ reviewers (the #2142
  aggregated-NACK barrier is the multi-reviewer case the composer
  must surface verbatim, not summarise).
* git-log delta command emitted verbatim with the per-producer
  ``last_reviewed_commit_sha`` substituted (NOT a
  ``changed_artifacts``-only shortcut — REVIEWER-SYNC.md +
  risk_analyst R6).
* Total prompt envelope (excluding git-log delta) ≤ 10 KB per case.
"""

from __future__ import annotations

import re

from orchestrator.routes.event_prompt import (
    MEMORY_EXCERPT_MAX_CHARS,
    PROMPT_ENVELOPE_MAX_BYTES,
    compose_event_prompt,
)


def _strip_git_log_blocks(prompt: str) -> str:
    """Return the prompt envelope with the rendered diff blocks elided.

    Tests of the envelope-size bound exclude the rendered delta because
    the delta scales with the actual change — capping it would defeat
    the full-delta re-review (REVIEWER-SYNC.md). We strip everything
    between ``Delta:`` (the composer's marker) and the closing fence
    so the remaining bytes are the surrounding prose.
    """
    return re.sub(
        r"Delta:\n```diff\n.*?\n```",
        "Delta:\n```diff\n(stripped)\n```",
        prompt,
        flags=re.DOTALL,
    )


# ---------------------------------------------------------------------------
# Prompt shape per role
# ---------------------------------------------------------------------------


def test_producer_propose_prompt_shape() -> None:
    """Producer ``propose`` events have no per-producer delta."""
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose", "tasks": ["task-3-1"]},
        "",  # no memory yet
        [],  # no NACKs
        [],  # no per-producer deltas on producer side
        "main",
    )

    assert "# BRC Event-Pump Handler — Role: coder" in prompt
    assert "Action: **propose**" in prompt
    # No per-producer delta block on producer events — the producer
    # looks at HEAD, there is no "review scope" to surface.
    assert "## Per-producer re-review delta" not in prompt
    # No NACK section when none are open.
    assert "## Open NACKs" not in prompt
    # Memory section omitted when excerpt is empty.
    assert "## Durable BRC memory" not in prompt


def test_reviewer_ack_prompt_includes_delta_and_memory() -> None:
    """Reviewer ACK events include per-producer delta + memory tail."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "delta": "diff --git a/x.py b/x.py\n+ pass\n",
        }
    ]
    memory_excerpt = "## Codebase / change model\n\nSlice-3 wiring.\n"

    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack", "producer": "coder", "version": 2},
        memory_excerpt,
        [],
        git_log_delta,
        "main",
    )

    assert "# BRC Event-Pump Handler — Role: reviewer_code" in prompt
    assert "## Per-producer re-review delta" in prompt
    # Verbatim git-log command with substituted SHA.
    assert "git log abc1234..HEAD --not origin/main -p" in prompt
    # The rendered delta itself is in the prompt.
    assert "diff --git a/x.py b/x.py" in prompt
    # Memory excerpt at tail position (after the "What to do" contract).
    assert "## Durable BRC memory (tail-position context)" in prompt
    assert "Slice-3 wiring." in prompt

    contract_idx = prompt.index("## What to do")
    memory_idx = prompt.index("## Durable BRC memory")
    assert memory_idx > contract_idx, "Memory must sit at the prompt tail (architect od-6 Option B)"


def test_dual_role_tester_prompt_handles_dual_perspective() -> None:
    """Dual-role agents see both a delta block and a memory tail.

    Tester is the canonical dual-role agent in the implement graph;
    when the wrapper invokes it post-coder-propose it acts both as a
    reviewer (ACK/NACK the coder) and as a producer (propose the
    hardening). The composer does not branch on role type — the
    same shape covers both readings.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "feedbac",
            "delta": "diff --git a/y.py b/y.py\n",
        }
    ]
    memory_excerpt = "## Codebase / change model\n\nTester reviews coder's tests.\n"

    prompt = compose_event_prompt(
        "tester",
        {"action": "ack", "producer": "coder", "version": 1},
        memory_excerpt,
        [],
        git_log_delta,
        "main",
    )

    assert "Role: tester" in prompt
    assert "git log feedbac..HEAD --not origin/main -p" in prompt
    assert "## Durable BRC memory" in prompt


# ---------------------------------------------------------------------------
# Memory excerpt truncation (2 KB cap)
# ---------------------------------------------------------------------------


def test_memory_excerpt_truncates_at_two_kb() -> None:
    """Memory excerpts past 2 KB are truncated with an ellipsis sentinel."""
    oversized = "A" * (MEMORY_EXCERPT_MAX_CHARS + 500)
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        oversized,
        [],
        [],
        "main",
    )

    # Find the memory block.
    section = prompt.split("## Durable BRC memory")[1]
    # The "A" run inside the markdown block is bounded at the cap.
    a_runs = re.findall(r"A+", section)
    # The longest run of A's should be at most MEMORY_EXCERPT_MAX_CHARS.
    assert a_runs, "memory excerpt block missing the truncated payload"
    longest = max(len(r) for r in a_runs)
    assert longest <= MEMORY_EXCERPT_MAX_CHARS, (
        f"truncation cap breached: longest A-run is {longest} > {MEMORY_EXCERPT_MAX_CHARS}"
    )
    # Truncation sentinel present.
    assert "…" in section


def test_memory_excerpt_under_cap_is_passed_through_verbatim() -> None:
    """Sub-2 KB excerpts are not truncated."""
    excerpt = "## Codebase / change model\n\n" + ("B" * 500)
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        excerpt,
        [],
        [],
        "main",
    )
    # The full 500-B run should be present.
    assert "B" * 500 in prompt
    # No truncation sentinel injected when under the cap.
    section = prompt.split("## Durable BRC memory")[1]
    section_payload = section.split("```markdown")[1].split("```")[0]
    assert "…" not in section_payload


# ---------------------------------------------------------------------------
# Open-NACK payload rendering (0 / 1 / 2+ reviewers)
# ---------------------------------------------------------------------------


def test_no_open_nacks_section_when_empty_list() -> None:
    """An empty NACK list omits the section entirely."""
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        [],
        [],
        "main",
    )
    assert "## Open NACKs" not in prompt


def test_open_nacks_section_renders_single_reviewer() -> None:
    """One NACK still renders the section so the agent sees the verdict."""
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 1,
            "reason": "Missing test for edge case X.",
            "artifact_refs": ["src/foo.py"],
        }
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose", "nacks": nacks},
        "",
        nacks,
        [],
        "main",
    )
    assert "## Open NACKs against the current proposal version" in prompt
    assert "reviewer_code" in prompt
    assert "Missing test for edge case X." in prompt
    assert "src/foo.py" in prompt


def test_open_nacks_section_renders_multi_reviewer_aggregation() -> None:
    """The #2142 aggregated barrier surface — multiple reviewers verbatim.

    The wrapper must hand the producer every NACK in a single prompt
    so the re-propose addresses them all at once; the orchestrator
    barrier rejects a re-propose that only addresses a subset.
    """
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 2,
            "reason": "Variable name shadowing.",
            "artifact_refs": ["src/foo.py"],
        },
        {
            "reviewer": "reviewer_security",
            "version": 2,
            "reason": "Missing input sanitisation.",
            "artifact_refs": ["src/bar.py"],
        },
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose", "nacks": nacks},
        "",
        nacks,
        [],
        "main",
    )
    # Both NACKs surfaced verbatim — the architect's plan acceptance
    # is that the barrier renders per-reviewer with reason +
    # artifact_refs, NOT a single aggregated summary.
    assert "reviewer_code" in prompt
    assert "reviewer_security" in prompt
    assert "Variable name shadowing." in prompt
    assert "Missing input sanitisation." in prompt
    assert "src/foo.py" in prompt
    assert "src/bar.py" in prompt


# ---------------------------------------------------------------------------
# git-log delta command rendering
# ---------------------------------------------------------------------------


def test_git_log_command_emitted_verbatim_with_sha_substituted() -> None:
    """The verbatim command appears next to the rendered delta.

    The architect plan: "git-log delta command is emitted verbatim
    with the per-producer ``last_reviewed_commit_sha`` substituted in
    (NO ``changed_artifacts``-only shortcut)". The assertion fails if
    a future refactor replaces the command with a summary.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "0123abc",
            "delta": "[delta body here]",
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
    # The exact command appears in the prompt.
    assert "git log 0123abc..HEAD --not origin/main -p" in prompt
    # The delta body appears too.
    assert "[delta body here]" in prompt


def test_git_log_command_handles_alternative_base_branch() -> None:
    """``base_branch`` substitutes into ``--not origin/<branch>``."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "f00bar",
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
    assert "git log f00bar..HEAD --not origin/release/v2 -p" in prompt


def test_git_log_renders_no_changed_artifacts_shortcut() -> None:
    """A regression guard against a future ``changed_artifacts`` summary.

    The architect plan explicitly rejects a ``changed_artifacts``-only
    shortcut (REVIEWER-SYNC.md + risk_analyst R6). This test fails if
    a future refactor surfaces only the orchestrator's signal-level
    artifact list instead of the full diff command.
    """
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack", "changed_artifacts": ["src/foo.py"]},
        "",
        [],
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "deadbeef",
                "delta": "(delta)",
            }
        ],
        "main",
    )
    # The verbatim git-log command must be present.
    assert re.search(r"git log deadbeef\.\.HEAD --not origin/main -p", prompt)
    # The delta block marker must be present (`Delta:` followed by
    # the diff fence).
    assert "Delta:\n```diff" in prompt


# ---------------------------------------------------------------------------
# Envelope budget (≤ 10 KB excluding the delta)
# ---------------------------------------------------------------------------


def test_prompt_envelope_bounded_at_ten_kb_producer() -> None:
    """Producer prompt envelope stays under the 10 KB cap."""
    # Realistic-shape inputs near the upper end of normal use.
    nacks = [
        {
            "reviewer": f"reviewer_{i}",
            "version": 2,
            "reason": "Reason " + ("x" * 200),
            "artifact_refs": [f"src/file_{i}.py"],
        }
        for i in range(4)
    ]
    memory_excerpt = "## Codebase / change model\n\n" + ("y" * 1500)

    prompt = compose_event_prompt(
        "coder",
        {"action": "propose", "nacks": nacks},
        memory_excerpt,
        nacks,
        [],
        "main",
    )

    # Strip git-log blocks (none here, but symmetric with the reviewer
    # case so the assertion is consistent across tests).
    envelope = _strip_git_log_blocks(prompt)
    envelope_bytes = len(envelope.encode("utf-8"))
    assert envelope_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {envelope_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes"
    )


def test_prompt_envelope_bounded_at_ten_kb_reviewer_with_large_delta() -> None:
    """Even with a huge rendered delta, the envelope stays under the cap.

    The delta scales freely (REVIEWER-SYNC.md needs the full diff for
    adversarial re-review); the cap applies to the *surrounding* prose.
    """
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            # 100 KB rendered diff to make sure the envelope check
            # really does exclude it.
            "delta": "+" * (100 * 1024),
        }
    ]
    memory_excerpt = "## Codebase / change model\n\n" + ("z" * 1500)
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 2,
            "reason": "Substantial blocker.",
            "artifact_refs": ["src/foo.py", "src/bar.py"],
        }
    ]

    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        memory_excerpt,
        nacks,
        git_log_delta,
        "main",
    )
    envelope = _strip_git_log_blocks(prompt)
    envelope_bytes = len(envelope.encode("utf-8"))
    assert envelope_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {envelope_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes"
    )
    # The rendered delta itself is still in the full prompt.
    assert "+" * 1024 in prompt


def test_pathological_nacks_payload_is_hard_truncated_under_envelope_cap() -> None:
    """A multi-KB-per-reviewer NACKs payload must not silently push the
    envelope past ``PROMPT_ENVELOPE_MAX_BYTES``.

    The reviewer_holistic v2 finding: ``PROMPT_ENVELOPE_MAX_BYTES`` was
    documented and tested under representative load but not enforced —
    a pathological NACK payload (6 reviewers × ~2 KB reason each) would
    sail past 10 KB silently. ``compose_event_prompt`` now truncates
    the NACKs section byte-exactly with an explicit sentinel when the
    envelope would overflow; the rest of the envelope (event banner,
    contract, memory tail) remains intact so the agent still has its
    role framing.
    """
    nacks = [
        {
            "reviewer": f"reviewer_{i}",
            "version": 2,
            "reason": ("Pathologically long blocker. " * 100),  # ~2.8 KB each
            "artifact_refs": [f"src/file_{i}.py"],
        }
        for i in range(6)
    ]

    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )

    # Whole prompt = envelope here (no delta), so the envelope-cap
    # check is the same as the full-prompt size check.
    prompt_bytes = len(prompt.encode("utf-8"))
    assert prompt_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {prompt_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes — "
        "the NACKs section must be hard-truncated when the surrounding "
        "envelope would otherwise overflow."
    )
    # Truncation sentinel must be present so the agent sees the cut
    # explicitly rather than silently reviewing half a barrier.
    assert "NACK list truncated" in prompt, (
        "envelope-truncation sentinel missing — the agent must see the cut"
    )
    # Role banner + contract survive — only the NACKs section is cut.
    assert "Role: coder" in prompt
    assert "## What to do" in prompt


def test_production_shaped_open_barrier_payload_honours_envelope_cap() -> None:
    """The production NACK payload is rendered ONCE, not twice.

    Reviewer re-review finding (commit ``aaaa17f1``): the prior
    truncation pass only touched ``nacks_section``, but the same NACK
    payload was also embedded verbatim inside ``event_section`` via
    ``json.dumps(event_payload, ...)`` because the production payload
    from ``_producer_has_open_barrier`` /
    ``_producer_has_unresolved_nacks_on_current_version`` puts the
    full ``nacks`` / ``unresolved_nacks`` list directly inside the
    event payload (see ``orchestrator/routes/consensus.py:233-249``
    and ``:340-358``). That meant the worked example — 6 reviewers ×
    multi-KB reasons — sailed past the 10 KiB cap because the JSON
    copy was untouched. The fix strips the NACK keys from the JSON
    block and routes them through the single ``nacks_section`` path
    that the envelope cap actually governs.

    This test pins the production-shaped payload (NACKs in both
    ``event_payload`` AND in the ``nacks`` argument, matching what
    ``_cli`` does at runtime via ``_extract_nacks``) and asserts the
    envelope stays under the cap.
    """
    nacks = [
        {
            "reviewer": f"reviewer_{i}",
            "version": 2,
            "reason": ("Pathologically long blocker. " * 100),  # ~2.8 KB each
            "artifact_refs": [f"src/file_{i}.py"],
            "timestamp": "2026-06-02T12:00:00Z",
        }
        for i in range(6)
    ]
    # Production shape from _producer_has_open_barrier — NACKs are
    # embedded directly in the event payload that flows through to the
    # composer. ``_cli`` then calls ``_extract_nacks(event_payload)``
    # and passes the result as the ``nacks`` argument, so in
    # production the same data is in both places.
    event_payload = {
        "status": "open_nacks_blocked",
        "producer": "coder",
        "current_version": 2,
        "nacking_reviewers": [n["reviewer"] for n in nacks],
        "nacks": nacks,
        "action": "propose",
    }

    prompt = compose_event_prompt(
        "coder",
        event_payload,
        "",
        nacks,
        [],
        "main",
    )

    prompt_bytes = len(prompt.encode("utf-8"))
    assert prompt_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {prompt_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes — "
        "production-shaped NACK payload (nacks in event_payload AND in the "
        "nacks argument) must not silently push the envelope past the cap."
    )
    # The JSON event block must NOT contain the full NACK list — only
    # the cross-reference marker. The reviewer's worked-example
    # reproduction relied on the second copy of the NACK list being
    # baked into the JSON; that has to be gone now.
    json_block_match = re.search(r"```json\n(.*?)\n```", prompt, flags=re.DOTALL)
    assert json_block_match, "event JSON block missing"
    json_block = json_block_match.group(1)
    assert "Pathologically long blocker" not in json_block, (
        "NACK ``reason`` text leaked into the event_section JSON — strip pass "
        "in _render_event_section is not stripping the nacks key."
    )
    assert "rendered in the" in json_block, (
        "cross-reference marker missing — the agent should still see that "
        "NACKs are attached and where to find them."
    )
    # The full NACK list (markdown rendering, or truncated form) lives
    # in ``nacks_section`` — the single source of truth.
    assert "## Open NACKs against the current proposal version" in prompt
    # Role banner + contract survive.
    assert "Role: coder" in prompt
    assert "## What to do" in prompt


def test_production_shaped_unresolved_nacks_payload_honours_envelope_cap() -> None:
    """Single-reviewer ``unresolved_nacks`` shape also goes through the strip pass.

    ``_producer_has_unresolved_nacks_on_current_version`` is the
    single-reviewer NACK case; ``_derive_next_action`` puts the result
    under the ``unresolved_nacks`` key (consensus.py:340-358). Same
    bug class as the multi-reviewer barrier: if the JSON block embeds
    the full list, the envelope can balloon under a pathologically
    chatty single reviewer.
    """
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 3,
            "reason": ("Pathologically long single-reviewer blocker. " * 200),  # ~9 KB
            "artifact_refs": ["src/file.py"],
        }
    ]
    event_payload = {
        "unresolved_nacks": nacks,
        "producer": "coder",
        "action": "propose",
    }

    prompt = compose_event_prompt(
        "coder",
        event_payload,
        "",
        nacks,
        [],
        "main",
    )

    prompt_bytes = len(prompt.encode("utf-8"))
    assert prompt_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {prompt_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes — "
        "unresolved_nacks shape must be stripped from event JSON same as nacks."
    )
    json_block_match = re.search(r"```json\n(.*?)\n```", prompt, flags=re.DOTALL)
    assert json_block_match, "event JSON block missing"
    json_block = json_block_match.group(1)
    assert "Pathologically long single-reviewer" not in json_block, (
        "unresolved_nacks reason leaked into event_section JSON"
    )


# ---------------------------------------------------------------------------
# Defensive shape — empty / None inputs
# ---------------------------------------------------------------------------


def test_compose_handles_none_inputs_gracefully() -> None:
    """``None`` payload / NACKs / deltas should not crash the composer."""
    prompt = compose_event_prompt(
        "coder",
        None,
        "",
        None,
        None,
        "main",
    )
    assert "Role: coder" in prompt
    assert "Action: **(unspecified)**" in prompt


def test_compose_handles_empty_role() -> None:
    """An empty role token falls back to ``unknown`` rather than crashing."""
    prompt = compose_event_prompt(
        "",
        {"action": "propose"},
        "",
        [],
        [],
        "main",
    )
    assert "Role: unknown" in prompt


def test_compose_handles_empty_base_branch() -> None:
    """An empty base branch falls back to ``main``."""
    git_log_delta = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "deadbeef",
            "delta": "(delta)",
        }
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        git_log_delta,
        "",
    )
    assert "git log deadbeef..HEAD --not origin/main -p" in prompt


# ---------------------------------------------------------------------------
# _build_delta_entries: changed_artifacts fallback (NACK #3 from
# reviewer_contract — plan TASK-3-2 acceptance "with EGG_BRC_MEMORY=
# write-only (slice-1 default), the prompt omits memory but still
# emits the git-log delta against the orchestrator's signal-level
# changed_artifacts as a fallback baseline").
# ---------------------------------------------------------------------------


def test_build_delta_entries_falls_back_to_changed_artifacts_when_no_sha() -> None:
    """No per-producer SHA → render the orchestrator's artifact list as a
    degraded baseline (not a verbatim diff).
    """
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    # No memory text → no SHAs parsed → fallback path.
    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "action": "ack",
            "producer": "coder",
            "changed_artifacts": ["src/foo.py", "src/bar.py"],
        },
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["producer"] == "coder"
    assert entry["last_reviewed_commit_sha"] == ""
    # The fallback delta names every artifact verbatim.
    assert "src/foo.py" in entry["delta"]
    assert "src/bar.py" in entry["delta"]
    # And labels itself as a degraded baseline so the agent does NOT
    # mistake it for adversarial-re-review-grade context.
    assert "degraded baseline" in entry["delta"]
    assert "adversarial-re-review" in entry["delta"]


def test_build_delta_entries_changed_artifacts_extracts_producer_role() -> None:
    """Fallback entry's ``producer`` field comes from the event payload."""
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "action": "ack",
            "producer_role": "documenter",
            "changed_artifacts": ["README.md"],
        },
    )
    assert entries[0]["producer"] == "documenter"


def test_build_delta_entries_no_fallback_when_no_changed_artifacts() -> None:
    """If neither SHAs nor changed_artifacts are available, return ``[]``.

    The agent then sees the prompt's "no rendered delta" branch, which
    is the legitimate first-ack-on-empty-state case rather than a
    silent failure.
    """
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={"action": "ack", "producer": "coder"},
    )
    assert entries == []


def test_build_delta_entries_prefers_real_sha_over_fallback() -> None:
    """When the memory file has a stored SHA, the verbatim git-log path
    takes precedence; the ``changed_artifacts`` payload is ignored.

    The fallback exists for the degenerate case (no SHA); when a SHA
    is available the adversarial-re-review path MUST be honoured.
    """
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _build_delta_entries

    memory_text = (
        "## Per-producer assessment\n\n"
        "### coder\n\n"
        "- producer: coder\n"
        "- last_reviewed_commit_sha: 0123abc\n"
        "- prior_verdict: ACK\n"
        "- prior_nack_reasons: -\n"
        "- prior_conditional_obligation: -\n"
        "- summary_of_assessment: clean.\n"
    )
    with patch("orchestrator.routes.event_prompt._run_git_log", return_value="(diff)"):
        entries = _build_delta_entries(
            action="ack",
            role="reviewer_code",
            base_branch="main",
            repo_path=Path("/tmp"),
            memory_text=memory_text,
            event_payload={"changed_artifacts": ["src/x.py"]},
        )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["producer"] == "coder"
    assert entry["last_reviewed_commit_sha"] == "0123abc"
    # The SHA-bearing path is NOT the fallback; "degraded baseline"
    # belongs only to the fallback render.
    assert "degraded baseline" not in entry["delta"]


# ---------------------------------------------------------------------------
# #3076: the re-review delta must end at the producer's PROPOSAL SHA, not
# the reviewer's own HEAD (which never contains the producer's commits —
# per-role worktrees), and the first-review fallback must render concrete
# `git show <sha>:<path>` reads instead of a directionless instruction.
# ---------------------------------------------------------------------------

_COD_MEMORY_WITH_SHA = (
    "## Per-producer assessment\n\n"
    "### coder\n\n"
    "- producer: coder\n"
    "- last_reviewed_commit_sha: 0123abc\n"
    "- prior_verdict: NACK\n"
    "- prior_nack_reasons: missing tests\n"
    "- prior_conditional_obligation: -\n"
    "- summary_of_assessment: needs revision.\n"
)


def test_build_delta_entries_scopes_delta_to_proposal_sha() -> None:
    """When pending_reviews carries the producer's proposal_commit_sha,
    the re-review delta runs ``{last_reviewed}..{proposal_sha}`` — NOT
    ``..HEAD``. ``{sha}..HEAD`` in the REVIEWER's worktree is empty even
    after the producer revises (the "re-review delta is empty" phantom
    NACK on pipeline-2b3d8b0b, #3076)."""
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _build_delta_entries

    with patch("orchestrator.routes.event_prompt._run_git_log", return_value="(diff)") as mock_log:
        entries = _build_delta_entries(
            action="nack",
            role="reviewer_code",
            base_branch="main",
            repo_path=Path("/tmp"),
            memory_text=_COD_MEMORY_WITH_SHA,
            event_payload={
                "pending_reviews": [
                    {
                        "producer": "coder",
                        "current_version": 2,
                        "artifact_refs": ["src/x.py"],
                        "proposal_commit_sha": "def5678",
                    }
                ]
            },
        )
    assert len(entries) == 1
    assert entries[0]["proposal_commit_sha"] == "def5678"
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs.get("end_ref") == "def5678", (
        f"delta must end at the proposal SHA, not HEAD — call was {mock_log.call_args!r}"
    )


def test_build_delta_entries_legacy_payload_keeps_head_endpoint() -> None:
    """Payloads without proposal_commit_sha keep the pre-#3076 HEAD
    endpoint — backward compatibility for legacy / synthetic callers."""
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _build_delta_entries

    with patch("orchestrator.routes.event_prompt._run_git_log", return_value="(diff)") as mock_log:
        _build_delta_entries(
            action="ack",
            role="reviewer_code",
            base_branch="main",
            repo_path=Path("/tmp"),
            memory_text=_COD_MEMORY_WITH_SHA,
            event_payload={"pending_reviews": [{"producer": "coder", "current_version": 1}]},
        )
    assert mock_log.call_args.kwargs.get("end_ref") == "HEAD"


def test_build_delta_entries_first_review_renders_git_show_commands() -> None:
    """First review (no stored SHA) with a proposal SHA renders concrete,
    working read commands — the producer's work is not in the reviewer's
    working tree, and a plain Read of the path is expected to fail."""
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    entries = _build_delta_entries(
        action="ack",
        role="risk_analyst",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "pending_reviews": [
                {
                    "producer": "architect",
                    "current_version": 1,
                    "artifact_refs": [".egg-state/drafts/p-plan.md"],
                    "proposal_commit_sha": "b521d7d",
                }
            ]
        },
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["proposal_commit_sha"] == "b521d7d"
    delta = entry["delta"]
    assert "git show b521d7d:.egg-state/drafts/p-plan.md" in delta
    assert "git log b521d7d --not origin/main -p" in delta
    assert "NOT in your working tree" in delta
    # The anti-phantom-NACK instruction is the point of the render.
    assert "Do NOT NACK" in delta

    # A 40-char SHA must render identically — guards against the
    # SHA-validation regex regressing to a fixed ``{7,7}`` width and
    # silently dropping full-length commit SHAs (reviewer suggestion #4
    # on #3078).
    full_sha = "b521d7d0123456789abcdef0123456789abcdef0"
    entries_full = _build_delta_entries(
        action="ack",
        role="risk_analyst",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "pending_reviews": [
                {
                    "producer": "architect",
                    "current_version": 1,
                    "artifact_refs": [".egg-state/drafts/p-plan.md"],
                    "proposal_commit_sha": full_sha,
                }
            ]
        },
    )
    assert len(entries_full) == 1
    delta_full = entries_full[0]["delta"]
    assert f"git show {full_sha}:.egg-state/drafts/p-plan.md" in delta_full
    assert f"git log {full_sha} --not origin/main -p" in delta_full


def test_build_delta_entries_first_review_strips_backticks_from_artifact_paths() -> None:
    """Producer-supplied artifact paths containing a backtick would otherwise
    break the markdown code span the agent renders. The first-review
    fallback strips backticks before interpolation; ``proposal_sha`` is
    hex-validated upstream so it is not the attack surface. Belt-and-
    braces only -- the agent (not bash) is the consumer, so this is not
    a shell-injection vector."""
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "pending_reviews": [
                {
                    "producer": "coder",
                    "current_version": 1,
                    "artifact_refs": ["src/`evil`.py", "src/ok.py"],
                    "proposal_commit_sha": "abc1234",
                }
            ]
        },
    )
    assert len(entries) == 1
    delta = entries[0]["delta"]
    # The stripped path is what gets interpolated; no raw backtick from
    # the artifact value reaches the rendered ``git show`` command, which
    # would otherwise terminate the surrounding markdown code span.
    assert "git show abc1234:src/evil.py" in delta
    assert "git show abc1234:src/ok.py" in delta
    assert "`evil`" not in delta


def test_build_delta_entries_first_review_no_sha_strips_backticks_from_refs_text() -> None:
    """Parallel to the ``if proposal_sha:`` strip test above, but covers
    the ``else`` (no-SHA) branch at ``event_prompt.py``. The strip is one
    list comp upstream of both branches (line 731), so this is a
    belt-and-braces pin on the shared upstream — a regression that
    re-introduces unescaped backticks in either rendered path would
    surface in both tests."""
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "pending_reviews": [
                {
                    "producer": "coder",
                    "current_version": 1,
                    "artifact_refs": ["src/`evil`.py", "src/ok.py"],
                    # No proposal_commit_sha -> drives the else branch
                    # (degraded-baseline ``refs_text`` rendering).
                }
            ]
        },
    )
    assert len(entries) == 1
    delta = entries[0]["delta"]
    # ``refs_text`` interpolates each artifact in a backtick-wrapped code
    # span (``- `{a}` ``). With the strip applied, ``evil`` lands without
    # surrounding raw backticks; without it, the inner backticks would
    # terminate the markdown code span early.
    assert "- `src/evil.py`" in delta
    assert "- `src/ok.py`" in delta
    assert "`evil`" not in delta
    # Sanity: this is the no-SHA branch, no ``git show`` command rendered.
    assert "git show" not in delta


def test_build_delta_entries_first_review_sha_without_artifacts_still_renders() -> None:
    """A proposal SHA with an empty artifact list still yields an entry
    (the full-change git log command) — pre-#3076 the empty artifact
    list silently dropped the producer from the section."""
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text="",
        event_payload={
            "pending_reviews": [
                {
                    "producer": "coder",
                    "current_version": 1,
                    "artifact_refs": [],
                    "proposal_commit_sha": "abc1234",
                }
            ]
        },
    )
    assert len(entries) == 1
    assert "git log abc1234 --not origin/main -p" in entries[0]["delta"]


def test_extract_proposal_sha_rejects_non_hex_tokens() -> None:
    """The SHA is interpolated into rendered shell commands — anything
    that is not a 7-64 char hex token must be discarded, falling back to
    the legacy degraded-baseline path."""
    from orchestrator.routes.event_prompt import _extract_proposal_sha_for_producer

    def payload(sha):
        return {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}

    assert _extract_proposal_sha_for_producer(payload("abc1234"), "coder") == "abc1234"
    assert _extract_proposal_sha_for_producer(payload("ABC1234DEF"), "coder") == "ABC1234DEF"
    for bad in ("$(rm -rf /)", "HEAD", "abc123", "abc1234..def5678", "", None, 42):
        assert _extract_proposal_sha_for_producer(payload(bad), "coder") == "", (
            f"non-hex token {bad!r} must be discarded"
        )


def test_render_delta_section_shows_proposal_sha_and_cautions_on_head() -> None:
    """The rendered section substitutes the proposal SHA into the verbatim
    command and only trusts an empty range when it was proposal-scoped;
    an empty HEAD-scoped range gets an explicit caution instead of the
    "re-review is a no-op" verdict."""
    from orchestrator.routes.event_prompt import _render_producer_delta_section

    # Proposal-scoped: empty range is a trustworthy no-op.
    section, _ = _render_producer_delta_section(
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "0123abc",
                "proposal_commit_sha": "def5678",
                "delta": "",
            }
        ],
        "main",
    )
    assert "git log 0123abc..def5678 --not origin/main -p" in section
    assert "proposal_commit_sha: `def5678`" in section
    assert "re-review is a no-op" in section
    assert "CAUTION" not in section

    # HEAD-scoped (legacy): empty range is NOT evidence — caution required.
    section, _ = _render_producer_delta_section(
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "0123abc",
                "proposal_commit_sha": "",
                "delta": "",
            }
        ],
        "main",
    )
    assert "git log 0123abc..HEAD --not origin/main -p" in section
    assert "CAUTION" in section
    assert "NOT evidence" in section


def test_empty_delta_caution_cross_references_not_synced_banner() -> None:
    """The HEAD-scoped empty-delta caution names the wrapper's
    "NOT-synced" banner and steers the reviewer at the rendered
    fallback command in the same section (#3077 slice-1 TASK-1-2).

    Plan TASK-1-2 acceptance: "Empty-delta caution names the
    NOT-synced banner and the fallback command." The R1 contract
    is end-to-end: the wrapper prepends a "worktree NOT synced ..."
    banner on a sync failure, and the empty-delta caution — which the
    reviewer hits when the delta block is empty and the worktree may
    not contain the producer's commits — must point back at that same
    banner so the reviewer follows a single, coherent decision tree
    (check the banner → fall back to the rendered ``git log`` /
    ``git show`` command) rather than two disconnected hints. The
    re-review path renders a ``git log <sha>..<end_ref>`` command in
    its "Re-review scope" line; the caution names that command rather
    than ``git show`` (which is only rendered in the first-review
    fallback path elsewhere in the prompt) so the reviewer doesn't
    search for a string that isn't in their section.

    Non-empty-delta and proposal-scoped paths are intentionally NOT
    asserted on here — TASK-1-2 acceptance line 2 ("Non-empty-delta
    rendering is byte-identical to today") is covered by the
    surrounding tests in this module
    (``test_render_delta_section_shows_proposal_sha_and_cautions_on_head``,
    ``test_producer_delta_empty_string_renders_no_commits_sentinel``).
    """
    from orchestrator.routes.event_prompt import _render_producer_delta_section

    # HEAD-scoped (no proposal_commit_sha) + empty delta is the only
    # case that emits the caution; that is the case TASK-1-2 amends.
    section, _ = _render_producer_delta_section(
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "0123abc",
                "proposal_commit_sha": "",
                "delta": "",
            }
        ],
        "main",
    )
    # Banner cross-reference: the caution mentions the NOT-synced
    # banner so a reviewer who sees both an empty delta AND the
    # wrapper-prepended banner has one coherent path forward.
    assert "NOT-synced" in section or "NOT synced" in section, (
        "Empty-delta caution must name the wrapper's NOT-synced "
        "banner (#3077 slice-1 TASK-1-2). Without the cross-reference "
        "the reviewer sees two disconnected hints — an empty delta "
        "block and a separate banner — instead of a single decision "
        "tree."
    )
    assert "banner" in section.lower(), (
        "Empty-delta caution must use the word 'banner' so the "
        "reviewer correlates it with the wrapper's prepended banner "
        "rather than guessing what 'NOT synced' refers to."
    )
    # Rendered fallback: the caution must name the rendered command in
    # the same section so the reviewer has a coherent next step. The
    # re-review path renders ``git log <sha>..<end_ref>`` (the
    # "Re-review scope" line above the empty delta block); ``git show``
    # only appears in the first-review fallback elsewhere. The caution
    # names ``git log`` so it cross-references content that's actually
    # in the reviewer's section (#3077 slice-1 follow-up to the
    # initially-decorative ``git show`` cross-reference).
    assert "git log" in section, (
        "Empty-delta caution must point at the rendered ``git log`` "
        "fallback — that is the command actually rendered above the "
        "empty delta in the re-review path. Plan TASK-1-2 acceptance: "
        "'Empty-delta caution names the NOT-synced banner and the "
        "fallback command.'"
    )


def test_empty_delta_caution_unchanged_for_proposal_scoped_empty_range() -> None:
    """Proposal-scoped empty delta keeps the trustworthy ``re-review is
    a no-op`` verdict — the slice-1 TASK-1-2 amendment touches ONLY the
    HEAD-scoped legacy caution, not the proposal-scoped path.

    Plan TASK-1-2 acceptance: "Non-empty-delta rendering is
    byte-identical to today." The proposal-scoped empty case is the
    closest analogue and must also stay unchanged: it never had a
    caution to amend.
    """
    from orchestrator.routes.event_prompt import _render_producer_delta_section

    section, _ = _render_producer_delta_section(
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "0123abc",
                "proposal_commit_sha": "def5678",
                "delta": "",
            }
        ],
        "main",
    )
    assert "re-review is a no-op" in section
    # The proposal-scoped empty case is the trustworthy no-op verdict,
    # so the NOT-synced banner cross-reference does NOT belong here.
    assert "NOT-synced" not in section
    assert "NOT synced" not in section


# ---------------------------------------------------------------------------
# CLI tests: the wrapper-bash entry-point (NACK #2 from reviewer_contract —
# plan TASK-3-2 acceptance "snapshot test verifies both branches" for
# EGG_BRC_MEMORY={full,write-only,off}).
# ---------------------------------------------------------------------------


def _make_tmp_repo_with_memory(
    tmp_path,
    *,
    role: str,
    producer_sha: str | None = None,
    codebase: str = "",
) -> tuple:
    """Build a tmp repo with a populated brc-memory.md file."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test"],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "test"],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=False,
        capture_output=True,
    )
    mem_dir = repo / ".egg-state" / "agent-outputs" / role
    mem_dir.mkdir(parents=True, exist_ok=True)
    body_parts = [
        "## Codebase / change model",
        "",
        codebase or "Slice-3 testing.",
        "",
        "## Per-producer assessment",
        "",
    ]
    if producer_sha is not None:
        body_parts.extend(
            [
                "### coder",
                "",
                "- producer: coder",
                f"- last_reviewed_commit_sha: {producer_sha}",
                "- prior_verdict: NACK",
                "- prior_nack_reasons: missing edge case",
                "- prior_conditional_obligation: -",
                "- summary_of_assessment: needs retry",
                "",
            ]
        )
    body_parts.extend(["## Decision log", "", "- entry", ""])
    # Pipeline-scoped filename (#3163) — matches the EGG_PIPELINE_ID that
    # ``_run_cli`` exports.
    (mem_dir / "brc-memory-issue-99.md").write_text("\n".join(body_parts), encoding="utf-8")
    return repo


def _run_cli(repo, *, role: str, memory_mode: str, action: str, event_payload: dict) -> str:
    """Run ``event_prompt.py`` as a CLI subprocess against the tmp repo."""
    import io
    import json as _json
    import os
    import sys as _sys

    from orchestrator.routes import event_prompt

    saved_env = {
        k: os.environ.get(k)
        for k in (
            "EGG_AGENT_ROLE",
            "EGG_BASE_BRANCH",
            "EGG_REPO_PATH",
            "EGG_BRC_MEMORY",
            "EGG_PIPELINE_ID",
            "EGG_ISSUE_NUMBER",
        )
    }
    saved_stdin = _sys.stdin
    saved_stdout = _sys.stdout
    out_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = role
        os.environ["EGG_BASE_BRANCH"] = "main"
        os.environ["EGG_REPO_PATH"] = str(repo)
        os.environ["EGG_BRC_MEMORY"] = memory_mode
        os.environ["EGG_PIPELINE_ID"] = "issue-99"
        os.environ.pop("EGG_ISSUE_NUMBER", None)
        _sys.stdin = io.StringIO(_json.dumps(event_payload))
        _sys.stdout = out_buf
        rc = event_prompt._cli([action])
    finally:
        _sys.stdin = saved_stdin
        _sys.stdout = saved_stdout
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 0, f"_cli returned non-zero rc={rc}"
    return out_buf.getvalue()


def test_cli_full_mode_emits_memory_and_delta(tmp_path) -> None:
    """``EGG_BRC_MEMORY=full`` renders both the memory excerpt AND the
    per-producer git-log delta (the slice-4 default).
    """
    repo = _make_tmp_repo_with_memory(
        tmp_path,
        role="reviewer_code",
        producer_sha="0123abc",
        codebase="Full-mode test.",
    )
    out = _run_cli(
        repo,
        role="reviewer_code",
        memory_mode="full",
        action="ack",
        event_payload={"action": "ack", "producer": "coder", "version": 2},
    )
    assert "## Durable BRC memory (tail-position context)" in out
    assert "Full-mode test." in out
    # Verbatim git-log command with substituted SHA.
    assert "git log 0123abc..HEAD --not origin/main -p" in out


def test_cli_write_only_mode_omits_memory_keeps_delta(tmp_path) -> None:
    """``EGG_BRC_MEMORY=write-only`` omits the memory excerpt but
    STILL emits the per-producer git-log delta against the memory
    file's stored SHAs as a fallback baseline. The plan TASK-3-2
    wording is verbatim: "with EGG_BRC_MEMORY=write-only (slice-1
    default), the prompt omits memory but still emits the git-log
    delta against the orchestrator's signal-level changed_artifacts
    as a fallback baseline". (Slice-4 task-4-1 flipped the unset-env
    default to ``full`` so production reads memory by default;
    ``write-only`` is now the one-release rollback target that keeps
    the writer warm without consuming the excerpt.)
    """
    repo = _make_tmp_repo_with_memory(
        tmp_path,
        role="reviewer_code",
        producer_sha="0123abc",
        codebase="Write-only-mode test.",
    )
    out = _run_cli(
        repo,
        role="reviewer_code",
        memory_mode="write-only",
        action="ack",
        event_payload={"action": "ack", "producer": "coder", "version": 2},
    )
    # Memory section omitted.
    assert "## Durable BRC memory" not in out
    assert "Write-only-mode test." not in out
    # But the per-producer delta is still rendered against the stored SHA.
    assert "git log 0123abc..HEAD --not origin/main -p" in out


def test_cli_ignores_prior_pipeline_role_keyed_memory(tmp_path) -> None:
    """A stale role-keyed ``brc-memory.md`` (the pre-#3163 filename,
    inherited from a previous pipeline via main) must never reach the
    prompt: the reader only resolves the pipeline-scoped filename.
    Observed live: a task_planner's first event prompt in pipeline
    issue-3064 carried memory headed issue-3077.
    """
    repo = _make_tmp_repo_with_memory(
        tmp_path,
        role="reviewer_code",
        producer_sha="0123abc",
        codebase="Current-pipeline memory.",
    )
    mem_dir = repo / ".egg-state" / "agent-outputs" / "reviewer_code"
    # Rename to the legacy role-keyed filename — as if it came from a
    # prior pipeline's merged context PR.
    (mem_dir / "brc-memory-issue-99.md").rename(mem_dir / "brc-memory.md")
    out = _run_cli(
        repo,
        role="reviewer_code",
        memory_mode="full",
        action="ack",
        event_payload={"action": "ack", "producer": "coder", "version": 2},
    )
    assert "Current-pipeline memory." not in out
    assert "## Durable BRC memory" not in out


def test_cli_off_mode_omits_memory_and_uses_changed_artifacts_fallback(tmp_path) -> None:
    """``EGG_BRC_MEMORY=off`` omits memory entirely but the
    ``changed_artifacts`` fallback still surfaces a degraded baseline
    when the event payload carries an artifact list.
    """
    repo = _make_tmp_repo_with_memory(
        tmp_path,
        role="reviewer_code",
        producer_sha=None,  # no SHA recorded
    )
    out = _run_cli(
        repo,
        role="reviewer_code",
        memory_mode="off",
        action="ack",
        event_payload={
            "action": "ack",
            "producer": "coder",
            "changed_artifacts": ["src/foo.py"],
        },
    )
    assert "## Durable BRC memory" not in out
    # changed_artifacts fallback fires when no SHA is available.
    assert "src/foo.py" in out
    assert "degraded baseline" in out


# ---------------------------------------------------------------------------
# reviewer_code v2 NACK: REVIEWER-SYNC.md path is shared/prompts/,
# NOT docs/architecture/ — every rendered prompt section must cite
# the correct path so an agent following the link doesn't 404.
# ---------------------------------------------------------------------------


def test_per_producer_delta_section_cites_correct_reviewer_sync_path() -> None:
    """The rendered per-producer delta section must point at
    ``shared/prompts/REVIEWER-SYNC.md`` (the real location), NOT the
    legacy ``docs/architecture/REVIEWER-SYNC.md`` placeholder that
    would 404 for any agent following the link.
    """
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack", "pending_reviews": [{"producer": "coder", "current_version": 1}]},
        "",
        [],
        [
            {
                "producer": "coder",
                "last_reviewed_commit_sha": "abc1234",
                "delta": "(diff body)",
            }
        ],
        "main",
    )
    # Correct path appears in the rendered prompt.
    assert "shared/prompts/REVIEWER-SYNC.md" in prompt
    # Wrong path absent — regression guard against the v1 placeholder.
    assert "docs/architecture/REVIEWER-SYNC.md" not in prompt


def test_module_docstring_cites_correct_reviewer_sync_path() -> None:
    """The module docstring is read by developers; it must cite the
    real ``shared/prompts/REVIEWER-SYNC.md`` location rather than the
    legacy ``docs/architecture/`` placeholder.
    """
    from orchestrator.routes import event_prompt

    doc = event_prompt.__doc__ or ""
    assert "shared/prompts/REVIEWER-SYNC.md" in doc
    assert "docs/architecture/REVIEWER-SYNC.md" not in doc


# ---------------------------------------------------------------------------
# reviewer_code_holistic v2 finding #2: ``_extract_nacks`` must accept the
# ``unresolved_nacks`` key the next-action route emits for the
# single-reviewer NACK propose path (the common case — the open-NACK
# barrier requires 2+ reviewers).
# ---------------------------------------------------------------------------


def test_extract_nacks_accepts_unresolved_nacks_key_from_next_action() -> None:
    """``unresolved_nacks`` is the key the next-action route's
    single-reviewer NACK propose path emits (``_derive_next_action``
    lines 329-348). The composer MUST extract it; omitting this key
    silently dropped single-reviewer NACK feedback from the per-event
    prompt (reviewer_code_holistic v2 finding #2).
    """
    from orchestrator.routes.event_prompt import _extract_nacks

    payload = {
        "producer": "coder",
        "unresolved_nacks": [
            {
                "reviewer": "reviewer_code",
                "version": 1,
                "reason": "missing edge case in foo()",
                "artifact_refs": ["src/foo.py"],
            },
        ],
    }
    extracted = _extract_nacks(payload)
    assert len(extracted) == 1
    assert extracted[0]["reviewer"] == "reviewer_code"
    assert extracted[0]["reason"] == "missing edge case in foo()"


def test_compose_event_prompt_renders_unresolved_nacks_section() -> None:
    """End-to-end: a producer re-propose event carrying
    ``unresolved_nacks`` must render the structured ``Open NACKs``
    section so the agent can see reviewer ``reason`` + ``artifact_refs``
    directly in the prompt (not recover them via a separate fetch).
    """
    from orchestrator.routes.event_prompt import _extract_nacks

    payload = {
        "producer": "coder",
        "unresolved_nacks": [
            {
                "reviewer": "reviewer_code",
                "version": 2,
                "reason": "the foo() helper still raises on None",
                "artifact_refs": ["orchestrator/routes/foo.py"],
            },
        ],
    }
    nacks = _extract_nacks(payload)
    prompt = compose_event_prompt(
        "coder",
        payload,
        "",
        nacks,
        [],  # producer side: no per-producer delta
        "main",
    )
    # Structured Open-NACKs section renders with the reviewer's identity
    # + reason + artifact_refs (the round-trip-per-NACK signal #2142
    # was built to enforce).
    assert "## Open NACKs against the current proposal version" in prompt
    assert "reviewer_code" in prompt
    assert "the foo() helper still raises on None" in prompt
    assert "orchestrator/routes/foo.py" in prompt


def test_extract_nacks_priority_order_nacks_over_unresolved_nacks() -> None:
    """When both keys are present, ``nacks`` takes priority — it's the
    canonical 2+-reviewer barrier shape that should override the
    single-reviewer convenience key. The ``unresolved_nacks`` payload
    is silently dropped in that edge case to keep barrier semantics
    primary.
    """
    from orchestrator.routes.event_prompt import _extract_nacks

    payload = {
        "nacks": [{"reviewer": "rA", "reason": "barrier"}],
        "unresolved_nacks": [{"reviewer": "rB", "reason": "single"}],
    }
    extracted = _extract_nacks(payload)
    assert len(extracted) == 1
    assert extracted[0]["reviewer"] == "rA"


# ---------------------------------------------------------------------------
# reviewer_code_holistic v2 finding #3: ``_build_delta_entries`` must
# scope the producer set to the current event's ``pending_reviews``,
# not enumerate every producer in the reviewer's memory file. Stale
# deltas for unrelated prior producers must not ride along when the
# event names a different producer for THIS invocation.
# ---------------------------------------------------------------------------


def test_build_delta_entries_scopes_to_pending_reviews_producer() -> None:
    """Memory has SHA for coder; event names tester. Renderer must
    produce the *tester* fallback (not coder's stale delta).
    """
    from pathlib import Path

    from orchestrator.routes.event_prompt import _build_delta_entries

    memory_text = "## Per-producer assessment\n\n### coder\n\n- last_reviewed_commit_sha: abc1234\n"
    entries = _build_delta_entries(
        action="ack",
        role="reviewer_code",
        base_branch="main",
        repo_path=Path("/tmp"),
        memory_text=memory_text,
        event_payload={
            "pending_reviews": [
                {
                    "producer": "tester",
                    "current_version": 1,
                    "artifact_refs": ["tests/test_x.py"],
                }
            ],
        },
    )
    # The rendered set should be scoped to *tester* (the current event's
    # producer) — NOT to coder (the stale memory producer).
    rendered_producers = [e["producer"] for e in entries]
    assert "tester" in rendered_producers
    assert "coder" not in rendered_producers
    # Tester has no SHA in memory → fallback artifact_refs render.
    tester_entry = next(e for e in entries if e["producer"] == "tester")
    assert tester_entry["last_reviewed_commit_sha"] == ""
    assert "tests/test_x.py" in tester_entry["delta"]
    assert "degraded baseline" in tester_entry["delta"]


def test_build_delta_entries_pending_reviews_with_sha_renders_real_delta() -> None:
    """When pending_reviews names producer X AND memory has X's SHA,
    render the verbatim git-log delta for X (not the fallback).
    """
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _build_delta_entries

    memory_text = (
        "## Per-producer assessment\n\n"
        "### coder\n\n"
        "- last_reviewed_commit_sha: aaaa111\n"
        "\n"
        "### tester\n\n"
        "- last_reviewed_commit_sha: bbbb222\n"
    )
    with patch(
        "orchestrator.routes.event_prompt._run_git_log",
        return_value="(real diff for tester)",
    ) as mock_log:
        entries = _build_delta_entries(
            action="ack",
            role="reviewer_code",
            base_branch="main",
            repo_path=Path("/tmp"),
            memory_text=memory_text,
            event_payload={
                "pending_reviews": [{"producer": "tester", "current_version": 2}],
            },
        )
    assert len(entries) == 1
    assert entries[0]["producer"] == "tester"
    assert entries[0]["last_reviewed_commit_sha"] == "bbbb222"
    # Verify ``_run_git_log`` was invoked only for tester's SHA, not
    # coder's — the scoping invariant must hold inside the git
    # subprocess layer too.
    sha_args = [call.args[0] for call in mock_log.call_args_list]
    assert sha_args == ["bbbb222"]


def test_build_delta_entries_multiple_pending_reviews_renders_each() -> None:
    """``pending_reviews`` may name multiple producers (e.g. on the
    first reviewer invocation of a slice). Each named producer
    surfaces in the rendered entries.
    """
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _build_delta_entries

    memory_text = "## Per-producer assessment\n\n### coder\n\n- last_reviewed_commit_sha: aaaa111\n"
    with patch(
        "orchestrator.routes.event_prompt._run_git_log",
        return_value="(diff)",
    ):
        entries = _build_delta_entries(
            action="ack",
            role="reviewer_code",
            base_branch="main",
            repo_path=Path("/tmp"),
            memory_text=memory_text,
            event_payload={
                "pending_reviews": [
                    {"producer": "coder", "current_version": 2},
                    {
                        "producer": "documenter",
                        "current_version": 1,
                        "artifact_refs": ["docs/x.md"],
                    },
                ],
            },
        )
    producers = [e["producer"] for e in entries]
    assert "coder" in producers
    assert "documenter" in producers
    # coder has a stored SHA → real delta path
    coder_entry = next(e for e in entries if e["producer"] == "coder")
    assert coder_entry["last_reviewed_commit_sha"] == "aaaa111"
    # documenter has no SHA → fallback to per-producer artifact_refs
    doc_entry = next(e for e in entries if e["producer"] == "documenter")
    assert doc_entry["last_reviewed_commit_sha"] == ""
    assert "docs/x.md" in doc_entry["delta"]


def test_build_delta_entries_no_pending_reviews_falls_back_to_memory_enum() -> None:
    """Legacy / synthetic-test payloads (no ``pending_reviews`` key)
    fall back to enumerating all stored memory SHAs — preserves
    backward compatibility for callers that bypass next-action.
    """
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _build_delta_entries

    memory_text = (
        "## Per-producer assessment\n\n"
        "### coder\n\n"
        "- last_reviewed_commit_sha: aaaa111\n"
        "\n"
        "### tester\n\n"
        "- last_reviewed_commit_sha: bbbb222\n"
    )
    with patch(
        "orchestrator.routes.event_prompt._run_git_log",
        return_value="(diff)",
    ):
        entries = _build_delta_entries(
            action="ack",
            role="reviewer_code",
            base_branch="main",
            repo_path=Path("/tmp"),
            memory_text=memory_text,
            event_payload={},  # no pending_reviews, no producer
        )
    # Both stored producers render (legacy fallback path).
    rendered = sorted(e["producer"] for e in entries)
    assert rendered == ["coder", "tester"]


def test_extract_current_producers_dedupes_in_first_seen_order() -> None:
    """The producer-extraction helper must preserve first-seen order
    so rendered sections are stable across calls with the same
    payload, and must de-dupe so a producer named twice doesn't
    render twice.
    """
    from orchestrator.routes.event_prompt import _extract_current_producers

    payload = {
        "pending_reviews": [
            {"producer": "tester"},
            {"producer": "coder"},
            {"producer": "tester"},  # duplicate
            {"producer": "documenter"},
        ],
    }
    assert _extract_current_producers(payload) == ["tester", "coder", "documenter"]


def test_extract_current_producers_falls_back_to_top_level_producer() -> None:
    """Producer-side events don't carry ``pending_reviews``; the
    extractor must fall back to top-level ``producer`` /
    ``producer_role``.
    """
    from orchestrator.routes.event_prompt import _extract_current_producers

    assert _extract_current_producers({"producer": "coder"}) == ["coder"]
    assert _extract_current_producers({"producer_role": "tester"}) == ["tester"]


def test_extract_artifacts_for_producer_walks_pending_reviews_first() -> None:
    """The per-producer artifact fallback must prefer
    ``pending_reviews[i].artifact_refs`` (the production payload
    shape) over the legacy top-level ``changed_artifacts`` key.
    """
    from orchestrator.routes.event_prompt import _extract_artifacts_for_producer

    payload = {
        "pending_reviews": [
            {"producer": "coder", "artifact_refs": ["src/foo.py"]},
            {"producer": "tester", "artifact_refs": ["tests/test_x.py"]},
        ],
        "changed_artifacts": ["legacy/path.py"],
    }
    # Each producer gets its OWN artifact_refs.
    assert _extract_artifacts_for_producer(payload, "coder") == ["src/foo.py"]
    assert _extract_artifacts_for_producer(payload, "tester") == ["tests/test_x.py"]


def test_extract_artifacts_for_producer_respects_top_level_producer_match() -> None:
    """When falling back to top-level ``changed_artifacts``, the
    extractor must only honour the fallback if the payload's
    top-level ``producer`` key matches the requested producer —
    prevents cross-producer artifact leak in legacy / synthetic
    test paths.
    """
    from orchestrator.routes.event_prompt import _extract_artifacts_for_producer

    payload = {"producer": "coder", "changed_artifacts": ["src/foo.py"]}
    # Match → return.
    assert _extract_artifacts_for_producer(payload, "coder") == ["src/foo.py"]
    # Mismatch → empty (no leak).
    assert _extract_artifacts_for_producer(payload, "tester") == []


# ===========================================================================
# Tester hardening (#2908 slice-3 task-3-6)
#
# Adversarial coverage on top of the coder-authored scaffold above.
# The coder's tests cover the happy-path shapes for each composer
# input; the hardening below probes the SUSPECT BOUNDARIES — exact
# off-by-one positions, UTF-8 multibyte byte/codepoint mismatches,
# defensive defaults on missing keys, subprocess error paths, and
# the CLI's stdin/--event-payload-file branches.
# ===========================================================================


def test_truncate_exact_boundary_returns_input_unchanged() -> None:
    """At ``len(text) == max_chars`` the truncator MUST be a no-op.

    The off-by-one check matters: ``text[: max_chars - 1]`` would silently
    drop one code point at the cap. Coverage gap in the original suite —
    only over-cap and under-cap cases were tested.
    """
    from orchestrator.routes.event_prompt import MEMORY_EXCERPT_MAX_CHARS, _truncate

    exact = "x" * MEMORY_EXCERPT_MAX_CHARS
    assert _truncate(exact, MEMORY_EXCERPT_MAX_CHARS) == exact
    assert "…" not in _truncate(exact, MEMORY_EXCERPT_MAX_CHARS)


def test_truncate_just_over_boundary_inserts_ellipsis_at_cap_minus_one() -> None:
    """At ``len(text) == max_chars + 1`` the result is ``max_chars`` chars + ``…``."""
    from orchestrator.routes.event_prompt import _truncate

    out = _truncate("x" * 11, 10)
    assert len(out) == 10
    assert out.endswith("…")
    assert out.count("x") == 9  # max_chars - 1 = 9 surviving x's


def test_memory_excerpt_utf8_multibyte_truncates_at_codepoint_cap() -> None:
    """UTF-8 multibyte chars truncate at the *code-point* cap, not byte cap.

    A 3000-codepoint CJK excerpt (~9000 bytes) is truncated to 2000
    code points (~6000 bytes) — well under the 10 KB envelope. The cap
    is documented as a code-point cap in ``_truncate`` so this is the
    intentional behaviour; the test pins it so a future "switch to
    bytes" refactor can't slip in unnoticed.
    """
    from orchestrator.routes.event_prompt import MEMORY_EXCERPT_MAX_CHARS

    excerpt = "中" * (MEMORY_EXCERPT_MAX_CHARS + 1000)
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        excerpt,
        [],
        [],
        "main",
    )
    section = prompt.split("## Durable BRC memory")[1]
    # 2000 codepoint cap → 1999 中 + 1 ellipsis (= 2000 code points total)
    assert section.count("中") == MEMORY_EXCERPT_MAX_CHARS - 1
    assert "…" in section
    # Envelope (with no NACKs, no delta) MUST still respect the 10 KB cap.
    envelope_bytes = len(_strip_git_log_blocks(prompt).encode("utf-8"))
    assert envelope_bytes <= PROMPT_ENVELOPE_MAX_BYTES, (
        f"envelope is {envelope_bytes} > {PROMPT_ENVELOPE_MAX_BYTES} bytes"
    )


def test_memory_excerpt_whitespace_only_omits_section() -> None:
    """Whitespace-only memory excerpt → section omitted (truncated.strip() empty).

    The composer's ``_render_memory_section`` guards on ``truncated.strip()``;
    a memory file that contains only whitespace (e.g. a freshly-created
    empty scaffold) must NOT render an empty memory block — that would
    waste bytes and confuse the agent's "memory present" heuristic.
    """
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "   \n\n\t\n",
        [],
        [],
        "main",
    )
    assert "## Durable BRC memory" not in prompt


def test_producer_delta_order_preserves_input_order() -> None:
    """The renderer iterates ``git_log_delta`` verbatim — it does NOT sort.

    Sorting belongs to the CLI's ``_build_delta_entries`` (which
    canonicalises producer order via ``sorted(per_producer.keys())``
    on the memory-enum fallback path). The composer accepts the
    caller's order so the production payload's preferred order
    (e.g. ``pending_reviews``-driven priority) flows through. This
    pins that contract.
    """
    deltas = [
        {"producer": "zeta", "last_reviewed_commit_sha": "z1", "delta": "z"},
        {"producer": "alpha", "last_reviewed_commit_sha": "a1", "delta": "a"},
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        deltas,
        "main",
    )
    zeta_idx = prompt.index("Producer: ``zeta``")
    alpha_idx = prompt.index("Producer: ``alpha``")
    assert zeta_idx < alpha_idx, "Producer delta order MUST follow input list, not sort"


def test_producer_delta_empty_string_renders_no_commits_sentinel() -> None:
    """Empty ``delta`` string renders the ``(no commits in range)`` sentinel.

    This is the real production state when a producer ACKs a confirmed
    proposal and re-confirms without any new commits between the
    ``last_reviewed_commit_sha`` and the proposal SHA. The agent must
    see the sentinel so they know the re-review is a no-op rather than
    silently reading a blank diff block.

    #3076: the trustworthy no-op verdict requires the range to have
    been scoped to the producer's ``proposal_commit_sha``. An empty
    HEAD-scoped range (legacy payloads) gets an explicit caution
    instead — the reviewer's own HEAD never contains the producer's
    commits, so emptiness there is NOT evidence of no revision (the
    "re-review delta is empty" phantom NACK).
    """
    deltas = [
        {
            "producer": "coder",
            "last_reviewed_commit_sha": "abc1234",
            "proposal_commit_sha": "def5678",
            "delta": "   \n\n",
        },
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        deltas,
        "main",
    )
    assert "(no commits in range — re-review is a no-op)" in prompt

    # Legacy HEAD-scoped empty range: caution, not a no-op verdict.
    deltas = [
        {"producer": "coder", "last_reviewed_commit_sha": "abc1234", "delta": "   \n\n"},
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        deltas,
        "main",
    )
    assert "re-review is a no-op" not in prompt
    assert "CAUTION" in prompt


def test_producer_delta_missing_keys_renders_defensive_defaults() -> None:
    """Missing ``producer`` / ``sha`` / ``delta`` keys do not crash.

    Surfaces the ``(unknown)`` producer label and the ``<no prior
    review>`` SHA sentinel so the agent can audit the degenerate
    shape rather than silently re-reviewing a zero-length diff.
    """
    deltas = [{}]  # everything missing
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        deltas,
        "main",
    )
    assert "Producer: ``(unknown)``" in prompt
    assert "<no prior review — full branch history>" in prompt


def test_producer_delta_non_string_delta_is_coerced() -> None:
    """A non-string ``delta`` value is coerced via ``str()`` rather than crashing.

    Defensive against an upstream renderer that passes a list or dict
    accidentally — the assertion pins ``str()`` coercion so a future
    type tightening (e.g. ``isinstance(delta, str)``) doesn't silently
    drop the entry.
    """
    deltas = [
        {"producer": "coder", "last_reviewed_commit_sha": "abc1234", "delta": 42},
    ]
    prompt = compose_event_prompt(
        "reviewer_code",
        {"action": "ack"},
        "",
        [],
        deltas,
        "main",
    )
    # 42 stringified appears in the diff body.
    assert "42" in prompt
    # Section still renders the verbatim command.
    assert "git log abc1234..HEAD --not origin/main -p" in prompt


def test_nack_missing_reviewer_renders_question_mark_sentinel() -> None:
    """A NACK with missing/empty ``reviewer`` renders ``?`` rather than crashing.

    Defensive shape against a malformed barrier payload.
    """
    nacks = [{"version": 2, "reason": "blocker", "artifact_refs": ["a"]}]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )
    assert "Reviewer: ``?``" in prompt


def test_nack_missing_reason_renders_none_recorded_sentinel() -> None:
    """A NACK with no ``reason`` falls back to the ``(none recorded)`` sentinel.

    The barrier surface must not crash on a partial NACK render; the
    sentinel tells the agent the field is missing rather than silently
    leaving a blank line.
    """
    nacks = [{"reviewer": "reviewer_code", "version": 1, "artifact_refs": []}]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
    )
    assert "(none recorded)" in prompt


def test_nack_non_list_artifact_refs_is_normalised_to_singleton() -> None:
    """A scalar ``artifact_refs`` (string instead of list) is wrapped to ``[ref]``.

    Forward-compat against a producer that serialises a single ref as
    a bare string. The renderer must surface the ref rather than
    dropping it.
    """
    nacks = [
        {
            "reviewer": "reviewer_code",
            "version": 1,
            "reason": "blocker",
            "artifact_refs": "src/single.py",  # not a list
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
    assert "src/single.py" in prompt


def test_parse_per_producer_sha_skips_dash_sentinel() -> None:
    """The slice-1 writer's ``-`` sentinel ("no prior review") MUST be skipped.

    Rendering ``git log -..HEAD`` would either hang or crash the
    subprocess. This is the on-disk shape for a first-time ACK
    before any review has happened.
    """
    from orchestrator.routes.event_prompt import _parse_per_producer_sha

    text = (
        "### coder\n\n"
        "- last_reviewed_commit_sha: -\n\n"
        "### documenter\n\n"
        "- last_reviewed_commit_sha: abc123\n"
    )
    parsed = _parse_per_producer_sha(text)
    assert "coder" not in parsed  # dash sentinel skipped
    assert parsed.get("documenter") == "abc123"


def test_parse_per_producer_sha_first_match_wins_per_heading() -> None:
    """Two bullets under one heading: the first wins (matches slice-1 writer's shape)."""
    from orchestrator.routes.event_prompt import _parse_per_producer_sha

    text = (
        "### coder\n\n"
        "- last_reviewed_commit_sha: aaa111\n"
        "- last_reviewed_commit_sha: bbb222\n"  # ignored
    )
    assert _parse_per_producer_sha(text) == {"coder": "aaa111"}


def test_parse_per_producer_sha_ignores_orphan_bullet() -> None:
    """A SHA bullet without a preceding heading is dropped (no anchor).

    Without this guard, a stray ``- last_reviewed_commit_sha: …`` from a
    docstring or comment block could be parsed under the previous
    heading and pollute the lookup.
    """
    from orchestrator.routes.event_prompt import _parse_per_producer_sha

    text = "- last_reviewed_commit_sha: ghost123\n"
    assert _parse_per_producer_sha(text) == {}


def test_parse_per_producer_sha_strips_backticks_from_heading() -> None:
    """A backtick-wrapped heading (``### `coder` ``) strips to ``coder``.

    The slice-1 writer's rendered shape allows the producer name to be
    wrapped in backticks for markdown emphasis; the parser must canonicalise.
    """
    from orchestrator.routes.event_prompt import _parse_per_producer_sha

    text = "### `coder`\n\n- last_reviewed_commit_sha: abc123\n"
    assert _parse_per_producer_sha(text) == {"coder": "abc123"}


def test_extract_nacks_falls_back_to_aggregated_nacks_key() -> None:
    """``_extract_nacks`` accepts ``aggregated_nacks`` as the third priority key.

    Priority order is documented in the implementation:
    ``nacks`` > ``unresolved_nacks`` > ``aggregated_nacks``. The
    coder's tests cover priority between the first two; this pins
    the third tier for forward-compat with a future next-action
    barrier-equivalent payload.
    """
    from orchestrator.routes.event_prompt import _extract_nacks

    payload = {"aggregated_nacks": [{"reviewer": "r1", "version": 2, "reason": "x"}]}
    out = _extract_nacks(payload)
    assert len(out) == 1
    assert out[0]["reviewer"] == "r1"


def test_extract_nacks_drops_non_dict_entries() -> None:
    """Non-dict entries inside the NACK list are dropped, not raised on."""
    from orchestrator.routes.event_prompt import _extract_nacks

    payload = {"nacks": [{"reviewer": "r1"}, "stringy", None, 42]}
    out = _extract_nacks(payload)
    assert out == [{"reviewer": "r1"}]


def test_extract_changed_artifacts_filters_none_and_whitespace() -> None:
    """``None`` and empty/whitespace entries are dropped; the rest stringified."""
    from orchestrator.routes.event_prompt import _extract_changed_artifacts

    payload = {"changed_artifacts": [None, "", "   ", "real.py", "  trim.py  "]}
    out = _extract_changed_artifacts(payload)
    # ``None`` and pure-whitespace entries are dropped. ``"  trim.py  "``
    # stringifies to itself (the strip() check is a truthiness gate, not
    # a normalisation step) — the strip-on-output is the caller's job.
    assert out == ["real.py", "  trim.py  "]


def test_extract_changed_artifacts_returns_empty_on_non_dict_payload() -> None:
    """A non-dict event payload yields an empty artifact list (defensive)."""
    from orchestrator.routes.event_prompt import _extract_changed_artifacts

    assert _extract_changed_artifacts("not a dict") == []
    assert _extract_changed_artifacts(None) == []
    assert _extract_changed_artifacts([1, 2]) == []


def test_extract_current_producers_skips_non_dict_pending_entries() -> None:
    """``pending_reviews`` walker is robust against mixed-shape entries.

    The next-action route's payload is canonical, but a future schema
    drift (e.g. a string entry for legacy compatibility) must not
    crash the wrapper. Non-dict entries are silently skipped.
    """
    from orchestrator.routes.event_prompt import _extract_current_producers

    payload = {
        "pending_reviews": [
            {"producer": "coder"},
            "stringy",  # non-dict — skipped
            None,
            {"producer_role": "documenter"},  # alternate key honoured
            {"producer": ""},  # empty string — skipped
            {"producer": 42},  # non-string — skipped
        ]
    }
    out = _extract_current_producers(payload)
    assert out == ["coder", "documenter"]


def test_extract_current_producers_returns_empty_on_non_dict_payload() -> None:
    """A non-dict event payload yields an empty list (defensive)."""
    from orchestrator.routes.event_prompt import _extract_current_producers

    assert _extract_current_producers("not a dict") == []
    assert _extract_current_producers(None) == []
    assert _extract_current_producers([1, 2]) == []


def test_extract_artifacts_for_producer_prefers_pending_reviews_over_top_level() -> None:
    """When both shapes are present, ``pending_reviews`` wins.

    The architect documented this priority order
    (reviewer_code_holistic v2 finding #1). The next-action route's
    enriched per-producer ``artifact_refs`` is the canonical source;
    the top-level ``changed_artifacts`` is a legacy / test-payload
    fallback only. This pins the priority direction.
    """
    from orchestrator.routes.event_prompt import _extract_artifacts_for_producer

    payload = {
        "producer": "coder",
        "changed_artifacts": ["legacy.py"],
        "pending_reviews": [
            {"producer": "coder", "artifact_refs": ["canonical.py"]},
        ],
    }
    assert _extract_artifacts_for_producer(payload, "coder") == ["canonical.py"]


def test_extract_artifacts_for_producer_empty_string_producer_returns_empty() -> None:
    """An empty / whitespace producer argument returns an empty list (defensive)."""
    from orchestrator.routes.event_prompt import _extract_artifacts_for_producer

    payload = {
        "pending_reviews": [
            {"producer": "coder", "artifact_refs": ["a.py"]},
        ],
    }
    assert _extract_artifacts_for_producer(payload, "") == []
    assert _extract_artifacts_for_producer(payload, "   ") == []


def test_run_git_log_timeout_returns_labelled_sentinel() -> None:
    """``subprocess.TimeoutExpired`` returns a sentinel string the agent can see."""
    import subprocess
    from pathlib import Path
    from unittest.mock import patch

    from orchestrator.routes.event_prompt import _run_git_log

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=60),
    ):
        out = _run_git_log("abc1234", "main", Path("/tmp"))
    assert "git log timed out" in out
    assert "abc1234..HEAD" in out


def test_run_git_log_nonzero_rc_returns_labelled_sentinel() -> None:
    """Non-zero rc from ``git log`` surfaces the stderr verbatim."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from orchestrator.routes.event_prompt import _run_git_log

    mock_result = MagicMock(returncode=128, stdout="", stderr="fatal: bad revision")
    with patch("subprocess.run", return_value=mock_result):
        out = _run_git_log("abc1234", "main", Path("/tmp"))
    assert "rc=128" in out
    assert "fatal: bad revision" in out


def test_run_git_log_truncates_oversized_output_with_explicit_marker() -> None:
    """A multi-megabyte ``git log`` output truncates at 256 KiB with a marker.

    Without this guard, a pathological refactor could push a single
    delta past Claude's context budget regardless of the cacheable-prefix
    bound. The marker tells the agent the cut happened so they can fetch
    the full delta if a thorough audit is required.
    """
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from orchestrator.routes.event_prompt import _GIT_LOG_DELTA_MAX_BYTES, _run_git_log

    big = "+" * (3 * _GIT_LOG_DELTA_MAX_BYTES)  # ~768 KiB
    mock_result = MagicMock(returncode=0, stdout=big, stderr="")
    with patch("subprocess.run", return_value=mock_result):
        out = _run_git_log("abc1234", "main", Path("/tmp"))
    assert "truncated — delta exceeded" in out
    # Output is bounded: 256 KiB + the truncation marker (~150 bytes).
    assert len(out.encode("utf-8")) < _GIT_LOG_DELTA_MAX_BYTES + 1024


def test_cli_event_payload_file_branch_reads_from_disk(tmp_path) -> None:
    """``--event-payload-file`` is the file-based alternative to stdin.

    The wrapper bash uses stdin in slice-2's wiring, but tests/operators
    can pass a file for repeatability. The branch isn't covered by the
    coder-authored CLI tests above (which all use stdin).
    """
    import io
    import json as _json
    import os
    import sys as _sys

    from orchestrator.routes import event_prompt

    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        _json.dumps({"action": "propose", "tasks": ["task-3-1", "task-3-2"]}),
        encoding="utf-8",
    )

    saved_env = {
        k: os.environ.get(k) for k in ("EGG_AGENT_ROLE", "EGG_REPO_PATH", "EGG_BRC_MEMORY")
    }
    saved_stdin = _sys.stdin
    saved_stdout = _sys.stdout
    out_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = "coder"
        os.environ["EGG_REPO_PATH"] = str(tmp_path)
        os.environ["EGG_BRC_MEMORY"] = "off"
        _sys.stdin = io.StringIO("")  # stdin ignored when --event-payload-file is set
        _sys.stdout = out_buf
        rc = event_prompt._cli(["propose", "--event-payload-file", str(payload_file)])
    finally:
        _sys.stdin = saved_stdin
        _sys.stdout = saved_stdout
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 0
    out = out_buf.getvalue()
    # Payload content surfaces in the rendered prompt's Payload (JSON) block.
    assert "task-3-1" in out
    assert "task-3-2" in out


def test_cli_event_payload_file_missing_returns_rc_two(tmp_path) -> None:
    """A missing ``--event-payload-file`` path returns rc=2 with an error message.

    The wrapper bash can detect rc=2 distinctly from rc=0 to surface a
    plumbing bug rather than silently rendering an empty prompt.
    """
    import io
    import os
    import sys as _sys

    from orchestrator.routes import event_prompt

    saved_env = {k: os.environ.get(k) for k in ("EGG_AGENT_ROLE",)}
    saved_stderr = _sys.stderr
    saved_stdin = _sys.stdin
    err_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = "coder"
        _sys.stdin = io.StringIO("")
        _sys.stderr = err_buf
        rc = event_prompt._cli(
            ["propose", "--event-payload-file", str(tmp_path / "does-not-exist.json")]
        )
    finally:
        _sys.stderr = saved_stderr
        _sys.stdin = saved_stdin
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 2
    assert "cannot read" in err_buf.getvalue()


def test_cli_malformed_json_stdin_falls_back_to_raw_payload(tmp_path) -> None:
    """Malformed-JSON stdin surfaces the raw payload under ``raw`` rather than crashing.

    The wrapper should never silently render an empty event; surfacing
    the raw bytes lets the agent (and the operator reading logs) see
    what was passed.
    """
    import io
    import os
    import sys as _sys

    from orchestrator.routes import event_prompt

    saved_env = {k: os.environ.get(k) for k in ("EGG_AGENT_ROLE", "EGG_REPO_PATH")}
    saved_stdin = _sys.stdin
    saved_stdout = _sys.stdout
    out_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = "coder"
        os.environ["EGG_REPO_PATH"] = str(tmp_path)
        _sys.stdin = io.StringIO("not valid {{ json")
        _sys.stdout = out_buf
        rc = event_prompt._cli(["propose"])
    finally:
        _sys.stdin = saved_stdin
        _sys.stdout = saved_stdout
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 0
    out = out_buf.getvalue()
    # The raw bytes survive under the "raw" key in the rendered payload.
    assert "not valid" in out


def test_cli_empty_stdin_falls_back_to_action_only_payload(tmp_path) -> None:
    """Empty stdin → ``{"action": <argv-action>}`` payload (no JSON parse).

    This is the bare-minimum invocation the wrapper falls back to when
    the orchestrator's next-action returns a verb but no rendered
    payload. The Action banner must still surface the verb.
    """
    import io
    import os
    import sys as _sys

    from orchestrator.routes import event_prompt

    saved_env = {k: os.environ.get(k) for k in ("EGG_AGENT_ROLE", "EGG_REPO_PATH")}
    saved_stdin = _sys.stdin
    saved_stdout = _sys.stdout
    out_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = "coder"
        os.environ["EGG_REPO_PATH"] = str(tmp_path)
        _sys.stdin = io.StringIO("")  # empty stdin
        _sys.stdout = out_buf
        rc = event_prompt._cli(["confirm"])
    finally:
        _sys.stdin = saved_stdin
        _sys.stdout = saved_stdout
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 0
    out = out_buf.getvalue()
    assert "Action: **confirm**" in out


# ---------------------------------------------------------------------------
# Task & operator directives section (#3123)
# ---------------------------------------------------------------------------


def test_task_section_rendered_when_task_description_provided() -> None:
    """The contract task statement is pushed into the per-event prompt."""
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose", "tasks": ["task-1-1"]},
        "",
        [],
        [],
        "main",
        task_description=(
            "## PRIOR SLICE-1 WORK: ADOPT, DO NOT REIMPLEMENT\n"
            "Merge origin egg/prior/slice-1 @ cfdca2b; do not rewrite."
        ),
    )

    assert "## Task & operator directives (contract ``task_description``)" in prompt
    assert "ADOPT, DO NOT REIMPLEMENT" in prompt
    # The directive framing makes bindingness explicit.
    assert "BINDING" in prompt
    # Placed right after the event section, before the role contract.
    event_idx = prompt.index("## Event")
    task_idx = prompt.index("## Task & operator directives")
    contract_idx = prompt.index("## What to do")
    assert event_idx < task_idx < contract_idx


def test_task_section_omitted_when_empty_or_default() -> None:
    """No task section without a task_description (GitHub-issue pipelines)."""
    for kwargs in ({}, {"task_description": ""}, {"task_description": "   \n"}):
        prompt = compose_event_prompt(
            "coder",
            {"action": "propose"},
            "",
            [],
            [],
            "main",
            **kwargs,
        )
        assert "## Task & operator directives" not in prompt


def test_task_section_truncates_at_cap_with_sentinel() -> None:
    """Oversized descriptions are cut with an explicit pointer to the contract."""
    from orchestrator.routes.event_prompt import TASK_DESCRIPTION_MAX_CHARS

    oversized = "directive " * (TASK_DESCRIPTION_MAX_CHARS // 5)
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        [],
        [],
        "main",
        task_description=oversized,
    )

    assert "task description truncated" in prompt
    assert "mcp__sdlc__show_contract" in prompt
    # The retained prefix is exactly the cap.
    task_idx = prompt.index("## Task & operator directives")
    assert "directive directive" in prompt[task_idx:]


def test_task_section_counts_toward_envelope_cap() -> None:
    """A capped task section + pathological NACKs still honour the 10 KB bound."""
    from orchestrator.routes.event_prompt import TASK_DESCRIPTION_MAX_CHARS

    nacks = [
        {
            "reviewer": f"reviewer_{i}",
            "version": 3,
            "reason": "blocker " * 600,
            "artifact_refs": [],
        }
        for i in range(6)
    ]
    prompt = compose_event_prompt(
        "coder",
        {"action": "propose"},
        "",
        nacks,
        [],
        "main",
        task_description="x" * TASK_DESCRIPTION_MAX_CHARS,
    )

    envelope = _strip_git_log_blocks(prompt)
    assert len(envelope.encode("utf-8")) <= PROMPT_ENVELOPE_MAX_BYTES + 1024, (
        "envelope must stay near the cap with the task section included"
    )
    # The NACKs section (the designated variable-size driver) was the
    # part that got truncated — the task section survives whole.
    assert "NACK list truncated" in prompt
    assert "task description truncated" not in prompt


def test_read_task_description_from_pipeline_id_contract(tmp_path) -> None:
    """``EGG_PIPELINE_ID`` resolves the worktree contract file."""
    import json as _json
    import os

    from orchestrator.routes.event_prompt import _read_task_description

    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "pipeline-abc123.json").write_text(
        _json.dumps({"task_description": "Adopt the prior branch.  "}),
        encoding="utf-8",
    )

    saved = {k: os.environ.get(k) for k in ("EGG_PIPELINE_ID", "EGG_ISSUE_NUMBER")}
    try:
        os.environ["EGG_PIPELINE_ID"] = "pipeline-abc123"
        os.environ.pop("EGG_ISSUE_NUMBER", None)
        assert _read_task_description(tmp_path) == "Adopt the prior branch."
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_read_task_description_issue_number_fallback(tmp_path) -> None:
    """``EGG_ISSUE_NUMBER`` falls back to the ``issue-<N>.json`` key."""
    import json as _json
    import os

    from orchestrator.routes.event_prompt import _read_task_description

    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "issue-42.json").write_text(
        _json.dumps({"task_description": "from issue contract"}),
        encoding="utf-8",
    )

    saved = {k: os.environ.get(k) for k in ("EGG_PIPELINE_ID", "EGG_ISSUE_NUMBER")}
    try:
        # Pipeline id points at a MISSING file; the issue key must win.
        os.environ["EGG_PIPELINE_ID"] = "pipeline-missing"
        os.environ["EGG_ISSUE_NUMBER"] = "42"
        assert _read_task_description(tmp_path) == "from issue contract"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_read_task_description_fail_soft(tmp_path) -> None:
    """Missing dir, malformed JSON, absent/None field → '' (never raises)."""
    import os

    from orchestrator.routes.event_prompt import _read_task_description

    saved = {k: os.environ.get(k) for k in ("EGG_PIPELINE_ID", "EGG_ISSUE_NUMBER")}
    try:
        os.environ["EGG_PIPELINE_ID"] = "pipeline-x"
        os.environ.pop("EGG_ISSUE_NUMBER", None)

        # No contracts dir at all.
        assert _read_task_description(tmp_path) == ""

        contracts = tmp_path / ".egg-state" / "contracts"
        contracts.mkdir(parents=True)
        target = contracts / "pipeline-x.json"

        # Malformed JSON.
        target.write_text("{not json", encoding="utf-8")
        assert _read_task_description(tmp_path) == ""

        # Valid JSON, field absent / null (GitHub-issue contracts).
        target.write_text('{"pipeline_id": "pipeline-x"}', encoding="utf-8")
        assert _read_task_description(tmp_path) == ""
        target.write_text('{"task_description": null}', encoding="utf-8")
        assert _read_task_description(tmp_path) == ""

        # No identifier env at all.
        os.environ.pop("EGG_PIPELINE_ID", None)
        target.write_text('{"task_description": "text"}', encoding="utf-8")
        assert _read_task_description(tmp_path) == ""
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_read_task_description_issue_anchor_fallback(tmp_path) -> None:
    """A pre-#3163 issue contract (``task_description: null`` but ``issue``
    identity present) yields a synthesized anchor, so the binding task
    section is never silently empty for issue-backed pipelines.
    """
    import json as _json
    import os

    from orchestrator.routes.event_prompt import _read_task_description

    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "issue-3064.json").write_text(
        _json.dumps(
            {
                "task_description": None,
                "issue": {
                    "number": 3064,
                    "title": "Issue #3064",
                    "url": "https://github.com/owner/repo/issues/3064",
                },
            }
        ),
        encoding="utf-8",
    )

    saved = {k: os.environ.get(k) for k in ("EGG_PIPELINE_ID", "EGG_ISSUE_NUMBER")}
    try:
        os.environ["EGG_PIPELINE_ID"] = "issue-3064"
        os.environ.pop("EGG_ISSUE_NUMBER", None)
        anchor = _read_task_description(tmp_path)
        assert "GitHub issue #3064" in anchor
        assert "https://github.com/owner/repo/issues/3064" in anchor
        assert "gh issue view 3064" in anchor
        # The placeholder title is not echoed as if it were a real title.
        assert "(Issue #3064)" not in anchor
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_cli_renders_task_section_from_worktree_contract(tmp_path) -> None:
    """End-to-end: the CLI reads the contract file and renders the section."""
    import io
    import json as _json
    import os
    import sys as _sys

    from orchestrator.routes import event_prompt

    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "pipeline-e2e.json").write_text(
        _json.dumps({"task_description": "PRIOR WORK: ADOPT, DO NOT REIMPLEMENT"}),
        encoding="utf-8",
    )

    saved_env = {
        k: os.environ.get(k)
        for k in ("EGG_AGENT_ROLE", "EGG_REPO_PATH", "EGG_PIPELINE_ID", "EGG_ISSUE_NUMBER")
    }
    saved_stdin = _sys.stdin
    saved_stdout = _sys.stdout
    out_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = "coder"
        os.environ["EGG_REPO_PATH"] = str(tmp_path)
        os.environ["EGG_PIPELINE_ID"] = "pipeline-e2e"
        os.environ.pop("EGG_ISSUE_NUMBER", None)
        _sys.stdin = io.StringIO('{"action": "propose"}')
        _sys.stdout = out_buf
        rc = event_prompt._cli(["propose"])
    finally:
        _sys.stdin = saved_stdin
        _sys.stdout = saved_stdout
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert rc == 0
    out = out_buf.getvalue()
    assert "## Task & operator directives" in out
    assert "PRIOR WORK: ADOPT, DO NOT REIMPLEMENT" in out
