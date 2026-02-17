"""Tests for checkpoint CLI cost subcommand."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

from egg_contracts.checkpoint_cli import (
    _get_checkpoint_repo_from_args,
    cmd_cost,
    create_parser,
    main,
)
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    SessionMetadata,
    SessionStatus,
    TokenUsage,
    TriggerType,
)


def _make_summary(
    checkpoint_id: str,
    pipeline_id: str = "issue-745",
    phase: str = "implement",
    agent_type: AgentType = AgentType.CODER,
    total_tokens: int = 10000,
) -> CheckpointSummaryV2:
    """Create a checkpoint summary for testing."""
    return CheckpointSummaryV2(
        id=checkpoint_id,
        trigger_type=TriggerType.SESSION_END,
        session_status=SessionStatus.COMPLETED,
        session_id="session-1",
        pipeline_id=pipeline_id,
        pipeline_phase=phase,
        agent_type=agent_type,
        total_tokens=total_tokens,
        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


def _make_checkpoint(
    checkpoint_id: str,
    phase: str = "implement",
    agent_type: AgentType = AgentType.CODER,
    input_tokens: int = 8000,
    output_tokens: int = 2000,
    model: str = "claude-opus-4-5-20251101",
) -> CheckpointV2:
    """Create a full checkpoint for testing."""
    return CheckpointV2(
        id=checkpoint_id,
        trigger_type=TriggerType.SESSION_END,
        session_status=SessionStatus.COMPLETED,
        session_id="session-1",
        pipeline_phase=phase,
        pipeline_id="issue-745",
        agent_type=agent_type,
        session=SessionMetadata(
            session_id="session-1",
            started_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            model=model,
        ),
        token_usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            total_tokens=input_tokens + output_tokens,
        ),
        created_at=datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC),
        session_started_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


class TestCostCommand:
    """Tests for the cost subcommand."""

    def test_cost_parser_registered(self):
        """cost subcommand is registered in the parser."""
        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        assert args.command == "cost"
        assert args.pipeline == "issue-745"

    def test_cost_parser_all_filters(self):
        """cost subcommand accepts --pipeline, --issue, --pr filters."""
        parser = create_parser()
        args = parser.parse_args(["cost", "--issue", "745", "--pr", "42"])
        assert args.issue == 745
        assert args.pr == 42

    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_aggregation(self, mock_ref, mock_index, mock_filter, mock_load, capsys):
        """cost subcommand aggregates token usage by phase and agent."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [
            _make_summary("ckpt-aaa11111", phase="plan", agent_type=AgentType.ARCHITECT),
            _make_summary("ckpt-bbb22222", phase="implement", agent_type=AgentType.CODER),
            _make_summary("ckpt-ccc33333", phase="implement", agent_type=AgentType.TESTER),
        ]
        mock_filter.return_value = summaries

        checkpoints = [
            _make_checkpoint(
                "ckpt-aaa11111",
                phase="plan",
                agent_type=AgentType.ARCHITECT,
                input_tokens=10000,
                output_tokens=5000,
            ),
            _make_checkpoint(
                "ckpt-bbb22222",
                phase="implement",
                agent_type=AgentType.CODER,
                input_tokens=50000,
                output_tokens=20000,
            ),
            _make_checkpoint(
                "ckpt-ccc33333",
                phase="implement",
                agent_type=AgentType.TESTER,
                input_tokens=20000,
                output_tokens=8000,
            ),
        ]
        mock_load.side_effect = checkpoints

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "Pipeline: issue-745" in output
        assert "Checkpoints: 3" in output
        assert "architect" in output
        assert "coder" in output
        assert "tester" in output
        assert "TOTAL" in output

    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_json_output(self, mock_ref, mock_index, mock_filter, mock_load, capsys):
        """cost --json outputs structured JSON with breakdown."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [
            _make_summary("ckpt-ddd44444", phase="implement", agent_type=AgentType.CODER),
        ]
        mock_filter.return_value = summaries

        checkpoint = _make_checkpoint(
            "ckpt-ddd44444",
            input_tokens=100000,
            output_tokens=40000,
        )
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745", "--json"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["pipeline_id"] == "issue-745"
        assert data["checkpoint_count"] == 1
        assert data["total_input_tokens"] == 100000
        assert data["total_output_tokens"] == 40000
        assert data["total_cost_usd"] > 0
        assert len(data["breakdown"]) == 1
        assert data["breakdown"][0]["phase"] == "implement"
        assert data["breakdown"][0]["agent"] == "coder"

    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_no_checkpoints(self, mock_ref, capsys):
        """cost returns 0 with message when no checkpoint branch exists."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints found" in output

    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_skips_checkpoints_without_token_usage(
        self, mock_ref, mock_index, mock_filter, mock_load, capsys
    ):
        """Checkpoints without token_usage are skipped in cost calculation."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [
            _make_summary("ckpt-eee55555"),
        ]
        mock_filter.return_value = summaries

        # Checkpoint with no token_usage
        checkpoint = _make_checkpoint("ckpt-eee55555")
        checkpoint.token_usage = None
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints with token usage data found" in output

    def test_cost_via_main(self):
        """cost subcommand is reachable via main()."""
        with patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref") as mock_ref:
            mock_ref.return_value = None
            result = main(["cost", "--pipeline", "issue-745"])
            assert result == 0


class TestGetCheckpointRepoFromArgs:
    """Tests for auto-detection of checkpoint_repo from repo config."""

    def _make_args(self, **kwargs) -> argparse.Namespace:
        defaults = {"checkpoint_repo": None, "repo_path": "/tmp/test-repo"}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_explicit_flag_takes_priority(self):
        """--checkpoint-repo flag is returned without auto-detection."""
        args = self._make_args(checkpoint_repo="owner/explicit-repo")
        assert _get_checkpoint_repo_from_args(args) == "owner/explicit-repo"

    @patch("config.repo_config.get_checkpoint_repo", return_value="jwbron/egg-checkpoints")
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_auto_detects_from_https_remote(self, mock_git, mock_config):
        """Auto-detects checkpoint_repo from HTTPS remote URL."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/jwbron/egg.git\n",
            stderr="",
        )
        args = self._make_args()
        result = _get_checkpoint_repo_from_args(args)
        assert result == "jwbron/egg-checkpoints"
        mock_config.assert_called_once_with("jwbron/egg")

    @patch("config.repo_config.get_checkpoint_repo", return_value="jwbron/egg-checkpoints")
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_auto_detects_from_ssh_remote(self, mock_git, mock_config):
        """Auto-detects checkpoint_repo from SSH remote URL."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="git@github.com:jwbron/egg.git\n",
            stderr="",
        )
        args = self._make_args()
        result = _get_checkpoint_repo_from_args(args)
        assert result == "jwbron/egg-checkpoints"
        mock_config.assert_called_once_with("jwbron/egg")

    @patch("config.repo_config.get_checkpoint_repo", return_value=None)
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_no_config(self, mock_git, mock_config):
        """Returns None when repo has no checkpoint_repo configured."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
            stderr="",
        )
        args = self._make_args()
        assert _get_checkpoint_repo_from_args(args) is None

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_git_remote_fails(self, mock_git):
        """Returns None when git remote get-url fails."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="fatal: not a git repo",
        )
        args = self._make_args()
        assert _get_checkpoint_repo_from_args(args) is None

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_remote_url_not_github(self, mock_git):
        """Returns None when remote URL is not a GitHub URL."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://gitlab.com/owner/repo.git\n",
            stderr="",
        )
        args = self._make_args()
        assert _get_checkpoint_repo_from_args(args) is None

    @patch("egg_contracts.checkpoint_cli.run_git", side_effect=Exception("timeout"))
    def test_returns_none_on_unexpected_error(self, mock_git):
        """Returns None gracefully on unexpected exceptions."""
        args = self._make_args()
        assert _get_checkpoint_repo_from_args(args) is None
