"""Unit tests for agent_salvage_cleanup (#2446)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent_salvage_cleanup import (
    CLEANUP_PIPELINE_ID,
    CleanupReport,
    RecoveryRefCleaner,
    is_recovery_ref,
    list_recovery_refs,
    sweep_recovery_refs,
)
from gateway_client import PushResult

# ---------------------------------------------------------------------------
# Real-git helpers — building a tiny clone-with-tracking-refs lets the
# committer-date and reachability paths exercise actual plumbing instead
# of mock side-effects.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=t@t.com",
            "-c",
            "user.name=Tester",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--initial-branch", "main", cwd=path)
    (path / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "seed", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _commit_at(
    path: Path,
    filename: str,
    content: str,
    message: str,
    *,
    when: datetime | None = None,
) -> str:
    """Commit a file. ``when`` overrides committer/author date so age tests
    can place a commit arbitrarily far in the past."""
    (path / filename).write_text(content)
    _git("add", filename, cwd=path)
    if when is None:
        _git("commit", "-q", "-m", message, cwd=path)
    else:
        iso = when.isoformat()
        cmd = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=t@t.com",
            "-c",
            "user.name=Tester",
            "-C",
            str(path),
            "commit",
            "-q",
            "-m",
            message,
        ]
        import os

        env = os.environ.copy()
        env["GIT_COMMITTER_DATE"] = iso
        env["GIT_AUTHOR_DATE"] = iso
        subprocess.run(cmd, cwd=path, capture_output=True, text=True, check=True, env=env)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _set_remote_tracking(repo: Path, ref: str, sha: str) -> None:
    """Stand in for a fetched ``refs/remotes/origin/<ref>``."""
    _git("update-ref", f"refs/remotes/origin/{ref}", sha, cwd=repo)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


class TestIsRecoveryRef:
    def test_matches_full_recovery_path(self) -> None:
        assert is_recovery_ref("egg/recovered/issue-99/coder/abc123def456")

    def test_rejects_other_egg_branches(self) -> None:
        assert not is_recovery_ref("egg/issue-99/work")

    def test_rejects_empty(self) -> None:
        assert not is_recovery_ref("")


# ---------------------------------------------------------------------------
# list_recovery_refs filters output of list_remote_branches_with_shas
# ---------------------------------------------------------------------------


class TestListRecoveryRefs:
    def test_filters_to_recovery_namespace(self) -> None:
        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = {
            "main": "0" * 40,
            "egg/issue-99/work": "1" * 40,
            "egg/recovered/issue-99/coder/abc123": "a" * 40,
            "egg/recovered/issue-99/slice-1-tester/def456": "b" * 40,
        }
        out = list_recovery_refs(gateway, "/repo")
        assert out == {
            "egg/recovered/issue-99/coder/abc123": "a" * 40,
            "egg/recovered/issue-99/slice-1-tester/def456": "b" * 40,
        }

    def test_returns_empty_on_empty(self) -> None:
        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = {}
        assert list_recovery_refs(gateway, "/repo") == {}


# ---------------------------------------------------------------------------
# sweep_recovery_refs
# ---------------------------------------------------------------------------


class TestSweepRecoveryRefs:
    def _make_gateway(
        self,
        *,
        refs: dict[str, str] | None = None,
        delete_result: PushResult | None = None,
    ) -> MagicMock:
        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = refs or {}
        gateway.fetch_branch.return_value = True
        gateway.delete_remote_branch.return_value = (
            delete_result if delete_result is not None else PushResult(ok=True)
        )
        return gateway

    def test_no_refs_no_actions(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        gateway = self._make_gateway(refs={})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)
        assert report.refs_inspected == 0
        assert report.refs_deleted == 0
        gateway.delete_remote_branch.assert_not_called()

    def test_deletes_old_unreachable_ref(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        # Commit dated 200 days ago, well past the 90-day cutoff.
        old = datetime.now(UTC) - timedelta(days=200)
        sha = _commit_at(repo, "old.txt", "stale", "old work", when=old)
        # Mirror the recovery ref into refs/remotes/origin/...
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        _set_remote_tracking(repo, ref_name, sha)

        gateway = self._make_gateway(refs={ref_name: sha})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)

        assert report.refs_inspected == 1
        assert report.refs_deleted == 1
        assert report.deleted_refs == [ref_name]
        gateway.delete_remote_branch.assert_called_once()
        kwargs = gateway.delete_remote_branch.call_args.kwargs
        assert kwargs["branch"] == ref_name
        assert kwargs["pipeline_id"] == CLEANUP_PIPELINE_ID

    def test_skips_recent_ref(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        recent = datetime.now(UTC) - timedelta(days=5)
        sha = _commit_at(repo, "fresh.txt", "fresh", "recent work", when=recent)
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        _set_remote_tracking(repo, ref_name, sha)

        gateway = self._make_gateway(refs={ref_name: sha})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)

        assert report.refs_skipped_recent == 1
        assert report.refs_deleted == 0
        gateway.delete_remote_branch.assert_not_called()
        # Oldest-remaining metric tracks kept refs.
        assert report.oldest_remaining_age_days is not None
        assert 4.0 < report.oldest_remaining_age_days < 6.5

    def test_skips_reachable_from_non_recovery_branch(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        old = datetime.now(UTC) - timedelta(days=200)
        sha = _commit_at(repo, "old.txt", "old", "stale work", when=old)
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        _set_remote_tracking(repo, ref_name, sha)
        # Operator already replayed: this commit also lives at refs/remotes/origin/recovered/issue-99
        _set_remote_tracking(repo, "recovered/issue-99", sha)

        gateway = self._make_gateway(refs={ref_name: sha})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)

        assert report.refs_skipped_reachable == 1
        assert report.refs_deleted == 0
        gateway.delete_remote_branch.assert_not_called()

    def test_skips_unknown_age_when_sha_missing_locally(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        # SHA deliberately not present in the local repo.
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        gateway = self._make_gateway(refs={ref_name: "0" * 40})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)

        assert report.refs_skipped_unknown_age == 1
        assert report.refs_deleted == 0
        gateway.delete_remote_branch.assert_not_called()

    def test_dry_run_does_not_call_delete(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        old = datetime.now(UTC) - timedelta(days=200)
        sha = _commit_at(repo, "old.txt", "old", "stale", when=old)
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        _set_remote_tracking(repo, ref_name, sha)

        gateway = self._make_gateway(refs={ref_name: sha})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90, dry_run=True)

        assert report.refs_deleted == 1
        assert report.deleted_refs == [ref_name]
        gateway.delete_remote_branch.assert_not_called()

    def test_already_deleted_treated_as_success(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        old = datetime.now(UTC) - timedelta(days=200)
        sha = _commit_at(repo, "old.txt", "old", "stale", when=old)
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        _set_remote_tracking(repo, ref_name, sha)

        gateway = self._make_gateway(
            refs={ref_name: sha},
            delete_result=PushResult(ok=False, category="already_deleted"),
        )
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)
        assert report.refs_deleted == 1
        assert report.refs_skipped_error == 0

    def test_real_delete_failure_counted_as_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        old = datetime.now(UTC) - timedelta(days=200)
        sha = _commit_at(repo, "old.txt", "old", "stale", when=old)
        ref_name = "egg/recovered/issue-99/coder/abc123def456"
        _set_remote_tracking(repo, ref_name, sha)

        gateway = self._make_gateway(
            refs={ref_name: sha},
            delete_result=PushResult(ok=False, category="auth_failed", detail="403"),
        )
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)
        assert report.refs_skipped_error == 1
        assert report.refs_deleted == 0

    def test_list_failure_aborts_cleanly(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        gateway = MagicMock()
        gateway.fetch_branch.return_value = True
        gateway.list_remote_branches_with_shas.side_effect = RuntimeError("gateway down")

        report = sweep_recovery_refs(gateway, repo, ttl_days=90)
        assert report.error is not None
        assert "gateway down" in report.error
        gateway.delete_remote_branch.assert_not_called()

    def test_per_ref_classify_failure_does_not_abort_loop(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        old = datetime.now(UTC) - timedelta(days=200)
        sha_b = _commit_at(repo, "b.txt", "b", "b commit", when=old)
        ref_a = "egg/recovered/issue-99/coder/aaa111"
        ref_b = "egg/recovered/issue-99/tester/bbb222"
        # Only B is locally available; A's SHA is unknown.
        _set_remote_tracking(repo, ref_b, sha_b)

        gateway = self._make_gateway(refs={ref_a: "0" * 40, ref_b: sha_b})
        report = sweep_recovery_refs(gateway, repo, ttl_days=90)
        # A → unknown_age (no local commit), B → deleted.
        assert report.refs_inspected == 2
        assert report.refs_deleted == 1
        assert report.refs_skipped_unknown_age == 1


# ---------------------------------------------------------------------------
# RecoveryRefCleaner background driver
# ---------------------------------------------------------------------------


class TestRecoveryRefCleanerLifecycle:
    def test_start_stop_is_idempotent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = {}
        gateway.fetch_branch.return_value = True

        # Long interval → loop sleeps and never sweeps within the test.
        cleaner = RecoveryRefCleaner(gateway, repo, ttl_days=90, interval_seconds=3600)
        cleaner.start()
        # Second start is a no-op.
        cleaner.start()
        assert cleaner.is_running
        cleaner.stop(timeout=2)
        assert not cleaner.is_running

    def test_run_once_returns_report(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = {}
        gateway.fetch_branch.return_value = True

        cleaner = RecoveryRefCleaner(gateway, repo, ttl_days=90, interval_seconds=3600)
        report = cleaner.run_once()
        assert isinstance(report, CleanupReport)
        assert report.refs_inspected == 0


# ---------------------------------------------------------------------------
# env_config knobs
# ---------------------------------------------------------------------------


class TestEnvConfig:
    def test_enabled_default_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from env_config import get_recovery_ref_cleanup_enabled

        monkeypatch.delenv("EGG_ORCH_RECOVERY_REF_CLEANUP_ENABLED", raising=False)
        assert get_recovery_ref_cleanup_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off"])
    def test_enabled_false(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        from env_config import get_recovery_ref_cleanup_enabled

        monkeypatch.setenv("EGG_ORCH_RECOVERY_REF_CLEANUP_ENABLED", raw)
        assert get_recovery_ref_cleanup_enabled() is False

    def test_enabled_unrecognised_falls_back_to_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from env_config import get_recovery_ref_cleanup_enabled

        monkeypatch.setenv("EGG_ORCH_RECOVERY_REF_CLEANUP_ENABLED", "maybe")
        assert get_recovery_ref_cleanup_enabled() is True

    def test_ttl_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from env_config import get_recovery_ref_ttl_days

        monkeypatch.delenv("EGG_ORCH_RECOVERY_REF_TTL_DAYS", raising=False)
        assert get_recovery_ref_ttl_days() == 90

    def test_ttl_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from env_config import get_recovery_ref_ttl_days

        monkeypatch.setenv("EGG_ORCH_RECOVERY_REF_TTL_DAYS", "30")
        assert get_recovery_ref_ttl_days() == 30

    def test_interval_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from env_config import get_recovery_ref_cleanup_interval_seconds

        monkeypatch.delenv("EGG_ORCH_RECOVERY_REF_CLEANUP_INTERVAL_SECONDS", raising=False)
        assert get_recovery_ref_cleanup_interval_seconds() == 86400.0
