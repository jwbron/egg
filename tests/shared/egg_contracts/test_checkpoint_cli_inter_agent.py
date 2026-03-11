"""Tests for inter-agent message display in checkpoint CLI.

Covers print_checkpoint_details output for inter_agent_messages field,
including direction arrows, grouping by type, and edge cases.
"""

from datetime import UTC, datetime

from egg_contracts.checkpoint_cli import print_checkpoint_details
from egg_contracts.checkpoints import (
    CheckpointV2,
    InterAgentMessage,
    SessionMetadata,
    TokenUsage,
    Transcript,
    TriggerType,
)


def _make_checkpoint(**kwargs) -> CheckpointV2:
    """Create a minimal CheckpointV2 for testing display."""
    now = datetime.now(UTC)
    defaults = {
        "id": "ckpt-aabbccdd1122",
        "trigger_type": TriggerType.COMMIT,
        "commit_sha": "abc1234567890123456789012345678901234567",
        "push_sha": "abc1234567890123456789012345678901234567",
        "branch": "egg/test",
        "session_id": "test-session",
        "session": SessionMetadata(session_id="test-session", started_at=now),
        "transcript": Transcript(messages=[], message_count=0),
        "token_usage": TokenUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        "created_at": now,
        "session_started_at": now,
        "inter_agent_messages": [],
    }
    defaults.update(kwargs)
    return CheckpointV2(**defaults)


class TestPrintCheckpointDetailsInterAgentMessages:
    """Tests for inter-agent message display in print_checkpoint_details."""

    def test_no_messages_no_section(self, capsys):
        """Should not print inter-agent section when no messages exist."""
        ckpt = _make_checkpoint(inter_agent_messages=[])
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        assert "Inter-Agent Messages" not in output

    def test_messages_shown_with_count(self, capsys):
        """Should show message count header."""
        messages = [
            InterAgentMessage(
                id="msg-1",
                pipeline_id="issue-999",
                from_role="coder",
                to_role="all",
                message_type="PROGRESS",
                subject="API done",
                body="Finished",
                timestamp=datetime(2026, 3, 11, 10, 0, 0, tzinfo=UTC),
                direction="sent",
            ),
            InterAgentMessage(
                id="msg-2",
                pipeline_id="issue-999",
                from_role="tester",
                to_role="coder",
                message_type="QUESTION",
                subject="Expected status?",
                body="What code?",
                timestamp=datetime(2026, 3, 11, 10, 5, 0, tzinfo=UTC),
                direction="received",
            ),
        ]
        ckpt = _make_checkpoint(inter_agent_messages=messages)
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        assert "Inter-Agent Messages: 2" in output
        assert "Sent: 1, Received: 1" in output

    def test_sent_message_shows_arrow_right(self, capsys):
        """Sent messages should show -> arrow and to_role."""
        messages = [
            InterAgentMessage(
                id="msg-1",
                pipeline_id="issue-999",
                from_role="coder",
                to_role="tester",
                message_type="RESPONSE",
                subject="Use 400",
                body="",
                timestamp=datetime(2026, 3, 11, 10, 0, 0, tzinfo=UTC),
                direction="sent",
            ),
        ]
        ckpt = _make_checkpoint(inter_agent_messages=messages)
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        assert "-> tester: [RESPONSE] Use 400" in output

    def test_received_message_shows_arrow_left(self, capsys):
        """Received messages should show <- arrow and from_role."""
        messages = [
            InterAgentMessage(
                id="msg-1",
                pipeline_id="issue-999",
                from_role="tester",
                to_role="coder",
                message_type="QUESTION",
                subject="Need help",
                body="",
                timestamp=datetime(2026, 3, 11, 10, 0, 0, tzinfo=UTC),
                direction="received",
            ),
        ]
        ckpt = _make_checkpoint(inter_agent_messages=messages)
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        assert "<- tester: [QUESTION] Need help" in output

    def test_message_type_grouping(self, capsys):
        """Should group messages by type with counts."""
        messages = [
            InterAgentMessage(
                id=f"msg-{i}",
                pipeline_id="issue-999",
                from_role="coder",
                to_role="all",
                message_type="PROGRESS",
                subject=f"Step {i}",
                timestamp=datetime(2026, 3, 11, 10, 0, 0, tzinfo=UTC),
                direction="sent",
            )
            for i in range(3)
        ] + [
            InterAgentMessage(
                id="msg-4",
                pipeline_id="issue-999",
                from_role="tester",
                to_role="coder",
                message_type="QUESTION",
                subject="Clarify",
                timestamp=datetime(2026, 3, 11, 10, 5, 0, tzinfo=UTC),
                direction="received",
            ),
        ]
        ckpt = _make_checkpoint(inter_agent_messages=messages)
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        assert "PROGRESS: 3" in output
        assert "QUESTION: 1" in output

    def test_unknown_direction_shows_arrow_left(self, capsys):
        """Messages with unknown direction should show <- arrow."""
        messages = [
            InterAgentMessage(
                id="msg-1",
                pipeline_id="issue-999",
                from_role="system",
                to_role="coder",
                message_type="AGENT_FAILED",
                subject="Tester failed",
                timestamp=datetime(2026, 3, 11, 10, 0, 0, tzinfo=UTC),
                direction="unknown",
            ),
        ]
        ckpt = _make_checkpoint(inter_agent_messages=messages)
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        # Unknown direction defaults to <- (else branch)
        assert "<- system: [AGENT_FAILED] Tester failed" in output

    def test_dict_input_also_works(self, capsys):
        """print_checkpoint_details accepts dict input; inter-agent messages should still display."""
        data = {
            "id": "ckpt-aabbccdd1122",
            "trigger_type": "commit",
            "commit_sha": "abc1234567890123456789012345678901234567",
            "branch": "egg/test",
            "session_id": "test-session",
            "created_at": "2026-03-11T10:00:00+00:00",
            "session": {"session_id": "test-session", "started_at": "2026-03-11T10:00:00+00:00"},
            "token_usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            "inter_agent_messages": [
                {
                    "id": "msg-1",
                    "pipeline_id": "issue-999",
                    "from_role": "coder",
                    "to_role": "all",
                    "message_type": "PROGRESS",
                    "subject": "Done",
                    "body": "",
                    "timestamp": "2026-03-11T10:00:00+00:00",
                    "direction": "sent",
                },
            ],
        }
        print_checkpoint_details(data)

        output = capsys.readouterr().out
        assert "Inter-Agent Messages: 1" in output
        assert "Sent: 1, Received: 0" in output

    def test_all_sent_no_received(self, capsys):
        """Should handle case where all messages are sent."""
        messages = [
            InterAgentMessage(
                id="msg-1",
                pipeline_id="issue-999",
                from_role="coder",
                to_role="all",
                message_type="STATUS",
                subject="Working",
                timestamp=datetime(2026, 3, 11, 10, 0, 0, tzinfo=UTC),
                direction="sent",
            ),
        ]
        ckpt = _make_checkpoint(inter_agent_messages=messages)
        print_checkpoint_details(ckpt)

        output = capsys.readouterr().out
        assert "Sent: 1, Received: 0" in output
