"""
Tests for overseer agent rules file and CLAUDE.md integration.

Verifies that the overseer.md rules file exists and covers all
required monitoring behaviors specified in issue #1059.
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent


class TestOverseerRulesFile:
    """Tests for sandbox/.claude/rules/overseer.md."""

    def test_overseer_rules_file_exists(self):
        """overseer.md rules file must exist."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        assert rules_path.exists(), "sandbox/.claude/rules/overseer.md does not exist"

    def test_overseer_rules_covers_monitoring_loop(self):
        """Rules file must describe the monitoring loop."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "Monitoring Loop" in content

    def test_overseer_rules_covers_stall_detection(self):
        """Rules file must describe adaptive stall detection."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "Stall Detection" in content or "stall" in content.lower()

    def test_overseer_rules_covers_corrective_action(self):
        """Rules file must describe the corrective action ladder."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "Corrective Action" in content
        assert "Nudge" in content
        assert "Redirect" in content
        assert "HITL" in content or "escalation" in content.lower()

    def test_overseer_rules_covers_issue_filing(self):
        """Rules file must describe autonomous issue filing."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "issue" in content.lower() and "filing" in content.lower() or "gh issue create" in content

    def test_overseer_rules_covers_self_monitoring(self):
        """Rules file must describe self-monitoring."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "Self-Monitoring" in content or "self-monitoring" in content

    def test_overseer_rules_covers_coordinator_monitoring(self):
        """Rules file must describe coordinator monitoring."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "Coordinator Monitoring" in content or "coordinator" in content.lower()

    def test_overseer_rules_covers_completion_summary(self):
        """Rules file must describe completion summary."""
        rules_path = _project_root / "sandbox" / ".claude" / "rules" / "overseer.md"
        content = rules_path.read_text()
        assert "Completion Summary" in content or "health summary" in content.lower()


class TestOverseerInEntrypoint:
    """Tests that overseer.md is included in the combined CLAUDE.md."""

    def test_overseer_in_rules_order(self):
        """entrypoint.py must include overseer.md in rules_order list."""
        entrypoint_path = _project_root / "sandbox" / "entrypoint.py"
        content = entrypoint_path.read_text()
        assert "overseer.md" in content, (
            "entrypoint.py rules_order must include overseer.md"
        )


class TestOverseerInMission:
    """Tests that the overseer is documented in mission.md concurrent mode."""

    def test_overseer_in_concurrent_patterns(self):
        """mission.md must include overseer collaboration pattern."""
        mission_path = _project_root / "sandbox" / ".claude" / "rules" / "mission.md"
        content = mission_path.read_text()
        assert "Overseer" in content and "concurrent" in content.lower()

    def test_overseer_failure_handling(self):
        """mission.md must document overseer failure handling."""
        mission_path = _project_root / "sandbox" / ".claude" / "rules" / "mission.md"
        content = mission_path.read_text()
        assert "Overseer fails" in content
