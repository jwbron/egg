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
  `.egg-state/` orchestration artifacts when the pipeline is *multi-slice* —
  implementation is then delivered through the stacked slice PRs — so running
  implementation verification on it is a false positive by construction. But a
  single-slice / monolithic pipeline commits implementation directly onto the
  work branch, so *its* work->main PR IS the implementation PR and must still be
  verified. The two share an identical head/base topology, so the gate keys the
  skip on the contract slice count on the work branch (`len(slices) > 1`,
  mirroring the orchestrator's `_is_slice_dag_mode`) rather than topology alone,
  and short-circuits the multi-slice roll-up to `run=false` *before* the
  added-contract trigger (condition 2) can fire on the contract the work->main
  diff necessarily adds. The assertions below lock in both the slice-count
  gating (so a single-slice/monolithic implementation PR is never silently
  skipped) and the ordering (so the skip cannot be reordered after — or removed
  by — a future edit).
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


@pytest.fixture
def reusable_pr_head_script() -> str:
    """Return the bash body of reusable-review.yml's `should-run` `pr-head` step.

    The #3254 slice-count discriminator lives here (not in the `check` step)
    because this step holds the `GH_TOKEN`; it fetches the contract on the work
    branch, counts slices, and emits the `context-pr` output the `check` step
    gates on.
    """
    if not REUSABLE_WORKFLOW.exists():
        pytest.skip(f"{REUSABLE_WORKFLOW} not found — repo is in an unexpected state")
    data = yaml.safe_load(REUSABLE_WORKFLOW.read_text(encoding="utf-8"))
    jobs = data.get("jobs", {})
    should_run = jobs.get("should-run")
    assert should_run is not None, "missing `should-run` job in reusable-review workflow"
    steps = should_run.get("steps", [])
    pr_head_steps = [s for s in steps if isinstance(s, dict) and s.get("id") == "pr-head"]
    assert pr_head_steps, "missing `pr-head` step (id: pr-head) in the should-run job"
    return pr_head_steps[0].get("run", "")


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
    """#3254: the multi-slice work->main context PR is deterministically skipped.

    The multi-slice context/state PR (`egg/<id>/work -> main`) holds only
    `.egg-state/` artifacts, so the gate must short-circuit it to `run=false`
    rather than letting implementation verification run.
    """
    assert "^egg/(.+)/work$" in check_script, (
        "should-run gate has no work-branch head match — the work->main "
        "context PR will fall through and run implementation verification (#3254)"
    )
    # The skip must actually disable the run.
    assert 'echo "run=false"' in check_script, (
        "the work->main gate does not set run=false anywhere — the skip is inert (#3254)"
    )


def test_context_pr_skip_is_slice_count_gated(check_script: str) -> None:
    """#3254 review (BLOCKING fix): the work->main skip must NOT fire on topology
    alone — only on a multi-slice contract.

    A single-slice / monolithic pipeline commits implementation directly onto
    `egg/<id>/work`, so its `egg/<id>/work -> main` PR IS the implementation PR
    and is the *only* GitHub-level verification it gets. A skip keyed on bare
    head/base topology would suppress that review (the regression flagged on the
    PR). The skip must instead be gated on the contract slice count
    (`len(slices) > 1`, mirroring the orchestrator's `_is_slice_dag_mode`), so a
    monolithic/single-slice work->main PR falls through to normal gating.
    """
    # The discriminator must consult the contract slice count, not just topology.
    assert "slice_count" in check_script and "-gt 1" in check_script, (
        "work->main skip is not gated on the contract slice count (`-gt 1`) — "
        "it would suppress review of single-slice/monolithic implementation "
        "PRs, the only GitHub-level review they get (#3254 review)"
    )
    # The slice count must come from the contract on the work branch, fetched
    # by the pipeline's own key — not the whole contracts/ dir (which inherits
    # historical multi-slice contracts from main and would false-skip).
    assert "contents/.egg-state/contracts/" in check_script, (
        "the slice count is not read from the pipeline's contract on the work "
        "branch — the discriminator has no source of truth (#3254 review)"
    )
    # The slices field (canonical) or its legacy `phases` alias must be counted.
    assert ".slices" in check_script, (
        "the slice count does not reference the contract `slices` field "
        "(_is_slice_dag_mode parity) (#3254 review)"
    )


def test_context_pr_skip_precedes_other_triggers(check_script: str) -> None:
    """The work->main skip must be evaluated BEFORE the sdlc:pr / added-contract triggers.

    The work->main diff necessarily *adds* the contract relative to main, which
    would trip the added-contract trigger (condition 2); the context PR also
    carries the `sdlc:pr` label. So the deterministic slice-count skip only wins
    for the multi-slice roll-up if it is evaluated first. Lock that ordering in.
    """
    lines = check_script.splitlines()
    work_idx = next((i for i, ln in enumerate(lines) if "^egg/(.+)/work$" in ln), None)
    label_idx = next((i for i, ln in enumerate(lines) if 'grep -q "^sdlc:pr$"' in ln), None)
    added_idx = next((i for i, ln in enumerate(lines) if 'status == "added"' in ln), None)
    assert work_idx is not None, "work->main skip missing (#3254)"
    assert label_idx is not None and added_idx is not None
    assert work_idx < label_idx, (
        "work->main skip is evaluated after the sdlc:pr label trigger — the "
        "label would win and verification would run on the context PR (#3254)"
    )
    assert work_idx < added_idx, (
        "work->main skip is evaluated after the added-contract trigger — the "
        "work->main contract add would win and verification would run (#3254)"
    )


def test_reusable_review_skips_context_pr(
    reusable_check_script: str, reusable_pr_head_script: str
) -> None:
    """#3254: the shared review gate (egg-reviewer) skips the *multi-slice*
    work->main context PR, and the decision is slice-count gated.

    The slice-count discriminator lives in the `pr-head` step (which holds the
    `GH_TOKEN`) and is surfaced to the `check` step as the `context-pr` output;
    the `check` step skips iff `context-pr == true`. Assert both halves so the
    egg-reviewer cannot regress to a bare-topology skip that suppresses review
    of single-slice/monolithic implementation PRs (#3254 review).
    """
    # pr-head: slice-count gated on the work-branch contract.
    assert "^egg/(.+)/work$" in reusable_pr_head_script, (
        "reusable-review pr-head has no work-branch head match (#3254)"
    )
    assert "slice_count" in reusable_pr_head_script and "-gt 1" in reusable_pr_head_script, (
        "reusable-review pr-head does not gate context-pr on the contract slice "
        "count — egg-reviewer would skip single-slice/monolithic implementation "
        "PRs (#3254 review)"
    )
    assert "contents/.egg-state/contracts/" in reusable_pr_head_script, (
        "reusable-review pr-head does not read the contract on the work branch "
        "to count slices (#3254 review)"
    )
    assert 'echo "context-pr=' in reusable_pr_head_script, (
        "reusable-review pr-head does not emit the context-pr output the check "
        "step gates on (#3254)"
    )
    # check: skip is driven by the context-pr output, not raw topology.
    assert "CONTEXT_PR" in reusable_check_script, (
        "reusable-review check step does not consult the context-pr output — "
        "the slice-count decision is not wired into the skip (#3254 review)"
    )
    skip_idx = next(
        (
            i
            for i, line in enumerate(reusable_check_script.splitlines())
            if 'CONTEXT_PR" == "true"' in line
        ),
        None,
    )
    assert skip_idx is not None, (
        "reusable-review check step has no `CONTEXT_PR == true` skip branch (#3254)"
    )
    tail = "\n".join(reusable_check_script.splitlines()[skip_idx : skip_idx + 5])
    assert "run=false" in tail, (
        "the reusable-review context-PR branch does not set run=false — the skip is inert (#3254)"
    )
