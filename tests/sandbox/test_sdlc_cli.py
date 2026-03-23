"""Tests for sandbox/egg_lib/sdlc_cli.py - SSE parsing, rendering, and repo resolution."""

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.sdlc_cli import (
    _resolve_repo_dir,
    _restart_pipeline,
    _write,
    parse_sse_stream,
    render_event_info,
    render_header,
    run_issue_mode,
    run_local_mode,
    watch_pipeline,
)

# ---------------------------------------------------------------------------
# parse_sse_stream tests
# ---------------------------------------------------------------------------


def _make_sse_lines(*frames):
    """Build raw SSE byte lines from (event_type, data_dict) pairs.

    Each frame is encoded as:
        event: <type>\n
        data: <json>\n
        \n  (blank line = end of event)
    """
    lines = []
    for event_type, data in frames:
        if event_type:
            lines.append(f"event: {event_type}\n".encode())
        lines.append(f"data: {json.dumps(data)}\n".encode())
        lines.append(b"\n")
    return lines


class TestParseSseStream:
    def test_single_event(self):
        """A single well-formed SSE event is parsed correctly."""
        raw = _make_sse_lines(("status", {"pipeline_id": "p1", "status": "running"}))
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 1
        assert events[0][0] == "status"
        assert events[0][1]["status"] == "running"

    def test_multiple_events(self):
        """Multiple SSE events in sequence."""
        raw = _make_sse_lines(
            ("status", {"status": "running"}),
            ("status", {"status": "awaiting_human"}),
            ("done", {"reason": "completed"}),
        )
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 3
        assert events[0][1]["status"] == "running"
        assert events[1][1]["status"] == "awaiting_human"
        assert events[2][0] == "done"

    def test_no_event_type_defaults_to_message(self):
        """When no event: line is present, type defaults to 'message'."""
        raw = [b'data: {"key": "val"}\n', b"\n"]
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 1
        assert events[0][0] == "message"
        assert events[0][1]["key"] == "val"

    def test_heartbeat_comments_ignored(self):
        """Lines starting with ':' are SSE comments (heartbeats)."""
        raw = [
            b":heartbeat\n",
            b"event: status\n",
            b'data: {"status": "ok"}\n',
            b"\n",
        ]
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 1
        assert events[0][1]["status"] == "ok"

    def test_multi_line_data(self):
        """Multiple data: lines are joined with newlines."""
        raw = [
            b"event: info\n",
            b'data: {"line1": true,\n',
            b'data:  "line2": false}\n',
            b"\n",
        ]
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 1
        # The data should be the joined lines parsed as JSON
        assert events[0][1]["line1"] is True
        assert events[0][1]["line2"] is False

    def test_invalid_json_returns_raw(self):
        """Non-JSON data is returned with a 'raw' key."""
        raw = [
            b"event: broken\n",
            b"data: not-json\n",
            b"\n",
        ]
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 1
        assert events[0][0] == "broken"
        assert events[0][1]["raw"] == "not-json"

    def test_empty_stream(self):
        """An empty stream yields no events."""
        events = list(parse_sse_stream(iter([])))
        assert events == []

    def test_carriage_return_stripped(self):
        """\\r\\n line endings are handled correctly."""
        raw = [
            b"event: status\r\n",
            b'data: {"ok": true}\r\n',
            b"\r\n",
        ]
        events = list(parse_sse_stream(iter(raw)))
        assert len(events) == 1
        assert events[0][1]["ok"] is True


# ---------------------------------------------------------------------------
# _write tests
# ---------------------------------------------------------------------------


class TestWrite:
    def test_tty_preserves_ansi(self):
        """When writing to a TTY, ANSI codes are preserved."""
        buf = StringIO()
        buf.isatty = lambda: True
        _write("\033[1mBold\033[0m", file=buf)
        assert "\033[1m" in buf.getvalue()

    def test_non_tty_strips_ansi(self):
        """When writing to a non-TTY, ANSI codes are stripped."""
        buf = StringIO()
        buf.isatty = lambda: False
        _write("\033[1mBold\033[0m", file=buf)
        assert "\033[" not in buf.getvalue()
        assert "Bold" in buf.getvalue()


# ---------------------------------------------------------------------------
# Render helper tests
# ---------------------------------------------------------------------------


class TestRenderHeader:
    def test_basic(self):
        header = render_header("issue-42")
        assert "issue-42" in header

    def test_with_event_type(self):
        header = render_header("p1", event_type="status")
        assert "status" in header


class TestRenderEventInfo:
    def test_running_status(self):
        data = {
            "status": "running",
            "current_phase": "implement",
            "timestamp": "2025-01-01T12:34:56Z",
        }
        info = render_event_info(data)
        assert "running" in info
        assert "implement" in info
        assert "12:34:56" in info

    def test_awaiting_human_display(self):
        data = {"status": "awaiting_human", "current_phase": "refine", "pending_decisions": 1}
        info = render_event_info(data)
        assert "awaiting approval" in info
        assert "1 pending decision" in info

    def test_multiple_pending(self):
        data = {"status": "awaiting_human", "current_phase": "plan", "pending_decisions": 3}
        info = render_event_info(data)
        assert "3 pending decisions" in info

    def test_no_pending_decisions(self):
        data = {"status": "complete", "current_phase": "done"}
        info = render_event_info(data)
        assert "pending" not in info


# ---------------------------------------------------------------------------
# _resolve_repo_dir tests
# ---------------------------------------------------------------------------


class TestResolveRepoDir:
    def test_egg_repos_match_with_existing_dir(self, tmp_path, monkeypatch):
        """When EGG_REPOS matches and directory exists, returns owner/repo."""
        repo_dir = tmp_path / "repos" / "myrepo"
        repo_dir.mkdir(parents=True)
        monkeypatch.setenv("EGG_REPOS", "owner/myrepo")
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _resolve_repo_dir("myrepo")
        assert result == "owner/myrepo"

    def test_egg_repos_match_without_existing_dir(self, tmp_path, monkeypatch):
        """When EGG_REPOS matches but directory doesn't exist, returns None."""
        # Don't create the directory — only set EGG_REPOS
        monkeypatch.setenv("EGG_REPOS", "owner/myrepo")
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _resolve_repo_dir("myrepo")
        assert result is None

    def test_no_egg_repos_falls_back_to_gh(self, tmp_path, monkeypatch):
        """When EGG_REPOS is empty, falls back to gh repo view."""
        repo_dir = tmp_path / "repos" / "myrepo"
        repo_dir.mkdir(parents=True)
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="org/myrepo\n")
            result = _resolve_repo_dir("myrepo")
        assert result == "org/myrepo"

    def test_directory_not_found_returns_none(self, tmp_path, monkeypatch):
        """When the directory doesn't exist at all, returns None."""
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _resolve_repo_dir("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# watch_pipeline draft pager integration tests
# ---------------------------------------------------------------------------


class TestWatchPipelineDraftPager:
    """Tests for the pre-decision draft pager display in watch_pipeline."""

    def _make_client(self):
        client = MagicMock()
        client.resolve_decision.return_value = {"status": "resolved"}
        return client

    @patch("egg_lib.sdlc_cli.handle_hitl_checkpoint")
    @patch("egg_lib.sdlc_cli.resolve_phase_draft")
    @patch("egg_lib.sdlc_cli._display_in_pager")
    @patch("egg_lib.sdlc_cli.display_visualization")
    @patch("egg_lib.sdlc_cli.parse_sse_stream")
    def test_draft_shown_once_for_two_decisions(
        self, mock_parse, mock_viz, mock_pager, mock_resolve, mock_hitl
    ):
        """Draft pager is shown once, not repeated for a second decision with same draft."""
        client = self._make_client()
        conn = MagicMock()
        response1 = MagicMock()
        response2 = MagicMock()
        response3 = MagicMock()

        # Three SSE connections: first two have awaiting_human decisions,
        # third completes. After resolving a decision the watch loop breaks
        # the inner loop and reconnects.
        client.stream_pipeline.side_effect = [
            (conn, response1),
            (conn, response2),
            (conn, response3),
        ]

        decision = {
            "id": "d1",
            "question": "Which approach?",
            "decision_type": "choice",
            "options": ["A", "B"],
        }
        decision2 = {
            "id": "d2",
            "question": "Another question?",
            "decision_type": "choice",
            "options": ["X", "Y"],
        }

        # parse_sse_stream is called once per connection with the response
        def parse_side_effect(resp):
            if resp is response1:
                return iter([("status", {"status": "awaiting_human", "pending_decisions": 1})])
            elif resp is response2:
                return iter([("status", {"status": "awaiting_human", "pending_decisions": 1})])
            else:
                return iter([("done", {"reason": "completed"})])

        mock_parse.side_effect = parse_side_effect

        client.list_decisions.side_effect = [[decision], [decision2]]
        mock_resolve.return_value = (".egg-state/drafts/42-analysis.md", "# Analysis", "refine")
        mock_hitl.return_value = "resolved"

        result = watch_pipeline(client, "issue-42", pipeline_mode="issue", issue_number=42)

        assert result == "complete"
        # Pager should be called only ONCE (deduplication by draft_rel)
        assert mock_pager.call_count == 1
        mock_pager.assert_called_with("# Analysis")

    @patch("egg_lib.sdlc_cli.handle_hitl_checkpoint")
    @patch("egg_lib.sdlc_cli.resolve_phase_draft")
    @patch("egg_lib.sdlc_cli._display_in_pager")
    @patch("egg_lib.sdlc_cli.display_visualization")
    @patch("egg_lib.sdlc_cli.parse_sse_stream")
    def test_draft_not_shown_for_phase_gate(
        self, mock_parse, mock_viz, mock_pager, mock_resolve, mock_hitl
    ):
        """Draft pager is NOT shown before phase_gate decisions (handled by hitl handler)."""
        client = self._make_client()
        conn = MagicMock()
        response1 = MagicMock()
        response2 = MagicMock()
        client.stream_pipeline.side_effect = [(conn, response1), (conn, response2)]

        decision = {
            "id": "d1",
            "question": "Approve the analysis?",
            "decision_type": "phase_gate",
        }

        def parse_side_effect(resp):
            if resp is response1:
                return iter([("status", {"status": "awaiting_human", "pending_decisions": 1})])
            else:
                return iter([("done", {"reason": "completed"})])

        mock_parse.side_effect = parse_side_effect
        client.list_decisions.return_value = [decision]
        mock_hitl.return_value = "resolved"

        result = watch_pipeline(client, "issue-42", pipeline_mode="issue", issue_number=42)

        assert result == "complete"
        # resolve_phase_draft should NOT be called for phase_gate
        mock_resolve.assert_not_called()
        mock_pager.assert_not_called()

    @patch("egg_lib.sdlc_cli.handle_hitl_checkpoint")
    @patch("egg_lib.sdlc_cli.resolve_phase_draft")
    @patch("egg_lib.sdlc_cli._display_in_pager")
    @patch("egg_lib.sdlc_cli.display_visualization")
    @patch("egg_lib.sdlc_cli.parse_sse_stream")
    def test_draft_not_shown_when_no_content(
        self, mock_parse, mock_viz, mock_pager, mock_resolve, mock_hitl
    ):
        """Draft pager is not shown when resolve_phase_draft returns no content."""
        client = self._make_client()
        conn = MagicMock()
        response1 = MagicMock()
        response2 = MagicMock()
        client.stream_pipeline.side_effect = [(conn, response1), (conn, response2)]

        decision = {
            "id": "d1",
            "question": "Which approach?",
            "decision_type": "choice",
            "options": ["A", "B"],
        }

        def parse_side_effect(resp):
            if resp is response1:
                return iter([("status", {"status": "awaiting_human", "pending_decisions": 1})])
            else:
                return iter([("done", {"reason": "completed"})])

        mock_parse.side_effect = parse_side_effect
        client.list_decisions.return_value = [decision]
        mock_resolve.return_value = (None, None, "implement")
        mock_hitl.return_value = "resolved"

        result = watch_pipeline(client, "issue-42", pipeline_mode="issue", issue_number=42)

        assert result == "complete"
        mock_pager.assert_not_called()

    @patch("egg_lib.sdlc_cli.handle_hitl_checkpoint")
    @patch("egg_lib.sdlc_cli.resolve_phase_draft")
    @patch("egg_lib.sdlc_cli._display_in_pager")
    @patch("egg_lib.sdlc_cli.display_visualization")
    @patch("egg_lib.sdlc_cli.parse_sse_stream")
    def test_client_passed_to_resolve_phase_draft(
        self, mock_parse, mock_viz, mock_pager, mock_resolve, mock_hitl
    ):
        """The client is passed to resolve_phase_draft for API fallback."""
        client = self._make_client()
        conn = MagicMock()
        response1 = MagicMock()
        response2 = MagicMock()
        client.stream_pipeline.side_effect = [(conn, response1), (conn, response2)]

        decision = {
            "id": "d1",
            "question": "Which approach?",
            "decision_type": "choice",
            "options": ["A", "B"],
        }

        def parse_side_effect(resp):
            if resp is response1:
                return iter([("status", {"status": "awaiting_human", "pending_decisions": 1})])
            else:
                return iter([("done", {"reason": "completed"})])

        mock_parse.side_effect = parse_side_effect
        client.list_decisions.return_value = [decision]
        mock_resolve.return_value = (None, None, "unknown")
        mock_hitl.return_value = "resolved"

        watch_pipeline(client, "issue-42", pipeline_mode="issue", issue_number=42)

        mock_resolve.assert_called_once_with(
            decision,
            pipeline_mode="issue",
            issue_number=42,
            pipeline_id="issue-42",
            client=client,
        )


# ---------------------------------------------------------------------------
# Concurrent config wiring tests
# ---------------------------------------------------------------------------


class TestRestartPipelineConcurrentConfig:
    """Verify _restart_pipeline passes config to create_pipeline."""

    def test_concurrent_config_passed(self):
        client = MagicMock()
        config = {"concurrent_execution": True}
        _restart_pipeline(
            client,
            "issue-1",
            1,
            "owner/repo",
            "egg/issue-1",
            network_mode="public",
            config=config,
        )
        client.create_pipeline.assert_called_once_with(
            issue_number=1,
            repo="owner/repo",
            branch="egg/issue-1",
            base_branch=None,
            network_mode="public",
            config={"concurrent_execution": True},
        )

    def test_no_concurrent_config(self):
        client = MagicMock()
        _restart_pipeline(
            client,
            "issue-2",
            2,
            "owner/repo",
            "egg/issue-2",
            network_mode="private",
        )
        client.create_pipeline.assert_called_once_with(
            issue_number=2,
            repo="owner/repo",
            branch="egg/issue-2",
            base_branch=None,
            network_mode="private",
            config=None,
        )


class TestRunLocalModeConcurrent:
    """Verify run_local_mode passes concurrent config to create_pipeline."""

    @patch("egg_lib.sdlc_cli.watch_pipeline", return_value="complete")
    @patch("egg_lib.sdlc_cli._detect_network_mode", return_value="public")
    def test_concurrent_true(self, _mock_net, _mock_watch):
        client = MagicMock()
        client.create_pipeline.return_value = {"id": "local-1"}
        run_local_mode(client, prompt="Build feature X", repo="owner/repo", concurrent=True)
        _, kwargs = client.create_pipeline.call_args
        assert kwargs["config"] == {"concurrent_execution": True}

    @patch("egg_lib.sdlc_cli.watch_pipeline", return_value="complete")
    @patch("egg_lib.sdlc_cli._detect_network_mode", return_value="public")
    def test_concurrent_false(self, _mock_net, _mock_watch):
        client = MagicMock()
        client.create_pipeline.return_value = {"id": "local-2"}
        run_local_mode(client, prompt="Build feature Y", repo="owner/repo", concurrent=False)
        _, kwargs = client.create_pipeline.call_args
        assert kwargs["config"] is None


class TestRunIssueModeConcurrent:
    """Verify run_issue_mode passes concurrent config to create_pipeline."""

    @patch("egg_lib.sdlc_cli.watch_pipeline", return_value="complete")
    @patch("egg_lib.sdlc_cli._detect_network_mode", return_value="public")
    def test_concurrent_true_creates_with_config(self, _mock_net, _mock_watch):
        client = MagicMock()
        run_issue_mode(client, issue_number=99, repo="owner/repo", concurrent=True)
        _, kwargs = client.create_pipeline.call_args
        assert kwargs["config"] == {"concurrent_execution": True}

    @patch("egg_lib.sdlc_cli.watch_pipeline", return_value="complete")
    @patch("egg_lib.sdlc_cli._detect_network_mode", return_value="public")
    def test_concurrent_false_creates_without_config(self, _mock_net, _mock_watch):
        client = MagicMock()
        run_issue_mode(client, issue_number=100, repo="owner/repo", concurrent=False)
        _, kwargs = client.create_pipeline.call_args
        assert kwargs["config"] is None
