"""Always-on prompt-text asserts for the ``reviewer_code`` subagent fan-out block.

Covers TASK-4-3 of issue #1965. The ``_build_review_prompt`` function
must emit a "Subagent Fan-Out Strategy" section ONLY for
``reviewer_type="code"`` AND ``phase="implement"``. The block carries
several literal markers that — taken together — verify the gate logic,
the partition-fetch instruction with both fallbacks, the parallelism
knob, the recursion ban, the cross-partition consistency pass, and the
gate-decision STATUS heartbeat instrumentation.

These asserts are deterministic — they do not run the LLM. They fire
with a clear message if any of the following drift:

- The fan-out block is removed.
- The threshold values (10 files / 500 LOC) drift.
- The 6-subagent cap or the 5-minute / 300-second timeout drops out.
- The recursion ban disappears.
- The parent cross-partition pass disappears.
- The STATUS-heartbeat instrumentation disappears.
- The mcp-unavailable fallback disappears.
- The block leaks to non-code reviewers or to non-implement phases.
- The ``reviewer_code_parallel`` kwarg stops being honoured.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Stub Docker the same way test_pipeline_prompts.py does.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import _build_review_prompt  # noqa: E402

# ---------------------------------------------------------------------------
# Block presence — only on (reviewer_type='code', phase='implement').
# ---------------------------------------------------------------------------


class TestFanOutBlockPresence:
    def test_present_for_code_reviewer_in_implement_phase(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "Subagent Fan-Out Strategy" in prompt

    @pytest.mark.parametrize(
        "reviewer_type",
        ["contract", "agent-design", "refine", "plan", "code-holistic"],
    )
    def test_absent_for_non_code_reviewer_types(self, reviewer_type: str) -> None:
        # Each non-code type uses an appropriate phase for that reviewer.
        phase_for_type = {
            "contract": "implement",
            "agent-design": "refine",
            "refine": "refine",
            "plan": "plan",
            # code-holistic runs in implement alongside reviewer_code but
            # MUST NOT receive the fan-out block — it always single-passes
            # the full diff (issue #2126).
            "code-holistic": "implement",
        }[reviewer_type]
        prompt = _build_review_prompt(
            phase=phase_for_type,
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type=reviewer_type,
            issue_number=100,
        )
        assert "Subagent Fan-Out Strategy" not in prompt, (
            f"reviewer_type={reviewer_type!r} should not include the "
            "subagent fan-out block — it's exclusive to reviewer_type='code'."
        )

    def test_absent_for_code_reviewer_in_plan_phase(self) -> None:
        """The block is gated on phase='implement' even for 'code' reviewers."""
        prompt = _build_review_prompt(
            phase="plan",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "Subagent Fan-Out Strategy" not in prompt, (
            "Fan-out is implement-phase only; plan-phase reviewer_code must not see the block."
        )


# ---------------------------------------------------------------------------
# Threshold + numstat instrumentation.
# ---------------------------------------------------------------------------


class TestFanOutThresholdInstrumentation:
    def setup_method(self) -> None:
        self.prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )

    def test_numstat_command_mentioned(self) -> None:
        assert "git diff --numstat" in self.prompt

    def test_files_threshold_present(self) -> None:
        # The threshold rule is `files_changed > 10` (or any equivalent
        # phrasing) — the literal '10' must appear next to 'files'.
        assert (
            "files_changed > 10" in self.prompt
            or "files > 10" in self.prompt
            or "10 files" in self.prompt
            or "files_changed > 10 OR" in self.prompt
        )

    def test_loc_threshold_present(self) -> None:
        # The plan picks 500 as the LOC threshold; either form is fine
        # provided the literal '500' appears in the threshold context.
        assert "500" in self.prompt

    def test_status_heartbeat_enabled_marker(self) -> None:
        assert "fan-out: enabled" in self.prompt

    def test_status_heartbeat_skipped_marker(self) -> None:
        assert "fan-out: skipped" in self.prompt


# ---------------------------------------------------------------------------
# Partition-list fetch + fallbacks.
# ---------------------------------------------------------------------------


class TestPartitionFetchAndFallbacks:
    def setup_method(self) -> None:
        self.prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )

    def test_mcp_show_contract_fetch_instruction(self) -> None:
        assert "mcp__sdlc__show_contract" in self.prompt

    def test_implement_tasks_path_mentioned(self) -> None:
        assert "phases.implement.tasks" in self.prompt

    def test_empty_task_list_fallback_mentioned(self) -> None:
        # The prompt must explicitly tell the reviewer how to fall back
        # when phases.implement.tasks is empty (custom-phase invocation,
        # contractless babysit_pr).
        prompt_lower = self.prompt.lower()
        assert (
            "no implement tasks" in prompt_lower
            or "empty implement-phase task" in prompt_lower
            or "empty task list" in prompt_lower
        ), (
            "Fan-out block must describe the empty-task-list fallback. "
            "Look for 'no implement tasks', 'empty implement-phase task', "
            "or similar."
        )

    def test_mcp_unavailable_fallback_mentioned(self) -> None:
        prompt_lower = self.prompt.lower()
        assert (
            "mcp unavailable" in prompt_lower
            or "fallback to single-pass" in prompt_lower
            or "parent fetches" in prompt_lower
        ), (
            "Fan-out block must describe the mcp-unavailable fallback "
            "(parent fetches contract OR fall back to single-pass)."
        )


# ---------------------------------------------------------------------------
# Subagent cap + per-subagent timeout.
# ---------------------------------------------------------------------------


class TestSubagentCapAndTimeout:
    def setup_method(self) -> None:
        self.prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )

    def test_subagent_cap_mentioned(self) -> None:
        assert ("capped at 6" in self.prompt) or (
            "never spawn more than 6 subagents" in self.prompt
        ), (
            "Fan-out block must cap subagents at 6 — look for 'capped at "
            "6' or 'never spawn more than 6 subagents'."
        )

    def test_per_subagent_timeout_mentioned(self) -> None:
        assert ("5 minutes" in self.prompt) or ("300 seconds" in self.prompt), (
            "Fan-out block must specify a 5-minute / 300-second per-subagent wall-clock cap."
        )


# ---------------------------------------------------------------------------
# Recursion ban.
# ---------------------------------------------------------------------------


class TestRecursionBan:
    def test_recursion_ban_literal(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "subagents must NOT spawn their own subagents" in prompt, (
            "Fan-out block must contain the literal recursion ban "
            "'subagents must NOT spawn their own subagents'."
        )


# ---------------------------------------------------------------------------
# Parent cross-partition consistency pass.
# ---------------------------------------------------------------------------


class TestParentCrossPartitionPass:
    def setup_method(self) -> None:
        self.prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )

    def test_cross_partition_marker(self) -> None:
        assert "cross-partition" in self.prompt.lower()

    def test_handler_marker(self) -> None:
        # The PR #1964 motivating example: handler ↔ allowlist.
        assert "handler" in self.prompt.lower()

    def test_allowlist_marker(self) -> None:
        assert "allowlist" in self.prompt.lower()


# ---------------------------------------------------------------------------
# reviewer_code_parallel kwarg.
# ---------------------------------------------------------------------------


class TestReviewerCodeParallelKwarg:
    def test_default_says_in_parallel(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "in parallel" in prompt.lower()

    def test_explicit_true_says_in_parallel(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            reviewer_code_parallel=True,
        )
        assert "in parallel" in prompt.lower()

    def test_explicit_false_says_sequentially(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            reviewer_code_parallel=False,
        )
        prompt_lower = prompt.lower()
        assert "sequentially" in prompt_lower
        # And the parallel-only language should not be the dominant
        # instruction; we tolerate the word "parallel" appearing in
        # surrounding prose but the explicit ordering directive should
        # use 'sequentially'.

    def test_kwarg_accepted_signature(self) -> None:
        """The kwarg must be optional, default True, and not raise."""
        # Pass a few permutations to make sure the signature accepts the
        # kwarg without TypeErrors.
        for value in (True, False):
            _build_review_prompt(
                phase="implement",
                pipeline_id="test-pipe",
                pipeline_mode="issue",
                reviewer_type="code",
                issue_number=100,
                reviewer_code_parallel=value,
            )
