"""Tests for the runtime escape-hatch handlers (#2529).

Covers ``check_file_restriction`` (pure local read) and
``report_impasse`` (writes typed Impasse to the role's agent-output
file). Both back the ``mcp__sdlc__check_file_restriction`` and
``mcp__sdlc__report_impasse`` tools registered in
``sandbox/egg_agent_tools/tools/sdlc.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The host-side test harness mirrors the sandbox-image layout via
# PYTHONPATH; insert here for IDE / pytest -m runs that don't go
# through the Makefile.
_SANDBOX_DIR = Path(__file__).resolve().parent.parent
_SHARED_DIR = _SANDBOX_DIR.parent / "shared"
for p in (_SHARED_DIR, _SANDBOX_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from egg_agent_tools.handlers import restrictions  # noqa: E402
from egg_agent_tools.handlers.errors import HandlerError  # noqa: E402


@pytest.fixture(autouse=True)
def _set_role(monkeypatch):
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)
    monkeypatch.delenv("EGG_ISSUE_NUMBER", raising=False)


class TestCheckFileRestriction:
    def test_blocked_path_for_coder(self):
        out = restrictions.check_file_restriction({"path": "tests/test_x.py"})
        assert out["ok"] is True
        assert out["role"] == "coder"
        assert out["can_write"] is False
        assert out["alternative_role"] == "tester"
        assert "blocked" in out["reason"]

    def test_allowed_path_for_coder(self):
        out = restrictions.check_file_restriction({"path": "orchestrator/routes/pipelines.py"})
        assert out["can_write"] is True
        assert out["alternative_role"] is None

    def test_github_path_no_alternative(self):
        # `.github/` is hard-blocked for every producer (#2508), so
        # alternative_role MUST be None — we don't want to misroute
        # the agent into a follow-on impasse.
        out = restrictions.check_file_restriction({"path": ".github/workflows/ci.yml"})
        assert out["can_write"] is False
        assert out["alternative_role"] is None

    def test_batch_form(self):
        out = restrictions.check_file_restriction({"path": ["tests/test_x.py", "src/app.py"]})
        assert out["ok"] is True
        assert len(out["results"]) == 2
        blocked = [r for r in out["results"] if not r["can_write"]]
        allowed = [r for r in out["results"] if r["can_write"]]
        assert len(blocked) == 1 and blocked[0]["alternative_role"] == "tester"
        assert len(allowed) == 1

    def test_explicit_role_override(self):
        # Tester should be allowed to write conftest.py.
        out = restrictions.check_file_restriction({"path": "tests/conftest.py", "role": "tester"})
        assert out["role"] == "tester"
        assert out["can_write"] is True

    def test_unknown_role_raises(self):
        with pytest.raises(HandlerError):
            restrictions.check_file_restriction({"path": "x.py", "role": "wizard"})

    def test_empty_path_raises(self):
        with pytest.raises(HandlerError):
            restrictions.check_file_restriction({"path": ""})

    def test_empty_list_raises(self):
        with pytest.raises(HandlerError):
            restrictions.check_file_restriction({"path": []})

    def test_no_role_no_env_raises(self, monkeypatch):
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        with pytest.raises(HandlerError):
            restrictions.check_file_restriction({"path": "x.py"})


class TestReportImpasse:
    def test_persists_to_agent_output(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid-42")

        out = restrictions.report_impasse(
            {
                "category": "wrong_role",
                "reason": "task lists tests/conftest.py but coder cannot write it",
                "task_id": "task-1-1",
                "suggested_role": "tester",
                "blocked_files": ["tests/conftest.py"],
                "evidence": {"detected_by": "check_file_restriction"},
            }
        )
        assert out["ok"] is True
        assert out["category"] == "wrong_role"
        assert out["suggested_role"] == "tester"
        assert "Stop all further work" in out["guidance"]

        on_disk = Path(out["written_to"])
        assert on_disk.exists()
        data = json.loads(on_disk.read_text())
        assert data["impasse"]["category"] == "wrong_role"
        assert data["impasse"]["suggested_role"] == "tester"
        assert data["impasse"]["task_id"] == "task-1-1"
        assert data["impasse"]["blocked_files"] == ["tests/conftest.py"]

    def test_preserves_pre_existing_handoff_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid-99")

        # Simulate a prior write of handoff_data on the same role-keyed
        # output file (the common case is that report_impasse comes
        # before any handoff write, but we shouldn't clobber if not).
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        existing = outputs_dir / "pid-99-coder-output.json"
        existing.write_text(json.dumps({"role": "coder", "handoff_data": {"foo": "bar"}}))

        restrictions.report_impasse(
            {
                "category": "plan_bug",
                "reason": "task contradicts itself",
            }
        )
        data = json.loads(existing.read_text())
        assert data["handoff_data"] == {"foo": "bar"}
        assert data["impasse"]["category"] == "plan_bug"

    def test_self_delegation_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid")

        with pytest.raises(HandlerError, match="cannot delegate to itself"):
            restrictions.report_impasse(
                {
                    "category": "wrong_role",
                    "reason": "x",
                    "suggested_role": "coder",
                }
            )

    def test_unknown_category_rejected(self):
        with pytest.raises(HandlerError, match="Unknown category"):
            restrictions.report_impasse({"category": "wat", "reason": "x"})

    def test_empty_reason_rejected(self):
        with pytest.raises(HandlerError, match="reason"):
            restrictions.report_impasse({"category": "wrong_role", "reason": ""})

    def test_blocked_files_must_be_strings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid")
        with pytest.raises(HandlerError, match="blocked_files"):
            restrictions.report_impasse(
                {
                    "category": "wrong_role",
                    "reason": "x",
                    "blocked_files": [1, 2],
                }
            )


class TestToolRegistration:
    def test_mcp_names_registered(self):
        from egg_agent_tools.tools.sdlc import REGISTRATIONS

        names = {r.name for r in REGISTRATIONS}
        assert "mcp__sdlc__check_file_restriction" in names
        assert "mcp__sdlc__report_impasse" in names
