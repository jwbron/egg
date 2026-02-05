"""Tests for gateway proxy_monitor module."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from proxy_monitor import (
    BlockedRequest,
    ProxyStats,
    log_allowed_request,
    log_blocked_request,
    parse_squid_json_log,
)


class TestBlockedRequest:
    """Tests for BlockedRequest named tuple."""

    def test_create(self):
        """Create a BlockedRequest."""
        ts = datetime.utcnow()
        req = BlockedRequest(
            timestamp=ts,
            client_ip="10.0.0.1",
            destination="evil.com",
            method="GET",
            status_code=403,
            reason="Blocked by policy",
        )
        assert req.client_ip == "10.0.0.1"
        assert req.destination == "evil.com"
        assert req.method == "GET"
        assert req.status_code == 403


class TestProxyStats:
    """Tests for ProxyStats class."""

    def test_initial_state(self):
        """Initial state has zero counts."""
        stats = ProxyStats()
        assert stats.allowed_count == 0
        assert stats.blocked_count == 0
        assert stats.blocked_requests == []

    def test_record_allowed(self):
        """Recording allowed requests increments count."""
        stats = ProxyStats()
        stats.record_allowed()
        stats.record_allowed()
        assert stats.allowed_count == 2

    def test_record_blocked(self):
        """Recording blocked requests increments count."""
        stats = ProxyStats()
        req = BlockedRequest(
            timestamp=datetime.utcnow(),
            client_ip="10.0.0.1",
            destination="blocked.com",
            method="GET",
            status_code=403,
            reason="policy",
        )
        stats.record_blocked(req)
        assert stats.blocked_count == 1
        assert len(stats.blocked_requests) == 1
        assert stats.blocked_by_destination["blocked.com"] == 1

    def test_blocked_by_destination_tracking(self):
        """Multiple blocks to same destination are counted."""
        stats = ProxyStats()
        for _ in range(3):
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination="badsite.com",
                method="GET",
                status_code=403,
                reason="policy",
            )
            stats.record_blocked(req)
        assert stats.blocked_by_destination["badsite.com"] == 3

    def test_get_summary_empty(self):
        """Summary with no requests."""
        stats = ProxyStats()
        summary = stats.get_summary()
        assert summary["allowed_requests"] == 0
        assert summary["blocked_requests"] == 0
        assert summary["block_rate"] == 0

    def test_get_summary_with_data(self):
        """Summary with mixed requests."""
        stats = ProxyStats()
        stats.record_allowed()
        stats.record_allowed()
        stats.record_allowed()
        req = BlockedRequest(
            timestamp=datetime.utcnow(),
            client_ip="10.0.0.1",
            destination="blocked.com",
            method="GET",
            status_code=403,
            reason="policy",
        )
        stats.record_blocked(req)

        summary = stats.get_summary()
        assert summary["allowed_requests"] == 3
        assert summary["blocked_requests"] == 1
        assert summary["block_rate"] == pytest.approx(0.25)

    def test_anomaly_detection_below_threshold(self):
        """No anomaly when below threshold."""
        stats = ProxyStats(alert_threshold=10, window_minutes=5)
        for i in range(5):
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination="site.com",
                method="GET",
                status_code=403,
                reason="policy",
            )
            stats.record_blocked(req)
        # No alert triggered (only 5, threshold is 10)
        assert stats.blocked_count == 5

    def test_anomaly_detection_at_threshold(self):
        """Alert sent when threshold reached."""
        stats = ProxyStats(alert_threshold=3, window_minutes=5)
        with patch.object(stats, "_send_alert") as mock_alert:
            for i in range(3):
                req = BlockedRequest(
                    timestamp=datetime.utcnow(),
                    client_ip="10.0.0.1",
                    destination="site.com",
                    method="GET",
                    status_code=403,
                    reason="policy",
                )
                stats.record_blocked(req)
            mock_alert.assert_called()

    def test_top_blocked_in_summary(self):
        """Summary shows top blocked destinations."""
        stats = ProxyStats()
        for dest in ["a.com", "a.com", "b.com", "a.com"]:
            req = BlockedRequest(
                timestamp=datetime.utcnow(),
                client_ip="10.0.0.1",
                destination=dest,
                method="GET",
                status_code=403,
                reason="policy",
            )
            stats.record_blocked(req)

        summary = stats.get_summary()
        top = summary["top_blocked_destinations"]
        assert top["a.com"] == 3
        assert top["b.com"] == 1

    def test_custom_threshold(self):
        """Custom alert threshold works."""
        stats = ProxyStats(alert_threshold=100, window_minutes=10)
        assert stats.alert_threshold == 100
        assert stats.window_minutes == 10


class TestParseSquidJsonLog:
    """Tests for parse_squid_json_log function."""

    def test_valid_json(self):
        """Parse valid JSON log line."""
        line = json.dumps({"url": "example.com", "status": 200})
        result = parse_squid_json_log(line)
        assert result is not None
        assert result["url"] == "example.com"
        assert result["status"] == 200

    def test_invalid_json(self):
        """Invalid JSON returns None."""
        result = parse_squid_json_log("not json at all")
        assert result is None

    def test_empty_string(self):
        """Empty string returns None."""
        result = parse_squid_json_log("")
        assert result is None

    def test_whitespace_stripped(self):
        """Whitespace is stripped before parsing."""
        line = '  {"key": "value"}  \n'
        result = parse_squid_json_log(line)
        assert result is not None
        assert result["key"] == "value"


class TestLogBlockedRequest:
    """Tests for log_blocked_request function."""

    def test_log_without_stats(self):
        """Log blocked request without stats tracker."""
        # Should not raise
        log_blocked_request(
            client_ip="10.0.0.1",
            destination="blocked.com",
            method="GET",
            reason="Policy violation",
        )

    def test_log_with_stats(self):
        """Log blocked request with stats tracker."""
        stats = ProxyStats()
        log_blocked_request(
            client_ip="10.0.0.1",
            destination="blocked.com",
            method="POST",
            reason="Not allowed",
            stats=stats,
        )
        assert stats.blocked_count == 1
        assert stats.blocked_by_destination["blocked.com"] == 1


class TestLogAllowedRequest:
    """Tests for log_allowed_request function."""

    def test_log_without_stats(self, monkeypatch):
        """Log allowed request without stats."""
        monkeypatch.delenv("PROXY_LOG_VERBOSE", raising=False)
        # Should not raise
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="allowed.com",
            method="GET",
        )

    def test_log_with_stats(self, monkeypatch):
        """Log allowed request with stats tracker."""
        monkeypatch.delenv("PROXY_LOG_VERBOSE", raising=False)
        stats = ProxyStats()
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="allowed.com",
            method="GET",
            stats=stats,
        )
        assert stats.allowed_count == 1

    def test_verbose_logging(self, monkeypatch):
        """Verbose mode logs allowed requests."""
        monkeypatch.setenv("PROXY_LOG_VERBOSE", "1")
        stats = ProxyStats()
        log_allowed_request(
            client_ip="10.0.0.1",
            destination="allowed.com",
            method="GET",
            stats=stats,
        )
        assert stats.allowed_count == 1
