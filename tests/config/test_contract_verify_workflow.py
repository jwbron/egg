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
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "on-pull-request-contract-verify.yml"


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
    check behind the `egg/<id>/slice-N` head match — i.e. the regex must appear
    before (and gate) the work-branch contract lookup in the script.
    """
    slice_regex_pos = check_script.find("slice-[0-9]+$")
    contracts_lookup_pos = check_script.rfind("contents/.egg-state/contracts?ref=${work_branch}")
    assert slice_regex_pos != -1, "slice-branch head match missing"
    assert contracts_lookup_pos != -1, "work-branch contract lookup missing"
    assert slice_regex_pos < contracts_lookup_pos, (
        "the work-branch contract lookup is not gated behind the slice-branch "
        "head match — this risks reintroducing the #1134 over-fire on PRs that "
        "merely inherit the default branch's contracts"
    )
