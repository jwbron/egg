"""Tests for ``_brc_history_identifier`` babysit and CUSTOM+PR behaviour.

These tests verify that babysit-pr and CUSTOM+PR pipelines get a
``pr-{pr}-{short_sha}`` namespace for BRC-history artifacts, while
issue-mode pipelines continue to use ``_pipeline_identifier``
(favouring the issue number).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import Pipeline, PipelineMode  # noqa: E402
from routes.pipelines import _brc_history_identifier  # noqa: E402


def _make_babysit_pipeline(
    *,
    pr_number: int | None = 42,
    pr_head_sha: str | None = "abc1234deadbeef",
    pipeline_id: str = "pr-42",
    issue_number: int | None = None,
) -> Pipeline:
    """Construct a babysit-mode Pipeline with the given metadata."""
    return Pipeline(
        id=pipeline_id,
        repo="owner/repo",
        mode=PipelineMode.BABYSIT,
        pr_number=pr_number,
        pr_head_sha=pr_head_sha,
        branch="feature-x",
        has_contract=False,
        issue_number=issue_number,
    )


class TestBabysitHistoryIdentifierFormat:
    """Babysit pipelines produce ``pr-{pr}-{short_sha}`` identifiers."""

    def test_basic_pr_42_with_short_sha(self):
        pipeline = _make_babysit_pipeline(pr_number=42, pr_head_sha="abc1234deadbeef")
        assert _brc_history_identifier(pipeline) == "pr-42-abc1234"

    def test_single_digit_pr_with_short_sha(self):
        pipeline = _make_babysit_pipeline(pr_number=7, pr_head_sha="def5678cafebabe")
        assert _brc_history_identifier(pipeline) == "pr-7-def5678"

    def test_large_pr_number_with_full_sha(self):
        # 40-char SHA — should be truncated to first 7
        full_sha = "0000000abcdef1234567890abcdef1234567890a"
        pipeline = _make_babysit_pipeline(pr_number=12345, pr_head_sha=full_sha)
        assert _brc_history_identifier(pipeline) == "pr-12345-0000000"

    def test_truncates_full_40_char_sha(self):
        full_sha = "abcdef1234567890abcdef1234567890abcdef12"
        pipeline = _make_babysit_pipeline(pr_number=99, pr_head_sha=full_sha)
        result = _brc_history_identifier(pipeline)
        assert result == "pr-99-abcdef1"
        # Confirm it is exactly first 7 chars
        assert result.endswith(full_sha[:7])


class TestBabysitNamespacingPerSha:
    """Different head SHAs for the same PR yield distinct identifiers."""

    def test_two_shas_same_pr_yield_distinct_identifiers(self):
        sha_a = "aaaaaaa1111111111111111111111111111aaaaa"
        sha_b = "bbbbbbb2222222222222222222222222222bbbbb"
        p1 = _make_babysit_pipeline(pr_number=42, pr_head_sha=sha_a)
        p2 = _make_babysit_pipeline(pr_number=42, pr_head_sha=sha_b)
        id1 = _brc_history_identifier(p1)
        id2 = _brc_history_identifier(p2)
        assert id1 == "pr-42-aaaaaaa"
        assert id2 == "pr-42-bbbbbbb"
        assert id1 != id2

    def test_three_shas_yield_three_distinct_identifiers(self):
        shas = [
            "1111111aaaabbbb",
            "2222222ccccdddd",
            "3333333eeeeffff",
        ]
        ids = {
            _brc_history_identifier(_make_babysit_pipeline(pr_number=100, pr_head_sha=sha))
            for sha in shas
        }
        assert ids == {"pr-100-1111111", "pr-100-2222222", "pr-100-3333333"}
        assert len(ids) == 3


class TestBabysitFallbackToGeneric:
    """When babysit metadata is missing/invalid, fall back to generic ID."""

    def test_pr_head_sha_none_falls_back(self):
        pipeline = _make_babysit_pipeline(pr_number=42, pr_head_sha=None, pipeline_id="pr-42")
        # issue_number is None, so falls back to id
        assert _brc_history_identifier(pipeline) == "pr-42"

    def test_pr_number_none_falls_back(self):
        # Pipeline pydantic validator forbids pr_number=None when we want
        # to test the fallback — use SimpleNamespace to bypass validation.
        pipeline = SimpleNamespace(
            mode=PipelineMode.BABYSIT,
            pr_number=None,
            pr_head_sha="abc1234",
            id="pr-42",
            issue_number=None,
        )
        assert _brc_history_identifier(pipeline) == "pr-42"

    def test_empty_pr_head_sha_falls_back(self):
        # Use SimpleNamespace because Pipeline validator normalizes "" to None.
        pipeline = SimpleNamespace(
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            pr_head_sha="",
            id="pr-42",
            issue_number=None,
        )
        assert _brc_history_identifier(pipeline) == "pr-42"

    def test_short_pr_head_sha_falls_back(self):
        # SHA is shorter than 7 characters -> should fall back.
        # Use SimpleNamespace because Pipeline validator rejects non-hex SHAs.
        pipeline = SimpleNamespace(
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            pr_head_sha="short",
            id="pr-42",
            issue_number=None,
        )
        assert _brc_history_identifier(pipeline) == "pr-42"

    def test_non_string_pr_head_sha_falls_back(self):
        # Use SimpleNamespace because Pipeline pydantic validation
        # rejects an int sha.
        pipeline = SimpleNamespace(
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            pr_head_sha=42,
            id="pr-42",
            issue_number=None,
        )
        assert _brc_history_identifier(pipeline) == "pr-42"

    def test_zero_pr_number_falls_back(self):
        # Pydantic ge=1 forbids pr_number=0; bypass with SimpleNamespace.
        pipeline = SimpleNamespace(
            mode=PipelineMode.BABYSIT,
            pr_number=0,
            pr_head_sha="abc1234deadbeef",
            id="pr-0",
            issue_number=None,
        )
        # 0 is falsy so the babysit branch is skipped, falls through to
        # _pipeline_identifier(None, "pr-0") -> "pr-0"
        assert _brc_history_identifier(pipeline) == "pr-0"


class TestIssueModeUsesIssueNumber:
    """Issue-mode pipelines never trigger the babysit branch."""

    def test_issue_mode_returns_issue_number_even_when_pr_metadata_present(self):
        # Even if pr_number / pr_head_sha happen to be set, mode=ISSUE
        # means we use the issue number.
        pipeline = Pipeline(
            id="issue-99",
            repo="owner/repo",
            mode=PipelineMode.ISSUE,
            issue_number=99,
            pr_number=42,
            pr_head_sha="abc1234deadbeef",
            branch="feature-x",
        )
        result = _brc_history_identifier(pipeline)
        assert result == 99
        assert isinstance(result, int)

    def test_issue_mode_without_issue_number_falls_back_to_id(self):
        pipeline = Pipeline(
            id="custom-id",
            repo="owner/repo",
            mode=PipelineMode.ISSUE,
            issue_number=None,
            branch="feature-x",
        )
        assert _brc_history_identifier(pipeline) == "custom-id"


class TestPipelineWithoutMode:
    """Objects lacking a ``mode`` attribute fall back to the generic path."""

    def test_pipeline_without_mode_attr_falls_back(self):
        # MagicMock's getattr would normally autocreate a Mock, so use a
        # SimpleNamespace where ``mode`` is genuinely absent.
        pipeline = SimpleNamespace(
            issue_number=None,
            id="some-id",
        )
        # No mode attr -> getattr returns None -> babysit branch skipped.
        assert _brc_history_identifier(pipeline) == "some-id"

    def test_pipeline_without_mode_with_issue_number(self):
        pipeline = SimpleNamespace(
            issue_number=555,
            id="ignored-id",
        )
        result = _brc_history_identifier(pipeline)
        assert result == 555
        assert isinstance(result, int)


def _make_custom_pr_pipeline(
    *,
    pr_number: int | None = 42,
    pr_head_sha: str | None = "abc1234deadbeef",
    pipeline_id: str = "pr-42",
    issue_number: int | None = None,
) -> Pipeline:
    """Construct a CUSTOM-mode Pipeline with PR metadata."""
    return Pipeline(
        id=pipeline_id,
        repo="owner/repo",
        mode=PipelineMode.CUSTOM,
        pr_number=pr_number,
        pr_head_sha=pr_head_sha,
        branch="feature-x",
        has_contract=False,
        issue_number=issue_number,
    )


class TestCustomPrHistoryIdentifierFormat:
    """CUSTOM+PR pipelines produce ``pr-{pr}-{short_sha}`` identifiers,
    matching BABYSIT behaviour (subsumption parity — #1762)."""

    def test_basic_custom_pr_with_short_sha(self):
        pipeline = _make_custom_pr_pipeline(pr_number=42, pr_head_sha="abc1234deadbeef")
        assert _brc_history_identifier(pipeline) == "pr-42-abc1234"

    def test_custom_pr_large_number_with_full_sha(self):
        full_sha = "0000000abcdef1234567890abcdef1234567890a"
        pipeline = _make_custom_pr_pipeline(pr_number=12345, pr_head_sha=full_sha)
        assert _brc_history_identifier(pipeline) == "pr-12345-0000000"

    def test_custom_pr_matches_babysit_output(self):
        """Same PR metadata should yield the same identifier regardless of mode."""
        pr_number = 99
        sha = "deadbeef1234567890"
        babysit = _make_babysit_pipeline(pr_number=pr_number, pr_head_sha=sha)
        custom = _make_custom_pr_pipeline(pr_number=pr_number, pr_head_sha=sha)
        assert _brc_history_identifier(babysit) == _brc_history_identifier(custom)


class TestCustomPrNamespacingPerSha:
    """Different head SHAs for the same PR yield distinct identifiers in CUSTOM mode."""

    def test_two_shas_same_pr_yield_distinct_identifiers(self):
        sha_a = "aaaaaaa1111111111111111111111111111aaaaa"
        sha_b = "bbbbbbb2222222222222222222222222222bbbbb"
        p1 = _make_custom_pr_pipeline(pr_number=42, pr_head_sha=sha_a)
        p2 = _make_custom_pr_pipeline(pr_number=42, pr_head_sha=sha_b)
        assert _brc_history_identifier(p1) == "pr-42-aaaaaaa"
        assert _brc_history_identifier(p2) == "pr-42-bbbbbbb"
        assert _brc_history_identifier(p1) != _brc_history_identifier(p2)


class TestCustomWithoutPrFallsBack:
    """CUSTOM-mode pipelines without pr_number use the generic path."""

    def test_custom_no_pr_falls_back_to_pipeline_id(self):
        pipeline = Pipeline(
            id="custom-task-abc",
            repo="owner/repo",
            mode=PipelineMode.CUSTOM,
            issue_number=None,
            branch="feature-x",
        )
        assert _brc_history_identifier(pipeline) == "custom-task-abc"

    def test_custom_with_issue_number_no_pr_uses_pipeline_id(self):
        """CUSTOM mode always keys by pipeline_id (not issue_number) to avoid
        collision with concurrent ISSUE-mode pipelines on the same issue."""
        pipeline = Pipeline(
            id="issue-77-qualifier",
            repo="owner/repo",
            mode=PipelineMode.CUSTOM,
            issue_number=77,
            branch="feature-x",
        )
        result = _brc_history_identifier(pipeline)
        assert result == "issue-77-qualifier"

    def test_custom_pr_missing_sha_falls_back(self):
        pipeline = _make_custom_pr_pipeline(pr_number=42, pr_head_sha=None, pipeline_id="pr-42")
        assert _brc_history_identifier(pipeline) == "pr-42"
