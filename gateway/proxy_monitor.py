"""Proxy monitoring and audit logging for network lockdown.

This module provides utilities for monitoring Squid proxy traffic and
detecting anomalies that might indicate attempted policy violations.
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from shared.egg_logging import get_logger

logger = get_logger("gateway.proxy-monitor")


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
        total = self.allowed_count + self.blocked_count
        return {
            "allowed_requests": self.allowed_count,
            "blocked_requests": self.blocked_count,
            "block_rate": self.blocked_count / total if total > 0 else 0,
            "top_blocked_destinations": dict(
                sorted(
                    self.blocked_by_destination.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:10]
            ),
        }


def parse_squid_json_log(line: str) -> dict[str, Any] | None:
    """Parse a JSON log line from Squid."""
    try:
        entry: dict[str, Any] = json.loads(line.strip())
        return entry
    except json.JSONDecodeError:
        return None


def log_blocked_request(
    client_ip: str,
    destination: str,
    method: str,
    reason: str,
    stats: ProxyStats | None = None,
) -> None:
    """Log a blocked request with structured audit format."""
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
    """Log an allowed request (verbose mode only)."""
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
    """
    log_file = Path(log_path)
    if not log_file.exists():
        logger.warning(f"Squid log not found: {log_path}")
        return

    # Start at end of file
    with open(log_file) as f:
        f.seek(0, 2)

        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue

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


if __name__ == "__main__":
    stats = ProxyStats()
    try:
        watch_squid_log(stats=stats)
    except KeyboardInterrupt:
        print("\nStopped. Summary:")
        print(json.dumps(stats.get_summary(), indent=2))
