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


# Default slice_id stamped onto implement-phase BRC messages by the test
# helpers below. Issue #2548 hard-switchover: ``_write_brc_history`` drops
# implement-phase BRC messages without a ``metadata['slice_id']`` with a
# warning, so every fixture in this module must seed one. Tests that want
# to exercise the missing-slice_id WARNING path explicitly pass
# ``slice_id=None`` (or omit ``slice_id`` from the override metadata).
_DEFAULT_IMPLEMENT_SLICE_ID = "slice-1"


def _make_brc_message(
    pipeline_id="issue-42",
    from_role="coder",
    message_type=MessageType.CONSENSUS_PROPOSE,
    subject="Proposal from coder",
    body="Implemented the feature",
    phase="implement",
    timestamp=None,
    metadata=None,
    slice_id="__default__",
):
    """Create a BRC Message for testing.

    ``slice_id`` is merged into ``metadata`` for implement-phase messages so
    tests can rely on the post-#2548 hard-switchover writer producing per-slice
    files. Pass ``slice_id=None`` to omit it (used by the missing-slice_id
    WARNING regression test). When ``metadata`` already contains a
    ``slice_id`` key it wins (caller intent).
    """
    md = dict(metadata or {})
    if slice_id == "__default__":
        # Default policy: implement phase auto-stamps slice-1 unless metadata
        # already supplies one; non-implement phases never auto-stamp.
        if phase == "implement" and "slice_id" not in md:
            md["slice_id"] = _DEFAULT_IMPLEMENT_SLICE_ID
    elif slice_id is not None:
        md.setdefault("slice_id", slice_id)
    # slice_id=None and metadata lacks "slice_id" -> intentionally unattributed
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        metadata=md,
    )


def _make_brc_messages(pipeline_id="issue-42", phase="implement", slice_id="__default__"):
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
            slice_id=slice_id,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="reviewer_code",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK from reviewer_code",
            body="Code looks good",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            slice_id=slice_id,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="tester",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK from tester",
            body="Tests pass",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 10, 0, tzinfo=UTC),
            slice_id=slice_id,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.CONSENSUS_CONFIRMED,
            subject="Confirmed by coder",
            body="",
            phase=phase,
            timestamp=datetime(2026, 4, 8, 12, 15, 0, tzinfo=UTC),
            slice_id=slice_id,
        ),
    ]


def _make_mixed_messages(pipeline_id="issue-42", phase="implement", slice_id="__default__"):
    """Create messages with both BRC and non-BRC types."""
    return [
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.PROGRESS,
            subject="Working on task",
            body="Starting implementation",
            phase=phase,
            slice_id=slice_id,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Proposal from coder",
            body="Done with implementation",
            phase=phase,
            slice_id=slice_id,
        ),
        _make_brc_message(
            pipeline_id=pipeline_id,
            from_role="tester",
            message_type=MessageType.STATUS,
            subject="Test status",
            body="Running tests",
            phase=phase,
            slice_id=slice_id,
        ),
    ]


def _implement_path(tmp_path, identifier="42", suffix=".md", slice_id=_DEFAULT_IMPLEMENT_SLICE_ID):
    """Resolve the canonical per-slice implement-phase BRC history path (#2548)."""
    return tmp_path / ".egg-state" / "brc-history" / f"{identifier}-implement-{slice_id}{suffix}"


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

        expected_path = _implement_path(tmp_path)
        assert expected_path.exists(), f"Expected BRC history file at {expected_path}"

        content = expected_path.read_text()
        assert len(content) > 0
        # #2548 hard switchover: aggregate file MUST NOT be produced.
        aggregate = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert not aggregate.exists(), "Aggregate implement file leaked through hard switchover"

    def test_file_contains_chronological_messages(self, tmp_path):
        """BRC history file contains messages in chronological order."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = _implement_path(tmp_path).read_text()

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

        expected_path = _implement_path(tmp_path)
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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
        # #2548: implement phase emits a per-slice file, not the aggregate.
        assert (history_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md").exists()
        assert not (history_dir / "42-implement.md").exists(), (
            "Aggregate implement.md leaked through hard switchover"
        )

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

        expected_path = _implement_path(tmp_path)
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
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

        json_path = _implement_path(tmp_path, suffix=".json")
        assert json_path.exists(), "JSON companion file should exist"

    def test_json_round_trips_to_message_dicts(self, tmp_path):
        """JSON companion deserializes to a list of dicts matching Message.to_dict()."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = _implement_path(tmp_path, suffix=".json")
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
        """JSON companion includes HANDOFF, AGENT_FAILED, OVERSEER_ALERT, STATUS, NUDGE.

        Issue #1897 removed QUESTION from BRC_HISTORY_TYPES (plan TASK-7-2);
        agents now use the NACK ``--reason`` channel or HEARTBEAT
        WAITING_ON_ROLE instead. A standalone test below
        (``test_question_not_in_history_types``) asserts the removal.
        """
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
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = _implement_path(tmp_path, suffix=".json")
        data = json.loads(json_path.read_text())
        types_in_json = {entry["message_type"] for entry in data}

        assert "CONSENSUS_PROPOSE" in types_in_json
        assert "HANDOFF" in types_in_json
        assert "AGENT_FAILED" in types_in_json
        assert "OVERSEER_ALERT" in types_in_json
        assert "STATUS" in types_in_json
        assert "NUDGE" in types_in_json

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

        json_path = _implement_path(tmp_path, suffix=".json")
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

        md_path = _implement_path(tmp_path)
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
        """BRC_HISTORY_TYPES includes STATUS, HANDOFF, AGENT_FAILED, NUDGE, OVERSEER_ALERT.

        Issue #1897 removed QUESTION from BRC_HISTORY_TYPES (plan TASK-7-2);
        see ``test_question_not_in_history_types`` for the removal regression
        guard. Agents now encode questions via NACK ``--reason`` sections or
        HEARTBEAT ``WAITING_ON_ROLE`` metadata.
        """
        from routes.pipelines import BRC_HISTORY_TYPES

        non_consensus_types = {
            "STATUS",
            "HANDOFF",
            "AGENT_FAILED",
            "NUDGE",
            "OVERSEER_ALERT",
        }
        assert non_consensus_types <= BRC_HISTORY_TYPES

    def test_progress_not_in_history_types(self):
        """PROGRESS is not BRC-adjacent and must NOT appear in BRC_HISTORY_TYPES."""
        from routes.pipelines import BRC_HISTORY_TYPES

        assert "PROGRESS" not in BRC_HISTORY_TYPES

    def test_question_not_in_history_types(self):
        """Plan TASK-7-2 acceptance (b): QUESTION was removed from
        BRC_HISTORY_TYPES as part of issue #1897.

        This is an explicit regression guard so a future refactor that
        re-introduces QUESTION (e.g., copy-pasting from an older enum
        definition) fails at test time rather than silently shipping.
        Callers that previously used QUESTION should encode the question
        via ``CONSENSUS_NACK --reason`` (for reviewers) or
        ``HEARTBEAT metadata.state=WAITING_ON_ROLE`` (for producers).
        """
        from routes.pipelines import BRC_HISTORY_TYPES

        assert "QUESTION" not in BRC_HISTORY_TYPES, (
            "QUESTION was removed from BRC_HISTORY_TYPES in #1897; "
            "if it's back, check the Phase 7 rollback path."
        )


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

        content = _implement_path(tmp_path).read_text()

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

        content = _implement_path(tmp_path).read_text()

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
        md_path = history_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        md_path.mkdir()

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # JSON should still be written despite MD failure
        json_path = history_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.json"
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

        json_path = _implement_path(tmp_path, suffix=".json")
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
        # #2548: implement is per-slice; aggregate JSON must NOT exist.
        assert (history_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.json").exists()
        assert not (history_dir / "42-implement.json").exists(), (
            "Aggregate implement.json leaked through hard switchover"
        )

        # Each JSON file should only contain messages for that phase
        for phase, fname in [
            ("refine", "42-refine.json"),
            ("plan", "42-plan.json"),
            ("implement", f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.json"),
        ]:
            data = json.loads((history_dir / fname).read_text())
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

        json_path = _implement_path(tmp_path, suffix=".json")
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
        assert "NUDGE" in content

    def test_status_replaces_removed_question_type(self, tmp_path):
        """STATUS messages (as the QUESTION replacement) are included in
        the history file.

        Issue #1897 removed QUESTION (plan TASK-7-2). Clarifying questions
        between agents now flow through either STATUS (for coordination
        pings) or NACK ``--reason`` blocks (for review-time questions).
        This test uses a STATUS message with question-like content as a
        concrete example of the replacement pattern.
        """
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="tester",
                message_type=MessageType.STATUS,
                subject="Question about auth flow",
                body="Should I test the SSO path?",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = _implement_path(tmp_path).read_text()
        assert "STATUS" in content
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

        content = _implement_path(tmp_path).read_text()
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

        content = _implement_path(tmp_path).read_text()
        assert "HANDOFF" in content


class TestBuildBrcHistoryLinkLine:
    """Tests for the one-line pointer to committed BRC history transcripts (#1828)."""

    def _touch(self, tmp_path, filename: str) -> None:
        history_dir = tmp_path / ".egg-state" / "brc-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / filename).write_text("stub")

    def test_returns_empty_when_identifier_is_none(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        self._touch(tmp_path, f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md")
        assert _build_brc_history_link_line(tmp_path, None) == ""

    def test_returns_empty_when_history_dir_missing(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        assert _build_brc_history_link_line(tmp_path, 42) == ""

    def test_returns_empty_when_no_matching_files(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        # File for a different pipeline/identifier (#2548: per-slice form).
        self._touch(tmp_path, f"99-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md")
        assert _build_brc_history_link_line(tmp_path, 42) == ""

    def test_links_files_in_canonical_phase_order(self, tmp_path):
        """Phases link in refine → plan → implement order even if files were created otherwise.

        After #2548, implement-phase BRC files are per-slice (e.g.
        ``42-implement-slice-1.md``); the link-line builder treats the suffix
        after the identifier as the phase label, so the slice file appears as
        ``implement-slice-1`` rather than ``implement``. The canonical phases
        ``refine``/``plan``/``pr`` still sort before any non-canonical name —
        which now includes the slice suffix.
        """
        from routes.pipelines import _build_brc_history_link_line

        # Create deliberately out of order. Implement phase uses the per-slice
        # form (#2548 hard switchover) — there is no aggregate ``42-implement.md``.
        impl_file = f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        self._touch(tmp_path, impl_file)
        self._touch(tmp_path, "42-plan.md")
        self._touch(tmp_path, "42-refine.md")

        result = _build_brc_history_link_line(tmp_path, 42)
        assert result.startswith("_Per-phase BRC transcripts:")
        assert result.endswith("._")
        # Canonical order: refine before plan before implement(-slice-N).
        assert result.index("refine") < result.index("plan") < result.index("implement")
        # Link format — refine/plan unchanged, implement now includes slice suffix.
        assert "[`plan`](./.egg-state/brc-history/42-plan.md)" in result
        assert "[`refine`](./.egg-state/brc-history/42-refine.md)" in result
        assert (
            f"[`implement-{_DEFAULT_IMPLEMENT_SLICE_ID}`](./.egg-state/brc-history/{impl_file})"
        ) in result

    def test_ignores_json_companions(self, tmp_path):
        from routes.pipelines import _build_brc_history_link_line

        self._touch(tmp_path, f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md")
        self._touch(tmp_path, f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.json")

        result = _build_brc_history_link_line(tmp_path, 42)
        # .json not surfaced as its own phase
        assert ".json" not in result
        assert f"[`implement-{_DEFAULT_IMPLEMENT_SLICE_ID}`]" in result

    def test_string_identifier_works(self, tmp_path):
        """PR-targeting identifiers like 'pr-123-abc1234' glob the corresponding files."""
        from routes.pipelines import _build_brc_history_link_line

        impl_file = f"pr-123-abc1234-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        self._touch(tmp_path, impl_file)
        self._touch(tmp_path, "pr-123-abc1234-plan.md")
        # Unrelated file for a different identifier must not leak in
        self._touch(tmp_path, "42-refine.md")

        result = _build_brc_history_link_line(tmp_path, "pr-123-abc1234")
        assert "[`plan`](./.egg-state/brc-history/pr-123-abc1234-plan.md)" in result
        assert (
            f"[`implement-{_DEFAULT_IMPLEMENT_SLICE_ID}`](./.egg-state/brc-history/{impl_file})"
        ) in result
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
        # #2548: implement is per-slice — the aggregate file is gone.
        impl_file = f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        (history_dir / impl_file).write_text("stub")

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "_Per-phase BRC transcripts:" in body
        assert "[`plan`](./.egg-state/brc-history/42-plan.md)" in body
        assert (
            f"[`implement-{_DEFAULT_IMPLEMENT_SLICE_ID}`](./.egg-state/brc-history/{impl_file})"
        ) in body
        # The dropped inline summary must not reappear
        assert "## BRC Consensus Summary" not in body
        # Existing sections still present
        assert "Authored-by: egg" in body
        assert title == "Fix authentication bypass in login flow"

    def test_body_omits_link_line_when_no_history_files(self, tmp_path):
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Per-phase BRC transcripts" not in body
        assert "Authored-by: egg" in body

    def test_link_line_appears_before_authored_by(self, tmp_path):
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)
        history_dir = tmp_path / ".egg-state" / "brc-history"
        history_dir.mkdir(parents=True)
        # #2548: per-slice implement file replaces the aggregate.
        (history_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md").write_text("stub")

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert body.index("Per-phase BRC transcripts") < body.index("Authored-by: egg")


# ---------------------------------------------------------------------------
# Per-slice implement-phase BRC history (#2548 slice-2)
# ---------------------------------------------------------------------------


class TestPerSliceImplementBrcHistory:
    """Implement-phase ``_write_brc_history`` partitions BRC messages by
    ``metadata['slice_id']`` and writes one file per slice (#2548 slice-2,
    hard switchover under D4 — no aggregate ``<id>-implement.{md,json}`` is
    produced).

    These tests pin the per-slice partitioning contract end-to-end:
    multi-slice fan-out, file-naming shape, message routing into the right
    bucket, and the no-aggregate-file invariant that the planner explicitly
    called out as the slice's most observable acceptance criterion.
    """

    def _make_implement_msgs(self, slice_id, *, body_prefix="work"):
        """Build a 4-message PROPOSE/ACK/ACK/CONFIRMED BRC quartet for *slice_id*."""
        return _make_brc_messages(pipeline_id="issue-42", phase="implement", slice_id=slice_id)

    def test_writes_one_file_per_slice_no_aggregate(self, tmp_path):
        """Two slices' worth of implement BRC messages produce two per-slice
        files and no aggregate ``42-implement.{md,json}``."""
        from routes.pipelines import _write_brc_history

        messages = []
        # Distinct timestamps so the two buckets render in stable order.
        for i, sid in enumerate(["slice-1", "slice-2"]):
            for j, m in enumerate(self._make_implement_msgs(sid)):
                # Disambiguate timestamps so renderer ordering is stable.
                m.timestamp = datetime(2026, 4, 8, 12, i * 30 + j, 0, tzinfo=UTC)
                messages.append(m)

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        slice1_md = history_dir / "42-implement-slice-1.md"
        slice2_md = history_dir / "42-implement-slice-2.md"
        slice1_json = history_dir / "42-implement-slice-1.json"
        slice2_json = history_dir / "42-implement-slice-2.json"

        # Per-slice files exist, both .md and .json.
        assert slice1_md.exists(), "slice-1 markdown file missing"
        assert slice2_md.exists(), "slice-2 markdown file missing"
        assert slice1_json.exists(), "slice-1 JSON companion missing"
        assert slice2_json.exists(), "slice-2 JSON companion missing"

        # Aggregate file MUST NOT exist (hard switchover, D4).
        assert not (history_dir / "42-implement.md").exists(), (
            "Aggregate 42-implement.md leaked through hard switchover"
        )
        assert not (history_dir / "42-implement.json").exists(), (
            "Aggregate 42-implement.json leaked through hard switchover"
        )

        # And there should be exactly the expected per-slice files plus the
        # JSON companions — no other implement-phase artifacts.
        produced = sorted(p.name for p in history_dir.glob("42-implement*"))
        assert produced == [
            "42-implement-slice-1.json",
            "42-implement-slice-1.md",
            "42-implement-slice-2.json",
            "42-implement-slice-2.md",
        ], f"Unexpected files: {produced}"

    def test_each_slice_file_contains_only_its_own_messages(self, tmp_path):
        """The slice-1 file must NOT contain any slice-2 messages and vice
        versa — partitioning must isolate the buckets."""
        from routes.pipelines import _write_brc_history

        messages = []
        for sid, marker in [("slice-1", "alpha-marker"), ("slice-2", "beta-marker")]:
            for m in self._make_implement_msgs(sid):
                if m.message_type == MessageType.CONSENSUS_PROPOSE:
                    m.body = f"{marker} body"
                messages.append(m)

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        slice1_md = (history_dir / "42-implement-slice-1.md").read_text()
        slice2_md = (history_dir / "42-implement-slice-2.md").read_text()

        # Each marker lands ONLY in its own slice's file.
        assert "alpha-marker" in slice1_md
        assert "alpha-marker" not in slice2_md, "alpha-marker leaked into slice-2"
        assert "beta-marker" in slice2_md
        assert "beta-marker" not in slice1_md, "beta-marker leaked into slice-1"

        # And the JSON companions match the same partitioning.
        slice1_json = json.loads((history_dir / "42-implement-slice-1.json").read_text())
        slice2_json = json.loads((history_dir / "42-implement-slice-2.json").read_text())
        assert all(entry["metadata"].get("slice_id") == "slice-1" for entry in slice1_json)
        assert all(entry["metadata"].get("slice_id") == "slice-2" for entry in slice2_json)

    def test_single_slice_still_uses_per_slice_filename(self, tmp_path):
        """Even a single-slice pipeline writes ``-implement-slice-1.{md,json}``
        — there is no fallback to the aggregate filename when only one slice
        exists. (Hard switchover — no special-case for N=1.)"""
        from routes.pipelines import _write_brc_history

        messages = self._make_implement_msgs("slice-1")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-implement-slice-1.md").exists()
        assert (history_dir / "42-implement-slice-1.json").exists()
        assert not (history_dir / "42-implement.md").exists()
        assert not (history_dir / "42-implement.json").exists()

    def test_per_slice_file_carries_slice_label_in_header(self, tmp_path):
        """Each per-slice file's ``# BRC Consensus History`` header includes
        the slice_id so a reviewer scanning the markdown knows which slice
        the consensus belongs to."""
        from routes.pipelines import _write_brc_history

        messages = self._make_implement_msgs("slice-7")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement-slice-7.md").read_text()
        assert "# BRC Consensus History" in content
        assert "slice-7" in content, "Slice label missing from per-slice file header"
        assert "implement" in content.lower()

    def test_messages_without_slice_id_dropped_with_warning(self, tmp_path):
        """Implement-phase BRC messages that lack ``metadata['slice_id']``
        are silently dropped from the on-disk history (hard switchover) and
        a single aggregate WARNING is emitted naming the dropped count."""
        from routes.pipelines import _write_brc_history

        # Mix attributed and unattributed messages.
        attributed = self._make_implement_msgs("slice-1")
        unattributed = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Stray PROPOSE",
                body="missing slice_id",
                phase="implement",
                slice_id=None,  # <-- intentionally omit metadata.slice_id
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="Stray NACK",
                body="missing slice_id",
                phase="implement",
                slice_id=None,
            ),
        ]
        for m in unattributed:
            assert "slice_id" not in m.metadata, "fixture leak — slice_id was stamped"

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = attributed + unattributed

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # The slice-1 file exists and contains ONLY the attributed PROPOSE
        # body; the unattributed Stray PROPOSE/NACK must NOT be present.
        slice1_md = tmp_path / ".egg-state" / "brc-history" / "42-implement-slice-1.md"
        assert slice1_md.exists()
        content = slice1_md.read_text()
        assert "missing slice_id" not in content, "Unattributed message leaked into slice-1 file"
        assert "Stray PROPOSE" not in content
        assert "Stray NACK" not in content

        # An aggregate file must STILL not exist, even though there were
        # unattributed messages — the writer never falls back.
        assert not (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").exists()
        assert not (tmp_path / ".egg-state" / "brc-history" / "42-implement.json").exists()

        # A single warning was emitted with the dropped count.
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "slice_id" in str(c) or "without metadata" in str(c)
        ]
        assert len(warning_calls) >= 1, (
            f"Expected a warning about dropped messages, got: {mock_logger.warning.call_args_list}"
        )
        # The warning surfaces the dropped count so an operator knows scale.
        kwargs = warning_calls[0][1]
        assert kwargs.get("dropped_count") == 2

    def test_all_messages_unattributed_writes_aggregate_fallback(self, tmp_path):
        """When EVERY implement-phase BRC message lacks ``slice_id``, the
        writer falls back to the aggregate ``{identifier}-implement.{md,json}``
        filename so non-slice pipelines (CUSTOM+PR) keep producing an
        artifact.

        Surfaced as v2-NACK reviewer_code_holistic finding #2 (#2548): v2
        dropped the entire BRC stream for non-slice pipelines; v3 falls
        back to aggregate when no message carries a canonical slice_id.
        """
        from routes.pipelines import _write_brc_history

        unattributed = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject=f"Stray {i}",
                body="no slice_id",
                phase="implement",
                slice_id=None,
            )
            for i in range(3)
        ]
        for m in unattributed:
            assert "slice_id" not in m.metadata

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = unattributed

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # Fallback: aggregate IS produced when no slice_id anywhere.
        assert (history_dir / "42-implement.md").exists(), (
            "Fallback: aggregate file MUST be written when no message carries slice_id"
        )
        assert (history_dir / "42-implement.json").exists()
        # And NO per-slice files were produced.
        per_slice = list(history_dir.glob("42-implement-*.md"))
        assert per_slice == [], f"Fallback must NOT write per-slice files, got: {per_slice}"

    def test_aggregate_fallback_contains_all_messages(self, tmp_path):
        """The aggregate fallback contains every (BRC-eligible)
        implement-phase message — no message is silently dropped just
        because no message in the bucket happened to carry slice_id."""
        from routes.pipelines import _write_brc_history

        unattributed = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="PR-PROPOSE",
                body="alpha",
                phase="implement",
                slice_id=None,
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="PR-ACK",
                body="beta",
                phase="implement",
                slice_id=None,
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = unattributed

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "alpha" in content, "Aggregate dropped the PROPOSE body"
        assert "beta" in content, "Aggregate dropped the ACK body"
        assert "PR-PROPOSE" in content
        assert "PR-ACK" in content

    def test_refine_phase_keeps_aggregate_filename(self, tmp_path):
        """Regression: refine phase still writes the aggregate
        ``42-refine.{md,json}`` and does NOT write a per-slice file even when
        messages happen to carry ``metadata['slice_id']``."""
        from routes.pipelines import _write_brc_history

        # Refine messages with a slice_id leftover (defensive — should be
        # ignored for non-implement phases).
        messages = _make_brc_messages(pipeline_id="issue-42", phase="refine", slice_id="slice-1")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "refine", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-refine.md").exists(), "Refine aggregate .md missing"
        assert (history_dir / "42-refine.json").exists(), "Refine aggregate .json missing"
        # No per-slice refine file should exist.
        assert not (history_dir / "42-refine-slice-1.md").exists(), (
            "Refine phase must not partition by slice"
        )
        assert not (history_dir / "42-refine-slice-1.json").exists(), (
            "Refine phase must not partition by slice"
        )

    def test_plan_phase_keeps_aggregate_filename(self, tmp_path):
        """Regression: plan phase still writes the aggregate
        ``42-plan.{md,json}`` (only implement is per-slice — D4)."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="plan", slice_id="slice-1")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "plan", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-plan.md").exists()
        assert (history_dir / "42-plan.json").exists()
        assert not (history_dir / "42-plan-slice-1.md").exists()
        assert not (history_dir / "42-plan-slice-1.json").exists()

    def test_pr_phase_keeps_aggregate_filename(self, tmp_path):
        """Regression: pr phase still writes the aggregate
        ``42-pr.{md,json}``. The contract carved out implement only."""
        from routes.pipelines import _write_brc_history

        messages = _make_brc_messages(pipeline_id="issue-42", phase="pr", slice_id="slice-1")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "pr", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-pr.md").exists()
        assert (history_dir / "42-pr.json").exists()
        assert not (history_dir / "42-pr-slice-1.md").exists()
        assert not (history_dir / "42-pr-slice-1.json").exists()

    def test_partial_attribution_only_attributed_messages_get_files(self, tmp_path):
        """Mix of slice-1, slice-2, and unattributed messages: per-slice
        files exist for slice-1 and slice-2, no aggregate, unattributed are
        dropped with a single warning."""
        from routes.pipelines import _write_brc_history

        slice1_msgs = self._make_implement_msgs("slice-1")
        slice2_msgs = self._make_implement_msgs("slice-2")
        # One unattributed message.
        stray = _make_brc_message(
            pipeline_id="issue-42",
            phase="implement",
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Stray",
            body="no slice_id",
            slice_id=None,
        )
        assert "slice_id" not in stray.metadata
        all_messages = slice1_msgs + slice2_msgs + [stray]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = all_messages

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        produced = sorted(p.name for p in history_dir.glob("42-implement*"))
        assert produced == [
            "42-implement-slice-1.json",
            "42-implement-slice-1.md",
            "42-implement-slice-2.json",
            "42-implement-slice-2.md",
        ], f"Unexpected files: {produced}"

        # The dropped count must equal exactly 1 — the writer must not
        # double-count or miscount.
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "slice_id" in str(c) or "without metadata" in str(c)
        ]
        assert any(c[1].get("dropped_count") == 1 for c in warning_calls), (
            f"Expected dropped_count=1, got warnings: {warning_calls}"
        )

    def test_non_consensus_unattributed_routed_to_unattributed_sibling_file(self, tmp_path):
        """Non-CONSENSUS BRC types (HEARTBEAT, OVERSEER_ALERT, AGENT_FAILED,
        STATUS, NUDGE, HANDOFF) without ``metadata['slice_id']`` are routed
        to ``{identifier}-implement-unattributed.{md,json}`` rather than
        dropped. Their emitters do not uniformly carry slice scope (overseer
        respawn, HealthMonitor escalation, CLI message-send), and dropping
        them would silently strip cross-cutting context from per-slice
        transcripts. See #2548 reviewer_code blocking finding."""
        from routes.pipelines import _write_brc_history

        # One canonical CONSENSUS_PROPOSE so partition mode engages.
        attributed = self._make_implement_msgs("slice-1")
        # An OVERSEER_ALERT and a HEARTBEAT, both without slice_id — these
        # should land in the unattributed sibling, not be dropped.
        unattributed_other = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.OVERSEER_ALERT,
                subject="brc_confirmation_timeout — call mcp__brc__confirm",
                body="orchestrator nudge with no explicit slice scope",
                phase="implement",
                slice_id=None,
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.HEARTBEAT,
                subject="heartbeat: WORKING",
                body="",
                phase="implement",
                slice_id=None,
            ),
        ]
        for m in unattributed_other:
            assert "slice_id" not in m.metadata, "fixture leak — slice_id stamped"

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = attributed + unattributed_other

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # Per-slice file exists for the canonical message.
        assert (history_dir / "42-implement-slice-1.md").exists()
        assert (history_dir / "42-implement-slice-1.json").exists()
        # Unattributed sibling file exists and contains the OVERSEER_ALERT
        # and HEARTBEAT.
        unattributed_md = history_dir / "42-implement-unattributed.md"
        unattributed_json = history_dir / "42-implement-unattributed.json"
        assert unattributed_md.exists(), (
            "Non-CONSENSUS BRC messages without slice_id must land in the "
            "unattributed sibling, not be dropped"
        )
        assert unattributed_json.exists()
        content = unattributed_md.read_text()
        assert "brc_confirmation_timeout" in content
        assert "OVERSEER_ALERT" in content
        assert "HEARTBEAT" in content
        # No aggregate file (partition mode is engaged).
        assert not (history_dir / "42-implement.md").exists()
        # No CONSENSUS_* drop warning (none were dropped).
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "without canonical metadata.slice_id" in str(c)
        ]
        assert warning_calls == [], (
            f"Did not expect drop warnings for non-CONSENSUS unattributed; got: {warning_calls}"
        )

    def test_consensus_dropped_non_consensus_routed_when_mixed(self, tmp_path):
        """When unattributed messages include BOTH CONSENSUS_* and
        non-CONSENSUS_* types, the writer must split the bucket: CONSENSUS_*
        are dropped with a warning (D4 contract violation), non-CONSENSUS_*
        are routed to the unattributed sibling so the audit trail stays
        complete."""
        from routes.pipelines import _write_brc_history

        attributed = self._make_implement_msgs("slice-1")
        stray_consensus = _make_brc_message(
            pipeline_id="issue-42",
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Stray PROPOSE",
            body="contract violation — must be dropped",
            phase="implement",
            slice_id=None,
        )
        stray_alert = _make_brc_message(
            pipeline_id="issue-42",
            from_role="orchestrator",
            message_type=MessageType.OVERSEER_ALERT,
            subject="overseer_restart",
            body="cross-cutting alert — must be routed to unattributed",
            phase="implement",
            slice_id=None,
        )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = attributed + [stray_consensus, stray_alert]

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # CONSENSUS_* drop produced a warning with count=1 (only the stray
        # PROPOSE, not the OVERSEER_ALERT).
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "CONSENSUS_*" in str(c) or "without canonical metadata.slice_id" in str(c)
        ]
        assert any(c[1].get("dropped_count") == 1 for c in warning_calls), (
            f"Expected CONSENSUS_* drop warning with count=1; got {warning_calls}"
        )
        # The OVERSEER_ALERT landed in the unattributed sibling.
        unattributed_md = (history_dir / "42-implement-unattributed.md").read_text()
        assert "overseer_restart" in unattributed_md
        assert "OVERSEER_ALERT" in unattributed_md
        # The stray CONSENSUS_PROPOSE did NOT land anywhere.
        for produced in history_dir.glob("42-implement*.md"):
            content = produced.read_text()
            assert "Stray PROPOSE" not in content, (
                f"CONSENSUS_* drop must not leak into {produced.name}"
            )

    def test_implement_messages_with_empty_slice_id_treated_as_unattributed(self, tmp_path):
        """Empty-string slice_id fails ``SLICE_ID_PATTERN`` validation and is
        treated as unattributed.

        Critically, the writer must NEVER produce a file named
        ``42-implement-.md`` (i.e. interpolating the empty string into the
        per-slice stem) — that would be both ugly on disk and a path-shape
        injection vector.

        When mixed with at least one canonical-attributed message, the
        empty-slice_id messages are dropped with a warning. When all
        messages have empty slice_id, the aggregate fallback engages
        (separate test).
        """
        from routes.pipelines import _write_brc_history

        # Mix an empty-slice_id message with a canonical one so partition
        # mode engages (otherwise we'd get the aggregate fallback path).
        canonical = _make_brc_message(
            pipeline_id="issue-42",
            phase="implement",
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Canonical",
            body="ok",
            slice_id="slice-1",
        )
        empty = _make_brc_message(
            pipeline_id="issue-42",
            phase="implement",
            from_role="reviewer_code",
            message_type=MessageType.CONSENSUS_NACK,
            subject="Empty slice_id",
            body="should be dropped",
            metadata={"slice_id": ""},
            slice_id=None,
        )
        # Sanity: the fixture really has empty string slice_id.
        assert empty.metadata.get("slice_id") == ""

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = [canonical, empty]

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # The dangerous filename MUST NOT be produced.
        assert not (history_dir / "42-implement-.md").exists(), (
            "Empty slice_id must NOT be interpolated into a per-slice stem"
        )
        # The canonical slice-1 file IS produced.
        assert (history_dir / "42-implement-slice-1.md").exists()
        # Aggregate is NOT produced (because partition mode engaged).
        assert not (history_dir / "42-implement.md").exists()
        # Drop warning was emitted with count=1.
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "slice_id" in str(c) or "without canonical" in str(c)
        ]
        assert len(warning_calls) >= 1, "Expected drop warning for empty slice_id"
        assert any(c[1].get("dropped_count") == 1 for c in warning_calls)

    def test_three_slices_all_get_distinct_files(self, tmp_path):
        """N=3 slices produces 3 distinct per-slice .md/.json pairs in
        deterministic order — exercises the bucket sort path."""
        from routes.pipelines import _write_brc_history

        messages = []
        for sid in ["slice-3", "slice-1", "slice-2"]:  # deliberately unordered
            messages.extend(self._make_implement_msgs(sid))

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        for sid in ["slice-1", "slice-2", "slice-3"]:
            assert (history_dir / f"42-implement-{sid}.md").exists(), (
                f"Per-slice file for {sid} missing"
            )
            assert (history_dir / f"42-implement-{sid}.json").exists()
        # Aggregate must not exist.
        assert not (history_dir / "42-implement.md").exists()
        assert not (history_dir / "42-implement.json").exists()

    def test_idempotent_per_slice_write(self, tmp_path):
        """Running the writer twice with the same input produces
        byte-identical per-slice files AND a byte-identical
        ``unattributed`` sibling file (idempotency invariant from #1714
        carried over into per-slice mode + the cross-cutting sibling
        added in the per-slice partition fix)."""
        from routes.pipelines import _write_brc_history

        messages = []
        for sid in ["slice-1", "slice-2"]:
            messages.extend(self._make_implement_msgs(sid))
        # Mix in non-CONSENSUS BRC types without slice_id so the writer
        # produces the unattributed sibling alongside the per-slice
        # files. The sibling is committed to the branch and read by
        # reviewers, so it is on the same idempotency contract.
        messages.append(
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="overseer",
                message_type=MessageType.OVERSEER_ALERT,
                subject="brc_confirmation_timeout",
                body="elapsed",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 30, 0, tzinfo=UTC),
                slice_id=None,
            )
        )
        messages.append(
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.HEARTBEAT,
                subject="alive",
                body="hb",
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 31, 0, tzinfo=UTC),
                slice_id=None,
            )
        )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        history_dir = tmp_path / ".egg-state" / "brc-history"

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)
            first_s1_md = (history_dir / "42-implement-slice-1.md").read_text()
            first_s2_md = (history_dir / "42-implement-slice-2.md").read_text()
            first_s1_json = (history_dir / "42-implement-slice-1.json").read_text()
            first_s2_json = (history_dir / "42-implement-slice-2.json").read_text()
            first_unattr_md = (history_dir / "42-implement-unattributed.md").read_text()
            first_unattr_json = (history_dir / "42-implement-unattributed.json").read_text()

            # Second call (e.g. PR-phase safety-net rewrite).
            _write_brc_history(tmp_path, "issue-42", "implement", 42)
            second_s1_md = (history_dir / "42-implement-slice-1.md").read_text()
            second_s2_md = (history_dir / "42-implement-slice-2.md").read_text()
            second_s1_json = (history_dir / "42-implement-slice-1.json").read_text()
            second_s2_json = (history_dir / "42-implement-slice-2.json").read_text()
            second_unattr_md = (history_dir / "42-implement-unattributed.md").read_text()
            second_unattr_json = (history_dir / "42-implement-unattributed.json").read_text()

        assert first_s1_md == second_s1_md, "slice-1 markdown not idempotent"
        assert first_s2_md == second_s2_md, "slice-2 markdown not idempotent"
        assert first_s1_json == second_s1_json, "slice-1 JSON not idempotent"
        assert first_s2_json == second_s2_json, "slice-2 JSON not idempotent"
        assert first_unattr_md == second_unattr_md, "unattributed sibling markdown not idempotent"
        assert first_unattr_json == second_unattr_json, "unattributed sibling JSON not idempotent"

    def test_message_metadata_is_always_a_dict(self):
        """Pydantic invariant: ``Message.metadata`` is a dict[str, Any] field
        with ``default_factory=dict`` — the writer relies on this to skip
        defensive None/non-dict guards (#2548 reviewer_code non-blocking).
        Pin the invariant so a future Pydantic-config change (e.g. allowing
        None) shows up here rather than as a runtime crash in the writer.
        """
        # Default construction yields an empty dict, never None.
        m = Message(
            pipeline_id="issue-42",
            from_role="coder",
            to_role="all",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="x",
            body="y",
            phase="implement",
        )
        assert isinstance(m.metadata, dict), (
            f"Pydantic default for Message.metadata must be a dict, got {type(m.metadata)}"
        )
        assert m.metadata == {}
        # Explicit dict is preserved.
        m2 = Message(
            pipeline_id="issue-42",
            from_role="coder",
            to_role="all",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="x",
            body="y",
            phase="implement",
            metadata={"slice_id": "slice-1"},
        )
        assert isinstance(m2.metadata, dict)
        assert m2.metadata.get("slice_id") == "slice-1"

    def test_invalid_slice_id_pattern_treated_as_unattributed(self, tmp_path):
        """slice_id values that don't match SLICE_ID_PATTERN (``^slice-[0-9]+$``)
        are treated as unattributed — preventing path-traversal /
        filename-injection through metadata.

        This is a defense-in-depth test: SLICE_ID_PATTERN is enforced at
        every gateway-facing seam upstream, but the writer also validates
        locally so a future leak cannot smuggle ``../etc/passwd`` (or any
        non-canonical value) into ``42-implement-<value>.md``.
        """
        from routes.pipelines import _write_brc_history

        # A canonical slice-1 message so the writer engages partition mode
        # (otherwise it would fall back to the aggregate).
        canonical = _make_brc_message(
            pipeline_id="issue-42",
            phase="implement",
            from_role="coder",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="canonical",
            body="ok",
            slice_id="slice-1",
        )
        # Various malformed slice_id values that MUST NOT produce a file.
        injection_payloads = [
            "../etc/passwd",  # path traversal
            "slice-1/extra",  # directory separator
            "slice-1.bad",  # extra suffix
            "slice-",  # missing digits
            "phase-1",  # legacy non-canonical
            "SLICE-1",  # case mismatch
            "slice- 1",  # whitespace
            "slice-01a",  # non-digits
            "slice-1\nx",  # newline injection
        ]
        bad_msgs = [
            _make_brc_message(
                pipeline_id="issue-42",
                phase="implement",
                from_role="coder",
                message_type=MessageType.CONSENSUS_NACK,
                subject=f"bad-{i}",
                body=f"injection {payload!r}",
                metadata={"slice_id": payload},
                slice_id=None,  # let metadata stand
            )
            for i, payload in enumerate(injection_payloads)
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = [canonical, *bad_msgs]

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # Only the canonical slice-1 file exists.
        produced = sorted(p.name for p in history_dir.glob("42-implement*"))
        assert produced == [
            "42-implement-slice-1.json",
            "42-implement-slice-1.md",
        ], f"Malformed slice_id payloads leaked into output files: {produced}"

        # The aggregate file MUST NOT exist (we have at least one canonical).
        assert not (history_dir / "42-implement.md").exists()
        # Path-traversal: must not have written anywhere outside brc-history.
        # Sanity: the brc-history dir is the ONLY dir under .egg-state for
        # this test (write would have escaped if traversal succeeded).
        sibling_dirs = sorted(p.name for p in (tmp_path / ".egg-state").iterdir() if p.is_dir())
        assert sibling_dirs == ["brc-history"], (
            f"Unexpected directories created: {sibling_dirs} — possible traversal"
        )

        # Drop warning was emitted with the right shape.
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "slice_id" in str(c) or "without canonical" in str(c)
        ]
        assert len(warning_calls) >= 1, (
            f"Expected drop warning for malformed slice_ids, got: {mock_logger.warning.call_args_list}"
        )
        kwargs = warning_calls[0][1]
        assert kwargs.get("dropped_count") == len(injection_payloads), (
            f"Expected dropped_count={len(injection_payloads)}, got: {kwargs}"
        )

    def test_natural_sort_per_slice_iteration_order(self, tmp_path):
        """Per-slice buckets iterate in natural-sort (integer-suffix) order
        so a 12-slice pipeline writes ``slice-1, slice-2, … slice-12`` and
        not the lexicographic ``slice-1, slice-10, slice-11, slice-12,
        slice-2, …`` (#2548 reviewer_code non-blocking).

        The current writer doesn't expose iteration order externally beyond
        the order of ``logger.info`` "Wrote BRC history file" calls, so we
        intercept those to assert the expected sequence.
        """
        from routes.pipelines import _write_brc_history

        # 12 slices in deliberately shuffled input order.
        sids = ["slice-7", "slice-1", "slice-12", "slice-2", "slice-11"]
        messages = []
        for sid in sids:
            messages.extend(self._make_implement_msgs(sid))

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        wrote_calls: list[dict] = []

        def capture(*args, **kwargs):
            if args and args[0] == "Wrote BRC history file":
                wrote_calls.append(kwargs)

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_logger.info.side_effect = capture
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # Order of slice_ids in the "Wrote BRC history file" log entries
        # should be sorted by integer suffix.
        slice_ids_in_order = [c["slice_id"] for c in wrote_calls]
        assert slice_ids_in_order == [
            "slice-1",
            "slice-2",
            "slice-7",
            "slice-11",
            "slice-12",
        ], f"Expected natural-sort iteration order, got {slice_ids_in_order}"


class TestPerSliceImplementBrcHistoryRewriteForPr:
    """The PR-phase safety-net rewrite (``_rewrite_brc_history_for_pr``)
    inherits the per-slice partitioning from ``_write_brc_history`` (#2548).
    These tests verify the rewrite path treats per-slice files correctly
    and never produces an aggregate.
    """

    def test_rewrite_for_pr_emits_per_slice_implement_files(self, tmp_path):
        """When the PR phase rewrites BRC history, the implement-phase
        rewrite produces per-slice files and no aggregate file."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement", slice_id="slice-1")
        messages.extend(
            _make_brc_messages(pipeline_id="issue-42", phase="implement", slice_id="slice-2")
        )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        phases = {
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-implement-slice-1.md").exists()
        assert (history_dir / "42-implement-slice-2.md").exists()
        assert not (history_dir / "42-implement.md").exists()
        assert not (history_dir / "42-implement.json").exists()

    def test_rewrite_for_pr_mixes_aggregate_refine_and_per_slice_implement(self, tmp_path):
        """Mixed multi-phase rewrite: refine emits aggregate, implement
        emits per-slice — both shapes coexist in the same brc-history dir."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        all_messages = []
        all_messages.extend(_make_brc_messages(pipeline_id="issue-42", phase="refine"))
        all_messages.extend(
            _make_brc_messages(pipeline_id="issue-42", phase="implement", slice_id="slice-1")
        )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = all_messages

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # Refine: aggregate. Implement: per-slice.
        assert (history_dir / "42-refine.md").exists()
        assert (history_dir / "42-refine.json").exists()
        assert (history_dir / "42-implement-slice-1.md").exists()
        assert (history_dir / "42-implement-slice-1.json").exists()
        # No aggregate implement file.
        assert not (history_dir / "42-implement.md").exists()
        assert not (history_dir / "42-implement.json").exists()
        # Refine never partitions by slice.
        assert not (history_dir / "42-refine-slice-1.md").exists()
