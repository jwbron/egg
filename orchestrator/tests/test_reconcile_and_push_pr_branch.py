"""
Tests for the reconcile-on-failure path inside ``push_worktree_branch``.

Reconcile was originally a wrapper (``_reconcile_and_push_pr_branch``)
around the gateway client push; it was folded into
``GatewayClient.push_worktree_branch`` itself in #1808 so every push
call site gets the same fetch+rebase+retry behavior without a wrapper.

Cases covered (originally added for #1706, rewritten for #1731 to
prefer rebase over merge and to auto-resolve conflicts under
``.egg-state/agent-outputs/`` by taking the remote side):

- First push attempt succeeds → return PushResult(ok=True), no fetch/rebase attempted.
- First push fails → fetch+rebase+retry path engaged.
- Fetch failure → hard fail, no rebase attempted, return PushResult(ok=False).
- Rebase conflict in a non-ephemeral path → rebase --abort, return PushResult(ok=False).
- Rebase conflict only under .egg-state/agent-outputs/ → auto-resolve and continue.
- Rebase timeout → rebase --abort, return PushResult(ok=False).
- Rebase succeeds but retry push still fails → return PushResult(ok=False).
- ``ref`` set (state-sync style, #1808): no reconcile — rebase is only
  meaningful when ``repo_path`` is a worktree checked out to the branch.
"""

import subprocess
from unittest.mock import MagicMock, patch

from gateway_client import (
    GatewayClient,
    PushResult,
    _rebase_with_agent_output_autoresolve,
)


def _run_result(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess stand-in for subprocess.run mocks."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _git(cwd, *args):
    """Run ``git <args>`` in ``cwd``; raise on failure, return CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _git_init(path, origin_url):
    """Initialise a non-bare repo at ``path`` wired to ``origin_url`` with an identity."""
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "remote", "add", "origin", origin_url)


def _patch_id_of(repo, sha):
    """Return the patch-id of ``sha`` (``git patch-id`` tolerates empty diffs)."""
    diff = subprocess.run(
        ["git", "-C", str(repo), "show", "--no-color", "--format=", sha],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"],
        input=diff.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    # patch-id output: "<patch-id> <commit-sha>\n" — take the first field,
    # or fall back to the commit SHA for empty patches (no diff at all).
    out = pid.stdout.strip()
    if out:
        return out.split()[0]
    return sha


def _make_client(push_results):
    """Return a GatewayClient whose ``_do_push`` yields ``push_results`` in order.

    Each element of ``push_results`` is a bool expressing success/failure;
    bools are wrapped into ``PushResult`` so the test doesn't need to
    care about the failure category (the reconcile helpers branch only
    on ``.ok``).
    """
    client = GatewayClient(
        gateway_host="test-gateway",
        gateway_port=9848,  # noqa: EGG002
        launcher_secret="test-secret",
    )
    results = [
        r if isinstance(r, PushResult) else PushResult(ok=bool(r), category=None if r else "test")
        for r in push_results
    ]
    client._do_push = MagicMock(side_effect=results)
    return client


class TestPushWorktreeBranchReconcile:
    def test_first_push_success_skips_fetch_and_rebase(self, tmp_path):
        """When the initial push succeeds, no git subprocess calls happen."""
        client = _make_client([True])
        with patch("gateway_client.subprocess.run") as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        assert client._do_push.call_count == 1
        mock_run.assert_not_called()

    def test_push_fail_then_fetch_rebase_retry_succeeds(self, tmp_path):
        """On initial failure: fetch, rebase, retry; all succeed → True."""
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[_run_result(), _run_result()],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        assert client._do_push.call_count == 2
        assert mock_run.call_count == 2
        fetch_cmd = mock_run.call_args_list[0].args[0]
        rebase_cmd = mock_run.call_args_list[1].args[0]
        assert "fetch" in fetch_cmd and "origin" in fetch_cmd and "egg/feature" in fetch_cmd
        assert "rebase" in rebase_cmd and "origin/egg/feature" in rebase_cmd

    def test_push_fail_then_fetch_fails_gives_up(self, tmp_path):
        """If fetch itself fails, rebase is not attempted and result is False."""
        client = _make_client([False])

        def _run_side_effect(cmd, *args, **kwargs):
            if "fetch" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=128, cmd=cmd, stderr="fatal: remote hung up"
                )
            raise AssertionError(f"Unexpected subprocess invocation: {cmd}")

        with patch("gateway_client.subprocess.run", side_effect=_run_side_effect):
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1

    def test_rebase_conflict_outside_agent_outputs_aborts(self, tmp_path):
        """Conflict in a non-ephemeral path triggers ``git rebase --abort`` and returns False."""
        client = _make_client([False])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase (conflict)
                _run_result(stdout="src/app.py\n"),  # diff --name-only --diff-filter=U
                _run_result(),  # rebase --abort
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_conflict_only_in_agent_outputs_auto_resolves(self, tmp_path):
        """Conflicts confined to .egg-state/agent-outputs/ auto-resolve to remote side."""
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(
                    stdout=".egg-state/agent-outputs/coder-test-changes.patch\n"
                ),  # unmerged paths
                _run_result(),  # checkout --theirs
                _run_result(),  # add
                _run_result(returncode=1),  # diff --cached --quiet → has staged changes
                _run_result(),  # rebase --continue (success)
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        assert client._do_push.call_count == 2
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("checkout" in c and "--theirs" in c for c in all_cmds)
        assert any("rebase" in c and "--continue" in c for c in all_cmds)

    def test_rebase_auto_resolve_uses_skip_when_index_empty(self, tmp_path):
        """When ``--theirs`` deletes the only conflicting file and leaves the index
        empty, ``git rebase --skip`` is used instead of ``--continue`` to avoid
        the 'No changes - did you forget to use git add?' error.
        """
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(stdout=".egg-state/agent-outputs/x.patch\n"),  # unmerged
                _run_result(),  # checkout --theirs
                _run_result(),  # add
                _run_result(returncode=0),  # diff --cached --quiet → empty index
                _run_result(),  # rebase --skip (success)
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("rebase" in c and "--skip" in c for c in all_cmds)
        assert not any("rebase" in c and "--continue" in c for c in all_cmds)

    def test_rebase_mixed_conflict_aborts(self, tmp_path):
        """A conflict list that includes any non-agent-outputs path aborts the rebase."""
        client = _make_client([False])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(
                    stdout=(".egg-state/agent-outputs/x.patch\norchestrator/routes/pipelines.py\n")
                ),  # mixed conflicts
                _run_result(),  # rebase --abort
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any("checkout" in c and "--theirs" in c for c in all_cmds)
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_timeout_aborts(self, tmp_path):
        """Rebase TimeoutExpired triggers ``git rebase --abort`` and returns False."""
        client = _make_client([False])

        call_count = 0

        def _run_side_effect(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _run_result()  # fetch ok
            if call_count == 2:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)  # rebase times out
            return _run_result()  # rebase --abort

        with patch("gateway_client.subprocess.run", side_effect=_run_side_effect) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_succeeds_retry_fails(self, tmp_path):
        """Successful reconcile but still-failing retry push returns False."""
        client = _make_client([False, False])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[_run_result(), _run_result()],  # fetch, rebase
        ):
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 2

    def test_ref_push_skips_reconcile(self, tmp_path):
        """When ``ref`` is set (state-sync style), reconcile is skipped.

        The rebase would mutate the checkout at repo_path, which for a
        ``ref``-based push is not a dedicated pipeline worktree but a
        shared repo whose checkout we must not disturb (see #1808).
        """
        client = _make_client([False])
        with patch("gateway_client.subprocess.run") as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="state-sync",
                repo_path=str(tmp_path),
                branch="egg/pipeline-state",
                ref="egg/pipeline-state",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1
        mock_run.assert_not_called()

    def test_rebase_without_base_branch_uses_plain_form(self, tmp_path):
        """Without ``base_branch``, the rebase falls back to ``git rebase origin/{branch}``.

        Preserves pre-#1976 behavior for callers that don't know the
        pipeline's base branch (e.g. old integration points).
        """
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[_run_result(), _run_result()],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        # Order: fetch, rebase (no --onto, no base-branch fetch/verify).
        assert mock_run.call_count == 2
        rebase_cmd = mock_run.call_args_list[1].args[0]
        assert "rebase" in rebase_cmd
        assert "--onto" not in rebase_cmd
        assert "origin/egg/feature" in rebase_cmd

    def test_rebase_with_base_branch_uses_onto_form(self, tmp_path):
        """With ``base_branch`` and ``origin/{base_branch}`` resolvable,
        the rebase uses ``--onto origin/{branch} origin/{base_branch}`` so
        only ``origin/{base_branch}..HEAD`` is replayed — commits already
        on main are not duplicated onto the pipeline branch (#1976).
        """
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch origin {branch}
                _run_result(),  # fetch origin {base_branch}
                _run_result(),  # rev-parse --verify origin/{base_branch}
                _run_result(),  # rebase --onto ...
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/issue-42",
                base_branch="main",
            )

        assert ok.ok is True
        assert mock_run.call_count == 4
        base_fetch_cmd = mock_run.call_args_list[1].args[0]
        assert "fetch" in base_fetch_cmd and "main" in base_fetch_cmd
        rebase_cmd = mock_run.call_args_list[3].args[0]
        assert rebase_cmd[-4:] == [
            "rebase",
            "--onto",
            "origin/egg/issue-42",
            "origin/main",
        ]

    def test_rebase_does_not_replay_main_commits_when_base_branch_set(self, tmp_path):
        """End-to-end regression for #1976.

        Reproduces the PR #1971 pathology with a real git repo:
        - ``origin/egg/issue-N`` is stuck at an old main snapshot +
          ``contract-init-v1``.
        - main has moved forward by N upstream commits since that snapshot.
        - A fresh local worktree is branched from the new main and adds
          ``contract-init-v2``.

        Without the fix, ``git rebase origin/egg/issue-N`` replays those N
        upstream commits on top of ``contract-init-v1`` (duplicate-by-content
        with different SHAs).  With ``base_branch="main"`` passed through,
        the rebase uses ``--onto origin/egg/issue-N origin/main`` so only
        ``contract-init-v2`` is replayed.

        Asserts the acceptance criterion from #1976: ``<base>..HEAD`` on
        the pipeline branch never contains commits whose patch-id matches
        an already-merged commit on main.
        """
        # Build a bare "origin" repo that acts as the remote.
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)

        # Seed main with an initial commit (old base).
        seed = tmp_path / "seed"
        seed.mkdir()
        _git_init(seed, str(origin))
        (seed / "README.md").write_text("initial\n")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "initial main commit")
        _git(seed, "push", "origin", "main")

        # Stale branch: contract-init-v1 on top of old main.
        _git(seed, "checkout", "-b", "egg/issue-42")
        (seed / "contract_v1.md").write_text("v1\n")
        _git(seed, "add", "contract_v1.md")
        _git(seed, "commit", "-m", "Initialize SDLC contract for issue #42 (v1)")
        _git(seed, "push", "origin", "egg/issue-42")

        # Main advances by 3 commits (simulates upstream landings).
        _git(seed, "checkout", "main")
        upstream_shas = []
        for i in range(3):
            (seed / f"upstream_{i}.md").write_text(f"upstream work {i}\n")
            _git(seed, "add", f"upstream_{i}.md")
            _git(seed, "commit", "-m", f"Fix #{100 + i}: upstream landing {i}")
            sha = _git(seed, "rev-parse", "HEAD").stdout.strip()
            upstream_shas.append(sha)
        _git(seed, "push", "origin", "main")

        # Fresh worktree cut from current origin/main + contract-init-v2.
        work = tmp_path / "work"
        work.mkdir()
        _git_init(work, str(origin))
        _git(work, "fetch", "origin")
        _git(work, "checkout", "-b", "egg/issue-42-work", "origin/main")
        (work / "contract_v2.md").write_text("v2\n")
        _git(work, "add", "contract_v2.md")
        _git(work, "commit", "-m", "Initialize SDLC contract for issue #42 (v2)")

        # Run the rebase helper in the worktree — emulating the push-reconcile
        # path after a non-ff rejection.  base_branch="main" should activate
        # the --onto form and prevent upstream-commit duplication.
        git_base = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            f"safe.directory={work}",
            "-C",
            str(work),
        ]
        result = _rebase_with_agent_output_autoresolve(
            git_base=git_base,
            pipeline_id="issue-42",
            branch="egg/issue-42",
            base_branch="main",
        )
        assert result.ok, f"rebase failed: {result.category} {result.detail}"

        # Collect the patch-ids of every upstream commit that landed on main.
        upstream_patch_ids = {_patch_id_of(seed, sha) for sha in upstream_shas}

        # Collect patch-ids of commits on the rebased branch that are NOT
        # on origin/main.  None of them should match an upstream commit.
        log = _git(work, "log", "--format=%H", "origin/main..HEAD").stdout.strip()
        branch_only_shas = [s for s in log.splitlines() if s]
        branch_only_patch_ids = {_patch_id_of(work, sha) for sha in branch_only_shas}

        duplicates = branch_only_patch_ids & upstream_patch_ids
        assert not duplicates, (
            f"pipeline branch contains duplicate-by-content commits of main: {duplicates}"
        )

        # Sanity: we expect exactly the two contract-init commits on the
        # branch (v1 from origin + v2 from local work) — 0 main-commit replays.
        assert len(branch_only_shas) == 2, (
            f"expected 2 branch-only commits, got {len(branch_only_shas)}: {branch_only_shas}"
        )

    def test_rebase_does_not_contaminate_when_base_fetch_silently_failed(self, tmp_path):
        """End-to-end regression for #2222.

        Reproduces the production shape:

        - ``origin/egg/issue-N`` is stuck at an old main snapshot (yesterday's
          aborted pipeline left it there).
        - ``main`` has advanced by several upstream commits since.
        - The local worktree is fresh off current main + an agent commit.
        - The local ``origin/main`` remote-tracking ref is *missing* — the
          best-effort base-branch fetch in ``_reconcile_and_retry_push``
          can fail silently (logged-and-continued; see gateway_client.py
          lines 1006-1031), leaving the rebase helper unable to verify it.

        Before the #2222 fix this caused ``_build_rebase_cmd`` to fall
        back to ``git rebase origin/egg/issue-N``, which from
        HEAD-at-current-main replayed every upstream main commit onto
        the stale tip — producing duplicate-by-content commits with new
        SHAs in the eventual PR diff.  After the fix the rebase helper
        returns ``reconcile_base_unavailable`` and the worktree HEAD is
        unchanged, so no contamination is possible.
        """
        # Bare "origin" remote.
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)

        # Seed main + push the stale pipeline branch tip.
        seed = tmp_path / "seed"
        seed.mkdir()
        _git_init(seed, str(origin))
        (seed / "README.md").write_text("initial\n")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "initial main commit")
        _git(seed, "push", "origin", "main")

        _git(seed, "checkout", "-b", "egg/issue-2137")
        (seed / "stale_contract.md").write_text("yesterday's run\n")
        _git(seed, "add", "stale_contract.md")
        _git(seed, "commit", "-m", "Initialize SDLC contract for #2137")
        _git(seed, "push", "origin", "egg/issue-2137")

        # Main advances by 3 upstream commits since the stale tip.
        _git(seed, "checkout", "main")
        upstream_shas = []
        for i in range(3):
            (seed / f"upstream_{i}.md").write_text(f"upstream work {i}\n")
            _git(seed, "add", f"upstream_{i}.md")
            _git(seed, "commit", "-m", f"Fix #{200 + i}: upstream landing {i}")
            upstream_shas.append(_git(seed, "rev-parse", "HEAD").stdout.strip())
        _git(seed, "push", "origin", "main")

        # Today's worktree: cut from current main, with an agent commit.
        # Critically: do NOT fetch origin/main into this worktree's
        # remote-tracking refs — simulating the silent base-fetch failure
        # path described in gateway_client.py lines 1006-1031.
        work = tmp_path / "work"
        work.mkdir()
        _git_init(work, str(origin))
        _git(work, "fetch", "origin", "main")
        _git(work, "checkout", "-b", "egg/issue-2137-work", "origin/main")
        (work / "agent_work.md").write_text("today's agent work\n")
        _git(work, "add", "agent_work.md")
        _git(work, "commit", "-m", "implement: agent commit on top of fresh main")
        head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

        # Fetch only ``egg/issue-2137`` (so the stale-tip ref exists locally),
        # then explicitly drop the ``origin/main`` ref to simulate the
        # silent-fetch-failure precondition.
        _git(work, "fetch", "origin", "egg/issue-2137")
        try:
            _git(work, "update-ref", "-d", "refs/remotes/origin/main")
        except subprocess.CalledProcessError:
            pass

        verify_main = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--verify", "origin/main"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify_main.returncode != 0, (
            "precondition failed: origin/main should not be resolvable in this worktree"
        )

        git_base = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            f"safe.directory={work}",
            "-C",
            str(work),
        ]
        result = _rebase_with_agent_output_autoresolve(
            git_base=git_base,
            pipeline_id="issue-2137",
            branch="egg/issue-2137",
            base_branch="main",
        )

        # The rebase must refuse rather than fall back to the unsafe form.
        assert result.ok is False, "rebase should fail-closed when origin/main is missing"
        assert result.category == "reconcile_base_unavailable", (
            f"unexpected category: {result.category}"
        )

        # HEAD must be untouched — no contamination possible.
        head_after = _git(work, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after, (
            f"HEAD changed during failed rebase: {head_before} -> {head_after}"
        )

        # No upstream-main commit ended up duplicated into the worktree's
        # local history (the contamination shape from #2222).
        upstream_patch_ids = {_patch_id_of(seed, sha) for sha in upstream_shas}
        log = _git(work, "log", "--format=%H", "HEAD").stdout.strip()
        local_patch_ids = {_patch_id_of(work, sha) for sha in log.splitlines() if sha.strip()}
        duplicates = local_patch_ids & upstream_patch_ids
        assert not duplicates, (
            f"worktree contains duplicate-by-content commits of upstream main: {duplicates}"
        )

    def test_rebase_fails_closed_when_base_unverifiable(self, tmp_path):
        """When ``base_branch`` is provided but ``origin/{base_branch}`` is
        not resolvable locally (e.g. the base-branch fetch failed silently
        and the tracking ref was never created), the reconcile path fails
        closed with ``reconcile_base_unavailable`` rather than falling back
        to the plain ``git rebase origin/{branch}`` form.

        The plain fallback is the contamination producer in #2222: from
        HEAD-at-current-main with a stale ``origin/{branch}`` it replays
        all the upstream main commits between the stale tip and HEAD onto
        the stale tip, producing a PR full of duplicate-by-content
        commits.  Surfacing the failure forces the operator to retry once
        the base ref is healthy rather than silently producing a broken PR.
        """
        client = _make_client([False])  # only the initial push runs
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch origin {branch}
                _run_result(
                    returncode=1, stderr="couldn't find remote ref"
                ),  # fetch origin {base_branch} fails
                _run_result(returncode=128, stderr="unknown revision"),  # rev-parse verify fails
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/issue-42",
                base_branch="main",
            )

        assert ok.ok is False
        assert ok.category == "reconcile_base_unavailable"
        assert "origin/main" in (ok.detail or "")
        # Critically: no ``rebase`` call landed on the worktree.
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any("rebase" in c for c in all_cmds), (
            f"unexpected rebase invocation in {all_cmds}"
        )
        # And the retry push was never attempted.
        assert client._do_push.call_count == 1
