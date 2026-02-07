"""Tests for sandbox/egg_lib/container_logging.py - Container log persistence."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.container_logging import (
    extract_task_id_from_command,
    extract_thread_ts_from_task_file,
    generate_container_id,
    get_docker_log_config,
    save_container_logs,
    update_log_index,
)


class TestGenerateContainerId:
    """Tests for generate_container_id."""

    def test_format(self):
        """Container ID has correct prefix format."""
        cid = generate_container_id()
        assert cid.startswith("egg-")
        parts = cid.split("-")
        assert len(parts) >= 3  # egg-YYYYMMDD-HHMMSS-PID

    def test_unique(self):
        """Each call returns a unique ID (includes PID)."""
        id1 = generate_container_id()
        id2 = generate_container_id()
        # Same PID and timestamp within the same second
        assert isinstance(id1, str)
        assert isinstance(id2, str)

    def test_contains_pid(self):
        """Container ID contains process ID."""
        cid = generate_container_id()
        assert str(os.getpid()) in cid


class TestGetDockerLogConfig:
    """Tests for get_docker_log_config."""

    def test_basic_config(self, tmp_path):
        """Returns basic logging config arguments."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("egg-test-123")
            assert "--log-driver" in args
            assert "json-file" in args
            assert "--log-opt" in args
            assert "max-size=10m" in args
            assert "--label" in args
            assert "egg.container_id=egg-test-123" in args

    def test_with_task_id(self, tmp_path):
        """Includes task ID label when provided."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("egg-test-123", task_id="task-20240101-120000")
            assert "egg.task_id=task-20240101-120000" in args

    def test_without_task_id(self, tmp_path):
        """Does not include task ID label when not provided."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("egg-test-123")
            task_labels = [a for a in args if "egg.task_id" in a]
            assert len(task_labels) == 0

    def test_creates_log_dir(self, tmp_path):
        """Creates container logs directory if it doesn't exist."""
        log_dir = tmp_path / "logs"
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", log_dir):
            get_docker_log_config("egg-test-123")
            assert log_dir.exists()


class TestExtractTaskIdFromCommand:
    """Tests for extract_task_id_from_command."""

    def test_extracts_from_path(self):
        """Extracts task ID from file path argument."""
        cmd = ["python", "incoming-processor.py", "/path/to/task-20251129-222239.md"]
        assert extract_task_id_from_command(cmd) == "task-20251129-222239"

    def test_extracts_bare_task_id(self):
        """Extracts bare task ID from arguments."""
        cmd = ["run", "task-20240101-120000"]
        assert extract_task_id_from_command(cmd) == "task-20240101-120000"

    def test_returns_none_when_not_found(self):
        """Returns None when no task ID in command."""
        cmd = ["python", "script.py", "--flag"]
        assert extract_task_id_from_command(cmd) is None

    def test_empty_command(self):
        """Returns None for empty command list."""
        assert extract_task_id_from_command([]) is None


class TestExtractThreadTsFromTaskFile:
    """Tests for extract_thread_ts_from_task_file."""

    def test_extracts_thread_ts(self, tmp_path):
        """Extracts thread_ts from YAML frontmatter."""
        task_file = tmp_path / "task.md"
        task_file.write_text(
            '---\ntask_id: "task-20251129-222239"\nthread_ts: "1764483758.159619"\n---\nBody\n'
        )
        assert extract_thread_ts_from_task_file(str(task_file)) == "1764483758.159619"

    def test_extracts_unquoted_thread_ts(self, tmp_path):
        """Extracts thread_ts without quotes."""
        task_file = tmp_path / "task.md"
        task_file.write_text("---\nthread_ts: 1764483758.159619\n---\n")
        assert extract_thread_ts_from_task_file(str(task_file)) == "1764483758.159619"

    def test_returns_none_when_not_found(self, tmp_path):
        """Returns None when thread_ts not in file."""
        task_file = tmp_path / "task.md"
        task_file.write_text("---\ntask_id: test\n---\n")
        assert extract_thread_ts_from_task_file(str(task_file)) is None

    def test_returns_none_for_nonexistent_file(self):
        """Returns None for nonexistent file."""
        assert extract_thread_ts_from_task_file("/nonexistent/path") is None

    def test_returns_none_on_error(self, tmp_path):
        """Returns None on read errors."""
        # Create a directory instead of file
        dir_path = tmp_path / "not_a_file"
        dir_path.mkdir()
        assert extract_thread_ts_from_task_file(str(dir_path)) is None


class TestUpdateLogIndex:
    """Tests for update_log_index."""

    def test_creates_new_index(self, tmp_path):
        """Creates new log index file."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("egg-123", task_id="task-001", thread_ts="123.456")
            index_file = tmp_path / "log-index.json"
            assert index_file.exists()
            data = json.loads(index_file.read_text())
            assert data["task_to_container"]["task-001"] == "egg-123"
            assert data["thread_to_task"]["123.456"] == "task-001"
            assert len(data["entries"]) == 1

    def test_appends_to_existing_index(self, tmp_path):
        """Appends to existing log index."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("egg-123", task_id="task-001")
            update_log_index("egg-456", task_id="task-002")
            data = json.loads((tmp_path / "log-index.json").read_text())
            assert len(data["entries"]) == 2
            assert data["task_to_container"]["task-001"] == "egg-123"
            assert data["task_to_container"]["task-002"] == "egg-456"

    def test_handles_no_task_id(self, tmp_path):
        """Works without task_id."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("egg-123")
            data = json.loads((tmp_path / "log-index.json").read_text())
            assert len(data["entries"]) == 1
            assert data["entries"][0]["task_id"] is None

    def test_handles_log_file(self, tmp_path):
        """Records log file path."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("egg-123", log_file="/path/to/log.log")
            data = json.loads((tmp_path / "log-index.json").read_text())
            assert data["entries"][0]["log_file"] == "/path/to/log.log"

    def test_truncates_old_entries(self, tmp_path):
        """Truncates to last 1000 entries."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            # Pre-populate with 1000 entries
            index = {"task_to_container": {}, "thread_to_task": {}, "entries": []}
            for i in range(1001):
                index["entries"].append({"container_id": f"egg-{i}", "task_id": None, "thread_ts": None, "log_file": None, "timestamp": "2024-01-01"})
            (tmp_path / "log-index.json").write_text(json.dumps(index))

            update_log_index("egg-new")
            data = json.loads((tmp_path / "log-index.json").read_text())
            assert len(data["entries"]) == 1000


class TestSaveContainerLogs:
    """Tests for save_container_logs."""

    def test_saves_logs_successfully(self, tmp_path):
        """Saves container logs to file."""
        mock_result = MagicMock(
            returncode=0,
            stdout="container output\n",
            stderr="container errors\n",
        )
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", return_value=mock_result):
                with patch("egg_lib.container_logging.update_log_index"):
                    result = save_container_logs("egg-test-123")
                    assert result is not None
                    assert result.exists()
                    content = result.read_text()
                    assert "egg-test-123" in content
                    assert "STDOUT" in content
                    assert "container output" in content
                    assert "STDERR" in content

    def test_creates_task_symlink(self, tmp_path):
        """Creates symlink from task_id to container log."""
        mock_result = MagicMock(returncode=0, stdout="output\n", stderr="")
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", return_value=mock_result):
                with patch("egg_lib.container_logging.update_log_index"):
                    save_container_logs("egg-test-123", task_id="task-001")
                    symlink = tmp_path / "task-001.log"
                    assert symlink.is_symlink()

    def test_returns_none_on_timeout(self, tmp_path):
        """Returns None when docker logs times out."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
                result = save_container_logs("egg-test-123")
                assert result is None

    def test_returns_none_on_file_not_found(self, tmp_path):
        """Returns None when docker is not found."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", side_effect=FileNotFoundError):
                result = save_container_logs("egg-test-123")
                assert result is None

    def test_returns_none_on_generic_error(self, tmp_path):
        """Returns None on generic error."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", side_effect=Exception("boom")):
                result = save_container_logs("egg-test-123")
                assert result is None

    def test_truncates_large_logs(self, tmp_path):
        """Truncates logs exceeding 100MB."""
        large_output = "x" * (60 * 1024 * 1024)  # 60MB stdout
        large_stderr = "e" * (60 * 1024 * 1024)  # 60MB stderr
        mock_result = MagicMock(returncode=0, stdout=large_output, stderr=large_stderr)
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", return_value=mock_result):
                with patch("egg_lib.container_logging.update_log_index"):
                    result = save_container_logs("egg-test-123")
                    assert result is not None
                    content = result.read_text()
                    assert "truncated" in content

    def test_includes_thread_ts_header(self, tmp_path):
        """Includes thread_ts in log header when provided."""
        mock_result = MagicMock(returncode=0, stdout="output\n", stderr="")
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            with patch("egg_lib.container_logging.subprocess.run", return_value=mock_result):
                with patch("egg_lib.container_logging.update_log_index"):
                    result = save_container_logs("egg-test-123", thread_ts="123.456")
                    content = result.read_text()
                    assert "123.456" in content
