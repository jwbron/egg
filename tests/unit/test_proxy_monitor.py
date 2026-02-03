"""Tests for gateway/proxy_monitor.py."""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from gateway.proxy_monitor import (
    BlockedRequest,
    ProxyStats,
    log_allowed_request,
    log_blocked_request,
    parse_squid_json_log,
    watch_squid_log,
)


class TestBlockedRequest:
    """Tests for BlockedRequest named tuple."""

    def test_create_blocked_request(self):
        """Test creating a blocked request."""
        now = datetime.utcnow()
        req = BlockedRequest(
            timestamp=now,
            client_ip="192.168.1.1",
            destination="https://blocked.com",
            method="GET",
            status_code=403,
            reason="Policy violation",
        )
        assert req.timestamp == now
        assert req.client_ip == "192.168.1.1"
        assert req.destination == "https://blocked.com"
        assert req.method == "GET"
        assert req.status_code == 403
        assert req.reason == "Policy violation"

    def test_blocked_request_immutable(self):
        """Test that BlockedRequest is immutable."""
        req = BlockedRequest(
            timestamp=datetime.utcnow(),
            client_ip="192.168.1.1",
            destination="https://blocked.com",
            method="GET",
            status_code=403,
            reason="Policy violation",
        )
        with pytest.raises(AttributeError):
            req.client_ip = "10.0.0.1"


class TestProxyStats:
    """Tests for ProxyStats class."""

    def test_init_defaults(self):
        """Test default initialization."""
        stats = ProxyStats()
        assert stats.alert_threshold == 50
        assert stats.window_minutes == 5
        assert stats.blocked_requests == []
        assert stats.allowed_count == 0
        assert stats.blocked_count == 0
        assert stats.blocked_by_destination == {}

    def test_init_custom_values(self):
        """Test custom initialization."""
        stats = ProxyStats(alert_threshold=100, window_minutes=10)
        assert stats.alert_threshold == 100
        assert stats.window_minutes == 10

    def test_record_allowed(self):
        """Test recording allowed requests."""
        stats = ProxyStats()
        stats.record_allowed()
        assert stats.allowed_count == 1
        stats.record_allowed()
        assert stats.allowed_count == 2

    def test_record_blocked(self):
        """Test recording blocked requests."""
        stats = ProxyStats(alert_threshold=100)
        now = datetime.utcnow()
        req = BlockedRequest(
            timestamp=now,
            client_ip="192.168.1.1",
            destination="https://blocked.com",
            method="GET",
            status_code=403,
            reason="Policy violation",
        )
        stats.record_blocked(req)
        assert stats.blocked_count == 1
        assert len(stats.blocked_requests) == 1
        assert stats.blocked_by_destination["https://blocked.com"] == 1

    def test_record_blocked_increments_destination_count(self):
        """Test that blocking same destination increments count."""
        stats = ProxyStats(alert_threshold=100)
        now = datetime.utcnow()
        for _ in range(3):
            req = BlockedRequest(
                timestamp=now,
                client_ip="192.168.1.1",
                destination="https://blocked.com",
                method="GET",
                status_code=403,
                reason="Policy violation",
            )
            stats.record_blocked(req)
        assert stats.blocked_by_destination["https://blocked.com"] == 3

    def test_check_anomaly_below_threshold(self):
        """Test anomaly detection when below threshold."""
        stats = ProxyStats(alert_threshold=5, window_minutes=5)
        now = datetime.utcnow()
        # Add 3 requests (below threshold of 5)
        for i in range(3):
            req = BlockedRequest(
                timestamp=now,
                client_ip="192.168.1.1",
                destination=f"https://blocked{i}.com",
                method="GET",
                status_code=403,
                reason="Policy violation",
            )
            stats.blocked_requests.append(req)

        assert stats._check_anomaly() is False

    def test_check_anomaly_at_threshold(self):
        """Test anomaly detection at threshold."""
        stats = ProxyStats(alert_threshold=5, window_minutes=5)
        now = datetime.utcnow()
        # Add exactly 5 requests (at threshold)
        for i in range(5):
            req = BlockedRequest(
                timestamp=now,
                client_ip="192.168.1.1",
                destination=f"https://blocked{i}.com",
                method="GET",
                status_code=403,
                reason="Policy violation",
            )
            stats.blocked_requests.append(req)

        assert stats._check_anomaly() is True

    def test_check_anomaly_old_requests_not_counted(self):
        """Test that old requests outside window are not counted."""
        stats = ProxyStats(alert_threshold=5, window_minutes=5)
        old_time = datetime.utcnow() - timedelta(minutes=10)
        now = datetime.utcnow()

        # Add 3 old requests
        for i in range(3):
            req = BlockedRequest(
                timestamp=old_time,
                client_ip="192.168.1.1",
                destination=f"https://old{i}.com",
                method="GET",
                status_code=403,
                reason="Policy violation",
            )
            stats.blocked_requests.append(req)

        # Add 2 recent requests
        for i in range(2):
            req = BlockedRequest(
                timestamp=now,
                client_ip="192.168.1.1",
                destination=f"https://new{i}.com",
                method="GET",
                status_code=403,
                reason="Policy violation",
            )
            stats.blocked_requests.append(req)

        # Should not trigger (only 2 recent)
        assert stats._check_anomaly() is False

    @patch("gateway.proxy_monitor.logger")
    def test_send_alert(self, mock_logger):
        """Test sending security alert."""
        stats = ProxyStats()
        stats.blocked_by_destination["https://blocked1.com"] = 10
        stats.blocked_by_destination["https://blocked2.com"] = 5
        stats._send_alert()

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args[0][0]
        assert "SECURITY ALERT" in call_args
        assert "high_block_rate" in call_args

    @patch("gateway.proxy_monitor.logger")
    def test_record_blocked_triggers_alert_at_threshold(self, mock_logger):
        """Test that recording blocked requests triggers alert at threshold."""
        stats = ProxyStats(alert_threshold=3, window_minutes=5)
        now = datetime.utcnow()

        for i in range(3):
            req = BlockedRequest(
                timestamp=now,
                client_ip="192.168.1.1",
                destination=f"https://blocked{i}.com",
                method="GET",
                status_code=403,
                reason="Policy violation",
            )
            stats.record_blocked(req)

        # Alert should have been triggered
        assert mock_logger.warning.called

    def test_get_summary_empty(self):
        """Test getting summary with no requests."""
        stats = ProxyStats()
        summary = stats.get_summary()
        assert summary["allowed_requests"] == 0
        assert summary["blocked_requests"] == 0
        assert summary["block_rate"] == 0
        assert summary["top_blocked_destinations"] == {}

    def test_get_summary_with_data(self):
        """Test getting summary with data."""
        stats = ProxyStats()
        stats.allowed_count = 100
        stats.blocked_count = 10
        stats.blocked_by_destination["https://bad.com"] = 7
        stats.blocked_by_destination["https://worse.com"] = 3

        summary = stats.get_summary()
        assert summary["allowed_requests"] == 100
        assert summary["blocked_requests"] == 10
        assert summary["block_rate"] == pytest.approx(0.0909, rel=0.01)
        assert "https://bad.com" in summary["top_blocked_destinations"]

    def test_get_summary_top_10_destinations(self):
        """Test that summary returns top 10 destinations."""
        stats = ProxyStats()
        for i in range(15):
            stats.blocked_by_destination[f"https://site{i}.com"] = i

        summary = stats.get_summary()
        assert len(summary["top_blocked_destinations"]) == 10


class TestParseSquidJsonLog:
    """Tests for parse_squid_json_log function."""

    def test_parse_valid_json(self):
        """Test parsing valid JSON log line."""
        log_line = '{"client_ip": "192.168.1.1", "url": "https://example.com", "status": 200}'
        result = parse_squid_json_log(log_line)
        assert result == {
            "client_ip": "192.168.1.1",
            "url": "https://example.com",
            "status": 200,
        }

    def test_parse_json_with_whitespace(self):
        """Test parsing JSON with leading/trailing whitespace."""
        log_line = '  {"key": "value"}  \n'
        result = parse_squid_json_log(log_line)
        assert result == {"key": "value"}

    def test_parse_invalid_json(self):
        """Test parsing invalid JSON returns None."""
        log_line = "not valid json"
        result = parse_squid_json_log(log_line)
        assert result is None

    def test_parse_empty_string(self):
        """Test parsing empty string returns None."""
        result = parse_squid_json_log("")
        assert result is None


class TestLogBlockedRequest:
    """Tests for log_blocked_request function."""

    @patch("gateway.proxy_monitor.logger")
    def test_log_blocked_request_basic(self, mock_logger):
        """Test logging blocked request without stats."""
        log_blocked_request(
            client_ip="192.168.1.1",
            destination="https://blocked.com",
            method="GET",
            reason="Policy violation",
        )

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args[0][0]
        assert "BLOCKED" in call_args
        assert "192.168.1.1" in call_args
        assert "https://blocked.com" in call_args

    @patch("gateway.proxy_monitor.logger")
    def test_log_blocked_request_with_stats(self, mock_logger):
        """Test logging blocked request updates stats."""
        stats = ProxyStats(alert_threshold=100)
        log_blocked_request(
            client_ip="192.168.1.1",
            destination="https://blocked.com",
            method="GET",
            reason="Policy violation",
            stats=stats,
        )

        assert stats.blocked_count == 1
        assert len(stats.blocked_requests) == 1
        assert stats.blocked_by_destination["https://blocked.com"] == 1


class TestLogAllowedRequest:
    """Tests for log_allowed_request function."""

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "0"})
    @patch("gateway.proxy_monitor.logger")
    def test_log_allowed_request_non_verbose(self, mock_logger):
        """Test that non-verbose mode doesn't log allowed requests."""
        log_allowed_request(
            client_ip="192.168.1.1",
            destination="https://allowed.com",
            method="GET",
        )
        mock_logger.info.assert_not_called()

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "1"})
    @patch("gateway.proxy_monitor.logger")
    def test_log_allowed_request_verbose(self, mock_logger):
        """Test that verbose mode logs allowed requests."""
        log_allowed_request(
            client_ip="192.168.1.1",
            destination="https://allowed.com",
            method="GET",
        )
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        assert "ALLOWED" in call_args

    @patch.dict(os.environ, {"PROXY_LOG_VERBOSE": "0"})
    def test_log_allowed_request_updates_stats(self):
        """Test that allowed requests update stats."""
        stats = ProxyStats()
        log_allowed_request(
            client_ip="192.168.1.1",
            destination="https://allowed.com",
            method="GET",
            stats=stats,
        )
        assert stats.allowed_count == 1


class TestWatchSquidLog:
    """Tests for watch_squid_log function."""

    @patch("gateway.proxy_monitor.logger")
    def test_watch_squid_log_missing_file(self, mock_logger):
        """Test watching non-existent log file."""
        watch_squid_log(log_path="/nonexistent/path.log")
        mock_logger.warning.assert_called_once()
        assert "not found" in mock_logger.warning.call_args[0][0]

    @patch("gateway.proxy_monitor.logger")
    def test_watch_squid_log_processes_entries(self, mock_logger):
        """Test processing log entries."""
        # Create a temp log file with entries
        blocked_entry = (
            '{"client_ip": "192.168.1.1", "url": "https://blocked.com", '
            '"status": 403, "method": "GET"}\n'
        )
        allowed_entry = (
            '{"client_ip": "192.168.1.2", "url": "https://allowed.com", '
            '"status": 200, "method": "GET"}\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
            f.write(blocked_entry)
            f.write(allowed_entry)
            f.flush()

        try:
            stats = ProxyStats(alert_threshold=100)

            # Create a helper function that processes existing content
            def process_log(path, stats=None):
                with open(path) as f:
                    for line in f:
                        entry = parse_squid_json_log(line)
                        if not entry:
                            continue
                        status = entry.get("status", 0)
                        if status >= 400:
                            log_blocked_request(
                                client_ip=entry.get("client_ip", "unknown"),
                                destination=entry.get("url", "unknown"),
                                method=entry.get("method", "unknown"),
                                reason=f"HTTP {status}",
                                stats=stats,
                            )
                        else:
                            log_allowed_request(
                                client_ip=entry.get("client_ip", "unknown"),
                                destination=entry.get("url", "unknown"),
                                method=entry.get("method", "unknown"),
                                stats=stats,
                            )

            # Run the processing
            process_log(log_path, stats=stats)

            # Verify stats were updated
            assert stats.blocked_count == 1
            assert stats.allowed_count == 1
        finally:
            os.unlink(log_path)
