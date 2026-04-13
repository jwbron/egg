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
        """When only non-BRC messages exist, no history file is created."""
        from routes.pipelines import _write_brc_history

        non_brc_messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                message_type=MessageType.PROGRESS,
                subject="Working",
                body="Starting",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                message_type=MessageType.STATUS,
                subject="Status",
                body="Running",
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

    def test_filters_only_brc_messages(self, tmp_path):
        """Mixed message types: only CONSENSUS_* types appear in history file."""
        from routes.pipelines import _write_brc_history

        messages = _make_mixed_messages(pipeline_id="issue-42", phase="implement")
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        expected_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert expected_path.exists()

        content = expected_path.read_text()
        # Should contain the BRC message
        assert "CONSENSUS_PROPOSE" in content
        # Should NOT contain non-BRC message types as entries
        # (PROGRESS/STATUS should be filtered out)
        assert "PROGRESS" not in content or content.count("PROGRESS") == 0

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

    def test_brc_summary_under_2000_chars(self, tmp_path):
        """BRC summary section stays under ~2000 characters."""
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
            assert len(brc_section) <= 2000, (
                f"BRC summary section is {len(brc_section)} chars, should be <=2000"
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

    def test_capped_at_2000_chars(self):
        """Summary is capped at approximately 2000 characters (truncated at phase-block boundaries)."""
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
                        body=f"Body content for message {i} " * 5,
                        phase=phase,
                    )
                )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Truncated at phase-block boundary — must be under 2000 chars
        assert len(result) <= 2000

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
        # Orchestrator should not appear in the participant role list
        assert "orchestrator" not in result

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
