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
# Crucially, the table runs against the regexes *extracted from the YAML run:
# script*, not the SLICE_RE/WORK_RE constants. A substring pin (`SLICE_RE in
# script`) would accept a typo'd workflow regex with a stray trailing character
# (e.g. `^egg/.+/slice-[0-9]+$X`) — that string contains SLICE_RE as a substring,
# so the pin passes, yet it matches different refs in bash. Parsing the actual
# `=~` operands out of the script and feeding *those* through the table closes
# that gap: a trailing-char regression changes the operand and trips a case here.


def _extract_branch_regexes(job: dict, step_id: str) -> list[str]:
    """Recover the bash ``=~`` regex operands from a should-run check script.

    Both gates embed ``[[ "$REF" =~ <slice-re> || "$REF" =~ <work-re> ]]``. The
    operands are unquoted (bash requires this for ``=~`` to treat the RHS as a
    regex rather than a literal) and are therefore whitespace-terminated, so a
    simple ``=~ <non-space-run>`` scan recovers the exact regexes the workflow
    applies at runtime — including any stray trailing character a typo would
    introduce, which a substring pin against the constants would silently accept.
    """
    script = _step_run(job, step_id)
    operands = re.findall(r"=~\s+(\S+)", script)
    assert operands, (
        f"no bash `=~` regex operands found in step (id: {step_id}) — the "
        "branch-topology check is not gating on the head ref at all"
    )
    # Pin the helper to "exactly the two gate regexes", not "whatever `=~`
    # operands happen to be here". The scan is greedy over the whole `run:`
    # script, so a future maintainer adding an unrelated `[[ … =~ … ]]` to the
    # same step would otherwise OR a stray operand into `_is_skipped` and flip
    # table cases. Asserting the count makes that break loud and self-pointing
    # at the new operand rather than silently mutating the behavioral table.
    assert len(operands) == 2, (
        f"expected exactly 2 bash `=~` regex operands in step (id: {step_id}), "
        f"found {len(operands)}: {operands!r} — the branch-topology gate should "
        "embed only the slice and work head-ref shapes; an extra operand here "
        "would be OR'd into the skip decision and change which refs are skipped"
    )
    return operands


# The two pipeline-PR gates whose embedded regexes drive the behavioral table.
_BEHAVIORAL_GATES = [
    pytest.param(REVIEW_CALLER, "should-run", "check", id="review-caller"),
    pytest.param(AUTOFIX_CALLER, "should-run", "check", id="autofix-caller"),
]


def _is_skipped(ref: str, patterns: list[str]) -> bool:
    """True when a head ref matches any pipeline shape — i.e. the bot skips it.

    Mirrors the workflows' ``[[ "$REF" =~ <re1> || "$REF" =~ <re2> ]]`` over the
    regexes actually extracted from the gate's ``run:`` script.
    """
    return any(re.search(pattern, ref) for pattern in patterns)


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


@pytest.mark.parametrize(("workflow", "job_id", "step_id"), _BEHAVIORAL_GATES)
@pytest.mark.parametrize(("ref", "skipped"), _REF_CASES)
def test_skip_patterns_match_expected_refs(
    workflow: Path, job_id: str, step_id: str, ref: str, skipped: bool
) -> None:
    """Each gate's *embedded* regexes skip exactly the pipeline branch shapes
    (and the documented multi-segment over-match), and let everything else
    through. Runs against the operands parsed out of the YAML run: script, so a
    trailing-char anchor regression that survives the substring pins is caught.
    """
    patterns = _extract_branch_regexes(_job(workflow, job_id), step_id)
    decision = _is_skipped(ref, patterns)
    assert decision is skipped, (
        f"{workflow.name} ref {ref!r}: expected skipped={skipped}, got "
        f"{decision} — extracted patterns={patterns!r}"
    )
