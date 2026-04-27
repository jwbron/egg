"""Tests for _get_concurrent_status and pipeline status concurrent monitoring.

These tests target gaps in the coder's implementation:
- _get_concurrent_status edge cases (no config, empty phases, no agents)
- Pipeline status endpoint assertion strength
- Missing agent lifecycle data paths
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import (
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _get_concurrent_status, pipelines_bp


@pytest.fixture
def app():
    """Create a test Flask app with the pipelines blueprint."""
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


def _make_concurrent_pipeline(pipeline_id: str = "issue-999", **config_overrides) -> Pipeline:
    """Create a pipeline with concurrent_execution enabled."""
    defaults = {
        "concurrent_execution": True,
        "max_concurrent_agents": 4,
        "message_poll_hint_seconds": 30,
        "consensus_timeout_minutes": 30,
    }
    defaults.update(config_overrides)
    config = PipelineConfig(**defaults)

    return Pipeline(
        id=pipeline_id,
        issue_number=999,
        repo="owner/repo",
        branch="egg/issue-999",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


class TestGetConcurrentStatusUnit:
    """Unit tests for _get_concurrent_status function."""

    def test_returns_none_when_concurrent_not_enabled(self):
        """Should return None for a pipeline with concurrent execution disabled."""
        config = PipelineConfig(concurrent_execution=False, concurrent_phases=[])
        pipeline = Pipeline(
            id="issue-100",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )
        result = _get_concurrent_status(pipeline)
        assert result is None

    def test_returns_dict_when_concurrent_enabled(self):
        """Should return a dict with enabled=True when concurrent is on."""
        pipeline = _make_concurrent_pipeline()
        result = _get_concurrent_status(pipeline)

        assert result is not None
        assert result["enabled"] is True
        assert result["max_concurrent_agents"] == 4

    def test_message_store_fallback_when_unavailable(self):
        """Should return zeroed message counts when message_store is not importable."""
        pipeline = _make_concurrent_pipeline()
        result = _get_concurrent_status(pipeline)

        # Phase 1 not implemented yet, so message_store import will fail
        assert "messages" in result
        assert result["messages"]["total"] == 0
        assert result["messages"]["by_type"] == {}

    def test_consensus_omitted_when_unavailable(self):
        """Should omit consensus key when consensus state is unavailable.

        This ensures callers (e.g. MCP get_consensus_status) can fall back to
        message-based inference instead of seeing a truthy-but-empty dict.
        See issue #1229.
        """
        pipeline = _make_concurrent_pipeline()

        # Patch both BRC tracker and legacy evaluator to simulate unavailability
        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            patch("consensus.get_consensus_evaluator", side_effect=ImportError("not available")),
        ):
            result = _get_concurrent_status(pipeline)

        assert "consensus" not in result

    def test_max_concurrent_agents_custom_value(self):
        """Should reflect custom max_concurrent_agents from config."""
        pipeline = _make_concurrent_pipeline(max_concurrent_agents=8)
        result = _get_concurrent_status(pipeline)

        assert result["max_concurrent_agents"] == 8

    def test_no_agents_in_phase_execution(self):
        """Should handle phase execution with no agents attribute."""
        pipeline = _make_concurrent_pipeline()
        # Pipeline has no phase execution data at all
        assert pipeline.phases == {}
        result = _get_concurrent_status(pipeline)

        # Should not have agents key when no phase execution
        assert "agents" not in result

    def test_agents_from_phase_execution(self):
        """Should include agent lifecycle data when phase has agents."""
        pipeline = _make_concurrent_pipeline()

        # Simulate a phase execution with agents
        mock_phase_exec = MagicMock()
        mock_agent_1 = MagicMock()
        mock_agent_1.role = "coder"
        mock_agent_1.status.value = "running"
        mock_agent_2 = MagicMock()
        mock_agent_2.role = "tester"
        mock_agent_2.status.value = "completed"
        mock_phase_exec.agents = [mock_agent_1, mock_agent_2]

        pipeline.phases["implement"] = mock_phase_exec

        result = _get_concurrent_status(pipeline)

        assert "agents" in result
        assert len(result["agents"]) == 2
        assert result["agents"][0]["role"] == "coder"
        assert result["agents"][0]["status"] == "running"
        assert result["agents"][1]["role"] == "tester"
        assert result["agents"][1]["status"] == "completed"

    def test_agents_include_container_id_and_elapsed_seconds(self):
        """Issue #2084: each running agent entry carries ``container_id``,
        ``started_at`` and a server-computed ``elapsed_seconds`` so the
        sandboxed overseer can anchor stall-duration math on the live
        container instead of message-bus history."""
        from datetime import UTC, datetime, timedelta

        pipeline = _make_concurrent_pipeline()

        mock_phase_exec = MagicMock()
        mock_agent = MagicMock(spec=["role", "status", "container_id", "started_at"])
        mock_agent.role = "coder"
        mock_agent.status.value = "running"
        mock_agent.container_id = "f98c4fe6abcdef"
        mock_agent.started_at = datetime.now(UTC) - timedelta(seconds=152)
        mock_phase_exec.agents = [mock_agent]

        pipeline.phases["implement"] = mock_phase_exec

        result = _get_concurrent_status(pipeline)

        assert "agents" in result
        agent_entry = result["agents"][0]
        assert agent_entry["role"] == "coder"
        assert agent_entry["container_id"] == "f98c4fe6abcdef"
        assert "started_at" in agent_entry
        assert 150 <= agent_entry["elapsed_seconds"] <= 160

    def test_agent_without_started_at_omits_elapsed_seconds(self):
        """Agents that haven't started yet (no ``started_at``) must not
        gain a fabricated ``elapsed_seconds`` field."""
        pipeline = _make_concurrent_pipeline()

        mock_phase_exec = MagicMock()
        mock_agent = MagicMock(spec=["role", "status"])
        mock_agent.role = "tester"
        mock_agent.status.value = "pending"
        mock_phase_exec.agents = [mock_agent]

        pipeline.phases["implement"] = mock_phase_exec

        result = _get_concurrent_status(pipeline)

        agent_entry = result["agents"][0]
        assert "elapsed_seconds" not in agent_entry
        assert "started_at" not in agent_entry
        assert "container_id" not in agent_entry

    def test_agents_without_role_attribute(self):
        """Should use str() fallback when agent has no role attribute."""
        pipeline = _make_concurrent_pipeline()

        mock_phase_exec = MagicMock()
        # Agent object without .role attribute (delattr to remove MagicMock auto-attr)
        mock_agent = MagicMock(spec=[])
        mock_phase_exec.agents = [mock_agent]

        pipeline.phases["implement"] = mock_phase_exec

        result = _get_concurrent_status(pipeline)

        assert "agents" in result
        assert len(result["agents"]) == 1
        # Should use str() since no .role attribute
        assert result["agents"][0]["role"] == str(mock_agent)
        assert result["agents"][0]["status"] == "unknown"


class TestPipelineStatusConcurrentEndpoint:
    """Tests for the pipeline status endpoint with concurrent data."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_concurrent_section_structure_is_complete(self, mock_resolve, mock_repo_path, client):
        """Verify that concurrent section has all required keys."""
        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.get("/api/v1/pipelines/issue-999/status")
        assert resp.status_code == 200

        data = json.loads(resp.data)
        concurrent = data["data"].get("concurrent")

        # Unlike the coder's test which uses "if concurrent is not None",
        # we assert it IS present when concurrent_execution is enabled
        assert concurrent is not None, "concurrent section should be present"
        assert concurrent["enabled"] is True
        assert "messages" in concurrent
        # consensus is omitted when no tracker/evaluator is available (#1229)
        assert "max_concurrent_agents" in concurrent
        assert concurrent["max_concurrent_agents"] == 4

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_status_exposes_overseer_advisor_recent_log_bytes_cap(
        self, mock_resolve, mock_repo_path, client
    ):
        """Issue #2120: the status endpoint exposes the byte-cap config field
        so the overseer agent can forward it to ``consult-advisor``."""
        pipeline = _make_concurrent_pipeline(
            overseer_advisor_recent_log_bytes_cap=65_536,
        )
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.get("/api/v1/pipelines/issue-999/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        cfg = data["data"]["config"]
        assert cfg["overseer_advisor_recent_log_bytes_cap"] == 65_536

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_concurrent_section_absent_for_non_concurrent(
        self, mock_resolve, mock_repo_path, client
    ):
        """Verify concurrent section is NOT present for non-concurrent pipelines."""
        config = PipelineConfig(concurrent_execution=False, concurrent_phases=[])
        pipeline = Pipeline(
            id="issue-100",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.get("/api/v1/pipelines/issue-100/status")
        assert resp.status_code == 200

        data = json.loads(resp.data)
        assert "concurrent" not in data["data"]

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_concurrent_message_counts_in_status(self, mock_resolve, mock_repo_path, client):
        """Verify message counts appear correctly in status response."""
        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.get("/api/v1/pipelines/issue-999/status")
        data = json.loads(resp.data)

        messages = data["data"]["concurrent"]["messages"]
        assert messages["total"] == 0  # Phase 1 not implemented
        assert messages["by_type"] == {}

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_concurrent_consensus_omitted_when_no_tracker(
        self, mock_resolve, mock_repo_path, client
    ):
        """Consensus key is absent when no tracker or evaluator is available.

        This allows callers to distinguish "no consensus data" from "consensus
        data with no agents" and fall back to message-based inference (#1229).
        """
        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            patch("consensus.get_consensus_evaluator", side_effect=ImportError("not available")),
        ):
            resp = client.get("/api/v1/pipelines/issue-999/status")
        data = json.loads(resp.data)

        assert "consensus" not in data["data"]["concurrent"]


def _make_pipeline_with_pr_artifact(pr_url: str | None) -> Pipeline:
    """Build a pipeline in the PR phase with optional ``pr_url`` artifact."""
    pipeline = Pipeline(
        id="issue-1613",
        issue_number=1613,
        repo="owner/repo",
        branch="egg/issue-1613",
        status=PipelineStatus.COMPLETE,
        current_phase=PipelinePhase.PR,
    )
    if pr_url is not None:
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PR)
        phase_exec.artifacts = {"pr_url": pr_url}
    return pipeline


class TestPipelineStatusPrInfo:
    """Tests for the PR URL/number fields in the pipeline status response (#1625)."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_pr_info_present_when_pr_phase_has_artifact(self, mock_resolve, mock_repo_path, client):
        """pr_url and pr_number are included once the PR phase has created a PR."""
        pipeline = _make_pipeline_with_pr_artifact("https://github.com/owner/repo/pull/1624")
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1613/status")
        assert resp.status_code == 200

        data = json.loads(resp.data)["data"]
        assert data["pr_url"] == "https://github.com/owner/repo/pull/1624"
        assert data["pr_number"] == 1624

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_pr_info_absent_when_no_pr_phase(self, mock_resolve, mock_repo_path, client):
        """No pr_url/pr_number keys when the pipeline has not reached the PR phase."""
        pipeline = Pipeline(
            id="issue-1613",
            issue_number=1613,
            repo="owner/repo",
            branch="egg/issue-1613",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1613/status")
        data = json.loads(resp.data)["data"]
        assert "pr_url" not in data
        assert "pr_number" not in data

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_pr_info_absent_when_artifacts_empty(self, mock_resolve, mock_repo_path, client):
        """Post-reset (request_changes / recovery) leaves artifacts empty; no stale URL leaks."""
        pipeline = _make_pipeline_with_pr_artifact("https://github.com/owner/repo/pull/1624")
        pipeline.get_phase_execution(PipelinePhase.PR).artifacts = {}
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1613/status")
        data = json.loads(resp.data)["data"]
        assert "pr_url" not in data
        assert "pr_number" not in data

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_pr_url_present_but_pr_number_absent_for_malformed_url(
        self, mock_resolve, mock_repo_path, client
    ):
        """A URL without a /pull/N segment still surfaces pr_url; pr_number is omitted."""
        pipeline = _make_pipeline_with_pr_artifact("not-a-valid-pr-url")
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1613/status")
        data = json.loads(resp.data)["data"]
        assert data["pr_url"] == "not-a-valid-pr-url"
        assert "pr_number" not in data

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_pr_info_works_with_enterprise_github_url(self, mock_resolve, mock_repo_path, client):
        """Enterprise GitHub URLs still match /pull/(\\d+) and yield a pr_number."""
        pipeline = _make_pipeline_with_pr_artifact("https://github.acme.com/owner/repo/pull/42")
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1613/status")
        data = json.loads(resp.data)["data"]
        assert data["pr_url"] == "https://github.acme.com/owner/repo/pull/42"
        assert data["pr_number"] == 42


class TestJiraPipelineConcurrentStatus:
    """Tests that _get_concurrent_status works with JIRA-format pipeline IDs (#1615)."""

    def test_jira_pipeline_message_store_returns_counts(self):
        """_get_concurrent_status should return message counts for JIRA pipelines."""
        pipeline = _make_concurrent_pipeline(pipeline_id="KORE-1234")

        with (
            patch("concurrent_executor.is_concurrent_execution", return_value=True),
            patch("message_store.get_message_store") as mock_get_store,
        ):
            mock_store = MagicMock()
            mock_store.get_status.return_value = {
                "total": 15,
                "by_type": {"CONSENSUS_PROPOSE": 5, "CONSENSUS_ACK": 5, "CONSENSUS_CONFIRMED": 5},
            }
            mock_get_store.return_value = mock_store

            result = _get_concurrent_status(pipeline)

        assert result is not None
        assert result["messages"]["total"] == 15
        assert result["messages"]["by_type"]["CONSENSUS_PROPOSE"] == 5
        mock_store.get_status.assert_called_once_with("KORE-1234")

    def test_jira_pipeline_consensus_state_returned(self):
        """_get_concurrent_status should return consensus state for JIRA pipelines."""
        pipeline = _make_concurrent_pipeline(pipeline_id="KORE-1234")

        mock_tracker = MagicMock()
        mock_tracker.get_state.return_value = {
            "is_complete": True,
            "blocking_agents": [],
            "agents": {
                "coder": {"producer_phase": "CONFIRMED", "confirmed": True},
            },
            "protocol": "brc",
        }

        with (
            patch("concurrent_executor.is_concurrent_execution", return_value=True),
            patch("message_store.get_message_store") as mock_get_store,
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
        ):
            mock_store = MagicMock()
            mock_store.get_status.return_value = {"total": 0, "by_type": {}}
            mock_get_store.return_value = mock_store

            result = _get_concurrent_status(pipeline)

        assert result is not None
        assert "consensus" in result
        assert result["consensus"]["is_complete"] is True
