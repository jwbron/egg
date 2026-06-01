"""Unit tests for ``compose_event_prompt`` (TASK-3-1 / TASK-3-6 of #2908 slice-3).

The composer assembles the **single-event** prompt the deterministic
event-pump wrapper invokes a BRC agent with. Per ``task-3-1`` of the
slice-3 contract, the helper signature is::

    compose_event_prompt(
        role,
        event_payload,
        memory_excerpt,
        nacks,
        git_log_delta,
        base_branch,
    ) -> str

Output shape (architect od-6 Option B — memory delivered inline at
the *tail* of the prompt):

  1. role banner (one line)
  2. one-line event description
  3. NACK payload from
     ``orchestrator/peer_consensus.py::_open_nacks_barrier_response``
     ``nacks[]`` rendered per-reviewer with ``reason`` +
     ``artifact_refs``
  4. per-producer
     ``git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p``
     delta — emitted **verbatim** (NOT collapsed to
     ``changed_artifacts`` — risk_analyst R6 +
     ``shared/prompts/REVIEWER-SYNC.md``)
  5. memory excerpt (≤ 2 KB; ``compose_event_prompt`` is responsible for
     truncating anything larger than 2 KB) appended at tail

Envelope rule (task-3-6 acceptance criterion):

  total prompt envelope ≤ 10 KB **excluding** the git-log delta —
  the git-log delta scales with the change size and is intentionally
  NOT counted against the envelope. The tests below enforce this by
  computing ``len(prompt) - len(git_log_delta)`` and asserting it
  is ≤ 10 KB.

These tests fail (with a clean ``ImportError``) until the coder lands
TASK-3-1; that is by design — the tester scaffolds the regression
gate before the coder ships per the slice-3 ordering. When tests
start failing on assertions rather than import, the coder is
expected to align the helper output with the contract above.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies that orchestrator.routes.pipelines imports at
# module level so the helper itself can be imported without Docker, etc.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

# Make ``orchestrator/`` and ``shared/`` importable so we can resolve
# ``routes.pipelines`` the same way the rest of the test suite does.
_project_root = Path(__file__).parent.parent.parent
for _p in (_project_root / "orchestrator", _project_root / "shared"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Import target. ``compose_event_prompt`` lands in
# ``orchestrator/routes/pipelines.py`` per the contract's
# ``files_affected`` for TASK-3-1; the coder may push it to a sibling
# module, in which case re-exporting from ``routes.pipelines`` keeps
# this import path stable.
from routes.pipelines import compose_event_prompt  # noqa: E402

# Envelope ceiling — surrounding prose only, NOT the git-log delta.
PROMPT_ENVELOPE_CEILING_BYTES = 10 * 1024
# Memory excerpt cap — composer must truncate excerpts larger than this.
MEMORY_EXCERPT_CAP_BYTES = 2 * 1024

# Representative producer / reviewer payloads. Kept minimal-but-realistic;
# the composer is *not* responsible for filling in defaults.
SAMPLE_EVENT_PAYLOAD_PROPOSE = {
    "message_type": "CONSENSUS_PROPOSE",
    "from_role": "coder",
    "version": 1,
    "summary": "Implement compose_event_prompt helper",
    "artifacts": ["orchestrator/routes/pipelines.py"],
    "commit_sha": "abc1234",
}

SAMPLE_EVENT_PAYLOAD_ACK = {
    "message_type": "CONSENSUS_ACK",
    "from_role": "reviewer_code",
    "version": 1,
    "producer_role": "coder",
}

SAMPLE_EVENT_PAYLOAD_NACK = {
    "message_type": "CONSENSUS_NACK",
    "from_role": "reviewer_code",
    "version": 1,
    "producer_role": "coder",
    "reason": "Missing happy-path test",
}

# A representative git-log delta. The composer must NOT count this
# against the 10 KB envelope, so we deliberately use a large block.
SAMPLE_GIT_LOG_DELTA = (
    "commit abc1234 (HEAD -> egg/issue-2908-impl2/slice-3)\n"
    "Author: Coder <coder@example.com>\n"
    "    Add compose_event_prompt helper.\n"
    "\n"
    "diff --git a/orchestrator/routes/pipelines.py "
    "b/orchestrator/routes/pipelines.py\n"
    "@@ -123,0 +124,42 @@\n" + ("+    body = body + content_line\n" * 200)
)


def _envelope_bytes(prompt: str, git_log_delta: str) -> int:
    """Return prompt size *excluding* the git-log delta (envelope rule).

    The git-log delta is emitted verbatim in the prompt; we subtract it
    when checking the envelope ceiling per task-3-6 acceptance criteria.
    """
    assert git_log_delta in prompt, (
        "compose_event_prompt must emit the git-log delta verbatim; "
        "the test cannot bound the envelope if the delta is mutated."
    )
    return len(prompt) - len(git_log_delta)


# ---------------------------------------------------------------------------
# Per-role prompt shape
# ---------------------------------------------------------------------------


class TestProducerPromptShape:
    """compose_event_prompt for a producer role (coder)."""

    def test_includes_role_banner(self):
        prompt = compose_event_prompt(
            role="coder",
            event_payload=SAMPLE_EVENT_PAYLOAD_ACK,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        # Role banner must name the role unambiguously so the model knows
        # which lifecycle subset to apply.
        assert "coder" in prompt.lower()

    def test_envelope_under_ceiling(self):
        prompt = compose_event_prompt(
            role="coder",
            event_payload=SAMPLE_EVENT_PAYLOAD_ACK,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        envelope = _envelope_bytes(prompt, SAMPLE_GIT_LOG_DELTA)
        assert envelope <= PROMPT_ENVELOPE_CEILING_BYTES, (
            f"producer envelope {envelope} > {PROMPT_ENVELOPE_CEILING_BYTES} "
            "(excluding git-log delta — the delta scales with change "
            "size and is exempt from the 10 KB budget per task-3-6)"
        )

    def test_includes_event_one_line_description(self):
        prompt = compose_event_prompt(
            role="coder",
            event_payload=SAMPLE_EVENT_PAYLOAD_ACK,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        # Event identity surfaces as the one-line description; the
        # message_type and the source role are the minimum useful info.
        assert "CONSENSUS_ACK" in prompt
        assert "reviewer_code" in prompt


class TestReviewerPromptShape:
    """compose_event_prompt for a reviewer role."""

    def test_includes_role_banner(self):
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        assert "reviewer_code" in prompt.lower()

    def test_envelope_under_ceiling(self):
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        envelope = _envelope_bytes(prompt, SAMPLE_GIT_LOG_DELTA)
        assert envelope <= PROMPT_ENVELOPE_CEILING_BYTES, (
            f"reviewer envelope {envelope} > {PROMPT_ENVELOPE_CEILING_BYTES}"
        )

    def test_event_describes_producer_proposal(self):
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        # Reviewer must see *what* is being proposed and *who* proposed.
        assert "CONSENSUS_PROPOSE" in prompt
        assert "coder" in prompt.lower()


class TestDualRolePromptShape:
    """compose_event_prompt for a dual-role agent (tester)."""

    def test_includes_role_banner(self):
        prompt = compose_event_prompt(
            role="tester",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        assert "tester" in prompt.lower()

    def test_envelope_under_ceiling(self):
        prompt = compose_event_prompt(
            role="tester",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        envelope = _envelope_bytes(prompt, SAMPLE_GIT_LOG_DELTA)
        assert envelope <= PROMPT_ENVELOPE_CEILING_BYTES, (
            f"dual-role envelope {envelope} > {PROMPT_ENVELOPE_CEILING_BYTES}"
        )


# ---------------------------------------------------------------------------
# Memory excerpt truncation
# ---------------------------------------------------------------------------


class TestMemoryExcerptTruncation:
    """The composer truncates ``memory_excerpt`` to ≤ 2 KB."""

    def test_oversized_memory_excerpt_is_truncated(self):
        # 4 KB of memory content — twice the cap. The composer should
        # surface only a 2 KB slice (or smaller).
        oversized = "X" * (4 * 1024)
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt=oversized,
            nacks=[],
            git_log_delta="",
            base_branch="main",
        )
        # Count the X-only memory region embedded in the prompt — we
        # don't require a specific delimiter; the assertion is that no
        # X-run exceeds the 2 KB cap.
        max_run = 0
        run = 0
        for ch in prompt:
            if ch == "X":
                run += 1
                if run > max_run:
                    max_run = run
            else:
                run = 0
        assert max_run <= MEMORY_EXCERPT_CAP_BYTES, (
            f"memory excerpt run of {max_run} bytes exceeds the 2 KB cap "
            f"({MEMORY_EXCERPT_CAP_BYTES} bytes); composer must truncate"
        )

    def test_undersized_memory_excerpt_passes_through_intact(self):
        # 1 KB of memory content — comfortably under the cap. Should be
        # surfaced intact so the agent gets the full distilled context.
        small = "A" * 1024
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt=small,
            nacks=[],
            git_log_delta="",
            base_branch="main",
        )
        assert small in prompt, "memory excerpts under the 2 KB cap must pass through intact"

    def test_empty_memory_excerpt_is_handled(self):
        # An empty memory excerpt corresponds to the
        # ``EGG_BRC_MEMORY=write-only`` slice-1 default; the composer
        # must not crash and must still emit a useful prompt.
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0


# ---------------------------------------------------------------------------
# NACK payload rendering
# ---------------------------------------------------------------------------


class TestNackPayloadRendering:
    """NACK delta with 0 / 1 / 2+ reviewers."""

    def test_zero_nacks_omits_nack_section(self):
        prompt = compose_event_prompt(
            role="coder",
            event_payload=SAMPLE_EVENT_PAYLOAD_ACK,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        # With zero NACKs the composer must not emit a phantom
        # "Reviewer: ..." block — that would mislead the agent into
        # responding to a non-existent objection.
        assert "Reviewer: reviewer_code" not in prompt
        assert "Reviewer: reviewer_concurrency" not in prompt

    def test_single_nack_renders_with_reason_and_artifacts(self):
        nacks = [
            {
                "reviewer": "reviewer_code",
                "version": 2,
                "reason": "API change without docstring update",
                "artifact_refs": ["orchestrator/routes/pipelines.py"],
                "timestamp": "2026-06-01T12:00:00Z",
            },
        ]
        prompt = compose_event_prompt(
            role="coder",
            event_payload=SAMPLE_EVENT_PAYLOAD_NACK,
            memory_excerpt="",
            nacks=nacks,
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        assert "reviewer_code" in prompt
        assert "API change without docstring update" in prompt
        assert "orchestrator/routes/pipelines.py" in prompt

    def test_two_nacks_both_render_per_reviewer(self):
        nacks = [
            {
                "reviewer": "reviewer_code",
                "version": 2,
                "reason": "API change without docstring update",
                "artifact_refs": ["orchestrator/routes/pipelines.py"],
                "timestamp": "2026-06-01T12:00:00Z",
            },
            {
                "reviewer": "reviewer_security",
                "version": 2,
                "reason": "Missing input validation on memory_excerpt",
                "artifact_refs": ["orchestrator/routes/pipelines.py"],
                "timestamp": "2026-06-01T12:01:00Z",
            },
        ]
        prompt = compose_event_prompt(
            role="coder",
            event_payload=SAMPLE_EVENT_PAYLOAD_NACK,
            memory_excerpt="",
            nacks=nacks,
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        # Per-reviewer renderings must both be present — aggregation
        # is enforced by the orchestrator (#2142); composer must NOT
        # squash multi-reviewer NACK sets to a single rollup.
        assert "reviewer_code" in prompt
        assert "reviewer_security" in prompt
        assert "API change without docstring update" in prompt
        assert "Missing input validation on memory_excerpt" in prompt


# ---------------------------------------------------------------------------
# git-log delta — verbatim emission with per-producer SHA substitution
# ---------------------------------------------------------------------------


class TestGitLogDeltaCommand:
    """The composer must NOT shortcut to ``changed_artifacts``.

    Per task-3-1 (citing risk_analyst R6 +
    ``shared/prompts/REVIEWER-SYNC.md``) the re-review must audit the
    *full* delta as a fresh review or the stateless pump
    systematically weakens adversarial re-review. These tests guard
    against a regression where the composer reduces the delta to the
    orchestrator-side ``changed_artifacts`` list.
    """

    def test_git_log_delta_emitted_verbatim(self):
        delta = "commit deadbeef1234\ndiff --git a/file.py b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=delta,
            base_branch="main",
        )
        assert delta in prompt, (
            "git-log delta must be emitted verbatim — collapsing it to "
            "``changed_artifacts`` would silently weaken adversarial "
            "re-review per task-3-1 / risk_analyst R6"
        )

    def test_git_log_delta_command_string_present(self):
        """The composer renders the *command* the wrapper executed so
        the agent can re-run / extend it if needed.

        The command must use the per-producer ``last_reviewed_commit_sha``
        substituted in — NOT the orchestrator-side ``changed_artifacts``
        shortcut.
        """
        delta = (
            "$ git log abc1234..HEAD --not origin/main -p\n"
            "commit deadbeef1234\n"
            "diff --git a/file.py b/file.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=delta,
            base_branch="main",
        )
        # Verbatim verification of the literal command — if anyone
        # rewrites the composer to use ``changed_artifacts`` only, this
        # assertion fires.
        assert "git log abc1234..HEAD --not origin/main -p" in prompt

    def test_no_changed_artifacts_shortcut(self):
        """Regression guard: composer MUST NOT replace the git-log delta
        with a ``changed_artifacts:`` rollup. risk_analyst R6 +
        ``shared/prompts/REVIEWER-SYNC.md`` require the full delta.
        """
        prompt = compose_event_prompt(
            role="reviewer_code",
            event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
            memory_excerpt="",
            nacks=[],
            git_log_delta=SAMPLE_GIT_LOG_DELTA,
            base_branch="main",
        )
        # A shortcut implementation would surface only artifact paths and
        # omit the actual diff lines. The fixture delta has 200 "+"
        # additions; the prompt must preserve them.
        assert prompt.count("+    body = body + content_line\n") >= 100, (
            "git-log delta diff lines truncated to ``changed_artifacts`` "
            "rollup — see risk_analyst R6 + REVIEWER-SYNC.md"
        )


# ---------------------------------------------------------------------------
# Adversarial: total envelope across all roles
# ---------------------------------------------------------------------------


class TestEnvelopeAcrossRoles:
    """Cross-role envelope check — a single regression should fire if
    any role's prompt prose blows past the 10 KB budget.
    """

    def test_every_role_under_envelope_ceiling(self):
        offenders: list[tuple[str, int]] = []
        for role in (
            "coder",
            "tester",
            "documenter",
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_concurrency",
            "reviewer_security",
            "reviewer_contract",
        ):
            prompt = compose_event_prompt(
                role=role,
                event_payload=SAMPLE_EVENT_PAYLOAD_PROPOSE,
                memory_excerpt="",
                nacks=[],
                git_log_delta=SAMPLE_GIT_LOG_DELTA,
                base_branch="main",
            )
            envelope = _envelope_bytes(prompt, SAMPLE_GIT_LOG_DELTA)
            if envelope > PROMPT_ENVELOPE_CEILING_BYTES:
                offenders.append((role, envelope))
        assert not offenders, (
            f"Roles exceeding the 10 KB envelope (excluding git-log delta): {offenders}"
        )
