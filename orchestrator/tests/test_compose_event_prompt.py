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
    (mem_dir / "brc-memory.md").write_text("\n".join(body_parts), encoding="utf-8")
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
        for k in ("EGG_AGENT_ROLE", "EGG_BASE_BRANCH", "EGG_REPO_PATH", "EGG_BRC_MEMORY")
    }
    saved_stdin = _sys.stdin
    saved_stdout = _sys.stdout
    out_buf = io.StringIO()
    try:
        os.environ["EGG_AGENT_ROLE"] = role
        os.environ["EGG_BASE_BRANCH"] = "main"
        os.environ["EGG_REPO_PATH"] = str(repo)
        os.environ["EGG_BRC_MEMORY"] = memory_mode
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
    """``EGG_BRC_MEMORY=write-only`` (slice-1 default) omits the memory
    excerpt but STILL emits the per-producer git-log delta against the
    memory file's stored SHAs as a fallback baseline. The plan
    TASK-3-2 wording is verbatim: "with EGG_BRC_MEMORY=write-only
    (slice-1 default), the prompt omits memory but still emits the
    git-log delta against the orchestrator's signal-level
    changed_artifacts as a fallback baseline".
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
