"""Role gate on the wrapper's ``sync_to_proposals`` merge (#3216, WS1 of #3209).

``sync_to_proposals`` runs ``git merge --no-edit <peer_proposal_sha>`` to
give a reviewer a real checkout of the proposal under review. Pre-#3216 it
ran on *every* reviewer ``ack``/``nack`` arm, ungated by role — which
merged the peer's whole tree into the reviewer's worktree and, for
dual-role agents, cross-pollinated concurrent producer lineages into the
criss-cross merge topology that corrupts shared drafts (#3208).

#3216 gates the merge to reviewers that must EXECUTE the proposal (only
the ``tester`` runs the proposed tree). Every other reviewer reads peer
artifacts via the per-event-prompt ``git show`` / ``egg-artifact`` served
reads, so it skips the merge entirely. The policy set lives in
``egg_contracts.agent_roles.REVIEWER_CHECKOUT_ROLE_VALUES`` and is rendered
into the wrapper template as the ``checkout_roles`` field.

These tests render the real wrapper, extract the ``sync_to_proposals``
shell function, and run it against a throwaway git repo to assert the
merge actually happens for ``tester`` and is actually skipped for a
read-only reviewer role.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consensus_wrapper import (  # noqa: E402
    EVENT_PUMP_CHECKOUT_ROLES,
    build_consensus_wrapped_command,
)
from egg_contracts.agent_roles import (  # noqa: E402
    REVIEWER_CHECKOUT_ROLE_VALUES,
)

# A reviewer role that only reads diffs and must therefore be gated OUT of
# the working-tree merge. Used both as a negative for the policy assertion
# and as the skip case in the behavioral test.
_READ_ONLY_REVIEWER_ROLE = "reviewer_code"


def _rendered_script() -> str:
    return build_consensus_wrapped_command("Prompt")[2]


def _extract_sync_function(script: str) -> str:
    """Return the ``sync_to_proposals`` shell function text.

    The wrapper renders each function's closing brace as ``}`` at column 0,
    so capture from the definition to the first column-0 ``}``.
    """
    match = re.search(
        r"^sync_to_proposals\(\) \{.*?^\}",
        script,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "sync_to_proposals function not found in rendered wrapper"
    body = match.group(0)
    # Sanity: we captured the whole function, not a truncated prefix.
    assert "esac" in body and "return 0" in body and body.rstrip().endswith("}")
    return body


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo_with_peer_proposal(repo: Path) -> str:
    """Create a repo whose HEAD is the review base and return a peer
    proposal SHA that is NOT yet in HEAD's ancestry."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD")

    # Peer's proposal: a commit adding a distinct file, on its own branch,
    # so it is not reachable from the reviewer's HEAD (still at base).
    git("checkout", "-q", "-b", "peer")
    (repo / "peer.txt").write_text("peer proposal\n")
    git("add", ".")
    git("commit", "-q", "-m", "peer proposal")
    peer_sha = git("rev-parse", "HEAD")

    # Put the reviewer worktree back on the review base.
    git("checkout", "-q", "main")
    assert git("rev-parse", "HEAD") == base_sha
    return peer_sha


def _run_sync(repo: Path, role: str, peer_sha: str) -> None:
    """Source the extracted function and invoke it with a one-proposal
    payload, as the given ``EGG_AGENT_ROLE``."""
    fn = _extract_sync_function(_rendered_script())
    harness = (
        "set -uo pipefail\n"
        "cw_log() { :; }\n"  # stub the wrapper logger
        'SYNC_FAILURE_BANNERS=""\n'
        f"{fn}\n"
        'sync_to_proposals "$1"\n'
    )
    payload = f'{{"pending_reviews":[{{"proposal_commit_sha":"{peer_sha}"}}]}}'
    subprocess.run(
        ["bash", "-c", harness, "bash", payload],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "EGG_REPO_PATH": str(repo), "EGG_AGENT_ROLE": role},
    )


# ---------------------------------------------------------------------------
# Policy wiring
# ---------------------------------------------------------------------------


def test_checkout_roles_render_from_policy_set() -> None:
    """The rendered ``checkout_roles`` list is exactly the policy set's
    values — so editing ``REVIEWER_CHECKOUT_ROLE_VALUES`` changes the gate,
    and no role is hard-coded in the bash."""
    expected = {str(r) for r in REVIEWER_CHECKOUT_ROLE_VALUES}
    assert set(EVENT_PUMP_CHECKOUT_ROLES.split()) == expected
    # tester is the execution reviewer; a read-only reviewer is excluded.
    assert "tester" in expected
    assert _READ_ONLY_REVIEWER_ROLE not in expected


def test_gate_is_rendered_into_the_wrapper() -> None:
    script = _rendered_script()
    assert f'case " {EVENT_PUMP_CHECKOUT_ROLES} " in' in script
    assert "skipping working-tree merge (#3216)" in script


# ---------------------------------------------------------------------------
# Behavioral: merge happens for tester, is skipped for a read-only reviewer
# ---------------------------------------------------------------------------


def test_tester_merges_peer_proposal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    peer_sha = _init_repo_with_peer_proposal(repo)

    _run_sync(repo, "tester", peer_sha)

    # The merge ran: the peer proposal is now in HEAD's ancestry and its
    # file is present in the reviewer's worktree.
    ancestor_rc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", peer_sha, "HEAD"],
    ).returncode
    assert ancestor_rc == 0, "tester worktree was not synced to the peer proposal"
    assert (repo / "peer.txt").exists()


def test_read_only_reviewer_skips_merge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    peer_sha = _init_repo_with_peer_proposal(repo)
    head_before = _git(repo, "rev-parse", "HEAD")

    _run_sync(repo, _READ_ONLY_REVIEWER_ROLE, peer_sha)

    # The merge was skipped: HEAD is unchanged and the peer's file never
    # landed in this reviewer's worktree.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert not (repo / "peer.txt").exists()
    ancestor_rc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", peer_sha, "HEAD"],
    ).returncode
    assert ancestor_rc != 0, "read-only reviewer should not contain the peer proposal"


@pytest.mark.parametrize("role", ["", "unknown_role"])
def test_unknown_or_unset_role_defaults_to_skip(tmp_path: Path, role: str) -> None:
    """Default-deny: an unset or unrecognized role reads via git-show, so it
    must not merge the peer tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    peer_sha = _init_repo_with_peer_proposal(repo)

    _run_sync(repo, role, peer_sha)

    assert not (repo / "peer.txt").exists()
