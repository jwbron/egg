"""Tests for ``cmd_consensus_status`` rendering of pre-merge obligations (#2006).

The CLI should surface a "Pending pre-merge obligations" subsection when the
consensus payload carries non-empty ``pre_merge_conditions``. When absent, the
output must be unchanged so humans scanning a clean pipeline don't see noise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_lib.orch_cli import cmd_consensus_status


def _make_args() -> argparse.Namespace:
    return argparse.Namespace(pipeline_id="test-pipeline-1", json=False)


@pytest.fixture(autouse=True)
def _pipeline_env(monkeypatch):
    # cmd_consensus_status calls require_pipeline_id which reads EGG_PIPELINE_ID
    # as a fallback — be explicit so the args value wins regardless.
    monkeypatch.setenv("EGG_PIPELINE_ID", "test-pipeline-1")


class TestStatusRendersConditions:
    def test_obligations_rendered_when_present(self, capsys):
        fake_state = {
            "consensus": {
                "is_complete": False,
                "agents": {
                    "coder": {"producer_phase": "CONFIRMED", "confirmed": True},
                    "reviewer_code": {"reviewer_phase": "CONFIRMED", "confirmed": True},
                },
                "blocking_agents": [],
                "pre_merge_conditions": [
                    {
                        "reviewer": "reviewer_code",
                        "producer": "coder",
                        "condition": "git mv legacy/x new/x before merge",
                        "version": 1,
                    }
                ],
            }
        }
        with patch(
            "egg_agent_tools.handlers.brc.brc_get_state",
            return_value=fake_state,
        ):
            rc = cmd_consensus_status(_make_args())
        out = capsys.readouterr().out
        assert rc == 0
        assert "Pending pre-merge obligations:" in out
        assert "reviewer_code" in out
        assert "coder" in out
        assert "git mv legacy/x new/x before merge" in out

    def test_no_obligations_section_when_absent(self, capsys):
        fake_state = {
            "consensus": {
                "is_complete": True,
                "agents": {
                    "coder": {"producer_phase": "CONFIRMED", "confirmed": True},
                },
                "blocking_agents": [],
                "pre_merge_conditions": [],
            }
        }
        with patch(
            "egg_agent_tools.handlers.brc.brc_get_state",
            return_value=fake_state,
        ):
            rc = cmd_consensus_status(_make_args())
        out = capsys.readouterr().out
        assert rc == 0
        assert "Pending pre-merge obligations" not in out

    def test_missing_key_is_treated_as_empty(self, capsys):
        """Old tracker payloads without the field must not crash or render."""
        fake_state = {
            "consensus": {
                "is_complete": True,
                "agents": {},
                "blocking_agents": [],
            }
        }
        with patch(
            "egg_agent_tools.handlers.brc.brc_get_state",
            return_value=fake_state,
        ):
            rc = cmd_consensus_status(_make_args())
        out = capsys.readouterr().out
        assert rc == 0
        assert "Pending pre-merge obligations" not in out
