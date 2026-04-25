"""Tests for the overseer ``gh issue create`` gateway guardrail.

Covers ``gateway.agent_restrictions.check_overseer_gh_issue_create``
(issue #1962, TASK-2-2) — repo enforcement, label injection, size
limits, and defense-in-depth secret-pattern rejection.
"""

from __future__ import annotations

import pytest
from agent_restrictions import (
    OVERSEER_FILE_ISSUE_BODY_MAX_BYTES,
    OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS,
    OVERSEER_REQUIRED_LABEL,
    OVERSEER_VALID_PRIORITY_LABELS,
    OverseerGhCheckResult,
    check_overseer_gh_issue_create,
)

_GH_PAT = "ghp_" + "A" * 36


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------


class TestRoleGating:
    def test_non_overseer_role_rejected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="coder",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="b",
        )
        assert result.allowed is False
        assert "only the overseer role" in result.reason

    def test_empty_role_rejected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=[],
            title="t",
            body="b",
        )
        assert result.allowed is False

    def test_role_case_insensitive(self) -> None:
        result = check_overseer_gh_issue_create(
            role="OVERSEER",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="b",
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# Repo enforcement
# ---------------------------------------------------------------------------


class TestRepoEnforcement:
    def test_cross_repo_rejected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="other/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="b",
        )
        assert result.allowed is False
        assert "cross-repo filing rejected" in result.reason

    def test_dev_shell_no_pipeline_repo_skipped(self) -> None:
        # When pipeline_repo is None (dev shell mode), the check skips
        # the cross-repo guard.
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="any/repo",
            pipeline_repo=None,
            labels=["agent:overseer", "p1"],
            title="t",
            body="b",
        )
        assert result.allowed is True

    def test_matching_repo_passes(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="b",
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------


class TestSizeLimits:
    def test_title_at_limit_passes(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="x" * OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS,
            body="body",
        )
        assert result.allowed is True

    def test_title_over_limit_rejected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="x" * (OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS + 1),
            body="body",
        )
        assert result.allowed is False
        assert "title exceeds" in result.reason

    def test_body_at_limit_passes(self) -> None:
        body_at_limit = "x" * OVERSEER_FILE_ISSUE_BODY_MAX_BYTES
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body=body_at_limit,
        )
        assert result.allowed is True

    def test_body_over_limit_rejected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="x" * (OVERSEER_FILE_ISSUE_BODY_MAX_BYTES + 1),
        )
        assert result.allowed is False
        assert "body exceeds" in result.reason

    def test_body_size_uses_utf8_byte_count(self) -> None:
        # Multi-byte chars should count as bytes, not codepoints.
        # 51000 codepoints of "€" (3 bytes each) = 153_000 bytes => over 50KB.
        body = "€" * 17_000  # 51_000 bytes
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body=body,
        )
        assert result.allowed is False


# ---------------------------------------------------------------------------
# Secret-pattern defense-in-depth
# ---------------------------------------------------------------------------


class TestSecretRejection:
    def test_body_with_gh_pat_rejected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body=f"Token leaked: {_GH_PAT}",
        )
        assert result.allowed is False
        assert "secret patterns" in result.reason
        assert "gh-pat" in result.secret_kinds

    def test_body_with_aws_key_rejected(self) -> None:
        akia = "AKIA" + "B" * 16
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body=f"AWS: {akia}",
        )
        assert result.allowed is False
        assert "aws-key" in result.secret_kinds

    def test_body_clean_passes(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="No secrets here.",
        )
        assert result.allowed is True
        assert result.secret_kinds == ()


# ---------------------------------------------------------------------------
# Label auto-injection
# ---------------------------------------------------------------------------


class TestLabelInjection:
    def test_caller_passes_both_labels_no_injection(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer", "p1"],
            title="t",
            body="b",
        )
        assert result.allowed is True
        assert result.injected_labels == ()

    def test_missing_overseer_label_injected(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["p1"],
            title="t",
            body="b",
        )
        assert result.allowed is True
        assert OVERSEER_REQUIRED_LABEL in result.injected_labels

    def test_missing_priority_label_injects_p2_default(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["agent:overseer"],
            title="t",
            body="b",
        )
        assert result.allowed is True
        assert "p2" in result.injected_labels

    def test_no_labels_injects_both(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=[],
            title="t",
            body="b",
        )
        assert result.allowed is True
        assert OVERSEER_REQUIRED_LABEL in result.injected_labels
        assert "p2" in result.injected_labels

    def test_label_match_is_case_insensitive(self) -> None:
        result = check_overseer_gh_issue_create(
            role="overseer",
            repo="owner/repo",
            pipeline_repo="owner/repo",
            labels=["AGENT:OVERSEER", "P1"],
            title="t",
            body="b",
        )
        assert result.allowed is True
        # Both lower-case lookups should already be present, no injection.
        assert result.injected_labels == ()


class TestExportedConstants:
    def test_required_label_exported(self) -> None:
        assert OVERSEER_REQUIRED_LABEL == "agent:overseer"

    def test_priority_labels_set(self) -> None:
        assert OVERSEER_VALID_PRIORITY_LABELS == frozenset({"p0", "p1", "p2", "p3"})

    def test_size_limits_match_cli_constants(self) -> None:
        # Symmetric limits with the sandbox CLI per TASK-2-2.
        assert OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS == 120
        assert OVERSEER_FILE_ISSUE_BODY_MAX_BYTES == 50_000

    def test_result_is_frozen_dataclass(self) -> None:
        result = OverseerGhCheckResult(
            allowed=True,
            reason="ok",
            injected_labels=("agent:overseer",),
            secret_kinds=(),
        )
        with pytest.raises((AttributeError, Exception)):
            result.allowed = False  # type: ignore[misc]
