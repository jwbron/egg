"""Tests for the ``/api/v1/worktrees/prune`` gateway route (#1759).

Covers:

- Launcher-bearer-token enforcement (401 on missing / wrong credentials).
- Dry-run default (``dry_run=true``) returns the orphan plan without
  calling ``cleanup_orphaned_worktrees``.
- Non-dry-run path calls through to the filesystem cleanup helper.
- In-process mutex serializes concurrent callers (second caller sees 409).
- The orchestrator-proxied payload shape stays stable so the MCP tool
  (and operator skills) can rely on it.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

# conftest.py loads the gateway modules under bare names.
import gateway

TEST_LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def launcher_auth_headers():
    return {"Authorization": f"Bearer {TEST_LAUNCHER_SECRET}"}


@pytest.fixture
def fake_manager():
    """A ``WorktreeManager`` substitute with the three helpers the route uses."""
    manager = MagicMock()
    manager.git_worktree_prune_all.return_value = {"egg": ["worktrees/stale-abc"]}
    manager.list_orphan_worktree_dirs.return_value = [
        "/home/egg/.egg-worktrees/ghost",
    ]
    manager.cleanup_orphaned_worktrees.return_value = 1
    return manager


@pytest.fixture(autouse=True)
def _reset_prune_lock():
    """Release the module-level prune lock between tests."""
    # The lock is created at import time and lives for the process.
    # Safest reset: drain any holder by grabbing and releasing.
    if gateway._worktree_prune_lock.locked():
        try:
            gateway._worktree_prune_lock.release()
        except RuntimeError:
            pass
    yield
    if gateway._worktree_prune_lock.locked():
        try:
            gateway._worktree_prune_lock.release()
        except RuntimeError:
            pass


class TestWorktreesPruneAuth:
    """Launcher auth is the only thing standing between an orchestrator-
    side compromise and wholesale worktree removal. Belt-and-suspenders
    coverage because this route mutates the host filesystem."""

    def test_missing_auth_returns_401(self, client):
        response = client.post("/api/v1/worktrees/prune", json={"dry_run": True})
        assert response.status_code == 401

    def test_wrong_bearer_token_returns_401(self, client):
        response = client.post(
            "/api/v1/worktrees/prune",
            json={"dry_run": True},
            headers={"Authorization": "Bearer nope"},
        )
        assert response.status_code == 401


class TestWorktreesPruneDryRun:
    """Default behavior — dry_run=true — must not mutate state."""

    def test_dry_run_default_returns_plan_without_cleanup(
        self, client, launcher_auth_headers, fake_manager
    ):
        with patch.object(gateway, "get_worktree_manager", return_value=fake_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={},  # no body → dry_run defaults to true
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        body = response.get_json()
        data = body["data"]
        assert data["dry_run"] is True
        assert data["git_worktree_prune"] == {"egg": ["worktrees/stale-abc"]}
        assert data["orphan_dirs"] == ["/home/egg/.egg-worktrees/ghost"]
        # The mutation helper must NOT have been called.
        fake_manager.cleanup_orphaned_worktrees.assert_not_called()
        # Reported count stays at zero under dry-run.
        assert data["removed_count"] == 0
        assert data["removed_paths"] == []

    def test_dry_run_true_explicit(self, client, launcher_auth_headers, fake_manager):
        with patch.object(gateway, "get_worktree_manager", return_value=fake_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": True},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        fake_manager.cleanup_orphaned_worktrees.assert_not_called()

    def test_dry_run_with_no_orphans_returns_empty_plan(self, client, launcher_auth_headers):
        clean_manager = MagicMock()
        clean_manager.git_worktree_prune_all.return_value = {}
        clean_manager.list_orphan_worktree_dirs.return_value = []
        clean_manager.cleanup_orphaned_worktrees.return_value = 0

        with patch.object(gateway, "get_worktree_manager", return_value=clean_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": True},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["orphan_dirs"] == []
        assert data["removed_count"] == 0


class TestWorktreesPruneMutation:
    """Non-dry-run path actually cleans up orphaned dirs."""

    def test_dry_run_false_calls_cleanup(
        self, client, launcher_auth_headers, fake_manager, tmp_path
    ):
        # The route double-checks whether paths still exist after cleanup
        # to build the ``removed_paths`` list. We simulate a pre-existing
        # path that gets removed during cleanup.
        orphan_path = tmp_path / "ghost"
        orphan_path.mkdir()
        fake_manager.list_orphan_worktree_dirs.return_value = [str(orphan_path)]

        def _fake_cleanup(active_containers):
            orphan_path.rmdir()
            return 1

        fake_manager.cleanup_orphaned_worktrees.side_effect = _fake_cleanup

        with patch.object(gateway, "get_worktree_manager", return_value=fake_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": False},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["dry_run"] is False
        assert data["removed_count"] == 1
        assert str(orphan_path) in data["removed_paths"]
        fake_manager.cleanup_orphaned_worktrees.assert_called_once()

    def test_cleanup_passes_active_container_ids_from_session_manager(
        self, client, launcher_auth_headers, fake_manager, tmp_path
    ):
        """BLOCKER fix on coder commit ac5c4900f:

        The route MUST pass an ``active_containers`` set derived from
        ``_collect_active_container_ids()`` — NOT an empty set. An empty
        set would cause every live-pipeline worktree to be treated as an
        orphan and wiped. The session manager is the primary source of
        truth, with ``docker ps`` as a safety-net fallback.

        This test stubs ``_collect_active_container_ids`` to return a
        non-empty set and asserts the route forwards exactly that set.
        """
        orphan_path = tmp_path / "orphan"
        orphan_path.mkdir()
        fake_manager.list_orphan_worktree_dirs.return_value = [str(orphan_path)]

        live_containers = {"pipeline-issue-1759-v3-coder", "pipeline-issue-1759-v3-tester"}

        with (
            patch.object(gateway, "get_worktree_manager", return_value=fake_manager),
            patch.object(gateway, "_collect_active_container_ids", return_value=live_containers),
        ):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": False},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200

        # Verify the cleanup helper got the exact live set — not empty,
        # not the orphan list, not something else.
        _args, kwargs = fake_manager.cleanup_orphaned_worktrees.call_args
        assert kwargs.get("active_containers") == live_containers, (
            "cleanup_orphaned_worktrees must receive the live container set "
            f"from _collect_active_container_ids, got: {kwargs!r}"
        )

        # And list_orphan_worktree_dirs must see it too — otherwise the
        # pre-enumeration would report a live worktree as an orphan in the
        # dry_run plan.
        _list_args, list_kwargs = fake_manager.list_orphan_worktree_dirs.call_args
        assert list_kwargs.get("active_containers") == live_containers

    def test_cleanup_still_runs_when_session_manager_unavailable(
        self, client, launcher_auth_headers, fake_manager, tmp_path
    ):
        """If ``_collect_active_container_ids`` returns an empty set
        because both the session manager and docker probe failed, the
        route must still proceed — but every dir will look orphaned.

        This is the worst-case fallback; the test simply asserts the
        route does not crash and that the empty set reaches cleanup.
        (The collector helper is responsible for the degrade-silently
        semantics — see its own unit tests.)
        """
        orphan_path = tmp_path / "orphan"
        orphan_path.mkdir()
        fake_manager.list_orphan_worktree_dirs.return_value = [str(orphan_path)]

        with (
            patch.object(gateway, "get_worktree_manager", return_value=fake_manager),
            patch.object(gateway, "_collect_active_container_ids", return_value=set()),
        ):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": False},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        _args, kwargs = fake_manager.cleanup_orphaned_worktrees.call_args
        assert kwargs.get("active_containers") == set()

    def test_mutation_without_orphans_skips_cleanup(self, client, launcher_auth_headers):
        """Empty orphan list → no cleanup call even on ``dry_run=false``."""
        clean_manager = MagicMock()
        clean_manager.git_worktree_prune_all.return_value = {}
        clean_manager.list_orphan_worktree_dirs.return_value = []

        with patch.object(gateway, "get_worktree_manager", return_value=clean_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": False},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        clean_manager.cleanup_orphaned_worktrees.assert_not_called()


class TestWorktreesPruneResponseShape:
    """Stable contract with the orchestrator-side MCP tool / deploy skills."""

    def test_response_keys(self, client, launcher_auth_headers, fake_manager):
        with patch.object(gateway, "get_worktree_manager", return_value=fake_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": True},
                headers=launcher_auth_headers,
            )
        body = response.get_json()
        assert body["success"] is True
        for key in (
            "dry_run",
            "git_worktree_prune",
            "orphan_dirs",
            "removed_count",
            "removed_paths",
        ):
            assert key in body["data"], f"missing key in response: {key}"


class TestWorktreesPruneMutex:
    """A single in-process mutex serializes prune activity.

    The second concurrent caller must receive 409 (or the shared mutex
    contract elsewhere in the codebase would silently break).
    """

    def test_concurrent_call_returns_409(self, client, launcher_auth_headers, fake_manager):
        """When lock.acquire fails (concurrent caller), the route returns 409."""
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False

        with (
            patch.object(gateway, "_worktree_prune_lock", fake_lock),
            patch.object(gateway, "get_worktree_manager", return_value=fake_manager),
        ):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": True},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        # release() must NOT be called when acquire failed — otherwise
        # we'd release a lock we don't hold.
        fake_lock.release.assert_not_called()

    def test_lock_is_released_on_success(self, client, launcher_auth_headers, fake_manager):
        """After a happy-path call, the mutex must be free for the next caller."""
        with patch.object(gateway, "get_worktree_manager", return_value=fake_manager):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": True},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        # Immediately try to acquire — must succeed because the route released.
        assert gateway._worktree_prune_lock.acquire(blocking=False)
        gateway._worktree_prune_lock.release()

    def test_lock_is_released_on_exception(self, client, launcher_auth_headers):
        """Finally-block releases the lock even if the handler raises."""
        bad_manager = MagicMock()
        bad_manager.git_worktree_prune_all.side_effect = RuntimeError("boom")

        with patch.object(gateway, "get_worktree_manager", return_value=bad_manager):
            # The route doesn't trap RuntimeError, so this will 500 — but
            # the finally-block must still release the lock.
            try:
                client.post(
                    "/api/v1/worktrees/prune",
                    json={"dry_run": True},
                    headers=launcher_auth_headers,
                )
            except Exception:
                pass

        assert gateway._worktree_prune_lock.acquire(blocking=False)
        gateway._worktree_prune_lock.release()


class TestWorktreeManagerHelperContract:
    """Sanity checks on the helpers the route now depends on — makes a
    rename visible at test time rather than at first-call time."""

    def test_list_orphan_worktree_dirs_exists(self):
        from worktree_manager import WorktreeManager

        assert callable(getattr(WorktreeManager, "list_orphan_worktree_dirs", None))

    def test_git_worktree_prune_all_exists(self):
        from worktree_manager import WorktreeManager

        assert callable(getattr(WorktreeManager, "git_worktree_prune_all", None))

    def test_cleanup_orphaned_worktrees_exists(self):
        from worktree_manager import WorktreeManager

        assert callable(getattr(WorktreeManager, "cleanup_orphaned_worktrees", None))


class TestListOrphanWorktreePathGuard:
    """``list_orphan_worktree_dirs`` must not follow symlinks outside the base."""

    def test_symlink_escape_is_skipped(self, tmp_path, monkeypatch):
        """A symlink pointing outside the worktree base is filtered out."""
        import threading

        from worktree_manager import WorktreeManager

        base = tmp_path / "worktrees"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        # Normal dir inside base — should be reported as orphan.
        (base / "normal-dir").mkdir()
        # Symlink inside base → outside base: path-guard must skip it.
        (base / "escape").symlink_to(outside, target_is_directory=True)

        mgr = WorktreeManager.__new__(WorktreeManager)
        mgr.worktree_base = base
        mgr.repos_base = tmp_path / "repos"  # unused for this call
        mgr._lock = threading.Lock()
        mgr._active_worktrees = {}

        orphans = mgr.list_orphan_worktree_dirs(active_containers=set())
        assert any(o.endswith("normal-dir") for o in orphans)
        assert not any("outside" in o for o in orphans), (
            "symlink escape must not appear in orphan list"
        )


class TestListOrphanWorktreeDirsActiveGuard:
    """``list_orphan_worktree_dirs`` must respect ``_active_worktrees`` so the
    dry-run prune route accurately reflects what cleanup would actually skip."""

    def test_in_flight_worktree_excluded_from_orphan_list(self, tmp_path):
        """A dir tracked in ``_active_worktrees`` must not appear as orphan."""
        import threading

        from worktree_manager import WorktreeInfo, WorktreeManager

        base = tmp_path / "worktrees"
        base.mkdir()
        (base / "issue-42-coder").mkdir()
        (base / "genuine-orphan").mkdir()

        mgr = WorktreeManager.__new__(WorktreeManager)
        mgr.worktree_base = base
        mgr.repos_base = tmp_path / "repos"
        mgr._lock = threading.Lock()
        mgr._active_worktrees = {
            "issue-42-coder": [
                WorktreeInfo(
                    container_id="issue-42-coder",
                    repo_name="webapp",
                    branch="egg/issue-42-coder/work",
                    worktree_path=base / "issue-42-coder" / "webapp",
                    git_dir=None,
                )
            ]
        }

        orphans = mgr.list_orphan_worktree_dirs(active_containers=set())
        orphan_names = [o.split("/")[-1] for o in orphans]
        assert "genuine-orphan" in orphan_names
        assert "issue-42-coder" not in orphan_names


class TestCollectActiveContainerIds:
    """BLOCKER fix coverage: ``_collect_active_container_ids`` is the
    helper the prune route leans on so it never runs with an empty
    active-container set. These tests verify the merge-and-degrade
    semantics so a session-manager regression surfaces here rather
    than in the form of a wiped live worktree."""

    def test_merges_session_manager_and_docker_ps(self):
        from unittest.mock import MagicMock, patch

        session_manager = MagicMock()
        session_manager.list_sessions.return_value = [
            {"container_id": "session-alpha"},
            {"container_id": "session-beta"},
            {"container_id": ""},  # empty string must not appear in the set
        ]

        with (
            patch.object(gateway, "get_session_manager", return_value=session_manager),
            patch.object(
                gateway,
                "get_active_docker_containers",
                return_value={"docker-gamma", "session-alpha"},
            ),
        ):
            result = gateway._collect_active_container_ids()

        assert result == {"session-alpha", "session-beta", "docker-gamma"}

    def test_degrades_when_session_manager_raises(self):
        from unittest.mock import patch

        with (
            patch.object(gateway, "get_session_manager", side_effect=RuntimeError("boom")),
            patch.object(gateway, "get_active_docker_containers", return_value={"docker-only"}),
        ):
            result = gateway._collect_active_container_ids()
        # session manager failure must not swallow the docker set.
        assert result == {"docker-only"}

    def test_degrades_when_docker_unavailable(self):
        """Running on k3s where dockerd is unreachable is a valid mode."""
        from unittest.mock import MagicMock, patch

        session_manager = MagicMock()
        session_manager.list_sessions.return_value = [{"container_id": "session-only"}]

        with (
            patch.object(gateway, "get_session_manager", return_value=session_manager),
            patch.object(
                gateway,
                "get_active_docker_containers",
                side_effect=RuntimeError("no docker"),
            ),
        ):
            result = gateway._collect_active_container_ids()
        assert result == {"session-only"}

    def test_both_sources_fail_returns_empty_set(self):
        """If both sources fail, the caller sees an empty set and the
        prune route will *still* run — the risk is covered by the
        mutation-test ``test_cleanup_still_runs_when_session_manager_unavailable``."""
        from unittest.mock import patch

        with (
            patch.object(gateway, "get_session_manager", side_effect=RuntimeError("sm fail")),
            patch.object(
                gateway, "get_active_docker_containers", side_effect=RuntimeError("docker fail")
            ),
        ):
            result = gateway._collect_active_container_ids()
        assert result == set()

    def test_per_agent_worktree_anchors_included(self):
        """Sessions with ``pipeline_id``+``agent_role`` must contribute the
        derived ``{pipeline_id}-{agent_role}`` and pipeline-level anchors
        so that ``cleanup_orphaned_worktrees`` does not wipe the per-agent
        worktrees on disk (which are named after ``agent_worktree_id``,
        not the session's ``container_id``).  Regression for #1874.
        """
        from unittest.mock import MagicMock, patch

        session_manager = MagicMock()
        session_manager.list_sessions.return_value = [
            {
                "container_id": "egg-agent-issue-1758-again-coder",
                "pipeline_id": "issue-1758-again",
                "agent_role": "coder",
            },
            {
                "container_id": "egg-agent-issue-1758-again-documenter",
                "pipeline_id": "issue-1758-again",
                "agent_role": "documenter",
            },
            # Interactive session with no pipeline context contributes only
            # its own container id.
            {"container_id": "solo-interactive", "pipeline_id": None, "agent_role": None},
        ]

        with (
            patch.object(gateway, "get_session_manager", return_value=session_manager),
            patch.object(gateway, "get_active_docker_containers", return_value=set()),
        ):
            result = gateway._collect_active_container_ids()

        assert "issue-1758-again-coder" in result
        assert "issue-1758-again-documenter" in result
        assert "issue-1758-again" in result
        assert "egg-agent-issue-1758-again-coder" in result
        assert "solo-interactive" in result


class TestDeriveWorktreeAnchorIds:
    """Unit tests for the helper that maps session metadata to the worktree
    dir names the orchestrator uses.  Keeping this standalone means future
    changes to the naming scheme fail here, loudly, before they can wipe a
    live pipeline's worktrees."""

    def test_derives_per_agent_and_pipeline_anchors(self):
        anchors = gateway._derive_worktree_anchor_ids(
            [
                {"pipeline_id": "issue-42", "agent_role": "coder"},
                {"pipeline_id": "issue-42", "agent_role": "tester"},
            ]
        )
        assert anchors == {"issue-42", "issue-42-coder", "issue-42-tester"}

    def test_pipeline_without_role_still_protects_pipeline_level(self):
        anchors = gateway._derive_worktree_anchor_ids(
            [{"pipeline_id": "issue-99", "agent_role": None}]
        )
        assert anchors == {"issue-99"}

    def test_session_without_pipeline_yields_no_anchors(self):
        anchors = gateway._derive_worktree_anchor_ids(
            [{"container_id": "adhoc", "pipeline_id": None, "agent_role": None}]
        )
        assert anchors == set()

    def test_missing_keys_handled(self):
        """Defensive against future session dicts that omit the field."""
        anchors = gateway._derive_worktree_anchor_ids([{"container_id": "adhoc"}])
        assert anchors == set()

    def test_slice_scoped_worktrees_protected_via_disk_scan(self, tmp_path):
        """Slice-scoped per-agent worktrees ({pipeline_id}-slice-N-{role})
        carry no slice context on the session, so the helper discovers
        them by scanning the worktree base for matching directories.
        Without this, the gateway's startup cleanup would wipe slice-
        scoped agent worktrees with unpushed commits — exactly the
        scenario salvage_agent_commits exists to recover from (#2463).
        """
        # Realistic slice-DAG layout: pipeline-state, overseer (per-role),
        # plus three slice-scoped agent worktrees.  A sibling worktree
        # whose suffix doesn't match the slice pattern must not leak in.
        (tmp_path / "issue-2261-v10").mkdir()
        (tmp_path / "issue-2261-v10-overseer").mkdir()
        (tmp_path / "issue-2261-v10-slice-2-coder").mkdir()
        (tmp_path / "issue-2261-v10-slice-2-tester").mkdir()
        (tmp_path / "issue-2261-v10-slice-5-coder").mkdir()
        (tmp_path / "issue-2261-v10-not-a-slice").mkdir()
        # Unrelated pipeline that must not be touched.
        (tmp_path / "issue-9999-slice-1-coder").mkdir()

        with patch.object(gateway, "WORKTREE_BASE_DIR", tmp_path):
            anchors = gateway._derive_worktree_anchor_ids(
                [
                    # Only the overseer session is alive — the slice
                    # agents may have died with the orch crash, but their
                    # worktrees on disk still hold unpushed commits.
                    {"pipeline_id": "issue-2261-v10", "agent_role": "overseer"},
                ]
            )

        assert anchors == {
            "issue-2261-v10",
            "issue-2261-v10-overseer",
            "issue-2261-v10-slice-2-coder",
            "issue-2261-v10-slice-2-tester",
            "issue-2261-v10-slice-5-coder",
        }

    def test_slice_scan_handles_missing_worktree_base(self, tmp_path):
        """Helper must not raise when ``WORKTREE_BASE_DIR`` is absent."""
        missing = tmp_path / "does-not-exist"
        with patch.object(gateway, "WORKTREE_BASE_DIR", missing):
            anchors = gateway._derive_worktree_anchor_ids(
                [{"pipeline_id": "p1", "agent_role": "coder"}]
            )
        assert anchors == {"p1", "p1-coder"}

    def test_slice_scan_skipped_when_no_live_pipelines(self, tmp_path):
        """No live pipeline_ids means no scan — sessions with only
        ``container_id`` fall through to the empty set.
        """
        (tmp_path / "issue-99-slice-1-coder").mkdir()
        with patch.object(gateway, "WORKTREE_BASE_DIR", tmp_path):
            anchors = gateway._derive_worktree_anchor_ids([{"container_id": "adhoc"}])
        assert anchors == set()

    def test_slice_scan_does_not_match_pipeline_id_prefix_collision(self, tmp_path):
        """Pipeline ID ``p1`` must not absorb slice worktrees of an
        unrelated pipeline ``p1-extra`` — the suffix regex anchors on
        ``-slice-N-{role}`` immediately after the pipeline id.
        """
        (tmp_path / "p1-extra-slice-2-coder").mkdir()
        with patch.object(gateway, "WORKTREE_BASE_DIR", tmp_path):
            anchors = gateway._derive_worktree_anchor_ids(
                [{"pipeline_id": "p1", "agent_role": "coder"}]
            )
        # p1's anchors are present, but p1-extra-slice-2-coder is not
        # because ``-extra-slice-2-coder`` does not match the slice
        # suffix regex anchored at the pipeline id boundary.
        assert "p1-extra-slice-2-coder" not in anchors


class TestContainerIdsFromSessions:
    """Unit tests for ``_container_ids_from_sessions`` — the shared helper
    that both ``_collect_active_container_ids`` and ``main()`` use to build
    the active set from session metadata."""

    def test_combines_container_ids_and_anchors(self):
        ids = gateway._container_ids_from_sessions(
            [
                {
                    "container_id": "egg-agent-issue-42-coder",
                    "pipeline_id": "issue-42",
                    "agent_role": "coder",
                },
                {"container_id": "solo", "pipeline_id": None, "agent_role": None},
            ]
        )
        assert ids == {
            "egg-agent-issue-42-coder",
            "issue-42",
            "issue-42-coder",
            "solo",
        }

    def test_empty_sessions_returns_empty(self):
        assert gateway._container_ids_from_sessions([]) == set()


class TestConcurrentSecondCallGets409:
    """End-to-end mutex verification: a real second request during an in-flight
    prune must be rejected. Uses a thread to hold the lock instead of a
    mocked acquire."""

    def test_live_second_caller_gets_409(self, client, launcher_auth_headers, fake_manager):
        release = threading.Event()
        acquired = threading.Event()

        def _hold_lock():
            with gateway._worktree_prune_lock:
                acquired.set()
                release.wait(timeout=5.0)

        t = threading.Thread(target=_hold_lock, daemon=True)
        t.start()
        acquired.wait(timeout=2.0)

        # Monkey-patch the lock to fail-fast so the test doesn't stall 60s.
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False

        with (
            patch.object(gateway, "_worktree_prune_lock", fake_lock),
            patch.object(gateway, "get_worktree_manager", return_value=fake_manager),
        ):
            response = client.post(
                "/api/v1/worktrees/prune",
                json={"dry_run": True},
                headers=launcher_auth_headers,
            )

        release.set()
        t.join(timeout=2.0)
        assert response.status_code == 409
