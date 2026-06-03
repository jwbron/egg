"""Tests for the runtime escape-hatch handlers (#2529).

Covers ``check_file_restriction`` (pure local read) and
``report_impasse`` (writes typed Impasse to the role's agent-output
file). Both back the ``egg-contract check-file-restriction`` and
``egg-contract report-impasse`` CLI subcommands; the
``mcp__sdlc__check_file_restriction`` / ``mcp__sdlc__report_impasse``
MCP tools they previously also backed were retired in #2908 slice-6.
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
    # No phase by default: the phase layer is a no-op so these role-layer
    # assertions stay deterministic regardless of the ambient EGG_PHASE.
    monkeypatch.delenv("EGG_PHASE", raising=False)


class TestCheckFileRestriction:
    def test_blocked_path_for_coder(self):
        # docs/ is documenter-owned: coder is blocked and documenter is the
        # sole producer alternative. (Used tests/test_x.py until #2936 let
        # the coder author its own tests, which made that path coder-writable
        # and left this assertion stale.)
        out = restrictions.check_file_restriction({"path": "docs/guide.md"})
        assert out["ok"] is True
        assert out["role"] == "coder"
        assert out["can_write"] is False
        assert out["alternative_role"] == "documenter"
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
        # docs/guide.md blocked (documenter alt), src/app.py allowed. (Was
        # tests/test_x.py until #2936 — see test_blocked_path_for_coder.)
        out = restrictions.check_file_restriction({"path": ["docs/guide.md", "src/app.py"]})
        assert out["ok"] is True
        assert len(out["results"]) == 2
        blocked = [r for r in out["results"] if not r["can_write"]]
        allowed = [r for r in out["results"] if r["can_write"]]
        assert len(blocked) == 1 and blocked[0]["alternative_role"] == "documenter"
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


class TestCheckFileRestrictionPhase:
    """The phase layer (#2968): can_write must also reflect the gateway's
    phase gate, not just the role pattern."""

    _PLAN_DRAFT = ".egg-state/drafts/pipeline-8cf1f000-plan.md"
    _ANALYSIS_DRAFT = ".egg-state/drafts/pipeline-8cf1f000-analysis.md"

    def test_refiner_plan_draft_blocked_by_phase_in_refine(self, monkeypatch):
        # The #2968 case: refiner's role pattern allows .egg-state/drafts/,
        # but the refine phase gate reserves *-plan.md to the plan phase.
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        monkeypatch.setenv("EGG_PHASE", "refine")
        out = restrictions.check_file_restriction({"path": self._PLAN_DRAFT})
        assert out["can_write"] is False
        assert out["role_can_write"] is True
        assert out["phase_allows"] is False
        assert out["blocked_by"] == "phase"
        assert out["phase"] == "refine"
        # No alternative role for a phase block — it's reserved phase-wide.
        assert out["alternative_role"] is None
        assert "phase_filter.py" in out["reason"]

    def test_refiner_plan_draft_writable_in_plan_phase(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        monkeypatch.setenv("EGG_PHASE", "plan")
        out = restrictions.check_file_restriction({"path": self._PLAN_DRAFT})
        assert out["can_write"] is True
        assert out["blocked_by"] is None

    def test_refiner_analysis_draft_writable_in_refine(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        monkeypatch.setenv("EGG_PHASE", "refine")
        out = restrictions.check_file_restriction({"path": self._ANALYSIS_DRAFT})
        assert out["can_write"] is True
        assert out["role_can_write"] is True
        assert out["phase_allows"] is True

    def test_explicit_phase_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        monkeypatch.setenv("EGG_PHASE", "plan")  # env says writable
        out = restrictions.check_file_restriction(
            {"path": self._PLAN_DRAFT, "phase": "refine"}  # arg says blocked
        )
        assert out["phase"] == "refine"
        assert out["can_write"] is False
        assert out["blocked_by"] == "phase"

    def test_role_block_takes_priority_over_phase(self, monkeypatch):
        # Contracts are blocked at BOTH layers for a coder in implement
        # (role: .egg-state/ minus carve-outs; phase: implement blocks
        # contracts). When both fire, blocked_by reports "role" so the
        # message points at the layer that might be delegable.
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
        monkeypatch.setenv("EGG_PHASE", "implement")
        out = restrictions.check_file_restriction({"path": ".egg-state/contracts/p.json"})
        assert out["can_write"] is False
        assert out["role_can_write"] is False
        assert out["phase_allows"] is False
        assert out["blocked_by"] == "role"

    def test_no_phase_env_is_role_only(self, monkeypatch):
        # Backward-compat: without EGG_PHASE the phase layer is a no-op,
        # so the refiner's role-level allowance stands.
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        monkeypatch.delenv("EGG_PHASE", raising=False)
        out = restrictions.check_file_restriction({"path": self._PLAN_DRAFT})
        assert out["can_write"] is True
        assert out["phase"] is None
        assert out["phase_allows"] is True

    def test_batch_form_carries_phase(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        monkeypatch.setenv("EGG_PHASE", "refine")
        out = restrictions.check_file_restriction(
            {"path": [self._ANALYSIS_DRAFT, self._PLAN_DRAFT]}
        )
        assert out["phase"] == "refine"
        by_path = {r["path"]: r for r in out["results"]}
        assert by_path[self._ANALYSIS_DRAFT]["can_write"] is True
        assert by_path[self._PLAN_DRAFT]["can_write"] is False
        assert by_path[self._PLAN_DRAFT]["blocked_by"] == "phase"

    def test_reviewer_impersonates_producer_via_explicit_args(self, monkeypatch):
        # #2968 secondary fix: a reviewer adjudicating a producer's proposal
        # needs to see the producer's verdict, not its own. With explicit
        # ``role`` + ``phase`` args, the reviewer's defaults
        # (EGG_AGENT_ROLE=reviewer_code, EGG_PHASE=implement) are overridden
        # and the tool returns what the gateway would have done to the
        # producer's refine-phase push of a plan draft.
        monkeypatch.setenv("EGG_AGENT_ROLE", "reviewer_code")
        monkeypatch.setenv("EGG_PHASE", "implement")
        out = restrictions.check_file_restriction(
            {
                "path": self._PLAN_DRAFT,
                "role": "refiner",
                "phase": "refine",
            }
        )
        assert out["role"] == "refiner"
        assert out["phase"] == "refine"
        assert out["can_write"] is False
        assert out["role_can_write"] is True
        assert out["phase_allows"] is False
        assert out["blocked_by"] == "phase"
        # No alternative role for a phase block — it's reserved phase-wide.
        assert out["alternative_role"] is None

    def test_non_string_phase_rejected(self, monkeypatch):
        # The schema declares ``phase`` a string; the handler boundary
        # rejects a malformed caller (int, dict, etc.) with a structured
        # HandlerError instead of leaking an AttributeError from
        # ``PipelinePhase(phase)``.
        monkeypatch.setenv("EGG_AGENT_ROLE", "refiner")
        with pytest.raises(HandlerError, match="'phase' must be a string"):
            restrictions.check_file_restriction({"path": self._PLAN_DRAFT, "phase": 7})


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
                    "task_id": "task-1-1",
                    "suggested_role": "tester",
                    "blocked_files": [1, 2],
                }
            )

    def test_wrong_role_without_suggested_role_rejected(self, tmp_path, monkeypatch):
        # ``wrong_role`` is the auto-delegateable category; without
        # suggested_role the orchestrator-side router can only escalate
        # to HITL, which silently degrades the producer's deliberately
        # set wrong_role signal. The handler boundary should reject
        # the call so the agent fixes the input in the same iteration.
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid")
        with pytest.raises(HandlerError, match="suggested_role.* is required"):
            restrictions.report_impasse(
                {
                    "category": "wrong_role",
                    "reason": "cannot write tests/conftest.py",
                    "task_id": "task-1-1",
                    "blocked_files": ["tests/conftest.py"],
                }
            )

    def test_wrong_role_without_task_id_rejected(self, tmp_path, monkeypatch):
        # Same defense-in-depth motivation — without task_id the
        # router's role-match fallback is fragile (multiple tasks per
        # role, role-less tasks). Require explicit task_id at the
        # handler boundary so routing never has to guess.
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid")
        with pytest.raises(HandlerError, match="task_id.* is required"):
            restrictions.report_impasse(
                {
                    "category": "wrong_role",
                    "reason": "cannot write tests/conftest.py",
                    "suggested_role": "tester",
                    "blocked_files": ["tests/conftest.py"],
                }
            )

    def test_plan_bug_without_task_id_accepted(self, tmp_path, monkeypatch):
        # Non-wrong_role categories don't auto-delegate, so the
        # task-level ambiguity that breaks the role-match fallback
        # never matters — keep these accepted without task_id so an
        # agent can flag a plan-wide impasse.
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EGG_PIPELINE_ID", "pid")
        out = restrictions.report_impasse(
            {
                "category": "plan_bug",
                "reason": "two slices contradict each other on schema shape",
            }
        )
        assert out["ok"] is True


# The MCP ``@tool``-decorated wrappers in ``egg_agent_tools.tools`` were
# retired in #2908 slice-6; the historical
# ``TestToolRegistration::test_mcp_names_registered`` lookup against
# ``egg_agent_tools.tools.sdlc.REGISTRATIONS`` no longer applies.  The
# CLI surface registration is covered by
# ``tests/sandbox/egg_lib/test_orch_cli_brc.py`` and
# ``integration_tests/test_sandbox_mcp_tools_e2e.py``.
