"""Tests for ``sandbox/egg_lib/overseer_issue_body.py`` (issue #1962, TASK-3-1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from egg_lib.overseer_issue_body import (
    DEFAULT_FILED_ISSUES_PATH,
    compose_issue_body,
    compose_issue_title,
    find_existing_issue,
)
from egg_overseer.state import (
    FiledIssueRecord,
    append_filed_issue,
)

_GH_PAT = "ghp_" + "A" * 36
_AKIA = "AKIA" + "B" * 16


# ---------------------------------------------------------------------------
# compose_issue_title
# ---------------------------------------------------------------------------


class TestComposeIssueTitle:
    def test_format_includes_signature_prefix(self) -> None:
        title = compose_issue_title(
            anomaly_type="agent-loop",
            agent_role="coder",
            anomaly_signature="abcdef0123456789",
        )
        assert title == "[Pipeline Diagnostic] agent-loop - coder [abcdef01]"

    def test_signature_truncated_to_8_chars(self) -> None:
        title = compose_issue_title(
            anomaly_type="x",
            agent_role="r",
            anomaly_signature="0123456789abcdef",
        )
        assert "[01234567]" in title
        # The remaining characters of the 16-hex sig must NOT leak.
        assert "89abcdef" not in title

    def test_title_under_120_chars_for_normal_inputs(self) -> None:
        title = compose_issue_title(
            anomaly_type="agent-stuck-phase-transition",
            agent_role="reviewer_contract",
            anomaly_signature="abcdef0123456789",
        )
        assert len(title) <= 120


# ---------------------------------------------------------------------------
# compose_issue_body
# ---------------------------------------------------------------------------


class TestComposeIssueBody:
    def _kwargs(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "anomaly_type": "agent-stall",
            "agent_role": "coder",
            "pipeline_id": "issue-1",
            "phase": "implement",
            "branch": "egg/issue-1",
            "commit_sha": "abc1234",
            "parent_alert_message_id": "msg-1",
            "classification": {
                "type": "agent-stall",
                "confidence": 0.9,
                "reasoning": "no progress for 5 minutes",
            },
            "recent_log_lines": ["line a", "line b"],
            "health_alerts": [{"type": "tier1", "detail": "x"}],
            "timeline": [{"timestamp": "ts", "event": "ev"}],
            "actions_taken": ["nudge sent"],
            "suggested_remediation": "check logs",
            "repo": "owner/repo",
        }
        base.update(overrides)
        return base

    def test_body_renders_all_pipeline_links_subblock(self) -> None:
        body = compose_issue_body(**self._kwargs())  # type: ignore[arg-type]
        assert "issue-1" in body
        assert "implement" in body
        assert "egg/issue-1" in body
        assert "abc1234" in body
        assert "msg-1" in body

    def test_body_includes_branch_url(self) -> None:
        body = compose_issue_body(**self._kwargs())  # type: ignore[arg-type]
        assert "https://github.com/owner/repo/tree/egg/issue-1" in body

    def test_body_includes_classification(self) -> None:
        body = compose_issue_body(**self._kwargs())  # type: ignore[arg-type]
        assert "no progress for 5 minutes" in body
        assert "0.9" in body or "confidence" in body.lower()

    def test_body_includes_actions_and_health_alerts_block(self) -> None:
        body = compose_issue_body(**self._kwargs())  # type: ignore[arg-type]
        assert "nudge sent" in body
        assert "Active Tier-1 health alerts" in body
        assert "tier1" in body

    def test_body_with_empty_optional_fields(self) -> None:
        body = compose_issue_body(
            anomaly_type="agent-stall",
            agent_role="coder",
            pipeline_id="issue-1",
            phase="implement",
            branch="b",
            commit_sha="c",
            parent_alert_message_id="m",
            repo="o/r",
        )
        assert "No timeline events recorded" in body
        assert "No corrective actions taken yet" in body
        assert "No classification data available" in body
        # Container Logs section should NOT appear when no log lines.
        assert "### Container Logs" not in body

    def test_body_truncates_logs_to_last_50(self) -> None:
        many = [f"line {i}" for i in range(120)]
        body = compose_issue_body(
            anomaly_type="x",
            agent_role="r",
            pipeline_id="p",
            phase="implement",
            branch="b",
            commit_sha="c",
            parent_alert_message_id="m",
            recent_log_lines=many,
            repo="o/r",
        )
        # Last 50 lines kept, earlier dropped.
        assert "line 119" in body
        assert "line 70" in body
        assert "line 0" not in body

    def test_body_scrubs_secrets(self) -> None:
        body = compose_issue_body(
            anomaly_type="x",
            agent_role="r",
            pipeline_id="p",
            phase="implement",
            branch="b",
            commit_sha="c",
            parent_alert_message_id="m",
            classification={
                "type": "x",
                "confidence": 1.0,
                "reasoning": f"Token: {_GH_PAT}",
            },
            recent_log_lines=[f"AWS access: {_AKIA}"],
            repo="o/r",
        )
        assert _GH_PAT not in body
        assert _AKIA not in body
        assert "[REDACTED:gh-pat]" in body
        assert "[REDACTED:aws-key]" in body

    def test_body_uses_repo_from_env_when_repo_arg_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "envowner/envrepo")
        body = compose_issue_body(
            anomaly_type="x",
            agent_role="r",
            pipeline_id="p",
            phase="implement",
            branch="b",
            commit_sha="c",
            parent_alert_message_id="m",
            repo=None,
        )
        assert "envowner/envrepo" in body


# ---------------------------------------------------------------------------
# find_existing_issue
# ---------------------------------------------------------------------------


def _filed_record(sig: str, num: int) -> FiledIssueRecord:
    from datetime import UTC, datetime

    return FiledIssueRecord(
        issue_number=num,
        anomaly_type="x",
        anomaly_signature=sig,
        agent_role="r",
        repo="owner/repo",
        pipeline_id="p",
        phase="implement",
        filed_at=datetime.now(UTC),
        hitl_outcome="filed",
    )


class TestFindExistingIssue:
    def test_local_cache_hit_returns_most_recent(self, tmp_path: Path) -> None:
        path = tmp_path / "filed-issues.jsonl"
        sig = "deadbeef" + "0" * 8
        # Two filings of the same signature; latest must win.
        append_filed_issue(path, _filed_record(sig, 100))
        append_filed_issue(path, _filed_record(sig, 200))
        # Different signature should not match.
        append_filed_issue(path, _filed_record("xxxxxxxx" + "0" * 8, 999))
        # _gh_runner is not invoked when local cache hits.
        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature=sig,
            filed_issues_path=path,
            _gh_runner=lambda *a, **k: pytest.fail("gh should not run on local hit"),
        )
        assert result == 200

    def test_skipped_record_is_not_a_match(self, tmp_path: Path) -> None:
        path = tmp_path / "filed-issues.jsonl"
        sig = "abcdef0123456789"
        # Skipped record has issue_number=None.
        from datetime import UTC, datetime

        rec = FiledIssueRecord(
            issue_number=None,
            anomaly_type="x",
            anomaly_signature=sig,
            agent_role="r",
            repo="owner/repo",
            pipeline_id="p",
            phase="implement",
            filed_at=datetime.now(UTC),
            hitl_outcome="skipped",
        )
        append_filed_issue(path, rec)

        # Skipped record should not be returned by find_existing_issue.
        # Verify by checking that gh fallback is invoked (returns nothing).
        gh_calls: list[list[str]] = []

        def _gh(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            gh_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature=sig,
            filed_issues_path=path,
            _gh_runner=_gh,
        )
        assert result is None
        assert len(gh_calls) == 1

    def test_gh_fallback_invoked_when_cache_miss(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        sig = "abcdef01" + "0" * 8

        called_argv: list[list[str]] = []

        def _gh(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            called_argv.append(argv)
            payload = [{"number": 123, "title": f"[Pipeline Diagnostic] x [{sig[:8]}]"}]
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature=sig,
            filed_issues_path=path,
            _gh_runner=_gh,
        )
        assert result == 123
        assert len(called_argv) == 1
        argv = called_argv[0]
        assert argv[:3] == ["gh", "issue", "list"]
        assert "--repo" in argv
        assert "agent:overseer" in argv
        # Search uses ``in:title`` qualifier so we don't match the
        # 8-char prefix in body or comment text. Reviewer flagged the
        # bare-prefix search as too loose.
        assert any(f"in:title {sig[:8]}" == arg for arg in argv)

    def test_gh_fallback_returns_none_when_no_matching_title(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        sig = "abcdef0123456789"

        def _gh(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    [
                        # Title does NOT contain the 8-char prefix — should not match.
                        {"number": 1, "title": "Unrelated bug"},
                    ]
                ),
                stderr="",
            )

        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature=sig,
            filed_issues_path=path,
            _gh_runner=_gh,
        )
        assert result is None

    def test_gh_nonzero_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"

        def _gh(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature="abcdef0123456789",
            filed_issues_path=path,
            _gh_runner=_gh,
        )
        assert result is None

    def test_gh_invalid_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"

        def _gh(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="")

        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature="abcdef0123456789",
            filed_issues_path=path,
            _gh_runner=_gh,
        )
        assert result is None

    def test_corrupt_local_cache_falls_back_to_gh(self, tmp_path: Path) -> None:
        path = tmp_path / "filed-issues.jsonl"
        # Header missing — load_filed_issues raises ValueError.
        path.write_text("garbage line\n", encoding="utf-8")

        called: list[list[str]] = []

        def _gh(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

        result = find_existing_issue(
            repo="owner/repo",
            anomaly_signature="abcdef0123456789",
            filed_issues_path=path,
            _gh_runner=_gh,
        )
        assert result is None
        # Falls back to gh after the local cache read fails.
        assert called

    def test_default_path_constant(self) -> None:
        # Smoke-test the constant the CLI verb reads.
        assert DEFAULT_FILED_ISSUES_PATH.endswith("filed-issues.jsonl")
        assert ".egg-state" in DEFAULT_FILED_ISSUES_PATH
