"""
Tests for auto PR creation functions (_build_pr_body, _auto_create_pr).
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus
from routes.pipelines import _auto_create_pr, _build_pr_body


def _make_pipeline(
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
    auto_create_pr=True,
):
    """Create a Pipeline for testing."""
    return Pipeline(
        id=f"issue-{issue_number}" if issue_number else "local-test",
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PR,
        config=PipelineConfig(auto_create_pr=auto_create_pr),
    )


def _make_contract_json(
    issue_number=42,
    issue_title="Fix the auth bug",
    pr_title="Fix authentication bypass in login flow",
    pr_description="Fixes a bypass where unauthenticated users could access protected routes.\n\nCloses #42",
):
    """Create a contract JSON dict for testing."""
    contract = {
        "schemaVersion": "1.0",
        "issue": {
            "number": issue_number,
            "title": issue_title,
            "url": f"https://github.com/owner/repo/issues/{issue_number}",
        },
        "current_phase": "pr",
        "phases": [],
    }
    if pr_title:
        contract["pr"] = {"title": pr_title, "description": pr_description or ""}
    return contract


class TestBuildPrBody:
    """Tests for _build_pr_body."""

    def test_uses_contract_pr_metadata(self, tmp_path):
        """Test that PR title/body come from contract PR metadata."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.json"
        contract_file.write_text(json.dumps(_make_contract_json()))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            title, body = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix authentication bypass in login flow"
        assert "Fixes a bypass" in body
        assert "Closes #42" in body

    def test_falls_back_to_issue_title(self, tmp_path):
        """Test fallback to issue title when no PR metadata."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.json"
        contract_file.write_text(
            json.dumps(_make_contract_json(pr_title=None))
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            title, body = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix the auth bug"

    def test_falls_back_to_pipeline_id(self, tmp_path):
        """Test fallback to pipeline ID when no contract exists."""
        pipeline = _make_pipeline()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "issue-42" in title

    def test_includes_commit_log(self, tmp_path):
        """Test that commit log is included in body."""
        pipeline = _make_pipeline()

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "log" in args:
                result.returncode = 0
                result.stdout = "abc1234 Fix auth bypass\ndef5678 Add tests"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "abc1234 Fix auth bypass" in body
        assert "## Commits" in body

    def test_includes_diff_stats(self, tmp_path):
        """Test that diff stats are included in body."""
        pipeline = _make_pipeline()

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "diff" in args:
                result.returncode = 0
                result.stdout = " src/auth.py | 10 ++++------\n 1 file changed"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "src/auth.py" in body
        assert "## Changes" in body

    def test_includes_issue_reference_when_no_description(self, tmp_path):
        """Test that issue reference is added when no PR description."""
        pipeline = _make_pipeline()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "Closes #42" in body

    def test_body_ends_with_authored_by(self, tmp_path):
        """Test that body ends with attribution."""
        pipeline = _make_pipeline()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "Authored-by: egg" in body


class TestAutoCreatePr:
    """Tests for _auto_create_pr."""

    def test_creates_pr_via_gateway(self):
        """Test that _auto_create_pr calls gateway.create_pr."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        with patch("routes.pipelines._build_pr_body") as mock_build:
            mock_build.return_value = ("Fix auth", "Body text")
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/1"
        spawner.gateway.create_pr.assert_called_once_with(
            pipeline_id="issue-42",
            repo="owner/repo",
            title="Fix auth",
            body="Body text",
            head="egg/issue-42",
        )

    def test_returns_none_when_no_repo(self):
        """Test that _auto_create_pr returns None when repo is missing."""
        pipeline = _make_pipeline(repo=None)
        spawner = MagicMock()

        result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_when_no_branch(self):
        """Test that _auto_create_pr returns None when branch is missing."""
        pipeline = _make_pipeline(branch=None)
        spawner = MagicMock()

        result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_on_gateway_error(self):
        """Test that _auto_create_pr returns None on gateway error."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.side_effect = Exception("Gateway unreachable")

        with patch("routes.pipelines._build_pr_body") as mock_build:
            mock_build.return_value = ("Title", "Body")
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None
