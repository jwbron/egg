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
    """A StateStore double serving the given ``{id: Pipeline-or-Exception}`` map."""
    store = MagicMock()
    store.repo_path = repo_path
    store.list_pipelines.return_value = list(pipelines)

    def _load(pid):
        value = pipelines[pid]
        if isinstance(value, Exception):
            raise value
        return value

    store.load_pipeline.side_effect = _load
    return store


class TestRelaunchDriverlessRunningPipelines:
    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_relaunches_running_pipeline_with_no_driver(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        epoch = datetime(2026, 7, 3, 6, 0, 0, tzinfo=UTC)
        store = _store({"issue-3469": _pipeline(run_epoch=epoch)})

        assert relaunch_driverless_running_pipelines(store) == 1
        mock_spawn.assert_called_once_with("issue-3469", store.repo_path, epoch)

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_falls_back_to_created_at_when_run_epoch_unset(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        pipeline = _pipeline(run_epoch=None)
        store = _store({"issue-3469": pipeline})

        assert relaunch_driverless_running_pipelines(store) == 1
        mock_spawn.assert_called_once_with("issue-3469", store.repo_path, pipeline.created_at)

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_skips_non_running_pipelines(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        store = _store(
            {
                "issue-1": _pipeline("issue-1", status=PipelineStatus.FAILED),
                "issue-2": _pipeline("issue-2", status=PipelineStatus.COMPLETE),
                "issue-3": _pipeline("issue-3", status=PipelineStatus.CANCELLED),
                # AWAITING_HUMAN is #3233's territory: revived on decision
                # resolution, never at boot.
                "issue-4": _pipeline("issue-4", status=PipelineStatus.AWAITING_HUMAN),
            }
        )

        assert relaunch_driverless_running_pipelines(store) == 0
        mock_spawn.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=True)
    def test_skips_pipeline_with_live_driver(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        store = _store({"issue-3469": _pipeline()})

        assert relaunch_driverless_running_pipelines(store) == 0
        mock_spawn.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_per_pipeline_failure_does_not_abort_sweep(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        epoch = datetime(2026, 7, 3, 6, 0, 0, tzinfo=UTC)
        store = _store(
            {
                "issue-broken": RuntimeError("corrupt state"),
                "issue-ok": _pipeline("issue-ok", run_epoch=epoch),
            }
        )

        assert relaunch_driverless_running_pipelines(store) == 1
        mock_spawn.assert_called_once_with("issue-ok", store.repo_path, epoch)

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    def test_list_pipelines_failure_returns_zero(self, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        store = MagicMock()
        store.list_pipelines.side_effect = RuntimeError("git wedged")

        assert relaunch_driverless_running_pipelines(store) == 0
        mock_spawn.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    def test_spawn_failure_is_isolated_and_not_counted(self, _mock_has_driver, mock_spawn):
        from routes.pipelines import relaunch_driverless_running_pipelines

        mock_spawn.side_effect = [RuntimeError("thread limit"), None]
        store = _store(
            {
                "issue-a": _pipeline("issue-a"),
                "issue-b": _pipeline("issue-b"),
            }
        )

        assert relaunch_driverless_running_pipelines(store) == 1
        assert mock_spawn.call_count == 2
