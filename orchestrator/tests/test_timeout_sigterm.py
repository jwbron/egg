"""Tests for the 2-hour timeout / SIGTERM classification fix (#3665)."""

from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes_spawner._models import _EventJobStatusView


class TestFailedWithTimeoutSigterm:
    """Tests for _failed_with_timeout_sigterm in _EventJobStatusView."""

    def test_returns_true_for_exit_143(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        container = MagicMock()
        container.exit_code = 143
        spawner.k8s.list_containers.return_value = [container]

        assert view._failed_with_timeout_sigterm("key-1") is True

    def test_returns_false_for_exit_0(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        container = MagicMock()
        container.exit_code = 0
        spawner.k8s.list_containers.return_value = [container]

        assert view._failed_with_timeout_sigterm("key-1") is False

    def test_returns_false_for_exit_137(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        container = MagicMock()
        container.exit_code = 137
        spawner.k8s.list_containers.return_value = [container]

        assert view._failed_with_timeout_sigterm("key-1") is False

    def test_returns_false_on_list_error(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        spawner.k8s.list_containers.side_effect = RuntimeError("k8s down")

        assert view._failed_with_timeout_sigterm("key-1") is False

    def test_returns_false_for_none_exit_code(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        container = MagicMock()
        container.exit_code = None
        spawner.k8s.list_containers.return_value = [container]

        assert view._failed_with_timeout_sigterm("key-1") is False


class TestOutcomeForTimeout:
    """Tests that outcome_for maps SIGTERM (143) to LEGITIMATE."""

    def test_timeout_sigterm_maps_to_legitimate(self) -> None:
        from models import ContainerStatus

        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        job = MagicMock()
        job.status = ContainerStatus.FAILED
        spawner.k8s.list_jobs.return_value = [job]

        container = MagicMock()
        container.exit_code = 143
        spawner.k8s.list_containers.return_value = [container]

        outcome = view.outcome_for("key-1")
        assert outcome == "legitimate"

    def test_timeout_sigterm_not_counted_as_abnormal(self) -> None:
        """A SIGTERM kill should not increment the fail-streak budget."""
        from models import ContainerStatus

        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        job = MagicMock()
        job.status = ContainerStatus.FAILED
        spawner.k8s.list_jobs.return_value = [job]

        container = MagicMock()
        container.exit_code = 143
        spawner.k8s.list_containers.return_value = [container]

        outcome = view.outcome_for("key-1")
        # Must NOT be "abnormal" — that would trigger record_abort and
        # increment the fail-streak budget (#3665).
        assert outcome != "abnormal"

    def test_non_timeout_failure_still_abnormal(self) -> None:
        """A real crash (exit 137) should still be abnormal."""
        from models import ContainerStatus

        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        job = MagicMock()
        job.status = ContainerStatus.FAILED
        spawner.k8s.list_jobs.return_value = [job]

        container = MagicMock()
        container.exit_code = 137
        spawner.k8s.list_containers.return_value = [container]

        outcome = view.outcome_for("key-1")
        assert outcome == "abnormal"


class TestExitDetailForTimeout:
    """Tests that exit_detail_for annotates SIGTERM."""

    def test_exit_detail_annotates_sigterm(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        container = MagicMock()
        container.exit_code = 143
        spawner.k8s.list_containers.return_value = [container]

        detail = view.exit_detail_for("key-1")
        assert detail is not None
        assert "143" in detail
        assert "SIGTERM" in detail
        assert "timeout" in detail.lower() or "teardown" in detail.lower()

    def test_exit_detail_no_annotation_for_normal_exit(self) -> None:
        spawner = MagicMock()
        view = _EventJobStatusView(spawner)

        container = MagicMock()
        container.exit_code = 0
        spawner.k8s.list_containers.return_value = [container]

        detail = view.exit_detail_for("key-1")
        assert detail is not None
        assert "SIGTERM" not in detail
