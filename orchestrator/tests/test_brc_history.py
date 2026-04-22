"""
Tests for BRC history persistence: _write_brc_history and the PR-body
one-line pointer to committed transcripts.

Covers:
- _write_brc_history: file creation with BRC messages, no-op on empty store
- _build_pr_body: one-line link to committed brc-history/*.md files (#1828)
- Edge cases: mixed message types, multiple phases
"""

import json
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from message_store import Message, MessageStore, MessageType
from models import Pipeline, PipelinePhase, PipelineStatus


def _make_pipeline(
    pipeline_id="issue-42",
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
):
    """Create a Pipeline for testing."""
    return Pipeline(
        id=pipeline_id,
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


def _make_brc_message(
    pipeline_id="issue-42",
    from_role="coder",
    message_type=MessageType.CONSENSUS_PROPOSE,
    subject="Proposal from coder",
    body="Implemented the feature",
    phase="implement",
    timestamp=None,
    metadata=None,
):
    """Create a BRC Message for testing."""
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        metadata=metadata or {},
    )


def _make_brc_messages(pipeline_id="issue-42", phase="implement"):
    """Create a typical set of BRC messages for a phase."""
    return [
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Proposal from coder",
            body="Implemented auth fix",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="reviewer_code",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK from reviewer_code",
            body="Code looks good",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="tester",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK from tester",
            body="Tests pass",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 10, 0, tzinfo=UTC),
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.CONSENSUS_CONFIRMED,
            subject="Confirmed by coder",
            body="",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 15, 0, tzinfo=UTC),
        ),
    ]


def _make_mixed_messages(pipeline_id="issue-42", phase="implement"):
    """Create messages with both BRC and non-BRC types."""
    return [
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.PROGRESS,
            subject="Working on task",
            body="Starting implementation",
            phase=phase,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Proposal from coder",
            body="Done with implementation",
            phase=phase,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="tester",
            message_type=MessageType.STATUS,
            subject="Test status",
            body="Running tests",
            phase=phase,
        ),
    ]


def _setup_contract(tmp_path, issue_number=42):
    """Set up a contract JSON file in the temp directory."""
    contract_dir = tmp_path / ".egg-state" / "contracts"
    contract_dir.mkdir(parents=True)
    contract_file = contract_dir / f"{issue_number}.json"
    contract_file.write_text(json.dumps(_make_contract_json(issue_number=issue_number)))


class TestWriteBrcHistory:
    """Tests for _write_brc_history."""

    def test_creates_file_with_brc_messages(self, tmp_path):
        """When BRC messages exist, _write_brc_history creates a markdown file."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        expected_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert expected_path.exists(), f"Expected BRC history file at {expected_path}"

        content = expected_path.read_text()
        assert len(content) > 0

    def test_file_contains_chronological_messages(self, tmp_path):
        """BRC history file contains messages in chronological order."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()

        # Verify all BRC message types appear
        assert "CONSENSUS_PROPOSE" in content
        assert "CONSENSUS_ACK" in content
        assert "CONSENSUS_CONFIRMED" in content

        # Verify roles appear
        assert "coder" in content
        assert "reviewer_code" in content
        assert "tester" in content

    def test_no_file_when_store_empty(self, tmp_path):
        """When no BRC messages, _write_brc_history creates no file (no-op)."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == [], "No files should be created for empty store"

    def test_no_file_when_only_non_brc_messages(self, tmp_path):
        """When only non-BRC/non-history messages exist, no history file is created.

        PROGRESS is not in BRC_HISTORY_TYPES, so it is filtered out.
        (STATUS *is* in BRC_HISTORY_TYPES since #1717.)
        """
        from routes.pipelines import _write_brc_history

        non_brc_messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                message_type=MessageType.PROGRESS,
                subject="Working",
                body="Starting",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = non_brc_messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == [], "No files for non-BRC messages"

    def test_filters_only_brc_history_messages(self, tmp_path):
        """Mixed message types: only BRC_HISTORY_TYPES appear in history file.

        Since #1717, CONSENSUS_* and BRC-adjacent types (STATUS, HANDOFF, etc.)
        are included in history. PROGRESS is still excluded.
        """
        from routes.pipelines import _write_brc_history

        messages = _make_mixed_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        expected_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert expected_path.exists()

        content = expected_path.read_text()
        # Should contain the CONSENSUS message
        assert "CONSENSUS_PROPOSE" in content
        # STATUS is now in BRC_HISTORY_TYPES, so it should be included
        assert "STATUS" in content
        # PROGRESS is NOT in BRC_HISTORY_TYPES, so it should be excluded
        assert "PROGRESS" not in content

    def test_creates_directory_if_not_exists(self, tmp_path):
        """_write_brc_history creates the brc-history directory if needed."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        # Ensure no .egg-state directory exists
        assert not (tmp_path / ".egg-state").exists()

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        assert (tmp_path / ".egg-state" / "brc-history").is_dir()

    def test_uses_pipeline_id_as_identifier_fallback(self, tmp_path):
        """When identifier is a string pipeline_id, file uses that as prefix."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="custom-pipeline", phase="refine")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "custom-pipeline", "refine", "custom-pipeline")

        expected_path = tmp_path / ".egg-state" / "brc-history" / "custom-pipeline-refine.md"
        assert expected_path.exists()

    def test_file_contains_phase_header(self, tmp_path):
        """BRC history file contains a header indicating the phase."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # File should contain a header with phase info
        assert "implement" in content.lower()

    def test_includes_nack_messages(self, tmp_path):
        """NACK messages with feedback appear in history."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="First attempt",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK from reviewer_code",
                body="Missing error handling in auth flow",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "CONSENSUS_NACK" in content
        assert "reviewer_code" in content

    def test_includes_re_review_and_withdraw(self, tmp_path):
        """RE_REVIEW and WITHDRAW message types appear in history."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review requested",
                body="Coder updated proposal",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 10, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_WITHDRAW,
                subject="Withdrawn",
                body="Withdrawing previous proposal",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 15, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "CONSENSUS_RE_REVIEW" in content
        assert "CONSENSUS_WITHDRAW" in content

    def test_filters_messages_by_phase(self, tmp_path):
        """Only messages matching the requested phase are included."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Plan proposal",
                body="Planning done",
                phase="plan",
                timestamp=datetime(2026, 4, 8, 11, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Implement proposal",
                body="Implementation done",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK for implement",
                body="Looks good",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # Should contain implement-phase messages
        assert "Implement proposal" in content
        assert "ACK for implement" in content
        # Should NOT contain plan-phase messages
        assert "Plan proposal" not in content

    def test_no_file_when_no_messages_for_requested_phase(self, tmp_path):
        """No file created when BRC messages exist but none match the phase."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="plan")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == [], "No file when no messages match phase"


class TestWriteBrcHistoryEdgeCases:
    """Edge case tests for _write_brc_history."""

    def test_handles_message_store_exception(self, tmp_path):
        """_write_brc_history handles message store errors gracefully."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.side_effect = Exception("Store unavailable")

        with patch("message_store.get_message_store", return_value=mock_store):
            # Should not raise — graceful handling
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # No file should be created on error
        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == []

    def test_multiple_phases_create_separate_files(self, tmp_path):
        """Each phase gets its own history file with only that phase's messages."""
        from routes.pipelines import _write_brc_history

        # All messages in the store (accumulated across phases)
        all_messages = []
        for phase in ["refine", "plan", "implement"]:
            all_messages.extend(_make_brc_messages(pipeline_id="issue-42", phase=phase))

        for phase in ["refine", "plan", "implement"]:
            mock_store = MagicMock(spec=MessageStore)
            mock_store.get_messages.return_value = all_messages

            with patch("message_store.get_message_store", return_value=mock_store):
                _write_brc_history(tmp_path, "issue-42", phase, 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-refine.md").exists()
        assert (history_dir / "42-plan.md").exists()
        assert (history_dir / "42-implement.md").exists()

    def test_messages_with_empty_body(self, tmp_path):
        """Messages with empty body are included but don't break formatting."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        expected_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert expected_path.exists()
        content = expected_path.read_text()
        assert "CONSENSUS_CONFIRMED" in content


class TestWriteBrcHistoryLossless:
    """Tests for lossless message projection in _write_brc_history (#1717)."""

    def test_ack_metadata_round_trips_into_yaml_block(self, tmp_path):
        """ACK artifact_references and ack_version appear in the YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK from reviewer",
                body="Looks good",
                phase="implement",
                metadata={
                    "artifact_references": ["orchestrator/routes/pipelines.py"],
                    "ack_version": 2,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "````yaml" in content
        assert "artifact_references" in content
        assert "orchestrator/routes/pipelines.py" in content
        assert "ack_version" in content

    def test_nack_metadata_reason_and_revision_count_round_trip(self, tmp_path):
        """NACK metadata.payload.reason and revision_count appear in YAML block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK from reviewer",
                body="Missing error handling",
                phase="implement",
                metadata={
                    "payload": {"reason": "Missing error handling"},
                    "revision_count": 3,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "````yaml" in content
        assert "revision_count" in content
        assert "payload" in content
        assert "Missing error handling" in content

    def test_propose_commit_sha_in_metadata(self, tmp_path):
        """PROPOSE commit_sha appears in the YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation done",
                phase="implement",
                metadata={"commit_sha": "abc123def456"},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "commit_sha" in content
        assert "abc123def456" in content

    def test_to_role_shown_for_directed_messages(self, tmp_path):
        """Directed messages (to_role != 'all') show the → to_role in the header."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review",
                body="Coder updated proposal",
                phase="implement",
            ),
        ]
        # Override to_role
        messages[0].to_role = "reviewer_code"

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "→ reviewer_code" in content

    def test_to_role_omitted_for_broadcast(self, tmp_path):
        """Broadcast messages (to_role == 'all') do NOT show → in the header."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Work done",
                phase="implement",
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "→" not in content

    def test_triple_backtick_body_does_not_corrupt_yaml_block(self, tmp_path):
        """Message body containing triple backticks must not break the YAML metadata block.

        Regression test: the YAML metadata is fenced with 4-backtick delimiters
        (````yaml ... ````) so that triple-backtick code fences in the body cannot
        open/close an unexpected fence and corrupt subsequent content.
        """
        from routes.pipelines import _write_brc_history

        body_with_fences = "Here is code:\n```python\nprint('hi')\n```\nDone."
        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal with code",
                body=body_with_fences,
                phase="implement",
                metadata={"version": 1, "commit_sha": "abc123"},
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # The body should appear verbatim
        assert "```python" in content
        assert "print('hi')" in content
        # The YAML block should still be parseable — find all ````yaml ... ```` blocks
        import yaml

        in_yaml = False
        yaml_lines: list[str] = []
        yaml_blocks_found = 0
        for line in content.splitlines():
            if line.strip() == "````yaml":
                in_yaml = True
                yaml_lines = []
            elif line.strip() == "````" and in_yaml:
                in_yaml = False
                yaml_blocks_found += 1
                parsed = yaml.safe_load("\n".join(yaml_lines))
                assert parsed is not None, "YAML block should be parseable"
                assert "metadata" in parsed
                assert parsed["metadata"]["commit_sha"] == "abc123"
            elif in_yaml:
                yaml_lines.append(line)
        assert yaml_blocks_found >= 1, "Should find at least one YAML metadata block"

    def test_handoff_included_in_history(self, tmp_path):
        """HANDOFF messages are included in the history file (BRC_HISTORY_TYPES)."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.HANDOFF,
                subject="Handoff to tester",
                body="Code ready for testing",
                phase="implement",
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "HANDOFF" in content
        assert "Code ready for testing" in content

    def test_overseer_alert_included_in_history(self, tmp_path):
        """OVERSEER_ALERT messages are included in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.OVERSEER_ALERT,
                subject="Alert: agent stall",
                body="Agent coder has not progressed in 10 minutes",
                phase="implement",
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "OVERSEER_ALERT" in content


class TestJsonCompanionFile:
    """Tests for the JSON companion file written by _write_brc_history (#1717)."""

    def test_json_file_written_alongside_md(self, tmp_path):
        """JSON companion file is written next to the .md file."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        assert json_path.exists(), "JSON companion file should exist"

    def test_json_round_trips_to_message_dicts(self, tmp_path):
        """JSON companion deserializes to a list of dicts matching Message.to_dict()."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        assert isinstance(data, list)
        assert len(data) == len(messages)

        # Each entry should have the expected Message fields
        for entry in data:
            assert "id" in entry
            assert "pipeline_id" in entry
            assert "from_role" in entry
            assert "to_role" in entry
            assert "message_type" in entry
            assert "phase" in entry
            assert "metadata" in entry
            assert "timestamp" in entry

    def test_json_includes_non_consensus_types(self, tmp_path):
        """JSON companion includes HANDOFF, AGENT_FAILED, OVERSEER_ALERT, STATUS, NUDGE, QUESTION."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Work",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.HANDOFF,
                subject="Handoff",
                body="Ready",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.AGENT_FAILED,
                subject="Agent failed",
                body="Tester crashed",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.OVERSEER_ALERT,
                subject="Alert",
                body="Stall",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.STATUS,
                subject="Status update",
                body="Working",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.NUDGE,
                subject="Nudge",
                body="Please proceed",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.QUESTION,
                subject="Question",
                body="Need clarification",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        types_in_json = {entry["message_type"] for entry in data}

        assert "CONSENSUS_PROPOSE" in types_in_json
        assert "HANDOFF" in types_in_json
        assert "AGENT_FAILED" in types_in_json
        assert "OVERSEER_ALERT" in types_in_json
        assert "STATUS" in types_in_json
        assert "NUDGE" in types_in_json
        assert "QUESTION" in types_in_json

    def test_json_includes_metadata_fields(self, tmp_path):
        """JSON companion preserves full metadata including artifact_references."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                phase="implement",
                metadata={
                    "artifact_references": ["file1.py", "file2.py"],
                    "ack_version": 2,
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        assert len(data) == 1
        metadata = data[0]["metadata"]
        assert metadata["artifact_references"] == ["file1.py", "file2.py"]
        assert metadata["ack_version"] == 2
        assert metadata["version"] == 1

    def test_json_write_failure_does_not_block_md(self, tmp_path):
        """If JSON write fails, the markdown file is still written."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Work",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        # Make json.dumps raise to simulate a JSON write failure
        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.json.dumps", side_effect=RuntimeError("Serialization failed")),
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        md_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert md_path.exists(), "Markdown file should still be written despite JSON failure"


class TestBrcHistoryTypes:
    """Tests for BRC_HISTORY_TYPES — the set of message types persisted to history files."""

    def test_contains_all_consensus_types(self):
        """BRC_HISTORY_TYPES includes every CONSENSUS_* message type."""
        from routes.pipelines import BRC_HISTORY_TYPES

        consensus_types = {
            "CONSENSUS_PROPOSE",
            "CONSENSUS_ACK",
            "CONSENSUS_NACK",
            "CONSENSUS_WITHDRAW",
            "CONSENSUS_CONFIRMED",
            "CONSENSUS_RE_REVIEW",
        }
        assert consensus_types <= BRC_HISTORY_TYPES

    def test_includes_non_consensus_types(self):
        """BRC_HISTORY_TYPES includes STATUS, HANDOFF, QUESTION, AGENT_FAILED, NUDGE, OVERSEER_ALERT."""
        from routes.pipelines import BRC_HISTORY_TYPES

        non_consensus_types = {
            "STATUS",
            "HANDOFF",
            "QUESTION",
            "AGENT_FAILED",
            "NUDGE",
            "OVERSEER_ALERT",
        }
        assert non_consensus_types <= BRC_HISTORY_TYPES

    def test_progress_not_in_history_types(self):
        """PROGRESS is not BRC-adjacent and must NOT appear in BRC_HISTORY_TYPES."""
        from routes.pipelines import BRC_HISTORY_TYPES

        assert "PROGRESS" not in BRC_HISTORY_TYPES


class TestYamlMetadataRoundTrip:
    """Tests verifying the YAML metadata blocks are valid parseable YAML."""

    def test_yaml_block_is_parseable(self, tmp_path):
        """The fenced YAML block in the markdown file must parse as valid YAML."""
        import yaml
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="Looks good",
                phase="implement",
                metadata={
                    "artifact_references": ["orchestrator/routes/pipelines.py", "tests/test.py"],
                    "ack_version": 2,
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()

        # Extract YAML blocks from fenced code blocks
        in_yaml = False
        yaml_lines: list[str] = []
        for line in content.split("\n"):
            if line.strip() == "````yaml":
                in_yaml = True
                yaml_lines = []
            elif line.strip() == "````" and in_yaml:
                in_yaml = False
                parsed = yaml.safe_load("\n".join(yaml_lines))
                assert isinstance(parsed, dict)
                assert "id" in parsed
                assert "metadata" in parsed
                assert parsed["metadata"]["artifact_references"] == [
                    "orchestrator/routes/pipelines.py",
                    "tests/test.py",
                ]
                assert parsed["metadata"]["ack_version"] == 2
            elif in_yaml:
                yaml_lines.append(line)

    def test_yaml_block_with_nested_metadata(self, tmp_path):
        """YAML block correctly serializes deeply nested metadata."""
        import yaml
        from routes.pipelines import _write_brc_history

        nested_metadata = {
            "payload": {
                "reason": "Missing error handling",
                "files_reviewed": ["a.py", "b.py"],
            },
            "revision_count": 3,
            "commit_sha": "abc123",
            "version": 2,
        }
        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK",
                body="Issues found",
                phase="implement",
                metadata=nested_metadata,
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()

        in_yaml = False
        yaml_lines: list[str] = []
        found_yaml = False
        for line in content.split("\n"):
            if line.strip() == "````yaml":
                in_yaml = True
                yaml_lines = []
            elif line.strip() == "````" and in_yaml:
                in_yaml = False
                parsed = yaml.safe_load("\n".join(yaml_lines))
                # Verify nested structure round-trips correctly
                assert parsed["metadata"]["payload"]["reason"] == "Missing error handling"
                assert parsed["metadata"]["payload"]["files_reviewed"] == ["a.py", "b.py"]
                assert parsed["metadata"]["revision_count"] == 3
                assert parsed["metadata"]["commit_sha"] == "abc123"
                found_yaml = True
            elif in_yaml:
                yaml_lines.append(line)

        assert found_yaml, "Expected at least one YAML block in the output"


class TestMarkdownWriteFailureIsolation:
    """Tests for markdown write failure not blocking JSON write."""

    def test_md_write_failure_does_not_block_json(self, tmp_path):
        """If markdown write fails, the JSON companion is still written."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Work done",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        history_dir = tmp_path / ".egg-state" / "brc-history"
        history_dir.mkdir(parents=True, exist_ok=True)

        # Make the .md file a directory so write_text fails
        md_path = history_dir / "42-implement.md"
        md_path.mkdir()

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # JSON should still be written despite MD failure
        json_path = history_dir / "42-implement.json"
        assert json_path.exists(), "JSON file should be written despite markdown write failure"
        data = json.loads(json_path.read_text())
        assert len(data) == 1
        assert data[0]["message_type"] == "CONSENSUS_PROPOSE"


class TestJsonCompanionEdgeCases:
    """Additional edge case tests for the JSON companion file."""

    def test_no_json_file_when_no_brc_messages(self, tmp_path):
        """JSON companion file is NOT created when there are no BRC messages."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        assert not json_path.exists(), "No JSON file should be created for empty message store"

    def test_multiple_phases_create_separate_json_files(self, tmp_path):
        """Each phase gets its own JSON companion file."""
        from routes.pipelines import _write_brc_history

        all_messages = []
        for phase in ["refine", "plan", "implement"]:
            all_messages.extend(_make_brc_messages(pipeline_id="issue-42", phase=phase))

        for phase in ["refine", "plan", "implement"]:
            mock_store = MagicMock(spec=MessageStore)
            mock_store.get_messages.return_value = all_messages

            with patch("message_store.get_message_store", return_value=mock_store):
                _write_brc_history(tmp_path, "issue-42", phase, 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-refine.json").exists()
        assert (history_dir / "42-plan.json").exists()
        assert (history_dir / "42-implement.json").exists()

        # Each JSON file should only contain messages for that phase
        for phase in ["refine", "plan", "implement"]:
            data = json.loads((history_dir / f"42-{phase}.json").read_text())
            for entry in data:
                assert entry["phase"] == phase, (
                    f"JSON for {phase} contains message from {entry['phase']}"
                )

    def test_json_preserves_to_role_for_directed_messages(self, tmp_path):
        """JSON companion preserves to_role field for directed messages."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review",
                body="Please re-review",
                phase="implement",
            ),
        ]
        messages[0].to_role = "reviewer_code"

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        assert data[0]["to_role"] == "reviewer_code"


class TestHistoryIncludesNonConsensusTypes:
    """Ensure all BRC_HISTORY_TYPES are individually included in history."""

    def test_status_included_in_history(self, tmp_path):
        """STATUS messages are included in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.STATUS,
                subject="All reviewers ACKed",
                body="Ready to confirm",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "STATUS" in content
        assert "Ready to confirm" in content

    def test_nudge_included_in_history(self, tmp_path):
        """NUDGE messages are included in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.NUDGE,
                subject="Nudge: coder",
                body="No progress in 5 minutes",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "NUDGE" in content

    def test_question_included_in_history(self, tmp_path):
        """QUESTION messages are included in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="tester",
                message_type=MessageType.QUESTION,
                subject="Question about auth flow",
                body="Should I test the SSO path?",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "QUESTION" in content
        assert "Should I test the SSO path?" in content

    def test_agent_failed_included_in_history(self, tmp_path):
        """AGENT_FAILED messages are included in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.AGENT_FAILED,
                subject="Agent tester failed",
                body="Container exited with code 1",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "AGENT_FAILED" in content
        assert "Container exited with code 1" in content

    def test_handoff_included_in_history(self, tmp_path):
        """HANDOFF messages are included in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.HANDOFF,
                subject="Handoff to tester",
                body="Implementation ready for review",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "HANDOFF" in content


class TestBuildBrcHistoryLinkLine:
    """Tests for the one-line pointer to committed BRC history transcripts (#1828)."""

    def _touch(self, tmp_path, filename: str) -> None:
        history_dir = tmp_path / ".egg-state" / "brc-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / filename).write_text("stub")

    def test_returns_empty_when_identifier_is_none(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        self._touch(tmp_path, "42-implement.md")
        assert _build_brc_history_link_line(tmp_path, None) == ""

    def test_returns_empty_when_history_dir_missing(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        assert _build_brc_history_link_line(tmp_path, 42) == ""

    def test_returns_empty_when_no_matching_files(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        # File for a different pipeline/identifier
        self._touch(tmp_path, "99-implement.md")
        assert _build_brc_history_link_line(tmp_path, 42) == ""

    def test_links_files_in_canonical_phase_order(self, tmp_path):
        """Phases link in refine → plan → implement → pr order even if files were created otherwise."""
        from routes.pipelines import _build_brc_history_link_line

        # Create deliberately out of order
        self._touch(tmp_path, "42-implement.md")
        self._touch(tmp_path, "42-plan.md")
        self._touch(tmp_path, "42-refine.md")

        result = _build_brc_history_link_line(tmp_path, 42)
        assert result.startswith("_Per-phase BRC transcripts:")
        assert result.endswith("._")
        # Canonical order: refine before plan before implement
        assert result.index("refine") < result.index("plan") < result.index("implement")
        # Link format
        assert "[`plan`](./.egg-state/brc-history/42-plan.md)" in result
        assert "[`refine`](./.egg-state/brc-history/42-refine.md)" in result
        assert "[`implement`](./.egg-state/brc-history/42-implement.md)" in result

    def test_ignores_json_companions(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        self._touch(tmp_path, "42-implement.md")
        self._touch(tmp_path, "42-implement.json")

        result = _build_brc_history_link_line(tmp_path, 42)
        # .json not surfaced as its own phase
        assert ".json" not in result
        assert "[`implement`]" in result

    def test_string_identifier_works(self, tmp_path):
        """Babysit-pr identifiers like 'pr-123-abc1234' glob the corresponding files."""
        from routes.pipelines import _build_brc_history_link_line

        self._touch(tmp_path, "pr-123-abc1234-implement.md")
        self._touch(tmp_path, "pr-123-abc1234-plan.md")
        # Unrelated file for a different identifier must not leak in
        self._touch(tmp_path, "42-refine.md")

        result = _build_brc_history_link_line(tmp_path, "pr-123-abc1234")
        assert "[`plan`](./.egg-state/brc-history/pr-123-abc1234-plan.md)" in result
        assert "[`implement`](./.egg-state/brc-history/pr-123-abc1234-implement.md)" in result
        assert "42-refine" not in result

    def test_unknown_phase_names_sorted_after_canonical(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        self._touch(tmp_path, "42-plan.md")
        self._touch(tmp_path, "42-custom.md")

        result = _build_brc_history_link_line(tmp_path, 42)
        # Canonical phase (plan) must appear before the non-canonical one
        assert result.index("plan") < result.index("custom")


class TestBuildPrBodyBrcLink:
    """Integration tests: _build_pr_body includes the one-line link when transcripts exist."""

    def test_body_includes_link_line_when_history_files_exist(self, tmp_path):
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)
        history_dir = tmp_path / ".egg-state" / "brc-history"
        history_dir.mkdir(parents=True)
        (history_dir / "42-plan.md").write_text("stub")
        (history_dir / "42-implement.md").write_text("stub")

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "_Per-phase BRC transcripts:" in body
        assert "[`plan`](./.egg-state/brc-history/42-plan.md)" in body
        assert "[`implement`](./.egg-state/brc-history/42-implement.md)" in body
        # The dropped inline summary must not reappear
        assert "## BRC Consensus Summary" not in body
        # Existing sections still present
        assert "Authored-by: egg" in body
        assert title == "Fix authentication bypass in login flow"

    def test_body_omits_link_line_when_no_history_files(self, tmp_path):
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        title, body = _build_pr_body(pipeline, tmp_path)

        assert "Per-phase BRC transcripts" not in body
        assert "Authored-by: egg" in body

    def test_link_line_appears_before_authored_by(self, tmp_path):
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)
        history_dir = tmp_path / ".egg-state" / "brc-history"
        history_dir.mkdir(parents=True)
        (history_dir / "42-implement.md").write_text("stub")

        title, body = _build_pr_body(pipeline, tmp_path)

        assert body.index("Per-phase BRC transcripts") < body.index("Authored-by: egg")
