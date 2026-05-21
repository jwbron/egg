"""Tests for ``_commit_slice_brc_history_to_integration_branch`` (#2548 task-2-2).

The hook runs after each slice's implement-phase consensus and before
``create_slice_pr`` is called.  It commits ONLY the per-slice BRC
history files (``<identifier>-implement-<slice_id>.{json,md}``) onto
the slice's integration branch as a final orchestrator-authored
commit, so the slice PR's diff carries the consensus transcript that
approved the slice's code.

Key invariants this file pins:

* Happy path: writer is called against a per-tick **staging
  directory** (not the work worktree, #2755), files are copied to
  the integration worktree, the orchestrator-authored commit is
  recorded, and the branch is pushed via ``push_worktree_branch``.
* Best-effort failure semantics: every step (writer, fetch, worktree
  add, commit, push) returns ``False`` on failure rather than raising
  — so PR creation can still proceed (the slice PR opens without
  its consensus transcript in that case).
* Idempotency: the hook is safe to re-run mid-flight.  The commit
  step skips when nothing is staged, and the push fast-forwards on
  no-op.
* Per-slice scoping: ONLY the named slice's files are copied; other
  slices' BRC files (and the unattributed sibling) stay in the
  per-tick staging directory and are cleaned up with it.
* Staging isolation: the writer's output lives in a ``mkdtemp``-rooted
  staging directory created per hook tick (#2755); the work worktree
  is never written to, so concurrent slice hooks cannot leave
  per-slice files on ``work`` that would later conflict with slice
  PR merges.
* Symlink defense: a planted symlink in
  ``.egg-state/brc-history/`` is skipped — never copied or staged —
  so a future writer change cannot leak attacker-controlled content
  onto the slice PR.
* No-op short-circuit: pipeline without a remote ``repo`` returns
  ``False`` without touching git or the gateway.

Adversarial probes (each one was on my "could the coder have missed
this?" list):

* What happens if ``_write_brc_history`` produces NO files for the
  slice?  The hook must surface that as a warning and return False
  rather than push an empty diff.
* What if push raises (``GatewayError``)?  Must be swallowed.
* What if push returns ``ok=False`` with a known category
  (``non_fast_forward``)?  Must be swallowed.
* What if the temp worktree path is left behind on a raised
  exception?  Cleanup must run regardless of branch outcome.
* What if the BRC file is a symlink pointing OUTSIDE the worktree?
  Defense-in-depth: skipped.
* What if files for OTHER slices exist in
  ``.egg-state/brc-history/``?  They must NOT be copied to this
  slice's integration branch (each slice carries only its own
  history per #2548 D2 / D5).
* What if ``_brc_history_identifier(pipeline)`` returns the issue
  number (int, not str)?  The path interpolation must still produce
  the canonical filename.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies before importing routes.pipelines.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


from gateway_client import PushResult  # noqa: E402
from models import Pipeline, PipelinePhase, PipelineStatus  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _commit_slice_brc_history_to_integration_branch,
)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def pipeline():
    """Pipeline with all the fields the hook reads."""
    return Pipeline(
        id="issue-2548",
        issue_number=2548,
        repo="owner/repo",
        branch="egg/issue-2548/work",
        base_branch="main",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )


@pytest.fixture
def make_spawner():
    """Factory: build a MagicMock spawner.gateway with sensible defaults."""

    def _build(
        *,
        push_result: PushResult | None = None,
        fetch_branch: bool = True,
        push_raise: Exception | None = None,
    ):
        spawner = MagicMock(name="spawner")
        gw = MagicMock(name="gateway")
        gw.fetch_branch.return_value = fetch_branch
        if push_raise is not None:
            gw.push_worktree_branch.side_effect = push_raise
        else:
            # Use explicit ``is None`` rather than ``or`` because
            # ``PushResult`` overrides ``__bool__`` to its ``ok`` flag —
            # a falsy ``PushResult(ok=False)`` would silently short-
            # circuit to a synthesized ``PushResult(ok=True)`` and
            # break the not-ok failure-path tests.
            gw.push_worktree_branch.return_value = (
                PushResult(ok=True) if push_result is None else push_result
            )
        spawner.gateway = gw
        return spawner

    return _build


@pytest.fixture(autouse=True)
def neutralise_git(monkeypatch):
    """Make subprocess.run + _commit_statefiles_to_worktree no-ops.

    The hook shells out to ``git worktree add / remove`` and calls
    ``_commit_statefiles_to_worktree``.  The container sandbox blocks
    ``git init``, so we patch both surfaces to return success without
    touching the filesystem state.

    Tests that need to exercise specific subprocess return codes (e.g.
    worktree-add failure) override this with their own ``patch``.
    """
    import subprocess

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # _commit_statefiles_to_worktree is also called — patch via the
    # module attribute so the helper passes through cleanly.
    monkeypatch.setattr("routes.pipelines._commit_statefiles_to_worktree", lambda *a, **kw: None)


def _seed_per_slice_brc_files(
    repo_root: Path,
    identifier: int | str,
    *,
    slice_ids: list[str],
) -> dict[str, Path]:
    """Pre-seed ``<repo_root>/.egg-state/brc-history/`` with per-slice BRC files.

    Returns a mapping of ``"<slice_id>-md"`` / ``"<slice_id>-json"``
    keys to the absolute file paths so individual tests can introspect.

    Note: after the #2755 refactor, the hook reads per-slice files from
    a per-tick staging directory (not the work worktree). Tests pair
    this seed with :func:`_writer_stub` patched in for
    ``_write_brc_history``; the stub copies the seeded files into the
    staging directory the hook passes to the writer, so the hook's
    scan-and-copy logic sees them at the expected staging path.
    """
    brc = repo_root / ".egg-state" / "brc-history"
    brc.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for sid in slice_ids:
        md = brc / f"{identifier}-implement-{sid}.md"
        md.write_text(f"# BRC history for {sid}\n")
        json_companion = brc / f"{identifier}-implement-{sid}.json"
        json_companion.write_text("[]\n")
        paths[f"{sid}-md"] = md
        paths[f"{sid}-json"] = json_companion
    return paths


def _writer_stub(seed_root: Path):
    """Build a ``_write_brc_history`` replacement that mirrors seeds into staging.

    The real writer renders per-slice files from the message store into
    ``<worktree>/.egg-state/brc-history/``. After #2755 the slice hook
    passes a per-tick staging directory as that worktree argument.

    This stub:

    * Records the staging path the hook passed (tests can inspect
      ``stub.staging_paths`` to assert the hook did NOT pass the work
      worktree).
    * Mirrors anything in ``<seed_root>/.egg-state/brc-history/`` into
      ``<staging>/.egg-state/brc-history/``, preserving symlinks so the
      symlink-defense path can be exercised.

    The factory returns the stub callable; the callable carries the
    ``staging_paths`` attribute for assertions.
    """
    import os

    seen: list[str] = []

    def _stub(*args, **kwargs):
        # The writer's first positional arg is the destination path. We
        # accept any extra args/kwargs so adding parameters to
        # ``_write_brc_history`` (e.g. ``write_per_slice``) doesn't break
        # patched tests.
        if not args:
            return
        staging_path = Path(args[0])
        seen.append(str(staging_path))
        src_brc = seed_root / ".egg-state" / "brc-history"
        if not src_brc.exists():
            return
        dst_brc = staging_path / ".egg-state" / "brc-history"
        dst_brc.mkdir(parents=True, exist_ok=True)
        for src in src_brc.iterdir():
            dst = dst_brc / src.name
            if src.is_symlink():
                # Preserve as a symlink so the hook's symlink defense
                # can be exercised in a path that mirrors production
                # (where the writer would have to be coerced into
                # producing a symlink for the defense to fire).
                target = os.readlink(src)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                os.symlink(target, dst)
            elif src.is_file():
                # Use raw bytes I/O rather than ``shutil.copy2`` so tests
                # that spy on ``shutil.copy2`` only see the hook's own
                # copy calls (staging → integration worktree), not the
                # stub's internal seed mirror (seed root → staging).
                dst.write_bytes(src.read_bytes())

    _stub.staging_paths = seen  # type: ignore[attr-defined]
    return _stub


def _no_op_write_brc_history(*args, **kwargs):
    """Replacement for the writer that does nothing.

    Used by tests that explicitly want NO files in the hook's staging
    directory (e.g. the "no per-slice files produced" path that must
    return False). Tests that DO want files staged use
    :func:`_writer_stub` instead, which mirrors pre-seeded files into
    the staging directory the hook passes to the writer.
    """


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_returns_true_when_files_committed_and_pushed(self, tmp_path, pipeline, make_spawner):
        """Happy path: the per-slice BRC files for the named slice are
        committed onto the integration branch and pushed via the
        gateway, and the helper returns True."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        assert ok is True
        spawner.gateway.push_worktree_branch.assert_called_once()
        push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
        # The push must target the integration branch supplied by the
        # caller — NOT the pipeline branch and NOT the work branch.
        assert push_kwargs["branch"] == "egg/issue-2548/slice-1"
        assert push_kwargs["pipeline_id"] == "issue-2548"
        # Pipeline.base_branch threads through so the reconcile-on-
        # non-fast-forward path uses the right base.
        assert push_kwargs["base_branch"] == "main"

    def test_calls_write_brc_history_to_refresh_files(self, tmp_path, pipeline, make_spawner):
        """Step 1: the writer is invoked against a per-tick **staging
        directory** (#2755) so messages that landed since the last
        phase-boundary write are captured without touching the work
        worktree."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        writer_calls: list[tuple] = []

        def _spy_writer(worktree, pipeline_id, phase, identifier, **kwargs):
            # Mirror real writer side-effects so the hook can find the
            # per-slice files at the staging path on its scan step.
            stub = _writer_stub(tmp_path)
            stub(worktree, pipeline_id, phase, identifier, **kwargs)
            writer_calls.append((str(worktree), pipeline_id, phase, identifier, dict(kwargs)))

        with patch("routes.pipelines._write_brc_history", _spy_writer):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        assert len(writer_calls) == 1, "writer must be called exactly once per hook tick"
        wt, pid, phase, identifier, kwargs = writer_calls[0]
        # Writer must NOT be called against the work worktree (#2755).
        # The hook stages into a temp directory so concurrent slice hooks
        # do not leave per-slice files on ``work`` that would conflict
        # with the slice PRs' add of the same paths.
        assert wt != str(tmp_path), (
            f"writer must not be called against the work worktree (#2755); "
            f"got {wt!r} which is the work-worktree path"
        )
        assert pid == "issue-2548"
        assert phase == "implement"
        # Identifier resolves via ``_brc_history_identifier`` — for an
        # issue pipeline that's the issue number (2548).
        assert identifier == 2548
        # The slice hook depends on per-slice file rendering — passing
        # ``write_per_slice=False`` here would silently skip the per-slice
        # bucket loop and break the feature (the downstream scan would
        # find no files and the hook would return False). Pin the kwarg
        # so a future regression that flips the default cannot escape
        # via ``_writer_stub``'s ``**kwargs`` permissiveness.
        assert kwargs.get("write_per_slice", True) is True, (
            f"slice hook must not pass write_per_slice=False to the writer (#2755); "
            f"got kwargs={kwargs!r}"
        )

    def test_fetches_integration_branch_before_worktree_add(self, tmp_path, pipeline, make_spawner):
        """Step 2: the local remote-tracking ref is refreshed via
        ``fetch_branch`` so the worktree-add doesn't operate on a
        stale tip."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        spawner.gateway.fetch_branch.assert_called_once()
        kwargs = spawner.gateway.fetch_branch.call_args.kwargs
        # Force-fetch refspec is the documented shape — the leading ``+``
        # makes the local ref a non-fast-forward overwrite of origin's.
        args = kwargs.get("args") or spawner.gateway.fetch_branch.call_args.args[2]
        assert any("egg/issue-2548/slice-1" in a for a in args), (
            f"fetch_branch must be invoked for the integration branch, got args={args}"
        )

    def test_fetch_failure_does_not_abort(self, tmp_path, pipeline, make_spawner):
        """Step 2 is best-effort: a failing fetch is logged at warning
        level but the hook continues with the existing local ref so a
        subsequent mid-flight retry still has a chance to succeed."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        spawner.gateway.fetch_branch.side_effect = RuntimeError("network blip")
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        # Fetch failure was tolerated — the helper continued past the
        # failing fetch and reached the push step (the existing local
        # ref is good enough to attempt a push, since worktree-add and
        # push are no-ops in this patched-subprocess environment).  If
        # the helper had aborted on fetch failure, push_worktree_branch
        # would never have been invoked.
        assert spawner.gateway.push_worktree_branch.called, (
            "fetch failure must not abort the helper — push step must still be reached"
        )

    def test_pipeline_repo_unset_returns_false_without_calling_gateway(
        self, tmp_path, make_spawner
    ):
        """Pipeline without a remote ``repo`` returns False immediately
        — never invokes the writer, never touches the gateway, never
        creates a temp worktree.  This protects local-only test
        pipelines from spurious gateway errors."""
        local_pipeline = Pipeline(
            id="local-only",
            issue_number=999,
            repo=None,
            branch=None,
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        spawner = make_spawner()
        ok = _commit_slice_brc_history_to_integration_branch(
            local_pipeline,
            spawner,
            tmp_path,
            slice_id="slice-1",
            integration_branch="egg/issue-999/slice-1",
        )
        assert ok is False
        spawner.gateway.fetch_branch.assert_not_called()
        spawner.gateway.push_worktree_branch.assert_not_called()


# ----------------------------------------------------------------------
# Per-slice scoping
# ----------------------------------------------------------------------


class TestPerSliceScoping:
    def test_only_named_slice_files_copied_other_slices_left_behind(
        self, tmp_path, pipeline, make_spawner
    ):
        """Each slice PR carries only its own consensus transcript per
        D2 / D5 of #2548.  When ``.egg-state/brc-history/`` contains
        files for slice-1, slice-2, AND slice-3, the slice-2 hook tick
        copies ONLY slice-2's files onto its integration worktree."""
        _seed_per_slice_brc_files(
            tmp_path, identifier=2548, slice_ids=["slice-1", "slice-2", "slice-3"]
        )
        spawner = make_spawner()

        # Capture the actual shutil.copy2 calls so we can introspect
        # which source files were targeted by the hook.
        copy_calls: list[tuple] = []

        import shutil

        original_copy = shutil.copy2

        def _spy_copy(src, dst, *args, **kwargs):
            copy_calls.append((str(src), str(dst)))
            return original_copy(src, dst, *args, **kwargs)

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("shutil.copy2", _spy_copy),
        ):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-2",
                integration_branch="egg/issue-2548/slice-2",
            )

        # Every copied src must be a slice-2 file.
        for src, _dst in copy_calls:
            assert "implement-slice-2" in src, (
                f"hook copied a file that is not slice-2's BRC: {src}"
            )
        # And we must have copied at least one file (otherwise the test
        # is silently a no-op).
        assert any("implement-slice-2.md" in src for src, _ in copy_calls), (
            "hook should have copied slice-2.md"
        )

    def test_no_per_slice_files_returns_false(self, tmp_path, pipeline, make_spawner):
        """If ``_write_brc_history`` produced no files for the named
        slice (e.g. a slice that proposed via the no-op path with
        ``no_test_changes_needed`` and emitted no consensus), the hook
        warns and returns False rather than pushing an empty commit."""
        # Create the directory but no files for slice-1.
        brc = tmp_path / ".egg-state" / "brc-history"
        brc.mkdir(parents=True, exist_ok=True)
        spawner = make_spawner()
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False
        spawner.gateway.push_worktree_branch.assert_not_called()


# ----------------------------------------------------------------------
# Symlink defense
# ----------------------------------------------------------------------


class TestSymlinkDefense:
    def test_symlink_brc_file_is_skipped(self, tmp_path, pipeline, make_spawner):
        """A symlink in the staged ``.egg-state/brc-history/`` directory
        must not be copied onto the integration worktree.

        After #2755 the hook scans a per-tick staging directory rather
        than the work worktree, so the seeded symlink lives at the
        seed root and ``_writer_stub`` mirrors it (as a symlink) into
        staging. The defense is defense-in-depth — even though staging
        is freshly ``mkdtemp``'d per hook tick, a future writer change
        that synthesised a symlink from attacker-controlled metadata
        would otherwise smuggle unrelated content onto the slice PR."""
        brc = tmp_path / ".egg-state" / "brc-history"
        brc.mkdir(parents=True, exist_ok=True)

        # Plant a real file outside the brc-history dir, then symlink
        # to it from the canonical BRC filename.
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVATE\n")
        sym_md = brc / "2548-implement-slice-1.md"
        sym_md.symlink_to(secret)
        # Keep a real .json companion so the hook has SOME file to
        # process — the symlinked .md should still be skipped.
        (brc / "2548-implement-slice-1.json").write_text("[]\n")

        spawner = make_spawner()

        copy_calls: list[tuple] = []
        import shutil

        original_copy = shutil.copy2

        def _spy_copy(src, dst, *args, **kwargs):
            copy_calls.append((str(src), str(dst)))
            return original_copy(src, dst, *args, **kwargs)

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("shutil.copy2", _spy_copy),
        ):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        # No ``.md`` file should appear in copy_calls — the only ``.md``
        # path under the staged ``brc-history`` directory is a symlink,
        # and the defense must skip it. The ``.json`` companion (a real
        # file) is still copied, so the hook is not a no-op.
        md_srcs = [src for src, _ in copy_calls if src.endswith(".md")]
        assert not md_srcs, f"symlinked .md leaked into copy_calls (defense broken): {md_srcs}"
        assert any(src.endswith(".json") for src, _ in copy_calls), (
            "the real .json companion should still have been copied"
        )


# ----------------------------------------------------------------------
# Best-effort failure semantics
# ----------------------------------------------------------------------


class TestFailureSemantics:
    def test_writer_raise_returns_false(self, tmp_path, pipeline, make_spawner):
        """If ``_write_brc_history`` raises (e.g. message store is
        unreachable mid-tick), the hook swallows the error, logs a
        warning, and returns False — never propagates."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()

        def _raising_writer(*args, **kwargs):
            raise RuntimeError("message store down")

        with patch("routes.pipelines._write_brc_history", _raising_writer):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False
        spawner.gateway.push_worktree_branch.assert_not_called()

    def test_worktree_add_failure_returns_false(self, tmp_path, pipeline, make_spawner):
        """If ``git worktree add`` fails, the hook returns False without
        attempting to commit or push.  The integration branch is
        unmodified — fail-soft."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()

        import subprocess

        def _selective_run(cmd, **kwargs):
            # Fail only on ``git worktree add``; pass everything else.
            if "worktree" in cmd and "add" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="cannot lock")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("subprocess.run", _selective_run),
        ):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False
        spawner.gateway.push_worktree_branch.assert_not_called()

    def test_push_raise_returns_false(self, tmp_path, pipeline, make_spawner):
        """If ``push_worktree_branch`` itself raises, swallow + False.
        Any uncaught exception here would propagate up through
        ``_run_one_slice_inner`` and abort the slice — exactly what
        the best-effort semantics promise to prevent."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner(push_raise=RuntimeError("gateway unavailable"))
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False

    def test_push_returns_not_ok_returns_false(self, tmp_path, pipeline, make_spawner):
        """If the push request returns a ``PushResult(ok=False)`` (e.g.
        non_fast_forward, auth_failed), the hook returns False and
        does not raise.  PR creation can still proceed."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner(
            push_result=PushResult(ok=False, category="non_fast_forward", detail="rejected")
        )
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False

    def test_commit_raise_returns_false(self, tmp_path, pipeline, make_spawner):
        """If ``_commit_statefiles_to_worktree`` raises (e.g. a
        subprocess.CalledProcessError from ``git add``), the hook
        swallows the error and returns False without pushing."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()

        def _raising_commit(*args, **kwargs):
            raise RuntimeError("git add failed")

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("routes.pipelines._commit_statefiles_to_worktree", _raising_commit),
        ):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False
        spawner.gateway.push_worktree_branch.assert_not_called()


# ----------------------------------------------------------------------
# Idempotency / cleanup
# ----------------------------------------------------------------------


class TestIdempotencyAndCleanup:
    def test_temp_worktree_cleanup_runs_on_success(self, tmp_path, pipeline, make_spawner):
        """The temp worktree is removed after a successful run so the
        pipeline doesn't accumulate stale temp dirs across slices."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        seen_remove_paths: list[str] = []

        def _spy_run(cmd, **kwargs):
            if "worktree" in cmd and "remove" in cmd:
                seen_remove_paths.append(cmd[-1])
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("subprocess.run", _spy_run),
        ):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        # We saw at least one ``git worktree remove`` call in cleanup —
        # that's the housekeeping that keeps temp dirs from
        # accumulating across slices.
        assert seen_remove_paths, "git worktree remove must be invoked in cleanup"

    def test_temp_worktree_cleanup_runs_on_push_failure(self, tmp_path, pipeline, make_spawner):
        """Cleanup MUST run even when the push step fails — otherwise a
        flaky gateway leaks temp worktree directories."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner(push_raise=RuntimeError("gateway down"))
        seen_remove_paths: list[str] = []

        def _spy_run(cmd, **kwargs):
            if "worktree" in cmd and "remove" in cmd:
                seen_remove_paths.append(cmd[-1])
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("subprocess.run", _spy_run),
        ):
            ok = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok is False
        assert seen_remove_paths, (
            "cleanup must run even when push raises — temp dirs would leak otherwise"
        )

    def test_idempotent_on_no_op_run(self, tmp_path, pipeline, make_spawner):
        """A re-run after a successful run is a no-op: the
        ``_commit_statefiles_to_worktree`` helper skips when nothing
        is staged, and the push fast-forwards on no-op.  The hook must
        still return True so callers can treat it as a successful
        re-run."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            ok1 = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
            ok2 = _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )
        assert ok1 is True
        assert ok2 is True
        # Both runs invoked push (the second time it's a fast-forward
        # no-op, but the helper still calls the gateway to confirm
        # origin matches).
        assert spawner.gateway.push_worktree_branch.call_count == 2


# ----------------------------------------------------------------------
# Identifier resolution
# ----------------------------------------------------------------------


class TestIdentifierResolution:
    def test_issue_pipeline_uses_issue_number_in_filename(self, tmp_path, pipeline, make_spawner):
        """The hook uses ``_brc_history_identifier(pipeline)`` to build
        the per-slice filename.  For an issue pipeline, that's the
        issue number (int) — the path interpolation must produce
        ``2548-implement-slice-1.md`` etc., regardless of whether the
        source is the work worktree (pre-#2755) or the per-tick
        staging directory (post-#2755)."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()

        copy_srcs: list[str] = []
        import shutil

        original_copy = shutil.copy2

        def _spy_copy(src, dst, *args, **kwargs):
            copy_srcs.append(str(src))
            return original_copy(src, dst, *args, **kwargs)

        with (
            patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)),
            patch("shutil.copy2", _spy_copy),
        ):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        # The canonical filename ``2548-implement-slice-1.{md,json}``
        # must appear as a copy source (which lives under the staging
        # directory post-#2755 — the test does not pin the directory
        # prefix because that is implementation detail).
        copy_basenames = [Path(s).name for s in copy_srcs]
        assert "2548-implement-slice-1.md" in copy_basenames, (
            f"slice-1 .md must be copied; got copies {copy_basenames!r}"
        )
        assert "2548-implement-slice-1.json" in copy_basenames, (
            f"slice-1 .json must be copied; got copies {copy_basenames!r}"
        )


# ----------------------------------------------------------------------
# Gateway allowlist compatibility (#2684)
# ----------------------------------------------------------------------


class TestGatewayAllowlistCompatibility:
    """Regression coverage for #2684.

    The hook constructs a temp worktree and passes ``repo_path=str(wt_path)``
    to ``gateway.push_worktree_branch``.  The gateway's
    ``validate_repo_path`` only accepts paths under
    ``ALLOWED_REPO_PATHS`` (``/home/egg/.egg-worktrees/`` et al.); a
    ``/tmp`` location is rejected and the push fails silently — slice
    PRs then open without their consensus-history file.  The hook must
    therefore root its temp worktree inside ``WORKTREE_BASE_DIR``.
    """

    def test_temp_worktree_is_rooted_under_worktree_base_dir(
        self, tmp_path, pipeline, make_spawner, monkeypatch
    ):
        import routes.pipelines as pipelines_mod

        fake_base = tmp_path / "egg-worktrees-root"
        fake_base.mkdir()
        monkeypatch.setattr(pipelines_mod, "WORKTREE_BASE_DIR", fake_base)

        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        with patch("routes.pipelines._write_brc_history", _writer_stub(tmp_path)):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        spawner.gateway.push_worktree_branch.assert_called_once()
        repo_path = spawner.gateway.push_worktree_branch.call_args.kwargs["repo_path"]
        assert repo_path.startswith(str(fake_base) + "/"), (
            f"slice BRC temp worktree must live under WORKTREE_BASE_DIR; "
            f"got {repo_path!r}, expected prefix {str(fake_base)!r}"
        )

    def test_production_worktree_base_dir_lies_within_gateway_allowlist(self):
        """Pin the contract between orchestrator and gateway.

        ``WORKTREE_BASE_DIR`` is where the slice-BRC temp worktree
        lives; the gateway's ``ALLOWED_REPO_PATHS`` must contain a
        prefix that covers it.  This test reads ``WORKTREE_BASE_DIR``
        from the orchestrator module rather than hardcoding the
        production path, so a drift on *either* side
        (orchestrator-side path move OR gateway-side allowlist tweak)
        trips the regression instead of silently passing against a
        stale hardcoded prefix.
        """
        from routes.pipelines import WORKTREE_BASE_DIR

        from gateway.git_client import validate_repo_path

        candidate = str(WORKTREE_BASE_DIR / "egg-slice-brc-pipeline-x-slice-y-abc" / "wt")
        ok, error = validate_repo_path(candidate)
        assert ok, f"WORKTREE_BASE_DIR drifted out of gateway ALLOWED_REPO_PATHS: {error}"


# ----------------------------------------------------------------------
# Work-worktree isolation (#2755 regression)
# ----------------------------------------------------------------------


class TestWorkWorktreeIsolation:
    """Regression coverage for #2755.

    Pre-fix, the slice hook called ``_write_brc_history`` against the
    work worktree directly, leaving per-slice files on
    ``work/.egg-state/brc-history/``. The end-of-implement-phase
    commit then picked those up and committed them to ``work``,
    causing add/add merge conflicts when slice PRs (slice → work)
    tried to merge the same files.

    Post-fix the hook stages to a per-tick temp directory; the work
    worktree's ``.egg-state/brc-history/`` directory is never
    written to by this hook.
    """

    def test_work_worktree_brc_history_untouched_by_slice_hook(
        self, tmp_path, pipeline, make_spawner
    ):
        """After the slice hook runs against a clean work worktree,
        ``<work>/.egg-state/brc-history/`` must contain no per-slice
        files (#2755). The staged copy lives under a temp directory
        rooted in ``WORKTREE_BASE_DIR`` and is cleaned up after the
        hook returns."""
        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        # Capture the work-worktree brc-history state BEFORE the hook
        # runs so we can compare against the AFTER state. The seed
        # above writes into ``tmp_path/.egg-state/brc-history/`` to
        # supply the ``_writer_stub`` source — but the hook's writer
        # call (against staging) must not echo any new files back to
        # this directory.
        brc_dir = tmp_path / ".egg-state" / "brc-history"
        before_files = sorted(p.name for p in brc_dir.iterdir())

        spawner = make_spawner()
        stub = _writer_stub(tmp_path)
        with patch("routes.pipelines._write_brc_history", stub):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        after_files = sorted(p.name for p in brc_dir.iterdir())
        # The work-worktree directory contents must be exactly what we
        # seeded — the hook must not have added anything to it.
        assert after_files == before_files, (
            "slice hook touched the work-worktree brc-history dir (#2755 regression); "
            f"before={before_files!r}, after={after_files!r}"
        )

        # And the writer must have been called against a path that is
        # NOT the work worktree.
        assert stub.staging_paths, "writer was never called"
        for staging in stub.staging_paths:
            assert staging != str(tmp_path), (
                f"writer was called against the work worktree (#2755 regression): {staging}"
            )

    def test_staging_dir_cleaned_up_after_hook(self, tmp_path, pipeline, make_spawner, monkeypatch):
        """The per-tick staging directory must be removed when the hook
        returns, so pipelines do not accumulate stale staging dirs
        under ``WORKTREE_BASE_DIR`` across slices (#2755)."""
        import routes.pipelines as pipelines_mod

        fake_base = tmp_path / "egg-worktrees-root"
        fake_base.mkdir()
        monkeypatch.setattr(pipelines_mod, "WORKTREE_BASE_DIR", fake_base)

        _seed_per_slice_brc_files(tmp_path, identifier=2548, slice_ids=["slice-1"])
        spawner = make_spawner()
        stub = _writer_stub(tmp_path)
        with patch("routes.pipelines._write_brc_history", stub):
            _commit_slice_brc_history_to_integration_branch(
                pipeline,
                spawner,
                tmp_path,
                slice_id="slice-1",
                integration_branch="egg/issue-2548/slice-1",
            )

        # The staging path was used; after cleanup it must no longer
        # exist on disk (``shutil.rmtree(tmp_worktree, ignore_errors=True)``
        # in the hook's ``finally`` clause).
        assert stub.staging_paths, "writer was never called"
        for staging in stub.staging_paths:
            assert not Path(staging).exists(), (
                f"staging directory {staging!r} leaked after hook returned"
            )
