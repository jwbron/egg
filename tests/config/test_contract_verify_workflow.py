"""Structural assertions over the contract-verification trigger gate.

`.github/workflows/on-pull-request-contract-verify.yml` decides — in the
`should-run` job's `check` step (a bash-in-YAML gate) — whether contract
verification runs for a given PR. That gate is a proven regression magnet:

* #680 first added a "contract file exists on the branch" trigger.
* #1134 narrowed it to "PR *adds* a new contract file" because the original
  over-fired on docs-only PRs (every PR inherits the contracts accumulated on
  the default branch).
* #3040: that narrowing silently dropped *slice* PRs
  (`egg/<id>/slice-N -> egg/<id>/work`) — they inherit the contract from the
  work branch rather than adding it, and carry `agent:<role>` labels (not
  `sdlc:pr`), so neither surviving trigger fired and a descope shipped
  unverified.

These assertions lock in all three trigger conditions so a future "cleanup"
cannot silently drop the slice trigger (or reintroduce the #1134 over-fire) without
the suite going red.

* #3254: the work->main *context/state* PR (`egg/<id>/work -> main`) holds only
  `.egg-state/` orchestration artifacts — never implementation code — so
  running implementation verification on it is a false positive by
  construction. A deterministic head/base topology gate short-circuits it to
  `run=false` *before* the added-contract trigger (condition 2) can fire on the
  contract the work->main diff necessarily adds. The assertions below lock that
  ordering in so the skip cannot be reordered after — or removed by — a future
  edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "on-pull-request-contract-verify.yml"
REUSABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reusable-review.yml"


@pytest.fixture
def check_script() -> str:
    """Return the bash body of the `should-run` job's `check` step."""
    if not WORKFLOW.exists():
        pytest.skip(f"{WORKFLOW} not found — repo is in an unexpected state")
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = data.get("jobs", {})
    should_run = jobs.get("should-run")
    assert should_run is not None, "missing `should-run` job in contract-verify workflow"
    steps = should_run.get("steps", [])
    check_steps = [s for s in steps if isinstance(s, dict) and s.get("id") == "check"]
    assert check_steps, "missing `check` step (id: check) in the should-run job"
    return check_steps[0].get("run", "")


@pytest.fixture
def reusable_check_script() -> str:
    """Return the bash body of reusable-review.yml's `should-run` `check` step."""
    if not REUSABLE_WORKFLOW.exists():
        pytest.skip(f"{REUSABLE_WORKFLOW} not found — repo is in an unexpected state")
    data = yaml.safe_load(REUSABLE_WORKFLOW.read_text(encoding="utf-8"))
    jobs = data.get("jobs", {})
    should_run = jobs.get("should-run")
    assert should_run is not None, "missing `should-run` job in reusable-review workflow"
    steps = should_run.get("steps", [])
    check_steps = [s for s in steps if isinstance(s, dict) and s.get("id") == "check"]
    assert check_steps, "missing `check` step (id: check) in the should-run job"
    return check_steps[0].get("run", "")


def test_label_trigger_present(check_script: str) -> None:
    """Condition 1: the `sdlc:pr` label still triggers verification."""
    assert 'grep -q "^sdlc:pr$"' in check_script, (
        "should-run gate no longer checks for the `sdlc:pr` label — "
        "the label-based trigger (condition 1) was dropped"
    )


def test_added_contract_file_trigger_present(check_script: str) -> None:
    """Condition 2: a PR that *adds* a contract file still triggers verification.

    This is what fires for the work->main context PR (it adds the contract
    relative to the default branch).
    """
    assert 'status == "added"' in check_script and ".egg-state/contracts/" in check_script, (
        "should-run gate no longer detects a newly-added contract file via the "
        "PR files diff — the added-contract trigger (condition 2) was dropped"
    )


def test_slice_pr_trigger_present(check_script: str) -> None:
    """Condition 3 (#3040): slice PRs trigger verification.

    A slice PR's head is `egg/<pipeline_id>/slice-<N>`; it inherits the
    contract from the work branch rather than adding it, so neither condition
    1 nor 2 fires. The gate must detect the slice-branch head and derive the
    work branch.
    """
    assert "slice-[0-9]+$" in check_script, (
        "should-run gate has no slice-branch head match — slice PRs "
        "(egg/<id>/slice-N -> egg/<id>/work) will skip contract verification (#3040)"
    )
    assert "/work" in check_script, (
        "slice-PR trigger does not derive the pipeline work branch — the "
        "contract presence check must target egg/<pipeline_id>/work (#3040)"
    )


def test_slice_trigger_is_topology_keyed_not_bare_existence(check_script: str) -> None:
    """The slice trigger must be gated on slice-branch *topology*, not bare
    contract presence.

    #1134 removed a branch-level "contract exists" check because every PR
    inherits the default branch's accumulated contracts, so it over-fired on
    docs-only PRs. The slice trigger must therefore guard the contract-presence
    check structurally — i.e. the work-branch contract lookup must sit *inside*
    the `if [[ "$branch_name" =~ ^egg/(.+)/slice-N$ ]]; then ... fi` block. A
    refactor that hoisted the lookup out of that block (while leaving the
    regex string in a comment) would reintroduce the #1134 over-fire even
    though a literal-position assertion would still pass.
    """
    lines = check_script.splitlines()

    slice_if_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip().startswith("if [[") and "slice-[0-9]+$" in line
        ),
        None,
    )
    assert slice_if_idx is not None, (
        "no `if [[ ... slice-[0-9]+$ ... ]]; then` line found — the slice "
        "trigger is not gated by a head-topology if-block"
    )

    # Walk forward counting `if` opens and `fi` closes from the slice-if line.
    # `elif` does not open a new block (matched by an outer `fi`), and `fi`
    # never appears mid-expression in this gate's bash, so simple stripped-line
    # matching is sufficient.
    depth = 0
    slice_fi_idx: int | None = None
    for i in range(slice_if_idx, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith("if "):
            depth += 1
        if stripped == "fi" or stripped.startswith("fi "):
            depth -= 1
            if depth == 0:
                slice_fi_idx = i
                break
    assert slice_fi_idx is not None, (
        "no matching `fi` found for the slice-branch `if` — the gate's "
        "bash block structure is malformed"
    )

    inside_block = "\n".join(lines[slice_if_idx + 1 : slice_fi_idx])
    assert "contents/.egg-state/contracts?ref=${work_branch}" in inside_block, (
        "the work-branch contract lookup is not inside the "
        "`if [[ ... slice-[0-9]+$ ... ]]; then ... fi` block — this risks "
        "reintroducing the #1134 over-fire on PRs that merely inherit the "
        "default branch's contracts"
    )


def test_context_pr_skip_present(check_script: str) -> None:
    """#3254: the work->main context PR is deterministically skipped.

    The context/state PR (`egg/<id>/work -> main`) holds only `.egg-state/`
    artifacts, so the gate must short-circuit it to `run=false` on the
    head/base topology rather than letting implementation verification run.
    """
    assert "^egg/.+/work$" in check_script, (
        "should-run gate has no context-PR head match — the work->main "
        "context PR will fall through and run implementation verification (#3254)"
    )
    # The skip must actually disable the run, not merely match the branch.
    skip_idx = next(
        (i for i, line in enumerate(check_script.splitlines()) if "^egg/.+/work$" in line),
        None,
    )
    assert skip_idx is not None
    tail = "\n".join(check_script.splitlines()[skip_idx : skip_idx + 5])
    assert "run=false" in tail, (
        "the context-PR head match does not set run=false on the next lines — "
        "the skip is inert (#3254)"
    )


def test_context_pr_skip_precedes_other_triggers(check_script: str) -> None:
    """The context-PR skip must fire BEFORE the sdlc:pr / added-contract triggers.

    The work->main diff necessarily *adds* the contract relative to main, which
    would trip the added-contract trigger (condition 2); the context PR also
    carries the `sdlc:pr` label. So the deterministic skip only wins if it is
    evaluated first. Lock that ordering in.
    """
    lines = check_script.splitlines()
    work_idx = next((i for i, ln in enumerate(lines) if "^egg/.+/work$" in ln), None)
    label_idx = next((i for i, ln in enumerate(lines) if 'grep -q "^sdlc:pr$"' in ln), None)
    added_idx = next((i for i, ln in enumerate(lines) if 'status == "added"' in ln), None)
    assert work_idx is not None, "context-PR skip missing (#3254)"
    assert label_idx is not None and added_idx is not None
    assert work_idx < label_idx, (
        "context-PR skip is evaluated after the sdlc:pr label trigger — the "
        "label would win and verification would run on the context PR (#3254)"
    )
    assert work_idx < added_idx, (
        "context-PR skip is evaluated after the added-contract trigger — the "
        "work->main contract add would win and verification would run (#3254)"
    )


def test_reusable_review_skips_context_pr(reusable_check_script: str) -> None:
    """#3254: the shared review gate (egg-reviewer + contract-verification) also
    skips the work->main context PR deterministically on head/base topology."""
    assert "^egg/.+/work$" in reusable_check_script, (
        "reusable-review should-run gate has no context-PR head match — the "
        "egg-reviewer will run implementation-style review on the work->main "
        "context PR (#3254)"
    )
    skip_idx = next(
        (i for i, line in enumerate(reusable_check_script.splitlines()) if "^egg/.+/work$" in line),
        None,
    )
    assert skip_idx is not None
    tail = "\n".join(reusable_check_script.splitlines()[skip_idx : skip_idx + 5])
    assert "run=false" in tail, (
        "the reusable-review context-PR match does not set run=false — the skip is inert (#3254)"
    )
