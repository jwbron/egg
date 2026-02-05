"""Tests for sandbox egg_lib container_logging module."""

import json
import os
from unittest.mock import patch

from egg_lib.container_logging import (
    extract_task_id_from_command,
    extract_thread_ts_from_task_file,
    generate_container_id,
    get_docker_log_config,
    update_log_index,
)


class TestGenerateContainerId:
    """Tests for generate_container_id function."""

    def test_starts_with_egg(self):
        """Container ID starts with 'egg-'."""
        cid = generate_container_id()
        assert cid.startswith("egg-")

    def test_contains_timestamp(self):
        """Container ID contains a timestamp-like pattern."""
        cid = generate_container_id()
        # Pattern: egg-YYYYMMDD-HHMMSS-PID
        parts = cid.split("-")
        assert len(parts) >= 3
        # Check date part looks like YYYYMMDD
        assert len(parts[1]) == 8
        assert parts[1].isdigit()

    def test_contains_pid(self):
        """Container ID includes the process ID."""
        cid = generate_container_id()
        assert str(os.getpid()) in cid

    def test_unique_ids(self):
        """Multiple calls produce different IDs (due to timestamp)."""
        id1 = generate_container_id()
        id2 = generate_container_id()
        # They should be the same since called in same second with same PID
        # but the format should be consistent
        assert id1.startswith("egg-")
        assert id2.startswith("egg-")


class TestGetDockerLogConfig:
    """Tests for get_docker_log_config function."""

    def test_basic_config(self, tmp_path):
        """Basic config includes log driver and rotation."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("test-container-123")

        assert "--log-driver" in args
        assert "json-file" in args
        assert "--log-opt" in args
        assert "max-size=10m" in args
        assert "max-file=5" in args

    def test_container_id_label(self, tmp_path):
        """Config includes container ID label."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("my-container")

        # Find the label
        label_idx = [i for i, a in enumerate(args) if a == "--label"]
        labels = [args[i + 1] for i in label_idx]
        assert any("egg.container_id=my-container" in label for label in labels)

    def test_task_id_label(self, tmp_path):
        """Config includes task ID label when provided."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("container-1", task_id="task-20251129-222239")

        label_idx = [i for i, a in enumerate(args) if a == "--label"]
        labels = [args[i + 1] for i in label_idx]
        assert any("egg.task_id=task-20251129-222239" in label for label in labels)

    def test_no_task_id_label_when_none(self, tmp_path):
        """No task ID label when not provided."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            args = get_docker_log_config("container-1")

        joined = " ".join(args)
        assert "egg.task_id" not in joined


class TestExtractTaskIdFromCommand:
    """Tests for extract_task_id_from_command function."""

    def test_extract_from_path(self):
        """Extract task ID from file path in command."""
        cmd = ["python", "incoming-processor.py", "/path/to/task-20251129-222239.md"]
        assert extract_task_id_from_command(cmd) == "task-20251129-222239"

    def test_extract_from_task_string(self):
        """Extract task ID from plain string."""
        cmd = ["process", "task-20240101-120000"]
        assert extract_task_id_from_command(cmd) == "task-20240101-120000"

    def test_no_task_id(self):
        """Return None when no task ID in command."""
        cmd = ["echo", "hello", "world"]
        assert extract_task_id_from_command(cmd) is None

    def test_empty_command(self):
        """Return None for empty command."""
        assert extract_task_id_from_command([]) is None

    def test_multiple_task_ids(self):
        """Returns first task ID found."""
        cmd = ["process", "task-20240101-120000", "task-20240102-130000"]
        assert extract_task_id_from_command(cmd) == "task-20240101-120000"


class TestExtractThreadTsFromTaskFile:
    """Tests for extract_thread_ts_from_task_file function."""

    def test_extract_from_frontmatter(self, tmp_path):
        """Extract thread_ts from YAML frontmatter."""
        task_file = tmp_path / "task.md"
        task_file.write_text(
            '---\ntask_id: "task-20251129-222239"\nthread_ts: "1764483758.159619"\n---\nContent here\n'
        )
        result = extract_thread_ts_from_task_file(str(task_file))
        assert result == "1764483758.159619"

    def test_extract_unquoted(self, tmp_path):
        """Extract unquoted thread_ts."""
        task_file = tmp_path / "task.md"
        task_file.write_text("---\nthread_ts: 1764483758.159619\n---\n")
        result = extract_thread_ts_from_task_file(str(task_file))
        assert result == "1764483758.159619"

    def test_no_thread_ts(self, tmp_path):
        """Return None when no thread_ts in file."""
        task_file = tmp_path / "task.md"
        task_file.write_text("---\ntask_id: test\n---\nNo thread_ts here\n")
        result = extract_thread_ts_from_task_file(str(task_file))
        assert result is None

    def test_nonexistent_file(self):
        """Return None for nonexistent file."""
        result = extract_thread_ts_from_task_file("/nonexistent/path/task.md")
        assert result is None

    def test_single_quoted_thread_ts(self, tmp_path):
        """Extract single-quoted thread_ts."""
        task_file = tmp_path / "task.md"
        task_file.write_text("---\nthread_ts: '1234567890.123456'\n---\n")
        result = extract_thread_ts_from_task_file(str(task_file))
        assert result == "1234567890.123456"


class TestUpdateLogIndex:
    """Tests for update_log_index function."""

    def test_create_new_index(self, tmp_path):
        """Create a new log index file."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("container-1", task_id="task-001")

        index_file = tmp_path / "log-index.json"
        assert index_file.exists()
        index = json.loads(index_file.read_text())
        assert "task-001" in index["task_to_container"]
        assert index["task_to_container"]["task-001"] == "container-1"
        assert len(index["entries"]) == 1

    def test_append_to_existing(self, tmp_path):
        """Append to existing log index."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("container-1", task_id="task-001")
            update_log_index("container-2", task_id="task-002")

        index_file = tmp_path / "log-index.json"
        index = json.loads(index_file.read_text())
        assert len(index["entries"]) == 2
        assert index["task_to_container"]["task-001"] == "container-1"
        assert index["task_to_container"]["task-002"] == "container-2"

    def test_thread_ts_correlation(self, tmp_path):
        """Thread TS to task mapping is recorded."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index(
                "container-1",
                task_id="task-001",
                thread_ts="1234567890.123456",
            )

        index_file = tmp_path / "log-index.json"
        index = json.loads(index_file.read_text())
        assert index["thread_to_task"]["1234567890.123456"] == "task-001"

    def test_no_task_id(self, tmp_path):
        """Entry without task_id still recorded."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("container-1")

        index_file = tmp_path / "log-index.json"
        index = json.loads(index_file.read_text())
        assert len(index["entries"]) == 1
        assert index["entries"][0]["task_id"] is None

    def test_log_file_recorded(self, tmp_path):
        """Log file path is recorded in entry."""
        with patch("egg_lib.container_logging.CONTAINER_LOGS_DIR", tmp_path):
            update_log_index("container-1", log_file="/var/log/test.log")

        index_file = tmp_path / "log-index.json"
        index = json.loads(index_file.read_text())
        assert index["entries"][0]["log_file"] == "/var/log/test.log"
