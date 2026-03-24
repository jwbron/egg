"""
Tests for auto PR creation functions (_build_pr_body, _auto_create_pr).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import Pipeline, PipelinePhase, PipelineStatus
from routes.pipelines import (
    _auto_create_pr,
    _build_pr_body,
    _compute_gateway_mode,
    _detect_default_branch,
    _handle_pr_creation_failure,
)


def _make_pipeline(
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
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
        contract_file.write_text(json.dumps(_make_contract_json(pr_title=None)))

        title, body = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix the auth bug"

    def test_falls_back_to_pipeline_id(self, tmp_path):
        """Test fallback to pipeline ID when no contract exists."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "issue-42" in title

    def test_does_not_include_commit_log(self, tmp_path):
        """Test that commit log is NOT included in body (GitHub shows it natively)."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "## Commits" not in body

    def test_does_not_include_diff_stats(self, tmp_path):
        """Test that diff stats are NOT included in body (GitHub shows it natively)."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "## Changes" not in body

    def test_includes_issue_reference_when_no_description(self, tmp_path):
        """Test that issue reference is added when no PR description."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "Closes #42" in body

    def test_body_ends_with_authored_by(self, tmp_path):
        """Test that body ends with attribution."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "Authored-by: egg" in body

    def test_includes_pipeline_context_section(self, tmp_path):
        """Test that pipeline context section is included in body."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "## Pipeline Context" in body
        assert "Pipeline: `issue-42`" in body
        assert "Issue: #42" in body

    def test_pipeline_context_before_authored_by(self, tmp_path):
        """Test that pipeline context appears before the authored-by line."""
        pipeline = _make_pipeline()

        title, body = _build_pr_body(pipeline, tmp_path)

        context_pos = body.index("## Pipeline Context")
        authored_pos = body.index("Authored-by: egg")
        assert context_pos < authored_pos

    def test_body_stays_well_under_github_limit(self, tmp_path):
        """Test that body without git log/diff stays well under 65536 chars."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        # Use a reasonably long PR description
        contract_file = contract_dir / "42.json"
        contract_file.write_text(json.dumps(_make_contract_json(pr_description="A" * 10_000)))

        title, body = _build_pr_body(pipeline, tmp_path)

        assert len(body) < 65_536


class TestAutoCreatePr:
    """Tests for _auto_create_pr."""

    def test_creates_pr_via_gateway(self):
        """Test that _auto_create_pr calls gateway.create_pr with metadata."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Fix auth", "Body text")),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/1"
        spawner.gateway.create_pr.assert_called_once_with(
            pipeline_id="issue-42",
            repo="owner/repo",
            title="Fix auth",
            body="Body text",
            head="egg/issue-42",
            base="main",
            issue_number=42,
            agent_role="orchestrator",
            mode="public",
            draft=False,
        )

    def test_creates_draft_pr_in_private_mode(self):
        """Test that _auto_create_pr creates a draft PR in private mode."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/2"

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Fix auth", "Body text")),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner, gateway_mode="private")

        assert result == "https://github.com/owner/repo/pull/2"
        spawner.gateway.create_pr.assert_called_once_with(
            pipeline_id="issue-42",
            repo="owner/repo",
            title="Fix auth",
            body="Body text",
            head="egg/issue-42",
            base="main",
            issue_number=42,
            agent_role="orchestrator",
            mode="private",
            draft=True,
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

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Title", "Body")),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None


class TestComputeGatewayMode:
    """Tests for _compute_gateway_mode helper."""

    def test_uses_explicit_network_mode(self):
        """Returns pipeline.network_mode when set, visibility is None."""
        pipeline = _make_pipeline()
        pipeline.network_mode = "private"
        mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "private"
        assert vis is None

    def test_auto_detects_private_repo(self):
        """Auto-detects private mode from repo visibility."""
        pipeline = _make_pipeline()
        pipeline.network_mode = None
        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "private"
        with patch("routes.pipelines.get_gateway_client", return_value=mock_client):
            mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "private"
        assert vis == "private"

    def test_auto_detects_internal_repo(self):
        """Treats internal repos as private."""
        pipeline = _make_pipeline()
        pipeline.network_mode = None
        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "internal"
        with patch("routes.pipelines.get_gateway_client", return_value=mock_client):
            mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "private"
        assert vis == "internal"

    def test_defaults_to_public(self):
        """Defaults to public when no network_mode and no repo."""
        pipeline = _make_pipeline(repo=None)
        pipeline.network_mode = None
        mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "public"
        assert vis is None

    def test_defaults_to_public_for_public_repo(self):
        """Returns public for public repos."""
        pipeline = _make_pipeline()
        pipeline.network_mode = None
        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "public"
        with patch("routes.pipelines.get_gateway_client", return_value=mock_client):
            mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "public"
        assert vis == "public"


class TestDetectDefaultBranch:
    """Tests for _detect_default_branch."""

    def test_detects_via_symbolic_ref(self, tmp_path):
        """Detects default branch from origin/HEAD symbolic ref."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "symbolic-ref" in args:
                result.returncode = 0
                result.stdout = "origin/master\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "master"

    def test_detects_main_branch(self, tmp_path):
        """Falls back to origin/main when symbolic-ref fails."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "symbolic-ref" in args:
                result.returncode = 1
                result.stdout = ""
            elif "rev-parse" in args and "origin/main" in args:
                result.returncode = 0
                result.stdout = "abc123\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "main"

    def test_detects_master_branch(self, tmp_path):
        """Falls back to origin/master when origin/main doesn't exist."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "symbolic-ref" in args:
                result.returncode = 1
                result.stdout = ""
            elif "rev-parse" in args and "origin/main" in args:
                result.returncode = 1
                result.stdout = ""
            elif "rev-parse" in args and "origin/master" in args:
                result.returncode = 0
                result.stdout = "abc123\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "master"

    def test_falls_back_to_main(self, tmp_path):
        """Falls back to 'main' when nothing resolves."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "main"


class TestAutoCreatePrPassesBaseBranch:
    """Tests that _auto_create_pr passes the detected base branch."""

    def test_passes_detected_base_branch(self):
        """Verify auto-detected base branch is passed to gateway.create_pr."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Title", "Body")),
            patch("routes.pipelines.get_default_branch", return_value="master"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/1"
        call_kwargs = spawner.gateway.create_pr.call_args
        assert call_kwargs[1]["base"] == "master"

    def test_passes_explicit_base_branch(self):
        """Verify explicit pipeline.base_branch is used for both PR base and body."""
        pipeline = _make_pipeline()
        pipeline.base_branch = "release/v2"
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/3"

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Title", "Body")) as mock_build,
            patch("routes.pipelines.get_default_branch") as mock_detect,
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/3"
        # Verify create_pr receives the explicit base branch
        call_kwargs = spawner.gateway.create_pr.call_args
        assert call_kwargs[1]["base"] == "release/v2"
        # Verify _build_pr_body is called (default_branch no longer passed)
        mock_build.assert_called_once_with(pipeline, Path("/tmp/repo"))
        # Verify get_default_branch is NOT called when explicit base is provided
        mock_detect.assert_not_called()


class TestHandlePrCreationFailure:
    """Tests for _handle_pr_creation_failure — the extracted helper that marks
    the pipeline FAILED when PR creation returns no URL.
    """

    def test_marks_pipeline_and_phase_failed(self):
        """_handle_pr_creation_failure sets pipeline and phase to FAILED."""
        pipeline = _make_pipeline()
        pipeline.status = PipelineStatus.RUNNING

        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with patch("routes.pipelines.get_pipeline_state_lock"):
            _handle_pr_creation_failure(
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                store=store,
            )

        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.error == "Auto PR creation failed: no PR URL returned"

        phase_execution = pipeline.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.status == PipelineStatus.FAILED
        assert phase_execution.error == "Auto PR creation failed: no PR URL returned"
        assert phase_execution.completed_at is not None

        store.save_pipeline.assert_called_once_with(pipeline)
