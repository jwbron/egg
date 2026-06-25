"""SDLC pipeline-produced PRs must skip the GitHub-level code reviewer and the
check autofixer.

The egg orchestrator opens exactly two branch shapes — ``egg/<id>/slice-<N>``
and ``egg/<id>/work`` — and both are already reviewed (and check-fixed) in-band
by the pipeline itself. Re-running the GitHub-level egg-reviewer and autofixer
on them is redundant and, because both push commits, pollutes slice history
(#3255). These tests pin the skip gates so a refactor can't silently re-enable
the bots on pipeline PRs.

Crucially, the code-review skip must live in ``on-pull-request.yml`` (the
egg-reviewer caller), NOT in the shared ``reusable-review.yml`` — the
contract-verification caller (``on-pull-request-contract-verify.yml``) also
calls ``reusable-review.yml`` and MUST keep verifying slice PRs (#3040).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REVIEW_CALLER = WORKFLOWS / "on-pull-request.yml"
AUTOFIX_CALLER = WORKFLOWS / "on-check-failure.yml"
REUSABLE_REVIEW = WORKFLOWS / "reusable-review.yml"

# The two pipeline branch shapes, as bash regexes. Both gates must match both.
SLICE_RE = "^egg/.+/slice-[0-9]+$"
WORK_RE = "^egg/.+/work$"


def _job(workflow: Path, job_id: str) -> dict:
    if not workflow.exists():
        pytest.skip(f"{workflow} not found — repo is in an unexpected state")
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    job = data.get("jobs", {}).get(job_id)
    assert job is not None, f"missing `{job_id}` job in {workflow.name}"
    return job


def _step_run(job: dict, step_id: str) -> str:
    steps = job.get("steps", [])
    matches = [s for s in steps if isinstance(s, dict) and s.get("id") == step_id]
    assert matches, f"missing step (id: {step_id})"
    return matches[0].get("run", "")


# --------------------------------------------------------------------------- #
# Code review (on-pull-request.yml)
# --------------------------------------------------------------------------- #


def test_review_gate_skips_both_pipeline_shapes() -> None:
    """The egg-reviewer should-run gate skips slice AND work branches."""
    script = _step_run(_job(REVIEW_CALLER, "should-run"), "check")
    assert SLICE_RE in script, (
        "on-pull-request.yml should-run does not skip slice PRs "
        f"({SLICE_RE}) — the code reviewer will run on pipeline slice PRs"
    )
    assert WORK_RE in script, (
        "on-pull-request.yml should-run does not skip work→main PRs "
        f"({WORK_RE}) — the code reviewer will run on pipeline work PRs"
    )
    assert "run=false" in script, "the skip branch does not set run=false (inert skip)"


def test_review_gate_allows_manual_dispatch() -> None:
    """workflow_dispatch is an explicit override and must always run."""
    script = _step_run(_job(REVIEW_CALLER, "should-run"), "check")
    assert "workflow_dispatch" in script and "run=true" in script, (
        "on-pull-request.yml should-run does not force a run on workflow_dispatch — "
        "the manual escape hatch is gone"
    )


def test_review_job_is_gated_on_should_run() -> None:
    """The review job must actually consume the gate, not just define it."""
    review = _job(REVIEW_CALLER, "review")
    needs = review.get("needs", [])
    assert "should-run" in needs, (
        "review job does not `needs: should-run` — the skip gate is not wired in"
    )
    cond = review.get("if", "")
    assert "should-run" in cond and "run" in cond, (
        "review job has no `if: needs.should-run.outputs.run == 'true'` — "
        "the gate output is not consulted"
    )


def test_review_skip_lives_in_caller_not_shared_workflow() -> None:
    """The broad pipeline-PR skip must NOT be in reusable-review.yml, which the
    contract-verification caller shares and which must keep verifying slice PRs
    (#3040). reusable-review keeps only its narrower multi-slice context skip.
    """
    reusable_check = _step_run(_job(REUSABLE_REVIEW, "should-run"), "check")
    assert SLICE_RE not in reusable_check, (
        "reusable-review.yml should-run skips slice PRs — this would also "
        "suppress contract verification on every slice PR (#3040)"
    )


# --------------------------------------------------------------------------- #
# Check autofixer (on-check-failure.yml)
# --------------------------------------------------------------------------- #


def test_autofix_gate_skips_both_pipeline_shapes() -> None:
    """The autofix should-run gate skips slice AND work branches."""
    script = _step_run(_job(AUTOFIX_CALLER, "should-run"), "check")
    assert SLICE_RE in script, (
        "on-check-failure.yml should-run does not skip slice PRs "
        f"({SLICE_RE}) — the autofixer will push commits onto pipeline slice PRs"
    )
    assert WORK_RE in script, (
        "on-check-failure.yml should-run does not skip work→main PRs "
        f"({WORK_RE}) — the autofixer will run on pipeline work PRs"
    )
    assert "head_branch" in script.lower(), (
        "on-check-failure.yml should-run does not read the triggering run's "
        "head_branch — it has no branch to gate on"
    )


def test_autofix_gate_allows_manual_dispatch() -> None:
    """workflow_dispatch is an explicit override and must always run."""
    script = _step_run(_job(AUTOFIX_CALLER, "should-run"), "check")
    assert "workflow_dispatch" in script and "run=true" in script, (
        "on-check-failure.yml should-run does not force a run on workflow_dispatch — "
        "the manual escape hatch is gone"
    )


# --------------------------------------------------------------------------- #
# Behavioral coverage of the skip patterns
# --------------------------------------------------------------------------- #
#
# The tests above pin the literal regex strings into the workflow scripts, which
# catches accidental deletion of a gate. They do NOT, however, catch a regex that
# still contains the pinned substring but is subtly broken (a dropped anchor, a
# stray character). The table below feeds representative branch refs through the
# SAME patterns the workflows embed — combined with the bash `||` the gates use —
# so the actual match/no-match behavior is locked in, not just the spelling.
#
# Both gates skip when the head ref matches SLICE_RE *or* WORK_RE. We compile the
# pinned constants (which `test_*_skips_both_pipeline_shapes` proves are the ones
# actually present in the scripts) and assert the resulting decision per ref.


def _is_skipped(ref: str) -> bool:
    """True when a head ref matches either pipeline shape — i.e. the bot skips it.

    Mirrors the workflows' ``[[ "$REF" =~ SLICE_RE || "$REF" =~ WORK_RE ]]``.
    """
    return bool(re.search(SLICE_RE, ref)) or bool(re.search(WORK_RE, ref))


# (ref, should_be_skipped). Comments note why, including the deliberate
# over-matches on multi-segment refs (acceptable: CLAUDE.md mandates
# single-segment human branches `egg/<topic>`).
_REF_CASES = [
    # Pipeline shapes — MUST skip.
    ("egg/issue-5/slice-2", True),
    ("egg/123/slice-0", True),
    ("egg/abc/slice-10", True),
    ("egg/issue-5/work", True),
    ("egg/123/work", True),
    # Human / doc-updater single-segment branches — MUST run (not skipped).
    ("egg/topic", False),
    ("egg/doc-update-1", False),
    ("egg/feature", False),
    ("main", False),
    # Anchor regressions the spelling-only tests would miss — MUST run.
    ("egg/issue-5/slice-2x", False),  # trailing char defeats `[0-9]+$`
    ("egg/issue-5/slice-", False),  # no digit
    ("egg/issue-5/work-extra", False),  # trailing chars defeat `work$`
    ("xegg/issue-5/work", False),  # leading char defeats `^egg/`
    ("egg/slice-2", False),  # needs an intermediate `<id>/` segment
    # Multi-segment refs DO match — documents the known over-match (a deliberate
    # trade-off; human branches are single-segment by convention).
    ("egg/feature/work", True),
    ("egg/foo/slice-1", True),
]


@pytest.mark.parametrize(("ref", "skipped"), _REF_CASES)
def test_skip_patterns_match_expected_refs(ref: str, skipped: bool) -> None:
    """The pinned regexes skip exactly the pipeline branch shapes (and the
    documented multi-segment over-match), and let everything else through."""
    assert _is_skipped(ref) is skipped, (
        f"ref {ref!r}: expected skipped={skipped}, got {_is_skipped(ref)} — "
        f"SLICE_RE={SLICE_RE!r} WORK_RE={WORK_RE!r}"
    )
