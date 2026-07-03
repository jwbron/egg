"""Regression tests for boot-time driver relaunch of RUNNING pipelines (#3469).

After an orchestrator restart, a pipeline in status=RUNNING is left
permanently driverless: startup reconciliation rebuilds consensus state from
the message store but never relaunches the pipeline's ``_run_pipeline``
driver thread / event loop.  ``restart_agent`` delegates the respawn into
the missing loop (and returns success), while ``start_pipeline`` rejects
status=RUNNING with a 409 — so no recovery verb works.  The startup sweep
``relaunch_driverless_running_pipelines`` closes the hole by relaunching a
fresh driver thread for every RUNNING pipeline with no live driver.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from models import Pipeline, PipelineStatus  # noqa: E402


def _pipeline(pipeline_id="issue-3469", status=PipelineStatus.RUNNING, run_epoch=None):
    p = Pipeline(id=pipeline_id, issue_number=3469, repo="owner/repo", branch="egg/test")
    p.status = status
    p.run_epoch = run_epoch
    return p


def _store(pipelines, repo_path=Path("/tmp/repo")):
    """A StateStore double whose ``get_active_pipelines`` returns ``pipelines``.

    The sweep iterates ``get_active_pipelines()`` (which already filters out
    terminal records at the store layer), so the double serves the active
    (non-terminal) pipelines directly rather than an id→record map.
    """
    store = MagicMock()
    store.repo_path = repo_path
    store.get_active_pipelines.return_value = list(pipelines)
    return store


class TestRelaunchDriverlessRunningPipelines:
    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_relaunches_running_pipeline_with_no_driver(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        epoch = datetime(2026, 7, 3, 6, 0, 0, tzinfo=UTC)
        store = _store([_pipeline(run_epoch=epoch)])

        assert relaunch_driverless_running_pipelines(store) == 1
        mock_spawn.assert_called_once_with("issue-3469", store.repo_path, epoch)

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_falls_back_to_created_at_when_run_epoch_unset(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        pipeline = _pipeline(run_epoch=None)
        store = _store([pipeline])

        assert relaunch_driverless_running_pipelines(store) == 1
        mock_spawn.assert_called_once_with("issue-3469", store.repo_path, pipeline.created_at)

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_skips_non_running_pipelines(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        # ``get_active_pipelines`` filters terminal records at the store layer,
        # so the sweep only ever sees non-terminal statuses. The status guard
        # must still skip the non-RUNNING actives — PENDING (not yet driving)
        # and AWAITING_HUMAN (#3233's territory: revived on decision
        # resolution, never at boot).
        store = _store(
            [
                _pipeline("issue-1", status=PipelineStatus.PENDING),
                _pipeline("issue-2", status=PipelineStatus.AWAITING_HUMAN),
            ]
        )

        assert relaunch_driverless_running_pipelines(store) == 0
        mock_spawn.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=True)
    def test_skips_pipeline_with_live_driver(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        store = _store([_pipeline()])

        assert relaunch_driverless_running_pipelines(store) == 0
        mock_spawn.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver")
    def test_per_pipeline_failure_does_not_abort_sweep(self, mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        epoch = datetime(2026, 7, 3, 6, 0, 0, tzinfo=UTC)

        # A per-pipeline probe failure (here: has_live_pipeline_driver raising
        # for one record) must be isolated so the rest of the sweep proceeds.
        def _driver(pid):
            if pid == "issue-broken":
                raise RuntimeError("driver probe wedged")
            return False

        mock_has_driver.side_effect = _driver
        store = _store(
            [
                _pipeline("issue-broken", run_epoch=epoch),
                _pipeline("issue-ok", run_epoch=epoch),
            ]
        )

        assert relaunch_driverless_running_pipelines(store) == 1
        mock_spawn.assert_called_once_with("issue-ok", store.repo_path, epoch)

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    def test_get_active_pipelines_failure_returns_zero(self, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        store = MagicMock()
        store.get_active_pipelines.side_effect = RuntimeError("git wedged")

        assert relaunch_driverless_running_pipelines(store) == 0
        mock_spawn.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_spawn_failure_is_isolated_and_not_counted(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        mock_spawn.side_effect = [RuntimeError("thread limit"), None]
        store = _store(
            [
                _pipeline("issue-a"),
                _pipeline("issue-b"),
            ]
        )

        assert relaunch_driverless_running_pipelines(store) == 1
        assert mock_spawn.call_count == 2
