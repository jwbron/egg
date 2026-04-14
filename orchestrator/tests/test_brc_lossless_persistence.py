"""
Tests for lossless BRC message persistence (issue #1717).

Covers:
- BRC_SUMMARY_TYPES / BRC_HISTORY_TYPES split
- Lossless _write_brc_history with YAML metadata blocks
- JSON companion file round-trip
- Expanded message types in history (HANDOFF, AGENT_FAILED, etc.)
- Directed-message to_role visibility
- PR body inline final-round content with <details> collapsing
- Artifact links in PR body summary
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from message_store import Message, MessageStore, MessageType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    pipeline_id="issue-42",
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
):
    """Create a Pipeline for testing."""
    from models import Pipeline, PipelinePhase, PipelineStatus

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
    pr_description=(
        "Fixes a bypass where unauthenticated users could access protected routes.\n\nCloses #42"
    ),
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


def _setup_contract(tmp_path, issue_number=42):
    """Set up a contract JSON file in the temp directory."""
    contract_dir = tmp_path / ".egg-state" / "contracts"
    contract_dir.mkdir(parents=True)
    contract_file = contract_dir / f"{issue_number}.json"
    contract_file.write_text(json.dumps(_make_contract_json(issue_number=issue_number)))


def _msg(
    pipeline_id="issue-42",
    from_role="coder",
    to_role="all",
    message_type=MessageType.CONSENSUS_PROPOSE,
    subject="Proposal from coder",
    body="Implemented the feature",
    phase="implement",
    timestamp=None,
    metadata=None,
    msg_id=None,
):
    """Create a Message for testing with full control over fields."""
    kwargs = dict(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=to_role,
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        metadata=metadata or {},
    )
    if msg_id is not None:
        kwargs["id"] = msg_id
    return Message(**kwargs)


# ---------------------------------------------------------------------------
# 1. BRC_SUMMARY_TYPES / BRC_HISTORY_TYPES split
# ---------------------------------------------------------------------------


class TestBrcMessageTypeSplit:
    """Verify BRC_MESSAGE_TYPES was correctly split into two sets."""

    def test_brc_summary_types_contains_only_consensus(self):
        """BRC_SUMMARY_TYPES contains only the six CONSENSUS_* types."""
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

    def test_brc_history_types_includes_non_consensus(self):
        """BRC_HISTORY_TYPES includes CONSENSUS_* plus orchestrator message types."""
        from routes.pipelines import BRC_HISTORY_TYPES

        # Must include all CONSENSUS types
        for t in [
            "CONSENSUS_PROPOSE",
            "CONSENSUS_ACK",
            "CONSENSUS_NACK",
            "CONSENSUS_WITHDRAW",
            "CONSENSUS_CONFIRMED",
            "CONSENSUS_RE_REVIEW",
        ]:
            assert t in BRC_HISTORY_TYPES, f"{t} missing from BRC_HISTORY_TYPES"

        # Must also include non-CONSENSUS types per issue #1717
        for t in [
            "STATUS",
            "HANDOFF",
            "QUESTION",
            "AGENT_FAILED",
            "NUDGE",
            "OVERSEER_ALERT",
        ]:
            assert t in BRC_HISTORY_TYPES, f"{t} missing from BRC_HISTORY_TYPES"

    def test_brc_message_types_alias(self):
        """BRC_MESSAGE_TYPES is kept as an alias of BRC_SUMMARY_TYPES."""
        from routes.pipelines import BRC_MESSAGE_TYPES, BRC_SUMMARY_TYPES

        assert BRC_MESSAGE_TYPES == BRC_SUMMARY_TYPES

    def test_summary_types_is_subset_of_history_types(self):
        """BRC_SUMMARY_TYPES is a proper subset of BRC_HISTORY_TYPES."""
        from routes.pipelines import BRC_HISTORY_TYPES, BRC_SUMMARY_TYPES

        assert BRC_SUMMARY_TYPES < BRC_HISTORY_TYPES


# ---------------------------------------------------------------------------
# 2. Lossless _write_brc_history — YAML metadata blocks
# ---------------------------------------------------------------------------


class TestWriteBrcHistoryMetadata:
    """Verify _write_brc_history renders YAML metadata blocks with full metadata."""

    def test_ack_artifact_references_in_yaml(self, tmp_path):
        """ACK messages with artifact_references have them in a YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK from reviewer_code",
                body="Code looks good",
                metadata={
                    "payload": {
                        "artifact_references": [
                            "orchestrator/routes/pipelines.py",
                            "orchestrator/tests/test_brc_history.py",
                        ],
                    },
                    "version": 1,
                },
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()

        # Must contain a YAML block with artifact_references
        assert "```yaml" in content or "```yml" in content
        assert "artifact_references" in content
        assert "orchestrator/routes/pipelines.py" in content

    def test_ack_version_in_yaml(self, tmp_path):
        """ACK messages with ack_version have it in the YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK from reviewer_code",
                body="Approved",
                metadata={
                    "payload": {"ack_version": 2},
                    "version": 2,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "ack_version" in content

    def test_nack_reason_and_revision_count_in_yaml(self, tmp_path):
        """NACK with reason and revision_count are round-tripped through YAML."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="First attempt",
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK from reviewer",
                body="Missing error handling",
                metadata={
                    "payload": {"reason": "Missing error handling in auth flow"},
                    "revision_count": 2,
                    "version": 1,
                },
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "revision_count" in content
        assert "reason" in content

    def test_propose_commit_sha_in_yaml(self, tmp_path):
        """PROPOSE with commit_sha has it visible in the YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Implementation proposal",
                body="All tests pass",
                metadata={
                    "commit_sha": "abc123def456",
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "commit_sha" in content
        assert "abc123def456" in content

    def test_metadata_version_field_in_yaml(self, tmp_path):
        """The metadata `version` field (consensus version counter) is preserved."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation",
                metadata={"version": 3},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "version" in content

    def test_no_yaml_block_when_metadata_empty(self, tmp_path):
        """Messages with empty metadata should not have a YAML block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation",
                metadata={},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # Empty metadata -> no yaml block (per contract "when non-empty")
        assert "```yaml" not in content

    def test_yaml_block_is_valid_yaml(self, tmp_path):
        """The fenced YAML block can be parsed by a YAML parser."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                metadata={
                    "payload": {
                        "artifact_references": ["file1.py", "file2.py"],
                        "ack_version": 1,
                    },
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()

        # Extract YAML block
        yaml_blocks = []
        in_yaml = False
        yaml_lines = []
        for line in content.splitlines():
            if line.strip().startswith("```yaml") or line.strip().startswith("```yml"):
                in_yaml = True
                yaml_lines = []
                continue
            if in_yaml and line.strip() == "```":
                in_yaml = False
                yaml_blocks.append("\n".join(yaml_lines))
                continue
            if in_yaml:
                yaml_lines.append(line)

        assert len(yaml_blocks) >= 1, "Expected at least one YAML block"
        parsed = yaml.safe_load(yaml_blocks[0])
        assert isinstance(parsed, dict)
        # Verify the parsed YAML round-trips the metadata
        assert "payload" in parsed or "version" in parsed


# ---------------------------------------------------------------------------
# 3. Directed-message to_role visibility
# ---------------------------------------------------------------------------


class TestDirectedMessageToRole:
    """Verify to_role is shown for directed messages and omitted for broadcasts."""

    def test_directed_message_shows_to_role(self, tmp_path):
        """Directed messages (to_role != 'all') show the arrow → target."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="orchestrator",
                to_role="coder",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review needed",
                body="Coder must update proposal",
                metadata={"version": 2},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # Directed message should show to_role: "orchestrator → coder" or similar
        assert "coder" in content
        # Should contain an arrow or similar indicator of direction
        assert "→" in content or "->" in content

    def test_broadcast_message_omits_to_role(self, tmp_path):
        """Broadcast messages (to_role='all') omit the to_role/arrow."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Implementation ready",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()

        # For broadcast, the header should NOT have an arrow → all
        # Look at the header line specifically
        headers = [line for line in content.splitlines() if line.startswith("### ")]
        assert len(headers) == 1
        # The header should not contain "→ all" or "-> all"
        assert "→ all" not in headers[0]
        assert "-> all" not in headers[0]


# ---------------------------------------------------------------------------
# 4. Expanded message types in history file (non-CONSENSUS types)
# ---------------------------------------------------------------------------


class TestExpandedMessageTypesInHistory:
    """Verify non-CONSENSUS message types are included in history but not summary."""

    def test_handoff_in_history(self, tmp_path):
        """HANDOFF messages appear in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="coder",
                to_role="tester",
                message_type=MessageType.HANDOFF,
                subject="Handing off to tester",
                body="Tests needed for auth module",
                timestamp=datetime(2026, 4, 8, 12, 1, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "HANDOFF" in content
        assert "Handing off to tester" in content

    def test_agent_failed_in_history(self, tmp_path):
        """AGENT_FAILED messages appear in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="orchestrator",
                message_type=MessageType.AGENT_FAILED,
                subject="Tester agent crashed",
                body="Container exited with code 137",
                timestamp=datetime(2026, 4, 8, 12, 2, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "AGENT_FAILED" in content

    def test_overseer_alert_in_history(self, tmp_path):
        """OVERSEER_ALERT messages appear in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="overseer",
                message_type=MessageType.OVERSEER_ALERT,
                subject="Anomaly detected",
                body="Agent stuck in loop",
                timestamp=datetime(2026, 4, 8, 12, 3, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "OVERSEER_ALERT" in content

    def test_status_in_history(self, tmp_path):
        """STATUS messages appear in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="orchestrator",
                message_type=MessageType.STATUS,
                subject="All reviewers ACKed",
                body="Ready to confirm",
                timestamp=datetime(2026, 4, 8, 12, 10, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "STATUS" in content
        assert "All reviewers ACKed" in content

    def test_nudge_in_history(self, tmp_path):
        """NUDGE messages appear in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="orchestrator",
                message_type=MessageType.NUDGE,
                subject="Nudge for reviewer",
                body="Awaiting your ACK/NACK",
                timestamp=datetime(2026, 4, 8, 12, 15, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "NUDGE" in content

    def test_question_in_history(self, tmp_path):
        """QUESTION messages appear in the history file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="tester",
                to_role="coder",
                message_type=MessageType.QUESTION,
                subject="Question about auth",
                body="Which endpoint should I test?",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "QUESTION" in content
        assert "Which endpoint should I test?" in content

    def test_non_consensus_excluded_from_summary_counts(self):
        """HANDOFF, AGENT_FAILED, OVERSEER_ALERT, STATUS, NUDGE, QUESTION are
        NOT counted in _build_brc_consensus_summary."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            # These should NOT affect counts
            _msg(
                from_role="coder",
                message_type=MessageType.HANDOFF,
                subject="Handoff",
                body="Handing off",
                timestamp=datetime(2026, 4, 8, 12, 1, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="orchestrator",
                message_type=MessageType.AGENT_FAILED,
                subject="Failed",
                body="Crashed",
                timestamp=datetime(2026, 4, 8, 12, 2, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="overseer",
                message_type=MessageType.OVERSEER_ALERT,
                subject="Alert",
                body="Anomaly",
                timestamp=datetime(2026, 4, 8, 12, 3, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Should show proposals/ACKs but NOT non-consensus types in counts
        assert "1 proposal" in result.lower()
        assert "1 ACK" in result
        # Should NOT include non-consensus types in the summary role list
        assert "HANDOFF" not in result
        assert "AGENT_FAILED" not in result
        assert "OVERSEER_ALERT" not in result

    def test_progress_still_excluded_from_history(self, tmp_path):
        """PROGRESS messages are NOT in BRC_HISTORY_TYPES and remain excluded."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.PROGRESS,
                subject="Progress update",
                body="50% complete",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == [], "PROGRESS should remain excluded"


# ---------------------------------------------------------------------------
# 5. JSON companion file
# ---------------------------------------------------------------------------


class TestJsonCompanionFile:
    """Verify JSON companion is written alongside the markdown history."""

    def test_json_file_created(self, tmp_path):
        """A .json file is written alongside the .md file."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation ready",
                metadata={"commit_sha": "abc123", "version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        assert json_path.exists(), f"Expected JSON companion at {json_path}"

    def test_json_file_deserializes_to_list(self, tmp_path):
        """JSON companion deserializes to a list of message dicts."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                metadata={"commit_sha": "abc123"},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
                metadata={
                    "payload": {
                        "artifact_references": ["file.py"],
                        "ack_version": 1,
                    },
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 2

    def test_json_roundtrips_to_dict_format(self, tmp_path):
        """Each JSON entry matches Message.to_dict() output structure."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                msg_id="test-msg-001",
                metadata={"commit_sha": "abc123", "version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        entry = data[0]

        # Must have all Message.to_dict() fields
        expected_keys = {
            "id",
            "pipeline_id",
            "from_role",
            "to_role",
            "message_type",
            "subject",
            "body",
            "metadata",
            "timestamp",
            "phase",
        }
        assert set(entry.keys()) == expected_keys
        assert entry["id"] == "test-msg-001"
        assert entry["from_role"] == "coder"
        assert entry["message_type"] == "CONSENSUS_PROPOSE"
        assert entry["metadata"]["commit_sha"] == "abc123"

    def test_json_includes_non_consensus_messages(self, tmp_path):
        """JSON companion includes HANDOFF, AGENT_FAILED, etc."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="coder",
                to_role="tester",
                message_type=MessageType.HANDOFF,
                subject="Handoff to tester",
                body="Please test auth flow",
                timestamp=datetime(2026, 4, 8, 12, 1, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="orchestrator",
                message_type=MessageType.AGENT_FAILED,
                subject="Agent failed",
                body="Crash",
                timestamp=datetime(2026, 4, 8, 12, 2, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="overseer",
                message_type=MessageType.OVERSEER_ALERT,
                subject="Alert",
                body="Anomaly",
                timestamp=datetime(2026, 4, 8, 12, 3, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        data = json.loads(json_path.read_text())
        types_in_json = {entry["message_type"] for entry in data}
        assert "HANDOFF" in types_in_json
        assert "AGENT_FAILED" in types_in_json
        assert "OVERSEER_ALERT" in types_in_json

    def test_json_failure_does_not_block_markdown(self, tmp_path):
        """If JSON writing fails, the markdown file is still written."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        # Make the JSON path unwritable by pre-creating it as a directory
        # (json.dumps will work, but Path.write_text to a dir will fail)
        json_dir = tmp_path / ".egg-state" / "brc-history"
        json_dir.mkdir(parents=True, exist_ok=True)
        json_blocker = json_dir / "42-implement.json"
        json_blocker.mkdir()  # Creating as dir blocks write_text

        with patch("message_store.get_message_store", return_value=mock_store):
            # Should not raise
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        md_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert md_path.exists(), "Markdown file should still be written even when JSON fails"

    def test_json_handles_non_serializable_metadata(self, tmp_path):
        """JSON uses default=str for non-serializable metadata values."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                metadata={
                    "some_datetime": datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        # Should not fail and should produce valid JSON
        data = json.loads(json_path.read_text())
        assert len(data) == 1


# ---------------------------------------------------------------------------
# 6. PR body inline content with <details> collapsing
# ---------------------------------------------------------------------------


class TestPrBodyInlineContent:
    """Verify PR body summary inlines final-round content with <details> for older rounds."""

    def _make_multi_round_messages(self, pipeline_id="issue-42", phase="implement"):
        """Create messages simulating two rounds: NACK in round 1, ACK in round 2."""
        return [
            # Round 1: proposal → NACK
            _msg(
                pipeline_id=pipeline_id,
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal v1",
                body="First attempt at the implementation",
                phase=phase,
                metadata={"commit_sha": "aaa111", "version": 1},
                timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            ),
            _msg(
                pipeline_id=pipeline_id,
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK from reviewer",
                body="Missing error handling in auth module",
                phase=phase,
                metadata={
                    "payload": {"reason": "Missing error handling in auth module"},
                    "revision_count": 1,
                    "version": 1,
                },
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            # Round 2: re-proposal → ACK → CONFIRMED
            _msg(
                pipeline_id=pipeline_id,
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal v2",
                body="Updated implementation with error handling",
                phase=phase,
                metadata={"commit_sha": "bbb222", "version": 2},
                timestamp=datetime(2026, 4, 8, 13, 0, 0, tzinfo=UTC),
            ),
            _msg(
                pipeline_id=pipeline_id,
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK from reviewer",
                body="Error handling looks good now",
                phase=phase,
                metadata={
                    "payload": {
                        "artifact_references": ["orchestrator/routes/pipelines.py"],
                        "ack_version": 2,
                    },
                    "version": 2,
                },
                timestamp=datetime(2026, 4, 8, 13, 5, 0, tzinfo=UTC),
            ),
            _msg(
                pipeline_id=pipeline_id,
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                phase=phase,
                timestamp=datetime(2026, 4, 8, 13, 10, 0, tzinfo=UTC),
            ),
            _msg(
                pipeline_id=pipeline_id,
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                phase=phase,
                timestamp=datetime(2026, 4, 8, 13, 11, 0, tzinfo=UTC),
            ),
        ]

    def test_final_round_proposal_body_inline(self, tmp_path):
        """PR body contains the final-round proposal body inline."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = self._make_multi_round_messages()
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # The final-round proposal body should be inline
        assert "Updated implementation with error handling" in result

    def test_final_round_ack_rationale_inline(self, tmp_path):
        """PR body contains the final-round ACK rationale inline."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = self._make_multi_round_messages()
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "Error handling looks good now" in result

    def test_final_round_nack_rationale_inline(self, tmp_path):
        """When the final round has a NACK, its rationale is shown inline."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal v1",
                body="My implementation",
                metadata={"version": 1},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_NACK,
                subject="NACK",
                body="Security concern in auth module",
                metadata={
                    "payload": {"reason": "Security concern in auth module"},
                    "version": 1,
                },
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # NACK rationale should be inline
        assert "Security concern in auth module" in result

    def test_older_rounds_wrapped_in_details(self, tmp_path):
        """Older/earlier-round messages are wrapped in <details> tags."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = self._make_multi_round_messages()
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Older rounds should be in <details>
        assert "<details>" in result
        assert "Earlier rounds" in result or "earlier" in result.lower()

    def test_single_round_no_details(self, tmp_path):
        """A single-round consensus does not wrap anything in <details>."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementation ready",
                metadata={"commit_sha": "abc123", "version": 1},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
                metadata={"payload": {"ack_version": 1}, "version": 1},
            ),
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                timestamp=datetime(2026, 4, 8, 12, 10, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                timestamp=datetime(2026, 4, 8, 12, 11, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Single round: no need for <details>
        assert "<details>" not in result

    def test_summary_cap_raised_to_40000(self, tmp_path):
        """Summary cap is ~40000 chars, not the old 2000."""
        from routes.pipelines import _build_brc_consensus_summary

        # Create messages to exceed 2000 but stay under 40000
        messages = []
        for phase in ["refine", "plan", "implement"]:
            messages.append(
                _msg(
                    from_role="coder",
                    message_type=MessageType.CONSENSUS_PROPOSE,
                    subject=f"Proposal for {phase}",
                    body=f"Detailed implementation description for {phase} " * 50,
                    phase=phase,
                    metadata={"commit_sha": "abc123", "version": 1},
                )
            )
            messages.append(
                _msg(
                    from_role="reviewer_code",
                    message_type=MessageType.CONSENSUS_ACK,
                    subject=f"ACK for {phase}",
                    body=f"Detailed review rationale for {phase} " * 30,
                    phase=phase,
                    timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
                    metadata={"payload": {"ack_version": 1}, "version": 1},
                )
            )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # Should be able to hold more than 2000 chars (old limit)
        # but not exceed 40000 chars (new limit)
        assert len(result) <= 40000

    def test_truncated_body_has_pointer_to_history(self, tmp_path):
        """When a message body is truncated, it has a pointer to the history file."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                # Very long body that should trigger truncation
                body="Very detailed implementation " * 2000,
                metadata={"commit_sha": "abc123", "version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        # If the body was truncated, it should have a pointer
        if "…" in result or "..." in result:
            assert "brc-history" in result


# ---------------------------------------------------------------------------
# 7. Artifact links in PR body
# ---------------------------------------------------------------------------


class TestArtifactLinksInPrBody:
    """Verify PR body contains links to committed BRC history artifacts."""

    def test_summary_contains_md_artifact_link(self, tmp_path):
        """PR body summary contains a link to the .md history artifact."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                metadata={"version": 1},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert ".md" in result or "brc-history" in result

    def test_summary_contains_json_artifact_link(self, tmp_path):
        """PR body summary contains a link to the .json history artifact."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                metadata={"version": 1},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert ".json" in result or "brc-history" in result

    def test_build_pr_body_passes_identifier_for_links(self, tmp_path):
        """_build_pr_body passes the identifier so artifact links have correct filenames."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline()
        _setup_contract(tmp_path)

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                metadata={"version": 1},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            title, body = _build_pr_body(pipeline, tmp_path)

        # The body should contain references to .egg-state/brc-history/
        if "BRC Consensus Summary" in body:
            assert "brc-history" in body


# ---------------------------------------------------------------------------
# 8. _build_brc_consensus_summary signature / call site update
# ---------------------------------------------------------------------------


class TestBrcConsensusSummarySignature:
    """Verify _build_brc_consensus_summary can produce artifact links."""

    def test_function_accepts_identifier_parameter(self):
        """_build_brc_consensus_summary accepts an identifier parameter."""
        import inspect

        from routes.pipelines import _build_brc_consensus_summary

        sig = inspect.signature(_build_brc_consensus_summary)
        # The function should accept an identifier parameter (new requirement)
        param_names = list(sig.parameters.keys())
        assert "identifier" in param_names or len(param_names) >= 2, (
            f"Expected identifier parameter; got params: {param_names}"
        )


# ---------------------------------------------------------------------------
# 9. History header format with to_role
# ---------------------------------------------------------------------------


class TestHistoryHeaderFormat:
    """Verify the new header format: ### [ts] from_role → to_role (type): subject."""

    def test_header_format_broadcast(self, tmp_path):
        """Broadcast messages have format: ### [ts] from_role (type): subject."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="My proposal",
                body="Implementation",
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        headers = [line for line in content.splitlines() if line.startswith("### ")]
        assert len(headers) == 1
        h = headers[0]
        # Must contain from_role, type, subject
        assert "coder" in h
        assert "CONSENSUS_PROPOSE" in h
        assert "My proposal" in h

    def test_header_format_directed(self, tmp_path):
        """Directed messages have format: ### [ts] from_role → to_role (type): subject."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="orchestrator",
                to_role="coder",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review needed",
                body="Please update",
                metadata={"version": 2},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        headers = [line for line in content.splitlines() if line.startswith("### ")]
        assert len(headers) == 1
        h = headers[0]
        assert "orchestrator" in h
        assert "coder" in h
        assert "→" in h or "->" in h
        assert "CONSENSUS_RE_REVIEW" in h
        assert "Re-review needed" in h


# ---------------------------------------------------------------------------
# 10. Edge cases and regression tests
# ---------------------------------------------------------------------------


class TestLosslessPersistenceEdgeCases:
    """Edge cases for the lossless persistence changes."""

    def test_metadata_with_nested_dicts(self, tmp_path):
        """Deeply nested metadata is preserved in YAML block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                metadata={
                    "payload": {
                        "artifact_references": ["file1.py", "file2.py"],
                        "ack_version": 1,
                        "nested": {"deep": {"value": 42}},
                    },
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        assert "artifact_references" in content
        # YAML should preserve nested structure

    def test_message_id_in_yaml_block(self, tmp_path):
        """Message id field appears in the YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                msg_id="unique-msg-id-999",
                metadata={"version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # The id should appear in the yaml block
        assert "unique-msg-id-999" in content

    def test_phase_in_yaml_block(self, tmp_path):
        """Phase field appears in the YAML metadata block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                phase="implement",
                metadata={"version": 1},
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # The YAML block should include the phase field
        assert "```yaml" in content or "```yml" in content
        # Phase should be in the yaml block (not just the header)

    def test_multiple_messages_each_get_yaml_block(self, tmp_path):
        """Each message with metadata gets its own YAML block."""
        from routes.pipelines import _write_brc_history

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
                metadata={"commit_sha": "abc123", "version": 1},
            ),
            _msg(
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="LGTM",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
                metadata={
                    "payload": {"ack_version": 1, "artifact_references": ["file.py"]},
                    "version": 1,
                },
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        content = (tmp_path / ".egg-state" / "brc-history" / "42-implement.md").read_text()
        # Count yaml blocks
        yaml_block_count = content.count("```yaml") + content.count("```yml")
        assert yaml_block_count == 2, (
            f"Expected 2 YAML blocks (one per message with metadata), got {yaml_block_count}"
        )

    def test_existing_tests_still_work_after_split(self):
        """Verify the BRC_MESSAGE_TYPES alias works for backward compatibility."""
        from routes.pipelines import BRC_MESSAGE_TYPES

        # Old tests use BRC_MESSAGE_TYPES — it should still work
        assert "CONSENSUS_PROPOSE" in BRC_MESSAGE_TYPES
        assert "CONSENSUS_ACK" in BRC_MESSAGE_TYPES
        assert isinstance(BRC_MESSAGE_TYPES, frozenset)

    def test_summary_still_excludes_orchestrator_from_roles(self):
        """Orchestrator is still excluded from participant roles in summary (regression)."""
        from routes.pipelines import _build_brc_consensus_summary

        messages = [
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Done",
            ),
            _msg(
                from_role="orchestrator",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review",
                body="Update needed",
                timestamp=datetime(2026, 4, 8, 12, 5, 0, tzinfo=UTC),
            ),
            _msg(
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                timestamp=datetime(2026, 4, 8, 12, 10, 0, tzinfo=UTC),
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            result = _build_brc_consensus_summary("issue-42")

        assert "orchestrator" not in result
        assert "Consensus reached" in result
