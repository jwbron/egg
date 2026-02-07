"""Tests for proxy_monitor.py."""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, mock_open, patch

import pytest
from proxy_monitor import (
    BlockedRequest,
    ProxyStats,
    log_allowed_request,
    log_blocked_request,
    parse_squid_json_log,
    watch_squid_log,
)


class TestBlockedRequest:
    """Tests for BlockedRequest namedtuple."""

    def test_creation(self):
        """BlockedRequest stores all fields."""
        req = BlockedRequest(
            timestamp=datetime(2024, 1, 1),
            client_ip="10.0.0.1",
            destination="example.com",
            method="GET",
            status_code=403,
            reason="not_allowed",
        )
        assert req.timestamp == datetime(2024, 1, 1)
        assert req.client_ip == "10.0.0.1"
        assert req.destination == "example.com"
        assert req.method == "GET"
        assert req.status_code == 403
        assert req.reason == "not_allowed"


class TestProxyStats:
    """Tests for ProxyStats class."""

    def test_init_defaults(self):
        """ProxyStats initializes with correct defaults."""
        stats = ProxyStats()
        assert stats.alert_threshold == 50
        assert stats.window_minutes == 5
        assert stats.allowed_count == 0
        assert stats.blocked_count == 0
        assert stats.blocked_requests == []
        assert dict(stats.blocked_by_destination) == {}

    def test_init_custom(self):
        """ProxyStats accepts custom threshold and window."""
        stats = ProxyStats(alert_threshold=10, window_minutes=2)
        assert stats.alert_threshold == 10
        assert stats.window_minutes == 2

    def test_record_allowed(self):
        """record_allowed increments count."""
        stats = ProxyStats()
        stats.record_allowed()
        stats.record_allowed()
        assert stats.allowed_count == 2

    def test_record_blocked(self):
        """record_blocked increments count and stores request."""
        stats = ProxyStats()
        req = BlockedRequest(
            timestamp=datetime.utcnow(),
            client_ip="10.0.0.1",
            destination="evil.com",
            method="GET",
            status_code=403,
            reason="blocked",
        )
        stats.record_blocked(req)
        assert stats.blocked_count == 1
        assert len(stats.blocked_requests) == 1
        assert stats.blocked_by_destination["evil.com"] == 1

    def test_record_blocked_multiple_same_destination(self):
        """record_blocked tracks per-destination counts."""
        stats = ProxyStats()
        for _ in range(3):
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination="evil.com",
                method="GET",
                status_code=403,
                reason="blocked",
            )
            stats.record_blocked(req)
        assert stats.blocked_by_destination["evil.com"] == 3

    def test_check_anomaly_below_threshold(self):
        """_check_anomaly returns False when below threshold."""
        stats = ProxyStats(alert_threshold=5)
        for _ in range(4):
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination="evil.com",
                method="GET",
                status_code=403,
                reason="blocked",
            )
            stats.blocked_requests.append(req)
        assert not stats._check_anomaly()

    def test_check_anomaly_at_threshold(self):
        """_check_anomaly returns True when at threshold."""
        stats = ProxyStats(alert_threshold=3, window_minutes=5)
        for _ in range(3):
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination="evil.com",
                method="GET",
                status_code=403,
                reason="blocked",
            )
            stats.blocked_requests.append(req)
        assert stats._check_anomaly()

    def test_check_anomaly_old_requests_ignored(self):
        """_check_anomaly ignores requests outside the time window."""
        stats = ProxyStats(alert_threshold=3, window_minutes=5)
        old_time = datetime.utcnow() - timedelta(minutes=10)
        for _ in range(5):
            req = BlockedRequest(
                timestamp=old_time,
                client_ip="10.0.0.1",
                destination="evil.com",
                method="GET",
                status_code=403,
                reason="blocked",
            )
            stats.blocked_requests.append(req)
        assert not stats._check_anomaly()

    @patch("proxy_monitor.logger")
    def test_send_alert(self, mock_logger):
        """_send_alert logs a warning with structured alert."""
        stats = ProxyStats()
        stats.blocked_by_destination["evil.com"] = 10
        stats._send_alert()
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args[0][0]
        assert "SECURITY ALERT" in call_args
        assert "high_block_rate" in call_args

    @patch("proxy_monitor.logger")
    def test_record_blocked_triggers_alert_at_threshold(self, mock_logger):
        """record_blocked triggers alert when threshold reached."""
        stats = ProxyStats(alert_threshold=2, window_minutes=5)
        for _ in range(2):
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination="evil.com",
                method="GET",
                status_code=403,
                reason="blocked",
            )
            stats.record_blocked(req)
        # Alert should have been sent
        mock_logger.warning.assert_called()

    def test_get_summary_empty(self):
        """get_summary returns zeros when no requests recorded."""
        stats = ProxyStats()
        summary = stats.get_summary()
        assert summary["allowed_requests"] == 0
        assert summary["blocked_requests"] == 0
        assert summary["block_rate"] == 0
        assert summary["top_blocked_destinations"] == {}

    def test_get_summary_with_data(self):
        """get_summary returns correct stats."""
        stats = ProxyStats()
        stats.allowed_count = 80
        stats.blocked_count = 20
        stats.blocked_by_destination["evil.com"] = 15
        stats.blocked_by_destination["bad.org"] = 5
        summary = stats.get_summary()
        assert summary["allowed_requests"] == 80
        assert summary["blocked_requests"] == 20
        assert summary["block_rate"] == 0.2
        assert "evil.com" in summary["top_blocked_destinations"]

    def test_get_summary_block_rate(self):
        """get_summary calculates block rate correctly."""
        stats = ProxyStats()
        stats.allowed_count = 3
        stats.blocked_count = 1
        summary = stats.get_summary()
        assert summary["block_rate"] == 0.25


class TestParseSquidJsonLog:
    """Tests for parse_squid_json_log."""

    def test_valid_json_dict(self):
        """Valid JSON dict is parsed correctly."""
        line = '{"status": 200, "url": "example.com"}'
        result = parse_squid_json_log(line)
        assert result == {"status": 200, "url": "example.com"}

    def test_valid_json_with_whitespace(self):
        """Valid JSON with trailing whitespace is parsed."""
        line = '{"status": 200}  \n'
        result = parse_squid_json_log(line)
        assert result == {"status": 200}

    def test_invalid_json(self):
        """Invalid JSON returns None."""
        result = parse_squid_json_log("not json at all")
        assert result is None

    def test_json_non_dict(self):
        """Non-dict JSON returns None."""
        result = parse_squid_json_log("[1, 2, 3]")
        assert result is None

    def test_empty_string(self):
        """Empty string returns None."""
        result = parse_squid_json_log("")
        assert result is None


class TestLogBlockedRequest:
    """Tests for log_blocked_request."""

    @patch("proxy_monitor.logger")
    def test_logs_blocked_request(self, mock_logger):
        """log_blocked_request logs structured entry."""
        log_blocked_request(
            client_ip="10.0.0.1",
            destination="evil.com",
            method="GET",
            reason="not allowed",
        )
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args[0][0]
        assert "BLOCKED" in call_args
        assert "evil.com" in call_args
        assert "10.0.0.1" in call_args

    @patch("proxy_monitor.logger")
    def test_logs_with_stats(self, mock_logger):
        """log_blocked_request updates stats when provided."""
        stats = ProxyStats()
        log_blocked_request(
            client_ip="10.0.0.1",
            destination="evil.com",
            method="POST",
            reason="blocked",
            stats=stats,
        )
        assert stats.blocked_count == 1
        assert stats.blocked_by_destination["evil.com"] == 1

    @patch("proxy_monitor.logger")
    def test_logs_without_stats(self, mock_logger):
        """log_blocked_request works without stats."""
        log_blocked_request(
            client_ip="10.0.0.1",
            destination="evil.com",
            method="GET",
            reason="blocked",
            stats=None,
        )
        mock_logger.warning.assert_called_once()


class TestLogAllowedRequest:
    """Tests for log_allowed_request."""

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "0"})
    @patch("proxy_monitor.logger")
    def test_not_logged_when_not_verbose(self, mock_logger):
        """log_allowed_request does not log when verbose is off."""
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="example.com",
            method="GET",
        )
        mock_logger.info.assert_not_called()

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "1"})
    @patch("proxy_monitor.logger")
    def test_logged_when_verbose(self, mock_logger):
        """log_allowed_request logs when verbose is on."""
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="example.com",
            method="GET",
        )
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        assert "ALLOWED" in call_args

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "0"})
    def test_updates_stats(self):
        """log_allowed_request updates stats when provided."""
        stats = ProxyStats()
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="example.com",
            method="GET",
            stats=stats,
        )
        assert stats.allowed_count == 1

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "0"})
    def test_no_stats(self):
        """log_allowed_request works without stats."""
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="example.com",
            method="GET",
            stats=None,
        )
        # Should not raise


class TestWatchSquidLog:
    """Tests for watch_squid_log."""

    def test_missing_log_file(self, tmp_path):
        """watch_squid_log returns when log file doesn't exist."""
        with patch("proxy_monitor.logger") as mock_logger:
            watch_squid_log(log_path=str(tmp_path / "nonexistent.log"))
            mock_logger.warning.assert_called_once()
            assert "not found" in mock_logger.warning.call_args[0][0]

    def test_processes_blocked_request(self, tmp_path):
        """watch_squid_log processes blocked requests from log."""
        log_file = tmp_path / "access.log"
        log_entry = json.dumps({"status": 403, "client_ip": "10.0.0.1", "url": "evil.com", "method": "GET"})
        log_file.write_text(log_entry + "\n")

        stats = ProxyStats()

        # Mock the infinite loop - read line then stop
        original_open = open
        call_count = 0

        def mock_readline(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Seek to beginning so we can read
                self.seek(0)
                return self._original_readline()
            # After reading line, raise to break the loop
            raise KeyboardInterrupt()

        # Use a simpler approach: patch the file to have content and break after processing
        with patch("proxy_monitor.logger"):
            with pytest.raises(KeyboardInterrupt):
                # Write content then interrupt after first read
                with patch("builtins.open", return_value=MagicMock()) as mock_f:
                    mock_f.return_value.__enter__ = MagicMock(return_value=mock_f.return_value)
                    mock_f.return_value.__exit__ = MagicMock(return_value=False)
                    mock_f.return_value.seek = MagicMock()
                    read_count = 0

                    def readline_side_effect():
                        nonlocal read_count
                        read_count += 1
                        if read_count == 1:
                            return log_entry + "\n"
                        raise KeyboardInterrupt()

                    mock_f.return_value.readline = readline_side_effect
                    watch_squid_log(log_path=str(log_file), stats=stats)

        assert stats.blocked_count == 1

    def test_processes_allowed_request(self, tmp_path):
        """watch_squid_log processes allowed requests from log."""
        log_file = tmp_path / "access.log"
        log_entry = json.dumps({"status": 200, "client_ip": "10.0.0.1", "url": "ok.com", "method": "GET"})
        log_file.write_text("")  # Create the file

        stats = ProxyStats()

        with patch("proxy_monitor.logger"):
            with pytest.raises(KeyboardInterrupt):
                with patch("builtins.open", return_value=MagicMock()) as mock_f:
                    mock_f.return_value.__enter__ = MagicMock(return_value=mock_f.return_value)
                    mock_f.return_value.__exit__ = MagicMock(return_value=False)
                    mock_f.return_value.seek = MagicMock()
                    read_count = 0

                    def readline_side_effect():
                        nonlocal read_count
                        read_count += 1
                        if read_count == 1:
                            return log_entry + "\n"
                        raise KeyboardInterrupt()

                    mock_f.return_value.readline = readline_side_effect
                    watch_squid_log(log_path=str(log_file), stats=stats)

        assert stats.allowed_count == 1

    def test_skips_invalid_log_lines(self, tmp_path):
        """watch_squid_log skips invalid JSON lines."""
        log_file = tmp_path / "access.log"
        log_file.write_text("")

        stats = ProxyStats()

        with patch("proxy_monitor.logger"):
            with pytest.raises(KeyboardInterrupt):
                with patch("builtins.open", return_value=MagicMock()) as mock_f:
                    mock_f.return_value.__enter__ = MagicMock(return_value=mock_f.return_value)
                    mock_f.return_value.__exit__ = MagicMock(return_value=False)
                    mock_f.return_value.seek = MagicMock()
                    read_count = 0

                    def readline_side_effect():
                        nonlocal read_count
                        read_count += 1
                        if read_count == 1:
                            return "not json\n"
                        raise KeyboardInterrupt()

                    mock_f.return_value.readline = readline_side_effect
                    watch_squid_log(log_path=str(log_file), stats=stats)

        assert stats.blocked_count == 0
        assert stats.allowed_count == 0
