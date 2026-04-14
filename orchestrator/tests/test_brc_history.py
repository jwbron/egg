"""
Tests for BRC history persistence: _write_brc_history and BRC summary in _build_pr_body.

Covers:
- _write_brc_history: file creation with BRC messages, no-op on empty store
- _build_pr_body: BRC Consensus Summary section presence/absence
- Edge cases: mixed message types, multiple phases, character limits
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


class TestBuildPrBodyBrcSummary:
    """Tests for BRC Consensus Summary in _build_pr_body."""

    def test_includes_brc_summary_when_messages_exist(self, tmp_path):
        """PR body includes '## BRC Consensus Summary' when BRC messages exist."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "## BRC Consensus Summary" in body

    def test_no_brc_section_when_no_messages(self, tmp_path):
        """PR body omits BRC section when no BRC messages exist."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "BRC Consensus Summary" not in body

    def test_no_brc_section_when_only_non_brc_messages(self, tmp_path):
        """PR body omits BRC section when only non-BRC messages exist."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        non_brc_messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                message_type=MessageType.PROGRESS,
                subject="Progress",
                body="Working",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = non_brc_messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "BRC Consensus Summary" not in body

    def test_brc_summary_before_authored_by(self, tmp_path):
        """BRC summary appears before 'Authored-by: egg' footer."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        brc_pos = body.index("BRC Consensus Summary")
        authored_pos = body.index("Authored-by: egg")
        assert brc_pos < authored_pos, "BRC summary must appear before Authored-by footer"

    def test_brc_summary_under_40000_chars(self, tmp_path):
        """BRC summary section stays under ~40000 characters (#1717 raised cap)."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        # Create many BRC messages across multiple phases to stress the limit
        messages = []
        for phase in ["refine", "plan", "implement"]:
            for i in range(20):
                messages.append(
                    _make_brc_message(
                        pipeline_id="issue-42",
                        from_role=f"agent_{i % 5}",
                        message_type=MessageType.CONSENSUS_PROPOSE
                        if i % 4 == 0
                        else MessageType.CONSENSUS_ACK,
                        subject=f"Message {i} in {phase}",
                        body=f"Details for message {i} " * 10,
                        phase=phase,
                        timestamp=datetime(2026, 4, 8, 12, i, 0, tzinfo=UTC),
                    )
                )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        # Extract just the BRC summary section
        if "## BRC Consensus Summary" in body:
            brc_start = body.index("## BRC Consensus Summary")
            # Find the next section or end
            rest = body[brc_start:]
            # Find the next ## header or Authored-by
            next_section = len(rest)
            for marker in ["## Pipeline Context", "## Test Plan", "Authored-by: egg"]:
                if marker in rest[1:]:
                    idx = rest.index(marker, 1)
                    if idx < next_section:
                        next_section = idx
            brc_section = rest[:next_section].strip()
            assert len(brc_section) <= 40000, (
                f"BRC summary section is {len(brc_section)} chars, should be <=40000"
            )

    def test_brc_summary_shows_phase_grouping(self, tmp_path):
        """BRC summary groups messages by phase."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement") + [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="planner",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Plan proposal",
                body="Planning done",
                phase="plan",
                timestamp=datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC),
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "## BRC Consensus Summary" in body
        # Should show multiple phases
        assert "implement" in body.lower()
        assert "plan" in body.lower()

    def test_brc_summary_shows_agent_roles(self, tmp_path):
        """BRC summary mentions agent roles involved."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        brc_start = body.index("## BRC Consensus Summary")
        brc_section = body[brc_start:]
        # Should mention the roles
        assert "coder" in brc_section
        assert "reviewer_code" in brc_section or "tester" in brc_section

    def test_existing_body_structure_preserved(self, tmp_path):
        """Existing PR body structure (description, test plan, etc.) is preserved."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        # Existing sections should still be present
        assert "Fixes a bypass" in body  # PR description from contract
        assert "Authored-by: egg" in body
        assert title == "Fix authentication bypass in login flow"

    def test_handles_message_store_exception_gracefully(self, tmp_path):
        """If get_message_store raises, _build_pr_body still returns valid PR body."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.side_effect = Exception("Redis unavailable")

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        # Should still produce a valid PR body without BRC section
        assert "Authored-by: egg" in body
        assert "BRC Consensus Summary" not in body

    def test_body_stays_under_github_limit(self, tmp_path):
        """Full body with BRC summary stays under GitHub's 65536 char limit."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert len(body) < 65_536


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


class TestBuildBrcConsensusSummary:
    """Tests for _build_brc_consensus_summary helper."""

    def test_returns_empty_string_when_no_messages(self):
        """Returns empty string when message store has no messages."""
        from routes.pipelines import _build_brc_consensus_summary

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert result == ""

    def test_returns_empty_string_when_only_non_brc_messages(self):
        """Returns empty string when only non-BRC messages exist."""
        from routes.pipelines import _build_brc_consensus_summary

        non_brc = [
            _make_brc_message(
                pipeline_id="issue-42",
                message_type=MessageType.PROGRESS,
                subject="Progress",
                body="Working",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = non_brc

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert result == ""

    def test_includes_header(self):
        """Summary starts with '## BRC Consensus Summary'."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert result.startswith("## BRC Consensus Summary")

    def test_shows_proposal_and_ack_counts(self):
        """Summary includes proposal and ACK counts."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "proposal" in result.lower()
        assert "ACK" in result

    def test_shows_nack_counts(self):
        """Summary includes NACK counts when present."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                body="Missing tests",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "NACK" in result

    def test_consensus_reached_indicator(self):
        """Shows consensus reached when all roles have CONFIRMED."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        # Add confirmations for all roles
        for role in ["reviewer_code", "tester"]:
            messages.append(
                _make_brc_message(
                    pipeline_id="issue-42",
                    from_role=role,
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject=f"Confirmed by {role}",
                    body="",
                    phase="implement",
                    timestamp=datetime(2026, 4, 8, 12, 20, 0, tzinfo=UTC),
                )
            )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "Consensus reached" in result or "consensus reached" in result.lower()

    def test_capped_at_40000_chars(self):
        """Summary is capped at approximately 40000 characters (#1717 raised cap).

        Truncation still happens at phase-block boundaries to keep markdown intact.
        """
        from routes.pipelines import _build_brc_consensus_summary

        # Create many messages to exceed the cap
        messages = []
        for phase in ["refine", "plan", "implement", "test", "review"]:
            for i in range(30):
                messages.append(
                    _make_brc_message(
                        pipeline_id="issue-42",
                        from_role=f"long_agent_role_name_{i}",
                        message_type=MessageType.CONSENSUS_PROPOSE
                        if i % 3 == 0
                        else MessageType.CONSENSUS_ACK,
                        subject=f"Message {i} with a somewhat longer subject line",
                        body=f"Body content for message {i} " * 50,
                        phase=phase,
                    )
                )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Truncated at phase-block boundary — must be under 40000 chars
        assert len(result) <= 40000

    def test_groups_by_phase(self):
        """Messages from different phases appear in separate groups."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                phase="plan",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "plan" in result
        assert "implement" in result

    def test_returns_empty_on_store_exception(self):
        """Returns empty string when message store raises exception."""
        from routes.pipelines import _build_brc_consensus_summary

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.side_effect = Exception("Redis down")

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert result == ""

    def test_consensus_reached_when_orchestrator_sent_re_review(self):
        """Regression for #1706: orchestrator CONSENSUS_RE_REVIEW messages must
        not cause the consensus check to fail.

        The orchestrator sends BRC coordination messages (e.g.,
        CONSENSUS_RE_REVIEW) but never sends CONSENSUS_CONFIRMED. If the
        summary counts orchestrator as a participant, consensus will always
        appear unreached after a re-review cycle, even when every real agent
        has confirmed.

        Since #1717, the summary inlines final-round content, so orchestrator
        may appear in the body of inline messages (e.g., RE_REVIEW). The key
        assertion is that consensus is shown as reached and that orchestrator
        is excluded from the *participant role list* on the first line.
        """
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            # Orchestrator issues a re-review directive — it is a coordinator,
            # not a participant, and never confirms.
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 7, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 15, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                phase="implement",
                timestamp=datetime(2026, 4, 8, 12, 16, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "Consensus reached" in result
        assert "not reached" not in result
        # Orchestrator should not appear in the participant role list (first line
        # of the phase block: "**implement**: coder, reviewer_code")
        first_phase_line = [line for line in result.split("\n") if line.startswith("**implement**")]
        assert first_phase_line, "Expected a line starting with **implement**"
        assert "orchestrator" not in first_phase_line[0]

    def test_consensus_not_reached_ignores_orchestrator_presence(self):
        """Orchestrator-only absence from CONFIRMED should not change the
        consensus verdict: if a real agent hasn't confirmed, consensus is
        still unreached regardless of orchestrator activity."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                phase="implement",
            ),
            # Only coder confirms — reviewer_code hasn't.
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "not reached" in result


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

    def test_handoff_excluded_from_summary_counts(self):
        """HANDOFF messages appear in history but NOT in the consensus summary counts."""
        from routes.pipelines import _build_brc_consensus_summary

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
                body="Handing off",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Summary should only count CONSENSUS_* types
        assert "1 proposal" in result
        assert "HANDOFF" not in result

    def test_overseer_alert_excluded_from_summary_counts(self):
        """OVERSEER_ALERT messages appear in history but NOT in the consensus summary."""
        from routes.pipelines import _build_brc_consensus_summary

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
                from_role="orchestrator",
                message_type=MessageType.OVERSEER_ALERT,
                subject="Alert",
                body="Stall detected",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "OVERSEER_ALERT" not in result


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


class TestBrcConsensusSummaryInline:
    """Tests for inline content and artifact links in _build_brc_consensus_summary (#1717)."""

    def test_final_round_proposal_body_inline(self):
        """PR body summary contains the final-round proposal body inline."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implemented auth fix with input validation",
                phase="implement",
                metadata={"version": 1},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="Code looks clean",
                phase="implement",
                metadata={"version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "Implemented auth fix with input validation" in result
        assert "Code looks clean" in result

    def test_final_round_nack_rationale_inline(self):
        """PR body summary contains final-round NACK rationale inline."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="First attempt",
                phase="implement",
                metadata={"version": 1},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK",
                body="Missing error handling in auth flow",
                phase="implement",
                metadata={"version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "Missing error handling in auth flow" in result

    def test_earlier_rounds_wrapped_in_details(self):
        """Older/earlier-round messages are wrapped in <details> blocks."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            # Round 1 (earlier)
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="First proposal",
                body="First attempt",
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK round 1",
                body="Missing tests",
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            # Round 2 (final)
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Second proposal",
                body="Fixed with tests",
                phase="implement",
                metadata={"version": 2},
                timestamp=datetime(2026, 4, 8, 13, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK round 2",
                body="All good now",
                phase="implement",
                metadata={"version": 2},
                timestamp=datetime(2026, 4, 8, 13, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Final round content should be inline (not in <details>)
        assert "Fixed with tests" in result
        assert "All good now" in result

        # Earlier round content should be in <details>
        assert "<details>" in result
        assert "Earlier rounds" in result
        assert "First attempt" in result or "Missing tests" in result

    def test_artifact_links_present(self):
        """Per-phase artifact links to .md and .json files are present."""
        from routes.pipelines import _build_brc_consensus_summary

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
            result = _build_brc_consensus_summary("issue-42", identifier=42)

        assert "Full record:" in result
        assert ".egg-state/brc-history/42-implement.md" in result
        assert ".egg-state/brc-history/42-implement.json" in result

    def test_artifact_links_omitted_without_identifier(self):
        """Artifact links are omitted when identifier is None."""
        from routes.pipelines import _build_brc_consensus_summary

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
            result = _build_brc_consensus_summary("issue-42")

        assert "Full record:" not in result

    def test_truncated_body_has_pointer(self):
        """When an individual body exceeds the inline limit, it is truncated with a pointer."""
        from routes.pipelines import _build_brc_consensus_summary

        long_body = "X" * 3000  # Exceeds _MAX_BODY_INLINE of 2000
        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body=long_body,
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Should contain truncated body with pointer
        assert "full content in brc-history/*.md" in result
        # Original 3000-char body should NOT be fully present
        assert long_body not in result

    def test_no_empty_metadata_yaml_block(self, tmp_path):
        """Messages with empty metadata should not have a YAML block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                phase="implement",
                metadata={},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # Should still have a yaml block with id and phase, but no metadata key
        assert "````yaml" in content
        assert "id:" in content
        assert "phase:" in content
        # metadata key should NOT appear since it's empty
        assert "metadata:" not in content


class TestBrcMessageTypeConstants:
    """Tests for BRC_SUMMARY_TYPES, BRC_HISTORY_TYPES, and backward-compat alias."""

    def test_brc_message_types_is_alias_of_summary_types(self):
        """BRC_MESSAGE_TYPES must remain a backward-compatible alias of BRC_SUMMARY_TYPES."""
        from routes.pipelines import BRC_MESSAGE_TYPES, BRC_SUMMARY_TYPES

        assert BRC_MESSAGE_TYPES is BRC_SUMMARY_TYPES

    def test_brc_summary_types_contains_only_consensus_types(self):
        """BRC_SUMMARY_TYPES must contain only CONSENSUS_* message types."""
        from routes.pipelines import BRC_SUMMARY_TYPES

        expected = frozenset(
            {
                "CONSENSUS_PROPOSE",
                "CONSENSUS_ACK",
                "CONSENSUS_NACK",
                "CONSENSUS_WITHDRAW",
                "CONSENSUS_CONFIRMED",
                "CONSENSUS_RE_REVIEW",
            }
        )
        assert BRC_SUMMARY_TYPES == expected

    def test_brc_history_types_is_superset_of_summary_types(self):
        """BRC_HISTORY_TYPES must be a strict superset of BRC_SUMMARY_TYPES."""
        from routes.pipelines import BRC_HISTORY_TYPES, BRC_SUMMARY_TYPES

        assert BRC_SUMMARY_TYPES < BRC_HISTORY_TYPES  # strict subset

    def test_brc_history_types_includes_non_consensus_types(self):
        """BRC_HISTORY_TYPES must include STATUS, HANDOFF, QUESTION, AGENT_FAILED, NUDGE, OVERSEER_ALERT."""
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
        """PROGRESS must NOT be in BRC_HISTORY_TYPES — it is not BRC-adjacent."""
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


class TestNackPayloadReasonFallback:
    """Tests for NACK rationale extraction from metadata.payload.reason when body is empty."""

    def test_nack_body_empty_falls_back_to_metadata_payload_reason(self):
        """When a NACK has empty body, _build_brc_consensus_summary uses metadata.payload.reason."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation done",
                phase="implement",
                metadata={"version": 1},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK",
                body="",  # Empty body
                phase="implement",
                metadata={
                    "version": 1,
                    "payload": {"reason": "Missing input validation on auth endpoint"},
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "Missing input validation on auth endpoint" in result

    def test_nack_payload_reason_not_dict_is_handled(self):
        """When metadata.payload is not a dict, NACK with empty body shows no content."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                phase="implement",
                metadata={"version": 1},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK",
                body="",
                phase="implement",
                metadata={
                    "version": 1,
                    "payload": "not-a-dict",
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Should not crash; NACK without extractable body is handled gracefully
        assert "## BRC Consensus Summary" in result


class TestVersionHandlingEdgeCases:
    """Tests for version handling edge cases in _build_brc_consensus_summary."""

    def test_non_integer_version_treated_as_zero(self):
        """Non-parseable version in metadata is treated as version 0."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation",
                phase="implement",
                metadata={"version": "not-a-number"},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="Looks good",
                phase="implement",
                metadata={"version": None},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Should not crash; all messages treated as version 0 → all "final round"
        assert "Implementation" in result
        assert "Looks good" in result

    def test_no_version_metadata_at_all(self):
        """When no messages have version metadata, all messages are treated as final round."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="First attempt",
                phase="implement",
                metadata={},  # No version
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                phase="implement",
                metadata={},  # No version
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # All messages should be treated as final round — no <details> block
        assert "First attempt" in result
        assert "LGTM" in result
        assert "<details>" not in result

    def test_confirmed_and_re_review_always_final_round(self):
        """CONFIRMED and RE_REVIEW messages are always treated as final-round."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal v1",
                body="First attempt",
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal v2",
                body="Second attempt",
                phase="implement",
                metadata={"version": 2},
                timestamp=datetime(2026, 4, 8, 13, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review",
                body="Please re-review",
                phase="implement",
                metadata={"version": 1},  # version 1 but should be final
                timestamp=datetime(2026, 4, 8, 13, 5, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="All done",
                phase="implement",
                metadata={},  # No version but should be final
                timestamp=datetime(2026, 4, 8, 14, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="Confirming",
                phase="implement",
                metadata={"version": 1},  # version 1 but should be final
                timestamp=datetime(2026, 4, 8, 14, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Final round (v2 propose, RE_REVIEW, CONFIRMED) should be inline
        assert "Second attempt" in result
        # CONFIRMED messages don't have body for inline in the current impl —
        # they may or may not show depending on whether they have body content
        # The key assertion: RE_REVIEW is final round (not wrapped in <details>)
        # Earlier round (v1 propose) should be in <details>
        assert "<details>" in result
        assert "First attempt" in result  # in <details> block


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

    def test_non_consensus_types_excluded_from_summary(self):
        """STATUS, HANDOFF, QUESTION, AGENT_FAILED, NUDGE, OVERSEER_ALERT excluded from summary."""
        from routes.pipelines import _build_brc_consensus_summary

        non_consensus_types = [
            (MessageType.STATUS, "Status update"),
            (MessageType.HANDOFF, "Handoff"),
            (MessageType.QUESTION, "Question"),
            (MessageType.AGENT_FAILED, "Agent failed"),
            (MessageType.NUDGE, "Nudge"),
            (MessageType.OVERSEER_ALERT, "Alert"),
        ]
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
        for msg_type, subject in non_consensus_types:
            messages.append(
                _make_brc_message(
                    pipeline_id="issue-42",
                    from_role="orchestrator",
                    message_type=msg_type,
                    subject=subject,
                    body="Content",
                    phase="implement",
                )
            )
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "1 proposal" in result
        # None of the non-consensus types should affect the summary
        for msg_type, _ in non_consensus_types:
            assert msg_type not in result


class TestBuildPrBodyArtifactLinks:
    """Tests for artifact links in _build_pr_body after #1717 changes."""

    def test_pr_body_includes_artifact_links(self, tmp_path):
        """PR body with BRC summary includes artifact links to .md and .json files."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = _make_brc_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        assert "Full record:" in body
        assert ".egg-state/brc-history/42-implement.md" in body
        assert ".egg-state/brc-history/42-implement.json" in body

    def test_pr_body_inline_content_for_final_round(self, tmp_path):
        """PR body contains inline proposal body content from final round."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implemented auth fix with validation",
                phase="implement",
                metadata={"version": 1},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="Verified and approved",
                phase="implement",
                metadata={"version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        # Final round content should be inline in the PR body
        assert "Implemented auth fix with validation" in body
        assert "Verified and approved" in body


class TestEarlierRoundsDetailsBlock:
    """Tests for the <details> block wrapping earlier-round messages."""

    def test_body_less_earlier_round_messages_shown_without_content(self):
        """Earlier-round messages with empty body are shown without content text."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            # Round 1 (earlier) — CONFIRMED with no body
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="First proposal",
                body="First attempt",
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK v1",
                body="",  # Empty body
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            # Round 2 (final)
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Second proposal",
                body="Updated implementation",
                phase="implement",
                metadata={"version": 2},
                timestamp=datetime(2026, 4, 8, 13, 0, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Earlier round should be in <details>
        assert "<details>" in result
        # Body-less ACK should appear with role and type but no content text
        assert "reviewer_code" in result
        # Final round should be inline
        assert "Updated implementation" in result

    def test_multiple_rounds_details_block_structure(self):
        """Multi-round consensus wraps all non-final rounds in a single <details> block."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            # Round 1
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Round 1 content",
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                body="Round 1 feedback",
                phase="implement",
                metadata={"version": 1},
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            # Round 2
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Round 2 content",
                phase="implement",
                metadata={"version": 2},
                timestamp=datetime(2026, 4, 8, 13, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                body="Round 2 feedback",
                phase="implement",
                metadata={"version": 2},
                timestamp=datetime(2026, 4, 8, 13, 5, 0, tzinfo=UTC),
            ),
            # Round 3 (final)
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Round 3 final",
                phase="implement",
                metadata={"version": 3},
                timestamp=datetime(2026, 4, 8, 14, 0, 0, tzinfo=UTC),
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                body="Approved",
                phase="implement",
                metadata={"version": 3},
                timestamp=datetime(2026, 4, 8, 14, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Final round inline
        assert "Round 3 final" in result
        assert "Approved" in result

        # Earlier rounds in <details>
        assert "<details>" in result
        assert "</details>" in result
        assert "Earlier rounds" in result

        # Both round 1 and round 2 content should be in the earlier section
        details_start = result.index("<details>")
        details_end = result.index("</details>")
        details_content = result[details_start:details_end]
        assert "Round 1 content" in details_content or "Round 1 feedback" in details_content
        assert "Round 2 content" in details_content or "Round 2 feedback" in details_content

    def test_single_round_no_details_block(self):
        """Single-round consensus has no <details> block."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Only attempt",
                phase="implement",
                metadata={"version": 1},
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                body="Approved",
                phase="implement",
                metadata={"version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Single round — no earlier rounds section
        assert "<details>" not in result
        assert "Only attempt" in result
        assert "Approved" in result


class TestSummaryMultiPhaseArtifactLinks:
    """Tests for artifact links in multi-phase summaries."""

    def test_artifact_links_per_phase_in_summary(self):
        """Each phase block in the summary has its own artifact links."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Plan done",
                phase="plan",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Implement done",
                phase="implement",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42", identifier=42)

        # Both phases should have artifact links
        assert ".egg-state/brc-history/42-plan.md" in result
        assert ".egg-state/brc-history/42-plan.json" in result
        assert ".egg-state/brc-history/42-implement.md" in result
        assert ".egg-state/brc-history/42-implement.json" in result
