"""Proxy monitoring and audit logging for network lockdown.

This module provides utilities for monitoring Squid proxy traffic and
detecting anomalies that might indicate attempted policy violations.

Reference: ADR-Internet-Tool-Access-Lockdown.md
"""

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class BlockedRequest(NamedTuple):
    """Represents a blocked proxy request."""

    timestamp: datetime
    client_ip: str
    destination: str
    method: str
    status_code: int
    reason: str


class ProxyStats:
    """Tracks proxy statistics for monitoring and alerting."""

    def __init__(self, alert_threshold: int = 50, window_minutes: int = 5):
        """Initialize proxy stats tracker.

        Args:
            alert_threshold: Number of blocked requests to trigger alert
            window_minutes: Time window in minutes for anomaly detection
        """
        self.alert_threshold = alert_threshold
        self.window_minutes = window_minutes
        self.blocked_requests: list[BlockedRequest] = []
        self.allowed_count = 0
        self.blocked_count = 0
        self.blocked_by_destination: dict[str, int] = defaultdict(int)

    def record_allowed(self) -> None:
        """Record an allowed request."""
        self.allowed_count += 1

    def record_blocked(self, request: BlockedRequest) -> None:
        """Record a blocked request and check for anomalies."""
        self.blocked_count += 1
        self.blocked_requests.append(request)
        self.blocked_by_destination[request.destination] += 1

        # Check for anomaly
        if self._check_anomaly():
            self._send_alert()

    def _check_anomaly(self) -> bool:
        """Check if blocked request rate exceeds threshold."""
        cutoff = datetime.utcnow() - timedelta(minutes=self.window_minutes)
        recent_blocks = [r for r in self.blocked_requests if r.timestamp > cutoff]

        return len(recent_blocks) >= self.alert_threshold

    def _send_alert(self) -> None:
        """Send security alert for anomalous traffic."""
        alert = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": "security_alert",
            "alert_type": "high_block_rate",
            "message": (
                f"High rate of blocked requests: {self.alert_threshold}+ "
                f"in {self.window_minutes} minutes"
            ),
            "top_blocked_destinations": dict(
                sorted(
                    self.blocked_by_destination.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:10]
            ),
        }
        logger.warning(f"SECURITY ALERT: {json.dumps(alert)}")

    def get_summary(self) -> dict[str, Any]:
        """Get summary statistics."""
        return {
            "allowed_requests": self.allowed_count,
            "blocked_requests": self.blocked_count,
            "block_rate": (
                self.blocked_count / (self.allowed_count + self.blocked_count)
                if (self.allowed_count + self.blocked_count) > 0
                else 0
            ),
            "top_blocked_destinations": dict(
                sorted(
                    self.blocked_by_destination.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:10]
            ),
        }


def parse_squid_json_log(line: str) -> dict[str, Any] | None:
    """Parse a JSON log line from Squid.

    Args:
        line: Raw log line from Squid access log

    Returns:
        Parsed log entry dict or None if parsing fails
    """
    try:
        result = json.loads(line.strip())
        if isinstance(result, dict):
            return result
        return None
    except json.JSONDecodeError:
        return None


def log_blocked_request(
    client_ip: str,
    destination: str,
    method: str,
    reason: str,
    stats: ProxyStats | None = None,
) -> None:
    """Log a blocked request with structured audit format.

    Args:
        client_ip: Source IP of the request
        destination: Target URL/domain
        method: HTTP method (GET, POST, CONNECT, etc.)
        reason: Why the request was blocked
        stats: Optional ProxyStats instance to update
    """
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event_type": "proxy_request_blocked",
        "client_ip": client_ip,
        "destination": destination,
        "method": method,
        "reason": reason,
        "action": "blocked",
        "source": "squid_proxy",
    }

    logger.warning(f"BLOCKED: {json.dumps(entry)}")

    if stats:
        request = BlockedRequest(
            timestamp=datetime.utcnow(),
            client_ip=client_ip,
            destination=destination,
            method=method,
            status_code=403,
            reason=reason,
        )
        stats.record_blocked(request)


def log_allowed_request(
    client_ip: str,
    destination: str,
    method: str,
    stats: ProxyStats | None = None,
) -> None:
    """Log an allowed request (verbose mode only).

    Args:
        client_ip: Source IP of the request
        destination: Target URL/domain
        method: HTTP method
        stats: Optional ProxyStats instance to update
    """
    if os.environ.get("PROXY_LOG_VERBOSE", "0") == "1":
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": "proxy_request_allowed",
            "client_ip": client_ip,
            "destination": destination,
            "method": method,
            "action": "allowed",
        }
        logger.info(f"ALLOWED: {json.dumps(entry)}")

    if stats:
        stats.record_allowed()


def watch_squid_log(
    log_path: str = "/var/log/squid/access.log",
    stats: ProxyStats | None = None,
) -> None:
    """Watch Squid access log and emit structured events.

    This function tails the Squid access log and emits structured
    audit events for blocked requests.

    Args:
        log_path: Path to Squid access log
        stats: Optional ProxyStats instance for tracking
    """
    import time

    log_file = Path(log_path)
    if not log_file.exists():
        logger.warning(f"Squid log not found: {log_path}")
        return

    # Start at end of file
    with open(log_file) as f:
        f.seek(0, 2)  # Seek to end

        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue

            entry = parse_squid_json_log(line)
            if not entry:
                continue

            # Check if request was blocked (4xx status)
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


if __name__ == "__main__":
    # Run as standalone log watcher
    stats = ProxyStats()
    try:
        watch_squid_log(stats=stats)
    except KeyboardInterrupt:
        print("\nStopped. Summary:")
        print(json.dumps(stats.get_summary(), indent=2))
