"""
Tests for consensus wrapper anchor recovery integration.

Covers:
- Recovery prompt includes anchor data when AGENT_ANCHOR_ID is set
- Recovery works without anchor (backward compatibility)
- Anchor state template substitution in shell script
"""

import json
import sys
from pathlib import Path

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


class TestRecoveryPromptWithAnchor:
    """Tests for recovery prompt anchor integration."""

    def test_recovery_prompt_contains_anchor_placeholder(self):
        """Recovery system prompt template includes anchor_state placeholder."""
        from consensus_wrapper import _RECOVERY_SYSTEM_PROMPT

        assert "{anchor_state}" in _RECOVERY_SYSTEM_PROMPT, (
            "Recovery prompt should include {anchor_state} placeholder"
        )

    def test_recovery_prompt_renders_with_anchor_data(self):
        """Recovery prompt renders correctly with anchor data."""
        from consensus_wrapper import _RECOVERY_SYSTEM_PROMPT

        anchor_data = {
            "agent_id": "coder-abc12345",
            "role": "coder",
            "task": "Implement anchor mechanism",
            "status": "in_progress",
        }

        rendered = _RECOVERY_SYSTEM_PROMPT.format(
            restart_number=1,
            max_restarts=2,
            brc_state=json.dumps({"coder": {"status": "proposed"}}),
            nack_feedback="",
            anchor_state=json.dumps(anchor_data, indent=2),
        )

        assert "coder-abc12345" in rendered
        assert "anchor mechanism" in rendered

    def test_recovery_prompt_renders_without_anchor(self):
        """Recovery prompt renders correctly with empty anchor (backward compat)."""
        from consensus_wrapper import _RECOVERY_SYSTEM_PROMPT

        rendered = _RECOVERY_SYSTEM_PROMPT.format(
            restart_number=1,
            max_restarts=2,
            brc_state="{}",
            nack_feedback="",
            anchor_state="",
        )

        # Should still have the core recovery instructions
        assert "restart" in rendered.lower()
        assert "BRC" in rendered


class TestWrapperTemplateAnchor:
    """Tests for anchor loading in the shell wrapper template."""

    def test_wrapper_template_checks_agent_anchor_id(self):
        """Wrapper template checks AGENT_ANCHOR_ID env var."""
        from consensus_wrapper import _CONSENSUS_WRAPPER_TEMPLATE

        assert "AGENT_ANCHOR_ID" in _CONSENSUS_WRAPPER_TEMPLATE

    def test_wrapper_template_calls_anchor_show(self):
        """Wrapper template calls egg-orch anchor show for anchor data."""
        from consensus_wrapper import _CONSENSUS_WRAPPER_TEMPLATE

        assert "egg-orch anchor show" in _CONSENSUS_WRAPPER_TEMPLATE

    def test_wrapper_template_has_anchor_substitution(self):
        """Wrapper template includes _CW_ANCHOR in template substitution."""
        from consensus_wrapper import _CONSENSUS_WRAPPER_TEMPLATE

        assert "_CW_ANCHOR" in _CONSENSUS_WRAPPER_TEMPLATE
        assert "anchor_state" in _CONSENSUS_WRAPPER_TEMPLATE

    def test_build_command_includes_anchor_in_mapping(self):
        """build_consensus_wrapped_command produces script with anchor handling."""
        from consensus_wrapper import build_consensus_wrapped_command

        cmd = build_consensus_wrapped_command("Test prompt")
        assert len(cmd) == 3
        assert cmd[0] == "bash"
        assert cmd[1] == "-c"
        script = cmd[2]
        assert "_CW_ANCHOR" in script
        assert "anchor_state" in script
