"""Regression test pinning the context-PR file-glob set against real
production filenames (#2548 review finding by reviewer_code).

The original ``_CONTEXT_PR_FILE_GLOBS`` used phase-prefix globs
(``<id>-refine-*.{md,json}`` and ``<id>-plan-*.{md,json}``) that
silently matched no files in production: agent transcripts are
emitted by ``save_agent_output`` as ``<id>-<role>-output.{json,md}``
(per shared/egg_contracts/orchestrator.py:386), NOT a phase-prefix
shape. The fix derives the glob set from
``get_roles_for_phase("refine")`` / ``get_roles_for_phase("plan")``
so the orchestrator's actual file emission is matched.

This test seeds the canonical filenames from production
(``<id>-architect-output.json``, ``<id>-task_planner-output.json``,
etc.) and asserts they are picked up by ``_gather_context_pr_files``.
A regression to the phase-prefix shape would silently drop the
agent transcripts and the test would fail.
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def work_worktree(tmp_path: Path) -> Path:
    """Build a minimal `.egg-state/` layout that mirrors what the
    orchestrator emits during a refine + plan phase."""
    state = tmp_path / ".egg-state"
    drafts = state / "drafts"
    brc = state / "brc-history"
    outs = state / "agent-outputs"
    for d in (drafts, brc, outs):
        d.mkdir(parents=True)

    identifier = "2548"

    # Drafts and aggregate BRC files (these matched even with the old
    # globs; included so the regression catches a future regression
    # affecting just one of the two surfaces).
    (drafts / f"{identifier}-analysis.md").write_text("# analysis\n")
    (drafts / f"{identifier}-plan.md").write_text("# plan\n")
    (brc / f"{identifier}-refine.json").write_text("{}")
    (brc / f"{identifier}-refine.md").write_text("# refine BRC\n")
    (brc / f"{identifier}-plan.json").write_text("{}")
    (brc / f"{identifier}-plan.md").write_text("# plan BRC\n")

    # Agent transcripts: the canonical
    # ``<identifier>-<role>-output.{json,md}`` shape that the original
    # phase-prefix globs failed to match.
    (outs / f"{identifier}-architect-output.json").write_text(json.dumps({"role": "architect"}))
    (outs / f"{identifier}-risk_analyst-output.json").write_text(
        json.dumps({"role": "risk_analyst"})
    )
    (outs / f"{identifier}-refiner-output.json").write_text(json.dumps({"role": "refiner"}))
    (outs / f"{identifier}-task_planner-output.json").write_text(
        json.dumps({"role": "task_planner"})
    )
    (outs / f"{identifier}-reviewer_refine-output.json").write_text(
        json.dumps({"role": "reviewer_refine"})
    )
    (outs / f"{identifier}-reviewer_plan-output.json").write_text(
        json.dumps({"role": "reviewer_plan"})
    )
    (outs / f"{identifier}-reviewer_agent_design-output.json").write_text(
        json.dumps({"role": "reviewer_agent_design"})
    )

    # A `.md` companion that some roles also emit alongside the JSON
    # output — the suffix list must catch both extensions.
    (outs / f"{identifier}-architect-output.md").write_text("# architect transcript\n")

    # Cross-pipeline noise that must NOT be picked up (different
    # identifier prefix → glob anchors filter it out).
    (outs / "9999-architect-output.json").write_text(json.dumps({"id": "9999"}))

    # Implement-phase agent output that should NOT appear on the
    # context PR (coder runs after the hook fires; safety check
    # against accidental over-broad globs that pull in implement
    # transcripts on a retry).
    (outs / f"{identifier}-coder-output.json").write_text(json.dumps({"role": "coder"}))
    (outs / f"{identifier}-tester-output.json").write_text(json.dumps({"role": "tester"}))

    return tmp_path


def test_glob_set_picks_up_canonical_agent_transcripts(work_worktree: Path) -> None:
    """The hook must pick up the orchestrator's actual agent-output
    filenames (``<id>-<role>-output.{json,md}``).  Regression for the
    silent no-op fixed in #2548 review."""
    from routes.pipelines import _gather_context_pr_files

    files = _gather_context_pr_files(work_worktree, "2548")
    rel = sorted(str(p.relative_to(work_worktree)) for p in files)

    # Drafts and BRC files always present.
    assert ".egg-state/drafts/2548-analysis.md" in rel
    assert ".egg-state/drafts/2548-plan.md" in rel
    assert ".egg-state/brc-history/2548-refine.json" in rel
    assert ".egg-state/brc-history/2548-refine.md" in rel
    assert ".egg-state/brc-history/2548-plan.json" in rel
    assert ".egg-state/brc-history/2548-plan.md" in rel

    # Refine-phase agent transcripts (producers + reviewers).
    assert ".egg-state/agent-outputs/2548-architect-output.json" in rel
    assert ".egg-state/agent-outputs/2548-architect-output.md" in rel
    assert ".egg-state/agent-outputs/2548-risk_analyst-output.json" in rel
    assert ".egg-state/agent-outputs/2548-refiner-output.json" in rel
    assert ".egg-state/agent-outputs/2548-reviewer_refine-output.json" in rel
    assert ".egg-state/agent-outputs/2548-reviewer_agent_design-output.json" in rel

    # Plan-phase agent transcripts.
    assert ".egg-state/agent-outputs/2548-task_planner-output.json" in rel
    assert ".egg-state/agent-outputs/2548-reviewer_plan-output.json" in rel


def test_glob_set_excludes_other_pipelines(work_worktree: Path) -> None:
    """A different pipeline's transcript with a foreign identifier
    prefix must NOT leak into the context PR's diff."""
    from routes.pipelines import _gather_context_pr_files

    files = _gather_context_pr_files(work_worktree, "2548")
    rel = [str(p.relative_to(work_worktree)) for p in files]

    assert ".egg-state/agent-outputs/9999-architect-output.json" not in rel


def test_glob_set_excludes_implement_phase_outputs(work_worktree: Path) -> None:
    """Coder / tester outputs (implement-phase roles) must NOT be
    matched: the hook fires at the plan→implement boundary, but a
    retry path could see implement-phase outputs and they would
    pollute the context PR's review surface."""
    from routes.pipelines import _gather_context_pr_files

    files = _gather_context_pr_files(work_worktree, "2548")
    rel = [str(p.relative_to(work_worktree)) for p in files]

    assert ".egg-state/agent-outputs/2548-coder-output.json" not in rel
    assert ".egg-state/agent-outputs/2548-tester-output.json" not in rel


def test_symlinks_are_dropped(tmp_path: Path) -> None:
    """Defense-in-depth (#2548 review by reviewer_security): a
    symlink under ``.egg-state/drafts/`` must NOT be followed —
    ``shutil.copy2(follow_symlinks=True)`` would otherwise dereference
    it onto the publicly-reviewable context PR."""
    from routes.pipelines import _gather_context_pr_files

    state = tmp_path / ".egg-state" / "drafts"
    state.mkdir(parents=True)

    target = tmp_path / "secret"
    target.write_text("SECRET")

    symlink = state / "2548-analysis.md"
    symlink.symlink_to(target)

    files = _gather_context_pr_files(tmp_path, "2548")
    assert symlink not in files
    assert all(not p.is_symlink() for p in files)


def test_role_roster_is_dynamic() -> None:
    """The role list MUST be derived from
    ``get_roles_for_phase`` rather than hardcoded — pin that the
    helper returns a non-empty list when the import is available
    (the production fallback returns ``[]`` only when import fails)."""
    from routes.pipelines import _refine_and_plan_role_values

    roles = _refine_and_plan_role_values()
    # Refine + plan roster must include at least the canonical
    # producers; a future role rename would surface here as a clear
    # signal rather than a silent skip.
    assert "architect" in roles
    assert "task_planner" in roles
    assert "risk_analyst" in roles
    assert "refiner" in roles
